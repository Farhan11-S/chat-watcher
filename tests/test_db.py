import sqlite3

import pytest

from chatwatcher import db


@pytest.fixture
def conn(tmp_path):
    c = db.open_db(tmp_path / "t.db")
    yield c
    c.close()


def test_migrate_creates_schema_and_is_idempotent(tmp_path):
    c = db.connect(tmp_path / "t.db")
    assert db.migrate(c) == list(range(1, db.SCHEMA_VERSION + 1))
    assert db.migrate(c) == []
    assert db.schema_version(c) == db.SCHEMA_VERSION
    assert set(db.table_counts(c)) == {
        "channels", "videos", "moments", "clips", "analyses", "jobs", "settings", "discovered",
    }
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    c.close()


def test_newer_schema_refused(tmp_path):
    c = db.open_db(tmp_path / "t.db")
    c.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError):
        db.migrate(c)
    c.close()


def _video(conn, vid="abcdefghijk"):
    return conn.execute("INSERT INTO videos (video_id) VALUES (?)", (vid,)).lastrowid


def test_job_key_unique_including_null_moment(conn):
    v = _video(conn)
    conn.execute("INSERT INTO jobs (video_id, stage) VALUES (?, 'ingest')", (v,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (video_id, stage) VALUES (?, 'ingest')", (v,))

    m = conn.execute(
        "INSERT INTO moments (video_id, rank, score, start_s, end_s, peak_s) VALUES (?,1,3.0,10,40,30)",
        (v,),
    ).lastrowid
    conn.execute("INSERT INTO jobs (video_id, moment_id, stage) VALUES (?, ?, 'clip')", (v, m))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO jobs (video_id, moment_id, stage) VALUES (?, ?, 'clip')", (v, m))


def test_checks_and_cascade(conn):
    v = _video(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE videos SET stream_type = 'asmr' WHERE id = ?", (v,))
    with pytest.raises(sqlite3.IntegrityError):  # end before start
        conn.execute(
            "INSERT INTO moments (video_id, rank, score, start_s, end_s, peak_s) VALUES (?,1,1,50,40,45)",
            (v,),
        )
    conn.execute(
        "INSERT INTO moments (video_id, rank, score, start_s, end_s, peak_s) VALUES (?,1,1,10,40,30)",
        (v,),
    )
    conn.execute("DELETE FROM videos WHERE id = ?", (v,))
    assert conn.execute("SELECT COUNT(*) FROM moments").fetchone()[0] == 0


def test_tx_rolls_back(conn):
    with pytest.raises(ValueError):
        with db.tx(conn):
            _video(conn)
            raise ValueError
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 0
    with db.tx(conn):
        _video(conn)
    assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 1
