# Chat Watcher v2 — Rewrite Plan

## Goal

Turn a finished VTuber stream into a folder of **ready-to-edit clips**, each with a
summary and a storyboard — with no manual searching. The human's job starts at editing
and animating.

**In scope:** VODs only (live mode removed), automatic channel watching, multi-signal
moment ranking tuned for VTubers (talk, gaming, karaoke), best-quality clip download
with no re-encoding, Claude-generated summaries + storyboards via the user's Claude
subscription, a web dashboard, local + Google Drive output.

**Out of scope:** live chat polling, YouTube Data API key, paid LLM APIs, transcoding.

---

## Architecture

One Python project replaces the Go API, the bot and the clipper.

```
            ┌──────────────── web dashboard (FastAPI + Jinja + htmx) ───────────────┐
            │ channels · VOD list · score timeline · moment player + storyboard     │
            └───────────────────────────────┬───────────────────────────────────────┘
                                            │ reads/writes
                                   SQLite (data/chatwatcher.db)
                                            │
            ┌───────────────────── background worker (same process) ────────────────┐
            │ discover → ingest → score → clip → frames → analyze → upload          │
            └──┬──────────┬────────────┬──────────┬───────────┬──────────┬──────────┘
            RSS feed    yt-dlp      (pure py)   yt-dlp     ffmpeg    claude -p   rclone
```

- **Stack:** Python ≥3.12 (plain venv; uv optional), FastAPI, Jinja2 + htmx +
  Tailwind CDN (no JS build step), SQLite via `sqlite3`/SQLModel, Typer for a debug CLI.
- **Jobs:** a `jobs` table acts as a queue. Each stage is an idempotent job keyed by
  `(video_id, stage, moment_id)`. On startup, jobs left `running` are reset to `pending`,
  so restarts never leave things stuck (fixes the old orphaned-status bug).
- **Concurrency limits** per stage: downloads 2, Claude 1, uploads 1.
- **Big files on disk, metadata in SQLite.** Raw chat, captions, heatmap and clips live
  under `data/` and `output/`.

### Package layout

```
chat-watcher/
  pyproject.toml
  config.example.toml
  src/chatwatcher/
    config.py          # one TOML config + env overrides
    db.py              # schema + migrations (versioned, in code)
    jobs.py            # queue, worker loop, retry/backoff
    discover.py        # channel RSS polling, archive-ready check
    ingest/
      ytdlp.py         # metadata, chat replay, heatmap, captions
      chat_parser.py   # .live_chat.json → normalized messages (+ superchats, members)
      captions.py      # json3/vtt → timed sentences
    scoring/
      features.py      # per-bin features
      lexicons.py      # VTuber keyword/emote lists per category
      moments.py       # combine, peak-pick, merge, rank, cut points
      presets.py       # weights per stream type (talk / gaming / karaoke)
    clip/
      download.py      # yt-dlp section download, best quality
      frames.py        # ffmpeg still frames for Claude
    analyze/
      claude_runner.py # wraps `claude -p`, schema, retries, usage-limit pause
      prompts.py
      storyboard.py    # analysis.json → storyboard.md
    upload/drive.py    # rclone copy + folder link
    web/
      app.py, routes/, templates/, static/
    cli.py             # cw ingest|score|clip|analyze|run <url>, cw serve
  tests/               # fixtures: real chat samples, fake claude runner
```

---

## Pipeline stages

### 1. Discover (automation)
- The user adds channels (URL or `@handle`) in the dashboard. The `channel_id` is resolved once
  via yt-dlp.
- The worker polls `https://www.youtube.com/feeds/videos.xml?channel_id=…` every 30 min
  (free, no API key).
- New entries are checked with yt-dlp: only `live_status == "was_live"` archives with chat
  replay available are queued. Upcoming or live entries are rechecked later. Premieres and
  normal uploads are skipped by default (configurable).
- Members-only VODs work through the cookies file.
- The user can also paste a single VOD URL by hand.

