import threading
from flask import Flask
import os
import re
import time
import uuid
import shutil
import asyncio
import unicodedata
import json
from difflib import SequenceMatcher
from pathlib import Path
from io import BytesIO

import requests
import yt_dlp
from dotenv import load_dotenv
from PIL import Image, ImageDraw, ImageFont

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "@Delgraphyha").strip()
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "").strip()

DOWNLOAD_ROOT = Path("downloads")
DOWNLOAD_ROOT.mkdir(exist_ok=True)
SEARCH_RESULTS_PER_SOURCE = 8
SEARCH_COOLDOWN_SECONDS = 5
last_search_time = {}
COOKIES_FILE = None

LIBRARY_FILE = Path("music_library.json")  # local cache only
LIBRARY_CHANNELS = {
    "@delgraphyha": "Delgraphyha",
    "@ahangzibamusic": "Ahang Ziba",
}
LIBRARY_INDEX_CHAT_ID = os.getenv("LIBRARY_INDEX_CHAT_ID", "").strip()

COLORS = {
    "white": (255, 255, 255, 255),
    "black": (0, 0, 0, 255),
    "gold": (212, 175, 55, 255),
    "red": (220, 35, 55, 255),
}

TEXT_STYLES = {
    "nastaliq": "🖋 نستعلیق",
    "hand": "✍️ دست‌نویس",
    "modern": "🔷 مدرن",
    "bold": "🔠 بولد",
}


def youtube_cookie_file():
    """Return a cookie file path for yt-dlp without storing secrets in GitHub."""
    env_cookies = os.getenv("YOUTUBE_COOKIES", "").strip()
    if env_cookies:
        runtime_cookie = Path("youtube_cookies_runtime.txt")
        # Render env vars may contain literal \\n instead of real newlines.
        content = env_cookies.replace("\\r\\n", "\n").replace("\\n", "\n")
        runtime_cookie.write_text(content, encoding="utf-8")
        return str(runtime_cookie)

    local_cookie = Path("cookies.txt")
    if local_cookie.exists():
        return str(local_cookie)

    return None

def font_path(style="modern"):
    env_font = os.getenv("PERSIAN_FONT")
    base = Path(__file__).resolve().parent

    candidates_by_style = {
        "nastaliq": [
            os.getenv("PERSIAN_FONT_NASTALIQ"),
            str(base / "fonts" / "nastaliq.ttf"),
            "C:/Windows/Fonts/IranNastaliq.ttf",
            env_font,
            "C:/Windows/Fonts/tahoma.ttf",
        ],
        "handwriting": [
            os.getenv("PERSIAN_FONT_HAND"),
            str(base / "fonts" / "handwriting.ttf"),
            env_font,
            "C:/Windows/Fonts/segoepr.ttf",
            "C:/Windows/Fonts/tahoma.ttf",
        ],
        "modern": [
            os.getenv("PERSIAN_FONT_MODERN"),
            str(base / "fonts" / "modern.ttf"),
            env_font,
            "C:/Windows/Fonts/tahoma.ttf",
            "C:/Windows/Fonts/arial.ttf",
        ],
        "bold": [
            os.getenv("PERSIAN_FONT_BOLD"),
            str(base / "fonts" / "bold.ttf"),
            env_font,
            "C:/Windows/Fonts/tahomabd.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/tahoma.ttf",
        ],
    }

    for p in candidates_by_style.get(style, candidates_by_style["modern"]):
        if p and Path(p).exists():
            return p
    raise FileNotFoundError(f"فونت مناسب برای مدل {TEXT_STYLES.get(style, style)} پیدا نشد.")


def main_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🎵 جستجو و دریافت موزیک", callback_data="menu_music")],
        [InlineKeyboardButton("✍️ ساخت نوشته فارسی", callback_data="menu_text")],
        [InlineKeyboardButton("🎙️ تبدیل متن فارسی به صدا", callback_data="menu_voice")],
    ])

def back_menu():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu_home")]])

