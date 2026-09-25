#!/usr/bin/env python3
"""
innertube.py — a from-scratch YouTube download engine.

No yt-dlp, no youtube-dl, no scraping of watch pages. Talks directly to
YouTube's Innertube player API using the Android/iOS app client contexts
(which are not subject to the web client's bot check), downloads the
adaptive streams over HTTPS (parallel ranged workers), and muxes with ffmpeg.

Public API:
    get_info(url_or_id, proxy=None) -> dict          # metadata + formats
    quality_options(info) -> list[dict]              # picker entries
    download(url_or_id, quality, out_dir, ...) -> Path
    YouTubeError                                     # clean, user-facing errors
"""

from __future__ import annotations

import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

API_ENDPOINT = "https://www.youtube.com/youtubei/v1/player"

# Mobile app client contexts. These pass YouTube's bot check from server IPs
# where the WEB/TV clients get "Sign in to confirm you're not a bot".
CLIENTS = {
    "android": {
        "clientName": "ANDROID",
        "clientVersion": "20.10.38",
        "androidSdkVersion": 33,
        "platform": "MOBILE",
        "userAgent": "com.google.android.youtube/20.10.38 (Linux; U; Android 13; en_US) gzip",
    },
    "ios": {
        "clientName": "IOS",
        "clientVersion": "20.10.4",
        "deviceMake": "Apple",
        "deviceModel": "iPhone16,2",
        "platform": "MOBILE",
        "osName": "iPhone",
        "osVersion": "18.3.0.22D50",
        "userAgent": "com.google.ios.youtube/20.10.4 (iPhone16,2; U; CPU iOS 18_3_0 like Mac OS X; en_US)",
    },
}
CLIENT_ORDER = ("android", "ios")

_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_URL_ID_PATTERNS = (
    re.compile(r"[?&]v=([A-Za-z0-9_-]{11})"),          # watch?v=
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{11})"),      # short links
    re.compile(r"/shorts/([A-Za-z0-9_-]{11})"),
    re.compile(r"/embed/([A-Za-z0-9_-]{11})"),
    re.compile(r"/live/([A-Za-z0-9_-]{11})"),
    re.compile(r"/v/([A-Za-z0-9_-]{11})"),
)


class YouTubeError(Exception):
    """A clean, user-facing error."""


# ------------------------------------------------------------------ parsing

def extract_video_id(url_or_id: str) -> str:
    s = (url_or_id or "").strip()
    if _VIDEO_ID_RE.fullmatch(s):
        return s
    for pat in _URL_ID_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(1)
    raise YouTubeError(
        "That doesn't look like a YouTube video link. "
        "Supported: watch?v=…, youtu.be/…, /shorts/…, /embed/…, /live/…")


def _parse_mime(mime: str) -> tuple[str, str]:
    """'video/mp4; codecs="avc1.64001f"' -> ('mp4', 'avc1')"""
    container = (mime.split(";")[0].split("/")[-1] or "").strip()
    m = re.search(r'codecs="?([\w.\-+]+)"?', mime)
    codec = m.group(1).split(".")[0] if m else ""
    return container, codec


def _fmt_int(v) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------ innertube

