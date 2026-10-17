#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音视频下载器 v2（自媒体素材提取）—— 零依赖，Python 3 标准库实现。
借鉴 yt-dlp 的工程做法：多档画质探测择优、.part 断点续传、失败重试；
下载后可选 ffmpeg 后处理（faststart 重封装 + 写入元数据 + 嵌入封面）。

用法：
  python douyin-dl.py                      # 交互模式：一行一个链接，空行开始
  python douyin-dl.py "https://v.douyin.com/xxxxxx/"
  python douyin-dl.py "8.88 复制打开抖音，看看【…】https://v.douyin.com/xxxxxx/ …"
  python douyin-dl.py link1 link2 ...      # 批量
选项：
  --quality 1080|720|auto   指定画质（默认 auto=探测后选最大的一档）
  --no-ffmpeg               跳过 ffmpeg 后处理（未装 ffmpeg 时自动跳过）
  -o 目录                   自定义输出目录（默认脚本旁 downloads/）

去水印模式（处理已经带烧录水印的本地视频，如 App 内保存/录屏来的文件）：
  python douyin-dl.py --remove-watermark <视频文件> [--region auto|tl|br|x,y,w,h] [--no-preview]
  原理是 ffmpeg delogo 对水印区域做邻域插值修复，属于“修复”不是“还原”：
  背景简单时近乎无痕；水印压着字幕/人脸时，那块内容会被一起涂抹，先看对照图再决定用不用。
  默认生成处理前/后抽帧对照图（--no-preview 关闭）。水印固定在一个角时用 tl 或 br，
  只修一个角、不误伤另一个角；位置对不上时用 x,y,w,h 像素自定义。下载器抓的本身是无水印源，一般用不到这个模式。

