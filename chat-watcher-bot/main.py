#!/usr/bin/env python3
"""
main.py — YouTube Chat Spike Watcher

Commands:
    python main.py add <url>              Add a stream to the watchlist
    python main.py list                   List all streams and their status
    python main.py run <url|video_id>     Process a specific stream
    python main.py run-pending            Process all pending streams
    python main.py results <url|video_id> Show spike results for a stream
    python main.py config                 Show global spike detection config
    python main.py set-config             Set global or per-stream spike config
"""

import sys
import traceback
from dateutil import parser as dateutil_parser

from src.api import YouTubeAPIError, get_stream_info
from src.config import extract_video_id, load_config
from src.watcher import run_live, run_vod
import src.db as db


# ── Helpers ───────────────────────────────────────────────────────────────────

def resolve_stream(config, arg: str) -> dict:
    """Get a stream row from DB by URL or video_id. Exits if not found."""
    try:
        video_id = extract_video_id(arg)
    except ValueError:
        print(f"  ❌ Could not parse video ID from: {arg}")
        sys.exit(1)

    stream = db.get_stream_by_video_id(config, video_id)
    if not stream:
        print(f"  ❌ Stream not found in DB: {video_id}")
        print(f"     Add it first with: python main.py add <url>")
        sys.exit(1)
    return stream


def process_stream(config, stream: dict):
    """Fetch info, load config, run watcher, save results for one stream."""
    video_id = stream["video_id"]
    stream_id = stream["id"]

    print(f"\n  🎬 Processing: {stream['url']}")

    # Mark as processing
    db.set_stream_status(config, stream_id, "processing")

    # Fetch YouTube metadata
    print("  🔍 Fetching video info...")
    try:
        info = get_stream_info(video_id, config.api_key)
    except YouTubeAPIError as e:
        err = str(e)
        print(f"  ❌ {err}")
        db.set_stream_status(config, stream_id, "error", err)
        return
    except Exception as e:
        err = traceback.format_exc()
        print(f"  ❌ Unexpected error fetching video info:\n{err}")
        db.set_stream_status(config, stream_id, "error", err)
        return

    # Update stream metadata in DB
    stream_start = info.get("start_time_iso")
    db.update_stream_info(
        config, stream_id,
        title=info["title"],
        channel=None,
        stream_start=stream_start,
        duration_secs=None,
    )

    print(f"     Title  : {info['title']}")
    print(f"     Mode   : {'🔴 Live Stream' if info['is_live'] else '📼 VOD Replay'}")
    if stream_start:
        print(f"     Start  : {stream_start}")

    # Load per-stream config (falls back to global default)
    cfg = db.get_spike_config(config, stream_id)
    print(f"\n  ⚙️  Config  : window={cfg['window_seconds']}s  "
          f"multiplier={cfg['spike_multiplier']}×  "
          f"cooldown={cfg['cooldown_seconds']}s")

    # Override config values from DB for this run
    config.window_seconds    = cfg["window_seconds"]
    config.baseline_windows  = cfg["baseline_windows"]
    config.spike_multiplier  = float(cfg["spike_multiplier"])
    config.min_messages      = cfg["min_messages"]
    config.cooldown_seconds  = cfg["cooldown_seconds"]

    # Parse stream start epoch
    stream_start_epoch = None
    if stream_start:
        try:
            stream_start_epoch = dateutil_parser.isoparse(stream_start).timestamp()
        except Exception:
            pass

    # Run watcher — returns detector with spikes populated
    try:
        if info["is_live"]:
            detector = run_live(config, info["live_chat_id"], stream_start_epoch)
        else:
            detector = run_vod(config, video_id, stream_start, stream_id=stream["id"])
    except Exception as e:
        err = traceback.format_exc()
        print(f"  ❌ Error during watcher run:\n{err}")
        db.set_stream_status(config, stream_id, "error", err)
        return

    # Save all spikes to DB
    count = db.save_all_spikes(config, stream_id, detector.spikes)
    db.set_stream_status(config, stream_id, "done")
    print(f"\n  💾 Saved {count} spike(s) to database.")


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_add(config, args):
    """Add one or more stream URLs to the watchlist."""
    if not args:
        print("  Usage: python main.py add <url> [url2 ...]")
        sys.exit(1)

    for url in args:
        try:
            video_id = extract_video_id(url)
        except ValueError as e:
            print(f"  ❌ {e}")
            continue

        stream_id = db.add_stream(config, video_id, url)
        existing = db.get_stream_by_video_id(config, video_id)
        status = existing["status"]
        print(f"  ✅ Added [{stream_id}] {video_id}  (status: {status})")


def cmd_list(config):
    """Print all streams from the stream_summary view."""
    rows = db.list_streams(config)
    if not rows:
        print("  No streams in database. Add one with: python main.py add <url>")
        return

    print()
    print(f"  {'ID':<4}  {'VIDEO ID':<13}  {'STATUS':<12}  {'SPIKES':<7}  {'PEAK /min':<10}  TITLE")
    print("  " + "─" * 80)
    for r in rows:
        title = (r["title"] or "—")[:45]
        peak  = f"{r['peak_spike_rate']}" if r["peak_spike_rate"] else "—"
        print(
            f"  {r['id']:<4}  {r['video_id']:<13}  {r['status']:<12}  "
            f"{r['total_spikes']:<7}  {peak:<10}  {title}"
        )
        if r["status"] == "error" and r.get("error_message"):
            # Show first line of error for quick diagnosis
            first_line = r["error_message"].strip().splitlines()[-1][:80]
            print(f"  {'':4}  {'':13}  ⚠️  {first_line}")
    print()


