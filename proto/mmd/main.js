// Throwaway prototype: an MMD model lip-synced to one Short's audio, with blinks, a few
// poses, faces and camera shots, all read from scene.json. Every value drawn is a pure
// function of the time t (physics aside), so ?t=12.3 gives the same picture as playback.
import * as THREE from 'three';
import { MMDLoader } from 'three/addons/loaders/MMDLoader.js';
import { MMDAnimationHelper } from 'three/addons/animation/MMDAnimationHelper.js';
import { OutlineEffect } from 'three/addons/effects/OutlineEffect.js';
import { buildStage } from './stage.js';

const $ = (id) => document.getElementById(id);
const q = new URLSearchParams(location.search);
const STILL = q.has('t') ? parseFloat(q.get('t')) : null;
const DEG = Math.PI / 180;

// ── what the script's words mean ─────────────────────────────────────────────
// On top of the model's rest pose (an A-pose). Arms: `down` lowers the arm towards her side,
// `fwd` swings it forward, an elbow's `bend` folds the forearm forward. Others: degrees [x, y, z].
const ARM = (down, fwd = 0) => ({ down, fwd }), ELBOW = (bend) => ({ bend });
const POSES = {
  rest:     { '左腕': ARM(34), '右腕': ARM(34), '左ひじ': ELBOW(10), '右ひじ': ELBOW(10) },
  explain:  { '左腕': ARM(30, 22), '右腕': ARM(34), '左ひじ': ELBOW(105), '右ひじ': ELBOW(10) },
  window:   { '左腕': ARM(34), '右腕': ARM(34), '左ひじ': ELBOW(10), '右ひじ': ELBOW(10),
              '首': [0, 18, 0], '頭': [-5, 16, 0], '上半身': [0, 8, 0] },
  shock:    { '左腕': ARM(24, 15), '右腕': ARM(24, 15), '左ひじ': ELBOW(130), '右ひじ': ELBOW(130),
              '上半身': [-8, 0, 0], '頭': [-5, 0, 0] },
  phone:    { '左腕': ARM(34), '左ひじ': ELBOW(10), '右腕': ARM(30, 48), '右ひじ': ELBOW(95),
              '頭': [5, 0, 5], '上半身': [3, -6, 0] },
  laugh:    { '左腕': ARM(30, 8), '右腕': ARM(30, 8), '左ひじ': ELBOW(75), '右ひじ': ELBOW(75),
              '上半身': [9, 0, 0], '頭': [8, 0, 6] },
  sheepish: { '左腕': ARM(34), '左ひじ': ELBOW(10), '右腕': ARM(28, 30), '右ひじ': ELBOW(145),
              '頭': [6, -10, -8], '首': [0, -6, 0] },
};
const CHILD = { '左腕': '左ひじ', '右腕': '右ひじ', '左ひじ': '左手首', '右ひじ': '右手首' };
// A face is morph weights; each entry lists the names to try, first one the model has wins.
const FACES = {
  neutral:  [],
  smile:    [[['にこり', 'にっこり'], 0.6], [['口角上げ', 'にやり'], 0.6], [['にこり口'], 0.35]],
  worried:  [[['困る', '困り'], 0.9], [['口角下げ'], 0.5], [['目尻_下げ', 'じと目'], 0.4]],
  shock:    [[['びっくり', '驚き'], 1.0], [['上', '眉上'], 0.9], [['瞳小'], 0.7], [['青ざめ'], 0.8]],
  excited:  [[['上', '眉上'], 0.5], [['きらきら目'], 1.0], [['口角上げ', 'にやり'], 0.6]],
  laugh:    [[['笑い', 'にっこり目'], 1.0], [['にこり', 'にっこり'], 0.7], [['笑い口', 'ワ'], 0.5]],
  sheepish: [[['困る', '困り'], 0.8], [['照れ', '頬染め'], 1.0], [['汗'], 1.0], [['口角上げ', 'にやり'], 0.4], [['じと目'], 0.25]],
};
const EYES_SHUT = new Set(['laugh']);
const VOWELS = { a: ['あ'], i: ['い'], u: ['う'], e: ['え'], o: ['お'] };
const BLINK = ['まばたき'];
const LAUGH = { mouth: ['大笑い', '笑い口', 'ワ'], eyes: ['笑い', 'にっこり目'], brow: ['にこり', 'にっこり'] };
// Shots frame the head: target height and distance in head-heights, lens fixed.
const SHOTS = { wide: [-2.5, 16], medium: [-1.25, 11.5], close: [-0.4, 5.4], face: [0, 3.7] };
// Two more that look at her window: `over` past her head from the aisle, `pov` out through the glass.
// She sits: thighs forward, shins down. Degrees [x, y, z] on the rest pose, under every pose.
const SEATED = { '左足': [-82, 0, 0], '右足': [-82, 0, 0], '左ひざ': [80, 0, 0], '右ひざ': [80, 0, 0] };

