import asyncio
import logging
import shutil

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import FSInputFile, InputMediaPhoto, Message

from app.cache_service import update_cache_asset_file_id, update_cache_telegram_file_id
from app.config import settings
from app.dump_metadata import DumpMetadata
from app.lang import t
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
        return t.timeout_seconds.format(seconds=settings.download_timeout_seconds)

    if isinstance(exc, PublicBotError):
        return exc.message

    return t.internal_error.format(type=type(exc).__name__, message=str(exc)[:500])


def dump_chat_id() -> int | str:
    if not settings.dump_chat_id:
        raise PublicBotError(t.dump_chat_id_missing_file)

    return int(settings.dump_chat_id) if settings.dump_chat_id.lstrip("-").isdigit() else settings.dump_chat_id


def user_caption() -> str:
    return t.downloaded_via.format(bot_username=settings.bot_username)


def cleanup_temp_dir(result: DownloadResult) -> None:
    if result.temp_dir and result.temp_dir.exists():
        shutil.rmtree(result.temp_dir, ignore_errors=True)


def _asset_media(asset, *, video: bool = False):
    if asset.telegram_file_id:
        return asset.telegram_file_id

    if not asset.path.exists():
        raise PublicBotError(t.video_has_no_file if video else t.album_has_no_files)

    return FSInputFile(asset.path)


async def ensure_telegram_file_id(
    bot: Bot,
    result: DownloadResult,
    metadata: DumpMetadata | None = None,
    force_upload: bool = False,
) -> str:
    asset = result.assets[0] if result.assets else None
    if not asset:
        raise PublicBotError(t.video_has_no_file)

    if asset.telegram_file_id and not force_upload and metadata is None:
        return asset.telegram_file_id

    if not settings.dump_chat_id:
        if asset.telegram_file_id:
            return asset.telegram_file_id
        raise PublicBotError(t.dump_chat_id_missing_video)

    sent = await bot.send_video(
        chat_id=dump_chat_id(),
        video=_asset_media(asset, video=True),
        caption=metadata.caption() if metadata else None,
        parse_mode=None,
        supports_streaming=True,
        request_timeout=settings.telegram_upload_timeout_seconds,
    )

    if not sent.video or not sent.video.file_id:
        raise PublicBotError(t.telegram_no_video_file_id)

    asset.telegram_file_id = sent.video.file_id
    await update_cache_telegram_file_id(result.media_id, sent.video.file_id)

    return sent.video.file_id


async def _send_photo_to_dump(
    bot: Bot,
    asset,
    caption: str | None,
) -> str:
    sent = await bot.send_photo(
        chat_id=dump_chat_id(),
        photo=_asset_media(asset),
        caption=caption,
        parse_mode=None,
        request_timeout=settings.telegram_upload_timeout_seconds,
    )

    if not sent.photo:
        raise PublicBotError(t.telegram_no_photo_file_id)

    return sent.photo[-1].file_id


async def _upload_photo_album_to_dump(
    bot: Bot,
    result: DownloadResult,
    metadata: DumpMetadata | None,
) -> None:
    if len(result.assets) == 1:
        file_id = await _send_photo_to_dump(
            bot,
            result.assets[0],
            metadata.caption() if metadata else None,
        )
        result.assets[0].telegram_file_id = file_id
        await update_cache_asset_file_id(result.media_id, 0, file_id)
        return

    chunk_size = max(2, min(settings.photo_album_chunk_size, 10))

    for offset in range(0, len(result.assets), chunk_size):
        chunk = list(enumerate(result.assets[offset : offset + chunk_size], start=offset))
        media = [
            InputMediaPhoto(
                media=_asset_media(asset),
                caption=metadata.caption() if metadata and index == 0 else None,
                parse_mode=None,
            )
            for index, asset in chunk
        ]

        sent_messages = await bot.send_media_group(
            chat_id=dump_chat_id(),
            media=media,
            request_timeout=settings.telegram_upload_timeout_seconds,
        )

        for (index, asset), sent in zip(chunk, sent_messages):
            if sent.photo:
                asset.telegram_file_id = sent.photo[-1].file_id
                await update_cache_asset_file_id(result.media_id, index, sent.photo[-1].file_id)


