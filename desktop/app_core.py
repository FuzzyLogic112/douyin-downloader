# -*- coding: utf-8 -*-
"""
APP 逻辑层（无界面）：把引擎 douyin_engine 的命令行流程接到回调上，
供图形界面调用，也可独立测试。
- LogTap：接管 print 输出，行回调给日志区；“下载中 NN%”行转成进度回调。
- run_downloads：批量下载（链接或整段分享文本都行）。
- run_delogo：去水印处理（调引擎 remove_watermark）。
注意：靠重定向 sys.stdout 工作，同一时刻只跑一个任务（界面会禁用按钮）。
"""

import contextlib
import io
import os
import re
import sys

import douyin_engine as engine


class LogTap(io.TextIOBase):
    def __init__(self, on_line, on_progress):
        self.on_line = on_line
        self.on_progress = on_progress
        self._buf = ""
        self._prog_re = re.compile(r"下载中\s*(\d+)%")

    def write(self, s):
        if not s:
            return 0
        self._buf += s
        while True:
            m = re.search(r"[\r\n]", self._buf)
            if not m:
                break
            seg, self._buf = self._buf[:m.start()], self._buf[m.end():]
            self._emit(seg)
        return len(s)

    def _emit(self, seg):
        seg = seg.strip()
        if not seg:
            return
        pm = self._prog_re.search(seg)
        if pm:
            try:
                self.on_progress(int(pm.group(1)))
            except Exception:
                pass
            return
        self.on_line(seg)

    def flush(self):
        if self._buf.strip():
            self._emit(self._buf)
            self._buf = ""


def _texts_from_blob(blob):
    """把输入框内容拆成条目：按行拆；无换行的整段文本就整段一条。"""
    lines = [ln.strip() for ln in (blob or "").splitlines() if ln.strip()]
    return lines


def run_downloads(blob, out_dir, quality, use_ffmpeg, on_line, on_progress, on_status):
    texts = _texts_from_blob(blob)
    if not texts:
        on_line("✗ 先粘贴至少一个链接。")
        return 0, 0
    os.makedirs(out_dir, exist_ok=True)
    tap = LogTap(on_line, on_progress)
    opener = engine.make_opener()
    ok = 0
    with contextlib.redirect_stdout(tap):
        for i, text in enumerate(texts, 1):
            print(f"[{i}/{len(texts)}]")
            on_status(f"处理中 {i}/{len(texts)}…")
            try:
                if engine.process(opener, text, out_dir, quality, use_ffmpeg):
                    ok += 1
            except Exception as e:
                print(f"  ✗ 处理失败：{e}")
            on_progress(0)
        print(f"完成：{ok}/{len(texts)} 成功，文件在 {out_dir}")
    on_status(f"完成：{ok}/{len(texts)} 成功")
    return ok, len(texts)


def run_delogo(src, region, on_line, on_progress, on_status):
    tap = LogTap(on_line, on_progress)
    on_status("去水印处理中（重新编码，时间看视频长度）…")
    with contextlib.redirect_stdout(tap):
        done = engine.remove_watermark(src, region, preview=True)
    on_status("去水印完成" if done else "去水印失败，看日志")
    return done
