# chat-watcher

Turns finished VTuber streams into **ready-to-edit clips**. It watches your channels and
finds the moments chat reacted to: laughs, screams, hype, karaoke applause, "clip it".
It downloads each moment at the best original quality and asks Claude for a summary,
titles, cut points and a shot-by-shot storyboard. You open a folder and start editing.

```
channel ─▶ ingest ─▶ score ─▶ clip ─▶ analyze ─▶ upload
           chat,      ranked   best-     Claude      Google
           captions,  moments  quality   storyboard  Drive
           heatmap             mp4       + digest    (optional)
```

- **Finds moments from chat, not guesswork.** It looks at reaction bursts compared with
  the stream's normal chat level, unique chatters, emote spam, Super Chats and YouTube's
  "most replayed" curve.
- **Tuned for talk, gaming and karaoke streams**, detected automatically. In karaoke
  streams every song becomes its own clip.
- **Clips are never re-encoded.** You get YouTube's best video and audio, losslessly
  cut, as mp4.
- **Claude runs on your Claude subscription** through the `claude` CLI. No API key needed.
- **Dashboard:** timeline per stream, clip player, storyboard, keep/skip, re-cut, re-analyse
  with any model, ask Claude follow-up questions about a clip, and see disk and Drive space.

---

## Setup (once)

You need Linux or macOS, **Python 3.12+** and **ffmpeg**.

```bash
git clone <this repo> chat-watcher && cd chat-watcher
python3 -m venv .venv
.venv/bin/pip install -e .          # installs the `cw` command and yt-dlp
cp config.example.toml config.toml  # every setting is optional; the file explains each one
.venv/bin/cw init                   # creates data/ and output/
```

Then install the helpers:

