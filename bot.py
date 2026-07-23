import asyncio
import io
import os
import random
import time
from dataclasses import dataclass, field
from urllib.parse import quote_plus, unquote

from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# -------------------- CONFIG --------------------
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_TOKEN_HERE")
MAX_CONCURRENT_PER_PROXY = 5
DEFAULT_PAGES = 3
DEFAULT_SPEED = 1.0          # requests/sec per proxy (0.5–2.0)
RESULTS_PER_PAGE = 7         # Yahoo pagination step ~7-10
REQUEST_TIMEOUT = 20
PROXY_CHECK_URL = "https://search.yahoo.com/"
PROXY_CHECK_TIMEOUT = 10

# -------------------- TLS ROTATION --------------------
IMPERSONATE_TARGETS = [
    "chrome99", "chrome100", "chrome101", "chrome104", "chrome107",
    "chrome110", "chrome116", "chrome119", "chrome120", "chrome123",
    "chrome124", "edge99", "edge101", "safari15_3", "safari15_5",
    "safari17_0", "safari17_2_ios",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36 Edg/120.0.0.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1",
]

ACCEPT_LANGS = [
    "en-US,en;q=0.9",
    "en-GB,en;q=0.8",
    "en-US,en;q=0.7,es;q=0.3",
    "en-CA,en;q=0.9,fr;q=0.6",
]

