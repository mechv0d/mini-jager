import asyncio
import logging
import shutil

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import FSInputFile, InputMediaPhoto, Message

from app.cache_service import update_cache_asset_file_id, update_cache_telegram_file_id
from app.config import settings
from app.models import DownloadResult, PublicBotError

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


def dump_chat_id() -> int | str:
    if not settings.dump_chat_id:
        raise PublicBotError(
            "внутренняя ошибка: DUMP_CHAT_ID не задан. "
            "Для inline-режима нужен служебный чат/канал, куда бот загрузит файл "
            "и получит Telegram file_id."
        )

    return int(settings.dump_chat_id) if settings.dump_chat_id.lstrip("-").isdigit() else settings.dump_chat_id


def cleanup_temp_dir(result: DownloadResult) -> None:
    if result.temp_dir and result.temp_dir.exists():
        shutil.rmtree(result.temp_dir, ignore_errors=True)


async def send_video_without_caption(message: Message, result: DownloadResult) -> None:
    last_error: Exception | None = None
    asset = result.assets[0] if result.assets else None
    if not asset:
        raise PublicBotError("внутренняя ошибка: у видео нет файла")

    for attempt in range(1, settings.telegram_upload_retries + 1):
        try:
            if asset.telegram_file_id:
                sent = await message.answer_video(
                    video=asset.telegram_file_id,
                    caption=None,
                    supports_streaming=True,
                    request_timeout=settings.telegram_upload_timeout_seconds,
                )
            else:
                sent = await message.answer_video(
                    video=FSInputFile(asset.path),
                    caption=None,
                    supports_streaming=True,
                    request_timeout=settings.telegram_upload_timeout_seconds,
                )

            if sent.video and sent.video.file_id:
                asset.telegram_file_id = sent.video.file_id
                await update_cache_telegram_file_id(result.media_id, sent.video.file_id)

            cleanup_temp_dir(result)

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


async def send_photo_album(message: Message, result: DownloadResult) -> None:
    if not result.assets:
        raise PublicBotError("внутренняя ошибка: у фотоальбома нет файлов")

    caption = settings.photo_caption_template.format(url=result.source_url)
    chunk_size = max(2, min(settings.photo_album_chunk_size, 10))

    if len(result.assets) == 1:
        asset = result.assets[0]
        sent = await message.answer_photo(
            photo=asset.telegram_file_id or FSInputFile(asset.path),
            caption=caption,
            request_timeout=settings.telegram_upload_timeout_seconds,
        )
        if sent.photo:
            asset.telegram_file_id = sent.photo[-1].file_id
            await update_cache_asset_file_id(result.media_id, 0, sent.photo[-1].file_id)
        cleanup_temp_dir(result)
        return

    for offset in range(0, len(result.assets), chunk_size):
        chunk = result.assets[offset : offset + chunk_size]
        media = []
        for index, asset in enumerate(chunk, start=offset):
            media.append(
                InputMediaPhoto(
                    media=asset.telegram_file_id or FSInputFile(asset.path),
                    caption=caption if index == 0 else None,
                )
            )

        sent_messages = await message.answer_media_group(
            media=media,
            request_timeout=settings.telegram_upload_timeout_seconds,
        )
        for index, sent in enumerate(sent_messages, start=offset):
            if sent.photo:
                result.assets[index].telegram_file_id = sent.photo[-1].file_id
                await update_cache_asset_file_id(result.media_id, index, sent.photo[-1].file_id)

    cleanup_temp_dir(result)


async def send_download_result(message: Message, result: DownloadResult) -> None:
    if result.kind == "video":
        await send_video_without_caption(message, result)
        return

    if result.kind == "photo_album":
        await send_photo_album(message, result)
        return

    raise PublicBotError(f"внутренняя ошибка: неизвестный тип медиа {result.kind}")


async def ensure_telegram_file_id(bot: Bot, result: DownloadResult) -> str:
    asset = result.assets[0] if result.assets else None
    if not asset:
        raise PublicBotError("внутренняя ошибка: у видео нет файла")

    if asset.telegram_file_id:
        return asset.telegram_file_id

    if not settings.dump_chat_id:
        raise PublicBotError(
            "внутренняя ошибка: DUMP_CHAT_ID не задан. "
            "Для inline-режима нужен служебный чат/канал, куда бот загрузит видео "
            "и получит Telegram file_id."
        )

    sent = await bot.send_video(
        chat_id=dump_chat_id(),
        video=FSInputFile(asset.path),
        caption=None,
        supports_streaming=True,
        request_timeout=settings.telegram_upload_timeout_seconds,
    )

    if not sent.video or not sent.video.file_id:
        raise PublicBotError("внутренняя ошибка: Telegram не вернул video.file_id")

    asset.telegram_file_id = sent.video.file_id
    await update_cache_telegram_file_id(result.media_id, sent.video.file_id)

    cleanup_temp_dir(result)

    return sent.video.file_id


async def ensure_photo_file_ids(bot: Bot, result: DownloadResult) -> DownloadResult:
    for index, asset in enumerate(result.assets):
        if asset.telegram_file_id:
            continue

        sent = await bot.send_photo(
            chat_id=dump_chat_id(),
            photo=FSInputFile(asset.path),
            caption=None,
            request_timeout=settings.telegram_upload_timeout_seconds,
        )

        if not sent.photo:
            raise PublicBotError("внутренняя ошибка: Telegram не вернул photo.file_id")

        asset.telegram_file_id = sent.photo[-1].file_id
        await update_cache_asset_file_id(result.media_id, index, sent.photo[-1].file_id)

    cleanup_temp_dir(result)
    return result
