// The plane cabin at night: her window seat, rows of seats, city lights far below, and a
// cloud that slides in front of them (the scene's `cloud` lane: -1 ahead, 0 inside, 1 past).
import * as THREE from 'three';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';
import { cityTexture, quiet, rng } from './paint.js';

function cloudTexture() {
  const W = 1024, H = 512, c = document.createElement('canvas');
  c.width = W; c.height = H;
  const g = c.getContext('2d'), r = rng(31);
  g.fillStyle = '#05070d';                             // inside the cloud it is simply dark
  g.fillRect(0, 0, W, H);
  for (let i = 0; i < 260; i++) {                      // lighter wisps towards both ends
    const side = r() < 0.5 ? 0 : 1, k = Math.pow(r(), 1.6) * 0.46;
    const x = (side ? 1 - k : k) * W, y = r() * H, rad = 30 + r() * 90;
    const glow = g.createRadialGradient(x, y, 0, x, y, rad);
    glow.addColorStop(0, `rgba(96,108,150,${0.10 + (0.46 - k) * 0.5})`);
    glow.addColorStop(1, 'rgba(96,108,150,0)');
    g.fillStyle = glow;
    g.fillRect(x - rad, y - rad, rad * 2, rad * 2);
  }
  g.globalCompositeOperation = 'destination-out';      // ragged, see-through ends
  for (let i = 0; i < 520; i++) {
    const side = r() < 0.5 ? 0 : 1, k = Math.pow(r(), 2.2) * 0.4;
    const x = (side ? 1 - k : k) * W, y = r() * H, rad = 24 + r() * 80 * (1 - k * 1.6);
    const hole = g.createRadialGradient(x, y, 0, x, y, rad);
    hole.addColorStop(0, `rgba(0,0,0,${0.5 * (1 - k / 0.4)})`);
    hole.addColorStop(1, 'rgba(0,0,0,0)');
    g.fillStyle = hole;
    g.fillRect(x - rad, y - rad, rad * 2, rad * 2);
  }
  const edge = g.createLinearGradient(0, 0, W, 0);     // and nothing at all at the very ends
  edge.addColorStop(0, 'rgba(0,0,0,1)'); edge.addColorStop(0.07, 'rgba(0,0,0,0)');
  edge.addColorStop(0.93, 'rgba(0,0,0,0)'); edge.addColorStop(1, 'rgba(0,0,0,1)');
  g.fillStyle = edge;
  g.fillRect(0, 0, W, H);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace;
  return t;
}

