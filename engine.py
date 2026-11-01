#!/usr/bin/env python3
"""Freeze + near-silence video cutter engine."""

APP_VERSION = "1.0"

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np

RMS_WINDOW = 0.10
AUDIO_SR = 48000

LogFn = Callable[[str], None]
ProgressFn = Callable[[float, str], None]


@dataclass
class CutParams:
    freeze_noise: float = 0.002
    freeze_duration: float = 0.50
    silence_db: float = -45.0
    silence_duration: float = 0.50
    min_cut_duration: float = 0.50
    merge_gap: float = 0.40
    cut_pad: float = 0.04
    min_keep_duration: float = 0.12


@dataclass
class CutResult:
    duration: float
    freeze: list[tuple[float, float]]
    quiet: list[tuple[float, float]]
    remove: list[tuple[float, float]]
    keep: list[tuple[float, float]]
    params: CutParams
    report_text: str = ""

    @property
    def freeze_total(self) -> float:
        return sum(e - s for s, e in self.freeze)

    @property
    def quiet_total(self) -> float:
        return sum(e - s for s, e in self.quiet)

    @property
    def remove_total(self) -> float:
        return sum(e - s for s, e in self.remove)

    @property
    def keep_total(self) -> float:
        return sum(e - s for s, e in self.keep)


