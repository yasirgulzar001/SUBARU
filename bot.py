import asyncio
import random
import re
import time
import os
import logging
from dataclasses import dataclass, field
from io import BytesIO
from typing import List, Set, Optional
from urllib.parse import quote, unquote, urlparse

import tls_client
from bs4 import BeautifulSoup
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters
)

# Image processing & OCR
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
BOT_TOKEN = "8939889745:AAEFORAmnxmL48jGS7hzxOjnQaAGW9MejLI"
AUTHORIZED_USER = 0  # 0 = allow anyone in private chat

# ==================== BLACKLIST (domains to skip) ====================
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

# ==================== SCRAPER CONFIGURATION ====================
@dataclass
class ScraperConfig:
    # Concurrency
    max_concurrent_requests: int = 10      # Max parallel Yahoo requests
    requests_per_second: float = 1.5       # Target steady rate (token bucket fill rate)
    burst_size: int = 5                    # Max instant bursts

    # Session pool
    session_pool_size: int = 15            # Number of pre-warmed TLS sessions
    session_reuse_count: int = 50          # Max requests per session before recycling

    # Retry & backoff
    max_retries: int = 3
    base_backoff: float = 2.0             # seconds

    # Parsing
    max_pages_per_dork: int = 10
    results_per_page: int = 10            # Yahoo shows 10 results per page

    # Captcha
    captcha_max_retries: int = 2

    # Adaptive throttling
    error_threshold: float = 0.3          # If error ratio > this, reduce concurrency

# ==================== TLS IDENTITIES & USER AGENTS ====================
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

def random_headers() -> dict:
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

# ==================== SESSION POOL & RATE LIMITER ====================
class SessionPool:
    """Maintains a pool of reusable TLS sessions with unique fingerprints."""
    def __init__(self, size: int = 15, max_requests_per_session: int = 50):
        self._pool: List[tls_client.Session] = []
        self._usage_count: List[int] = []
        self._lock = asyncio.Lock()
        self.max_requests = max_requests_per_session
        # Pre-warm pool
        for _ in range(size):
            self._add_session()

    def _add_session(self):
        profile = random.choice(TLS_PROFILES)
        session = tls_client.Session(
            client_identifier=profile,
            random_tls_extension_order=True,
        )
        self._pool.append(session)
        self._usage_count.append(0)

    async def get_session(self) -> tls_client.Session:
        async with self._lock:
            # Recycle exhausted sessions
            for i, count in enumerate(self._usage_count):
                if count >= self.max_requests:
                    self._pool[i] = self._create_fresh_session()
                    self._usage_count[i] = 0
            # Pick random (avoids sequential fingerprint linking)
            idx = random.randrange(len(self._pool))
            self._usage_count[idx] += 1
            return self._pool[idx]

    def _create_fresh_session(self) -> tls_client.Session:
        profile = random.choice(TLS_PROFILES)
        return tls_client.Session(
            client_identifier=profile,
            random_tls_extension_order=True,
        )

class TokenBucket:
    """Asynchronous token bucket rate limiter."""
    def __init__(self, rate: float, burst: int):
        self.rate = rate          # tokens per second
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

