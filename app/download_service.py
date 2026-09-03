import asyncio
import html
import os
import json
import logging
import re
import shutil
import tempfile
from urllib.parse import urlparse
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.extractor.tiktok import TikTokIE
from yt_dlp.utils import DownloadError

from app.cache_service import (
    find_cached_by_url,
    find_cached_by_media_id,
    get_media_lock,
    put_cache,
)
from app.config import settings
from app.lang import t
from app.models import DownloadResult, MediaAsset, PublicBotError
from app.url_utils import (
    is_tiktok_photo_url,
    is_tiktok_url,
    normalize_url,
    parse_video_id_from_url,
    to_ytdlp_tiktok_url,
    url_hash,
)

logger = logging.getLogger("ttsavefrom_bot.download")


def classify_download_error(exc: Exception) -> str:
    text = str(exc).strip()
    lower = text.lower()

    if "your ip address is blocked" in lower:
        return t.ip_blocked

    if "connectionrefusederror" in lower or "connection refused" in lower or "winerror 10061" in lower:
        proxy_hint = f" ({settings.tiktok_proxy})" if settings.tiktok_proxy else ""
        return t.proxy_connection_failed.format(proxy_hint=proxy_hint)

    if "failed to parse json" in lower:
        return t.tiktok_invalid_json

    http_match = re.search(r"HTTP Error\s+(\d{3})|status code\s+(\d{3})|HTTP\s+(\d{3})", text)
    if http_match:
        code = next(group for group in http_match.groups() if group)
        return f"HTTP {code}: {text[:500]}"

    if "timed out" in lower or "timeout" in lower:
        return t.download_timeout.format(error=text[:500])

    return t.download_internal_error.format(error=text[:500])


def ytdlp_base_options() -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": settings.download_timeout_seconds,
        "retries": 1,
        "fragment_retries": 1,
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;q=0.9,"
                "image/avif,image/webp,image/apng,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "Sec-Fetch-Dest": "document",
            "Sec-Fetch-Mode": "navigate",
            "Sec-Fetch-Site": "none",
        },
    }

    if settings.tiktok_proxy:
        options["proxy"] = settings.tiktok_proxy

    return options


def resolve_tiktok_redirect_sync(url: str) -> str:
    """
    Resolves short TikTok URLs like https://www.tiktok.com/t/...
    into full URLs like https://www.tiktok.com/@user/photo/<id>
    or https://www.tiktok.com/@user/video/<id>.

    Uses yt-dlp's urlopen so TIKTOK_PROXY is respected.
    """
    if parse_video_id_from_url(url):
        return normalize_url(url)

    options = ytdlp_base_options()

    with YoutubeDL(options) as ydl:
        response = ydl.urlopen(url)

        try:
            final_url = getattr(response, "url", None)

            if not final_url and hasattr(response, "geturl"):
                final_url = response.geturl()

            if not final_url:
                final_url = response.headers.get("Location")

            return normalize_url(final_url or url)

        finally:
            try:
                response.close()
            except Exception:
                pass


