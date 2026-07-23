import asyncio
import random
import re
import time
import logging
from dataclasses import dataclass
from typing import List, Set, Optional, Dict, Tuple
from urllib.parse import quote, unquote
from io import BytesIO

from curl_cffi.requests import AsyncSession
from selectolax.parser import HTMLParser

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
    ConversationHandler
)

# Captcha OCR
import pytesseract
import cv2
import numpy as np
from PIL import Image

# ==================== LOGGING ====================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==================== CONFIG ====================
BOT_TOKEN = "8939889745:AAEFORAmnxmL48jGS7hzxOjnQaAGW9MejLI"   # <-- replace with your bot token
AUTHORIZED_USER = 0            # 0 = anyone can use in private chat

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

TLS_PROFILES = [
    "chrome120", "chrome119", "chrome116", "chrome110", "chrome107",
    "safari16_0", "safari15_5", "firefox120", "firefox110",
    "opera91", "edge101",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:120.0) Gecko/20100101 Firefox/120.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
]

def random_headers() -> Dict[str, str]:
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": random.choice(["en-US,en;q=0.9", "en-GB,en;q=0.8", "en;q=0.7"]),
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Cache-Control": "max-age=0",
    }

# ==================== CAPTCHA SOLVER (FULL) ====================
class CaptchaSolver:
    CAPTCHA_KEYWORDS = ["captcha", "security check", "are you a robot", "sorry"]

    def is_captcha_page(self, html: str) -> bool:
        return any(word in html.lower() for word in self.CAPTCHA_KEYWORDS)

    def _preprocess_image(self, image_bytes: bytes) -> bytes:
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return image_bytes
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        denoised = cv2.bilateralFilter(gray, 9, 75, 75)
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(denoised)
        thresh = cv2.adaptiveThreshold(
            enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 15, 3
        )
        kernel = np.ones((2, 2), np.uint8)
        cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)
        pil_img = Image.fromarray(255 - cleaned)
        buf = BytesIO()
        pil_img.save(buf, format="PNG")
        return buf.getvalue()

    def _ocr(self, image_bytes: bytes) -> str:
        processed = self._preprocess_image(image_bytes)
        img = Image.open(BytesIO(processed))
        config = r'--oem 3 --psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
        return pytesseract.image_to_string(img, config=config).strip()

    def _find_image_and_form(self, html: str, base_url: str) -> Tuple[Optional[str], Optional[str]]:
        tree = HTMLParser(html)
        img_url = None
        for node in tree.css("img[src]"):
            src = node.attributes.get("src", "")
            if "captcha" in src.lower() or "challenge" in src.lower():
                if src.startswith("//"):
                    img_url = "https:" + src
                elif src.startswith("/"):
                    from urllib.parse import urlparse
                    parsed = urlparse(base_url)
                    img_url = f"{parsed.scheme}://{parsed.netloc}{src}"
                else:
                    img_url = src
                break
        form_action = None
        for node in tree.css("form"):
            action = node.attributes.get("action", "")
            if action and any(w in action.lower() for w in self.CAPTCHA_KEYWORDS):
                if action.startswith("//"):
                    form_action = "https:" + action
                elif action.startswith("/"):
                    from urllib.parse import urlparse
                    parsed = urlparse(base_url)
                    form_action = f"{parsed.scheme}://{parsed.netloc}{action}"
                else:
                    form_action = action
                break
        return img_url, form_action

    async def solve_async(self, session: AsyncSession, captcha_html: str, original_url: str) -> bool:
        img_url, form_action = self._find_image_and_form(captcha_html, original_url)
        if not img_url or not form_action:
            return False
        for _ in range(2):
            try:
                resp = await session.get(img_url, headers=random_headers(), timeout=10)
                if resp.status_code != 200:
                    continue
                loop = asyncio.get_running_loop()
                answer = await loop.run_in_executor(None, self._ocr, resp.content)
                if len(answer) < 3:
                    continue
                payload = {"captchaAnswer": answer, "answer": answer, "submit": "Continue"}
                post_resp = await session.post(form_action, data=payload, headers=random_headers(), timeout=15, allow_redirects=True)
                if not self.is_captcha_page(post_resp.text):
                    return True
            except Exception as e:
                logger.warning(f"Captcha solve error: {e}")
        return False

