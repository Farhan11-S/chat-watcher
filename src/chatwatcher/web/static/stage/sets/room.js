// The default set: her room at night. She sits in a gaming chair; a big screen at her left
// (the right of the frame) plays the stream footage of the moment, so any clip works: she
// reacts to what was on her stream. Built in code, nothing to license.
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { quiet, rng } from './paint.js';

const PIC_W = 1280, PIC_H = 720;

function skyline() {                 // the night view through her window
  const W = 1024, H = 640, c = document.createElement('canvas');
  c.width = W; c.height = H;
  const g = c.getContext('2d'), r = rng(11);
  const sky = g.createLinearGradient(0, 0, 0, H);
  sky.addColorStop(0, '#070a1c'); sky.addColorStop(0.65, '#1c1840'); sky.addColorStop(1, '#3a2350');
  g.fillStyle = sky; g.fillRect(0, 0, W, H);
  for (let i = 0; i < 140; i++) { g.fillStyle = `rgba(255,255,255,${0.2 + r() * 0.6})`; g.fillRect(r() * W, r() * H * 0.55, 1.5, 1.5); }
  for (let x = 0; x < W;) {
    const w = 40 + r() * 90, h = 120 + r() * 330, y = H - h;
    g.fillStyle = `hsl(${235 + r() * 20}, 30%, ${7 + r() * 6}%)`;
    g.fillRect(x, y, w, h);
    for (let wy = y + 10; wy < H - 8; wy += 14) for (let wx = x + 6; wx < x + w - 8; wx += 11) {
      if (r() < 0.32) { g.fillStyle = r() < 0.8 ? 'rgba(255,200,120,0.85)' : 'rgba(160,210,255,0.8)'; g.fillRect(wx, wy, 5, 7); }
    }
    x += w + r() * 6;
  }
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

function poster(seed, hue) {
  const c = document.createElement('canvas');
  c.width = 256; c.height = 360;
  const g = c.getContext('2d'), r = rng(seed);
  const bg = g.createLinearGradient(0, 0, 256, 360);
  bg.addColorStop(0, `hsl(${hue}, 70%, 55%)`); bg.addColorStop(1, `hsl(${hue + 60}, 70%, 30%)`);
  g.fillStyle = bg; g.fillRect(0, 0, 256, 360);
  for (let i = 0; i < 7; i++) {
    g.fillStyle = `hsla(${hue + r() * 120}, 80%, ${50 + r() * 30}%, 0.7)`;
    g.beginPath(); g.arc(r() * 256, r() * 360, 20 + r() * 70, 0, Math.PI * 2); g.fill();
  }
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

// fig: the figure (its standing heights). Returns what stage/main.js uses.
export function build(scene, fig) {
  const seatTop = fig.hip - 1.25, floor = fig.hip - fig.knee - 0.5, top = floor + 34;
  const set = new THREE.Group();
  scene.add(set);
  scene.background = new THREE.Color(0x0b0a16);
  scene.fog = new THREE.Fog(0x0b0a16, 70, 150);
  const mat = (color, extra = {}) => quiet(new THREE.MeshLambertMaterial({ color, ...extra }));
  const M = { wall: mat(0x2c2a48), side: mat(0x262441), floor: mat(0x2a1f22), rug: mat(0x4a3a6a), wood: mat(0x5a3d2c),
              dark: mat(0x16161f), seat: mat(0x232232), accent: mat(0xd2386c), metal: mat(0x55586a) };
  const box = (w, h, d, x, y, z, m, g = set) => { const b = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), m); b.position.set(x, y, z); g.add(b); return b; };
  const BACK = -15, LEFT = -30;

  // floor, rug, walls
  box(120, 0.4, 120, 10, floor - 0.2, 20, M.floor);
  const rug = new THREE.Mesh(new THREE.CircleGeometry(13, 40).rotateX(-Math.PI / 2), M.rug);
  rug.position.set(2, floor + 0.02, 4);
  set.add(rug);
  box(120, top - floor, 0.6, 10, (top + floor) / 2, BACK - 0.3, M.wall);
  box(0.6, top - floor, 80, LEFT - 0.3, (top + floor) / 2, 20, M.side);

  // the window behind her, with the city at night
  const win = new THREE.Mesh(new THREE.PlaneGeometry(20, 13), quiet(new THREE.MeshBasicMaterial({ map: skyline(), fog: false })));
  win.position.set(-12, fig.eyes + 3, BACK + 0.05);
  set.add(win);
  box(21, 0.7, 0.8, -12, fig.eyes + 3 + 6.85, BACK + 0.3, M.metal);
  box(21, 0.7, 1.4, -12, fig.eyes + 3 - 6.85, BACK + 0.5, M.metal);
  box(0.6, 13, 0.6, -12, fig.eyes + 3, BACK + 0.3, M.metal);

  // shelves with things on them, posters, an LED strip along the top
  const r = rng(5);
  for (const y of [fig.eyes - 3, fig.eyes + 3, fig.eyes + 9]) {
    box(16, 0.5, 3, 12, y, BACK + 1.5, M.wood);
    for (let x = 5; x < 19;) {
      const w = 0.8 + r() * 1.8, h = 1.5 + r() * 2.8;
      box(w, h, 1.8, x + w / 2, y + 0.25 + h / 2, BACK + 1.5, mat(new THREE.Color().setHSL(r(), 0.55, 0.45)));
      x += w + 0.2 + r() * 0.8;
    }
  }
  for (const [x, z, hue, seed] of [[LEFT + 0.05, 2, 300, 3], [LEFT + 0.05, 12, 190, 4]]) {
    const p = new THREE.Mesh(new THREE.PlaneGeometry(6, 8.4), quiet(new THREE.MeshLambertMaterial({ map: poster(seed, hue) })));
    p.rotation.y = Math.PI / 2;
    p.position.set(x, fig.eyes + 2, z);
    set.add(p);
  }
  const led = box(118, 0.35, 0.35, 10, top - 1.2, BACK + 0.4, quiet(new THREE.MeshBasicMaterial({ color: 0xff5fc8 })));
  led.material.toneMapped = false;
  const ledLight = new THREE.PointLight(0xff4fb8, 45, 60, 1.4);
  ledLight.position.set(0, top - 3, BACK + 4);
  set.add(ledLight);

  // her chair
  const chair = new THREE.Group();
  const cushion = new THREE.Mesh(new RoundedBoxGeometry(9.4, 1.8, 8, 3, 0.7), M.seat);
  cushion.position.set(0, seatTop - 0.9, -0.3);
  const back = new THREE.Mesh(new RoundedBoxGeometry(9.2, 13, 1.8, 3, 0.8), M.seat);
  back.position.set(0, seatTop + 6, -4.6); back.rotation.x = -0.14;
  const stripe = new THREE.Mesh(new RoundedBoxGeometry(2.4, 12, 1.9, 2, 0.5), M.accent);
  stripe.position.set(0, seatTop + 6.1, -4.55); stripe.rotation.x = -0.14;
  const pillow = new THREE.Mesh(new RoundedBoxGeometry(5.4, 2.4, 1.2, 3, 0.5), M.accent);
  pillow.position.set(0, seatTop + 11.2, -3.6); pillow.rotation.x = -0.14;
  chair.add(cushion, back, stripe, pillow);
  for (const s of [-5.3, 5.3]) {
    const arm = new THREE.Mesh(new RoundedBoxGeometry(1.1, 0.8, 6.2, 2, 0.3), M.dark);
    arm.position.set(s, seatTop + 2.6, -0.4);
    const post = new THREE.Mesh(new THREE.BoxGeometry(0.6, 3, 0.6), M.dark);
    post.position.set(s, seatTop + 1, -1.4);
    chair.add(arm, post);
  }
  const column = new THREE.Mesh(new THREE.CylinderGeometry(0.6, 0.6, seatTop - floor - 2.4, 12), M.metal);
  column.position.set(0, (seatTop + floor) / 2 - 0.6, -0.6);
  chair.add(column);
  for (let i = 0; i < 5; i++) {
    const leg = new THREE.Mesh(new THREE.BoxGeometry(0.7, 0.5, 6), M.dark);
    leg.position.set(Math.sin(i * 1.2566) * 2.6, floor + 0.7, -0.6 + Math.cos(i * 1.2566) * 2.6);
    leg.rotation.y = i * 1.2566;
    chair.add(leg);
  }
  set.add(chair);

  // the screen at her left, turned towards her and the camera
  const sc = { x: 15, y: fig.eyes - 0.5, z: 9, w: 24, h: 13.5, turn: -0.95 };
  const normal = new THREE.Vector3(Math.sin(sc.turn), 0, Math.cos(sc.turn));
  const screen = new THREE.Group();
  screen.position.set(sc.x, sc.y, sc.z);
  screen.rotation.y = sc.turn;
  const bezel = new THREE.Mesh(new RoundedBoxGeometry(sc.w + 1, sc.h + 1, 0.9, 2, 0.3), M.dark);
  bezel.position.z = -0.5;
  const canvas = document.createElement('canvas');
  canvas.width = PIC_W; canvas.height = PIC_H;
  const pic = canvas.getContext('2d');
  pic.fillStyle = '#05060a'; pic.fillRect(0, 0, PIC_W, PIC_H);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  const glass = new THREE.Mesh(new THREE.PlaneGeometry(sc.w, sc.h), quiet(new THREE.MeshBasicMaterial({ map: tex, fog: false })));
  glass.material.toneMapped = false;
  screen.add(bezel, glass);
  const stand = new THREE.Mesh(new THREE.BoxGeometry(14, sc.y - sc.h / 2 - floor - 0.5, 5), M.wood);
  stand.position.set(sc.x - 1, (floor + sc.y - sc.h / 2 - 0.5) / 2, sc.z - 2);
  stand.rotation.y = sc.turn;
  set.add(screen, stand);
  const glow = new THREE.PointLight(0xaab8ff, 40, 34, 1.3);
  glow.position.set(sc.x + normal.x * 6, sc.y, sc.z + normal.z * 6);
  set.add(glow);

  // a warm lamp behind her right shoulder
  const lamp = new THREE.PointLight(0xffc98a, 55, 40, 1.4);
  lamp.position.set(-16, fig.eyes + 4, -6);
  set.add(lamp);
  box(0.4, fig.eyes + 2 - floor, 0.4, -16, (fig.eyes + 2 + floor) / 2, -6, M.metal);
  const shade = new THREE.Mesh(new THREE.CylinderGeometry(1.6, 2.6, 3, 16, 1, true), quiet(new THREE.MeshBasicMaterial({ color: 0xffd9a8, side: THREE.DoubleSide })));
  shade.position.set(-16, fig.eyes + 3.5, -6);
  set.add(shade);

  const tint = document.createElement('canvas');
  tint.width = tint.height = 4;
  const tg = tint.getContext('2d', { willReadFrequently: true });
  let lastSrc = -1;
  return {
    feature: new THREE.Vector3(sc.x, sc.y, sc.z),
    shots: {
      over(camera, f, push) {        // behind her right shoulder, looking past her at the screen
        const head = new THREE.Vector3(0, f.head, 0), centre = new THREE.Vector3(sc.x, sc.y, sc.z);
        const back = head.clone().sub(centre).setY(0).normalize();          // from the screen towards her
        const right = new THREE.Vector3(back.z, 0, -back.x);                 // the camera's right
        const aim = head.clone().lerp(centre, 0.55).setY(f.head - f.headSize * 0.6);
        camera.fov = 32;
        camera.position.copy(head).addScaledVector(back, 13).addScaledVector(right, 3.2).setY(f.head + f.headSize * 0.9);
        camera.position.lerp(aim, push);
        camera.lookAt(aim);
      },
      screen(camera, f, push) {      // the screen fills the frame
        camera.fov = 30;
        const tan = Math.tan(15 * Math.PI / 180);
        const byH = (sc.h / 2) / tan * 1.08, byW = (sc.w / 2) / (tan * camera.aspect) * 1.04;
        const d = (camera.aspect < 1 ? Math.min(byW, byH * 1.15) : Math.max(byH, byW)) * (1 - push);
        camera.position.set(sc.x + normal.x * d, sc.y, sc.z + normal.z * d);
        camera.lookAt(sc.x, sc.y, sc.z);
      },
    },
    // picture: {video or image, region} — the stream footage, already at the right time
    // (the exporter hands it still frames: stage/export.py).
    update(t, lanes, picture) {
      const v = picture?.image || picture?.video;
      if (!v || (picture.video && !picture.image && v.readyState < 2)) return;
      const src = picture.image ? picture.image.src : v.currentTime;
      if (src === lastSrc && !picture.force) return;
      lastSrc = src;
      const vw = v.videoWidth || v.naturalWidth, vh = v.videoHeight || v.naturalHeight, g = picture.region;
      const sx = g ? g.x * vw : 0, sy = g ? g.y * vh : 0, sw = g ? g.w * vw : vw, sh = g ? g.h * vh : vh;
      const k = Math.min(PIC_W / sw, PIC_H / sh), dw = sw * k, dh = sh * k;
      pic.fillStyle = '#05060a';
      pic.fillRect(0, 0, PIC_W, PIC_H);
      pic.drawImage(v, sx, sy, sw, sh, (PIC_W - dw) / 2, (PIC_H - dh) / 2, dw, dh);
      tex.needsUpdate = true;
      tg.drawImage(canvas, 0, 0, 4, 4);              // the room takes the screen's colour
      const d = tg.getImageData(0, 0, 4, 4).data;
      let rr = 0, gg = 0, bb = 0;
      for (let i = 0; i < d.length; i += 4) { rr += d[i]; gg += d[i + 1]; bb += d[i + 2]; }
      const n = d.length / 4, lum = (rr + gg + bb) / (3 * 255 * n);
      glow.color.setRGB(0.35 + rr / n / 255 * 0.65, 0.35 + gg / n / 255 * 0.65, 0.4 + bb / n / 255 * 0.6);
      glow.intensity = 18 + 50 * lum;
    },
  };
}
