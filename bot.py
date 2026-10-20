import os
import socket

# Smart IPv4/IPv6 resolution: prefers IPv4 to avoid broken IPv6 routes on cloud hosts, with automatic fallback
_orig_getaddrinfo = socket.getaddrinfo
def _smart_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    if family == 0:
        try:
            return _orig_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)
        except Exception:
            return _orig_getaddrinfo(host, port, 0, type, proto, flags)
    return _orig_getaddrinfo(host, port, family, type, proto, flags)
socket.getaddrinfo = _smart_getaddrinfo

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
import sqlite3
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip().strip('"').strip("'")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0").strip().strip('"').strip("'"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads").strip().strip('"').strip("'"))
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip().strip('"').strip("'")
DOWNLOAD_DIR.mkdir(exist_ok=True)
BOT_VERSION = "v3.4-direct-session-support-and-auto-persistence"

DB_PATH = Path("archive.db")

DEFAULT_ARCHIVE_CHANNEL_ID = -1004482083833

def init_db():
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS archive (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_id TEXT UNIQUE,
                    platform TEXT,
                    source_url TEXT,
                    channel_msg_id INTEGER DEFAULT 0,
                    caption TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            conn.execute("""
                INSERT OR IGNORE INTO settings (key, value) VALUES ('archive_channel_id', '-1004482083833')
            """)
            conn.execute("""
                INSERT OR IGNORE INTO settings (key, value) VALUES ('archive_channel_title', 'SD')
            """)
            conn.commit()
            logger.info("Archive database initialized successfully with default channel SD (-1004482083833)")
    except Exception as e:
        logger.error(f"init_db failed: {e}")

def get_canonical_post_id(url: str) -> tuple[str, str]:
    """Returns unique (post_id, platform_name) to reliably detect duplicates."""
    clean_url = url.split("?")[0].rstrip("/")
    m = re.search(r"(?:x|twitter)\.com/[^/]+/status/(\d+)", url, re.I)
    if m:
        return f"x_{m.group(1)}", "X_Twitter"
    m = re.search(r"instagram\.com/(?:p|reel|reels|tv|stories)/([A-Za-z0-9_-]+)", url, re.I)
    if m:
        return f"ig_{m.group(1)}", "Instagram"
    m = re.search(r"tiktok\.com/@[^/]+/(?:video|photo)/(\d+)", url, re.I)
    if m:
        return f"tt_{m.group(1)}", "TikTok"
    m = re.search(r"(?:vm|vt|lite)\.tiktok\.com/([A-Za-z0-9_-]+)", url, re.I)
    if m:
        return f"tt_{m.group(1)}", "TikTok"
    m = re.search(r"tiktok\.com/t/([A-Za-z0-9_-]+)", url, re.I)
    if m:
        return f"tt_{m.group(1)}", "TikTok"
    m = re.search(r"(?:facebook|fb)\.com/.*/(?:videos|posts|reel)/(\d+)", url, re.I)
    if m:
        return f"fb_{m.group(1)}", "Facebook"
    m = re.search(r"(?:facebook|fb)\.com/watch/?\?v=(\d+)", url, re.I)
    if m:
        return f"fb_{m.group(1)}", "Facebook"
    return "url_" + hashlib.sha256(clean_url.encode()).hexdigest()[:20], "Media"

def is_post_archived(post_id: str) -> dict | None:
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            cur = conn.execute("SELECT * FROM archive WHERE post_id = ?", (post_id,))
            row = cur.fetchone()
            return dict(row) if row else None
    except Exception as e:
        logger.error(f"is_post_archived error: {e}")
        return None

def save_archive_item(post_id: str, platform: str, source_url: str, channel_msg_id: int = 0, caption: str = ""):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO archive (post_id, platform, source_url, channel_msg_id, caption) VALUES (?, ?, ?, ?, ?)",
                (post_id, platform, source_url, channel_msg_id, (caption or "")[:1000])
            )
            conn.commit()
    except Exception as e:
        logger.error(f"save_archive_item error: {e}")

def get_archive_channel():
    env_ch = os.getenv("ARCHIVE_CHANNEL_ID", "").strip()
    if env_ch:
        try:
            return int(env_ch)
        except ValueError:
            return env_ch
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.execute("SELECT value FROM settings WHERE key = 'archive_channel_id'")
            row = cur.fetchone()
            if row and row[0]:
                try:
                    return int(row[0])
                except ValueError:
                    return row[0]
    except Exception as e:
        logger.error(f"get_archive_channel error: {e}")
    return DEFAULT_ARCHIVE_CHANNEL_ID

def set_archive_channel(channel_id, channel_title=""):
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES ('archive_channel_id', ?)",
                (str(channel_id),)
            )
            if channel_title:
                conn.execute(
                    "INSERT OR REPLACE INTO settings (key, value) VALUES ('archive_channel_title', ?)",
                    (str(channel_title),)
                )
            conn.commit()
            logger.info(f"Archive channel set to {channel_title} ({channel_id})")
    except Exception as e:
        logger.error(f"set_archive_channel error: {e}")