# ==================== CAPTCHA SOLVER (Enhanced) ====================
class CaptchaSolver:
    """Self‑contained Yahoo captcha solver using Tesseract OCR with advanced preprocessing."""
    CAPTCHA_KEYWORDS = ["captcha", "security check", "are you a robot", "sorry"]

    def __init__(self):
        # Tesseract path (adjust if needed)
        self.tesseract_cmd = pytesseract.pytesseract.tesseract_cmd or 'tesseract'

    def is_captcha_page(self, html: str) -> bool:
        return any(word in html.lower() for word in self.CAPTCHA_KEYWORDS)

    def _preprocess_image(self, image_bytes: bytes) -> bytes:
        """Heavy preprocessing for noisy captchas."""
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
        if img is None:
            return image_bytes
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        # Remove noise with bilateral filter
        denoised = cv2.bilateralFilter(gray, 9, 75, 75)
        # Increase contrast
        clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(denoised)
        # Adaptive threshold
        thresh = cv2.adaptiveThreshold(
            enhanced, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 15, 3
        )
        # Remove small blobs
        kernel = np.ones((2, 2), np.uint8)
        cleaned = cv2.morphologyEx(thresh, cv2.MORPH_OPEN, kernel, iterations=1)
        # Invert for Tesseract (black text on white)
        pil_img = Image.fromarray(255 - cleaned)
        buf = BytesIO()
        pil_img.save(buf, format="PNG")
        return buf.getvalue()

    def _ocr_image(self, image_bytes: bytes) -> str:
        processed = self._preprocess_image(image_bytes)
        img = Image.open(BytesIO(processed))
        # Config for single word alphanumeric
        config = r'--oem 3 --psm 8 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789'
        return pytesseract.image_to_string(img, config=config).strip()

    def _find_image_and_form(self, html: str, base_url: str) -> tuple[Optional[str], Optional[str]]:
        soup = BeautifulSoup(html, "lxml")
        # Find image
        img_url = None
        for img in soup.find_all("img", src=True):
            if "captcha" in img["src"].lower() or "challenge" in img["src"].lower():
                src = img["src"]
                if src.startswith("//"):
                    img_url = "https:" + src
                elif src.startswith("/"):
                    parsed = urlparse(base_url)
                    img_url = f"{parsed.scheme}://{parsed.netloc}{src}"
                else:
                    img_url = src
                break
        # Find form action
        form_action = None
        for form in soup.find_all("form"):
            if any(w in (form.get("action","")+str(form)).lower() for w in self.CAPTCHA_KEYWORDS):
                action = form.get("action")
                if action:
                    if action.startswith("//"):
                        form_action = "https:" + action
                    elif action.startswith("/"):
                        parsed = urlparse(base_url)
                        form_action = f"{parsed.scheme}://{parsed.netloc}{action}"
                    else:
                        form_action = action
                break
        return img_url, form_action

    def solve(self, session: tls_client.Session, captcha_html: str, original_url: str) -> bool:
        img_url, form_action = self._find_image_and_form(captcha_html, original_url)
        if not img_url or not form_action:
            return False

        for attempt in range(2):  # two tries with slight image adjustments
            try:
                resp = session.get(img_url, headers=random_headers(), timeout_seconds=10)
                if resp.status_code != 200:
                    continue
                answer = self._ocr_image(resp.content)
                if len(answer) < 3:
                    continue

                payload = {"captchaAnswer": answer, "answer": answer, "submit": "Continue"}
                post_resp = session.post(
                    form_action, data=payload, headers=random_headers(),
                    timeout_seconds=15, allow_redirects=True
                )
                if not self.is_captcha_page(post_resp.text):
                    return True
            except Exception as e:
                logger.warning(f"Captcha solve attempt {attempt+1} failed: {e}")
        return False

captcha_solver = CaptchaSolver()

