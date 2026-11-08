"""`cw` — debug/ops CLI. The dashboard is the main interface; this drives stages by hand."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Optional

import typer

from chatwatcher import __version__, db, store
from chatwatcher.config import Config, load_config
from chatwatcher.doctor import run_checks

app = typer.Typer(
    help="Chat Watcher v2 — find, clip and storyboard VTuber stream moments.",
    no_args_is_help=True,
    add_completion=False,
)

ConfigOpt = Annotated[
    Optional[Path],
    typer.Option("--config", "-c", help="Path to config.toml (default: $CHATWATCHER_CONFIG or ./config.toml)."),
]

_ICON = {"ok": "✅", "warn": "⚠️ ", "fail": "❌"}

# YouTube ids can start with "-" (e.g. -k8EZv9wkAs); let such words through as the video
# argument instead of failing as an unknown option.
_VIDEO_ARG = {"ignore_unknown_options": True}


def _load(config: Path | None) -> Config:
    try:
        return load_config(config)
    except FileNotFoundError as e:
        typer.secho(str(e), fg="red", err=True)
        raise typer.Exit(2)
    except Exception as e:  # tomllib / pydantic errors
        typer.secho(f"Invalid config: {e}", fg="red", err=True)
        raise typer.Exit(2)


@app.callback(invoke_without_command=True)
def _main(
    version: Annotated[bool, typer.Option("--version", help="Show version and exit.")] = False,
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit()


@app.command()
def init(config: ConfigOpt = None) -> None:
    """Create data/output folders and the database (safe to re-run)."""
    cfg = _load(config)
    cfg.paths.data_dir.mkdir(parents=True, exist_ok=True)
    cfg.paths.output_dir.mkdir(parents=True, exist_ok=True)
    conn = db.connect(cfg.paths.db_path)
    try:
        applied = db.migrate(conn)
        version = db.schema_version(conn)
    finally:
        conn.close()
    if applied:
        typer.echo(f"Database ready at {cfg.paths.db_path} (applied migration(s) {applied}, now v{version}).")
    else:
        typer.echo(f"Database already up to date at {cfg.paths.db_path} (v{version}).")


@app.command("config")
def show_config(config: ConfigOpt = None) -> None:
    """Print the effective settings as TOML-like text."""
    cfg = _load(config)
    typer.echo(f"# source: {cfg.source or 'defaults (no config file)'}")
    for section, values in cfg.model_dump(mode="json").items():
        typer.echo(f"\n[{section}]")
        for key, value in values.items():
            typer.echo(f"{key} = {value!r}")


@app.command()
def doctor(config: ConfigOpt = None) -> None:
    """Check required tools (yt-dlp, ffmpeg, deno, claude, rclone) and settings."""
    cfg = _load(config)
    checks = run_checks(cfg)
    width = max(len(c.name) for c in checks)
    for c in checks:
        typer.echo(f"{_ICON[c.level]} {c.name:<{width}}  {c.detail}")
    if any(c.level == "fail" for c in checks):
        raise typer.Exit(1)


def _fmt_duration(seconds: float | None) -> str:
    if not seconds:
        return "?"
    s = int(seconds)
    return f"{s // 3600}h{s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m{s % 60:02d}s"


@app.command(context_settings=_VIDEO_ARG)
def ingest(
    url: Annotated[str, typer.Argument(help="VOD URL or 11-character video id.")],
    force: Annotated[bool, typer.Option("--force", help="Re-download chat and captions.")] = False,
    no_captions: Annotated[bool, typer.Option("--no-captions", help="Skip captions.")] = False,
    config: ConfigOpt = None,
) -> None:
    """Fetch a VOD's info, chat replay, captions and replay heatmap."""
    from chatwatcher.ingest.service import IngestError, ingest_video

    cfg = _load(config)
    conn = db.open_db(cfg.paths.db_path)
    progress_shown = False

    def on_step(msg: str) -> None:
        nonlocal progress_shown
        if progress_shown:
            typer.echo()
            progress_shown = False
        typer.echo(f"  {msg}")

    def on_progress(line: str) -> None:
        nonlocal progress_shown
        if line.startswith("[download]") and ("%" in line or "frag" in line):
            typer.echo(f"\r    {line[:100]:<100}", nl=False)
            progress_shown = True

    try:
        r = ingest_video(cfg, conn, url, force=force, want_captions=not no_captions,
                         on_step=on_step, on_progress=on_progress)
    except IngestError as e:
        if progress_shown:
            typer.echo()
        typer.secho(f"❌ {e}", fg="red", err=True)
        if e.retryable:
            typer.secho("   (temporary — safe to retry later)", fg="yellow", err=True)
        raise typer.Exit(1)
    finally:
        conn.close()

    if progress_shown:
        typer.echo()
    typer.echo(f"\n🎬 {r.title}")
    typer.echo(f"   {r.channel or '?'} · {r.live_status or '?'} · {_fmt_duration(r.duration_s)}")
    if r.chat:
        c = r.chat
        paid = c["by_kind"].get("superchat", 0) + c["by_kind"].get("sticker", 0)
        typer.echo(f"💬 {c['messages']:,} messages · {c['unique_authors']:,} chatters · "
                   f"{paid:,} super chats/stickers" + ("  (reused)" if r.chat_reused else ""))
    else:
        typer.echo("💬 no chat replay")
    if r.caption_lang:
        typer.echo(f"📝 captions {r.caption_lang}{' (auto)' if r.caption_auto else ''} · "
                   f"{r.caption_segments:,} lines")
    else:
        typer.echo("📝 no captions")
    typer.echo(f"🔥 heatmap {r.heatmap_points} points" if r.heatmap_points else "🔥 no replay heatmap")
    for w in r.warnings:
        typer.secho(f"⚠️  {w}", fg="yellow")
    typer.echo(f"✅ saved to {r.files.root}")