// ── the set ──────────────────────────────────────────────────────────────────
// fig: the figure (its standing heights). Returns what stage/main.js uses.
export function build(scene, fig) {
  const seatTop = fig.hip - 1.25, floor = fig.hip - fig.knee - 0.5;
  const WALL = 8.6, AISLE = -15.5, FAR = -44, PITCH = 12.5;
  const win = { x: WALL, y: fig.eyes - 0.4, z: 1.2, w: 3.3, h: 4.7 };
  const set = new THREE.Group();
  scene.add(set);
  scene.background = new THREE.Color(0x04050a);
  scene.fog = new THREE.Fog(0x04050a, 55, 125);
  const mat = (color) => quiet(new THREE.MeshLambertMaterial({ color }));
  const M = { wall: mat(0x474d66), trim: mat(0x6a7190), seat: mat(0x1d2444), head: mat(0x2a3360), floor: mat(0x0d0f18), bin: mat(0x555b76), shade: mat(0x8087a3) };

  // side walls with their windows cut out
  const hole = (z) => {
    const p = new THREE.Path(), x0 = z - win.w / 2, y0 = win.y - win.h / 2, rad = 1.25;
    p.moveTo(x0 + rad, y0);
    p.lineTo(x0 + win.w - rad, y0); p.quadraticCurveTo(x0 + win.w, y0, x0 + win.w, y0 + rad);
    p.lineTo(x0 + win.w, y0 + win.h - rad); p.quadraticCurveTo(x0 + win.w, y0 + win.h, x0 + win.w - rad, y0 + win.h);
    p.lineTo(x0 + rad, y0 + win.h); p.quadraticCurveTo(x0, y0 + win.h, x0, y0 + win.h - rad);
    p.lineTo(x0, y0 + rad); p.quadraticCurveTo(x0, y0, x0 + rad, y0);
    return p;
  };
  const wallShape = new THREE.Shape();
  wallShape.moveTo(-70, floor); wallShape.lineTo(70, floor); wallShape.lineTo(70, floor + 30); wallShape.lineTo(-70, floor + 30);
  const zs = [];
  for (let z = win.z - PITCH * 5; z < 66; z += PITCH / 2) zs.push(z);
  for (const z of zs) wallShape.holes.push(hole(z));
  const wallGeo = new THREE.ExtrudeGeometry(wallShape, { depth: 0.7, bevelEnabled: false, curveSegments: 6 });
  wallGeo.rotateY(-Math.PI / 2);                        // shape (z, y) → a wall facing the aisle
  const wall = new THREE.Mesh(wallGeo, M.wall);
  wall.position.x = WALL + 0.7;
  const far = new THREE.Mesh(wallGeo, M.wall);
  far.rotation.y = Math.PI; far.position.x = FAR - 0.7;
  set.add(wall, far);
  for (const z of zs) {                                 // a lighter rim round each window, a half-drawn shade on some
    const rim = new THREE.Shape(hole(z).getPoints(24).map((p) => new THREE.Vector2(z + (p.x - z) * 1.16, win.y + (p.y - win.y) * 1.12)));
    rim.holes.push(hole(z));
    const g = new THREE.ShapeGeometry(rim);
    g.rotateY(-Math.PI / 2);
    const m = new THREE.Mesh(g, M.trim);
    m.position.x = WALL - 0.03;
    set.add(m);
    if (Math.abs(z - win.z) > 1 && (Math.round(z * 7) % 3 === 0)) {
      const shade = new THREE.Mesh(new THREE.PlaneGeometry(win.w, win.h * 0.62), M.shade);
      shade.rotation.y = -Math.PI / 2;
      shade.position.set(WALL + 0.5, win.y + win.h * 0.19, z);
      set.add(shade);
    }
  }

  // floor, ceiling, the bins over the seats
  const slab = (w, h, d, x, y, z, m) => { const b = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), m); b.position.set(x, y, z); set.add(b); return b; };
  slab(WALL - FAR + 2, 0.4, 140, (WALL + FAR) / 2, floor - 0.2, 0, M.floor);
  slab(WALL - FAR + 2, 0.4, 140, (WALL + FAR) / 2, floor + 30, 0, M.wall);
  slab(7.5, 6, 140, WALL - 3.75, floor + 27, 0, M.bin);
  slab(7.5, 6, 140, FAR + 3.75, floor + 27, 0, M.bin);
  slab(0.5, 0.25, 140, AISLE + 2, floor + 0.05, 0, mat(0x4a3b20));     // floor path light strips
  slab(0.5, 0.25, 140, AISLE - 9, floor + 0.05, 0, mat(0x4a3b20));

  // seats: hers is at x = 0, one beside it, two more across the aisle
  const back = new RoundedBoxGeometry(9.6, 10.4, 1.5, 3, 0.6), cushion = new RoundedBoxGeometry(9.6, 1.7, 7.4, 3, 0.6);
  const rest = new RoundedBoxGeometry(6.4, 2.6, 0.9, 3, 0.4), arm = new RoundedBoxGeometry(0.9, 0.8, 6.4, 2, 0.3);
  for (let row = 0; row < 6; row++) for (const x of [0, -10.2, AISLE - 12, AISLE - 22.2]) {
    const z = -row * PITCH, seat = new THREE.Group();
    const b = new THREE.Mesh(back, M.seat);
    b.position.set(0, seatTop + 4.6, -4.3); b.rotation.x = -0.16;
    const h = new THREE.Mesh(rest, M.head);
    h.position.set(0, seatTop + 8.3, -4.3); h.rotation.x = -0.16;
    const cu = new THREE.Mesh(cushion, M.seat);
    cu.position.set(0, seatTop - 0.85, -0.2);
    seat.add(b, h, cu);
    for (const s of [-5.1, 5.1]) { const a = new THREE.Mesh(arm, M.head); a.position.set(s, seatTop + 1.9, -0.6); seat.add(a); }
    seat.position.set(x, 0, z);
    set.add(seat);
  }

  // outside: tilted ground of lights (as if banking over the city), and the cloud
  const city = cityTexture();
  city.repeat.set(4, 4);
  const ground = new THREE.Mesh(new THREE.PlaneGeometry(2400, 2400).rotateX(-Math.PI / 2),
    quiet(new THREE.MeshBasicMaterial({ map: city, fog: false })));
  ground.position.set(WALL + 560, win.y - 230, 0);
  ground.rotation.z = 0.36;
  ground.renderOrder = -2;
  const cloud = new THREE.Mesh(new THREE.PlaneGeometry(520, 300).rotateY(-Math.PI / 2),
    quiet(new THREE.MeshBasicMaterial({ map: cloudTexture(), transparent: true, depthWrite: false, fog: false })));
  cloud.renderOrder = -1;
  set.add(ground, cloud);

  // a warm reading light on her seat, cool moonlight through her window
  const lamp = new THREE.PointLight(0xffd9a0, 60, 26, 1.6);
  lamp.position.set(-1, floor + 23, 2);
  const glow = new THREE.PointLight(0x7f9cff, 30, 16, 1.5);
  glow.position.set(WALL - 0.5, win.y, win.z);
  set.add(lamp, glow);

  const feature = new THREE.Vector3(WALL, win.y, win.z);
  return {
    feature,
    shots: {
      over(camera, f, push) {        // from the aisle, past her head, to the window
        camera.fov = 40;
        camera.position.set(-10 + push * 8, f.head + 1.5, 4.2);
        camera.lookAt(win.x - 3.2, win.y - 0.6, win.z - 0.6);
      },
      pov(camera, f, push) {         // out through the glass, down at the ground
        camera.fov = 58;
        camera.position.set(WALL - 5.6 + push * 6, win.y + 1.6, win.z + 0.4);
        camera.lookAt(WALL + 60, win.y - 20, win.z - 4);
      },
    },
    update(t, lanes) {
      const where = lanes.cloud ?? -1;
      city.offset.y = -t * 0.0045;                      // the ground drifts backwards
      city.offset.x = t * 0.0006;
      cloud.position.set(WALL + 70, win.y - 45, -where * 330 + Math.sin(t * 0.8) * 3);
      glow.intensity = 30 * (0.25 + 0.75 * Math.min(Math.abs(where), 1));    // no city glow inside the cloud
    },
  };
}
