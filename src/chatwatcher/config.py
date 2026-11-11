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

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

ENV_CONFIG = "CHATWATCHER_CONFIG"
DEFAULT_CONFIG_FILE = "config.toml"


class _Section(BaseModel):
    # Reject unknown keys so typos in config.toml fail loudly.
    model_config = ConfigDict(extra="forbid")


class GeneralConfig(_Section):
    # Time zone for every time shown (dashboard, `cw` output, logs) and folder dates.
    # IANA name, e.g. "Asia/Jakarta" (WIB). Empty = this computer's.
    timezone: str = ""

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, v: str) -> str:
        if v.strip():
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
            try:
                ZoneInfo(v.strip())
            except (ZoneInfoNotFoundError, ValueError) as e:
                raise ValueError(f"unknown time zone {v!r}; use a name like 'Asia/Jakarta'") from e
        return v.strip()


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
    analyze_top: int = Field(0, ge=0)    # Claude-analyse the best N moments (0 = all)
    # Moments that aren't downloaded get a low-res, video-only preview so Claude can look at
    # them before you decide (only the frames are kept). 0 = analyse downloaded clips only.
    preview_height: int = Field(360, ge=0, le=1080)


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


class EditConfig(_Section):
    # The dashboard's editor: cuts are frame-exact, so the edit is re-encoded once.
    crf: int = Field(18, ge=0, le=51)          # x264 quality: lower = better and bigger; 17-20 looks lossless
    preset: str = "veryfast"                   # x264 speed: ultrafast … veryslow (slower = smaller file)
    audio_bitrate: str = "192k"
    loudness_lufs: float = Field(-14.0, le=0)  # YouTube plays at -14 LUFS; 0 = leave the loudness as is
    fade_ms: int = Field(30, ge=0, le=1000)    # tiny audio fade at every cut so cuts don't click
    snap_s: float = Field(1.5, ge=0, le=5)     # how far Claude's cut points may move to land in a pause


class TranscribeConfig(_Section):
    """Speech to text on each downloaded clip with Whisper (faster-whisper), on this computer:
    a word-by-word transcript for subtitles. Needs `pip install faster-whisper`; models are
    downloaded once into <data_dir>/models."""
    enabled: bool = True
    auto: bool = True                 # after each clip downloads; False = only the editor's button
    model: str = "large-v3-turbo"     # or "small": about a third faster, more misheard words
    language: str = ""                # "" = detect per line (handles mixed English/Indonesian); or "en", "id", "ja"
    threads: int = Field(0, ge=0)     # CPU threads; 0 = all
    speakers: bool = False            # guess who says each line (pip install sherpa-onnx); off: you pick in the editor
    speaker_threshold: float = Field(0.6, gt=0, lt=2)   # higher = fewer voices (one person may split into two)


class SubtitlesConfig(_Section):
    """Subtitles in the editor's render, built from Whisper's word timings."""
    on: bool = True                                    # burned into new edits (karaoke clips start off)
    size: Literal["small", "normal", "large"] = "normal"
    position: Literal["bottom", "top"] = "bottom"
    font: str = "Noto Sans CJK JP"                     # any installed font; this one also has Japanese


class ClaudeConfig(_Section):
    """Summaries and storyboards via the `claude` CLI, i.e. your Claude subscription."""
    enabled: bool = True
    cli_path: str = "claude"
    model: str = "opus"           # CLI alias for the latest model; pick another per clip on the dashboard
    effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # What "Analyse again" on one moment offers first: a stronger model for the ones that matter.
    redo_model: str = "opus"
    redo_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"
    # The editor's Claude buttons (✨ Suggest effects, subtitle ✨ Clean up): few calls, and
    # their taste shows in the finished video, so a stronger model by default.
    edit_model: str = "opus"
    edit_effort: Literal["low", "medium", "high", "xhigh", "max"] = "high"
    # At most moment_cap analyses in any moment_cap_hours (a rolling window, like Claude's 5-hour
    # limit). Extra clips wait until the oldest analysis in the window is older than that.
    moment_cap: int = Field(50, ge=0)                     # 0 = no cap
    moment_cap_hours: float = Field(5, gt=0, le=168)
    timeout_s: int = Field(600, ge=30)
    output_language: str = "English"          # summaries, storyboard, translations
    title_languages: list[str] = Field(default_factory=lambda: ["ja", "en"])
    allow_api_key: bool = False   # False: ANTHROPIC_API_KEY is removed so the CLI uses the subscription

    @model_validator(mode="before")
    @classmethod
    def _renamed(cls, data):
        if isinstance(data, dict) and "daily_moment_cap" in data:
            raise ValueError("claude.daily_moment_cap was replaced by claude.moment_cap: analyses per "
                             "claude.moment_cap_hours (default 50 per 5 hours)")
        return data


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
    general: GeneralConfig = Field(default_factory=GeneralConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    youtube: YouTubeConfig = Field(default_factory=YouTubeConfig)
    discover: DiscoverConfig = Field(default_factory=DiscoverConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    scoring: ScoringConfig = Field(default_factory=ScoringConfig)
    clip: ClipConfig = Field(default_factory=ClipConfig)
    edit: EditConfig = Field(default_factory=EditConfig)
    transcribe: TranscribeConfig = Field(default_factory=TranscribeConfig)
    subtitles: SubtitlesConfig = Field(default_factory=SubtitlesConfig)
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
