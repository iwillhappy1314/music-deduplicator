# 音乐库安全去重工具

独立仓库：[github.com/iwillhappy1314/music-deduplicator](https://github.com/iwillhappy1314/music-deduplicator)

发布镜像：`ghcr.io/iwillhappy1314/music-deduplicator:latest`

这个工具用于在 Synology NAS 上扫描音乐库，并按照音频元数据中的“歌曲名 + 演唱者”识别重复歌曲。

## 去重规则

- 歌曲名和演唱者相同：只保留一个文件。
- 同一组中存在 FLAC：优先保留 FLAC；如果有多个 FLAC，保留文件最大的一个。
- 同一组都不是 FLAC：保留文件最大的一个。
- 文件大小相同：按路径排序选择，保证每次结果稳定。
- 歌曲名相同但演唱者不同：不合并，全部保留。
- 优先读取音频内嵌的 `title` 和 `artist` 标签。
- `.webm` 缺少标签时，从文件名提取 Title，并移除类似 `.f248` 的下载格式后缀；Artist 只从明确配置的目录映射中读取。
- Artist 无法明确取得的文件会跳过，不会根据普通专辑名猜测演唱者；文件名也不会被当作 Artist。
- 匹配会统一 Artist 的繁体和简体中文，再统一 Unicode 兼容字符、大小写和多余空白；Title 不做简繁转换，也不会删除标点或合并其他不同文字，避免误合并。
- 简繁转换只用于 Artist 的去重比较键，不会改写音频文件标签、Artist 映射文件或报告中的原始 Artist。
- Docker Compose 默认使用 `--apply`；每次 Dockge 启动/重启容器都会执行一次去重。
- `--apply` 只会把重复文件移动到隔离目录，不会永久删除；容器成功退出后不会自动循环运行。
- 如果扫描出现读取权限或解码错误，`--apply` 会自动阻止所有移动，避免基于不完整扫描清理。
- `._*`、隐藏目录和非音频文件会忽略。当前支持 Mutagen 能识别的常见音频格式，并用 `ffprobe` 补充读取 WebM 等容器。

每次运行都可以写出 JSON 报告，其中包含重复组、保留文件、被隔离文件、跳过原因和失败动作。

## 在当前 Mac 上先预览

`/Volumes/music` 当前是 SMB 挂载的 NAS 音乐目录。可以在本机用 Docker 先做只读预览：

```bash
cd /Volumes/Storage/AiProjects/music-deduplicator
docker build -t music-deduplicator:local .
docker run --rm \
  -v /Volumes/music:/music:ro \
  -v /tmp/music-dedup-reports:/reports \
  music-deduplicator:local \
  --report /reports/preview.json --verbose
```

预览报告在 `/tmp/music-dedup-reports/preview.json`。确认保留结果后，再在 NAS 上执行 `--apply`。

本地预览如果要处理示例中的李宗盛 WebM 目录，可以额外传入示例映射：

```bash
docker run --rm \
  -v /Volumes/music:/music:ro \
  -v /tmp/music-dedup-reports:/reports \
  -v /Volumes/Storage/AiProjects/music-deduplicator/artist-map.example.json:/config/artist-map.json:ro \
  music-deduplicator:local \
  --artist-map /config/artist-map.json --report /reports/preview-with-webm.json --verbose
```

## 部署到 Synology Container Manager

假设音乐共享文件夹是 `/volume1/music`，在 NAS SSH 或 Container Manager 项目目录中执行：

```bash
mkdir -p /volume1/docker/music-deduplicator
mkdir -p /volume1/music-dedup-quarantine
mkdir -p /volume1/music-dedup-reports
mkdir -p /volume1/music-dedup-config
```

把本仓库复制或克隆到 `/volume1/docker/music-deduplicator`，然后：

```bash
git clone https://github.com/iwillhappy1314/music-deduplicator.git /volume1/docker/music-deduplicator
cd /volume1/docker/music-deduplicator
cp .env.example .env
cp artist-map.example.json /volume1/music-dedup-config/artist-map.json
docker compose pull
docker compose run --rm music-deduplicator --apply --report /reports/first-apply.json --verbose
```

如果 NAS 上的共享文件夹路径不同，修改 `.env` 中的四个路径。Artist 映射文件的键必须是相对于 `/volume1/music` 的目录路径；没有列入映射的 WebM 会继续跳过。隔离目录、报告目录和配置目录建议放在音乐共享文件夹外面，避免它们再次被扫描。

查看 `first-apply.json` 确认移动结果。之后在 Dockge 中启动或重启容器即可再次执行一次去重：

```bash
cd /volume1/docker/music-deduplicator
docker compose run --rm music-deduplicator --apply --report /reports/first-apply.json
```

此命令只移动重复文件。原始相对路径会在隔离目录中保留，例如：

```text
/volume1/music/Album/song.mp3
→ /volume1/music-dedup-quarantine/Album/song.mp3
```

如果目标路径已经存在，程序会自动增加唯一后缀，不会覆盖隔离目录中的文件。确认音乐播放器和备份均正常后，再由管理员手动清理隔离目录。

## 配置 DSM 定时任务

建议先完成一次人工预览和 `--apply`，再在 DSM「控制面板 → 任务计划 → 新增 → 计划的任务 → 用户定义的脚本」中设置每天凌晨运行。用户定义的脚本填写：

```bash
/volume1/docker/music-deduplicator/run_scheduled.sh
```

脚本会执行带 `--apply` 的安全移动，并将最新报告写入 `/volume1/music-dedup-reports/latest.json`。推荐安排在备份任务之后且播放器不使用音乐库的时段，例如每天 03:30。任务的运行用户必须能读写音乐库、隔离目录和报告目录。

GitHub Actions 每次向 `main` 推送工具改动后会自动构建并更新 `latest` 镜像。Dockge 中执行「更新/拉取镜像并重建」即可使用新版本；定时脚本每次运行前也会执行一次镜像拉取。

如只想先定期观察，不想自动移动，把脚本中的 `--apply` 删除；默认 dry-run 可以重复运行。

## 恢复隔离文件

每份 apply 报告都记录 `source_path` 和 `quarantine_path`。确认误移后，根据报告把对应文件从隔离目录移回原路径；如果原路径已有新文件，请先人工比较，不要直接覆盖。

## 命令参数

```text
--root PATH          音乐库目录，默认读取 MUSIC_ROOT 或 /music
--quarantine PATH   隔离目录，默认读取 DEDUP_QUARANTINE 或 /quarantine
--report PATH       JSON 报告路径，可选
--artist-map PATH   WebM 目录到 Artist 的 JSON 映射，可选
--apply             执行移动；不传时只预览
--dry-run           明确指定只预览，不修改文件
--verbose           在终端列出每个重复组
--lock-file PATH    防止并发运行，默认 /tmp/music-deduplicator.lock
```
