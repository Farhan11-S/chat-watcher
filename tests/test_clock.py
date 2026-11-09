from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from chatwatcher import clock, db
from chatwatcher.analyze.service import cap_wait, recent_analyses
from chatwatcher.config import Config, GeneralConfig

WIB = ZoneInfo("Asia/Jakarta")


def test_fmt_converts_stored_utc_to_wib():
    assert clock.fmt("2026-11-08 05:51:21", WIB) == "2026-11-08 12:51 WIB"          # SQLite datetime('now')
    assert clock.fmt("2026-11-07T18:30:00Z", WIB) == "2026-11-08 01:30 WIB"         # ISO from yt-dlp ingest
    assert clock.fmt("2026-11-07T18:30:00Z", WIB, date_only=True) == "2026-11-08"
    assert clock.fmt("2026-11-08 05:51:21", ZoneInfo("Asia/Tokyo")) == "2026-11-08 14:51 JST"
    assert clock.fmt("2026-11-08 05:51:21", timezone(timedelta(hours=5, minutes=30))) == "2026-11-08 11:21 UTC+05:30"
    assert clock.fmt(None, WIB) == "" and clock.fmt("never", WIB) == "never"
    assert clock.zone_name(WIB) == "WIB"


def test_config_timezone():
    assert clock.zone(Config()) is None                                            # this computer's zone
    assert clock.zone(Config(general=GeneralConfig(timezone="Asia/Jakarta"))) == WIB
    with pytest.raises(ValueError, match="unknown time zone"):
        GeneralConfig(timezone="Mars/Olympus")


def test_cap_counts_the_last_5_hours(tmp_path):
    conn = db.open_db(tmp_path / "t.db")
    conn.execute("INSERT INTO videos (id, video_id) VALUES (1, 'abcdefghijk')")
    conn.execute("INSERT INTO moments (id, video_id, rank, score, start_s, end_s, peak_s) VALUES (1, 1, 1, 1, 0, 9, 5)")
    now = datetime(2026, 11, 8, 12, 0, tzinfo=timezone.utc)
    for ago in (timedelta(hours=6), timedelta(hours=4), timedelta(hours=1), timedelta(minutes=10)):
        conn.execute("INSERT INTO analyses (video_id, moment_id, analysis_json, created_at) VALUES (1, 1, '{}', ?)",
                     ((now - ago).strftime("%Y-%m-%d %H:%M:%S"),))
    assert len(recent_analyses(conn, 5, now)) == 3                 # the 6-hour-old one is out of the window
    cfg = Config()
    cfg.claude.moment_cap, cfg.claude.moment_cap_hours = 4, 5
    assert cap_wait(cfg, conn, now) == timedelta(0)                # 3 of 4 used
    cfg.claude.moment_cap = 3
    assert cap_wait(cfg, conn, now) == timedelta(hours=1, minutes=2)    # the 4-hour-old one leaves in 1 h
    cfg.claude.moment_cap = 2                                      # lowered below what's used: two must leave
    assert cap_wait(cfg, conn, now) == timedelta(hours=4, minutes=2)
    cfg.claude.moment_cap = 0                                      # no cap
    assert cap_wait(cfg, conn, now) == timedelta(0)
    conn.close()
