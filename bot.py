import os
import asyncio
import logging
import re
import subprocess
import json
import hashlib
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

def get_cookie_args() -> list:
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        return ["--cookie-file", COOKIES_FILE]
    return []

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


async def safe_edit(msg, text):
    try:
        await msg.edit_text(text)
    except TelegramError:
        pass

cancelled_downloads = {}


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


def collect_carousel_urls(info: dict) -> list:
    urls = []
    edges = info.get("edge_sidecar_to_children", {}).get("edges", [])
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
    logger.info(f"Carousel collected {len(urls)} URLs before dedupe")
    seen = set()
    result = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            result.append(u)
    return result


def get_carousel_image_urls(url: str) -> list:
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


def run_ytdlp_download(url: str, download_path: Path, is_carousel: bool, is_x: bool, use_cookies: bool) -> str:
    """Run one yt-dlp download attempt. Returns stderr text for diagnostics."""
    cmd_dl = [
        "yt-dlp",
        "--no-warnings",
        "--no-check-certificates",
        "--ignore-no-formats-error",
        "--write-info-json",
        "--no-overwrites",
    ]
    if use_cookies:
        cmd_dl += get_cookie_args()
    cmd_dl += ["-o", str(download_path / "%(autonumber)s_%(id)s.%(ext)s")]
    if is_x:
        cmd_dl += ["--verbose", "--write-thumbnail"]
    if not is_carousel:
        cmd_dl.append("--no-playlist")
    cmd_dl.append(url)

    try:
        proc = subprocess.run(cmd_dl, capture_output=True, text=True, timeout=600)
        stderr = proc.stderr or ""
        if stderr.strip():
            logger.error(f"yt-dlp stderr (cookies={use_cookies}): {stderr.strip()[:1000]}")
        return stderr
    except subprocess.TimeoutExpired:
        raise ValueError("Download timed out (10 min limit).")
    except FileNotFoundError:
        raise ValueError("yt-dlp not found.")


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
                for key in ["images", "image_urls", "image_list"]:
                    val = data.get(key)
                    if isinstance(val, list):
                        for item in val:
                            if isinstance(item, str) and item.startswith("http"):
                                urls.append(item)
                            elif isinstance(item, dict):
                                for uk in ["url", "src", "download_url"]:
                                    if uk in item and isinstance(item[uk], str):
                                        urls.append(item[uk])
                                        break
                urls = list(dict.fromkeys(urls))
                logger.info(f"TikTok fallback: {len(urls)} image URL(s)")
                return urls
    except Exception as e:
        logger.error(f"TikTok JSON fetch failed: {e}")
    return []


