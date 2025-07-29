#!/usr/bin/env python3
import os
import re
import uuid
import shlex
import subprocess
from pathlib import Path
from typing import List, Tuple
import base64

from flask import (
    Flask, render_template, request,
    redirect, url_for, flash
)

# ── CONFIG & PATHS ────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent.resolve()
DL_DIR   = BASE_DIR / "downloads"
CUT_DIR  = BASE_DIR / "cuts"
OUT_DIR  = BASE_DIR / "static" / "outputs"
for d in (DL_DIR, CUT_DIR, OUT_DIR):
    d.mkdir(parents=True, exist_ok=True)

# If you want to ship cookies via env:
if "COOKIES_B64" in os.environ:
    with open("cookies.txt", "wb") as f:
        f.write(base64.b64decode(os.environ["COOKIES_B64"]))

# ── FLASK SETUP ───────────────────────────────────────────────────────────
app = Flask(__name__,
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
        m1,s1,m2,s2 = map(int, m.groups())
        st, en = m1*60+s1, m2*60+s2
        if en <= st:
            raise ValueError("End time must be after start time")
        out.append((st,en))
    return out

def run(cmd: str):
    p = subprocess.run(shlex.split(cmd), stderr=subprocess.PIPE)
    if p.returncode:
        raise RuntimeError(p.stderr.decode("utf-8","ignore"))

def download_youtube(url: str) -> Path:
    """Try yt-dlp; if it fails, bubble up the error."""
    out_tmpl = str(DL_DIR / "%(id)s.%(ext)s")
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
    cmd = (
        f"yt-dlp --user-agent {shlex.quote(ua)} "
        f"-f bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4 "
        f"-o {shlex.quote(out_tmpl)} {shlex.quote(url)}"
    )
    run(cmd)
    mp4s = sorted(DL_DIR.glob("*.mp4"),
                  key=lambda p: p.stat().st_mtime,
                  reverse=True)
    if not mp4s:
        raise FileNotFoundError("Download failed (no mp4 file).")
    return mp4s[0]

def cut_and_concat(src: Path, segments: List[Tuple[float,float]]) -> Path:
    cuts = []
    for i,(st,en) in enumerate(segments):
        out = CUT_DIR/f"cut_{uuid.uuid4().hex[:8]}_{i}.mp4"
        cmd = (
            f"ffmpeg -y -ss {st} -to {en} -i {shlex.quote(str(src))} "
            f"-c:v libx264 -c:a aac -preset veryfast -crf 23 "
            f"{shlex.quote(str(out))}"
        )
        run(cmd)
        cuts.append(out)

    lst = CUT_DIR/f"concat_{uuid.uuid4().hex[:8]}.txt"
    lst.write_text("\n".join(f"file '{p}'" for p in cuts))

    final = OUT_DIR/f"clip_{uuid.uuid4().hex[:8]}.mp4"
    run(
        f"ffmpeg -y -f concat -safe 0 "
        f"-i {shlex.quote(str(lst))} -c copy {shlex.quote(str(final))}"
    )
    return final


# ── ROUTES ────────────────────────────────────────────────────────────────
@app.route("/", methods=("GET","POST"))
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
            flash(str(e), "error")
    return render_template("index.html")

# note: explicitly allow POST and give us a “cut” endpoint
@app.route("/cut", methods=("POST",))
def cut():
    # (this route won’t actually get called because our form POSTS
    #  to “/” – you can delete it or repoint your form action)
    return redirect(url_for("index"))

@app.route("/preview/<vid>")
def preview(vid):
    path = OUT_DIR / vid
    if not path.exists():
        flash("Clip not found", "error")
        return redirect(url_for("index"))
    return render_template("preview.html",
                           video_file=url_for("static",
                                              filename=f"outputs/{vid}"))

@app.route("/debug")
def debug():
    return {
        "downloads": len(list(DL_DIR.glob("*.mp4"))),
        "outputs":   len(list(OUT_DIR.glob("*.mp4"))),
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)