captcha_solver = CaptchaSolver()

# ==================== PER-PROXY CONFIG ====================
@dataclass
class ProxyConfig:
    proxy_url: str
    fingerprint: str = "chrome120"
    max_concurrency: int = 5
    requests_per_second: float = 1.0
    burst_size: int = 3
    valid: bool = False

# ==================== PROXY CHECKER ====================
class ProxyChecker:
    def __init__(self, proxy_configs: List[ProxyConfig], check_interval: int = 300):
        self.configs = proxy_configs
        self.check_interval = check_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def _test_proxy(self, cfg: ProxyConfig) -> Tuple[bool, float, str]:
        headers = random_headers()
        session = AsyncSession(
            impersonate=cfg.fingerprint,
            proxy=cfg.proxy_url,
            timeout=10,
            verify=False
        )
        try:
            # 1. Connectivity + anonymity (ipify)
            start = time.monotonic()
            resp = await session.get("https://api.ipify.org?format=json", headers=headers)
            if resp.status_code != 200:
                return (False, 0, f"ipify status {resp.status_code}")
            try:
                data = resp.json()
                if not data.get("ip"):
                    return (False, 0, "No IP returned")
            except Exception:
                return (False, 0, "Invalid JSON from ipify")

            # 2. Yahoo compatibility (search test)
            yahoo_resp = await session.get("https://search.yahoo.com/search?p=test&pz=1", headers=headers, timeout=15)
            if yahoo_resp.status_code != 200:
                return (False, 0, f"Yahoo status {yahoo_resp.status_code}")
            if captcha_solver.is_captcha_page(yahoo_resp.text):
                return (False, 0, "Yahoo captcha/block")

            latency = time.monotonic() - start
            return (True, latency, "")
        except Exception as e:
            return (False, 0, str(e))
        finally:
            await session.close()

    async def check_all(self) -> int:
        logger.info("🕵️ Checking proxies...")
        tasks = [self._test_proxy(cfg) for cfg in self.configs]
        results = await asyncio.gather(*tasks)
        valid_count = 0
        for cfg, (ok, lat, err) in zip(self.configs, results):
            cfg.valid = ok
            if ok:
                logger.info(f"✅ {cfg.proxy_url[:30]}... ({lat:.2f}s)")
                valid_count += 1
            else:
                logger.warning(f"❌ {cfg.proxy_url[:30]}... {err}")
        logger.info(f"🔎 {valid_count}/{len(self.configs)} proxies valid")
        return valid_count

    async def periodic_check(self):
        self._running = True
        while self._running:
            await self.check_all()
            await asyncio.sleep(self.check_interval)

    def start_background(self):
        self._task = asyncio.create_task(self.periodic_check())

    def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()

# ==================== TOKEN BUCKET ====================
class TokenBucket:
    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self.tokens = burst
        self.last_refill = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            self.tokens = min(self.burst, self.tokens + elapsed * self.rate)
            self.last_refill = now
            if self.tokens < 1:
                wait = (1 - self.tokens) / self.rate
                await asyncio.sleep(wait)
                self.tokens = 0
            else:
                self.tokens -= 1