// ── small helpers ────────────────────────────────────────────────────────────
const clamp01 = (v) => Math.min(1, Math.max(0, v));
const smooth = (v) => { v = clamp01(v); return v * v * (3 - 2 * v); };
function cueAt(list, t) {            // → [previous, current, 0..1 into the ease]
  let i = 0;
  while (i + 1 < list.length && list[i + 1].t <= t) i++;
  const cur = list[i], prev = list[Math.max(i - 1, 0)];
  return [prev, cur, i === 0 ? 1 : smooth((t - cur.t) / (cur.ease ?? 0.3))];
}
function rng(seed) { return () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296); }

// ── load ─────────────────────────────────────────────────────────────────────
const scene3 = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(24, 9 / 16, 1, 4000);
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
const effect = new OutlineEffect(renderer);
scene3.add(new THREE.AmbientLight(0xdfe4ff, 1.35));
const sun = new THREE.DirectionalLight(0xffffff, 1.15);
sun.position.set(-1, 1.4, 1.6);
scene3.add(sun);

let script, track, mesh, helper, physics, stage, head = 17, headSize = 2.2;
const bones = {}, rest = {}, seatRest = {}, lap = {}, dir = {}, morph = {}, missing = { bones: [], morphs: [] };
const audio = new Audio();
let blinks = [];

async function load() {
  script = await (await fetch('scene.json', { cache: 'no-store' })).json();
  track = await (await fetch(script.track, { cache: 'no-store' })).json();
  audio.src = script.audio;
  audio.preload = 'auto';
  window.Ammo = await window.Ammo();
  const manager = new THREE.LoadingManager();     // PMX files name textures with Windows backslashes
  manager.setURLModifier((url) => url.replace(/\\|%5C/gi, '/'));
  const textures = new Promise((ok) => { manager.onLoad = ok; });
  manager.onError = (url) => console.error('missing file', url);
  mesh = await new Promise((ok, bad) => new MMDLoader(manager).load(script.model, ok, undefined, bad));
  await textures;                                 // her textures arrive after the mesh
  scene3.add(mesh);

  for (const b of mesh.skeleton.bones) bones[b.name] = b;
  for (const pose of Object.values(POSES)) for (const name of Object.keys(pose)) {
    if (bones[name]) rest[name] = bones[name].quaternion.clone();
    else if (!missing.bones.includes(name)) missing.bones.push(name);
  }
  for (const [name, child] of Object.entries(CHILD)) if (bones[child]) dir[name] = bones[child].position.clone().normalize();
  for (const name of ['上半身', '上半身2', '頭', '首', '左肩', '右肩']) if (bones[name] && !rest[name]) rest[name] = bones[name].quaternion.clone();
  const dict = mesh.morphTargetDictionary || {};
  const pick = (names) => names.find((n) => n in dict);
  const want = (names) => {
    const n = pick(names);
    if (!n && !missing.morphs.includes(names[0])) missing.morphs.push(names[0]);
    return n;
  };
  for (const [v, names] of Object.entries(VOWELS)) morph[v] = want(names);
  morph.blink = want(BLINK);
  for (const [k, names] of Object.entries(LAUGH)) morph['laugh:' + k] = want(names);
  for (const [face, parts] of Object.entries(FACES)) morph['face:' + face] = parts.map(([names, w]) => [want(names), w]).filter(([n]) => n);

  mesh.updateMatrixWorld(true);
  const hb = bones['頭'], eye = bones['左目'] || bones['右目'];  // not 両目: that one floats above the head
  if (hb && eye) {                  // frame by the face itself: hats and ears don't count
    const eyes = eye.getWorldPosition(new THREE.Vector3()).y;
    headSize = eyes * 0.17;         // an anime head is about a sixth of the eye height
    head = eyes - headSize * 0.1;
  }

  const height = (name) => bones[name]?.getWorldPosition(new THREE.Vector3()).y;
  for (const name of Object.keys(SEATED)) if (bones[name]) seatRest[name] = bones[name].quaternion.clone();
  stage = buildStage(scene3, { hip: height('左足') ?? 10, knee: height('左ひざ') ?? 6, eyes: height('左目') ?? head });

  // Sitting bends the legs through the skirt, and its physics can't follow (the joints fight the
  // legs and it thrashes). So the skirt and the plushies on it stop being simulated and are laid
  // over her lap by hand: front strips fold forward with the thighs, back ones stay.
  const hipZ = bones['左足']?.getWorldPosition(new THREE.Vector3()).z ?? 0;
  for (const rb of mesh.geometry.userData.MMD?.rigidBodies || []) {
    const b = mesh.skeleton.bones[rb.boneIndex];
    if (!b || !/^(Skirt|Tail)_/.test(b.name) || /_Nui$/.test(b.name)) continue;   // the plushies stay simulated; her tail is pinned behind her
    rb.type = 0;                                    // 0 = follows its bone
    if (!/_01$/.test(b.name) || lap[b.name]) continue;
    const p = b.getWorldPosition(new THREE.Vector3());
    const front = clamp01((p.z - hipZ + 0.3) / 0.9);
    const fold = b.name === 'Tail_01'
      ? new THREE.Euler(55 * DEG, 0, 0)             // straight back, behind the seat, not down through the floor
      : new THREE.Euler(-80 * front * DEG, 0, Math.sign(p.x) * (Math.abs(p.x) > 1.2 ? 10 : 0) * front * DEG, 'YXZ');
    lap[b.name] = b.quaternion.clone().multiply(new THREE.Quaternion().setFromEuler(fold));
  }

  helper = new MMDAnimationHelper({ afterglow: 0 });
  helper.add(mesh, { physics: true });
  physics = helper.objects.get(mesh).physics;

  const r = rng(57);
  for (let t = 1.2; t < track.duration; t += 1.8 + r() * 3.2) blinks.push(t);

  $('stage').prepend(renderer.domElement);
  $('msg').remove();
  $('seek').max = track.duration;
  info();
}

