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
SUBSOURCE_API_KEY     = os.environ["SUBSOURCE_API_KEY"]
TELEGRAM_TOKEN        = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID      = os.environ["TELEGRAM_CHAT_ID"]
WATCHLIST             = json.loads(os.environ.get("WATCHLIST", "[]"))
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30"))
LANGUAGES             = ["english", "persian"]   # always watch both

SUBSOURCE_BASE = "https://api.subsource.net/api/v1"
HEADERS        = {"X-API-Key": SUBSOURCE_API_KEY}
STATE_FILE     = "/tmp/seen_subtitles.json"

# ── State ─────────────────────────────────────────────────────────────────────
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
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }, timeout=10)
        r.raise_for_status()
        log.info("Telegram notification sent.")
    except Exception as e:
        log.error(f"Telegram send error: {e}")

LANG_FLAG = {"english": "🇬🇧", "persian": "🇮🇷"}
LANG_NAME = {"english": "English", "persian": "فارسی"}

def format_message(movie_title: str, sub: dict) -> str:
    lang     = sub.get("language", "")
    flag     = LANG_FLAG.get(lang, "🌐")
    lang_lbl = LANG_NAME.get(lang, lang.capitalize())
    release  = " | ".join(sub.get("releaseInfo", [])) or "N/A"
    sub_id   = sub["subtitleId"]
    link     = f"https://subsource.net/subtitles/{sub_id}"
    downloads = sub.get("downloads", 0)
    good     = sub.get("rating", {}).get("good", 0)

    return (
        f"🎬 <b>New Subtitle Available!</b>\n\n"
        f"📽 <b>{movie_title}</b>\n"
        f"{flag} Language: <b>{lang_lbl}</b>\n"
        f"📦 Release: <b>{release}</b>\n"
        f"⬇️ Downloads: {downloads}  |  👍 {good}\n"
        f"🔗 <a href='{link}'>Download Subtitle</a>"
    )

# ── Dummy HTTP server (keeps Render Web Service happy) ────────────────────────
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

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    threading.Thread(target=start_health_server, daemon=True).start()

    if not WATCHLIST:
        log.warning("WATCHLIST is empty!")
        send_telegram("⚠️ Bot started but WATCHLIST is empty.\nPlease set the WATCHLIST environment variable.")
        return

    seen     = load_seen()
    interval = CHECK_INTERVAL_MINUTES * 60

    log.info(f"Bot started. Watching {len(WATCHLIST)} title(s). Interval: {CHECK_INTERVAL_MINUTES}min.")
    send_telegram(
        f"✅ <b>SubSource Notifier started!</b>\n"
        f"📋 Watching <b>{len(WATCHLIST)}</b> title(s)\n"
        f"🇬🇧 English  +  🇮🇷 فارسی\n"
        f"⏱ Checking every <b>{CHECK_INTERVAL_MINUTES} minutes</b>"
    )

    while True:
        log.info(f"--- Checking at {datetime.now().strftime('%Y-%m-%d %H:%M')} ---")

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
                        send_telegram(format_message(title, sub))
                        log.info(f"  ✅ New: ID={sub_id}")
                        time.sleep(1)

        save_seen(seen)
        log.info(f"Sleeping {CHECK_INTERVAL_MINUTES} min...")
        time.sleep(interval)

if __name__ == "__main__":
    main()