# ==================== PROXY WORKER ====================
class ProxyWorker:
    def __init__(self, cfg: ProxyConfig):
        self.cfg = cfg
        self.session: Optional[AsyncSession] = None
        self.semaphore = asyncio.Semaphore(cfg.max_concurrency)
        self.token_bucket = TokenBucket(cfg.requests_per_second, cfg.burst_size)
        self.error_count = 0
        self.total_requests = 0

    async def start(self):
        if not self.cfg.valid:
            raise ValueError(f"Proxy {self.cfg.proxy_url} is invalid")
        self.session = AsyncSession(
            impersonate=self.cfg.fingerprint,
            proxy=self.cfg.proxy_url,
            timeout=20,
            verify=False
        )

    async def stop(self):
        if self.session:
            await self.session.close()

    async def fetch(self, url: str) -> Optional[str]:
        async with self.semaphore:
            await self.token_bucket.acquire()
            try:
                self.total_requests += 1
                resp = await self.session.get(url, headers=random_headers())
                if resp.status_code == 429:
                    self.error_count += 1
                    await asyncio.sleep(5)
                    return None
                if resp.status_code != 200:
                    self.error_count += 1
                    return None
                return resp.text
            except Exception as e:
                self.error_count += 1
                logger.warning(f"Worker {self.cfg.proxy_url[:20]}... error: {e}")
                return None

    @property
    def is_healthy(self) -> bool:
        if self.total_requests == 0:
            return True
        return (self.error_count / self.total_requests) < 0.3

# ==================== YAHOO FIXED PROXY SCRAPER ====================
class YahooFixedProxyScraper:
    def __init__(self, proxy_configs: List[ProxyConfig], dork_pages: int = 10):
        self.proxy_configs = proxy_configs
        self.pages_per_dork = dork_pages
        self.workers: List[ProxyWorker] = []
        self._next_worker_idx = 0
        self._lock = asyncio.Lock()
        self.checker = ProxyChecker(proxy_configs)

    async def start(self):
        await self.checker.check_all()
        self.checker.start_background()
        self.workers = []
        for cfg in self.proxy_configs:
            if cfg.valid:
                worker = ProxyWorker(cfg)
                await worker.start()
                self.workers.append(worker)
        if not self.workers:
            raise RuntimeError("No valid proxies available!")
        logger.info(f"🚀 Scraper started with {len(self.workers)} proxies")

    async def stop(self):
        self.checker.stop()
        await asyncio.gather(*(w.stop() for w in self.workers))

    async def _get_worker(self) -> ProxyWorker:
        async with self._lock:
            # Try to find a healthy worker
            for _ in range(len(self.workers)):
                idx = self._next_worker_idx
                self._next_worker_idx = (idx + 1) % len(self.workers)
                if self.workers[idx].is_healthy:
                    return self.workers[idx]
            # All unhealthy – wait until one recovers
            while True:
                await asyncio.sleep(0.5)
                for w in self.workers:
                    if w.is_healthy:
                        return w

    @staticmethod
    def clean_yahoo_url(href: str) -> Optional[str]:
        if "/RU=" in href:
            m = re.search(r"/RU=([^/]+)/RK=", href)
            if m:
                return unquote(m.group(1))
        if href.startswith("http"):
            return href
        return None

    @staticmethod
    def is_blacklisted(url: str) -> bool:
        return any(b in url.lower() for b in BLACKLIST)

    def _parse_results(self, html: str) -> List[str]:
        urls = set()
        tree = HTMLParser(html)
        for node in tree.css("a[href]"):
            href = node.attributes.get("href", "")
            real = self.clean_yahoo_url(href)
            if real and real.startswith("http") and not self.is_blacklisted(real):
                urls.add(real.rstrip("/"))
        return list(urls)

    async def _fetch_page(self, dork: str, page: int) -> List[str]:
        offset = (page * 10) + 1
        query = quote(dork)
        url = f"https://search.yahoo.com/search?p={query}&b={offset}&pz=10"

        for attempt in range(3):
            worker = await self._get_worker()
            html = await worker.fetch(url)
            if html is None:
                await asyncio.sleep(random.uniform(0.5, 1.5))
                continue

            # Captcha solving
            if captcha_solver.is_captcha_page(html):
                logger.info(f"Captcha on {dork} page {page} via {worker.cfg.proxy_url[:20]}... solving")
                solved = await captcha_solver.solve_async(worker.session, html, url)
                if solved:
                    # Retry immediately with the same worker (session now has valid cookies)
                    html = await worker.fetch(url)
                    if html and not captcha_solver.is_captcha_page(html):
                        return self._parse_results(html)
                # If not solved, try another worker
                await asyncio.sleep(2)
                continue

            return self._parse_results(html)
        return []

    async def process_dork(self, dork: str) -> Set[str]:
        tasks = [self._fetch_page(dork, p) for p in range(self.pages_per_dork)]
        results = await asyncio.gather(*tasks)
        found = set()
        for lst in results:
            found.update(lst)
        return found

