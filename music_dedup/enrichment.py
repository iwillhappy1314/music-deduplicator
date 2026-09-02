"""根据音频元数据获取外挂歌词和专辑封面的安全写入逻辑。"""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .core import (
    AudioRecord,
    has_embedded_lyrics,
    normalize_artist_for_match,
    normalize_for_match,
    read_album_metadata,
)


class EnrichmentError(Exception):
    """表示歌词或专辑封面接口响应无法安全处理。"""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        """保存接口错误信息和可用的 HTTP 状态码。"""

        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class LyricsLookup:
    """保存一次 LRCLIB 查询的可复用结果。"""

    payload: dict[str, Any] | None
    status: str
    error: str | None = None


@dataclass(frozen=True)
class EnrichmentAction:
    """保存一次歌词或专辑封面补齐动作及结果。"""

    kind: str
    audio_path: Path
    destination: Path | None
    status: str
    source: str | None = None
    error: str | None = None

    def to_report(self, root: Path) -> dict[str, str | None]:
        """将补齐动作转换为相对音乐库根目录的报告结构。"""

        return {
            "kind": self.kind,
            "audio_path": str(self.audio_path),
            "relative_audio_path": str(self.audio_path.relative_to(root)),
            "destination": str(self.destination) if self.destination is not None else None,
            "status": self.status,
            "source": self.source,
            "error": self.error,
        }


LYRICS_API_URL = "https://lrclib.net/api/get"
ARTWORK_API_URL = "https://itunes.apple.com/search"
DEFAULT_USER_AGENT = "music-deduplicator/1.0 (https://github.com/iwillhappy1314/music-deduplicator)"
LYRICS_SIDECAR_SUFFIXES = (".ttml", ".yaml", ".yml", ".elrc", ".lrc", ".srt", ".txt")
COVER_FILENAMES = (
    "cover.jpg",
    "cover.jpeg",
    "cover.png",
    "cover.webp",
    "folder.jpg",
    "folder.jpeg",
    "folder.png",
    "folder.webp",
    "front.jpg",
    "front.jpeg",
    "front.png",
    "front.webp",
)
MAX_RESPONSE_BYTES = 12 * 1024 * 1024


def _request_bytes(url: str, user_agent: str, timeout: float) -> bytes:
    """使用带有客户端标识的 HTTP GET 获取有限大小的响应体。"""

    request = Request(url, headers={"User-Agent": user_agent, "Accept": "application/json, image/*"})
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_RESPONSE_BYTES + 1)
    except HTTPError as error:
        raise EnrichmentError(f"网络请求失败: {error}", status_code=error.code) from error
    except (URLError, OSError, TimeoutError) as error:
        raise EnrichmentError(f"网络请求失败: {error}") from error
    if len(payload) > MAX_RESPONSE_BYTES:
        raise EnrichmentError("接口响应超过安全大小限制")
    return payload