# ==================== YAHOO SCRAPER ENGINE ====================
class YahooScraper:
    def __init__(self, config: Optional[ScraperConfig] = None):
        self.config = config or ScraperConfig()
        self.session_pool = SessionPool(
            size=self.config.session_pool_size,
            max_requests_per_session=self.config.session_reuse_count
        )
        self.rate_limiter = TokenBucket(
            rate=self.config.requests_per_second,
            burst=self.config.burst_size
        )
        self.semaphore = asyncio.Semaphore(self.config.max_concurrent_requests)
        self._error_count = 0
        self._total_requests = 0
        self._lock = asyncio.Lock()

    async def _adaptive_delay(self, success: bool):
        """Slow down if error ratio exceeds threshold."""
        async with self._lock:
            self._total_requests += 1
            if not success:
                self._error_count += 1
            error_ratio = self._error_count / max(1, self._total_requests)
            if error_ratio > self.config.error_threshold:
                # Temporarily reduce concurrency by sleeping extra
                await asyncio.sleep(random.uniform(1.0, 3.0))

    @staticmethod
    def _clean_yahoo_url(href: str) -> Optional[str]:
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

    @staticmethod
    def _is_blacklisted(url: str) -> bool:
        low = url.lower()
        return any(b in low for b in BLACKLIST)

    async def _fetch_page(self, dork: str, page: int) -> List[str]:
        """Fetch a single search results page, returning list of cleaned URLs."""
        offset = (page * 10) + 1
        query = quote(dork)
        url = f"https://search.yahoo.com/search?p={query}&b={offset}&pz=10"

        for retry in range(self.config.max_retries):
            # Rate limit before request
            await self.rate_limiter.acquire()
            async with self.semaphore:
                session = await self.session_pool.get_session()
                headers = random_headers()
                try:
                    loop = asyncio.get_running_loop()
                    # Run blocking network call in thread (tls_client is synchronous)
                    resp = await loop.run_in_executor(
                        None, lambda: session.get(url, headers=headers, timeout_seconds=25)
                    )
                    if resp.status_code == 429:
                        await self._adaptive_delay(False)
                        backoff = self.config.base_backoff * (2 ** retry) + random.uniform(0, 1)
                        await asyncio.sleep(backoff)
                        continue

                    if resp.status_code != 200:
                        await self._adaptive_delay(False)
                        return []

                    html = resp.text
                    # Captcha handling
                    if captcha_solver.is_captcha_page(html):
                        logger.info(f"Captcha on {dork} page {page}, solving...")
                        solved = await loop.run_in_executor(
                            None, captcha_solver.solve, session, html, url
                        )
                        if solved:
                            logger.info("Captcha solved, retrying request")
                            continue  # retry with same session (now has valid cookies)
                        else:
                            await self._adaptive_delay(False)
                            return []

                    # Parse
                    urls = await loop.run_in_executor(None, self._parse_results, html)
                    await self._adaptive_delay(True)
                    return urls

                except Exception as e:
                    logger.error(f"Request failed (retry {retry}): {e}")
                    await self._adaptive_delay(False)
                    backoff = self.config.base_backoff * (2 ** retry) + random.uniform(0, 1)
                    await asyncio.sleep(backoff)
        return []

    def _parse_results(self, html: str) -> List[str]:
        urls = set()
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            real = self._clean_yahoo_url(a["href"])
            if not real or not real.startswith("http"):
                continue
            if self._is_blacklisted(real):
                continue
            urls.add(real.rstrip("/"))
        return list(urls)

    async def process_dork(self, dork: str) -> Set[str]:
        """Scrape all configured pages for a single dork."""
        tasks = [
            self._fetch_page(dork, page)
            for page in range(self.config.max_pages_per_dork)
        ]
        results = await asyncio.gather(*tasks)
        found = set()
        for lst in results:
            found.update(lst)
        return found

# ==================== TELEGRAM BOT SESSION & HANDLERS ====================
class UserSession:
    def __init__(self):
        self.running = False
        self.stop_flag = False
        self.config = ScraperConfig()
        self.dorks: List[str] = []
        self.found_urls: Set[str] = set()
        self.current_dork = 0
        self.total_dorks = 0
        self.status_msg = None

sessions: dict[int, UserSession] = {}

def get_session(uid: int) -> UserSession:
    if uid not in sessions:
        sessions[uid] = UserSession()
    return sessions[uid]

def authorized(uid: int) -> bool:
    return AUTHORIZED_USER == 0 or uid == AUTHORIZED_USER

# Command handlers (similar to before but using UserSession.config)
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    text = (
        "🔎 *Yahoo Mass Dork Parser — High‑Speed Proxyless*\n\n"
        "⚡ Session pool · Adaptive rate limit · Captcha bypass\n"
        "🚀 Handles 20k+ dorks efficiently\n\n"
        "*Commands:*\n"
        "/pages `<n>` – pages per dork (1-50)\n"
        "/speed `<n>` – max requests/sec (0.5‑3.0)\n"
        "/status – live progress\n"
        "/stop – stop & save\n"
        "/reset – clear session\n"
        "/help – this message\n\n"
        "📤 Upload a `.txt` file (1 dork/line) to start."
    )
    await update.message.reply_text(text, parse_mode="Markdown")

async def help_cmd(update, context): await start(update, context)

