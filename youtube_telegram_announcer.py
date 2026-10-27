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

# Only this Telegram account may use bot commands. Other users are ignored.
AUTHORIZED_USER_ID = 1454026540
COMMAND_CHAT_ID = str(AUTHORIZED_USER_ID)

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

# Posts use a YouTube thumbnail as a Telegram photo with an inline button.
# If the high-resolution thumbnail is unavailable, the sender falls back to hqdefault.

# History file is saved next to this script, not to the current working directory.
POSTED_IDS_FILE = BASE_DIR / "posted_videos.txt"
TELEGRAM_OFFSET_FILE = BASE_DIR / "telegram_update_offset.txt"

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
    """Build a compact caption for a thumbnail-based Telegram channel post."""
    title = str(entry.get("title", "Новое видео") or "Новое видео").strip()
    shorts = is_probably_shorts(entry, title)
    header = MESSAGE_HEADER_SHORTS if shorts else MESSAGE_HEADER

    # Telegram photo captions have a 1024-character limit after parsing.
    max_title_length = 700
    if len(title) > max_title_length:
        title = title[: max_title_length - 1].rstrip() + "…"

    parts = [header, f"<b>{html.escape(title)}</b>"]
    if MESSAGE_FOOTER.strip():
        parts.append(MESSAGE_FOOTER.strip())
    message = "\n\n".join(parts)
    if len(message) > 1024:
        # Keep the post valid even if the configured footer is unusually long.
        message = message[:1000].rstrip()
    return message


def send_telegram_message(message: str, video_id: str | None = None, shorts: bool = False) -> None:
    """Send a thumbnail post with a YouTube button, retrying transient errors."""
    if not video_id:
        # Compatibility fallback for any plain-text announcement call.
        api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": CHANNEL_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        response = requests.post(api_url, json=payload, timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            result = response.json()
        except ValueError:
            result = {}
        if not response.ok or not result.get("ok"):
            raise RuntimeError(f"Telegram API вернул ошибку: {result.get('description', response.text[:500])}")
        return

    video_url = build_video_url(video_id, shorts)
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    payload = {
        "chat_id": CHANNEL_ID,
        "caption": message,
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[{"text": "▶ Смотреть на YouTube", "url": video_url}]]
        },
    }

    # maxresdefault is widescreen and crisp when available; hqdefault is the reliable fallback.
    thumbnail_urls = [
        f"https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg",
        f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
    ]
    last_error = "неизвестная ошибка"
    for thumbnail_url in thumbnail_urls:
        photo_payload = {**payload, "photo": thumbnail_url}
        for attempt in range(1, MAX_TELEGRAM_ATTEMPTS + 1):
            try:
                response = requests.post(api_url, json=photo_payload, timeout=REQUEST_TIMEOUT_SECONDS)
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt >= MAX_TELEGRAM_ATTEMPTS:
                    break
                delay = min(2 ** attempt, 20)
                logger.warning("Ошибка сети Telegram; повтор через %s сек. (%s/%s)", delay, attempt, MAX_TELEGRAM_ATTEMPTS)
                time.sleep(delay)
                continue

            try:
                result = response.json()
            except ValueError:
                result = {}

            if response.ok and result.get("ok"):
                return

            last_error = result.get("description", response.text[:500])
            if response.status_code == 429 and attempt < MAX_TELEGRAM_ATTEMPTS:
                delay = max(1, int(result.get("parameters", {}).get("retry_after", 5)))
                logger.warning("Telegram ограничил частоту; повтор через %s сек.", delay)
                time.sleep(delay)
                continue
            if response.status_code >= 500 and attempt < MAX_TELEGRAM_ATTEMPTS:
                delay = min(2 ** attempt, 20)
                logger.warning("Ошибка сервера Telegram (%s); повтор через %s сек.", response.status_code, delay)
                time.sleep(delay)
                continue
            # A bad/missing max-resolution image should not prevent the post;
            # move to hqdefault. Other errors will also get one safe fallback attempt.
            break

    # Last resort: publish text with the button if Telegram cannot fetch either image.
    fallback_payload = {
        "chat_id": CHANNEL_ID,
        "text": message,
        "parse_mode": "HTML",
        "reply_markup": payload["reply_markup"],
        "disable_web_page_preview": True,
    }
    fallback_response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        json=fallback_payload,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    try:
        fallback_result = fallback_response.json()
    except ValueError:
        fallback_result = {}
    if not fallback_response.ok or not fallback_result.get("ok"):
        raise RuntimeError(
            "Не удалось отправить пост с обложкой или текстовый запасной вариант: "
            f"{fallback_result.get('description', last_error)}"
        )
    logger.warning("Опубликован текстовый пост без обложки: %s", last_error)


# ---------------------------------------------------------------------------
# PRIVATE TELEGRAM COMMANDS (run once from a scheduled GitHub Actions job)
# ---------------------------------------------------------------------------

