"""命令行安全闸测试。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from music_dedup.cli import main
from music_dedup.core import ScanIssue, ScanResult


class CliSafetyTestCase(unittest.TestCase):
    """验证不完整扫描时不会执行 apply。"""

    def test_apply_is_blocked_when_scan_has_error(self) -> None:
        """扫描有 error 级问题时应生成阻止状态且不调用移动逻辑。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            issue = ScanIssue(root / "broken.flac", "无法读取", "error")
            scan = ScanResult(root, (), (issue,))
            with patch("music_dedup.cli.scan_library", return_value=scan):
                with patch("music_dedup.cli.apply_duplicates") as apply_mock:
                    result = main(
                        [
                            "--root",
                            str(root),
                            "--quarantine",
                            str(root.parent / "quarantine"),
                            "--apply",
                        ]
                    )

            self.assertEqual(result, 2)
            apply_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
