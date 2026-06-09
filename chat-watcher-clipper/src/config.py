"""
config.py — Load environment variables into a Config dataclass.
"""

import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    # Database
    db_host: str
    db_port: int
    db_name: str
    db_user: str
    db_password: str

    # Paths
    output_dir: str
    cookies_file: str

    # YouTube
    yt_po_token: str

    # rclone
    rclone_remote: str
    rclone_folder: str


def load_config() -> Config:
    return Config(
        db_host=os.getenv("DB_HOST", "localhost"),
        db_port=int(os.getenv("DB_PORT", "5432")),
        db_name=os.getenv("DB_NAME", "chatwatcher"),
        db_user=os.getenv("DB_USER", "chatwatcher"),
        db_password=os.getenv("DB_PASSWORD", "chatwatcher123"),
        output_dir=os.getenv("OUTPUT_DIR", "./output"),
        cookies_file=os.getenv("COOKIES_FILE", "./youtube_cookies.txt"),
        yt_po_token=os.getenv("YT_PO_TOKEN", ""),
        rclone_remote=os.getenv("RCLONE_REMOTE", "gdrive"),
        rclone_folder=os.getenv("RCLONE_FOLDER", "chat-watcher-clips"),
    )
