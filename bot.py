import os
import asyncio
import logging
import re
import subprocess
import json
import hashlib
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
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

def get_cookie_args() -> list:
    if COOKIES_FILE:
        return ["--cookie-file", COOKIES_FILE]
    return []

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

cancelled_downloads = {}


def is_authorized(user_id: int) -> bool:
    if AUTHORIZED_USER_ID == 0:
        return True
    return user_id == AUTHORIZED_USER_ID


def is_valid_url(url: str) -> bool:
    url = url.strip()
    patterns = [
        r"(https?://)?(www\.)?(x\.com|twitter\.com)/\S+/status/\d+",
        r"(https?://)?(www\.)?(instagram\.com)/(p|reel|tv|stories)/\S+",
        r"(https?://)?(www\.|m\.|web\.)*(facebook|fb)\.com/.*/(videos|posts)/\S+",
        r"(https?://)?fb\.watch/\S+",
        r"(https?://)?(www\.|m\.|web\.)*(facebook|fb)\.com/reel/\S+",
        r"(https?://)?(www\.|m\.|web\.)*(facebook|fb)\.com/share/\S+",
        r"(https?://)?(www\.|m\.|web\.)*(facebook|fb)\.com/permalink\.php",
        r"(https?://)?vm\.tiktok\.com/\S+",
        r"(https?://)?(www\.)?tiktok\.com/@\S+/video/\d+",
    ]
    for pattern in patterns:
        if re.match(pattern, url):
            return True
    return False


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


def dedupe_thumbnails(file_paths):
    groups = {}
    for fp in file_paths:
        p = Path(fp)
        groups.setdefault(p.stem, []).append(fp)
    result = []
    for stem, files in groups.items():
        has_video = any(Path(f).suffix.lower() in VIDEO_EXTS for f in files)
        for f in files:
            if has_video and Path(f).suffix.lower() in IMAGE_EXTS:
                continue
            result.append(f)
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