产出：<作者>-<标题>.mp4 / .jpg（封面）/ .txt（文案）/ .json（元数据）
仅用于公开作品的对标研究与自有内容备份；请尊重原作者版权，勿搬运冒充原创。
"""

import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
             "Mobile/15E148 Safari/604.1")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RETRIES = 3


# ---------------- 基础 HTTP（带重试） ----------------

def make_opener():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def http_get(opener, url, referer=None, timeout=30):
    headers = {"User-Agent": MOBILE_UA}
    if referer:
        headers["Referer"] = referer
    last = None
    for attempt in range(RETRIES):
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
                return resp.read().decode("utf-8", "ignore")
        except Exception as e:
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise last


def extract_url(text):
    m = re.search(r"https?://[^\s\"'，。；、）】]+", text or "")
    return m.group(0) if m else None


# ---------------- 解析 ----------------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def resolve_video_id(opener, url):
    m = re.search(r"/(?:video|share/video|note)/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"(?:modal_id|vid)=(\d+)", url)
    if m:
        return m.group(1)
    no_redir = urllib.request.build_opener()
    no_redir.add_handler(NoRedirect())
    final = ""
    try:
        with no_redir.open(urllib.request.Request(url, headers={"User-Agent": MOBILE_UA}), timeout=20) as resp:
            final = resp.geturl()
    except urllib.error.HTTPError as e:
        final = e.headers.get("Location") or ""
    except Exception:
        try:
            with opener.open(urllib.request.Request(url, headers={"User-Agent": MOBILE_UA}), timeout=20) as resp:
                final = resp.geturl()
        except Exception:
            final = ""
    m = re.search(r"/(?:video|share/video|note)/(\d+)", final or "")
    return m.group(1) if m else None


def fetch_item(opener, video_id):
    """ttwid 预热后读分享页 SSR 数据（跳过预热会拿到空壳）。"""
    for _ in range(2):
        try:
            http_get(opener, "https://www.iesdouyin.com/")
        except Exception:
            pass
        try:
            html = http_get(opener, f"https://www.iesdouyin.com/share/video/{video_id}/",
                            referer="https://www.iesdouyin.com/")
        except Exception:
            continue
        m = re.search(r"window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>", html, re.S)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
            return data["loaderData"]["video_(id)/page"]["videoInfoRes"]["item_list"][0]
        except (KeyError, IndexError, TypeError, json.JSONDecodeError):
            continue
    return None


# ---------------- 画质探测与下载（yt-dlp 式） ----------------

def probe(opener, url):
    """打开候选地址只读响应头，返回 (content_length, content_type) 或 None。"""
    headers = {"User-Agent": MOBILE_UA, "Referer": "https://www.iesdouyin.com/"}
    try:
        with opener.open(urllib.request.Request(url, headers=headers), timeout=25) as resp:
            ctype = resp.headers.get("Content-Type") or ""
            length = resp.headers.get("Content-Length")
            resp.read(1024)  # 确认真能读到数据
            return (int(length) if length else -1), ctype
    except Exception:
        return None


def pick_rendition(opener, item, quality):
    """返回 (url, label)。auto 时探测各档并选体积最大的一档。"""
    video = item.get("video") or {}
    play_list = (video.get("play_addr") or {}).get("url_list") or []
    if not play_list:
        return None, None
    play = play_list[0].replace("playwm", "play")
    candidates = []
    if "ratio=" in play:
        candidates.append(("1080p", re.sub(r"ratio=[^&]+", "ratio=1080p", play)))
    candidates.append(("默认", play))
    dl_list = (video.get("download_addr") or {}).get("url_list") or []
    if dl_list:
        candidates.append(("下载档", dl_list[0]))
    if quality == "1080":
        return candidates[0]
    if quality == "720":
        return candidates[-1] if len(candidates) == 1 else candidates[1] if len(candidates) > 1 else candidates[0]
    best, best_size = None, -2
    for label, url in candidates:
        p = probe(opener, url)
        if p and "video" in p[1]:
            if p[0] > best_size:
                best, best_size = (url, label), p[0]
    if best:
        return best
    return candidates[0][1], candidates[0][0]


def download_resumable(opener, url, path):
    """断点续传下载：写 .part，支持 Range 续传与重试。返回总字节数。"""
    part = path + ".part"
    headers_base = {"User-Agent": MOBILE_UA, "Referer": "https://www.iesdouyin.com/"}
    for attempt in range(RETRIES):
        have = os.path.getsize(part) if os.path.exists(part) else 0
        headers = dict(headers_base)
        if have:
            headers["Range"] = f"bytes={have}-"
        try:
            with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as resp:
                if have and resp.status != 206:  # 服务器没理 Range，重下
                    have = 0
                total = have + int(resp.headers.get("Content-Length") or 0)
                mode = "ab" if have else "wb"
                with open(part, mode) as f:
                    done = have
                    while True:
                        chunk = resp.read(262144)
                        if not chunk:
                            break
                        f.write(chunk)
                        done += len(chunk)
                        if total:
                            print(f"\r    下载中 {done * 100 // total}% ({done}/{total} 字节)", end="", flush=True)
                print()
            os.replace(part, path)
            return os.path.getsize(path)
        except Exception as e:
            print(f"\n    下载中断（{e}），重试 {attempt + 1}/{RETRIES}…")
            time.sleep(2 * (attempt + 1))
    raise RuntimeError("下载多次重试仍失败")


def download_simple(opener, url, path):
    headers = {"User-Agent": MOBILE_UA, "Referer": "https://www.iesdouyin.com/"}
    with opener.open(urllib.request.Request(url, headers=headers), timeout=60) as resp:
        with open(path, "wb") as f:
            while True:
                chunk = resp.read(262144)
                if not chunk:
                    break
                f.write(chunk)
    return os.path.getsize(path)


# ---------------- ffmpeg 后处理 ----------------

def ffmpeg_available():
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def ffmpeg_postprocess(mp4, cover_path, meta):
    """faststart 重封装 + 写元数据 + 嵌入封面（stream copy，不转码）。成功返回 True。"""
    tmp = mp4 + ".proc.mp4"
    cmd = ["ffmpeg", "-y", "-v", "error", "-i", mp4]
    if cover_path and os.path.exists(cover_path):
        # 封面多为 WebP，MP4 装不下 WebP 流，单独把封面流转成 MJPEG（yt-dlp 同款处理）
        cmd += ["-i", cover_path, "-map", "0", "-map", "1", "-c", "copy",
                "-c:v:1", "mjpeg", "-disposition:v:1", "attached_pic"]
    else:
        cmd += ["-map", "0", "-c", "copy"]
    title = (meta.get("desc") or "").split("\n")[0].strip()
    if title:
        cmd += ["-metadata", f"title={title}"]
    if meta.get("author"):
        cmd += ["-metadata", f"artist={meta['author']}"]
    if meta.get("desc"):
        cmd += ["-metadata", f"comment={meta['desc'][:500]}"]
    if meta.get("create_time_str"):
        cmd += ["-metadata", f"date={meta['create_time_str']}"]
    cmd += ["-movflags", "+faststart", tmp]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 10240:
        os.replace(tmp, mp4)
        return True
    if os.path.exists(tmp):
        os.remove(tmp)
    print(f"    （ffmpeg 后处理失败，保留原始文件：{r.stderr.strip()[:120]}）")
    return False


def probe_summary(path):
    """ffprobe 自检：时长/分辨率/流构成/moov 是否前置。"""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries",
             "format=duration:stream=codec_type,width,height:stream_disposition=attached_pic",
             "-of", "json", path], capture_output=True, text=True, timeout=30).stdout
        info = json.loads(out)
        dur = float(info["format"]["duration"])
        streams = info.get("streams", [])
        kinds = [s["codec_type"] for s in streams]
        wh = next(((s.get("width"), s.get("height")) for s in streams if s["codec_type"] == "video"), (None, None))
        has_pic = any((s.get("disposition") or {}).get("attached_pic") for s in streams)
        data = open(path, "rb").read(2_000_000)
        moov, mdat = data.find(b"moov"), data.find(b"mdat")
        faststart = 0 <= moov and (mdat == -1 or moov < mdat)
        return f"时长 {dur:.1f}s｜分辨率 {wh[0]}x{wh[1]}｜流 {kinds}｜封面嵌入 {'✓' if has_pic else '—'}｜faststart {'✓' if faststart else '—'}"
    except Exception as e:
        return f"（自检跳过：{e}）"


# ---------------- 去水印（本地已烧录水印的视频） ----------------

def probe_dims(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", path], capture_output=True, text=True, timeout=30).stdout
    info = json.loads(out)
    st = info["streams"][0]
    return int(st["width"]), int(st["height"]), float(info["format"]["duration"])


def delogo_regions(region, w, h):
    """返回 [(x, y, w, h), ...] 像素区域。预设按抖音导出水印的常见位置（左上/右下）。"""
    if re.fullmatch(r"\d+,\d+,\d+,\d+", region or ""):
        return [tuple(int(v) for v in region.split(","))]
    bw, bh = int(w * 0.36), int(h * 0.085)
    tl = (int(w * 0.025), int(h * 0.018), bw, bh)
    br = (w - int(w * 0.025) - bw, h - int(h * 0.025) - bh, bw, bh)
    if region == "tl":
        return [tl]
    if region == "br":
        return [br]
    return [tl, br]  # auto / both：抖音导出水印常在两角交替出现，两个都修


def remove_watermark(src, region="auto", preview=True):
    if not ffmpeg_available():
        print("✗ 去水印需要 ffmpeg/ffprobe，请先安装 ffmpeg。")
        return False
    if not os.path.exists(src):
        print(f"✗ 文件不存在：{src}")
        return False
    w, h, dur = probe_dims(src)
    regions = delogo_regions(region, w, h)
    vf = ",".join(f"delogo=x={x}:y={y}:w={rw}:h={rh}" for x, y, rw, rh in regions)
    stem, _ = os.path.splitext(src)
    out = stem + "-去水印.mp4"
    print(f"  分辨率 {w}x{h}｜修复区域 {len(regions)} 处｜处理中（需重新编码，请稍候）…")
    r = subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-i", src, "-vf", vf,
         "-c:v", "libx264", "-crf", "18", "-preset", "medium",
         "-c:a", "copy", "-movflags", "+faststart", out],
        capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        print(f"  ✗ 去水印失败：{r.stderr.strip()[:200]}")
        return False
    print(f"  ✓ 已生成：{out}")
    if preview:
        sheet = stem + "-去水印对照.jpg"
        tiles = []
        tmpdir = stem + "-预览临时"
        os.makedirs(tmpdir, exist_ok=True)
        try:
            for tag, f in (("原", src), ("后", out)):
                for i, frac in enumerate((0.15, 0.5, 0.85)):
                    p = os.path.join(tmpdir, f"{tag}{i}.jpg")
                    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{dur * frac:.1f}",
                                    "-i", f, "-vframes", "1", "-q:v", "3", p],
                                   capture_output=True, timeout=60)
                    tiles.append(p)
            grid_in = "".join(f"-i {p} " for p in tiles)
            subprocess.run(
                f"ffmpeg -y -v error {grid_in}-filter_complex tile=3x2 -q:v 3 \"{sheet}\"",
                shell=True, capture_output=True, timeout=120)
            if os.path.exists(sheet):
                print(f"  ✓ 对照图（上排原图/下排处理后）：{sheet}")
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)
    return True


# ---------------- 主流程 ----------------

def sanitize(name, max_len=60):
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    return (name[:max_len].rstrip() or "douyin")


def fmt_time(ts):
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(int(ts)))
    except Exception:
        return ""


def process(opener, raw_text, out_dir, quality, use_ffmpeg):
    url = extract_url(raw_text)
    if not url:
        print("  ✗ 没找到链接，跳过")
        return False
    print(f"  链接：{url}")
    video_id = resolve_video_id(opener, url)
    if not video_id:
        print("  ✗ 没能解析出作品 ID（链接可能过期或不是作品链接）")
        return False
    item = fetch_item(opener, video_id)
    if not item:
        print("  ✗ 分享页没返回作品数据：可能触发了平台风控，或作品已删除/私密。")
        print("    可稍后重试；若持续失败，把链接发我，我走登录态路线帮你取。")
        return False

    author = (item.get("author") or {}).get("nickname") or "佚名"
    desc = (item.get("desc") or "").strip()
    title = desc.split("\n")[0].strip() or video_id
    stem = sanitize(f"{author}-{title}")
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, stem)
    print(f"  作者：{author}｜标题：{title[:40]}")

    stats = item.get("statistics") or {}
    music = item.get("music") or {}
    meta = {
        "video_id": video_id,
        "author": author,
        "author_id": (item.get("author") or {}).get("unique_id") or (item.get("author") or {}).get("short_id"),
        "desc": desc,
        "create_time": fmt_time(item.get("create_time")),
        "create_time_str": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(int(item.get("create_time") or 0))) if item.get("create_time") else "",
        "digg_count": stats.get("digg_count"),
        "comment_count": stats.get("comment_count"),
        "share_count": stats.get("share_count"),
        "music_title": music.get("title"),
        "music_author": music.get("author"),
        "source_url": url,
    }

    ok = True
    images = item.get("images") or []
    video = item.get("video") or {}
    cover_path = base + ".jpg"
    cover_urls = (video.get("cover") or {}).get("url_list") or []
    if images:
        for i, img in enumerate(images, 1):
            urls = img.get("url_list") or []
            if not urls:
                continue
            p = f"{base}-{i:02d}.jpg"
            try:
                download_resumable(opener, urls[-1], p)
                print(f"    ✓ 图片 {i}：{os.path.basename(p)}")
            except Exception as e:
                print(f"    ✗ 图片 {i} 下载失败：{e}")
                ok = False
    else:
        picked = pick_rendition(opener, item, quality)
        if not picked[0]:
            print("  ✗ 没拿到播放地址")
            return False
        play, label = picked
        print(f"    画质：{label}")
        mp4 = base + ".mp4"
        try:
            download_resumable(opener, play, mp4)
            print(f"    ✓ 视频：{os.path.basename(mp4)}")
        except Exception as e:
            print(f"  ✗ 视频下载失败：{e}")
            return False
        if cover_urls:
            try:
                download_simple(opener, cover_urls[0], cover_path)
                print(f"    ✓ 封面：{os.path.basename(cover_path)}")
            except Exception:
                cover_path = None
        if use_ffmpeg:
            if ffmpeg_postprocess(mp4, cover_path, meta):
                print("    ✓ ffmpeg 后处理：faststart + 元数据" + (" + 封面嵌入" if cover_path else ""))
            print(f"    自检：{probe_summary(mp4)}")

    with open(base + ".txt", "w", encoding="utf-8") as f:
        f.write(desc)
    with open(base + ".json", "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in meta.items() if k != "create_time_str"}, f, ensure_ascii=False, indent=2)
    print(f"    ✓ 文案与元数据：{os.path.basename(base)}.txt / .json")
    return ok


def main():
    argv = sys.argv[1:]
    # 去水印模式（独立子命令）
    if "--remove-watermark" in argv:
        idx = argv.index("--remove-watermark")
        if idx + 1 >= len(argv):
            print("用法：python douyin-dl.py --remove-watermark <视频文件> [--region auto|tl|br|x,y,w,h] [--preview]")
            return
        region, preview = "auto", "--no-preview" not in argv
        if "--region" in argv and argv.index("--region") + 1 < len(argv):
            region = argv[argv.index("--region") + 1]
        remove_watermark(argv[idx + 1], region, preview)
        return
    quality, use_ffmpeg, out_dir = "auto", True, os.path.join(BASE_DIR, "downloads")
    links = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--quality" and i + 1 < len(argv):
            quality = argv[i + 1]; i += 2
        elif a == "--no-ffmpeg":
            use_ffmpeg = False; i += 1
        elif a == "-o" and i + 1 < len(argv):
            out_dir = argv[i + 1]; i += 2
        else:
            links.append(a); i += 1
    if use_ffmpeg and not ffmpeg_available():
        print("（未检测到 ffmpeg/ffprobe，跳过后处理；装上 ffmpeg 后自动启用）")
        use_ffmpeg = False
    opener = make_opener()
    if not links:
        print("抖音视频下载器 v2 —— 把链接（或整段分享文字）粘进来，一行一个，空行回车开始下载：")
        while True:
            try:
                line = input()
            except EOFError:
                break
            if not line.strip():
                break
            links.append(line)
    if not links:
        print("没有输入链接。")
        return
    success = 0
    for idx, t in enumerate(links, 1):
        print(f"[{idx}/{len(links)}]")
        try:
            if process(opener, t, out_dir, quality, use_ffmpeg):
                success += 1
        except Exception as e:
            print(f"  ✗ 处理失败：{type(e).__name__}: {e}")
    print(f"\n完成：{success}/{len(links)} 成功，文件在 {out_dir}")


if __name__ == "__main__":
    main()
