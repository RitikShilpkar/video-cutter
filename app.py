#!/usr/bin/env python3
import re, uuid, shlex, subprocess
from pathlib import Path
from typing import List, Tuple
import os
import base64

from flask import Flask, render_template, request, redirect, url_for, flash
from werkzeug.utils import secure_filename

from selenium import webdriver
from selenium.webdriver.chrome.options import Options

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

# ------------------ helpers ----------------
TS_RE = re.compile(r"\s*(\d+):(\d+)-(\d+):(\d+)\s*")

def parse_ts_list(ts_string: str) -> List[Tuple[float, float]]:
    """
    'mm:ss-mm:ss, mm:ss-mm:ss' -> [(start_sec, end_sec), ...]
    """
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
    """Run shell command, raise with stderr on failure."""
    proc = subprocess.run(shlex.split(cmd), stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode("utf-8", "ignore"))

def download_youtube_with_selenium(url: str) -> Path:
    """
    Use Selenium to fetch the YouTube page and extract the video URL as a fallback.
    This is a placeholder: actual video extraction from YouTube with Selenium is non-trivial and may require additional logic or third-party tools.
    Here, we just demonstrate launching the browser and saving the page source for debugging.
    """
    chrome_options = Options()
    chrome_options.add_argument('--headless')
    chrome_options.add_argument('--no-sandbox')
    chrome_options.add_argument('--disable-dev-shm-usage')
    driver = webdriver.Chrome(options=chrome_options)
    driver.get(url)
    # For demonstration, save the page source (in practice, you would parse for video URL or cookies)
    page_source_path = DL_DIR / f"{uuid.uuid4().hex[:8]}_page.html"
    with open(page_source_path, "w", encoding="utf-8") as f:
        f.write(driver.page_source)
    driver.quit()
    raise RuntimeError("Headless browser fallback is not fully implemented. See saved page source for debugging.")

def download_youtube(url: str) -> Path:
    """
    Download video with yt-dlp, return local MP4 path. Fallback to Selenium if yt-dlp fails.
    """
    out_tmpl = str(DL_DIR / "%(id)s.%(ext)s")
    cookies = "cookies.txt"  # Path to your cookies file (must be present in the app directory)
    
    # Try with cookies first
    cmd = f"yt-dlp --cookies {shlex.quote(cookies)} -f bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4 -o {shlex.quote(out_tmpl)} {shlex.quote(url)}"
    try:
        app.logger.info(f"Running yt-dlp command: {cmd}")
        run(cmd)
        files = sorted(DL_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not files:
            raise FileNotFoundError("Download failed: no mp4 created.")
        app.logger.info(f"yt-dlp successful, downloaded: {files[0]}")
        return files[0]
    except Exception as e:
        error_msg = str(e)
        app.logger.error(f"yt-dlp failed: {error_msg}")
        
        # Check for specific error types
        if "429" in error_msg or "Too Many Requests" in error_msg:
            raise RuntimeError("YouTube is rate limiting requests. Please try again in a few minutes or use a different video.")
        elif "content isn't available" in error_msg or "This content isn't available" in error_msg:
            raise RuntimeError("This video is not available (private, deleted, or region-restricted). Please try a different video.")
        elif "Sign in to confirm you're not a bot" in error_msg:
            raise RuntimeError("YouTube requires authentication for this video. Please try a public video or check your cookies.")
        else:
            # Try without cookies as fallback
            try:
                app.logger.info("Trying yt-dlp without cookies...")
                cmd_no_cookies = f"yt-dlp -f bestvideo[ext=mp4]+bestaudio[ext=m4a]/mp4 -o {shlex.quote(out_tmpl)} {shlex.quote(url)}"
                run(cmd_no_cookies)
                files = sorted(DL_DIR.glob("*.mp4"), key=lambda p: p.stat().st_mtime, reverse=True)
                if not files:
                    raise FileNotFoundError("Download failed: no mp4 created.")
                app.logger.info(f"yt-dlp without cookies successful: {files[0]}")
                return files[0]
            except Exception as no_cookie_error:
                app.logger.error(f"yt-dlp without cookies also failed: {no_cookie_error}")
                raise RuntimeError(f"Unable to download video. Error: {error_msg}. Please try a different video or check if the URL is correct.")

def cut_and_concat(src: Path, segments: List[Tuple[float, float]]) -> Path:
    """
    Cut segments & concat. Re-encodes pieces, then does stream copy on concat.
    """
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

# ------------------ routes -----------------
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
        app.logger.error(f"Video file not found: {vid_path}")
        flash(f"Video file not found: {vid}", "error")
        return redirect(url_for("index"))
    
    app.logger.info(f"Serving video: {vid_path}")
    return render_template("preview.html",
                           video_file=url_for("static", filename=f"outputs/{vid}"))

@app.get("/debug")
def debug():
    """Debug endpoint to check if the app is running and show basic info."""
    import os
    debug_info = {
        "app_running": True,
        "cookies_file_exists": os.path.exists("cookies.txt"),
        "downloads_dir_exists": DL_DIR.exists(),
        "outputs_dir_exists": OUT_DIR.exists(),
        "cookies_file_size": os.path.getsize("cookies.txt") if os.path.exists("cookies.txt") else 0,
        "downloads_files": len(list(DL_DIR.glob("*"))),
        "outputs_files": len(list(OUT_DIR.glob("*")))
    }
    return debug_info

if __name__ == "__main__":
    # For external access use: app.run(host="0.0.0.0", port=5000, debug=True)
    app.run(debug=True)
