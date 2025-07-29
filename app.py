#!/usr/bin/env python3
import os
import re
import uuid
import shlex
import subprocess
import base64
import json
from pathlib import Path
from typing import List, Tuple
from urllib.parse import urlencode

import requests
from flask import Flask, render_template, request, redirect, url_for, flash

# ── CONFIG & PATHS ────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
DL_DIR   = BASE_DIR / "downloads"
CUT_DIR  = BASE_DIR / "cuts"
OUT_DIR  = BASE_DIR / "static" / "outputs"
for d in (DL_DIR, CUT_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

# allow cookies via env
if "COOKIES_B64" in os.environ:
    with open("cookies.txt", "wb") as f:
        f.write(base64.b64decode(os.environ["COOKIES_B64"]))

# ── FLASK SETUP ───────────────────────────────────────────────────────────
app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static")
)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret")


# ── HELPERS ────────────────────────────────────────────────────────────────
TS_RE = re.compile(r"\s*(\d+):(\d+)-(\d+):(\d+)\s*")
def parse_ts_list(ts_string: str) -> List[Tuple[float, float]]:
    out = []
    for part in ts_string.split(","):
        m = TS_RE.fullmatch(part.strip())
        if not m:
            raise ValueError(f"Bad timestamp: '{part}' (must be mm:ss-mm:ss)")
        m1, s1, m2, s2 = map(int, m.groups())
        st, en = m1*60 + s1, m2*60 + s2
        if en <= st:
            raise ValueError("End time must be after start time")
        out.append((st, en))
    return out

def run(cmd: str, env: dict = None):
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    p = subprocess.run(shlex.split(cmd),
                       stderr=subprocess.PIPE,
                       env=full_env)
    if p.returncode:
        raise RuntimeError(p.stderr.decode("utf-8", "ignore"))

# ---- InnerTube JSON downloader -----------------------------------------
class InnerTubeError(Exception):
    pass

def download_via_innertube(video_id: str) -> str:
    """
    Fetch the /youtubei/v1/player JSON and pull out the highest‐quality mp4 URL.
    Returns the direct URL to the .mp4 stream.
    """
    # You’ll need a valid INNERTUBE_API_KEY and client version; you can scrape these
    # from YouTube’s page, but here we assume they’re set in env:
    key = os.environ.get("INNERTUBE_API_KEY")
    client_ver = os.environ.get("INNERTUBE_CLIENT_VERSION")
    if not key or not client_ver:
        raise InnerTubeError("No InnerTube API key/client available")

    endpoint = "https://www.youtube.com/youtubei/v1/player?" + urlencode({
        "key": key
    })
    payload = {
        "context": {
            "client": {
                "clientName": "WEB",
                "clientVersion": client_ver
            }
        },
        "videoId": video_id
    }
    resp = requests.post(endpoint, json=payload, timeout=10)
    data = resp.json()
    # navigate JSON → streamingData → formats
    fmts = data.get("streamingData", {}).get("formats", [])
    if not fmts:
        raise InnerTubeError("No formats in InnerTube response")
    # pick highest‐quality mp4
    mp4s = [f for f in fmts if f.get("mimeType", "").startswith("video/mp4")]
    if not mp4s:
        raise InnerTubeError("No MP4 format in InnerTube")
    best = max(mp4s, key=lambda f: f.get("height", 0))
    url = best.get("url")
    if not url:
        raise InnerTubeError("No URL field in selected format")
    return url

def download_via_innertube_to_file(url: str, dest: Path):
    run(f"ffmpeg -y -i {shlex.quote(url)} "
        f"-c copy {shlex.quote(str(dest))}")

def download_via_ytdlp(url: str) -> Path:
    out_tmpl = str(DL_DIR / "%(id)s.%(ext)s")
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    cmd = (
        f"yt-dlp --user-agent {shlex.quote(ua)} "
        f"-f bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4 "
        f"-o {shlex.quote(out_tmpl)} {shlex.quote(url)}"
    )
    run(cmd)
    files = sorted(DL_DIR.glob("*.mp4"),
                   key=lambda p: p.stat().st_mtime,
                   reverse=True)
    if not files:
        raise FileNotFoundError("yt-dlp download failed")
    return files[0]

def download_youtube(url: str) -> Path:
    """
    Try InnerTube first; on any failure, fall back to yt-dlp.
    """
    # extract video ID
    m = re.search(r"(?:v=|youtu\.be/)([A-Za-z0-9_-]{11})", url)
    if not m:
        return download_via_ytdlp(url)
    vid = m.group(1)

    # attempt InnerTube
    try:
        app.logger.info(f"[InnerTube] fetching stream for {vid}")
        stream_url = download_via_innertube(vid)
        dest = DL_DIR / f"{vid}.mp4"
        download_via_innertube_to_file(stream_url, dest)
        return dest
    except InnerTubeError as e:
        app.logger.warning(f"InnerTube failed ({e}), falling back to yt-dlp")
        return download_via_ytdlp(url)

# ── CUT & CONCAT ──────────────────────────────────────────────────────────
def cut_and_concat(src: Path, segments: List[Tuple[float, float]]) -> Path:
    cuts = []
    for i, (st, en) in enumerate(segments):
        out = CUT_DIR / f"cut_{uuid.uuid4().hex[:8]}_{i}.mp4"
        cmd = (
            f"ffmpeg -y -ss {st} -to {en} -i {shlex.quote(str(src))} "
            f"-c:v libx264 -c:a aac -preset veryfast -crf 23 "
            f"{shlex.quote(str(out))}"
        )
        run(cmd)
        cuts.append(out)
    lst = CUT_DIR / f"concat_{uuid.uuid4().hex[:8]}.txt"
    lst.write_text("\n".join(f"file '{p}'" for p in cuts))
    final = OUT_DIR / f"clip_{uuid.uuid4().hex[:8]}.mp4"
    run(
        f"ffmpeg -y -f concat -safe 0 "
        f"-i {shlex.quote(str(lst))} -c copy {shlex.quote(str(final))}"
    )
    return final


# ── ROUTES ────────────────────────────────────────────────────────────────
@app.route("/", methods=("GET", "POST"))
def index():
    if request.method == "POST":
        url  = request.form["url"].strip()
        tsin = request.form["timestamps"].strip()
        try:
            segs = parse_ts_list(tsin)
            src  = download_youtube(url)
            clip = cut_and_concat(src, segs)
            return redirect(url_for("preview", vid=clip.name))
        except Exception as e:
            app.logger.exception("Cut failed")
            flash(str(e), "error")
    return render_template("index.html")

@app.route("/preview/<vid>")
def preview(vid: str):
    path = OUT_DIR / vid
    if not path.exists():
        flash("Clip not found", "error")
        return redirect(url_for("index"))
    return render_template("preview.html",
                           video_file=url_for("static", filename=f"outputs/{vid}"))

@app.route("/debug")
def debug():
    return {
        "downloads": len(list(DL_DIR.glob("*.mp4"))),
        "outputs":   len(list(OUT_DIR.glob("*.mp4"))),
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
