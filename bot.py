import os
import json
import time
import logging
import threading
import requests
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import pytz

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
SUBSOURCE_API_KEY      = os.environ["SUBSOURCE_API_KEY"]
TELEGRAM_TOKEN         = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID       = os.environ["TELEGRAM_CHAT_ID"]
WATCHLIST              = json.loads(os.environ.get("WATCHLIST", "[]"))
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30"))
LANGUAGES              = ["english", "persian"]

SUBSOURCE_BASE = "https://api.subsource.net/api/v1"
HEADERS        = {"X-API-Key": SUBSOURCE_API_KEY}
STATE_FILE     = "/tmp/seen_subtitles.json"

# ── Shared state (accessible from both threads) ───────────────────────────────
_seen: set = set()
_seen_lock = threading.Lock()
_last_check: datetime = None
_bot_start_time: datetime = datetime.now()
_force_check = threading.Event()   # set this to trigger immediate check

# ── State helpers ─────────────────────────────────────────────────────────────
def load_seen() -> set:
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_seen(seen: set):
    with open(STATE_FILE, "w") as f:
        json.dump(list(seen), f)

# ── SubSource API ─────────────────────────────────────────────────────────────
_slug_cache: dict = {}

def fetch_movie_slug(movie_id: int) -> str:
    try:
        r = requests.get(f"{SUBSOURCE_BASE}/movies/{movie_id}", headers=HEADERS, timeout=15)
        r.raise_for_status()
        return r.json().get("data", {}).get("slug", "")
    except Exception as e:
        log.error(f"Could not fetch slug for movieId={movie_id}: {e}")
        return ""

def get_movie_slug(movie_id: int) -> str:
    if movie_id not in _slug_cache:
        _slug_cache[movie_id] = fetch_movie_slug(movie_id)
    return _slug_cache[movie_id]

def fetch_subtitles(movie_id: int, language: str) -> list:
    try:
        r = requests.get(
            f"{SUBSOURCE_BASE}/subtitles",
            params={"movieId": movie_id, "language": language, "sort": "newest", "limit": 20},
            headers=HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("data", [])
    except Exception as e:
        log.error(f"SubSource API error for movieId={movie_id} lang={language}: {e}")
        return []

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text: str, chat_id: str = None):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": chat_id or TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        r.raise_for_status()
    except Exception as e:
        log.error(f"Telegram send error: {e}")

