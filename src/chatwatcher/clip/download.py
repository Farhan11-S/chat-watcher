"""Download one time range of a VOD at the best original quality.

yt-dlp cuts with stream copy, so the cut lands on keyframes and some formats bring an
audio-only lead-in (audio starts on an earlier cluster than the video keyframe). We
measure where the file really starts in stream time and losslessly trim that lead-in.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from chatwatcher.clip.media import MediaError, probe, remux
from chatwatcher.config import ClipConfig
from chatwatcher.ingest.ytdlp import LineFn, YtDlp, YtDlpError

_VIDEO_EXTS = (".mp4", ".mkv", ".webm", ".mov")
_RETRYABLE_KINDS = ("error", "no_file", "timeout", "network")


def _attempts(cfg: ClipConfig) -> list[tuple[str, str]]:
    """Configured choice first, then the DASH (non-HLS) variant of the same quality tier,
    then the best single pre-merged file. Some HLS+audio combinations make yt-dlp's
    ffmpeg section cut fail."""
    out: list[tuple[str, str]] = []
    for a in ((cfg.format, cfg.sort), (cfg.format, "res,fps,proto,br"), ("bv*+ba/b", "res,fps,proto,br"), ("b", cfg.sort)):
        if a not in out:
            out.append(a)
    return out


@dataclass
class ClipFile:
    path: Path
    zero_s: float          # stream time (seconds) at file time 0
    duration_s: float
    width: int | None
    height: int | None
    fps: float | None
    vcodec: str | None
    acodec: str | None
    size_bytes: int
    format_id: str | None = None

    @property
    def container(self) -> str:
        return self.path.suffix.lstrip(".")

    def to_clip_time(self, stream_s: float) -> float:
        return round(stream_s - self.zero_s, 3)


def download_clip(yt: YtDlp, info_json: Path, start_s: float, end_s: float, dest_stem: Path,
                  cfg: ClipConfig, *, on_line: LineFn | None = None) -> ClipFile:
    """Download [start_s, end_s] into dest_stem.<mp4|mkv>. info_json is a full yt-dlp -J
    dump (reused so each clip doesn't re-fetch the watch page)."""
    if end_s <= start_s:
        raise ValueError(f"Bad range {start_s}-{end_s}")
    tmp = dest_stem.parent / f".dl_{dest_stem.name}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    format_id: list[str] = []

    def _line(line: str) -> None:
        if line.startswith("CWFMT "):
            format_id.append(line[6:].strip())
        elif on_line:
            on_line(line)

    try:
        raw = None
        errors: list[str] = []
        for fmt, sort in _attempts(cfg):
            for old in tmp.iterdir():
                old.unlink()
            try:
                yt.run(
                    ["--load-info-json", str(info_json), "-f", fmt, "-S", sort,
                     "--download-sections", f"*{start_s:.2f}-{end_s:.2f}",
                     "--merge-output-format", cfg.merge_output_format,
                     "--print", "before_dl:CWFMT %(format_id)s", "--no-simulate",
                     "--newline", "-P", str(tmp), "-o", "clip.%(ext)s"],
                    timeout=max(900.0, (end_s - start_s) * 10), on_line=_line,
                )
                files = [p for p in tmp.iterdir() if p.suffix in _VIDEO_EXTS and p.stem == "clip"]
                if not files:
                    raise YtDlpError("yt-dlp finished but wrote no clip file", kind="no_file")
                pr = probe(files[0])
                raw = files[0]
                break
            except (YtDlpError, MediaError) as e:
                if isinstance(e, YtDlpError) and e.kind not in _RETRYABLE_KINDS:
                    raise  # bot check, members-only, …: another format won't help
                errors.append(f"[-f {fmt} -S {sort}] {str(e).strip().splitlines()[-1][:200]}")
                format_id.clear()
        if raw is None:
            raise YtDlpError("All download attempts failed:\n" + "\n".join(errors), kind="error")
        # The end of a copy-cut is exact, so the start in stream time is end - duration.
        zero = end_s - pr.duration_s
        lead_in = pr.first_video_pts if pr.first_video_pts > 0.5 else 0.0
        for old in dest_stem.parent.glob(f"{dest_stem.name}.*"):  # earlier download
            if old.suffix in _VIDEO_EXTS:
                old.unlink()
        if lead_in or raw.suffix != ".mp4":
            final = remux(raw, dest_stem, trim_start=lead_in)
            zero += lead_in
            pr = probe(final)
        else:
            final = dest_stem.with_suffix(".mp4")
            raw.replace(final)
        return ClipFile(
            path=final, zero_s=round(zero, 3), duration_s=pr.duration_s, width=pr.width,
            height=pr.height, fps=pr.fps, vcodec=pr.vcodec, acodec=pr.acodec,
            size_bytes=pr.size_bytes, format_id=format_id[0] if format_id else None,
        )
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
