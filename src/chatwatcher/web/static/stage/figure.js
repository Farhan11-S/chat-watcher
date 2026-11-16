// The talent: her MMD model, seated, posed and given a face at any time t from the scene
// (stage/script.py names) and the voice track (stage/track.py). The model's own quirks come
// from its profile (stage/models.py): which simulated parts are pinned when she sits, extra
// folds, and its own morphs for a face.
import * as THREE from 'three';
import { MMDLoader } from 'three/addons/loaders/MMDLoader.js';
import { MMDAnimationHelper } from 'three/addons/animation/MMDAnimationHelper.js';
import { actionPose, backOut, blinkAt, blinkTimes, clamp01, cueAt, envAt, envelopeAt, mouthAt, smooth, spanAt } from './cues.js';

const DEG = Math.PI / 180;

// On top of the model's rest pose (an A-pose). Arms: `down` lowers the arm towards her side
// (34 = by her side, negative raises it), `fwd` swings it forward; an elbow's `bend` folds
// the forearm forward. Anything else: degrees [x, y, z] (x + = nod down, y + = turn to her left).
const ARM = (down, fwd = 0) => ({ down, fwd }), ELBOW = (bend) => ({ bend });
const DOWN = { '左腕': ARM(34), '右腕': ARM(34), '左ひじ': ELBOW(10), '右ひじ': ELBOW(10) };
export const POSES = {
  rest:     { ...DOWN },
  explain:  { ...DOWN, '左腕': ARM(30, 22), '左ひじ': ELBOW(105) },
  look:     { ...DOWN, '首': [0, 18, 0], '頭': [-5, 16, 0], '上半身': [0, 8, 0] },
  shock:    { '左腕': ARM(24, 15), '右腕': ARM(24, 15), '左ひじ': ELBOW(130), '右ひじ': ELBOW(130),
              '上半身': [-8, 0, 0], '頭': [-5, 0, 0] },
  phone:    { ...DOWN, '右腕': ARM(30, 48), '右ひじ': ELBOW(95), '頭': [5, 0, 5], '上半身': [3, -6, 0] },
  laugh:    { '左腕': ARM(30, 8), '右腕': ARM(30, 8), '左ひじ': ELBOW(75), '右ひじ': ELBOW(75),
              '上半身': [9, 0, 0], '頭': [8, 0, 6] },
  sheepish: { ...DOWN, '右腕': ARM(28, 30), '右ひじ': ELBOW(145), '頭': [6, -10, -8], '首': [0, -6, 0] },
  point:    { ...DOWN, '左腕': ARM(-22, 40), '左ひじ': ELBOW(8), '首': [0, 16, 0], '頭': [-3, 12, 0], '上半身': [0, 10, 0] },
  think:    { ...DOWN, '右腕': ARM(30, 38), '右ひじ': ELBOW(140), '頭': [6, -6, 9], '首': [0, -4, 0] },
  cheer:    { '左腕': ARM(-5, 30), '右腕': ARM(-5, 30), '左ひじ': ELBOW(115), '右ひじ': ELBOW(115),
              '上半身': [-6, 0, 0], '頭': [-6, 0, 0] },
};
const CHILD = { '左腕': '左ひじ', '右腕': '右ひじ', '左ひじ': '左手首', '右ひじ': '右手首' };