### 2. Ingest
One yt-dlp metadata call (`-J`) gives title, duration, upload date, chapters and the
**`heatmap`** ("Most replayed" curve, when YouTube has one). Then it downloads:
- the chat replay (`--sub-langs live_chat --skip-download`)
- auto captions or uploaded subs (`ja`, `en`; json3 format) when available

The chat parser keeps what the old one had and adds:
- Super Chat amount, converted to a number, plus currency
- membership joins and gifted memberships
- the author's channel ID, for counting unique chatters
- whether the author is a moderator or the streamer (excluded from the hype signal)

### 3. Score moments
Chat is cut into 5-second bins. Features per bin:

| Feature | Notes |
|---|---|
| message rate | vs. a **local** baseline (rolling median + MAD over the previous 5 min), so busy endings don't swamp the whole stream |
| unique chatters | resists a single person spamming |
| laugh | 草, w/ｗ runs, 笑, lol, lmao, kekw, :_kusa: style emotes |
| hype / clutch | pog, W, ナイス, うおお, gg, let's go, nice |
| surprise / confusion | ？？？, !?, え, wtf, huh |
| karaoke applause | 8888, 👏, 🎵, うまい, kawaii, 天才 |
| "clip it" | clip, 切り抜き, clip this, ここすき |
| money | super chats, gifted memberships (amount-weighted) |
| replay heatmap | YouTube's "Most replayed" peaks, interpolated to bins, when available |

- Each feature becomes a robust z-score. A **preset per stream type** sets the weights:
  - *talk:* laugh and "clip it" count more
  - *gaming:* hype and surprise count more
  - *karaoke:* applause counts more, and moments become whole songs
- Stream type is guessed from the title and tags (歌枠/karaoke/singing → karaoke, game
  names/"playing" → gaming, otherwise talk). It can be overridden per VOD.
- **Turning bins into moments:**
  1. Smooth the score.
  2. Find the peaks.
  3. Merge peaks closer than 45s.
  4. Make each moment run from where the score rises above the threshold to where it falls back.
  5. Rank the moments and keep the top N (default 10) above a minimum score.
- **Cut points:**
  - Chat reacts 5–20s *after* the event, so each start is pushed back (default 25s).
  - The start then snaps to the start of a caption sentence when captions exist.
  - The end goes 5s past the point where the reaction dies down.
  - Lengths are kept between 20s and 3 min (configurable).
- **Karaoke:** a song's edges are found from applause bursts (8888 at the end) plus quiet
  gaps in the captions. The whole song becomes the moment, with a copyright flag.
- Every moment stores its score breakdown, so the dashboard can show *why* it ranked.

### 4. Clip download — best quality, no re-encode
```
yt-dlp -f "bv*+ba/b" -S "res,fps,br" \
       --download-sections "*START-END" \
       --merge-output-format mp4/mkv \
       --cookies … [--extractor-args youtube:po_token=…] URL
```
- The top resolution and frame rate (VP9/AV1 + Opus) are kept as-is. The container is mp4
  where the codecs allow it, mkv otherwise. **No `--force-keyframes-at-cuts`**, because
  that re-encodes. So cuts land on the nearest keyframe and may start a few seconds early.
  The 25s pre-roll absorbs this.
- One file per moment. The old 60s-part splitting and full-VOD fallback are dropped.
  Failed downloads retry with backoff.
- Two downloads at a time.

### 5. Frames
`ffmpeg` pulls 6–8 evenly spaced JPEG frames (plus one at the peak) from each clip into
`frames/`. These are only for Claude's visual context and as thumbnail options.

### 6. Analyze with Claude (subscription, no API key)
For each moment, one headless Claude Code call:
```
claude -p "<prompt>" \
  --output-format json --json-schema '<schema>' \
  --model sonnet --allowedTools Read --add-dir <moment_dir> \
  --no-session-persistence
```
- **Do not use `--bare`.** Bare mode only accepts `ANTHROPIC_API_KEY`, so it would leave
  the subscription.
- Only `Read` is allowed (to view frames). Chat text is untrusted input, so there are no
  Bash, Write or web tools.