def send_command_reply(chat_id: str | int, message: str) -> None:
    """Send a plain-text reply to the authorized user's private chat."""
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(
        api_url,
        json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    try:
        result = response.json()
    except ValueError:
        result = {}
    if not response.ok or not result.get("ok"):
        raise RuntimeError(f"Не удалось ответить на команду Telegram: {result.get('description', response.text[:300])}")


def read_update_offset() -> int:
    """Read the next Telegram update ID to request; defaults to the beginning."""
    try:
        raw = TELEGRAM_OFFSET_FILE.read_text(encoding="utf-8").strip()
        return max(0, int(raw)) if raw else 0
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Не удалось прочитать {TELEGRAM_OFFSET_FILE}: {exc}") from exc


def save_update_offset(offset: int) -> None:
    """Persist the next update ID so scheduled runs don't repeat commands."""
    temp_file = TELEGRAM_OFFSET_FILE.with_name(TELEGRAM_OFFSET_FILE.name + ".tmp")
    temp_file.write_text(f"{offset}\n", encoding="utf-8")
    temp_file.replace(TELEGRAM_OFFSET_FILE)


def get_last_posted_video() -> tuple[str, str] | None:
    """Return the newest RSS video that is already recorded as posted."""
    posted_ids = read_history()
    for entry in fetch_feed_entries():
        video_id = extract_video_id(entry)
        if video_id and video_id in posted_ids:
            title = str(entry.get("title", "Новое видео") or "Новое видео").strip()
            shorts = is_probably_shorts(entry, title)
            return title, build_video_url(video_id, shorts)
    return None


def handle_private_command(chat_id: str | int, command_text: str) -> None:
    """Run one allowlisted command and reply to the authorized private chat."""
    command = command_text.strip().split()[0].split("@", 1)[0].lower() if command_text.strip() else ""

    if command in ("/start", "/help"):
        send_command_reply(
            chat_id,
            "Привет! Я бот-анонсер MiniCot.\n\n"
            "/status — состояние бота и число сохранённых видео\n"
            "/check — проверить YouTube прямо сейчас\n"
            "/last — последнее опубликованное видео\n"
            "/help — список команд",
        )
    elif command == "/status":
        send_command_reply(
            chat_id,
            "Бот работает.\n"
            f"Видео в истории: {len(read_history())}\n"
            "Проверка YouTube по расписанию: примерно раз в 30 минут.\n"
            "Команды проверяются примерно раз в 15 минут.",
        )
    elif command == "/check":
        try:
            check_for_new_videos()
            send_command_reply(chat_id, "Проверка YouTube завершена. Если появилось новое видео, я отправил пост в канал.")
        except Exception as exc:
            logger.exception("Ошибка при выполнении команды /check")
            send_command_reply(chat_id, f"Не удалось проверить YouTube: {str(exc)[:700]}")
    elif command == "/last":
        try:
            latest = get_last_posted_video()
            if latest:
                title, url = latest
                send_command_reply(chat_id, f"Последнее опубликованное видео:\n{title}\n{url}")
            else:
                send_command_reply(chat_id, "В истории пока нет опубликованных видео.")
        except Exception as exc:
            logger.exception("Ошибка при выполнении команды /last")
            send_command_reply(chat_id, f"Не удалось получить последнее видео: {str(exc)[:700]}")
    else:
        send_command_reply(chat_id, "Неизвестная команда. Напиши /help, чтобы увидеть список команд.")


def process_telegram_commands_once() -> None:
    """Fetch pending updates; fully ignore users other than the configured owner."""
    offset = read_update_offset()
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    response = requests.get(
        api_url,
        params={"offset": offset, "limit": 100, "timeout": 0, "allowed_updates": '["message"]'},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram getUpdates вернул ошибку: {result.get('description', 'неизвестная ошибка')}")

    updates = result.get("result", [])
    if not updates:
        logger.info("Новых Telegram-команд нет.")
        return

    for update in updates:
        update_id = int(update.get("update_id", 0))
        message = update.get("message") or {}
        sender = message.get("from") or {}
        chat = message.get("chat") or {}
        text = str(message.get("text", "") or "").strip()

        # Only process messages sent by the owner in their private chat.
        # All other users/chats are silently ignored, but their update IDs are
        # advanced so they don't remain queued forever.
        if (int(sender.get("id", 0) or 0) == AUTHORIZED_USER_ID
                and str(chat.get("type", "")) == "private"
                and str(chat.get("id", "")) == COMMAND_CHAT_ID
                and text.startswith("/")):
            handle_private_command(chat["id"], text)
        else:
            logger.info("Пропущено обновление Telegram без ответа (не владелец или не личный чат).")

        offset = update_id + 1
        save_update_offset(offset)

    logger.info("Обработано обновлений Telegram: %s", len(updates))


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
        shorts = is_probably_shorts(entry, title)

        # Save the ID only after Telegram confirms the post was sent.
        # If sending fails, the ID remains unrecorded and will be retried next poll.
        send_telegram_message(message, video_id=video_id, shorts=shorts)
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

    if "--commands-once" in sys.argv:
        try:
            process_telegram_commands_once()
        except Exception:
            logger.exception("Ошибка обработки Telegram-команд.")
            raise SystemExit(1)
        return

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
