"""YouTube RSS -> Telegram channel announcer.

The first successful run only records videos already present in the RSS feed.
Later runs announce new entries, then save each successfully posted video ID.
"""

from __future__ import annotations

import html
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import feedparser
import requests
from dotenv import load_dotenv


# ---------------------------------------------------------------------------
# CONFIGURATION — change these settings to customize the bot.
# ---------------------------------------------------------------------------

# Load secrets from .env located next to this Python file.
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# Keep your bot token in .env, not directly in this source file.
# .env should contain: TELEGRAM_BOT_TOKEN=YOUR_NEW_TOKEN_FROM_BOTFATHER
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()

# Telegram channel ID. The bot must be added to the channel as an administrator
# with permission to post messages.
CHANNEL_ID = "-1002363892622"

# YouTube channel RSS feed.
RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id=UCiJs4ibVRUobi5GRpjI28UQ"

# Local continuous-mode polling interval (used when running without --once).
# On GitHub Actions, change the cron schedule in .github/workflows/youtube-announcer.yml.
CHECK_INTERVAL_MINUTES = 7

# Network request timeout and retry settings.
REQUEST_TIMEOUT_SECONDS = 20
MAX_TELEGRAM_ATTEMPTS = 4

# Message header for entries whose type cannot be determined from RSS.
# Keep HTML tags valid if you change the text (for example <b>...</b>).
MESSAGE_HEADER = "🎬 <b>НОВЫЙ РОЛИК НА КАНАЛЕ!</b>"

# Used only when the RSS entry's link or title clearly indicates a Short.
MESSAGE_HEADER_SHORTS = "⚡ <b>НОВЫЙ YOUTUBE SHORTS!</b>"

# Footer is optional. You can add your own hashtags and valid Telegram HTML.
MESSAGE_FOOTER = "Залетай смотреть и поддержи просмотром!\n\n#новое_видео #YouTube"

# Keep the preview enabled so Telegram can show the YouTube thumbnail/player preview.
ENABLE_LINK_PREVIEW = True

# History file is saved next to this script, not to the current working directory.
POSTED_IDS_FILE = BASE_DIR / "posted_videos.txt"

# ---------------------------------------------------------------------------
# LOGGING
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("youtube-telegram-announcer")


# ---------------------------------------------------------------------------
# RSS HELPERS
# ---------------------------------------------------------------------------

def fetch_feed_entries() -> list[Any]:
    """Download and parse the YouTube RSS feed; raise on a failed/empty feed."""
    response = requests.get(
        RSS_URL,
        timeout=REQUEST_TIMEOUT_SECONDS,
        headers={"User-Agent": "YouTubeTelegramAnnouncer/1.0"},
    )
    response.raise_for_status()

    feed = feedparser.parse(response.content)
    if getattr(feed, "bozo", False):
        # Some feeds can still be usable when the parser reports a minor issue.
        logger.warning("RSS parser warning: %s", getattr(feed, "bozo_exception", "unknown issue"))

    if not feed.entries:
        raise RuntimeError("RSS-лента не содержит записей; история не будет инициализирована.")

    return list(feed.entries)


def extract_video_id(entry: Any) -> str | None:
    """Get a YouTube video ID from common feedparser fields or the entry URL."""
    # YouTube RSS entries usually expose the yt:videoId field as yt_videoid.
    video_id = str(entry.get("yt_videoid", "") or "").strip()
    if video_id:
        return video_id

    raw_id = str(entry.get("id", "") or "")
    match = re.search(r"yt:video:([A-Za-z0-9_-]+)", raw_id)
    if match:
        return match.group(1)

    links = [str(entry.get("link", "") or "")]
    links.extend(
        str(link.get("href", ""))
        for link in entry.get("links", [])
        if isinstance(link, dict)
    )
    for url in links:
        match = re.search(r"[?&]v=([A-Za-z0-9_-]+)", url)
        if match:
            return match.group(1)
        match = re.search(r"youtube\.com/shorts/([A-Za-z0-9_-]+)", url, re.IGNORECASE)
        if match:
            return match.group(1)
        match = re.search(r"youtu\.be/([A-Za-z0-9_-]+)", url, re.IGNORECASE)
        if match:
            return match.group(1)

    return None