# ==================== TELEGRAM SESSION & STATES ====================
WAITING_FOR_PROXY_FILE = 1

class UserSession:
    def __init__(self):
        self.running = False
        self.stop_flag = False
        self.pages_per_dork = 10
        self.dorks: List[str] = []
        self.found_urls: Set[str] = set()
        self.current_dork = 0
        self.total_dorks = 0
        self.status_msg = None
        self.proxy_configs: List[ProxyConfig] = []

sessions: Dict[int, UserSession] = {}

def get_session(uid: int) -> UserSession:
    if uid not in sessions:
        sessions[uid] = UserSession()
    return sessions[uid]

def authorized(uid: int) -> bool:
    return AUTHORIZED_USER == 0 or uid == AUTHORIZED_USER

# ==================== BOT COMMANDS ====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    text = (
        "🔎 *Yahoo Dork Parser — Fixed Proxies + Auto Checker*\n\n"
        "⚡ 50 proxies × 5 concurrent = 1000+ URLs/sec\n"
        "🕵️ Auto proxy validation & captcha bypass\n\n"
        "*Commands:*\n"
        "/setproxies – Upload proxy list\n"
        "/checkproxies – Validate current proxies\n"
        "/pages `<n>` – pages per dork (1‑50)\n"
        "/speed `<n>` – req/s per proxy (0.5‑2.0)\n"
        "/status – live progress\n"
        "/stop – stop & save\n"
        "/reset – clear session\n\n"
        "📤 Upload a `.txt` file (1 dork per line) to scan."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def help_cmd(update, context): await start(update, context)

async def set_pages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    s = get_session(update.effective_user.id)
    try:
        n = int(context.args[0])
        if not 1 <= n <= 50: raise ValueError
        s.pages_per_dork = n
        await update.message.reply_text(f"✅ Pages per dork set to *{n}*.", parse_mode="Markdown")
    except:
        await update.message.reply_text("Usage: `/pages 40`", parse_mode="Markdown")

async def set_speed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    s = get_session(update.effective_user.id)
    try:
        rate = float(context.args[0])
        if not 0.5 <= rate <= 2.0: raise ValueError
        for cfg in s.proxy_configs:
            cfg.requests_per_second = rate
            cfg.burst_size = max(2, int(rate * 3))
        await update.message.reply_text(f"✅ Speed set to *{rate}* req/s per proxy.", parse_mode="Markdown")
    except:
        await update.message.reply_text("Usage: `/speed 1.5`", parse_mode="Markdown")

async def status_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    s = get_session(update.effective_user.id)
    if not s.running:
        await update.message.reply_text("💤 No active scan.")
        return
    await update.message.reply_text(
        f"📊 *Live Status*\n\n"
        f"🔗 URLs: `{len(s.found_urls)}`\n"
        f"📝 Dork: `{s.current_dork}/{s.total_dorks}`\n"
        f"📄 Pages/dork: `{s.pages_per_dork}`",
        parse_mode="Markdown"
    )

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    s = get_session(update.effective_user.id)
    if not s.running:
        await update.message.reply_text("💤 Nothing to stop.")
        return
    s.stop_flag = True
    await update.message.reply_text("🛑 Stopping... Results will be sent.")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    sessions[update.effective_user.id] = UserSession()
    await update.message.reply_text("♻️ Session reset.")

