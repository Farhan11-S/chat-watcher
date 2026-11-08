"""Settings: one TOML file, every field optional (defaults below).

Lookup order for the file:
  1. the path passed to load_config()
  2. $CHATWATCHER_CONFIG
  3. ./config.toml (if it exists)
Missing file → all defaults.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ENV_CONFIG = "CHATWATCHER_CONFIG"
DEFAULT_CONFIG_FILE = "config.toml"


class _Section(BaseModel):
    # Reject unknown keys so typos in config.toml fail loudly.
    model_config = ConfigDict(extra="forbid")


class PathsConfig(_Section):
    data_dir: Path = Path("data")      # db, raw chat, captions, heatmap
    output_dir: Path = Path("output")  # finished clips + storyboards

    @property
    def db_path(self) -> Path:
        return self.data_dir / "chatwatcher.db"


class YouTubeConfig(_Section):
    ytdlp_path: str = "yt-dlp"
    cookies_file: str = ""   # Netscape cookies.txt; needed for members-only + bot checks
    po_token: str = ""
    deno_path: str = ""      # yt-dlp JS challenge solver; empty = find on PATH
    caption_langs: list[str] = Field(default_factory=lambda: ["ja", "en"])
    extra_args: list[str] = Field(default_factory=list)  # appended to every yt-dlp call


class DiscoverConfig(_Section):
    """Channel watcher: reads each channel's Streams tab (streams only, no uploads/Shorts)."""
    poll_minutes: int = Field(30, ge=5)
    streams_per_check: int = Field(15, ge=1, le=50)
    min_duration_s: int = Field(600, ge=0)     # ignore streams shorter than this
    default_backfill: int = Field(1, ge=0)     # finished streams to queue when a channel is added


class PipelineConfig(_Section):
    """What the worker does automatically after ingest + score."""
    auto_clip: bool = True
    auto_analyze: bool = True
    clip_top: int = Field(0, ge=0)       # clip the best N moments (0 = all scored)
    analyze_top: int = Field(5, ge=0)    # Claude-analyse the best N clips (0 = all); saves plan usage


class ScoringConfig(_Section):
    bin_seconds: int = Field(5, ge=1)
    top_n: int = Field(10, ge=1)
    min_score: float = 1.0       # combined score (weighted mean of √z) a moment must reach
    merge_gap_s: int = 45        # peaks closer than this become one moment
    pre_roll_s: int = 25         # chat lags the event; start this much earlier
    post_roll_s: int = 5
    min_clip_s: int = 20
    max_clip_s: int = 180
    default_stream_type: Literal["auto", "talk", "gaming", "karaoke"] = "auto"
    react_window_s: int = Field(15, ge=1)       # chat reactions are summed over this window
    baseline_window_s: int = Field(600, ge=60)  # "normal" chat level = median over this window
    feature_cap: float = 64.0                   # z is capped here before the √ (max 8 per signal)
    edge_ignore_s: int = 60                     # no moments in the first/last N seconds
    # Per-type weight overrides, e.g. {talk = {laugh = 3.0}}
    weights: dict[str, dict[str, float]] = Field(default_factory=dict)
    # Extra literal keywords per reaction category, e.g. {laugh = [":_mywaraiemote:"]}
    extra_keywords: dict[str, list[str]] = Field(default_factory=dict)


class ClipConfig(_Section):
    # Best original quality, never re-encoded. AAC audio is preferred: same quality as
    # Opus to the ear, fits mp4, opens in every editor (Premiere can't read Opus).
    format: str = "bv*+ba[ext=m4a]/bv*+ba/b"
    sort: str = "res,fps,br"      # highest resolution, then fps, then bitrate
    merge_output_format: str = "mp4/mkv"
    max_concurrent: int = Field(2, ge=1)
    frames_per_clip: int = Field(8, ge=1)


class ClaudeConfig(_Section):
    """Summaries and storyboards via the `claude` CLI, i.e. your Claude subscription."""
    enabled: bool = True
    cli_path: str = "claude"
    model: str = "opus"           # CLI alias for the latest model; "sonnet" stretches usage limits
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    daily_moment_cap: int = Field(40, ge=0)   # max moment analyses per day (0 = no cap)
    timeout_s: int = Field(600, ge=30)
    output_language: str = "English"          # summaries, storyboard, translations
    title_languages: list[str] = Field(default_factory=lambda: ["ja", "en"])
    allow_api_key: bool = False   # False: ANTHROPIC_API_KEY is removed so the CLI uses the subscription


class DriveConfig(_Section):
    """Copy finished stream folders to Google Drive with rclone (never deletes on Drive)."""
    enabled: bool = False
    rclone_path: str = "rclone"
    remote: str = "gdrive"            # the name you gave the remote in `rclone config`
    folder: str = "chat-watcher"      # top folder on Drive; mirrors output/<channel>/<stream>/
    auto_upload: bool = True          # upload every stream when the pipeline finishes it
    transfers: int = Field(4, ge=1, le=16)
    # Days a Drive sign-in lasts. 7 while your Google app is in "Testing"; 0 = it doesn't expire.
    login_expires_days: int = Field(0, ge=0)


class WebConfig(_Section):
    host: str = "127.0.0.1"
    port: int = 8000


class Config(_Section):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    youtube: YouTubeConfig = Field(default_factory=YouTubeConfig)
    discover: DiscoverConfig = Field(default_factory=DiscoverConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    clip: ClipConfig = Field(default_factory=ClipConfig)
    claude: ClaudeConfig = Field(default_factory=ClaudeConfig)
    drive: DriveConfig = Field(default_factory=DriveConfig)
    web: WebConfig = Field(default_factory=WebConfig)

    # Where the settings came from (None = defaults only). Not a TOML key.
    source: Path | None = Field(default=None, exclude=True)


def find_config_file(explicit: str | Path | None = None) -> Path | None:
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise FileNotFoundError(f"Config file not found: {path}")
        return path
    env = os.environ.get(ENV_CONFIG)
    if env:
        path = Path(env)
        if not path.is_file():
            raise FileNotFoundError(f"{ENV_CONFIG} points to a missing file: {path}")
        return path
    default = Path(DEFAULT_CONFIG_FILE)
    return default if default.is_file() else None


def load_config(path: str | Path | None = None) -> Config:
    file = find_config_file(path)
    if file is None:
        return Config()
    with file.open("rb") as f:
        raw = tomllib.load(f)
    cfg = Config.model_validate(raw)
    cfg.source = file

    # Relative paths in the file are relative to the file, not the cwd.
    base = file.resolve().parent
    for name in ("data_dir", "output_dir"):
        value: Path = getattr(cfg.paths, name)
        if not value.is_absolute():
            setattr(cfg.paths, name, base / value)
    return cfg