// Cartoon actions (Tom & Jerry): the exaggeration is in her body, never her mouth. Each is a
// short move on top of whatever pose she is in, made of extremes: an anticipation (a small
// wind-up the other way), the hit (snap: arrives in a few frames), an overshoot and a wobble,
// then back to her pose. Values add to the pose: degrees [x, y, z] (x + = lean/nod forward,
// y + = turn to her left), arms {up, fwd} degrees (up raises, fwd swings forward), elbows
// {bend}, and center [x, y, z] moves her whole body (y up = she hops off the seat, z + =
// towards the camera), in model units (she is about 20 tall). face: eye morphs over [from,
// to] of the action; shake: the camera's shake over time (0..1). stage/script.py lists them for Claude.
const BOTH = (up, fwd = 0) => ({ '左腕': { up, fwd }, '右腕': { up, fwd } });
const BENT = (bend) => ({ '左ひじ': { bend }, '右ひじ': { bend } });
const WIDE_EYES = [[['びっくり', '驚き'], 1], [['瞳小'], 0.9], [['上', '眉上'], 0.7]];
export const ACTIONS = {
  take: { dur: 1.3, shake: [[0.2, 0], [0.25, 1], [0.6, 0.2], [0.9, 0]], face: { parts: WIDE_EYES, from: 0.2, to: 1.15 },
    keys: [[0, {}],
      [0.16, { center: [0, -0.5, 0.5], '上半身': [14, 0, 0], '首': [6, 0, 0], '頭': [12, 0, 0], ...BOTH(-8, 12), ...BENT(20) }],
      [0.25, { snap: 1, center: [0, 1.5, -0.6], '上半身': [-22, 0, 0], '首': [-8, 0, 0], '頭': [-16, 0, 0], ...BOTH(80, 30), ...BENT(35) }],
      [0.42, { center: [0, 0.4, -0.3], '上半身': [-10, 0, 0], '頭': [-6, 0, 0], ...BOTH(45, 20), ...BENT(25) }],
      [0.6, { center: [0, 0.7, -0.4], '上半身': [-14, 0, 0], '頭': [-9, 0, 0], ...BOTH(55, 22), ...BENT(28) }],
      [1.3, {}]] },
  jolt: { dur: 0.7, shake: [[0.1, 0], [0.13, 0.45], [0.35, 0]], face: { parts: WIDE_EYES, from: 0.1, to: 0.6 },
    keys: [[0, {}], [0.07, { center: [0, -0.25, 0], '上半身': [5, 0, 0] }],
      [0.14, { snap: 1, center: [0, 0.7, -0.2], '上半身': [-9, 0, 0], '頭': [-8, 0, 0], '左肩': [0, 0, 8], '右肩': [0, 0, -8], ...BOTH(22, 8) }],
      [0.32, { center: [0, 0.15, 0], '上半身': [-3, 0, 0], ...BOTH(6) }], [0.7, {}]] },
  slump: { dur: 2.2, face: { parts: [[['困る', '困り'], 0.7], [['青ざめ'], 0.8], [['汗', '汗2', '汗左+汗右'], 1]], from: 0.15, to: 0.95 },
    keys: [[0, {}],
      [0.5, { center: [0, -0.7, 0.2], '上半身': [18, 0, 0], '上半身2': [6, 0, 0], '首': [10, 0, 0], '頭': [16, 0, -4], '左肩': [0, 0, -7], '右肩': [0, 0, 7], ...BOTH(-6) }],
      [1.7, { center: [0, -0.8, 0.2], '上半身': [21, 0, 0], '上半身2': [7, 0, 0], '首': [11, 0, 0], '頭': [19, 0, -5], '左肩': [0, 0, -8], '右肩': [0, 0, 8], ...BOTH(-7) }],
      [2.2, {}]] },
  bounce: { dur: 1.3, face: { parts: [[['きらきら目', 'キラキラ目', 'キラキラ', '星目'], 1], [['上', '眉上'], 0.5]], from: 0.05, to: 1.1 },
    keys: [[0, {}], [0.08, { center: [0, -0.35, 0], ...BOTH(0, 15), ...BENT(90) }],
      [0.2, { snap: 1, center: [0, 1.0, 0], '頭': [-6, 0, 0], ...BOTH(35, 35), ...BENT(110) }],
      [0.34, { center: [0, -0.25, 0], ...BOTH(5, 20), ...BENT(95) }],
      [0.48, { snap: 1, center: [0, 0.85, 0], '頭': [-5, 0, 4], ...BOTH(35, 35), ...BENT(110) }],
      [0.62, { center: [0, -0.2, 0], ...BOTH(5, 20), ...BENT(95) }],
      [0.76, { snap: 1, center: [0, 0.7, 0], '頭': [-5, 0, -4], ...BOTH(30, 30), ...BENT(105) }],
      [0.95, { center: [0, 0, 0], ...BOTH(5, 15), ...BENT(60) }], [1.3, {}]] },
  lean_in: { dur: 2.0,
    keys: [[0, {}], [0.12, { center: [0, 0, -0.2], '上半身': [-4, 0, 0] }],
      [0.35, { snap: 1, center: [0, 0.1, 1.0], '上半身': [18, 0, 0], '首': [2, 0, 0], '頭': [-8, 0, 0] }],
      [1.5, { center: [0, 0.1, 1.1], '上半身': [20, 0, 0], '頭': [-9, 0, 3] }], [2.0, {}]] },
  recoil: { dur: 1.0, shake: [[0.08, 0], [0.11, 0.5], [0.3, 0]], face: { parts: WIDE_EYES, from: 0.08, to: 0.8 },
    keys: [[0, {}], [0.1, { snap: 1, center: [0, 0.35, -0.6], '上半身': [-15, 0, 0], '頭': [-5, 0, 0], ...BOTH(30, 45), ...BENT(75) }],
      [0.3, { center: [0, 0.15, -0.4], '上半身': [-10, 0, 0], '頭': [-3, 0, 0], ...BOTH(22, 38), ...BENT(65) }], [1.0, {}]] },
  shake_head: { dur: 0.95,
    keys: [[0, {}], [0.1, { snap: 1, '頭': [2, 24, 0], '首': [0, 10, 0] }], [0.24, { snap: 1, '頭': [2, -24, 0], '首': [0, -10, 0] }],
      [0.38, { snap: 1, '頭': [2, 20, 0], '首': [0, 8, 0] }], [0.52, { snap: 1, '頭': [2, -18, 0], '首': [0, -7, 0] }],
      [0.68, { '頭': [1, 8, 0] }], [0.95, {}]] },
  nod: { dur: 0.95,
    keys: [[0, {}], [0.12, { snap: 1, '頭': [18, 0, 0], '首': [7, 0, 0] }], [0.26, { '頭': [-4, 0, 0] }],
      [0.4, { snap: 1, '頭': [16, 0, 0], '首': [6, 0, 0] }], [0.54, { '頭': [-3, 0, 0] }],
      [0.68, { snap: 1, '頭': [12, 0, 0], '首': [4, 0, 0] }], [0.95, {}]] },
  laugh_fit: { dur: 2.4, face: { parts: [[['笑い', 'にっこり目'], 1]], from: 0.05, to: 0.95 },
    keys: [[0, {}], [0.2, { center: [0, 0.2, 0.3], '上半身': [20, 0, 0], '頭': [14, 0, 0], ...BOTH(0, 25), ...BENT(70) }],
      [0.5, { center: [0, 0.6, -0.3], '上半身': [-14, 0, 0], '頭': [-20, 0, 6], ...BOTH(10, 15), ...BENT(50) }],
      [0.8, { center: [0, 0.2, 0.3], '上半身': [22, 0, 0], '頭': [15, 0, -4], ...BOTH(0, 28), ...BENT(75) }],
      [1.1, { center: [0, 0.5, -0.2], '上半身': [-12, 0, 0], '頭': [-18, 0, 6], ...BOTH(8, 15), ...BENT(50) }],
      [1.45, { center: [0, 0.1, 0.25], '上半身': [18, 0, 0], '頭': [12, 0, 0], ...BOTH(0, 22), ...BENT(65) }],
      [1.8, { '上半身': [6, 0, 0], '頭': [4, 0, 0] }], [2.4, {}]] },
  facepalm: { dur: 1.9, face: { parts: [[['困る', '困り'], 0.8], [['汗', '汗2', '汗左+汗右'], 1]], from: 0.15, to: 0.9 },
    keys: [[0, {}], [0.1, { '頭': [-6, 0, 0], '右腕': { up: 0, fwd: 15 } }],
      [0.3, { snap: 1, '上半身': [10, 0, 0], '頭': [20, 0, 0], '右腕': { up: 25, fwd: 60 }, '右ひじ': { bend: 140 } }],
      [1.5, { '上半身': [12, 0, 0], '頭': [22, 0, 3], '右腕': { up: 25, fwd: 62 }, '右ひじ': { bend: 142 } }], [1.9, {}]] },
  shrug: { dur: 1.4,
    keys: [[0, {}], [0.12, { '左肩': [0, 0, -3], '右肩': [0, 0, 3] }],
      [0.3, { snap: 1, '左肩': [0, 0, 12], '右肩': [0, 0, -12], '頭': [0, 0, 10], ...BOTH(18, 20), ...BENT(70) }],
      [1.0, { '左肩': [0, 0, 10], '右肩': [0, 0, -10], '頭': [0, 0, 8], ...BOTH(16, 18), ...BENT(65) }], [1.4, {}]] },
  tremble: { dur: 1.3, face: { parts: [[['青ざめ'], 0.8], [['瞳小'], 0.6], [['困る', '困り'], 0.5]], from: 0.05, to: 0.95 },
    keys: [[0, {}], ...Array.from({ length: 22 }, (_, i) => [0.05 + i * 0.05, {
      center: [(i % 2 ? 0.07 : -0.07), -0.2, 0], '上半身': [6, (i % 2 ? 2.5 : -2.5), 0], '頭': [6, 0, (i % 2 ? -2.5 : 2.5)],
      ...BOTH(-4, 10), ...BENT(40) }]), [1.3, {}]] },
  whip_look: { dur: 1.3, face: { parts: [[['瞳小'], 0.5]], from: 0.05, to: 0.9 },
    keys: [[0, {}], [0.06, { '頭': [0, -5, 0] }], [0.14, { snap: 1, '首': [0, 24, 0], '頭': [-4, 24, 0], '上半身': [0, 12, 0] }],
      [0.24, { '首': [0, 20, 0], '頭': [-3, 18, 0], '上半身': [0, 10, 0] }], [0.95, { '首': [0, 20, 0], '頭': [-3, 19, 0], '上半身': [0, 10, 0] }],
      [1.3, {}]] },
};
const ALIVE = ['上半身', '上半身2', '頭', '首', '左肩', '右肩'];