_SIGNAL_LABEL = {
    "rate": "chat rate", "chatters": "chatters", "emotes": "emote spam", "laugh": "laughs",
    "hype": "hype", "surprise": "surprise", "applause": "applause", "cute": "cute/てぇてぇ",
    "emotion": "emotional", "clip": "\"clip it\"", "money": "super chats", "replay": "rewatched",
}


@app.command(context_settings=_VIDEO_ARG)
def score(
    url: Annotated[str, typer.Argument(help="VOD URL or video id (must be ingested).")],
    stream_type: Annotated[Optional[str], typer.Option(
        "--type", "-t", help="Override detection: talk, gaming or karaoke.")] = None,
    top: Annotated[Optional[int], typer.Option("--top", "-n", help="How many moments to keep.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Replace moments that already have clips or reviews.")] = False,
    config: ConfigOpt = None,
) -> None:
    """Find and rank the best moments of an ingested VOD."""
    from chatwatcher.scoring.moments import fmt_ts
    from chatwatcher.scoring.service import ScoreError, score_video

    cfg = _load(config)
    if top:
        cfg.scoring.top_n = top
    conn = db.open_db(cfg.paths.db_path)
    try:
        r = score_video(cfg, conn, url, stream_type=stream_type, force=force)
    except ScoreError as e:
        typer.secho(f"❌ {e}", fg="red", err=True)
        raise typer.Exit(1)
    finally:
        conn.close()

    icon = {"talk": "💬", "gaming": "🎮", "karaoke": "🎤"}[r.stream_type]
    typer.echo(f"\n🎬 {r.title}")
    typer.echo(f"{icon} {r.stream_type} ({r.type_reason}) · {len(r.moments)} moments\n")
    for m in r.moments:
        tag = "🎵 song" if m.kind == "song" else ""
        why = " · ".join(f"{_SIGNAL_LABEL.get(x['signal'], x['signal'])} {x['z']}"
                         for x in m.reasons[:3])
        typer.echo(f"{m.rank:>2}. {fmt_ts(m.start_s)}–{fmt_ts(m.end_s)}  "
                   f"({int(m.length_s)}s)  score {m.score:.2f}  {tag}")
        typer.echo(f"    why : {why}")
        if m.top_messages:
            chat = ", ".join(f"{t[:18]}×{n}" for t, n in m.top_messages[:4])
            typer.echo(f"    chat: {chat}")
        typer.echo(f"    https://youtu.be/{r.video_id}?t={int(m.start_s)}")
    for w in r.warnings:
        typer.secho(f"⚠️  {w}", fg="yellow")
    typer.echo(f"\n✅ saved {r.files.moments.name} and {r.files.scores.name} in {r.files.root}")


