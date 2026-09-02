FROM python:3.12-alpine

WORKDIR /app

LABEL org.opencontainers.image.source="https://github.com/iwillhappy1314/music-deduplicator"
LABEL org.opencontainers.image.description="Safe metadata-based music deduplicator for Synology NAS"
LABEL org.opencontainers.image.licenses="MIT"

# ffprobe is used as a fallback for containers such as WebM that Mutagen cannot read.
RUN apk add --no-cache ffmpeg

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY music_dedup ./music_dedup

ENTRYPOINT ["python", "-m", "music_dedup"]