// A face is morph weights; each part lists names to try, the first the model has wins
// ("左口角上げ+右口角上げ": both, for models that only have the two sides). A profile's
// `faces` replaces these for that model ({face: [[morph, weight], …]}).
const SMILE_EYES = ['にこり', 'にっこり', 'まゆにこり', 'にこ', 'ニコ', '微笑', '微笑み'];
const CORNERS_UP = ['口角上げ', '左口角上げ+右口角上げ', '口角左上げ+口角右上げ'];
const CORNERS_DOWN = ['口角下げ', '左口角下げ+右口角下げ', '口角左下げ+口角右下げ'];
const GRIN = ['にやり', 'にっ', 'ニッ', ...CORNERS_UP];
const SPARKLE = ['きらきら目', 'キラキラ目', 'キラキラ', 'きらきら', '星目', '瞳星', '星'];
const BLUSH = ['照れ', '頬染め', '赤面', '顔赤', 'ほほ染め'];
const SWEAT = ['汗', '汗2', '汗左+汗右', '汗左'];
export const FACES = {
  neutral:  [],
  smile:    [[SMILE_EYES, 0.6], [CORNERS_UP, 0.6], [['にこり口'], 0.35]],
  smug:     [[GRIN, 0.8], [['じと目', 'ジト目'], 0.5], [['にこり'], 0.3]],
  worried:  [[['困る', '困り'], 0.9], [CORNERS_DOWN, 0.5], [['目尻_下げ', 'じと目'], 0.4]],
  sad:      [[['困る', '困り'], 1.0], [['涙'], 0.8], [CORNERS_DOWN, 0.6], [['目尻_下げ'], 0.4]],
  angry:    [[['怒り'], 0.9], [['ムッ', 'むっ'], 0.7], [['ムカッ'], 1.0], [['キリッ'], 0.3]],
  shock:    [[['びっくり', '驚き'], 1.0], [['上', '眉上'], 0.9], [['瞳小'], 0.7], [['青ざめ'], 0.8], [['▲', 'お'], 0.45]],
  excited:  [[['上', '眉上'], 0.5], [SPARKLE, 1.0], [GRIN, 0.6]],
  laugh:    [[['笑い', 'にっこり目'], 1.0], [SMILE_EYES, 0.7], [['笑い口', 'ワ'], 0.5]],
  sheepish: [[['困る', '困り'], 0.8], [BLUSH, 1.0], [SWEAT, 1.0], [GRIN, 0.4], [['じと目'], 0.25]],
};
const EYES_SHUT = new Set(['laugh']);
const VOWELS = { a: ['あ', 'a'], i: ['い', 'i'], u: ['う', 'u'], e: ['え', 'e'], o: ['お', 'o'] };
const BLINK = ['まばたき', 'blink', 'Blink'];
const LAUGH = { mouth: ['大笑い', '笑い口', 'にこり口', 'ワ'], eyes: ['笑い', 'にっこり目'], brow: SMILE_EYES };
// She sits: thighs forward, shins down (degrees on the rest pose, under every pose).
const SEATED = { '左足': [-82, 0, 0], '右足': [-82, 0, 0], '左ひざ': [80, 0, 0], '右ひざ': [80, 0, 0] };
const GAIN = { a: 1, i: 0.8, u: 0.85, e: 0.8, o: 0.9 };


