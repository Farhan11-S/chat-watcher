import stat

import pytest

from chatwatcher.config import YouTubeConfig
from chatwatcher.ingest.ytdlp import YtDlp, YtDlpError, classify_error, extract_video_id

VID = "dQw4w9WgXcQ"


@pytest.mark.parametrize("text", [
    VID,
    f"https://www.youtube.com/watch?v={VID}",
    f"https://www.youtube.com/watch?feature=share&v={VID}&t=120s",
    f"https://youtu.be/{VID}?si=abc",
    f"https://www.youtube.com/live/{VID}?feature=share",
    f"https://youtube.com/shorts/{VID}",
    f"https://www.youtube.com/embed/{VID}",
    f"  {VID}  ",
])
def test_extract_video_id(text):
    assert extract_video_id(text) == VID


@pytest.mark.parametrize("text", ["", "abc", "https://www.youtube.com/@channel",
                                  f"https://www.youtube.com/watch?v={VID}X"])
def test_extract_video_id_rejects(text):
    with pytest.raises(ValueError):
        extract_video_id(text)


@pytest.mark.parametrize("output,kind,retryable", [
    ("ERROR: [youtube] x: Sign in to confirm you're not a bot.", "bot_check", True),
    ("ERROR: Join this channel to get access to members-only content", "members_only", False),
    ("ERROR: [youtube] x: Private video. Sign in", "private", False),
    ("ERROR: [youtube] x: Video unavailable", "unavailable", False),
    ("ERROR: This live event will begin in 3 hours.", "not_started", True),
    ("ERROR: HTTP Error 429: Too Many Requests", "rate_limited", True),
    ("ERROR: Requested format is not available", "error", False),
])
def test_classify_error(output, kind, retryable):
    assert classify_error(output)[:2] == (kind, retryable)


def _script(tmp_path, body):
    p = tmp_path / "fake-yt-dlp"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return YtDlp(YouTubeConfig(ytdlp_path=str(p)))


def test_missing_executable(tmp_path):
    with pytest.raises(YtDlpError) as e:
        YtDlp(YouTubeConfig(ytdlp_path=str(tmp_path / "nope")))
    assert e.value.kind == "missing"


def test_common_args(tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("# Netscape")
    deno = tmp_path / "deno"
    deno.write_text("")
    yt = _script(tmp_path, "exit 0\n")
    yt.cfg = YouTubeConfig(cookies_file=str(cookies), po_token="TOKEN", deno_path=str(deno),
                           extra_args=["--proxy", "x"])
    args = yt.common_args()
    assert args[:2] == ["--ignore-config", "--no-playlist"]
    assert ["--cookies", str(cookies)] == args[2:4]
    assert "youtube:po_token=web+TOKEN" in args
    assert f"deno:{deno}" in args
    assert args[-2:] == ["--proxy", "x"]

    yt.cfg = YouTubeConfig(cookies_file=str(tmp_path / "missing.txt"))
    with pytest.raises(YtDlpError) as e:
        yt.common_args()
    assert e.value.kind == "config"


def test_run_streams_lines_and_classifies_failure(tmp_path):
    yt = _script(tmp_path, 'echo "[download] 10%"\necho "[download] 50%"\nexit 0\n')
    seen = []
    yt.run(["x"], timeout=10, on_line=seen.append)
    assert seen == ["[download] 10%", "[download] 50%"]

    yt = _script(tmp_path, "echo \"ERROR: Sign in to confirm you're not a bot\" >&2\nexit 1\n")
    with pytest.raises(YtDlpError) as e:
        yt.run(["x"], timeout=10)
    assert e.value.kind == "bot_check" and e.value.retryable
    assert "cookies" in str(e.value)


def test_run_timeout(tmp_path):
    yt = _script(tmp_path, "sleep 5\n")
    with pytest.raises(YtDlpError) as e:
        yt.run(["x"], timeout=0.5)
    assert e.value.kind == "timeout"


def test_info_parses_json(tmp_path):
    yt = _script(tmp_path, "echo '{\"id\": \"abc\", \"live_status\": \"was_live\"}'\n")
    assert yt.info(VID) == {"id": "abc", "live_status": "was_live"}

    yt = _script(tmp_path, "echo 'ERROR: [youtube] x: Video unavailable' >&2\nexit 1\n")
    with pytest.raises(YtDlpError) as e:
        yt.info(VID)
    assert e.value.kind == "unavailable"
