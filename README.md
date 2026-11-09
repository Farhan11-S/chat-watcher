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
   - **the editor** (once the clip is downloaded): Claude's cut on a waveform timeline,
     with each edge already moved to the nearest pause so it doesn't start mid-word.
     Claude's storyboard beats (hook, payoff, …) sit above the waveform and the transcript
     sits beside the player.
     - drag an edge (it snaps to a pause; hold **Shift** to place it exactly), or nudge it
       by ½ s, 0.1 s or one frame
     - **✂ Split** (key **S**) at the playhead, **+ Segment**, **↑ Earlier / ↓ Later** to
       reorder (e.g. open on the payoff), **volume** per segment (e.g. −15 dB on a screech)
     - click a transcript line to jump there; **⇤ / ⇥** start or end the selected segment
       at that line
     - **▶ Play edit** (Enter) plays your cut as it will come out, skipping the gaps
     - start over from **Claude's cut**, **Claude's short** or the **whole clip**;
       **Ctrl+Z** undoes. Changes save by themselves.
     - **sound effects** (from your library, see **Sounds** below) sit in a lane under the
       waveform. **+ Sound** (key **F**) adds one at the playhead; drag it to move it. Orange
       ticks mark sudden loud moments (a scream, a crash) and sounds snap to them. Per sound:
       which sound, its volume, and **her voice under it** (lowers the clip while it plays).
       **▶ Play edit** plays the sounds too.
     - **zooms, shakes and flashes** sit in a second lane:
       - **+ Punch-in** (key **Z**): an instant zoom on a region, e.g. her face when she
         realises, then straight back
       - **+ Push-in**: a slow, eased zoom that builds up to a moment
       - **+ Shake** (key **X**): camera shake, light / normal / heavy
       - **+ Flash**: white, red or black, fading out

       Drag one to move it, drag its right end to change how long it lasts, or set its
       length, region, zoom, strength or colour in the panel. Punch-ins, shakes and flashes
       snap to the orange ticks like sounds do. The player shows the zoom while the
       playhead is inside one, so you see the result without rendering.
     - **▢ Regions**: drag a box on the picture around her face (and the game, chat…)
       once per channel; every stream of that channel uses it. Zooms aim at the centre of
       their region, and **⤢ fill** zooms until the region fills the frame. Redraw a box
       and every zoom that uses it follows.
     - **✨ Suggest effects** (light / normal / heavy): Claude places sounds from your
       library and zooms, shakes and flashes aimed at your regions, with a reason for
       each, e.g. a fail sound + shake + red flash on the screech, a punch-in on her face
       for a line to camera. Your own effects stay and Claude builds around them; asking
       again replaces only Claude's. **Clear Claude's** removes them. One suggestion is
       one Claude call (about 10–20k tokens).
     - **Render edit** makes `edit.mp4` next to `clip.mp4`: frame-exact cuts, a tiny
       fade at each cut so nothing clicks, smooth zooms, shakes and flashes, your sounds
       mixed in, loudness levelled to YouTube's −14 LUFS. It
       goes ahead of other queued work and takes roughly a fifth of the edit's length
       for 720p (more for 1080p60). Watch it on the page, download it, or copy its path.
   - frames you click to jump to
   - Claude's summary, **titles and thumbnail text with copy buttons**, storyboard and
     captions
   - **Keep / Skip** buttons (Skip also stops it from being downloaded or analysed)
   - **Download range**: type new stream start/end times when the moment needs more of
     the stream than was downloaded; it re-downloads that range and Claude re-analyses it
     automatically
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
    edit.mp4                  ← your cut from the dashboard editor (after Render edit)
    edit.srt                  ← its subtitles, for YouTube's CC
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

## Transcripts (Whisper)

Each downloaded clip is transcribed on this computer with Whisper, right after it
downloads: her speech as text with a time for every word, the start of subtitles. It
works for streams YouTube hasn't captioned (often the case right after a stream ends) and
for mixed English/Indonesian. Install it once:

