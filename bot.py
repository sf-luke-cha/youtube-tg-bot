import asyncio
import logging
import os
import re
import shutil
import tempfile
import uuid
from pathlib import Path

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ytbot")

TOKEN = os.environ["BOT_TOKEN"]
MAX_BYTES = 50 * 1024 * 1024
SAFE_BYTES = 48 * 1024 * 1024
URL_RE = re.compile(r"https?://\S+")
COOKIES_SRC = os.environ.get("COOKIES_FILE", "/etc/secrets/cookies.txt")
COOKIES_FILE = "/tmp/cookies.txt"  # yt-dlp rewrites the file; Render's secrets dir is read-only
if os.environ.get("COOKIES_JSON"):
    import json

    lines = ["# Netscape HTTP Cookie File"]
    for c in json.loads(os.environ["COOKIES_JSON"]):
        lines.append("\t".join([
            c["domain"],
            "TRUE" if c["domain"].startswith(".") else "FALSE",
            c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(int(c.get("expirationDate", 0))),
            c["name"],
            c["value"],
        ]))
    Path(COOKIES_FILE).write_text("\n".join(lines) + "\n")
elif os.environ.get("COOKIES_B64"):
    import base64

    Path(COOKIES_FILE).write_bytes(base64.b64decode(os.environ["COOKIES_B64"]))
elif Path(COOKIES_SRC).is_file():
    shutil.copy(COOKIES_SRC, COOKIES_FILE)
elif Path("cookies.txt").is_file():
    shutil.copy("cookies.txt", COOKIES_FILE)
ALLOWED = {int(x) for x in os.environ.get("ALLOWED_USER_IDS", "").split(",") if x.strip()}

# url store: short id -> url (callback_data is limited to 64 bytes)
PENDING: dict[str, str] = {}
# one download at a time keeps us inside the free tier's 512 MB RAM
DL_LOCK = asyncio.Lock()


def allowed(update: Update) -> bool:
    return not ALLOWED or (update.effective_user and update.effective_user.id in ALLOWED)


def base_opts(outdir: str) -> dict:
    opts = {
        "outtmpl": f"{outdir}/%(title).80B.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": MAX_BYTES,
        "restrictfilenames": True,
    }
    if Path(COOKIES_FILE).is_file():
        opts["cookiefile"] = COOKIES_FILE
    return opts


def download(url: str, kind: str, outdir: str) -> Path:
    opts = base_opts(outdir)
    if kind == "audio":
        opts.update(
            format="bestaudio/best",
            postprocessors=[
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}
            ],
        )
    else:
        opts.update(
            format=(
                "bv*[height<=720][filesize<40M]+ba[filesize<8M]/"
                "bv*[height<=720][filesize_approx<40M]+ba[filesize_approx<8M]/"
                "bv*[height<=480]+ba/b[filesize<48M]/wv*+wa/w"
            ),
            merge_output_format="mp4",
        )
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    files = [p for p in Path(outdir).iterdir() if p.is_file()]
    if not files:
        raise RuntimeError("Nothing downloaded (file may be over 50 MB).")
    return max(files, key=lambda p: p.stat().st_size)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text(
        "Send me a video link (YouTube and most other sites) and choose Video or Audio.\n"
        "Max file size: 50 MB."
    )


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    m = URL_RE.search(update.message.text or "")
    if not m:
        await update.message.reply_text("Please send a valid link.")
        return
    key = uuid.uuid4().hex[:10]
    PENDING[key] = m.group(0)
    if len(PENDING) > 200:  # cap memory
        PENDING.pop(next(iter(PENDING)))
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🎬 Video", callback_data=f"video|{key}"),
            InlineKeyboardButton("🎵 Audio (mp3)", callback_data=f"audio|{key}"),
        ]]
    )
    await update.message.reply_text("What do you want?", reply_markup=kb)


async def on_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    kind, _, key = q.data.partition("|")
    url = PENDING.get(key)
    if not url:
        await q.edit_message_text("This request expired. Send the link again.")
        return
    if DL_LOCK.locked():
        await q.edit_message_text("Another download is running, waiting for my turn...")
    async with DL_LOCK:
        await q.edit_message_text(f"⏳ Downloading {kind}...")
        tmp = tempfile.mkdtemp(prefix="ytbot_")
        try:
            path = await asyncio.to_thread(download, url, kind, tmp)
            size = path.stat().st_size
            if size > MAX_BYTES:
                await q.edit_message_text(
                    f"File is {size / 1048576:.1f} MB, over Telegram's 50 MB bot limit. "
                    "Try Audio instead."
                )
                return
            await q.edit_message_text("📤 Uploading...")
            chat_id = q.message.chat_id
            with path.open("rb") as f:
                if kind == "audio":
                    await context.bot.send_audio(
                        chat_id, f, filename=path.name, read_timeout=120, write_timeout=120
                    )
                else:
                    await context.bot.send_video(
                        chat_id, f, filename=path.name, supports_streaming=True,
                        read_timeout=120, write_timeout=120,
                    )
            await q.delete_message()
        except Exception as e:
            log.exception("download failed")
            msg = str(e)
            if "Sign in to confirm" in msg or "bot" in msg.lower() and "confirm" in msg.lower():
                msg = "YouTube blocked this server (needs cookies). Try again later."
            await q.edit_message_text(f"❌ Failed: {msg[:300]}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            PENDING.pop(key, None)


def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler(["start", "help"], start))
    app.add_handler(CallbackQueryHandler(on_choice, pattern=r"^(video|audio)\|"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))

    base_url = os.environ.get("RENDER_EXTERNAL_URL") or os.environ.get("WEBHOOK_URL")
    if base_url:
        # Webhook mode: Telegram's request wakes the sleeping free Render service.
        port = int(os.environ.get("PORT", "10000"))
        secret = os.environ.get("WEBHOOK_SECRET") or TOKEN.split(":")[1][:32]
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=secret,
            webhook_url=f"{base_url.rstrip('/')}/{secret}",
            secret_token=secret,
            drop_pending_updates=True,
        )
    else:
        app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
