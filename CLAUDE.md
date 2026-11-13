# chat-watcher

VTuber stream clipper: finds moments in a VOD from chat and "most replayed", downloads clips,
has Claude analyse and edit them, and serves a web dashboard. Python, FastAPI + Jinja, SQLite,
no build step. Read only the feature you are changing; the map below says where it lives.

## Run

- Tests: `.venv/bin/python -m pytest -q` (about 3 minutes; run it in the background). One file:
  `.venv/bin/python -m pytest -q tests/test_web.py`. Web tests share the `site` fixture in `tests/test_web.py`.
- The dashboard runs live as the systemd user service `chatwatcher` (`cw serve`).
  - Templates and static files are re-read from disk, so an edit is live at once. Python changes need
    `systemctl --user restart chatwatcher`: change Python first, then the template, and restart together.
  - Before restarting, check the `jobs` table has no row with `status = 'running'`.

## Where things are (`src/chatwatcher/`)

| Feature | Files |
|---|---|
| Config, database, schema | `config.py`, `db.py` (schema + migrations), `store.py`, `prefs.py`, `paths.py`, `clock.py` |
| Jobs and the worker | `jobs.py` (queue), `worker.py` (loop), `pipeline.py` (stages, `Deps` = the fakeable tools), `cli.py` (`cw …`) |
| Watching channels | `discover.py` |
| Ingest (metadata, chat, heatmap) | `ingest/` — `ytdlp.py`, `chat_parser.py`, `captions.py`, `service.py` |
| Scoring moments | `scoring/` — `features.py`, `lexicons.py`, `presets.py`, `moments.py`, `service.py` |
| Downloading clips | `clip/` — `download.py`, `media.py` (ffmpeg/ffprobe), `package.py`, `preview.py`, `service.py` |
| Claude's analysis of a clip | `analyze/` — `claude_runner.py` (`claude -p`), `prompts.py`, `sheet.py` (contact sheet), `service.py` |
| Compilation ideas: one Claude call reads a whole stream | `compile/` — `chat.py` (thin the chat), `prompt.py` (timeline, token budget, schema), `parse.py` (check the answer), `service.py` (run, store, load); the on-demand `compile` job (`jobs.VIDEO_STAGES`) |
| Transcripts and subtitles | `subs/` — `transcribe.py` (Whisper), `align.py`, `lines.py`, `polish.py`, `names.py`, `speakers.py` (VTubers, colours) |
| The clip editor and renders | `edit/` — `service.py` (re-exports only), `clipref.py` (clip + edit rows, file names), `validate.py` (checking segments and effects), `substate.py` (subtitles as the editor sees them), `editstore.py` (load/save, tables `edits` and `shorts`), `rendering.py` (render a saved edit), `render.py` (ffmpeg graph), `pieces.py`, `audio.py`, `captions.py`, `layout.py` (Shorts), `motion.py`, `spots.py`, `texts.py`, `layers.py` (a region of the clip over the picture), `regions.py`, `proxy.py` |
| AI edit and Direct | `edit/autoedit/` — `prompt.py` (system prompt, answer schema), `plan.py` (reading Claude's answer), `assemble.py` (the cut), `camera.py` (shots), `effects.py` (sounds, overlays, texts, spots), `run.py` (gather, ask, apply); `edit/direct.py` |
| Sound and overlay library | `edit/sounds.py`, `edit/overlays.py` |
| Brand, intro, thumbnail | `brand/` — `brand.py` (also the sticker library), `intro.py`, `sfx.py`, `art.py`, `service.py`; thumbnails: `layers.py` (the layer model), `canvas.py` (draws layers), `cutout.py` (her cut-out, anime-seg ONNX model in `data/models`), `thumbai.py` (Claude composes versions; the on-demand `thumb` job), `thumb.py` (text, sizes, the Short layout) |
| Google Drive upload | `upload/drive.py`, `upload/login.py` |
| Disk and Drive usage, deleting | `storage.py`, `doctor.py` (health checks) |

## Web dashboard (`src/chatwatcher/web/`)

`app.py` is setup only: lifespan and worker thread, Jinja filters and globals, `render`, `get_conn`,
and `ROUTERS` (the order routers are included in). Each feature is `routes/<name>.py` with
`router(ctx)`; `ctx` (`routes/__init__.py`, `Ctx`) carries `cfg`, `deps`, `render`, `get_conn`, `tz`,
the worker status and the Drive cache. Never use module globals for these: tests build many apps.

| Page / URLs | Routes | Template | Script |
|---|---|---|---|
| `/`, `/add`, `/video/…` (stream page: review, Compilation ideas) | `routes/streams.py` | `home.html`, `video.html` | `app.js` |
| `/clips`, `/moment/{pk}`, keep/skip, re-cut | `routes/clips.py` | `clips.html`, `moment.html` | `app.js` |
| Editor API (`/api/moment/…`, `/moment/{pk}/edit`, `render`, `autoedit`, `direct`, `subs`, …) | `routes/editor.py` | `moment.html` | `editor/main.js` (modules, below), `editor.css` |
| `/brand`, stickers, intro and thumbnail endpoints | `routes/brand.py` | `brand.html`, `moment.html` | `maker.js` (intro), `thumb.js` (thumbnail layers) |
| `/sounds`, `/overlays/…` | `routes/library.py` | `sounds.html` | |
| `/vtubers` | `routes/vtubers.py` | `vtubers.html` | |
| `/channels` | `routes/channels.py` | `channels.html` | |
| `/jobs`, `/api/version` | `routes/queue.py` | `jobs.html` | |
| `/storage`, `/settings` | `routes/storage.py` | `storage.html` | |
| `/media/…` (files the database points at) | `routes/media.py` | | |

- The editor's script is native ES modules in `static/editor/` (no build step); `moment.html` loads
  `editor/main.js`. A module imports only from ones before it in this list, state shared between
  modules is the one object `S` in `state.js`, and the few calls upwards go through `call` (filled
  in by `main.js`). Read only the module you change:

  | Module | What |
  |---|---|
  | `state.js` | page elements, constants, `S` (the edit), `call` |
  | `util.js` | helpers, colours, subtitle wrapping (`subs/lines.py`), status lines, `body()` (what is saved) |
  | `preview.js` | camera (`edit/motion.py`), Short panels (`edit/layout.py`), spotlights, the stage canvas |
  | `audio.js`, `playback.js` | Web Audio sounds; seek, Play edit, toolbar buttons |
  | `captions.js` | speakers and colours, caption styles (`edit/captions.py`), the subtitle preview |
  | `io.js` | autosave, render request and status |
  | `editing.js` | undo, segment / sound / effect operations, subtitle line operations |
  | `overlays.js` | overlay clips, text callouts, spotlights and their drag handles |
  | `timeline.js` | time ↔ x, drawing the lanes, hit-testing |
  | `transcript.js` | the list of lines beside the player, the Transcript tab |
  | `panels.js` | side panel tabs and menus, the Selected panel, people, the subtitle bar |
  | `regions.js`, `layout.js` | drawing regions; the Short's layout panel, Long / Short switch, loading an edit |
  | `ai.js` | AI edit and Direct |
  | `input.js` | pointer and keyboard handlers |
  | `main.js` | entry: wires `call`, `paintAll`, the frame loop, start |

  `tests/test_motion.py`, `test_subs.py` and `test_polish.py` run slices of `preview.js`, `util.js`
  and `editing.js` in node against the Python they mirror.
- List-page SQL (search, filter, sort, paging) is `web/listing.py`; page read models are
  `web/queries.py`; the SVG charts are `web/charts.py`. Shared markup: `base.html`, `_macros.html`.
- Route order matters: `POST /moment/{pk}/{action}` (clips) and `POST /video/{video_id}/{action}`
  (streams) are catch-alls. A new `/moment/{pk}/<word>` route goes in a router listed before
  `clips` in `ROUTERS`, or above the catch-all in the same file.
- Requests never do long work: they queue a job (`jobs.enqueue`) and the worker does it.
