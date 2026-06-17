import asyncio
import html
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
        return (
            "TikTok заблокировал текущий IP-адрес. "
            "Проверь TIKTOK_PROXY в .env или выбери другой proxy/VPN endpoint."
        )

    if "connectionrefusederror" in lower or "connection refused" in lower or "winerror 10061" in lower:
        proxy_hint = f" ({settings.tiktok_proxy})" if settings.tiktok_proxy else ""
        return (
            f"не удалось подключиться к TIKTOK_PROXY{proxy_hint}. "
            "Проверь, что proxy/VPN запущен и адрес в .env указан верно."
        )

    if "failed to parse json" in lower:
        return (
            "TikTok API вернул пустой или невалидный ответ. "
            "Проверь TIKTOK_PROXY/VPN и повтори запрос."
        )

    http_match = re.search(r"HTTP Error\s+(\d{3})|status code\s+(\d{3})|HTTP\s+(\d{3})", text)
    if http_match:
        code = next(group for group in http_match.groups() if group)
        return f"HTTP {code}: {text[:500]}"

    if "timed out" in lower or "timeout" in lower:
        return f"таймаут: {text[:500]}"

    return f"внутренняя ошибка скачивания: {text[:500]}"


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



def extract_photo_url_pairs(info: dict) -> list[tuple[str, str]]:
    """Return (photo_url, thumbnail_url) pairs for fast inline photo results.

    InlineQueryResultPhoto can show thumbnail_url immediately and send photo_url
    only after the user chooses the result. This avoids uploading every HD image
    to Telegram before answering the inline query.
    """
    pairs: list[tuple[str, str]] = []
    seen_photo_urls: set[str] = set()

    def is_usable_url(value) -> bool:
        if not isinstance(value, str) or not value.startswith(("http://", "https://")):
            return False
        return ".heic" not in urlparse(value).path.lower()

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
                "thumb",
                "preview",
                "cover",
            ):
                walk(value.get(nested_key))

        walk(container)
        return found

    def first_url_from_keys(image: dict, keys: tuple[str, ...]) -> str | None:
        for key in keys:
            candidates = collect_urls_from_container(image.get(key))
            if candidates:
                return candidates[0]
        return None

    def add_pair(photo_url: str | None, thumbnail_url: str | None = None) -> None:
        if not photo_url or not is_usable_url(photo_url) or photo_url in seen_photo_urls:
            return
        seen_photo_urls.add(photo_url)
        pairs.append((photo_url, thumbnail_url or photo_url))

    def add_slide(image) -> None:
        if isinstance(image, str):
            add_pair(image, image)
            return

        if not isinstance(image, dict):
            return

        photo_url = first_url_from_keys(
            image,
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
        thumbnail_url = first_url_from_keys(image, ("thumbnail", "thumb", "preview", "cover"))

        if not photo_url:
            # Last-resort fallback for odd TikTok shapes. Keep only one URL per
            # slide, not every watermark/thumbnail variant.
            candidates = collect_urls_from_container(image)
            photo_url = candidates[0] if candidates else None

        add_pair(photo_url, thumbnail_url)

    raw_detail = info.get("aweme_detail") or info

    for post_key in ("imagePost", "image_post_info"):
        image_post = raw_detail.get(post_key) or {}
        images = image_post.get("images") or []
        if not images:
            continue

        for image in images:
            add_slide(image)

        if pairs:
            return pairs

    for key in ("entries", "images"):
        for item in info.get(key) or []:
            add_slide(item)

    return pairs

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
    url_pairs = extract_photo_url_pairs(info)[: settings.max_photo_count]

    if not url_pairs:
        raise RuntimeError("TikTok webpage did not return inline photo URLs")

    logger.info("TikTok inline photo preview prepared %d remote photo URL(s)", len(url_pairs))

    return DownloadResult(
        media_id=str(info.get("id") or media_id),
        kind="photo_album",
        source_url=effective_url,
        title=info.get("title"),
        assets=[
            MediaAsset(
                path=Path(""),
                media_type="photo",
                remote_url=photo_url,
                thumbnail_url=thumbnail_url,
            )
            for photo_url, thumbnail_url in url_pairs
        ],
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
        raise PublicBotError("невалидная ссылка: поддерживаются только ссылки TikTok")

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
                    f"фото слишком большое: {oversized[0].stat().st_size // 1024 // 1024} MB, "
                    f"лимит {settings.max_photo_size_bytes // 1024 // 1024} MB"
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
                f"видео слишком большое: {path.stat().st_size // 1024 // 1024} MB, "
                f"лимит {settings.max_video_size_bytes // 1024 // 1024} MB"
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
        raise PublicBotError("ссылка ведёт на фотоальбом, а ожидалось видео")
    return result
