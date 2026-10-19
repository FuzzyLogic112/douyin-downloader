#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Douyin Downloader MCP Server (zero-dependency, stdio transport)

Wraps the existing douyin-dl.py engine as MCP tools so any MCP-compatible
AI client (Claude / Claude Code / Cursor / Windsurf / Muse, etc.)
can call it as a native tool instead of guessing CLI arguments.

Tools:
  douyin_parse            Parse a Douyin link/share-text -> metadata only, no download
  douyin_download         Download no-watermark video (or image set) + cover + caption + metadata json
  douyin_remove_watermark Repair a local video that already has a burned-in watermark (ffmpeg delogo)

Protocol: MCP over stdio, newline-delimited JSON-RPC 2.0.
No third-party packages: Python 3.8+ standard library only.

Engine lookup order for douyin-dl.py:
  1. $DOUYIN_DL_PATH (full path to douyin-dl.py)
  2. Same directory as this server file
  3. Parent directory of this server file (repo layout: <repo>/mcp/ + <repo>/douyin-dl.py)
  4. Current working directory

Default download directory: $DOUYIN_DOWNLOAD_DIR, else ./downloads beside the engine.
"""

import contextlib
import importlib.util
import io
import json
import os
import sys
import traceback

SERVER_NAME = "douyin-downloader"
SERVER_VERSION = "1.0.0"
DEFAULT_PROTOCOL = "2024-11-05"


# ---------------- engine loading ----------------

def find_engine_path():
    candidates = []
    env = os.environ.get("DOUYIN_DL_PATH", "").strip()
    if env:
        candidates.append(env)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append(os.path.join(here, "douyin-dl.py"))
    candidates.append(os.path.join(os.path.dirname(here), "douyin-dl.py"))
    candidates.append(os.path.join(os.getcwd(), "douyin-dl.py"))
    for p in candidates:
        if p and os.path.isfile(p):
            return p
    return None


ENGINE_PATH = find_engine_path()
_engine = None


def load_engine():
    global _engine
    if _engine is not None:
        return _engine
    if not ENGINE_PATH:
        raise RuntimeError(
            "douyin-dl.py not found. Put it beside this server, in the parent "
            "directory, or set DOUYIN_DL_PATH to its full path.")
    spec = importlib.util.spec_from_file_location("douyin_dl_engine", ENGINE_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    _engine = mod
    return mod


def default_out_dir():
    env = os.environ.get("DOUYIN_DOWNLOAD_DIR", "").strip()
    if env:
        return env
    base = os.path.dirname(ENGINE_PATH) if ENGINE_PATH else os.getcwd()
    return os.path.join(base, "downloads")


def snapshot(d):
    out = {}
    if os.path.isdir(d):
        for name in os.listdir(d):
            p = os.path.join(d, name)
            if os.path.isfile(p):
                try:
                    out[name] = os.path.getsize(p)
                except OSError:
                    pass
    return out


# ---------------- tool implementations ----------------

def tool_parse(args):
    eng = load_engine()
    raw = (args.get("url") or "").strip()
    if not raw:
        raise ValueError("missing required argument: url")
    url = eng.extract_url(raw)
    if not url:
        return {"success": False, "error": "no_url_found",
                "message": "No http(s) link found in the input text."}
    opener = eng.make_opener()
    video_id = eng.resolve_video_id(opener, url)
    if not video_id:
        return {"success": False, "error": "id_not_resolved",
                "message": "Could not resolve a Douyin work ID from the link (expired or not a work link)."}
    item = eng.fetch_item(opener, video_id)
    if not item:
        return {"success": False, "error": "item_not_found", "video_id": video_id,
                "message": "Share page returned no data: possible risk control, or the work was deleted / is private."}
    author = item.get("author") or {}
    stats = item.get("statistics") or {}
    music = item.get("music") or {}
    video = item.get("video") or {}
    images = item.get("images") or []
    return {
        "success": True,
        "video_id": video_id,
        "source_url": url,
        "author": author.get("nickname"),
        "author_id": author.get("unique_id") or author.get("short_id"),
        "desc": (item.get("desc") or "").strip(),
        "create_time": eng.fmt_time(item.get("create_time")),
        "is_image_set": bool(images),
        "image_count": len(images),
        "duration_ms": video.get("duration"),
        "cover_url": ((video.get("cover") or {}).get("url_list") or [None])[0],
        "statistics": {
            "digg_count": stats.get("digg_count"),
            "comment_count": stats.get("comment_count"),
            "share_count": stats.get("share_count"),
            "collect_count": stats.get("collect_count"),
        },
        "music": {"title": music.get("title"), "author": music.get("author")},
    }


def tool_download(args):
    eng = load_engine()
    raw = (args.get("url") or "").strip()
    if not raw:
        raise ValueError("missing required argument: url")
    quality = args.get("quality") or "auto"
    if quality not in ("auto", "1080", "720"):
        raise ValueError("quality must be one of: auto, 1080, 720")
    use_ffmpeg = bool(args.get("use_ffmpeg", True))
    out_dir = (args.get("output_dir") or "").strip() or default_out_dir()
    os.makedirs(out_dir, exist_ok=True)
    if use_ffmpeg and not eng.ffmpeg_available():
        use_ffmpeg = False
    before = snapshot(out_dir)
    opener = eng.make_opener()
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = eng.process(opener, raw, out_dir, quality, use_ffmpeg)
    after = snapshot(out_dir)
    new_files = []
    for name, size in sorted(after.items()):
        if name not in before or before[name] != size:
            if name.endswith(".part"):
                continue
            new_files.append({"name": name, "path": os.path.join(out_dir, name), "size_bytes": size})
    meta = None
    for f in new_files:
        if f["name"].endswith(".json"):
            try:
                with open(f["path"], encoding="utf-8") as fh:
                    meta = json.load(fh)
            except Exception:
                pass
    result = {
        "success": bool(ok),
        "output_dir": out_dir,
        "quality": quality,
        "ffmpeg_postprocess": use_ffmpeg,
        "files": new_files,
        "metadata": meta,
        "engine_log_tail": buf.getvalue().strip()[-800:],
    }
    if not ok and not new_files:
        result["error"] = "download_failed"
        result["message"] = ("Download failed. Common causes: risk control on this IP, "
                              "deleted/private work, or expired link. Try again later.")
    return result


def tool_remove_watermark(args):
    eng = load_engine()
    src = (args.get("file_path") or "").strip()
    if not src:
        raise ValueError("missing required argument: file_path")
    region = args.get("region") or "auto"
    preview = bool(args.get("preview", True))
    if not os.path.isfile(src):
        return {"success": False, "error": "file_not_found", "message": f"File does not exist: {src}"}
    if not eng.ffmpeg_available():
        return {"success": False, "error": "ffmpeg_missing",
                "message": "ffmpeg/ffprobe not found; the remove-watermark mode requires ffmpeg."}
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        ok = eng.remove_watermark(src, region, preview)
    stem, _ = os.path.splitext(src)
    out = stem + "-去水印.mp4"
    result = {"success": bool(ok), "output_file": out if ok and os.path.isfile(out) else None,
              "engine_log_tail": buf.getvalue().strip()[-800:]}
    if preview and ok:
        sheet = stem + "-去水印对照.jpg"
        if os.path.isfile(sheet):
            result["preview_file"] = sheet
    return result


TOOLS = [
    {
        "name": "douyin_parse",
        "description": ("Parse a Douyin video/note link (or a full share text containing the link) and return "
                        "structured metadata only: author, caption, publish time, like/comment/share counts, "
                        "music, cover URL, whether it is an image set. Does NOT download the video. "
                        "Use this first to inspect a work before downloading it."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "Douyin link (e.g. https://v.douyin.com/xxxxxx/) or the full share text containing it."}
            },
            "required": ["url"],
        },
    },
    {
        "name": "douyin_download",
        "description": ("Download a Douyin work without watermark: video MP4 (best available quality up to 1080p) "
                        "or every image for image-set posts, plus cover JPG, caption TXT and metadata JSON "
                        "(likes/comments/shares/publish time/music). Returns the absolute file paths and sizes. "
                        "Long videos can take a while; the call blocks until the download finishes."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "Douyin link or full share text containing it."},
                "quality": {"type": "string", "enum": ["auto", "1080", "720"], "default": "auto",
                            "description": "auto = probe all renditions and pick the largest; 1080 / 720 = prefer that tier."},
                "output_dir": {"type": "string",
                               "description": "Optional output directory. Default: $DOUYIN_DOWNLOAD_DIR or <engine dir>/downloads."},
                "use_ffmpeg": {"type": "boolean", "default": True,
                               "description": "If ffmpeg is available, post-process: faststart remux, embed metadata and cover. Auto-disabled when ffmpeg is missing."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "douyin_remove_watermark",
        "description": ("Repair a LOCAL video file that already has a burned-in watermark (e.g. saved from the app or "
                        "screen-recorded) using ffmpeg delogo interpolation. This is repair, not restoration: it works "
                        "best on simple backgrounds and will smear subtitles/faces under the watermark. "
                        "Videos downloaded with douyin_download are already watermark-free and do not need this."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path of the local video file."},
                "region": {"type": "string", "default": "auto",
                           "description": "auto = fix both top-left and bottom-right corners; tl / br = one corner only; or custom pixels 'x,y,w,h'."},
                "preview": {"type": "boolean", "default": True,
                            "description": "Also generate a before/after frame comparison JPG."},
            },
            "required": ["file_path"],
        },
    },
]

TOOL_FUNCS = {
    "douyin_parse": tool_parse,
    "douyin_download": tool_download,
    "douyin_remove_watermark": tool_remove_watermark,
}


# ---------------- MCP / JSON-RPC plumbing ----------------

def send(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def log(msg):
    sys.stderr.write(f"[douyin-mcp] {msg}\n")
    sys.stderr.flush()


def handle(req):
    method = req.get("method", "")
    rid = req.get("id")
    params = req.get("params") or {}
    is_notification = rid is None

    def result(payload):
        if not is_notification:
            send({"jsonrpc": "2.0", "id": rid, "result": payload})

    def error(code, message):
        if not is_notification:
            send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})

    try:
        if method == "initialize":
            client_proto = params.get("protocolVersion") or DEFAULT_PROTOCOL
            result({
                "protocolVersion": client_proto,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        elif method in ("notifications/initialized", "notifications/cancelled"):
            pass
        elif method == "ping":
            result({})
        elif method == "tools/list":
            result({"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name", "")
            args = params.get("arguments") or {}
            fn = TOOL_FUNCS.get(name)
            if fn is None:
                result({"content": [{"type": "text", "text": f"Unknown tool: {name}"}], "isError": True})
                return
            try:
                payload = fn(args)
                is_err = not payload.get("success", True)
                result({
                    "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                    "structuredContent": payload,
                    "isError": is_err,
                })
            except Exception as e:
                log(traceback.format_exc())
                payload = {"success": False, "error": "tool_exception",
                           "message": f"{type(e).__name__}: {e}"}
                result({
                    "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                    "structuredContent": payload,
                    "isError": True,
                })
        else:
            error(-32601, f"Method not found: {method}")
    except Exception as e:
        log(traceback.format_exc())
        error(-32603, f"Internal error: {type(e).__name__}: {e}")


def main():
    log(f"starting v{SERVER_VERSION}; engine: {ENGINE_PATH or 'NOT FOUND'}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(req, list):
            for r in req:
                handle(r)
        else:
            handle(req)


if __name__ == "__main__":
    main()
