"""音乐去重工具的命令行入口和终端输出。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Sequence

from .core import (
    DuplicateGroup,
    MoveAction,
    ScanResult,
    apply_duplicates,
    build_report,
    find_duplicate_groups,
    load_artist_map,
    process_lock,
    scan_library,
    write_report,
)
from .enrichment import (
    ARTWORK_API_URL,
    EnrichmentAction,
    LYRICS_API_URL,
    build_enrichment_report,
    fetch_album_artwork,
    fetch_lyrics,
)
from .web import WebConfig, run_web_server


def build_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器。"""

    parser = argparse.ArgumentParser(
        description="按歌曲名和演唱者安全识别音乐库中的重复音频。"
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("MUSIC_ROOT", "/music"),
        help="音乐库目录，默认读取 MUSIC_ROOT 或 /music。",
    )
    parser.add_argument(
        "--quarantine",
        default=os.environ.get("DEDUP_QUARANTINE", "/quarantine"),
        help="重复文件隔离目录，默认读取 DEDUP_QUARANTINE 或 /quarantine。",
    )
    parser.add_argument(
        "--report",
        default=os.environ.get("DEDUP_REPORT"),
        help="可选的 JSON 报告路径。",
    )
    parser.add_argument(
        "--artist-map",
        default=os.environ.get("DEDUP_ARTIST_MAP"),
        help="WebM 目录到 Artist 的 JSON 映射，默认读取 DEDUP_ARTIST_MAP。",
    )
    parser.add_argument(
        "--fetch-lyrics",
        action="store_true",
        help="为缺少歌词的音频获取同目录外挂歌词；需要同时使用 --apply。",
    )
    parser.add_argument(
        "--fetch-artwork",
        action="store_true",
        help="为缺少封面的专辑目录获取 cover 图片；需要同时使用 --apply。",
    )
    parser.add_argument(
        "--lyrics-api-url",
        default=os.environ.get("LYRICS_API_URL", LYRICS_API_URL),
        help="歌词接口地址，默认使用 LRCLIB。",
    )
    parser.add_argument(
        "--artwork-api-url",
        default=os.environ.get("ARTWORK_API_URL", ARTWORK_API_URL),
        help="专辑封面搜索接口地址，默认使用 iTunes Search API。",
    )
    parser.add_argument(
        "--request-delay",
        type=float,
        default=float(os.environ.get("ENRICHMENT_REQUEST_DELAY", "0.35")),
        help="连续网络请求之间的间隔秒数，默认 0.35。",
    )
    parser.add_argument(
        "--skip-dedup",
        action="store_true",
        help="只执行 --fetch-lyrics 或 --fetch-artwork，不重复执行去重。",
    )
    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument(
        "--apply",
        action="store_true",
        help="将重复文件移动到隔离目录；不传时只扫描预览。",
    )
    mode_group.add_argument(
        "--dry-run",
        action="store_true",
        help="明确指定只扫描预览，不修改文件。默认模式。",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="在终端列出每个重复组。",
    )
    parser.add_argument(
        "--lock-file",
        default=os.environ.get("DEDUP_LOCK_FILE", "/tmp/music-deduplicator.lock"),
        help="并发锁文件路径。",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="启动 Web 控制台，不直接执行一次性命令。",
    )
    parser.add_argument(
        "--web-host",
        default=os.environ.get("WEB_HOST", "0.0.0.0"),
        help="Web 控制台监听地址，默认 0.0.0.0。",
    )
    parser.add_argument(
        "--web-port",
        type=int,
        default=int(os.environ.get("WEB_PORT", "8080")),
        help="Web 控制台监听端口，默认 8080。",
    )
    parser.add_argument(
        "--web-token",
        default=os.environ.get("WEB_TOKEN", ""),
        help="可选的 Web 控制台访问令牌。",
    )
    parser.add_argument(
        "--web-log",
        default=os.environ.get("WEB_LOG", "/reports/web.log"),
        help="Web 控制台任务日志路径。",
    )
    return parser


def _format_size(size: int) -> str:
    """把字节数格式化为便于阅读的大小。"""

    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _print_group(group: DuplicateGroup, root: Path, actions: Sequence[MoveAction]) -> None:
    """输出一个重复组的保留文件和待处理文件。"""

    action_by_source = {str(action.source_path): action for action in actions}
    print(f"[重复] {group.title} — {group.artist}")
    print(
        f"  保留: {group.keeper.path.relative_to(root)} "
        f"({group.keeper.path.suffix.lower().lstrip('.')}, "
        f"{_format_size(group.keeper.size)})"
    )
    for duplicate in group.duplicates:
        action = action_by_source.get(str(duplicate.path))
        status = "待隔离" if action is None else action.status
        print(
            f"  处理: {duplicate.path.relative_to(root)} "
            f"({_format_size(duplicate.size)}) [{status}]"
        )