def _parse_ranks(text: str | None) -> list[int] | None:
    if not text:
        return None
    try:
        return sorted({int(x) for x in text.replace(" ", "").split(",") if x})
    except ValueError:
        raise typer.BadParameter("use comma-separated numbers, e.g. 1,2,5")


def _mb(n: int | None) -> str:
    return f"{(n or 0) / 1_048_576:.0f}MB"


@app.command(context_settings=_VIDEO_ARG)
def clip(
    url: Annotated[str, typer.Argument(help="VOD URL or video id (must be scored).")],
    ranks: Annotated[Optional[str], typer.Option("--ranks", "-r", help="Only these ranks, e.g. 1,2,5.")] = None,
    top: Annotated[Optional[int], typer.Option("--top", "-n", help="Only the best N moments.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Re-download clips that already exist.")] = False,
    no_frames: Annotated[bool, typer.Option("--no-frames", help="Skip still frames.")] = False,
    config: ConfigOpt = None,
) -> None:
    """Download each moment at the best original quality (no re-encoding)."""
    from chatwatcher.clip.service import ClipError, clip_video

    cfg = _load(config)
    conn = db.open_db(cfg.paths.db_path)

    def on_event(rank: int, msg: str) -> None:
        typer.echo(f"  {'#' + str(rank) if rank else '  '} {msg}")

    try:
        run = clip_video(cfg, conn, url, ranks=_parse_ranks(ranks), top=top, force=force,
                         frames=not no_frames, on_event=on_event)
    except ClipError as e:
        typer.secho(f"❌ {e}", fg="red", err=True)
        raise typer.Exit(1)
    finally:
        conn.close()

    typer.echo("")
    for o in run.outcomes:
        if o.status == "done" and o.clip:
            c = o.clip
            res = f"{c.height}p{int(round(c.fps or 0))}" if c.height else "?"
            typer.echo(f"✅ #{o.rank:<2} {res} {c.vcodec}/{c.acodec} {int(c.duration_s)}s "
                       f"{_mb(c.size_bytes)} · {len(o.frames)} frames → {o.folder.name}/{c.path.name}")
        elif o.status == "skipped":
            typer.echo(f"⏭️  #{o.rank:<2} already clipped ({o.folder.name if o.folder else '?'}); --force to redo")
        else:
            typer.secho(f"❌ #{o.rank:<2} {o.message.splitlines()[0][:160] if o.message else 'failed'}",
                        fg="red")
    typer.echo(f"\n📁 {run.output_dir}")
    if run.count("error"):
        raise typer.Exit(1)


