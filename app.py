#!/usr/bin/env python3
import re, uuid, shlex, subprocess, os, base64
from pathlib import Path
from typing import List, Tuple

from flask import Flask, render_template, request, redirect, url_for, flash
from pytube import YouTube

# ─── Directories ────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DL_DIR   = BASE_DIR / "downloads"
CUT_DIR  = BASE_DIR / "cuts"
OUT_DIR  = BASE_DIR / "static" / "outputs"
for d in (DL_DIR, CUT_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

# If you supply a base64‑encoded cookies.txt via env, decode it
if "COOKIES_B64" in os.environ:
    with open(BASE_DIR / "cookies.txt", "wb") as f:
        f.write(base64.b64decode(os.environ["COOKIES_B64"]))

# ─── Flask app setup ────────────────────────────────────────────────────────
app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret")

# ─── Timestamp parsing ──────────────────────────────────────────────────────
TS_RE = re.compile(r"\s*(\d+):(\d+)-(\d+):(\d+)\s*")
def parse_ts_list(ts_string: str) -> List[Tuple[float, float]]:
    out = []
    for part in ts_string.split(","):
        m = TS_RE.fullmatch(part.strip())
        if not m:
            raise ValueError(f"Bad timestamp: '{part}' (use mm:ss-mm:ss)")
        m1, s1, m2, s2 = map(int, m.groups())
        start, end = m1*60 + s1, m2*60 + s2
        if end <= start:
            raise ValueError(f"End <= start in '{part}'")
        out.append((start, end))
    return out

# ─── Shell runner ──────────────────────────────────────────────────────────
def run(cmd: str) -> None:
    proc = subprocess.run(shlex.split(cmd), stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "ignore"))

# ─── Primary downloader: pytube ─────────────────────────────────────────────
def download_with_pytube(url: str) -> Path:
    yt = YouTube(url)
    # pick highest‑res progressive mp4
    stream = yt.streams.filter(progressive=True, file_extension="mp4") \
                       .order_by("resolution").desc().first()
    if not stream:
        raise RuntimeError("No progressive MP4 stream available")
    out_path = DL_DIR / f"{yt.video_id}.mp4"
    stream.download(output_path=str(DL_DIR), filename=out_path.name)
    app.logger.info(f"[pytube] downloaded: {out_path.name}")
    return out_path

# ─── Fallback downloader: Invidious + yt‑dlp ─────────────────────────────────
def download_via_ytdlp(url: str) -> Path:
    # you can rotate through multiple Invidious instances here if you like...
    cmd = (
        "yt-dlp "
        "--cookies cookies.txt "
        "--user-agent \"Mozilla/5.0 (X11; Linux x86_64)\" "
        "--sleep-interval 2 --max-sleep-interval 5 "
        "-f bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4 "
        "-o \"downloads/%(id)s.%(ext)s\" "
        f"{shlex.quote(url)}"
    )
    run(cmd)
    files = sorted(DL_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise FileNotFoundError("yt-dlp fallback failed to produce an MP4")
    app.logger.info(f"[yt-dlp] fallback downloaded: {files[0].name}")
    return files[0]

# ─── Unified download entrypoint ────────────────────────────────────────────
def download_youtube(url: str) -> Path:
    try:
        return download_with_pytube(url)
    except Exception as e:
        app.logger.warning(f"[pytube] failed ({e}), falling back to yt-dlp…")
        return download_via_ytdlp(url)

# ─── Cutting + concatenation ────────────────────────────────────────────────
def cut_and_concat(src: Path, segments: List[Tuple[float, float]]) -> Path:
    cuts = []
    for i, (st, en) in enumerate(segments):
        out = CUT_DIR / f"cut_{uuid.uuid4().hex[:8]}_{i}.mp4"
        cmd = (
            f"ffmpeg -y -ss {st} -to {en} -i {src} "
            f"-c:v libx264 -c:a aac -preset veryfast -crf 23 {out}"
        )
        run(cmd)
        cuts.append(out)
    # build concat list
    lst = CUT_DIR / f"concat_{uuid.uuid4().hex[:8]}.txt"
    lst.write_text("\n".join(f"file '{p}'" for p in cuts))
    final = OUT_DIR / f"clip_{uuid.uuid4().hex[:8]}.mp4"
    run(f"ffmpeg -y -f concat -safe 0 -i {lst} -c copy {final}")
    return final

# ─── Routes ────────────────────────────────────────────────────────────────
@app.get("/")
def index():
    return render_template("index.html")

@app.post("/cut")
def cut():
    url  = request.form.get("url","").strip()
    tsin = request.form.get("timestamps","").strip()
    try:
        if not url or not tsin:
            raise ValueError("You must supply both URL and timestamps")
        segs = parse_ts_list(tsin)
        src  = download_youtube(url)
        out  = cut_and_concat(src, segs)
        return redirect(url_for("preview", vid=out.name))
    except Exception as e:
        app.logger.exception("Cut failed")
        flash(str(e), "error")
        return redirect(url_for("index"))

@app.get("/preview/<vid>")
def preview(vid: str):
    path = OUT_DIR / vid
    if not path.exists():
        flash("Clip not found", "error")
        return redirect(url_for("index"))
    return render_template("preview.html", video_file=url_for("static", filename=f"outputs/{vid}"))

@app.get("/debug")
def debug():
    return {
        "pytube_installed": True,
        "downloads": len(list(DL_DIR.glob("*"))),
        "cuts": len(list(CUT_DIR.glob("*"))),
        "outputs": len(list(OUT_DIR.glob("*"))),
    }

if __name__ == "__main__":
    # production: bind 0.0.0.0:$PORT via Gunicorn
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)), debug=False)
