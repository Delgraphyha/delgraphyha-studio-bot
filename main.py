import os
import re
import time
import uuid
import shutil
import asyncio
import unicodedata
from difflib import SequenceMatcher
import threading
from pathlib import Path

from flask import Flask, request
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

import yt_dlp


TOKEN = os.getenv("TELEGRAM_TOKEN", "")
CHANNEL_USERNAME = "@delgraphyha"
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

DOWNLOAD_ROOT = Path("downloads")
DOWNLOAD_ROOT.mkdir(exist_ok=True)

SEARCH_RESULTS_PER_SOURCE = 8
SEARCH_COOLDOWN_SECONDS = 5

# محدود کردن فشار روی سایت‌ها
last_search_time = {}

# فایل کوکی فقط یک بار ساخته می‌شود
COOKIES_FILE = None

application = None
background_loop = None

app = Flask(__name__)


# ----------------------------
# Flask / Healthcheck / Webhook
# ----------------------------

@app.route("/")
def home():
    return "Bot is running and alive!", 200


@app.route(f"/{TOKEN}", methods=["POST"])
def webhook():
    global application, background_loop

    if not application or not background_loop:
        return "Bot not ready", 503

    try:
        json_data = request.get_json(force=True)
        update = Update.de_json(json_data, application.bot)

        asyncio.run_coroutine_threadsafe(
            application.process_update(update),
            background_loop
        )

        return "OK", 200

    except Exception as e:
        print(f"Webhook error: {e}")
        return "ERROR", 500


# ----------------------------
# Helpers
# ----------------------------

def start_background_loop(loop):
    asyncio.set_event_loop(loop)
    loop.run_forever()


def setup_cookies():
    global COOKIES_FILE

    cookies_content = os.getenv("YOUTUBE_COOKIES", "").strip()

    if not cookies_content:
        COOKIES_FILE = None
        return

    cookie_path = Path("cookies.txt")
    cookie_path.write_text(cookies_content, encoding="utf-8")
    COOKIES_FILE = str(cookie_path)


def is_url(text: str) -> bool:
    return bool(re.match(r"^https?://", text.strip(), flags=re.IGNORECASE))


def format_duration(seconds):
    if seconds is None:
        return ""

    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return ""

    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)

    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"

    return f"{minutes}:{seconds:02d}"


def base_ydl_options():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "socket_timeout": 25,
        "retries": 2,
    }

    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE

    return opts


# ----------------------------
# Subscription
# ----------------------------

async def check_subscription(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(
            chat_id=CHANNEL_USERNAME,
            user_id=user_id
        )

        return member.status in [
            "member",
            "creator",
            "administrator",
        ]

    except Exception as e:
        print(f"Subscription check error: {e}")
        return False


async def require_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    # داخل گروه محدودیت عضویت اعمال نمی‌شود
    if update.effective_chat and update.effective_chat.type in ["group", "supergroup"]:
        return True

    user_id = update.effective_user.id

    if await check_subscription(user_id, context):
        return True

    keyboard = [
        [
            InlineKeyboardButton(
                "📢 عضویت در کانال",
                url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}"
            )
        ],
        [
            InlineKeyboardButton(
                "✅ عضو شدم، بررسی کن",
                callback_data="check_sub"
            )
        ]
    ]

    text = (
        "⚠️ برای استفاده از ربات ابتدا در کانال عضو شوید:\n"
        f"{CHANNEL_USERNAME}"
    )

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception:
            pass
    elif update.message:
        await update.message.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    return False


# ----------------------------
# Search
# ----------------------------

def normalize_search_entry(entry, source):
    if not entry:
        return None

    title = entry.get("title") or "Unknown"
    uploader = (
        entry.get("uploader")
        or entry.get("channel")
        or entry.get("artist")
        or ""
    )

    duration = entry.get("duration")
    webpage_url = entry.get("webpage_url")

    # ytsearch در بعضی حالت‌ها فقط ID می‌دهد
    if source == "YouTube":
        video_id = entry.get("id")

        if not webpage_url and video_id:
            webpage_url = f"https://www.youtube.com/watch?v={video_id}"

    if source == "SoundCloud":
        if not webpage_url:
            url_value = entry.get("url")
            if isinstance(url_value, str) and url_value.startswith("http"):
                webpage_url = url_value

    if not webpage_url:
        return None

    return {
        "title": title,
        "uploader": uploader,
        "duration": duration,
        "source": source,
        "url": webpage_url,
    }


