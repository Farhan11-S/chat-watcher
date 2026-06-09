"""
watcher.py — Orchestrates VOD replay analysis and live stream polling
"""

import pathlib
import time

import requests

from src.api import _parse_chat_file, download_chat_replay, fetch_chat_page
from src.config import Config
from src.detector import SpikeDetector
from src.output import print_spike_alert, print_status_line, print_summary, save_results


def run_vod(config: Config, video_id: str, start_time_iso: str | None,
            stream_id: int = 0) -> SpikeDetector:
    """
    Download full chat replay via yt-dlp, simulate sliding windows
    across the entire VOD to find spikes. Returns the detector with results.
    """
    # Reuse already-downloaded file if it exists in /tmp
    cached = list(pathlib.Path("/tmp").glob(f"yt_chat_*/{video_id}.live_chat.json"))
    pre_existing_cache = bool(cached)
    if pre_existing_cache:
        print(f"  ♻️  Reusing cached chat file: {cached[0]}")
        messages = _parse_chat_file(cached[0])
    else:
        messages = download_chat_replay(
            video_id,
            cookies_file=config.yt_cookies_file,
            po_token=config.yt_po_token,
            deno_path=config.yt_deno_path,
        )

    # Persist messages to DB for later summarization / frontend use.
    # Skip only if reusing a pre-existing /tmp cache to avoid overwriting
    # good DB data with a potentially stale or partial cache.
    if stream_id > 0 and messages and not pre_existing_cache:
        from src.db import save_chat_messages
        print(f"  💾 Saving {len(messages):,} messages to database...")
        saved = save_chat_messages(config, stream_id, messages)
        print(f"     Saved {saved:,} messages.")

    if not messages:
        print("  ⚠️  No chat messages found in replay.")
        return SpikeDetector(
            window_seconds=config.window_seconds,
            baseline_windows=config.baseline_windows,
            spike_multiplier=config.spike_multiplier,
            min_messages=config.min_messages,
            cooldown_seconds=config.cooldown_seconds,
        )

    detector = SpikeDetector(
        window_seconds=config.window_seconds,
        baseline_windows=config.baseline_windows,
        spike_multiplier=config.spike_multiplier,
        min_messages=config.min_messages,
        cooldown_seconds=config.cooldown_seconds,
    )

    max_offset = messages[-1]["time_in_seconds"]
    step = config.window_seconds // 2  # advance half a window for sensitivity

    print(f"  ⏩ Scanning {len(messages):,} messages across "
          f"{int(max_offset // 60)}m {int(max_offset % 60)}s of stream...\n")

    window_end = float(config.window_seconds)

    while window_end <= max_offset + config.window_seconds:
        window_start = window_end - config.window_seconds
        batch = [
            m for m in messages
            if window_start <= m["time_in_seconds"] < window_end
        ]

        spike = detector.ingest_vod_window(batch, window_end)

        if spike:
            print_spike_alert(spike, config.window_seconds)
            save_results(detector, config.output_file)

        # Progress every ~5 minutes of stream time
        if int(window_end) % 300 == 0:
            pct = min(100, int(window_end / max_offset * 100))
            print(f"\r  ⏩ Progress: {pct}%  |  "
                  f"Spikes so far: {len(detector.spikes)}", end="", flush=True)

        window_end += step

    print()
    save_results(detector, config.output_file)
    print_summary(detector, config.output_file)

    return detector


def run_live(config: Config, live_chat_id: str, stream_start_epoch: float | None) -> SpikeDetector:
    """
    Poll YouTube Data API v3 in a loop for an active live stream.
    Returns the detector with results after the user stops it.
    """
    detector = SpikeDetector(
        window_seconds=config.window_seconds,
        baseline_windows=config.baseline_windows,
        spike_multiplier=config.spike_multiplier,
        min_messages=config.min_messages,
        cooldown_seconds=config.cooldown_seconds,
    )

    page_token: str | None = None
    poll_count = 0

    print("  👀 Watching live chat... Press Ctrl+C to stop.\n")

    try:
        while True:
            try:
                page = fetch_chat_page(live_chat_id, config.api_key, page_token)
            except requests.HTTPError as e:
                if e.response.status_code == 403:
                    print("\n  ❌ API quota exceeded or access denied.")
                    break
                print(f"\n  ⚠️  HTTP {e.response.status_code} — retrying in 10s")
                time.sleep(10)
                continue
            except requests.RequestException as e:
                print(f"\n  ⚠️  Network error: {e} — retrying in 10s")
                time.sleep(10)
                continue

            messages = page["messages"]
            page_token = page["next_page_token"]
            poll_interval = page["poll_interval_seconds"]
            poll_count += 1

            spike = detector.ingest_live(messages, stream_start_epoch)

            print_status_line(poll_count, len(messages), detector)

            if spike:
                print_spike_alert(spike, config.window_seconds)
                save_results(detector, config.output_file)

            time.sleep(poll_interval)

    except KeyboardInterrupt:
        print("\n\n  Stopped by user.")

    save_results(detector, config.output_file)
    print_summary(detector, config.output_file)

    return detector
