import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from dotenv import load_dotenv

from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import CommandStart
from aiogram.types import (
    FSInputFile,
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedVideo,
    InputTextMessageContent,
    Message,
)
from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

load_dotenv()

# =======================
# GLOBAL OPTIONS
# =======================

BOT_TOKEN = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
BOT_USERNAME = os.getenv("BOT_USERNAME", "ttsavefrom_bot").lstrip("@")
ENABLE_CASHE = os.getenv("ENABLE_CASHE", "1") == "1"

DUMP_CHAT_ID = os.getenv("DUMP_CHAT_ID")

CACHE_DIR = Path(os.getenv("CACHE_DIR", "./cache"))
CACHE_INDEX_FILE = CACHE_DIR / "index.json"

CACHE_TTL_SECONDS = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
CACHE_CLEAN_INTERVAL_SECONDS = int(os.getenv("CACHE_CLEAN_INTERVAL_SECONDS", "60"))

DOWNLOAD_TIMEOUT_SECONDS = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "15"))

# Нужен для inline-режима, когда видео еще не имеет Telegram file_id.
# Создай приватный канал/группу, добавь бота, дай право отправлять сообщения
# и укажи chat_id, например -1001234567890.
DUMP_CHAT_ID = os.getenv("DUMP_CHAT_ID")

# Telegram Bot API при обычной загрузке multipart ограничивает видео примерно 50 MB.
MAX_VIDEO_SIZE_BYTES = int(os.getenv("MAX_VIDEO_SIZE_MB", "48")) * 1024 * 1024

TELEGRAM_UPLOAD_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_UPLOAD_TIMEOUT_SECONDS", "120"))
TELEGRAM_UPLOAD_RETRIES = int(os.getenv("TELEGRAM_UPLOAD_RETRIES", "3"))

# =======================
# LOGGING
# =======================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("ttsavefrom_bot")


# =======================
# URL VALIDATION
# =======================

URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)
LONG_TIKTOK_VIDEO_ID_RE = re.compile(r"/video/(\d+)", re.IGNORECASE)


def normalize_url(url: str) -> str:
    url = url.strip().strip(".,;!?)»\"'")
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", "", ""))


def is_tiktok_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    host = parsed.netloc.lower()
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def extract_tiktok_url(text: str | None) -> str | None:
    if not text:
        return None

    for match in URL_RE.findall(text):
        url = normalize_url(match)
        if is_tiktok_url(url):
            return url

    return None


def parse_video_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    match = LONG_TIKTOK_VIDEO_ID_RE.search(parsed.path)
    if match:
        return match.group(1)
    return None


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def safe_inline_id(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:64]


# =======================
# CACHE
# =======================

@dataclass
class VideoResult:
    video_id: str
    path: Path
    source_url: str
    title: str | None = None
    telegram_file_id: str | None = None
    from_cache: bool = False
    temp_dir: Path | None = None


_index_lock = asyncio.Lock()
_video_locks: dict[str, asyncio.Lock] = {}


def ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not CACHE_INDEX_FILE.exists():
        CACHE_INDEX_FILE.write_text(
            json.dumps({"videos": {}, "aliases": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def load_index_sync() -> dict:
    ensure_cache_dir()
    try:
        return json.loads(CACHE_INDEX_FILE.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Cache index is broken, recreating it")
        return {"videos": {}, "aliases": {}}


def save_index_sync(index: dict) -> None:
    ensure_cache_dir()
    tmp = CACHE_INDEX_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CACHE_INDEX_FILE)


async def load_index() -> dict:
    async with _index_lock:
        return await asyncio.to_thread(load_index_sync)


async def save_index(index: dict) -> None:
    async with _index_lock:
        await asyncio.to_thread(save_index_sync, index)


def cache_record_to_result(video_id: str, record: dict) -> VideoResult | None:
    path = Path(record.get("path", ""))
    if not path.exists():
        return None

    return VideoResult(
        video_id=video_id,
        path=path,
        source_url=record.get("source_url", ""),
        title=record.get("title"),
        telegram_file_id=record.get("telegram_file_id"),
        from_cache=True,
    )


async def find_cached_by_video_id(video_id: str) -> VideoResult | None:
    if not ENABLE_CASHE:
        return None

    index = await load_index()
    record = index.get("videos", {}).get(video_id)
    if not record:
        return None

    result = cache_record_to_result(video_id, record)
    if result:
        return result

    index.get("videos", {}).pop(video_id, None)
    await save_index(index)
    return None


async def find_cached_by_url(url: str) -> VideoResult | None:
    if not ENABLE_CASHE:
        return None

    index = await load_index()
    alias = index.get("aliases", {}).get(url_hash(url))
    if not alias:
        return None

    record = index.get("videos", {}).get(alias)
    if not record:
        return None

    result = cache_record_to_result(alias, record)
    if result:
        return result

    index.get("videos", {}).pop(alias, None)
    index.get("aliases", {}).pop(url_hash(url), None)
    await save_index(index)
    return None


async def put_cache(result: VideoResult, source_url: str) -> None:
    if not ENABLE_CASHE:
        return

    index = await load_index()
    index.setdefault("videos", {})
    index.setdefault("aliases", {})

    index["videos"][result.video_id] = {
        "path": str(result.path),
        "source_url": source_url,
        "title": result.title,
        "telegram_file_id": result.telegram_file_id,
        "created_at": int(time.time()),
    }
    index["aliases"][url_hash(source_url)] = result.video_id

    await save_index(index)


async def update_cache_telegram_file_id(video_id: str, telegram_file_id: str) -> None:
    if not ENABLE_CASHE:
        return

    index = await load_index()
    record = index.get("videos", {}).get(video_id)
    if not record:
        return

    record["telegram_file_id"] = telegram_file_id
    await save_index(index)


async def cache_heartbeat() -> None:
    ensure_cache_dir()

    while True:
        try:
            if ENABLE_CASHE:
                now = int(time.time())
                index = await load_index()
                videos = index.get("videos", {})
                aliases = index.get("aliases", {})

                expired_ids: list[str] = []
                for video_id, record in list(videos.items()):
                    created_at = int(record.get("created_at", 0))
                    path = Path(record.get("path", ""))

                    if now - created_at >= CACHE_TTL_SECONDS:
                        expired_ids.append(video_id)
                        try:
                            if path.exists():
                                path.unlink()
                        except Exception:
                            logger.exception("Failed to delete cached file: %s", path)

                for video_id in expired_ids:
                    videos.pop(video_id, None)

                for alias_hash, video_id in list(aliases.items()):
                    if video_id in expired_ids:
                        aliases.pop(alias_hash, None)

                if expired_ids:
                    await save_index(index)
                    logger.info("Cache heartbeat deleted %d expired video(s)", len(expired_ids))

        except Exception:
            logger.exception("Cache heartbeat failed")

        await asyncio.sleep(CACHE_CLEAN_INTERVAL_SECONDS)


# =======================
# YT-DLP
# =======================

class PublicBotError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def classify_download_error(exc: Exception) -> str:
    text = str(exc).strip()
    lower = text.lower()

    http_match = re.search(r"HTTP Error\s+(\d{3})|status code\s+(\d{3})|HTTP\s+(\d{3})", text)
    if http_match:
        code = next(group for group in http_match.groups() if group)
        return f"HTTP {code}: {text[:500]}"

    if "timed out" in lower or "timeout" in lower:
        return f"таймаут: {text[:500]}"

    return f"внутренняя ошибка скачивания: {text[:500]}"


def ytdlp_base_options() -> dict:
    return {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": DOWNLOAD_TIMEOUT_SECONDS,
        "retries": 1,
        "fragment_retries": 1,
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            )
        },
    }


def probe_info_sync(url: str) -> dict:
    options = ytdlp_base_options()
    options.update({"skip_download": True})

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def download_video_sync(url: str, output_dir: Path) -> tuple[dict, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    options = ytdlp_base_options()
    options.update(
        {
            "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        }
    )

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        clean_info = ydl.sanitize_info(info)

        filepath = None
        for item in clean_info.get("requested_downloads") or []:
            if item.get("filepath"):
                filepath = item["filepath"]
                break

        if not filepath:
            guessed = Path(ydl.prepare_filename(info))
            candidates = list(output_dir.glob(f"{clean_info.get('id', '*')}.*"))
            if guessed.exists():
                filepath = str(guessed)
            elif candidates:
                filepath = str(candidates[0])

        if not filepath:
            raise RuntimeError("yt-dlp did not return downloaded filepath")

        path = Path(filepath)
        if not path.exists():
            raise RuntimeError(f"downloaded file does not exist: {path}")

        return clean_info, path


async def to_thread_ytdlp(func, *args):
    try:
        return await asyncio.to_thread(func, *args)
    except DownloadError as exc:
        raise PublicBotError(classify_download_error(exc)) from exc


async def get_or_download_video(url: str) -> VideoResult:
    if not is_tiktok_url(url):
        raise PublicBotError("невалидная ссылка: поддерживаются только ссылки TikTok")

    cached = await find_cached_by_url(url)
    if cached:
        return cached

    parsed_video_id = parse_video_id_from_url(url)
    if parsed_video_id:
        cached = await find_cached_by_video_id(parsed_video_id)
        if cached:
            return cached

    info = await to_thread_ytdlp(probe_info_sync, url)
    video_id = str(info.get("id") or parsed_video_id or url_hash(url))
    title = info.get("title")

    cached = await find_cached_by_video_id(video_id)
    if cached:
        return cached

    lock = _video_locks.setdefault(video_id, asyncio.Lock())
    async with lock:
        cached = await find_cached_by_video_id(video_id)
        if cached:
            return cached

        if ENABLE_CASHE:
            output_dir = CACHE_DIR
        else:
            output_dir = Path(tempfile.mkdtemp(prefix="ttbot_"))

        info, path = await to_thread_ytdlp(download_video_sync, url, output_dir)

        if path.stat().st_size > MAX_VIDEO_SIZE_BYTES:
            if not ENABLE_CASHE:
                shutil.rmtree(output_dir, ignore_errors=True)
            raise PublicBotError(
                f"видео слишком большое: {path.stat().st_size // 1024 // 1024} MB, "
                f"лимит {MAX_VIDEO_SIZE_BYTES // 1024 // 1024} MB"
            )

        result = VideoResult(
            video_id=str(info.get("id") or video_id),
            path=path,
            source_url=url,
            title=info.get("title") or title,
            temp_dir=None if ENABLE_CASHE else output_dir,
        )

        await put_cache(result, url)
        return result


# =======================
# TELEGRAM HELPERS
# =======================

async def safe_delete(message: Message | None) -> None:
    if not message:
        return

    try:
        await message.delete()
    except TelegramBadRequest:
        pass
    except Exception:
        logger.exception("Failed to delete message")


def public_error_message(exc: Exception) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return f"таймаут {DOWNLOAD_TIMEOUT_SECONDS} секунд"

    if isinstance(exc, PublicBotError):
        return exc.message

    return f"внутренняя ошибка: {type(exc).__name__}: {str(exc)[:500]}"


async def send_video_without_caption(message: Message, result: VideoResult) -> None:
    last_error: Exception | None = None

    for attempt in range(1, TELEGRAM_UPLOAD_RETRIES + 1):
        try:
            if result.telegram_file_id:
                sent = await message.answer_video(
                    video=result.telegram_file_id,
                    caption=None,
                    supports_streaming=True,
                    request_timeout=TELEGRAM_UPLOAD_TIMEOUT_SECONDS,
                )
            else:
                sent = await message.answer_video(
                    video=FSInputFile(result.path),
                    caption=None,
                    supports_streaming=True,
                    request_timeout=TELEGRAM_UPLOAD_TIMEOUT_SECONDS,
                )

            if sent.video and sent.video.file_id:
                await update_cache_telegram_file_id(result.video_id, sent.video.file_id)

            if result.temp_dir and result.temp_dir.exists():
                shutil.rmtree(result.temp_dir, ignore_errors=True)

            return

        except TelegramNetworkError as exc:
            last_error = exc
            logger.warning(
                "Telegram upload failed, attempt %s/%s: %s",
                attempt,
                TELEGRAM_UPLOAD_RETRIES,
                exc,
            )

            if attempt < TELEGRAM_UPLOAD_RETRIES:
                await asyncio.sleep(2 * attempt)
                continue

        except Exception as exc:
            last_error = exc
            raise

    raise PublicBotError(
        f"ошибка отправки видео в Telegram после {TELEGRAM_UPLOAD_RETRIES} попыток: {last_error}"
    )


async def ensure_telegram_file_id(bot: Bot, result: VideoResult) -> str:
    if result.telegram_file_id:
        return result.telegram_file_id

    if not DUMP_CHAT_ID:
        raise PublicBotError(
            "внутренняя ошибка: DUMP_CHAT_ID не задан. "
            "Для inline-режима нужен служебный чат/канал, куда бот загрузит видео "
            "и получит Telegram file_id."
        )

    sent = await bot.send_video(
        chat_id=int(DUMP_CHAT_ID) if DUMP_CHAT_ID.lstrip("-").isdigit() else DUMP_CHAT_ID,
        video=FSInputFile(result.path),
        caption=None,
        supports_streaming=True,
        request_timeout=TELEGRAM_UPLOAD_TIMEOUT_SECONDS,
    )

    if not sent.video or not sent.video.file_id:
        raise PublicBotError("внутренняя ошибка: Telegram не вернул video.file_id")

    await update_cache_telegram_file_id(result.video_id, sent.video.file_id)

    if result.temp_dir and result.temp_dir.exists():
        shutil.rmtree(result.temp_dir, ignore_errors=True)

    return sent.video.file_id


# =======================
# BOT HANDLERS
# =======================

dp = Dispatcher()


@dp.startup()
async def on_startup() -> None:
    ensure_cache_dir()
    asyncio.create_task(cache_heartbeat())
    logger.info("Bot started. ENABLE_CASHE=%s CACHE_TTL_SECONDS=%s", ENABLE_CASHE, CACHE_TTL_SECONDS)


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        "Привет! Отправь ссылку на TikTok-видео, и я отправлю его сюда.\n\n"
        f"Также можно использовать inline-режим: @{BOT_USERNAME} https://www.tiktok.com/..."
    )


@dp.message(F.text)
async def message_handler(message: Message) -> None:
    url = extract_tiktok_url(message.text)
    if not url:
        return

    loading = await message.answer("Загрузка началась")

    try:
        result = await asyncio.wait_for(
            get_or_download_video(url),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )

        await safe_delete(loading)
        await send_video_without_caption(message, result)

    except Exception as exc:
        error_text = public_error_message(exc)
        logger.exception("Failed to handle message download: %s", error_text)

        await safe_delete(loading)
        await message.answer(f"Ошибка: {error_text}")


@dp.inline_query()
async def inline_query_handler(inline_query: InlineQuery, bot: Bot) -> None:
    url = extract_tiktok_url(inline_query.query)

    if not url:
        result = InlineQueryResultArticle(
            id="help",
            title="Вставь ссылку TikTok",
            description=f"Пример: @{BOT_USERNAME} https://www.tiktok.com/...",
            input_message_content=InputTextMessageContent(
                message_text="Пришли ссылку на TikTok-видео."
            ),
        )
        await inline_query.answer([result], cache_time=0, is_personal=True)
        return

    try:
        video = await asyncio.wait_for(
            get_or_download_video(url),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )

        telegram_file_id = await asyncio.wait_for(
            ensure_telegram_file_id(bot, video),
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )

        result = InlineQueryResultCachedVideo(
            id=safe_inline_id(video.video_id),
            video_file_id=telegram_file_id,
            title="Отправить видео",
            description="Видео готово",
            caption=None,
        )

        await inline_query.answer([result], cache_time=0, is_personal=True)

    except Exception as exc:
        error_text = public_error_message(exc)
        logger.exception("Failed to handle inline download: %s", error_text)

        result = InlineQueryResultArticle(
            id=safe_inline_id(f"error:{url}:{error_text}"),
            title="Не получилось скачать видео",
            description=error_text[:120],
            input_message_content=InputTextMessageContent(
                message_text=f"Ошибка: {error_text}"
            ),
        )
        await inline_query.answer([result], cache_time=0, is_personal=True)


async def main() -> None:
    if not BOT_TOKEN or BOT_TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise RuntimeError("Set BOT_TOKEN env variable")

    bot = Bot(token=BOT_TOKEN)
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())