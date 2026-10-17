#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音视频下载器 · Web 版（零依赖，Python 3 标准库）

启动：
  python3 server.py                # 默认 0.0.0.0:8901
  PORT=9000 python3 server.py      # 自定义端口
  DOUYIN_DL_CODE=你的口令 python3 server.py   # 可选：开启访问口令

接口：
  GET  /                        网页界面
  POST /api/parse  {url}        解析链接 → 元数据 + 下载入口
  GET  /api/download?id=&q=     代理下载无水印视频（q=1080 优先高清）
  GET  /api/cover?id=           代理下载封面

原理与 CLI 版相同：ttwid 预热 → 分享页 _ROUTER_DATA → playwm 换 play。
仅用于公开作品的对标研究与自有内容备份，请尊重原作者版权。
"""

import http.cookiejar
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 "
             "Mobile/15E148 Safari/604.1")
PORT = int(os.environ.get("PORT", "8901"))
ACCESS_CODE = os.environ.get("DOUYIN_DL_CODE", "").strip()

CACHE = {}          # video_id -> {"item": dict, "ts": float}
CACHE_LOCK = threading.Lock()
CACHE_TTL = 1800    # 30 分钟
CACHE_MAX = 200


# ---------- 解析核心（与 CLI 版同源） ----------

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def make_opener():
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))


def http_get(opener, url, referer=None, timeout=30):
    headers = {"User-Agent": MOBILE_UA}
    if referer:
        headers["Referer"] = referer
    req = urllib.request.Request(url, headers=headers)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "ignore")


def extract_url(text):
    m = re.search(r"https?://[^\s\"'，。；、）】]+", text or "")
    return m.group(0) if m else None


def resolve_video_id(opener, url):
    m = re.search(r"/(?:video|share/video|note)/(\d+)", url)
    if m:
        return m.group(1)
    m = re.search(r"(?:modal_id|vid)=(\d+)", url)
    if m:
        return m.group(1)
    no_redir = urllib.request.build_opener()
    no_redir.add_handler(NoRedirect())
    req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA})
    final = ""
    try:
        with no_redir.open(req, timeout=20) as resp:
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


def fetch_item(video_id):
    opener = make_opener()
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


def get_item(video_id):
    now = time.time()
    with CACHE_LOCK:
        hit = CACHE.get(video_id)
        if hit and now - hit["ts"] < CACHE_TTL:
            return hit["item"]
    item = fetch_item(video_id)
    if item:
        with CACHE_LOCK:
            if len(CACHE) >= CACHE_MAX:
                CACHE.clear()
            CACHE[video_id] = {"item": item, "ts": now}
    return item


def play_url(item, quality):
    urls = (item.get("video") or {}).get("play_addr", {}).get("url_list") or []
    if not urls:
        return None
    play = urls[0].replace("playwm", "play")
    if quality == "1080" and "ratio=" in play:
        play = re.sub(r"ratio=[^&]+", "ratio=1080p", play)
    return play


def item_meta(item, video_id):
    author = item.get("author") or {}
    stats = item.get("statistics") or {}
    music = item.get("music") or {}
    video = item.get("video") or {}
    cover = (video.get("cover") or {}).get("url_list") or []
    images = item.get("images") or []
    return {
        "id": video_id,
        "author": author.get("nickname") or "佚名",
        "desc": (item.get("desc") or "").strip(),
        "create_time": time.strftime("%Y-%m-%d %H:%M", time.localtime(int(item.get("create_time") or 0))),
        "digg_count": stats.get("digg_count"),
        "comment_count": stats.get("comment_count"),
        "share_count": stats.get("share_count"),
        "music_title": music.get("title"),
        "cover": cover[0] if cover else None,
        "is_images": bool(images),
        "image_count": len(images),
        "has_video": bool((video.get("play_addr") or {}).get("url_list")),
    }


def safe_name(item, video_id):
    author = (item.get("author") or {}).get("nickname") or "douyin"
    title = ((item.get("desc") or "").split("\n")[0] or video_id).strip()
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", f"{author}-{title}")[:80]
    return name or video_id


# ---------- 网页界面 ----------

INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#0f1115">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon.png">
<link rel="apple-touch-icon" href="/icon.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="mobile-web-app-capable" content="yes">
<title>抖音视频下载器</title>
<style>
body{font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;background:#0f1115;color:#e8eaed;margin:0;padding:24px 16px}
.wrap{max-width:680px;margin:0 auto}
h1{font-size:22px;margin:8px 0 4px}
.sub{color:#9aa0a6;font-size:13px;margin-bottom:20px}
textarea{width:100%;box-sizing:border-box;background:#1c1f26;border:1px solid #333;border-radius:10px;color:#fff;padding:12px;font-size:15px;min-height:64px}
button{background:#fe2c55;color:#fff;border:0;border-radius:10px;padding:12px 22px;font-size:16px;cursor:pointer;margin-top:12px}
button:disabled{opacity:.5}
.card{background:#1c1f26;border:1px solid #2c3038;border-radius:12px;padding:16px;margin-top:20px;display:none}
.card img{width:120px;border-radius:8px;float:right;margin-left:12px}
.meta{color:#9aa0a6;font-size:13px;line-height:1.7}
.desc{white-space:pre-wrap;font-size:14px;line-height:1.6;margin:8px 0}
a.dl{display:inline-block;background:#2c3038;color:#fff;text-decoration:none;padding:9px 16px;border-radius:8px;margin:6px 8px 0 0;font-size:14px}
.err{color:#ff7b72;margin-top:14px;font-size:14px}
</style></head><body><div class="wrap">
<h1>抖音视频下载器</h1>
<div class="sub">粘贴抖音分享链接（整段分享文字也行），解析后下载无水印视频、封面与文案。素材请用于对标研究与自有备份，尊重原作者版权。</div>
<textarea id="inp" placeholder="在此粘贴链接…"></textarea>
<button id="go">解析链接</button>
<div class="err" id="err"></div>
<div class="card" id="card">
  <img id="cover" alt="">
  <div class="desc" id="desc"></div>
  <div class="meta" id="meta"></div>
  <div id="btns"></div>
</div>
<script>
const $ = id => document.getElementById(id);
let code = localStorage.getItem('dl_code') || '';
async function api(path, opts={}) {
  opts.headers = Object.assign({'Content-Type':'application/json','X-Access-Code':code}, opts.headers||{});
  let r = await fetch(path, opts);
  if (r.status === 401) {
    code = prompt('请输入访问口令') || '';
    localStorage.setItem('dl_code', code);
    opts.headers['X-Access-Code'] = code;
    r = await fetch(path, opts);
  }
  return r.json();
}
$('go').onclick = async () => {
  const url = $('inp').value.trim();
  $('err').textContent = ''; $('card').style.display = 'none';
  if (!url) { $('err').textContent = '先粘贴一个链接'; return; }
  $('go').disabled = true; $('go').textContent = '解析中…';
  try {
    const d = await api('/api/parse', {method:'POST', body: JSON.stringify({url})});
    if (!d.ok) { $('err').textContent = d.error || '解析失败'; return; }
    const m = d.meta;
    $('desc').textContent = m.desc || '(无文案)';
    $('meta').textContent = `作者：${m.author}｜发布：${m.create_time}｜点赞 ${m.digg_count ?? '-'}｜评论 ${m.comment_count ?? '-'}｜转发 ${m.share_count ?? '-'}` + (m.music_title ? `｜音乐：${m.music_title}` : '');
    if (m.cover) $('cover').src = m.cover; else $('cover').removeAttribute('src');
    let h = '';
    if (m.has_video) {
      h += `<a class="dl" href="/api/download?id=${m.id}&q=1080&code=${encodeURIComponent(code)}">下载视频（1080p）</a>`;
      h += `<a class="dl" href="/api/download?id=${m.id}&code=${encodeURIComponent(code)}">下载视频（默认）</a>`;
    }
    if (m.is_images) h += `<span class="meta">图集作品共 ${m.image_count} 张，请用命令行版批量下载</span>`;
    if (m.cover) h += `<a class="dl" href="/api/cover?id=${m.id}&code=${encodeURIComponent(code)}">下载封面</a>`;
    $('btns').innerHTML = h;
    $('card').style.display = 'block';
  } catch(e) { $('err').textContent = '请求失败：' + e; }
  finally { $('go').disabled = false; $('go').textContent = '解析链接'; }
};
</script></div></body></html>
"""