def save_setting(key: str, value: str):
    """Persist key-value setting in SQLite."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, str(value)))
            conn.commit()
    except Exception as e:
        logger.error(f"save_setting failed: {e}")


def get_setting(key: str, default: str = None) -> str:
    """Retrieve key-value setting from SQLite."""
    try:
        with sqlite3.connect(DB_PATH) as conn:
            cur = conn.cursor()
            cur.execute("SELECT value FROM settings WHERE key = ?", (key,))
            row = cur.fetchone()
            return row[0] if row else default
    except Exception as e:
        logger.error(f"get_setting failed: {e}")
        return default


def merge_cookie_texts(old_text: str, new_text: str) -> str:
    """Merge Netscape cookie files, retaining existing lines and updating/adding new ones."""
    lines = {}
    header = "# Netscape HTTP Cookie File\n# This file was generated by bot\n\n"
    for text in [old_text, new_text]:
        for line in text.splitlines():
            line_s = line.strip()
            if not line_s or line_s.startswith("#"):
                continue
            parts = line_s.split("\t")
            if len(parts) >= 7:
                domain, _, path, _, _, name, _ = parts[:7]
                key = (domain.lower(), path, name)
                lines[key] = line_s
    return header + "\n".join(lines.values()) + "\n"


def restore_cookies_from_settings():
    """Ensure cookies.txt is populated from DB or environment variables on startup."""
    try:
        cookies_path = Path(COOKIES_FILE if COOKIES_FILE else "cookies.txt")
        env_sess = os.getenv("INSTAGRAM_SESSIONID", "").strip()
        env_cookies = os.getenv("COOKIES_TEXT", "").strip()
        db_cookies = get_setting("cookies_txt", "")

        curr_text = cookies_path.read_text(encoding="utf-8", errors="ignore") if cookies_path.exists() else ""
        merged = curr_text
        if db_cookies:
            merged = merge_cookie_texts(merged, db_cookies)
        if env_cookies:
            merged = merge_cookie_texts(merged, env_cookies)
        if env_sess:
            sess_line = f".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t{env_sess}\n"
            merged = merge_cookie_texts(merged, sess_line)

        if merged and merged != curr_text:
            cookies_path.write_text(merged, encoding="utf-8")
            logger.info("Restored persistent cookies on startup from DB/Env")
    except Exception as e:
        logger.warning(f"Could not restore cookies: {e}")


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

    audio_bitrate_kbps = 96 if duration > 600 else 128
    total_bitrate = (target_bytes * 8 * 0.95) / duration
    video_bitrate_kbps = max(50, int((total_bitrate / 1000) - audio_bitrate_kbps))

    if video_bitrate_kbps < 100:
        minutes = int(duration // 60)
        size_mb = int(size_bytes / (1024 * 1024))
        raise ValueError(
            f"الفيديو طويل جداً ({minutes} دقيقة، {size_mb}MB).\n"
            f"الحد الأقصى للإرسال على تيليجرام هو 50MB، وضغط فيديو بهذه المدة الطويلة سيفقده الجودة تماماً.\n"
            f"جرب تبعت فيديو أقصر أو أصغر."
        )

    scale_filter = []
    if video_bitrate_kbps < 300:
        scale_filter = ["-vf", "scale=min(640,iw):-2"]
    elif video_bitrate_kbps < 600:
        scale_filter = ["-vf", "scale=min(854,iw):-2"]
    elif video_bitrate_kbps < 1200:
        scale_filter = ["-vf", "scale=min(1280,iw):-2"]

    maxrate_kbps = int(video_bitrate_kbps * 1.5)
    bufsize_kbps = int(video_bitrate_kbps * 2)

    logger.info(f"Compressing: {size_bytes/(1024*1024):.1f}MB target={target_mb}MB duration={duration:.0f}s vbitrate={video_bitrate_kbps}k abitrate={audio_bitrate_kbps}k")

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c:v", "libx264",
        "-b:v", f"{video_bitrate_kbps}k",
        "-maxrate", f"{maxrate_kbps}k",
        "-bufsize", f"{bufsize_kbps}k",
    ]
    if scale_filter:
        cmd.extend(scale_filter)
    cmd.extend([
        "-preset", "fast",
        "-c:a", "aac",
        "-b:a", f"{audio_bitrate_kbps}k",
        "-movflags", "+faststart",
        output_path,
    ])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
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


def split_video(input_path: str, output_dir: Path | str, target_mb: float = 48.0) -> list[str]:
    """Split video into segments under target_mb using ffmpeg stream copy (-c copy).
    Stream copy is 100% LOSSLESS and near-instantaneous (2-3 seconds).
    Returns list of paths to created segments."""
    input_path = str(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not os.path.exists(input_path):
        return []

    size_bytes = os.path.getsize(input_path)
    target_bytes = target_mb * 1024 * 1024
    if size_bytes <= target_bytes:
        return [input_path]

    duration = get_video_duration(input_path)
    if duration <= 0:
        duration = max(30.0, size_bytes / (2 * 1024 * 1024))

    # Calculate safe segment duration with 10% safety margin to account for keyframe placement
    safe_target_bytes = target_bytes * 0.90
    seg_time = (safe_target_bytes / size_bytes) * duration
    seg_time = max(5.0, round(seg_time, 1))

    stem = Path(input_path).stem
    out_pattern = str(output_dir / f"{stem}_part_%03d.mp4")

    logger.info(
        f"[split_video] Splitting {Path(input_path).name} ({size_bytes/(1024*1024):.1f}MB, {duration:.1f}s) "
        f"into ~{target_mb:.1f}MB chunks (segment_time={seg_time}s)"
    )

    cmd = [
        "ffmpeg", "-y", "-i", input_path,
        "-c", "copy",
        "-map", "0",
        "-f", "segment",
        "-segment_time", str(seg_time),
        "-reset_timestamps", "1",
        "-movflags", "+faststart",
        out_pattern,
    ]

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0:
            logger.error(f"[split_video] ffmpeg segment failed: {proc.stderr[:400]}")
            return []
    except Exception as e:
        logger.error(f"[split_video] Exception during split: {e}")
        return []

    # Collect and sort generated segments
    prefix = f"{stem}_part_"
    parts = sorted([
        str(output_dir / f) for f in os.listdir(output_dir)
        if f.startswith(prefix) and f.endswith(".mp4") and os.path.getsize(output_dir / f) > 1024
    ])

    logger.info(f"[split_video] Successfully created {len(parts)} parts for {Path(input_path).name}")
    return parts


def get_cookie_args() -> list:
    paths = [COOKIES_FILE, "cookies.txt"] if COOKIES_FILE else ["cookies.txt"]
    for p in paths:
        if p and Path(p).exists():
            return ["--cookies", p]
    return []

_consecutive_connect_errors = 0


class SimpleHealthServer(BaseHTTPRequestHandler):
    def do_GET(self):
        global _consecutive_connect_errors
        # If the bot has suffered 6+ consecutive connection errors, signal 503 so health checks notice
        if _consecutive_connect_errors >= 6:
            self.send_response(503)
            self.send_header("Content-type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Bot disconnected from Telegram\n")
            return

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
    # X / Twitter
    r"(?:https?://)?(?:www\.)?(?:x|twitter)\.com/[^\s/]+/status/\d+\S*",
    # Instagram (Posts, Reels, TV, Stories, and Profiles)
    r"(?:https?://)?(?:www\.)?instagram\.com/(?:p|reel|reels|tv|stories)/[A-Za-z0-9_-]+\S*",
    r"(?:https?://)?(?:www\.)?instagram\.com/[A-Za-z0-9_\.]+/?(?:\?[^\s]*)?",
    # TikTok (all post, album, and shortlink formats)
    r"(?:https?://)?(?:www\.|m\.|lite\.)?tiktok\.com/@[\w\.-]+/(?:video|photo)/\d+\S*",
    r"(?:https?://)?(?:www\.|m\.|lite\.)?tiktok\.com/t/[A-Za-z0-9_-]+\S*",
    r"(?:https?://)?(?:vm|vt|lite)\.tiktok\.com/[A-Za-z0-9_-]+\S*",
    r"(?:https?://)?(?:www\.|m\.)?tiktok\.com/v/\d+\S*",
    # Facebook
    r"(?:https?://)?(?:www\.|m\.|web\.)*(?:facebook|fb)\.com/.*/(?:videos|posts)/\S+",
    r"(?:https?://)?(?:www\.|m\.|web\.)*(?:facebook|fb)\.com/watch\S*",
    r"(?:https?://)?fb\.watch/\S+",
    r"(?:https?://)?(?:www\.|m\.|web\.)*(?:facebook|fb)\.com/reel/\S+",
    r"(?:https?://)?(?:www\.|m\.|web\.)*(?:facebook|fb)\.com/share/\S+",
    r"(?:https?://)?(?:www\.|m\.|web\.)*(?:facebook|fb)\.com/permalink\.php\S*",
]


def extract_url(text: str) -> str:
    """Find the first supported link anywhere inside the message text
    (including cluttered share messages from TikTok/TikTok Lite),
    while explicitly filtering out promotional/app-store links."""
    text = text.strip()
    promo_patterns = [
        r"tiktok\.com/(?:tiktoklite|download|app|about|legal|privacy)(?:/|\?|$)",
        r"instagram\.com/(?:about|legal|download|explore|accounts|developer|directory)(?:/|\?|$)",
        r"(?:x|twitter)\.com/(?:download|about)(?:/|\?|$)",
    ]

    for pattern in URL_PATTERNS:
        for match in re.finditer(pattern, text):
            url = match.group(0).rstrip(".,!?;:)>]\"'")
            if any(re.search(promo, url, re.I) for promo in promo_patterns):
                continue
            if not url.lower().startswith("http"):
                url = "https://" + url
            return url
    return None


cancel_keyboard = InlineKeyboardMarkup(
    [[InlineKeyboardButton("Cancel", callback_data="cancel_download")]]
)

start_keyboard = ReplyKeyboardMarkup(
    [[KeyboardButton("Start")]],
    resize_keyboard=True,
)

VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.mov')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')


def make_progress_bar(current: int, total: int, length: int = 10) -> str:
    """Generate visual progress bar [████░░░░░░] 40% from current and total counts."""
    if total <= 0:
        return f"[{'░' * length}] 0%"
    fraction = min(1.0, max(0.0, current / total))
    filled = int(round(fraction * length))
    bar = "█" * filled + "░" * (length - filled)
    pct = int(fraction * 100)
    return f"[{bar}] {pct}%"


def make_progress_bar_pct(pct: float, length: int = 10) -> str:
    """Generate visual progress bar [██████░░░░] from a float percentage (0.0 - 100.0)."""
    fraction = min(1.0, max(0.0, pct / 100.0))
    filled = int(round(fraction * length))
    bar = "█" * filled + "░" * (length - filled)
    return f"[{bar}]"


async def safe_edit_message(target, text: str, parse_mode: str = None, reply_markup=None) -> bool:
    """Safely edit a message or callback query with retry handling, Markdown fallback, and ignoring 'not modified' errors."""
    for _ in range(2):
        try:
            if hasattr(target, "edit_message_text"):
                await target.edit_message_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            elif hasattr(target, "edit_text"):
                await target.edit_text(text, parse_mode=parse_mode, reply_markup=reply_markup)
            return True
        except telegram.error.RetryAfter as ra:
            await asyncio.sleep(ra.retry_after + 0.5)
        except telegram.error.BadRequest as br:
            err = str(br)
            if "Message is not modified" in err:
                return True
            if "Can't parse entities" in err and parse_mode is not None:
                # Markdown entity error fallback: retry without parse_mode
                try:
                    if hasattr(target, "edit_message_text"):
                        await target.edit_message_text(text, parse_mode=None, reply_markup=reply_markup)
                    elif hasattr(target, "edit_text"):
                        await target.edit_text(text, parse_mode=None, reply_markup=reply_markup)
                    return True
                except Exception:
                    pass
            break
        except Exception as e:
            logger.debug(f"safe_edit_message error: {e}")
            break
    return False


def _get_api_url():
    use_local = os.getenv("USE_LOCAL_SERVER", "false").lower() == "true"
    local_url = os.getenv("LOCAL_API_URL", "").strip()
    if use_local and local_url:
        return f"{local_url.rstrip('/')}/bot{BOT_TOKEN}"
    return f"https://api.telegram.org/bot{BOT_TOKEN}"


async def _curl_upload(api_url, endpoint, path, extra_fields=None, timeout=300, thumb_path=None):
    """Run curl in a subprocess with EXPLICIT lifecycle management and Cloud API fallback."""
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

        # If local server timed out, retry immediately on Telegram Cloud API!
        if "127.0.0.1" in api_url or "localhost" in api_url:
            cloud_url = f"https://api.telegram.org/bot{BOT_TOKEN}"
            logger.warning(f"[upload] Local server timed out on {endpoint}. Retrying on Cloud API...")
            cmd_cloud = list(cmd)
            for ci, carg in enumerate(cmd_cloud):
                if f"{api_url}/{endpoint}" in carg:
                    cmd_cloud[ci] = f"{cloud_url}/{endpoint}"
            try:
                proc2 = await asyncio.create_subprocess_exec(
                    *cmd_cloud,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout2, stderr2 = await asyncio.wait_for(proc2.communicate(), timeout=timeout)
                out2 = stdout2.decode(errors="replace").strip()
                res2 = json.loads(out2) if out2 else {}
                if res2.get("ok"):
                    logger.info(f"[upload] {endpoint} succeeded via Cloud API fallback after local timeout")
                    return res2
            except Exception as ce:
                logger.error(f"[upload] Cloud fallback after timeout failed: {ce}")

        raise asyncio.TimeoutError(f"Upload timed out after {timeout}s")

    stdout, stderr = comm_task.result()
    output = stdout.decode(errors="replace").strip()
    result = json.loads(output) if output else {}

    # If failed on local server, fallback to official Telegram Cloud API immediately!
    if proc.returncode not in (0, None) or not result.get("ok"):
        err = result.get('description', output[:200]) if result else stderr.decode(errors="replace").strip()[:300]
        if "127.0.0.1" in api_url or "localhost" in api_url:
            cloud_url = f"https://api.telegram.org/bot{BOT_TOKEN}"
            logger.warning(f"[upload] Local server returned error for {endpoint} ({err}). Retrying via Cloud API...")
            cmd_cloud = list(cmd)
            for ci, carg in enumerate(cmd_cloud):
                if f"{api_url}/{endpoint}" in carg:
                    cmd_cloud[ci] = f"{cloud_url}/{endpoint}"
            try:
                proc2 = await asyncio.create_subprocess_exec(
                    *cmd_cloud,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout2, stderr2 = await asyncio.wait_for(proc2.communicate(), timeout=timeout)
                out2 = stdout2.decode(errors="replace").strip()
                res2 = json.loads(out2) if out2 else {}
                if res2.get("ok"):
                    logger.info(f"[upload] {endpoint} succeeded via Cloud API fallback")
                    return res2
            except Exception as ce:
                logger.error(f"[upload] Cloud API fallback failed: {ce}")

        if proc.returncode not in (0, None):
            raise Exception(f"curl rc={proc.returncode}: {err}")
        raise Exception(f"Telegram: {err}")

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


async def _curl_upload_media_group(chat_id, media_items, caption="", timeout=600, thumb_map=None, meta_map=None):
    """Upload an album (2-10 items of photos, videos, or mixed) via curl directly to Telegram Bot API."""
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
    for idx, item_data in enumerate(media_items[:10]):
        path = item_data[0]
        kind = item_data[1]
        item_cap = item_data[2] if len(item_data) > 2 else (caption if idx == 0 and caption else None)
        key = f"file_{idx}"
        item = {
            "type": "photo" if kind == "photo" else "video",
            "media": f"attach://{key}",
        }
        if item_cap:
            item["caption"] = item_cap[:1024]

        if kind == "video":
            item["supports_streaming"] = True
            if meta_map and path in meta_map and meta_map[path]:
                meta = meta_map[path]
                for mk in ("duration", "width", "height"):
                    if meta.get(mk):
                        try:
                            item[mk] = int(meta[mk])
                        except (ValueError, TypeError):
                            pass
            if thumb_map and path in thumb_map and thumb_map[path] and os.path.exists(thumb_map[path]):
                thumb_key = f"thumb_{idx}"
                item["thumbnail"] = f"attach://{thumb_key}"
                cmd.extend(["-F", f"{thumb_key}=@{thumb_map[path]}"])

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
    result = json.loads(output) if output else {}

    if proc.returncode not in (0, None) or not result.get("ok"):
        err_desc = result.get('description', output[:200]) if result else (stderr.decode(errors="replace").strip()[:300])
        logger.warning(f"sendMediaGroup failed on {api_url}: {err_desc}")
        if "localhost" in api_url or "127.0.0.1" in api_url:
            cloud_url = f"https://api.telegram.org/bot{BOT_TOKEN}"
            logger.info("Retrying sendMediaGroup directly via official Telegram Cloud API...")
            cmd_cloud = list(cmd)
            for ci, carg in enumerate(cmd_cloud):
                if f"{api_url}/sendMediaGroup" in carg:
                    cmd_cloud[ci] = f"{cloud_url}/sendMediaGroup"
            try:
                proc2 = await asyncio.create_subprocess_exec(
                    *cmd_cloud,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout2, _ = await asyncio.wait_for(proc2.communicate(), timeout=timeout)
                out2 = stdout2.decode(errors="replace").strip()
                res2 = json.loads(out2) if out2 else {}
                if res2.get("ok"):
                    logger.info(f"[upload] sendMediaGroup succeeded via Cloud API ({len(media_items)} items)")
                    return True
                logger.error(f"Cloud API retry failed: {res2.get('description', out2[:200])}")
            except Exception as e:
                logger.error(f"Cloud API retry exception: {e}")
        return False

    logger.info(f"[upload] sendMediaGroup succeeded with {len(media_items)} items")
    return True


async def send_media_group_isolated(bot, chat_id, media_list, caption="", thumb_map=None, meta_map=None):
    """Send a media group (album) of photos/videos in one message.
    media_list: list of (path, kind) tuples where kind is 'photo' or 'video'"""
    if len(media_list) < 2:
        return False

    # Attempt 1: Isolated curl (handles local server, large files, streaming without RAM bloat)
    try:
        ok = await _curl_upload_media_group(chat_id, media_list, caption, thumb_map=thumb_map, meta_map=meta_map)
        if ok:
            return True
    except Exception as e:
        logger.warning(f"Curl media group upload failed, trying python-telegram-bot fallback: {e}")

    # Attempt 2: python-telegram-bot fallback with properly opened binary files
    file_handles = []
    try:
        grouped_media = []
        for idx, item_data in enumerate(media_list[:10]):
            path = item_data[0]
            kind = item_data[1]
            if not os.path.exists(path):
                continue
            item_caption = item_data[2] if len(item_data) > 2 else (caption if idx == 0 and caption else None)
            fh = open(path, "rb")
            file_handles.append(fh)
            if kind == "photo":
                grouped_media.append(InputMediaPhoto(media=fh, caption=item_caption))
            elif kind == "video":
                thumb_fh = None
                if thumb_map and path in thumb_map and thumb_map[path] and os.path.exists(thumb_map[path]):
                    try:
                        thumb_fh = open(thumb_map[path], "rb")
                        file_handles.append(thumb_fh)
                    except Exception:
                        thumb_fh = None
                meta = meta_map.get(path) if meta_map else None
                grouped_media.append(InputMediaVideo(
                    media=fh,
                    caption=item_caption,
                    supports_streaming=True,
                    thumbnail=thumb_fh,
                    duration=int(meta["duration"]) if meta and meta.get("duration") else None,
                    width=int(meta["width"]) if meta and meta.get("width") else None,
                    height=int(meta["height"]) if meta and meta.get("height") else None,
                ))

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
    finally:
        for tmp_file in (raw_thumb, thumb_path + ".small.jpg"):
            try:
                if os.path.exists(tmp_file):
                    os.remove(tmp_file)
            except Exception:
                pass
    return None


def fix_media_file_extension(fpath: Path) -> Path:
    """Check magic bytes of the file and ensure its extension matches its actual content.
    Prevents photos from being mistakenly saved and sent as corrupted/1-second empty videos."""
    if not fpath.exists() or fpath.stat().st_size < 12:
        return fpath

    try:
        with open(fpath, "rb") as f:
            header = f.read(32)

        new_ext = None
        if header.startswith(b"\xff\xd8\xff"):
            new_ext = ".jpg"
        elif header.startswith(b"\x89PNG\r\n\x1a\n"):
            new_ext = ".png"
        elif header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            new_ext = ".webp"
        elif header.startswith(b"GIF87a") or header.startswith(b"GIF89a"):
            new_ext = ".gif"
        elif b"ftyp" in header[4:16] or header.startswith(b"\x1a\x45\xdf\xa3"):
            new_ext = ".mp4"

        if new_ext and fpath.suffix.lower() != new_ext:
            target_path = fpath.with_suffix(new_ext)
            if target_path.exists():
                target_path.unlink()
            fpath.rename(target_path)
            logger.info(f"Corrected media extension based on magic bytes: {fpath.name} -> {target_path.name}")
            return target_path
    except Exception as e:
        logger.warning(f"Could not check/fix extension for {fpath}: {e}")

    return fpath


def _download_direct_url(url: str, dest: Path, timeout: int = 300, progress_cb=None) -> bool:
    """Download a direct URL (e.g. Twitter/TikTok/Instagram CDN MP4 or photo) via urllib with proper headers."""
    referer = "https://x.com/"
    if "tiktok" in url or "tiktokcdn" in url or "tikwm" in url:
        referer = "https://www.tiktok.com/"
    elif "instagram" in url or "cdninstagram" in url or "fbcdn.net" in url:
        referer = "https://www.instagram.com/"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        "Referer": referer,
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
    url_low = image_url.lower()
    is_image = (
        re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", url_low)
        or "photomode" in url_low
        or "image" in url_low
        or "/photo/" in url_low
    )
    is_video = (
        not is_image
        and (
            ".mp4" in url_low
            or "video.twimg.com" in url_low
            or "mime_type=video_mp4" in url_low
            or "/video/" in url_low
            or "/play/" in url_low
        )
    )
    if is_video:
        ext = "mp4"
    elif ".png" in url_low:
        ext = "png"
    elif ".webp" in url_low:
        ext = "webp"
    else:
        ext = "jpg"
    fpath = download_path / f"{idx:05d}.{ext}"

    # If it is a direct image URL (including pbs.twimg.com, fbcdn.net) or Twitter/TikTok CDN video, download directly via HTTP!
    if (
        "twimg.com" in image_url
        or "tiktokcdn" in image_url
        or "tikwm.com" in image_url
        or "cdninstagram.com" in image_url
        or "fbcdn.net" in image_url
        or "mime_type=video_mp4" in image_url
        or re.search(r"\.(jpg|jpeg|png|webp|mp4)(\?|$)", image_url, re.I)
    ):
        logger.info(f"Direct downloading media ({'video' if is_video else 'photo'}): {image_url[:80]}")
        if _download_direct_url(image_url, fpath, progress_cb=progress_cb):
            fpath = fix_media_file_extension(fpath)
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
            fixed_f = fix_media_file_extension(f)
            return str(fixed_f)
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
    timeout: int = 90,
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
        "--progress-template", "download:PROGRESS %(progress._percent_str)s | %(progress._downloaded_bytes_str)s/%(progress._total_bytes_str)s | %(progress._speed_str)s | ETA %(progress._eta_str)s",
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
            if "PROGRESS" in line_s:
                parts = line_s.replace("PROGRESS", "").strip().split("|")
                pct_str = parts[0].strip() if len(parts) > 0 else ""
                size_str = parts[1].strip() if len(parts) > 1 else ""
                speed_str = parts[2].strip() if len(parts) > 2 else ""
                eta_str = parts[3].strip() if len(parts) > 3 else ""

                pct_val = 0.0
                m = re.search(r"(\d+(?:\.\d+)?)%", pct_str)
                if m:
                    pct_val = float(m.group(1))
                bar = make_progress_bar_pct(pct_val)

                msg = f"⏳ **جاري تحميل الفيديو:**\n{bar} {pct_str}"
                if size_str and "N/A" not in size_str:
                    msg += f" ({size_str})"
                if speed_str and "Unknown" not in speed_str:
                    msg += f"\n⚡ السرعة: {speed_str}"
                if eta_str and "Unknown" not in eta_str and "NA" not in eta_str:
                    msg += f" • المتبقي: {eta_str.replace('ETA', '').strip()}"

                if progress is not None:
                    progress["msg"] = msg
            elif line_s.startswith("[download]") and "%" in line_s:
                m_pct = re.search(r"(\d+(?:\.\d+)?)%", line_s)
                if m_pct:
                    pct_val = float(m_pct.group(1))
                    bar = make_progress_bar_pct(pct_val)
                    m_size = re.search(r"of\s+(\S+)", line_s)
                    m_spd = re.search(r"at\s+(\S+)", line_s)
                    m_eta = re.search(r"ETA\s+(\S+)", line_s)
                    msg = f"⏳ **جاري تحميل الفيديو:**\n{bar} {pct_val:.1f}%"
                    if m_size:
                        msg += f" ({m_size.group(1)})"
                    if m_spd:
                        msg += f"\n⚡ السرعة: {m_spd.group(1)}"
                    if m_eta and "Unknown" not in m_eta.group(1):
                        msg += f" • المتبقي: {m_eta.group(1)}"
                    if progress is not None:
                        progress["msg"] = msg

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


def get_tiktok_media(url: str) -> tuple[dict, list]:
    """Fetch direct watermark-free video or photo album URLs from TikTok via TikWM API.
    Guarantees fast, reliable extraction for videos and photo slideshows without yt-dlp blocks."""
    api_url = "https://www.tikwm.com/api/"
    post_data = urllib.parse.urlencode({"url": url, "hd": 1}).encode("utf-8")
    req = urllib.request.Request(
        api_url,
        data=post_data,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded",
        }
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
            if data.get("code") == 0 and "data" in data:
                d = data["data"]
                info = {
                    "title": d.get("title", ""),
                    "description": d.get("title", ""),
                    "uploader": (d.get("author") or {}).get("nickname", ""),
                    "id": str(d.get("id", "")),
                }
                # Check for Photo Mode (images album)
                images = d.get("images")
                if isinstance(images, list) and images:
                    valid_imgs = [img for img in images if isinstance(img, str) and img.startswith("http")]
                    logger.info(f"TikWM API found {len(valid_imgs)} photo(s) in TikTok album")
                    return info, valid_imgs

                # Check for Video (hdplay preferred, then play, then wmplay)
                video_url = d.get("hdplay") or d.get("play") or d.get("wmplay")
                if video_url:
                    logger.info("TikWM API found direct TikTok video URL")
                    return info, [video_url]
    except Exception as e:
        logger.warning(f"TikWM API attempt failed for {url}: {e}")
    return {}, []


def get_instaloader_instance():
    """Creates Instaloader instance with cookies loaded from cookies.txt if available."""
    import instaloader
    L = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        compress_json=False,
        request_timeout=15.0,
        max_connection_attempts=1,
    )
    # Prevent Instaloader from sleeping 11 minutes (660s) on HTTP 429
    def _fast_handle_429(query_type: str):
        raise instaloader.exceptions.ConnectionException("HTTP error 429 - Too Many Requests (Login/Cookies required)")

    if hasattr(L.context, "_rate_controller"):
        L.context._rate_controller.handle_429 = _fast_handle_429

    # Automatically restore cookies from DB/Env if available
    restore_cookies_from_settings()

    cookie_paths = [COOKIES_FILE, "cookies.txt"] if COOKIES_FILE else ["cookies.txt"]
    for cp in cookie_paths:
        if cp and Path(cp).exists():
            try:
                import http.cookiejar
                cj = http.cookiejar.MozillaCookieJar(cp)
                cj.load(ignore_discard=True, ignore_expires=True)

                session_id = None
                csrf_token = None
                ds_user_id = None
                for c in cj:
                    if "instagram.com" in c.domain:
                        if c.name == "sessionid":
                            session_id = c.value
                        elif c.name == "csrftoken":
                            csrf_token = c.value
                        elif c.name == "ds_user_id":
                            ds_user_id = c.value

                L.context._session.cookies = cj
                if csrf_token:
                    L.context._session.headers.update({"X-CSRFToken": csrf_token})
                if ds_user_id:
                    L.context.username = ds_user_id
                elif session_id:
                    L.context.username = "logged_in_user"

                logger.info(f"Loaded cookies from {cp} into Instaloader (sessionid={'YES' if session_id else 'NO'}, user={L.context.username})")
                break
            except Exception as ce:
                logger.warning(f"Could not load cookies from {cp} into Instaloader: {ce}")
    return L


def extract_instagram_shortcode(url: str) -> str:
    m = re.search(r"instagram\.com/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)", url, re.I)
    return m.group(1) if m else None


def is_instagram_profile_url(url: str) -> str | None:
    """If url is an Instagram profile link, return the username. Otherwise None."""
    clean = url.strip()
    m = re.search(r"(?:https?://)?(?:www\.)?instagram\.com/([A-Za-z0-9_\.]+)/?(?:\?[^\s]*)?$", clean, re.I)
    if not m:
        return None
    username = m.group(1).lower()
    system_names = {"p", "reel", "reels", "tv", "stories", "explore", "accounts", "about", "legal", "download", "direct", "developer"}
    if username in system_names:
        return None
    return username


def get_instagram_media(url: str) -> tuple[dict, list]:
    """Fetch direct photo, video, or carousel album URLs from Instagram using Instaloader.
    Handles single photos, multi-photo/video carousels, and reels seamlessly."""
    shortcode = extract_instagram_shortcode(url)
    if not shortcode:
        return {}, []

    try:
        import instaloader
        L = get_instaloader_instance()
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        info = {
            "title": post.caption or "",
            "description": post.caption or "",
            "uploader": post.owner_username or "",
            "id": shortcode,
        }
        urls = []
        if post.typename == "GraphSidecar":
            for node in post.get_sidecar_nodes():
                if node.is_video and node.video_url:
                    urls.append(node.video_url)
                elif node.display_url:
                    urls.append(node.display_url)
        elif post.is_video and post.video_url:
            urls.append(post.video_url)
        elif post.url:
            urls.append(post.url)

        logger.info(f"Instaloader found {len(urls)} media item(s) for Instagram post {shortcode}")
        return info, urls
    except Exception as e:
        logger.warning(f"Instaloader attempt failed for Instagram post {shortcode}: {e}")
        return {}, []


async def send_instagram_profile_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, username: str):
    keyboard = [
        [
            InlineKeyboardButton("📸 تحميل آخر 30 منشور (كل منشور لوحده)", callback_data=f"ig_posts_{username}"),
        ],
        [
            InlineKeyboardButton("⭕ تحميل الاستوري (Stories)", callback_data=f"ig_stories_{username}"),
            InlineKeyboardButton("⭐ الهايلايتس (Highlights)", callback_data=f"ig_hl_{username}"),
        ],
        [
            InlineKeyboardButton("❌ إلغاء", callback_data="cancel_download"),
        ]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    text = (
        f"👤 **بروفايل إنستجرام:** `@{username}`\n\n"
        f"اختر ما تريد تحميله من الحساب:"
    )
    if update.message:
        await update.message.reply_text(text, reply_markup=reply_markup, parse_mode="Markdown")
    elif update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=reply_markup, parse_mode="Markdown")


async def handle_instagram_profile_posts(query, context: ContextTypes.DEFAULT_TYPE, username: str, limit: int = 30):
    chat_id = query.message.chat_id
    loop = asyncio.get_running_loop()
    start_time = time.time()
    await safe_edit_message(query, f"⏳ جاري فحص بروفايل @{username} وجلب آخر {limit} منشور...")

    try:
        import instaloader
        L = get_instaloader_instance()
        profile = await loop.run_in_executor(None, lambda: instaloader.Profile.from_username(L.context, username))
    except Exception as e:
        err_str = str(e)
        logger.error(f"Failed to fetch profile {username}: {e}")
        is_auth_barrier = any(w in err_str.lower() for w in ["429", "302", "401", "403", "login", "redirect", "checkpoint", "two-factor", "cookie", "found when accessing"])
        if is_auth_barrier:
            await safe_edit_message(
                query,
                f"🛑 **يتطلب إنستجرام تسجيل الدخول لتصفح بروفايل @{username}:**\n\n"
                f"خوادم إنستجرام تمنع تصفح البروفايلات بدون حساب وتقوم بالتحويل لصفحة تسجيل الدخول (`302 Redirect`).\n\n"
                f"🔑 **طريقة تشغيل التحميل بسهولة في دقيقة:**\n"
                f"1️⃣ افتح Instagram على متصفحك وسجل دخول لحسابك.\n"
                f"2️⃣ استخرج ملف الكوكيز `cookies.txt` (باستخدام إضافة متصفح مثل *Get cookies.txt LOCALLY*).\n"
                f"3️⃣ أرسل ملف `cookies.txt` هنا في شات البوت كملف عادي 📎.\n\n"
                f"⚡ سيتعرف عليه البوت تلقائياً ويعمل معك تحميل البروفايلات والاستوري والهايلايتس فوراً بدون أي حظر!",
                parse_mode="Markdown"
            )
            return
        else:
            await safe_edit_message(query, f"🛑 تعذر العثور على الحساب @{username}: {e}")
            return

    if getattr(profile, "is_private", False) and not getattr(profile, "followed_by_viewer", True):
        await safe_edit_message(
            query,
            f"🔒 **حساب إنستجرام خاص (Private):**\n\n"
            f"الحساب `@{username}` مقفل ولا يمكن تصفح منشوراته إلا إذا كان حسابك المسجل يتابعه بالفعل.",
            parse_mode="Markdown"
        )
        return

    def _get_posts():
        posts = []
        # Attempt 1: Try profile.get_posts()
        try:
            for p in profile.get_posts():
                posts.append(p)
                if len(posts) >= limit:
                    break
        except Exception as ge:
            logger.warning(f"profile.get_posts() error for {username}: {ge}. Trying web page data fallback...")

        # Fallback: Extract directly from profile web page data if GraphQL failed (e.g. 502 Bad Gateway)
        if not posts:
            try:
                page = L.context.get_page_data(f"{username.lower()}/")
                for item in page:
                    if "xig_user_by_username" in item:
                        u = item["xig_user_by_username"]
                        conn = u.get("polaris_ordered_timeline_connection") or u.get("edge_owner_to_timeline_media")
                        if conn:
                            for edge in conn.get("edges", []):
                                node = edge.get("node", {})
                                code = node.get("code") or node.get("shortcode")
                                if code:
                                    try:
                                        p_obj = instaloader.Post.from_shortcode(L.context, code)
                                        posts.append(p_obj)
                                        if len(posts) >= limit:
                                            break
                                    except Exception as se:
                                        logger.warning(f"Error fetching post {code}: {se}")
            except Exception as pe:
                logger.error(f"Fallback page data extraction failed for {username}: {pe}")

        return posts

    try:
        posts = await asyncio.wait_for(loop.run_in_executor(None, _get_posts), timeout=60)
    except Exception as e:
        err_str = str(e)
        logger.error(f"Error fetching posts for {username}: {e}")
        is_auth_barrier = any(w in err_str.lower() for w in ["429", "302", "401", "403", "login", "redirect", "checkpoint", "two-factor", "cookie", "found when accessing"])
        if is_auth_barrier:
            await safe_edit_message(
                query,
                f"🛑 **توقف جلب المنشورات:** يتطلب إنستجرام ملف كوكيز مسجل (`cookies.txt`).\n\n"
                f"أرسل ملف `cookies.txt` هنا في الشات لتفعيل تصفح المنشورات فوراً.",
                parse_mode="Markdown"
            )
        elif "502" in err_str or "503" in err_str or "504" in err_str:
            await safe_edit_message(
                query,
                f"⚠️ **استجابة بطيئة من خوادم إنستجرام (502 Bad Gateway):**\n\n"
                f"خوادم إنستجرام تشهد ضغطاً مؤقتاً في الرد على طلبات المنشورات.\n"
                f"👉 اضغط على زر التحميل مرة أخرى الآن بعد لحظات وسيتم التحميل بنجاح.",
                parse_mode="Markdown"
            )
        else:
            await safe_edit_message(query, f"🛑 تعذر جلب قائمة المنشورات: {e}")
        return

    if not posts:
        await safe_edit_message(query, f"ℹ️ لا توجد منشورات متاحة في حساب @{username}.")
        return

    total = len(posts)
    sent_count = 0
    failed_count = 0
    req_dir = DOWNLOAD_DIR / f"ig_profile_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
    req_dir.mkdir(parents=True, exist_ok=True)

    try:
        for idx, post in enumerate(posts, 1):
            elapsed = int(time.time() - start_time)
            bar = make_progress_bar(idx - 1, total, length=10)
            status_text = (
                f"📥 **جاري تحميل بروفايل @{username}:**\n"
                f"{bar} ({idx - 1}/{total} تم إرساله)\n\n"
                f"⏳ **جاري الآن معالجة المنشور {idx} من {total}...**\n"
                f"✅ تم بنجاح: {sent_count} | ⏱️ الوقت: {elapsed}ث"
            )
            await safe_edit_message(query, status_text, parse_mode="Markdown")

            post_dir = req_dir / f"post_{idx:03d}"
            post_dir.mkdir(parents=True, exist_ok=True)

            try:
                urls = []
                if post.typename == "GraphSidecar":
                    try:
                        nodes = await loop.run_in_executor(None, lambda: list(post.get_sidecar_nodes()))
                        for node in nodes:
                            if node.is_video and node.video_url:
                                urls.append(node.video_url)
                            elif node.display_url:
                                urls.append(node.display_url)
                    except Exception as se:
                        logger.warning(f"Could not get sidecar nodes for post {idx}: {se}")
                        if post.url:
                            urls.append(post.url)
                elif post.is_video and post.video_url:
                    urls.append(post.video_url)
                elif post.url:
                    urls.append(post.url)

                if not urls:
                    logger.warning(f"No media URLs found for post {idx}")
                    failed_count += 1
                    shutil.rmtree(post_dir, ignore_errors=True)
                    continue

                post_files = []
                for u_idx, media_url in enumerate(urls):
                    fp = await loop.run_in_executor(None, download_image_ytdlp, media_url, post_dir, u_idx)
                    if fp and os.path.exists(fp):
                        fp = str(fix_media_file_extension(Path(fp)))
                        post_files.append(fp)

                if not post_files:
                    logger.warning(f"Failed to download media files for post {idx}")
                    failed_count += 1
                    shutil.rmtree(post_dir, ignore_errors=True)
                    continue

                cap_text = post.caption or ""
                post_caption = f"📸 منشور {idx} من {total} | @{username}\n\n{cap_text}"[:1000]

                prepared_items = []
                for pf in post_files:
                    k = "photo" if pf.lower().endswith(IMAGE_EXTS) else "video"
                    prepared_items.append((pf, k))

                v_meta = {}
                t_cache = {}
                for p, k in prepared_items:
                    if k == "video":
                        v_meta[p] = await loop.run_in_executor(None, get_video_metadata, str(p))
                        t_cache[p] = await loop.run_in_executor(None, extract_thumbnail, str(p))

                sends, _ = await send_media_to_target(
                    context.bot, chat_id, prepared_items, post_caption,
                    v_meta, t_cache, loop
                )
                if sends > 0:
                    sent_count += 1
                else:
                    failed_count += 1

            except Exception as pe:
                logger.error(f"Error processing post {idx} for @{username}: {pe}")
                failed_count += 1
            finally:
                shutil.rmtree(post_dir, ignore_errors=True)

            await asyncio.sleep(1.5)

        final_bar = make_progress_bar(sent_count, total, length=10)
        elapsed_total = int(time.time() - start_time)
        res_text = (
            f"✅ **اكتملت العملية بنجاح!**\n\n"
            f"{final_bar} تم إرسال {sent_count} من أصل {total} منشور من حساب @{username}.\n"
            f"⏱️ إجمالي الوقت المستغرق: {elapsed_total} ثانية."
        )
        if failed_count > 0:
            res_text += f"\n⚠️ (تعذر إرسال {failed_count} منشور بسبب قيود حقوق أو حذف من إنستجرام)."
        await safe_edit_message(query, res_text, parse_mode="Markdown")

    except Exception as ge:
        logger.error(f"Unexpected error in handle_instagram_profile_posts: {ge}", exc_info=True)
        await safe_edit_message(query, f"🛑 حدث خطأ أثناء معالجة البروفايل: {ge}")
    finally:
        shutil.rmtree(req_dir, ignore_errors=True)


async def handle_instagram_stories(query, context: ContextTypes.DEFAULT_TYPE, username: str):
    chat_id = query.message.chat_id
    loop = asyncio.get_running_loop()
    start_time = time.time()
    await safe_edit_message(query, f"⏳ جاري فحص استوري @{username}...")

    try:
        import instaloader
        L = get_instaloader_instance()
        profile = await loop.run_in_executor(None, lambda: instaloader.Profile.from_username(L.context, username))
    except Exception as e:
        err_str = str(e)
        logger.error(f"Failed to fetch profile for stories {username}: {e}")
        is_auth_barrier = any(w in err_str.lower() for w in ["429", "302", "401", "403", "login", "redirect", "checkpoint", "two-factor", "cookie", "found when accessing"])
        if is_auth_barrier:
            await safe_edit_message(
                query,
                f"🛑 **يتطلب تحميل الاستوري تسجيل الدخول لحسابك:**\n\n"
                f"إنستجرام يشترط وجود جلسة مسجلة لمشاهدة الاستوري.\n"
                f"يرجى إرسال ملف `cookies.txt` هنا في الشات مع البوت كملف 📎 لتفعيل تحميل الاستوري فوراً.",
                parse_mode="Markdown"
            )
        else:
            await safe_edit_message(query, f"🛑 تعذر الوصول للاستوري: {e}")
        return

    def _fetch_stories():
        items = []
        for story in L.get_stories(userids=[profile.userid]):
            for item in story.get_items():
                items.append(item)
        return items

    try:
        story_items = await asyncio.wait_for(loop.run_in_executor(None, _fetch_stories), timeout=45)
    except Exception as e:
        await safe_edit_message(query, f"🛑 تعذر تحميل الاستوري: يتطلب وجود كوكيز حساب إنستجرام صالح في `cookies.txt` ({e}).", parse_mode="Markdown")
        return

    if not story_items:
        await safe_edit_message(query, f"ℹ️ لا توجد استوري نشطة حالياً في حساب @{username}.")
        return

    total = len(story_items)
    req_dir = DOWNLOAD_DIR / f"ig_stories_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
    req_dir.mkdir(parents=True, exist_ok=True)

    try:
        sent_count = 0
        failed_count = 0
        for idx, item in enumerate(story_items, 1):
            elapsed = int(time.time() - start_time)
            bar = make_progress_bar(idx - 1, total, length=10)
            status_text = (
                f"⭕ **جاري تحميل استوري @{username}:**\n"
                f"{bar} ({idx - 1}/{total} تم إرساله)\n\n"
                f"⏳ **جاري الآن معالجة الاستوري {idx} من {total}...**\n"
                f"✅ تم بنجاح: {sent_count} | ⏱️ الوقت: {elapsed}ث"
            )
            await safe_edit_message(query, status_text, parse_mode="Markdown")

            try:
                u = item.video_url if item.is_video else item.url
                if not u:
                    failed_count += 1
                    continue
                fp = await loop.run_in_executor(None, download_image_ytdlp, u, req_dir, idx)
                if fp and os.path.exists(fp):
                    fp = str(fix_media_file_extension(Path(fp)))
                    kind = "video" if item.is_video else "photo"
                    caption = f"⭕ استوري {idx} من {total} | @{username}"
                    v_meta = {fp: await loop.run_in_executor(None, get_video_metadata, str(fp))} if kind == "video" else {}
                    t_cache = {fp: await loop.run_in_executor(None, extract_thumbnail, str(fp))} if kind == "video" else {}
                    s, _ = await send_media_to_target(context.bot, chat_id, [(fp, kind)], caption, v_meta, t_cache, loop)
                    if s > 0:
                        sent_count += 1
                    else:
                        failed_count += 1
                    try:
                        os.remove(fp)
                    except Exception:
                        pass
                else:
                    failed_count += 1
            except Exception as se:
                logger.warning(f"Error sending story {idx} for {username}: {se}")
                failed_count += 1
            await asyncio.sleep(1.0)

        final_bar = make_progress_bar(sent_count, total, length=10)
        elapsed_total = int(time.time() - start_time)
        res_text = (
            f"✅ **اكتمل تحميل الاستوري بنجاح!**\n\n"
            f"{final_bar} تم إرسال {sent_count} من أصل {total} استوري من @{username}.\n"
            f"⏱️ الوقت المستغرق: {elapsed_total}ث"
        )
        if failed_count > 0:
            res_text += f"\n⚠️ (تعذر إرسال {failed_count} استوري)."
        await safe_edit_message(query, res_text, parse_mode="Markdown")
    finally:
        shutil.rmtree(req_dir, ignore_errors=True)


async def handle_instagram_highlights(query, context: ContextTypes.DEFAULT_TYPE, username: str):
    chat_id = query.message.chat_id
    loop = asyncio.get_running_loop()
    start_time = time.time()
    await safe_edit_message(query, f"⏳ جاري فحص هايلايتس @{username}...")

    try:
        import instaloader
        L = get_instaloader_instance()
        profile = await loop.run_in_executor(None, lambda: instaloader.Profile.from_username(L.context, username))
    except Exception as e:
        err_str = str(e)
        logger.error(f"Failed to fetch profile for highlights {username}: {e}")
        is_auth_barrier = any(w in err_str.lower() for w in ["429", "302", "401", "403", "login", "redirect", "checkpoint", "two-factor", "cookie", "found when accessing"])
        if is_auth_barrier:
            await safe_edit_message(
                query,
                f"🛑 **يتطلب تحميل الهايلايتس تسجيل الدخول لحسابك:**\n\n"
                f"إنستجرام يشترط وجود جلسة مسجلة لمشاهدة الهايلايتس.\n"
                f"يرجى إرسال ملف `cookies.txt` هنا في الشات مع البوت كملف 📎 لتفعيل تحميل الهايلايتس فوراً.",
                parse_mode="Markdown"
            )
        else:
            await safe_edit_message(query, f"🛑 تعذر الوصول للهايلايتس: {e}")
        return

    def _fetch_highlights():
        hl_list = []
        for hl in profile.get_highlights():
            for item in hl.get_items():
                hl_list.append((hl.title, item))
                if len(hl_list) >= 30:
                    break
            if len(hl_list) >= 30:
                break
        return hl_list

    try:
        hl_items = await asyncio.wait_for(loop.run_in_executor(None, _fetch_highlights), timeout=45)
    except Exception as e:
        await safe_edit_message(query, f"🛑 تعذر تحميل الهايلايتس: يتطلب وجود كوكيز حساب إنستجرام صالح في `cookies.txt` ({e}).", parse_mode="Markdown")
        return

    if not hl_items:
        await safe_edit_message(query, f"ℹ️ لا توجد هايلايتس متاحة في حساب @{username}.")
        return

    total = len(hl_items)
    req_dir = DOWNLOAD_DIR / f"ig_hl_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
    req_dir.mkdir(parents=True, exist_ok=True)

    try:
        sent_count = 0
        failed_count = 0
        for idx, (hl_title, item) in enumerate(hl_items, 1):
            elapsed = int(time.time() - start_time)
            bar = make_progress_bar(idx - 1, total, length=10)
            status_text = (
                f"⭐ **جاري تحميل هايلايتس @{username}:**\n"
                f"{bar} ({idx - 1}/{total} تم إرساله)\n\n"
                f"⏳ **جاري الآن معالجة: {hl_title} ({idx} من {total})...**\n"
                f"✅ تم بنجاح: {sent_count} | ⏱️ الوقت: {elapsed}ث"
            )
            await safe_edit_message(query, status_text, parse_mode="Markdown")

            try:
                u = item.video_url if item.is_video else item.url
                if not u:
                    failed_count += 1
                    continue
                fp = await loop.run_in_executor(None, download_image_ytdlp, u, req_dir, idx)
                if fp and os.path.exists(fp):
                    fp = str(fix_media_file_extension(Path(fp)))
                    kind = "video" if item.is_video else "photo"
                    caption = f"⭐ هايلايت: {hl_title} ({idx}/{total}) | @{username}"
                    v_meta = {fp: await loop.run_in_executor(None, get_video_metadata, str(fp))} if kind == "video" else {}
                    t_cache = {fp: await loop.run_in_executor(None, extract_thumbnail, str(fp))} if kind == "video" else {}
                    s, _ = await send_media_to_target(context.bot, chat_id, [(fp, kind)], caption, v_meta, t_cache, loop)
                    if s > 0:
                        sent_count += 1
                    else:
                        failed_count += 1
                    try:
                        os.remove(fp)
                    except Exception:
                        pass
                else:
                    failed_count += 1
            except Exception as he:
                logger.warning(f"Error sending highlight item {idx} for {username}: {he}")
                failed_count += 1
            await asyncio.sleep(1.0)

        final_bar = make_progress_bar(sent_count, total, length=10)
        elapsed_total = int(time.time() - start_time)
        res_text = (
            f"✅ **اكتمل تحميل الهايلايتس بنجاح!**\n\n"
            f"{final_bar} تم إرسال {sent_count} من أصل {total} عنصر من هايلايتس @{username}.\n"
            f"⏱️ الوقت المستغرق: {elapsed_total}ث"
        )
        if failed_count > 0:
            res_text += f"\n⚠️ (تعذر إرسال {failed_count} عنصر)."
        await safe_edit_message(query, res_text, parse_mode="Markdown")
    finally:
        shutil.rmtree(req_dir, ignore_errors=True)


def extract_media(url: str, download_path: Path, chat_id: int, status_msg_id: int, app: Application, progress: dict = None) -> tuple[dict, list]:
    if status_msg_id in cancelled_downloads:
        logger.info(f"Download cancelled by user before extract: {url}")
        return {}, []

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
        or "vt.tiktok.com" in url_lower
    )
    is_x = "twitter.com" in url_lower or "x.com" in url_lower
    is_tiktok = (
        "tiktok.com" in url_lower
        or "vm.tiktok.com" in url_lower
        or "vt.tiktok.com" in url_lower
    )
    is_instagram = "instagram.com" in url_lower

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

    # For TikTok, first try direct API extraction for watermark-free videos and photo albums
    if is_tiktok and not all_image_urls:
        tt_info, tt_urls = get_tiktok_media(url)
        if tt_urls:
            logger.info(f"TikTok direct extraction: {len(tt_urls)} media item(s) found")
            if not info and tt_info:
                info = tt_info
            for u in tt_urls:
                if u not in all_image_urls:
                    all_image_urls.append(u)

    # For Instagram, first try direct Instaloader extraction for photos, carousels, and reels
    if is_instagram and not all_image_urls:
        ig_info, ig_urls = get_instagram_media(url)
        if ig_urls:
            logger.info(f"Instagram direct extraction: {len(ig_urls)} media item(s) found")
            if not info and ig_info:
                info = ig_info
            for u in ig_urls:
                if u not in all_image_urls:
                    all_image_urls.append(u)

    if status_msg_id in cancelled_downloads:
        return {}, []

    # If not X, or if direct X extraction found nothing, run yt-dlp
    if not all_image_urls:
        if have_cookies_file:
            logger.info(f"Attempting download with cookies (is_x={is_x})...")
            try:
                last_stderr = run_ytdlp_download(url, download_path, is_carousel, is_x, use_cookies=True, progress=progress, timeout=90)
            except Exception as e:
                logger.warning(f"Download with cookies failed: {e}")
                last_stderr = str(e)

            if status_msg_id in cancelled_downloads:
                return {}, []

            # Check if any media was downloaded
            after_files = set(f.name for f in download_path.iterdir() if f.is_file() and not f.name.endswith(".info.json"))
            if not (after_files - before_files):
                logger.info("No media found using cookies. Retrying WITHOUT cookies as fallback...")
                try:
                    last_stderr = run_ytdlp_download(url, download_path, is_carousel, is_x, use_cookies=False, progress=progress, timeout=90)
                except Exception as e:
                    logger.warning(f"Fallback download without cookies failed: {e}")
                    last_stderr = str(e)
        else:
            logger.info(f"Attempting download without cookies (is_x={is_x})...")
            try:
                last_stderr = run_ytdlp_download(url, download_path, is_carousel, is_x, use_cookies=False, progress=progress, timeout=90)
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

        if is_tiktok:
            after_files = set(f.name for f in download_path.iterdir() if f.is_file() and not f.name.endswith(".info.json"))
            if not (after_files - before_files) and not all_image_urls:
                tt_info, tt_urls = get_tiktok_media(url)
                if tt_urls:
                    if not info and tt_info:
                        info = tt_info
                    for u in tt_urls:
                        if u not in all_image_urls:
                            all_image_urls.append(u)

        if is_instagram:
            after_files = set(f.name for f in download_path.iterdir() if f.is_file() and not f.name.endswith(".info.json"))
            if not (after_files - before_files) and not all_image_urls:
                ig_info, ig_urls = get_instagram_media(url)
                if ig_urls:
                    if not info and ig_info:
                        info = ig_info
                    for u in ig_urls:
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

    is_tiktok = (
        "tiktok.com" in url_lower
        or "vm.tiktok.com" in url_lower
        or "vt.tiktok.com" in url_lower
    )

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
        if status_msg_id in cancelled_downloads:
            logger.info(f"Image download loop cancelled by user for msg_id={status_msg_id}")
            break
        def _dl_progress(downloaded, total, _idx=image_idx, _total=total_urls):
            pct = int(downloaded * 100 / total) if total > 0 else 0
            mb_done = downloaded / (1024 * 1024)
            mb_total = total / (1024 * 1024)
            bar = make_progress_bar(downloaded, total, length=10)
            if progress is not None:
                progress["msg"] = f"⬇️ **تحميل ملف {_idx+1}/{_total}:**\n{bar} ({mb_done:.1f}/{mb_total:.1f}MB)"
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
    downloaded_files = [str(fix_media_file_extension(Path(fp))) for fp in downloaded_files if os.path.exists(fp)]

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
    arch_ch = get_archive_channel()
    arch_status = f"قناة الأرشيف: <code>{arch_ch}</code> ✅" if arch_ch else "قناة الأرشيف: غير مربوطة (حول أي رسالة من قناتك لربطها تلقائياً)"
    await update.message.reply_text(
        "Send me a link and I will download it.\n\n"
        "Supported:\n"
        "- X/Twitter videos and images (Photo & Video Albums 📸🎬)\n"
        "- Instagram posts, reels, stories\n"
        "- Facebook videos and reels\n"
        "- TikTok videos and photo albums (Photo & Video Albums 📸🎬)\n\n"
        f"🤖 الإصدار النشط: {BOT_VERSION}\n"
        f"📁 {arch_status}",
        parse_mode="HTML",
        reply_markup=start_keyboard,
    )


async def archive_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat and chat.type in ("channel", "supergroup"):
        set_archive_channel(chat.id, chat.title or "Archive Channel")
        await context.bot.send_message(
            chat_id=chat.id,
            text=f"✅ <b>تم ضبط هذه القناة كقناة للأرشيف بنجاح!</b>\n📌 <b>{chat.title}</b> (<code>{chat.id}</code>)",
            parse_mode="HTML"
        )
        return

    if not update.effective_user or not is_authorized(update.effective_user.id):
        return
    current_ch = get_archive_channel()
    if context.args:
        raw_id = context.args[0].strip()
        try:
            ch_id = int(raw_id)
            set_archive_channel(ch_id, f"Channel {ch_id}")
            await update.message.reply_text(f"✅ تم ضبط قناة الأرشيف بنجاح: <code>{ch_id}</code>", parse_mode="HTML")
        except ValueError:
            await update.message.reply_text("يرجى إرسال معرف صالح للقناة (مثال: -1001234567890)")
        return
    if current_ch:
        await update.message.reply_text(
            f"📁 <b>قناة الأرشيف الحالية:</b> <code>{current_ch}</code> ✅\n\n"
            "لربط قناة جديدة:\n"
            "1. ضيف البوت كـ مشرف (Admin) في قناتك مع صلاحية إرسال الرسائل.\n"
            "2. حول (Forward) أي رسالة من القناة إلى هنا مباشرة وسيرتبط تلقائياً!",
            parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            "📁 <b>لم يتم ربط قناة أرشيف بعد!</b>\n\n"
            "<b>طريقة الربط في ثواني:</b>\n"
            "1. ضيف البوت كـ مشرف (Admin) في قناتك مع صلاحية إرسال الرسائل.\n"
            "2. أعد توجيه (Forward) أي رسالة من القناة إلى هنا وسيقوم البوت بالربط فوراً!\n"
            "أو اكتب: <code>/channel -100xxxxxxxxxx</code> مع وضع ID القناة.",
            parse_mode="HTML"
        )


async def on_channel_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.channel_post and update.channel_post.chat:
        chat = update.channel_post.chat
        if chat.type in ("channel", "supergroup"):
            set_archive_channel(chat.id, chat.title or "Archive Channel")
            logger.info(f"Auto-bound archive channel via channel_post: {chat.title} ({chat.id})")


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.my_chat_member:
        return
    chat = update.my_chat_member.chat
    new_member = update.my_chat_member.new_chat_member
    if chat.type in ("channel", "supergroup") and new_member.status in ("administrator", "creator"):
        set_archive_channel(chat.id, chat.title or "Archive Channel")
        logger.info(f"Bot added as admin to channel: {chat.title} ({chat.id})")
        if AUTHORIZED_USER_ID:
            try:
                await context.bot.send_message(
                    chat_id=AUTHORIZED_USER_ID,
                    text=f"✅ <b>تم ربط قناة الأرشيف بنجاح!</b>\n\n📌 اسم القناة: <b>{chat.title}</b>\n🆔 المعرّف: <code>{chat.id}</code>\n\nالآن كل وسائط جديدة سيتم إرسال نسخة منها تلقائياً لقناتك مع منع التكرار! 📁",
                    parse_mode="HTML"
                )
            except Exception as e:
                logger.error(f"Failed to notify user about channel link: {e}")


async def cancel_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "cancel_download":
        msg_id = query.message.message_id
        cancelled_downloads.add(msg_id)
        await query.edit_message_text("تم إلغاء هذا التحميل.")
    elif query.data.startswith("force_dl_"):
        post_id = query.data.replace("force_dl_", "")
        archived = is_post_archived(post_id)
        if archived and archived.get("source_url"):
            url = archived["source_url"]
            await query.edit_message_text(f"⏳ جاري إعادة التحميل والإرسال بناءً على طلبك: {url}...")
            # Trigger download bypassing de-duplication check
            context.application.create_task(
                handle_url(update, context, force_url=url)
            )
        else:
            await query.edit_message_text("تعذر العثور على رابط المنشور لإعادة تحميله.")
    elif query.data.startswith("ig_posts_"):
        username = query.data.replace("ig_posts_", "")
        context.application.create_task(handle_instagram_profile_posts(query, context, username, limit=30))
    elif query.data.startswith("ig_stories_"):
        username = query.data.replace("ig_stories_", "")
        context.application.create_task(handle_instagram_stories(query, context, username))
    elif query.data.startswith("ig_hl_"):
        username = query.data.replace("ig_hl_", "")
        context.application.create_task(handle_instagram_highlights(query, context, username))


async def cookies_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Bot is for personal use only.")
        return
    await update.message.reply_text(
        "🍪 **إعداد ملف الكوكيز (cookies.txt):**\n\n"
        "أرسل ملف `cookies.txt` هنا مباشرة كملف عادي (📎 Document).\n\n"
        "💡 **لتصدير كوكيز إنستجرام بشكل صحيح:**\n"
        "1. افتح موقع Instagram.com على متصفحك وسجل الدخول لحسابك.\n"
        "2. استخدم إضافة مثل *Get cookies.txt LOCALLY* واضغط **Export**.\n"
        "3. أرسل الملف الناتج هنا، وسيقوم البوت بدمجه وتفعيله فوراً!",
        parse_mode="Markdown"
    )


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Any file sent to the bot is treated as cookies upload."""
    global COOKIES_FILE
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Bot is for personal use only.")
        return
    if not update.message.document:
        return

    file_id = update.message.document.file_id
    file_name = update.message.document.file_name or "cookies.txt"
    cookies_path = Path(COOKIES_FILE if COOKIES_FILE else "cookies.txt")

    try:
        file = await context.bot.get_file(file_id)
        temp_dl = Path(f"temp_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}_{file_name}")
        if hasattr(file, "download_to_drive"):
            await file.download_to_drive(str(temp_dl))
        else:
            await file.download(str(temp_dl))

        new_content = temp_dl.read_text(encoding="utf-8", errors="ignore")
        old_content = cookies_path.read_text(encoding="utf-8", errors="ignore") if cookies_path.exists() else ""

        merged_content = merge_cookie_texts(old_content, new_content)
        cookies_path.write_text(merged_content, encoding="utf-8")
        COOKIES_FILE = str(cookies_path)
        save_setting("cookies_txt", merged_content)
        try:
            temp_dl.unlink(missing_ok=True)
        except Exception:
            pass

        # Inspect what cookies were uploaded
        has_instagram = "instagram.com" in new_content
        has_ig_session = "sessionid" in new_content
        has_twitter = "twitter.com" in new_content or "x.com" in new_content
        has_tw_auth = "auth_token" in new_content

        msg_parts = ["✅ **تم استلام وتحديث ملف الكوكيز بنجاح!**\n"]

        if has_instagram:
            if has_ig_session:
                msg_parts.append("📸 **إنستجرام:** تم التعرف على جلسة تسجيل الدخول (`sessionid` موجود بنجاح) ✅\nيمكنك الآن تحميل أي بروفايل أو استوري أو هايلايتس فوراً!")
            else:
                msg_parts.append(
                    "⚠️ **إنستجرام: تنبيه هام جداً!**\n"
                    "الملف يحتوي على كوكيز إنستجرام ولكن **غير مسجل الدخول (`sessionid` مفقود)**!\n"
                    f"حجم الملف ({len(new_content)} بايت) صغير جداً، وهذا يعني أنك قمت بتصدير الكوكيز قبل تسجيل الدخول.\n\n"
                    "👉 **الحل في خطوتين:**\n"
                    "1. افتح موقع Instagram.com على متصفحك وسجل الدخول بحسابك وتأكد أنك داخل صفحتك الرئيسية.\n"
                    "2. اضغط على إضافة الكوكيز واعمل **Export** مجدداً، ثم أرسل الملف الجديد للبوت.\n\n"
                    "💡 *أو يمكنك بدلاً من ذلك نسخ الـ `sessionid` وكتابة الأمر:* `/session <كود>`"
                )

        if has_twitter:
            if has_tw_auth:
                msg_parts.append("🐦 **تويتر/X:** تم تفعيل كوكيز تسجيل الدخول (`auth_token` موجود) ✅")
            else:
                msg_parts.append("🐦 **تويتر/X:** كوكيز عامة بدون تسجيل دخول.")

        if not has_instagram and not has_twitter:
            msg_parts.append("📁 تم حفظ ملف الكوكيز وتفعيله للأدوات التلقائية.")

        await update.message.reply_text("\n\n".join(msg_parts), parse_mode="Markdown")
        logger.info(f"Cookies updated: ig={has_instagram}(session={has_ig_session}), tw={has_twitter}")

    except Exception as e:
        logger.error(f"Failed to process uploaded cookies: {e}")
        await update.message.reply_text(f"🛑 تعذر حفظ ملف الكوكيز: {e}")


