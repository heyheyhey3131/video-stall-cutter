#!/usr/bin/env python3
"""卡顿 + 静音裁剪器图形界面。"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
import tkinter as tk

APP_DIR = Path(__file__).resolve().parent
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

from engine import APP_VERSION, CutParams, CutResult, StallCutter, find_ffmpeg, fmt_ts

LEVEL_NAMES = {
    1: "最严",
    2: "很严",
    3: "较严",
    4: "偏严",
    5: "标准",
    6: "适中",
    7: "偏松",
    8: "较松",
    9: "很松",
    10: "最松",
}

# 1 = 严格（更容易判定为卡顿/静音，剪得更多）
VIDEO_PRESETS = {
    1: {"freeze_duration": 0.25, "freeze_noise": 0.0010},
    2: {"freeze_duration": 0.32, "freeze_noise": 0.0013},
    3: {"freeze_duration": 0.40, "freeze_noise": 0.0016},
    4: {"freeze_duration": 0.48, "freeze_noise": 0.0020},
    5: {"freeze_duration": 0.60, "freeze_noise": 0.0025},
    6: {"freeze_duration": 0.80, "freeze_noise": 0.0032},
    7: {"freeze_duration": 1.10, "freeze_noise": 0.0045},
    8: {"freeze_duration": 1.50, "freeze_noise": 0.0060},
    9: {"freeze_duration": 2.20, "freeze_noise": 0.0090},
    10: {"freeze_duration": 3.00, "freeze_noise": 0.0150},
}

AUDIO_PRESETS = {
    1: {"silence_db": -30.0, "silence_duration": 0.25},
    2: {"silence_db": -33.0, "silence_duration": 0.32},
    3: {"silence_db": -36.0, "silence_duration": 0.40},
    4: {"silence_db": -39.0, "silence_duration": 0.45},
    5: {"silence_db": -42.0, "silence_duration": 0.50},
    6: {"silence_db": -45.0, "silence_duration": 0.60},
    7: {"silence_db": -48.0, "silence_duration": 0.80},
    8: {"silence_db": -51.0, "silence_duration": 1.10},
    9: {"silence_db": -54.0, "silence_duration": 1.50},
    10: {"silence_db": -58.0, "silence_duration": 2.20},
}

VIDEO_FILTERS = [
    (
        "视频文件",
        "*.mp4 *.mkv *.mov *.avi *.flv *.ts *.webm *.m4v *.mpg *.mpeg",
    ),
    ("全部文件", "*.*"),
]


def enable_dpi() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


class StallCutApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(f"卡顿静音裁剪器 V{APP_VERSION}")
        self.root.geometry("980x820")
        self.root.minsize(860, 700)

        self.ffmpeg = find_ffmpeg()
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.cutter: StallCutter | None = None
        self.result: CutResult | None = None
        self._syncing = False
        self._busy = False

        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.ffmpeg_var = tk.StringVar(value=str(self.ffmpeg) if self.ffmpeg else "未找到 ffmpeg")
        self.video_level = tk.IntVar(value=5)
        self.audio_level = tk.IntVar(value=6)
        self.video_level_label = tk.StringVar()
        self.audio_level_label = tk.StringVar()
        self.freeze_dur = tk.StringVar()
        self.freeze_noise = tk.StringVar()
        self.silence_db = tk.StringVar()
        self.silence_dur = tk.StringVar()
        self.min_cut = tk.StringVar(value="0.50")
        self.merge_gap = tk.StringVar(value="0.40")
        self.cut_pad = tk.StringVar(value="0.04")
        self.status_var = tk.StringVar(value="选择视频后，先分析预览，再裁剪")
        self.summary_var = tk.StringVar(value="尚未分析")
        self.progress_var = tk.DoubleVar(value=0)

        self._build_style()
        self._build_ui()
        self._apply_video_preset(5)
        self._apply_audio_preset(6)
        self._set_busy(False)

        if not self.ffmpeg:
            messagebox.showwarning(
                "未找到 ffmpeg",
                "没有在常见位置找到 ffmpeg。\n请点击路径旁的“浏览”手动指定 ffmpeg.exe。",
            )

    def _build_style(self) -> None:
        self.font = ("Microsoft YaHei UI", 10)
        self.font_small = ("Microsoft YaHei UI", 9)
        self.font_title = ("Microsoft YaHei UI", 18, "bold")
        self.font_sub = ("Microsoft YaHei UI", 10)
        self.root.option_add("*Font", self.font)
        style = ttk.Style()
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Header.TFrame", background="#1f3a5f")
        style.configure(
            "Header.TLabel",
            background="#1f3a5f",
            foreground="#ffffff",
            font=self.font_title,
        )
        style.configure(
            "SubHeader.TLabel",
            background="#1f3a5f",
            foreground="#d6e4f5",
            font=self.font_sub,
        )
        style.configure("Card.TLabelframe", padding=10)
        style.configure("Card.TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TProgressbar", thickness=12)

    def _build_ui(self) -> None:
        header = ttk.Frame(self.root, style="Header.TFrame")
        header.pack(fill="x")
        inner = ttk.Frame(header, style="Header.TFrame")
        inner.pack(fill="x", padx=20, pady=14)
        title_row = ttk.Frame(inner, style="Header.TFrame")
        title_row.pack(fill="x")
        ttk.Label(title_row, text="卡顿静音裁剪器", style="Header.TLabel").pack(side="left")
        ttk.Label(
            title_row,
            text=f"V{APP_VERSION}",
            style="SubHeader.TLabel",
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(side="left", padx=(10, 0), pady=(6, 0))
        ttk.Label(
            inner,
            text="剪掉画面卡住且音量很小的片段 · 画面 / 声音可分开调节",
            style="SubHeader.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        body = ttk.Frame(self.root, padding=14)
        body.pack(fill="both", expand=True)

        files = ttk.LabelFrame(body, text="文件", style="Card.TLabelframe")
        files.pack(fill="x")
        self._row_path(files, 0, "输入视频", self.input_var, self._browse_input)
        self._row_path(files, 1, "输出视频", self.output_var, self._browse_output)
        self._row_path(files, 2, "ffmpeg", self.ffmpeg_var, self._browse_ffmpeg)

        video = ttk.LabelFrame(body, text="画面卡顿", style="Card.TLabelframe")
        video.pack(fill="x", pady=(10, 0))
        self._build_slider_row(
            video,
            level_var=self.video_level,
            label_var=self.video_level_label,
            on_slide=self._on_video_slide,
        )
        hint = ttk.Label(
            video,
            text="越严格：越短的画面停住也会被判为卡顿。越宽松：只剪更长、更死的定格。",
            foreground="#555555",
            font=self.font_small,
        )
        hint.pack(anchor="w", pady=(0, 6))
        man = ttk.Frame(video)
        man.pack(fill="x")
        self._labeled_entry(man, 0, "冻结时长（秒）", self.freeze_dur, 10)
        self._labeled_entry(man, 1, "噪声容差", self.freeze_noise, 10)
        ttk.Label(
            man,
            text="可直接改数字；改完后档位显示为“自定义”",
            foreground="#555555",
            font=self.font_small,
        ).grid(row=0, column=4, padx=(16, 0), sticky="w")
        self.freeze_dur.trace_add("write", lambda *_: self._mark_video_custom())
        self.freeze_noise.trace_add("write", lambda *_: self._mark_video_custom())

        audio = ttk.LabelFrame(body, text="音频静音", style="Card.TLabelframe")
        audio.pack(fill="x", pady=(10, 0))
        self._build_slider_row(
            audio,
            level_var=self.audio_level,
            label_var=self.audio_level_label,
            on_slide=self._on_audio_slide,
        )
        ttk.Label(
            audio,
            text="越严格：相对更大的声音也会当成“太轻”。越宽松：只剪更接近无声的部分。",
            foreground="#555555",
            font=self.font_small,
        ).pack(anchor="w", pady=(0, 6))
        aman = ttk.Frame(audio)
        aman.pack(fill="x")
        self._labeled_entry(aman, 0, "音量阈值（dB）", self.silence_db, 10)
        self._labeled_entry(aman, 1, "最短静音（秒）", self.silence_dur, 10)
        ttk.Label(
            aman,
            text="音量按 100ms RMS 计算。画面卡住且音量低于阈值才会剪。",
            foreground="#555555",
            font=self.font_small,
        ).grid(row=0, column=4, padx=(16, 0), sticky="w")
        self.silence_db.trace_add("write", lambda *_: self._mark_audio_custom())
        self.silence_dur.trace_add("write", lambda *_: self._mark_audio_custom())

        adv = ttk.LabelFrame(body, text="高级", style="Card.TLabelframe")
        adv.pack(fill="x", pady=(10, 0))
        arow = ttk.Frame(adv)
        arow.pack(fill="x")
        self._labeled_entry(arow, 0, "最短裁剪（秒）", self.min_cut, 8)
        self._labeled_entry(arow, 1, "合并间隙（秒）", self.merge_gap, 8)
        self._labeled_entry(arow, 2, "边界填充（秒）", self.cut_pad, 8)

        btns = ttk.Frame(body)
        btns.pack(fill="x", pady=(12, 0))
        self.btn_analyze = tk.Button(
            btns,
            text="分析预览",
            command=self._on_analyze,
            font=self.font,
            padx=16,
            pady=6,
            relief="flat",
            bg="#e8eef6",
            fg="#1f3a5f",
            activebackground="#d5e0ee",
        )
        self.btn_analyze.pack(side="left")
        self.btn_cut = tk.Button(
            btns,
            text="开始裁剪",
            command=self._on_cut,
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=20,
            pady=6,
            relief="flat",
            bg="#1f6feb",
            fg="#ffffff",
            activebackground="#1a5fd0",
            activeforeground="#ffffff",
        )
        self.btn_cut.pack(side="left", padx=(10, 0))
        self.btn_stop = tk.Button(
            btns,
            text="停止",
            command=self._on_stop,
            font=self.font,
            padx=16,
            pady=6,
            relief="flat",
            bg="#f3d6d4",
            fg="#8a1f11",
            activebackground="#e8c0bd",
        )
        self.btn_stop.pack(side="left", padx=(10, 0))
        self.btn_open = tk.Button(
            btns,
            text="打开输出目录",
            command=self._open_output_dir,
            font=self.font,
            padx=12,
            pady=6,
            relief="flat",
            bg="#e8eef6",
            fg="#1f3a5f",
        )
        self.btn_open.pack(side="right")

        self.progress = ttk.Progressbar(
            body, maximum=100, variable=self.progress_var, mode="determinate"
        )
        self.progress.pack(fill="x", pady=(10, 4))
        ttk.Label(body, textvariable=self.status_var, foreground="#1f3a5f").pack(anchor="w")
        ttk.Label(body, textvariable=self.summary_var, foreground="#333333").pack(
            anchor="w", pady=(2, 6)
        )

        log_frame = ttk.LabelFrame(body, text="日志 / 切分预览", style="Card.TLabelframe")
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(
            log_frame,
            height=12,
            wrap="word",
            font=("Consolas", 9),
            bg="#f7f9fc",
            relief="flat",
            state="disabled",
        )
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def _row_path(self, parent: ttk.LabelFrame, row: int, label: str, var: tk.StringVar, cmd) -> None:
        ttk.Label(parent, text=label, width=8).grid(row=row, column=0, sticky="w", pady=3)
        ent = ttk.Entry(parent, textvariable=var)
        ent.grid(row=row, column=1, sticky="ew", padx=8, pady=3)
        ttk.Button(parent, text="浏览", command=cmd, width=8).grid(row=row, column=2, pady=3)
        parent.columnconfigure(1, weight=1)

    def _build_slider_row(self, parent, level_var, label_var, on_slide) -> None:
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(0, 4))
        ttk.Label(row, text="严格", width=4).pack(side="left")
        scale = ttk.Scale(
            row,
            from_=1,
            to=10,
            variable=level_var,
            command=on_slide,
            orient="horizontal",
        )
        scale.pack(side="left", fill="x", expand=True, padx=8)
        ttk.Label(row, text="宽松", width=4).pack(side="left")
        ttk.Label(row, textvariable=label_var, width=16, foreground="#1f6feb").pack(
            side="left", padx=(8, 0)
        )

    def _labeled_entry(
        self, parent: ttk.Frame, col: int, label: str, var: tk.StringVar, width: int
    ) -> None:
        box = ttk.Frame(parent)
        box.grid(row=0, column=col * 2, columnspan=2, sticky="w", padx=(0, 18))
        ttk.Label(box, text=label).pack(side="left")
        ttk.Entry(box, textvariable=var, width=width).pack(side="left", padx=(6, 0))

    def _set_level_label(self, label_var: tk.StringVar, level: int, custom: bool = False) -> None:
        if custom:
            label_var.set("自定义")
        else:
            label_var.set(f"第 {level} 档 · {LEVEL_NAMES[level]}")

    def _on_video_slide(self, _val=None) -> None:
        if self._syncing:
            return
        level = int(round(float(self.video_level.get())))
        self._apply_video_preset(level)

    def _on_audio_slide(self, _val=None) -> None:
        if self._syncing:
            return
        level = int(round(float(self.audio_level.get())))
        self._apply_audio_preset(level)

    def _apply_video_preset(self, level: int) -> None:
        level = min(10, max(1, level))
        preset = VIDEO_PRESETS[level]
        self._syncing = True
        try:
            self.video_level.set(level)
            self.freeze_dur.set(f"{preset['freeze_duration']:.2f}")
            self.freeze_noise.set(f"{preset['freeze_noise']:.4f}")
            self._set_level_label(self.video_level_label, level)
            self._refresh_min_cut_from_durations()
        finally:
            self._syncing = False

    def _apply_audio_preset(self, level: int) -> None:
        level = min(10, max(1, level))
        preset = AUDIO_PRESETS[level]
        self._syncing = True
        try:
            self.audio_level.set(level)
            self.silence_db.set(f"{preset['silence_db']:.1f}")
            self.silence_dur.set(f"{preset['silence_duration']:.2f}")
            self._set_level_label(self.audio_level_label, level)
            self._refresh_min_cut_from_durations()
        finally:
            self._syncing = False

    def _refresh_min_cut_from_durations(self) -> None:
        try:
            fd = float(self.freeze_dur.get())
            sd = float(self.silence_dur.get())
            self.min_cut.set(f"{min(fd, sd):.2f}")
        except ValueError:
            pass

    def _mark_video_custom(self) -> None:
        if self._syncing:
            return
        self._set_level_label(self.video_level_label, int(self.video_level.get()), custom=True)

    def _mark_audio_custom(self) -> None:
        if self._syncing:
            return
        self._set_level_label(self.audio_level_label, int(self.audio_level.get()), custom=True)

    def _browse_input(self) -> None:
        path = filedialog.askopenfilename(title="选择视频", filetypes=VIDEO_FILTERS)
        if not path:
            return
        self.input_var.set(path)
        src = Path(path)
        self.output_var.set(str(src.with_name(src.stem + "_cut.mp4")))
        self.result = None
        self.summary_var.set("尚未分析")

    def _browse_output(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存为",
            defaultextension=".mp4",
            filetypes=[("MP4", "*.mp4"), ("全部文件", "*.*")],
        )
        if path:
            self.output_var.set(path)

    def _browse_ffmpeg(self) -> None:
        path = filedialog.askopenfilename(
            title="选择 ffmpeg.exe",
            filetypes=[("ffmpeg", "ffmpeg.exe"), ("全部文件", "*.*")],
        )
        if path:
            self.ffmpeg = Path(path)
            self.ffmpeg_var.set(path)

    def _append_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def ui(self, fn) -> None:
        self.root.after(0, fn)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        state = "disabled" if busy else "normal"
        self.btn_analyze.configure(state=state)
        self.btn_cut.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    def _read_params(self) -> CutParams:
        try:
            freeze_duration = float(self.freeze_dur.get())
            freeze_noise = float(self.freeze_noise.get())
            silence_db = float(self.silence_db.get())
            silence_duration = float(self.silence_dur.get())
            min_cut = float(self.min_cut.get())
            merge_gap = float(self.merge_gap.get())
            cut_pad = float(self.cut_pad.get())
        except ValueError as exc:
            raise ValueError("裁剪条件必须是数字") from exc
        if freeze_duration <= 0 or silence_duration <= 0 or min_cut <= 0:
            raise ValueError("时长必须大于 0")
        if freeze_noise <= 0:
            raise ValueError("噪声容差必须大于 0")
        if silence_db >= 0:
            raise ValueError("音量阈值一般是负数，例如 -45")
        return CutParams(
            freeze_noise=freeze_noise,
            freeze_duration=freeze_duration,
            silence_db=silence_db,
            silence_duration=silence_duration,
            min_cut_duration=min_cut,
            merge_gap=max(0.0, merge_gap),
            cut_pad=max(0.0, cut_pad),
        )

    def _validate_paths(self, need_output: bool) -> tuple[Path, Path]:
        src = Path(self.input_var.get().strip())
        if not src.is_file():
            raise ValueError("请先选择有效的输入视频")
        out_text = self.output_var.get().strip()
        if not out_text:
            out_text = str(src.with_name(src.stem + "_cut.mp4"))
            self.output_var.set(out_text)
        out = Path(out_text)
        if need_output and out.resolve() == src.resolve():
            raise ValueError("输出文件不能和输入文件相同")
        ff_text = self.ffmpeg_var.get().strip()
        ffmpeg = Path(ff_text) if ff_text and Path(ff_text).is_file() else find_ffmpeg()
        if not ffmpeg or not Path(ffmpeg).is_file():
            raise ValueError("找不到 ffmpeg.exe")
        self.ffmpeg = Path(ffmpeg)
        self.ffmpeg_var.set(str(self.ffmpeg))
        return src, out

    def _on_analyze(self) -> None:
        self._start_job(do_encode=False)

    def _on_cut(self) -> None:
        self._start_job(do_encode=True)

    def _on_stop(self) -> None:
        self.cancel_event.set()
        if self.cutter:
            self.cutter.stop()
        self.status_var.set("正在停止…")

    def _open_output_dir(self) -> None:
        path = self.output_var.get().strip() or self.input_var.get().strip()
        if not path:
            return
        folder = Path(path).resolve().parent
        if folder.is_dir():
            os.startfile(folder)  # type: ignore[attr-defined]

    def _start_job(self, do_encode: bool) -> None:
        if self._busy:
            return
        try:
            src, out = self._validate_paths(need_output=do_encode)
            params = self._read_params()
        except ValueError as exc:
            messagebox.showerror("参数错误", str(exc))
            return
        if do_encode and out.exists():
            if not messagebox.askyesno("覆盖确认", f"输出文件已存在，是否覆盖？\n{out}"):
                return
        self.cancel_event = threading.Event()
        self.result = None if not do_encode else self.result
        self._set_busy(True)
        self.progress_var.set(0)
        self.status_var.set("准备中…")
        self._append_log("-" * 48)
        self._append_log(
            f"{'开始裁剪' if do_encode else '开始分析'}  画面冻结≥{params.freeze_duration}s "
            f"n={params.freeze_noise}  音量<{params.silence_db} dB  ≥{params.silence_duration}s"
        )

        def log(msg: str) -> None:
            self.ui(lambda m=msg: self._append_log(m))

        def progress(pct: float, msg: str) -> None:
            def upd() -> None:
                if pct >= 0:
                    self.progress_var.set(pct)
                self.status_var.set(msg)

            self.ui(upd)

        def work() -> None:
            try:
                cutter = StallCutter(
                    self.ffmpeg,
                    log=log,
                    progress=progress,
                    cancel_event=self.cancel_event,
                )
                self.cutter = cutter
                reuse = (
                    do_encode
                    and self.result is not None
                    and self.result.params == params
                )
                if not reuse:
                    result = cutter.analyze(src, params)
                else:
                    result = self.result
                    assert result is not None
                self.result = result
                report_path = out.with_name(out.stem + "_report.txt") if do_encode else src.with_name(
                    src.stem + "_cut_report.txt"
                )

                def show_result() -> None:
                    self.summary_var.set(
                        f"卡顿 {len(result.freeze)} 段 / {fmt_ts(result.freeze_total)}    "
                        f"静音 {len(result.quiet)} 段 / {fmt_ts(result.quiet_total)}    "
                        f"将剪掉 {len(result.remove)} 段 / {fmt_ts(result.remove_total)}    "
                        f"保留 {fmt_ts(result.keep_total)}"
                    )
                    self._append_log(result.report_text.split("=== KEEP ===")[0].strip())

                self.ui(show_result)
                if not do_encode:
                    report_path.write_text(result.report_text, encoding="utf-8")
                    self.ui(lambda: self.status_var.set(f"分析完成，报告已写到 {report_path.name}"))
                    return
                if not result.remove:
                    self.ui(
                        lambda: messagebox.showinfo("无需裁剪", "按当前条件没有需要剪掉的片段。")
                    )
                    return
                if not result.keep:
                    raise RuntimeError("按当前条件会剪光整段视频，已中止")
                cutter.encode(src, result.keep, out)
                report_path.write_text(result.report_text, encoding="utf-8")

                def done() -> None:
                    self.progress_var.set(100)
                    self.status_var.set(f"裁剪完成  {out}")
                    messagebox.showinfo(
                        "完成",
                        f"已保存：\n{out}\n\n"
                        f"原片 {fmt_ts(result.duration)} → 保留 {fmt_ts(result.keep_total)}\n"
                        f"剪掉 {len(result.remove)} 段 / {fmt_ts(result.remove_total)}",
                    )

                self.ui(done)
            except Exception as exc:
                tb = traceback.format_exc()
                self.ui(lambda: self._append_log(tb))
                msg = str(exc)
                self.ui(lambda m=msg: self.status_var.set(m))
                if "已取消" in str(exc):
                    self.ui(lambda: self.status_var.set("已停止"))
                else:
                    self.ui(lambda m=msg: messagebox.showerror("失败", m))
            finally:
                self.ui(lambda: self._set_busy(False))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()


def main() -> None:
    enable_dpi()
    root = tk.Tk()
    StallCutApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