| Tool | Why | How |
|---|---|---|
| **Deno** | yt-dlp needs it to get through YouTube's checks | `curl -fsSL https://deno.land/install.sh \| sh` (goes to `~/.deno`, found automatically) |
| **Claude Code** | summaries and storyboards | install Claude Code, then `claude auth login` once. This login is separate from the desktop app's. |
| **rclone** *(optional)* | Google Drive upload | see [Google Drive](#google-drive-optional) |
| **cookies.txt** *(optional)* | members-only streams; fewer bot checks | export from your browser, then set `youtube.cookies_file` |

Check everything:

```bash
.venv/bin/cw doctor
```

> Tip: `source .venv/bin/activate` lets you type `cw` instead of `.venv/bin/cw`.

---

## Daily use

```bash
cw serve
```

Open **http://localhost:8000**. The dashboard and the worker run in the same process.

1. **Channels → Watch a channel.** Paste `@handle` or the channel URL, and pick a type
   (or leave `auto`). It processes the latest finished stream right away, then checks
   every 30 minutes. Upcoming and live streams are picked up once they end.
2. **Streams** shows each stream's progress through ingest → score → clip → analyze
   (→ upload).
3. **Open a stream and review its moments** on the embedded YouTube player:
   - a button per moment under the player jumps straight to it; **Next / Prev** walk
     through them, and clicking the chart jumps to any time
   - **✓ Keep / ✗ Skip** next to the player (keys **K** / **S**, **U** undo, **N** / **P**
     next / previous, **R** replay). After each choice it jumps to the next moment to review.
     Every row of the moments table has ✓ / ✗ too.
   - **Clip kept** downloads the moments you kept; Claude analyses them after.
   - a timeline of chat reactions, YouTube's "most replayed" curve, and Claude's digest
     (highlights order, compilation titles, best Shorts)

   Claude has already analysed **every** moment by the time you open the stream: each
   one gets a small 360p, video-only preview whose still frames Claude reads (only the
   frames are kept, ~400 KB per moment). The moment buttons show Claude's rating, you can
   go through them **by Claude's rating**, and **✓ Keep the N rated 7+** keeps Claude's
   picks in one click. When you **Clip kept**, the full-quality clip reuses Claude's
   analysis (times shifted to the new file), so it isn't asked again.

   With `pipeline.auto_clip = false`, nothing is downloaded at full quality until you press
   **Clip kept**. With `true`, the best `clip_top` moments are downloaded automatically.
   `pipeline.preview_height = 0` turns previews off (Claude then only sees downloaded clips).
4. **Open a moment:**
   - the clip player, and frames you click to jump to
   - Claude's summary, **titles and thumbnail text with copy buttons**, storyboard and
     captions
   - **Keep / Skip** buttons (Skip also stops it from being downloaded or analysed)
   - **Re-cut**: type new start/end times; it re-downloads that range and Claude
     re-analyses it automatically
   - **Analyse again** with another model or effort (e.g. `opus`, `high effort`) when the
     default Sonnet analysis misses something
   - **Ask Claude about this clip**: follow-up questions such as "what is supposed to happen
     in Undertale here?" or "explain the jargon". Answers stay in a thread on the page and
     never change the analysis. Tick *search the web* for recent VTuber news Claude may not
     know. Each question costs about 5–10k tokens.
   - **☁ Upload this clip to Drive** (when Drive is set up)
5. **Edit.** Everything for a moment is in one folder:

```
output/<channel>/<date>_<video id>_<title>/
  stream_digest.md            ← Claude's plan for the whole stream
  01_laugh_1h00m50s/
    clip.mp4                  ← best quality, ready for your editor
    storyboard.md             ← summary, cut points, titles, shot list, captions
    analysis.json             ← the same, machine-readable
    frames/                   ← stills; one is Claude's thumbnail pick
    chat_excerpt.txt          ← every chat message, in clip time
    transcript.txt            ← what was said, in clip time
    moment.json               ← why it ranked, stream times, file details
```

All storyboard and caption times are **seconds from the start of `clip.mp4`**.

### Without the dashboard

| Command | What it does |
|---|---|
| `cw channel add @handle [--type karaoke] [--backfill N]` | watch a channel (also `list`, `disable`, `enable`, `remove`) |
| `cw add <url>` | queue one VOD |
| `cw run <url>` | process one VOD now, every stage, in the foreground |
| `cw worker` | run the queue and channel checks until Ctrl+C (`--once` = one pass, for cron) |
| `cw jobs` / `cw retry <url>` | see the queue and why things wait or fail; re-run failures |
| `cw ingest` · `cw score` · `cw clip` · `cw analyze` · `cw upload` | run a single stage by hand |
| `cw videos` · `cw status` · `cw doctor` · `cw config` | look around |

Video IDs starting with `-` work as-is (`cw run -k8EZv9wkAs`).

---

## Keep it running

`cw serve` and `cw worker` can be stopped at any time. Interrupted work picks up where
it left off, and finished work is never redone. To run it in the background as a
**systemd user service**, create `~/.config/systemd/user/chatwatcher.service`:

```ini
[Unit]
Description=chat-watcher (dashboard + worker)
After=network-online.target

[Service]
WorkingDirectory=%h/projects/chat-watcher
ExecStart=%h/projects/chat-watcher/.venv/bin/cw serve
Environment=PATH=%h/.local/bin:%h/.deno/bin:/usr/local/bin:/usr/bin:/bin
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now chatwatcher
loginctl enable-linger "$USER"     # keep running after you log out
journalctl --user -u chatwatcher -f
```

The worker also logs to `data/logs/worker.log`. Only one worker can run per data folder;
a second one refuses to start.

---

## Claude usage

Claude only sees what it needs:
- the clip's transcript
- a compressed view of its chat
- ~9 still frames, which it can read but nothing else: no shell, no web, no
  plugins

Every clip is analysed by **Sonnet at low effort** by default: about 10–15k tokens per clip,
plus one digest call per stream. Every `cw analyze` prints its total. When a clip deserves a
closer look, pick another model on its page (**Analyse again**) or redo a whole stream from
the stream page.

- `pipeline.analyze_top = 5`: only analyse the 5 best clips per stream (default 0 = all).
- `claude.model` / `claude.effort` set the default (`"sonnet"` / `"low"`); `"opus"` or a
  higher effort uses much more of a Pro/Max plan's limits.
- `claude.daily_moment_cap = 40` limits analyses per day.
- **Hit a usage limit?** Remaining clips wait until your plan resets and then continue
  automatically.
- `ANTHROPIC_API_KEY` is ignored on purpose, so nothing is ever billed to the API.

## Google Drive (optional)

1. Connect rclone to your Google account. A browser opens to sign in:
   ```bash
   rclone config create gdrive drive scope=drive.file
   ```
   `drive.file` means rclone can only see files it created, so the rest of your Drive
   stays private.
2. **Recommended:** make your own Google client ID. rclone's shared one is being
   retired. Follow https://rclone.org/drive/#making-your-own-client-id, then
   `rclone config update gdrive client_id=… client_secret=…` and
   `rclone config reconnect gdrive:`.
3. Set `drive.enabled = true` in `config.toml` and run `cw doctor`.

Streams are copied to `chat-watcher/<channel>/<stream>/` on Drive after processing.
Re-runs only send new or changed files. **Nothing on Drive is ever deleted.**

**Upload by hand instead:** turn *auto-upload* off on the **Settings** page (it wins over
`drive.auto_upload` in `config.toml`, no restart needed). Then nothing goes to Drive until
you press a button: **☁ Upload all** or **☁ Upload kept** (only clips marked ✓ Keep) on a
stream page, or **☁ Upload this clip** on a clip page. Clips on Drive show a ☁.

The **Storage** page shows how much space clips take on this computer and on Drive, per
channel and per stream. Tick streams there to free space:
- **Delete videos only** removes the clip videos (most of the space) and keeps summaries,
  storyboards, frames and your questions. **Clip kept** on the stream page (or **Download it
  again** on a clip page) brings them back.
- **Delete streams** removes everything about them from this computer, and the channel
  watcher won't fetch them again (add the URL by hand if you change your mind).

Neither touches Google Drive.

---

## Tuning

Everything is in `config.toml`. Look at `config.example.toml` for every option.

- **Moments are missed or there are too many:** change `scoring.min_score` (top
  moments are usually 1.2–2.5) and `scoring.top_n`.
- **A channel's own emotes:** add them per reaction type, e.g.
  `extra_keywords = { laugh = [":_mywarai:"] }`.
- **Weights:** e.g. `weights = { talk = { laugh = 3.0 } }`.
- **Wrong stream type:** set it per channel (dashboard or `--type`), or re-score a
  stream from its page.
- **Clip padding:** `pre_roll_s` (default 25s), `min_clip_s`, `max_clip_s`.
- **Time zone:** `general.timezone = "Asia/Jakarta"` shows every time in WIB (dashboard,
  `cw` output, logs), dates new stream folders in WIB and resets the daily Claude cap at
  WIB midnight. Empty = this computer's time zone. Times are still stored in UTC.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Sign in to confirm you're not a bot" | set `youtube.cookies_file`; keep yt-dlp updated (`.venv/bin/pip install -U yt-dlp`) |
| Members-only streams skipped | needs cookies from an account with that membership |
| "claude CLI isn't logged in" | `claude auth login` (the CLI's login is separate from the desktop app) |
| Drive "login expired" | `rclone config reconnect gdrive:` |
| Something is stuck | `cw jobs` shows why; `cw retry <url>` re-runs it |

## Safety

- The dashboard has **no login**. It listens on `127.0.0.1` only, so don't expose it
  to a network you don't trust.
- It only serves files recorded in its database. The YouTube PO token is hidden on the
  Settings page.
- Chat and captions are treated as untrusted input in Claude prompts.
- Clips are for editing. Respect each agency's clip guidelines, and check the music
  copyright flag before posting karaoke clips.

## Development

```bash
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

The tests use fake yt-dlp, rclone and Claude executables, plus small ffmpeg-generated
videos. They don't touch the network or your Claude plan.

Design notes and what testing on real streams showed are in
[docs/REWRITE_PLAN.md](docs/REWRITE_PLAN.md).
