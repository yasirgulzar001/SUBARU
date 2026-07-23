#!/usr/bin/env python3
"""
Mass Dork Parser for Yahoo – Telegram Bot
=========================================
Commands:
  /dork <query>      – add a single dork
  /loaddorks         – upload a .txt file with one dork per line (mass input)
  /setproxies        – upload a .txt with proxies (ip:port)
  /checkproxies      – validate current proxies
  /pages <n>         – pages per dork (1‑50)
  /speed <n>         – max req/s per proxy (0.5‑2.0)
  /start             – begin parsing all queued dorks
  /stop              – stop & save results
  /status            – live progress
  /reset             – clear session
"""

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional, Set
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession as CffiSession
from curl_cffi.requests.impersonate import BrowserType
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "8939889745:AAEFORAmnxmL48jGS7hzxOjnQaAGW9MejLI")

BLACKLIST_DOMAINS = {
    "google.com", "www.google.com",
    "yahoo.com", "www.yahoo.com",
    "youtube.com", "www.youtube.com",
    "redirect.com", "www.redirect.com",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:123.0) Gecko/20100101 Firefox/123.0",
]

IMPERSONATES = [
    BrowserType.chrome110,
    BrowserType.chrome120,
    BrowserType.chrome123,
    BrowserType.firefox102,
    BrowserType.firefox110,
    BrowserType.edge101,
    BrowserType.safari15_5,
]

# ---------- Rate Limiter ----------
class AsyncRateLimiter:
    def __init__(self, rate: float):
        self.rate = rate
        self.tokens = rate
        self.max_tokens = rate
        self.updated_at = time.monotonic()

    async def wait(self):
        while True:
            now = time.monotonic()
            elapsed = now - self.updated_at
            self.tokens = min(self.max_tokens, self.tokens + elapsed * self.rate)
            self.updated_at = now
            if self.tokens >= 1:
                self.tokens -= 1
                return
            sleep_time = (1 - self.tokens) / self.rate
            await asyncio.sleep(sleep_time)

# ---------- Proxy Manager ----------
class ProxyManager:
    def __init__(self):
        self.raw_proxies: List[str] = []
        self.valid_proxies: List[str] = []

    def set_proxies(self, text: str):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        self.raw_proxies = lines
        self.valid_proxies = []

    async def validate_proxies(self):
        valid = []
        async def test_one(proxy: str):
            try:
                async with CffiSession() as session:
                    resp = await session.get("http://httpbin.org/ip", proxy=proxy, timeout=10)
                    if resp.status_code == 200:
                        valid.append(proxy)
            except Exception:
                pass
        sem = asyncio.Semaphore(20)
        async def sem_test(p):
            async with sem:
                await test_one(p)
        await asyncio.gather(*(sem_test(p) for p in self.raw_proxies))
        self.valid_proxies = valid

# ---------- Session ----------
@dataclass
class ParserSession:
    dorks: List[str] = field(default_factory=list)
    pages_per_dork: int = 5
    speed: float = 1.0
    results: Set[str] = field(default_factory=set)
    total_jobs: int = 0
    completed_jobs: int = 0
    running: bool = False
    stop_requested: bool = False
    task: Optional[asyncio.Task] = None

    def reset(self):
        self.dorks.clear()
        self.results.clear()
        self.total_jobs = 0
        self.completed_jobs = 0

# ---------- Yahoo Parser ----------
def extract_urls(html: str) -> Set[str]:
    soup = BeautifulSoup(html, "lxml")
    urls = set()
    for a in soup.find_all("a", class_="ac-algo"):
        href = a.get("href")
        if not href:
            continue
        if href.startswith("//"):
            href = "https:" + href
        if not href.startswith("http"):
            continue
        parsed = urlparse(href)
        domain = parsed.netloc.lower()
        if domain in BLACKLIST_DOMAINS or "yahoo.com" in domain:
            continue
        urls.add(href)
    return urls

# ---------- Dork Engine ----------
class DorkEngine:
    WORKERS_PER_PROXY = 8

    def __init__(self, session: ParserSession, proxy_mgr: ProxyManager):
        self.session = session
        self.proxy_mgr = proxy_mgr
        self.job_queue = deque()
        self.rate_limiters = {}

    def _build_jobs(self):
        self.job_queue.clear()
        for dork in self.session.dorks:
            for page in range(1, self.session.pages_per_dork + 1):
                offset = (page - 1) * 10 + 1
                self.job_queue.append((dork, offset, page))
        self.session.total_jobs = len(self.job_queue)
        self.session.completed_jobs = 0

    async def _worker(self, proxy: str, worker_id: int):
        rate_limiter = self.rate_limiters[proxy]
        impersonate = IMPERSONATES[worker_id % len(IMPERSONATES)]
        ua = USER_AGENTS[worker_id % len(USER_AGENTS)]
        async with CffiSession(
            impersonate=impersonate,
            timeout=15,
            headers={"User-Agent": ua},
        ) as session:
            while not self.session.stop_requested:
                try:
                    dork, offset, page = self.job_queue.popleft()
                except IndexError:
                    return
                await rate_limiter.wait()
                url = f"https://search.yahoo.com/search?p={dork}&b={offset}"
                try:
                    resp = await session.get(url, proxy=proxy)
                    if resp.status_code != 200:
                        raise Exception("bad status")
                    new_urls = extract_urls(resp.text)
                    self.session.results.update(new_urls)
                    self.session.completed_jobs += 1
                except Exception:
                    pass

    async def run(self):
        if not self.session.dorks:
            return
        self.session.running = True
        self.session.stop_requested = False
        self._build_jobs()
        valid = self.proxy_mgr.valid_proxies.copy()
        if not valid:
            logging.warning("No valid proxies.")
            self.session.running = False
            return
        self.rate_limiters = {proxy: AsyncRateLimiter(self.session.speed) for proxy in valid}
        workers = []
        for proxy in valid:
            for i in range(self.WORKERS_PER_PROXY):
                workers.append(asyncio.create_task(self._worker(proxy, i)))
        while self.job_queue and not self.session.stop_requested:
            await asyncio.sleep(0.1)
        if self.session.stop_requested:
            for w in workers:
                w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self.session.running = False
        self.session.task = None
        if self.session.results:
            self._save_results()

    def _save_results(self):
        filename = f"results_{int(time.time())}.txt"
        with open(filename, "w") as f:
            for url in sorted(self.session.results):
                f.write(url + "\n")
        logging.info(f"Results saved to {filename}")