def sub_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 عضویت در Delgraphyha",
                              url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
        [InlineKeyboardButton("✅ عضو شدم، بررسی کن", callback_data="check_sub")],
    ])

async def is_member(user_id, context):
    try:
        m = await context.bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return m.status in {
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
        }
    except Exception as e:
        print("Membership check:", repr(e))
        return False

async def require_member(update, context):
    if await is_member(update.effective_user.id, context):
        return True
    msg = "❤️ برای استفاده از امکانات بات ابتدا عضو @Delgraphyha شوید."
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(msg, reply_markup=sub_menu())
        except Exception:
            await update.callback_query.message.reply_text(msg, reply_markup=sub_menu())
    else:
        await update.effective_message.reply_text(msg, reply_markup=sub_menu())
    return False

def setup_cookies():
    global COOKIES_FILE
    content = os.getenv("YOUTUBE_COOKIES", "").strip()
    if content:
        Path("cookies.txt").write_text(content, encoding="utf-8")
        COOKIES_FILE = "cookies.txt"
    elif Path("cookies.txt").exists():
        COOKIES_FILE = "cookies.txt"

def ydl_base():
    o = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "js_runtimes": {"node": {"path": "/usr/local/bin/node"}},
        "remote_components": {"ejs:npm"},
        "extractor_args": {
            "youtube": {"player_client": ["mweb"]},
            "youtubepot-bgutilhttp": {"base_url": ["http://127.0.0.1:4416"]},
        },
        "ignoreerrors": True, "socket_timeout": 30,
        "retries": 3, "fragment_retries": 3,
    }
    if COOKIES_FILE:
        o["cookiefile"] = COOKIES_FILE
    return o

def is_url(s):
    return bool(re.match(r"^https?://", s.strip(), re.I))

