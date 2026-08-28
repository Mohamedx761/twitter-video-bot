import os
import asyncio
import logging
import re
import subprocess
import json
from pathlib import Path
from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
AUTHORIZED_USER_ID = int(os.getenv("AUTHORIZED_USER_ID", "0"))
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", "./downloads"))
DOWNLOAD_DIR.mkdir(exist_ok=True)

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

VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.mov')
IMAGE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.webp')


def extract_media(url: str, download_path: Path, chat_id: int, status_msg_id: int, app: Application) -> tuple[dict, list]:
    downloaded_files = []
    before_files = set(f.name for f in download_path.iterdir() if f.is_file())

    info_file = download_path / "info.json"

    cmd = [
        "yt-dlp",
        "--dump-json",
        "--no-warnings",
        "-o", str(download_path / "%(autonumber)s_%(id)s.%(ext)s"),
        url,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            error = result.stderr.strip()
            if "No video" in error or "could not be found" in error:
                raise ValueError("No media found in this post.")
            elif "Private" in error or "protected" in error:
                raise ValueError("This post is from a private account.")
            elif "HTTP Error 404" in error:
                raise ValueError("Post not found or has been deleted.")
            else:
                raise ValueError(error[:200] if error else "Download failed.")
    except subprocess.TimeoutExpired:
        raise ValueError("Download timed out.")
    except FileNotFoundError:
        raise ValueError("yt-dlp not found on server.")

    info = {}
    if result.stdout.strip():
        try:
            first_line = result.stdout.strip().split('\n')[0]
            info = json.loads(first_line)
        except json.JSONDecodeError:
            info = {"title": "Media"}

    cmd_download = [
        "yt-dlp",
        "--no-warnings",
        "-o", str(download_path / "%(autonumber)s_%(id)s.%(ext)s"),
        "--no-overwrites",
        url,
    ]

    proc = subprocess.Popen(
        cmd_download,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    _, stderr = proc.communicate(timeout=300)

    after_files = set(f.name for f in download_path.iterdir() if f.is_file())
    new_files = after_files - before_files

    for fname in sorted(new_files):
        fpath = download_path / fname
        if fpath.is_file() and fpath.stat().st_size > 0:
            downloaded_files.append(str(fpath))

    if not downloaded_files:
        for f in download_path.iterdir():
            if f.is_file() and f.stat().st_size > 0 and f.name != "info.json":
                downloaded_files.append(str(f))

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
        "- Facebook videos and reels"
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

    url = update.message.text.strip()
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

        caption = info.get("description", "") or info.get("title", "") or ""

        await status_msg.edit_text(f"Uploading {len(file_paths)} file(s)...")

        for file_path in file_paths:
            if not os.path.exists(file_path):
                continue

            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            is_video = file_path.lower().endswith(VIDEO_EXTS)

            if file_size_mb > 50:
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
