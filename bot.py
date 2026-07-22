import asyncio
import io
import os
import random
import re
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

# ---------- CONFIG ----------
BOT_TOKEN = os.environ["BOT_TOKEN"]                     # Required
ALLOWED_USERS = os.environ.get("ALLOWED_USER_IDS", "")  # Comma-separated, optional
if ALLOWED_USERS:
    ALLOWED_USERS = {int(uid) for uid in ALLOWED_USERS.split(",") if uid.strip()}

MAX_PAGES_LIMIT = 50
DEFAULT_MAX_PAGES = 50
CONCURRENT_WORKERS = 3
REQUEST_SEMAPHORE = asyncio.Semaphore(5)   # Max simultaneous Yahoo requests
PROGRESS_INTERVAL = 3                      # seconds

# Blacklist: common “pro” sites we never want to target
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

# Pool of TLS fingerprints – rotate endlessly
IMPERSONATE_TARGETS = [
    "chrome99", "chrome100", "chrome101", "chrome104", "chrome107",
    "chrome110", "chrome116", "chrome119", "chrome120", "chrome123",
    "edge99", "edge101", "edge110", "edge120",
    "safari15_3", "safari15_5", "safari17_0",
    "firefox110", "firefox120",
]
# ---------------------------------

def is_blacklisted(url: str) -> bool:
    """Return True if the domain is in the blacklist."""
    try:
        netloc = urlparse(url).netloc.lower()
        netloc = netloc.split(":")[0]   # remove port
        for d in BLACKLIST_DOMAINS:
            if netloc == d or netloc.endswith("." + d):
                return True
        return False
    except Exception:
        return True

async def extract_real_urls_from_html(html: str) -> set:
    """
    Parse Yahoo search result page.
    Look for <a> tags with href containing 'r.search.yahoo.com' and 'RU=',
    decode the RU parameter to get the real URL.
    """
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

async def fetch_yahoo_page(session: AsyncSession, query: str, offset: int, impersonate: str) -> str | None:
    """Fetch one Yahoo search results page with a specific TLS fingerprint."""
    url = f"https://search.yahoo.com/search?p={query}&b={offset}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",  # Overridden by curl_cffi
    }
    async with REQUEST_SEMAPHORE:
        try:
            resp = await session.get(url, headers=headers, impersonate=impersonate, timeout=15)
            if resp.status_code == 200:
                return resp.text
        except Exception:
            pass
    return None

