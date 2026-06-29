# SubSource Telegram Notifier

Monitors SubSource for new subtitles and sends Telegram notifications.

## Setup on Render

1. Push this repo to GitHub
2. Create a new **Background Worker** on Render
3. Set these environment variables:

| Variable | Description | Example |
|---|---|---|
| `SUBSOURCE_API_KEY` | Your SubSource API key | `sk_291c...` |
| `TELEGRAM_TOKEN` | Your Telegram bot token | `123456:ABC...` |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID | `123456789` |
| `WATCHLIST` | JSON list of movies to watch (see below) | see example |
| `CHECK_INTERVAL_MINUTES` | How often to check (default: 30) | `30` |
| `SUBTITLE_LANGUAGE` | Language filter (default: english) | `english` |
| `CHECK_TIMEZONE` | Your timezone (default: Asia/Tehran) | `Asia/Tehran` |

## WATCHLIST Format

```json
[
  {
    "title": "Severance",
    "movieId": 128763,
    "days": [6, 0],
    "start": 14,
    "end": 20
  },
  {
    "title": "The Bear",
    "movieId": 99999,
    "days": [1],
    "start": 14,
    "end": 20
  }
]
```

- `days`: 0=Monday, 1=Tuesday, 2=Wednesday, 3=Thursday, 4=Friday, 5=Saturday, 6=Sunday
- `start`/`end`: Hour in 24h format (Tehran time)

## How to find movieId

Use the SubSource API:
```
GET https://api.subsource.net/api/v1/movies/search?searchType=imdb&imdb=tt1234567
```
The `movieId` is in the response.
