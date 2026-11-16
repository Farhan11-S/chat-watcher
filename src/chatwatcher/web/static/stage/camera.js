// The camera for a shot (stage/script.py SHOTS, plus each set's own). The shared shots
// frame her face: [height above her eyes, distance], both in head-heights, so every model
// is framed alike whatever her size, hat or ears.
const DEG = Math.PI / 180;
export const SHOTS = { wide: [-2.5, 16], medium: [-1.25, 11.5], close: [-0.4, 5.4], face: [0, 3.7] };

// shot: {shot, yaw, push}; into: 0..1 through the shot (it creeps in by `push` meanwhile).
export function place(camera, shot, into, fig, set) {
  const push = (shot?.push || 0) * into;
  const own = set?.shots?.[shot?.shot];
  if (own) {
    own(camera, fig, push);
  } else {
    const [dy, dist] = SHOTS[shot?.shot] || SHOTS.medium;
    const d = dist * fig.headSize * (1 - push);
    const yaw = (shot?.yaw || 0) * DEG, ty = fig.head + dy * fig.headSize + (fig.lift || 0) * 0.75;
    camera.fov = 24;
    camera.position.set(Math.sin(yaw) * d, ty + fig.headSize * 0.25, Math.cos(yaw) * d);
    camera.lookAt(0, ty, 0);
  }
  camera.updateProjectionMatrix();
}
