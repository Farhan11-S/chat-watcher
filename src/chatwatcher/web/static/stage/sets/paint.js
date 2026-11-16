// Painted textures and small helpers the sets share. Everything is drawn here, in code: no
// asset files to license.
import * as THREE from 'three';

export const quiet = (m) => { m.userData.outlineParameters = { visible: false }; return m; };
export function rng(seed) { return () => ((seed = (seed * 1664525 + 1013904223) >>> 0) / 4294967296); }

export function cityTexture() {
  const S = 2048, c = document.createElement('canvas');
  c.width = c.height = S;
  const g = c.getContext('2d'), r = rng(7);
  g.fillStyle = '#020308';
  g.fillRect(0, 0, S, S);
  g.globalCompositeOperation = 'lighter';
  const tint = () => { const v = r(); return v < 0.68 ? '255,176,84' : v < 0.9 ? '255,238,205' : v < 0.97 ? '150,205,255' : '255,90,70'; };
  const dot = (x, y, size, a, rgb = tint()) => {
    for (const ox of [0, S, -S]) for (const oy of [0, S, -S]) {     // drawn wrapped, so it tiles
      const px = ((x % S) + S) % S + ox, py = ((y % S) + S) % S + oy;
      if (px < -20 || px > S + 20 || py < -20 || py > S + 20) continue;
      const glow = g.createRadialGradient(px, py, 0, px, py, size * 4);
      glow.addColorStop(0, `rgba(${rgb},${a})`);
      glow.addColorStop(0.25, `rgba(${rgb},${a * 0.35})`);
      glow.addColorStop(1, `rgba(${rgb},0)`);
      g.fillStyle = glow;
      g.fillRect(px - size * 4, py - size * 4, size * 8, size * 8);
    }
  };
  const towns = [];
  for (let i = 0; i < 26; i++) towns.push([r() * S, r() * S, 60 + r() * r() * 330]);
  for (const [cx, cy, rad] of towns) {                 // street grids, denser in the middle
    const turn = r() * Math.PI, step = 13 + r() * 9, n = Math.ceil(rad / step);
    for (let i = -n; i <= n; i++) for (let j = -n; j <= n; j++) {
      const d = Math.hypot(i, j) / n;
      if (d > 1 || r() < 0.25 + d * 0.6) continue;
      const lx = i * step + (r() - 0.5) * 4, ly = j * step + (r() - 0.5) * 4;
      dot(cx + lx * Math.cos(turn) - ly * Math.sin(turn), cy + lx * Math.sin(turn) + ly * Math.cos(turn),
        1.6 + r() * 1.8, 0.55 + r() * 0.45);
    }
    dot(cx, cy, rad * 0.16, 0.10, '255,170,90');        // the haze over a town
  }
  for (let i = 0; i < towns.length; i++) {             // roads between neighbouring towns
    const [ax, ay] = towns[i], [bx, by] = towns[(i + 1 + Math.floor(r() * 3)) % towns.length];
    const bend = (r() - 0.5) * 260, len = Math.hypot(bx - ax, by - ay);
    for (let s = 0; s < len; s += 11) {
      const k = s / len, w = Math.sin(k * Math.PI) * bend;
      dot(ax + (bx - ax) * k - (by - ay) / len * w, ay + (by - ay) * k + (bx - ax) / len * w, 1.5, 0.6, '255,196,120');
    }
  }
  for (let i = 0; i < 900; i++) dot(r() * S, r() * S, 1 + r() * 1.4, 0.25 + r() * 0.5);   // farms, lone houses
  const t = new THREE.CanvasTexture(c);
  t.wrapS = t.wrapT = THREE.RepeatWrapping;
  t.colorSpace = THREE.SRGBColorSpace;
  t.anisotropy = 8;
  return t;
}
