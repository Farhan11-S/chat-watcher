"""Score an ingested VOD and store its ranked moments."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np

from chatwatcher import store
from chatwatcher.config import Config
from chatwatcher.ingest.captions import read_transcript
from chatwatcher.ingest.chat_parser import read_messages
from chatwatcher.ingest.ytdlp import extract_video_id
from chatwatcher.paths import VodFiles, vod_files
from chatwatcher.scoring.features import FEATURES, build_timeline
from chatwatcher.scoring.lexicons import Lexicon
from chatwatcher.scoring.moments import Moment, ScoreParams, pick_moments
from chatwatcher.scoring.presets import STREAM_TYPES, detect_type, weights_for


class ScoreError(Exception):
    pass


@dataclass
class ScoreResult:
    video_id: str
    title: str
    stream_type: str
    type_reason: str
    moments: list[Moment]
    n_bins: int
    bin_s: int
    files: VodFiles
    warnings: list[str] = field(default_factory=list)


def params_from(cfg: Config) -> ScoreParams:
    s = cfg.scoring
    return ScoreParams(
        min_score=s.min_score, top_n=s.top_n, merge_gap_s=s.merge_gap_s,
        pre_roll_s=s.pre_roll_s, post_roll_s=s.post_roll_s, min_clip_s=s.min_clip_s,
        max_clip_s=s.max_clip_s, feature_cap=s.feature_cap, edge_ignore_s=s.edge_ignore_s,
    )


def resolve_type(requested: str | None, video_row: sqlite3.Row | None, cfg_default: str,
                 info: dict[str, Any], tl=None) -> tuple[str, str]:
    """Explicit argument > per-video override > config default > auto-detect."""
    for value, why in ((requested, "requested"),
                       (video_row["stream_type"] if video_row else None, "set on video"),
                       (cfg_default, "config default")):
        if value and value != "auto":
            if value not in STREAM_TYPES:
                raise ScoreError(f"Unknown stream type {value!r}; use one of {STREAM_TYPES}")
            return value, why
    return detect_type(info, tl)


def score_video(
    cfg: Config,
    conn: sqlite3.Connection,
    url_or_id: str,
    *,
    stream_type: str | None = None,
    force: bool = False,
) -> ScoreResult:
    try:
        video_id = extract_video_id(url_or_id)
    except ValueError as e:
        raise ScoreError(str(e)) from e
    row = store.get_video(conn, video_id)
    files = vod_files(cfg, video_id)
    if row is None or not files.info.is_file():
        raise ScoreError(f"{video_id} hasn't been ingested yet. Run `cw ingest {video_id}` first.")

    _guard_existing(conn, row["id"], force)

    info = json.loads(files.info.read_text(encoding="utf-8"))
    messages = read_messages(files.chat) if files.chat.is_file() else []
    transcript = read_transcript(files.transcript) if files.transcript.is_file() else []
    heatmap = json.loads(files.heatmap.read_text()) if files.heatmap.is_file() else None
    duration = float(info.get("duration") or (messages[-1].t if messages else 0))

    warnings: list[str] = []
    if not messages and not heatmap:
        raise ScoreError("Nothing to score: no chat replay and no replay heatmap.")
    if not messages:
        warnings.append("No chat: moments come from the replay heatmap only.")
    if not transcript:
        warnings.append("No transcript: cut points use chat timing only.")

    s = cfg.scoring
    try:
        lexicon = Lexicon(s.extra_keywords)
        tl = build_timeline(
            messages, duration, bin_s=s.bin_seconds, react_window_s=s.react_window_s,
            baseline_window_s=s.baseline_window_s, lexicon=lexicon, heatmap=heatmap,
        )
        kind, reason = resolve_type(stream_type, row, s.default_stream_type, info, tl)
        weights = weights_for(kind, s.weights)
    except ValueError as e:  # bad extra_keywords / weights in config
        raise ScoreError(str(e)) from e
    if not messages:  # heatmap-only: let replay carry the whole score
        weights = {f: (w if f == "replay" else 0.0) for f, w in weights.items()}
    moments, score = pick_moments(tl, weights, messages, transcript, duration, params_from(cfg),
                                  songs=(kind == "karaoke"))
    if not moments:
        warnings.append(f"No moment reached min_score={s.min_score}; try lowering it.")

    _write_timeline(files, tl, score, kind, weights)
    _write_moments(files, video_id, kind, reason, moments)
    _save(conn, row["id"], video_id, kind, moments)
    return ScoreResult(video_id=video_id, title=info.get("title") or video_id, stream_type=kind,
                       type_reason=reason, moments=moments, n_bins=tl.n_bins, bin_s=tl.bin_s,
                       files=files, warnings=warnings)


def _guard_existing(conn: sqlite3.Connection, video_pk: int, force: bool) -> None:
    """Re-scoring replaces moments; don't silently throw away clips or review decisions."""
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM moments m
        WHERE m.video_id = ? AND (m.status != 'new'
            OR EXISTS (SELECT 1 FROM clips c WHERE c.moment_id = m.id)
            OR EXISTS (SELECT 1 FROM analyses a WHERE a.moment_id = m.id))
        """,
        (video_pk,),
    ).fetchone()
    if row["n"] and not force:
        raise ScoreError(f"{row['n']} moment(s) already have clips, analyses or a review decision. "
                         "Re-scoring would delete them; pass --force to do it anyway.")


def _save(conn: sqlite3.Connection, video_pk: int, video_id: str, kind: str,
          moments: list[Moment]) -> None:
    from chatwatcher.db import tx
    with tx(conn):
        conn.execute("DELETE FROM moments WHERE video_id = ?", (video_pk,))
        conn.executemany(
            """INSERT INTO moments (video_id, rank, score, start_s, end_s, peak_s, kind, features_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(video_pk, m.rank, m.score, m.start_s, m.end_s, m.peak_s, m.kind,
              json.dumps(m.features_json(), ensure_ascii=False)) for m in moments],
        )
        store.upsert_video(conn, video_id, detected_type=kind,
                           scored_at=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))


def _write_timeline(files: VodFiles, tl, score: np.ndarray, kind: str,
                    weights: dict[str, float]) -> None:
    data = {
        "bin_s": tl.bin_s, "n_bins": tl.n_bins, "stream_type": kind, "weights": weights,
        "score": np.round(score, 2).tolist(),
        "z": {f: np.round(tl.z[f], 2).tolist() for f in FEATURES},
        "rate": tl.raw["rate"].astype(int).tolist(),
    }
    tmp = files.scores.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
    tmp.replace(files.scores)


def _write_moments(files: VodFiles, video_id: str, kind: str, reason: str,
                   moments: list[Moment]) -> None:
    data = {
        "video_id": video_id, "stream_type": kind, "type_reason": reason,
        "scored_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "moments": [{"rank": m.rank, "score": m.score, "kind": m.kind, "start_s": m.start_s,
                     "end_s": m.end_s, "peak_s": m.peak_s, **m.features_json()} for m in moments],
    }
    tmp = files.moments.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(files.moments)
