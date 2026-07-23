#!/usr/bin/env python3
"""
Yahoo Dork Parser — Telegram controlled
High-performance async scanner with fixed proxies + auto proxy checker.

Deps:
    pip install python-telegram-bot==21.6 aiohttp beautifulsoup4 lxml

Run:
    export TELEGRAM_TOKEN="123:abc"
    export ADMIN_IDS="11111111,22222222"   # optional allowlist
    python yahoo_dork_bot.py
"""

import os
import re
import time
import html
import random
import asyncio
import logging
from urllib.parse import quote_plus, urlparse, parse_qs, unquote
from dataclasses import dataclass, field

import aiohttp
from bs4 import BeautifulSoup

from telegram import Update, InputFile
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ContextTypes, filters,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("yahoo-dork")

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
TOKEN = os.getenv("TELEGRAM_TOKEN", "8939889745:AAEFORAmnxmL48jGS7hzxOjnQaAGW9MejLI")
ADMIN_IDS = {
    int(x) for x in os.getenv("ADMIN_IDS", "6535041385").split(",") if x.strip().isdigit()
}

YAHOO_URL = "https://search.yahoo.com/search"
RESULTS_PER_PAGE = 10
REQUEST_TIMEOUT = 20
MAX_PROXY_FAILS = 4          # drop a proxy after this many consecutive failures
CHECK_URL = "https://search.yahoo.com/search?p=test"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
    "Gecko/20100101 Firefox/125.0",
]


# --------------------------------------------------------------------------- #
# Session state (per Telegram chat)
# --------------------------------------------------------------------------- #
@dataclass
class Proxy:
    raw: str                 # e.g. http://user:pass@host:port  OR host:port
    fails: int = 0
    alive: bool = True

    @property
    def url(self) -> str:
        if "://" not in self.raw:
            return "http://" + self.raw
        return self.raw


@dataclass
class Session:
    proxies: list = field(default_factory=list)      # list[Proxy]
    pages: int = 3
    speed: float = 1.0                               # req/s per proxy
    running: bool = False
    stop_flag: bool = False
    results: dict = field(default_factory=dict)      # dork -> set(urls)
    total_urls: int = 0
    done_dorks: int = 0
    total_dorks: int = 0
    started_at: float = 0.0

    def reset(self):
        self.proxies.clear()
        self.pages = 3
        self.speed = 1.0
        self.running = False
        self.stop_flag = False
        self.results.clear()
        self.total_urls = 0
        self.done_dorks = 0
        self.total_dorks = 0
        self.started_at = 0.0


SESSIONS: dict[int, Session] = {}


def get_session(chat_id: int) -> Session:
    if chat_id not in SESSIONS:
        SESSIONS[chat_id] = Session()
    return SESSIONS[chat_id]


def is_allowed(update: Update) -> bool:
    if not ADMIN_IDS:
        return True
    uid = update.effective_user.id if update.effective_user else 0
    return uid in ADMIN_IDS


# --------------------------------------------------------------------------- #
# Yahoo parsing
# --------------------------------------------------------------------------- #
def unwrap_yahoo_url(href: str) -> str:
    """Yahoo often wraps real links: .../RU=<encoded>/RK=... Extract the real one."""
    if not href:
        return ""
    if "/RU=" in href:
        try:
            part = href.split("/RU=", 1)[1].split("/RK=", 1)[0]
            return unquote(part)
        except Exception:
            pass
    # some links are r.search.yahoo.com redirects with ?u= or ?RU=
    try:
        q = parse_qs(urlparse(href).query)
        for key in ("RU", "u"):
            if key in q:
                return unquote(q[key][0])
    except Exception:
        pass
    return href


def parse_results(html_text: str) -> list[str]:
    """Return a list of clean result URLs from a Yahoo SERP."""
    soup = BeautifulSoup(html_text, "lxml")
    urls: list[str] = []

    # Yahoo result containers have changed over time; try several selectors.
    selectors = [
        "div.algo a[href]",
        "div.Sr a[href]",
        "h3.title a[href]",
        "ol.searchCenterMiddle a[href]",
    ]
    seen = set()
    for sel in selectors:
        for a in soup.select(sel):
            href = a.get("href", "")
            real = unwrap_yahoo_url(href)
            if not real.startswith("http"):
                continue
            netloc = urlparse(real).netloc.lower()
            # skip yahoo's own chrome / tracking links
            if any(b in netloc for b in ("yahoo.com", "bing.com", "yimg.com")):
                continue
            if real not in seen:
                seen.add(real)
                urls.append(real)
    return urls