def is_probably_shorts(entry: Any, title: str) -> bool:
    """Detect Shorts only when the feed link/title gives a clear hint.

    YouTube's standard RSS feed usually does not reliably identify Shorts.
    A #Shorts hashtag is only a heuristic; otherwise the bot uses a generic
    announcement and the standard YouTube watch link.
    """
    urls = [str(entry.get("link", "") or "")]
    urls.extend(
        str(link.get("href", ""))
        for link in entry.get("links", [])
        if isinstance(link, dict)
    )
    if any("youtube.com/shorts/" in url.lower() for url in urls):
        return True

    return bool(re.search(r"(?:^|\s)#shorts?\b", title, flags=re.IGNORECASE))


def build_video_url(video_id: str, shorts: bool) -> str:
    """Build a stable URL for a video or, when detected, a Short."""
    if shorts:
        return f"https://www.youtube.com/shorts/{video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


# ---------------------------------------------------------------------------
# PERSISTENT VIDEO HISTORY
# ---------------------------------------------------------------------------

def read_history() -> set[str]:
    """Read posted video IDs; a missing/empty file means first-run seeding."""
    if not POSTED_IDS_FILE.exists():
        return set()

    try:
        return {
            line.strip()
            for line in POSTED_IDS_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
    except OSError as exc:
        raise RuntimeError(f"Не удалось прочитать историю {POSTED_IDS_FILE}: {exc}") from exc


def write_initial_history(video_ids: set[str]) -> None:
    """Atomically create the initial history without announcing old videos."""
    temp_file = POSTED_IDS_FILE.with_name(POSTED_IDS_FILE.name + ".tmp")
    content = "".join(f"{video_id}\n" for video_id in sorted(video_ids))
    try:
        temp_file.write_text(content, encoding="utf-8")
        temp_file.replace(POSTED_IDS_FILE)
    except OSError as exc:
        raise RuntimeError(f"Не удалось создать файл истории: {exc}") from exc


def remember_posted_video(video_id: str) -> None:
    """Persist one ID immediately after Telegram confirms a successful post."""
    try:
        with POSTED_IDS_FILE.open("a", encoding="utf-8") as history_file:
            history_file.write(video_id + "\n")
            history_file.flush()
            os.fsync(history_file.fileno())
    except OSError as exc:
        raise RuntimeError(f"Пост отправлен, но ID не удалось записать в историю: {exc}") from exc


# ---------------------------------------------------------------------------
# TELEGRAM MESSAGE SENDING
# ---------------------------------------------------------------------------

def build_telegram_message(entry: Any, video_id: str) -> str:
    """Format a neat Telegram post using HTML parse mode."""
    title = str(entry.get("title", "Новое видео") or "Новое видео").strip()
    shorts = is_probably_shorts(entry, title)
    video_url = build_video_url(video_id, shorts)

    # Escape the YouTube title to prevent accidental/broken Telegram HTML.
    safe_title = html.escape(title)
    safe_url = html.escape(video_url, quote=True)
    header = MESSAGE_HEADER_SHORTS if shorts else MESSAGE_HEADER

    message_parts = [
        header,
        f"<b>{safe_title}</b>",
        f'<a href="{safe_url}">▶️ Смотреть на YouTube</a>',
    ]
    if MESSAGE_FOOTER.strip():
        message_parts.append(MESSAGE_FOOTER.strip())

    message = "\n\n".join(message_parts)
    if len(message) > 4096:
        # Telegram's sendMessage text limit is 4096 characters after parsing.
        # RSS titles are normally short; this is a defensive fallback.
        overflow = len(message) - 4096
        shorter_title = title[: max(20, len(title) - overflow - 10)] + "…"
        safe_title = html.escape(shorter_title)
        message_parts[1] = f"<b>{safe_title}</b>"
        message = "\n\n".join(message_parts)

    return message


def send_telegram_message(message: str) -> None:
    """Send a message with retries for temporary network/server/rate-limit errors."""
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": CHANNEL_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": not ENABLE_LINK_PREVIEW,
    }

    for attempt in range(1, MAX_TELEGRAM_ATTEMPTS + 1):
        try:
            response = requests.post(api_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            if attempt >= MAX_TELEGRAM_ATTEMPTS:
                raise RuntimeError(f"Ошибка соединения с Telegram: {exc}") from exc
            delay = min(2 ** attempt, 20)
            logger.warning("Ошибка сети Telegram; повтор через %s сек. (%s/%s)", delay, attempt, MAX_TELEGRAM_ATTEMPTS)
            time.sleep(delay)
            continue

        try:
            result = response.json()
        except ValueError:
            result = {}

        if response.status_code == 429:
            retry_after = result.get("parameters", {}).get("retry_after", 5)
            if attempt < MAX_TELEGRAM_ATTEMPTS:
                delay = max(1, int(retry_after))
                logger.warning("Telegram ограничил частоту; повтор через %s сек.", delay)
                time.sleep(delay)
                continue

        if response.status_code >= 500 and attempt < MAX_TELEGRAM_ATTEMPTS:
            delay = min(2 ** attempt, 20)
            logger.warning("Ошибка сервера Telegram (%s); повтор через %s сек.", response.status_code, delay)
            time.sleep(delay)
            continue

        if not response.ok or not result.get("ok"):
            description = result.get("description", response.text[:500])
            raise RuntimeError(f"Telegram API вернул ошибку: {description}")

        return

    raise RuntimeError("Не удалось отправить сообщение в Telegram после нескольких попыток.")


# ---------------------------------------------------------------------------
# MAIN POLLING LOGIC
# ---------------------------------------------------------------------------

def check_for_new_videos() -> None:
    """Seed existing IDs once, then announce new entries oldest-to-newest."""
    entries = fetch_feed_entries()
    records: list[tuple[str, Any]] = []

    for entry in entries:
        video_id = extract_video_id(entry)
        if not video_id:
            logger.warning("Пропускаю запись RSS без ID видео: %s", entry.get("title", "(без названия)"))
            continue
        records.append((video_id, entry))

    if not records:
        raise RuntimeError("В RSS есть записи, но не удалось извлечь ни одного ID видео.")

    posted_ids = read_history()

    # On a clean install (or an empty history file), record current feed entries
    # and do not send them. This prevents a flood of old announcements on start.
    if not posted_ids:
        initial_ids = {video_id for video_id, _ in records}
        write_initial_history(initial_ids)
        logger.info(
            "Первый запуск: сохранено ID существующих видео — %s. Старые видео не публикуются.",
            len(initial_ids),
        )
        return

    # YouTube RSS is usually newest-first; reverse it so multiple new videos
    # are announced in chronological order.
    for video_id, entry in reversed(records):
        if video_id in posted_ids:
            continue

        title = str(entry.get("title", "Новое видео") or "Новое видео").strip()
        message = build_telegram_message(entry, video_id)

        # Save the ID only after Telegram confirms the post was sent.
        # If sending fails, the ID remains unrecorded and will be retried next poll.
        send_telegram_message(message)
        remember_posted_video(video_id)
        posted_ids.add(video_id)
        logger.info("Опубликовано: %s (ID: %s)", title, video_id)

    logger.info("Проверка завершена; новых видео нет либо все новые видео опубликованы.")


def main() -> None:
    """Validate configuration and run the polling loop until stopped."""
    if not BOT_TOKEN:
        raise SystemExit(
            "Не задан токен. Создай файл .env рядом со скриптом и добавь строку:\n"
            "TELEGRAM_BOT_TOKEN=НОВЫЙ_ТОКЕН_ОТ_BOTFATHER"
        )
    if not CHANNEL_ID:
        raise SystemExit("Не задан CHANNEL_ID в начале файла.")
    if not RSS_URL.startswith("https://"):
        raise SystemExit("RSS_URL должен быть HTTPS-адресом.")
    if CHECK_INTERVAL_MINUTES < 1:
        raise SystemExit("CHECK_INTERVAL_MINUTES должен быть положительным числом.")

    logger.info("Файл истории: %s", POSTED_IDS_FILE)

    # --once performs exactly one RSS check and exits. This is useful for
    # scheduled services such as GitHub Actions, where each run is a fresh VM.
    if "--once" in sys.argv:
        try:
            check_for_new_videos()
        except Exception:
            logger.exception("Ошибка проверки RSS/публикации в режиме --once.")
            raise SystemExit(1)
        return

    logger.info("Бот-анонсер запущен. Интервал проверки: %s мин.", CHECK_INTERVAL_MINUTES)
    while True:
        try:
            check_for_new_videos()
        except KeyboardInterrupt:
            logger.info("Остановка по Ctrl+C.")
            break
        except Exception:
            # Keep the process alive if RSS, Telegram, or the network temporarily fails.
            logger.exception("Ошибка проверки RSS/публикации. Следующая попытка будет по расписанию.")

        logger.info("Следующая проверка через %s мин.", CHECK_INTERVAL_MINUTES)
        try:
            time.sleep(CHECK_INTERVAL_MINUTES * 60)
        except KeyboardInterrupt:
            logger.info("Остановка по Ctrl+C.")
            break


if __name__ == "__main__":
    main()
