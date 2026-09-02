"""允许使用 `python -m music_dedup` 启动命令行程序。"""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