# --------------------------------------------------------------------------- #
# Fetch with proxy rotation + rate limiting
# --------------------------------------------------------------------------- #
async def fetch_page(session: aiohttp.ClientSession, dork: str,
                     page: int, proxy: Proxy) -> tuple[bool, list[str]]:
    start = page * RESULTS_PER_PAGE + 1
    params = {"p": dork, "b": str(start), "pz": str(RESULTS_PER_PAGE)}
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        async with session.get(
            YAHOO_URL, params=params, headers=headers,
            proxy=proxy.url, timeout=aiohttp.ClientTimeout(total=REQUEST_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                proxy.fails += 1
                return False, []
            text = await resp.text(errors="ignore")
            proxy.fails = 0
            return True, parse_results(text)
    except Exception as e:
        proxy.fails += 1
        log.debug("fetch error via %s: %s", proxy.raw, e)
        return False, []


async def worker(name: int, queue: asyncio.Queue, sess: Session,
                 http: aiohttp.ClientSession, proxy: Proxy):
    """One worker bound to one proxy; obeys per-proxy req/s."""
    delay = 1.0 / max(sess.speed, 0.01)
    while not sess.stop_flag:
        try:
            dork, page = queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        if not proxy.alive:
            queue.task_done()
            # requeue for another proxy
            await queue.put((dork, page))
            await asyncio.sleep(0.2)
            return

        ok, urls = await fetch_page(http, dork, page, proxy)
        if ok:
            bucket = sess.results.setdefault(dork, set())
            before = len(bucket)
            bucket.update(urls)
            sess.total_urls += len(bucket) - before
        if proxy.fails >= MAX_PROXY_FAILS:
            proxy.alive = False
            log.info("Proxy %s marked dead", proxy.raw)
        queue.task_done()
        await asyncio.sleep(delay + random.uniform(0, 0.15))


async def run_scan(chat_id: int, dorks: list[str],
                   context: ContextTypes.DEFAULT_TYPE):
    sess = get_session(chat_id)
    sess.running = True
    sess.stop_flag = False
    sess.results.clear()
    sess.total_urls = 0
    sess.done_dorks = 0
    sess.total_dorks = len(dorks)
    sess.started_at = time.time()

    alive_proxies = [p for p in sess.proxies if p.alive]
    if not alive_proxies:
        await context.bot.send_message(chat_id, "⚠️ No live proxies. Use /setproxies then /checkproxies.")
        sess.running = False
        return

    queue: asyncio.Queue = asyncio.Queue()
    for dork in dorks:
        for page in range(sess.pages):
            queue.put_nowait((dork, page))

    connector = aiohttp.TCPConnector(limit=0, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as http:
        # Track dork completion by monitoring the queue drain.
        async def progress_tracker():
            last = 0
            while not queue.empty() and not sess.stop_flag:
                done_pages = (sess.total_dorks * sess.pages) - queue.qsize()
                sess.done_dorks = done_pages // max(sess.pages, 1)
                await asyncio.sleep(2)

        tasks = [
            asyncio.create_task(worker(i, queue, sess, http, p))
            for i, p in enumerate(alive_proxies)
        ]
        # Re-spawn workers as long as queue has items (proxies finish their slice)
        tracker = asyncio.create_task(progress_tracker())
        await queue.join()
        for t in tasks:
            t.cancel()
        tracker.cancel()

    sess.done_dorks = sess.total_dorks
    sess.running = False
    await send_results_file(chat_id, sess, context)


async def send_results_file(chat_id, sess: Session, context):
    lines = []
    for dork, urls in sess.results.items():
        for u in sorted(urls):
            lines.append(u)
    body = "\n".join(lines)
    if not body:
        await context.bot.send_message(chat_id, "✅ Scan finished — 0 URLs found.")
        return
    data = body.encode("utf-8")
    from io import BytesIO
    bio = BytesIO(data)
    bio.name = "yahoo_results.txt"
    elapsed = int(time.time() - sess.started_at)
    caption = (f"✅ Done — {sess.total_urls} unique URLs "
               f"from {sess.total_dorks} dorks in {elapsed}s")
    await context.bot.send_document(chat_id, document=InputFile(bio), caption=caption)


# --------------------------------------------------------------------------- #
# Proxy checker
# --------------------------------------------------------------------------- #
async def check_one(http, proxy: Proxy) -> bool:
    try:
        async with http.get(
            CHECK_URL, proxy=proxy.url,
            headers={"User-Agent": random.choice(USER_AGENTS)},
            timeout=aiohttp.ClientTimeout(total=12),
        ) as r:
            proxy.alive = r.status == 200
            proxy.fails = 0 if proxy.alive else proxy.fails + 1
            return proxy.alive
    except Exception:
        proxy.alive = False
        return False


async def check_proxies(sess: Session) -> tuple[int, int]:
    connector = aiohttp.TCPConnector(limit=0, ssl=False)
    async with aiohttp.ClientSession(connector=connector) as http:
        results = await asyncio.gather(*(check_one(http, p) for p in sess.proxies))
    alive = sum(1 for r in results if r)
    return alive, len(sess.proxies)


# --------------------------------------------------------------------------- #
# Telegram command handlers
# --------------------------------------------------------------------------- #
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = (
        "🔎 *Yahoo Dork Parser* — Fixed Proxies + Auto Checker\n\n"
        "*Commands:*\n"
        "/setproxies – Upload proxy list\n"
        "/checkproxies – Validate current proxies\n"
        "/pages <n> – pages per dork (1‑50)\n"
        "/speed <n> – req/s per proxy (0.5‑2.0)\n"
        "/status – live progress\n"
        "/stop – stop & save\n"
        "/reset – clear session\n\n"
        "📤 Upload a `.txt` file (1 dork per line) to scan."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def cmd_pages(update, context):
    if not is_allowed(update):
        return
    sess = get_session(update.effective_chat.id)
    try:
        n = int(context.args[0])
        if not 1 <= n <= 50:
            raise ValueError
        sess.pages = n
        await update.message.reply_text(f"📄 Pages per dork set to {n}.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /pages <1‑50>")


async def cmd_speed(update, context):
    if not is_allowed(update):
        return
    sess = get_session(update.effective_chat.id)
    try:
        v = float(context.args[0])
        if not 0.5 <= v <= 2.0:
            raise ValueError
        sess.speed = v
        await update.message.reply_text(f"⚡ Speed set to {v} req/s per proxy.")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /speed <0.5‑2.0>")


async def cmd_setproxies(update, context):
    if not is_allowed(update):
        return
    await update.message.reply_text(
        "📥 Send a `.txt` file with one proxy per line.\n"
        "Formats: `host:port` or `http://user:pass@host:port`",
        parse_mode=ParseMode.MARKDOWN,
    )
    context.user_data["awaiting_proxies"] = True


async def cmd_checkproxies(update, context):
    if not is_allowed(update):
        return
    sess = get_session(update.effective_chat.id)
    if not sess.proxies:
        await update.message.reply_text("No proxies loaded. Use /setproxies first.")
        return
    msg = await update.message.reply_text("🔄 Checking proxies…")
    alive, total = await check_proxies(sess)
    await msg.edit_text(f"✅ Proxies alive: {alive}/{total}")


async def cmd_status(update, context):
    if not is_allowed(update):
        return
    sess = get_session(update.effective_chat.id)
    if not sess.running and sess.total_dorks == 0:
        await update.message.reply_text("Idle. Upload a dork .txt to start.")
        return
    elapsed = int(time.time() - sess.started_at) if sess.started_at else 0
    alive = sum(1 for p in sess.proxies if p.alive)
    state = "RUNNING" if sess.running else "STOPPED"
    await update.message.reply_text(
        f"📊 *Status:* {state}\n"
        f"Dorks: {sess.done_dorks}/{sess.total_dorks}\n"
        f"URLs found: {sess.total_urls}\n"
        f"Live proxies: {alive}/{len(sess.proxies)}\n"
        f"Pages: {sess.pages} | Speed: {sess.speed}/s\n"
        f"Elapsed: {elapsed}s",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_stop(update, context):
    if not is_allowed(update):
        return
    sess = get_session(update.effective_chat.id)
    if not sess.running:
        await update.message.reply_text("Nothing running.")
        return
    sess.stop_flag = True
    await update.message.reply_text("🛑 Stopping & saving current results…")


async def cmd_reset(update, context):
    if not is_allowed(update):
        return
    sess = get_session(update.effective_chat.id)
    sess.stop_flag = True
    sess.reset()
    await update.message.reply_text("♻️ Session cleared.")


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    doc = update.message.document
    if not doc.file_name.lower().endswith(".txt"):
        await update.message.reply_text("Please send a .txt file.")
        return

    file = await doc.get_file()
    raw = await file.download_as_bytearray()
    lines = [l.strip() for l in raw.decode("utf-8", "ignore").splitlines() if l.strip()]

    sess = get_session(update.effective_chat.id)

    if context.user_data.get("awaiting_proxies"):
        context.user_data["awaiting_proxies"] = False
        sess.proxies = [Proxy(raw=l) for l in lines]
        await update.message.reply_text(
            f"✅ Loaded {len(sess.proxies)} proxies. Run /checkproxies to validate."
        )
        return

    # Otherwise treat as dork list
    if sess.running:
        await update.message.reply_text("A scan is already running. /stop first.")
        return
    if not sess.proxies:
        await update.message.reply_text("Load proxies first with /setproxies.")
        return

    await update.message.reply_text(
        f"🚀 Starting scan: {len(lines)} dorks × {sess.pages} pages "
        f"via {sum(1 for p in sess.proxies if p.alive)} proxies…"
    )
    asyncio.create_task(run_scan(update.effective_chat.id, lines, context))


# --------------------------------------------------------------------------- #
def main():
    if not TOKEN:
        raise SystemExit("Set TELEGRAM_TOKEN env var.")
    app = Application.builder().token(TOKEN).concurrent_updates(True).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("pages", cmd_pages))
    app.add_handler(CommandHandler("speed", cmd_speed))
    app.add_handler(CommandHandler("setproxies", cmd_setproxies))
    app.add_handler(CommandHandler("checkproxies", cmd_checkproxies))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))

    log.info("Bot started.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
