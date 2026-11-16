// The 3D stage page (templates/stage.html): set it up, play it with the cut's sound, have
// Claude write the scene and direct it, export it. ?t=12.3 draws one still (screenshots,
// the model check), ?export=1&w=&h= is the exporter's view (stage/export.py drives
// window.stage3d.step), &probe=1 logs the parts that still move.
import * as THREE from 'three';
import { OutlineEffect } from 'three/addons/effects/OutlineEffect.js';
import { Figure, loadModel } from './figure.js';
import { place } from './camera.js';
import { clipTime, laneAt, shotAt } from './cues.js';
import * as room from './sets/room.js';
import * as cabin from './sets/cabin.js';

const SETS = { room, cabin };
const $ = (id) => document.getElementById(id);
const root = $('st');
const pk = root.dataset.moment;
const q = new URLSearchParams(location.search);
const MODE = q.has('export') ? 'export' : q.has('t') ? 'still' : 'live';
const BLANK = { shots: [{ t: 0, shot: 'medium', yaw: 0, push: 0.05 }], poses: [{ t: 0, pose: 'rest', ease: 0.3 }],
                faces: [{ t: 0, face: 'neutral', ease: 0.3 }], actions: [], lanes: {}, note: '' };

let S, scene, track, fig, set, video, renderer, effect, scene3, camera;
const audio = new Audio();

