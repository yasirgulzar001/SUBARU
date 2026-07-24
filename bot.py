#!/usr/bin/env python3
"""
main.py
=======
Production-ready Telegram bot for mass dork parsing of Yahoo search engine.
Features:
- Parallel proxy architecture (30 proxies × 8 workers = 240 concurrent requests)
- TLS fingerprint rotation via curl_cffi
- Random User-Agents and headers
- Yahoo redirect URL decoding
- CAPTCHA/block detection and proxy switching

Dependencies:
    pip install python-telegram-bot[job-queue] curl_cffi beautifulsoup4 aiofiles httpx[socks]
"""

import asyncio
import base64
import json
import logging
import os
import random
import re
import time
from collections import defaultdict, deque
from datetime import datetime
from typing import Optional
from urllib.parse import quote, unquote

import aiofiles
import httpx
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from telegram import InputFile, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ──────────────────────────────────────────────────────────────
#  Logging Configuration
# ──────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s — %(name)s — %(levelname)s — %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)
logger = logging.getLogger("yahoo-dork-bot")


# ──────────────────────────────────────────────────────────────
#  Constants & Headers (constants.py)
# ──────────────────────────────────────────────────────────────

TLS_FINGERPRINTS = [
    "chrome110", "chrome116", "chrome119", "chrome120", "chrome123",
    "chrome124", "chrome131", "edge99", "edge101",
    "safari15_3", "safari15_5", "safari17_0",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:121.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0",
]

ACCEPT_LANGUAGES = [
    "en-US,en;q=0.9", "en-US,en;q=0.9,es;q=0.8", "en-GB,en;q=0.9,en-US;q=0.8",
    "en-US,en;q=0.9,fr;q=0.8", "en,en-US;q=0.9", "en-US,en;q=0.9,de;q=0.8",
    "en-US,en;q=0.9,ja;q=0.8", "en-US,en;q=0.9,zh-CN;q=0.8",
]

ACCEPT_ENCODINGS = [
    "gzip, deflate, br", "gzip, deflate", "br, gzip, deflate", "gzip, deflate, br, zstd",
]

SEC_CH_UA_PLATFORMS = ['"Windows"', '"macOS"', '"Linux"']
SEC_CH_UA_STRINGS = [
    '"Chromium";v="131", "Not_A Brand";v="24", "Google Chrome";v="131"',
    '"Chromium";v="130", "Not_A Brand";v="24", "Google Chrome";v="130"',
    '"Chromium";v="129", "Not_A Brand";v="24", "Google Chrome";v="129"',
    '"Microsoft Edge";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
]

def get_random_headers() -> dict:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": random.choice(ACCEPT_LANGUAGES),
        "Accept-Encoding": random.choice(ACCEPT_ENCODINGS),
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "sec-ch-ua": random.choice(SEC_CH_UA_STRINGS),
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": random.choice(SEC_CH_UA_PLATFORMS),
        "DNT": random.choice(["1", "0"]),
    }

def get_random_fingerprint() -> str:
    return random.choice(TLS_FINGERPRINTS)


# ──────────────────────────────────────────────────────────────
#  Yahoo Parser (yahoo_parser.py)
# ──────────────────────────────────────────────────────────────

def _decode_base64_url(encoded: str) -> str:
    encoded = encoded.replace("-", "+").replace("_", "/")
    padding = 4 - (len(encoded) % 4)
    if padding != 4:
        encoded += "=" * padding
    try:
        return base64.b64decode(encoded).decode("utf-8", errors="ignore")
    except Exception:
        return ""

def extract_real_url(href: str) -> Optional[str]:
    if not href:
        return None
    if "r.search.yahoo.com" not in href and "search.yahoo.com/redir" not in href:
        if href.startswith(("http://", "https://")):
            if "yahoo.com" in href:
                return None
            return href
        return None

    ru_match = re.search(r"/RU=([^/]+)", href)
    if ru_match:
        decoded = _decode_base64_url(ru_match.group(1))
        if decoded.startswith("http"):
            return decoded

    rh_match = re.search(r"/RH=([^/]+)", href)
    if rh_match:
        decoded = _decode_base64_url(rh_match.group(1))
        if decoded.startswith("http"):
            return decoded

    url_match = re.search(r"RU=([^&/]+)", href)
    if url_match:
        decoded = unquote(url_match.group(1))
        if decoded.startswith("http"):
            return decoded
    return None

