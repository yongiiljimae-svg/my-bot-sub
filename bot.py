import os
import json
import time
import logging
import threading
import requests
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
import pytz

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger(__name__)

# ── Config from environment variables ────────────────────────────────────────
SUBSOURCE_API_KEY = os.environ["SUBSOURCE_API_KEY"]
TELEGRAM_TOKEN    = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID  = os.environ["TELEGRAM_CHAT_ID"]

# Movies to watch — loaded from environment variable as JSON
# Format: [{"title": "...", "movieId": 12345, "days": [5,6], "start": 14, "end": 20}, ...]
# days: 0=Mon,1=Tue,2=Wed,3=Thu,4=Fri,5=Sat,6=Sun
WATCHLIST_JSON = os.environ.get("WATCHLIST", "[]")
WATCHLIST = json.loads(WATCHLIST_JSON)

TIMEZONE = os.environ.get("CHECK_TIMEZONE", "Asia/Tehran")
CHECK_INTERVAL_MINUTES = int(os.environ.get("CHECK_INTERVAL_MINUTES", "30"))
LANGUAGE = os.environ.get("SUBTITLE_LANGUAGE", "english")

# Persistent state file (stores seen subtitle IDs)
STATE_FILE = "/tmp/seen_subtitles.json"

SUBSOURCE_BASE = "https://api.subsource.net/api/v1"
HEADERS = {"X-API-Key": SUBSOURCE_API_KEY}

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
        log.error(f"SubSource API error for movieId={movie_id}: {e}")
        return []

# ── Telegram ──────────────────────────────────────────────────────────────────
def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        r = requests.post(url, json=payload, timeout=10)
        r.raise_for_status()
        log.info("Telegram notification sent.")
    except Exception as e:
        log.error(f"Telegram send error: {e}")

def format_message(movie_title: str, sub: dict) -> str:
    release_info = " | ".join(sub.get("releaseInfo", [])) or "N/A"
    sub_id = sub["subtitleId"]
    link = f"https://subsource.net/subtitles/{sub_id}"
    downloads = sub.get("downloads", 0)
    rating = sub.get("rating", {})
    good = rating.get("good", 0)

    return (
        f"🎬 <b>New Subtitle Available!</b>\n\n"
        f"📽 <b>{movie_title}</b>\n"
        f"🌐 Language: <b>{sub.get('language', 'N/A').capitalize()}</b>\n"
        f"📦 Release: <b>{release_info}</b>\n"
        f"⬇️ Downloads: {downloads}\n"
        f"👍 Rating: {good}\n"
        f"🔗 <a href='{link}'>Download Subtitle</a>"
    )

# ── Schedule check ────────────────────────────────────────────────────────────
def should_check(entry: dict, tz: pytz.BaseTzInfo) -> bool:
    now = datetime.now(tz)
    day_ok = now.weekday() in entry.get("days", list(range(7)))
    hour_ok = entry.get("start", 0) <= now.hour < entry.get("end", 24)
    return day_ok and hour_ok

# ── Dummy HTTP server (keeps Render Web Service happy) ────────────────────────
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"SubSource Notifier is running!")

    def log_message(self, format, *args):
        pass  # silence HTTP logs

def start_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    log.info(f"Health server listening on port {port}")
    server.serve_forever()

# ── Main loop ─────────────────────────────────────────────────────────────────
def main():
    # Start dummy HTTP server in background thread
    t = threading.Thread(target=start_health_server, daemon=True)
    t.start()

    if not WATCHLIST:
        log.warning("WATCHLIST is empty! Set the WATCHLIST env variable.")
        send_telegram("⚠️ SubSource Bot started but WATCHLIST is empty.\nPlease set the WATCHLIST environment variable.")
        return

    tz = pytz.timezone(TIMEZONE)
    seen = load_seen()
    interval = CHECK_INTERVAL_MINUTES * 60

    log.info(f"Bot started. Watching {len(WATCHLIST)} title(s). Interval: {CHECK_INTERVAL_MINUTES}min.")
    send_telegram(
        f"✅ <b>SubSource Notifier started!</b>\n"
        f"📋 Watching <b>{len(WATCHLIST)}</b> title(s)\n"
        f"🌐 Language: <b>{LANGUAGE.capitalize()}</b>\n"
        f"⏱ Check interval: every <b>{CHECK_INTERVAL_MINUTES} min</b> during active windows"
    )

    while True:
        now = datetime.now(tz)
        log.info(f"Tick — {now.strftime('%A %Y-%m-%d %H:%M')} ({TIMEZONE})")

        for entry in WATCHLIST:
            title = entry.get("title", f"movieId:{entry.get('movieId')}")
            movie_id = entry.get("movieId")

            if not movie_id:
                log.warning(f"Skipping entry with no movieId: {entry}")
                continue

            if not should_check(entry, tz):
                log.info(f"⏭ Skipping '{title}' — outside scheduled window")
                continue

            log.info(f"🔍 Checking subtitles for '{title}' (movieId={movie_id})")
            subtitles = fetch_subtitles(movie_id, LANGUAGE)

            new_count = 0
            for sub in subtitles:
                sub_id = sub.get("subtitleId")
                if sub_id and sub_id not in seen:
                    seen.add(sub_id)
                    new_count += 1
                    msg = format_message(title, sub)
                    send_telegram(msg)
                    log.info(f"  ✅ New subtitle found: ID={sub_id}")
                    time.sleep(1)  # small delay between messages

            if new_count == 0:
                log.info(f"  No new subtitles for '{title}'")

        save_seen(seen)
        log.info(f"Sleeping {CHECK_INTERVAL_MINUTES} minutes...\n")
        time.sleep(interval)

if __name__ == "__main__":
    main()
