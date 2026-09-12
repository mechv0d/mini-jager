import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultAudio,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultPhoto,
    InlineQueryResultVideo,
    InputTextMessageContent,
    Message,
    CallbackQuery,
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
)

from app.cache_service import cache_heartbeat, ensure_cache_dir, find_cached_by_url
from app.config import settings
from app.download_service import (
    get_or_download_media,
    probe_inline_media,
)
from app.models import DownloadResult
from app.telegram_service import (
    ensure_photo_file_ids,
    ensure_telegram_file_id,
    public_error_message,
    safe_delete,
    send_download_result,
)
from app.lang import t
from app.url_utils import (
    extract_tiktok_url,
    normalize_url,
    safe_inline_id,
)

logger = logging.getLogger("ttsavefrom_bot.handlers")

dp = Dispatcher()

_INLINE_READY_TTL_SECONDS = 30 * 60
_INLINE_PHOTO_PREFETCH_TIMEOUT_SECONDS = 9
_inline_ready_media: dict[str, tuple[float, DownloadResult]] = {}


def inline_loading_result_id(url: str) -> str:
    return safe_inline_id(f"loading:{normalize_url(url)}")


def remember_inline_ready_media(result: DownloadResult, *urls: str) -> None:
    now = time.time()
    for raw_url in (result.source_url, *urls):
        if raw_url:
            _inline_ready_media[normalize_url(raw_url)] = (now, result)


def find_inline_ready_media(url: str) -> DownloadResult | None:
    now = time.time()
    for key, (created_at, _result) in list(_inline_ready_media.items()):
        if now - created_at > _INLINE_READY_TTL_SECONDS:
            _inline_ready_media.pop(key, None)

    item = _inline_ready_media.get(normalize_url(url))
    if not item:
        return None

    return item[1]


def build_photo_album_inline_results(result: DownloadResult) -> list[InlineQueryResultCachedPhoto | InlineQueryResultPhoto | InlineQueryResultVideo | InlineQueryResultAudio]:
    results: list[InlineQueryResultCachedPhoto | InlineQueryResultPhoto | InlineQueryResultVideo | InlineQueryResultAudio] = []

    visible_assets = [asset for asset in result.assets if asset.media_type in {"photo", "video"}]
    visual_total = len(visible_assets)
    visual_index = 0

    for raw_index, asset in enumerate(result.assets[:50], start=1):
        remote_url = getattr(asset, "remote_url", None)

        if asset.media_type == "audio":
            if not remote_url:
                continue

            results.append(
                InlineQueryResultAudio(
                    id=safe_inline_id(f"remote-audio:{result.media_id}_{raw_index}"),
                    audio_url=remote_url,
                    title=getattr(asset, "title", None) or result.title or t.inline_audio_title,
                    performer=getattr(asset, "performer", None),
                    audio_duration=getattr(asset, "duration_seconds", None),
                    caption=settings.photo_caption_template.format(url=result.source_url),
                )
            )
            continue

        if asset.media_type in {"photo", "video"}:
            visual_index += 1

        caption = (
            settings.photo_caption_template.format(url=result.source_url)
            if visual_index == 1
            else None
        )

        if asset.telegram_file_id and asset.media_type == "photo":
            results.append(
                InlineQueryResultCachedPhoto(
                    id=safe_inline_id(f"cached-photo:{result.media_id}_{visual_index}"),
                    photo_file_id=asset.telegram_file_id,
                    title=t.inline_photo_title.format(index=visual_index),
                    description=f"Фото {visual_index} из {visual_total}",
                    caption=caption,
                )
            )
            continue

        thumbnail_url = getattr(asset, "thumbnail_url", None) or remote_url
        if not remote_url or not thumbnail_url:
            continue

        if asset.media_type == "video":
            results.append(
                InlineQueryResultVideo(
                    id=safe_inline_id(f"remote-video:{result.media_id}_{visual_index}"),
                    video_url=remote_url,
                    mime_type="video/mp4",
                    thumbnail_url=thumbnail_url,
                    title=t.inline_video_slide_title.format(index=visual_index),
                    description=f"Видео {visual_index} из {visual_total}",
                    caption=caption,
                )
            )
            continue

        if asset.media_type == "photo":
            results.append(
                InlineQueryResultPhoto(
                    id=safe_inline_id(f"remote-photo:{result.media_id}_{visual_index}"),
                    photo_url=remote_url,
                    thumbnail_url=thumbnail_url,
                    title=t.inline_photo_title.format(index=visual_index),
                    description=f"Фото {visual_index} из {visual_total}",
                    caption=caption,
                )
            )

    return results


def build_inline_error_result(url: str, title: str, description: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=safe_inline_id(f"error:{normalize_url(url)}:{title}"),
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(message_text=description),
    )


def inline_loading_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t.inline_loading_button,
                    callback_data="inline_loading_status",
                )
            ]
        ]
    )

@dp.startup()
async def on_startup() -> None:
    if settings.enable_cache:
        ensure_cache_dir()
        asyncio.create_task(cache_heartbeat())

    logger.info(
        "Bot started. ENABLE_CACHE=%s CACHE_TTL_SECONDS=%s",
        settings.enable_cache,
        settings.cache_ttl_seconds,
    )


@dp.message(CommandStart())
async def start_handler(message: Message) -> None:
    await message.answer(
        t.start.format(bot_username=settings.bot_username)
    )