def probe_info_sync(url: str) -> dict:
    if "/photo/" in url.lower():
        raise RuntimeError(
            "internal routing error: TikTok photo URL was sent to regular yt-dlp probe"
        )

    options = ytdlp_base_options()
    options.update({"skip_download": True})

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def parse_tiktok_embedded_json(webpage: str, script_id: str) -> dict | None:
    match = re.search(
        rf'<script[^>]+id=["\']{re.escape(script_id)}["\'][^>]*>(.*?)</script>',
        webpage,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if not match:
        return None

    raw_json = html.unescape(match.group(1).strip())
    if not raw_json:
        return None

    try:
        return json.loads(raw_json)
    except json.JSONDecodeError:
        logger.debug("Failed to parse TikTok embedded JSON script %s", script_id, exc_info=True)
        return None


def find_tiktok_item_struct(data, media_id: str) -> dict | None:
    """Find the TikTok itemStruct/aweme object inside webpage hydration JSON."""
    stack = [data]
    seen: set[int] = set()
    first_photo_item: dict | None = None

    while stack:
        current = stack.pop()
        current_id = id(current)
        if current_id in seen:
            continue
        seen.add(current_id)

        if isinstance(current, dict):
            nested_item = current.get("itemStruct")
            if isinstance(nested_item, dict):
                stack.append(nested_item)

            object_ids = {
                str(current[key])
                for key in ("id", "aweme_id")
                if current.get(key) is not None
            }
            is_photo_item = isinstance(current.get("imagePost"), dict) or isinstance(
                current.get("image_post_info"), dict
            )

            if media_id in object_ids and (is_photo_item or current.get("video") or current.get("desc")):
                return current

            if first_photo_item is None and is_photo_item:
                first_photo_item = current

            stack.extend(current.values())

        elif isinstance(current, list):
            stack.extend(current)

    return first_photo_item


def download_tiktok_webpage_sync(ydl: YoutubeDL, extractor: TikTokIE, url: str, media_id: str) -> str:
    """Download a TikTok webpage using yt-dlp first, then a plain urlopen fallback."""
    try:
        result = extractor._download_webpage_handle(
            url,
            media_id,
            note="Downloading TikTok photo webpage",
            fatal=False,
            impersonate=True,
        )
        if result is not False:
            webpage, _urlh = result
            if webpage:
                return webpage
    except TypeError:
        # Older yt-dlp builds may not support the impersonate= argument.
        result = extractor._download_webpage_handle(
            url,
            media_id,
            note="Downloading TikTok photo webpage",
            fatal=False,
        )
        if result is not False:
            webpage, _urlh = result
            if webpage:
                return webpage
    except Exception:
        logger.debug("yt-dlp webpage download failed for %s", url, exc_info=True)

    response = ydl.urlopen(url)
    try:
        return response.read().decode("utf-8", errors="replace")
    finally:
        try:
            response.close()
        except Exception:
            pass


def probe_photo_info_sync(web_url: str, media_id: str, source_url: str | None = None) -> dict:
    """Probe a TikTok photo post through webpage hydration data, not mobile app API.

    TikTok's mobile API often returns an empty body without valid signed headers.
    For /photo/<id> posts we probe the /video/<id> web URL first, because yt-dlp's
    TikTok web extractor currently reads the item from the webapp.video-detail scope.
    """
    options = ytdlp_base_options()
    options.update({"skip_download": True})

    source_url = source_url or web_url
    urls_to_try = []
    for candidate in (web_url, source_url):
        if candidate and candidate not in urls_to_try:
            urls_to_try.append(candidate)

    with YoutubeDL(options) as ydl:
        extractor = TikTokIE(ydl)
        aweme_detail: dict | None = None
        last_webpage_hint = ""

        for candidate_url in urls_to_try:
            # Prefer yt-dlp's webpage path because it handles TikTok web quirks such
            # as cookies, redirects, and challenge pages better than a raw request.
            try:
                web_data, _status = extractor._extract_web_data_and_status(
                    candidate_url,
                    media_id,
                    fatal=False,
                )
                aweme_detail = find_tiktok_item_struct(web_data, media_id)
            except Exception:
                logger.debug("yt-dlp TikTok webpage probe failed for %s", candidate_url, exc_info=True)

            if aweme_detail:
                break

            try:
                webpage = download_tiktok_webpage_sync(ydl, extractor, candidate_url, media_id)
                last_webpage_hint = webpage[:300].replace("\n", " ").replace("\r", " ")
            except Exception:
                logger.debug("Direct TikTok webpage fallback failed for %s", candidate_url, exc_info=True)
                continue

            for script_id in (
                "__UNIVERSAL_DATA_FOR_REHYDRATION__",
                "SIGI_STATE",
                "sigi-persisted-data",
            ):
                json_data = parse_tiktok_embedded_json(webpage, script_id)
                aweme_detail = find_tiktok_item_struct(json_data, media_id) if json_data else None
                if aweme_detail:
                    break

            if aweme_detail:
                break

        if not aweme_detail:
            raise RuntimeError(
                "TikTok webpage did not return photo post details"
                + (f"; first_html={last_webpage_hint!r}" if last_webpage_hint else "")
            )

        image_post = aweme_detail.get("imagePost") or aweme_detail.get("image_post_info") or {}
        info = {
            "id": str(aweme_detail.get("id") or aweme_detail.get("aweme_id") or media_id),
            "title": aweme_detail.get("desc") or image_post.get("title"),
            "source_url": source_url,
            "aweme_detail": aweme_detail,
        }

        if not extract_photo_urls(info):
            raise RuntimeError("TikTok webpage did not return photo URLs")

        return ydl.sanitize_info(info)


def extract_photo_urls(info: dict, *, include_thumbnails: bool = False) -> list[str]:
    """Return one downloadable image URL per TikTok slideshow image.

    TikTok exposes several URLs for the same slide: clean display image,
    watermark image, user watermark image, and thumbnail. The previous version
    scanned every nested URL and therefore downloaded 2+ copies of the same
    visual slide. This function keeps image boundaries and picks only one best
    URL for each slide.
    """
    urls: list[str] = []

    def is_usable_url(value) -> bool:
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            return False

        # TikTok may expose HEIC variants first; Telegram and most Python image
        # tooling handle JPEG/WEBP/PNG more reliably, so prefer non-HEIC URLs.
        return ".heic" not in urlparse(value).path.lower()

    def add_url(value) -> None:
        if is_usable_url(value) and value not in urls:
            urls.append(value)

    def collect_urls_from_container(container) -> list[str]:
        found: list[str] = []

        def add_candidate(value) -> None:
            if is_usable_url(value) and value not in found:
                found.append(value)

        def walk(value) -> None:
            if isinstance(value, str):
                add_candidate(value)
                return

            if isinstance(value, list):
                for item in value:
                    walk(item)
                return

            if not isinstance(value, dict):
                return

            for key in ("url", "uri", "src", "displayUrl", "downloadUrl"):
                add_candidate(value.get(key))

            for list_key in ("url_list", "urlList", "UrlList"):
                walk(value.get(list_key))

            for nested_key in (
                "imageURL",
                "imageUrl",
                "image_url",
                "display_image",
                "displayImage",
                "owner_watermark_image",
                "ownerWatermarkImage",
                "user_watermark_image",
                "userWatermarkImage",
                "thumbnail",
                "cover",
            ):
                walk(value.get(nested_key))

        walk(container)
        return found

    def pick_best_url_for_slide(image) -> str | None:
        if isinstance(image, str):
            return image if is_usable_url(image) else None
        if not isinstance(image, dict):
            return None

        clean_keys = (
            # Web TikTok slideshow shape.
            "imageURL",
            "imageUrl",
            "image_url",
            # Mobile/app TikTok slideshow shape.
            "display_image",
            "displayImage",
        )
        fallback_keys = (
            "downloadUrl",
            "url",
            "cover",
            "owner_watermark_image",
            "ownerWatermarkImage",
            "user_watermark_image",
            "userWatermarkImage",
        )
        thumbnail_keys = ("thumbnail", "thumb", "preview")

        for key in clean_keys:
            candidates = collect_urls_from_container(image.get(key))
            if candidates:
                return candidates[0]

        for key in fallback_keys:
            candidates = collect_urls_from_container(image.get(key))
            if candidates:
                return candidates[0]

        if include_thumbnails:
            for key in thumbnail_keys:
                candidates = collect_urls_from_container(image.get(key))
                if candidates:
                    return candidates[0]

        # Last resort: scan the whole slide object, but still return only one URL
        # for this slide instead of every watermark/thumbnail variant.
        candidates = collect_urls_from_container(image)
        return candidates[0] if candidates else None

    raw_detail = info.get("aweme_detail") or info

    # Prefer explicit slideshow containers. Do not merge imagePost and
    # image_post_info when both exist; they can describe the same slides with
    # different URLs and cause duplicate downloads.
    for post_key in ("imagePost", "image_post_info"):
        image_post = raw_detail.get(post_key) or {}
        images = image_post.get("images") or []
        if not images:
            continue

        for image in images:
            add_url(pick_best_url_for_slide(image))

        if urls:
            return urls

        if include_thumbnails:
            add_url(pick_best_url_for_slide(image_post.get("cover")))
            if urls:
                return urls

    # Generic fallback for non-standard yt-dlp shapes.
    for key in ("entries", "images"):
        for item in info.get(key) or []:
            add_url(pick_best_url_for_slide(item))

    if include_thumbnails:
        for item in info.get("thumbnails") or []:
            add_url(pick_best_url_for_slide(item))

    return urls



def extract_inline_album_assets(info: dict) -> list[MediaAsset]:
    """Return ordered remote assets for inline slideshow results.

    TikTok photo/slideshow posts are usually image-only, but some rare posts can
    contain short video slides. For inline mode we should preserve the slide type:
    - photos become InlineQueryResultPhoto later;
    - videos become InlineQueryResultVideo later.

    The returned assets are URL-only; no HD media is downloaded or uploaded to
    Telegram here.
    """
    assets: list[MediaAsset] = []
    seen_keys: set[str] = set()

    def is_usable_url(value) -> bool:
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            return False
        return ".heic" not in urlparse(value).path.lower()

    def is_video_like_url(value: str) -> bool:
        parsed = urlparse(value)
        haystack = f"{parsed.netloc}{parsed.path}?{parsed.query}".lower()
        return (
            ".mp4" in haystack
            or "mime_type=video" in haystack
            or "mime_type=video_mp4" in haystack
            or "/video/" in haystack
            or "video/tos" in haystack
            or "video_mp4" in haystack
        )

    def collect_urls_from_container(container) -> list[str]:
        found: list[str] = []

        def add_candidate(value) -> None:
            if is_usable_url(value) and value not in found:
                found.append(value)

        def walk(value) -> None:
            if isinstance(value, str):
                add_candidate(value)
                return

            if isinstance(value, list):
                for item in value:
                    walk(item)
                return

            if not isinstance(value, dict):
                return

            for key in (
                "url",
                "uri",
                "src",
                "displayUrl",
                "downloadUrl",
                "playUrl",
                "playApi",
                "mainUrl",
                "backupUrl",
            ):
                add_candidate(value.get(key))

            for list_key in (
                "url_list",
                "urlList",
                "UrlList",
                "url_list_1",
                "UrlList1",
            ):
                walk(value.get(list_key))

            for nested_key in (
                "imageURL",
                "imageUrl",
                "image_url",
                "display_image",
                "displayImage",
                "owner_watermark_image",
                "ownerWatermarkImage",
                "user_watermark_image",
                "userWatermarkImage",
                "thumbnail",
                "thumb",
                "preview",
                "cover",
                "coverUrl",
                "originCover",
                "dynamicCover",
                "animatedCover",
                "playAddr",
                "downloadAddr",
                "play_addr",
                "download_addr",
                "PlayAddr",
                "DownloadAddr",
            ):
                walk(value.get(nested_key))

        walk(container)
        return found

    def is_image_like_url(value: str) -> bool:
        parsed = urlparse(value)
        haystack = f"{parsed.netloc}{parsed.path}?{parsed.query}".lower()
        return (
            ".jpg" in haystack
            or ".jpeg" in haystack
            or ".webp" in haystack
            or ".png" in haystack
            or "mime_type=image" in haystack
            or "/image/" in haystack
            or "image/tos" in haystack
            or "tos-maliva-i" in haystack
            or "tos-alisg-i" in haystack
        )

    def first_url_from_keys(container: dict, keys: tuple[str, ...]) -> str | None:
        for key in keys:
            candidates = collect_urls_from_container(container.get(key))
            if candidates:
                return candidates[0]
        return None

    VIDEO_URL_KEYS = (
        "playAddr",
        "PlayAddr",
        "play_addr",
        "downloadAddr",
        "DownloadAddr",
        "download_addr",
        "playUrl",
        "play_url",
        "videoUrl",
        "videoURL",
        "video_url",
        "mainUrl",
        "backupUrl",
    )

    VIDEO_CONTAINER_KEYS = (
        "video",
        "videoInfo",
        "video_info",
        "videoStruct",
        "video_data",
        "videoData",
        "videoResource",
        "video_resource",
        "livePhoto",
        "live_photo",
        "motionPhoto",
        "motion_photo",
        "animatedImage",
        "animated_image",
        "playAddr",
        "downloadAddr",
        "play_addr",
        "download_addr",
        "PlayAddr",
        "DownloadAddr",
        "bitrateInfo",
        "bitrate_info",
        "bitRate",
        "bit_rate",
    )

    def first_deep_url_from_keys(container, keys: tuple[str, ...], *, allow_image_fallback: bool = False) -> str | None:
        """Find a URL under specific video-address keys, even in nested bitrate arrays."""
        stack = [container]
        seen: set[int] = set()

        while stack:
            current = stack.pop()
            current_id = id(current)
            if current_id in seen:
                continue
            seen.add(current_id)

            if isinstance(current, dict):
                for key, value in current.items():
                    if key in keys:
                        candidates = collect_urls_from_container(value)
                        for candidate in candidates:
                            if is_video_like_url(candidate):
                                return candidate
                        if allow_image_fallback:
                            for candidate in candidates:
                                if not is_image_like_url(candidate):
                                    return candidate
                            if candidates:
                                return candidates[0]
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)

        return None

    def find_explicit_video_containers(slide: dict) -> list:
        containers = []
        for key in VIDEO_CONTAINER_KEYS:
            value = slide.get(key)
            if value:
                containers.append(value)

        # Some TikTok variants hide video data one level deeper but keep obvious
        # names like `videoResource` or `livePhoto`. Do a shallow scan by key name.
        for key, value in slide.items():
            key_lower = str(key).lower()
            if (
                value
                and value not in containers
                and (
                    "video" in key_lower
                    or "bitrate" in key_lower
                    or "livephoto" in key_lower
                    or "motionphoto" in key_lower
                    or key in VIDEO_URL_KEYS
                )
            ):
                containers.append(value)

        return containers

    def first_video_url_from_container(container) -> str | None:
        candidates = collect_urls_from_container(container)
        for candidate in candidates:
            if is_video_like_url(candidate):
                return candidate
        return None

    def pick_photo_url(slide: dict) -> str | None:
        photo_url = first_url_from_keys(
            slide,
            (
                "imageURL",
                "imageUrl",
                "image_url",
                "display_image",
                "displayImage",
                "downloadUrl",
                "url",
            ),
        )
        if photo_url:
            return photo_url

        # Last-resort fallback for odd TikTok shapes. Keep only one URL per slide,
        # not every watermark/thumbnail variant.
        candidates = collect_urls_from_container(slide)
        return candidates[0] if candidates else None

    def pick_thumbnail_url(slide: dict) -> str | None:
        # For video slides the thumbnail may live either directly on the slide or
        # inside the nested video object.
        direct = first_url_from_keys(
            slide,
            (
                "thumbnail",
                "thumb",
                "preview",
                "cover",
                "coverUrl",
                "originCover",
                "dynamicCover",
                "animatedCover",
                "imageURL",
                "imageUrl",
                "display_image",
                "displayImage",
            ),
        )
        if direct:
            return direct

        video = slide.get("video") or slide.get("videoInfo") or slide.get("video_info") or slide.get("videoStruct")
        if isinstance(video, dict):
            return first_url_from_keys(
                video,
                (
                    "cover",
                    "coverUrl",
                    "originCover",
                    "dynamicCover",
                    "animatedCover",
                    "thumbnail",
                    "thumb",
                ),
            )
        return None

    def pick_video_url(slide: dict) -> str | None:
        # Prefer explicit video-address fields. A normal image slide usually only
        # has imageURL/displayImage fields; a mixed/video slide should expose
        # playAddr/downloadAddr/bitrateInfo or an obvious video/live-photo object.
        explicit_containers = find_explicit_video_containers(slide)

        for container in explicit_containers:
            found = first_deep_url_from_keys(container, VIDEO_URL_KEYS, allow_image_fallback=True)
            if found:
                return found

        for container in explicit_containers:
            found = first_video_url_from_container(container)
            if found:
                return found

        # Some structures mark the slide type but keep the URL at the slide root.
        media_type_hint = str(
            slide.get("type")
            or slide.get("mediaType")
            or slide.get("media_type")
            or slide.get("slideType")
            or slide.get("itemType")
            or slide.get("subType")
            or ""
        ).lower()
        if "video" in media_type_hint or "live" in media_type_hint or media_type_hint in {"2", "4", "video_slide"}:
            found = first_deep_url_from_keys(slide, VIDEO_URL_KEYS, allow_image_fallback=True)
            if found:
                return found
            return first_video_url_from_container(slide)

        return None

    def add_asset(media_type: str, remote_url: str | None, thumbnail_url: str | None = None) -> None:
        if not remote_url or not is_usable_url(remote_url):
            return
        dedupe_key = f"{media_type}:{remote_url}"
        if dedupe_key in seen_keys:
            return
        seen_keys.add(dedupe_key)
        assets.append(
            MediaAsset(
                path=Path(""),
                media_type=media_type,
                remote_url=remote_url,
                thumbnail_url=thumbnail_url or remote_url,
            )
        )

    def add_slide(slide) -> None:
        if isinstance(slide, str):
            add_asset("photo", slide, slide)
            return

        if not isinstance(slide, dict):
            return

        video_url = pick_video_url(slide)
        if video_url:
            add_asset("video", video_url, pick_thumbnail_url(slide) or pick_photo_url(slide) or video_url)
            return

        photo_url = pick_photo_url(slide)
        add_asset("photo", photo_url, pick_thumbnail_url(slide) or photo_url)

    raw_detail = info.get("aweme_detail") or info

    for post_key in ("imagePost", "image_post_info"):
        image_post = raw_detail.get(post_key) or {}
        images = image_post.get("images") or []
        if not images:
            continue

        for slide in images:
            add_slide(slide)

        if assets:
            return assets

    # Generic fallback for non-standard yt-dlp/TikTok shapes.
    for key in ("entries", "images"):
        for item in info.get(key) or []:
            add_slide(item)

    return assets



