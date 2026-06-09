"""
output.py — Saving results to JSON and printing to terminal
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from src.detector import SpikeDetector


def save_results(detector: SpikeDetector, output_file: str):
    """Write spikes + stats to a JSON file (incremental — safe to call anytime)."""
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "stats": detector.summary(),
        "spikes": [s.to_dict() for s in detector.spikes],
    }
    Path(output_file).write_text(json.dumps(data, indent=2))


def print_spike_alert(spike, window_seconds: int):
    """Print a spike alert to the terminal."""
    print()
    print(f"  🔥 SPIKE at [{spike.stream_timestamp}]")
    print(f"     Rate     : {spike.current_rate_per_min}/min  ({spike.spike_ratio}× baseline)")
    print(f"     Baseline : {spike.baseline_rate_per_min}/min")
    print(f"     Messages in last {window_seconds}s: {spike.messages_in_window}")
    print()


def print_status_line(poll: int, new_msgs: int, detector: SpikeDetector):
    """Overwrite the current terminal line with a live status update."""
    stats = detector.summary()
    line = (
        f"\r  [Poll #{poll:>4}]  +{new_msgs:<4} msgs  |  "
        f"Rate: {detector.current_rate:>6.1f}/min  |  "
        f"Baseline: {stats['average_baseline_rate_per_min']:>6.1f}/min  |  "
        f"Spikes: {stats['total_spikes_detected']}"
    )
    print(line, end="", flush=True)


def print_summary(detector: SpikeDetector, output_file: str):
    """Print the final summary table after the watcher stops."""
    stats = detector.summary()
    width = 54

    print("\n")
    print("  " + "═" * width)
    print("    FINAL SUMMARY")
    print("  " + "═" * width)
    print(f"    Total messages seen    : {stats['total_messages_seen']}")
    print(f"    Avg baseline rate      : {stats['average_baseline_rate_per_min']} msgs/min")
    print(f"    Peak spike rate        : {stats['peak_spike_rate_per_min']} msgs/min")
    print(f"    Total spikes detected  : {stats['total_spikes_detected']}")
    print("  " + "─" * width)

    if detector.spikes:
        print("    CLIP THESE TIMESTAMPS:")
        print()
        for i, spike in enumerate(detector.spikes, 1):
            print(
                f"    {i:>2}.  [{spike.stream_timestamp}]  "
                f"{spike.current_rate_per_min}/min  "
                f"({spike.spike_ratio}× baseline)"
            )
    else:
        print("    No spikes detected.")

    print("  " + "═" * width)
    print(f"    Results saved to: {Path(output_file).resolve()}")
    print()