const qa = new THREE.Quaternion(), qb = new THREE.Quaternion(), qc = new THREE.Quaternion();
const eu = new THREE.Euler(), X = new THREE.Vector3(1, 0, 0), Z = new THREE.Vector3(0, 0, 1), axis = new THREE.Vector3();
const euler = (a, out) => out.setFromEuler(eu.set(a[0] * DEG, a[1] * DEG, a[2] * DEG, 'YXZ'));

export async function loadModel(url, onProgress) {
  const manager = new THREE.LoadingManager();      // PMX files name textures with Windows backslashes
  manager.setURLModifier((u) => u.replace(/\\|%5C/gi, '/'));
  const textures = new Promise((ok) => { manager.onLoad = ok; });
  const missing = [];
  manager.onError = (u) => missing.push(decodeURIComponent(u.split('/').pop()));
  const mesh = await new Promise((ok, bad) => new MMDLoader(manager).load(url, ok,
    (e) => onProgress && e.total && onProgress(e.loaded / e.total), bad));
  await textures;                                   // her textures arrive after the mesh
  return { mesh, missingFiles: missing };
}

export class Figure {
  constructor(mesh, profile = {}) {
    this.mesh = mesh;
    this.profile = profile;
    this.bones = {};
    this.rest = {};
    this.seatRest = {};
    this.lap = {};
    this.dir = {};
    this.morph = {};
    this.missing = { bones: [], morphs: [] };
    for (const b of mesh.skeleton.bones) this.bones[b.name] = b;
    for (const pose of Object.values(POSES)) for (const name of Object.keys(pose)) {
      if (this.bones[name]) this.rest[name] = this.bones[name].quaternion.clone();
      else if (!this.missing.bones.includes(name)) this.missing.bones.push(name);
    }
    for (const name of ALIVE) if (this.bones[name] && !this.rest[name]) this.rest[name] = this.bones[name].quaternion.clone();
    for (const a of Object.values(ACTIONS)) for (const [, k] of a.keys) for (const name of Object.keys(k)) {
      if (this.bones[name] && !this.rest[name]) this.rest[name] = this.bones[name].quaternion.clone();
    }
    this.centerBone = this.bones['センター'] || this.bones['全ての親'] || null;
    this.centerRest = this.centerBone?.position.clone();
    for (const [name, child] of Object.entries(CHILD)) {
      if (this.bones[child]) this.dir[name] = this.bones[child].position.clone().normalize();
    }
    this._faces();
    mesh.updateMatrixWorld(true);
    const at = (name) => this.bones[name]?.getWorldPosition(new THREE.Vector3());
    const eye = at('左目') || at('右目');           // not 両目: that one floats above the head
    this.eyes = eye ? eye.y : 16;
    this.headSize = this.eyes * 0.17;                // an anime head is about a sixth of the eye height
    this.head = this.eyes - this.headSize * 0.1;
    this.hip = at('左足')?.y ?? this.eyes * 0.58;
    this.knee = at('左ひざ')?.y ?? this.eyes * 0.35;
    this.hipZ = at('左足')?.z ?? 0;
  }