def _player_request(video_id: str, client: str, proxy: str | None) -> dict:
    cfg = CLIENTS[client]
    body = {"context": {"client": {k: v for k, v in cfg.items() if k != "userAgent"}},
            "videoId": video_id}
    try:
        r = requests.post(
            API_ENDPOINT,
            params={"prettyPrint": "false"},
            json=body,
            headers={"User-Agent": cfg["userAgent"], "Content-Type": "application/json"},
            proxies={"https": proxy, "http": proxy} if proxy else None,
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except requests.RequestException as e:
        raise YouTubeError(f"Network error talking to YouTube: {e}") from e


def _normalize_format(f: dict) -> dict | None:
    if not f.get("url"):                      # signatureCipher-only: skip
        return None
    mime, codec = _parse_mime(f.get("mimeType", ""))
    is_audio = f.get("mimeType", "").startswith("audio/")
    height = _fmt_int(f.get("height"))
    return {
        "itag": f.get("itag"),
        "url": f["url"],
        "container": mime,
        "codec": codec,
        "audio_only": is_audio,
        "video_only": not is_audio and not f.get("audioQuality"),
        "progressive": not is_audio and bool(f.get("audioQuality")),
        "width": _fmt_int(f.get("width")),
        "height": height,
        "fps": _fmt_int(f.get("fps")) or 30,
        "bitrate": _fmt_int(f.get("averageBitrate")) or _fmt_int(f.get("bitrate")) or 0,
        "size": _fmt_int(f.get("contentLength")),
        "quality_label": f.get("qualityLabel") or (f"{height}p" if height else ""),
        "audio_quality": f.get("audioQuality", ""),
    }


def get_info(url_or_id: str, proxy: str | None = None) -> dict:
    video_id = extract_video_id(url_or_id)

    data, err = None, None
    for client in CLIENT_ORDER:
        data = _player_request(video_id, client, proxy)
        ps = data.get("playabilityStatus", {})
        if ps.get("status") == "OK" and data.get("streamingData"):
            err = None
            break
        err = ps.get("reason") or ps.get("messages", [""])[0] or ps.get("status") or "unavailable"
    if err:
        low = err.lower()
        if "sign in" in low or "bot" in low or "login" in low:
            raise YouTubeError(
                "YouTube demands a login for this video (age restriction or a hard IP "
                "block). Try again later, or run the tool through a residential proxy.")
        raise YouTubeError(f"YouTube refused this video: {err}")

    sd = data.get("streamingData", {})
    raw = (sd.get("formats") or []) + (sd.get("adaptiveFormats") or [])
    formats = [f for f in (_normalize_format(x) for x in raw) if f]
    if not formats:
        if sd.get("hlsManifestUrl") or sd.get("serverAbrStream"):
            raise YouTubeError("This is a live stream — nothing to download yet.")
        raise YouTubeError("YouTube returned no downloadable streams for this video.")

    vd = data.get("videoDetails", {})
    duration = _fmt_int(vd.get("lengthSeconds"))
    # fill missing sizes from bitrate × duration
    for f in formats:
        if not f["size"] and duration and f["bitrate"]:
            f["size"] = int(f["bitrate"] * duration / 8)

    thumbs = (vd.get("thumbnail", {}) or {}).get("thumbnails") or []
    return {
        "id": video_id,
        "title": vd.get("title") or f"YouTube video {video_id}",
        "channel": vd.get("author") or "",
        "duration": duration,
        "views": _fmt_int(vd.get("viewCount")),
        "thumbnail": thumbs[-1]["url"] if thumbs else None,
        "formats": formats,
    }


# ------------------------------------------------------------------ options

def quality_options(info: dict) -> list[dict]:
    fmts = info["formats"]
    videos = [f for f in fmts if not f["audio_only"]]
    audios = [f for f in fmts if f["audio_only"]]
    best_audio_size = max((a["size"] or 0) for a in audios) if audios else 0

    options = [{"id": "best", "label": "Best available", "size": None}]
    heights = sorted({f["height"] for f in videos if f["height"]}, reverse=True)
    for h in heights[:8]:
        vsize = max((f["size"] or 0) for f in videos if f["height"] == h)
        total = vsize + best_audio_size if vsize else 0
        options.append({"id": str(h), "label": f"{h}p", "size": _human(total) if total else None})
    if audios:
        options.append({"id": "audio", "label": "Audio only (MP3)",
                        "size": _human(best_audio_size) if best_audio_size else None})
    return options


def _human(n) -> str | None:
    if not n:
        return None
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return None


# ------------------------------------------------------------------ picking

def _best_audio(fmts: list[dict], prefer_container: str) -> dict | None:
    audios = [f for f in fmts if f["audio_only"]]
    if not audios:
        return None
    pref = [f for f in audios if f["container"] == prefer_container] or audios
    return max(pref, key=lambda f: f["bitrate"])


def _codec_rank(codec: str) -> int:
    """Prefer universal compatibility, then efficiency: h264 > av1 > vp9 > rest."""
    c = (codec or "").lower()
    if c.startswith("avc1"):
        return 0
    if c.startswith("av01"):
        return 1
    if c.startswith(("vp9", "vp09")):
        return 2
    return 3


def _pick_streams(info: dict, quality: str) -> tuple[list[dict], str]:
    """Return (streams_to_download, mode) where mode ∈ {progressive, mux, audio}."""
    fmts = info["formats"]
    if quality == "audio":
        a = _best_audio(fmts, "mp4")
        if not a:
            raise YouTubeError("This video has no separate audio stream.")
        return [a], "audio"

    videos_only = [f for f in fmts if f["video_only"]]
    progressive = [f for f in fmts if f["progressive"]]

    def cap(fs, h):
        return [f for f in fs if (f["height"] or 0) <= h]

    def vkey(f):
        return (f["height"] or 0, -_codec_rank(f["codec"]), f["bitrate"])

    if quality == "best":
        v_pool, p_pool = videos_only, progressive
    else:
        h = int(quality)
        v_pool, p_pool = cap(videos_only, h), cap(progressive, h)
        if not v_pool and not p_pool:          # nothing at/below cap → go lower-bound
            v_pool, p_pool = videos_only, progressive

    if v_pool:
        v = max(v_pool, key=vkey)
        a = _best_audio(fmts, "mp4" if _codec_rank(v["codec"]) <= 1 else "webm")
        if a:
            return [v, a], "mux"
        return [v], "progressive"              # video without audio track (rare)
    if p_pool:
        p = max(p_pool, key=vkey)
        return [p], "progressive"
    raise YouTubeError("No video stream matches the requested quality.")


def _container_for(video: dict, audio: dict | None) -> str:
    if audio is None:
        return video["container"] or "mp4"
    vrank, ac = _codec_rank(video["codec"]), audio["codec"]
    if vrank <= 1 and ac.startswith("mp4a"):
        return "mp4"
    if ac == "opus" and (video["codec"].startswith(("vp9", "vp09", "av01"))):
        return "webm"
    return "mkv"


# ------------------------------------------------------------------ transfer

def _session(proxy: str | None) -> requests.Session:
    s = requests.Session()
    if proxy:
        s.proxies.update({"https": proxy, "http": proxy})
    return s


class _Source:
    """A resumable, refreshable stream source.

    googlevideo stream URLs pin a particular edge server, and some edges
    answer 403 to datacenter IPs while others serve fine. On a 403 we ask
    the player API for a fresh URL (usually a different edge) and carry on —
    ranged requests are content-based, so a partial file stays valid.
    """

    def __init__(self, provider, proxy: str | None):
        self.provider = provider          # callable -> fresh url
        self.proxy = proxy
        self.lock = threading.Lock()
        self.gen = 0
        self.refreshes = 0
        self.url = provider()
        self.sess = _session(proxy)

    def refresh(self, gen: int) -> None:
        with self.lock:
            if gen != self.gen:            # another thread already refreshed
                return
            if self.refreshes >= 4:
                raise YouTubeError(
                    "YouTube's CDN keeps refusing this stream (HTTP 403). "
                    "Try again in a minute, or use a proxy.")
            self.refreshes += 1
            self.gen += 1
            self.sess.close()
            self.sess = _session(self.proxy)
            self.url = self.provider()


def _fetch_range(src: _Source, start: int, end: int, dest: Path,
                 counter, lock, on_progress, stop) -> None:
    """Download bytes [start, end] into dest, resuming from a partial file."""
    got = dest.stat().st_size if dest.exists() else 0
    pos = start + got
    tries = 0
    while pos <= end:
        if stop.is_set():
            raise YouTubeError("Cancelled")
        tries += 1
        if tries > 6:
            raise YouTubeError("Too many retries on one chunk")
        url, gen, sess = src.url, src.gen, src.sess
        headers = {"Range": f"bytes={pos}-{end}"}
        try:
            with sess.get(url, headers=headers, stream=True, timeout=30) as r:
                if r.status_code == 403:
                    src.refresh(gen)
                    continue
                if r.status_code not in (200, 206):
                    raise YouTubeError(f"YouTube returned HTTP {r.status_code}")
                with open(dest, "ab" if pos > start else "wb") as fh:
                    for chunk in r.iter_content(1 << 16):
                        if not chunk:
                            continue
                        fh.write(chunk)
                        pos += len(chunk)
                        with lock:
                            counter[0] += len(chunk)
                        on_progress(counter[0])
            tries = 0
        except requests.RequestException:
            time.sleep(1.5 * tries)


def _download_stream(provider, dest: Path, size: int | None, proxy: str | None,
                     counter, lock, on_progress, stop, workers: int = 4) -> None:
    src = _Source(provider, proxy)
    if size and size > 8_000_000 and workers > 1:
        part = max(2_000_000, -(-size // workers))
        ranges = [(s, min(s + part - 1, size - 1)) for s in range(0, size, part)]
        dest.parent.mkdir(parents=True, exist_ok=True)
        parts = [dest.parent / f"{dest.name}.part{i}" for i in range(len(ranges))]
        errors = []

        def work(i):
            try:
                _fetch_range(src, ranges[i][0], ranges[i][1], parts[i],
                             counter, lock, on_progress, stop)
            except Exception as e:            # noqa: BLE001
                errors.append(e)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(work, range(len(ranges))))
        if errors:
            for p in parts:
                p.unlink(missing_ok=True)
            raise errors[0]
        with open(dest, "wb") as out:
            for p in parts:
                with open(p, "rb") as fh:
                    shutil.copyfileobj(fh, out)
                p.unlink(missing_ok=True)
    elif size:
        _fetch_range(src, 0, size - 1, dest, counter, lock, on_progress, stop)
    else:
        _fetch_unsized(src, dest, counter, lock, on_progress, stop)


def _fetch_unsized(src: _Source, dest: Path, counter, lock, on_progress, stop):
    tries = 0
    while True:
        tries += 1
        if tries > 4:
            raise YouTubeError("Download kept failing")
        url, gen, sess = src.url, src.gen, src.sess
        try:
            with sess.get(url, stream=True, timeout=30) as r:
                if r.status_code == 403:
                    src.refresh(gen)
                    continue
                r.raise_for_status()
                with open(dest, "wb") as fh:
                    for chunk in r.iter_content(1 << 16):
                        if stop.is_set():
                            raise YouTubeError("Cancelled")
                        fh.write(chunk)
                        with lock:
                            counter[0] += len(chunk)
                        on_progress(counter[0])
            return
        except requests.RequestException:
            time.sleep(1.5 * tries)




def _make_provider(url_or_id: str, fmt: dict, proxy: str | None):
    """URL supplier for _Source: first call is free, later calls re-query
    the player API for a fresh edge URL of the same itag."""
    state = {"first": True}

    def prov():
        if state["first"]:
            state["first"] = False
            return fmt["url"]
        info = get_info(url_or_id, proxy)
        m = next((x for x in info["formats"] if x["itag"] == fmt["itag"]), None)
        if not m:
            raise YouTubeError("Stream disappeared while refreshing its URL.")
        return m["url"]

    return prov


# ------------------------------------------------------------------ ffmpeg

def _ffmpeg(args: list[str]) -> None:
    exe = shutil.which("ffmpeg")
    if not exe:
        raise YouTubeError("ffmpeg is not installed — needed to combine video+audio "
                           "or convert to MP3. See README.")
    r = subprocess.run([exe, "-y", "-loglevel", "error", *args],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise YouTubeError(f"ffmpeg failed: {r.stderr.strip()[:300]}")


# ------------------------------------------------------------------ public dl

def sanitize(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip().rstrip(".")
    return name[:120] or "video"


def download(url_or_id: str, quality: str, out_dir: Path,
             on_progress=None, on_phase=None, proxy: str | None = None,
             audio_format: str = "mp3") -> Path:
    """Download one video. on_progress(done_bytes, total_bytes, speed_bps)."""
    on_progress = on_progress or (lambda *a: None)
    on_phase = on_phase or (lambda *a: None)

    info = get_info(url_or_id, proxy)
    streams, mode = _pick_streams(info, quality)
    total = sum(f["size"] or 0 for f in streams) or None

    counter, lock = [0], threading.Lock()
    stop = threading.Event()
    last = {"t": time.time(), "b": 0}

    def cb(done):
        now = time.time()
        dt = now - last["t"]
        speed = (done - last["b"]) / dt if dt > 0.4 else None
        if speed is not None:
            last.update(t=now, b=done)
        on_progress(done, total, speed)

    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp = out_dir / ".parts"
    tmp.mkdir(exist_ok=True)

    on_phase("Downloading…")
    paths = []
    for i, f in enumerate(streams):
        dest = tmp / f"stream{i}.{f['container'] or 'bin'}"
        _download_stream(_make_provider(url_or_id, f, proxy), dest, f["size"],
                         proxy, counter, lock, cb, stop)
        paths.append(dest)

    base = f"{sanitize(info['title'])} [{info['id']}]"

    if mode == "audio":
        on_phase("Converting audio…")
        out = out_dir / f"{base}.{audio_format}"
        if audio_format == "mp3":
            _ffmpeg(["-i", str(paths[0]), "-vn", "-acodec", "libmp3lame", "-q:a", "0", str(out)])
        elif paths[0].suffix.strip(".") == audio_format:
            paths[0].rename(out)
        else:
            _ffmpeg(["-i", str(paths[0]), "-vn", "-acodec", "copy", str(out)])
    elif mode == "mux":
        on_phase("Merging video + audio…")
        ext = _container_for(streams[0], streams[1])
        out = out_dir / f"{base}.{ext}"
        _ffmpeg(["-i", str(paths[0]), "-i", str(paths[1]),
                 "-c", "copy", "-movflags", "+faststart", str(out)])
    else:
        ext = streams[0]["container"] or "mp4"
        out = out_dir / f"{base}.{ext}"
        paths[0].rename(out)

    shutil.rmtree(tmp, ignore_errors=True)
    on_phase("Done")
    return out