def search_with_prefix(query_text: str, prefix: str, source: str, limit: int):
    opts = base_ydl_options()
    opts.update({
        "skip_download": True,
        "extract_flat": True,
    })

    search_query = f"{prefix}{limit}:{query_text}"

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_query, download=False)

        if not info:
            return []

        entries = info.get("entries") or []
        results = []

        for entry in entries:
            item = normalize_search_entry(entry, source)

            if item:
                results.append(item)

        return results

    except Exception as e:
        print(f"{source} search error: {e}")
        return []


def inspect_direct_url(url: str):
    opts = base_ydl_options()
    opts.update({
        "skip_download": True,
        "noplaylist": True,
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return None

        if "entries" in info and info.get("entries"):
            info = info["entries"][0]

        return {
            "title": info.get("title") or "Unknown",
            "uploader": (
                info.get("uploader")
                or info.get("channel")
                or info.get("artist")
                or ""
            ),
            "duration": info.get("duration"),
            "source": info.get("extractor_key") or info.get("extractor") or "Direct Link",
            "url": info.get("webpage_url") or url,
        }

    except Exception as e:
        print(f"Direct URL inspect error: {e}")
        return None


async def search_music(query_text: str):
    # Direct URL from any yt-dlp-supported site
    if is_url(query_text):
        item = await asyncio.to_thread(inspect_direct_url, query_text)
        return [item] if item else []

    def clean_text(value: str) -> str:
        value = unicodedata.normalize("NFKC", (value or "").lower())
        value = re.sub(r"[^\w\s]", " ", value, flags=re.UNICODE)
        return " ".join(value.split())

    def score_result(item: dict) -> float:
        query = clean_text(query_text)
        title = clean_text(item.get("title", ""))
        uploader = clean_text(item.get("uploader", ""))
        haystack = f"{title} {uploader}".strip()

        if not query or not haystack:
            return 0.0

        query_words = set(query.split())
        title_words = set(title.split())
        all_words = set(haystack.split())

        # Exact query/title matches matter most.
        score = SequenceMatcher(None, query, title).ratio() * 55
        score += SequenceMatcher(None, query, haystack).ratio() * 20

        if query == title:
            score += 35
        elif query in title:
            score += 22
        elif query in haystack:
            score += 12

        if query_words:
            score += (len(query_words & title_words) / len(query_words)) * 35
            score += (len(query_words & all_words) / len(query_words)) * 15

        # Prefer sensible song-length results over very long videos/streams.
        duration = item.get("duration")
        if isinstance(duration, (int, float)):
            if 90 <= duration <= 600:
                score += 8
            elif duration > 1200:
                score -= 15

        # Usually the user wants the original track, not these variants.
        penalty_words = {
            "karaoke": 14,
            "reaction": 18,
            "tutorial": 18,
            "cover": 8,
            "remix": 6,
            "slowed": 8,
            "reverb": 8,
            "instrumental": 6,
            "live": 4,
        }
        for word, penalty in penalty_words.items():
            if word in title_words and word not in query_words:
                score -= penalty

        # YouTube is the primary source; SoundCloud remains a fallback.
        if item.get("source") == "YouTube":
            score += 5

        return score

    # Search YouTube first with a larger candidate pool.
    youtube_results = await asyncio.to_thread(
        search_with_prefix,
        query_text,
        "ytsearch",
        "YouTube",
        SEARCH_RESULTS_PER_SOURCE,
    )

    candidates = list(youtube_results)

    # SoundCloud is a fallback/additional source, not an alternating list.
    # Search it when YouTube returned only a few usable candidates.
    if len(candidates) < 6:
        soundcloud_results = await asyncio.to_thread(
            search_with_prefix,
            query_text,
            "scsearch",
            "SoundCloud",
            SEARCH_RESULTS_PER_SOURCE,
        )
        candidates.extend(soundcloud_results)

    # Remove duplicate URLs before ranking.
    unique = []
    seen_urls = set()
    for item in candidates:
        url = item.get("url")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        item["score"] = score_result(item)
        unique.append(item)

    unique.sort(key=lambda x: x.get("score", 0), reverse=True)
    return unique[:8]


# ----------------------------
# Download
# ----------------------------

def download_audio(url: str, user_id: int):
    job_id = uuid.uuid4().hex[:12]
    user_dir = DOWNLOAD_ROOT / str(user_id) / job_id
    user_dir.mkdir(parents=True, exist_ok=True)

    output_template = str(user_dir / "track.%(ext)s")

    opts = base_ydl_options()
    opts.update({
        "format": "bestaudio/best",
        "outtmpl": output_template,
        "noplaylist": True,
        "ignoreerrors": False,
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "128",
            }
        ],
    })

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)

        if not info:
            raise RuntimeError("No media information returned")

        title = info.get("title") or "Music"

        mp3_files = list(user_dir.glob("*.mp3"))

        if not mp3_files:
            raise FileNotFoundError("MP3 output not found")

        return {
            "file_path": str(mp3_files[0]),
            "title": title,
            "work_dir": str(user_dir),
        }

    except Exception as e:
        print(f"yt-dlp download error for {url}: {type(e).__name__}: {e}")
        shutil.rmtree(user_dir, ignore_errors=True)
        raise


