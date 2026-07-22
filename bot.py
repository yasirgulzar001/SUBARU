import asyncio
import random
import re
import time
import os
from urllib.parse import quote, urlparse, parse_qs, unquote
from io import BytesIO

import tls_client
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# ==================== CONFIG ====================
BOT_TOKEN = "8939889745:AAEFORAmnxmL48jGS7hzxOjnQaAGW9MejLI"
AUTHORIZED_USER = 0  # Your Telegram ID (0 = allow anyone in pvt)

# ==================== BLACKLIST ====================
BLACKLIST = [
    "google.", "facebook.", "youtube.", "twitter.", "instagram.",
    "linkedin.", "microsoft.", "apple.", "amazon.", "wikipedia.",
    "yahoo.", "bing.", "pinterest.", "reddit.", "tumblr.",
    "wordpress.com", "blogspot.", "adobe.", "cloudflare.",
    "gstatic.", "googleapis.", "googleusercontent.", "doubleclick.",
    "fbcdn.", "akamai.", "w3.org", "schema.org", "mozilla.",
    "github.com", "stackoverflow.", "medium.com", "quora.",
    "yandex.", "duckduckgo.", "baidu.", "ask.com", "aol.",
    "office.com", "live.com", "bing.net", "msn.com", "whatsapp.",
    "telegram.", "t.co", "bit.ly", "goo.gl", "tinyurl.",
    "paypal.com", "ebay.com", "netflix.", "spotify.", "twitch.",
]

# ==================== TLS PROFILES (INFINITE ROTATION) ====================
TLS_PROFILES = [
    "chrome_103", "chrome_104", "chrome_105", "chrome_106",
    "chrome_107", "chrome_108", "chrome_109", "chrome_110",
    "chrome_111", "chrome_112", "chrome_116", "chrome_117",
    "chrome_118", "chrome_119", "chrome_120",
    "firefox_102", "firefox_104", "firefox_105", "firefox_106",
    "firefox_108", "firefox_110", "firefox_117", "firefox_120",
    "opera_89", "opera_90", "opera_91",
    "safari_15_3", "safari_15_6_1", "safari_16_0",
    "safari_ios_15_5", "safari_ios_15_6", "safari_ios_16_0",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:119.0) Gecko/20100101 Firefox/119.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Safari/605.1.15",
]

# ==================== TLS ROTATOR ====================
class InfiniteTLSRotator:
    """Advanced infinite TLS session rotation - creates fresh fingerprints on demand."""

    def __init__(self):
        self._counter = 0

    def new_session(self):
        self._counter += 1
        profile = random.choice(TLS_PROFILES)
        session = tls_client.Session(
            client_identifier=profile,
            random_tls_extension_order=True,  # infinite fingerprint variety
        )
        return session

    def headers(self):
        return {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": random.choice(["en-US,en;q=0.9", "en-GB,en;q=0.8", "en;q=0.7"]),
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": random.choice(["none", "same-origin"]),
            "Cache-Control": "max-age=0",
        }


tls_rotator = InfiniteTLSRotator()

# ==================== YAHOO PARSER ====================
class YahooParser:
    def __init__(self, pages=10, concurrency=15):
        self.pages = pages
        self.concurrency = concurrency
        self.sem = asyncio.Semaphore(concurrency)

    def is_blacklisted(self, url):
        low = url.lower()
        return any(b in low for b in BLACKLIST)

    def clean_yahoo_url(self, href):
        """Extract real URL from Yahoo redirect links."""
        try:
            if "/RU=" in href:
                m = re.search(r"/RU=([^/]+)/RK=", href)
                if m:
                    return unquote(m.group(1))
            if href.startswith("http"):
                return href
        except Exception:
            pass
        return None

    def _fetch_page(self, dork, page):
        """Blocking fetch for one page (runs in thread)."""
        offset = (page * 10) + 1
        query = quote(dork)
        url = f"https://search.yahoo.com/search?p={query}&b={offset}&pz=10"
        try:
            session = tls_rotator.new_session()
            resp = session.get(url, headers=tls_rotator.headers(), timeout_seconds=20)
            if resp.status_code != 200:
                return []
            return self._extract(resp.text)
        except Exception:
            return []

    def _extract(self, html):
        urls = set()
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.find_all("a", href=True):
            href = a["href"]
            real = self.clean_yahoo_url(href)
            if not real:
                continue
            if not real.startswith("http"):
                continue
            if self.is_blacklisted(real):
                continue
            # normalize
            real = real.strip().rstrip("/")
            urls.add(real)
        return list(urls)

    async def fetch_page(self, dork, page):
        async with self.sem:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, self._fetch_page, dork, page)
            await asyncio.sleep(random.uniform(0.3, 1.0))
            return result

    async def process_dork(self, dork):
        tasks = [self.fetch_page(dork, p) for p in range(self.pages)]
        results = await asyncio.gather(*tasks)
        urls = set()
        for r in results:
            urls.update(r)
        return urls


# ==================== SESSION STATE ====================
class UserSession:
    def __init__(self):
        self.running = False
        self.stop_flag = False
        self.pages = 10
        self.dorks = []
        self.found_urls = set()
        self.current_dork = 0
        self.total_dorks = 0
        self.status_msg = None


sessions = {}


def get_session(uid):
    if uid not in sessions:
        sessions[uid] = UserSession()
    return sessions[uid]


# ==================== AUTH ====================
def authorized(uid):
    return AUTHORIZED_USER == 0 or uid == AUTHORIZED_USER


