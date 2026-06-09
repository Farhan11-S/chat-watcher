"""
config.py — Load and validate all settings from .env
"""

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (one level up from src/)
_ENV_PATH = Path(__file__).parent.parent / ".env"
load_dotenv(_ENV_PATH)


@dataclass
class Config:
    # YouTube
    api_key: str

    # yt-dlp auth
    yt_cookies_file: str
    yt_po_token: str
    yt_deno_path: str

    # Database
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str

    # Default spike detection (used if no per-stream config in DB)
    window_seconds: int
    baseline_windows: int
    spike_multiplier: float
    min_messages: int
    cooldown_seconds: int

    # Output
    output_file: str


def load_config() -> Config:
    api_key = os.getenv("YOUTUBE_API_KEY", "").strip()

    if not api_key or api_key == "YOUR_API_KEY_HERE":
        print("❌ YOUTUBE_API_KEY is not set in .env")
        sys.exit(1)

    return Config(
        api_key=api_key,
        yt_cookies_file=os.getenv("YT_COOKIES_FILE", ""),
        yt_po_token=os.getenv("YT_PO_TOKEN", ""),
        yt_deno_path=os.getenv("YT_DENO_PATH", "/root/.deno/bin/deno"),
        db_host=os.getenv("DB_HOST", "localhost"),
        db_port=int(os.getenv("DB_PORT", 5432)),
        db_name=os.getenv("DB_NAME", "chatwatcher"),
        db_user=os.getenv("DB_USER", "chatwatcher"),
        db_password=os.getenv("DB_PASSWORD", ""),
        window_seconds=int(os.getenv("WINDOW_SECONDS", 30)),
        baseline_windows=int(os.getenv("BASELINE_WINDOWS", 10)),
        spike_multiplier=float(os.getenv("SPIKE_MULTIPLIER", 2.5)),
        min_messages=int(os.getenv("MIN_MESSAGES", 5)),
        cooldown_seconds=int(os.getenv("COOLDOWN_SECONDS", 60)),
        output_file=os.getenv("OUTPUT_FILE", "chat_spikes.json"),
    )


def extract_video_id(url_or_id: str) -> str:
    patterns = [
        r"(?:v=|youtu\.be/|/live/)([A-Za-z0-9_-]{11})",
        r"^([A-Za-z0-9_-]{11})$",
    ]
    for pattern in patterns:
        match = re.search(pattern, url_or_id)
        if match:
            return match.group(1)
    raise ValueError(f"Could not extract video ID from: {url_or_id}")
