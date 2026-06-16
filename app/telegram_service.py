import asyncio
import logging
import shutil

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import FSInputFile, Message

from app.cache_service import update_cache_telegram_file_id
from app.config import settings
from app.models import PublicBotError, VideoResult

logger = logging.getLogger("ttsavefrom_bot.telegram")


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
        return f"таймаут {settings.download_timeout_seconds} секунд"

    if isinstance(exc, PublicBotError):
        return exc.message

    return f"внутренняя ошибка: {type(exc).__name__}: {str(exc)[:500]}"


async def send_video_without_caption(message: Message, result: VideoResult) -> None:
    last_error: Exception | None = None

    for attempt in range(1, settings.telegram_upload_retries + 1):
        try:
            if result.telegram_file_id:
                sent = await message.answer_video(
                    video=result.telegram_file_id,
                    caption=None,
                    supports_streaming=True,
                    request_timeout=settings.telegram_upload_timeout_seconds,
                )
            else:
                sent = await message.answer_video(
                    video=FSInputFile(result.path),
                    caption=None,
                    supports_streaming=True,
                    request_timeout=settings.telegram_upload_timeout_seconds,
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
                settings.telegram_upload_retries,
                exc,
            )

            if attempt < settings.telegram_upload_retries:
                await asyncio.sleep(2 * attempt)
                continue

        except Exception:
            raise

    raise PublicBotError(
        f"ошибка отправки видео в Telegram после {settings.telegram_upload_retries} попыток: {last_error}"
    )


async def ensure_telegram_file_id(bot: Bot, result: VideoResult) -> str:
    if result.telegram_file_id:
        return result.telegram_file_id

    if not settings.dump_chat_id:
        raise PublicBotError(
            "внутренняя ошибка: DUMP_CHAT_ID не задан. "
            "Для inline-режима нужен служебный чат/канал, куда бот загрузит видео "
            "и получит Telegram file_id."
        )

    sent = await bot.send_video(
        chat_id=int(settings.dump_chat_id) if settings.dump_chat_id.lstrip("-").isdigit() else settings.dump_chat_id,
        video=FSInputFile(result.path),
        caption=None,
        supports_streaming=True,
        request_timeout=settings.telegram_upload_timeout_seconds,
    )

    if not sent.video or not sent.video.file_id:
        raise PublicBotError("внутренняя ошибка: Telegram не вернул video.file_id")

    await update_cache_telegram_file_id(result.video_id, sent.video.file_id)

    if result.temp_dir and result.temp_dir.exists():
        shutil.rmtree(result.temp_dir, ignore_errors=True)

    return sent.video.file_id