async def send_downloaded_audio(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    result: dict,
):
    query = update.callback_query
    user_id = query.from_user.id

    try:
        await query.edit_message_text(
            f"⬇️ در حال دریافت:\n{result['title']}\n\n"
            f"منبع: {result['source']}"
        )

        downloaded = await asyncio.to_thread(
            download_audio,
            result["url"],
            user_id,
        )

        file_path = downloaded["file_path"]
        title = downloaded["title"]

        with open(file_path, "rb") as audio_file:
            await context.bot.send_audio(
                chat_id=query.message.chat_id,
                audio=audio_file,
                title=title,
                caption=(
                    f"🎵 {title}\n"
                    f"🔎 منبع: {result['source']}\n"
                    "🤖 Delgraphyha Music Bot"
                ),
            )

        await query.edit_message_text(
            f"✅ ارسال شد:\n{title}"
        )

    except Exception as e:
        print(f"Download/send error: {e}")

        try:
            await query.edit_message_text(
                "❌ دریافت این نتیجه ممکن نشد.\n"
                "ممکن است منبع محدودیت داشته باشد یا موقتاً در دسترس نباشد."
            )
        except Exception:
            pass

    finally:
        # پاک کردن فایل‌های موقت مربوط به همین کاربر/دانلود
        user_root = DOWNLOAD_ROOT / str(user_id)

        if user_root.exists():
            for child in list(user_root.iterdir()):
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)


# ----------------------------
# Telegram handlers
# ----------------------------

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_subscription(update, context):
        return

    await update.message.reply_text(
        "🎵 نام آهنگ، خواننده یا هر عبارتی که می‌خواهید بفرستید.\n\n"
        "مثال:\n"
        "Adele Hello\n"
        "Ebi Khalij\n"
        "Rammstein Sonne\n"
        "Tarkan Dudu\n"
        "Arijit Singh Tum Hi Ho\n\n"
        "همچنین می‌توانید لینک مستقیم یک آهنگ/ویدیو از سایت‌های پشتیبانی‌شده بفرستید."
    )


async def check_sub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id
    is_member = await check_subscription(user_id, context)

    if is_member:
        await query.edit_message_text(
            "✅ عضویت تایید شد.\n"
            "حالا نام آهنگ یا خواننده را بفرستید."
        )
    else:
        await query.answer(
            "❌ هنوز عضویت شما تایید نشده است.",
            show_alert=True
        )