def _request_json(url: str, user_agent: str, timeout: float) -> dict[str, Any]:
    """请求 JSON 接口并验证顶层响应结构。"""

    try:
        payload = json.loads(_request_bytes(url, user_agent, timeout).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise EnrichmentError(f"接口返回的 JSON 无法解析: {error}") from error
    if not isinstance(payload, dict):
        raise EnrichmentError("接口返回的 JSON 不是对象")
    return payload


def _write_new_file(path: Path, content: bytes) -> bool:
    """以不覆盖既有文件的方式原子写入新文件。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    file_descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary_path = temporary_name
    try:
        with os.fdopen(file_descriptor, "wb") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        try:
            os.link(temporary_name, path)
        except FileExistsError:
            return False
        return True
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def _with_trailing_newline(value: str) -> str:
    """统一外挂歌词文本的结尾换行，避免生成难以追加的单行文件。"""

    return value.rstrip("\r\n") + "\n"


def _artist_matches(expected: str, actual: str) -> bool:
    """使用与去重相同的简繁转换规则比较接口返回的演唱者。"""

    return normalize_artist_for_match(expected) == normalize_artist_for_match(actual)


def _sidecar_exists(audio_path: Path) -> Path | None:
    """查找同名的任意已存在歌词外挂文件。"""

    for suffix in LYRICS_SIDECAR_SUFFIXES:
        candidate = audio_path.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None


def _existing_lyrics_action(record: AudioRecord) -> EnrichmentAction | None:
    """检查音频是否已经存在外挂或内嵌歌词。"""

    existing_sidecar = _sidecar_exists(record.path)
    if existing_sidecar is not None or has_embedded_lyrics(record.path):
        return EnrichmentAction("lyrics", record.path, existing_sidecar, "skipped_exists")
    return None


def _lookup_lyrics(
    record: AudioRecord,
    api_url: str,
    user_agent: str,
    timeout: float,
) -> LyricsLookup:
    """从 LRCLIB 查询歌词并返回可供同键歌曲复用的结果。"""

    query = urlencode(
        {
            "artist_name": record.artist,
            "track_name": record.title,
        }
    )
    try:
        payload = _request_json(f"{api_url}?{query}", user_agent, timeout)
    except EnrichmentError as error:
        if error.status_code == 404:
            return LyricsLookup(None, "not_found")
        return LyricsLookup(None, "failed", str(error))

    return LyricsLookup(payload, "found")


def _lyrics_action_from_lookup(record: AudioRecord, lookup: LyricsLookup) -> EnrichmentAction:
    """根据歌词查询结果为指定音频创建外挂歌词动作。"""

    if lookup.status == "not_found":
        return EnrichmentAction("lyrics", record.path, None, "not_found", "lrclib")
    if lookup.status == "failed":
        return EnrichmentAction("lyrics", record.path, None, "failed", "lrclib", lookup.error)
    if lookup.payload is None:
        return EnrichmentAction("lyrics", record.path, None, "not_found", "lrclib")
    payload = lookup.payload

    result_title = str(payload.get("trackName") or payload.get("name") or "").strip()
    result_artist = str(payload.get("artistName") or "").strip()
    if not result_title or not result_artist:
        return EnrichmentAction("lyrics", record.path, None, "not_found", "lrclib")
    if normalize_for_match(result_title) != record.normalized_title or not _artist_matches(
        record.artist, result_artist
    ):
        return EnrichmentAction("lyrics", record.path, None, "not_found", "lrclib")

    synced_lyrics = str(payload.get("syncedLyrics") or "").strip()
    plain_lyrics = str(payload.get("plainLyrics") or "").strip()
    lyrics = synced_lyrics or plain_lyrics
    if not lyrics:
        return EnrichmentAction("lyrics", record.path, None, "not_found", "lrclib")

    suffix = ".lrc" if synced_lyrics else ".txt"
    destination = record.path.with_suffix(suffix)
    if _write_new_file(destination, _with_trailing_newline(lyrics).encode("utf-8")):
        return EnrichmentAction("lyrics", record.path, destination, "created", "lrclib")
    return EnrichmentAction("lyrics", record.path, destination, "skipped_exists", "lrclib")


def _lyrics_action(
    record: AudioRecord,
    api_url: str,
    user_agent: str,
    timeout: float,
) -> EnrichmentAction:
    """为一个音频文件查询并保存外挂歌词。"""

    existing_action = _existing_lyrics_action(record)
    if existing_action is not None:
        return existing_action
    return _lyrics_action_from_lookup(record, _lookup_lyrics(record, api_url, user_agent, timeout))


def fetch_lyrics(
    records: Sequence[AudioRecord],
    request_delay: float = 0.35,
    api_url: str = LYRICS_API_URL,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 20,
) -> tuple[EnrichmentAction, ...]:
    """按 Artist 和 Title 顺序获取歌词，不覆盖任何已有歌词。"""

    actions: list[EnrichmentAction] = []
    lookup_cache: dict[tuple[str, str], LyricsLookup] = {}
    for record in records:
        if not record.path.exists():
            actions.append(EnrichmentAction("lyrics", record.path, None, "skipped_missing"))
            continue
        existing_action = _existing_lyrics_action(record)
        if existing_action is not None:
            actions.append(existing_action)
            continue
        key = record.match_key
        if key not in lookup_cache:
            lookup_cache[key] = _lookup_lyrics(record, api_url, user_agent, timeout)
            if request_delay > 0:
                time.sleep(request_delay)
        action = _lyrics_action_from_lookup(record, lookup_cache[key])
        actions.append(action)
    return tuple(actions)


def _find_itunes_album(results: Iterable[object], record: AudioRecord, album: str) -> dict[str, Any] | None:
    """从专辑搜索结果中选择 Artist 和 Album 都精确匹配的结果。"""

    for item in results:
        if not isinstance(item, dict):
            continue
        collection_name = str(item.get("collectionName") or "").strip()
        artist_name = str(item.get("artistName") or "").strip()
        if normalize_for_match(collection_name) != normalize_for_match(album):
            continue
        if not _artist_matches(record.artist, artist_name):
            continue
        artwork_url = str(item.get("artworkUrl100") or item.get("artworkUrl60") or "").strip()
        if artwork_url:
            return item
    return None


def _image_extension(payload: bytes) -> str | None:
    """根据图片文件头选择 Navidrome 可识别的封面扩展名。"""

    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp"
    return None


def _artwork_action(
    directory: Path,
    record: AudioRecord,
    request_delay: float,
    api_url: str,
    user_agent: str,
    timeout: float,
) -> EnrichmentAction:
    """为一个专辑目录搜索并保存专辑封面。"""

    existing_cover = next(
        (directory / filename for filename in COVER_FILENAMES if (directory / filename).is_file()),
        None,
    )
    if existing_cover is not None:
        return EnrichmentAction("artwork", record.path, existing_cover, "skipped_exists")

    album = read_album_metadata(record.path)
    if not album:
        return EnrichmentAction("artwork", record.path, None, "skipped_metadata")

    query = urlencode(
        {
            "term": f"{record.artist} {album}",
            "entity": "album",
            "media": "music",
            "limit": "10",
        }
    )
    try:
        payload = _request_json(f"{api_url}?{query}", user_agent, timeout)
        result = _find_itunes_album(payload.get("results", []), record, album)
        if result is None:
            return EnrichmentAction("artwork", record.path, None, "not_found", "itunes")
        artwork_url = str(result.get("artworkUrl100") or result.get("artworkUrl60") or "")
        artwork_url = artwork_url.replace("100x100", "1200x1200").replace("60x60", "1200x1200")
        image = _request_bytes(artwork_url, user_agent, timeout)
        suffix = _image_extension(image)
        if suffix is None:
            raise EnrichmentError("下载内容不是受支持的图片格式")
    except EnrichmentError as error:
        return EnrichmentAction("artwork", record.path, None, "failed", "itunes", str(error))

    destination = directory / f"cover{suffix}"
    if _write_new_file(destination, image):
        return EnrichmentAction("artwork", record.path, destination, "created", "itunes")
    return EnrichmentAction("artwork", record.path, destination, "skipped_exists", "itunes")


def fetch_album_artwork(
    records: Sequence[AudioRecord],
    request_delay: float = 0.35,
    api_url: str = ARTWORK_API_URL,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout: float = 20,
) -> tuple[EnrichmentAction, ...]:
    """按专辑目录获取封面，每个目录最多请求一次且不覆盖已有封面。"""

    actions: list[EnrichmentAction] = []
    processed_directories: set[Path] = set()
    for record in records:
        directory = record.path.parent.resolve()
        if directory in processed_directories:
            continue
        processed_directories.add(directory)
        action = _artwork_action(directory, record, request_delay, api_url, user_agent, timeout)
        actions.append(action)
        if request_delay > 0 and action.status not in {"skipped_exists", "skipped_metadata"}:
            time.sleep(request_delay)
    return tuple(actions)


def build_enrichment_report(actions: Sequence[EnrichmentAction], root: Path) -> dict[str, object]:
    """构造歌词和封面补齐结果报告。"""

    result: dict[str, object] = {}
    for kind in ("lyrics", "artwork"):
        kind_actions = [action for action in actions if action.kind == kind]
        result[kind] = {
            "created": sum(action.status == "created" for action in kind_actions),
            "skipped_exists": sum(action.status == "skipped_exists" for action in kind_actions),
            "not_found": sum(action.status == "not_found" for action in kind_actions),
            "skipped_metadata": sum(action.status == "skipped_metadata" for action in kind_actions),
            "failed": sum(action.status == "failed" for action in kind_actions),
            "actions": [action.to_report(root) for action in kind_actions],
        }
    return result
