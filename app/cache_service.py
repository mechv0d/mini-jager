import asyncio
import json
import logging
import time
from pathlib import Path

from app.config import settings
from app.models import VideoResult
from app.url_utils import url_hash

logger = logging.getLogger("ttsavefrom_bot.cache")

_index_lock = asyncio.Lock()
_video_locks: dict[str, asyncio.Lock] = {}


def get_video_lock(video_id: str) -> asyncio.Lock:
    return _video_locks.setdefault(video_id, asyncio.Lock())


def ensure_cache_dir() -> None:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    if not settings.cache_index_file.exists():
        settings.cache_index_file.write_text(
            json.dumps({"videos": {}, "aliases": {}}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def load_index_sync() -> dict:
    ensure_cache_dir()
    try:
        return json.loads(settings.cache_index_file.read_text(encoding="utf-8"))
    except Exception:
        logger.exception("Cache index is broken, recreating it")
        return {"videos": {}, "aliases": {}}


def save_index_sync(index: dict) -> None:
    ensure_cache_dir()
    tmp = settings.cache_index_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(settings.cache_index_file)


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
    if not settings.enable_cache:
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
    if not settings.enable_cache:
        return None

    index = await load_index()
    alias_hash = url_hash(url)
    alias = index.get("aliases", {}).get(alias_hash)
    if not alias:
        return None

    record = index.get("videos", {}).get(alias)
    if not record:
        return None

    result = cache_record_to_result(alias, record)
    if result:
        return result

    index.get("videos", {}).pop(alias, None)
    index.get("aliases", {}).pop(alias_hash, None)
    await save_index(index)
    return None


async def put_cache(result: VideoResult, source_url: str) -> None:
    if not settings.enable_cache:
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
    if not settings.enable_cache:
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
            if settings.enable_cache:
                now = int(time.time())
                index = await load_index()
                videos = index.get("videos", {})
                aliases = index.get("aliases", {})

                expired_ids: list[str] = []
                for video_id, record in list(videos.items()):
                    created_at = int(record.get("created_at", 0))
                    path = Path(record.get("path", ""))

                    if now - created_at >= settings.cache_ttl_seconds:
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

        await asyncio.sleep(settings.cache_clean_interval_seconds)