function info() {
  const dict = Object.keys(mesh.morphTargetDictionary || {});
  $('info').textContent =
    `model: ${script.model}\n${mesh.skeleton.bones.length} bones, ${dict.length} morphs\n` +
    `words clock offset: ${track.word_offset}s\n\n` +
    `missing bones: ${missing.bones.join(' ') || 'none'}\n` +
    `missing morphs: ${missing.morphs.join(' ') || 'none'}\n\n` +
    `all morphs:\n${dict.join(' ')}`;
}

// ── the picture at time t ────────────────────────────────────────────────────
function env(t, span = 3) {          // loudness around t, averaged over `span` samples
  const i = Math.round(t * track.rate);
  let s = 0, n = 0;
  for (let k = i - span + 1; k <= i; k++) if (k >= 0 && k < track.env.length) { s += track.env[k]; n++; }
  return n ? s / n : 0;
}
const REST = [0, 'a', 0];
function mouthAt(t) {                // → [cue now, cue before, 0..1 crossfade]
  const v = track.mouth;
  let lo = 0, hi = v.length - 1, i = -1;
  while (lo <= hi) { const m = (lo + hi) >> 1; if (v[m][0] <= t) { i = m; lo = m + 1; } else hi = m - 1; }
  if (i < 0) return [REST, REST, 1];
  return [v[i], i > 0 ? v[i - 1] : REST, smooth((t - v[i][0]) / (v[i][2] === 0 ? 0.045 : 0.07))];
}
function spanAt(spans, t, ease = 0.14) {   // 0..1: how far inside one of [start, end] spans t is
  let best = 0;
  for (const [a, b] of spans || []) if (t > a - ease && t < b + ease) best = Math.max(best, smooth(Math.min(t - a + ease, b + ease - t) / (2 * ease)));
  return best;
}
const laughAt = (t) => spanAt(track.laughs, t);

const qa = new THREE.Quaternion(), qb = new THREE.Quaternion(), qc = new THREE.Quaternion(), e = new THREE.Euler();
const X = new THREE.Vector3(1, 0, 0), Z = new THREE.Vector3(0, 0, 1), axis = new THREE.Vector3();
function poseQuat(pose, name, out) {
  const a = POSES[pose]?.[name];
  if (!a) return out.identity();
  if (Array.isArray(a)) return out.setFromEuler(e.set(a[0] * DEG, a[1] * DEG, a[2] * DEG, 'YXZ'));
  const d = dir[name];               // which way the bone points at rest
  if (!d) return out.identity();
  if (a.bend !== undefined) return out.setFromAxisAngle(axis.crossVectors(d, Z).normalize(), a.bend * DEG);
  out.setFromAxisAngle(X, -a.fwd * DEG);
  return out.multiply(qc.setFromAxisAngle(Z, (d.x > 0 ? -1 : 1) * a.down * DEG));
}