# ==================== COMMANDS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    text = (
        "🔎 *Yahoo Mass Dork Parser*\n\n"
        "⚡ Proxyless | Infinite TLS Rotation\n"
        "🚀 Handles 20k-30k dorks\n\n"
        "*Commands:*\n"
        "/pages `<n>` - Set max pages (1-50)\n"
        "/status - Show live progress\n"
        "/stop - Stop & get results\n"
        "/reset - Clear session\n"
        "/help - Show help\n\n"
        "📤 Upload a `.txt` file (1 dork per line) to begin."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    await start(update, context)


async def set_pages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    s = get_session(update.effective_user.id)
    try:
        n = int(context.args[0])
        if n < 1 or n > 50:
            await update.message.reply_text("⚠️ Pages must be between 1 and 50.")
            return
        s.pages = n
        await update.message.reply_text(f"✅ Max pages set to *{n}*.", parse_mode="Markdown")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: `/pages 40`", parse_mode="Markdown")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    s = get_session(update.effective_user.id)
    if not s.running:
        await update.message.reply_text("💤 No active scan.")
        return
    await update.message.reply_text(
        f"📊 *Live Status*\n\n"
        f"🔗 URLs: `{len(s.found_urls)}`\n"
        f"📝 Dork: `{s.current_dork}/{s.total_dorks}`\n"
        f"📄 Pages: `{s.pages}`",
        parse_mode="Markdown"
    )


async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    s = get_session(update.effective_user.id)
    if not s.running:
        await update.message.reply_text("💤 Nothing to stop.")
        return
    s.stop_flag = True
    await update.message.reply_text("🛑 Stopping... sending results shortly.")


async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id):
        return
    sessions[update.effective_user.id] = UserSession()
    await update.message.reply_text("♻️ Session reset.")


# ==================== FILE UPLOAD ====================
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid):
        return
    s = get_session(uid)
    if s.running:
        await update.message.reply_text("⚠️ A scan is already running. Use /stop first.")
        return

    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Please upload a `.txt` file.")
        return

    file = await doc.get_file()
    data = await file.download_as_bytearray()
    raw = data.decode("utf-8", errors="ignore")
    dorks = [d.strip() for d in raw.splitlines() if d.strip()]
    dorks = list(dict.fromkeys(dorks))  # dedupe

    if not dorks:
        await update.message.reply_text("❌ File is empty.")
        return

    s.dorks = dorks
    s.total_dorks = len(dorks)
    s.found_urls = set()
    s.current_dork = 0
    s.stop_flag = False

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🚀 Start Scan ({len(dorks)} dorks)", callback_data="start_scan")]
    ])
    await update.message.reply_text(
        f"✅ Loaded *{len(dorks)}* dorks.\n"
        f"📄 Pages per dork: *{s.pages}*\n\n"
        f"Press start to begin.",
        parse_mode="Markdown",
        reply_markup=kb
    )


# ==================== SCAN ENGINE ====================
async def run_scan(context, uid, chat_id):
    s = get_session(uid)
    s.running = True
    s.stop_flag = False

    parser = YahooParser(pages=s.pages, concurrency=20)

    s.status_msg = await context.bot.send_message(
        chat_id,
        "🔎 *Scanning...*\n\nInitializing...",
        parse_mode="Markdown"
    )

    last_update = 0
    for i, dork in enumerate(s.dorks, 1):
        if s.stop_flag:
            break
        s.current_dork = i
        try:
            urls = await parser.process_dork(dork)
            s.found_urls.update(urls)
        except Exception:
            pass

        # Live update every 2 seconds
        now = time.time()
        if now - last_update > 2:
            last_update = now
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=s.status_msg.message_id,
                    text=(
                        f"🔎 *Live Results*\n\n"
                        f"🔗 URL `{len(s.found_urls)}`\n"
                        f"📝 Dork `{s.current_dork}/{s.total_dorks}`\n"
                        f"📄 Pages `{s.pages}`"
                    ),
                    parse_mode="Markdown"
                )
            except Exception:
                pass

    await finish_scan(context, uid, chat_id)


async def finish_scan(context, uid, chat_id):
    s = get_session(uid)
    s.running = False

    urls = sorted(s.found_urls)
    if not urls:
        await context.bot.send_message(chat_id, "❌ No URLs found.")
        return

    content = "\n".join(urls)
    buf = BytesIO(content.encode("utf-8"))
    buf.name = f"yahoo_results_{int(time.time())}.txt"

    await context.bot.send_message(
        chat_id,
        f"✅ *Scan Complete!*\n\n"
        f"🔗 Total URLs: `{len(urls)}`\n"
        f"📝 Dorks processed: `{s.current_dork}/{s.total_dorks}`\n"
        f"📄 Pages: `{s.pages}`",
        parse_mode="Markdown"
    )
    await context.bot.send_document(chat_id, document=buf, filename=buf.name)


# ==================== CALLBACK ====================
async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if not authorized(uid):
        return
    await query.answer()

    if query.data == "start_scan":
        s = get_session(uid)
        if s.running:
            await query.edit_message_text("⚠️ Already running.")
            return
        if not s.dorks:
            await query.edit_message_text("❌ No dorks loaded. Upload a file.")
            return
        await query.edit_message_text("🚀 Scan started! Live results below 👇")
        asyncio.create_task(run_scan(context, uid, query.message.chat_id))


# ==================== MAIN ====================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pages", set_pages))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(CallbackQueryHandler(button_handler))

    print("🚀 Yahoo Dork Parser Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
