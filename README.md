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
- Docker Compose 默认启动 Web 控制台；`WEB_RUN_ON_START=true` 时每次启动服务会先执行一次去重。
- Web 控制台可以单独触发去重、外挂歌词、专辑封面或全部操作；任务完成后会保留日志和最新报告。
- `--apply` 只会把重复文件移动到隔离目录，不会永久删除；外挂歌词和封面只补齐缺失文件，不覆盖已有文件。
- 如果扫描出现读取权限或解码错误，`--apply` 会自动阻止所有移动，避免基于不完整扫描清理。
- `._*`、隐藏目录和非音频文件会忽略。当前支持 Mutagen 能识别的常见音频格式，并用 `ffprobe` 补充读取 WebM 等容器。

可选的歌词和封面补齐使用 Artist、Title、Album 元数据。歌词通过 LRCLIB 查询，优先写入同步歌词 `.lrc`，没有同步歌词时写入 `.txt`；封面通过 iTunes Search API 精确匹配 Artist 和 Album 后写入专辑目录的 `cover.*`。接口不可用或找不到结果时只记录在报告中，不会阻止去重。

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

以下示例按你的 NAS 路径 `/volume2/music` 配置音乐共享文件夹；在 NAS SSH 或 Container Manager 项目目录中执行：

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

如果 NAS 上的共享文件夹路径不同，修改 `.env` 中的四个路径。Artist 映射文件的键必须是相对于 `/volume2/music` 的目录路径；没有列入映射的 WebM 会继续跳过。隔离目录、报告目录和配置目录建议放在音乐共享文件夹外面，避免它们再次被扫描。

查看 `first-apply.json` 确认移动结果。之后在 Dockge 中启动项目即可打开 Web 控制台；默认启动时会再执行一次去重：

```bash
cd /volume1/docker/music-deduplicator
docker compose up -d
```

浏览器打开 `http://群晖IP:8080`。如果 `.env` 设置了 `WEB_TOKEN`，在页面输入相同令牌后才能点击操作按钮。建议在首次部署时设置一个足够长的随机令牌。

如果不希望 Dockge 启动时自动去重，将 `.env` 改为：

```dotenv
WEB_RUN_ON_START=false
```

之后仍可以在控制台中手动点击「执行去重」。

## Web 控制台操作

控制台提供以下操作：

- 「执行去重」：按 Title + Artist 规则移动重复文件。
- 「获取外挂歌词」：只获取缺少 `.lrc`、`.txt` 等外挂歌词的音频，不重复执行去重。
- 「获取专辑封面」：只处理没有常见 `cover.*`、`folder.*` 或 `front.*` 文件的专辑目录。
- 「全部执行」：先去重，再为当前仍存在的文件获取歌词和封面。

所有操作都会写入 `/volume1/music-dedup-reports/latest.json` 和 `web.log`。Web 服务只接受固定任务类型，不接受任意 shell 命令，并且同一时间只允许运行一个任务。

歌词生成后，Navidrome 建议把外挂歌词放在优先级中：

```yaml
environment:
  ND_LYRICSPRIORITY: ".lrc,.txt,embedded"
```

然后对 Navidrome 执行一次完整扫描：

```bash
docker compose exec navidrome navidrome scan --full
```

此命令只移动重复文件。原始相对路径会在隔离目录中保留，例如：

```text
/volume2/music/Album/song.mp3
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
--fetch-lyrics      获取缺失的外挂歌词，需要同时使用 --apply
--fetch-artwork     获取缺失的专辑封面，需要同时使用 --apply
--skip-dedup        只执行歌词或封面操作，不重复执行去重
--request-delay SEC 网络请求间隔，默认 0.35
--web               启动 Web 控制台
--verbose           在终端列出每个重复组
--lock-file PATH    防止并发运行，默认 /tmp/music-deduplicator.lock
```
