"""音乐文件扫描、元数据读取和安全隔离逻辑。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from pathlib import PurePosixPath
from typing import Iterator, Mapping, Sequence

try:
    import fcntl
except ImportError:  # pragma: no cover - Synology/Linux and macOS both provide fcntl.
    fcntl = None

try:
    import mutagen
except ImportError:  # pragma: no cover - exercised by the dependency check, not normal runs.
    mutagen = None


SUPPORTED_AUDIO_SUFFIXES = frozenset(
    {
        ".aif",
        ".aiff",
        ".ape",
        ".dsf",
        ".dff",
        ".flac",
        ".m4a",
        ".mka",
        ".mp3",
        ".mp4",
        ".oga",
        ".ogg",
        ".opus",
        ".wav",
        ".webm",
        ".wv",
    }
)


class MetadataError(Exception):
    """表示音频文件无法读取或不包含所需标签。"""


class MissingMetadataError(MetadataError):
    """表示音频缺少歌曲名或演唱者标签，因此不能安全匹配。"""


@dataclass(frozen=True)
class AudioRecord:
    """保存一个可参与去重的音频文件及其稳定文件特征。"""

    path: Path
    title: str
    artist: str
    normalized_title: str
    normalized_artist: str
    size: int
    modified_ns: int

    @property
    def match_key(self) -> tuple[str, str]:
        """返回用于判断重复的规范化歌曲名和演唱者。"""

        return self.normalized_title, self.normalized_artist

    @property
    def format_rank(self) -> int:
        """返回格式优先级，其中 FLAC 高于其他格式。"""

        return int(self.path.suffix.casefold() == ".flac")

    @property
    def preference_key(self) -> tuple[int, int, str]:
        """返回保留文件的排序键，数值越优先越靠前。"""

        return -self.format_rank, -self.size, self.path.as_posix().casefold()

    def to_report(self, root: Path) -> dict[str, object]:
        """把文件信息转换为相对于音乐库根目录的报告结构。"""

        return {
            "path": str(self.path),
            "relative_path": str(self.path.relative_to(root)),
            "title": self.title,
            "artist": self.artist,
            "format": self.path.suffix.lower().lstrip("."),
            "size_bytes": self.size,
        }


@dataclass(frozen=True)
class ScanIssue:
    """保存扫描时被跳过的文件及原因。"""

    path: Path
    reason: str
    severity: str

    def to_report(self) -> dict[str, str]:
        """把扫描问题转换为 JSON 报告结构。"""

        return {
            "path": str(self.path),
            "reason": self.reason,
            "severity": self.severity,
        }


@dataclass(frozen=True)
class DuplicateGroup:
    """保存一个重复标签组、应保留的文件和待隔离文件。"""

    title: str
    artist: str
    keeper: AudioRecord
    duplicates: tuple[AudioRecord, ...]

    @property
    def duplicate_count(self) -> int:
        """返回本组需要处理的重复文件数量。"""

        return len(self.duplicates)


@dataclass(frozen=True)
class MoveAction:
    """保存一次重复文件移动动作及其执行结果。"""

    source_path: Path
    quarantine_path: Path | None
    status: str
    error: str | None = None

    def to_report(self) -> dict[str, str | None]:
        """把移动动作转换为 JSON 报告结构。"""

        return {
            "source_path": str(self.source_path),
            "quarantine_path": (
                str(self.quarantine_path) if self.quarantine_path is not None else None
            ),
            "status": self.status,
            "error": self.error,
        }


@dataclass(frozen=True)
class ScanResult:
    """保存一次完整扫描的可用文件、跳过项和重复组。"""

    root: Path
    records: tuple[AudioRecord, ...]
    issues: tuple[ScanIssue, ...]


def normalize_tag_value(value: object) -> str:
    """把 Mutagen 标签值安全转换为去除首尾空白的字符串。"""

    if isinstance(value, (list, tuple)):
        for item in value:
            text = normalize_tag_value(item)
            if text:
                return text
        return ""
    if value is None:
        return ""
    return str(value).strip()


def normalize_for_match(value: str) -> str:
    """规范化歌曲名或演唱者以进行保守、大小写不敏感的匹配。"""

    normalized = unicodedata.normalize("NFKC", value)
    normalized = " ".join(normalized.split())
    return normalized.casefold()


def _find_tag(tags: Mapping[object, object], aliases: Sequence[str]) -> str:
    """在不同音频格式的标签命名方式中查找第一个非空值。"""

    aliases_casefolded = {alias.casefold() for alias in aliases}
    for key, value in tags.items():
        key_text = str(key).casefold()
        if key_text in aliases_casefolded:
            result = normalize_tag_value(value)
            if result:
                return result
    return ""


def _read_with_mutagen(path: Path) -> tuple[str, str]:
    """使用 Mutagen 读取常见音频格式的歌曲名和演唱者。"""

    if mutagen is None:
        raise MetadataError("未安装 Mutagen 依赖")

    try:
        audio = mutagen.File(path, easy=True)
    except Exception as error:  # Mutagen exposes format-specific exception classes.
        raise MetadataError(f"Mutagen 读取失败: {error}") from error

    if audio is None:
        raise MetadataError("Mutagen 不支持此音频容器")

    tags = audio.tags or {}
    title = _find_tag(tags, ("title", "TIT2", "©nam", "TITLE"))
    artist = _find_tag(tags, ("artist", "TPE1", "©ART", "ARTIST"))
    return title, artist


def _read_with_ffprobe(path: Path) -> tuple[str, str]:
    """使用 ffprobe 作为 Mutagen 不支持容器时的元数据后备读取器。"""

    command = shutil.which("ffprobe")
    if command is None:
        raise MetadataError("未找到 ffprobe 后备读取器")

    try:
        completed = subprocess.run(
            [
                command,
                "-v",
                "error",
                "-show_entries",
                "format_tags=title,artist",
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
        raise MetadataError(f"ffprobe 读取失败: {error}") from error

    tags = payload.get("format", {}).get("tags", {})
    if not isinstance(tags, dict):
        return "", ""
    return _find_tag(tags, ("title", "TITLE")), _find_tag(tags, ("artist", "ARTIST"))


def _read_audio_tags(path: Path) -> tuple[str, str]:
    """读取音频内嵌的歌曲名和演唱者，并保留可能缺失的单个字段。"""

    mutagen_error: MetadataError | None = None
    try:
        title, artist = _read_with_mutagen(path)
    except MetadataError as error:
        mutagen_error = error
        title, artist = "", ""

    ffprobe_error: MetadataError | None = None
    if not title or not artist:
        try:
            fallback_title, fallback_artist = _read_with_ffprobe(path)
        except MetadataError as error:
            ffprobe_error = error
            fallback_title, fallback_artist = "", ""
        title = title or fallback_title
        artist = artist or fallback_artist

    if not title or not artist:
        if mutagen_error is not None and ffprobe_error is not None:
            raise MetadataError(f"{mutagen_error}; {ffprobe_error}")
    return title, artist


def read_audio_metadata(
    path: Path,
    fallback_title: str = "",
    fallback_artist: str = "",
) -> tuple[str, str]:
    """读取并验证音频标签，必要时使用调用方提供的保守回退值。"""

    title, artist = _read_audio_tags(path)
    title = title or normalize_tag_value(fallback_title)
    artist = artist or normalize_tag_value(fallback_artist)
    if not title or not artist:
        raise MissingMetadataError("缺少 title 或 artist 标签")
    return title, artist


def extract_webm_filename_title(path: Path) -> str:
    """从 WebM 文件名提取歌曲名并移除 yt-dlp 格式后缀如 `.f248`。"""

    title = path.stem
    title = re.sub(r"\.f\d{3,4}$", "", title, flags=re.IGNORECASE)
    return title.strip()


def _normalize_artist_map_key(value: str) -> str:
    """规范化 Artist 映射中的相对目录键，并拒绝越界路径。"""

    value = value.strip().replace("\\", "/")
    if value in {"", "."}:
        return ""
    relative_path = PurePosixPath(value)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError(f"Artist 映射目录必须是音乐库内的相对路径: {value}")
    return relative_path.as_posix()


def load_artist_map(map_path: Path | None) -> dict[str, str]:
    """读取相对目录到 Artist 的 JSON 映射，文件不存在时返回空映射。"""

    if map_path is None:
        return {}
    map_path = map_path.expanduser().resolve()
    if not map_path.exists():
        return {}
    if not map_path.is_file():
        raise ValueError(f"Artist 映射路径不是文件: {map_path}")
    try:
        payload = json.loads(map_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"Artist 映射 JSON 无法读取: {map_path}: {error}") from error
    if not isinstance(payload, dict):
        raise ValueError("Artist 映射 JSON 必须是“相对目录”: “Artist”的对象")

    artist_map: dict[str, str] = {}
    for directory, artist in payload.items():
        if not isinstance(directory, str) or not isinstance(artist, str):
            raise ValueError("Artist 映射的目录和 Artist 都必须是字符串")
        directory_key = _normalize_artist_map_key(directory)
        artist_value = normalize_tag_value(artist)
        if not artist_value:
            raise ValueError(f"Artist 映射不能为空: {directory}")
        artist_map[directory_key] = artist_value
    return artist_map


def _artist_for_directory(path: Path, root: Path, artist_map: Mapping[str, str]) -> str:
    """根据文件所在目录的相对路径查找明确配置的 Artist。"""

    relative_directory = path.parent.relative_to(root).as_posix()
    if relative_directory == ".":
        relative_directory = ""
    return artist_map.get(relative_directory, "")


def _is_hidden_relative_path(path: Path, root: Path) -> bool:
    """判断相对音乐库根目录的路径是否属于隐藏系统文件。"""

    relative_path = path.relative_to(root)
    return any(part.startswith(".") for part in relative_path.parts)


def _raise_walk_error(error: OSError) -> None:
    """让目录读取权限错误中止扫描，避免在不完整扫描上执行移动。"""

    raise error


def iter_audio_files(root: Path) -> Iterator[Path]:
    """以不跟随符号链接的方式递归枚举支持的音频文件。"""

    for current_directory, directory_names, file_names in os.walk(
        root,
        topdown=True,
        onerror=_raise_walk_error,
        followlinks=False,
    ):
        current_path = Path(current_directory)
        directory_names[:] = [
            name for name in directory_names if not name.startswith(".")
        ]
        for file_name in file_names:
            path = current_path / file_name
            if file_name.startswith(".") or _is_hidden_relative_path(path, root):
                continue
            if path.suffix.casefold() not in SUPPORTED_AUDIO_SUFFIXES:
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    continue
            except OSError:
                continue
            yield path


def scan_library(
    root: Path,
    artist_map: Mapping[str, str] | None = None,
) -> ScanResult:
    """扫描音乐库并读取所有可安全参与匹配的音频标签。"""

    root = root.expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"音乐库目录不存在或不可访问: {root}")

    artist_map = artist_map or {}
    records: list[AudioRecord] = []
    issues: list[ScanIssue] = []
    for path in iter_audio_files(root):
        try:
            if path.suffix.casefold() == ".webm":
                title, artist = read_audio_metadata(
                    path,
                    fallback_title=extract_webm_filename_title(path),
                    fallback_artist=_artist_for_directory(path, root, artist_map),
                )
            else:
                title, artist = read_audio_metadata(path)
            stat = path.stat()
        except MissingMetadataError as error:
            issues.append(ScanIssue(path, str(error), "info"))
            continue
        except (MetadataError, OSError) as error:
            issues.append(ScanIssue(path, str(error), "error"))
            continue

        records.append(
            AudioRecord(
                path=path,
                title=title,
                artist=artist,
                normalized_title=normalize_for_match(title),
                normalized_artist=normalize_for_match(artist),
                size=stat.st_size,
                modified_ns=stat.st_mtime_ns,
            )
        )

    return ScanResult(root, tuple(records), tuple(issues))


def find_duplicate_groups(records: Sequence[AudioRecord]) -> tuple[DuplicateGroup, ...]:
    """按规范化歌曲名和演唱者分组，并为每组选择唯一保留文件。"""

    grouped_records: defaultdict[tuple[str, str], list[AudioRecord]] = defaultdict(list)
    for record in records:
        grouped_records[record.match_key].append(record)

    duplicate_groups: list[DuplicateGroup] = []
    for group in grouped_records.values():
        if len(group) < 2:
            continue
        ordered = sorted(group, key=lambda item: item.preference_key)
        keeper = ordered[0]
        duplicate_groups.append(
            DuplicateGroup(
                title=keeper.title,
                artist=keeper.artist,
                keeper=keeper,
                duplicates=tuple(ordered[1:]),
            )
        )

    return tuple(
        sorted(
            duplicate_groups,
            key=lambda group: (group.title.casefold(), group.artist.casefold()),
        )
    )


def _is_relative_to(path: Path, parent: Path) -> bool:
    """判断路径是否等于或位于指定父目录内。"""

    return path == parent or parent in path.parents


def _destination_for(source: Path, root: Path, quarantine_root: Path) -> Path:
    """根据源文件相对路径生成隔离目标路径，并避免覆盖现有文件。"""

    relative_path = source.relative_to(root)
    destination = quarantine_root / relative_path
    digest = hashlib.sha256(str(relative_path).encode("utf-8")).hexdigest()[:12]
    candidate = destination
    suffix_index = 0
    while os.path.lexists(candidate):
        suffix_index += 1
        suffix = f".duplicate-{digest}"
        if suffix_index > 1:
            suffix = f"{suffix}-{suffix_index}"
        candidate = destination.with_name(
            f"{destination.stem}{suffix}{destination.suffix}"
        )
    return candidate


def _record_is_unchanged(record: AudioRecord) -> bool:
    """确认 apply 前源文件仍与扫描时的大小和修改时间一致。"""

    try:
        if record.path.is_symlink():
            return False
        stat = record.path.stat()
    except OSError:
        return False
    return stat.st_size == record.size and stat.st_mtime_ns == record.modified_ns


def apply_duplicates(
    scan: ScanResult,
    duplicate_groups: Sequence[DuplicateGroup],
    quarantine_root: Path,
) -> tuple[MoveAction, ...]:
    """把扫描确认的重复文件安全移动到音乐库外的隔离目录。"""

    quarantine_root = quarantine_root.expanduser().resolve()
    if _is_relative_to(quarantine_root, scan.root):
        raise ValueError("隔离目录必须位于音乐库根目录之外")
    quarantine_root.mkdir(parents=True, exist_ok=True)

    actions: list[MoveAction] = []
    for group in duplicate_groups:
        for duplicate in group.duplicates:
            destination = _destination_for(duplicate.path, scan.root, quarantine_root)
            if not _record_is_unchanged(duplicate):
                actions.append(
                    MoveAction(
                        duplicate.path,
                        destination,
                        "skipped_changed",
                        "文件在扫描后发生变化或已不存在",
                    )
                )
                continue
            try:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(duplicate.path), str(destination))
                if duplicate.path.exists() or not destination.exists():
                    raise OSError("移动后文件状态校验失败")
            except OSError as error:
                actions.append(
                    MoveAction(duplicate.path, destination, "failed", str(error))
                )
            else:
                actions.append(MoveAction(duplicate.path, destination, "moved"))
    return tuple(actions)


@contextmanager
def process_lock(lock_path: Path) -> Iterator[None]:
    """创建独占锁，避免 DSM 定时任务与手动扫描同时处理音乐库。"""

    if fcntl is None:
        yield
        return

    lock_path = lock_path.expanduser()
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w", encoding="utf-8") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("已有另一个去重任务正在运行") from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _record_for_report(record: AudioRecord, root: Path) -> dict[str, object]:
    """生成重复组中单个文件的报告信息。"""

    return record.to_report(root)


def build_report(
    scan: ScanResult,
    duplicate_groups: Sequence[DuplicateGroup],
    actions: Sequence[MoveAction],
    apply_mode: bool,
    quarantine_root: Path | None = None,
    apply_blocked: bool = False,
) -> dict[str, object]:
    """构造完整的机器可读扫描和处理报告。"""

    action_by_source = {str(action.source_path): action for action in actions}
    resolved_quarantine_root = (
        quarantine_root.expanduser().resolve() if quarantine_root is not None else None
    )
    groups: list[dict[str, object]] = []
    for group in duplicate_groups:
        duplicate_reports: list[dict[str, object]] = []
        for duplicate in group.duplicates:
            action = action_by_source.get(str(duplicate.path))
            if action is None:
                planned_path = (
                    _destination_for(duplicate.path, scan.root, resolved_quarantine_root)
                    if resolved_quarantine_root is not None
                    else None
                )
                action = MoveAction(duplicate.path, planned_path, "planned")
            duplicate_reports.append(
                {
                    **_record_for_report(duplicate, scan.root),
                    "action": action.to_report(),
                }
            )
        groups.append(
            {
                "title": group.title,
                "artist": group.artist,
                "keeper": _record_for_report(group.keeper, scan.root),
                "duplicates": duplicate_reports,
            }
        )

    moved_count = sum(action.status == "moved" for action in actions)
    failed_count = sum(action.status in {"failed", "skipped_changed"} for action in actions)
    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "mode": (
            "apply_blocked"
            if apply_blocked
            else "apply"
            if apply_mode
            else "dry_run"
        ),
        "root": str(scan.root),
        "summary": {
            "audio_files_read": len(scan.records),
            "issues": len(scan.issues),
            "duplicate_groups": len(duplicate_groups),
            "duplicate_files": sum(group.duplicate_count for group in duplicate_groups),
            "moved_files": moved_count,
            "failed_actions": failed_count,
        },
        "duplicate_groups": groups,
        "issues": [issue.to_report() for issue in scan.issues],
        "actions": [action.to_report() for action in actions],
    }


def write_report(report: Mapping[str, object], report_path: Path) -> None:
    """以原子替换方式写出 JSON 报告，避免任务中断留下半个文件。"""

    report_path = report_path.expanduser().resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=report_path.parent,
            prefix=f".{report_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = temporary_file.name
            json.dump(report, temporary_file, ensure_ascii=False, indent=2)
            temporary_file.write("\n")
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, report_path)
    finally:
        if temporary_path is not None and os.path.exists(temporary_path):
            os.unlink(temporary_path)
