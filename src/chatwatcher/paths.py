"""On-disk layout for per-VOD working data."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from chatwatcher.config import Config


@dataclass(frozen=True)
class VodFiles:
    root: Path

    @property
    def info(self) -> Path:          # trimmed yt-dlp metadata
        return self.root / "info.json"

    @property
    def chat_raw(self) -> Path:      # yt-dlp's live_chat.json, gzipped after parsing
        return self.root / "live_chat.json.gz"

    @property
    def chat(self) -> Path:          # normalized messages, one JSON per line
        return self.root / "chat.jsonl.gz"

    @property
    def transcript(self) -> Path:    # caption segments, one JSON per line
        return self.root / "transcript.jsonl"

    @property
    def heatmap(self) -> Path:       # YouTube "Most replayed" curve
        return self.root / "heatmap.json"

    @property
    def manifest(self) -> Path:      # what ingest found, counts, tool versions
        return self.root / "ingest.json"

    @property
    def scores(self) -> Path:        # per-bin signal timeline (for the dashboard chart)
        return self.root / "scores.json"

    @property
    def moments(self) -> Path:       # ranked moments with reasons and top chat
        return self.root / "moments.json"

    @property
    def downloads(self) -> Path:     # scratch area for yt-dlp output
        return self.root / "_dl"


def vod_files(cfg: Config, video_id: str) -> VodFiles:
    return VodFiles(cfg.paths.data_dir / "vods" / video_id)
