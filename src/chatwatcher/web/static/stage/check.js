// The model check page (templates/model.html): her model seated in the room, drawn in every
// pose, face and mouth shape, the parts that never settle when she sits, and her profile.
import * as THREE from 'three';
import { OutlineEffect } from 'three/addons/effects/OutlineEffect.js';
import { ACTIONS, Figure, loadModel, POSES, FACES } from './figure.js';
import { place } from './camera.js';
import * as room from './sets/room.js';

const $ = (id) => document.getElementById(id);
const box = $('mc');
const profile = JSON.parse($('mc-profile').textContent);
const W = 300, H = 400;
const quietTrack = (mouth = []) => ({ rate: 60, env: new Array(600).fill(mouth.length ? 1 : 0), mouth, laughs: [], mumbles: [], duration: 10 });
const sceneOf = (pose, face, shot) => ({ shots: [{ t: 0, shot, yaw: 12, push: 0 }], poses: [{ t: 0, pose }],
                                         faces: [{ t: 0, face }], lanes: {} });

function fillForm() {
  $('mc-seated').value = JSON.stringify(profile.seated, null, 1);
  $('mc-faces-json').value = JSON.stringify(profile.faces || {}, null, 1);
  $('mc-form').onsubmit = async (ev) => {
    ev.preventDefault();
    const status = $('mc-save-status');
    let changes;
    try {
      changes = { name: $('mc-name').value, vtuber: $('mc-vtuber').value, pmx: $('mc-pmx').value,
                  seated: JSON.parse($('mc-seated').value || '{}'), faces: JSON.parse($('mc-faces-json').value || '{}'),
                  mouth: parseFloat($('mc-mouth-gain').value) || 1,
                  notes: $('mc-notes').value, checked: $('mc-checked').checked };
    } catch (e) { status.textContent = 'Not valid JSON: ' + e.message; return; }
    const r = await fetch(`/models/${box.dataset.slug}/profile`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
                                                                    body: JSON.stringify({ changes }) });
    const d = await r.json();
    status.textContent = d.ok ? 'Saved. Reload to see the change.' : d.error;
  };
}

async function main() {
  fillForm();
  const status = $('mc-status');
  status.textContent = 'Loading physics…';
  window.Ammo = typeof window.Ammo === 'function' ? await window.Ammo() : window.Ammo;
  const { mesh, missingFiles } = await loadModel(box.dataset.url, (k) => { status.textContent = `Loading… ${Math.round(k * 100)}%`; });
  const fig = new Figure(mesh, profile);
  fig.sit();
  const scene3 = new THREE.Scene();
  scene3.add(new THREE.AmbientLight(0xdfe4ff, 1.35));
  const sun = new THREE.DirectionalLight(0xffffff, 1.15);
  sun.position.set(-1, 1.4, 1.6);
  scene3.add(sun);
  const set = room.build(scene3, fig);
  scene3.add(mesh);
  fig.start(10);
  const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
  renderer.setPixelRatio(1);
  const effect = new OutlineEffect(renderer);
  effect.setSize(W, H);
  const camera = new THREE.PerspectiveCamera(24, W / H, 1, 4000);

  const tile = (grid, label, scene, track, settle = 40) => {
    fig.settle(0, scene, track, settle);
    fig.apply(0, scene, track);
    place(camera, scene.shots[0], 0, fig, set);
    effect.render(scene3, camera);
    const fc = document.createElement('figure');
    fc.className = 'mc-cell';
    const img = document.createElement('img');
    img.src = renderer.domElement.toDataURL('image/jpeg', 0.85);
    img.alt = label;
    fc.append(img);
    const cap = document.createElement('figcaption');
    cap.textContent = label;
    fc.append(cap);
    $(grid).append(fc);
  };
  const pause = () => new Promise((ok) => setTimeout(ok, 0));
  let n = 0;
  for (const pose of Object.keys(POSES)) { status.textContent = `Drawing poses… ${++n}`; tile('mc-poses', pose, sceneOf(pose, 'neutral', 'medium'), quietTrack()); await pause(); }
  n = 0;
  for (const face of Object.keys(FACES)) {
    status.textContent = `Drawing faces… ${++n}`;
    const own = fig.morph['face:' + face] || [];
    tile('mc-faces', `${face}${own.length || !FACES[face].length ? '' : ' (nothing found)'}`, sceneOf('rest', face, 'face'), quietTrack(), 6);
    await pause();
  }
  for (const v of ['a', 'i', 'u', 'e', 'o']) { tile('mc-mouth', v, sceneOf('rest', 'neutral', 'face'), quietTrack([[-1, v, 1]]), 6); await pause(); }
  // each action as a filmstrip: she sits still, the action starts at 0.3 s
  const FRAMES = 8, SW = 150, SH = 200;
  for (const [name, a] of Object.entries(ACTIONS)) {
    status.textContent = `Drawing actions… ${name}`;
    const scene = { ...sceneOf('rest', 'neutral', 'medium'), actions: [{ t: 0.3, action: name, size: 1 }] };
    const strip = document.createElement('canvas');
    strip.width = SW * FRAMES; strip.height = SH;
    const g = strip.getContext('2d');
    const end = 0.3 + a.dur, want = Array.from({ length: FRAMES }, (_, i) => 0.3 + (a.dur * 0.85) * i / (FRAMES - 1));
    fig.settle(0, scene, quietTrack(), 30);
    let next = 0;
    for (let t = 0; t <= end && next < FRAMES; t += 1 / 30) {
      fig.apply(t, scene, quietTrack());
      fig.step(1 / 30);
      if (t + 1e-6 >= want[next]) {
        place(camera, scene.shots[0], 0, fig, set);
        effect.render(scene3, camera);
        g.drawImage(renderer.domElement, next * SW, 0, SW, SH);
        g.fillStyle = 'rgba(0,0,0,.55)'; g.fillRect(next * SW, SH - 16, 44, 16);
        g.fillStyle = '#fff'; g.font = '11px system-ui'; g.fillText(`${(t - 0.3).toFixed(2)}s`, next * SW + 4, SH - 4);
        next++;
      }
    }
    const fc = document.createElement('figure');
    const img = document.createElement('img');
    img.src = strip.toDataURL('image/jpeg', 0.85);
    img.alt = name;
    const cap = document.createElement('figcaption');
    cap.textContent = name;
    fc.append(img, cap);
    $('mc-actions').append(fc);
    await pause();
  }
  status.textContent = 'Measuring the physics…';
  await pause();
  const probe = fig.probe(0, sceneOf('rest', 'neutral', 'medium'), quietTrack());
  $('mc-probe').textContent = probe.map(([name, d]) => `${d >= 0.02 ? '⚠ ' : '  '}${name}  ${d}`).join('\n') +
    `\n\npinned while seated: ${fig.pinned.join(' ') || 'nothing'}`;
  $('mc-info').textContent = [
    `${mesh.skeleton.bones.length} bones, ${fig.morphNames.length} expressions, ${(mesh.geometry.userData.MMD?.rigidBodies || []).length} physics bodies`,
    `not in this model: ${[...fig.missing.bones, ...fig.missing.morphs].join(' ') || 'nothing'}`,
    missingFiles.length ? `files it names but doesn't have: ${missingFiles.join(', ')}` : 'every texture found',
    '', 'expressions:', fig.morphNames.join(' '),
  ].join('\n');
  status.textContent = '';
  document.title = 'ready';
}

main().catch((err) => { console.error(err); $('mc-status').textContent = 'Could not load: ' + (err?.message || err); document.title = 'error'; });