- **The prompt includes:**
  - stream title and type
  - score breakdown
  - deduplicated, sampled chat (≤150 lines)
  - transcript lines for the clip window
  - frame file paths
- **Output schema:**
  - `summary`, `why_it_works`, `category`
  - `format`: short / long / both
  - `suggested_in`, `suggested_out`
  - `titles[3]`, `hook_line`, `thumbnail_frame`, `thumbnail_text`
  - `storyboard[]`: `t_start`, `t_end`, `shot`, `on_screen_text`, `edit` (zoom, shake,
    SFX, freeze, meme insert), `caption`
  - `vertical_crop_hint`, `music_copyright_risk`, `tags`
- **One extra call per stream** writes `stream_digest.md`: what happened overall, the
  best clip order for a highlights video, and a compilation title.
- **Usage limits:**
  - Claude runs one call at a time.
  - A daily moment cap is configurable.
  - A usage-limit error pauses the Claude queue and retries later. The rest of the
    pipeline keeps going.
- If Claude is unavailable, moments still get chat and transcript excerpts, so nothing
  blocks on it.

### 7. Output + Drive
```
output/<channel>/<YYYY-MM-DD>_<video_id>_<title-slug>/
  stream_digest.md
  moments.json
  01_<score>_<slug>/
    clip.mp4            # best-quality original
    storyboard.md       # human-readable: summary, hook, titles, shot list
    analysis.json
    chat_excerpt.txt
    transcript.txt
    frames/
```
`rclone copy` mirrors each stream folder to `gdrive:chat-watcher/…`. The folder link
(from `rclone lsjson`) is stored in the database and shown in the dashboard. Uploads are
optional per channel.

---

## Dashboard

| Page | What it does |
|---|---|
| **Channels** | add or remove channels, per-channel stream type default, auto-upload toggle, last check time |
| **Inbox** | new VODs and their pipeline progress per stage; retry a failed stage |
| **VOD** | score timeline chart (features stacked, moments highlighted, heatmap overlay), ranked moment list, stream digest, Drive link |
| **Moment** | local video player, storyboard, summary, titles, "nudge" in/out ±5s → re-download, re-run Claude, approve/reject (rejected moments feed lexicon/weight tuning later), open folder / Drive |
| **Settings** | presets and weights, lexicons, top N, clip length limits, pre-roll, cookies/PO token/Deno paths, Claude model and daily cap, rclone remote |

Clips are served with HTTP range support so the browser can scrub through them.
Server-rendered pages with htmx polling for progress. No SPA.

---

## Data model (SQLite)

- `channels(id, channel_id, handle, title, default_type, auto_upload, enabled, last_checked_at)`
- `videos(id, video_id, channel_id, title, duration, published_at, stream_type, status, error, data_dir, output_dir, drive_url)`
- `moments(id, video_id, rank, score, start_s, end_s, peak_s, kind, features_json, status[new/approved/rejected])`
- `clips(id, moment_id, path, format, width, height, fps, vcodec, size_bytes)`
- `analyses(id, moment_id, model, analysis_json, created_at)` (also video-level digest rows with `moment_id NULL`)
- `jobs(id, video_id, moment_id, stage, status, attempts, run_after, error, updated_at)`
- `settings(key, value_json)`

---

## Milestones

Each milestone ends with something usable.

1. **Foundation** ✅: Python project, config, SQLite schema, `cw init|config|doctor|status`.
   Also untracked `__pycache__` and added a root `.gitignore`.
2. **Ingest** ✅: `cw ingest <url>` and `cw videos`. Saves metadata, chat, heatmap and
   captions. Tested on real Hololive and Nijisanji archives. Real-data notes:
   - Videos can list several `-orig` caption tracks (an AI-dub `en-orig` next to the true
     `ja-orig`). We pick the one matching the video language.
   - Newer "jewel" gifts (`giftMessageViewModel`) have no price or channel id.
   - The heatmap is always 100 points, whatever the stream length, so a 4h stream gets
     ~2.4 min per point. Smaller streams often have none.
   - Japanese auto-caption lines often break mid-word. Milestone 3 merges them into
     sentences before choosing cut points.