def collect_image_urls(info: dict) -> list:
    primary = []
    fallback = []

    def walk(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "display_resources" and isinstance(v, list):
                    # Multiple resolutions of the same image; keep only the last (highest res)
                    r = v[-1]
                    if isinstance(r, dict) and r.get("src"):
                        primary.append(r["src"])
                elif k == "images" and isinstance(v, list):
                    for im in v:
                        if isinstance(im, dict) and im.get("url"):
                            primary.append(im["url"])
                elif k == "thumbnails" and isinstance(v, list):
                    t = v[-1]
                    if isinstance(t, dict) and t.get("url"):
                        primary.append(t["url"])
                elif k == "thumbnail" and isinstance(v, str) and v.startswith("http"):
                    fallback.append(v)
                elif k == "url" and isinstance(v, str) and re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", v, re.I):
                    primary.append(v)
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


def download_image_ytdlp(image_url: str, download_path: Path, idx: int) -> str:
    fpath = download_path / f"{idx:05d}.%(ext)s"
    cmd = [
        "yt-dlp",
        "--no-warnings",
        "--no-check-certificates",
        "--no-playlist",
        "--ignore-no-formats-error",
        "--no-overwrites",
        "--no-write-info-json",
    ] + get_cookie_args() + [
        "-o", str(fpath),
        image_url,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0 and proc.stderr.strip():
            logger.error(f"Image yt-dlp stderr: {proc.stderr.strip()[:500]}")
    except Exception as e:
        logger.error(f"Image yt-dlp exception: {e}")
        return None
    for f in download_path.iterdir():
        if f.name.startswith(f"{idx:05d}.") and f.is_file() and not f.name.endswith(".info.json") and f.stat().st_size > 0:
            return str(f)
    return None


def extract_media(url: str, download_path: Path, chat_id: int, status_msg_id: int, app: Application) -> tuple[dict, list]:
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

    if COOKIES_FILE and not Path(COOKIES_FILE).exists():
        logger.warning(f"COOKIES_FILE '{COOKIES_FILE}' does not exist — X/Instagram may need cookies")

    if is_x:
        logger.info("X/Twitter detected — may require cookies/authentication")

    cmd_dl = [
        "yt-dlp",
        "--no-warnings",
        "--no-check-certificates",
        "--ignore-no-formats-error",
        "--write-info-json",
        "--no-overwrites",
    ] + get_cookie_args() + [
        "-o", str(download_path / "%(autonumber)s_%(id)s.%(ext)s"),
    ]
    if not is_carousel:
        cmd_dl.append("--no-playlist")
    cmd_dl.append(url)

    try:
        proc = subprocess.run(cmd_dl, capture_output=True, text=True, timeout=600)
        if proc.stderr.strip():
            logger.error(f"yt-dlp stderr: {proc.stderr.strip()[:1000]}")
    except subprocess.TimeoutExpired:
        raise ValueError("Download timed out (10 min limit).")
    except FileNotFoundError:
        raise ValueError("yt-dlp not found.")

    info = {}
    best_info = {}
    best_count = 0
    for f in sorted(download_path.glob("*.info.json")):
        try:
            with open(f) as fp:
                data = json.load(fp)
                urls = collect_image_urls(data)
                logger.info(f"Info file {f.name}: {len(urls)} image URL(s), keys={list(data.keys())[:10]}")
                if len(urls) > best_count:
                    best_count = len(urls)
                    best_info = data
                try:
                    f.unlink()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Failed to read info file {f.name}: {e}")
            continue
    if best_count > 0:
        info = best_info
        logger.info(f"Using best info file: {best_count} image URLs")

    if not info:
        info = get_json_info(url)
        logger.info(f"Fallback get_json_info returned dict: {isinstance(info, dict)}, keys={list(info.keys())[:10] if info else 'empty'}")

    image_urls = collect_image_urls(info)
    logger.info(f"Found {len(image_urls)} image URL(s): {[u[:60] for u in image_urls[:5]]}")
    seen_hashes = set()
    image_idx = 0
    for image_url in image_urls:
        fp = download_image_ytdlp(image_url, download_path, image_idx)
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
        if is_x:
            raise ValueError("X/Twitter requires cookies/authentication to download. Please provide cookies in the env (COOKIES_FILE).")
        raise ValueError("No media found in this post. The post may be private or require login.")

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
        chat_id = query.message.chat.id
        cancelled_downloads[chat_id] = True
        await query.edit_message_text("Download cancelled.")


async def handle_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_authorized(user_id):
        await update.message.reply_text("Bot is for personal use only.")
        return

    text = update.message.text.strip()
    if text.lower() in ("start", "/start"):
        await start(update, context)
        return

    url = text
    if not is_valid_url(url):
        await update.message.reply_text(
            "Please send a valid link.\n\n"
            "Supported:\n"
            "- X/Twitter: https://x.com/user/status/123\n"
            "- Instagram: https://instagram.com/p/ABC/\n"
            "- Facebook: https://facebook.com/user/videos/123"
        )
        return

    chat_id = update.effective_chat.id
    cancelled_downloads[chat_id] = False

    status_msg = await update.message.reply_text(
        "Starting download...",
        reply_markup=cancel_keyboard,
    )

    try:
        loop = asyncio.get_running_loop()
        info, file_paths = await loop.run_in_executor(
            None, extract_media, url, DOWNLOAD_DIR, chat_id, status_msg.message_id, context.application
        )

        if cancelled_downloads.get(chat_id):
            await status_msg.edit_text("Download cancelled.")
            return

        if not file_paths:
            await status_msg.edit_text("No media found.")
            return

        caption = (info or {}).get("description", "") or (info or {}).get("title", "") or ""

        await status_msg.edit_text(f"Uploading {len(file_paths)} file(s)...")

        for file_path in file_paths:
            if not os.path.exists(file_path):
                continue

            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            is_video = file_path.lower().endswith(VIDEO_EXTS)

            # Telegram: photos max 10MB, videos/documents max 50MB
            max_size = 50 if is_video else 10
            if file_size_mb > max_size:
                if is_video:
                    await status_msg.edit_text(f"Video is {file_size_mb:.1f}MB. Compressing...")
                    compressed_path = str(file_path) + ".compressed.mp4"
                    compress_cmd = (
                        f'ffmpeg -i "{file_path}" -c:v libx264 -crf 28 '
                        f'-vf "scale=\'min(1280,iw)\':-2" -c:a aac -b:a 128k '
                        f'-movflags +faststart "{compressed_path}" -y'
                    )
                    await loop.run_in_executor(None, lambda: os.system(compress_cmd))
                    if os.path.exists(compressed_path) and os.path.getsize(compressed_path) < 50 * 1024 * 1024:
                        file_path = compressed_path
                    else:
                        with open(file_path, "rb") as f:
                            await update.message.reply_document(document=f, caption=caption)
                        os.remove(file_path)
                        continue
                else:
                    with open(file_path, "rb") as f:
                        await update.message.reply_document(document=f, caption=caption)
                    os.remove(file_path)
                    continue

            if is_video:
                with open(file_path, "rb") as video_file:
                    await update.message.reply_video(video=video_file, caption=caption)
            else:
                with open(file_path, "rb") as photo_file:
                    await update.message.reply_photo(photo=photo_file, caption=caption)

        await status_msg.delete()

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error: {error_msg}")
        await status_msg.edit_text(f"Error: {error_msg[:200]}")

    finally:
        cancelled_downloads.pop(chat_id, None)
        for f in DOWNLOAD_DIR.iterdir():
            if f.is_file():
                try:
                    f.unlink()
                except Exception:
                    pass


def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN not set in .env file")

    application = Application.builder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", start))
    application.add_handler(CallbackQueryHandler(cancel_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    logger.info("Bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