def extract_inline_audio_asset(info: dict) -> MediaAsset | None:
    """Return the slideshow music as a URL-only inline audio asset, if present.

    TikTok photo mode usually stores the soundtrack in raw_detail["music"].playUrl.
    This is not a per-slide video; it is the audio track for the whole slideshow.
    Telegram can expose it as a separate InlineQueryResultAudio.
    """
    raw_detail = info.get("aweme_detail") or info
    if not isinstance(raw_detail, dict):
        return None

    music = raw_detail.get("music") or raw_detail.get("music_info") or raw_detail.get("musicInfo") or {}
    if not isinstance(music, dict):
        return None

    def first_string(*values) -> str | None:
        for value in values:
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    def first_int(*values) -> int | None:
        for value in values:
            if value is None or value == "":
                continue
            try:
                number = int(float(value))
            except (TypeError, ValueError):
                continue
            if number > 0:
                return number
        return None

    def collect_urls(container) -> list[str]:
        found: list[str] = []

        def add(value) -> None:
            if isinstance(value, str) and value.startswith(("http://", "https://")) and value not in found:
                found.append(value)

        def walk(value) -> None:
            if isinstance(value, str):
                add(value)
                return
            if isinstance(value, list):
                for item in value:
                    walk(item)
                return
            if not isinstance(value, dict):
                return

            for key in (
                "playUrl",
                "play_url",
                "audioUrl",
                "audioURL",
                "audio_url",
                "url",
                "downloadUrl",
                "mainUrl",
                "backupUrl",
            ):
                walk(value.get(key))

            for key in ("urlList", "url_list", "UrlList"):
                walk(value.get(key))

        walk(container)
        return found

    audio_url = first_string(
        music.get("playUrl"),
        music.get("play_url"),
        music.get("audioUrl"),
        music.get("audioURL"),
        music.get("audio_url"),
    )

    if not audio_url:
        for candidate in collect_urls(music):
            lower = candidate.lower()
            if "mime_type=audio" in lower or "audio" in lower or lower.endswith((".mp3", ".m4a", ".aac")):
                audio_url = candidate
                break

    if not audio_url or not audio_url.startswith(("http://", "https://")):
        return None

    precise_duration = music.get("preciseDuration") or {}
    if not isinstance(precise_duration, dict):
        precise_duration = {}

    title = first_string(music.get("title"), raw_detail.get("desc"), info.get("title"), "TikTok audio")
    performer = first_string(music.get("authorName"), music.get("author"), music.get("owner"))
    duration_seconds = first_int(
        music.get("duration"),
        music.get("shoot_duration"),
        precise_duration.get("preciseDuration"),
        precise_duration.get("preciseVideoDuration"),
        precise_duration.get("preciseShootDuration"),
    )

    logger.info("TikTok inline slideshow audio prepared: title=%s duration=%s", title, duration_seconds)

    return MediaAsset(
        path=Path(""),
        media_type="audio",
        remote_url=audio_url,
        thumbnail_url=None,
        title=title,
        performer=performer,
        duration_seconds=duration_seconds,
    )

