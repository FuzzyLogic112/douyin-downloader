# -*- coding: utf-8 -*-
"""
抖音下载器 APP（图形界面）
- 下载：粘贴链接或整段分享文本，批量下载无水印视频 + 封面 + 文案 + 元数据
- 去水印：给已带烧录水印的本地视频做 delogo 修复，附处理前后对照图
打包：双击「打包成EXE.bat」用 PyInstaller 生成独立 exe（见 README）。
"""

import json
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

import app_core

if getattr(sys, "frozen", False):
    APP_DIR = os.path.dirname(sys.executable)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "app_settings.json")
# 让引擎能找到与 exe/脚本同目录的 ffmpeg（用户把 ffmpeg.exe 放旁边即可启用后处理）
os.environ["PATH"] = APP_DIR + os.pathsep + os.environ.get("PATH", "")

QUALITY_LABELS = {"自动（探测选最大）": "auto", "1080p": "1080", "720p": "720"}
REGION_LABELS = {"自动（左上+右下两角）": "auto", "只修左上": "tl", "只修右下": "br", "自定义坐标": "custom"}


def load_settings():
    try:
        with open(SETTINGS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(s):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def open_folder(path):
    try:
        if sys.platform.startswith("win"):
            os.startfile(path)  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.run(["open", path], check=False)
        else:
            subprocess.run(["xdg-open", path], check=False)
    except Exception:
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("抖音下载器")
        self.geometry("780x640")
        self.minsize(680, 540)
        self.settings = load_settings()
        self.q = queue.Queue()
        self.busy = False
        self._build_ui()
        self.after(100, self._poll)

    # ---------- UI ----------
    def _build_ui(self):
        pad = {"padx": 10, "pady": 4}
        nb = ttk.Notebook(self)
        nb.pack(fill="x", **pad)
        tab_dl = ttk.Frame(nb)
        tab_wm = ttk.Frame(nb)
        nb.add(tab_dl, text="  下载视频  ")
        nb.add(tab_wm, text="  去水印  ")

        # 下载页
        ttk.Label(tab_dl, text="粘贴抖音链接或整段分享文本（一行一个，可多行批量）：").pack(anchor="w", **pad)
        self.txt_links = scrolledtext.ScrolledText(tab_dl, height=6, font=("Microsoft YaHei", 10))
        self.txt_links.pack(fill="x", **pad)
        row = ttk.Frame(tab_dl)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="画质：").pack(side="left")
        self.cmb_quality = ttk.Combobox(row, values=list(QUALITY_LABELS), state="readonly", width=18)
        self.cmb_quality.set(self.settings.get("quality_label", "自动（探测选最大）"))
        self.cmb_quality.pack(side="left", padx=4)
        self.var_ffmpeg = tk.BooleanVar(value=self.settings.get("use_ffmpeg", True))
        ttk.Checkbutton(row, text="ffmpeg 后处理（元数据+封面嵌入，需已装 ffmpeg）", variable=self.var_ffmpeg).pack(side="left", padx=8)
        row2 = ttk.Frame(tab_dl)
        row2.pack(fill="x", **pad)
        ttk.Label(row2, text="保存到：").pack(side="left")
        self.ent_out = ttk.Entry(row2)
        self.ent_out.insert(0, self.settings.get("out_dir", os.path.join(APP_DIR, "downloads")))
        self.ent_out.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row2, text="浏览…", command=self._pick_out).pack(side="left")
        row3 = ttk.Frame(tab_dl)
        row3.pack(fill="x", **pad)
        self.btn_dl = ttk.Button(row3, text="开始下载", command=self._start_download)
        self.btn_dl.pack(side="left")
        ttk.Button(row3, text="打开保存文件夹", command=lambda: open_folder(self.ent_out.get())).pack(side="left", padx=8)
        ttk.Button(row3, text="清空输入", command=lambda: self.txt_links.delete("1.0", "end")).pack(side="left")

        # 去水印页
        ttk.Label(tab_wm, text="选择一个已经带水印的本地视频（App 内保存/录屏来的那种），做区域修复：").pack(anchor="w", **pad)
        row4 = ttk.Frame(tab_wm)
        row4.pack(fill="x", **pad)
        self.ent_file = ttk.Entry(row4)
        self.ent_file.pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(row4, text="选择视频…", command=self._pick_file).pack(side="left")
        row5 = ttk.Frame(tab_wm)
        row5.pack(fill="x", **pad)
        ttk.Label(row5, text="水印位置：").pack(side="left")
        self.cmb_region = ttk.Combobox(row5, values=list(REGION_LABELS), state="readonly", width=20)
        self.cmb_region.set(self.settings.get("region_label", "自动（左上+右下两角）"))
        self.cmb_region.pack(side="left", padx=4)
        self.ent_custom = ttk.Entry(row5, width=18)
        self.ent_custom.insert(0, self.settings.get("custom_region", ""))
        self.ent_custom.pack(side="left", padx=4)
        ttk.Label(row5, text="（自定义填 x,y,w,h 像素）").pack(side="left")
        ttk.Label(tab_wm, text="说明：这是插值修复不是还原，背景简单时近乎无痕；水印压着字幕/人脸时会被一起涂抹。\n处理完会生成一张处理前/后对照图，先看图再决定用不用。下载页下的视频本身无水印，不需要这一步。",
                  foreground="#666").pack(anchor="w", **pad)
        row6 = ttk.Frame(tab_wm)
        row6.pack(fill="x", **pad)
        self.btn_wm = ttk.Button(row6, text="开始去水印", command=self._start_delogo)
        self.btn_wm.pack(side="left")

        # 底部共享：状态 + 进度 + 日志
        self.lbl_status = ttk.Label(self, text="就绪")
        self.lbl_status.pack(anchor="w", **pad)
        self.progress = ttk.Progressbar(self, mode="determinate", maximum=100)
        self.progress.pack(fill="x", **pad)
        ttk.Label(self, text="日志：").pack(anchor="w", padx=10)
        self.txt_log = scrolledtext.ScrolledText(self, height=11, font=("Consolas", 9), state="disabled")
        self.txt_log.pack(fill="both", expand=True, **pad)
        ttk.Label(self, text="仅用于公开作品的对标研究与自有内容备份，请尊重原作者版权。", foreground="#999").pack(anchor="w", padx=10, pady=2)

    # ---------- 事件 ----------
    def _pick_out(self):
        d = filedialog.askdirectory(initialdir=self.ent_out.get() or APP_DIR)
        if d:
            self.ent_out.delete(0, "end")
            self.ent_out.insert(0, d)

    def _pick_file(self):
        f = filedialog.askopenfilename(filetypes=[("视频文件", "*.mp4 *.mov *.mkv *.webm"), ("所有文件", "*.*")])
        if f:
            self.ent_file.delete(0, "end")
            self.ent_file.insert(0, f)

    def _persist(self):
        self.settings.update({
            "out_dir": self.ent_out.get(),
            "quality_label": self.cmb_quality.get(),
            "use_ffmpeg": bool(self.var_ffmpeg.get()),
            "region_label": self.cmb_region.get(),
            "custom_region": self.ent_custom.get().strip(),
        })
        save_settings(self.settings)

    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_dl.config(state=state)
        self.btn_wm.config(state=state)
        if not busy:
            self.progress.config(mode="determinate")
            self.progress["value"] = 0

    def _start_download(self):
        if self.busy:
            return
        blob = self.txt_links.get("1.0", "end").strip()
        if not blob:
            messagebox.showinfo("提示", "先粘贴至少一个链接。")
            return
        self._persist()
        self._set_busy(True)
        self.lbl_status.config(text="开始下载…")
        args = (blob, self.ent_out.get(), QUALITY_LABELS.get(self.cmb_quality.get(), "auto"), bool(self.var_ffmpeg.get()))
        threading.Thread(target=self._run, args=(app_core.run_downloads, args), daemon=True).start()

    def _start_delogo(self):
        if self.busy:
            return
        src = self.ent_file.get().strip()
        if not src or not os.path.exists(src):
            messagebox.showinfo("提示", "先选择一个存在的视频文件。")
            return
        label = self.cmb_region.get()
        region = REGION_LABELS.get(label, "auto")
        if region == "custom":
            region = self.ent_custom.get().strip()
            import re as _re
            if not _re.fullmatch(r"\d+,\d+,\d+,\d+", region):
                messagebox.showinfo("提示", "自定义坐标格式不对，应为 x,y,w,h（像素数字）。")
                return
        self._persist()
        self._set_busy(True)
        self.progress.config(mode="indeterminate")
        self.progress.start(60)
        threading.Thread(target=self._run, args=(app_core.run_delogo, (src, region)), daemon=True).start()

    # ---------- 线程桥 ----------
    def _run(self, fn, args):
        def on_line(s):
            self.q.put(("line", s))

        def on_progress(p):
            self.q.put(("progress", p))

        def on_status(s):
            self.q.put(("status", s))

        try:
            fn(*args, on_line, on_progress, on_status)
        except Exception as e:
            self.q.put(("line", f"✗ 出错：{e}"))
        finally:
            self.q.put(("done", None))

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "line":
                    self.txt_log.config(state="normal")
                    self.txt_log.insert("end", val + "\n")
                    self.txt_log.see("end")
                    self.txt_log.config(state="disabled")
                elif kind == "progress":
                    if self.progress["mode"] == "determinate":
                        self.progress["value"] = val
                elif kind == "status":
                    self.lbl_status.config(text=val)
                elif kind == "done":
                    self.progress.stop()
                    self._set_busy(False)
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    # 打包成 windowed exe 时 sys.stdout 可能为 None，给引擎的 print 兜个底
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w")
    App().mainloop()
