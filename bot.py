import asyncio
import io
import random
from urllib.parse import urlparse, parse_qs, unquote

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

# ---------- 🔐 YOUR CREDENTIALS (HARDCODED) ----------
BOT_TOKEN = "YOUR_BOT_TOKEN"                    # ⚠️ Paste your token here
ADMIN_USER_IDS = {6535041385}                   # Your Telegram user ID (add more if needed)
# ----------------------------------------------------

MAX_PAGES_LIMIT = 50
DEFAULT_MAX_PAGES = 50
MAX_CONCURRENT_REQUESTS = 400          # Increase if your IP can handle it
PROGRESS_INTERVAL = 2

BLACKLIST_DOMAINS = {
    "google.com", "youtube.com", "facebook.com", "instagram.com", "twitter.com",
    "linkedin.com", "pinterest.com", "reddit.com", "amazon.com", "ebay.com",
    "netflix.com", "microsoft.com", "apple.com", "wikipedia.org", "imdb.com",
    "etsy.com", "shopify.com", "bigcommerce.com", "yahoo.com", "bing.com",
    "tiktok.com", "whatsapp.com", "telegram.org", "discord.com", "slack.com",
    "twitch.tv", "spotify.com", "quora.com", "medium.com", "nytimes.com",
    "cnn.com", "bbc.com", "foxnews.com", "wsj.com", "washingtonpost.com",
    "github.com", "gitlab.com", "stackoverflow.com", "bitbucket.org",
    "adobe.com", "salesforce.com", "oracle.com", "ibm.com",
    "zoom.us", "webex.com", "gotomeeting.com",
}

IMPERSONATE_TARGETS = [
    "chrome99", "chrome100", "chrome101", "chrome104", "chrome107",
    "chrome110", "chrome116", "chrome119", "chrome120", "chrome123",
    "edge99", "edge101", "edge110", "edge120",
    "safari15_3", "safari15_5", "safari17_0",
    "firefox110", "firefox120",
]

YAHOO_SEARCH_DOMAINS = [
    "search.yahoo.com", "uk.search.yahoo.com", "de.search.yahoo.com",
    "fr.search.yahoo.com", "es.search.yahoo.com", "it.search.yahoo.com",
    "in.search.yahoo.com", "br.search.yahoo.com", "ca.search.yahoo.com",
    "au.search.yahoo.com", "mx.search.yahoo.com", "ar.search.yahoo.com",
    "nl.search.yahoo.com", "se.search.yahoo.com", "no.search.yahoo.com",
    "dk.search.yahoo.com", "fi.search.yahoo.com", "pl.search.yahoo.com",
    "ru.search.yahoo.com", "tr.search.yahoo.com",
]

def is_blacklisted(url: str) -> bool:
    try:
        netloc = urlparse(url).netloc.lower().split(":")[0]
        for d in BLACKLIST_DOMAINS:
            if netloc == d or netloc.endswith("." + d):
                return True
        return False
    except Exception:
        return True

async def extract_real_urls_from_html(html: str) -> set:
    soup = BeautifulSoup(html, "html.parser")
    urls = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "r.search.yahoo.com" in href and "RU=" in href:
            parsed = urlparse(href)
            qs = parse_qs(parsed.query)
            ru = qs.get("RU", [None])[0]
            if ru:
                real_url = unquote(ru)
                if real_url.startswith("http") and not is_blacklisted(real_url):
                    urls.add(real_url)
    return urls

async def fetch_page(session: AsyncSession, query: str, offset: int,
                     impersonate: str, domain: str, semaphore: asyncio.Semaphore):
    url = f"https://{domain}/search?p={query}&b={offset}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    async with semaphore:
        try:
            resp = await session.get(url, headers=headers, impersonate=impersonate, timeout=10)
            if resp.status_code == 200:
                return resp.text
        except Exception:
            pass
    return None