@app.command(context_settings=_VIDEO_ARG)
def analyze(
    url: Annotated[str, typer.Argument(help="VOD URL or video id (must be clipped).")],
    ranks: Annotated[Optional[str], typer.Option("--ranks", "-r", help="Only these ranks, e.g. 1,2,5.")] = None,
    top: Annotated[Optional[int], typer.Option("--top", "-n", help="Only the best N moments.")] = None,
    force: Annotated[bool, typer.Option("--force", help="Re-analyse moments that already have a storyboard.")] = False,
    no_digest: Annotated[bool, typer.Option("--no-digest", help="Skip the stream digest.")] = False,
    config: ConfigOpt = None,
) -> None:
    """Ask Claude (your subscription) for a summary, titles and storyboard per clip."""
    from chatwatcher.analyze.service import AnalyzeError, analyze_video

    cfg = _load(config)
    conn = db.open_db(cfg.paths.db_path)

    def on_event(rank: int, msg: str) -> None:
        typer.echo(f"  {'#' + str(rank) if rank else '  '} {msg}")

    try:
        run = analyze_video(cfg, conn, url, ranks=_parse_ranks(ranks), top=top, force=force,
                            digest=not no_digest, on_event=on_event)
    except AnalyzeError as e:
        typer.secho(f"❌ {e}", fg="red", err=True)
        raise typer.Exit(1)
    finally:
        conn.close()

    typer.echo("")
    unclipped = [o.rank for o in run.outcomes if o.status == "skipped" and not o.title
                 and o.message.startswith("not clipped")]
    for o in run.outcomes:
        if o.rank in unclipped:
            continue
        if o.status in ("done", "skipped") and o.title:
            mark = "✅" if o.status == "done" else "⏭️ "
            typer.echo(f"{mark} #{o.rank:<2} {o.worthiness}/10 {o.category:<10} {o.title[:70]}")
            if o.status == "done" and o.folder:
                typer.echo(f"        → {o.folder.name}/storyboard.md")
        elif o.status == "deferred":
            typer.secho(f"⏸️  #{o.rank:<2} {o.message[:140]}", fg="yellow")
        else:
            color = "red" if o.status == "error" else None
            typer.secho(f"{'❌' if o.status == 'error' else '⏭️ '} #{o.rank:<2} {o.message[:140]}", fg=color)
    if unclipped:
        typer.echo(f"⏭️  {len(unclipped)} moment(s) not clipped yet "
                   f"({', '.join('#' + str(r) for r in unclipped)}); run `cw clip` first")
    if run.digest_path:
        typer.echo(f"\n📋 {run.digest_path}")
    if run.calls:
        u = run.usage
        typer.echo(f"🧮 Claude: {run.calls} call(s) · {u['input'] + u['cache_read'] + u['cache_write']:,} "
                   f"input tokens ({u['cache_read']:,} cached) · {u['output']:,} output tokens")
    if run.digest_error:
        typer.secho(f"⚠️  digest failed: {run.digest_error[:200]}", fg="yellow")
    if run.count("error"):
        raise typer.Exit(1)


@app.command()
def videos(
    limit: Annotated[int, typer.Option("--limit", "-n")] = 30,
    config: ConfigOpt = None,
) -> None:
    """List ingested videos, newest first."""
    cfg = _load(config)
    conn = db.open_db(cfg.paths.db_path)
    try:
        rows = store.list_videos(conn, limit)
    finally:
        conn.close()
    if not rows:
        typer.echo("No videos yet. Add one with `cw ingest <url>`.")
        return
    typer.echo(f"{'VIDEO ID':<12} {'STATUS':<10} {'LENGTH':>7} {'MSGS':>8}  CAP  HEAT  TITLE")
    for v in rows:
        msgs = f"{v['message_count']:,}" if v["message_count"] is not None else "-"
        title = (v["title"] or "")[:60]
        typer.echo(f"{v['video_id']:<12} {v['status']:<10} {_fmt_duration(v['duration_s']):>7} "
                   f"{msgs:>8}  {'✓' if v['caption_lang'] else '-':^3}  "
                   f"{'✓' if v['has_heatmap'] else '-':^4}  {title}")
        if v["status"] == "error" and v["error"]:
            typer.secho(f"{'':12} ↳ {v['error'].splitlines()[0][:100]}", fg="red")


@app.command()
def status(config: ConfigOpt = None) -> None:
    """Show database location, schema version and row counts."""
    cfg = _load(config)
    if not cfg.paths.db_path.exists():
        typer.echo(f"No database at {cfg.paths.db_path}. Run `cw init` first.")
        raise typer.Exit(1)
    conn = db.connect(cfg.paths.db_path)
    try:
        version = db.schema_version(conn)
        counts = db.table_counts(conn)
    finally:
        conn.close()
    typer.echo(f"Database: {cfg.paths.db_path}  (schema v{version}, code v{db.SCHEMA_VERSION})")
    for name, n in counts.items():
        typer.echo(f"  {name:<10} {n}")