def find_ffmpeg() -> Path | None:
    env = os.environ.get("STALL_CUTTER_FFMPEG") or os.environ.get("FFMPEG")
    candidates: list[Path] = []
    if env:
        candidates.append(Path(env))
    which = shutil.which("ffmpeg")
    if which:
        candidates.append(Path(which))
    candidates.extend(
        [
            Path(r"D:\Applications\FormatFactory-5.21.0.0\ffmpeg.exe"),
            Path(r"D:\Applications\FormatFactory-5.21.0.0\FormatFactory\ffmpeg.exe"),
            Path(r"C:\ffmpeg\bin\ffmpeg.exe"),
            Path(r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"),
            Path.home() / "ffmpeg" / "bin" / "ffmpeg.exe",
            Path.home() / "scoop" / "shims" / "ffmpeg.exe",
        ]
    )
    seen: set[str] = set()
    for p in candidates:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        if p.is_file():
            return p
    return None


def fmt_ts(t: float) -> str:
    if t < 0:
        t = 0.0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def merge_intervals(
    intervals: list[tuple[float, float]], gap: float
) -> list[tuple[float, float]]:
    if not intervals:
        return []
    intervals = sorted((max(0.0, s), e) for s, e in intervals if e > s)
    out = [[intervals[0][0], intervals[0][1]]]
    for s, e in intervals[1:]:
        if s <= out[-1][1] + gap:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def invert_intervals(
    remove: list[tuple[float, float]], duration: float, min_keep: float
) -> list[tuple[float, float]]:
    keep: list[tuple[float, float]] = []
    t = 0.0
    for s, e in remove:
        if s > t + 1e-3:
            keep.append((t, s))
        t = max(t, e)
    if duration > t + 1e-3:
        keep.append((t, duration))
    return [(s, e) for s, e in keep if e - s >= min_keep]


def mask_to_intervals(
    mask: np.ndarray, win: float, min_dur: float
) -> list[tuple[float, float]]:
    padded = np.concatenate([[False], mask.astype(bool), [False]])
    diff = np.diff(padded.astype(np.int8))
    starts = np.where(diff == 1)[0]
    ends = np.where(diff == -1)[0]
    out: list[tuple[float, float]] = []
    for s, e in zip(starts, ends):
        if (e - s) * win >= min_dur:
            out.append((s * win, e * win))
    return out


def parse_tagged_intervals(
    text: str,
    start_re: re.Pattern[str],
    end_re: re.Pattern[str],
    duration: float,
) -> list[tuple[float, float]]:
    events: list[tuple[float, str]] = []
    for m in start_re.finditer(text):
        events.append((float(m.group(1)), "start"))
    for m in end_re.finditer(text):
        events.append((float(m.group(1)), "end"))
    events.sort(key=lambda x: (x[0], 0 if x[1] == "start" else 1))
    intervals: list[tuple[float, float]] = []
    open_start: float | None = None
    for t, kind in events:
        if kind == "start":
            if open_start is None:
                open_start = t
        elif open_start is not None:
            if t > open_start:
                intervals.append((open_start, t))
            open_start = None
    if open_start is not None and duration > open_start:
        intervals.append((open_start, duration))
    return merge_intervals(intervals, gap=0.0)


def build_filter(keep: list[tuple[float, float]]) -> str:
    parts: list[str] = []
    concat_in: list[str] = []
    for i, (s, e) in enumerate(keep):
        parts.append(f"[0:v]trim=start={s:.4f}:end={e:.4f},setpts=PTS-STARTPTS[v{i}]")
        parts.append(f"[0:a]atrim=start={s:.4f}:end={e:.4f},asetpts=PTS-STARTPTS[a{i}]")
        concat_in.append(f"[v{i}][a{i}]")
    parts.append("".join(concat_in) + f"concat=n={len(keep)}:v=1:a=1[outv][outa]")
    return ";\n".join(parts)


def write_report(result: CutResult) -> str:
    p = result.params
    lines = [
        f"duration: {fmt_ts(result.duration)} ({result.duration:.3f}s)",
        f"freeze_noise={p.freeze_noise} freeze_d={p.freeze_duration}s",
        f"silence_rms_db={p.silence_db} silence_d={p.silence_duration}s",
        f"min_cut={p.min_cut_duration}s merge_gap={p.merge_gap}s pad={p.cut_pad}s",
        "",
        f"freeze segments: {len(result.freeze)}, total {fmt_ts(result.freeze_total)}",
        f"quiet segments (RMS < {p.silence_db} dB): {len(result.quiet)}, total {fmt_ts(result.quiet_total)}",
        f"cut segments (freeze AND quiet): {len(result.remove)}, total {fmt_ts(result.remove_total)}",
        f"keep segments: {len(result.keep)}, total {fmt_ts(result.keep_total)}",
        "",
        "=== CUT ===",
    ]
    for s, e in result.remove:
        lines.append(f"{fmt_ts(s)} - {fmt_ts(e)}  ({e - s:.3f}s)")
    lines.append("")
    lines.append("=== KEEP ===")
    for s, e in result.keep:
        lines.append(f"{fmt_ts(s)} - {fmt_ts(e)}  ({e - s:.3f}s)")
    return "\n".join(lines) + "\n"


class StallCutter:
    def __init__(
        self,
        ffmpeg: Path,
        log: LogFn | None = None,
        progress: ProgressFn | None = None,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.ffmpeg = Path(ffmpeg)
        self.log = log or (lambda _m: None)
        self.progress = progress or (lambda _p, _m: None)
        self.cancel_event = cancel_event or threading.Event()
        self._proc: subprocess.Popen[str] | None = None
        self._video_key = ""
        self._duration = 0.0
        self._freeze_key = ""
        self._freeze: list[tuple[float, float]] = []
        self._rms_key = ""
        self._times = np.array([])
        self._db = np.array([])
        self._no_audio = False

    def _check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise RuntimeError("已取消")

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = str(self.ffmpeg.parent) + os.pathsep + env.get("PATH", "")
        env["AV_LOG_FORCE_NOCOLOR"] = "1"
        return env

    def _cache_dir(self, video: Path) -> Path:
        st = video.stat()
        blob = f"{video.resolve()}|{st.st_mtime_ns}|{st.st_size}".encode("utf-8")
        digest = hashlib.sha1(blob).hexdigest()[:16]
        path = Path(tempfile.gettempdir()) / "stall_cutter" / digest
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _run_ffmpeg(
        self,
        args: list[str],
        duration: float | None,
        phase: str,
        require_ok: bool = True,
    ) -> str:
        self._check_cancel()
        cmd = [
            str(self.ffmpeg),
            "-hide_banner",
            "-nostats",
            "-progress",
            "pipe:1",
            *args,
        ]
        self.log(phase)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env(),
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._proc = proc
        stderr_chunks: list[str] = []

        def read_err() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                stderr_chunks.append(line)

        t = threading.Thread(target=read_err, daemon=True)
        t.start()
        assert proc.stdout is not None
        last_ms = 0
        try:
            for raw in proc.stdout:
                self._check_cancel()
                line = raw.strip()
                if line.startswith("out_time_ms="):
                    try:
                        last_ms = int(line.split("=", 1)[1])
                    except ValueError:
                        continue
                    if duration and duration > 0:
                        pct = max(0.0, min(99.0, (last_ms / 1000.0) / duration * 100.0))
                        self.progress(pct, f"{phase}  {fmt_ts(last_ms / 1000.0)}")
                elif line.startswith("out_time=") and duration:
                    ts = line.split("=", 1)[1].strip()
                    self.progress(-1, f"{phase}  {ts}")
        except RuntimeError:
            proc.kill()
            proc.wait()
            raise
        code = proc.wait()
        t.join(timeout=2)
        self._proc = None
        err = "".join(stderr_chunks)
        if self.cancel_event.is_set():
            raise RuntimeError("已取消")
        failed = code != 0
        fatal = "Error opening" in err or "No such file" in err or "Invalid argument" in err
        if failed and (require_ok or fatal):
            raise RuntimeError(f"ffmpeg 失败 ({code})\n{err[-2500:]}")
        return err

    def stop(self) -> None:
        self.cancel_event.set()
        proc = self._proc
        if proc and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def probe_duration(self, video: Path) -> float:
        work = self._cache_dir(video)
        return self._probe_raw(video, work / "probe.log")

    def _probe_raw(self, video: Path, log_path: Path) -> float:
        cmd = [str(self.ffmpeg), "-hide_banner", "-i", str(video)]
        proc = subprocess.run(
            cmd,
            env=self._env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        text = proc.stderr or ""
        log_path.write_text(text, encoding="utf-8", errors="replace")
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", text)
        if not m:
            raise RuntimeError("无法读取视频时长")
        h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
        self._no_audio = "Audio:" not in text
        return h * 3600 + mi * 60 + s

    def analyze(self, video: Path, params: CutParams) -> CutResult:
        video = video.resolve()
        if not video.is_file():
            raise FileNotFoundError(str(video))
        work = self._cache_dir(video)
        key = f"{video}|{video.stat().st_mtime_ns}|{video.stat().st_size}"
        if self._video_key != key:
            self._duration = self._probe_raw(video, work / "probe.log")
            self._video_key = key
            self._freeze_key = ""
            self._rms_key = ""
        self.log(f"视频时长 {fmt_ts(self._duration)}")
        self._check_cancel()

        freeze_key = f"{key}|{params.freeze_noise}|{params.freeze_duration}"
        detect_log = (
            work / f"detect_n{params.freeze_noise:g}_d{params.freeze_duration:g}.log"
        )
        if self._freeze_key != freeze_key:
            self.progress(2, "检测画面卡顿")
            self.log(
                f"画面检测：冻结 ≥ {params.freeze_duration:.3f}s，噪声容差 {params.freeze_noise}"
            )
            if detect_log.is_file() and detect_log.stat().st_size > 0:
                self.log("使用已缓存的画面检测结果")
                err = detect_log.read_text(encoding="utf-8", errors="replace")
            else:
                err = self._run_ffmpeg(
                    [
                        "-i",
                        str(video),
                        "-vf",
                        f"scale=320:-2,freezedetect=n={params.freeze_noise}:d={params.freeze_duration}",
                        "-an",
                        "-f",
                        "null",
                        "-",
                    ],
                    duration=self._duration,
                    phase="检测卡顿",
                    require_ok=False,
                )
                detect_log.write_text(err, encoding="utf-8", errors="replace")
            self._freeze = parse_tagged_intervals(
                err,
                re.compile(r"freeze_start:\s*([0-9.]+)"),
                re.compile(r"freeze_end:\s*([0-9.]+)"),
                self._duration,
            )
            self._freeze_key = freeze_key
        self.log(f"画面卡顿 {len(self._freeze)} 段，共 {fmt_ts(sum(e - s for s, e in self._freeze))}")

        if self._no_audio:
            self.log("没有音轨，静音条件视为全程满足")
            nwin = int(self._duration / RMS_WINDOW)
            self._times = (np.arange(max(nwin, 1)) + 0.5) * RMS_WINDOW
            self._db = np.full(len(self._times), -90.0)
            self._rms_key = key + "|noaudio"
        elif self._rms_key != key:
            self.progress(40, "分析音量")
            raw = work / "audio_48k_mono.s16le"
            if not (raw.is_file() and raw.stat().st_size > 1000):
                self.log("提取音频并计算 RMS")
                self._run_ffmpeg(
                    [
                        "-y",
                        "-i",
                        str(video),
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        str(AUDIO_SR),
                        "-f",
                        "s16le",
                        str(raw),
                    ],
                    duration=self._duration,
                    phase="提取音频",
                )
            else:
                self.log("使用已缓存的音频 RMS")
            pcm = np.fromfile(raw, dtype=np.int16).astype(np.float64) / 32768.0
            hop = int(AUDIO_SR * RMS_WINDOW)
            nwin = len(pcm) // hop
            if nwin < 1:
                raise RuntimeError("音频太短，无法计算音量")
            frames = pcm[: nwin * hop].reshape(nwin, hop)
            rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)
            self._db = 20.0 * np.log10(rms)
            self._times = (np.arange(nwin) + 0.5) * RMS_WINDOW
            self._rms_key = key

        times, db = self._times, self._db
        freeze_mask = np.zeros(len(db), dtype=bool)
        for s, e in self._freeze:
            freeze_mask |= (times >= s) & (times < e)
        quiet_mask = db < params.silence_db
        quiet_min = max(params.silence_duration, 0.05)
        quiet = mask_to_intervals(quiet_mask, RMS_WINDOW, quiet_min)
        both_min = max(params.min_cut_duration, 0.05)
        both = mask_to_intervals(freeze_mask & quiet_mask, RMS_WINDOW, both_min)
        remove = merge_intervals(
            [
                (max(0.0, s - params.cut_pad), min(self._duration, e + params.cut_pad))
                for s, e in both
            ],
            gap=params.merge_gap,
        )
        remove = [(s, e) for s, e in remove if e - s >= params.min_cut_duration]
        keep = invert_intervals(remove, self._duration, params.min_keep_duration)
        result = CutResult(
            duration=self._duration,
            freeze=self._freeze,
            quiet=quiet,
            remove=remove,
            keep=keep,
            params=params,
        )
        result.report_text = write_report(result)
        self.progress(100, "分析完成")
        return result

    def encode(self, video: Path, keep: list[tuple[float, float]], output: Path) -> None:
        if not keep:
            raise RuntimeError("没有可保留的片段")
        video = video.resolve()
        output = output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        work = self._cache_dir(video)
        script = work / "filter.txt"
        script.write_text(build_filter(keep), encoding="utf-8")
        keep_total = sum(e - s for s, e in keep)
        self.log(f"开始编码 {len(keep)} 段 → {output.name}")
        self._run_ffmpeg(
            [
                "-y",
                "-i",
                str(video),
                "-filter_complex_script",
                str(script).replace("\\", "/"),
                "-map",
                "[outv]",
                "-map",
                "[outa]",
                "-fps_mode",
                "vfr",
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "20",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-b:a",
                "96k",
                "-movflags",
                "+faststart",
                str(output),
            ],
            duration=keep_total,
            phase="编码",
        )
        if not output.is_file() or output.stat().st_size < 1000:
            raise RuntimeError("编码完成但输出文件无效")
        self.progress(100, "编码完成")
        self.log(f"完成：{output}  ({output.stat().st_size / 1024 / 1024:.1f} MB)")