```bash
pip install -e ".[subtitles]"
```

The model (`large-v3-turbo`, 1.6 GB) downloads into `data/models/` the first time. On a
4-core CPU a 3-minute clip takes about 1½ minutes; renders you queue go first. In the
clip folder:

```
words.json                ← every line and word with start and end (clip seconds)
transcript.txt            ← Whisper's lines; the editor, "Analyse again" and "Ask Claude" use it
transcript.youtube.txt    ← the YouTube auto-caption excerpt it replaced
```

The editor shows where the transcript came from, with **🎙 Transcribe** for clips you
downloaded before, or **🎙 Again**. `[transcribe] model = "small"` is about a third faster
with more misheard words; `auto = false` transcribes only when you press the button.
Claude's first analysis happens on the preview frames, before the clip is downloaded, so
it still works from the YouTube captions (or chat only, when there are none).

## Subtitles

Once a clip is transcribed, the editor turns Whisper's words into subtitle lines: a new
line at each pause or sentence, never more than two lines on screen, each held a moment
after its last word. They show over the player while you play, sit in their own lane on
the timeline, and replace the transcript in the list beside the player.

- **Retype a line** by clicking it in the list (Enter keeps it, Esc undoes it). Put
  `*stars*` around a word to make it yellow.
- **Retime** by dragging a line's edges in the lane (they snap to Whisper's word times;
  hold Shift to place them freely), or drag the middle to move it. Click a line for its
  panel: nudge its start and end, **✂ Split** it at the playhead, **⤓ Join next**, or delete it.
- **+ Line** adds one at the playhead; **↻ From transcript** starts over from Whisper's
  words. Until you change a line, they follow the transcript, so **🎙 Again** updates them.
- **Burn in**, **size** and **at the** bottom / top are per edit. Karaoke clips start with
  burn-in off.
- **✨ Clean up** (or **✨ Fix & translate** when the clip isn't in English): one Claude call
  goes over the lines in your cut. It fixes misheard words and names (using chat, the
  stream title and the channel's names), punctuation and capitals, drops filler, translates
  lines that aren't in `claude.output_language`, and stars a punch word now and then. Every
  line keeps its timing; Claude may only join a line with the next or drop a filler-only
  one. Lines you typed stay as they are. Ctrl+Z undoes it.
- **Names Claude spells right**: one list per channel (the talent, friends, games,
  catchphrases), under the subtitles bar. Claude adds the names it finds, and a name you
  fix by retyping a line (a capitalised word mid-sentence, or one like `holo1GP`) is added
  too, so the next stream gets it right.

**Render edit** burns them in (bold white with a black outline, over any zoom, so the text
never zooms) and writes `edit.srt` next to `edit.mp4`, on the edit's timeline, for
YouTube's CC. `[subtitles]` in the config sets the defaults and the font.

## Sounds

The **Sounds** page is your sound-effect library. Drop in files (mp3, wav, ogg, m4a, flac,
or a short video, under 30 s each) and give each a **description of when to use it**:
Claude reads it to pick sounds, e.g. *"bonk: someone says something dumb or gets
corrected"*. Each file is cleaned up as it's added: silence at both ends is cut, so it
plays exactly at its marker, and it's levelled so volume 0 is about as loud as normal
talking. Files live in `data/sounds/`.

Note where each sound came from. Meme sounds are often cut from shows or games, and short
ones rarely get a Content ID claim, but they can. freesound.org (CC0), Pixabay and the
YouTube Audio Library are free to use.

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
- `claude.moment_cap = 50` allows at most 50 analyses in any 5 hours
  (`claude.moment_cap_hours`), like the plan's 5-hour limit. Extra clips wait for a slot
  and then continue automatically.
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
  `cw` output, logs) and dates new stream folders in WIB. Empty = this computer's time
  zone. Times are still stored in UTC.

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