def duration_text(v):
    try:
        v = int(v)
    except (TypeError, ValueError):
        return ""
    m, s = divmod(v, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def clean_music_text(s):
    s = unicodedata.normalize("NFKC", (s or "").lower())
    s = re.sub(r"[@#_\-–—|]+", " ", s)
    return " ".join(re.sub(r"[^\w\s\u0600-\u06ff]", " ", s).split())

def load_music_library():
    if not LIBRARY_FILE.exists():
        return []
    try:
        data = json.loads(LIBRARY_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception as e:
        print("Library load:", repr(e))
        return []

def save_music_library(items):
    LIBRARY_FILE.write_text(
        json.dumps(items, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

def upsert_library_track(track):
    items = load_music_library()
    key = (str(track.get("chat_id")), str(track.get("message_id")))
    replaced = False
    for i, old in enumerate(items):
        if (str(old.get("chat_id")), str(old.get("message_id"))) == key:
            items[i] = track
            replaced = True
            break
    if not replaced:
        items.append(track)
    save_music_library(items)
    return len(items)

def library_score(query, track):
    q = clean_music_text(query)
    title = clean_music_text(track.get("title"))
    performer = clean_music_text(track.get("performer"))
    caption = clean_music_text(track.get("caption"))
    channel = clean_music_text(track.get("channel"))
    hay = " ".join(x for x in [title, performer, caption, channel] if x)
    if not q or not hay:
        return 0
    qw = set(q.split())
    hw = set(hay.split())
    s = SequenceMatcher(None, q, hay).ratio() * 45
    s += SequenceMatcher(None, q, title).ratio() * 35
    if q in hay:
        s += 45
    if qw:
        s += len(qw & hw) / len(qw) * 55
    # Brand words should strongly favor our own library.
    if "delgraphyha" in q and "delgraphyha" in hay:
        s += 60
    if any(w in q for w in ("ahangzibamusic", "ahang", "ziba")) and track.get("channel_username") == "@ahangzibamusic":
        s += 35
    return s

def search_library(query, limit=8):
    scored = []
    for track in load_music_library():
        s = library_score(query, track)
        if s >= 45:
            x = dict(track)
            x["score"] = s
            x["source"] = "Library"
            scored.append(x)
    return sorted(scored, key=lambda x: x["score"], reverse=True)[:limit]

def audio_track_from_message(msg, channel_username=None):
    audio = getattr(msg, "audio", None)
    doc = getattr(msg, "document", None)
    if not audio and not (doc and (doc.mime_type or "").startswith("audio/")):
        return None
    media = audio or doc
    title = getattr(audio, "title", None) or getattr(doc, "file_name", None) or "Music"
    performer = getattr(audio, "performer", None) or ""
    caption = msg.caption or ""
    username = channel_username or (f"@{msg.chat.username.lower()}" if msg.chat.username else "")
    return {
        "title": title,
        "performer": performer,
        "caption": caption,
        "duration": getattr(media, "duration", None),
        "file_id": media.file_id,
        "file_unique_id": media.file_unique_id,
        "chat_id": msg.chat_id,
        "message_id": msg.message_id,
        "channel_username": username.lower(),
        "channel": LIBRARY_CHANNELS.get(username.lower(), username or "Delgraphyha Library"),
    }

async def export_library_cmd(update, context):
    if not await require_member(update, context):
        return
    items = await asyncio.to_thread(load_music_library)
    if not items:
        await update.effective_message.reply_text("📚 کتابخانه خالی است.")
        return
    data = json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8")
    bio = BytesIO(data)
    bio.name = "music_library.json"
    await update.effective_message.reply_document(
        document=bio,
        filename="music_library.json",
        caption=f"💾 بکاپ کتابخانه موزیک | {len(items)} آهنگ"
    )

async def import_library_backup(update, context):
    msg = update.message
    if not msg or not msg.document or msg.document.file_name != "music_library.json":
        return
    try:
        tgfile = await msg.document.get_file()
        data = await tgfile.download_as_bytearray()
        items = json.loads(bytes(data).decode("utf-8"))
        if not isinstance(items, list):
            raise ValueError("invalid library")
        await asyncio.to_thread(save_music_library, items)
        await msg.reply_text(f"✅ بکاپ کتابخانه بازیابی شد: {len(items)} آهنگ")
    except Exception as e:
        print("Library restore:", repr(e))
        await msg.reply_text("❌ فایل بکاپ کتابخانه معتبر نیست.")

async def capture_channel_audio(update, context):
    msg = update.channel_post
    if not msg:
        return
    username = f"@{msg.chat.username.lower()}" if msg.chat.username else ""
    if username not in LIBRARY_CHANNELS:
        return
    track = audio_track_from_message(msg, username)
    if not track:
        return
    count = await asyncio.to_thread(upsert_library_track, track)
    print(f"Library: saved {track['title']} from {username}; total={count}")

async def import_forwarded_audio(update, context):
    """Import an old channel audio by forwarding it to the bot in a private chat."""
    msg = update.message
    if not msg:
        return
    origin = getattr(msg, "forward_origin", None)
    origin_chat = getattr(origin, "chat", None)
    username = f"@{origin_chat.username.lower()}" if origin_chat and origin_chat.username else ""
    if username not in LIBRARY_CHANNELS:
        return
    track = audio_track_from_message(msg, username)
    if not track:
        return
    count = await asyncio.to_thread(upsert_library_track, track)
    await msg.reply_text(f"✅ به کتابخانه اضافه شد: {track['title']}\n📚 تعداد ثبت‌شده: {count}")

def normalize_entry(e, source):
    if not e:
        return None
    url = e.get("webpage_url")
    if source == "YouTube" and not url and e.get("id"):
        url = f"https://www.youtube.com/watch?v={e['id']}"
    if source == "SoundCloud" and not url and str(e.get("url", "")).startswith("http"):
        url = e["url"]
    if not url:
        return None
    return {
        "title": e.get("title") or "Unknown",
        "uploader": e.get("uploader") or e.get("channel") or e.get("artist") or "",
        "duration": e.get("duration"),
        "source": source,
        "url": url,
    }

def search_source(q, prefix, source):
    o = ydl_base()
    o.update({"skip_download": True, "extract_flat": True})
    try:
        with yt_dlp.YoutubeDL(o) as y:
            info = y.extract_info(f"{prefix}{SEARCH_RESULTS_PER_SOURCE}:{q}", download=False)
        return [x for e in ((info or {}).get("entries") or [])
                if (x := normalize_entry(e, source))]
    except Exception as e:
        print(f"Search {source}:", repr(e))
        return []

def direct_info(url):
    o = ydl_base()
    o.update({"skip_download": True})
    try:
        with yt_dlp.YoutubeDL(o) as y:
            e = y.extract_info(url, download=False)
        if e and e.get("entries"):
            e = e["entries"][0]
        if not e:
            return None
        return {
            "title": e.get("title") or "Unknown",
            "uploader": e.get("uploader") or e.get("channel") or "",
            "duration": e.get("duration"),
            "source": e.get("extractor_key") or "Direct",
            "url": e.get("webpage_url") or url,
        }
    except Exception as e:
        print("Direct URL:", repr(e))
        return None

async def search_music(q):
    if is_url(q):
        x = await asyncio.to_thread(direct_info, q)
        return [x] if x else []

    def clean(s):
        s = unicodedata.normalize("NFKC", (s or "").lower())
        return " ".join(re.sub(r"[^\w\s]", " ", s).split())

    def score(x):
        q1, t, u = clean(q), clean(x["title"]), clean(x["uploader"])
        hay = f"{t} {u}"
        qw, tw, hw = set(q1.split()), set(t.split()), set(hay.split())
        s = SequenceMatcher(None, q1, t).ratio() * 55
        s += SequenceMatcher(None, q1, hay).ratio() * 20
        if q1 == t: s += 35
        elif q1 in t: s += 22
        elif q1 in hay: s += 12
        if qw:
            s += len(qw & tw) / len(qw) * 35
            s += len(qw & hw) / len(qw) * 15
        bad = {"karaoke":14,"reaction":18,"tutorial":18,"cover":8,
               "remix":6,"slowed":8,"reverb":8,"instrumental":6}
        for w, p in bad.items():
            if w in tw and w not in qw: s -= p
        if x["source"] == "YouTube": s += 5
        return s

    # General/public music search uses SoundCloud only.
    # Delgraphyha and @ahangzibamusic will later be resolved from our own library first.
    items = await asyncio.to_thread(search_source, q, "scsearch", "SoundCloud")
    out, seen = [], set()
    for x in items:
        if x["url"] in seen: continue
        seen.add(x["url"])
        x["score"] = score(x)
        out.append(x)
    return sorted(out, key=lambda z: z["score"], reverse=True)[:8]

def download_audio(url, user_id):
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg روی سیستم پیدا نشد.")
    d = DOWNLOAD_ROOT / str(user_id) / uuid.uuid4().hex[:10]
    d.mkdir(parents=True, exist_ok=True)
    o = ydl_base()
    o.update({
        "format": "bestaudio/best",
        "outtmpl": str(d / "track.%(ext)s"),
        "ignoreerrors": False,

        # Temporary Render diagnostics. These messages go only to Render Logs.
        # Cookie contents are never printed.
        "quiet": False,
        "no_warnings": False,
        "verbose": True,

        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "128",
        }],
    })

    print("\n===== DELGRAPHYHA YT-DLP DIAGNOSTIC START =====")
    print("URL:", url)
    print("Cookies:", "loaded" if COOKIES_FILE and Path(COOKIES_FILE).exists() else "missing")
    print("FFmpeg:", shutil.which("ffmpeg") or "missing")

    try:
        with yt_dlp.YoutubeDL(o) as y:
            info = y.extract_info(url, download=True)
        files = list(d.glob("*.mp3"))
        if not files:
            raise RuntimeError("فایل MP3 ساخته نشد.")
        print("Diagnostic result: SUCCESS")
        print("===== DELGRAPHYHA YT-DLP DIAGNOSTIC END =====\n")
        return files[0], (info or {}).get("title") or "Music", d
    except Exception as e:
        print("Diagnostic result: FAILED")
        print("Exception:", repr(e))
        print("===== DELGRAPHYHA YT-DLP DIAGNOSTIC END =====\n")
        shutil.rmtree(d, ignore_errors=True)
        raise


def _text_width(draw, text, font):
    b = draw.textbbox((0, 0), text or " ", font=font, direction="rtl",
                      language="fa", stroke_width=2)
    return max(1, b[2] - b[0])

def _wrap_rtl_text(text, font, max_width=1400):
    """Wrap Persian text by rendered width, preserving paragraphs and words."""
    probe = ImageDraw.Draw(Image.new("RGBA", (10, 10), (0, 0, 0, 0)))
    lines = []
    paragraphs = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for para in paragraphs:
        para = para.strip()
        if not para:
            lines.append("")
            continue
        words = para.split()
        current = ""
        for word in words:
            candidate = word if not current else current + " " + word
            if _text_width(probe, candidate, font) <= max_width:
                current = candidate
            else:
                if current:
                    lines.append(current)
                # Extremely long single token: keep it intact rather than
                # cutting Persian letters in the middle.
                current = word
        if current:
            lines.append(current)
    return lines or [""]

def make_png_pages(text, color, style="modern"):
    """Return one or more transparent PNGs for short or long Persian text."""
    fp = font_path(style)
    if not fp:
        raise RuntimeError("فونت فارسی پیدا نشد.")

    # Distinct sizing helps each typeface keep its intended character.
    sizes = {"nastaliq": 108, "handwriting": 100, "modern": 94, "bold": 96}
    font = ImageFont.truetype(fp, sizes.get(style, 96))

    max_text_width = 1400
    max_lines_per_page = 7
    spacing = 28 if style == "nastaliq" else 22
    pad_x, pad_y = 70, 60

    lines = _wrap_rtl_text(text, font, max_text_width=max_text_width)
    chunks = [lines[i:i + max_lines_per_page]
              for i in range(0, len(lines), max_lines_per_page)]

    pages = []
    for page_no, page_lines in enumerate(chunks, 1):
        tmp = Image.new("RGBA", (10, 10), (0, 0, 0, 0))
        d = ImageDraw.Draw(tmp)
        boxes = [
            d.textbbox((0, 0), line or " ", font=font, direction="rtl",
                       language="fa", stroke_width=2)
            for line in page_lines
        ]
        widths = [max(1, b[2]-b[0]) for b in boxes]
        heights = [max(1, b[3]-b[1]) for b in boxes]
        w = int(max(300, min(max_text_width, max(widths)) + 2*pad_x))
        h = int(max(160, sum(heights) + spacing*max(0,len(page_lines)-1) + 2*pad_y))

        im = Image.new("RGBA", (w, h), (0,0,0,0))
        d = ImageDraw.Draw(im)
        y = pad_y
        for line, box, lh in zip(page_lines, boxes, heights):
            d.text(
                (w/2, y-box[1]), line or " ",
                font=font, fill=COLORS[color], anchor="ma",
                direction="rtl", language="fa",
                stroke_width=2,
                stroke_fill=(0,0,0,80) if color=="white" else (255,255,255,60),
            )
            y += lh + spacing

        bio = BytesIO()
        im.save(bio, "PNG")
        bio.seek(0)
        bio.name = f"delgraphyha_text_{page_no:02d}.png"
        pages.append(bio)
    return pages

def make_png(text, color, style="modern"):
    # Backward-compatible helper for any older call site.
    return make_png_pages(text, color, style)[0]

def make_voice(text):
    if not ELEVENLABS_API_KEY or not ELEVENLABS_VOICE_ID:
        raise RuntimeError("تنظیم ElevenLabs هنوز انجام نشده است.")
    r = requests.post(
        f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVENLABS_VOICE_ID}",
        headers={"xi-api-key": ELEVENLABS_API_KEY,
                 "Content-Type":"application/json","Accept":"audio/mpeg"},
        json={"text":text,"model_id":"eleven_multilingual_v2"},
        timeout=90,
    )
    if not r.ok:
        raise RuntimeError(f"ElevenLabs HTTP {r.status_code}")
    b = BytesIO(r.content); b.name = "delgraphyha_voice.mp3"; return b

async def start(update, context):
    context.user_data.clear()
    if not await require_member(update, context): return
    await update.effective_message.reply_text(
        "🎬 Delgraphyha Studio | دلگرافیها\n\nابزار موردنظر را انتخاب کنید:",
        reply_markup=main_menu())

async def command_mode(update, context, mode):
    if not await require_member(update, context): return
    context.user_data["mode"] = mode
    p = {"music":"🎵 نام آهنگ، خواننده یا لینک را بفرست.",
         "text":"✍️ متن فارسی را بفرست.",
         "voice":"🎙️ متن فارسی Voice-over را بفرست."}
    await update.effective_message.reply_text(p[mode], reply_markup=back_menu())

async def callback(update, context):
    q = update.callback_query
    await q.answer()

    if q.data == "check_sub":
        if await is_member(q.from_user.id, context):
            context.user_data.clear()
            await q.edit_message_text("✅ عضویت تأیید شد.", reply_markup=main_menu())
        else:
            await q.answer("❌ هنوز عضویت تأیید نشده است.", show_alert=True)
        return

    if not await require_member(update, context): return

    if q.data == "menu_home":
        context.user_data.clear()
        # Send a fresh Home message so Telegram jumps to the bottom of the chat.
        try:
            await q.message.reply_text(
                "🏠 به صفحه اصلی برگشتید\n\n🎬 Delgraphyha Studio | دلگرافیها\n\nابزار را انتخاب کنید:",
                reply_markup=main_menu()
            )
        except Exception:
            await q.edit_message_text(
                "🏠 به صفحه اصلی برگشتید\n\n🎬 Delgraphyha Studio | دلگرافیها\n\nابزار را انتخاب کنید:",
                reply_markup=main_menu()
            )
        return

    if q.data.startswith("menu_"):
        mode = q.data[5:]
        context.user_data["mode"] = mode
        p = {"music":"🎵 نام آهنگ، خواننده یا لینک را بفرست.",
             "text":"✍️ متن فارسی را بفرست؛ بعد نوع خط و رنگ را انتخاب می‌کنی.",
             "voice":"🎙️ متن فارسی Voice-over را بفرست."}
        await q.edit_message_text(p[mode], reply_markup=back_menu())
        return

    if q.data.startswith("music_pick_"):
        try:
            i = int(q.data.rsplit("_",1)[1])
            x = context.user_data["results"][i]
        except Exception:
            await q.edit_message_text("❌ نتیجه منقضی شده. دوباره جستجو کن.", reply_markup=back_menu())
            return
        work = None
        try:
            if x.get("source") == "Library":
                caption = f"🎵 {x.get('title','Music')}"
                if x.get("performer"):
                    caption += f"\n🎤 {x['performer']}"
                caption += f"\n📚 {x.get('channel','Delgraphyha')}"
                await context.bot.send_audio(
                    q.message.chat_id,
                    audio=x["file_id"],
                    title=x.get("title") or "Music",
                    performer=x.get("performer") or None,
                    caption=caption,
                )
                await q.edit_message_text("✅ موزیک از کتابخانه ارسال شد.", reply_markup=back_menu())
                return

            results = context.user_data.get("results") or []
            # Start with the selected result, then try the remaining ranked
            # SoundCloud results if a candidate cannot be downloaded (e.g. DRM).
            candidates = []
            seen_urls = set()
            if x.get("source") == "SoundCloud":
                candidates.append(x)
                seen_urls.add(x.get("url"))
            for alt in results:
                if alt.get("source") == "SoundCloud" and alt.get("url") not in seen_urls:
                    candidates.append(alt)
                    seen_urls.add(alt.get("url"))
            if not candidates:
                raise RuntimeError("هیچ نتیجه SoundCloud برای این جستجو پیدا نشد.")

            last_error = None
            for pos, candidate in enumerate(candidates, 1):
                try:
                    if pos == 1:
                        msg = f"⬇️ در حال دریافت:\n{candidate['title']}"
                    else:
                        msg = f"🔄 نتیجه قبلی قابل دریافت نبود؛ امتحان نتیجه بعدی:\n{candidate['title']}"
                    await q.edit_message_text(msg)

                    path, title, work = await asyncio.to_thread(
                        download_audio, candidate["url"], q.from_user.id
                    )
                    with open(path, "rb") as f:
                        await context.bot.send_audio(
                            q.message.chat_id, f, title=title,
                            caption=f"🎵 {title}\n🤖 Delgraphyha Studio"
                        )
                    await q.edit_message_text("✅ موزیک ارسال شد.", reply_markup=back_menu())
                    return
                except Exception as e:
                    last_error = e
                    print(f"Download candidate {pos}/{len(candidates)}:", repr(e))
                    if work:
                        shutil.rmtree(work, ignore_errors=True)
                        work = None
                    continue

            raise last_error or RuntimeError("هیچ نتیجه قابل دانلودی پیدا نشد.")
        except Exception as e:
            print("Download:", repr(e))
            await q.edit_message_text(
                f"❌ هیچ نتیجه قابل دانلودی پیدا نشد.\n{str(e)[:160]}",
                reply_markup=back_menu()
            )
        finally:
            if work:
                shutil.rmtree(work, ignore_errors=True)
        return

    if q.data.startswith("style_"):
        context.user_data["text_style"] = q.data.replace("style_", "", 1)
        keyboard = [
            [InlineKeyboardButton("⚪ سفید", callback_data="color_white"),
             InlineKeyboardButton("⚫ مشکی", callback_data="color_black")],
            [InlineKeyboardButton("🟡 طلایی", callback_data="color_gold"),
             InlineKeyboardButton("🔴 قرمز", callback_data="color_red")],
            [InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu_home")],
        ]
        await q.edit_message_text("رنگ نوشته را انتخاب کن:", reply_markup=InlineKeyboardMarkup(keyboard))
        return

    if q.data.startswith("color_"):
        text = context.user_data.get("pending_text")
        if not text:
            await q.edit_message_text("❌ متن منقضی شده.", reply_markup=back_menu()); return
        try:
            pages = await asyncio.to_thread(
                make_png_pages,
                text,
                q.data[6:],
                context.user_data.get("text_style", "modern")
            )
            total = len(pages)
            for i, img in enumerate(pages, 1):
                caption = "✍️ PNG شفاف | Delgraphyha Studio"
                if total > 1:
                    caption += f"\n📄 صفحه {i} از {total}"
                await context.bot.send_document(
                    q.message.chat_id,
                    img,
                    filename=img.name,
                    caption=caption
                )
            await q.edit_message_text(
                f"✅ نوشته آماده شد." + (f" {total} صفحه ساخته شد." if total > 1 else ""),
                reply_markup=back_menu()
            )
        except Exception as e:
            await q.edit_message_text(f"❌ {e}", reply_markup=back_menu())

async def text_message(update, context):
    if not await require_member(update, context): return
    text = update.message.text.strip()
    mode = context.user_data.get("mode")
    if not mode:
        await update.message.reply_text("اول ابزار را انتخاب کن:", reply_markup=main_menu()); return

    if mode == "music":
        now = time.monotonic()
        if now-last_search_time.get(update.effective_user.id,0) < SEARCH_COOLDOWN_SECONDS:
            await update.message.reply_text("⏳ چند ثانیه صبر کن."); return
        last_search_time[update.effective_user.id] = now
        m = await update.message.reply_text("🔍 در حال جستجو...")
        results = await asyncio.to_thread(search_library, text)
        if results:
            await m.edit_text("📚 در کتابخانه Delgraphyha پیدا شد...")
        else:
            results = await search_music(text)
        if not results:
            await m.edit_text("❌ نتیجه‌ای پیدا نشد."); return
        context.user_data["results"] = results
        keys = []
        for i,x in enumerate(results):
            t = x["title"].replace("\n"," ")
            if len(t)>38: t=t[:35]+"..."
            dur=duration_text(x.get("duration"))
            keys.append([InlineKeyboardButton(
                f"{i+1}. {t}" + (f" · {dur}" if dur else ""),
                callback_data=f"music_pick_{i}")])
        keys.append([InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu_home")])
        await m.edit_text("🎵 یکی از نتایج را انتخاب کن:", reply_markup=InlineKeyboardMarkup(keys))

    elif mode == "text":
        context.user_data["pending_text"] = text
        context.user_data.pop("text_style", None)
        await update.message.reply_text(
            "✍️ نوع خط را انتخاب کن:",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🖋 نستعلیق", callback_data="style_nastaliq"),
                 InlineKeyboardButton("✍️ دست‌نویس", callback_data="style_handwriting")],
                [InlineKeyboardButton("🔷 مدرن", callback_data="style_modern"),
                 InlineKeyboardButton("🔠 بولد", callback_data="style_bold")],
                [InlineKeyboardButton("🏠 منوی اصلی", callback_data="menu_home")],
            ])
        )

    elif mode == "voice":
        m = await update.message.reply_text("🎙️ در حال ساخت صدا...")
        try:
            audio = await asyncio.to_thread(make_voice, text[:2500])
            await context.bot.send_audio(update.effective_chat.id, audio,
                                         filename="delgraphyha_voice.mp3")
            await m.edit_text("✅ صدا آماده شد.", reply_markup=back_menu())
        except Exception as e:
            await m.edit_text(f"❌ {e}", reply_markup=back_menu())

async def help_cmd(update, context):
    if not await require_member(update, context): return
    await update.effective_message.reply_text(
        "🎵 /music جستجو و دریافت موزیک\n"
        "✍️ /text ساخت PNG فارسی شفاف\n"
        "🎙️ /voice تبدیل متن به صدا\n"
        "🏠 /start منوی اصلی")

def main():
    if not TOKEN:
        raise SystemExit("❌ TELEGRAM_TOKEN در .env پیدا نشد.")
    setup_cookies()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("music", lambda u,c: command_mode(u,c,"music")))
    app.add_handler(CommandHandler("text", lambda u,c: command_mode(u,c,"text")))
    app.add_handler(CommandHandler("voice", lambda u,c: command_mode(u,c,"voice")))
    app.add_handler(CommandHandler("librarybackup", export_library_cmd))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.UpdateType.CHANNEL_POST, capture_channel_audio))
    app.add_handler(MessageHandler(
        (filters.AUDIO | filters.Document.AUDIO) & filters.FORWARDED,
        import_forwarded_audio
    ))
    app.add_handler(MessageHandler(
        filters.Document.FileExtension("json"),
        import_library_backup
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_message))
    print("🚀 Delgraphyha Studio is running...")
    print(f"📢 Required channel: {CHANNEL_USERNAME}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


health_app = Flask(__name__)

@health_app.get("/")
def health():
    return "Delgraphyha Studio Bot is running", 200

@health_app.get("/health")
def health_check():
    return {"status": "ok"}, 200

def start_pot_provider():
    """Start local bgutil PO-token provider in the same Render container."""
    import subprocess
    import time
    server_js = Path(__file__).resolve().parent / "bgutil-provider" / "server" / "build" / "main.js"
    if not server_js.exists():
        print("PO Token provider build not found:", server_js)
        return None
    try:
        proc = subprocess.Popen(
            ["node", str(server_js), "--host", "127.0.0.1", "--port", "4416"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        time.sleep(2)
        print("PO Token provider started on 127.0.0.1:4416")
        return proc
    except Exception as e:
        print("PO Token provider failed to start:", repr(e))
        return None


def run_health_server():
    port = int(os.getenv("PORT", "10000"))
    health_app.run(host="0.0.0.0", port=port, use_reloader=False)

if __name__ == "__main__":
    pot_provider_process = start_pot_provider()
    threading.Thread(target=run_health_server, daemon=True).start()
    main()
