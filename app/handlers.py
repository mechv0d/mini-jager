import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    InlineQuery,
    InlineQueryResultArticle,
    InlineQueryResultCachedVideo,
    InputTextMessageContent,
    Message,
)

from app.cache_service import cache_heartbeat, ensure_cache_dir
from app.config import settings
from app.download_service import get_or_download_video
from app.telegram_service import (
    ensure_telegram_file_id,
    public_error_message,
    safe_delete,
    send_video_without_caption,
)
from app.url_utils import extract_tiktok_url, safe_inline_id

logger = logging.getLogger("ttsavefrom_bot.handlers")

dp = Dispatcher()


@dp.startup()
async def on_startup() -> None:
    ensure_cache_dir()
    asyncio.create_task(cache_heartbeat())
    logger.info(
        "Bot started. ENABLE_CASHE=%s CACHE_TTL_SECONDS=%s",
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
            get_or_download_video(url),
            timeout=settings.download_timeout_seconds,
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
            description=f"Пример: @{settings.bot_username} https://www.tiktok.com/...",
            input_message_content=InputTextMessageContent(
                message_text="Пришли ссылку на TikTok-видео."
            ),
        )
        await inline_query.answer([result], cache_time=0, is_personal=True)
        return

    try:
        video = await asyncio.wait_for(
            get_or_download_video(url),
            timeout=settings.download_timeout_seconds,
        )

        telegram_file_id = await asyncio.wait_for(
            ensure_telegram_file_id(bot, video),
            timeout=settings.telegram_upload_timeout_seconds * settings.telegram_upload_retries,
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