# ==================== PROXY UPLOAD (FLEXIBLE FORMAT) ====================
async def set_proxies_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    await update.message.reply_text(
        "📎 Upload a `.txt` file with proxies.\n"
        "Formats accepted:\n"
        "`ip:port`\n"
        "`ip:port:user:pass`\n"
        "`http://user:pass@ip:port`\n"
        "`http://ip:port`"
    )
    return WAITING_FOR_PROXY_FILE

async def receive_proxy_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid): return ConversationHandler.END
    s = get_session(uid)

    doc = update.message.document
    if not doc or not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Must be a `.txt` file. Try again with /setproxies.")
        return ConversationHandler.END

    file = await doc.get_file()
    data = await file.download_as_bytearray()
    lines = data.decode("utf-8", errors="ignore").strip().splitlines()
    configs = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Remove protocol if present
        line = re.sub(r'^https?://', '', line)

        # Pattern 1: user:pass@host:port
        m = re.match(r'^([^:]+):([^@]+)@([^:]+):(\d+)$', line)
        if m:
            user, pwd, host, port = m.groups()
            proxy_url = f"http://{user}:{pwd}@{host}:{port}"
            fingerprint = random.choice(TLS_PROFILES)
            configs.append(ProxyConfig(proxy_url, fingerprint=fingerprint))
            continue

        # Pattern 2: host:port:user:pass (no @)
        parts = line.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            proxy_url = f"http://{user}:{pwd}@{host}:{port}"
            fingerprint = random.choice(TLS_PROFILES)
            configs.append(ProxyConfig(proxy_url, fingerprint=fingerprint))
            continue

        # Pattern 3: host:port (no auth)
        if len(parts) == 2 and parts[1].isdigit():
            host, port = parts
            proxy_url = f"http://{host}:{port}"
            fingerprint = random.choice(TLS_PROFILES)
            configs.append(ProxyConfig(proxy_url, fingerprint=fingerprint))
            continue

        logger.warning(f"Invalid proxy line: {line}")

    if not configs:
        await update.message.reply_text(
            "❌ No valid proxies found. Supported formats:\n"
            "`ip:port`\n`ip:port:user:pass`\n`http://user:pass@ip:port`"
        )
        return ConversationHandler.END

    s.proxy_configs = configs
    # Automatically check proxies immediately
    await update.message.reply_text(f"✅ Loaded *{len(configs)}* proxies. Running automatic check...", parse_mode="Markdown")
    checker = ProxyChecker(s.proxy_configs)
    valid = await checker.check_all()
    await update.message.reply_text(
        f"🕵️ Automatic check complete: *{valid}/{len(configs)}* valid.\n"
        f"You can also run `/checkproxies` again later.",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def check_proxies_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    s = get_session(update.effective_user.id)
    if not s.proxy_configs:
        await update.message.reply_text("❌ No proxies loaded. Use /setproxies first.")
        return
    msg = await update.message.reply_text("🕵️ Checking proxies...")
    checker = ProxyChecker(s.proxy_configs)
    valid = await checker.check_all()
    await msg.edit_text(f"✅ Proxy check complete: *{valid}/{len(s.proxy_configs)}* valid.", parse_mode="Markdown")

# ==================== DORK FILE UPLOAD ====================
async def handle_dork_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid): return
    s = get_session(uid)
    if s.running:
        await update.message.reply_text("⚠️ Scan already running. /stop first.")
        return

    doc = update.message.document
    if not doc.file_name.endswith(".txt"):
        await update.message.reply_text("❌ Please upload a `.txt` file.")
        return

    file = await doc.get_file()
    data = await file.download_as_bytearray()
    raw = data.decode("utf-8", errors="ignore")
    dorks = list(dict.fromkeys(line.strip() for line in raw.splitlines() if line.strip()))
    if not dorks:
        await update.message.reply_text("❌ File is empty.")
        return

    s.dorks = dorks
    s.total_dorks = len(dorks)
    s.found_urls.clear()
    s.current_dork = 0
    s.stop_flag = False

    valid_count = sum(1 for c in s.proxy_configs if c.valid)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🚀 Start Scan ({len(dorks)} dorks)", callback_data="start_scan")]
    ])
    await update.message.reply_text(
        f"✅ Loaded *{len(dorks)}* dorks.\n"
        f"⚡ Proxies: `{len(s.proxy_configs)}` loaded (`{valid_count}` valid)\n"
        f"📄 Pages per dork: `{s.pages_per_dork}`\n\n"
        f"Press start to begin.",
        parse_mode="Markdown",
        reply_markup=kb
    )