# ---------- Telegram Bot ----------
session = ParserSession()
proxy_mgr = ProxyManager()
engine = DorkEngine(session, proxy_mgr)

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if session.running:
        await update.message.reply_text("Already running. /stop first.")
        return
    if not session.dorks:
        await update.message.reply_text("No dorks. Use /dork or /loaddorks to add some.")
        return
    if not proxy_mgr.valid_proxies:
        await update.message.reply_text("No valid proxies. /setproxies then /checkproxies.")
        return
    await update.message.reply_text(
        f"Starting mass parser: {len(session.dorks)} dorks × {session.pages_per_dork} pages, "
        f"speed {session.speed} req/s/proxy, {len(proxy_mgr.valid_proxies)} proxies."
    )
    session.task = asyncio.create_task(engine.run())

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not session.running:
        await update.message.reply_text("Nothing to stop.")
        return
    session.stop_requested = True
    await update.message.reply_text("Stopping – results will be saved when workers exit.")

async def dork_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = " ".join(context.args)
    if not query:
        await update.message.reply_text("Usage: /dork <search query>")
        return
    session.dorks.append(query)
    await update.message.reply_text(f"1 dork added. Total: {len(session.dorks)}")

async def loaddorks_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /loaddorks command: user sends a .txt file with dorks."""
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Please upload a .txt file with one dork per line using /loaddorks.")
        return
    file = await context.bot.get_file(doc)
    text = (await file.download_as_bytearray()).decode(errors="ignore")
    new_dorks = [line.strip() for line in text.splitlines() if line.strip()]
    if not new_dorks:
        await update.message.reply_text("File was empty.")
        return
    session.dorks.extend(new_dorks)
    await update.message.reply_text(f"Loaded {len(new_dorks)} dorks. Total dorks now: {len(session.dorks)}")

async def pages_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        n = int(context.args[0])
        if not 1 <= n <= 50:
            raise ValueError
        session.pages_per_dork = n
        await update.message.reply_text(f"Pages per dork = {n}")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /pages <1-50>")

async def speed_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        s = float(context.args[0])
        if not 0.5 <= s <= 2.0:
            raise ValueError
        session.speed = s
        await update.message.reply_text(f"Speed = {s} req/s per proxy")
    except (IndexError, ValueError):
        await update.message.reply_text("Usage: /speed <0.5-2.0>")

async def setproxies_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("Please upload a .txt file with proxies (ip:port).")
        return
    file = await context.bot.get_file(doc)
    text = (await file.download_as_bytearray()).decode()
    proxy_mgr.set_proxies(text)
    await update.message.reply_text(f"Loaded {len(proxy_mgr.raw_proxies)} proxies. /checkproxies to validate.")

async def checkproxies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not proxy_mgr.raw_proxies:
        await update.message.reply_text("No proxies. /setproxies first.")
        return
    msg = await update.message.reply_text(f"Checking {len(proxy_mgr.raw_proxies)} proxies...")
    await proxy_mgr.validate_proxies()
    await msg.edit_text(f"Done. {len(proxy_mgr.valid_proxies)}/{len(proxy_mgr.raw_proxies)} are working.")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    running = session.running
    dorks = len(session.dorks)
    pages = session.pages_per_dork
    total = session.total_jobs
    done = session.completed_jobs
    speed = session.speed
    proxies = len(proxy_mgr.valid_proxies)
    results = len(session.results)
    msg = f"""
<b>Mass Parser Status</b>
Running: {running}
Dorks loaded: {dorks}
Pages per dork: {pages}
Jobs: {done}/{total}
URLs found: {results}
Speed: {speed} req/s/proxy
Active proxies: {proxies}
"""
    await update.message.reply_html(msg)

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session.reset()
    await update.message.reply_text("Session wiped (dorks, results, progress).")

async def unknown_msg(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown input. Use /help for commands.")

def main():
    logging.basicConfig(level=logging.INFO)
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("dork", dork_cmd))
    app.add_handler(CommandHandler("pages", pages_cmd))
    app.add_handler(CommandHandler("speed", speed_cmd))
    app.add_handler(CommandHandler("checkproxies", checkproxies_cmd))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("loaddorks", loaddorks_handler))
    # handlers for file uploads
    app.add_handler(MessageHandler(filters.Document.TEXT & filters.Regex("(?i).*\\.txt$") & ~filters.COMMAND, setproxies_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_msg))

    print("Mass Dork Parser bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
