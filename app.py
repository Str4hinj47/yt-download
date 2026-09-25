#!/usr/bin/env python3
"""
YouTube Downloader — web app backed by a custom Innertube engine (no yt-dlp).

Run:  python3 app.py   →   http://localhost:8000
Optional: YT_DOWNLOADER_PROXY=http://user:pass@host:port  to route through a proxy.
"""

import os
import shutil
import threading
import time
import uuid
from pathlib import Path

from flask import Flask, abort, jsonify, render_template, request, send_file

from engine import innertube as yt

BASE = Path(__file__).resolve().parent
DOWNLOADS = BASE / "downloads"
DOWNLOADS.mkdir(exist_ok=True)
PROXY = os.environ.get("YT_DOWNLOADER_PROXY") or None

MAX_JOB_AGE = 2 * 3600  # finished files are kept for 2 hours

app = Flask(__name__)
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()


def sweep_old_jobs() -> None:
    now = time.time()
    with LOCK:
        for jid in [j for j, job in JOBS.items()
                    if job.get("done_at") and now - job["done_at"] > MAX_JOB_AGE]:
            shutil.rmtree(JOBS[jid]["dir"], ignore_errors=True)
            del JOBS[jid]


# ---------------------------------------------------------------- routes

@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/info")
def api_info():
    url = ((request.get_json(silent=True) or {}).get("url") or "").strip()
    if not url:
        return jsonify(error="Please paste a YouTube link first."), 400
    try:
        info = yt.get_info(url, PROXY)
    except yt.YouTubeError as e:
        return jsonify(error=str(e)), 400
    except Exception as e:  # noqa: BLE001
        return jsonify(error=f"Unexpected error: {e}"), 500
    return jsonify({
        "id": info["id"],
        "title": info["title"],
        "channel": info["channel"],
        "duration": info["duration"],
        "views": info["views"],
        "thumbnail": info["thumbnail"],
        "url": url,
        "options": yt.quality_options(info),
    })


@app.post("/api/download")
def api_download():
    sweep_old_jobs()
    body = request.get_json(silent=True) or {}
    url = (body.get("url") or "").strip()
    quality = str(body.get("quality") or "best")
    if not url:
        return jsonify(error="Missing URL."), 400
    if quality not in ("best", "audio") and not quality.isdigit():
        return jsonify(error="Bad quality value."), 400

    job_id = uuid.uuid4().hex[:12]
    job_dir = DOWNLOADS / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    job = {
        "id": job_id, "dir": job_dir,
        "status": "starting", "phase": "Starting…",
        "progress": None, "speed": None, "eta": None,
        "file": None, "filename": None, "size": None,
        "error": None, "done_at": None,
    }
    with LOCK:
        JOBS[job_id] = job
    threading.Thread(target=run_download, args=(job, url, quality), daemon=True).start()
    return jsonify(job_id=job_id)


def run_download(job: dict, url: str, quality: str) -> None:
    def update(**kw):
        with LOCK:
            job.update(kw)

    def on_progress(done, total, speed):
        pct = round(done / total * 100, 1) if total else None
        eta = (total - done) / speed if (speed and total and speed > 0) else None
        update(status="downloading", phase="Downloading…",
               progress=pct, speed=speed, eta=eta)

    def on_phase(phase):
        if phase in ("Merging video + audio…", "Converting audio…"):
            update(status="processing", phase=phase, progress=None)
        elif phase == "Done":
            update(phase="Done")
        else:
            update(status="downloading", phase=phase)

    try:
        result = yt.download(url, quality, job["dir"],
                             on_progress=on_progress, on_phase=on_phase, proxy=PROXY)
        update(status="done", phase="Done", progress=100.0,
               file=str(result), filename=result.name,
               size=yt._human(result.stat().st_size), done_at=time.time())
    except Exception as e:  # noqa: BLE001
        msg = str(e).strip().splitlines()[0] if str(e).strip() else "Download failed."
        update(status="error", phase="Failed", error=msg, done_at=time.time())


@app.get("/api/status/<job_id>")
def api_status(job_id):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            return jsonify(error="Unknown or expired job."), 404
        public = {k: job[k] for k in
                  ("id", "status", "phase", "progress", "speed", "eta",
                   "filename", "size", "error")}
    return jsonify(public)


@app.get("/api/file/<job_id>")
def api_file(job_id):
    with LOCK:
        job = JOBS.get(job_id)
    if not job or job.get("status") != "done" or not job.get("file"):
        abort(404, "File not available (job unknown, still running, or expired).")
    path = Path(job["file"])
    if not path.is_file():
        abort(404, "File no longer exists on disk.")
    return send_file(path, as_attachment=True, download_name=path.name)


if __name__ == "__main__":
    print("YouTube Downloader running on http://0.0.0.0:8000")
    app.run(host="0.0.0.0", port=8000, threaded=True)