async def session_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow direct submission of Instagram sessionid value without uploading a file."""
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Bot is for personal use only.")
        return

    text = update.message.text.strip()
    parts = text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        await update.message.reply_text(
            "🔑 **إدخال sessionid إنستجرام مباشرة:**\n\n"
            "طريقة الاستخدام:\n"
            "اكتب الأمر ومعه كود الـ sessionid، مثال:\n"
            "`/session 1094818157%3Af9gK...`\n\n"
            "💡 **كيف تجد sessionid في ثوانٍ من المتصفح؟**\n"
            "1. افتح Instagram.com وتأكد أنك داخل حسابك.\n"
            "2. اضغط F12 (فحص العنصر) ثم اختر **Application** (أو Storage في فايرفوكس).\n"
            "3. اضغط على **Cookies** ثم `https://www.instagram.com`.\n"
            "4. اضغط مرتين على قيمة **sessionid** وانسخها والصقها هنا بعد الأمر `/session`!\n\n"
            "*(أو يمكنك إرسال ملف `cookies.txt` كامل كملف عادي 📎)*",
            parse_mode="Markdown"
        )
        return

    sess_val = parts[1].strip().strip('"').strip("'")
    if "sessionid=" in sess_val:
        sess_val = sess_val.split("sessionid=")[-1].split(";")[0].strip()

    cookies_path = Path(COOKIES_FILE if COOKIES_FILE else "cookies.txt")
    old_content = cookies_path.read_text(encoding="utf-8", errors="ignore") if cookies_path.exists() else ""

    new_line = f".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t{sess_val}\n"
    merged = merge_cookie_texts(old_content, new_line)
    cookies_path.write_text(merged, encoding="utf-8")
    save_setting("cookies_txt", merged)

    await update.message.reply_text(
        "✅ **تم تفعيل وحفظ جلسة إنستجرام بنجاح!** 🎉\n\n"
        "الآن يمكنك إرسال أي رابط بروفايل أو استوري لتحميله فوراً بدون أي قيود!",
        parse_mode="Markdown"
    )
    logger.info("Instagram sessionid updated via /session command and saved to DB")


async def send_media_to_target(
    bot,
    target_chat_id: int | str,
    items: list[tuple[str, str]],
    caption: str,
    video_meta_cache: dict,
    thumb_cache: dict,
    loop
) -> tuple[int, bool]:
    """Sends a list of (path, kind) items to target_chat_id (user or channel).
    Returns (successful_sends_count, sent_as_group)."""
    if not items:
        return 0, False

    successful_sends = 0
    sent_group = False

    # Attempt album grouping if > 1 items
    if len(items) > 1:
        chunk_size = 10
        all_chunks_sent = True
        for i in range(0, len(items), chunk_size):
            chunk = items[i:i + chunk_size]
            chunk_caption = caption if i == 0 else ""
            if len(chunk) == 1:
                chunk_item = chunk[0]
                path = chunk_item[0]
                kind = chunk_item[1]
                chunk_caption = chunk_item[2] if len(chunk_item) > 2 else (caption if i == 0 else "")
                try:
                    if kind == "photo":
                        await _send_photo_isolated(target_chat_id, path, chunk_caption, timeout=120)
                    else:
                        meta = video_meta_cache.get(path)
                        thumb = thumb_cache.get(path)
                        f_mb = os.path.getsize(path) / (1024 * 1024) if os.path.exists(path) else 0
                        v_timeout = max(300, int((f_mb * 2) + 120))
                        await _send_video_isolated(target_chat_id, path, chunk_caption, meta=meta, thumb_path=thumb, timeout=v_timeout)
                    successful_sends += 1
                except Exception as e:
                    logger.warning(f"Single item send failed to {target_chat_id}: {e}")
                    all_chunks_sent = False
            else:
                try:
                    group_ok = await send_media_group_isolated(
                        bot, target_chat_id, chunk, chunk_caption,
                        thumb_map=thumb_cache, meta_map=video_meta_cache
                    )
                    if group_ok:
                        successful_sends += len(chunk)
                    else:
                        all_chunks_sent = False
                        break
                except Exception as e:
                    logger.warning(f"Media group send failed to {target_chat_id}: {e}")
                    all_chunks_sent = False
                    break
        if all_chunks_sent and successful_sends > 0:
            sent_group = True

    # Fallback: send one by one if not sent as group
    if not sent_group:
        for idx, item_data in enumerate(items):
            path = item_data[0]
            kind = item_data[1]
            if not os.path.exists(path):
                continue
            item_caption = item_data[2] if len(item_data) > 2 else (caption if idx == 0 else "")
            f_mb = os.path.getsize(path) / (1024 * 1024) if os.path.exists(path) else 0
            v_timeout = max(300, int((f_mb * 2) + 120))
            for attempt in range(2):
                try:
                    if kind == "photo":
                        await _send_photo_isolated(target_chat_id, path, item_caption, timeout=120)
                    else:
                        if path not in video_meta_cache:
                            video_meta_cache[path] = await loop.run_in_executor(None, get_video_metadata, str(path))
                        meta = video_meta_cache[path]
                        if path not in thumb_cache:
                            thumb_cache[path] = await loop.run_in_executor(None, extract_thumbnail, str(path))
                        await _send_video_isolated(target_chat_id, path, item_caption, meta=meta, thumb_path=thumb_cache.get(path), timeout=v_timeout)
                    successful_sends += 1
                    break
                except Exception as e:
                    logger.error(f"Send failed to {target_chat_id} for {path} (attempt {attempt+1}): {e}")
                    if attempt == 0:
                        await asyncio.sleep(2)

    return successful_sends, sent_group


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE, force_url: str = None):
    user = update.effective_user
    chat = update.effective_chat
    user_id = user.id if user else 0
    chat_id = chat.id if chat else 0

    if not is_authorized(user_id):
        if update.message:
            await update.message.reply_text("Bot is for personal use only.")
        elif chat_id:
            await context.bot.send_message(chat_id=chat_id, text="Bot is for personal use only.")
        return

    # Check for forwarded message from a channel to bind it automatically
    if update.message:
        fwd_chat = None
        if hasattr(update.message, "forward_origin") and update.message.forward_origin:
            origin = update.message.forward_origin
            if getattr(origin, "type", None) == "channel" and hasattr(origin, "chat"):
                fwd_chat = origin.chat
        if not fwd_chat and getattr(update.message, "forward_from_chat", None):
            fwd_chat = update.message.forward_from_chat

        if fwd_chat and getattr(fwd_chat, "type", "") in ("channel", "supergroup"):
            ch_id = fwd_chat.id
            ch_title = getattr(fwd_chat, "title", "Archive Channel")
            set_archive_channel(ch_id, ch_title)
            await update.message.reply_text(
                f"✅ <b>تم ربط قناة الأرشيف بنجاح!</b>\n\n"
                f"📌 اسم القناة: <b>{ch_title}</b>\n"
                f"🆔 المعرّف: <code>{ch_id}</code>\n\n"
                f"من الآن فصاعداً:\n"
                f"1. أي وسائط جديدة هتحملها، هيروح منها نسخة تلقائياً لقناتك 📁✨\n"
                f"2. لو المنشور اتحمل قبل كدا، البوت هينبهك فوراً ومش هيكرره أبداً لمنع التكرار 🛡️",
                parse_mode="HTML"
            )
            return

    # Check if user sent a Telegram invite link directly
    if update.message and update.message.text:
        raw_text = update.message.text.strip()
        if re.search(r"t\.me/(?:\+|joinchat/)", raw_text):
            await update.message.reply_text(
                "ℹ️ <b>تنبيه بخصوص ربط القناة:</b>\n\n"
                "بوتات التيليجرام لا تستطيع الدخول عبر روابط الدعوة (Invite Links) بنفسها.\n\n"
                "<b>طريقة الربط في ثواني:</b>\n"
                "1️⃣ افتح قناتك واضغط على إعدادات القناة (⚙️) ➡️ المشرفين (Administrators).\n"
                "2️⃣ اضغط <b>إضافة مشرف (Add Admin)</b> وابحث عن البوت: <code>@mohamed_x_downloader_bot</code>\n"
                "3️⃣ امنحه صلاحية <b>نشر الرسائل (Post Messages)</b> ثم احفظ.\n"
                "4️⃣ بعد إضافته، <b>أعد توجيه (Forward) أي رسالة</b> من القناة إلى هنا، وسيتم الربط فوراً وتلقائياً! 🚀",
                parse_mode="HTML"
            )
            return

    if force_url:
        url = force_url
    else:
        if not update.message or not update.message.text:
            return
        text = update.message.text.strip()
        if text.lower() in ("start", "/start"):
            await start(update, context)
            return

        if text.startswith("sessionid=") or re.match(r"^\d{6,15}%3[aA][A-Za-z0-9_\-\:]{20,}", text):
            sess_val = text.split("sessionid=")[-1].split(";")[0].strip().strip('"').strip("'")
            cookies_path = Path(COOKIES_FILE if COOKIES_FILE else "cookies.txt")
            old_content = cookies_path.read_text(encoding="utf-8", errors="ignore") if cookies_path.exists() else ""
            new_line = f".instagram.com\tTRUE\t/\tTRUE\t2147483647\tsessionid\t{sess_val}\n"
            merged = merge_cookie_texts(old_content, new_line)
            cookies_path.write_text(merged, encoding="utf-8")
            save_setting("cookies_txt", merged)
            await update.message.reply_text(
                "✅ **تم التعرف على كود sessionid وحفظه وتفعيله بنجاح!** 🎉\n\n"
                "الآن جرب إرسال رابط أي بروفايل أو استوري لتحميله فوراً!",
                parse_mode="Markdown"
            )
            return

        url = extract_url(text)
        if not url:
            await update.message.reply_text(
                "Please send a valid link.\n\n"
                "Supported:\n"
                "- X/Twitter: https://x.com/user/status/123\n"
                "- Instagram: https://instagram.com/p/ABC/\n"
                "- Facebook: https://facebook.com/user/videos/123\n"
                "- TikTok: https://vm.tiktok.com/ABC/"
            )
            return

    # Check if the URL is an Instagram profile link
    ig_profile_user = is_instagram_profile_url(url)
    if ig_profile_user:
        await send_instagram_profile_menu(update, context, ig_profile_user)
        return

    # Canonical de-duplication check
    canonical_id, platform_name = get_canonical_post_id(url)
    if not force_url:
        archived_item = is_post_archived(canonical_id)
        if archived_item:
            arch_date = archived_item.get("created_at", "سابقاً")
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 تحميل وإرسال لي مجدداً", callback_data=f"force_dl_{canonical_id}")]
            ])
            text_warn = (
                f"⚠️ <b>هذا المنشور تم تحميله وأرشفته بالفعل!</b>\n\n"
                f"🌐 <b>المنصة:</b> {platform_name}\n"
                f"🆔 <b>المعرّف:</b> <code>{canonical_id}</code>\n"
                f"📅 <b>تاريخ الأرشفة:</b> {arch_date}\n\n"
                f"🛡️ <i>تم منع تكرار الإرسال إلى القناة للحفاظ على نظافة الأرشيف.</i>\n"
                f"إذا كنت تريد إرسال نسخة لك هنا في المحادثة، اضغط على الزر بالأسفل:"
            )
            if update.message:
                await update.message.reply_text(text_warn, parse_mode="HTML", reply_markup=kb)
            else:
                await context.bot.send_message(chat_id=chat_id, text=text_warn, parse_mode="HTML", reply_markup=kb)
            return

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

    if update.message:
        status_msg = await update.message.reply_text(status_text, reply_markup=cancel_keyboard)
    else:
        status_msg = await context.bot.send_message(chat_id=chat_id, text=status_text, reply_markup=cancel_keyboard)
    status_id = status_msg.message_id

    req_dir = DOWNLOAD_DIR / f"req_{int(time.time()*1000)}_{uuid.uuid4().hex[:6]}"
    req_dir.mkdir(parents=True, exist_ok=True)

    loop = asyncio.get_running_loop()
    _edit_lock = asyncio.Lock()
    file_paths = None
    info = None

    async def safe_edit(msg, text):
        async with _edit_lock:
            return await safe_edit_message(msg, text, parse_mode="Markdown")

    async def notify_stopped(reason: str):
        full_msg = f"🛑 توقف التحميل:\n{reason}"
        edited = await safe_edit(status_msg, full_msg)
        if not edited:
            try:
                if update.message:
                    await update.message.reply_text(full_msg)
                else:
                    await context.bot.send_message(chat_id=chat_id, text=full_msg)
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
                    await asyncio.sleep(2.5)
                    elapsed = int(time.time() - start_t)
                    cur = progress.get("msg", "")
                    display = f"{cur}\n⏱️ الوقت: {elapsed}ث" if cur else f"⏳ جاري فحص الرابط وبدء التحميل... ({elapsed}ث)"
                    if display != last:
                        last = display
                        await safe_edit(status_msg, display)

            poll_task = asyncio.ensure_future(_poll_progress())
            try:
                info, file_paths = await asyncio.wait_for(
                    loop.run_in_executor(
                        None, extract_media, url, req_dir, chat_id, status_id, context.application, progress
                    ),
                    timeout=210,
                )
            except asyncio.TimeoutError:
                await notify_stopped("استغرق التحميل وقتاً أطول من المتوقع (تجاوزت المهلة 3.5 دقيقة) وتوقف لتجنب تعليق البوت.")
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
            await safe_edit(status_msg, f"⬆️ تجهيز {len(file_paths)} ملف للرفع...")

            file_paths = [str(fix_media_file_extension(Path(fp))) for fp in file_paths if os.path.exists(fp)]
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

            for video_path in videos:
                if not os.path.exists(video_path):
                    continue
                file_mb = os.path.getsize(video_path) / (1024 * 1024)

                if use_local:
                    if file_mb <= 2000:
                        await loop.run_in_executor(None, ensure_faststart, video_path)
                        prepared_items.append((video_path, "video"))
                    else:
                        await safe_edit(status_msg, f"✂️ حجم الفيديو ({file_mb:.1f}MB) يتجاوز 2GB — جاري تقسيمه بجودته الأصلية...")
                        parts = await loop.run_in_executor(None, split_video, video_path, req_dir, 1950.0)
                        if parts:
                            if len(parts) > 1:
                                for p_idx, p in enumerate(parts, 1):
                                    p_cap = f"{caption}\n\n📦 جزء {p_idx} من {len(parts)}" if caption else f"📦 جزء {p_idx} من {len(parts)}"
                                    prepared_items.append((p, "video", p_cap))
                            else:
                                prepared_items.append((parts[0], "video"))
                        else:
                            await notify_stopped(f"تعذر تقسيم الفيديو الكبير ({file_mb:.1f}MB).")
                            return
                else:
                    if file_mb <= 50:
                        prepared_items.append((video_path, "video"))
                    else:
                        duration = get_video_duration(video_path)
                        can_compress = duration > 0 and duration < 600 and ((49 * 1024 * 1024 * 8) / duration) > 400_000

                        compressed_ok = False
                        if can_compress:
                            await safe_edit(status_msg, f"⏳ حجم الفيديو ({file_mb:.1f}MB) — جاري ضغطه ليناسب إرسال تيليجرام (أقل من 50MB)...")
                            cpath = str(video_path) + ".compressed.mp4"
                            try:
                                compressed = await loop.run_in_executor(None, compress_video, video_path, cpath, 49.0)
                                if compressed and os.path.exists(cpath) and os.path.getsize(cpath) <= 50 * 1024 * 1024:
                                    prepared_items.append((cpath, "video"))
                                    compressed_ok = True
                            except Exception as ce:
                                logger.info(f"Compression skipped or failed ({ce}), will split losslessly...")

                        if not compressed_ok:
                            await safe_edit(status_msg, f"✂️ حجم الفيديو ({file_mb:.1f}MB) — جاري تقسيمه لأجزاء بجودته الأصلية 100% بدون أي ضغط...")
                            parts = await loop.run_in_executor(None, split_video, video_path, req_dir, 47.0)
                            if parts:
                                if len(parts) > 1:
                                    for p_idx, p in enumerate(parts, 1):
                                        p_cap = f"{caption}\n\n📦 جزء {p_idx} من {len(parts)}" if caption else f"📦 جزء {p_idx} من {len(parts)}"
                                        prepared_items.append((p, "video", p_cap))
                                else:
                                    prepared_items.append((parts[0], "video"))
                            else:
                                await notify_stopped(f"تعذر تجهيز الفيديو ({file_mb:.1f}MB) للإرسال.")
                                return

            seen_paths = set()
            unique_items = []
            for item_data in prepared_items:
                p = item_data[0]
                if p not in seen_paths and os.path.exists(p):
                    seen_paths.add(p)
                    unique_items.append(item_data)

            if not unique_items:
                await notify_stopped("لم يتم العثور على أي ملفات صالحة للإرسال.")
                return

            video_meta_cache = {}
            thumb_cache = {}
            for item_data in unique_items:
                p = item_data[0]
                k = item_data[1]
                if k == "video":
                    if p not in video_meta_cache:
                        video_meta_cache[p] = await loop.run_in_executor(None, get_video_metadata, str(p))
                    if p not in thumb_cache:
                        thumb_cache[p] = await loop.run_in_executor(None, extract_thumbnail, str(p))

            await safe_edit(status_msg, f"⬆️ جاري الرفع إلى تيليجرام ({len(unique_items)} ملف)... (0ث)")
            upload_start_t = time.time()
            uploading = True

            async def _poll_upload():
                while uploading:
                    await asyncio.sleep(2.5)
                    if not uploading:
                        break
                    elapsed = int(time.time() - upload_start_t)
                    await safe_edit(status_msg, f"⬆️ جاري الرفع إلى تيليجرام ({len(unique_items)} ملف)... ({elapsed}ث)")

            upload_task = asyncio.ensure_future(_poll_upload())
            try:
                successful_sends, sent_group = await send_media_to_target(
                    context.bot, chat_id, unique_items, caption,
                    video_meta_cache, thumb_cache, loop
                )
            finally:
                uploading = False
                upload_task.cancel()
                try:
                    await upload_task
                except asyncio.CancelledError:
                    pass

            if successful_sends > 0:
                try:
                    await status_msg.delete()
                except Exception:
                    pass
                if successful_sends < len(unique_items):
                    if update.message:
                        await update.message.reply_text(f"⚠️ تم إرسال {successful_sends} من أصل {len(unique_items)} ملفات (تعذر إرسال الباقي).")

                # If this was not a forced re-download, save to archive and forward to channel!
                if not force_url:
                    save_archive_item(canonical_id, platform_name, url, caption=caption)
                    arch_ch = get_archive_channel()
                    if arch_ch:
                        ch_caption = f"🔗 {url}\n\n"
                        if caption:
                            ch_caption += f"{caption[:350]}\n\n"
                        ch_caption += f"#{platform_name.replace(' ', '_')}"
                        try:
                            ch_sends, _ = await send_media_to_target(
                                context.bot, arch_ch, unique_items, ch_caption,
                                video_meta_cache, thumb_cache, loop
                            )
                            logger.info(f"Archived {ch_sends} media item(s) to channel {arch_ch}")
                        except Exception as ce:
                            logger.error(f"Failed to forward to archive channel {arch_ch}: {ce}")
            else:
                await notify_stopped("تعذر إرسال الملفات إلى تيليجرام.")

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

    init_db()

    from telegram.request import HTTPXRequest

    http_request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=15.0,
        read_timeout=60.0,
        write_timeout=60.0,
    )
    builder = Application.builder().token(BOT_TOKEN).request(http_request)

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
            logger.info(f"Using LOCAL Bot API Server at {local_url} (2GB upload limit)")
        else:
            os.environ["USE_LOCAL_SERVER"] = "false"
            logger.info("Using Telegram Cloud API (50MB limit) + ffmpeg compression")
    else:
        logger.info("Using Telegram Cloud API (50MB limit) + ffmpeg compression")

    application = builder.build()

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        global _consecutive_connect_errors
        err = context.error
        err_str = str(err)

        is_conn_error = (
            isinstance(err, (TelegramError, Exception))
            and (
                "ConnectError" in err_str
                or "All connection attempts failed" in err_str
                or "ConnectTimeout" in err_str
                or "NetworkError" in err_str
                or "Network is unreachable" in err_str
                or "Connection refused" in err_str
                or "timed out" in err_str
            )
        )

        if is_conn_error:
            _consecutive_connect_errors += 1
            current_base_url = getattr(context.bot, "_base_url", "")
            logger.error(
                f"Telegram connection error #{_consecutive_connect_errors}: {err} (target: {current_base_url})"
            )

            # If using local server and it fails 2 times consecutively: fall back immediately to Telegram Cloud API!
            if "127.0.0.1" in current_base_url or "localhost" in current_base_url:
                if _consecutive_connect_errors >= 2:
                    logger.warning(
                        f"Local Bot API server at {current_base_url} is unreachable ({_consecutive_connect_errors} failures). "
                        "Switching immediately to official Telegram Cloud API (api.telegram.org)!"
                    )
                    context.bot._base_url = f"https://api.telegram.org/bot{BOT_TOKEN}"
                    context.bot._base_file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}"
                    os.environ["USE_LOCAL_SERVER"] = "false"
                    _consecutive_connect_errors = 0
                    return

            # If on Cloud API and we have 6 consecutive connection errors (~2 minutes):
            if _consecutive_connect_errors >= 6:
                logger.critical(
                    f"Unrecoverable connection failure to Telegram ({_consecutive_connect_errors} consecutive errors). "
                    "Restarting container process for clean network reconnect..."
                )
                os._exit(1)
        else:
            _consecutive_connect_errors = 0
            logger.error(f"Telegram error occurred: {err}")

    application.add_error_handler(error_handler)

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CommandHandler("cookies", cookies_command))
    application.add_handler(CommandHandler("session", session_command))
    application.add_handler(CommandHandler("sessionid", session_command))
    application.add_handler(CommandHandler("channel", archive_command))
    application.add_handler(CommandHandler("archive", archive_command))
    application.add_handler(CommandHandler("set_archive", archive_command))
    application.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.ChatType.CHANNEL, on_channel_post))
    application.add_handler(CallbackQueryHandler(cancel_button))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.FORWARDED, handle_url))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    start_health_check_server()
    logger.info("Bot started")

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