3. **Scoring** ✅: `cw score <video>` ranks moments and says why. Tuned on real talk
   (Matsuri, Mito), gaming (Pekora horror) and karaoke (AZKi) archives. What changed
   from the original design after seeing real data:
   - Signals combine as a weighted mean of √z instead of hard-capped z. Busy chats hit
     z of 15–30, and capping made every moment score the same.
   - The threshold adapts: on calm streams it drops to the stream's own top 3%, so
     quiet chats still produce candidates. Overlapping clips merge into one.
   - Greeting waves (おつ, こんばんワニノコ) lower a moment's score.
   - Karaoke is detected from the title, or from chat (≥4 big 8888 waves per hour).
     Tags are never used for karaoke: channels tag every video 歌ってみた.
   - Song = window ending at its applause wave. Captions are no help here because ASR
     transcribes the lyrics as text, but the transcript does contain the song
     announcement and title for Claude to use.
4. **Clips + frames** ✅: `cw clip <video> [--top N | --ranks 1,3] [--force]`. Each moment
   folder gets `clip.mp4`, `frames/`, `chat_excerpt.txt`, `transcript.txt` and
   `moment.json`. Real-data notes:
   - The format sort is `res,fps,br`. It picks YouTube's higher-bitrate HLS tier (e.g.
     1080p60 H.264 format 312, ~2× the bitrate of DASH 299) and AAC audio
     (`ba[ext=m4a]`). Opus doesn't open in Premiere, and HLS+Opus section cuts make
     ffmpeg fail.
   - If a format fails, it falls back to DASH of the same quality, then a single file.
     Bot checks and members-only errors are not retried.
   - Copy-cuts land on keyframes (YouTube archives use ~6s GOPs). DASH cuts also bring
     an audio-only lead-in. We measure the real start (end − duration), trim the lead-in
     losslessly, and store `zero_s` so frames, chat and transcript line up.
   - One `-J` fetch per run (`--load-info-json`) instead of one per clip. The
     signed-URL dump is deleted afterwards.
5. **Claude analysis** ✅: `cw analyze <video>` writes `analysis.json` and `storyboard.md`
   per clip, plus `stream_digest.md`. Tested on real Pekora (gaming) and AZKi (karaoke)
   clips:
   - It corrects auto-caption errors using chat and the frames, and names songs from the
     karaoke title card.
   - It pinpoints where a song really starts, and flags music copyright risk.
   - About 15k tokens per clip; each run prints its total.
   - Uses `claude -p --output-format json --json-schema … --tools Read --restricted
     --strict-mcp-config --disable-slash-commands --no-session-persistence` with the clip
     folder as cwd. It never uses `--bare`, and it removes `ANTHROPIC_API_KEY` /
     `ANTHROPIC_AUTH_TOKEN` / `CLAUDE_CODE_SIMPLE` unless `claude.allow_api_key`.
   - Default model is `opus` at effort `medium`. `sonnet` stretches plan limits.
   - Busy chat is compressed to the top messages per 5s, plus every Super Chat.
   - Outputs are sanitized: times are clamped to the clip, and the thumbnail must be a
     real frame.
   - A usage limit pauses the run and defers the remaining moments. A login problem
     stops the run. Bad output is retried once. There is a daily cap.
   - The CLI's login is separate from the desktop app's. `cw doctor` checks it with
     `claude auth status`.