function apply(t) {
  // body: blend the two poses, then add the little movements that keep her alive
  const [p0, p1, pk] = cueAt(script.poses, t);
  const loud = env(t, 8), ha = laughAt(t), burst = ha * clamp01(env(t, 2) * 1.6);
  const live = {
    '上半身': [Math.sin(t * 1.7) * 0.9 + ha * 3 + burst * 5, Math.sin(t * 0.53) * 2.2, Math.sin(t * 0.71) * 1.3],
    '上半身2': [burst * 4, 0, 0],
    '左肩':   [0, 0, burst * 5], '右肩': [0, 0, -burst * 5],
    '首':     [0, Math.sin(t * 0.43 + 1) * 3, 0],
    '頭':     [loud * 4 - 1 + Math.sin(t * 2.9) * loud * 2 - ha * 7 + burst * 11, Math.sin(t * 0.37) * 4, Math.sin(t * 0.61 + 2) * 2.5],
    '左腕':   [0, 0, Math.sin(t * 1.7) * 0.8], '右腕': [0, 0, -Math.sin(t * 1.7) * 0.8],
  };
  for (const [name, a] of Object.entries(SEATED)) {
    if (seatRest[name]) bones[name].quaternion.copy(seatRest[name]).multiply(qa.setFromEuler(e.set(a[0] * DEG, a[1] * DEG, a[2] * DEG, 'YXZ')));
  }
  for (const [name, quat] of Object.entries(lap)) bones[name].quaternion.copy(quat);
  for (const name of Object.keys(rest)) {
    poseQuat(p0.pose, name, qa).slerp(poseQuat(p1.pose, name, qb), pk);
    const l = live[name];
    if (l) qa.multiply(qb.setFromEuler(e.set(l[0] * DEG, l[1] * DEG, l[2] * DEG, 'YXZ')));
    bones[name].quaternion.copy(rest[name]).multiply(qa);
  }

  // face: expression, then blink, then mouth on top
  const w = mesh.morphTargetInfluences, dict = mesh.morphTargetDictionary;
  if (w) {
    w.fill(0);
    const add = (name, v) => { if (name) w[dict[name]] = clamp01(w[dict[name]] + v); };
    const [f0, f1, fk] = cueAt(script.faces, t);
    for (const [n, v] of morph['face:' + f0.face] || []) add(n, v * (1 - fk));
    for (const [n, v] of morph['face:' + f1.face] || []) add(n, v * fk);
    const shut = (EYES_SHUT.has(f1.face) ? fk : 0) + (EYES_SHUT.has(f0.face) ? 1 - fk : 0);
    const b = blinks.find((x) => t >= x && t < x + 0.17);
    if (b !== undefined && shut < 0.5 && ha < 0.5) add(morph.blink, 1 - Math.abs((t - b) / 0.085 - 1));

    // laughing: happy shut eyes, and a wide mouth that pumps with each "ha"
    add(morph['laugh:eyes'], ha * 0.95);
    add(morph['laugh:brow'], ha * 0.5);
    add(morph['laugh:mouth'], ha * 0.3 + burst * 0.7);
    add(morph.a, burst * 0.2);
    // talking: the shape of the letter being said, opened by how loud she is
    const amp = (0.42 + 0.58 * clamp01(env(t, 3) * 1.5)) * (1 - ha);
    const gain = { a: 1, i: 0.8, u: 0.85, e: 0.8, o: 0.9 };
    const [c1, c0, ck] = mouthAt(t);
    add(morph[c0[1]], c0[2] * gain[c0[1]] * amp * (1 - ck));
    add(morph[c1[1]], c1[2] * gain[c1[1]] * amp * ck);
    const mum = spanAt(track.mumbles, t);          // words nobody wrote down: flap with the loudness
    if (mum > 0) {
      const beat = t / 0.17, v = ['a', 'e', 'o', 'a', 'i'], k = smooth((beat % 1) * 2.5);
      const open = mum * clamp01(env(t, 3) * 1.5);
      add(morph[v[Math.floor(beat - 1 + 5) % 5]], open * (1 - k));
      add(morph[v[Math.floor(beat) % 5]], open * k);
    }
  }

  // camera: hold the shot, drift in a little the whole time it is up
  const [, s] = cueAt(script.shots, t);
  const next = script.shots.find((x) => x.t > s.t)?.t ?? track.duration;
  const into = clamp01((t - s.t) / Math.max(next - s.t, 0.01));
  const win = stage.window, push = (s.push || 0) * into;
  if (s.shot === 'pov') {            // out through the glass, down at the ground
    camera.fov = 58;
    camera.position.set(win.x - 5.6 + push * 6, win.y + 1.6, win.z + 0.4);
    camera.lookAt(win.x + 60, win.y - 20, win.z - 4);
  } else if (s.shot === 'over') {    // from the aisle, past her head, to the window
    camera.fov = 40;
    camera.position.set(-10 + push * 8, head + 1.5, 4.2);
    camera.lookAt(win.x - 3.2, win.y - 0.6, win.z - 0.6);
  } else {
    const [dy, dist] = SHOTS[s.shot] || SHOTS.medium;
    const d = dist * headSize * (1 - push);
    const yaw = (s.yaw || 0) * DEG, ty = head + dy * headSize;
    camera.fov = 24;
    camera.position.set(Math.sin(yaw) * d, ty + headSize * 0.25, Math.cos(yaw) * d);
    camera.lookAt(0, ty, 0);
  }
  camera.updateProjectionMatrix();

  // outside the window: where the cloud is
  const [o0, o1, ok] = cueAt(script.outside, t);
  stage.update(t, o0.cloud + (o1.cloud - o0.cloud) * ok);

  const line = track.lines.find((l) => t >= l.t0 && t < l.t1);
  $('caption').textContent = line ? line.text.replace(/[*"]/g, '') : '';
}

function size() {
  const { clientWidth: w, clientHeight: h } = $('stage');
  effect.setSize(w, h);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
}

// ── run ──────────────────────────────────────────────────────────────────────
let last = performance.now(), lastT = 0;
function frame(now) {
  const t = audio.paused ? parseFloat($('seek').value) : audio.currentTime;
  const dt = Math.min((now - last) / 1000, 0.05);
  last = now;
  if (Math.abs(t - lastT) > 0.5) physics?.reset();
  lastT = t;
  apply(t);
  helper.update(dt);
  effect.render(scene3, camera);
  if (!audio.paused) $('seek').value = t;
  $('clock').textContent = t.toFixed(1);
  requestAnimationFrame(frame);
}

load().then(() => {
  size();
  addEventListener('resize', size);
  if (STILL !== null) {
    document.body.classList.add('still');
    size();
    apply(STILL);
    physics?.reset();
    for (let i = 0; i < 90; i++) { apply(STILL); helper.update(1 / 60); }
    effect.render(scene3, camera);
    if (q.has('probe')) {            // which simulated bones are still moving once she has settled?
      const at = new Map(), speed = new Map(), v = new THREE.Vector3();
      for (let i = 0; i < 180; i++) {
        apply(STILL); helper.update(1 / 60); mesh.updateMatrixWorld(true);
        for (const b of mesh.skeleton.bones) {
          b.getWorldPosition(v);
          if (at.has(b)) speed.set(b, (speed.get(b) || 0) + at.get(b).distanceTo(v) / 179);
          at.set(b, v.clone());
        }
      }
      const top = [...speed].sort((a, b) => b[1] - a[1]).slice(0, 10).map(([b, d]) => `${b.name} ${d.toFixed(3)}`);
      console.log('PROBE units per step: ' + top.join(' | '));
    }
    document.title = 'ready';
    return;
  }
  $('play').onclick = () => {
    if (audio.paused) { audio.currentTime = parseFloat($('seek').value); audio.play(); } else audio.pause();
  };
  audio.onplay = () => { $('play').textContent = 'Pause'; };
  audio.onpause = () => { $('play').textContent = 'Play'; };
  audio.onended = () => { $('seek').value = 0; };
  $('seek').oninput = () => { if (!audio.paused) audio.currentTime = parseFloat($('seek').value); };
  addEventListener('keydown', (ev) => { if (ev.code === 'Space') { ev.preventDefault(); $('play').click(); } });
  requestAnimationFrame(frame);
}).catch((err) => {
  console.error(err);
  const m = $('msg') || $('caption');
  m.textContent = 'Could not load: ' + (err?.message || err?.target?.src || err);
  document.title = 'error';
});