def get_updates(offset: int = None) -> list:
    try:
        params = {"timeout": 10}
        if offset:
            params["offset"] = offset
        r = requests.get(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates",
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        return r.json().get("result", [])
    except Exception:
        return []

LANG_FLAG = {"english": "🇬🇧", "persian": "🇮🇷"}
LANG_NAME = {"english": "English", "persian": "فارسی"}

def format_subtitle_message(movie_title: str, movie_id: int, sub: dict) -> str:
    lang      = sub.get("language", "")
    flag      = LANG_FLAG.get(lang, "🌐")
    lang_lbl  = LANG_NAME.get(lang, lang.capitalize())
    release   = " | ".join(sub.get("releaseInfo", [])) or "N/A"
    sub_id    = sub["subtitleId"]
    downloads = sub.get("downloads", 0)
    good      = sub.get("rating", {}).get("good", 0)

    slug = get_movie_slug(movie_id)
    link = f"https://subsource.net/subtitle/{slug}/{lang}/{sub_id}" if slug else f"https://subsource.net/subtitles/{sub_id}"

    return (
        f"🎬 <b>New Subtitle Available!</b>\n\n"
        f"📽 <b>{movie_title}</b>\n"
        f"{flag} Language: <b>{lang_lbl}</b>\n"
        f"📦 Release: <b>{release}</b>\n"
        f"⬇️ Downloads: {downloads}  |  👍 {good}\n"
        f"🔗 <a href='{link}'>Download Subtitle</a>"
    )

# ── Command handlers ──────────────────────────────────────────────────────────
def handle_status(chat_id: str):
    uptime = datetime.now() - _bot_start_time
    hours, rem = divmod(int(uptime.total_seconds()), 3600)
    minutes = rem // 60

    last = _last_check.strftime("%Y-%m-%d %H:%M") if _last_check else "Not yet"

    titles = "\n".join(f"  • {e.get('title', e.get('movieId'))}" for e in WATCHLIST) or "  (empty)"

    send_telegram(
        f"✅ <b>Bot is alive!</b>\n\n"
        f"⏱ Uptime: <b>{hours}h {minutes}m</b>\n"
        f"🕐 Last check: <b>{last}</b>\n"
        f"⏰ Interval: every <b>{CHECK_INTERVAL_MINUTES} min</b>\n"
        f"📋 Watching:\n{titles}",
        chat_id=chat_id,
    )

def handle_check(chat_id: str):
    send_telegram("🔍 Checking now, please wait...", chat_id=chat_id)
    _force_check.set()

# ── Telegram command listener (runs in background thread) ─────────────────────
def command_listener():
    offset = None
    log.info("Command listener started.")
    while True:
        updates = get_updates(offset)
        for update in updates:
            offset = update["update_id"] + 1
            msg = update.get("message", {})
            text = msg.get("text", "").strip()
            chat_id = str(msg.get("chat", {}).get("id", ""))

            # Only accept commands from the authorized chat
            if chat_id != TELEGRAM_CHAT_ID:
                continue

            if text == "/status":
                log.info("Command: /status")
                handle_status(chat_id)
            elif text == "/check":
                log.info("Command: /check")
                handle_check(chat_id)

        time.sleep(2)

# ── Core check logic ──────────────────────────────────────────────────────────
def run_check():
    global _last_check
    _last_check = datetime.now()
    log.info(f"--- Checking at {_last_check.strftime('%Y-%m-%d %H:%M')} ---")

    new_total = 0
    with _seen_lock:
        seen = set(_seen)

    for entry in WATCHLIST:
        title    = entry.get("title", f"movieId:{entry.get('movieId')}")
        movie_id = entry.get("movieId")
        if not movie_id:
            continue

        for lang in LANGUAGES:
            log.info(f"🔍 '{title}' [{lang}]")
            for sub in fetch_subtitles(movie_id, lang):
                sub_id = sub.get("subtitleId")
                if sub_id and sub_id not in seen:
                    seen.add(sub_id)
                    send_telegram(format_subtitle_message(title, movie_id, sub))
                    log.info(f"  ✅ New: ID={sub_id}")
                    new_total += 1
                    time.sleep(1)

    with _seen_lock:
        _seen.update(seen)
    save_seen(_seen)
    return new_total

# ── Dummy HTTP server ─────────────────────────────────────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"SubSource Notifier is running!")
    def log_message(self, *args):
        pass

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthHandler).serve_forever()

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _seen

    threading.Thread(target=start_health_server, daemon=True).start()

    if not WATCHLIST:
        log.warning("WATCHLIST is empty!")
        send_telegram("⚠️ Bot started but WATCHLIST is empty.\nPlease set the WATCHLIST environment variable.")
        return

    _seen = load_seen()
    interval = CHECK_INTERVAL_MINUTES * 60

    threading.Thread(target=command_listener, daemon=True).start()

    log.info(f"Bot started. Watching {len(WATCHLIST)} title(s). Interval: {CHECK_INTERVAL_MINUTES}min.")
    send_telegram(
        f"✅ <b>SubSource Notifier started!</b>\n"
        f"📋 Watching <b>{len(WATCHLIST)}</b> title(s)\n"
        f"🇬🇧 English  +  🇮🇷 فارسی\n"
        f"⏱ Checking every <b>{CHECK_INTERVAL_MINUTES} minutes</b>\n\n"
        f"Commands:\n/status — check if bot is alive\n/check — force check now"
    )

    while True:
        run_check()

        # Wait for interval OR until /check command arrives
        _force_check.clear()
        _force_check.wait(timeout=interval)

if __name__ == "__main__":
    main()
