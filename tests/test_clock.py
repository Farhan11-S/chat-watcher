from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from chatwatcher import clock, db
from chatwatcher.analyze.service import analyses_today
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


def test_daily_cap_counts_the_local_day(tmp_path):
    conn = db.open_db(tmp_path / "t.db")
    conn.execute("INSERT INTO videos (id, video_id) VALUES (1, 'abcdefghijk')")
    conn.execute("INSERT INTO moments (id, video_id, rank, score, start_s, end_s, peak_s) VALUES (1, 1, 1, 1, 0, 9, 5)")
    start = datetime.fromisoformat(clock.today_start_utc(WIB)).replace(tzinfo=timezone.utc)
    assert start.astimezone(WIB).strftime("%H:%M") == "00:00"
    for t in (start - timedelta(minutes=1), start + timedelta(minutes=1)):           # yesterday WIB, today WIB
        conn.execute("INSERT INTO analyses (video_id, moment_id, analysis_json, created_at) VALUES (1, 1, '{}', ?)",
                     (t.strftime("%Y-%m-%d %H:%M:%S"),))
    assert analyses_today(conn, WIB) == 1
    left = clock.until_midnight(WIB)
    assert timedelta(minutes=5) < left <= timedelta(days=1, minutes=5)
    assert (datetime.now(timezone.utc) + left).astimezone(WIB).strftime("%H:%M") in ("00:05", "00:04")
    conn.close()