@dp.message(F.text)
async def message_handler(message: Message) -> None:
    url = extract_tiktok_url(message.text)
    if not url:
        return

    loading = await message.answer(t.loading_started)

    try:
        # Даём чуть больше времени, так как API + скачивание бинарников может занять время
        result = await asyncio.wait_for(
            get_or_download_media(url),
            timeout=settings.download_timeout_seconds + 15,
        )

        await safe_delete(loading)
        await send_download_result(message, result)

    except Exception as exc:
        error_text = public_error_message(exc)
        logger.exception("Failed to handle message download: %s", error_text)

        await safe_delete(loading)
        await message.answer(t.error.format(error=error_text))


@dp.inline_query()
async def inline_query_handler(inline_query: InlineQuery, bot: Bot) -> None:
    url = extract_tiktok_url(inline_query.query)

    if not url:
        result = InlineQueryResultArticle(
            id="help",
            title=t.inline_help_title,
            description=t.inline_help_description.format(bot_username=settings.bot_username),
            input_message_content=InputTextMessageContent(
                message_text=t.inline_help_message
            ),
        )
        await inline_query.answer([result], cache_time=0, is_personal=True)
        return

    cached = await find_cached_by_url(url)
    if not cached:
        cached = find_inline_ready_media(url)

    if cached and cached.kind == "video" and cached.assets and cached.assets[0].telegram_file_id:
        result = InlineQueryResultCachedVideo(
            id=safe_inline_id(cached.media_id),
            video_file_id=cached.assets[0].telegram_file_id,
            title=t.inline_send_video_title,
            description=t.inline_video_ready,
            caption=None,
        )

        await inline_query.answer([result], cache_time=0, is_personal=True)
        return

    if cached and cached.kind == "photo_album":
        results = build_photo_album_inline_results(cached)

        if results:
            await inline_query.answer(results, cache_time=0, is_personal=True)
            return

    # Если нет в кэше, делаем быстрый запрос к API, чтобы узнать, видео это или слайдшоу
    try:
        prepared = await asyncio.wait_for(
            probe_inline_media(url),
            timeout=_INLINE_PHOTO_PREFETCH_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning("Inline media probe failed: %s", public_error_message(exc), exc_info=True)
        prepared = None

    if prepared and prepared.kind == "photo_album":
        remember_inline_ready_media(prepared, url)
        results = build_photo_album_inline_results(prepared)
        if results:
            await inline_query.answer(results, cache_time=0, is_personal=True)
            return

    # Если это видео (или API упал), используем заглушку, чтобы бот скачал и перезалил его в Telegram
    result = InlineQueryResultArticle(
        id=inline_loading_result_id(url),
        title=t.inline_download_title,
        description=t.inline_download_description,
        input_message_content=InputTextMessageContent(
            message_text=t.inline_download_message
        ),
        reply_markup=inline_loading_keyboard(),
    )

    await inline_query.answer([result], cache_time=0, is_personal=True)


@dp.chosen_inline_result()
async def chosen_inline_result_handler(chosen_result: ChosenInlineResult, bot: Bot) -> None:
    url = extract_tiktok_url(chosen_result.query)

    if not url:
        return

    if chosen_result.result_id != inline_loading_result_id(url):
        return

    if not chosen_result.inline_message_id:
        logger.warning(
            "Chosen inline result has no inline_message_id. "
            "Check /setinlinefeedback and inline keyboard."
        )
        return

    asyncio.create_task(
        process_inline_video_job(
            bot=bot,
            inline_message_id=chosen_result.inline_message_id,
            url=url,
        )
    )

@dp.callback_query(F.data == "inline_loading_status")
async def inline_loading_status_handler(callback: CallbackQuery) -> None:
    await callback.answer(t.inline_loading_callback)

async def process_inline_video_job(bot: Bot, inline_message_id: str, url: str) -> None:
    try:
        result = await asyncio.wait_for(
            get_or_download_media(url),
            timeout=settings.download_timeout_seconds + 15,
        )

        if result.kind == "photo_album":
            result = await asyncio.wait_for(
                ensure_photo_file_ids(bot, result),
                timeout=settings.telegram_upload_timeout_seconds * settings.telegram_upload_retries + 10,
            )
            remember_inline_ready_media(result, url)
            first_asset = result.assets[0]

            await bot.edit_message_media(
                inline_message_id=inline_message_id,
                media=InputMediaPhoto(
                    media=first_asset.telegram_file_id,
                    caption=(
                        settings.photo_caption_template.format(url=result.source_url)
                        if len(result.assets) == 1
                        else t.inline_album_ready_retry
                    ),
                ),
                reply_markup=None,
            )
            return

        telegram_file_id = await asyncio.wait_for(
            ensure_telegram_file_id(bot, result),
            timeout=settings.telegram_upload_timeout_seconds * settings.telegram_upload_retries + 10,
        )
        remember_inline_ready_media(result, url)

        await bot.edit_message_media(
            inline_message_id=inline_message_id,
            media=InputMediaVideo(
                media=telegram_file_id,
                supports_streaming=True,
            ),
            reply_markup=None,
        )

    except Exception as exc:
        error_text = public_error_message(exc)
        logger.exception("Failed to process async inline video job: %s", error_text)

        try:
            await bot.edit_message_text(
                inline_message_id=inline_message_id,
                text=t.error.format(error=error_text),
                reply_markup=None,
            )
        except Exception:
            logger.exception("Failed to edit inline message with error")