async def run_scan(context, uid: int, chat_id: int):
    s = get_session(uid)
    s.running = True
    s.stop_flag = False

    if not s.proxy_configs or not any(c.valid for c in s.proxy_configs):
        await context.bot.send_message(chat_id, "❌ No valid proxies. Use /setproxies and /checkproxies first.")
        s.running = False
        return

    scraper = YahooFixedProxyScraper(s.proxy_configs, s.pages_per_dork)
    await scraper.start()
    try:
        s.status_msg = await context.bot.send_message(
            chat_id, "🔎 *Scanning with fixed proxies...*", parse_mode="Markdown"
        )
        last_update = time.monotonic()
        for i, dork in enumerate(s.dorks, 1):
            if s.stop_flag:
                break
            s.current_dork = i
            try:
                urls = await scraper.process_dork(dork)
                s.found_urls.update(urls)
            except Exception as e:
                logger.error(f"Dork error: {e}")

            now = time.monotonic()
            if now - last_update > 2:
                last_update = now
                try:
                    await context.bot.edit_message_text(
                        chat_id=chat_id,
                        message_id=s.status_msg.message_id,
                        text=f"🔎 *Live Results*\n\n"
                             f"🔗 URLs: `{len(s.found_urls)}`\n"
                             f"📝 Dork: `{s.current_dork}/{s.total_dorks}`\n"
                             f"⚡ Proxies alive: `{sum(w.is_healthy for w in scraper.workers)}`/{len(scraper.workers)}",
                        parse_mode="Markdown"
                    )
                except:
                    pass
    finally:
        await scraper.stop()

    s.running = False
    urls = sorted(s.found_urls)
    if not urls:
        await context.bot.send_message(chat_id, "❌ No URLs found.")
        return
    content = "\n".join(urls)
    buf = BytesIO(content.encode("utf-8"))
    buf.name = f"yahoo_results_{int(time.time())}.txt"
    await context.bot.send_message(
        chat_id, f"✅ *Scan Complete!*\n\n🔗 Total URLs: `{len(urls)}`",
        parse_mode="Markdown"
    )
    await context.bot.send_document(chat_id, document=buf, filename=buf.name)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    if not authorized(uid): return
    await query.answer()

    if query.data == "start_scan":
        s = get_session(uid)
        if s.running:
            await query.edit_message_text("⚠️ Already running.")
            return
        if not s.dorks:
            await query.edit_message_text("❌ No dorks loaded.")
            return
        await query.edit_message_text("🚀 Scan started! Live updates below.")
        asyncio.create_task(run_scan(context, uid, query.message.chat_id))

# ==================== MAIN ====================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Conversation handler for proxy upload
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("setproxies", set_proxies_start)],
        states={
            WAITING_FOR_PROXY_FILE: [MessageHandler(filters.Document.ALL, receive_proxy_file)],
        },
        fallbacks=[CommandHandler("cancel", lambda u,c: ConversationHandler.END)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pages", set_pages))
    app.add_handler(CommandHandler("speed", set_speed))
    app.add_handler(CommandHandler("status", status_cmd))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(CommandHandler("checkproxies", check_proxies_cmd))
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_dork_file))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("🚀 Yahoo Fixed Proxy Bot running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