def next_fingerprint():
    return {
        "impersonate": random.choice(IMPERSONATE_TARGETS),
        "headers": {
            "User-Agent": random.choice(USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
            "Accept-Language": random.choice(ACCEPT_LANGS),
            "Accept-Encoding": "gzip, deflate, br",
            "DNT": random.choice(["1", "0"]),
            "Upgrade-Insecure-Requests": "1",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        },
    }

# -------------------- PROXY MANAGER --------------------
@dataclass
class Proxy:
    raw: str
    alive: bool = True
    fails: int = 0
    last_used: float = 0.0
    semaphore: asyncio.Semaphore = field(default=None)
    lock: asyncio.Lock = field(default=None)

    def __post_init__(self):
        self.semaphore = asyncio.Semaphore(MAX_CONCURRENT_PER_PROXY)
        self.lock = asyncio.Lock()

    @property
    def url(self):
        r = self.raw.strip()
        if "://" not in r:
            r = "http://" + r
        return r

def _normalize(line: str) -> str:
    line = line.strip()
    if not line:
        return ""
    if "://" in line:
        return line
    parts = line.split(":")
    if len(parts) == 4:          # ip:port:user:pass
        ip, port, user, pw = parts
        return f"http://{user}:{pw}@{ip}:{port}"
    return line                  # ip:port

class ProxyManager:
    def __init__(self, speed: float = DEFAULT_SPEED):
        self.proxies: list[Proxy] = []
        self.speed = speed

    def load(self, lines: list[str]):
        self.proxies = []
        seen = set()
        for ln in lines:
            norm = _normalize(ln)
            if norm and norm not in seen:
                seen.add(norm)
                self.proxies.append(Proxy(raw=norm))

    @property
    def alive_proxies(self):
        return [p for p in self.proxies if p.alive]

    async def _throttle(self, proxy: Proxy):
        min_interval = 1.0 / max(self.speed, 0.01)
        async with proxy.lock:
            wait = proxy.last_used + min_interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            proxy.last_used = time.monotonic()

    async def fetch(self, proxy: Proxy, url: str, params: dict = None):
        await self._throttle(proxy)
        fp = next_fingerprint()
        async with proxy.semaphore:
            try:
                async with AsyncSession() as s:
                    resp = await s.get(
                        url,
                        params=params,
                        headers=fp["headers"],
                        impersonate=fp["impersonate"],
                        proxies={"http": proxy.url, "https": proxy.url},
                        timeout=REQUEST_TIMEOUT,
                        allow_redirects=True,
                    )
                    if resp.status_code == 200 and "captcha" not in resp.text.lower():
                        proxy.fails = 0
                        return resp.text
                    proxy.fails += 1
            except Exception:
                proxy.fails += 1
            if proxy.fails >= 3:
                proxy.alive = False
            return None

    async def check_one(self, proxy: Proxy):
        fp = next_fingerprint()
        try:
            async with AsyncSession() as s:
                resp = await s.get(
                    PROXY_CHECK_URL,
                    headers=fp["headers"],
                    impersonate=fp["impersonate"],
                    proxies={"http": proxy.url, "https": proxy.url},
                    timeout=PROXY_CHECK_TIMEOUT,
                )
                proxy.alive = resp.status_code == 200
                proxy.fails = 0 if proxy.alive else proxy.fails + 1
        except Exception:
            proxy.alive = False
        return proxy.alive

    async def check_all(self):
        results = await asyncio.gather(*[self.check_one(p) for p in self.proxies])
        alive = sum(1 for r in results if r)
        return alive, len(self.proxies)

# -------------------- YAHOO PARSER --------------------
def _build_url(dork: str, page_index: int) -> tuple[str, dict]:
    b = page_index * RESULTS_PER_PAGE + 1
    return "https://search.yahoo.com/search", {"p": dork, "b": b}

def _parse_results(html: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    urls = []
    for a in soup.select("a"):
        href = a.get("href", "")
        if href.startswith("http") and "yahoo.com" not in href:
            urls.append(href)
        elif "/RU=" in href:
            try:
                target = href.split("/RU=")[1].split("/RK=")[0]
                urls.append(unquote(target))
            except Exception:
                pass
    seen, out = set(), []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out

class YahooParser:
    def __init__(self, proxy_manager, pages: int, on_result=None):
        self.pm = proxy_manager
        self.pages = pages
        self.on_result = on_result
        self.found = 0
        self.done_tasks = 0
        self.total_tasks = 0
        self._stop = False

    def stop(self):
        self._stop = True

    async def _scan_one(self, dork: str, page_idx: int, proxy):
        if self._stop:
            return []
        url, params = _build_url(dork, page_idx)
        html = await self.pm.fetch(proxy, url, params)
        self.done_tasks += 1
        if not html:
            return []
        results = _parse_results(html)
        self.found += len(results)
        if self.on_result and results:
            await self.on_result(dork, results)
        return results

    async def run(self, dorks: list[str]):
        alive = self.pm.alive_proxies
        if not alive:
            raise RuntimeError("No alive proxies available.")
        tasks = []
        i = 0
        for dork in dorks:
            for page_idx in range(self.pages):
                proxy = alive[i % len(alive)]
                i += 1
                tasks.append(self._scan_one(dork, page_idx, proxy))
        self.total_tasks = len(tasks)
        all_results = await asyncio.gather(*tasks, return_exceptions=True)
        merged, seen = [], set()
        for r in all_results:
            if isinstance(r, list):
                for u in r:
                    if u not in seen:
                        seen.add(u)
                        merged.append(u)
        return merged

# -------------------- SESSION --------------------
class UserSession:
    def __init__(self):
        self.proxy_manager = ProxyManager(speed=DEFAULT_SPEED)
        self.pages = DEFAULT_PAGES
        self.speed = DEFAULT_SPEED
        self.parser = None
        self.results = []
        self.running = False

    def reset(self):
        self.__init__()

SESSIONS: dict[int, UserSession] = {}

def get_session(user_id: int) -> UserSession:
    if user_id not in SESSIONS:
        SESSIONS[user_id] = UserSession()
    return SESSIONS[user_id]

# -------------------- TELEGRAM BOT --------------------
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔎 *Yahoo Dork Parser*\n\n"
        "Distributed proxy scanning with TLS + UA rotation.\n\n"
        "*Commands:*\n"
        "/setproxies – reply/upload proxy .txt\n"
        "/checkproxies – validate proxies\n"
        "/pages <n> – pages per dork (1-50)\n"
        "/speed <n> – req/s per proxy (0.5-2.0)\n"
        "/status – live progress\n"
        "/stop – stop & save\n"
        "/reset – clear session\n\n"
        "Upload a .txt (1 dork per line) to scan.",
        parse_mode="Markdown",
    )

async def setproxies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["awaiting"] = "proxies"
    await update.message.reply_text("📥 Upload your proxy .txt file now.")

async def pages(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    try:
        n = int(ctx.args[0])
        assert 1 <= n <= 50
        s.pages = n
        await update.message.reply_text(f"✅ Pages per dork: {n}")
    except Exception:
        await update.message.reply_text("Usage: /pages <1-50>")

async def speed(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    try:
        v = float(ctx.args[0])
        assert 0.5 <= v <= 2.0
        s.speed = v
        s.proxy_manager.speed = v
        await update.message.reply_text(f"✅ Speed: {v} req/s per proxy")
    except Exception:
        await update.message.reply_text("Usage: /speed <0.5-2.0>")

async def checkproxies(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    if not s.proxy_manager.proxies:
        await update.message.reply_text("No proxies loaded. Use /setproxies.")
        return
    msg = await update.message.reply_text("🕵️ Checking proxies...")
    alive, total = await s.proxy_manager.check_all()
    await msg.edit_text(f"✅ Alive: {alive}/{total}")

async def status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    if not s.parser or not s.running:
        await update.message.reply_text("Idle. No active scan.")
        return
    p = s.parser
    await update.message.reply_text(
        f"📊 *Live status*\n"
        f"Tasks: {p.done_tasks}/{p.total_tasks}\n"
        f"URLs found: {p.found}\n"
        f"Alive proxies: {len(s.proxy_manager.alive_proxies)}",
        parse_mode="Markdown",
    )

async def stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    if s.parser:
        s.parser.stop()
        s.running = False
        await _send_results(update, s)

async def reset(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    get_session(update.effective_user.id).reset()
    await update.message.reply_text("🧹 Session cleared.")

async def _send_results(update, s):
    if not s.results:
        await update.message.reply_text("No results to save.")
        return
    data = "\n".join(s.results).encode()
    await update.message.reply_document(
        document=io.BytesIO(data),
        filename="results.txt",
        caption=f"📤 {len(s.results)} URLs",
    )

async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = get_session(update.effective_user.id)
    doc = update.message.document
    file = await doc.get_file()
    content = (await file.download_as_bytearray()).decode(errors="ignore")
    lines = [l.strip() for l in content.splitlines() if l.strip()]

    if ctx.user_data.get("awaiting") == "proxies":
        s.proxy_manager.load(lines)
        ctx.user_data["awaiting"] = None
        await update.message.reply_text(
            f"✅ Loaded {len(s.proxy_manager.proxies)} proxies. "
            f"Run /checkproxies to validate."
        )
        return

    # otherwise treat as dork list -> start scan
    if not s.proxy_manager.alive_proxies:
        await update.message.reply_text(
            "⚠️ No alive proxies. Use /setproxies and /checkproxies first."
        )
        return

    await update.message.reply_text(
        f"🚀 Scanning {len(lines)} dorks × {s.pages} pages "
        f"across {len(s.proxy_manager.alive_proxies)} proxies..."
    )

    async def on_result(dork, urls):
        s.results.extend(urls)

    s.parser = YahooParser(s.proxy_manager, s.pages, on_result=on_result)
    s.running = True
    try:
        await s.parser.run(lines)
    finally:
        s.running = False
        await _send_results(update, s)

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("setproxies", setproxies))
    app.add_handler(CommandHandler("checkproxies", checkproxies))
    app.add_handler(CommandHandler("pages", pages))
    app.add_handler(CommandHandler("speed", speed))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    print("Bot running...")
    app.run_polling()

if __name__ == "__main__":
    main()
