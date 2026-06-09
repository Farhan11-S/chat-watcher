"""
detector.py — Sliding-window spike detection logic

Works for both:
  - Live streams: messages arrive in real time via API polling
  - VOD replay: messages are pre-loaded and simulated by stream timestamp
"""

import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class Spike:
    wall_time_iso: str
    wall_time_readable: str
    stream_timestamp: str          # HH:MM:SS relative to stream start
    stream_seconds: float
    current_rate_per_min: float
    baseline_rate_per_min: float
    spike_ratio: float
    messages_in_window: int

    def to_dict(self) -> dict:
        return {
            "wall_time": self.wall_time_iso,
            "wall_time_readable": self.wall_time_readable,
            "stream_timestamp": self.stream_timestamp,
            "stream_seconds": self.stream_seconds,
            "current_rate_per_min": self.current_rate_per_min,
            "baseline_rate_per_min": self.baseline_rate_per_min,
            "spike_ratio": self.spike_ratio,
            "messages_in_window": self.messages_in_window,
        }


def format_stream_timestamp(seconds: float) -> str:
    """Convert seconds-from-start into HH:MM:SS or MM:SS."""
    s = max(0, int(seconds))
    h, remainder = divmod(s, 3600)
    m, sec = divmod(remainder, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


class SpikeDetector:
    def __init__(
        self,
        window_seconds: int,
        baseline_windows: int,
        spike_multiplier: float,
        min_messages: int,
        cooldown_seconds: int,
    ):
        self.window_seconds = window_seconds
        self.baseline_windows = baseline_windows
        self.spike_multiplier = spike_multiplier
        self.min_messages = min_messages
        self.cooldown_seconds = cooldown_seconds

        # Stream-time offsets (seconds) of messages in the current sliding window
        self._message_offsets: deque[float] = deque()

        # Completed-window rates (msgs/min) for rolling baseline
        self._window_rates: deque[float] = deque(maxlen=baseline_windows)

        self._last_window_boundary: float = 0.0   # stream seconds
        self._last_spike_offset: float | None = None

        self.total_messages: int = 0
        self.spikes: list[Spike] = []

    # ── Public ────────────────────────────────────────────────────────────────

    def ingest_vod_window(self, messages: list[dict], window_end_offset: float) -> Spike | None:
        """
        Feed a batch of VOD messages that fall within the current window.
        window_end_offset: stream seconds at the end of this window.
        """
        for msg in messages:
            self._message_offsets.append(msg["time_in_seconds"])
            self.total_messages += 1

        # Drop messages outside the sliding window
        cutoff = window_end_offset - self.window_seconds
        while self._message_offsets and self._message_offsets[0] < cutoff:
            self._message_offsets.popleft()

        # Flush completed windows into baseline
        while window_end_offset - self._last_window_boundary >= self.window_seconds:
            self._window_rates.append(self.current_rate)
            self._last_window_boundary += self.window_seconds

        return self._evaluate(window_end_offset)

    def ingest_live(self, messages: list[dict], stream_start_epoch: float | None) -> Spike | None:
        """
        Feed a batch of live messages (real-time polling).
        Uses wall clock time as the stream offset when no start epoch is known.
        """
        wall_now = time.time()
        stream_offset = (wall_now - stream_start_epoch) if stream_start_epoch else wall_now

        for msg in messages:
            self._message_offsets.append(stream_offset)
            self.total_messages += 1

        cutoff = stream_offset - self.window_seconds
        while self._message_offsets and self._message_offsets[0] < cutoff:
            self._message_offsets.popleft()

        mono_now = time.monotonic()
        if mono_now - self._last_window_boundary >= self.window_seconds:
            self._window_rates.append(self.current_rate)
            self._last_window_boundary = mono_now

        return self._evaluate(stream_offset)

    @property
    def current_rate(self) -> float:
        """Current msgs/min inside the sliding window."""
        return len(self._message_offsets) / (self.window_seconds / 60)

    @property
    def baseline_rate(self) -> float:
        """Rolling average msgs/min across completed windows."""
        if not self._window_rates:
            return 0.0
        return sum(self._window_rates) / len(self._window_rates)

    def summary(self) -> dict:
        peak = max((s.current_rate_per_min for s in self.spikes), default=0.0)
        return {
            "total_messages_seen": self.total_messages,
            "average_baseline_rate_per_min": round(self.baseline_rate, 1),
            "peak_spike_rate_per_min": round(peak, 1),
            "total_spikes_detected": len(self.spikes),
        }

    # ── Private ───────────────────────────────────────────────────────────────

    def _evaluate(self, stream_offset: float) -> Spike | None:
        count = len(self._message_offsets)
        rate = self.current_rate
        baseline = self.baseline_rate

        if count < self.min_messages or baseline == 0:
            return None

        ratio = rate / baseline
        if ratio < self.spike_multiplier:
            return None

        # Cooldown: don't fire again within cooldown_seconds of stream time
        if self._last_spike_offset is not None:
            if (stream_offset - self._last_spike_offset) < self.cooldown_seconds:
                return None

        self._last_spike_offset = stream_offset

        now_dt = datetime.now(timezone.utc)
        spike = Spike(
            wall_time_iso=now_dt.isoformat(),
            wall_time_readable=now_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
            stream_timestamp=format_stream_timestamp(stream_offset),
            stream_seconds=round(stream_offset, 1),
            current_rate_per_min=round(rate, 1),
            baseline_rate_per_min=round(baseline, 1),
            spike_ratio=round(ratio, 2),
            messages_in_window=count,
        )
        self.spikes.append(spike)
        return spike
