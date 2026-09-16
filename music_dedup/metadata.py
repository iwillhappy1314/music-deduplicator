"""扫描和编辑音频文件中的 Title、Artist 与 Album 元数据。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping

try:
    import mutagen
except ImportError:  # pragma: no cover - Docker installs the dependency.
    mutagen = None

from .core import (
    SUPPORTED_AUDIO_SUFFIXES,
    _find_tag,
    extract_webm_filename_title,
    iter_audio_files,
    normalize_tag_value,
)


class MetadataReadError(Exception):
    """表示音频的元数据无法通过可用读取器解析。"""


class MetadataWriteError(Exception):
    """表示音频元数据无法安全保存。"""


@dataclass(frozen=True)
class MetadataIssue:
    """保存一个存在空元数据字段的音频及其可编辑内容。"""

    path: Path
    title: str
    artist: str
    album: str
    missing_fields: tuple[str, ...]
    size_bytes: int | None
    modified_ns: int | None
    editable: bool
    error: str | None = None

    def to_report(self, root: Path) -> dict[str, object]:
        """将元数据问题转换为 Web API 和报告使用的相对路径结构。"""

        return {
            "path": str(self.path),
            "relative_path": str(self.path.relative_to(root)),
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "missing_fields": list(self.missing_fields),
            "size_bytes": self.size_bytes,
            "modified_ns": str(self.modified_ns) if self.modified_ns is not None else None,
            "editable": self.editable,
            "error": self.error,
        }


def _read_with_mutagen(path: Path) -> tuple[str, str, str]:
    """使用 Mutagen 读取三个可编辑字段。"""

    if mutagen is None:
        raise MetadataReadError("未安装 Mutagen 依赖")
    try:
        audio = mutagen.File(path, easy=True)
    except Exception as error:  # Mutagen exposes format-specific exceptions.
        raise MetadataReadError(f"Mutagen 读取失败: {error}") from error
    if audio is None:
        raise MetadataReadError("Mutagen 不支持此音频容器")
    tags = audio.tags or {}
    return (
        _find_tag(tags, ("title", "TIT2", "©nam", "TITLE")),
        _find_tag(tags, ("artist", "TPE1", "©ART", "ARTIST")),
        _find_tag(tags, ("album", "TALB", "©alb", "ALBUM")),
    )


def _read_with_ffprobe(path: Path) -> tuple[str, str, str]:
    """使用 ffprobe 读取 Mutagen 无法处理的容器元数据。"""

    command = shutil.which("ffprobe")
    if command is None:
        raise MetadataReadError("未找到 ffprobe 后备读取器")
    try:
        completed = subprocess.run(
            [
                command,
                "-v",
                "error",
                "-show_entries",
                "format_tags=title,artist,album",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        payload = json.loads(completed.stdout or "{}")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as error:
        raise MetadataReadError(f"ffprobe 读取失败: {error}") from error
    tags = payload.get("format", {}).get("tags", {})
    if not isinstance(tags, dict):
        return "", "", ""
    return (
        _find_tag(tags, ("title", "TITLE")),
        _find_tag(tags, ("artist", "ARTIST")),
        _find_tag(tags, ("album", "ALBUM")),
    )


def read_metadata_fields(path: Path) -> tuple[str, str, str]:
    """读取 Title、Artist、Album，并在必要时使用 ffprobe 补齐字段。"""

    mutagen_error: MetadataReadError | None = None
    try:
        title, artist, album = _read_with_mutagen(path)
    except MetadataReadError as error:
        mutagen_error = error
        title, artist, album = "", "", ""

    ffprobe_error: MetadataReadError | None = None
    if not title or not artist or not album:
        try:
            fallback_title, fallback_artist, fallback_album = _read_with_ffprobe(path)
        except MetadataReadError as error:
            ffprobe_error = error
            fallback_title, fallback_artist, fallback_album = "", "", ""
        title = title or fallback_title
        artist = artist or fallback_artist
        album = album or fallback_album

    if not title and not artist and not album and mutagen_error and ffprobe_error:
        raise MetadataReadError(f"{mutagen_error}; {ffprobe_error}")
    return title, artist, album


def _fallback_artist(path: Path, root: Path, artist_map: Mapping[str, str]) -> str:
    """按相对目录取得 WebM 的明确 Artist 回退值。"""

    relative_directory = path.parent.relative_to(root).as_posix()
    if relative_directory == ".":
        relative_directory = ""
    return normalize_tag_value(artist_map.get(relative_directory, ""))


def _editable_values(
    path: Path,
    root: Path,
    artist_map: Mapping[str, str],
) -> tuple[str, str, str, tuple[str, ...], str | None, bool]:
    """读取显示值、原始空字段和读取状态。"""

    fallback_title = extract_webm_filename_title(path) if path.suffix.casefold() == ".webm" else ""
    fallback_artist = (
        _fallback_artist(path, root, artist_map)
        if path.suffix.casefold() == ".webm"
        else ""
    )
    try:
        raw_title, raw_artist, raw_album = read_metadata_fields(path)
    except MetadataReadError as error:
        raw_title, raw_artist, raw_album = "", "", ""
        return (
            fallback_title,
            fallback_artist,
            "",
            ("title", "artist", "album"),
            str(error),
            False,
        )

    missing_fields = tuple(
        field
        for field, value in (
            ("title", raw_title),
            ("artist", raw_artist),
            ("album", raw_album),
        )
        if not value
    )
    return (
        raw_title or fallback_title,
        raw_artist or fallback_artist,
        raw_album,
        missing_fields,
        None,
        True,
    )


def inspect_metadata_file(
    path: Path,
    root: Path,
    artist_map: Mapping[str, str] | None = None,
) -> MetadataIssue | None:
    """检查单个音频，只有存在空字段或读取问题时才返回结果。"""

    artist_map = artist_map or {}
    try:
        stat = path.stat()
    except OSError as error:
        return MetadataIssue(path, "", "", "", ("title", "artist", "album"), None, None, False, str(error))
    title, artist, album, missing_fields, error, editable = _editable_values(path, root, artist_map)
    if not missing_fields and error is None:
        return None
    return MetadataIssue(
        path,
        title,
        artist,
        album,
        missing_fields,
        stat.st_size,
        stat.st_mtime_ns,
        editable,
        error,
    )


def scan_metadata_issues(
    root: Path,
    artist_map: Mapping[str, str] | None = None,
) -> tuple[MetadataIssue, ...]:
    """扫描音乐库并列出 Album、Artist、Title 任一为空的音频。"""

    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"音乐库目录不存在或不可访问: {root}")
    artist_map = artist_map or {}
    issues: list[MetadataIssue] = []
    for path in iter_audio_files(root):
        issue = inspect_metadata_file(path, root, artist_map)
        if issue is not None:
            issues.append(issue)
    return tuple(sorted(issues, key=lambda issue: issue.path.relative_to(root).as_posix().casefold()))


def resolve_metadata_path(root: Path, relative_path: str) -> Path:
    """把 API 提供的相对路径解析为音乐库内的真实音频，并拒绝越界路径。"""

    normalized_path = relative_path.strip().replace("\\", "/")
    relative = PurePosixPath(normalized_path)
    if (
        not normalized_path
        or relative.is_absolute()
        or ".." in relative.parts
        or relative == PurePosixPath(".")
    ):
        raise ValueError("音频路径必须是音乐库内的相对路径")
    root = root.expanduser().resolve()
    candidate = root.joinpath(*relative.parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise ValueError("音频文件不存在或不是普通文件")
    resolved = candidate.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError("音频路径必须位于音乐库目录内")
    if resolved.suffix.casefold() not in SUPPORTED_AUDIO_SUFFIXES:
        raise ValueError("文件格式不是支持的音频格式")
    return resolved


def _set_easy_tag(tags: object, key: str, value: str) -> None:
    """设置或删除一个 Mutagen EasyID3 风格的标签字段。"""

    if value:
        tags[key] = [value]  # type: ignore[index]
        return
    try:
        del tags[key]  # type: ignore[index]
    except KeyError:
        pass


def _write_with_mutagen(path: Path, title: str, artist: str, album: str) -> None:
    """使用 Mutagen 保存常见音频格式的标签。"""

    if mutagen is None:
        raise MetadataWriteError("未安装 Mutagen 依赖")
    try:
        audio = mutagen.File(path, easy=True)
        if audio is None:
            raise MetadataWriteError("Mutagen 不支持此音频容器")
        if audio.tags is None:
            audio.add_tags()
        _set_easy_tag(audio.tags, "title", title)
        _set_easy_tag(audio.tags, "artist", artist)
        _set_easy_tag(audio.tags, "album", album)
        audio.save()
    except MetadataWriteError:
        raise
    except Exception as error:  # Mutagen exposes format-specific write errors.
        raise MetadataWriteError(f"Mutagen 保存失败: {error}") from error


def _write_with_ffmpeg(path: Path, title: str, artist: str, album: str) -> None:
    """使用无损流复制为 Mutagen 不支持的容器写入标签。"""

    command = shutil.which("ffmpeg")
    if command is None:
        raise MetadataWriteError("未找到 ffmpeg 后备写入器")
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=path.suffix,
            delete=False,
        ) as temporary_file:
            temporary_path = temporary_file.name
        completed = subprocess.run(
            [
                command,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(path),
                "-map",
                "0",
                "-c",
                "copy",
                "-metadata",
                f"title={title}",
                "-metadata",
                f"artist={artist}",
                "-metadata",
                f"album={album}",
                temporary_path,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=300,
        )
        del completed
        os.replace(temporary_path, path)
        temporary_path = None
    except (OSError, subprocess.SubprocessError) as error:
        detail = getattr(error, "stderr", "") or str(error)
        raise MetadataWriteError(f"ffmpeg 保存失败: {detail.strip()}") from error
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)


def write_audio_metadata(path: Path, title: str, artist: str, album: str) -> None:
    """保存三个元数据字段，优先使用 Mutagen，失败时使用 ffmpeg 无损复制。"""

    values = {"title": title, "artist": artist, "album": album}
    for field, value in values.items():
        if "\x00" in value:
            raise MetadataWriteError(f"{field} 不能包含 NUL 字符")
        if len(value) > 1000:
            raise MetadataWriteError(f"{field} 长度不能超过 1000 个字符")
    if path.suffix.casefold() not in SUPPORTED_AUDIO_SUFFIXES:
        raise MetadataWriteError("文件格式不是支持的音频格式")
    try:
        _write_with_mutagen(path, title, artist, album)
    except MetadataWriteError as mutagen_error:
        try:
            _write_with_ffmpeg(path, title, artist, album)
        except MetadataWriteError as ffmpeg_error:
            raise MetadataWriteError(f"{mutagen_error}; {ffmpeg_error}") from ffmpeg_error
