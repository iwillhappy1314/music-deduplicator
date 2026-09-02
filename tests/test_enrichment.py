"""外挂歌词和专辑封面获取逻辑的确定性测试。"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from music_dedup.core import AudioRecord, normalize_artist_for_match, normalize_for_match
from music_dedup.enrichment import fetch_album_artwork, fetch_lyrics


class FakeResponse:
    """提供 urlopen 所需最小响应接口的测试替身。"""

    def __init__(self, payload: bytes) -> None:
        """保存响应体。"""

        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        """进入响应上下文。"""

        return self

    def __exit__(self, *args: object) -> None:
        """离开响应上下文。"""

    def read(self, limit: int = -1) -> bytes:
        """返回测试响应体。"""

        return self.payload[:limit] if limit >= 0 else self.payload


class EnrichmentTestCase(unittest.TestCase):
    """验证歌词和封面只补缺失文件的行为。"""

    def _record(self, root: Path, name: str = "song.flac") -> AudioRecord:
        """创建一个带有匹配键的音频测试记录。"""

        path = root / name
        path.write_bytes(b"audio")
        return AudioRecord(
            path=path,
            title="歌曲",
            artist="張學友",
            normalized_title=normalize_for_match("歌曲"),
            normalized_artist=normalize_artist_for_match("張學友"),
            size=path.stat().st_size,
            modified_ns=path.stat().st_mtime_ns,
        )

    def test_fetch_lyrics_prefers_synced_lrc_and_skips_existing_sidecar(self) -> None:
        """歌词获取应优先保存同步 LRC，已有歌词时不覆盖。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            record = self._record(root)
            payload = {
                "trackName": "歌曲",
                "artistName": "张学友",
                "syncedLyrics": "[00:01.00]第一句",
                "plainLyrics": "第一句",
            }
            with patch(
                "music_dedup.enrichment.urlopen",
                return_value=FakeResponse(json.dumps(payload).encode("utf-8")),
            ):
                actions = fetch_lyrics((record,), request_delay=0)

            self.assertEqual(actions[0].status, "created")
            self.assertEqual((root / "song.lrc").read_text(encoding="utf-8"), "[00:01.00]第一句\n")

            (root / "song.lrc").write_text("existing\n", encoding="utf-8")
            with patch("music_dedup.enrichment.urlopen") as urlopen_mock:
                skipped = fetch_lyrics((record,), request_delay=0)

            urlopen_mock.assert_not_called()
            self.assertEqual(skipped[0].status, "skipped_exists")
            self.assertEqual((root / "song.lrc").read_text(encoding="utf-8"), "existing\n")

    def test_fetch_lyrics_uses_txt_for_plain_lyrics(self) -> None:
        """只有普通歌词时应保存为 TXT 外挂文件。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            record = self._record(root)
            payload = {
                "trackName": "歌曲",
                "artistName": "张学友",
                "syncedLyrics": "",
                "plainLyrics": "第一句\n第二句",
            }
            with patch(
                "music_dedup.enrichment.urlopen",
                return_value=FakeResponse(json.dumps(payload).encode("utf-8")),
            ):
                actions = fetch_lyrics((record,), request_delay=0)

            self.assertEqual(actions[0].status, "created")
            self.assertEqual((root / "song.txt").read_text(encoding="utf-8"), "第一句\n第二句\n")

    def test_fetch_lyrics_reuses_lookup_for_same_match_key(self) -> None:
        """相同 Artist 和 Title 的多个文件应复用一次网络查询但分别写入歌词。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            first = self._record(root, "first.flac")
            second = self._record(root, "second.flac")
            payload = {
                "trackName": "歌曲",
                "artistName": "张学友",
                "syncedLyrics": "[00:01.00]第一句",
            }
            with patch(
                "music_dedup.enrichment.urlopen",
                return_value=FakeResponse(json.dumps(payload).encode("utf-8")),
            ) as urlopen_mock:
                actions = fetch_lyrics((first, second), request_delay=0)

            self.assertEqual(urlopen_mock.call_count, 1)
            self.assertEqual([action.status for action in actions], ["created", "created"])
            self.assertTrue((root / "first.lrc").is_file())
            self.assertTrue((root / "second.lrc").is_file())

    def test_fetch_album_artwork_matches_album_and_writes_cover(self) -> None:
        """封面获取应按 Artist 和 Album 精确匹配并写入 cover 文件。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            record = self._record(root)
            payload = {
                "resultCount": 1,
                "results": [
                    {
                        "collectionName": "专辑",
                        "artistName": "张学友",
                        "artworkUrl100": "https://example.test/100x100.jpg",
                    }
                ],
            }
            image = b"\xff\xd8\xff\xe0fake-jpeg"
            with patch("music_dedup.enrichment.read_album_metadata", return_value="专辑"):
                with patch(
                    "music_dedup.enrichment.urlopen",
                    side_effect=[
                        FakeResponse(json.dumps(payload).encode("utf-8")),
                        FakeResponse(image),
                    ],
                ):
                    actions = fetch_album_artwork((record,), request_delay=0)

            self.assertEqual(actions[0].status, "created")
            self.assertEqual((root / "cover.jpg").read_bytes(), image)


if __name__ == "__main__":
    unittest.main()