async def ensure_photo_file_ids(
    bot: Bot,
    result: DownloadResult,
    metadata: DumpMetadata | None = None,
    force_upload: bool = False,
) -> DownloadResult:
    if not result.assets:
        raise PublicBotError(t.album_has_no_files)

    if metadata is not None or force_upload:
        if not settings.dump_chat_id:
            if all(asset.telegram_file_id for asset in result.assets):
                return result
            raise PublicBotError(t.dump_chat_id_missing_file)

        await _upload_photo_album_to_dump(bot, result, metadata)
        return result

    for index, asset in enumerate(result.assets):
        if asset.telegram_file_id:
            continue

        file_id = await _send_photo_to_dump(bot, asset, None)
        asset.telegram_file_id = file_id
        await update_cache_asset_file_id(result.media_id, index, file_id)

    return result


async def upload_result_to_dump(
    bot: Bot,
    result: DownloadResult,
    metadata: DumpMetadata,
) -> None:
    if result.kind == "video":
        await ensure_telegram_file_id(
            bot,
            result,
            metadata=metadata,
            force_upload=True,
        )
        return

    if result.kind == "photo_album":
        await ensure_photo_file_ids(
            bot,
            result,
            metadata=metadata,
            force_upload=True,
        )
        return

    raise PublicBotError(t.unknown_media_kind.format(kind=result.kind))


async def send_video_with_caption(message: Message, result: DownloadResult) -> None:
    last_error: Exception | None = None
    asset = result.assets[0] if result.assets else None
    if not asset:
        raise PublicBotError(t.video_has_no_file)

    caption = user_caption()

    for attempt in range(1, settings.telegram_upload_retries + 1):
        try:
            sent = await message.answer_video(
                video=_asset_media(asset, video=True),
                caption=caption,
                parse_mode=None,
                supports_streaming=True,
                request_timeout=settings.telegram_upload_timeout_seconds,
            )

            if sent.video and sent.video.file_id:
                asset.telegram_file_id = sent.video.file_id
                await update_cache_telegram_file_id(result.media_id, sent.video.file_id)

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
        t.video_upload_failed.format(retries=settings.telegram_upload_retries, error=last_error)
    )


async def send_photo_album(message: Message, result: DownloadResult) -> None:
    if not result.assets:
        raise PublicBotError(t.album_has_no_files)

    for asset in result.assets:
        if not asset.telegram_file_id and not asset.path.exists():
            raise PublicBotError("Файл повреждён или не был скачан")

    caption = user_caption()
    chunk_size = max(2, min(settings.photo_album_chunk_size, 10))

    if len(result.assets) == 1:
        asset = result.assets[0]
        sent = await message.answer_photo(
            photo=_asset_media(asset),
            caption=caption,
            parse_mode=None,
            request_timeout=settings.telegram_upload_timeout_seconds,
        )
        if sent.photo:
            asset.telegram_file_id = sent.photo[-1].file_id
            await update_cache_asset_file_id(result.media_id, 0, sent.photo[-1].file_id)
        return

    for offset in range(0, len(result.assets), chunk_size):
        chunk = result.assets[offset : offset + chunk_size]
        media = []
        for index, asset in enumerate(chunk, start=offset):
            media.append(
                InputMediaPhoto(
                    media=_asset_media(asset),
                    caption=caption if index == 0 else None,
                    parse_mode=None,
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


async def send_download_result(
    bot: Bot,
    message: Message,
    result: DownloadResult,
    metadata: DumpMetadata,
) -> None:
    try:
        if settings.dump_chat_id:
            try:
                await upload_result_to_dump(bot, result, metadata)
            except Exception:
                logger.exception(
                    "Failed to upload media to dump chat with metadata; "
                    "sending the user copy without a dump record"
                )

        if result.kind == "video":
            await send_video_with_caption(message, result)
            return

        if result.kind == "photo_album":
            await send_photo_album(message, result)
            return

        raise PublicBotError(t.unknown_media_kind.format(kind=result.kind))
    finally:
        cleanup_temp_dir(result)
