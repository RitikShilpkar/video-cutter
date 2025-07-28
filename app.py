#!/usr/bin/env python3
import re, uuid, shlex, subprocess, random, os, base64
from pathlib import Path
from typing import List, Tuple

from flask import Flask, render_template, request, redirect, url_for, flash

# ------------------ paths ------------------
BASE_DIR = Path(__file__).resolve().parent
DL_DIR   = BASE_DIR / "downloads"
CUT_DIR  = BASE_DIR / "cuts"
OUT_DIR  = BASE_DIR / "static" / "outputs"
for p in (DL_DIR, CUT_DIR, OUT_DIR):
    p.mkdir(parents=True, exist_ok=True)

# Decode cookies.txt from environment variable if present
if "COOKIES_B64" in os.environ:
    with open("cookies.txt", "wb") as f:
        f.write(base64.b64decode(os.environ["COOKIES_B64"]))

# ------------------ flask ------------------
app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.secret_key = "dev-secret"  # change in prod

# ─────────────────────────────────────────────────────────────────────────────
# Invidious mirrors to rotate through
INVIDIOUS_MIRRORS = [
    "https://yewtu.be",
    "https://yewtu.eu",
    "https://yewtu.cafe",
    "https://yewtu.in",
]

# ─────────────────── helper functions ──────────────────── #
TS_RE = re.compile(r"\s*(\d+):(\d+)-(\d+):(\d+)\s*")

def parse_ts_list(ts_string: str) -> List[Tuple[float, float]]:
    out = []
    for part in ts_string.split(","):
        m = TS_RE.fullmatch(part.strip())
        if not m:
            raise ValueError(f"Bad timestamp: '{part}' (use mm:ss-mm:ss)")
        m1, s1, m2, s2 = map(int, m.groups())
        start = m1 * 60 + s1
        end   = m2 * 60 + s2
        if end <= start:
            raise ValueError(f"End <= start in '{part}'")
        out.append((start, end))
    return out

def run(cmd: str) -> None:
    proc = subprocess.run(shlex.split(cmd), stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "ignore"))

def download_youtube(url: str) -> Path:
    """
    Download video with yt-dlp, returning local MP4 path,
    rotating through Invidious mirrors first.
    """
    out_tmpl   = str(DL_DIR / "%(id)s.%(ext)s")
    cookies    = "cookies.txt"
    user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    last_err = None
    # 1) try each Invidious mirror
    for inv in random.sample(INVIDIOUS_MIRRORS, k=len(INVIDIOUS_MIRRORS)):
        cmd = (
            f"yt-dlp --cookies {shlex.quote(cookies)} "
            f"--user-agent {shlex.quote(user_agent)} "
            f"--extractor-args youtube:invidious_url={shlex.quote(inv)} "
            f"-f bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4 "
            f"-o {shlex.quote(out_tmpl)} {shlex.quote(url)}"
        )
        app.logger.info(f"[download] trying Invidious mirror {inv}")
        try:
            run(cmd)
            files = sorted(DL_DIR.glob("*.mp4"),
                           key=lambda p: p.stat().st_mtime,
                           reverse=True)
            if files:
                app.logger.info(f"[download] success via {inv}: {files[0]}")
                return files[0]
            last_err = "no file produced"
        except Exception as e:
            last_err = str(e)
            app.logger.warning(f"[download] {inv} failed: {last_err}")

    # 2) fallback to direct yt-dlp with cookies
    app.logger.info("[download] falling back to direct yt-dlp")
    try:
        cmd = (
            f"yt-dlp --cookies {shlex.quote(cookies)} "
            f"--user-agent {shlex.quote(user_agent)} "
            f"-f bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4 "
            f"-o {shlex.quote(out_tmpl)} {shlex.quote(url)}"
        )
        run(cmd)
        files = sorted(DL_DIR.glob("*.mp4"),
                       key=lambda p: p.stat().st_mtime,
                       reverse=True)
        if files:
            app.logger.info(f"[download] direct yt-dlp success: {files[0]}")
            return files[0]
        last_err = "no file produced in fallback"
    except Exception as e:
        last_err = str(e)
        app.logger.error(f"[download] fallback failed: {last_err}")

    # 3) give up
    raise RuntimeError(
        "Unable to download video after Invidious + direct attempts. "
        f"Last error: {last_err}"
    )

def cut_and_concat(src: Path, segments: List[Tuple[float, float]]) -> Path:
    cuts = []
    for i, (st, en) in enumerate(segments):
        out = CUT_DIR / f"cut_{uuid.uuid4().hex[:8]}_{i}.mp4"
        cmd = (
            f"ffmpeg -y -ss {st} -to {en} -i {shlex.quote(str(src))} "
            f"-c:v libx264 -c:a aac -preset veryfast -crf 23 {shlex.quote(str(out))}"
        )
        run(cmd)
        cuts.append(out)

    concat_list = CUT_DIR / f"concat_{uuid.uuid4().hex[:8]}.txt"
    concat_list.write_text("\n".join(f"file '{p}'" for p in cuts))

    final = OUT_DIR / f"clip_{uuid.uuid4().hex[:8]}.mp4"
    cmd = (
        f"ffmpeg -y -f concat -safe 0 -i {shlex.quote(str(concat_list))} "
        f"-c copy {shlex.quote(str(final))}"
    )
    run(cmd)
    return final

# ─────────────────── routes ─────────────────── #
@app.get("/")
def index():
    return render_template("index.html")

@app.post("/cut")
def cut():
    url  = request.form.get("url", "").strip()
    tsin = request.form.get("timestamps", "").strip()
    try:
        if not url:
            raise ValueError("Please provide a YouTube URL.")
        if not tsin:
            raise ValueError("Please provide timestamps.")

        segs = parse_ts_list(tsin)
        app.logger.info(f"Processing URL: {url} with {len(segs)} segments")

        src = download_youtube(url)
        app.logger.info(f"Downloaded video to: {src}")

        final_mp4 = cut_and_concat(src, segs)
        app.logger.info(f"Created clip: {final_mp4}")

        return redirect(url_for("preview", vid=final_mp4.name))
    except Exception as e:
        app.logger.exception("Cut failed")
        flash(f"Error: {str(e)}", "error")
        return redirect(url_for("index"))

@app.get("/preview/<vid>")
def preview(vid: str):
    vid_path = OUT_DIR / vid
    if not vid_path.exists():
        app.logger.error(f"Video not found: {vid_path}")
        flash(f"Video file not found: {vid}", "error")
        return redirect(url_for("index"))
    return render_template("preview.html",
                           video_file=url_for("static", filename=f"outputs/{vid}"))

@app.get("/debug")
def debug():
    import os
    return {
        "app_running": True,
        "cookies":     os.path.exists("cookies.txt"),
        "downloads":   len(list(DL_DIR.glob("*"))),
        "outputs":     len(list(OUT_DIR.glob("*"))),
        "mirrors":     INVIDIOUS_MIRRORS,
    }

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
