#!/usr/bin/env python3
import re, uuid, shlex, subprocess, os, base64
from pathlib import Path
from typing import List, Tuple
from flask import Flask, render_template, request, redirect, url_for, flash

# ─── paths ──────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
DL_DIR   = BASE_DIR / "downloads"
CUT_DIR  = BASE_DIR / "cuts"
OUT_DIR  = BASE_DIR / "static" / "outputs"
for p in (DL_DIR, CUT_DIR, OUT_DIR):
    p.mkdir(parents=True, exist_ok=True)

# If you’ve set COOKIES_B64 in Render’s env, decode it into cookies.txt:
if "COOKIES_B64" in os.environ:
    with open(BASE_DIR / "cookies.txt", "wb") as f:
        f.write(base64.b64decode(os.environ["COOKIES_B64"]))

# ─── flask app ─────────────────────────────────────────────────────────────
app = Flask(
    __name__,
    template_folder=str(BASE_DIR / "templates"),
    static_folder=str(BASE_DIR / "static"),
)
app.secret_key = os.environ.get("FLASK_SECRET", "dev‑secret")

# ─── timestamp parsing ─────────────────────────────────────────────────────
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
            raise ValueError(f"End ≤ start in '{part}'")
        out.append((start, end))
    return out

# ─── shell helper ──────────────────────────────────────────────────────────
def run(cmd: str) -> None:
    proc = subprocess.run(shlex.split(cmd), stderr=subprocess.PIPE)
    if proc.returncode:
        raise RuntimeError(proc.stderr.decode("utf-8", "ignore"))

# ─── Invidious mirrors & youtube download ───────────────────────────────────
INVIDIOUS = [
    "https://yewtu.be",
    "https://yewtu.eu",
    "https://yewtu.cafe",
    "https://yewtu.in"
]
def download_youtube(url: str) -> Path:
    out_tmpl = str(DL_DIR / "%(id)s.%(ext)s")
    ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
    cookies = BASE_DIR / "cookies.txt"

    # 1) Try Invidious API first (no rate‑limit, no auth)
    for inv in INVIDIOUS:
        try:
            app.logger.info(f"➤ Fetching via Invidious: {inv}")
            cmd = (
                f"yt-dlp --ignore-config "
                f"--user-agent {shlex.quote(ua)} "
                f"--extractor-args "
                f"\"youtube:youtube_domain={inv.replace('https://','')}\" "
                f"-f bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4 "
                f"-o {shlex.quote(out_tmpl)} {shlex.quote(url)}"
            )
            run(cmd)
            files = sorted(DL_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
            if files:
                return files[0]
        except Exception as e:
            app.logger.warning(f"Invidious @ {inv} failed: {e}")

    # 2) Fallback to yt-dlp with cookies + UA
    base_cmd = (
        f"yt-dlp --ignore-config "
        f"--user-agent {shlex.quote(ua)} "
        f"-f bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4 "
        f"-o {shlex.quote(out_tmpl)} "
    )
    # try with cookies
    if cookies.exists():
        cmd = f"{base_cmd} --cookies {shlex.quote(str(cookies))} {shlex.quote(url)}"
    else:
        cmd = f"{base_cmd} {shlex.quote(url)}"
    try:
        app.logger.info("➤ Falling back to direct yt-dlp")
        run(cmd)
        files = sorted(DL_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            return files[0]
        raise FileNotFoundError("Download succeeded but no .mp4 found")
    except Exception as e:
        app.logger.error(f"yt-dlp failed: {e}")
        raise RuntimeError("Unable to download video; please try again later.")

# ─── cut & concat ──────────────────────────────────────────────────────────
def cut_and_concat(src: Path, segments: List[Tuple[float, float]]) -> Path:
    cuts = []
    for i, (st, en) in enumerate(segments):
        out = CUT_DIR / f"cut_{uuid.uuid4().hex[:8]}_{i}.mp4"
        run((f"ffmpeg -y -ss {st} -to {en} -i {shlex.quote(str(src))} "
             f"-c:v libx264 -c:a aac -preset veryfast -crf 23 {shlex.quote(str(out))}"))
        cuts.append(out)
    # write concat list
    txt = CUT_DIR / f"concat_{uuid.uuid4().hex[:8]}.txt"
    txt.write_text("\n".join(f"file '{p}'" for p in cuts))
    final = OUT_DIR / f"clip_{uuid.uuid4().hex[:8]}.mp4"
    run((f"ffmpeg -y -f concat -safe 0 -i {shlex.quote(str(txt))} "
         f"-c copy {shlex.quote(str(final))}"))
    return final

# ─── routes ───────────────────────────────────────────────────────────────
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/cut", methods=["POST"])
def cut():
    url  = request.form.get("url","").strip()
    tsin = request.form.get("timestamps","").strip()
    try:
        if not url:  raise ValueError("Please provide a YouTube URL.")
        if not tsin: raise ValueError("Please provide timestamps.")
        segs = parse_ts_list(tsin)
        src  = download_youtube(url)
        clip = cut_and_concat(src, segs)
        return redirect(url_for("preview", vid=clip.name))
    except Exception as e:
        app.logger.exception("Cut failed")
        flash(str(e), "error")
        return redirect(url_for("index"))

@app.route("/preview/<vid>", methods=["GET"])
def preview(vid: str):
    path = OUT_DIR / vid
    if not path.exists():
        flash("Clip not found", "error")
        return redirect(url_for("index"))
    return render_template("preview.html", video_file=url_for("static", filename=f"outputs/{vid}"))

@app.route("/debug", methods=["GET"])
def debug():
    return {
        "invidious": INVIDIOUS,
        "downloads": len(list(DL_DIR.glob("*"))),
        "outputs":   len(list(OUT_DIR.glob("*"))),
        "cookies":   os.path.exists(str(BASE_DIR/"cookies.txt"))
    }

if __name__ == "__main__":
    # in Render you’ll run: gunicorn app:app --bind 0.0.0.0:$PORT
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=False)
