"""音乐去重核心逻辑的确定性测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from music_dedup.core import (
    MissingMetadataError,
    apply_duplicates,
    build_report,
    extract_webm_filename_title,
    find_duplicate_groups,
    load_artist_map,
    normalize_artist_for_match,
    normalize_for_match,
    read_audio_metadata,
    scan_library,
)


class DeduplicationTestCase(unittest.TestCase):
    """验证去重规则、隔离动作和安全跳过行为。"""

    def _create_file(self, root: Path, relative_path: str, size: int) -> Path:
        """创建指定大小的测试音频占位文件。"""

        path = root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path.resolve()

    def test_normalization_is_case_and_whitespace_insensitive(self) -> None:
        """歌曲名匹配应统一兼容字符、大小写和多余空白。"""

        self.assertEqual(normalize_for_match("  Ｓｏｎｇ　Name  "), "song name")

    def test_artist_normalization_converts_traditional_to_simplified(self) -> None:
        """Artist 比较键应把繁体中文转换为简体中文。"""

        self.assertEqual(
            normalize_artist_for_match("張學友"),
            normalize_for_match("张学友"),
        )

    def test_traditional_and_simplified_artists_are_duplicate_group(self) -> None:
        """繁体和简体 Artist 相同的歌曲应进入同一重复组。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            files = {
                self._create_file(root, "traditional.mp3", 10): ("歌曲", "張學友"),
                self._create_file(root, "simplified.mp3", 20): ("歌曲", "张学友"),
            }
            with patch(
                "music_dedup.core.read_audio_metadata",
                side_effect=lambda path: files[path.resolve()],
            ):
                scan = scan_library(root)

            groups = find_duplicate_groups(scan.records)

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].duplicate_count, 1)

    def test_webm_filename_title_removes_download_format_suffix(self) -> None:
        """WebM Title 回退应去除格式码而保留歌曲名中的普通句点。"""

        self.assertEqual(
            extract_webm_filename_title(Path("我是真的愛你.f248.webm")),
            "我是真的愛你",
        )
        self.assertEqual(
            extract_webm_filename_title(Path("Mr. Brightside.webm")),
            "Mr. Brightside",
        )

    def test_webm_metadata_can_use_explicit_artist_fallback(self) -> None:
        """内嵌标签缺失时应使用调用方明确提供的 WebM 回退值。"""

        with patch(
            "music_dedup.core._read_audio_tags",
            return_value=("", ""),
        ):
            metadata = read_audio_metadata(
                Path("song.webm"),
                fallback_title="Song",
                fallback_artist="Artist",
            )

        self.assertEqual(metadata, ("Song", "Artist"))

    def test_artist_map_uses_relative_directory_keys(self) -> None:
        """Artist 映射应读取相对于音乐库根目录的目录键。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            map_path = Path(temporary_directory) / "artist-map.json"
            map_path.write_text(
                '{"Album - Live": "Artist"}',
                encoding="utf-8",
            )

            artist_map = load_artist_map(map_path)

        self.assertEqual(artist_map, {"Album - Live": "Artist"})

    def test_flac_is_preferred_then_largest_flac(self) -> None:
        """存在多个 FLAC 时应保留最大的 FLAC，而不是更大的 MP3。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            files = {
                self._create_file(root, "song.mp3", 100): ("Song", "Artist"),
                self._create_file(root, "song-small.flac", 10): ("Song", "Artist"),
                self._create_file(root, "song-large.flac", 20): ("Song", "Artist"),
            }
            with patch(
                "music_dedup.core.read_audio_metadata",
                side_effect=lambda path: files[path.resolve()],
            ):
                scan = scan_library(root)

            groups = find_duplicate_groups(scan.records)
            self.assertEqual(groups[0].keeper.path.name, "song-large.flac")
            self.assertEqual(groups[0].duplicate_count, 2)

    def test_largest_non_flac_is_preferred(self) -> None:
        """没有 FLAC 时应保留最大的其他格式文件。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            files = {
                self._create_file(root, "small.mp3", 10): ("Song", "Artist"),
                self._create_file(root, "large.m4a", 20): ("Song", "Artist"),
            }
            with patch(
                "music_dedup.core.read_audio_metadata",
                side_effect=lambda path: files[path.resolve()],
            ):
                scan = scan_library(root)

            groups = find_duplicate_groups(scan.records)
            self.assertEqual(groups[0].keeper.path.name, "large.m4a")

    def test_same_title_with_different_artists_is_retained(self) -> None:
        """歌曲名相同但演唱者不同的文件不能进入同一重复组。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            files = {
                self._create_file(root, "one.mp3", 10): ("Song", "Artist One"),
                self._create_file(root, "two.mp3", 20): ("Song", "Artist Two"),
            }
            with patch(
                "music_dedup.core.read_audio_metadata",
                side_effect=lambda path: files[path.resolve()],
            ):
                scan = scan_library(root)

            self.assertEqual(find_duplicate_groups(scan.records), ())

    def test_missing_metadata_is_skipped(self) -> None:
        """缺少关键标签时应跳过文件而不是根据文件名猜测。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            path = self._create_file(root, "artist - song.mp3", 10)
            with patch(
                "music_dedup.core.read_audio_metadata",
                side_effect=MissingMetadataError("缺少 title 或 artist 标签"),
            ):
                scan = scan_library(root)

            self.assertEqual(scan.records, ())
            self.assertEqual(scan.issues[0].severity, "info")

    def test_apply_moves_duplicate_and_keeps_original_keeper(self) -> None:
        """apply 应移动重复文件到隔离目录且保留选中的原文件。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "music"
            quarantine = base / "quarantine"
            keeper = self._create_file(root, "album/song.flac", 20)
            duplicate = self._create_file(root, "other/song.mp3", 10)
            files = {
                keeper: ("Song", "Artist"),
                duplicate: ("Song", "Artist"),
            }
            with patch(
                "music_dedup.core.read_audio_metadata",
                side_effect=lambda path: files[path.resolve()],
            ):
                scan = scan_library(root)
                groups = find_duplicate_groups(scan.records)
                actions = apply_duplicates(scan, groups, quarantine)

            self.assertEqual(actions[0].status, "moved")
            self.assertTrue(keeper.exists())
            self.assertFalse(duplicate.exists())
            self.assertTrue((quarantine / "other/song.mp3").exists())

    def test_apply_skips_file_changed_after_scan(self) -> None:
        """扫描后文件发生变化时不能继续移动旧的决策。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "music"
            quarantine = base / "quarantine"
            keeper = self._create_file(root, "song.flac", 20)
            duplicate = self._create_file(root, "song.mp3", 10)
            files = {
                keeper: ("Song", "Artist"),
                duplicate: ("Song", "Artist"),
            }
            with patch(
                "music_dedup.core.read_audio_metadata",
                side_effect=lambda path: files[path.resolve()],
            ):
                scan = scan_library(root)
                groups = find_duplicate_groups(scan.records)
                duplicate.write_bytes(b"changed-after-scan")
                actions = apply_duplicates(scan, groups, quarantine)

            self.assertEqual(actions[0].status, "skipped_changed")
            self.assertTrue(duplicate.exists())

    def test_dry_run_report_contains_planned_quarantine_path(self) -> None:
        """dry-run 报告应展示计划路径，而不是把源路径伪装成目标路径。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            base = Path(temporary_directory)
            root = base / "music"
            quarantine = base / "quarantine"
            keeper = self._create_file(root, "song.flac", 20)
            duplicate = self._create_file(root, "song.mp3", 10)
            files = {
                keeper: ("Song", "Artist"),
                duplicate: ("Song", "Artist"),
            }
            with patch(
                "music_dedup.core.read_audio_metadata",
                side_effect=lambda path: files[path.resolve()],
            ):
                scan = scan_library(root)
            groups = find_duplicate_groups(scan.records)

            report = build_report(scan, groups, (), False, quarantine)

            action = report["duplicate_groups"][0]["duplicates"][0]["action"]
            self.assertEqual(action["status"], "planned")
            self.assertEqual(
                action["quarantine_path"], str(quarantine.resolve() / "song.mp3")
            )


if __name__ == "__main__":
    unittest.main()
