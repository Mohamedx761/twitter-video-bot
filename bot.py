import os
import asyncio
import logging
import re
import subprocess
import json
import hashlib
import urllib.request
import threading
import time
import uuid
import shutil
import math
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, InputMediaPhoto, InputMediaVideo, InputFile
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip().strip('"').strip("'")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0").strip().strip('"').strip("'"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads").strip().strip('"').strip("'"))
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip().strip('"').strip("'")
DOWNLOAD_DIR.mkdir(exist_ok=True)


def get_video_duration(path: str) -> float:
    """Get video duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


def get_video_metadata(path: str) -> dict:
    """Extract duration, width, height via ffprobe."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return {}
        data = json.loads(result.stdout)
        duration = float(data.get("format", {}).get("duration", 0))
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                return {
                    "duration": int(duration),
                    "width": int(s.get("width", 0)),
                    "height": int(s.get("height", 0)),
                }
        return {"duration": int(duration)}
    except Exception:
        return {}


def ensure_faststart(input_path: str) -> str:
    """Re-mux video with -movflags +faststart for progressive streaming.
    Does not re-encode — just moves the moov atom to the front.
    Returns output path (same as input if skipped)."""
    output_path = input_path + ".faststart.mp4"
    try:
        result = subprocess.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-c", "copy", "-movflags", "+faststart",
             output_path],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0 and os.path.exists(output_path):
            os.replace(output_path, input_path)
            logger.info(f"faststart applied: {os.path.basename(input_path)}")
        else:
            logger.warning(f"faststart failed: {result.stderr[:200]}")
            if os.path.exists(output_path):
                os.remove(output_path)
    except Exception as e:
        logger.warning(f"faststart error: {e}")
        if os.path.exists(output_path):
            os.remove(output_path)
    return input_path