def parse_yahoo_results(html: str) -> list[str]:
    if not html:
        return []
    soup = BeautifulSoup(html, "html.parser")
    urls, seen = [], set()

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "")
        if not href or "search.yahoo.com/search" in href:
            continue
        classes = anchor.get("class", [])
        class_str = " ".join(classes) if classes else ""
        is_result = (
            "ac-algo" in class_str or "algo" in class_str
            or anchor.get("data-testid") == "result-title"
            or anchor.find_parent("div", class_=re.compile("algo")) is not None
            or anchor.find_parent(class_=re.compile("compTitle")) is not None
        )
        if not is_result:
            continue
        real_url = extract_real_url(href)
        if real_url and real_url not in seen and "yahoo.com" not in real_url:
            seen.add(real_url)
            urls.append(real_url)

    if not urls:
        for anchor in soup.find_all("a", href=True):
            href = anchor.get("href", "")
            if "r.search.yahoo.com" in href:
                real_url = extract_real_url(href)
                if real_url and real_url not in seen and "yahoo.com" not in real_url:
                    seen.add(real_url)
                    urls.append(real_url)
    return urls

def detect_captcha(html: str) -> bool:
    if not html:
        return False
    html_lower = html.lower()
    indicators = [
        "captcha", "unusual traffic", "verify you are human",
        "are you a robot", "automated queries",
        "sorry, we could not process your request",
        "to continue, please type the characters below",
    ]
    return any(ind in html_lower for ind in indicators)


# ──────────────────────────────────────────────────────────────
#  Proxy Pool (proxy_pool.py)
# ──────────────────────────────────────────────────────────────

def normalize_proxy(proxy: str) -> Optional[str]:
    if not proxy:
        return None
    proxy = proxy.strip()
    if not proxy:
        return None
    if proxy.startswith(("http://", "https://", "socks5://", "socks4://", "socks5h://")):
        return proxy
    return f"http://{proxy}"

def is_valid_proxy_string(raw: str) -> bool:
    if not raw:
        return False
    s = raw.strip()
    if not s:
        return False
    if "://" in s:
        s = s.split("://", 1)[1]
    return ":" in s

class ProxyPool:
    def __init__(self) -> None:
        self._all_proxies: list[str] = []
        self._available: asyncio.Queue[str] = asyncio.Queue()
        self._in_use: set[str] = set()
        self._dead: set[str] = set()
        self._lock = asyncio.Lock()

    async def load_proxies(self, proxies: list[str]) -> int:
        async with self._lock:
            self._all_proxies.clear()
            self._in_use.clear()
            self._dead.clear()
            self._available = asyncio.Queue()
            for raw in proxies:
                if is_valid_proxy_string(raw):
                    norm = normalize_proxy(raw)
                    if norm and norm not in self._all_proxies:
                        self._all_proxies.append(norm)
                        self._available.put_nowait(norm)
            return len(self._all_proxies)

    async def acquire(self) -> Optional[str]:
        if self._available.empty():
            return None
        try:
            proxy = await asyncio.wait_for(self._available.get(), timeout=2.0)
        except asyncio.TimeoutError:
            return None
        async with self._lock:
            self._in_use.add(proxy)
        return proxy

    async def release(self, proxy: str, healthy: bool = True) -> None:
        async with self._lock:
            self._in_use.discard(proxy)
            if healthy and proxy not in self._dead:
                self._available.put_nowait(proxy)
            else:
                self._dead.add(proxy)

    async def mark_dead(self, proxy: str) -> None:
        async with self._lock:
            self._in_use.discard(proxy)
            self._dead.add(proxy)

    async def release_all(self) -> None:
        async with self._lock:
            for proxy in list(self._in_use):
                self._in_use.discard(proxy)
                if proxy not in self._dead:
                    self._available.put_nowait(proxy)

    @property
    def total(self) -> int: return len(self._all_proxies)
    @property
    def dead_count(self) -> int: return len(self._dead)

    async def check_all_proxies(self, test_url: str = "https://httpbin.org/ip", timeout: float = 10.0, concurrency: int = 50) -> tuple[list[tuple[str, float]], list[str]]:
        if not self._all_proxies:
            return [], []
        sem = asyncio.Semaphore(concurrency)
        async def _check(proxy: str):
            async with sem:
                try:
                    async with httpx.AsyncClient(proxy=proxy, timeout=timeout, follow_redirects=True) as client:
                        start = time.time()
                        resp = await client.get(test_url)
                        lat = (time.time() - start) * 1000
                        if resp.status_code == 200:
                            return proxy, True, lat
                        return proxy, False, -1.0
                except Exception:
                    return proxy, False, -1.0
        results = await asyncio.gather(*[_check(p) for p in self._all_proxies])
        alive = [(p, l) for p, ok, l in results if ok]
        dead = [p for p, ok, _ in results if not ok]
        async with self._lock:
            for p in dead: self._dead.add(p)
        return alive, dead