# ---------- HTTP 服务 ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "DouyinDL/1.0"

    def log_message(self, fmt, *args):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self):
        if not ACCESS_CODE:
            return True
        code = self.headers.get("X-Access-Code") or ""
        if not code:
            from urllib.parse import urlparse, parse_qs
            code = (parse_qs(urlparse(self.path).query).get("code") or [""])[0]
        return code == ACCESS_CODE

    def do_GET(self):
        from urllib.parse import urlparse, parse_qs
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/manifest.webmanifest":
            body = json.dumps({
                "name": "抖音视频下载器", "short_name": "抖音下载",
                "start_url": "/", "display": "standalone",
                "background_color": "#0f1115", "theme_color": "#0f1115",
                "icons": [{"src": "/icon.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}],
            }, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/manifest+json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path == "/icon.png":
            ipath = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.png")
            if os.path.exists(ipath):
                with open(ipath, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        if u.path == "/":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if u.path in ("/api/download", "/api/cover"):
            if not self._authed():
                return self._json(401, {"ok": False, "error": "需要访问口令"})
            vid = (q.get("id") or [""])[0]
            if not vid.isdigit():
                return self._json(400, {"ok": False, "error": "id 不合法"})
            item = get_item(vid)
            if not item:
                return self._json(404, {"ok": False, "error": "作品数据已过期或取不到，请重新解析"})
            if u.path == "/api/cover":
                urls = (item.get("video") or {}).get("cover", {}).get("url_list") or []
                if not urls:
                    return self._json(404, {"ok": False, "error": "没有封面"})
                return self._proxy(urls[0], safe_name(item, vid) + ".jpg", "image/jpeg")
            quality = (q.get("q") or [""])[0]
            url = play_url(item, quality)
            if not url:
                return self._json(404, {"ok": False, "error": "没有视频地址"})
            fallback = play_url(item, "") if quality == "1080" else None
            return self._proxy(url, safe_name(item, vid) + ".mp4", "video/mp4", fallback_url=fallback)
        return self._json(404, {"ok": False, "error": "not found"})

    def _proxy(self, url, filename, ctype, fallback_url=None):
        req = urllib.request.Request(url, headers={"User-Agent": MOBILE_UA, "Referer": "https://www.iesdouyin.com/"})
        try:
            resp = urllib.request.urlopen(req, timeout=60)
        except Exception as e:
            if fallback_url and fallback_url != url:
                return self._proxy(fallback_url, filename, ctype)
            return self._json(502, {"ok": False, "error": f"上游下载失败：{e}"})
        self.send_response(200)
        self.send_header("Content-Type", resp.headers.get("Content-Type") or ctype)
        length = resp.headers.get("Content-Length")
        if length:
            self.send_header("Content-Length", length)
        from urllib.parse import quote
        self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{quote(filename)}")
        self.end_headers()
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            try:
                self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
        resp.close()

    def do_POST(self):
        from urllib.parse import urlparse
        if urlparse(self.path).path != "/api/parse":
            return self._json(404, {"ok": False, "error": "not found"})
        if not self._authed():
            return self._json(401, {"ok": False, "error": "需要访问口令"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json(400, {"ok": False, "error": "请求体不是合法 JSON"})
        url = extract_url(body.get("url") or "")
        if not url:
            return self._json(400, {"ok": False, "error": "没有找到链接"})
        opener = make_opener()
        vid = resolve_video_id(opener, url)
        if not vid:
            return self._json(422, {"ok": False, "error": "没能解析出作品 ID（链接可能过期或不是作品链接）"})
        item = get_item(vid)
        if not item:
            return self._json(502, {"ok": False, "error": "分享页没返回作品数据：可能触发平台风控，或作品已删除/私密，稍后重试"})
        return self._json(200, {"ok": True, "meta": item_meta(item, vid)})


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"抖音下载器 Web 版已启动：http://0.0.0.0:{PORT}（口令保护：{'开' if ACCESS_CODE else '关'}）")
    srv.serve_forever()
