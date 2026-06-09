package models

import (
	"time"
)

// ── Streams ───────────────────────────────────────────────────────────────────

type Stream struct {
	ID           int        `db:"id"            json:"id"`
	VideoID      string     `db:"video_id"      json:"video_id"`
	URL          string     `db:"url"           json:"url"`
	Title        *string    `db:"title"         json:"title"`
	Channel      *string    `db:"channel"       json:"channel"`
	StreamStart  *time.Time `db:"stream_start"  json:"stream_start"`
	DurationSecs *int       `db:"duration_secs" json:"duration_secs"`
	Status       string     `db:"status"        json:"status"`
	ErrorMessage *string    `db:"error_message" json:"error_message,omitempty"`
	AddedAt      time.Time  `db:"added_at"      json:"added_at"`
	ProcessedAt  *time.Time `db:"processed_at"  json:"processed_at"`
}

type CreateStreamRequest struct {
	URL string `json:"url" validate:"required"`
}

type UpdateStreamRequest struct {
	URL    *string `json:"url"`
	Status *string `json:"status"`
}

// ── Spike Config ──────────────────────────────────────────────────────────────

type SpikeConfig struct {
	ID              int        `db:"id"               json:"id"`
	StreamID        *int       `db:"stream_id"        json:"stream_id"`
	WindowSeconds   int        `db:"window_seconds"   json:"window_seconds"`
	BaselineWindows int        `db:"baseline_windows" json:"baseline_windows"`
	SpikeMultiplier float64    `db:"spike_multiplier" json:"spike_multiplier"`
	MinMessages     int        `db:"min_messages"     json:"min_messages"`
	CooldownSeconds int        `db:"cooldown_seconds" json:"cooldown_seconds"`
	ClipWindowSecs  int        `db:"clip_window_secs" json:"clip_window_secs"`
	ClipResolution  string     `db:"clip_resolution"  json:"clip_resolution"`
	CreatedAt       time.Time  `db:"created_at"       json:"created_at"`
}

type UpsertSpikeConfigRequest struct {
	WindowSeconds   int     `json:"window_seconds"`
	BaselineWindows int     `json:"baseline_windows"`
	SpikeMultiplier float64 `json:"spike_multiplier"`
	MinMessages     int     `json:"min_messages"`
	CooldownSeconds int     `json:"cooldown_seconds"`
	ClipWindowSecs  int     `json:"clip_window_secs"`
	ClipResolution  string  `json:"clip_resolution"`
}

// ── Spike Results ─────────────────────────────────────────────────────────────

type SpikeResult struct {
	ID                 int       `db:"id"                   json:"id"`
	StreamID           int       `db:"stream_id"            json:"stream_id"`
	StreamTimestamp    string    `db:"stream_timestamp"     json:"stream_timestamp"`
	StreamSeconds      float64   `db:"stream_seconds"       json:"stream_seconds"`
	CurrentRatePerMin  float64   `db:"current_rate_per_min" json:"current_rate_per_min"`
	BaselineRatePerMin float64   `db:"baseline_rate_per_min" json:"baseline_rate_per_min"`
	SpikeRatio         float64   `db:"spike_ratio"          json:"spike_ratio"`
	MessagesInWindow   int       `db:"messages_in_window"   json:"messages_in_window"`
	ContextSummary     *string   `db:"context_summary"      json:"context_summary"`
	DetectedAt         time.Time `db:"detected_at"          json:"detected_at"`
}

// ── Stream Summary (view) ─────────────────────────────────────────────────────

type StreamSummary struct {
	ID              int        `db:"id"                json:"id"`
	VideoID         string     `db:"video_id"          json:"video_id"`
	Title           *string    `db:"title"             json:"title"`
	Status          string     `db:"status"            json:"status"`
	ErrorMessage    *string    `db:"error_message"     json:"error_message,omitempty"`
	StreamStart     *time.Time `db:"stream_start"      json:"stream_start"`
	DurationSecs    *int       `db:"duration_secs"     json:"duration_secs"`
	TotalSpikes     int        `db:"total_spikes"      json:"total_spikes"`
	AvgBaselineRate *float64   `db:"avg_baseline_rate" json:"avg_baseline_rate"`
	PeakSpikeRate   *float64   `db:"peak_spike_rate"   json:"peak_spike_rate"`
	ProcessedAt     *time.Time `db:"processed_at"      json:"processed_at"`
}