# ── automation ───────────────────────────────────────────────────────────────

channel_app = typer.Typer(help="Channels the worker watches for new streams.", no_args_is_help=True)
app.add_typer(channel_app, name="channel")


@channel_app.command("add")
def channel_add(
    channel: Annotated[str, typer.Argument(help="Channel URL, @handle or UC… id.")],
    stream_type: Annotated[str, typer.Option("--type", "-t", help="auto, talk, gaming or karaoke.")] = "auto",
    backfill: Annotated[Optional[int], typer.Option(
        "--backfill", "-b", help="How many recent finished streams to process now (default from config).")] = None,
    config: ConfigOpt = None,
) -> None:
    """Watch a channel: new finished streams get processed automatically."""
    from chatwatcher.discover import DiscoverError, add_channel

    cfg = _load(config)
    conn = db.open_db(cfg.paths.db_path)
    try:
        row, res = add_channel(cfg, conn, channel, stream_type=stream_type, backfill=backfill)
    except DiscoverError as e:
        typer.secho(f"❌ {e}", fg="red", err=True)
        raise typer.Exit(1)
    finally:
        conn.close()
    typer.echo(f"👀 Watching {row['title']} ({row['handle'] or row['channel_id']}) · type {row['default_type']}")
    typer.echo(f"   queued {len(res.queued)} stream(s)"
               + (f", skipped {len(res.skipped)} older/short one(s)" if res.skipped else "")
               + (f", {res.waiting} upcoming/live (picked up when they end)" if res.waiting else ""))
    if res.queued:
        typer.echo("   Start the worker to process them: cw worker")


@channel_app.command("list")
def channel_list(
    all_: Annotated[bool, typer.Option("--all", help="Include disabled channels.")] = False,
    config: ConfigOpt = None,
) -> None:
    """List watched channels."""
    from chatwatcher.discover import list_channels

    cfg = _load(config)
    conn = db.open_db(cfg.paths.db_path)
    try:
        rows = list_channels(conn, include_disabled=all_)
    finally:
        conn.close()
    if not rows:
        typer.echo("No channels yet. Add one with `cw channel add @handle`.")
        return
    from chatwatcher.clock import fmt, zone
    tz = zone(cfg)
    typer.echo(f"{'ID':>3}  {'ON':<3} {'TYPE':<8} {'VIDEOS':>6}  {'LAST CHECK':<20}  CHANNEL")
    for r in rows:
        checked = fmt(r["last_checked_at"], tz) if r["last_checked_at"] else "never"
        typer.echo(f"{r['id']:>3}  {'✓' if r['enabled'] else '-':<3} {r['default_type']:<8} {r['videos']:>6}  "
                   f"{checked:<20}  {r['title']} ({r['handle'] or r['channel_id']})")


def _toggle_channel(text: str, config: Path | None, enabled: int | None) -> None:
    from chatwatcher.discover import find_channel

    cfg = _load(config)
    conn = db.open_db(cfg.paths.db_path)
    try:
        row = find_channel(conn, text)
        if not row:
            typer.secho(f"❌ No channel matching {text!r} (see `cw channel list --all`)", fg="red", err=True)
            raise typer.Exit(1)
        if enabled is None:
            conn.execute("DELETE FROM channels WHERE id = ?", (row["id"],))
            typer.echo(f"🗑️  Removed {row['title']} (its videos and clips are kept)")
        else:
            conn.execute("UPDATE channels SET enabled = ? WHERE id = ?", (enabled, row["id"]))
            typer.echo(f"{'▶️' if enabled else '⏸️'}  {row['title']} {'enabled' if enabled else 'disabled'}")
    finally:
        conn.close()


