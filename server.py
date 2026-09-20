"""ReelSave backend: paste an Instagram link -> get download links.

Run locally:  python server.py
Production:   gunicorn -w 2 -k gthread --threads 8 -b 0.0.0.0:8000 server:app

Optional env vars:
  IG_COOKIES_FILE  path to a Netscape cookies.txt (helps when Instagram asks for login)
  IG_PROXY         proxy URL for outgoing requests (e.g. a residential proxy)
  TRUST_PROXY=1    read client IP from X-Forwarded-For (set when behind nginx/Cloudflare)
  PORT             default 8000
"""
import html
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urljoin, urlparse

import requests
import yt_dlp
from flask import Flask, Response, jsonify, request

app = Flask(__name__, static_folder="public", static_url_path="")
app.logger.setLevel(logging.INFO)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
PATH_RE = re.compile(r"^/(?:[\w.]+/)?(?:share/)?(p|reel|reels|tv)/([\w-]+)/?$")
CDN_SUFFIXES = (".cdninstagram.com", ".fbcdn.net")


# ---------- helpers ----------

def allowed(url):
    """Only Instagram/Facebook CDN hosts may be proxied (prevents SSRF)."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    host = (p.hostname or "").lower()
    return p.scheme == "https" and any(host.endswith(s) for s in CDN_SUFFIXES)


def clean_url(raw):
    """Return (canonical_url, shortcode) for a valid Instagram post link, else None."""
    try:
        p = urlparse(raw.strip())
    except Exception:
        return None
    if p.scheme not in ("http", "https"):
        return None
    if (p.hostname or "").lower() not in ("instagram.com", "www.instagram.com"):
        return None
    m = PATH_RE.match(p.path)
    if not m:
        return None
    return "https://www.instagram.com" + p.path.rstrip("/") + "/", m.group(2)


_hits = defaultdict(deque)
_lock = threading.Lock()


def client_ip():
    if os.environ.get("TRUST_PROXY") == "1":
        xff = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
        if xff:
            return xff
    return request.remote_addr or "unknown"


def too_many(bucket, limit, window=60):
    now = time.time()
    key = (bucket, client_ip())
    with _lock:
        if len(_hits) > 20000:
            for k in [k for k, q in _hits.items() if not q or now - q[-1] > window]:
                del _hits[k]
        q = _hits[key]
        while q and now - q[0] > window:
            q.popleft()
        if len(q) >= limit:
            return True
        q.append(now)
    return False


def extract(url):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 15,
        "retries": 1,
    }
    cookies = os.environ.get("IG_COOKIES_FILE")
    if cookies and os.path.isfile(cookies):
        opts["cookiefile"] = cookies
    proxy = os.environ.get("IG_PROXY")
    if proxy:
        opts["proxy"] = proxy
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def video_items(info):
    entries = info.get("entries") or [info]
    items = []
    for e in entries:
        if not e:
            continue
        formats = e.get("formats") or ([e] if e.get("url") else [])
        app.logger.info(
            "formats: %s",
            [(f.get("format_id"), f.get("height"), f.get("protocol"), f.get("vcodec"), f.get("acodec"), f.get("ext"))
             for f in formats],
        )
        best = {}
        for f in formats:
            u = f.get("url")
            if not u or not allowed(u):
                continue
            if f.get("protocol") not in ("http", "https", None):
                continue
            if f.get("vcodec") == "none" or f.get("acodec") == "none":
                continue  # skip audio-only / video-only DASH streams
            h = f.get("height") or 0
            if h not in best or (f.get("tbr") or 0) > (best[h].get("tbr") or 0):
                best[h] = f
        if not best:
            continue
        options = [
            {
                "label": f"{h}p" if h else "",
                "url": f["url"],
                "ext": f.get("ext") or "mp4",
                "size": f.get("filesize") or f.get("filesize_approx"),
            }
            for h, f in sorted(best.items(), key=lambda kv: kv[0], reverse=True)
        ]
        thumb = e.get("thumbnail")
        items.append(
            {
                "type": "video",
                "thumb": thumb if thumb and allowed(thumb) else None,
                "duration": e.get("duration"),
                "options": options,
            }
        )
    return items


def caption(info):
    desc = (info.get("description") or "").strip()
    if desc:
        return desc.splitlines()[0][:140]
    return (info.get("title") or "")[:140]


def meta(prop, page):
    for pat in (
        rf'<meta[^>]+property=["\']{prop}["\'][^>]+content=["\']([^"\']*)',
        rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']{prop}["\']',
    ):
        m = re.search(pat, page)
        if m:
            return html.unescape(m.group(1))
    return ""


def photo_fallback(url):
    """Best effort for photo posts, which yt-dlp does not handle."""
    try:
        r = requests.get(
            url,
            headers={
                "User-Agent": "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
                "Accept-Language": "en",
            },
            timeout=12,
        )
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    img = meta("og:image", r.text)
    if not img or not allowed(img):
        return None
    return {"url": img, "title": meta("og:title", r.text)[:140]}


def open_upstream(url, max_hops=3):
    """GET a CDN file. Redirects are followed only when they stay on allowed CDN hosts."""
    headers = {"User-Agent": UA, "Referer": "https://www.instagram.com/", "Accept": "*/*"}
    for _ in range(max_hops + 1):
        r = requests.get(url, headers=headers, stream=True, timeout=(8, 20), allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308):
            nxt = urljoin(url, r.headers.get("Location", ""))
            r.close()
            if not allowed(nxt):
                return None
            url = nxt
            continue
        return r
    return None


# ---------- routes ----------

@app.after_request
def headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    return resp


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/healthz")
def healthz():
    return "ok"


@app.post("/api/fetch")
def api_fetch():
    if too_many("fetch", 12):
        return jsonify(ok=False, error="rate"), 429

    data = request.get_json(silent=True) or {}
    cleaned = clean_url(str(data.get("url", "")))
    if not cleaned:
        return jsonify(ok=False, error="invalid"), 400
    url, code = cleaned

    try:
        info = extract(url)
    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "no video in this post" in msg:
            ph = photo_fallback(url)
            if ph:
                return jsonify(
                    ok=True,
                    id=code,
                    title=ph["title"],
                    uploader="",
                    items=[
                        {
                            "type": "image",
                            "thumb": ph["url"],
                            "duration": None,
                            "options": [{"label": "", "url": ph["url"], "ext": "jpg", "size": None}],
                        }
                    ],
                )
            return jsonify(ok=False, error="failed"), 404
        if any(k in msg for k in ("login", "logged", "private", "empty media response", "restricted")):
            return jsonify(ok=False, error="private"), 403
        if "429" in msg or "rate-limit" in msg or "rate limit" in msg:
            return jsonify(ok=False, error="rate"), 429
        app.logger.warning("yt-dlp failed for %s: %s", url, e)
        return jsonify(ok=False, error="failed"), 502
    except Exception:
        app.logger.exception("unexpected error for %s", url)
        return jsonify(ok=False, error="failed"), 500

    items = video_items(info)
    if not items:
        return jsonify(ok=False, error="failed"), 404

    return jsonify(
        ok=True,
        id=code,
        title=caption(info),
        uploader=info.get("uploader_id") or info.get("channel") or info.get("uploader") or "",
        items=items,
    )


@app.get("/api/media")
def api_media():
    """Stream a CDN file to the browser (thumbnails inline, downloads as attachment)."""
    if too_many("media", 90):
        return "Too many requests", 429
    u = request.args.get("u", "")
    if not allowed(u):
        return "Bad request", 400
    dl = request.args.get("dl") == "1"

    try:
        up = open_upstream(u)
    except requests.RequestException as e:
        app.logger.warning("media: request failed host=%s err=%s", urlparse(u).hostname, e)
        return "Upstream error", 502
    if up is None:
        app.logger.warning("media: redirect blocked or too many hops host=%s", urlparse(u).hostname)
        return "Upstream error", 502

    ctype = up.headers.get("Content-Type", "").split(";")[0].strip().lower()
    if ctype in ("", "application/octet-stream", "binary/octet-stream"):
        path = urlparse(u).path.lower()
        ctype = "video/mp4" if ".mp4" in path else "image/jpeg" if ".jpg" in path else ctype
    if up.status_code != 200 or not ctype.startswith(("video/", "image/")):
        app.logger.warning(
            "media: bad upstream status=%s type=%s host=%s",
            up.status_code, up.headers.get("Content-Type"), urlparse(u).hostname,
        )
        up.close()
        return "Upstream error", 502

    out = {"Cache-Control": "no-store" if dl else "private, max-age=3600"}
    if up.headers.get("Content-Length"):
        out["Content-Length"] = up.headers["Content-Length"]
    if dl:
        name = re.sub(r"[^A-Za-z0-9.\-]+", "_", request.args.get("name", "reelsave"))[:80] or "reelsave"
        out["Content-Disposition"] = f'attachment; filename="{name}"'

    def gen():
        try:
            for chunk in up.iter_content(65536):
                if chunk:
                    yield chunk
        finally:
            up.close()

    return Response(gen(), headers=out, content_type=ctype)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))

