"""Web 控制台任务管理的确定性测试。"""

from __future__ import annotations

import tempfile
import threading
import time
import urllib.error
import urllib.request
import unittest
from pathlib import Path
from unittest.mock import patch

from music_dedup.web import ControlHTTPServer, JobManager, WebConfig, _make_handler


class WebJobTestCase(unittest.TestCase):
    """验证 Web 按钮只能启动受控且不并发的后台任务。"""

    def _config(self, root: Path) -> WebConfig:
        """创建测试用 Web 配置。"""

        return WebConfig(
            root=str(root / "music"),
            quarantine=str(root / "quarantine"),
            report=str(root / "report.json"),
            artist_map=None,
            host="127.0.0.1",
            port=18080,
            token="",
            log_path=str(root / "web.log"),
            run_on_start=False,
            request_delay=0,
            lyrics_api_url="https://lyrics.test/api/get",
            artwork_api_url="https://artwork.test/search",
        )

    def test_start_runs_fixed_dedup_command(self) -> None:
        """点击去重操作应启动一次 apply 命令并记录成功状态。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = JobManager(self._config(Path(temporary_directory)))
            with patch("music_dedup.cli.main", return_value=0) as cli_mock:
                accepted, _ = manager.start("dedup")
                manager.wait(2)

            self.assertTrue(accepted)
            self.assertEqual(manager.status()["status"], "success")
            arguments = cli_mock.call_args.args[0]
            self.assertIn("--apply", arguments)
            self.assertNotIn("--fetch-lyrics", arguments)

    def test_running_job_rejects_second_job(self) -> None:
        """已有任务运行时应拒绝第二个任务，避免并发移动音乐文件。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            manager = JobManager(self._config(Path(temporary_directory)))

            def slow_main(arguments: list[str]) -> int:
                """模拟一个正在运行的命令。"""

                time.sleep(0.05)
                return 0

            with patch("music_dedup.cli.main", side_effect=slow_main):
                accepted, _ = manager.start("lyrics")
                rejected, payload = manager.start("artwork")
                manager.wait(2)

            self.assertTrue(accepted)
            self.assertFalse(rejected)
            self.assertEqual(payload["status"], "running")
            self.assertEqual(manager.status()["status"], "success")

    def test_http_console_serves_page_status_and_rejects_unknown_action(self) -> None:
        """HTTP 控制台应提供页面、状态接口并拒绝未定义的任务类型。"""

        with tempfile.TemporaryDirectory() as temporary_directory:
            config = self._config(Path(temporary_directory))
            manager = JobManager(config)
            server = ControlHTTPServer((config.host, 0), _make_handler(manager))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base_url = f"http://127.0.0.1:{server.server_address[1]}"
            try:
                with urllib.request.urlopen(f"{base_url}/", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                    self.assertIn("音乐库控制台", response.read().decode("utf-8"))
                with urllib.request.urlopen(f"{base_url}/api/status", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                request = urllib.request.Request(
                    f"{base_url}/api/run/unknown",
                    method="POST",
                )
                try:
                    urllib.request.urlopen(request, timeout=2)
                except urllib.error.HTTPError as error:
                    try:
                        self.assertEqual(error.code, 409)
                    finally:
                        error.close()
                else:
                    self.fail("未知任务类型应该返回 409")
            finally:
                server.shutdown()
                thread.join(2)
                server.server_close()


if __name__ == "__main__":
    unittest.main()