  _faces() {
    const dict = this.mesh.morphTargetDictionary || {};
    const want = (names) => {
      const n = names.find((x) => x.split('+').every((y) => y in dict));
      if (!n && !this.missing.morphs.includes(names[0])) this.missing.morphs.push(names[0]);
      return n;
    };
    for (const [v, names] of Object.entries(VOWELS)) this.morph[v] = want(names);
    this.morph.blink = want(BLINK);
    for (const [k, names] of Object.entries(LAUGH)) this.morph['laugh:' + k] = want(names);
    const quiet = (names) => names.find((x) => x.split('+').every((y) => y in dict));   // no "missing" note for these
    for (const [name, a] of Object.entries(ACTIONS)) {
      this.morph['act:' + name] = (a.face?.parts || []).map(([names, w]) => [quiet(names), w]).filter(([n]) => n);
    }
    const own = this.profile.faces || {};
    for (const [face, parts] of Object.entries(FACES)) {
      this.morph['face:' + face] = own[face]
        ? own[face].filter(([n]) => n.split('+').every((y) => y in dict))
        : parts.map(([names, w]) => [want(names), w]).filter(([n]) => n);
    }
  }

  get morphNames() { return Object.keys(this.mesh.morphTargetDictionary || {}); }

  // Sitting bends the legs through skirts and tails, whose physics can't follow (the joints
  // fight the legs and thrash). So they stop being simulated and are laid over her lap: the
  // first simulated bone of each chain folds forward as far as it hangs in front of her.
  sit() {
    for (const name of Object.keys(SEATED)) if (this.bones[name]) this.seatRest[name] = this.bones[name].quaternion.clone();
    const seated = this.profile.seated || { pin: 'lower', keep: [], fold: {} };
    const keep = (seated.keep || []).map((p) => new RegExp(p));
    const pats = seated.pin === 'lower' ? null : (seated.pin || []).map((p) => new RegExp(p));
    const under = (b, name) => { for (let p = b.parent; p; p = p.parent) if (p.name === name) return true; return false; };
    const bodies = this.mesh.geometry.userData.MMD?.rigidBodies || [];
    const pinned = new Set();
    for (const rb of bodies) {
      const b = this.mesh.skeleton.bones[rb.boneIndex];
      if (!b || rb.type === 0 || keep.some((k) => k.test(b.name))) continue;
      if (pats ? pats.some((p) => p.test(b.name)) : under(b, '下半身')) { rb.type = 0; pinned.add(b); }
    }
    this.pinned = [...pinned].map((b) => b.name);
    const v = new THREE.Vector3();
    for (const b of pinned) {
      if (pinned.has(b.parent)) continue;            // only each chain's first bone moves
      b.getWorldPosition(v);
      const front = clamp01((v.z - this.hipZ + 0.3) / 0.9);
      if (front <= 0) continue;
      const side = Math.abs(v.x) > 1.2 ? Math.sign(v.x) * 10 : 0;
      this.lap[b.name] = b.quaternion.clone().multiply(euler([-80 * front, 0, side * front], qa));
    }
    for (const [name, a] of Object.entries(seated.fold || {})) {
      const b = this.bones[name];
      if (b) this.lap[name] = b.quaternion.clone().multiply(euler(a, qa));
    }
  }