@channel_app.command("enable")
def channel_enable(channel: str, config: ConfigOpt = None) -> None:
    """Resume watching a channel."""
    _toggle_channel(channel, config, 1)


@channel_app.command("disable")
def channel_disable(channel: str, config: ConfigOpt = None) -> None:
    """Stop watching a channel (keeps its history)."""
    _toggle_channel(channel, config, 0)


@channel_app.command("remove")
def channel_remove(channel: str, config: ConfigOpt = None) -> None:
    """Forget a channel. Its videos, clips and storyboards are kept."""
    _toggle_channel(channel, config, None)


def _video_pk(conn, url: str) -> tuple[int, str]:
    from chatwatcher.ingest.ytdlp import extract_video_id
    try:
        vid = extract_video_id(url)
    except ValueError as e:
        typer.secho(f"❌ {e}", fg="red", err=True)
        raise typer.Exit(2)
    return store.upsert_video(conn, vid), vid


@app.command(context_settings=_VIDEO_ARG)
def add(
    url: Annotated[str, typer.Argument(help="VOD URL or video id.")],
    config: ConfigOpt = None,
) -> None:
    """Queue one VOD for the worker (ingest → score → clip → analyze)."""
    from chatwatcher import jobs

    cfg = _load(config)
    conn = db.open_db(cfg.paths.db_path)
    try:
        pk, vid = _video_pk(conn, url)
        job = jobs.enqueue(conn, pk, "ingest")
    finally:
        conn.close()
    typer.echo(f"➕ Queued {vid}" if job else f"⏳ {vid} is being processed right now")
    typer.echo("   The worker picks it up: cw worker  (or run it now: cw run " + vid + ")")


@app.command(context_settings=_VIDEO_ARG)
def run(
    url: Annotated[str, typer.Argument(help="VOD URL or video id.")],
    config: ConfigOpt = None,
) -> None:
    """Process one VOD now, every stage, in the foreground."""
    from chatwatcher.worker import WorkerLocked, run_now, setup_logging

    cfg = _load(config)
    setup_logging(cfg)
    conn = db.open_db(cfg.paths.db_path)
    try:
        pk, vid = _video_pk(conn, url)
        result = run_now(cfg, conn, pk, say=lambda m: None)
    except WorkerLocked:
        typer.secho("❌ A worker is running; queue it with `cw add` instead.", fg="red", err=True)
        raise typer.Exit(1)
    finally:
        conn.close()
    if result == "done":
        typer.echo(f"\n✅ {vid} done. See `cw videos`, the output folder, and stream_digest.md.")
    elif result == "waiting":
        typer.secho(f"\n⏳ {vid} is waiting (see `cw jobs`); the worker will retry it automatically.",
                    fg="yellow")
    else:
        typer.secho(f"\n❌ {vid} failed (see `cw jobs`). Fix the cause, then `cw retry {vid}`.", fg="red")
        raise typer.Exit(1)


@app.command()
def worker(
    once: Annotated[bool, typer.Option("--once", help="Check channels, run what's due, then exit (for cron).")] = False,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
    config: ConfigOpt = None,
) -> None:
    """Watch channels and process the queue until stopped (Ctrl+C)."""
    from chatwatcher.worker import Worker, WorkerLocked, setup_logging

    cfg = _load(config)
    log_path = setup_logging(cfg, verbose=verbose)
    w = Worker(cfg)
    w.install_signal_handlers()
    typer.echo(f"🤖 chat-watcher worker · log: {log_path}" + ("" if once else " · Ctrl+C to stop"))
    try:
        n = w.run(once=once)
    except WorkerLocked as e:
        typer.secho(f"❌ {e}", fg="red", err=True)
        raise typer.Exit(1)
    except KeyboardInterrupt:
        typer.echo("\nStopped. Interrupted work is resumed on the next start.")
        return
    typer.echo(f"Processed {n} job(s).")