async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    if not await require_subscription(update, context):
        return

    user_id = update.effective_user.id
    query_text = update.message.text.strip()

    if len(query_text) < 2:
        await update.message.reply_text(
            "❌ نام آهنگ یا خواننده خیلی کوتاه است."
        )
        return

    # Rate limit ساده
    now = time.monotonic()
    previous = last_search_time.get(user_id, 0)

    if now - previous < SEARCH_COOLDOWN_SECONDS:
        remaining = int(SEARCH_COOLDOWN_SECONDS - (now - previous)) + 1

        await update.message.reply_text(
            f"⏳ لطفاً {remaining} ثانیه صبر کنید و دوباره جستجو کنید."
        )
        return

    last_search_time[user_id] = now

    processing_msg = await update.message.reply_text(
        "🔍 در حال جستجو در چند منبع..."
    )

    try:
        results = await search_music(query_text)

        if not results:
            await processing_msg.edit_text(
                "❌ نتیجه‌ای پیدا نشد.\n"
                "نام آهنگ و خواننده را دقیق‌تر بنویسید."
            )
            return

        # نتایج فقط برای همین کاربر ذخیره می‌شود
        context.user_data["music_search_results"] = results

        keyboard = []

        for index, item in enumerate(results):
            title = item["title"].replace("\n", " ").strip()

            if len(title) > 45:
                title = title[:42] + "..."

            duration = format_duration(item.get("duration"))
            source = item["source"]

            rank = index + 1
            button_text = f"{rank}. 🎵 {title}"

            if duration:
                button_text += f" · {duration}"

            button_text += f" [{source}]"

            keyboard.append([
                InlineKeyboardButton(
                    button_text,
                    callback_data=f"music_pick_{index}"
                )
            ])

        await processing_msg.edit_text(
            f"🔎 نتایج برای:\n{query_text}\n\n"
            "یکی را انتخاب کنید:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

    except Exception as e:
        print(f"Search handler error: {e}")

        await processing_msg.edit_text(
            "❌ هنگام جستجو خطایی رخ داد. کمی بعد دوباره امتحان کنید."
        )


async def music_pick_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not await require_subscription(update, context):
        return

    try:
        index = int(query.data.replace("music_pick_", ""))
    except ValueError:
        await query.edit_message_text("❌ انتخاب نامعتبر است.")
        return

    results = context.user_data.get("music_search_results") or []

    if index < 0 or index >= len(results):
        await query.edit_message_text(
            "❌ نتیجه جستجو منقضی شده است. دوباره جستجو کنید."
        )
        return

    selected = results[index]

    await send_downloaded_audio(
        update,
        context,
        selected,
    )


# ----------------------------
# Main
# ----------------------------

def main():
    global application, background_loop

    if not TOKEN:
        print("❌ TELEGRAM_TOKEN is not set!")
        return

    setup_cookies()

    application = ApplicationBuilder().token(TOKEN).build()

    application.add_handler(
        CommandHandler("start", start_handler)
    )

    application.add_handler(
        CallbackQueryHandler(
            check_sub_callback,
            pattern=r"^check_sub$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            music_pick_callback,
            pattern=r"^music_pick_\d+$"
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            message_handler
        )
    )

    # یک event loop دائمی برای Telegram
    background_loop = asyncio.new_event_loop()

    thread = threading.Thread(
        target=start_background_loop,
        args=(background_loop,),
        daemon=True,
    )
    thread.start()

    async def init_bot():
        await application.initialize()
        await application.start()

        if RENDER_EXTERNAL_URL:
            webhook_url = (
                f"{RENDER_EXTERNAL_URL.rstrip('/')}/{TOKEN}"
            )

            await application.bot.set_webhook(
                webhook_url,
                allowed_updates=Update.ALL_TYPES,
            )

            print(f"✅ Webhook set to: {webhook_url}")
        else:
            print(
                "⚠️ RENDER_EXTERNAL_URL is not set. "
                "Webhook was not configured."
            )

    future = asyncio.run_coroutine_threadsafe(
        init_bot(),
        background_loop,
    )

    future.result()

    port = int(os.environ.get("PORT", 10000))

    print(f"🚀 Bot server starting on port {port}")

    app.run(
        host="0.0.0.0",
        port=port,
        threaded=True,
    )


if __name__ == "__main__":
    main()