  start(duration) {
    this.helper = new MMDAnimationHelper({ afterglow: 0 });
    this.helper.add(this.mesh, { physics: true });
    this.physics = this.helper.objects.get(this.mesh).physics;
    this.blinks = blinkTimes(duration);
  }

  // The actions playing at t, summed: {bone: value} offsets, the eye morphs, and the camera shake.
  actionsAt(t, scene) {
    const bones = {}, face = [];
    let shake = 0;
    for (const cue of scene.actions || []) {
      const a = ACTIONS[cue.action];
      const local = t - cue.t;
      if (!a || local < 0 || local > a.dur) continue;
      const size = cue.size ?? 1;
      for (const [name, v] of Object.entries(actionPose(a.keys, local))) {
        const acc = bones[name] || (bones[name] = Array.isArray(v) ? [0, 0, 0] : {});
        if (Array.isArray(v)) v.forEach((x, j) => { acc[j] += x * size; });
        else for (const [f, x] of Object.entries(v)) acc[f] = (acc[f] || 0) + x * size;
      }
      if (a.shake) shake += envelopeAt(a.shake, local) * size;
      if (a.face) {
        const x = local / a.dur, { from, to } = a.face;
        const w = clamp01(Math.min((x - from) / 0.06, (to - x) / 0.1)) * Math.min(size, 1);
        if (w > 0) for (const [n, k] of this.morph['act:' + cue.action] || []) face.push([n, k * w]);
      }
    }
    return { bones, face, shake };
  }