@app.command(context_settings=_VIDEO_ARG)
def upload(
    url: Annotated[str, typer.Argument(help="VOD URL or video id (must be clipped).")],
    config: ConfigOpt = None,
) -> None:
    """Copy a stream's output folder to Google Drive now (only new/changed files)."""
    from chatwatcher.upload.drive import UploadError, upload_video

    cfg = _load(config)
    conn = db.open_db(cfg.paths.db_path)
    try:
        r = upload_video(cfg, conn, url, on_line=lambda m: typer.echo(f"  {m}"))
    except UploadError as e:
        typer.secho(f"❌ {e}", fg="red", err=True)
        raise typer.Exit(1)
    finally:
        conn.close()
    typer.echo(f"☁️  {r.local_dir.name} → {r.dest}")
    typer.echo(f"   {r.url}")


@app.command()
def serve(
    host: Annotated[Optional[str], typer.Option(help="Address to listen on (default from config: 127.0.0.1).")] = None,
    port: Annotated[Optional[int], typer.Option(help="Port (default from config: 8000).")] = None,
    no_worker: Annotated[bool, typer.Option("--no-worker", help="Dashboard only; run `cw worker` separately.")] = False,
    config: ConfigOpt = None,
) -> None:
    """Open the web dashboard (and run the worker in the background)."""
    import uvicorn

    from chatwatcher.web.app import create_app
    from chatwatcher.worker import setup_logging

    cfg = _load(config)
    log_path = setup_logging(cfg)
    host = host or cfg.web.host
    port = port or cfg.web.port
    if host not in ("127.0.0.1", "localhost", "::1"):
        typer.secho("⚠️  The dashboard has no login. Only expose it on a network you trust.", fg="yellow")
    typer.echo(f"🌐 http://{'localhost' if host in ('127.0.0.1', '::1') else host}:{port}"
               + ("" if no_worker else f"  · worker runs here (log: {log_path})") + "  · Ctrl+C to stop")
    uvicorn.run(create_app(cfg, start_worker=not no_worker), host=host, port=port, log_level="warning")


@app.command("jobs")
def jobs_cmd(
    all_: Annotated[bool, typer.Option("--all", help="Include finished jobs.")] = False,
    limit: Annotated[int, typer.Option("--limit", "-n")] = 40,
    config: ConfigOpt = None,
) -> None:
    """Show the work queue."""
    from chatwatcher import jobs

    cfg = _load(config)
    conn = db.open_db(cfg.paths.db_path)
    try:
        rows = jobs.list_jobs(conn, include_done=all_, limit=limit)
    finally:
        conn.close()
    if not rows:
        typer.echo("Queue is empty." + ("" if all_ else " (`--all` shows finished jobs)"))
        return
    from chatwatcher.clock import fmt, zone
    tz = zone(cfg)
    icon = {"running": "▶️ ", "pending": "⏳", "error": "❌", "done": "✅"}
    for r in rows:
        when = f"at {fmt(r['run_after'], tz)}" if r["status"] == "pending" else ""
        typer.echo(f"{icon[r['status']]} {r['yt_id']} {r['stage']:<8} try {r['attempts']} {when}  "
                   f"{(r['title'] or '')[:40]}")
        note = r["error"] or r["message"]
        if note:
            typer.echo(f"     {note.splitlines()[0][:150]}")


@app.command(context_settings=_VIDEO_ARG)
def retry(
    url: Annotated[str, typer.Argument(help="VOD URL or video id.")],
    config: ConfigOpt = None,
) -> None:
    """Re-run a video's failed or waiting jobs now."""
    from chatwatcher import jobs

    cfg = _load(config)
    conn = db.open_db(cfg.paths.db_path)
    try:
        pk, vid = _video_pk(conn, url)
        n = jobs.retry_video(conn, pk)
        if n:
            store.upsert_video(conn, vid, status="processing", error=None)
    finally:
        conn.close()
    typer.echo(f"↻ {n} job(s) of {vid} queued to run now" if n else f"Nothing to retry for {vid}.")


if __name__ == "__main__":
    app()