6. **Worker + automation** ✅: `cw channel add|list|enable|disable|remove`, `cw worker
   [--once]`, `cw add`, `cw run`, `cw jobs`, `cw retry`.
   - The watcher reads each channel's **Streams tab** (`yt-dlp --flat-playlist`), not
     RSS. That tab lists only streams, with their live status, in one sub-second call.
   - Finished streams are queued; upcoming and live ones are picked up once they end.
   - Short and members-only streams are skipped (members-only when there are no
     cookies). On first add, only `default_backfill` recent streams are processed.
   - The queue is the `jobs` table: one job per (video, stage), ingest → score → clip →
     analyze. A single worker holds a file lock, and jobs interrupted by a crash or
     Ctrl+C are re-queued on start.
   - Retry rules per failure:
     - archive still processing: every 30 min for 2 days
     - network: exponential backoff
     - bot check: every 2h, 3 tries
     - Claude usage limit: until the reset time in the error message
     - daily cap: until midnight UTC
     - not logged in: hourly
   - `pipeline.clip_top` / `analyze_top` limit the work per stream (Claude defaults to
     the top 5 to save Pro usage). Log at `data/logs/worker.log`.
7. **Dashboard** ✅: `cw serve` (runs the worker in the same process unless `--no-worker`
   or a separate `cw worker` is already running).
   - Pages: Streams (progress per stage), Channels, Queue, Settings (health check +
     effective config), a stream page with a score timeline and "most replayed" strip,
     and a moment page with player, frames, cut editor, storyboard and captions.
   - Plain server-rendered HTML with ~100 lines of JS and no CDNs. Charts are
     server-side SVG; colours were validated for colour-blindness in light and dark mode.
   - Buttons only queue jobs. Per-moment jobs (re-cut → re-download → re-analyse)
     share the worker queue.
   - Media is served only from paths in the database, with range requests for seeking.
   - Found on real data: SQLite connections must allow cross-thread use in FastAPI,
     since one request may span threads. Parallel frame loads failed otherwise.
8. **Drive upload** ✅ (tested on real Google Drive: 44 files / 134 MB in ~2 min; re-run sent 0 B):
   - The `upload` pipeline stage, `cw upload <video>`, and an "Upload to Drive" button
     with a Drive folder link on the stream page.
   - `rclone copy` (never `sync`) mirrors `output/<channel>/<stream>/` to
     `<remote>:<drive.folder>/…`. Temp files are excluded, and the folder link comes from
     `lsjson --stat`.
   - Re-cut / re-analysed moments trigger a re-upload; only changed files are sent.
   - A missing remote or expired login retries hourly, so uploads resume once fixed.
   - rclone v1.76.0 is installed in `~/.local/bin` (checksum verified). We recommend
     `scope=drive.file`, so rclone can only see files it created.
   - Real-Drive findings:
     - `lsjson --stat` on a folder has no ID, so the link comes from listing its parent.
     - rclone's shared Google client id is being retired in 2026; `cw doctor` warns and
       links to making your own.
9. **Cleanup** ✅: README with setup, daily use, background service, Claude usage, Drive,
   tuning and troubleshooting. Removed `chat-watcher-api/`, `chat-watcher-bot/` and
   `chat-watcher-clipper/`; git history keeps them, including the last uncommitted
   `watcher.go` change. Their `.env` files and `chat_spikes.json` were moved to the
   git-ignored `legacy-backup/`.

## Setup requirements
- Python ≥3.12, `ffmpeg` (installed)
- `yt-dlp`: installed as a package dependency; update often (`pip install -U yt-dlp`)
- Deno (yt-dlp's YouTube challenge solver)
- `claude` CLI, logged in with the subscription (installed)
- `rclone` with a `gdrive` remote (milestone 8)
- A YouTube cookies file is recommended (bot checks, members-only streams)

## Risks / open points
- **YouTube bot checks:** may need cookies, Deno or a PO token. Keep yt-dlp updated and
  show its errors clearly in the dashboard.
- **Missing heatmap:** common on low-view VODs. The scoring must work without it.
- **Captions often missing on JP live archives:** cut points fall back to chat-only. A
  later optional step can transcribe locally with faster-whisper (slow without a GPU).
- **Claude usage limits:** keep top N small and run analysis one at a time; queue the rest.
- **Keyframe-aligned cuts:** clips may start a few seconds early, a deliberate trade-off
  for zero quality loss.
- **Karaoke song edges** are fuzzy. Let the user nudge them in the dashboard.