def get_x_image_urls(url: str) -> list:
    """Use --dump-single-json to get X/Twitter image post metadata."""
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
        logger.info(f"X image fetch: running yt-dlp --dump-single-json for {url}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        logger.info(f"X image fetch: returncode={result.returncode}, stdout_len={len(result.stdout or '')}, stderr_len={len(result.stderr or '')}")
        if result.stderr:
            logger.error(f"X image fetch stderr: {result.stderr.strip()[:500]}")
        if result.stdout.strip():
            try:
                data = json.loads(result.stdout.strip())
            except json.JSONDecodeError:
                logger.error(f"X JSON parse error: {result.stdout.strip()[:500]}")
                return []
            if isinstance(data, dict):
                all_keys = list(data.keys())[:30]
                logger.info(f"X JSON top keys: {all_keys}")
                urls = collect_image_urls(data)
                if urls:
                    logger.info(f"X JSON via collect_image_urls: {len(urls)} image URL(s)")
                    return urls

                entries = data.get("entries")
                if isinstance(entries, list):
                    for idx, entry in enumerate(entries):
                        if not isinstance(entry, dict):
                            continue
                        logger.info(f"X entry {idx} keys: {list(entry.keys())[:20]}")
                        entry_urls = collect_image_urls(entry)
                        if entry_urls:
                            logger.info(f"X entry {idx}: {len(entry_urls)} image URL(s)")
                            urls.extend(entry_urls)
                    if urls:
                        return list(dict.fromkeys(urls))

                for key in ["photos", "images", "media_details", "extended_entities", "media"]:
                    val = data.get(key)
                    if isinstance(val, dict):
                        val = val.get("media", [])
                    if isinstance(val, list):
                        for item in val:
                            if isinstance(item, str) and item.startswith("http"):
                                urls.append(item)
                            elif isinstance(item, dict):
                                for uk in ["url", "src", "media_url_https", "original_url", "media_url"]:
                                    if uk in item and isinstance(item[uk], str) and item[uk].startswith("http"):
                                        urls.append(item[uk])
                                        break

                if not urls:
                    logger.info("X: deep recursive search for any URLs in JSON")
                    def _deep_find(obj, depth=0):
                        if depth > 12:
                            return
                        if isinstance(obj, dict):
                            for k, v in obj.items():
                                if k in ("url", "media_url", "media_url_https", "original_url", "src") and isinstance(v, str) and v.startswith("http"):
                                    if re.search(r"\.(jpg|jpeg|png|webp|gif)(\?|$)", v, re.I):
                                        urls.append(v)
                                _deep_find(v, depth + 1)
                        elif isinstance(obj, list):
                            for item in obj:
                                _deep_find(item, depth + 1)
                    _deep_find(data)

                urls = list(dict.fromkeys(urls))
                logger.info(f"X final: {len(urls)} image URL(s)")
                return urls
        else:
            logger.warning("X image fetch: no stdout output")
    except Exception as e:
        logger.error(f"X JSON fetch failed: {e}")
    return []


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

    have_cookies_file = bool(COOKIES_FILE) and Path(COOKIES_FILE).exists()
    logger.info(f"Cookies: COOKIES_FILE={COOKIES_FILE!r}, exists={have_cookies_file}")

    if is_x:
        if have_cookies_file:
            logger.info("X/Twitter: using cookies")
            last_stderr = run_ytdlp_download(url, download_path, is_carousel, is_x, use_cookies=True)
        else:
            logger.info("X/Twitter: no cookies file, attempting without cookies")
            last_stderr = run_ytdlp_download(url, download_path, is_carousel, is_x, use_cookies=False)
    else:
        last_stderr = run_ytdlp_download(url, download_path, is_carousel, is_x, use_cookies=True)

    all_image_urls = []
    info = {}
    for f in sorted(download_path.glob("*.info.json")):
        try:
            with open(f) as fp:
                data = json.load(fp)
                urls = collect_image_urls(data)
                all_image_urls.extend(urls)
                if not info:
                    info = data
                logger.info(f"Info file {f.name}: {len(urls)} image URL(s), keys={list(data.keys())[:10]}")
                try:
                    f.unlink()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Failed to read info file {f.name}: {e}")
            continue
    logger.info(f"All info files: {len(all_image_urls)} image URL(s) total")

    is_tiktok = "tiktok.com" in url_lower or "vm.tiktok.com" in url_lower

    if is_carousel and not all_image_urls:
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

    if is_x and not all_image_urls:
        logger.info("X/Twitter: no video found, trying to get images via --dump-single-json")
        x_urls = get_x_image_urls(url)
        for u in x_urls:
            if u not in all_image_urls:
                all_image_urls.append(u)
        logger.info(f"After X image fetch: {len(all_image_urls)} image URL(s)")

    image_urls = all_image_urls
    logger.info(f"Final image URL(s): {len(image_urls)}")
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
            if have_cookies_file:
                tail = (last_stderr or "").strip().splitlines()
                hint = tail[-1] if tail else ""
                raise ValueError(
                    "فشل التحميل من X حتى مع الكوكيز.\n\n"
                    f"السبب المحتمل: {hint[:200]}\n\n"
                    "جرب:\n"
                    "1. الكوكيز ممكن تكون منتهية — اعمل Export جديد وابعته بـ /cookies\n"
                    "2. حدّث yt-dlp لآخر إصدار: pip install -U yt-dlp"
                )
            tail = (last_stderr or "").strip().splitlines()
            hint = tail[-1] if tail else ""
            raise ValueError(
                f"التحميل من X فشل.\n\n"
                f"سبب yt-dlp: {hint[:200]}\n\n"
                "لو البوست عام وطبيعي، جرب تاني بعد شوية.\n"
                "لو المشكلة مستمرة، ابعت كوكيز بالأمر /cookies"
            )
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
            await safe_edit(status_msg, "Download cancelled.")
            return

        if not file_paths:
            await safe_edit(status_msg, "No media found.")
            return

        caption = (info or {}).get("description", "") or (info or {}).get("title", "") or ""

        await safe_edit(status_msg, f"Uploading {len(file_paths)} file(s)...")

        photos = [fp for fp in file_paths if fp.lower().endswith(IMAGE_EXTS)]
        videos = [fp for fp in file_paths if fp.lower().endswith(VIDEO_EXTS)]

        prepared_items = []
        oversized_documents = []

        for photo_path in photos:
            if not os.path.exists(photo_path):
                continue
            fp_size = os.path.getsize(photo_path) / (1024 * 1024)
            use_path = photo_path
            if fp_size > 10:
                await safe_edit(status_msg, "Photo too large, compressing...")
                cpath = str(photo_path) + ".compressed.jpg"
                await loop.run_in_executor(None, lambda p=str(photo_path), c=str(cpath): subprocess.run([
                    "ffmpeg", "-i", p, "-vf", "scale=min(1280,iw):-2",
                    "-q:v", "2", c, "-y"
                ]))
                if os.path.exists(cpath) and os.path.getsize(cpath) < 10 * 1024 * 1024:
                    use_path = cpath
            prepared_items.append((use_path, "photo"))

        for video_path in videos:
            if not os.path.exists(video_path):
                continue
            file_size_mb = os.path.getsize(video_path) / (1024 * 1024)
            use_path = video_path
            if file_size_mb > 50:
                await safe_edit(status_msg, f"Video is {file_size_mb:.1f}MB. Compressing...")
                cpath = str(video_path) + ".compressed.mp4"
                await loop.run_in_executor(None, lambda v=str(video_path), c=str(cpath): subprocess.run([
                    "ffmpeg", "-i", v, "-c:v", "libx264", "-crf", "28",
                    "-vf", "scale=min(1280,iw):-2", "-c:a", "aac", "-b:a", "128k",
                    "-movflags", "+faststart", c, "-y"
                ]))
                if os.path.exists(cpath) and os.path.getsize(cpath) < 50 * 1024 * 1024:
                    use_path = cpath
                else:
                    oversized_documents.append(video_path)
                    continue
            prepared_items.append((use_path, "video"))

        await safe_edit(status_msg, f"Uploading {len(prepared_items) + len(oversized_documents)} file(s)...")

        for i in range(0, len(prepared_items), 10):
            group = prepared_items[i:i + 10]

            if len(group) == 1:
                path, kind = group[0]
                with open(path, "rb") as f:
                    if kind == "photo":
                        await update.message.reply_photo(photo=f, caption=caption)
                    else:
                        await update.message.reply_video(video=f, caption=caption)
                continue

            media = []
            files = []
            for idx, (path, kind) in enumerate(group):
                fname = Path(path).name
                files.append(InputFile(str(path)))
                item_caption = caption if (i == 0 and idx == 0) else None
                if kind == "photo":
                    media.append(InputMediaPhoto(media=f"attach://{fname}", caption=item_caption))
                else:
                    media.append(InputMediaVideo(media=f"attach://{fname}", caption=item_caption))
            try:
                await context.bot.send_media_group(chat_id=chat_id, media=media, files=files)
                logger.info(f"send_media_group sent {len(group)} item(s)")
            except Exception as e:
                logger.error(f"send_media_group failed: {e}, falling back to individual sends")
                for path, kind in group:
                    with open(path, "rb") as f:
                        if kind == "photo":
                            await update.message.reply_photo(photo=f, caption=caption)
                        else:
                            await update.message.reply_video(video=f, caption=caption)

        for video_path in oversized_documents:
            with open(video_path, "rb") as f:
                await update.message.reply_document(document=f, caption=caption)
            os.remove(video_path)

        await status_msg.delete()

    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error: {error_msg}")
        await safe_edit(status_msg, f"Error: {error_msg[:200]}")

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
    application.add_handler(CommandHandler("cookies", cookies_command))
    application.add_handler(CallbackQueryHandler(cancel_button))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_url))

    logger.info("Bot started")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
