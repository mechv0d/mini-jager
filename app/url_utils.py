import hashlib
import re
from urllib.parse import urlparse, urlunparse

URL_RE = re.compile(r"https?://[^\s<>()\"']+", re.IGNORECASE)
LONG_TIKTOK_VIDEO_ID_RE = re.compile(r"/video/(\d+)", re.IGNORECASE)


def normalize_url(url: str) -> str:
    url = url.strip().strip(".,;!?)В»\"'")
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", "", ""))


def is_tiktok_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False

    if parsed.scheme not in {"http", "https"}:
        return False

    host = parsed.netloc.lower()
    return host == "tiktok.com" or host.endswith(".tiktok.com")


def extract_tiktok_url(text: str | None) -> str | None:
    if not text:
        return None

    for match in URL_RE.findall(text):
        url = normalize_url(match)
        if is_tiktok_url(url):
            return url

    return None


def parse_video_id_from_url(url: str) -> str | None:
    parsed = urlparse(url)
    match = LONG_TIKTOK_VIDEO_ID_RE.search(parsed.path)
    if match:
        return match.group(1)
    return None


def url_hash(url: str) -> str:
    return hashlib.sha256(normalize_url(url).encode("utf-8")).hexdigest()


def safe_inline_id(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
    return digest[:64]