async function api(url, body) {
  const r = await fetch(url, body === undefined ? { cache: 'no-store' }
    : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  let d;
  try { d = await r.json(); } catch { d = { ok: false, error: `${r.status} ${r.statusText}` }; }
  if (d.ok === false) throw new Error(d.error || 'Something went wrong.');
  return d;
}
const say = (text) => { const m = $('st-msg'); m.hidden = !text; m.textContent = text || ''; };
const esc = (s) => String(s ?? '').replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

// ── the picture ──────────────────────────────────────────────────────────────

async function build3d() {
  const stage = $('st-stage');
  if (MODE !== 'live') {
    document.body.classList.add('st-bare');
    stage.style.width = `${+q.get('w') || (S.stage.shape === 'long' ? 1280 : 540)}px`;
    stage.style.height = `${+q.get('h') || (S.stage.shape === 'long' ? 720 : 960)}px`;
  }
  renderer = new THREE.WebGLRenderer({ antialias: q.get('aa') !== '0', preserveDrawingBuffer: MODE !== 'live' });
  renderer.setPixelRatio(MODE === 'live' ? Math.min(devicePixelRatio, 2) : 1);
  effect = q.get('outline') === '0' ? renderer : new OutlineEffect(renderer);
  scene3 = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(24, 9 / 16, 1, 4000);
  scene3.add(new THREE.AmbientLight(0xdfe4ff, 1.35));
  const sun = new THREE.DirectionalLight(0xffffff, 1.15);
  sun.position.set(-1, 1.4, 1.6);
  scene3.add(sun);

  say('Loading physics…');
  window.Ammo = typeof window.Ammo === 'function' ? await window.Ammo() : window.Ammo;
  say(`Loading ${S.profile.name}…`);
  const { mesh, missingFiles } = await loadModel(S.model_url, (k) => say(`Loading ${S.profile.name}… ${Math.round(k * 100)}%`));
  fig = new Figure(mesh, S.profile);
  fig.missingFiles = missingFiles;
  fig.sit();
  set = (SETS[S.stage.set_name] || room).build(scene3, fig);
  scene3.add(mesh);
  fig.start(track.duration);
  if (S.stage.set_name === 'room') {
    video = document.createElement('video');
    Object.assign(video, { muted: true, playsInline: true, preload: 'auto', crossOrigin: 'anonymous', src: S.video });
  }
  audio.src = S.track.audio;
  audio.preload = 'auto';
  stage.prepend(renderer.domElement);
  say('');
  size();
  addEventListener('resize', size);
}

function size() {
  const st = $('st-stage');
  effect.setSize(st.clientWidth, st.clientHeight);
  camera.aspect = st.clientWidth / st.clientHeight;
  camera.updateProjectionMatrix();
}

function lanesAt(t) {
  const out = {};
  for (const [name, list] of Object.entries(scene.lanes || {})) out[name] = laneAt(list, t, list[0]?.v ?? 0);
  return out;
}

const captionAt = (t) => {
  const line = track.lines.find((l) => t >= l.t0 && t < l.t1);
  return line ? line.text.replace(/[*"]/g, '') : '';
};

function draw(t, force = false, image = null) {
  fig.apply(t, scene, track);
  const [shot, into] = shotAt(scene.shots, t, track.duration);
  place(camera, shot, into, fig, set);
  if (fig.shake > 0) {                       // a take shakes the camera (deterministic, so exports match)
    const k = fig.shake * fig.headSize * 0.07;
    camera.position.x += k * Math.sin(t * 91.7);
    camera.position.y += k * Math.sin(t * 73.1 + 1.3);
    camera.rotation.z += fig.shake * 0.025 * Math.sin(t * 61.3);
  }
  set.update(t, lanesAt(t), image || video ? { video, image, region: S.region, force } : null);
  $('st-caption').textContent = captionAt(t);
}

const seeked = (v) => new Promise((ok) => { v.addEventListener('seeked', ok, { once: true }); setTimeout(ok, 3000); });
async function videoAt(t) {
  if (!video) return;
  if (video.readyState < 1) await new Promise((ok) => { video.addEventListener('loadeddata', ok, { once: true }); setTimeout(ok, 8000); });
  const want = clipTime(track.segments, t);
  if (Math.abs(video.currentTime - want) > 0.001) { video.currentTime = want; await seeked(video); }
}

// ── live playback ────────────────────────────────────────────────────────────

let last = performance.now(), lastT = 0;
function frame(now) {
  const t = audio.paused ? parseFloat($('st-seek').value) : audio.currentTime;
  const dt = Math.min((now - last) / 1000, 0.05);
  last = now;
  if (Math.abs(t - lastT) > 0.5) fig.reset();
  lastT = t;
  if (video) {
    const want = clipTime(track.segments, t);
    if (!audio.paused) {
      if (Math.abs(video.currentTime - want) > 0.3 && !video.seeking) video.currentTime = want;
      if (video.paused) video.play().catch(() => {});
    } else {
      if (!video.paused) video.pause();
      if (Math.abs(video.currentTime - want) > 0.05 && !video.seeking) video.currentTime = want;
    }
  }
  draw(t);
  fig.step(dt);
  effect.render(scene3, camera);
  if (!audio.paused) $('st-seek').value = t;
  $('st-clock').textContent = `${t.toFixed(1)} / ${track.duration.toFixed(1)}`;
  requestAnimationFrame(frame);
}

function live() {
  const play = $('st-play'), seek = $('st-seek');
  seek.max = track.duration;
  play.disabled = false;
  play.onclick = () => {
    if (audio.paused) { audio.currentTime = parseFloat(seek.value); audio.play(); } else audio.pause();
  };
  audio.onplay = () => { play.textContent = 'Pause'; };
  audio.onpause = () => { play.textContent = 'Play'; video?.pause(); };
  audio.onended = () => { seek.value = 0; };
  seek.oninput = () => { if (!audio.paused) audio.currentTime = parseFloat(seek.value); };
  addEventListener('keydown', (ev) => {
    if (ev.code === 'Space' && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName)) { ev.preventDefault(); play.click(); }
  });
  requestAnimationFrame(frame);
}

// ── stills and the exporter ──────────────────────────────────────────────────

async function still(t) {
  await videoAt(t);
  draw(t, true);
  fig.settle(t, scene, track);
  draw(t, true);
  effect.render(scene3, camera);
  if (q.has('probe')) console.log('PROBE ' + JSON.stringify(fig.probe(t, scene, track)));
  document.title = 'ready';
}

// The picture with the caption and the credit drawn in, as the exporter writes it (the
// page's own caption and credit are HTML over the canvas; this draws the same in 2D).
const out = document.createElement('canvas');
function composite(t) {
  const src = renderer.domElement, W = src.width, H = src.height, long = S.stage.shape === 'long';
  out.width = W; out.height = H;
  const g = out.getContext('2d');
  g.drawImage(src, 0, 0);
  const text = captionAt(t);
  if (text) {
    const size = Math.round(H * (long ? 0.05 : 0.034));
    g.font = `800 ${size}px system-ui, "Noto Sans", "Noto Sans CJK JP", sans-serif`;
    g.textAlign = 'center';
    g.textBaseline = 'alphabetic';
    const max = W * 0.88, lines = [];
    let cur = '';
    for (const word of text.split(/\s+/)) {
      const next = cur ? `${cur} ${word}` : word;
      if (cur && g.measureText(next).width > max) { lines.push(cur); cur = word; } else cur = next;
    }
    if (cur) lines.push(cur);
    const lh = size * 1.2, bottom = H * (1 - (long ? 0.08 : 0.12));
    g.lineJoin = 'round';
    g.lineWidth = Math.max(3, size * 0.17);
    g.strokeStyle = '#000';
    g.fillStyle = '#fff';
    lines.forEach((line, i) => {
      const y = bottom - (lines.length - 1 - i) * lh - size * 0.2;
      g.strokeText(line, W / 2, y);
      g.fillText(line, W / 2, y);
    });
  }
  const cs = Math.max(10, Math.round(H * 0.015));
  g.font = `600 ${cs}px system-ui, sans-serif`;
  g.textAlign = 'right';
  g.shadowColor = '#000';
  g.shadowBlur = cs * 0.3;
  g.fillStyle = 'rgba(255,255,255,.72)';
  g.fillText('© COVER', W * 0.975, H * 0.984);
  g.shadowBlur = 0;
  return out.toDataURL('image/jpeg', 0.92).slice('data:image/jpeg;base64,'.length);
}

const loadImage = (url) => new Promise((ok, bad) => {
  const img = new Image();
  img.onload = () => ok(img);
  img.onerror = () => bad(new Error('no screen frame ' + url));
  img.src = url;
});

// The exporter steps through the cut in order (the hair and clothes physics needs that):
// frame(t) once to settle her, then frame(t, 1/fps) for each next one. screen: a still of
// the stream footage for the room's screen (the exporter cuts them; seeking is too slow).
window.stage3d = {
  ready: null,
  get duration() { return track?.duration; },
  async step(t, dt, screen) {
    const image = screen ? await loadImage(screen) : null;
    if (!image) await videoAt(t);
    if (dt === undefined) { draw(t, true, image); fig.settle(t, scene, track, 60); }
    draw(t, true, image);
    if (dt !== undefined) fig.step(dt);
    effect.render(scene3, camera);
    return true;
  },
  async frame(t, dt, screen) {
    await this.step(t, dt, screen);
    return composite(t);
  },
};

// ── the side panel ───────────────────────────────────────────────────────────

function fillSetup() {
  const st = S.stage || {}, guess = S.guess;
  const ms = $('st-model');
  ms.innerHTML = S.models.length
    ? S.models.map((m) => `<option value="${esc(m.slug)}">${esc(m.name)}${m.checked ? '' : ' (not checked yet)'}</option>`).join('')
    : '<option value="">No models yet: import one on the Models page</option>';
  ms.value = st.model || guess.model || S.models[0]?.slug || '';
  const ss = $('st-set');
  ss.innerHTML = Object.entries(S.vocab.sets).map(([k, v]) => `<option value="${k}">${esc(v.label)}</option>`).join('');
  ss.value = st.set_name || guess.set_name;
  for (const r of document.querySelectorAll('input[name=st-shape]')) r.checked = r.value === (st.shape || guess.shape);
  $('st-setup-go').textContent = S.stage ? 'Apply' : 'Set up';
  $('st-setup').onsubmit = async (ev) => {
    ev.preventDefault();
    const btn = $('st-setup-go');
    btn.disabled = true;
    $('st-setup-status').textContent = 'Making the voice track (10-40 s)…';
    try {
      await api(`/moment/${pk}/stage/setup`, { model: ms.value || null, set: ss.value,
        shape: document.querySelector('input[name=st-shape]:checked')?.value, recut: $('st-recut').checked });
      location.reload();
    } catch (e) {
      $('st-setup-status').textContent = e.message;
      btn.disabled = false;
    }
  };
}

function fillThread(messages = S.thread) {
  const box = $('st-thread');
  box.innerHTML = messages.map((m) => `<div class="st-say ${m.role}"><b>${m.role === 'user' ? 'You' : 'Claude'}</b> ${esc(m.text)}</div>`).join('')
    || '<p class="muted"><small>Claude writes the shots, poses, faces and set cues from what she says. Then tell it what to change.</small></p>';
  box.scrollTop = box.scrollHeight;
  $('st-write').textContent = messages.length ? '✨ Write again' : '✨ Write the scene';
}

function setScene(next) {
  scene = next || BLANK;
  $('st-json').value = JSON.stringify(scene, null, 1);
  if (fig) fig.reset();
}

async function talk(body, btn) {
  const status = $('st-direct-status');
  btn.disabled = true;
  status.textContent = 'Claude is writing (30-90 s)…';
  try {
    const d = await api(`/moment/${pk}/stage/direct`, body);
    if (d.applied) setScene(d.scene);
    fillThread(d.messages);
    status.textContent = '';
    return true;
  } catch (e) {
    status.textContent = e.message;
    return false;
  } finally {
    btn.disabled = false;
  }
}

function fillScene() {
  const ready = !!(S.stage && S.track?.ready && S.profile);
  $('st-write').disabled = !ready || !S.claude;
  $('st-send').disabled = !ready || !S.claude;
  $('st-write').onclick = () => talk({ new: true, start_over: $('st-over').checked }, $('st-write'));
  $('st-direct').onsubmit = async (ev) => {
    ev.preventDefault();
    const text = $('st-say').value.trim();
    if (!text) return;
    if (await talk({ message: text }, $('st-send'))) $('st-say').value = '';
  };
  $('st-json-save').onclick = async () => {
    try {
      const d = await api(`/moment/${pk}/stage/scene`, { scene: JSON.parse($('st-json').value) });
      setScene(d.scene);
      $('st-json-status').textContent = d.warnings.length ? 'Saved; skipped: ' + d.warnings.join('; ') : 'Saved.';
    } catch (e) {
      $('st-json-status').textContent = e.message;
    }
  };
}

let polling = null;
function fillRender(r = S.render) {
  const s = $('st-render-status'), dl = $('st-dl');
  const job = r.job;
  const busy = job && (job.status === 'pending' || job.status === 'running');
  $('st-export').disabled = busy || !(S.stage && S.track?.ready && S.profile);
  if (busy) s.textContent = job.status === 'running' ? `Exporting… ${Math.round((r.progress || 0) * 100)}%` : `Waiting in the queue (worker: ${r.worker})…`;
  else if (job && job.status === 'error') s.textContent = 'Export failed: ' + (job.error || '');
  else if (r.have) s.textContent = r.fresh ? `stage.mp4 is up to date (${r.rendered_at || ''}).` : 'stage.mp4 is from an older version of this scene.';
  else s.textContent = 'Not exported yet.';
  dl.hidden = !r.have;
  dl.href = `/media/stage/${pk}/video?dl=1`;
  $('st-watch').hidden = !r.have;
  $('st-watch').href = `/media/stage/${pk}/video`;
  clearTimeout(polling);
  if (busy) polling = setTimeout(async () => fillRender(await api(`/api/moment/${pk}/stage/render`)), 2000);
}

function fillInfo() {
  const lines = [];
  if (S.profile) lines.push(`${S.profile.name} · ${S.profile.pmx}`);
  if (fig) {
    lines.push(`${fig.mesh.skeleton.bones.length} bones, ${fig.morphNames.length} expressions`);
    lines.push(`not in this model: ${[...fig.missing.bones, ...fig.missing.morphs].join(' ') || 'nothing'}`);
    if (fig.missingFiles?.length) lines.push(`files it names but doesn't have: ${fig.missingFiles.join(', ')}`);
    lines.push(`pinned while seated: ${fig.pinned.length} parts`);
  }
  if (track) lines.push(`voice track: ${track.words.length} words (${track.placed} timed letter by letter), ` +
                       `${track.laughs.length} laughs, cut ${track.duration.toFixed(1)} s`);
  $('st-info').textContent = lines.join('\n');
  const warn = [...(S.track?.warnings || []), ...(S.track?.error ? [S.track.error] : [])];
  $('st-warn').hidden = !warn.length;
  $('st-warn').textContent = warn.join(' ');
}

// ── start ────────────────────────────────────────────────────────────────────

async function boot() {
  S = await api(`/api/moment/${pk}/stage`);
  root.classList.toggle('long', S.stage?.shape === 'long');
  if (MODE === 'live') {
    fillSetup();
    fillThread();
    fillScene();
    fillRender();
    $('st-export').onclick = async () => {
      try { fillRender((await api(`/moment/${pk}/stage/render`, {})).render); } catch (e) { $('st-render-status').textContent = e.message; }
    };
  }
  setScene(S.stage?.scene);
  const stop = (text) => { say(text); fillInfo(); if (MODE !== 'live') document.title = 'error'; };
  if (!S.stage) return stop('Choose her model, a set and the shape, then press Set up.');
  if (!S.profile) return stop('No model for this stage: import one on the Models page, then choose it here.');
  if (!S.track?.ready) return stop(S.track?.error || 'The voice track is out of date: press Apply.');
  track = await (await fetch(S.track.url, { cache: 'no-store' })).json();
  await build3d();
  fillInfo();
  if (MODE === 'still') return still(parseFloat(q.get('t')) || 0);
  if (MODE === 'export') { document.title = 'ready'; return; }
  live();
}

window.stage3d.ready = boot().catch((err) => {
  console.error(err);
  say('Could not load: ' + (err?.message || err?.target?.src || err));
  document.title = 'error';
  throw err;
});