async def dork_worker(
    user_id: int,
    dork_queue: asyncio.Queue,
    stop_event: asyncio.Event,
    max_pages: int,
    shared_counts: dict,
    lock: asyncio.Lock,
    collected_urls: set,
):
    """Worker that processes dorks one by one."""
    # Each worker gets its own session for complete TLS isolation
    session = AsyncSession()
    try:
        while not stop_event.is_set():
            try:
                dork = await asyncio.wait_for(dork_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                break

            for page_num in range(1, max_pages + 1):
                if stop_event.is_set():
                    break
                offset = (page_num - 1) * 10 + 1

                # Rotate TLS fingerprint
                impersonate = random.choice(IMPERSONATE_TARGETS)
                html = await fetch_yahoo_page(session, dork, offset, impersonate)
                if html is None:
                    # Retry once with a different fingerprint
                    await asyncio.sleep(1)
                    impersonate = random.choice(IMPERSONATE_TARGETS)
                    html = await fetch_yahoo_page(session, dork, offset, impersonate)
                    if html is None:
                        continue  # skip this page

                if "captcha" in html.lower() or "unusual traffic" in html.lower():
                    # Hit a block – wait and stop this dork completely
                    await asyncio.sleep(30)
                    break

                urls = await extract_real_urls_from_html(html)
                async with lock:
                    for u in urls:
                        collected_urls.add(u)
                    shared_counts["total_pages"] += 1

                # Be gentle with Yahoo
                await asyncio.sleep(random.uniform(1.5, 3.0))

            async with lock:
                shared_counts["completed_dorks"] += 1
            dork_queue.task_done()
    finally:
        await session.close()

async def progress_updater(
    chat_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    stop_event: asyncio.Event,
    shared_counts: dict,
    collected_urls: set,
    lock: asyncio.Lock,
):
    """Edit a live status message every PROGRESS_INTERVAL seconds."""
    msg = await context.bot.send_message(chat_id, "Starting …")
    while not stop_event.is_set():
        await asyncio.sleep(PROGRESS_INTERVAL)
        async with lock:
            url_count = len(collected_urls)
            total = shared_counts["total_dorks"]
            done = shared_counts["completed_dorks"]
            pages = shared_counts["total_pages"]
        text = f"URL {url_count}\nDork {done}/{total}\nPages {pages}"
        try:
            await msg.edit_text(text)
        except Exception:
            pass

async def send_results(chat_id: int, context: ContextTypes.DEFAULT_TYPE, urls: set):
    """Send collected URLs as a .txt file."""
    if not urls:
        await context.bot.send_message(chat_id, "No URLs collected.")
        return
    content = "\n".join(sorted(urls))
    bio = io.BytesIO(content.encode("utf-8"))
    bio.name = "results.txt"
    await context.bot.send_document(chat_id, document=bio, caption=f"Total URLs: {len(urls)}")

async def start_processing(user_id, dorks, max_pages, context, chat_id):
    """Main processing coroutine – creates workers, updater, handles /stop."""
    stop_event = asyncio.Event()
    lock = asyncio.Lock()
    collected_urls = set()
    shared_counts = {"completed_dorks": 0, "total_dorks": len(dorks), "total_pages": 0}

    # Store state for external access (e.g., /stop)
    context.bot_data.setdefault("user_states", {})[user_id] = {
        "stop_event": stop_event,
        "collected_urls": collected_urls,
        "lock": lock,
        "shared_counts": shared_counts,
    }

    # Queue of dorks
    dork_queue = asyncio.Queue()
    for d in dorks:
        await dork_queue.put(d)

    # Start progress updater
    updater = asyncio.create_task(
        progress_updater(chat_id, context, stop_event, shared_counts, collected_urls, lock)
    )

    # Start workers
    workers = [
        asyncio.create_task(
            dork_worker(user_id, dork_queue, stop_event, max_pages, shared_counts, lock, collected_urls)
        )
        for _ in range(CONCURRENT_WORKERS)
    ]

    # Wait until all dorks are done OR stop_event is set from outside
    stop_task = asyncio.create_task(stop_event.wait())
    done, pending = await asyncio.wait(
        [asyncio.gather(*workers), stop_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    # If stop was requested, workers will see the event and exit soon
    stop_task.cancel()
    await asyncio.gather(*workers, return_exceptions=True)

    # Stop the updater (it will see stop_event)
    await updater

    # Send results
    await send_results(chat_id, context, collected_urls)

    # Cleanup
    context.bot_data["user_states"].pop(user_id, None)

# -------------------- Telegram handlers --------------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and update.effective_user.id not in ALLOWED_USERS:
        return
    await update.message.reply_text(
        "Send me a .txt file with dorks (one per line).\n"
        "Use /pages <number> to set max pages per dork (1‑50). Default is 50.\n"
        "Use /stop to abort and get current results."
    )

async def cmd_pages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and update.effective_user.id not in ALLOWED_USERS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /pages <1-50>")
        return
    try:
        p = int(context.args[0])
        if p < 1 or p > MAX_PAGES_LIMIT:
            await update.message.reply_text(f"Must be 1‑{MAX_PAGES_LIMIT}")
            return
    except ValueError:
        await update.message.reply_text("Invalid number.")
        return

    context.user_data["max_pages"] = p
    await update.message.reply_text(f"Max pages set to {p}. Now upload your dork file.")

async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and update.effective_user.id not in ALLOWED_USERS:
        return
    user_id = update.effective_user.id
    state = context.bot_data.get("user_states", {}).get(user_id)
    if not state:
        await update.message.reply_text("No processing is running.")
        return
    state["stop_event"].set()
    await update.message.reply_text("Stopping – results will be sent shortly.")

async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if ALLOWED_USERS and update.effective_user.id not in ALLOWED_USERS:
        return
    user_id = update.effective_user.id
    if user_id in context.bot_data.get("user_states", {}):
        await update.message.reply_text("Already processing. Use /stop first.")
        return

    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Please send a .txt file.")
        return

    file = await context.bot.get_file(doc.file_id)
    file_bytes = await file.download_as_bytearray()
    raw = file_bytes.decode("utf-8", errors="ignore")
    dorks = [line.strip() for line in raw.splitlines() if line.strip()]
    if not dorks:
        await update.message.reply_text("No dorks found.")
        return

    max_pages = context.user_data.get("max_pages", DEFAULT_MAX_PAGES)
    await update.message.reply_text(f"Processing {len(dorks)} dorks, up to {max_pages} pages each.")
    asyncio.create_task(start_processing(user_id, dorks, max_pages, context, update.effective_chat.id))

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("pages", cmd_pages))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    print("Bot running …")
    app.run_polling()

if __name__ == "__main__":
    main()
