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
    CallbackQuery,
    ChosenInlineResult,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaVideo,
)

from app.cache_service import cache_heartbeat, ensure_cache_dir, find_cached_by_url
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

    cached = await find_cached_by_url(url)

    if cached and cached.telegram_file_id:
        result = InlineQueryResultCachedVideo(
            id=safe_inline_id(cached.video_id),
            video_file_id=cached.telegram_file_id,
            title="Отправить видео",
            description="Видео готово",
            caption=None,
        )

        await inline_query.answer([result], cache_time=0, is_personal=True)
        return

    result = InlineQueryResultArticle(
        id=safe_inline_id(url),
        title="Скачать TikTok-видео",
        description="Сообщение отправится сразу, видео появится после загрузки",
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
    await callback.answer("Видео ещё загружается. Сообщение обновится автоматически.")

async def process_inline_video_job(bot: Bot, inline_message_id: str, url: str) -> None:
    try:
        video = await asyncio.wait_for(
            get_or_download_video(url),
            timeout=settings.download_timeout_seconds,
        )

        telegram_file_id = await asyncio.wait_for(
            ensure_telegram_file_id(bot, video),
            timeout=settings.telegram_upload_timeout_seconds * settings.telegram_upload_retries + 10,
        )

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