def compress_video(input_path: str, output_path: str, target_mb: float = 49.0) -> bool:
    """Compress video to fit under target_mb using single-pass bitrate targeting.
    Raises ValueError if video is too long/large to compress reasonably."""
    duration = get_video_duration(input_path)
    if duration <= 0:
        raise ValueError("مقدرش أحدد مدة الفيديو.")

    size_bytes = os.path.getsize(input_path)
    target_bytes = target_mb * 1024 * 1024
    if size_bytes <= target_bytes:
        return False

    audio_bitrate_kbps = 128
    total_bitrate = (target_bytes * 8 * 0.95) / duration
    video_bitrate_kbps = max(100, int((total_bitrate / 1000) - audio_bitrate_kbps))

    if video_bitrate_kbps < 300:
        minutes = int(duration // 60)
        size_mb = int(size_bytes / (1024 * 1024))
        raise ValueError(
            f"الفيديو كبير جداً ({size_mb}MB، {minutes} دقيقة).\n"
            f"الحد الأقصى للإرسال هو 50MB، والضغط ممكن يخلي الجودة سيئة جداً.\n"
            f"جرب تبعت فيديو أقصر أو أصغر."
        )
    maxrate_kbps = int(video_bitrate_kbps * 1.5)
    bufsize_kbps = int(video_bitrate_kbps * 2)

    logger.info(f"Compressing: {size_bytes/(1024*1024):.1f}MB target={target_mb}MB duration={duration:.0f}s vbitrate={video_bitrate_kbps}k abitrate={audio_bitrate_kbps}k")

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264",
        "-b:v", f"{video_bitrate_kbps}k",
        "-maxrate", f"{maxrate_kbps}k",
        "-bufsize", f"{bufsize_kbps}k",
        "-preset", "fast",
        "-c:a", "aac",
        "-b:a", f"{audio_bitrate_kbps}k",
        "-movflags", "+faststart",
        output_path,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if proc.returncode != 0:
            logger.error(f"ffmpeg error: {proc.stderr[:500]}")
            return False
        if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
            out_size = os.path.getsize(output_path) / (1024 * 1024)
            logger.info(f"Compressed: {size_bytes/(1024*1024):.1f}MB -> {out_size:.1f}MB")
            return True
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg compression timed out")
    except FileNotFoundError:
        logger.error("ffmpeg not found")
    return False

def get_cookie_args() -> list:
    paths = [COOKIES_FILE, "cookies.txt"] if COOKIES_FILE else ["cookies.txt"]
    for p in paths:
        if p and Path(p).exists():
            return ["--cookies", p]
    return []

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class SimpleHealthServer(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Twitter Video Bot is running successfully!\n")

    def log_message(self, format, *args):
        pass


def start_health_check_server():
    port_str = os.getenv("PORT", "10000").strip()
    try:
        port = int(port_str)
    except ValueError:
        port = 10000

    try:
        server = HTTPServer(("0.0.0.0", port), SimpleHealthServer)
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        logger.info(f"Health check HTTP server running on port {port}")
    except Exception as e:
        logger.warning(f"Could not start health check HTTP server on port {port}: {e}")

cancelled_downloads = set()
_download_semaphore = asyncio.Semaphore(1)
_queue_lock = asyncio.Lock()
_waiting_count = 0


def is_authorized(user_id: int) -> bool:
    if AUTHORIZED_USER_ID == 0:
        return True
    return user_id == AUTHORIZED_USER_ID


URL_PATTERNS = [
    r"(https?://)?(www\.)?(x\.com|twitter\.com)/\S+/status/\d+",
    r"(https?://)?(www\.)?(instagram\.com)/(p|reel|tv|stories)/\S+",
    r"(https?://)?(www\.|m\.|web\.)*(facebook|fb)\.com/.*/(videos|posts)/\S+",
    r"(https?://)?fb\.watch/\S+",
    r"(https?://)?(www\.|m\.|web\.)*(facebook|fb)\.com/reel/\S+",
    r"(https?://)?(www\.|m\.|web\.)*(facebook|fb)\.com/share/\S+",
    r"(https?://)?(www\.|m\.|web\.)*(facebook|fb)\.com/permalink\.php\S*",
    r"(https?://)?vm\.tiktok\.com/\S+",
    r"(https?://)?(www\.)?tiktok\.com/@\S+/video/\d+",
]


def extract_url(text: str) -> str:
    """Find the first supported link anywhere inside the message text
    (not just when the whole message is a bare URL), and return a clean
    URL string usable by yt-dlp."""
    text = text.strip()
    for pattern in URL_PATTERNS:
        match = re.search(pattern, text)
        if match:
            url = match.group(0)
            # Trim trailing punctuation that's likely not part of the URL
            url = url.rstrip(".,!?;:)>]\"'")
            if not url.lower().startswith("http"):
                url = "https://" + url
            return url
    return None


def is_valid_url(url: str) -> bool:
    return extract_url(url) is not None


def make_progress_bar(percent: int) -> str:
    filled = int(percent / 5)
    empty = 20 - filled
    bar = "█" * filled + "░" * empty
    return f"[{bar}] {percent}%"


cancel_keyboard = InlineKeyboardMarkup(
    [[InlineKeyboardButton("Cancel", callback_data="cancel_download")]]
)

start_keyboard = ReplyKeyboardMarkup(
    [[KeyboardButton("Start")]],
    resize_keyboard=True,
)

VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.mov')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')


def _cleanup_file(path):
    """Delete a file and its thumbnails to free memory.
    Only deletes the exact file and its direct thumbnail variants, NOT siblings."""
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
        for ext in ('.jpg', '.webp', '.png', '.jpeg'):
            thumb = p.with_suffix(p.suffix + ext)
            if thumb.exists():
                thumb.unlink()
        thumb_direct = Path(str(p) + ".thumb.jpg")
        if thumb_direct.exists():
            thumb_direct.unlink()
        compressed = Path(str(p) + ".compressed.mp4")
        if compressed.exists():
            compressed.unlink()
        compressed_jpg = Path(str(p) + ".compressed.jpg")
        if compressed_jpg.exists():
            compressed_jpg.unlink()
    except Exception:
        pass


def _get_api_url():
    use_local = os.getenv("USE_LOCAL_SERVER", "false").lower() == "true"
    local_url = os.getenv("LOCAL_API_URL", "").strip()
    if use_local and local_url:
        return f"{local_url.rstrip('/')}/bot{BOT_TOKEN}"
    return f"https://api.telegram.org/bot{BOT_TOKEN}"


async def _curl_upload(api_url, endpoint, path, extra_fields=None, timeout=300, thumb_path=None):
    """Run curl in a subprocess with EXPLICIT lifecycle management.
    Uses asyncio.wait (NOT wait_for) so we control the kill ourselves."""
    cmd = [
        "curl", "-s", "-S", "-X", "POST",
        f"{api_url}/{endpoint}",
        "--max-time", str(timeout),
        "--connect-timeout", "15",
    ]
    if thumb_path and os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
        cmd.extend(["-F", f"thumb=@{thumb_path}"])
        logger.info(f"[upload] attaching thumbnail {os.path.basename(thumb_path)} ({os.path.getsize(thumb_path)} bytes)")
    file_field = "video" if endpoint == "sendVideo" else "photo"
    cmd.extend(["-F", f"{file_field}=@{path}"])
    if extra_fields:
        for k, v in extra_fields.items():
            cmd.extend(["-F", f"{k}={v}"])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    logger.info(f"[upload] started curl pid={proc.pid} {endpoint} {os.path.basename(path)}")

    comm_task = asyncio.ensure_future(proc.communicate())
    done, pending = await asyncio.wait(
        [comm_task],
        timeout=timeout,
    )

    if pending:
        logger.warning(f"[upload] TIMEOUT after {timeout}s — killing curl pid={proc.pid}")
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await asyncio.wait(pending, timeout=5)
        comm_task.cancel()
        raise asyncio.TimeoutError(f"Upload timed out after {timeout}s")

    stdout, stderr = comm_task.result()
    output = stdout.decode(errors="replace").strip()
    if proc.returncode not in (0, None):
        err = stderr.decode(errors="replace").strip()[:300]
        raise Exception(f"curl rc={proc.returncode}: {err}")

    result = json.loads(output) if output else {}
    if not result.get("ok"):
        raise Exception(f"Telegram: {result.get('description', output[:200])}")
    return result


async def _send_video_isolated(chat_id, path, caption, meta=None, thumb_path=None, timeout=300):
    """Upload video via isolated curl subprocess."""
    api_url = _get_api_url()
    fields = {"chat_id": str(chat_id), "supports_streaming": "true"}
    if caption:
        fields["caption"] = caption[:1024]
    if meta:
        for k in ("duration", "width", "height"):
            if meta.get(k):
                fields[k] = str(meta[k])
    logger.info(f"[upload] sendVideo {os.path.basename(path)} timeout={timeout}s")
    if thumb_path and os.path.exists(thumb_path):
        return await _curl_upload(api_url, "sendVideo", path, extra_fields=fields, timeout=timeout, thumb_path=thumb_path)
    return await _curl_upload(api_url, "sendVideo", path, extra_fields=fields, timeout=timeout)


async def _send_photo_isolated(chat_id, path, caption, timeout=120):
    """Upload photo via isolated curl subprocess."""
    api_url = _get_api_url()
    fields = {"chat_id": str(chat_id)}
    if caption:
        fields["caption"] = caption[:1024]
    logger.info(f"[upload] sendPhoto {os.path.basename(path)} timeout={timeout}s")
    return await _curl_upload(api_url, "sendPhoto", path, extra_fields=fields, timeout=timeout)


async def _curl_upload_media_group(chat_id, media_items, caption="", timeout=300):
    """Upload an album (2-10 items) via curl directly to Telegram Bot API."""
    if len(media_items) < 2:
        return False

    api_url = _get_api_url()
    cmd = [
        "curl", "-s", "-S", "-X", "POST",
        f"{api_url}/sendMediaGroup",
        "--max-time", str(timeout),
        "--connect-timeout", "20",
        "-F", f"chat_id={chat_id}",
    ]
    media_json = []
    for idx, (path, kind) in enumerate(media_items[:10]):
        key = f"file_{idx}"
        item = {
            "type": "photo" if kind == "photo" else "video",
            "media": f"attach://{key}",
        }
        if idx == 0 and caption:
            item["caption"] = caption[:1024]
        media_json.append(item)
        cmd.extend(["-F", f"{key}=@{path}"])

    cmd.extend(["-F", f"media={json.dumps(media_json)}"])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    logger.info(f"[upload] started curl sendMediaGroup ({len(media_items)} items) pid={proc.pid}")

    comm_task = asyncio.ensure_future(proc.communicate())
    done, pending = await asyncio.wait([comm_task], timeout=timeout)

    if pending:
        logger.warning(f"[upload] TIMEOUT after {timeout}s — killing curl sendMediaGroup pid={proc.pid}")
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await asyncio.wait(pending, timeout=5)
        comm_task.cancel()
        raise asyncio.TimeoutError(f"Upload media group timed out after {timeout}s")

    stdout, stderr = comm_task.result()
    output = stdout.decode(errors="replace").strip()
    if proc.returncode not in (0, None):
        err = stderr.decode(errors="replace").strip()[:300]
        logger.error(f"sendMediaGroup curl failed rc={proc.returncode}: {err}")
        return False

    result = json.loads(output) if output else {}
    if not result.get("ok"):
        logger.error(f"sendMediaGroup Telegram error: {result.get('description', output[:200])}")
        return False

    logger.info(f"[upload] sendMediaGroup succeeded with {len(media_items)} items")
    return True


async def send_media_group_isolated(bot, chat_id, media_list, caption=""):
    """Send a media group (album) of photos/videos in one message.
    media_list: list of (path, kind) tuples where kind is 'photo' or 'video'"""
    if len(media_list) < 2:
        return False

    # Attempt 1: Isolated curl (handles local server, large files, streaming without RAM bloat)
    try:
        ok = await _curl_upload_media_group(chat_id, media_list, caption)
        if ok:
            return True
    except Exception as e:
        logger.warning(f"Curl media group upload failed, trying python-telegram-bot fallback: {e}")

    # Attempt 2: python-telegram-bot fallback with properly opened binary files
    from telegram import InputMediaPhoto, InputMediaVideo
    file_handles = []
    try:
        grouped_media = []
        for idx, (path, kind) in enumerate(media_list[:10]):
            if not os.path.exists(path):
                continue
            fh = open(path, "rb")
            file_handles.append(fh)
            item_caption = caption if idx == 0 and caption else None
            if kind == "photo":
                grouped_media.append(InputMediaPhoto(media=fh, caption=item_caption))
            elif kind == "video":
                grouped_media.append(InputMediaVideo(media=fh, caption=item_caption))

        if len(grouped_media) >= 2:
            await bot.send_media_group(chat_id=chat_id, media=grouped_media)
            logger.info(f"python-telegram-bot send_media_group succeeded with {len(grouped_media)} items")
            return True
    except Exception as e:
        logger.error(f"python-telegram-bot send_media_group failed: {e}")
    finally:
        for fh in file_handles:
            try:
                fh.close()
            except Exception:
                pass

    return False


def dedupe_thumbnails(file_paths):
    videos = [fp for fp in file_paths if Path(fp).suffix.lower() in VIDEO_EXTS]
    images = [fp for fp in file_paths if Path(fp).suffix.lower() in IMAGE_EXTS]
    if not videos:
        return file_paths
    video_stems = {Path(v).stem for v in videos}
    result = list(videos)
    for img in images:
        img_stem = Path(img).stem
        is_thumb = any(
            img_stem == vs or img_stem.startswith(vs + ".") or img_stem.startswith(vs + "_") or vs.startswith(img_stem + ".") or vs.startswith(img_stem + "_")
            for vs in video_stems
        )
        if not is_thumb:
            result.append(img)
    return result


def get_json_info(url: str) -> dict:
    cmd = [
        "yt-dlp",
        "--dump-single-json",
        "--no-download",
        "--no-warnings",
        "--no-check-certificates",
        "--extractor-retries", "3",
        "--retry-sleep", "1",
    ] + get_cookie_args() + [url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.stdout.strip():
            try:
                data = json.loads(result.stdout.strip())
            except json.JSONDecodeError:
                logger.error(f"JSON parse failed: {result.stdout.strip()[:200]}")
                data = None
            if isinstance(data, dict):
                logger.info(f"get_json_info keys: {list(data.keys())[:15]}")
                return data
            else:
                logger.error(f"get_json_info returned non-dict: {type(data)}")
    except Exception as e:
        logger.error(f"JSON fetch failed: {e}")
    return {}


def _best_str_url(value):
    """TikTok (and some other extractors) give image URLs as a list of
    quality/CDN variants instead of a single string. Pick the last one
    that looks like a usable URL."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        for item in reversed(value):
            if isinstance(item, str) and item.startswith("http"):
                return item
    return None


def collect_image_urls(info: dict) -> list:
    primary = []
    fallback = []

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "display_resources" and isinstance(v, list):
                    r = v[-1]
                    if isinstance(r, dict) and r.get("src"):
                        primary.append(r["src"])
                elif k == "images" and isinstance(v, list):
                    for im in v:
                        if isinstance(im, dict) and im.get("url") is not None:
                            u = _best_str_url(im["url"])
                            if u:
                                primary.append(u)
                elif k == "thumbnails" and isinstance(v, list):
                    t = v[-1]
                    if isinstance(t, dict) and t.get("url"):
                        u = _best_str_url(t["url"])
                        if u:
                            primary.append(u)
                elif k == "thumbnail" and isinstance(v, str) and v.startswith("http"):
                    fallback.append(v)
                elif k == "url" and isinstance(v, str) and re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", v, re.I):
                    primary.append(v)
                elif k == "url" and isinstance(v, list):
                    u = _best_str_url(v)
                    if u and re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", u, re.I):
                        primary.append(u)
                    else:
                        walk(v)
                else:
                    walk(v)
        elif isinstance(obj, list):
            for item in obj:
                walk(item)

    walk(info)
    urls = primary if primary else fallback
    seen = set()
    result = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def extract_thumbnail(video_path: str) -> str | None:
    """Extract a single frame thumbnail from a video via ffmpeg.
    Tries multiple positions: 25%, 10%, 50%, 75%, 1s to avoid white/black frames.
    Resizes to Telegram limits: JPEG, <200KB, <900px."""
    thumb_path = video_path + ".thumb.jpg"
    raw_thumb = video_path + ".thumb.raw.jpg"
    try:
        duration = get_video_duration(video_path)
        logger.info(f"Thumbnail: video duration={duration}s for {os.path.basename(video_path)}")
        positions = []
        if duration > 4:
            positions = [int(duration * 0.25), int(duration * 0.10), int(duration * 0.50), int(duration * 0.75)]
        else:
            positions = [1]
        found = False
        for seek_pos in positions:
            seek_pos = max(1, seek_pos)
            seek_str = f"{seek_pos // 3600:02d}:{(seek_pos % 3600) // 60:02d}:{seek_pos % 60:02d}"
            try:
                result = subprocess.run(
                    ["ffmpeg", "-y", "-ss", seek_str, "-i", video_path,
                     "-vframes", "1", "-q:v", "3", raw_thumb],
                    capture_output=True, text=True, timeout=30
                )
                if result.returncode == 0 and os.path.exists(raw_thumb) and os.path.getsize(raw_thumb) > 100:
                    logger.info(f"Thumbnail extracted at {seek_str} ({os.path.getsize(raw_thumb)} bytes)")
                    found = True
                    break
                else:
                    logger.warning(f"Thumbnail at {seek_str} failed: rc={result.returncode}")
            except Exception as e:
                logger.warning(f"Thumbnail seek {seek_str} error: {e}")
        if not found:
            return None

        # Resize to Telegram limits: JPEG, max 900px width, under 200KB
        resize_result = subprocess.run(
            ["ffmpeg", "-y", "-i", raw_thumb,
             "-vf", "scale='min(900,iw)':'-2':flags=lanczos",
             "-q:v", "3", thumb_path],
            capture_output=True, text=True, timeout=15
        )
        if resize_result.returncode != 0 or not os.path.exists(thumb_path):
            # Fallback: use raw thumbnail
            if os.path.exists(raw_thumb):
                os.replace(raw_thumb, thumb_path)
        else:
            if os.path.exists(raw_thumb):
                os.remove(raw_thumb)

        # If still over 200KB, compress more
        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 200 * 1024:
            subprocess.run(
                ["ffmpeg", "-y", "-i", thumb_path,
                 "-vf", "scale='min(640,iw)':'-2':flags=lanczos",
                 "-q:v", "5", thumb_path + ".small.jpg"],
                capture_output=True, text=True, timeout=15
            )
            if os.path.exists(thumb_path + ".small.jpg"):
                os.replace(thumb_path + ".small.jpg", thumb_path)

        if os.path.exists(thumb_path) and os.path.getsize(thumb_path) > 0:
            logger.info(f"Thumbnail final: {os.path.getsize(thumb_path)} bytes")
            return thumb_path
    except Exception as e:
        logger.warning(f"Thumbnail extraction failed: {e}")
        if os.path.exists(raw_thumb):
            os.remove(raw_thumb)
    return None


def _download_direct_url(url: str, dest: Path, timeout: int = 300, progress_cb=None) -> bool:
    """Download a direct URL (e.g. Twitter CDN MP4) via urllib with proper headers."""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": "https://x.com/",
    }
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=timeout)
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                if progress_cb and total > 0:
                    progress_cb(downloaded, total)
        return dest.exists() and dest.stat().st_size > 0
    except Exception as e:
        logger.error(f"Direct download failed: {e}")
        return False


def download_image_ytdlp(image_url: str, download_path: Path, idx: int, progress_cb=None) -> str:
    is_video = ".mp4" in image_url or "video.twimg.com" in image_url
    ext = "mp4" if is_video else "jpg"
    if ".png" in image_url.lower():
        ext = "png"
    elif ".webp" in image_url.lower():
        ext = "webp"
    fpath = download_path / f"{idx:05d}.{ext}"

    # If it is a direct image URL (including pbs.twimg.com) or Twitter CDN video, download directly via HTTP!
    if "twimg.com" in image_url or re.search(r"\.(jpg|jpeg|png|webp|mp4)(\?|$)", image_url, re.I):
        logger.info(f"Direct downloading media ({'video' if is_video else 'photo'}): {image_url[:80]}")
        if _download_direct_url(image_url, fpath, progress_cb=progress_cb):
            return str(fpath)
        logger.warning("Direct download failed, falling back to yt-dlp")

    # Determine platform for proper headers
    is_tiktok = "tiktok.com" in image_url or "tiktokcdn.com" in image_url
    is_instagram = "instagram.com" in image_url or "cdninstagram.com" in image_url or "fbcdn.net" in image_url

    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-check-certificates",
        "--no-playlist",
        "--ignore-no-formats-error",
        "--no-overwrites",
        "--no-write-info-json",
        "--legacy-server-connect",
    ]
    if is_tiktok or is_instagram:
        cmd += [
            "--user-agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "--referer", "https://www.tiktok.com/" if is_tiktok else "https://www.instagram.com/",
        ]
    cmd += get_cookie_args() + ["-o", str(fpath), image_url]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode != 0 and proc.stderr.strip():
            logger.error(f"Download stderr: {proc.stderr.strip()[:500]}")
    except Exception as e:
        logger.error(f"Download exception: {e}")

    for f in download_path.iterdir():
        if f.name.startswith(f"{idx:05d}.") and f.is_file() and not f.name.endswith(".info.json") and f.stat().st_size > 0:
            return str(f)
    return None


def collect_carousel_urls(info: dict) -> list:
    urls = []

    edges = info.get("edge_sidecar_to_children", {}).get("edges", [])
    if edges:
        logger.info(f"Carousel edges found: {len(edges)}")
        for i, edge in enumerate(edges):
            node = edge.get("node", {})
            logger.info(f"Edge {i}: keys={list(node.keys())[:15]}")
            for res in node.get("display_resources", []):
                if isinstance(res, dict) and res.get("src"):
                    urls.append(res["src"])
            img_versions = node.get("image_versions2", {})
            for c in img_versions.get("candidates", []):
                if isinstance(c, dict) and c.get("src"):
                    urls.append(c["src"])

    entries = info.get("entries", [])
    if entries and not urls:
        logger.info(f"Carousel entries found: {len(entries)}")
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            entry_keys = list(entry.keys())[:15]
            logger.info(f"Entry {i}: keys={entry_keys}")
            for key in ("url", "webpage_url", "original_url"):
                val = entry.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    urls.append(val)
            for key in ("thumbnail", "thumb", "display_url"):
                val = entry.get(key)
                if isinstance(val, str) and val.startswith("http"):
                    urls.append(val)
            for thumb in entry.get("thumbnails", []):
                if isinstance(thumb, dict):
                    for key in ("url", "src"):
                        val = thumb.get(key)
                        if isinstance(val, str) and val.startswith("http"):
                            urls.append(val)
            for res in entry.get("display_resources", []):
                if isinstance(res, dict) and res.get("src"):
                    urls.append(res["src"])
            img_versions = entry.get("image_versions2", {})
            for c in img_versions.get("candidates", []):
                if isinstance(c, dict) and c.get("src"):
                    urls.append(c["src"])

    logger.info(f"Carousel collected {len(urls)} URLs before dedupe")
    seen = set()
    result = []
    for u in urls:
        # Normalize: strip query params and fragments for dedupe
        norm = u.split("?")[0].split("#")[0]
        if norm not in seen:
            seen.add(norm)
            result.append(u)
    return result


def get_carousel_image_urls(url: str) -> list:
    cmd = [
        "yt-dlp",
        "--dump-single-json",
        "--no-download",
        "--no-warnings",
        "--no-check-certificates",
        "--ignore-no-formats-error",
        "--extractor-retries", "3",
        "--retry-sleep", "1",
    ] + get_cookie_args() + [url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.stdout.strip():
            raw = result.stdout.strip()
            logger.info(f"Carousel JSON stdout length: {len(raw)}")
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                logger.error(f"Carousel JSON parse error: {raw[:500]}")
                return []
            if isinstance(data, dict):
                logger.info(f"Carousel JSON top keys: {list(data.keys())[:20]}")
                if "edge_sidecar_to_children" in data:
                    children = data["edge_sidecar_to_children"]
                    if isinstance(children, dict):
                        edges = children.get("edges", [])
                        logger.info(f"edge_sidecar_to_children.edges count: {len(edges)}")
                        if edges:
                            logger.info(f"First edge keys: {list(edges[0].keys()) if isinstance(edges[0], dict) else edges[0]}")
                            node = edges[0].get("node", {})
                            logger.info(f"First node keys: {list(node.keys()) if isinstance(node, dict) else node}")
                            dr = node.get("display_resources", []) if isinstance(node, dict) else []
                            logger.info(f"First node display_resources count: {len(dr)}")
                            if dr:
                                logger.info(f"First display_resource keys: {list(dr[0].keys()) if isinstance(dr[0], dict) else dr[0]}")
                urls = collect_carousel_urls(data)
                logger.info(f"Carousel JSON: {len(urls)} image URL(s)")
                return urls
            elif isinstance(data, list):
                logger.info(f"Carousel JSON is list of {len(data)} items")
                urls = []
                for item in data:
                    if isinstance(item, dict):
                        urls.extend(collect_carousel_urls(item))
                logger.info(f"Carousel JSON from list: {len(urls)} image URL(s)")
                return urls
            else:
                logger.error(f"Carousel JSON returned {type(data)}: {raw[:500]}")
    except Exception as e:
        logger.error(f"Carousel JSON fetch failed: {e}")
    return []


def run_ytdlp_download(
    url: str,
    download_path: Path,
    is_carousel: bool,
    is_x: bool,
    use_cookies: bool,
    progress: dict = None,
    timeout: int = 60,
) -> str:
    """Run yt-dlp download attempt with streaming output and timeout.
    Returns stderr text for diagnostics."""
    cmd_dl = [
        "yt-dlp",
        "--no-warnings",
        "--no-check-certificates",
        "--ignore-no-formats-error",
        "--write-info-json",
        "--no-overwrites",
        "--concurrent-fragments", "2",
        "--socket-timeout", "15",
        "--retries", "2",
        "--newline",
        "--progress-template", "download:%(progress._percent_str)s of %(progress._total_bytes_str)s",
    ]
    if use_cookies:
        cmd_dl += get_cookie_args()
    cmd_dl += ["-o", str(download_path / "%(autonumber)s_%(id)s.%(ext)s")]
    if is_x:
        cmd_dl += [
            "--user-agent",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        ]
    if not is_carousel:
        cmd_dl.append("--no-playlist")
    cmd_dl.append(url)

    logger.info(f"Running yt-dlp (cookies={use_cookies}, timeout={timeout}s) for {url}")
    start_t = time.time()
    stderr_chunks = []

    try:
        proc = subprocess.Popen(
            cmd_dl,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        raise ValueError("أداة التحميل yt-dlp غير مثبتة على السيرفر.")

    def _read_stdout():
        for line in iter(proc.stdout.readline, ''):
            if not line:
                break
            line_s = line.strip()
            if line_s.startswith("download:"):
                pct = line_s.replace("download:", "").strip()
                if progress is not None and pct:
                    progress["msg"] = f"⏳ جاري التحميل: {pct}"

    def _read_stderr():
        for line in iter(proc.stderr.readline, ''):
            if not line:
                break
            stderr_chunks.append(line)

    t_out = threading.Thread(target=_read_stdout, daemon=True)
    t_err = threading.Thread(target=_read_stderr, daemon=True)
    t_out.start()
    t_err.start()

    while proc.poll() is None:
        if time.time() - start_t > timeout:
            try:
                proc.kill()
            except Exception:
                pass
            logger.warning(f"yt-dlp killed after {timeout}s timeout for {url}")
            t_out.join(timeout=1)
            t_err.join(timeout=1)
            raise ValueError(f"استغرق التحميل وقتاً طويلاً (تجاوز {timeout} ثانية وتوقف).")
        time.sleep(0.5)

    t_out.join(timeout=2)
    t_err.join(timeout=2)

    stderr = "".join(stderr_chunks)
    if stderr.strip():
        logger.info(f"yt-dlp stderr (cookies={use_cookies}): {stderr.strip()[:600]}")
    return stderr



def get_tiktok_image_urls(url: str) -> list:
    """Use --dump-single-json to get TikTok image post metadata."""
    cmd = [
        "yt-dlp",
        "--dump-single-json",
        "--no-download",
        "--no-warnings",
        "--no-check-certificates",
        "--no-playlist",
        "--ignore-no-formats-error",
    ] + get_cookie_args() + [url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.stdout.strip():
            try:
                data = json.loads(result.stdout.strip())
            except json.JSONDecodeError:
                logger.error(f"TikTok JSON parse error: {result.stdout.strip()[:500]}")
                return []
            if isinstance(data, dict):
                logger.info(f"TikTok JSON top keys: {list(data.keys())[:20]}")
                urls = collect_image_urls(data)
                if urls:
                    logger.info(f"TikTok JSON: {len(urls)} image URL(s)")
                    return urls
                for key in ["images", "image_urls", "image_list", "image_post", "cover"]:
                    val = data.get(key)
                    if isinstance(val, list):
                        for item in val:
                            if isinstance(item, str) and item.startswith("http"):
                                urls.append(item)
                            elif isinstance(item, dict):
                                for uk in ["url", "src", "download_url", "display_url", "uri"]:
                                    if uk in item and isinstance(item[uk], str):
                                        urls.append(item[uk])
                                        break
                urls = list(dict.fromkeys(urls))
                logger.info(f"TikTok fallback: {len(urls)} image URL(s)")
                return urls
    except Exception as e:
        logger.error(f"TikTok JSON fetch failed: {e}")
    return []


def get_x_syndication_token(tweet_id: str) -> str:
    """Calculate the mathematical token required for cdn.syndication.twimg.com."""
    try:
        val = (float(tweet_id) / 1e15) * math.pi
        digits = '0123456789abcdefghijklmnopqrstuvwxyz'
        def to_b36(n):
            res = []
            n = int(n)
            while n > 0:
                n, r = divmod(n, 36)
                res.append(digits[r])
            return ''.join(reversed(res)) or '0'
        int_p = int(val)
        frac_p = val - int_p
        frac_s = []
        c = frac_p
        for _ in range(12):
            c *= 36
            d = int(c)
            frac_s.append(to_b36(d))
            c -= d
        s = to_b36(int_p) + ''.join(frac_s)
        return re.sub(r'(0+|\.)', '', s)
    except Exception:
        return 'x'


def get_x_media(url: str) -> tuple[dict, list]:
    """Fetch photos and video URLs directly from X/Twitter via FxTwitter v2 and Syndication API.
    Guarantees full support for image galleries and direct video downloads without yt-dlp blocks."""
    tweet_id_match = re.search(r"/status/(\d+)", url)
    if not tweet_id_match:
        return {}, []
    tweet_id = tweet_id_match.group(1)

    info = {}
    media_urls = []

    # Source 1: FxTwitter API v2
    fxtwitter_endpoints = [
        f"https://api.fxtwitter.com/2/status/{tweet_id}",
    ]
    handle_match = re.search(r"(?:x\.com|twitter\.com)/([^/]+)/status/\d+", url)
    if handle_match and handle_match.group(1) not in ("i", "status"):
        handle = handle_match.group(1)
        fxtwitter_endpoints.append(f"https://api.fxtwitter.com/{handle}/status/{tweet_id}")

    for api_url in fxtwitter_endpoints:
        try:
            req = urllib.request.Request(
                api_url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
            )
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())
                st = data.get("status") or data.get("tweet") or {}
                if st:
                    if not info:
                        info = {"description": st.get("text", ""), "title": st.get("text", "")}
                    media = st.get("media", {})
                    # Photos
                    for p in media.get("photos", []):
                        u = p.get("url")
                        if u and u not in media_urls:
                            if "pbs.twimg.com/media/" in u and "name=" not in u:
                                u = f"{u}?name=orig"
                            media_urls.append(u)
                    # Videos
                    for v in media.get("videos", []):
                        u = v.get("url")
                        if u and u not in media_urls:
                            media_urls.append(u)
                    # All (fallback)
                    for a in media.get("all", []):
                        u = a.get("url")
                        if u and u not in media_urls:
                            if "pbs.twimg.com/media/" in u and "name=" not in u:
                                u = f"{u}?name=orig"
                            media_urls.append(u)
                    if media_urls:
                        logger.info(f"FxTwitter API found {len(media_urls)} media URL(s) for tweet {tweet_id}")
                        return info, media_urls
        except Exception as e:
            logger.info(f"FxTwitter attempt failed ({api_url}): {e}")

    # Source 2: Twitter Syndication API
    token = get_x_syndication_token(tweet_id)
    syndication_url = f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&token={token}&lang=en"
    try:
        req = urllib.request.Request(
            syndication_url,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Referer": "https://platform.twitter.com/",
            }
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
            if not info and data.get("text"):
                info = {"description": data.get("text", ""), "title": data.get("text", "")}

            media_lists = [data.get("mediaDetails", [])]
            if not media_lists[0]:
                media_lists = [data.get("extended_entities", {}).get("media", [])]

            for media_list in media_lists:
                for media in media_list:
                    media_type = media.get("type", "")
                    if media_type == "photo":
                        media_url = media.get("media_url_https", "")
                        if media_url:
                            if "name=" not in media_url:
                                media_url = f"{media_url}?name=orig"
                            if media_url not in media_urls:
                                media_urls.append(media_url)
                    elif media_type in ("video", "animated_gif"):
                        variants = media.get("video_info", {}).get("variants", [])
                        best = None
                        for v in variants:
                            if v.get("content_type") == "video/mp4" and v.get("url"):
                                if best is None or v.get("bitrate", 0) > best.get("bitrate", 0):
                                    best = v
                        if best and best["url"] not in media_urls:
                            media_urls.append(best["url"])
                        else:
                            for v in variants:
                                if v.get("url") and v["url"] not in media_urls:
                                    media_urls.append(v["url"])
                                    break
        if media_urls:
            logger.info(f"Syndication API found {len(media_urls)} media URL(s) for tweet {tweet_id}")
            return info, media_urls
    except Exception as e:
        logger.warning(f"Syndication API failed for tweet {tweet_id}: {e}")

    return info, media_urls


def extract_media(url: str, download_path: Path, chat_id: int, status_msg_id: int, app: Application, progress: dict = None) -> tuple[dict, list]:
    downloaded_files = []
    before_files = set(f.name for f in download_path.iterdir() if f.is_file())

    for f in download_path.glob("*.info.json"):
        try:
            f.unlink()
        except Exception:
            pass

    url_lower = url.lower()
    is_carousel = (
        "instagram.com" in url_lower
        or "tiktok.com" in url_lower
        or "vm.tiktok.com" in url_lower
    )
    is_x = "twitter.com" in url_lower or "x.com" in url_lower

    _cookie_paths = [COOKIES_FILE, "cookies.txt"] if COOKIES_FILE else ["cookies.txt"]
    have_cookies_file = any(Path(p).exists() for p in _cookie_paths if p)
    logger.info(f"Cookies: COOKIES_FILE={COOKIES_FILE!r}, exists={have_cookies_file}")

    all_image_urls = []
    info = {}
    last_stderr = ""

    # For X (Twitter), first try direct API extraction for photos and videos
    if is_x:
        x_info, x_urls = get_x_media(url)
        if x_urls:
            logger.info(f"X/Twitter direct extraction: {len(x_urls)} media item(s) found")
            if not info and x_info:
                info = x_info
            for u in x_urls:
                if u not in all_image_urls:
                    all_image_urls.append(u)

    # If not X, or if direct X extraction found nothing, run yt-dlp
    if not all_image_urls:
        if have_cookies_file:
            logger.info(f"Attempting download with cookies (is_x={is_x})...")
            try:
                last_stderr = run_ytdlp_download(url, download_path, is_carousel, is_x, use_cookies=True, progress=progress, timeout=60)
            except Exception as e:
                logger.warning(f"Download with cookies failed: {e}")
                last_stderr = str(e)

            # Check if any media was downloaded
            after_files = set(f.name for f in download_path.iterdir() if f.is_file() and not f.name.endswith(".info.json"))
            if not (after_files - before_files):
                logger.info("No media found using cookies. Retrying WITHOUT cookies as fallback...")
                try:
                    last_stderr = run_ytdlp_download(url, download_path, is_carousel, is_x, use_cookies=False, progress=progress, timeout=60)
                except Exception as e:
                    logger.warning(f"Fallback download without cookies failed: {e}")
                    last_stderr = str(e)
        else:
            logger.info(f"Attempting download without cookies (is_x={is_x})...")
            try:
                last_stderr = run_ytdlp_download(url, download_path, is_carousel, is_x, use_cookies=False, progress=progress, timeout=60)
            except Exception as e:
                logger.warning(f"Download without cookies failed: {e}")
                last_stderr = str(e)

        # If yt-dlp finished and is_x still has no media, try get_x_media as fallback
        if is_x:
            after_files = set(f.name for f in download_path.iterdir() if f.is_file() and not f.name.endswith(".info.json"))
            if not (after_files - before_files) and not all_image_urls:
                x_info, x_urls = get_x_media(url)
                if x_urls:
                    if not info and x_info:
                        info = x_info
                    for u in x_urls:
                        if u not in all_image_urls:
                            all_image_urls.append(u)

    for f in sorted(download_path.glob("*.info.json")):
        try:
            with open(f) as fp:
                data = json.load(fp)
                if not info:
                    info = data
                logger.info(f"Info file {f.name}: keys={list(data.keys())[:15]}")
                try:
                    f.unlink()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Failed to read info file {f.name}: {e}")
            continue
    logger.info(f"All info files parsed")

    is_tiktok = "tiktok.com" in url_lower or "vm.tiktok.com" in url_lower

    if is_carousel and not all_image_urls:
        if is_tiktok:
            logger.info("TikTok: skipping Instagram carousel extractor, using TikTok extractor")
        else:
            carousel_urls = get_carousel_image_urls(url)
            for u in carousel_urls:
                if u not in all_image_urls:
                    all_image_urls.append(u)
            logger.info(f"After carousel fetch: {len(all_image_urls)} image URL(s)")

    if is_tiktok and not all_image_urls:
        logger.info("TikTok: no images from info files, trying --dump-single-json")
        tiktok_urls = get_tiktok_image_urls(url)
        for u in tiktok_urls:
            if u not in all_image_urls:
                all_image_urls.append(u)
        logger.info(f"After TikTok fetch: {len(all_image_urls)} image URL(s)")

    image_urls = all_image_urls
    logger.info(f"Final image URL(s): {len(image_urls)}")
    seen_hashes = set()
    image_idx = 0
    total_urls = len(image_urls)
    for image_url in image_urls:
        def _dl_progress(downloaded, total, _idx=image_idx, _total=total_urls):
            pct = int(downloaded * 100 / total) if total > 0 else 0
            mb_done = downloaded / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            if progress is not None:
                progress["msg"] = f"⬇️ تحميل {_idx+1}/{_total}: {pct}% ({mb_done:.1f}/{mb_total:.1f}MB)"
        fp = download_image_ytdlp(image_url, download_path, image_idx, progress_cb=_dl_progress)
        image_idx += 1
        if fp:
            h = hashlib.md5(Path(fp).read_bytes()).hexdigest()
            if h not in seen_hashes:
                seen_hashes.add(h)
                downloaded_files.append(fp)
                logger.info(f"Image downloaded: {Path(fp).name}")
            else:
                Path(fp).unlink()
        else:
            logger.error(f"Image download failed: {image_url[:100]}")

    after_files = set(f.name for f in download_path.iterdir() if f.is_file() and not f.name.endswith(".info.json"))
    for fname in sorted(after_files - before_files):
        fpath = download_path / fname
        if not (fpath.is_file() and fpath.stat().st_size > 0):
            continue
        h = hashlib.md5(fpath.read_bytes()).hexdigest()
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        downloaded_files.append(str(fpath))

    for f in download_path.glob("*.info.json"):
        try:
            f.unlink()
        except Exception:
            pass

    downloaded_files = dedupe_thumbnails(downloaded_files)

    if not downloaded_files:
        tail = (last_stderr or "").strip().splitlines()
        hint = tail[-1] if tail else ""
        if "No video could be found" in hint or "no suitable formats" in hint.lower():
            reason = "تعذر العثور على ميديا في هذا الرابط (قد يكون المنشور نصياً فقط، أو الحساب خاص، أو تم حذف الفيديو)."
        elif "Private video" in hint or "login required" in hint.lower() or "requires authentication" in hint.lower():
            reason = "هذا المحتوى خاص أو يتطلب تسجيل الدخول لمشاهدته."
        elif "Rate-limit" in hint or "429" in hint:
            reason = "تم تجاوز حد الطلبات المسموح به من الموقع مؤقتاً. يرجى المحاولة بعد قليل."
        elif "404" in hint or "not found" in hint.lower():
            reason = "الرابط غير موجود أو تم حذف المنشور من قِبل صاحبه."
        elif is_x:
            reason = (
                "تعذر استخراج الفيديو من X (تويتر).\n"
                f"السبب: {hint[:150] if hint else 'لم يتم العثور على ميديا'}\n"
                "لو المحتوى خاص أو مقيد بالعمر، يمكنك إرسال كوكيز جديدة عبر /cookies"
            )
        else:
            reason = f"تعذر استخراج الميديا من الرابط.\n{hint[:150]}" if hint else "تعذر استخراج الميديا من هذا الرابط."

        raise ValueError(reason)

    return info, downloaded_files


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Bot is for personal use only.")
        return
    await update.message.reply_text(
        "Send me a link and I will download it.\n\n"
        "Supported:\n"
        "- X/Twitter videos and images\n"
        "- Instagram posts, reels, stories\n"
        "- Facebook videos and reels\n"
        "- TikTok videos",
        reply_markup=start_keyboard,
    )


async def cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_download":
        msg_id = query.message.message_id
        cancelled_downloads.add(msg_id)
        await query.edit_message_text("تم إلغاء هذا التحميل.")


async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global COOKIES_FILE
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Bot is for personal use only.")
        return

    if update.message.document:
        file_id = update.message.document.file_id
        cookies_path = COOKIES_FILE if COOKIES_FILE else "cookies.txt"
        try:
            file = await context.bot.get_file(file_id)
            if hasattr(file, "download_to_drive"):
                await file.download_to_drive(cookies_path)
            else:
                await file.download(cookies_path)
            COOKIES_FILE = cookies_path
            await update.message.reply_text(
                "✅ Cookies uploaded successfully!\n"
                "Now try sending an X/Twitter link again.\n\n"
                "✅ ملف الكوكيز اتحمل بنجاح!\n"
                "جرب ابعت لنك اكس تاني الحين."
            )
            logger.info("Cookies file uploaded successfully")
        except Exception as e:
            logger.error(f"Failed to upload cookies: {e}")
            await update.message.reply_text(f"Failed to upload cookies: {e}")
    else:
        await update.message.reply_text(
            "Send me your cookies.txt file.\n\n"
            "ابعتلي ملف cookies.txt بتاعك."
        )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any file sent to the bot is treated as cookies upload."""
    global COOKIES_FILE
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Bot is for personal use only.")
        return
    if update.message.document:
        file_id = update.message.document.file_id
        cookies_path = COOKIES_FILE if COOKIES_FILE else "cookies.txt"
        try:
            file = await context.bot.get_file(file_id)
            if hasattr(file, "download_to_drive"):
                await file.download_to_drive(cookies_path)
            else:
                await file.download(cookies_path)
            COOKIES_FILE = cookies_path
            await update.message.reply_text(
                "✅ Cookies uploaded successfully!\n"
                "Now try sending an X/Twitter link again.\n\n"
                "✅ ملف الكوكيز اتحمل بنجاح!\n"
                "جرب ابعت لنك اكس تاني الحين."
            )
            logger.info("Cookies file uploaded successfully via document handler")
        except Exception as e:
            logger.error(f"Failed to upload cookies: {e}")
            await update.message.reply_text(f"Failed to upload cookies: {e}")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("Bot is for personal use only.")
        return

    text = update.message.text.strip()
    if text.lower() in ("start", "/start"):
        await start(update, context)
        return

    url = extract_url(text)
    if not url:
        await update.message.reply_text(
            "Please send a valid link.\n\n"
            "Supported:\n"
            "- X/Twitter: https://x.com/user/status/123\n"
            "- Instagram: https://instagram.com/p/ABC/\n"
            "- Facebook: https://facebook.com/user/videos/123"
        )
        return

    chat_id = update.effective_chat.id

    queued = False
    async with _queue_lock:
        global _waiting_count
        if _download_semaphore.locked():
            _waiting_count += 1
            queued = True
            position = _waiting_count + 1
            status_text = f"⏳ في دور #{position} — في انتظار انتهاء التحميل السابق..."
        else:
            position = 1
            status_text = "⏳ جاري التحميل..."

    status_msg = await update.message.reply_text(status_text, reply_markup=cancel_keyboard)
    status_id = status_msg.message_id

    req_dir = DOWNLOAD_DIR / f"req_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
    req_dir.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    _edit_lock = asyncio.Lock()
    file_paths = None
    info = None

    async def safe_edit(msg, text):
        async with _edit_lock:
            for _ in range(2):
                try:
                    await msg.edit_text(text)
                    return True
                except telegram.error.RetryAfter as ra:
                    await asyncio.sleep(ra.retry_after + 0.5)
                except telegram.error.BadRequest as br:
                    if "Message is not modified" in str(br):
                        return True
                    break
                except TelegramError:
                    break
            return False

    async def notify_stopped(reason: str):
        """Notify user clearly via status edit AND an explicit new reply that the download has stopped."""
        full_msg = f"🛑 توقف التحميل:\n{reason}"
        await safe_edit(status_msg, full_msg)
        try:
            await update.message.reply_text(full_msg)
        except Exception as e:
            logger.error(f"Failed to send stop notification reply: {e}")

    try:
        async with _download_semaphore:
            if queued:
                async with _queue_lock:
                    _waiting_count = max(0, _waiting_count - 1)
                queued = False
                await safe_edit(status_msg, "⏳ جاري التحميل... (0ث)")

            if status_id in cancelled_downloads:
                await notify_stopped("تم إلغاء التحميل بناءً على طلبك.")
                return

            progress = {"msg": ""}
            start_t = time.time()

            async def _poll_progress():
                last = ""
                while True:
                    await asyncio.sleep(2)
                    elapsed = int(time.time() - start_t)
                    cur = progress.get("msg", "")
                    display = f"{cur} ({elapsed}ث)" if cur else f"⏳ جاري التحميل... ({elapsed}ث)"
                    if display != last:
                        last = display
                        await safe_edit(status_msg, display)

            poll_task = asyncio.ensure_future(_poll_progress())
            try:
                info, file_paths = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, extract_media, url, req_dir, chat_id, status_id, context.application, progress
                    ),
                    timeout=140,
                )
            except asyncio.TimeoutError:
                await notify_stopped("استغرق التحميل وقتاً طويلاً (تجاوزت المهلة دقيقتين) وتوقف.")
                return
            finally:
                poll_task.cancel()
                try:
                    await poll_task
                except asyncio.CancelledError:
                    pass

            if status_id in cancelled_downloads:
                await notify_stopped("تم إلغاء التحميل.")
                return

            if not file_paths:
                await notify_stopped("تعذر العثور على أي ملفات ميديا قابلة للتحميل في هذا الرابط.")
                return

            caption = (info or {}).get("description", "") or (info or {}).get("title", "") or ""
            await safe_edit(status_msg, f"⬆️ رفع {len(file_paths)} ملف...")

            photos = [fp for fp in file_paths if fp.lower().endswith(IMAGE_EXTS)]
            videos = [fp for fp in file_paths if fp.lower().endswith(VIDEO_EXTS)]
            prepared_items = []
            use_local = os.getenv("USE_LOCAL_SERVER", "false").lower() == "true"

            for photo_path in photos:
                if not os.path.exists(photo_path):
                    continue
                fp_size = os.path.getsize(photo_path) / (1024 * 1024)
                use_path = photo_path
                if not use_local and fp_size > 10:
                    cpath = str(photo_path) + ".compressed.jpg"
                    try:
                        await loop.run_in_executor(None, lambda p=str(photo_path), c=str(cpath): subprocess.run([
                            "ffmpeg", "-i", p, "-vf", "scale=min(1280,iw):-2",
                            "-q:v", "2", c, "-y"
                        ]))
                        if os.path.exists(cpath) and os.path.getsize(cpath) < 10 * 1024 * 1024:
                            use_path = cpath
                    except FileNotFoundError:
                        pass
                prepared_items.append((use_path, "photo"))

            upload_limit_mb = 2000 if use_local else 50
            for video_path in videos:
                if not os.path.exists(video_path):
                    continue
                file_mb = os.path.getsize(video_path) / (1024 * 1024)
                if file_mb > upload_limit_mb:
                    await notify_stopped(f"حجم الفيديو ({file_mb:.1f}MB) كبير جداً ويتجاوز الحد الأقصى ({upload_limit_mb}MB).")
                    return
                elif not use_local and file_mb > 50:
                    cpath = str(video_path) + ".compressed.mp4"
                    try:
                        compressed = await loop.run_in_executor(None, compress_video, video_path, cpath, 49.0)
                        prepared_items.append((cpath if compressed and os.path.exists(cpath) else video_path, "video"))
                    except ValueError as ve:
                        await notify_stopped(str(ve))
                        return
                else:
                    if use_local:
                        await loop.run_in_executor(None, ensure_faststart, video_path)
                    prepared_items.append((video_path, "video"))

            video_meta_cache = {}
            thumb_cache = {}
            successful_sends = 0
            last_upload_error = ""

            photo_items = [(path, kind) for path, kind in prepared_items if kind == "photo"]
            video_items = [(path, kind) for path, kind in prepared_items if kind == "video"]

            # Try to send as media group first if multiple items
            sent_group = False
            if len(photo_items) > 1 or (len(photo_items) >= 1 and len(video_items) >= 1) or len(video_items) > 1:
                all_items = photo_items + video_items
                seen_paths = set()
                unique_items = []
                for path, kind in all_items:
                    if path not in seen_paths:
                        seen_paths.add(path)
                        unique_items.append((path, kind))
                if len(unique_items) > 1:
                    chunk_size = 10
                    all_chunks_sent = True
                    for i in range(0, len(unique_items), chunk_size):
                        chunk = unique_items[i:i + chunk_size]
                        if len(chunk) == 1:
                            path, kind = chunk[0]
                            try:
                                if kind == "photo":
                                    await _send_photo_isolated(chat_id, path, caption if i == 0 else "", timeout=120)
                                else:
                                    await _send_video_isolated(chat_id, path, caption if i == 0 else "", timeout=300)
                                successful_sends += 1
                                _cleanup_file(path)
                            except Exception as e:
                                logger.warning(f"Single item send failed: {e}")
                                last_upload_error = str(e)
                                all_chunks_sent = False
                        else:
                            try:
                                group_ok = await send_media_group_isolated(context.bot, chat_id, chunk, caption if i == 0 else "")
                                if group_ok:
                                    successful_sends += len(chunk)
                                    for path, _ in chunk:
                                        _cleanup_file(path)
                                else:
                                    all_chunks_sent = False
                                    break
                            except Exception as e:
                                logger.warning(f"Media group send failed: {e}")
                                last_upload_error = str(e)
                                all_chunks_sent = False
                                break
                    if all_chunks_sent and successful_sends > 0:
                        sent_group = True

            if not sent_group:
                for idx, (path, kind) in enumerate(prepared_items):
                    if not os.path.exists(path):
                        continue
                    if status_id in cancelled_downloads:
                        await notify_stopped("تم إلغاء التحميل.")
                        break

                    file_mb = os.path.getsize(path) / (1024 * 1024) if os.path.exists(path) else 0
                    upload_timeout = 300 if kind == "video" else 120

                    for attempt in range(2):
                        try:
                            await safe_edit(status_msg, f"⬆️ رفع {idx+1}/{len(prepared_items)} — {os.path.basename(path)} ({file_mb:.1f}MB)")
                            if kind == "photo":
                                await _send_photo_isolated(chat_id, path, caption, timeout=120)
                            else:
                                if path not in video_meta_cache:
                                    video_meta_cache[path] = await loop.run_in_executor(None, get_video_metadata, str(path))
                                meta = video_meta_cache[path]
                                if path not in thumb_cache:
                                    thumb_cache[path] = await loop.run_in_executor(None, extract_thumbnail, str(path))
                                await _send_video_isolated(chat_id, path, caption, meta=meta, thumb_path=thumb_cache.get(path), timeout=upload_timeout)
                            logger.info(f"Sent {kind}: {os.path.basename(path)} ({file_mb:.1f}MB)")
                            successful_sends += 1
                            break
                        except asyncio.TimeoutError:
                            last_upload_error = f"انتهت مهلة إرسال الملف ({file_mb:.1f}MB)"
                            logger.error(f"Send timed out for {path} (attempt {attempt+1})")
                            if attempt == 0:
                                await safe_edit(status_msg, f"⚠️ رفع {os.path.basename(path)} علق — بجرب تاني...")
                                await asyncio.sleep(2)
                        except Exception as e:
                            last_upload_error = str(e)
                            logger.error(f"Send failed for {path} (attempt {attempt+1}): {e}")
                            if attempt == 0:
                                await safe_edit(status_msg, f"⚠️ فشل {os.path.basename(path)} — بجرب تاني...")
                                await asyncio.sleep(2)
                    _cleanup_file(path)

            if successful_sends > 0:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                if successful_sends < len(prepared_items):
                    await update.message.reply_text(f"⚠️ تم إرسال {successful_sends} من أصل {len(prepared_items)} ملفات (تعذر إرسال الباقي).")
            else:
                await notify_stopped(f"تعذر إرسال الملفات إلى تيليجرام:\n{last_upload_error[:150] if last_upload_error else 'حدث خطأ أثناء الإرسال'}")

    except asyncio.TimeoutError:
        logger.error(f"Timeout processing {url}")
        await notify_stopped("استغرقت العملية وقتاً طويلاً وتوقفت.")
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error processing {url}: {error_msg}")
        await notify_stopped(error_msg)
    finally:
        if queued:
            async with _queue_lock:
                _waiting_count = max(0, _waiting_count - 1)
        cancelled_downloads.discard(status_id)
        try:
            shutil.rmtree(req_dir, ignore_errors=True)
        except Exception:
            pass


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set in .env file")

    builder = Application.builder().token(BOT_TOKEN)

    use_local = os.getenv("USE_LOCAL_SERVER", "false").lower() == "true"
    local_url = os.getenv("LOCAL_API_URL", "").strip()

    if use_local and local_url:
        server_alive = False
        try:
            req = urllib.request.Request(f"{local_url}/", method="GET")
            with urllib.request.urlopen(req, timeout=3):
                server_alive = True
        except urllib.error.HTTPError:
            server_alive = True
        except Exception as e:
            logger.warning(f"Local server check failed ({e}) — falling back to official Telegram Cloud API")
            server_alive = False

        if server_alive:
            builder = builder.base_url(f"{local_url}/bot")
            builder = builder.base_file_url(f"{local_url}/file/bot")
            builder = builder.connect_timeout(30)
            builder = builder.read_timeout(300)
            builder = builder.write_timeout(300)
            logger.info(f"Using LOCAL Bot API Server at {local_url} (2GB upload limit)")
        else:
            os.environ["USE_LOCAL_SERVER"] = "false"
            logger.info("Using Telegram Cloud API (50MB limit) + ffmpeg compression")
    else:
        logger.info("Using Telegram Cloud API (50MB limit) + ffmpeg compression")

    application = builder.build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("cookies", cookies_command))
    application.add_handler(CallbackQueryHandler(cancel_button))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    start_health_check_server()
    logger.info("Bot started")

    import time
    while True:
        try:
            application.run_polling(allowed_updates=Update.ALL_TYPES)
        except Exception as e:
            logger.error(f"Bot polling crashed: {e} — restarting in 5s...")
            time.sleep(5)
        except KeyboardInterrupt:
            logger.info("Bot stopped by user")
            break


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(f"FATAL: {e}")
        raise
