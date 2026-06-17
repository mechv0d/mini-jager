import asyncio
import logging
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InlineQueryResultCachedVideo,
    InlineQueryResultPhoto,
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
    probe_photo_album_links_sync,
    resolve_tiktok_redirect_sync,
    to_thread_ytdlp,
)
from app.models import DownloadResult
from app.telegram_service import (
    ensure_photo_file_ids,
    ensure_telegram_file_id,
    public_error_message,
    safe_delete,
    send_download_result,
)
from app.url_utils import (
    extract_tiktok_url,
    is_tiktok_photo_url,
    normalize_url,
    parse_video_id_from_url,
    safe_inline_id,
)

logger = logging.getLogger("ttsavefrom_bot.handlers")

dp = Dispatcher()

_INLINE_READY_TTL_SECONDS = 30 * 60
# Telegram clients usually stop waiting for an inline answer quickly.
# For photo posts we only probe TikTok URLs and return InlineQueryResultPhoto
# with thumbnail_url/photo_url. We do not upload all HD photos before answering.
_INLINE_PHOTO_PREFETCH_TIMEOUT_SECONDS = 9
_inline_ready_media: dict[str, tuple[float, DownloadResult]] = {}


def inline_loading_result_id(url: str) -> str:
    return safe_inline_id(f"loading:{normalize_url(url)}")


def remember_inline_ready_media(result: DownloadResult, *urls: str) -> None:
    """
    Keeps Telegram file_id values available for repeated inline queries even when
    ENABLE_CACHE=0. This is process-local and is lost after bot restart.
    """
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


def build_photo_album_inline_results(result: DownloadResult) -> list[InlineQueryResultCachedPhoto | InlineQueryResultPhoto]:
    results: list[InlineQueryResultCachedPhoto | InlineQueryResultPhoto] = []

    for index, asset in enumerate(result.assets[:50], start=1):
        caption = (
            settings.photo_caption_template.format(url=result.source_url)
            if index == 1
            else None
        )

        if asset.telegram_file_id:
            results.append(
                InlineQueryResultCachedPhoto(
                    id=safe_inline_id(f"cached:{result.media_id}_{index}"),
                    photo_file_id=asset.telegram_file_id,
                    title=f"Photo {index}",
                    description=f"Фото {index} из {len(result.assets)}",
                    caption=caption,
                )
            )
            continue

        photo_url = getattr(asset, "remote_url", None)
        thumbnail_url = getattr(asset, "thumbnail_url", None) or photo_url
        if not photo_url or not thumbnail_url:
            continue

        results.append(
            InlineQueryResultPhoto(
                id=safe_inline_id(f"remote:{result.media_id}_{index}"),
                photo_url=photo_url,
                thumbnail_url=thumbnail_url,
                title=f"Photo {index}",
                description=f"Фото {index} из {len(result.assets)}",
                caption=caption,
            )
        )

    return results


def is_photo_effective_url(url: str) -> bool:
    return is_tiktok_photo_url(url) or "/photo/" in url.lower()


async def resolve_inline_tiktok_url(url: str) -> str:
    """Resolve short TikTok links once before deciding inline behavior."""
    effective_url = normalize_url(url)
    if parse_video_id_from_url(effective_url) is None:
        resolved_url = await to_thread_ytdlp(resolve_tiktok_redirect_sync, effective_url)
        if resolved_url:
            effective_url = normalize_url(resolved_url)
    return effective_url


def build_inline_error_result(url: str, title: str, description: str) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=safe_inline_id(f"error:{normalize_url(url)}:{title}"),
        title=title,
        description=description,
        input_message_content=InputTextMessageContent(message_text=description),
    )


async def try_prepare_inline_photo_album(bot: Bot, url: str) -> DownloadResult | None:
    """
    If the inline query points to a TikTok photo post, prepare URL-based inline
    photo results immediately. This is the @pics-like path: Telegram receives a
    small thumbnail_url for the grid and a photo_url for the final HD message.

    No DUMP_CHAT_ID upload happens here, so the user does not wait 5-6 seconds
    for every HD photo to be uploaded before seeing choices.
    """
    effective_url = await resolve_inline_tiktok_url(url)

    if not is_photo_effective_url(effective_url):
        return None

    result = await to_thread_ytdlp(probe_photo_album_links_sync, effective_url)
    if result.kind != "photo_album":
        return None

    remember_inline_ready_media(result, url, effective_url)
    return result