def extract_photo_url_pairs(info: dict) -> list[tuple[str, str]]:
    """Backward-compatible wrapper for older local-photo code paths."""
    return [
        (asset.remote_url, asset.thumbnail_url or asset.remote_url)
        for asset in extract_inline_album_assets(info)
        if asset.media_type == "photo" and asset.remote_url
    ]

def detect_media_kind(info: dict) -> str:
    if extract_photo_urls(info):
        return "photo_album"

    has_video_formats = bool(info.get("formats") or info.get("requested_formats") or info.get("url"))
    if not has_video_formats and len(extract_photo_urls(info, include_thumbnails=True)) > 1:
        return "photo_album"

    return "video"


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


def extension_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".webp", ".png"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".jpg"


def download_photo_album_sync(url: str, output_dir: Path, info: dict) -> tuple[dict, list[Path]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    media_id = str(info.get("id") or parse_video_id_from_url(url) or url_hash(url))
    photo_urls = extract_photo_urls(info) or extract_photo_urls(info, include_thumbnails=True)
    photo_urls = photo_urls[: settings.max_photo_count]

    if not photo_urls:
        raise RuntimeError("yt-dlp did not return photo URLs")

    logger.info("TikTok photo album extracted %d candidate photo URL(s)", len(photo_urls))

    paths: list[Path] = []
    seen_content_hashes: set[str] = set()
    options = ytdlp_base_options()

    with YoutubeDL(options) as ydl:
        for source_index, photo_url in enumerate(photo_urls, start=1):
            try:
                response = ydl.urlopen(photo_url)
                try:
                    content = response.read()
                finally:
                    try:
                        response.close()
                    except Exception:
                        pass
            except Exception as exc:
                raise RuntimeError(f"failed to download photo {source_index}: {exc}") from exc

            if not content:
                raise RuntimeError(f"downloaded photo is empty: source #{source_index}")

            content_hash = __import__("hashlib").sha256(content).hexdigest()
            if content_hash in seen_content_hashes:
                logger.info("TikTok photo album skipped duplicate photo candidate #%d", source_index)
                continue

            seen_content_hashes.add(content_hash)
            path = output_dir / f"{media_id}_{len(paths) + 1:03d}{extension_from_url(photo_url)}"
            path.write_bytes(content)

            if not path.exists() or path.stat().st_size == 0:
                raise RuntimeError(f"downloaded photo is empty: {path}")

            paths.append(path)

    if not paths:
        raise RuntimeError("yt-dlp did not return unique photo files")

    logger.info("TikTok photo album saved %d unique photo file(s)", len(paths))

    return info, paths




def collect_interesting_media_paths(value, *, max_items: int = 200) -> list[dict]:
    """Collect suspicious media-related paths from a TikTok JSON object.

    This is diagnostic only. It helps understand rare slideshow variants where a
    TikTok mobile app shows a slide as video, while web hydration exposes only a
    still image in imagePost.images[].
    """
    interesting: list[dict] = []
    seen: set[int] = set()

    def should_keep_key(key: str) -> bool:
        key_lower = key.lower()
        return any(
            token in key_lower
            for token in (
                "video",
                "play",
                "download",
                "bitrate",
                "bit_rate",
                "live",
                "motion",
                "animated",
                "animation",
                "cover",
                "url",
                "uri",
                "mime",
            )
        )

    def should_keep_string(text: str) -> bool:
        lower = text.lower()
        return any(
            token in lower
            for token in (
                ".mp4",
                ".m3u8",
                "video",
                "tos-maliva",
                "photomode",
                "mime_type=video",
                "mime_type=video_mp4",
            )
        )

    def walk(current, path: str, parent_key: str = "") -> None:
        if len(interesting) >= max_items:
            return

        if isinstance(current, (dict, list)):
            current_id = id(current)
            if current_id in seen:
                return
            seen.add(current_id)

        if isinstance(current, dict):
            for key, nested in current.items():
                nested_path = f"{path}.{key}" if path else str(key)
                if should_keep_key(str(key)):
                    preview = nested
                    if isinstance(preview, (dict, list)):
                        preview = type(preview).__name__
                    interesting.append(
                        {
                            "path": nested_path,
                            "key": str(key),
                            "value_type": type(nested).__name__,
                            "preview": preview if isinstance(preview, (str, int, float, bool)) or preview is None else repr(preview)[:300],
                        }
                    )
                    if len(interesting) >= max_items:
                        return
                walk(nested, nested_path, str(key))
        elif isinstance(current, list):
            for index, nested in enumerate(current):
                walk(nested, f"{path}[{index}]", parent_key)
                if len(interesting) >= max_items:
                    return
        elif isinstance(current, str):
            if should_keep_key(parent_key) or should_keep_string(current):
                interesting.append(
                    {
                        "path": path,
                        "key": parent_key,
                        "value_type": "str",
                        "preview": current[:1000],
                    }
                )

    walk(value, "")
    return interesting


def dump_inline_slideshow_debug(info: dict, media_id: str, assets: list[MediaAsset]) -> None:
    """Optionally dump slideshow JSON for rare TikTok mixed post formats.

    Enable with TIKTOK_DEBUG_INLINE_SLIDES=1 in .env.

    The compact dump remains small and focuses on imagePost.images[]. Enable
    TIKTOK_DEBUG_INLINE_FULL=1 as well to write the whole aweme/itemStruct and a
    media-hints index. That is needed when the mobile app displays a slide as a
    video but web imagePost.images[] contains only imageURL/imageWidth/imageHeight.
    """
    if os.getenv("TIKTOK_DEBUG_INLINE_SLIDES", "0") != "1":
        return

    try:
        raw_detail = info.get("aweme_detail") or info
        image_post = raw_detail.get("imagePost") or raw_detail.get("image_post_info") or {}
        images = image_post.get("images") or []
        debug_payload = {
            "media_id": media_id,
            "root_keys": sorted(raw_detail.keys()) if isinstance(raw_detail, dict) else type(raw_detail).__name__,
            "image_post_keys": sorted(image_post.keys()) if isinstance(image_post, dict) else type(image_post).__name__,
            "asset_types": [asset.media_type for asset in assets],
            "asset_urls": [getattr(asset, "remote_url", None) for asset in assets],
            "slides": [
                {
                    "index": index,
                    "keys": sorted(slide.keys()) if isinstance(slide, dict) else type(slide).__name__,
                    "raw": slide,
                }
                for index, slide in enumerate(images, start=1)
            ],
        }
        settings.cache_dir.mkdir(parents=True, exist_ok=True)
        path = settings.cache_dir / f"tiktok_inline_slides_{media_id}.json"
        path.write_text(json.dumps(debug_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.warning("TikTok inline slideshow debug dump written: %s", path)

        if os.getenv("TIKTOK_DEBUG_INLINE_FULL", "0") == "1":
            full_payload = {
                "media_id": media_id,
                "asset_types": [asset.media_type for asset in assets],
                "media_hints": collect_interesting_media_paths(raw_detail),
                "raw_detail": raw_detail,
            }
            full_path = settings.cache_dir / f"tiktok_inline_full_{media_id}.json"
            full_path.write_text(json.dumps(full_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.warning("TikTok inline full debug dump written: %s", full_path)
    except Exception:
        logger.exception("Failed to write TikTok inline slideshow debug dump")

def probe_photo_album_links_sync(url: str) -> DownloadResult:
    """Probe a TikTok photo post and return remote photo/thumbnail URLs only.

    This is the fast inline path: it does not download HD photos locally and does
    not upload them to DUMP_CHAT_ID. Telegram clients can show thumbnail_url in
    the inline result grid and Telegram downloads photo_url only after selection.
    """
    if not is_tiktok_url(url):
        raise RuntimeError("not a TikTok URL")

    effective_url = normalize_url(url)

    if parse_video_id_from_url(effective_url) is None:
        resolved_url = resolve_tiktok_redirect_sync(effective_url)
        if resolved_url and resolved_url != effective_url:
            logger.info("TikTok URL resolved for inline photo preview: %s -> %s", effective_url, resolved_url)
            effective_url = resolved_url

    if not (is_tiktok_photo_url(effective_url) or "/photo/" in effective_url.lower()):
        raise RuntimeError("not a TikTok photo post")

    media_id = parse_video_id_from_url(effective_url)
    if not media_id:
        raise RuntimeError("TikTok photo URL did not contain media id")

    ytdlp_url = to_ytdlp_tiktok_url(effective_url)
    ytdlp_url = re.sub(r"/photo/(\d+)", r"/video/\1", ytdlp_url, flags=re.IGNORECASE)

    info = probe_photo_info_sync(ytdlp_url, media_id, effective_url)
    visual_assets = extract_inline_album_assets(info)[: settings.max_photo_count]
    audio_asset = extract_inline_audio_asset(info)
    assets = ([audio_asset] if audio_asset else []) + visual_assets
    dump_inline_slideshow_debug(info, media_id, assets)

    if not assets:
        raise RuntimeError("TikTok webpage did not return inline slideshow media URLs")

    logger.info(
        "TikTok inline slideshow preview prepared %d remote asset(s): %s",
        len(assets),
        ", ".join(asset.media_type for asset in assets),
    )

    return DownloadResult(
        media_id=str(info.get("id") or media_id),
        kind="photo_album",
        source_url=effective_url,
        title=info.get("title"),
        assets=assets,
    )

async def to_thread_ytdlp(func, *args):
    try:
        return await asyncio.to_thread(func, *args)
    except DownloadError as exc:
        raise PublicBotError(classify_download_error(exc)) from exc
    except Exception as exc:
        raise PublicBotError(classify_download_error(exc)) from exc


async def get_or_download_media(url: str) -> DownloadResult:
    logger.warning("ACTIVE get_or_download_media FILE: %s", __file__)
    logger.warning("ACTIVE URL: %s", url)

    if not is_tiktok_url(url):
        raise PublicBotError(t.invalid_tiktok_url)

    original_url = url

    cached = await find_cached_by_url(original_url)
    if cached:
        return cached

    effective_url = original_url

    # Short TikTok links like /t/... do not contain /video/<id> or /photo/<id>.
    # Resolve them before deciding whether this is a video or a photo post.
    if parse_video_id_from_url(effective_url) is None:
        resolved_url = await to_thread_ytdlp(resolve_tiktok_redirect_sync, effective_url)

        if resolved_url and resolved_url != effective_url:
            logger.info("TikTok URL resolved: %s -> %s", effective_url, resolved_url)
            effective_url = resolved_url

    cached = await find_cached_by_url(effective_url)
    if cached:
        return cached

    is_photo_url = is_tiktok_photo_url(effective_url) or "/photo/" in effective_url.lower()
    ytdlp_url = to_ytdlp_tiktok_url(effective_url)

    if is_photo_url:
        ytdlp_url = re.sub(r"/photo/(\d+)", r"/video/\1", ytdlp_url, flags=re.IGNORECASE)

    parsed_video_id = parse_video_id_from_url(effective_url)

    if parsed_video_id:
        cached = await find_cached_by_media_id(parsed_video_id)
        if cached:
            return cached

    logger.info(
        "TikTok media probe: id=%s is_photo=%s source=%s effective=%s ytdlp=%s",
        parsed_video_id,
        is_photo_url,
        original_url,
        effective_url,
        ytdlp_url,
    )

    if parsed_video_id and is_photo_url:
        # Use /video/<id> for probing because yt-dlp's TikTok web extractor reads
        # the post from webapp.video-detail even when the public URL is /photo/<id>.
        info = await to_thread_ytdlp(probe_photo_info_sync, ytdlp_url, parsed_video_id, effective_url)
    else:
        info = await to_thread_ytdlp(probe_info_sync, ytdlp_url)

    media_id = str(info.get("id") or parsed_video_id or url_hash(effective_url))
    title = info.get("title")
    kind = detect_media_kind(info)

    cached = await find_cached_by_media_id(media_id)
    if cached:
        return cached

    lock = get_media_lock(media_id)
    async with lock:
        cached = await find_cached_by_media_id(media_id)
        if cached:
            return cached

        if settings.enable_cache:
            output_dir = settings.cache_dir
        else:
            output_dir = Path(tempfile.mkdtemp(prefix="ttbot_"))

        if kind == "photo_album":
            info, paths = await to_thread_ytdlp(
                download_photo_album_sync,
                effective_url,
                output_dir,
                info,
            )

            oversized = [path for path in paths if path.stat().st_size > settings.max_photo_size_bytes]
            if oversized:
                if not settings.enable_cache:
                    shutil.rmtree(output_dir, ignore_errors=True)

                raise PublicBotError(
                    t.photo_too_large.format(
                        size_mb=oversized[0].stat().st_size // 1024 // 1024,
                        limit_mb=settings.max_photo_size_bytes // 1024 // 1024
                    )
                )

            result = DownloadResult(
                media_id=str(info.get("id") or media_id),
                kind="photo_album",
                source_url=effective_url,
                title=info.get("title") or title,
                assets=[MediaAsset(path=path, media_type="photo") for path in paths],
                temp_dir=None if settings.enable_cache else output_dir,
            )

            await put_cache(result, original_url)

            if effective_url != original_url:
                await put_cache(result, effective_url)

            return result

        info, path = await to_thread_ytdlp(download_video_sync, ytdlp_url, output_dir)

        if path.stat().st_size > settings.max_video_size_bytes:
            if not settings.enable_cache:
                shutil.rmtree(output_dir, ignore_errors=True)

            raise PublicBotError(
                t.video_too_large.format(
                    size_mb=path.stat().st_size // 1024 // 1024,
                    limit_mb=settings.max_video_size_bytes // 1024 // 1024
                )
            )

        result = DownloadResult(
            media_id=str(info.get("id") or media_id),
            kind="video",
            source_url=effective_url,
            title=info.get("title") or title,
            assets=[MediaAsset(path=path, media_type="video")],
            temp_dir=None if settings.enable_cache else output_dir,
        )

        await put_cache(result, original_url)

        if effective_url != original_url:
            await put_cache(result, effective_url)

        return result


async def get_or_download_video(url: str) -> DownloadResult:
    result = await get_or_download_media(url)
    if result.kind != "video":
        raise PublicBotError(t.expected_video_got_album)
    return result