# ──────────────────────────────────────────────────────────────
#  Scanner Engine (scanner.py)
# ──────────────────────────────────────────────────────────────

class Scanner:
    WORKERS_PER_PROXY = 8
    MAX_PROXIES = 30
    MAX_RETRIES = 3
    PROXY_FAIL_THRESHOLD = 3
    REQUESTS_PER_SESSION = 10
    SPEED_WINDOW = 10.0

    def __init__(self, proxy_pool: ProxyPool, pages_per_dork: int, dorks: list[str]) -> None:
        self.proxy_pool = proxy_pool
        self.pages_per_dork = max(1, pages_per_dork)
        self.dorks = list(dorks)
        self.task_queue: asyncio.Queue = asyncio.Queue()
        self.results: dict[str, set[str]] = defaultdict(set)
        self.urls_found = 0
        self.pages_fetched = 0
        self.pages_failed = 0
        self.tasks_done = 0
        self.active_workers = 0
        self.working_proxies: set[str] = set()
        self.start_time: Optional[float] = None
        self._recent_fetches: deque = deque()
        self._stop_event = asyncio.Event()
        self._workers: list[asyncio.Task] = []
        self._lock = asyncio.Lock()
        self._tasks_total = 0
        self.num_proxies = min(self.MAX_PROXIES, proxy_pool.total)
        self.total_workers = self.num_proxies * self.WORKERS_PER_PROXY

    def get_status(self) -> dict:
        elapsed = "00:00:00"
        if self.start_time:
            secs = int(time.time() - self.start_time)
            h, rem = divmod(secs, 3600)
            m, s = divmod(rem, 60)
            elapsed = f"{h:02d}:{m:02d}:{s:02d}"
        speed = 0.0
        now = time.time()
        cutoff = now - self.SPEED_WINDOW
        while self._recent_fetches and self._recent_fetches[0][0] < cutoff:
            self._recent_fetches.popleft()
        if self._recent_fetches:
            total = sum(c for _, c in self._recent_fetches)
            span = now - self._recent_fetches[0][0]
            speed = total / span if span > 0 else 0.0

        return {
            "dorks_total": len(self.dorks),
            "dorks_done": min(self.tasks_done // max(self.pages_per_dork, 1), len(self.dorks)),
            "urls_found": self.urls_found,
            "pages_fetched": self.pages_fetched,
            "pages_failed": self.pages_failed,
            "speed": round(speed, 1),
            "active_workers": self.active_workers,
            "total_workers": self.total_workers,
            "working_proxies": len(self.working_proxies),
            "total_proxies": self.proxy_pool.total,
            "elapsed": elapsed,
            "running": not self._stop_event.is_set() and self.tasks_done < self._tasks_total,
            "tasks_remaining": self._tasks_total - self.tasks_done,
        }

    def get_results(self) -> list[dict]:
        return [{"dork": d, "urls": sorted(u)} for d, u in self.results.items()]

    async def start(self) -> None:
        self.start_time = time.time()
        self._stop_event.clear()
        self._prepare_tasks()
        if self.task_queue.empty(): return

        initial_proxies = []
        for _ in range(self.num_proxies):
            p = await self.proxy_pool.acquire()
            if p:
                initial_proxies.append(p)
                self.working_proxies.add(p)
            else: break

        if not initial_proxies: return
        self.total_workers = len(initial_proxies) * self.WORKERS_PER_PROXY

        for i in range(self.total_workers):
            proxy = initial_proxies[i // self.WORKERS_PER_PROXY]
            self._workers.append(asyncio.create_task(self._worker(i, proxy)))

        try:
            await self.task_queue.join()
        except asyncio.CancelledError:
            pass

        for w in self._workers:
            if not w.done(): w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    async def stop(self) -> None:
        self._stop_event.set()
        for w in self._workers:
            if not w.done(): w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        await self.proxy_pool.release_all()

    def _prepare_tasks(self) -> None:
        for dork in self.dorks:
            for page in range(self.pages_per_dork):
                self.task_queue.put_nowait((dork, page * 10 + 1, page + 1))
        self._tasks_total = self.task_queue.qsize()

    async def _close_session(self, session: Optional[AsyncSession]) -> None:
        if session:
            try: await session.close()
            except Exception: pass

    async def _worker(self, worker_id: int, initial_proxy: str) -> None:
        proxy = initial_proxy
        session: Optional[AsyncSession] = None
        session_reqs = 0
        fails = 0
        unfinished = False
        self.active_workers += 1
        self.working_proxies.add(proxy)

        try:
            while not self._stop_event.is_set():
                try:
                    task = await asyncio.wait_for(self.task_queue.get(), timeout=1.0)
                    unfinished = True
                except asyncio.TimeoutError:
                    continue
                dork, start, page_num = task

                if session is None or session_reqs >= self.REQUESTS_PER_SESSION or fails >= self.PROXY_FAIL_THRESHOLD:
                    await self._close_session(session)
                    session = None
                    if fails >= self.PROXY_FAIL_THRESHOLD:
                        await self.proxy_pool.mark_dead(proxy)
                        self.working_proxies.discard(proxy)
                        proxy = await self.proxy_pool.acquire()
                        if proxy:
                            self.working_proxies.add(proxy)
                            fails = 0
                        else:
                            self.pages_failed += 1
                            self.tasks_done += 1
                            unfinished = False
                            self.task_queue.task_done()
                            continue

                    try:
                        session = AsyncSession(impersonate=get_random_fingerprint(), proxy=proxy, timeout=15, allow_redirects=True)
                    except Exception:
                        fails += 1
                        self.pages_failed += 1
                        self.tasks_done += 1
                        unfinished = False
                        self.task_queue.task_done()
                        continue
                    session_reqs = 0

                success = False
                for attempt in range(self.MAX_RETRIES):
                    if self._stop_event.is_set(): break
                    try:
                        url = f"https://search.yahoo.com/search?p={quote(dork)}&b={start}"
                        resp = await session.get(url, headers=get_random_headers())
                        session_reqs += 1

                        if resp.status_code == 200:
                            html = resp.text
                            if detect_captcha(html):
                                fails += 1
                                if fails < self.PROXY_FAIL_THRESHOLD:
                                    await asyncio.sleep(2 ** attempt)
                                    continue
                                else: break
                            
                            urls = parse_yahoo_results(html)
                            async with self._lock:
                                for u in urls: self.results[dork].add(u)
                                self.urls_found += len(urls)
                                self.pages_fetched += 1
                                self._recent_fetches.append((time.time(), len(urls)))
                            success = True
                            fails = 0
                            break
                        elif resp.status_code in (429, 403, 503):
                            fails += 1
                            if fails < self.PROXY_FAIL_THRESHOLD:
                                await asyncio.sleep(2 ** attempt + random.uniform(0, 1))
                                continue
                            else: break
                        else: break
                    except asyncio.CancelledError: raise
                    except Exception as exc:
                        fails += 1
                        if fails < self.PROXY_FAIL_THRESHOLD:
                            await asyncio.sleep(2 ** attempt + random.uniform(0, 0.5))
                            continue
                        else: break

                if not success: self.pages_failed += 1
                self.tasks_done += 1
                unfinished = False
                self.task_queue.task_done()
        except asyncio.CancelledError: pass
        finally:
            await self._close_session(session)
            self.active_workers -= 1
            if unfinished:
                try: self.task_queue.task_done()
                except ValueError: pass


# ──────────────────────────────────────────────────────────────
#  Telegram Bot Logic (bot.py)
# ──────────────────────────────────────────────────────────────

class BotState:
    def __init__(self) -> None:
        self.pages_per_dork: int = 1
        self.dork_queue: list[str] = []
        self.scan_running: bool = False
        self.scanner: Optional[Scanner] = None
        self.proxy_pool: ProxyPool = ProxyPool()
        self.scan_task: Optional[asyncio.Task] = None
        self.bot = None
        self.chat_id: Optional[int] = None

    def reset(self) -> None:
        self.dork_queue.clear()
        if self.scanner:
            self.scanner.results.clear()

state = BotState()

def _fmt(n: int) -> str:
    return f"{n:,}"

async def save_results(scanner: Scanner) -> str:
    os.makedirs("results", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join("results", f"dork_results_{ts}.json")
    results = scanner.get_results() or [{"dork": d, "urls": []} for d in scanner.dorks]
    async with aiofiles.open(path, "w", encoding="utf-8") as f:
        await f.write(json.dumps(results, indent=2, ensure_ascii=False))
    return path

async def _send_document(chat_id: int, filepath: str, caption: str) -> None:
    if not state.bot: return
    try:
        with open(filepath, "rb") as f:
            await state.bot.send_document(
                chat_id=chat_id,
                document=InputFile(f, filename=os.path.basename(filepath)),
                caption=caption,
                parse_mode="Markdown",
            )
    except Exception as exc:
        logger.error("Failed to send document: %s", exc)

async def _run_scan() -> None:
    try:
        await state.scanner.start()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.error("Scan error: %s", exc, exc_info=True)
    state.scan_running = False
    filepath = await save_results(state.scanner)
    s = state.scanner.get_status()
    caption = (
        f"✅ *Scan Complete!*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 Dorks: {s['dorks_done']}/{s['dorks_total']}\n"
        f"🔗 URLs: {_fmt(s['urls_found'])}\n"
        f"📄 Pages fetched: {s['pages_fetched']}\n"
        f"❌ Pages failed: {s['pages_failed']}\n"
        f"⏱️ Elapsed: {s['elapsed']}\n"
        f"📁 `{filepath}`"
    )
    if state.chat_id:
        await _send_document(state.chat_id, filepath, caption)

async def cmd_help(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "🤖 *Yahoo Dork Parser Bot*\n\n"
        "*Commands:*\n\n"
        "📄 `/pages <n>` — Set pages per dork (1–50). Default: 1\n"
        "➕ `/adddork <dork>` — Add a dork to the queue\n"
        "🔍 `/scan` — Start scanning all queued dorks\n"
        "📊 `/status` — Show live scan progress\n"
        "⏹️ `/stop` — Stop scan, save JSON, send summary\n"
        "🔄 `/reset` — Clear dork queue + collected URLs\n"
        "📤 `/setproxys` — Upload a .txt file with proxies\n"
        "✅ `/checkproxys` — Test all loaded proxies\n"
        "❓ `/help` — Show this message\n\n"
        "*Proxy format:* `ip:port` or `user:pass@ip:port`"
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def cmd_pages(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text(f"📄 Current pages per dork: *{state.pages_per_dork}*", parse_mode="Markdown")
        return
    try:
        n = int(ctx.args[0])
        if not 1 <= n <= 50:
            await update.message.reply_text("❌ Pages must be between 1 and 50.")
            return
    except ValueError:
        await update.message.reply_text("❌ Invalid number.", parse_mode="Markdown")
        return
    old = state.pages_per_dork
    state.pages_per_dork = n
    if state.scan_running:
        await update.message.reply_text(f"📄 Pages updated: {old} → {n}\n⚠️ Applies to the *next* scan.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"📄 Pages per dork set to *{n}*.", parse_mode="Markdown")

async def cmd_adddork(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not ctx.args:
        await update.message.reply_text("➕ Usage: `/adddork <dork_string>`", parse_mode="Markdown")
        return
    dork = " ".join(ctx.args)
    state.dork_queue.append(dork)
    msg = f"➕ Dork added (queue: {len(state.dork_queue)})\n`{dork}`"
    if state.scan_running:
        msg += "\n⚠️ Will be included in the next scan."
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if state.scan_running:
        await update.message.reply_text("⚠️ A scan is already running.\nUse /status or /stop.")
        return
    if not state.dork_queue:
        await update.message.reply_text("❌ No dorks in queue. Use /adddork first.")
        return
    if state.proxy_pool.total == 0:
        await update.message.reply_text("❌ No proxies loaded. Use /setproxys to upload a .txt file.")
        return

    state.bot = ctx.bot
    state.chat_id = update.effective_chat.id
    state.scanner = Scanner(proxy_pool=state.proxy_pool, pages_per_dork=state.pages_per_dork, dorks=list(state.dork_queue))
    state.scan_running = True
    state.scan_task = asyncio.create_task(_run_scan())

    total_tasks = len(state.dork_queue) * state.pages_per_dork
    await update.message.reply_text(
        f"🔍 *Scan started!*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📋 Dorks: {len(state.dork_queue)}\n"
        f"📄 Pages/dork: {state.pages_per_dork}\n"
        f"🌐 Proxies: {state.proxy_pool.total}\n"
        f"👥 Workers: {min(30, state.proxy_pool.total) * 8}\n"
        f"📊 Total tasks: {total_tasks}\n\n"
        f"Use /status for live progress.",
        parse_mode="Markdown",
    )

async def cmd_status(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not state.scan_running or not state.scanner:
        await update.message.reply_text(
            f"📊 *Bot Status*\n━━━━━━━━━━━━━━━━━━\n🔍 Scan: Not running\n📋 Dorks in queue: {len(state.dork_queue)}\n📄 Pages per dork: {state.pages_per_dork}\n🌐 Proxies loaded: {state.proxy_pool.total}\n💀 Dead proxies: {state.proxy_pool.dead_count}",
            parse_mode="Markdown",
        )
        return
    s = state.scanner.get_status()
    pct = s["dorks_done"] / s["dorks_total"] * 100 if s["dorks_total"] else 0
    await update.message.reply_text(
        f"📊 *Scan Status*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔍 Dorks: {s['dorks_done']}/{s['dorks_total']} ({pct:.1f}%)\n"
        f"🔗 URLs found: {_fmt(s['urls_found'])}\n"
        f"📄 Pages fetched: {s['pages_fetched']}\n"
        f"❌ Pages failed: {s['pages_failed']}\n"
        f"⚡ Speed: {s['speed']} URLs/sec\n"
        f"👥 Workers: {s['active_workers']}/{s['total_workers']}\n"
        f"🌐 Proxies: {s['working_proxies']}/{s['total_proxies']}\n"
        f"📋 Tasks remaining: {s['tasks_remaining']}\n"
        f"⏱️ Elapsed: {s['elapsed']}\n"
        f"━━━━━━━━━━━━━━━━━━",
        parse_mode="Markdown",
    )

async def cmd_stop(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not state.scan_running:
        await update.message.reply_text("❌ No scan is currently running.")
        return
    if state.scanner:
        await state.scanner.stop()
    if state.scan_task and not state.scan_task.done():
        state.scan_task.cancel()
        try: await state.scan_task
        except asyncio.CancelledError: pass
    state.scan_running = False

    filepath = await save_results(state.scanner)
    s = state.scanner.get_status()
    caption = (
        f"⏹️ *Scan Stopped*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"🔗 URLs found: {_fmt(s['urls_found'])}\n"
        f"📄 Pages fetched: {s['pages_fetched']}\n"
        f"❌ Pages failed: {s['pages_failed']}\n"
        f"⏱️ Elapsed: {s['elapsed']}\n"
        f"📁 `{filepath}`"
    )
    await _send_document(update.effective_chat.id, filepath, caption)

async def cmd_reset(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if state.scan_running:
        if state.scanner: await state.scanner.stop()
        if state.scan_task and not state.scan_task.done():
            state.scan_task.cancel()
            try: await state.scan_task
            except asyncio.CancelledError: pass
        state.scan_running = False
    state.reset()
    await update.message.reply_text("🔄 Dork queue and collected URLs cleared.")

async def cmd_setproxys(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if state.scan_running:
        await update.message.reply_text("⚠️ Cannot load proxies during scan. Stop the scan first.")
        return
    await update.message.reply_text(
        "📤 Please upload a `.txt` file with proxies.\nOne proxy per line:\n`ip:port` or `user:pass@ip:port`",
        parse_mode="Markdown",
    )

async def cmd_checkproxys(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if state.scan_running:
        await update.message.reply_text("⚠️ Cannot check proxies during scan.")
        return
    if state.proxy_pool.total == 0:
        await update.message.reply_text("❌ No proxies loaded. Use /setproxys first.")
        return
    await update.message.reply_text(f"🔍 Checking {state.proxy_pool.total} proxies… ⏳")
    alive, dead = await state.proxy_pool.check_all_proxies()
    lines = [
        "✅ *Proxy Check Results*\n", "━━━━━━━━━━━━━━━━━━",
        f"✅ Alive: {len(alive)}", f"❌ Dead: {len(dead)}",
        "━━━━━━━━━━━━━━━━━━\n", "*Alive proxies (sorted by latency):*",
    ]
    for proxy, latency in sorted(alive, key=lambda x: x[1])[:20]:
        lines.append(f"  🟢 {latency:.0f}ms — `{proxy}`")
    if len(alive) > 20: lines.append(f"  … and {len(alive) - 20} more")
    if dead:
        lines.append("\n*Dead proxies:*")
        for p in dead[:10]: lines.append(f"  🔴 `{p}`")
        if len(dead) > 10: lines.append(f"  … and {len(dead) - 10} more")
    text = "\n".join(lines)
    if len(text) > 4000: text = text[:3990] + "\n… (truncated)"
    await update.message.reply_text(text, parse_mode="Markdown")

async def handle_document(update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if state.scan_running:
        await update.message.reply_text("⚠️ Cannot load proxies during scan.")
        return
    doc = update.message.document
    if not doc: return
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Please upload a `.txt` file.")
        return
    tg_file = await doc.get_file()
    raw_bytes = await tg_file.download_as_bytearray()
    content = raw_bytes.decode("utf-8", errors="ignore")
    proxies = [line.strip() for line in content.splitlines() if line.strip() and is_valid_proxy_string(line.strip())]
    if not proxies:
        await update.message.reply_text("❌ No valid proxies found in the file.")
        return
    count = await state.proxy_pool.load_proxies(proxies)
    await update.message.reply_text(f"✅ Loaded *{count}* proxies into the pool.\nUse /checkproxys to test them.", parse_mode="Markdown")

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Unhandled exception: %s", ctx.error, exc_info=ctx.error)
    if isinstance(update, Update) and update.effective_message:
        try: await update.effective_message.reply_text(f"❌ An error occurred: `{ctx.error}`", parse_mode="Markdown")
        except Exception: pass

def main() -> None:
    token = os.environ.get("BOT_TOKEN")
    if not token:
        print("ERROR: BOT_TOKEN environment variable is not set.\nGet a token from @BotFather and run:\n  export BOT_TOKEN=your_token_here")
        return
    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("pages", cmd_pages))
    app.add_handler(CommandHandler("adddork", cmd_adddork))
    app.add_handler(CommandHandler("scan", cmd_scan))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("setproxys", cmd_setproxys))
    app.add_handler(CommandHandler("checkproxys", cmd_checkproxys))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_error_handler(error_handler)
    logger.info("Bot starting …")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
