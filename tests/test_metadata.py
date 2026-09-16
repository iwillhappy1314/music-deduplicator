"""元数据问题扫描、保存和 Web 编辑流程测试。"""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path
from unittest.mock import patch

from music_dedup.metadata import resolve_metadata_path, scan_metadata_issues
from music_dedup.web import ControlHTTPServer, JobManager, WebConfig, _make_handler


class FakeAudio:
    """提供 Mutagen 音频对象所需最小接口的测试替身。"""

    def __init__(self) -> None:
        """初始化一个缺少 Artist 的音频标签集合。"""

        self.tags = {"title": ["歌曲"], "artist": [], "album": ["专辑"]}
        self.saved = False

    def add_tags(self) -> None:
        """提供空标签初始化接口。"""

        self.tags = {}

    def save(self) -> None:
        """记录标签保存动作。"""

        self.saved = True


class FakeMutagen:
    """返回固定测试音频对象的 Mutagen 模块替身。"""

    def __init__(self, audio: FakeAudio) -> None:
        """保存测试音频对象。"""

        self.audio = audio

    def File(self, path: Path, easy: bool = True) -> FakeAudio:
        """返回测试音频对象。"""

        return self.audio


class MetadataTestCase(unittest.TestCase):
    """验证空字段识别和手动保存流程。"""

    def test_scan_lists_empty_artist_and_resolves_only_library_paths(self) -> None:
        """扫描应列出空 Artist，并拒绝音乐库外路径。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            audio_path = root / "song.mp3"
            audio_path.write_bytes(b"audio")
            fake_audio = FakeAudio()
            with patch("music_dedup.metadata.mutagen", FakeMutagen(fake_audio)):
                issues = scan_metadata_issues(root)

            self.assertEqual(len(issues), 1)
            self.assertEqual(issues[0].missing_fields, ("artist",))
            self.assertEqual(issues[0].title, "歌曲")
            self.assertEqual(issues[0].album, "专辑")
            self.assertEqual(resolve_metadata_path(root, "song.mp3"), audio_path.resolve())
            with self.assertRaises(ValueError):
                resolve_metadata_path(root, "../song.mp3")

    def test_http_save_removes_fixed_issue_from_next_scan(self) -> None:
        """保存完整字段后，下一次扫描不应继续列出该歌曲。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            audio_path = root / "song.mp3"
            audio_path.write_bytes(b"audio")
            fake_audio = FakeAudio()
            config = WebConfig(
                root=str(root),
                quarantine=str(root / "quarantine"),
                report=str(root / "report.json"),
                artist_map=None,
                lock_file=str(root / "lock"),
                host="127.0.0.1",
                port=0,
                token="secret",
                log_path=str(root / "web.log"),
                run_on_start=False,
                request_delay=0,
                lyrics_api_url="https://lyrics.test/api/get",
                artwork_api_url="https://artwork.test/search",
            )
            manager = JobManager(config)
            server = ControlHTTPServer((config.host, config.port), _make_handler(manager))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            headers = {"X-Auth-Token": "secret"}
            try:
                with patch("music_dedup.metadata.mutagen", FakeMutagen(fake_audio)):
                    request = urllib.request.Request(
                        f"{base_url}/api/metadata/issues",
                        headers=headers,
                    )
                    with urllib.request.urlopen(request, timeout=2) as response:
                        issues = json.load(response)["issues"]
                    self.assertEqual(len(issues), 1)

                    payload = json.dumps(
                        {
                            "relative_path": "song.mp3",
                            "title": "歌曲",
                            "artist": "歌手",
                            "album": "专辑",
                            "size_bytes": issues[0]["size_bytes"],
                            "modified_ns": issues[0]["modified_ns"],
                        }
                    ).encode("utf-8")
                    save_request = urllib.request.Request(
                        f"{base_url}/api/metadata",
                        data=payload,
                        headers={**headers, "Content-Type": "application/json"},
                        method="PUT",
                    )
                    with urllib.request.urlopen(save_request, timeout=2) as response:
                        self.assertIsNone(json.load(response)["issue"])

                self.assertTrue(fake_audio.saved)
                self.assertEqual(fake_audio.tags["artist"], ["歌手"])
            finally:
                server.shutdown()
                thread.join(2)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