def inline_loading_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Загрузка выполняется",
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
        "Привет! Отправь ссылку на TikTok-видео, и я отправлю его сюда.\n\n"
        f"Также можно использовать inline-режим: @{settings.bot_username} https://www.tiktok.com/..."
    )


@dp.message(F.text)
async def message_handler(message: Message) -> None:
    url = extract_tiktok_url(message.text)
    if not url:
        return

    loading = await message.answer("Загрузка началась")

    try:
        result = await asyncio.wait_for(
            get_or_download_media(url),
            timeout=settings.download_timeout_seconds,
        )

        await safe_delete(loading)
        await send_download_result(message, result)

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
            description=f"Пример: @{settings.bot_username} https://www.tiktok.com/...",
            input_message_content=InputTextMessageContent(
                message_text="Пришли ссылку на TikTok-видео."
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
            title="Отправить видео",
            description="Видео готово",
            caption=None,
        )

        await inline_query.answer([result], cache_time=0, is_personal=True)
        return

    if cached and cached.kind == "photo_album":
        results = build_photo_album_inline_results(cached)

        if results:
            await inline_query.answer(results, cache_time=0, is_personal=True)
            return

    # For TikTok photo/slideshow links do NOT return the video placeholder.
    # Inline results cannot be updated later, so the first answer must already
    # contain InlineQueryResultPhoto items with thumbnail_url/photo_url.
    try:
        effective_url = await asyncio.wait_for(
            resolve_inline_tiktok_url(url),
            timeout=min(4, _INLINE_PHOTO_PREFETCH_TIMEOUT_SECONDS),
        )
    except Exception as exc:
        logger.warning("Inline TikTok URL resolve failed: %s", public_error_message(exc), exc_info=True)
        effective_url = normalize_url(url)

    if is_photo_effective_url(effective_url):
        try:
            prepared = await asyncio.wait_for(
                try_prepare_inline_photo_album(bot, effective_url),
                timeout=_INLINE_PHOTO_PREFETCH_TIMEOUT_SECONDS,
            )
        except Exception as exc:
            logger.warning("Inline photo prefetch failed: %s", public_error_message(exc), exc_info=True)
            await inline_query.answer(
                [
                    build_inline_error_result(
                        url,
                        "Не удалось подготовить фото",
                        "TikTok не отдал ссылки на фото достаточно быстро. Повтори inline-запрос через секунду.",
                    )
                ],
                cache_time=0,
                is_personal=True,
            )
            return

        if prepared and prepared.kind == "photo_album":
            results = build_photo_album_inline_results(prepared)
            if results:
                await inline_query.answer(results, cache_time=0, is_personal=True)
                return

        await inline_query.answer(
            [
                build_inline_error_result(
                    url,
                    "Фото не найдены",
                    "Ссылка похожа на TikTok slideshow, но фото URL не были найдены.",
                )
            ],
            cache_time=0,
            is_personal=True,
        )
        return

    # Only video/unknown TikTok links use the async placeholder flow.
    result = InlineQueryResultArticle(
        id=inline_loading_result_id(url),
        title="Скачать TikTok",
        description="Видео появится после загрузки",
        input_message_content=InputTextMessageContent(
            message_text="Загрузка началась. Видео появится здесь автоматически."
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
        # The user selected an already prepared cached photo/video result.
        # Do not start a new download job for these selections.
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
    await callback.answer("Медиа ещё загружается. Сообщение обновится автоматически.")

async def process_inline_video_job(bot: Bot, inline_message_id: str, url: str) -> None:
    try:
        result = await asyncio.wait_for(
            get_or_download_media(url),
            timeout=settings.download_timeout_seconds,
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
                        else "Фотоальбом готов. Для выбора всех фото повторите inline-запрос."
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
                text=f"Ошибка: {error_text}",
                reply_markup=None,
            )
        except Exception:
            logger.exception("Failed to edit inline message with error")
