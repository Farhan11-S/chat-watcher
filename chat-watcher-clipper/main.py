"""
main.py — CLI entry point for chat-watcher-clipper.

Usage:
    python main.py clip-spike <stream_id> <spike_id> <job_id>
    python main.py status <job_id>
"""

import sys
import logging
from src.config import load_config
from src.db import get_clip_job
from src.clipper import run_spike_clip_job

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


def cmd_clip_spike(stream_id: int, spike_id: int, job_id: int):
    cfg = load_config()
    run_spike_clip_job(cfg, stream_id, spike_id, job_id)


def cmd_status(job_id: int):
    cfg = load_config()
    job = get_clip_job(cfg, job_id)
    if not job:
        print(f"Job {job_id} not found")
        sys.exit(1)
    print(f"Job {job_id}: status={job['status']} clips={job['clips_count']} "
          f"error={job['error_msg']} drive={job['drive_folder_url']}")


def main():
    if len(sys.argv) < 2:
        print("Usage: python main.py clip-spike <stream_id> <spike_id> <job_id>")
        print("       python main.py status <job_id>")
        sys.exit(1)

    command = sys.argv[1]

    if command == "clip-spike":
        if len(sys.argv) != 5:
            print("Usage: python main.py clip-spike <stream_id> <spike_id> <job_id>")
            sys.exit(1)
        cmd_clip_spike(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))

    elif command == "status":
        if len(sys.argv) != 3:
            print("Usage: python main.py status <job_id>")
            sys.exit(1)
        cmd_status(int(sys.argv[2]))

    else:
        print(f"Unknown command: {command}")
        sys.exit(1)


if __name__ == "__main__":
    main()