def print_summary(
    scan: ScanResult,
    duplicate_groups: Sequence[DuplicateGroup],
    actions: Sequence[MoveAction],
    apply_mode: bool,
    verbose: bool,
) -> None:
    """输出一次扫描的简明结果和可选的重复明细。"""

    duplicate_file_count = sum(group.duplicate_count for group in duplicate_groups)
    moved_count = sum(action.status == "moved" for action in actions)
    failed_count = sum(action.status in {"failed", "skipped_changed"} for action in actions)
    mode_label = "执行移动" if apply_mode else "只读预览"
    print(f"模式: {mode_label}")
    print(f"音乐库: {scan.root}")
    print(f"读取音频: {len(scan.records)} 个")
    print(f"重复组: {len(duplicate_groups)} 组，待处理文件: {duplicate_file_count} 个")
    if apply_mode:
        print(f"已移动: {moved_count} 个，失败或跳过: {failed_count} 个")
    print(f"标签缺失或读取问题: {len(scan.issues)} 个")
    if verbose:
        for group in duplicate_groups:
            _print_group(group, scan.root, actions)


def print_enrichment_summary(actions: Sequence[EnrichmentAction]) -> None:
    """输出歌词和封面补齐的简明结果。"""

    created_count = sum(action.status == "created" for action in actions)
    not_found_count = sum(action.status == "not_found" for action in actions)
    skipped_count = sum(
        action.status in {"skipped_exists", "skipped_metadata", "skipped_missing"}
        for action in actions
    )
    failed_count = sum(action.status == "failed" for action in actions)
    print(
        "补齐结果: "
        f"新增 {created_count} 个，已有或跳过 {skipped_count} 个，"
        f"未找到 {not_found_count} 个，失败 {failed_count} 个"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """执行一次扫描、可选移动和报告写入，并返回进程退出码。"""

    parser = build_parser()
    arguments = parser.parse_args(argv)
    if arguments.web:
        run_web_server(
            WebConfig(
                root=arguments.root,
                quarantine=arguments.quarantine,
                report=arguments.report or "/reports/latest.json",
                artist_map=arguments.artist_map,
                host=arguments.web_host,
                port=arguments.web_port,
                token=arguments.web_token,
                log_path=arguments.web_log,
                run_on_start=os.environ.get("WEB_RUN_ON_START", "true").casefold()
                not in {"0", "false", "no", "off"},
                request_delay=arguments.request_delay,
                lyrics_api_url=arguments.lyrics_api_url,
                artwork_api_url=arguments.artwork_api_url,
            )
        )
        return 0
    if (arguments.fetch_lyrics or arguments.fetch_artwork) and not arguments.apply:
        parser.error("获取歌词或封面会修改音乐库，必须同时使用 --apply")
    if arguments.skip_dedup and not (arguments.fetch_lyrics or arguments.fetch_artwork):
        parser.error("--skip-dedup 必须与 --fetch-lyrics 或 --fetch-artwork 一起使用")
    if arguments.request_delay < 0:
        parser.error("--request-delay 不能小于 0")
    actions: tuple[MoveAction, ...] = ()
    enrichment_actions: list[EnrichmentAction] = []
    try:
        with process_lock(Path(arguments.lock_file)):
            artist_map = load_artist_map(
                Path(arguments.artist_map) if arguments.artist_map else None
            )
            scan = scan_library(Path(arguments.root), artist_map)
            dedup_enabled = not arguments.skip_dedup
            duplicate_groups = find_duplicate_groups(scan.records) if dedup_enabled else ()
            apply_blocked = dedup_enabled and arguments.apply and any(
                issue.severity == "error" for issue in scan.issues
            )
            if dedup_enabled and arguments.apply and not apply_blocked:
                actions = apply_duplicates(
                    scan,
                    duplicate_groups,
                    Path(arguments.quarantine),
                )
            if (arguments.fetch_lyrics or arguments.fetch_artwork) and not apply_blocked:
                enrichment_scan = scan
                if dedup_enabled and arguments.apply:
                    enrichment_scan = scan_library(Path(arguments.root), artist_map)
                if arguments.fetch_lyrics:
                    enrichment_actions.extend(
                        fetch_lyrics(
                            enrichment_scan.records,
                            request_delay=arguments.request_delay,
                            api_url=arguments.lyrics_api_url,
                        )
                    )
                if arguments.fetch_artwork:
                    enrichment_actions.extend(
                        fetch_album_artwork(
                            enrichment_scan.records,
                            request_delay=arguments.request_delay,
                            api_url=arguments.artwork_api_url,
                        )
                    )
            report = build_report(
                scan,
                duplicate_groups,
                actions,
                arguments.apply,
                Path(arguments.quarantine),
                apply_blocked,
            )
            if arguments.fetch_lyrics or arguments.fetch_artwork:
                report["enrichment"] = build_enrichment_report(
                    enrichment_actions,
                    scan.root,
                )
            if arguments.report:
                write_report(report, Path(arguments.report))
            print_summary(
                scan,
                duplicate_groups,
                actions,
                arguments.apply,
                arguments.verbose,
            )
            if enrichment_actions:
                print_enrichment_summary(enrichment_actions)
            if apply_blocked:
                print("错误: 扫描存在读取错误，已阻止移动；请先修复后重试。", file=sys.stderr)
                return 2
    except (RuntimeError, ValueError, OSError) as error:
        print(f"错误: {error}", file=sys.stderr)
        return 1

    has_action_failures = any(
        action.status in {"failed", "skipped_changed"} for action in actions
    )
    has_enrichment_failures = any(
        action.status == "failed" for action in enrichment_actions
    )
    has_scan_errors = any(issue.severity == "error" for issue in scan.issues)
    return 2 if has_action_failures or has_enrichment_failures or has_scan_errors else 0