async def set_pages(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    s = get_session(update.effective_user.id)
    try:
        n = int(context.args[0])
        if not 1 <= n <= 50:
            raise ValueError
        s.config.max_pages_per_dork = n
        await update.message.reply_text(f"✅ Pages per dork set to *{n}*.", parse_mode="Markdown")
    except:
        await update.message.reply_text("Usage: `/pages 40`", parse_mode="Markdown")

async def set_speed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    s = get_session(update.effective_user.id)
    try:
        rate = float(context.args[0])
        if not 0.5 <= rate <= 3.0:
            raise ValueError
        s.config.requests_per_second = rate
        # Also adjust burst and concurrency proportionally
        s.config.burst_size = max(2, int(rate * 3))
        s.config.max_concurrent_requests = max(2, int(rate * 5))
        await update.message.reply_text(
            f"✅ Speed set to *{rate}* req/s (concurrency: {s.config.max_concurrent_requests}).",
            parse_mode="Markdown"
        )
    except:
        await update.message.reply_text("Usage: `/speed 2.5`", parse_mode="Markdown")

async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    s = get_session(update.effective_user.id)
    if not s.running:
        await update.message.reply_text("💤 No active scan.")
        return
    await update.message.reply_text(
        f"📊 *Live Status*\n\n"
        f"🔗 URLs found: `{len(s.found_urls)}`\n"
        f"📝 Dork: `{s.current_dork}/{s.total_dorks}`\n"
        f"📄 Pages/dork: `{s.config.max_pages_per_dork}`\n"
        f"⚡ Speed: `{s.config.requests_per_second}` req/s",
        parse_mode="Markdown"
    )

async def stop_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    s = get_session(update.effective_user.id)
    if not s.running:
        await update.message.reply_text("💤 Nothing to stop.")
        return
    s.stop_flag = True
    await update.message.reply_text("🛑 Stopping... Results will be sent shortly.")

async def reset_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not authorized(update.effective_user.id): return
    sessions[update.effective_user.id] = UserSession()
    await update.message.reply_text("♻️ Session reset.")

async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not authorized(uid): return
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
    dorks = list(dict.fromkeys(line.strip() for line in raw.splitlines() if line.strip()))

    if not dorks:
        await update.message.reply_text("❌ File is empty.")
        return

    s.dorks = dorks
    s.total_dorks = len(dorks)
    s.found_urls.clear()
    s.current_dork = 0
    s.stop_flag = False

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🚀 Start ({len(dorks)} dorks)", callback_data="start_scan")]
    ])
    await update.message.reply_text(
        f"✅ Loaded *{len(dorks)}* dorks.\n"
        f"⚡ Speed: *{s.config.requests_per_second}* req/s\n"
        f"📄 Pages per dork: *{s.config.max_pages_per_dork}*\n\n"
        f"Press button to begin.",
        parse_mode="Markdown",
        reply_markup=kb
    )

async def run_scan(context, uid: int, chat_id: int):
    s = get_session(uid)
    s.running = True
    s.stop_flag = False

    scraper = YahooScraper(s.config)
    s.status_msg = await context.bot.send_message(
        chat_id,
        "🔎 *Scanning with adaptive engine...*\n\nCaptcha bypass active.",
        parse_mode="Markdown"
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

        # Live update every 2 seconds
        now = time.monotonic()
        if now - last_update > 2:
            last_update = now
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=s.status_msg.message_id,
                    text=(
                        f"🔎 *Live Results*\n\n"
                        f"🔗 URLs: `{len(s.found_urls)}`\n"
                        f"📝 Dork: `{s.current_dork}/{s.total_dorks}`\n"
                        f"⚡ Speed: `{s.config.requests_per_second}` req/s"
                    ),
                    parse_mode="Markdown"
                )
            except:
                pass

    # Finalize
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
        f"📝 Dorks processed: `{s.current_dork}/{s.total_dorks}`",
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
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("pages", set_pages))
    app.add_handler(CommandHandler("speed", set_speed))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("stop", stop_cmd))
    app.add_handler(CommandHandler("reset", reset_cmd))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(CallbackQueryHandler(button_handler))

    logger.info("🚀 Yahoo Dork Parser bot running (proxyless, high‑speed)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