def cmd_run(config, args):
    """Process a specific stream by URL or video_id."""
    if not args:
        print("  Usage: python main.py run <url|video_id>")
        sys.exit(1)

    stream = resolve_stream(config, args[0])
    process_stream(config, stream)


def cmd_run_pending(config):
    """Process all streams with status=pending."""
    pending = db.get_pending_streams(config)
    if not pending:
        print("  No pending streams.")
        return

    print(f"  Found {len(pending)} pending stream(s).\n")
    for stream in pending:
        process_stream(config, stream)


def cmd_results(config, args):
    """Show spike results for a stream."""
    if not args:
        print("  Usage: python main.py results <url|video_id>")
        sys.exit(1)

    stream = resolve_stream(config, args[0])
    spikes = db.get_spikes_for_stream(config, stream["id"])

    print(f"\n  Stream : {stream['title'] or stream['video_id']}")
    print(f"  Status : {stream['status']}")
    print(f"  Spikes : {len(spikes)}")
    print()

    if not spikes:
        print("  No spikes recorded.")
        return

    print(f"  {'#':<4}  {'TIMESTAMP':<12}  {'RATE/min':<10}  {'BASELINE':<10}  {'RATIO':<7}  MSGS")
    print("  " + "─" * 60)
    for i, s in enumerate(spikes, 1):
        print(
            f"  {i:<4}  {s['stream_timestamp']:<12}  "
            f"{float(s['current_rate_per_min']):<10.1f}  "
            f"{float(s['baseline_rate_per_min']):<10.1f}  "
            f"{float(s['spike_ratio']):<7.2f}  "
            f"{s['messages_in_window']}"
        )
    print()


def cmd_config(config, args):
    """Show or set spike detection config (global or per-stream)."""
    # Show global config
    if not args or args[0] == "show":
        cfg = db.get_spike_config(config, None)
        print("\n  Global spike detection config:")
        print(f"    window_seconds   : {cfg['window_seconds']}")
        print(f"    baseline_windows : {cfg['baseline_windows']}")
        print(f"    spike_multiplier : {cfg['spike_multiplier']}")
        print(f"    min_messages     : {cfg['min_messages']}")
        print(f"    cooldown_seconds : {cfg['cooldown_seconds']}")
        print()
        return

    # set-config [video_id] key=value ...
    # e.g. python main.py config set spike_multiplier=3.0
    # e.g. python main.py config set fOl4gYT-dfw spike_multiplier=3.0
    if args[0] == "set":
        params = args[1:]
        stream_id = None

        # Check if first param looks like a video_id/url
        if params and "=" not in params[0]:
            try:
                video_id = extract_video_id(params[0])
                stream = db.get_stream_by_video_id(config, video_id)
                if stream:
                    stream_id = stream["id"]
                    params = params[1:]
                    print(f"  Setting config for stream: {video_id}")
                else:
                    print(f"  ❌ Stream not found: {params[0]}")
                    sys.exit(1)
            except ValueError:
                pass

        # Load current config as base
        cfg = db.get_spike_config(config, stream_id)
        updates = dict(cfg)

        for p in params:
            if "=" not in p:
                print(f"  ❌ Invalid param (expected key=value): {p}")
                sys.exit(1)
            key, val = p.split("=", 1)
            if key not in ("window_seconds", "baseline_windows", "spike_multiplier",
                           "min_messages", "cooldown_seconds"):
                print(f"  ❌ Unknown config key: {key}")
                sys.exit(1)
            updates[key] = val

        db.set_spike_config(
            config, stream_id,
            window_seconds=int(updates["window_seconds"]),
            baseline_windows=int(updates["baseline_windows"]),
            spike_multiplier=float(updates["spike_multiplier"]),
            min_messages=int(updates["min_messages"]),
            cooldown_seconds=int(updates["cooldown_seconds"]),
        )
        scope = f"stream {stream_id}" if stream_id else "global"
        print(f"  ✅ Config updated ({scope}).")


# ── Entry point ───────────────────────────────────────────────────────────────

USAGE = """
  YouTube Chat Spike Watcher

  Commands:
    add <url> [url2 ...]          Add stream(s) to the watchlist
    list                          List all streams and status
    run <url|video_id>            Process a specific stream
    run-pending                   Process all pending streams
    results <url|video_id>        Show spike results for a stream
    config show                   Show global spike detection config
    config set [video_id] k=v ... Set config values (global or per-stream)

  Examples:
    python main.py add https://youtube.com/watch?v=fOl4gYT-dfw
    python main.py run fOl4gYT-dfw
    python main.py config set spike_multiplier=3.0
    python main.py config set fOl4gYT-dfw cooldown_seconds=30
"""


def main():
    config = load_config()

    if len(sys.argv) < 2:
        print(USAGE)
        sys.exit(0)

    cmd = sys.argv[1]
    args = sys.argv[2:]

    if cmd == "add":
        cmd_add(config, args)
    elif cmd == "list":
        cmd_list(config)
    elif cmd == "run":
        cmd_run(config, args)
    elif cmd == "run-pending":
        cmd_run_pending(config)
    elif cmd == "results":
        cmd_results(config, args)
    elif cmd == "config":
        cmd_config(config, args)
    else:
        print(f"  ❌ Unknown command: {cmd}")
        print(USAGE)
        sys.exit(1)


if __name__ == "__main__":
    main()
