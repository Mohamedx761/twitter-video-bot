import os
import asyncio
import logging
import re
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
import yt_dlp

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

X_URL_PATTERN = re.compile(
    r"(https?://)?(www\.)?(x\.com|twitter\.com)/\S+/status/\d+"
)

INSTAGRAM_URL_PATTERN = re.compile(
    r"(https?://)?(www\.)?(instagram\.com)/(p|reel|tv)/\S+"
)

cancelled_downloads = {}


def is_authorized(user_id: int) -> bool:
    if AUTHORIZED_USER_ID == 0:
        return True
    return user_id == AUTHORIZED_USER_ID


def is_valid_url(url: str) -> bool:
    return bool(X_URL_PATTERN.match(url.strip())) or bool(INSTAGRAM_URL_PATTERN.match(url.strip()))


def clean_url(url: str) -> str:
    url = url.strip()
    if "?" in url:
        url = url.split("?")[0]
    if "twitter.com" in url:
        url = url.replace("twitter.com", "x.com")
    if not url.startswith("http"):
        url = "https://" + url
    return url


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
    cleaned_url = clean_url(url)
    downloaded_files = []

    before_files = set(f.name for f in download_path.iterdir() if f.is_file())

    def progress_hook(d):
        if cancelled_downloads.get(chat_id):
            raise yt_dlp.utils.DownloadCancelled("Cancelled")

        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed", 0)
            eta = d.get("eta", 0)

            if total > 0:
                percent = int((downloaded / total) * 100)
            else:
                percent = 0

            speed_mb = speed / (1024 * 1024) if speed else 0
            downloaded_mb = downloaded / (1024 * 1024)
            total_mb = total / (1024 * 1024) if total else 0

            bar = make_progress_bar(percent)
            text = f"Downloading...\n\n{bar}\n\n"
            text += f"Downloaded: {downloaded_mb:.1f}/{total_mb:.1f} MB\n"
            text += f"Speed: {speed_mb:.1f} MB/s\n"
            text += f"Time left: {eta}s" if eta else "Time left: calculating..."

            try:
                app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=text,
                    reply_markup=cancel_keyboard,
                )
            except Exception:
                pass

        elif d["status"] == "finished":
            try:
                total_bytes = d.get("total_bytes", 0)
                total_mb = total_bytes / (1024 * 1024)
                app.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=status_msg_id,
                    text=f"Download complete!\n\nFile size: {total_mb:.1f} MB\nUploading...",
                )
            except Exception:
                pass

    ydl_opts = {
        "outtmpl": str(download_path / "%(id)s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "max_filesize": 50 * 1024 * 1024,
        "progress_hooks": [progress_hook],
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(cleaned_url, download=True)

        if not info:
            raise ValueError("Could not extract info.")

        if "entries" in info:
            for entry in info["entries"]:
                if entry:
                    ydl.download([entry.get("url", cleaned_url)])
        else:
            ydl.download([cleaned_url])

    after_files = set(f.name for f in download_path.iterdir() if f.is_file())
    new_files = after_files - before_files

    for fname in sorted(new_files):
        fpath = download_path / fname
        if fpath.is_file() and fpath.stat().st_size > 0:
            downloaded_files.append(str(fpath))

    if not downloaded_files:
        for f in download_path.iterdir():
            if f.is_file() and f.stat().st_size > 0:
                downloaded_files.append(str(f))

    return info, downloaded_files


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        await update.message.reply_text("Bot is for personal use only.")
        return
    await update.message.reply_text(
        "Send me a link from X (Twitter) or Instagram and I will download it.\n\n"
        "Supported:\n"
        "- X/Twitter videos and images\n"
        "- Instagram posts, reels, and videos"
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
            "Please send a valid link from X or Instagram.\n\n"
            "Examples:\n"
            "- https://x.com/user/status/123456\n"
            "- https://www.instagram.com/p/ABC123/"
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

        description = info.get("description", "") or info.get("title", "")
        caption = description if description else ""

        await status_msg.edit_text(f"Uploading {len(file_paths)} file(s)...")

        for file_path in file_paths:
            if not os.path.exists(file_path):
                continue

            file_size_mb = os.path.getsize(file_path) / (1024 * 1024)
            is_video = file_path.lower().endswith(VIDEO_EXTS)

            if is_video and file_size_mb > 50:
                await status_msg.edit_text(
                    f"Video is {file_size_mb:.1f}MB. Compressing..."
                )
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

            if is_video:
                with open(file_path, "rb") as video_file:
                    await update.message.reply_video(
                        video=video_file,
                        caption=caption,
                    )
            else:
                with open(file_path, "rb") as photo_file:
                    await update.message.reply_photo(
                        photo=photo_file,
                        caption=caption,
                    )

        await status_msg.delete()

    except yt_dlp.utils.DownloadCancelled:
        await status_msg.edit_text("Download cancelled.")

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error: {error_msg}")
        if "No video" in error_msg or "could not be found" in error_msg:
            await status_msg.edit_text("No media found in this post.")
        elif "Private" in error_msg or "protected" in error_msg:
            await status_msg.edit_text("This post is from a private account.")
        elif "unavailable" in error_msg or "not found" in error_msg:
            await status_msg.edit_text("This post is unavailable or has been deleted.")
        else:
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