  _offsetQuat(name, v, out) {
    if (Array.isArray(v)) return euler(v, out);
    out.identity();
    const d = this.dir[name];
    if (!d) return out;
    if (v.bend) out.multiply(qc.setFromAxisAngle(axis.crossVectors(d, Z).normalize(), v.bend * DEG));
    if (v.up) out.multiply(qc.setFromAxisAngle(Z, (d.x > 0 ? 1 : -1) * v.up * DEG));
    if (v.fwd) out.multiply(qc.setFromAxisAngle(X, -v.fwd * DEG));
    return out;
  }

  _poseQuat(pose, name, out) {
    const a = POSES[pose]?.[name];
    if (!a) return out.identity();
    if (Array.isArray(a)) return euler(a, out);
    const d = this.dir[name];                        // which way the bone points at rest
    if (!d) return out.identity();
    if (a.bend !== undefined) return out.setFromAxisAngle(axis.crossVectors(d, Z).normalize(), a.bend * DEG);
    out.setFromAxisAngle(X, -a.fwd * DEG);
    return out.multiply(qc.setFromAxisAngle(Z, (d.x > 0 ? -1 : 1) * a.down * DEG));
  }

  // Her body and face at t.
  apply(t, scene, track) {
    const [p0, p1] = cueAt(scene.poses, t);
    const pk = p0 === p1 ? 1 : backOut((t - p1.t) / (p1.ease ?? 0.3));   // snaps in and overshoots a little
    const act = this.actionsAt(t, scene);
    this.shake = act.shake;
    const loud = envAt(track, t, 8), ha = spanAt(track.laughs, t), burst = ha * clamp01(envAt(track, t, 2) * 1.6);
    const live = {
      '上半身': [Math.sin(t * 1.7) * 0.9 + ha * 3 + burst * 5, Math.sin(t * 0.53) * 2.2, Math.sin(t * 0.71) * 1.3],
      '上半身2': [burst * 4, 0, 0],
      '左肩': [0, 0, burst * 5], '右肩': [0, 0, -burst * 5],
      '首': [0, Math.sin(t * 0.43 + 1) * 3, 0],
      '頭': [loud * 4 - 1 + Math.sin(t * 2.9) * loud * 2 - ha * 7 + burst * 11, Math.sin(t * 0.37) * 4, Math.sin(t * 0.61 + 2) * 2.5],
      '左腕': [0, 0, Math.sin(t * 1.7) * 0.8], '右腕': [0, 0, -Math.sin(t * 1.7) * 0.8],
    };
    for (const [name, a] of Object.entries(SEATED)) {
      if (this.seatRest[name]) this.bones[name].quaternion.copy(this.seatRest[name]).multiply(euler(a, qa));
    }
    for (const [name, quat] of Object.entries(this.lap)) this.bones[name].quaternion.copy(quat);
    for (const name of Object.keys(this.rest)) {
      this._poseQuat(p0?.pose, name, qa).slerp(this._poseQuat(p1?.pose, name, qb), pk);
      const l = live[name];
      if (l) qa.multiply(euler(l, qb));
      if (act.bones[name]) qa.multiply(this._offsetQuat(name, act.bones[name], qb));
      this.bones[name].quaternion.copy(this.rest[name]).multiply(qa);
    }
    this.lift = (act.bones.center || [0, 0, 0])[1];       // the camera follows most of a hop
    if (this.centerBone) {
      const c = act.bones.center || [0, 0, 0];
      this.centerBone.position.set(this.centerRest.x + c[0], this.centerRest.y + c[1], this.centerRest.z + c[2]);
    }

    const w = this.mesh.morphTargetInfluences, dict = this.mesh.morphTargetDictionary, m = this.morph;
    if (!w) return;
    w.fill(0);
    const add = (name, v) => { if (name) for (const n of name.split('+')) w[dict[n]] = clamp01(w[dict[n]] + v); };
    const [f0, f1, fk] = cueAt(scene.faces, t);
    for (const [n, v] of m['face:' + f0?.face] || []) add(n, v * (1 - fk));
    for (const [n, v] of m['face:' + f1?.face] || []) add(n, v * fk);
    for (const [n, v] of act.face) add(n, v);
    const shut = (EYES_SHUT.has(f1?.face) ? fk : 0) + (EYES_SHUT.has(f0?.face) ? 1 - fk : 0);
    if (shut < 0.5 && ha < 0.5) add(m.blink, blinkAt(this.blinks || [], t));
    // laughing: happy shut eyes, and a wide mouth that pumps with each "ha"
    add(m['laugh:eyes'], ha * 0.95);
    add(m['laugh:brow'], ha * 0.5);
    add(m['laugh:mouth'], ha * 0.3 + burst * 0.7);
    add(m.a, burst * 0.2);
    // talking: the shape of the letter being said, opened by how loud she is
    const amp = (0.42 + 0.58 * clamp01(envAt(track, t, 3) * 1.5)) * (1 - ha) * (this.profile.mouth ?? 1);
    const [c1, c0, ck] = mouthAt(track, t);
    add(m[c0[1]], Math.min(1, c0[2] * GAIN[c0[1]] * amp) * (1 - ck));
    add(m[c1[1]], Math.min(1, c1[2] * GAIN[c1[1]] * amp) * ck);
    const mum = spanAt(track.mumbles, t);           // talk nobody wrote down: flap with the loudness
    if (mum > 0) {
      const beat = t / 0.17, v = ['a', 'e', 'o', 'a', 'i'], k = smooth((beat % 1) * 2.5);
      const open = mum * clamp01(envAt(track, t, 3) * 1.5);
      add(m[v[Math.floor(beat - 1 + 5) % 5]], open * (1 - k));
      add(m[v[Math.floor(beat) % 5]], open * k);
    }
  }

  step(dt) { this.helper.update(dt); }
  reset() { this.physics?.reset(); }

  // Hold her still at t until the simulated parts settle.
  settle(t, scene, track, steps = 90) {
    this.apply(t, scene, track);
    this.reset();
    for (let i = 0; i < steps; i++) { this.apply(t, scene, track); this.step(1 / 60); }
  }

  // Which bones still move once she has settled at t (a part that never settles thrashes).
  probe(t, scene, track, steps = 180) {
    const at = new Map(), speed = new Map(), v = new THREE.Vector3();
    for (let i = 0; i < steps; i++) {
      this.apply(t, scene, track); this.step(1 / 60); this.mesh.updateMatrixWorld(true);
      for (const b of this.mesh.skeleton.bones) {
        b.getWorldPosition(v);
        if (at.has(b)) speed.set(b.name, (speed.get(b.name) || 0) + at.get(b).distanceTo(v) / (steps - 1));
        at.set(b, v.clone());
      }
    }
    return [...speed].sort((a, b) => b[1] - a[1]).slice(0, 12).map(([name, d]) => [name, +d.toFixed(4)]);
  }
}
