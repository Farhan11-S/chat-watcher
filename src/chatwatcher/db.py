"""SQLite storage. Schema changes are numbered migrations tracked by PRAGMA user_version.

To change the schema, append a new SQL string to MIGRATIONS — never edit an old one.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

MIGRATIONS: list[str] = [
    # 1 — initial schema
    """
    CREATE TABLE channels (
        id              INTEGER PRIMARY KEY,
        channel_id      TEXT NOT NULL UNIQUE,          -- YouTube UC… id
        handle          TEXT,                          -- @handle
        title           TEXT,
        default_type    TEXT NOT NULL DEFAULT 'auto'
                        CHECK (default_type IN ('auto','talk','gaming','karaoke')),
        auto_upload     INTEGER NOT NULL DEFAULT 0,
        enabled         INTEGER NOT NULL DEFAULT 1,
        last_checked_at TEXT,
        added_at        TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE videos (
        id            INTEGER PRIMARY KEY,
        video_id      TEXT NOT NULL UNIQUE,            -- 11-char YouTube id
        channel_id    INTEGER REFERENCES channels(id) ON DELETE SET NULL,
        title         TEXT,
        duration_s    REAL,
        published_at  TEXT,
        stream_type   TEXT NOT NULL DEFAULT 'auto'
                      CHECK (stream_type IN ('auto','talk','gaming','karaoke')),
        status        TEXT NOT NULL DEFAULT 'new'
                      CHECK (status IN ('new','processing','done','error','skipped')),
        error         TEXT,
        data_dir      TEXT,
        output_dir    TEXT,
        drive_url     TEXT,
        added_at      TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at    TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_videos_channel ON videos(channel_id);
    CREATE INDEX idx_videos_status ON videos(status);

    CREATE TABLE moments (
        id             INTEGER PRIMARY KEY,
        video_id       INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        rank           INTEGER NOT NULL,
        score          REAL NOT NULL,
        start_s        REAL NOT NULL,
        end_s          REAL NOT NULL,
        peak_s         REAL NOT NULL,
        kind           TEXT NOT NULL DEFAULT 'reaction',   -- reaction | song | ...
        features_json  TEXT NOT NULL DEFAULT '{}',         -- score breakdown
        status         TEXT NOT NULL DEFAULT 'new'
                       CHECK (status IN ('new','approved','rejected')),
        created_at     TEXT NOT NULL DEFAULT (datetime('now')),
        CHECK (end_s > start_s)
    );
    CREATE INDEX idx_moments_video ON moments(video_id, rank);

    CREATE TABLE clips (
        id          INTEGER PRIMARY KEY,
        moment_id   INTEGER NOT NULL UNIQUE REFERENCES moments(id) ON DELETE CASCADE,
        path        TEXT NOT NULL,
        container   TEXT,
        width       INTEGER,
        height      INTEGER,
        fps         REAL,
        vcodec      TEXT,
        acodec      TEXT,
        size_bytes  INTEGER,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );

    -- moment_id NULL = stream-level digest for the video
    CREATE TABLE analyses (
        id             INTEGER PRIMARY KEY,
        video_id       INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        moment_id      INTEGER REFERENCES moments(id) ON DELETE CASCADE,
        model          TEXT,
        analysis_json  TEXT NOT NULL,
        created_at     TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_analyses_moment ON analyses(moment_id);
    CREATE INDEX idx_analyses_video ON analyses(video_id);

    CREATE TABLE jobs (
        id          INTEGER PRIMARY KEY,
        video_id    INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        moment_id   INTEGER REFERENCES moments(id) ON DELETE CASCADE,
        stage       TEXT NOT NULL
                    CHECK (stage IN ('ingest','score','clip','frames','analyze','digest','upload')),
        status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','done','error')),
        attempts    INTEGER NOT NULL DEFAULT 0,
        run_after   TEXT NOT NULL DEFAULT (datetime('now')),
        error       TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    -- One job per (video, stage, moment); NULL moment counts as one value.
    CREATE UNIQUE INDEX uq_jobs_key ON jobs(video_id, stage, COALESCE(moment_id, 0));
    CREATE INDEX idx_jobs_queue ON jobs(status, stage, run_after);

    CREATE TABLE settings (
        key         TEXT PRIMARY KEY,
        value_json  TEXT NOT NULL
    );
    """,
    # 2 — ingest results on videos
    """
    ALTER TABLE videos ADD COLUMN live_status TEXT;
    ALTER TABLE videos ADD COLUMN message_count INTEGER;   -- NULL = not ingested / no chat replay
    ALTER TABLE videos ADD COLUMN caption_lang TEXT;       -- e.g. 'ja-orig'; NULL = none
    ALTER TABLE videos ADD COLUMN has_heatmap INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE videos ADD COLUMN ingested_at TEXT;
    """,
    # 3 — scoring results on videos
    """
    ALTER TABLE videos ADD COLUMN detected_type TEXT;     -- talk | gaming | karaoke (auto-detected)
    ALTER TABLE videos ADD COLUMN scored_at TEXT;
    """,
    # 4 — clip details: where the file starts in stream time, frames, folder
    """
    ALTER TABLE clips ADD COLUMN folder TEXT;
    ALTER TABLE clips ADD COLUMN zero_s REAL;          -- stream time at clip time 0
    ALTER TABLE clips ADD COLUMN duration_s REAL;
    ALTER TABLE clips ADD COLUMN req_start_s REAL;     -- range asked of yt-dlp
    ALTER TABLE clips ADD COLUMN req_end_s REAL;
    ALTER TABLE clips ADD COLUMN format_id TEXT;
    ALTER TABLE clips ADD COLUMN frames_json TEXT NOT NULL DEFAULT '[]';
    """,
    # 5 — automation: job timing/messages, and streams the channel watcher has seen
    """
    ALTER TABLE jobs ADD COLUMN started_at TEXT;
    ALTER TABLE jobs ADD COLUMN finished_at TEXT;
    ALTER TABLE jobs ADD COLUMN message TEXT;              -- last outcome, incl. warnings

    CREATE TABLE discovered (
        video_id     TEXT PRIMARY KEY,
        channel_id   INTEGER REFERENCES channels(id) ON DELETE CASCADE,
        title        TEXT,
        duration_s   REAL,
        action       TEXT NOT NULL CHECK (action IN ('queued', 'skipped')),
        reason       TEXT,
        seen_at      TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    # 6 — per-job options, per-clip Drive uploads, and Q&A with Claude about a moment
    """
    ALTER TABLE jobs ADD COLUMN params_json TEXT;          -- e.g. {"model": "opus", "effort": "high"}
    ALTER TABLE clips ADD COLUMN uploaded_at TEXT;         -- when this clip's folder last went to Drive

    CREATE TABLE moment_chats (
        id          INTEGER PRIMARY KEY,
        moment_id   INTEGER NOT NULL REFERENCES moments(id) ON DELETE CASCADE,
        role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        text        TEXT NOT NULL,
        model       TEXT,
        usage_json  TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_moment_chats ON moment_chats(moment_id, id);

    -- Streams uploaded before this version: their clips are on Drive already.
    UPDATE clips SET uploaded_at = datetime('now') WHERE moment_id IN (
        SELECT m.id FROM moments m JOIN videos v ON v.id = m.video_id WHERE v.drive_url IS NOT NULL);
    """,
    # 7 — low-res previews: frames + text for Claude, made for every moment before you review
    """
    CREATE TABLE previews (
        moment_id    INTEGER PRIMARY KEY REFERENCES moments(id) ON DELETE CASCADE,
        folder       TEXT NOT NULL,                     -- data/vods/<id>/previews/<NN_label>/
        zero_s       REAL NOT NULL,                     -- stream time at preview time 0
        duration_s   REAL NOT NULL,
        height       INTEGER,
        frames_json  TEXT NOT NULL DEFAULT '[]',
        created_at   TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    # 8 — the dashboard editor: your cut of each clip, and a 'render' job to make it.
    # SQLite can't change a CHECK, so the jobs table is rebuilt with the new stage.
    """
    CREATE TABLE edits (
        moment_id        INTEGER PRIMARY KEY REFERENCES moments(id) ON DELETE CASCADE,
        segments_json    TEXT NOT NULL,       -- [[start_s, end_s, gain_db], …] stream time, play order
        source           TEXT,                -- what you started from: long | short | whole
        proposed_json    TEXT,                -- Claude's cut when you started (to compare with yours)
        rendered_json    TEXT,                -- the segments edit.mp4 was made from
        render_progress  REAL,                -- 0..1 while rendering
        rendered_at      TEXT,
        updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );

    CREATE TABLE jobs_new (
        id          INTEGER PRIMARY KEY,
        video_id    INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        moment_id   INTEGER REFERENCES moments(id) ON DELETE CASCADE,
        stage       TEXT NOT NULL
                    CHECK (stage IN ('ingest','score','clip','frames','analyze','digest','upload','render')),
        status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','done','error')),
        attempts    INTEGER NOT NULL DEFAULT 0,
        run_after   TEXT NOT NULL DEFAULT (datetime('now')),
        error       TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
        started_at  TEXT,
        finished_at TEXT,
        message     TEXT,
        params_json TEXT
    );
    INSERT INTO jobs_new (id, video_id, moment_id, stage, status, attempts, run_after, error,
                          created_at, updated_at, started_at, finished_at, message, params_json)
        SELECT id, video_id, moment_id, stage, status, attempts, run_after, error,
               created_at, updated_at, started_at, finished_at, message, params_json FROM jobs;
    DROP TABLE jobs;
    ALTER TABLE jobs_new RENAME TO jobs;
    CREATE UNIQUE INDEX uq_jobs_key ON jobs(video_id, stage, COALESCE(moment_id, 0));
    CREATE INDEX idx_jobs_queue ON jobs(status, stage, run_after);
    """,
    # 9 — sound effects: your library, and the effects placed in each edit
    """
    CREATE TABLE sounds (
        id           TEXT PRIMARY KEY,                -- short name Claude uses, e.g. 'faaah'
        name         TEXT NOT NULL,
        description  TEXT NOT NULL DEFAULT '',        -- when to use it (Claude reads this)
        tags         TEXT NOT NULL DEFAULT '',
        source       TEXT NOT NULL DEFAULT '',        -- where it's from, and its license
        file         TEXT NOT NULL,                   -- under data/sounds/
        duration_s   REAL NOT NULL,
        gain_db      REAL NOT NULL DEFAULT 0,         -- volume when placed (0 = about her voice)
        original     TEXT,                            -- the uploaded file's name
        added_at     TEXT NOT NULL DEFAULT (datetime('now'))
    );
    ALTER TABLE edits ADD COLUMN effects_json TEXT NOT NULL DEFAULT '[]';   -- stream time, like segments
    """,
    # 10 — named boxes on the picture (e.g. "face") that zoom effects aim at, drawn once per channel
    """
    CREATE TABLE regions (
        scope       TEXT NOT NULL,                    -- 'channel:<UC id>', or 'video:<id>' without a channel
        name        TEXT NOT NULL,                    -- 'face', 'game', …
        x REAL NOT NULL, y REAL NOT NULL,             -- top left, as fractions of the frame
        w REAL NOT NULL, h REAL NOT NULL,
        updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
        PRIMARY KEY (scope, name)
    );
    """,
    # 11 — a 'transcribe' job: Whisper speech to text on each downloaded clip (jobs rebuilt
    # for the new stage, as in 8)
    """
    CREATE TABLE jobs_new (
        id          INTEGER PRIMARY KEY,
        video_id    INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        moment_id   INTEGER REFERENCES moments(id) ON DELETE CASCADE,
        stage       TEXT NOT NULL
                    CHECK (stage IN ('ingest','score','clip','transcribe','frames','analyze','digest','upload','render')),
        status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','done','error')),
        attempts    INTEGER NOT NULL DEFAULT 0,
        run_after   TEXT NOT NULL DEFAULT (datetime('now')),
        error       TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
        started_at  TEXT,
        finished_at TEXT,
        message     TEXT,
        params_json TEXT
    );
    INSERT INTO jobs_new (id, video_id, moment_id, stage, status, attempts, run_after, error,
                          created_at, updated_at, started_at, finished_at, message, params_json)
        SELECT id, video_id, moment_id, stage, status, attempts, run_after, error,
               created_at, updated_at, started_at, finished_at, message, params_json FROM jobs;
    DROP TABLE jobs;
    ALTER TABLE jobs_new RENAME TO jobs;
    CREATE UNIQUE INDEX uq_jobs_key ON jobs(video_id, stage, COALESCE(moment_id, 0));
    CREATE INDEX idx_jobs_queue ON jobs(status, stage, run_after);
    """,
    # 12 — subtitles: your edited lines (stream time; NULL = built from Whisper's words) and their style
    """
    ALTER TABLE edits ADD COLUMN subs_json TEXT;
    ALTER TABLE edits ADD COLUMN subs_style_json TEXT;
    """,
    # 13 — names Claude must spell right in subtitles (VTubers, games, catchphrases), per channel
    """
    CREATE TABLE names (
        scope       TEXT PRIMARY KEY,                 -- 'channel:<UC id>', or 'video:<id>' (as regions)
        terms_json  TEXT NOT NULL DEFAULT '[]',
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    # 14 — the brand intro before an edit, and the clip's thumbnail; an 'intro' job to render
    # the intro on its own (jobs rebuilt for the new stage, as in 8)
    """
    CREATE TABLE intros (
        moment_id      INTEGER PRIMARY KEY REFERENCES moments(id) ON DELETE CASCADE,
        on_edit        INTEGER NOT NULL DEFAULT 0,   -- edit.mp4 starts with the intro
        teasers_json   TEXT,                         -- [[start, end, gain_db, style]] stream time; NULL = auto-picked
        rendered_json  TEXT,                         -- what intro.mp4 was made from
        progress       REAL,
        rendered_at    TEXT,
        updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE thumbs (
        moment_id      INTEGER PRIMARY KEY REFERENCES moments(id) ON DELETE CASCADE,
        settings_json  TEXT NOT NULL,                -- frame (stream time), text, zoom, style…
        made_at        TEXT
    );
    CREATE TABLE jobs_new (
        id          INTEGER PRIMARY KEY,
        video_id    INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        moment_id   INTEGER REFERENCES moments(id) ON DELETE CASCADE,
        stage       TEXT NOT NULL
                    CHECK (stage IN ('ingest','score','clip','transcribe','frames','analyze','digest','upload',
                                     'render','intro')),
        status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','done','error')),
        attempts    INTEGER NOT NULL DEFAULT 0,
        run_after   TEXT NOT NULL DEFAULT (datetime('now')),
        error       TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
        started_at  TEXT,
        finished_at TEXT,
        message     TEXT,
        params_json TEXT
    );
    INSERT INTO jobs_new (id, video_id, moment_id, stage, status, attempts, run_after, error,
                          created_at, updated_at, started_at, finished_at, message, params_json)
        SELECT id, video_id, moment_id, stage, status, attempts, run_after, error,
               created_at, updated_at, started_at, finished_at, message, params_json FROM jobs;
    DROP TABLE jobs;
    ALTER TABLE jobs_new RENAME TO jobs;
    CREATE UNIQUE INDEX uq_jobs_key ON jobs(video_id, stage, COALESCE(moment_id, 0));
    CREATE INDEX idx_jobs_queue ON jobs(status, stage, run_after);
    """,
    # 15 — Shorts: a 9:16 edit of the clip next to the long one (its own cut, effects and
    # subtitles, plus a layout), and a 'short' job to render it (jobs rebuilt, as in 8)
    """
    CREATE TABLE shorts (
        moment_id        INTEGER PRIMARY KEY REFERENCES moments(id) ON DELETE CASCADE,
        segments_json    TEXT NOT NULL,       -- like edits: stream time, play order
        source           TEXT,
        proposed_json    TEXT,
        effects_json     TEXT NOT NULL DEFAULT '[]',
        subs_json        TEXT,
        subs_style_json  TEXT,
        layout_json      TEXT,                -- {type: crop|stack|fit, x, y, scale, top, bottom, split, title}
        rendered_json    TEXT,                -- what short.mp4 was made from
        render_progress  REAL,
        rendered_at      TEXT,
        updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE jobs_new (
        id          INTEGER PRIMARY KEY,
        video_id    INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        moment_id   INTEGER REFERENCES moments(id) ON DELETE CASCADE,
        stage       TEXT NOT NULL
                    CHECK (stage IN ('ingest','score','clip','transcribe','frames','analyze','digest','upload',
                                     'render','intro','short')),
        status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','done','error')),
        attempts    INTEGER NOT NULL DEFAULT 0,
        run_after   TEXT NOT NULL DEFAULT (datetime('now')),
        error       TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
        started_at  TEXT,
        finished_at TEXT,
        message     TEXT,
        params_json TEXT
    );
    INSERT INTO jobs_new (id, video_id, moment_id, stage, status, attempts, run_after, error,
                          created_at, updated_at, started_at, finished_at, message, params_json)
        SELECT id, video_id, moment_id, stage, status, attempts, run_after, error,
               created_at, updated_at, started_at, finished_at, message, params_json FROM jobs;
    DROP TABLE jobs;
    ALTER TABLE jobs_new RENAME TO jobs;
    CREATE UNIQUE INDEX uq_jobs_key ON jobs(video_id, stage, COALESCE(moment_id, 0));
    CREATE INDEX idx_jobs_queue ON jobs(status, stage, run_after);
    """,
    # 16 — the Short's cover (1080×1920), next to the thumbnail's settings
    """
    ALTER TABLE thumbs ADD COLUMN short_json TEXT;      -- like settings_json, for short_cover.jpg
    ALTER TABLE thumbs ADD COLUMN short_made_at TEXT;
    """,
    # 17 — who's talking: each clip's voices named and coloured, and each person's colour
    # remembered per channel (subs.speakers)
    """
    CREATE TABLE voices (
        moment_id   INTEGER PRIMARY KEY REFERENCES moments(id) ON DELETE CASCADE,
        voices_json TEXT NOT NULL,                  -- {"0": {"name": "Gigi", "color": "FF8FC8"}, ...}
        updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE TABLE people (
        scope TEXT NOT NULL,                        -- like regions: the channel
        name  TEXT NOT NULL,                        -- lower case
        color TEXT NOT NULL,
        PRIMARY KEY (scope, name)
    );
    """,
    # 18 — one colour per VTuber, for every channel (the VTubers page): replaces the
    # per-channel memory of 17
    """
    CREATE TABLE vtubers (
        key        TEXT PRIMARY KEY,                -- the name in lower case
        name       TEXT NOT NULL,                   -- as you write it
        color      TEXT NOT NULL,                   -- RRGGBB
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    INSERT OR IGNORE INTO vtubers (key, name, color)
        SELECT name, upper(substr(name, 1, 1)) || substr(name, 2), color FROM people;
    DROP TABLE people;
    """,
    # 19 — the VTubers list from a CSV: Japanese name (matches Japanese subtitles), the
    # accent colour, active / alum, profile page
    """
    ALTER TABLE vtubers ADD COLUMN japanese_name TEXT NOT NULL DEFAULT '';
    ALTER TABLE vtubers ADD COLUMN accent TEXT NOT NULL DEFAULT '';
    ALTER TABLE vtubers ADD COLUMN status TEXT NOT NULL DEFAULT '';
    ALTER TABLE vtubers ADD COLUMN profile_url TEXT NOT NULL DEFAULT '';
    """,
    # 20 — directing the AI edit: one Claude session per clip edit (long / short), and its
    # messages. Replaces the Q&A of 6 (Ask Claude was removed).
    """
    DROP TABLE moment_chats;
    CREATE TABLE edit_chats (
        id          INTEGER PRIMARY KEY,
        moment_id   INTEGER NOT NULL REFERENCES moments(id) ON DELETE CASCADE,
        kind        TEXT NOT NULL CHECK (kind IN ('long', 'short')),
        session_id  TEXT NOT NULL,                  -- the claude CLI session it belongs to
        role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        text        TEXT NOT NULL,
        applied     INTEGER NOT NULL DEFAULT 0,     -- assistant: it changed the edit
        model       TEXT,
        usage_json  TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_edit_chats ON edit_chats(moment_id, kind, id);
    """,
    # 21 — green-screen overlays (meme clips like the LoL "?" ping spam): keyed to a
    # transparent video when added, placed on an edit like a sound
    """
    CREATE TABLE overlays (
        id           TEXT PRIMARY KEY,                -- short name Claude uses, e.g. 'question_pings'
        name         TEXT NOT NULL,
        description  TEXT NOT NULL DEFAULT '',        -- when to use it (Claude reads this)
        tags         TEXT NOT NULL DEFAULT '',
        source       TEXT NOT NULL DEFAULT '',        -- where it's from, and its license
        file         TEXT NOT NULL,                   -- under data/overlays/: VP9 with transparency
        sound        TEXT,                            -- its sound, levelled (NULL when silent)
        duration_s   REAL NOT NULL,
        width        INTEGER NOT NULL,
        height       INTEGER NOT NULL,
        key_color    TEXT NOT NULL DEFAULT '',        -- RRGGBB that was made transparent
        gain_db      REAL NOT NULL DEFAULT 0,         -- its sound's volume when placed
        original     TEXT,
        added_at     TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    # 22 — compilations: what one Claude read of a whole stream found (compile/), and a
    # 'compile' job to run it (jobs rebuilt, as in 8)
    """
    CREATE TABLE compile_runs (
        id          INTEGER PRIMARY KEY,
        video_id    INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        model       TEXT,
        input_json  TEXT NOT NULL,              -- size of what was sent: chars, est_tokens, lines
        usage_json  TEXT,                       -- tokens the CLI reported
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    );
    CREATE INDEX idx_compile_runs_video ON compile_runs(video_id);
    CREATE TABLE compilations (
        id            INTEGER PRIMARY KEY,
        run_id        INTEGER NOT NULL REFERENCES compile_runs(id) ON DELETE CASCADE,
        video_id      INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        position      INTEGER NOT NULL,         -- 1 = best
        title         TEXT NOT NULL,
        kind          TEXT NOT NULL CHECK (kind IN ('recurring', 'callback', 'other')),
        reason        TEXT NOT NULL DEFAULT '',
        rating        INTEGER NOT NULL,         -- 1-10
        segments_json TEXT NOT NULL             -- play order: [{start, end, role, note}], stream seconds
    );
    CREATE INDEX idx_compilations_video ON compilations(video_id);
    CREATE TABLE jobs_new (
        id          INTEGER PRIMARY KEY,
        video_id    INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
        moment_id   INTEGER REFERENCES moments(id) ON DELETE CASCADE,
        stage       TEXT NOT NULL
                    CHECK (stage IN ('ingest','score','clip','transcribe','frames','analyze','digest','upload',
                                     'render','intro','short','compile')),
        status      TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','running','done','error')),
        attempts    INTEGER NOT NULL DEFAULT 0,
        run_after   TEXT NOT NULL DEFAULT (datetime('now')),
        error       TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
        started_at  TEXT,
        finished_at TEXT,
        message     TEXT,
        params_json TEXT
    );
    INSERT INTO jobs_new (id, video_id, moment_id, stage, status, attempts, run_after, error,
                          created_at, updated_at, started_at, finished_at, message, params_json)
        SELECT id, video_id, moment_id, stage, status, attempts, run_after, error,
               created_at, updated_at, started_at, finished_at, message, params_json FROM jobs;
    DROP TABLE jobs;
    ALTER TABLE jobs_new RENAME TO jobs;
    CREATE UNIQUE INDEX uq_jobs_key ON jobs(video_id, stage, COALESCE(moment_id, 0));
    CREATE INDEX idx_jobs_queue ON jobs(status, stage, run_after);
    """,
    # 23 — inserts: pictures and videos from outside the stream (an X post, another video)
    # kept with one clip, as overlays without a key colour (edit/inserts.py)
    """
    ALTER TABLE overlays ADD COLUMN moment_id INTEGER REFERENCES moments(id) ON DELETE CASCADE;
    CREATE INDEX idx_overlays_moment ON overlays(moment_id);
    """,
]

SCHEMA_VERSION = len(MIGRATIONS)


def connect(db_path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """Open a connection with the pragmas every caller needs.

    check_same_thread=False is for the web app, where one request's connection may be
    opened, used and closed on different threads (never concurrently)."""
    path = Path(db_path)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None,  # autocommit; use tx()
                           check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")  # web + worker read/write together
    return conn


@contextmanager
def tx(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Run a block in one transaction (BEGIN IMMEDIATE: take the write lock up front)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def schema_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply pending migrations. Returns the versions applied (empty if up to date)."""
    current = schema_version(conn)
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"Database schema v{current} is newer than this code (v{SCHEMA_VERSION}). "
            "Update chatwatcher."
        )
    applied: list[int] = []
    for version in range(current + 1, SCHEMA_VERSION + 1):
        script = MIGRATIONS[version - 1]
        # executescript() commits first, so wrap the script and version bump ourselves.
        try:
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
    return applied


def open_db(db_path: str | Path) -> sqlite3.Connection:
    """Connect and bring the schema up to date."""
    conn = connect(db_path)
    migrate(conn)
    return conn


def table_counts(conn: sqlite3.Connection) -> dict[str, int]:
    names = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
            "ORDER BY name"
        )
    ]
    return {n: conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in names}
