"""
uploader.py — Upload clips to Google Drive via rclone.
"""

import subprocess
import logging

logger = logging.getLogger(__name__)


def upload_clip(local_path: str, remote: str, remote_folder: str,
                subfolder: str) -> tuple[bool, str]:
    """
    Upload a single clip to Google Drive.
    Returns (success, drive_url).
    Destination: {remote}:{remote_folder}/{subfolder}/
    """
    destination = f"{remote}:{remote_folder}/{subfolder}"
    cmd = [
        "rclone", "copy",
        local_path,
        destination,
        "--stats", "0",  # disable periodic stats output to avoid pipe buffer deadlock
    ]

    logger.info(f"Uploading {local_path} → {destination}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)

    if result.returncode == 0:
        drive_url = f"https://drive.google.com/drive/folders/{subfolder}"
        logger.info(f"Upload succeeded: {destination}")
        return True, drive_url

    logger.error(f"Upload failed: {result.stderr[:300]}")
    return False, ""


def get_folder_url(remote: str, remote_folder: str, subfolder: str) -> str:
    """
    Get the shareable Google Drive folder URL by looking up the folder ID via rclone.
    Falls back to a search URL if lookup fails.
    """
    cmd = [
        "rclone", "lsjson",
        f"{remote}:{remote_folder}",
        "--dirs-only",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)

    if result.returncode == 0:
        import json
        try:
            items = json.loads(result.stdout)
            for item in items:
                if item.get("Name") == subfolder:
                    folder_id = item.get("ID", "")
                    if folder_id:
                        return f"https://drive.google.com/drive/folders/{folder_id}"
        except Exception:
            pass

    return f"https://drive.google.com/drive/search?q={subfolder}"