async def page_worker(queue, stop_event, semaphore, collected_urls, lock, shared_counts):
    session = AsyncSession()
    while not stop_event.is_set():
        try:
            dork, page_num = await asyncio.wait_for(queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            break
        offset = (page_num - 1) * 10 + 1
        impersonate = random.choice(IMPERSONATE_TARGETS)
        domain = random.choice(YAHOO_SEARCH_DOMAINS)
        html = await fetch_page(session, dork, offset, impersonate, domain, semaphore)
        if html is None:
            impersonate = random.choice(IMPERSONATE_TARGETS)
            domain = random.choice(YAHOO_SEARCH_DOMAINS)
            html = await fetch_page(session, dork, offset, impersonate, domain, semaphore)
        if html and "captcha" not in html.lower() and "unusual traffic" not in html.lower():
            urls = await extract_real_urls_from_html(html)
            async with lock:
                for u in urls:
                    collected_urls.add(u)
                shared_counts["total_pages_completed"] += 1
        queue.task_done()
    await session.close()

async def progress_updater(chat_id, context, stop_event, shared_counts, collected_urls, lock):
    msg = await context.bot.send_message(chat_id, "Starting …")
    while not stop_event.is_set():
        await asyncio.sleep(PROGRESS_INTERVAL)
        async with lock:
            url_count = len(collected_urls)
            total_dorks = shared_counts["total_dorks"]
            completed_dorks = shared_counts["completed_dorks"]
            pages_done = shared_counts["total_pages_completed"]
            total_pages = shared_counts["total_pages_to_fetch"]
        text = f"URL {url_count}\nDork {completed_dorks}/{total_dorks}\nPages {pages_done}/{total_pages}"
        try:
            await msg.edit_text(text)
        except Exception:
            pass

async def send_results(chat_id, context, urls):
    if not urls:
        await context.bot.send_message(chat_id, "No URLs collected.")
        return
    content = "\n".join(sorted(urls))
    bio = io.BytesIO(content.encode("utf-8"))
    bio.name = "results.txt"
    await context.bot.send_document(chat_id, document=bio, caption=f"Total URLs: {len(urls)}")

async def start_processing(user_id, dorks, max_pages, context, chat_id):
    stop_event = asyncio.Event()
    lock = asyncio.Lock()
    collected_urls = set()

    total_pages_to_fetch = len(dorks) * max_pages
    shared_counts = {
        "completed_dorks": len(dorks),
        "total_dorks": len(dorks),
        "total_pages_completed": 0,
        "total_pages_to_fetch": total_pages_to_fetch,
    }

    context.bot_data.setdefault("user_states", {})[user_id] = {
        "stop_event": stop_event,
        "collected_urls": collected_urls,
        "lock": lock,
        "shared_counts": shared_counts,
    }

    queue = asyncio.Queue()
    for dork in dorks:
        for page_num in range(1, max_pages + 1):
            await queue.put((dork, page_num))

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)

    updater = asyncio.create_task(
        progress_updater(chat_id, context, stop_event, shared_counts, collected_urls, lock)
    )

    workers = [
        asyncio.create_task(
            page_worker(queue, stop_event, semaphore, collected_urls, lock, shared_counts)
        )
        for _ in range(MAX_CONCURRENT_REQUESTS)
    ]

    stop_task = asyncio.create_task(stop_event.wait())
    await asyncio.wait(
        [asyncio.gather(*workers, return_exceptions=True), stop_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    stop_task.cancel()

    await queue.join()
    for w in workers:
        w.cancel()
    await asyncio.gather(*workers, return_exceptions=True)

    await updater
    await send_results(chat_id, context, collected_urls)
    context.bot_data["user_states"].pop(user_id, None)

# ---------- Telegram handlers (authorised for admin only) ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    await update.message.reply_text(
        "🚀 प्रॉक्सी-लेस हाईस्पीड याहू डॉर्क पार्सर\n\n"
        "एक .txt फाइल भेजें (एक डॉर्क प्रति लाइन)\n"
        "/pages <1-50>  - हर डॉर्क के लिए मैक्स पेज सेट करें (डिफॉल्ट 50)\n"
        "/stop - रोकें और अब तक के रिजल्ट भेजें"
    )

async def cmd_pages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /pages <1-50>")
        return
    try:
        p = int(context.args[0])
        if p < 1 or p > MAX_PAGES_LIMIT:
            await update.message.reply_text(f"1‑{MAX_PAGES_LIMIT} के बीच होना चाहिए")
            return
    except ValueError:
        await update.message.reply_text("Invalid number.")
        return
    context.user_data["max_pages"] = p
    await update.message.reply_text(f"मैक्स पेज {p} सेट हो गए। अब डॉर्क फाइल भेजें।")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    user_id = update.effective_user.id
    state = context.bot_data.get("user_states", {}).get(user_id)
    if not state:
        await update.message.reply_text("कोई प्रोसेस नहीं चल रहा।")
        return
    state["stop_event"].set()
    await update.message.reply_text("रोका जा रहा है... रिजल्ट जल्द भेज दिए जाएंगे।")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_USER_IDS:
        return
    user_id = update.effective_user.id
    if user_id in context.bot_data.get("user_states", {}):
        await update.message.reply_text("पहले से एक प्रोसेस चल रही है। /stop करें।")
        return
    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("सिर्फ .txt फाइल भेजें।")
        return
    file = await context.bot.get_file(doc.file_id)
    file_bytes = await file.download_as_bytearray()
    dorks = [line.strip() for line in file_bytes.decode("utf-8", errors="ignore").splitlines() if line.strip()]
    if not dorks:
        await update.message.reply_text("कोई डॉर्क नहीं मिला।")
        return

    max_pages = context.user_data.get("max_pages", DEFAULT_MAX_PAGES)
    await update.message.reply_text(
        f"प्रोसेसिंग शुरू: {len(dorks)} डॉर्क, {max_pages} पेज प्रति डॉर्क, कंकरेंसी: {MAX_CONCURRENT_REQUESTS}"
    )
    asyncio.create_task(start_processing(user_id, dorks, max_pages, context, update.effective_chat.id))

async def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("pages", cmd_pages))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    print("⚡ Bot चालू – सिर्फ admin के लिए प्राइवेट है।")
    await app.run_polling()

if __name__ == "__main__":
    asyncio.run(main())
