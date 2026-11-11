"""The intro: a teaser montage from the clip → a glitch into the brand colour → the logo
reveal (logo pops in, the name drops in letter by letter, tagline, then a white push
into the edit). Built frame by frame with Pillow and encoded with ffmpeg.

Teasers are [start, end, gain_db, style] in clip time; style "normal" (punch-in at the
cut, then a slow push) or "blast" (shake, colour split, red tint: for a scream or a loud
fail). Animation timings are written as frames at 30 fps and scaled to the clip's rate."""

from __future__ import annotations

import math
import random
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np
from PIL import Image, ImageDraw

from chatwatcher.brand import art, sfx
from chatwatcher.brand.brand import Brand
from chatwatcher.clip.media import AUDIO_FROM_ZERO, MediaError, _exe, probe

STYLES = ("normal", "blast")
MAX_TEASERS = 8
MIN_TEASER_S = 0.3
MAX_TEASER_S = 6.0
MAX_TOTAL_S = 16.0
GAIN_RANGE = (-20.0, 6.0)
GLITCH_F30 = 8            # frames at 30 fps
LOGO_F30 = 96             # 3.2 s
HOOK_BEATS = {"hook", "payoff", "reaction", "callback"}


class IntroError(Exception):
    pass


@dataclass
class Rendered:
    path: Path
    duration_s: float
    size_bytes: int


def logo_seconds() -> float:
    return (GLITCH_F30 + LOGO_F30) / 30


# ── teasers ──────────────────────────────────────────────────────────────────

def clean(raw: Any, duration: float) -> list[list[Any]]:
    """Check teasers from the browser (clip time). Raises IntroError."""
    if not isinstance(raw, list):
        raise IntroError("Teasers must be a list.")
    if len(raw) > MAX_TEASERS:
        raise IntroError(f"At most {MAX_TEASERS} teasers.")
    out = []
    for i, t in enumerate(raw, 1):
        try:
            s, e = float(t[0]), float(t[1])
            g = float(t[2]) if len(t) > 2 else 0.0
        except (TypeError, ValueError, IndexError):
            raise IntroError(f"Teaser {i} isn't [start, end, gain, style].")
        if not all(math.isfinite(v) for v in (s, e, g)):
            raise IntroError(f"Teaser {i} has a bad number.")
        style = t[3] if len(t) > 3 and t[3] in STYLES else "normal"
        s, e = max(0.0, s), min(e, duration)
        if e - s < MIN_TEASER_S:
            raise IntroError(f"Teaser {i} is shorter than {MIN_TEASER_S:g} s.")
        e = min(e, s + MAX_TEASER_S)
        out.append([round(s, 3), round(e, 3), round(min(max(g, GAIN_RANGE[0]), GAIN_RANGE[1]), 1), style])
    if sum(e - s for s, e, *_ in out) > MAX_TOTAL_S:
        raise IntroError(f"The teasers add up to more than {MAX_TOTAL_S:g} s; keep the intro short.")
    return out


def pick(segments: list[list[float]], cues: list[dict[str, Any]], lines: list[dict[str, Any]],
         peaks: list[int], rate: int, hits: list[tuple[float, float]], beats: list[dict[str, Any]],
         words: list[list[float]] | None = None, max_lines: int = 3) -> list[list[Any]]:
    """Teasers chosen from the edit: the loudest subtitle lines (more if they're longer, in
    Claude's hook / payoff beats, or end in ! or ?), plus the biggest sudden loud moment as
    a "blast" with the line that leads into it. Long lines are cut after about 3 s, at
    the end of a word (words: Whisper's [start, end] times). All inside the edit's
    segments, in clip order."""
    spans = [(s, e) for s, e, *_ in segments]
    ends = sorted(float(w[1]) for w in words or [])

    def inside(a: float, b: float) -> bool:
        return any(s - 0.05 <= a and b <= e + 0.05 for s, e in spans)

    def loud(a: float, b: float) -> float:
        part = peaks[int(a * rate):max(int(a * rate) + 1, int(b * rate))]
        return sum(part) / len(part) / 100 if part else 0.0

    def trim(a: float, b: float, most: float = 3.0) -> float:
        if b - a <= most + 0.4:
            return b
        fits = [e for e in ends if a + 1.2 <= e <= a + most]
        return fits[-1] + 0.12 if fits else a + most

    cands = []
    for c in cues:
        a = float(c["t0"])
        b = trim(a, float(c["t1"]))
        if b - a >= 0.8 and inside(a, b):
            cands.append((a, b, str(c.get("text", ""))))
    if not cands:
        for i, ln in enumerate(lines):
            a = float(ln["t"])
            b = min(float(lines[i + 1]["t"]) if i + 1 < len(lines) else a + 3.0, a + 3.0)
            if b - a >= 0.8 and inside(a, b):
                cands.append((a, b, str(ln.get("text", ""))))

    def score(c: tuple[float, float, str]) -> float:
        a, b, text = c
        s = loud(a, b) + 0.3 * min(b - a, 2.5) / 2.5        # a whole sentence teases better than "Oh."
        if any(bt.get("beat") in HOOK_BEATS and bt["start"] < b and a < bt["end"] for bt in beats):
            s += 0.4
        t = text.strip()
        if "*" in t or t.endswith(("!", "?", "！", "？")):
            s += 0.25
        return s

    chosen: list[tuple[float, float]] = []
    total = 0.0
    for a, b, _ in sorted(cands, key=score, reverse=True):
        if len(chosen) >= max_lines or total + (b - a) > 7.5:
            continue
        if any(a < y + 1.5 and x - 1.5 < b for x, y in chosen):
            continue
        chosen.append((a, b))
        total += b - a
    if len(chosen) < 2:                       # no usable lines: the loudest stretches instead
        windows = []
        for s, e in spans:
            t = s
            while t + 1.6 <= e:
                windows.append((t, t + 1.6))
                t += 0.8
        for a, b in sorted(windows, key=lambda w: loud(*w), reverse=True):
            if len(chosen) >= max_lines:
                break
            if not any(a < y + 1.5 and x - 1.5 < b for x, y in chosen):
                chosen.append((a, b))
    blast = None
    for t, rise in sorted(hits, key=lambda h: -h[1]):
        a, b = t - 0.1, t + 0.75
        if rise >= 10 and inside(a, b):
            blast = (max(0.0, a), b)
            break
    if blast:
        a, b = blast
        kept = []
        for x, y in chosen:
            if y <= a or x >= b:
                kept.append((x, y))
            elif a - x >= 0.8:                # runs into the blast: end it there
                kept.append((x, a))
        chosen = kept
        # The line just before it sets it up ("if I record this…" → feedback screech): keep it.
        lead = [(x, min(y, a)) for x, y, _ in cands if a - 3.5 <= x <= a - 0.8 and y >= a - 1.5]
        if lead and not any(x < a and a - 0.3 <= y for x, y in chosen):
            x, y = max(lead, key=lambda c: c[1])
            if len(chosen) >= max_lines:
                chosen.remove(min(chosen, key=lambda c: score((c[0], c[1], ""))))
            chosen = [c for c in chosen if not (c[0] < min(y, a) and x < c[1])] + [(x, min(y, a))]
    out: list[list[Any]] = [[round(a, 3), round(b, 3), 0.0, "normal"] for a, b in chosen if b - a >= MIN_TEASER_S]
    if blast:
        out.append([round(blast[0], 3), round(blast[1], 3), -6.0, "blast"])
    return sorted(out, key=lambda x: x[0])


# ── drawing ──────────────────────────────────────────────────────────────────

def _sprite(text: str, fnt, fill, stroke, stroke_w: int, shadow, k: float, anchor: str = "lm"):
    """Text drawn once, with its outline and shadow: (image, where the anchor point is in it)."""
    probe_img = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    x0, y0, x1, y1 = probe_img.textbbox((0, 0), text, font=fnt, anchor=anchor, stroke_width=stroke_w)
    pad = int(14 * k) + stroke_w
    img = Image.new("RGBA", (x1 - x0 + pad * 2, y1 - y0 + pad * 2), (0, 0, 0, 0))
    origin = (pad - x0, pad - y0)
    art.outlined_text(img, origin, text, fnt, fill, stroke, stroke_w, anchor=anchor, shadow=shadow, k=k)
    return img, origin


class Painter:
    """Every frame of the intro, at the video's size."""

    def __init__(self, b: Brand, logo_file: Path | None, title_font: str | None, text_font: str | None,
                 w: int, h: int, fps: float, seed: int = 7):
        self.b, self.w, self.h, self.fps, self.logo_file = b, w, h, fps, logo_file
        self.k, self.kx = h / 1080, w / 1920
        self.rng = random.Random(seed)
        k = self.k
        self.deep, self.purple, self.lav = b.rgb("deep"), b.rgb("purple"), b.rgb("lavender")
        self.ink = tuple(int(c * 0.75) for c in self.deep)
        self.logo = art.logo(b, logo_file, int(560 * k), title_font, k=k)
        self.bg = art.background(b, w, h)
        self.parts = art.particles(38, w, h, self.rng, k)
        name = b.name.upper()
        size = 190 * k
        while size > 40 * k and art.font(title_font, size).getlength(name) > 1000 * self.kx:
            size -= 4 * k
        self.title_size = size
        title = art.font(title_font, size)
        self.glyphs = []
        x = 800 * self.kx
        for ch in name:
            if ch.strip():
                self.glyphs.append((x, _sprite(ch, title, (255, 255, 255), self.ink, int(14 * k), self.deep, k)))
            x += title.getlength(ch)
        self.title_end = x
        tag_size = 40 * k
        room = w - 810 * self.kx - 40 * k
        while tag_size > 14 * k and art.font(text_font, tag_size).getlength(b.tagline.upper()) > room:
            tag_size -= 2 * k
        self.tag = _sprite(b.tagline.upper(), art.font(text_font, tag_size), (255, 255, 255), self.purple,
                           max(1, int(6 * k)), None, k) if b.tagline else None
        self.bug = self._bug(title_font)

    def _bug(self, title_font: str | None) -> Image.Image:
        """The channel badge in the top-left corner during the teaser."""
        k = self.k
        fnt = art.font(title_font, 34 * k)
        name = self.b.name.upper()
        right = int(146 * k + fnt.getlength(name) + 30 * k)
        img = Image.new("RGBA", (right + int(20 * k), int(170 * k)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle((70 * k, 40 * k, right, 96 * k), radius=int(28 * k), fill=self.deep + (150,))
        _paste_at(img, art.logo(self.b, self.logo_file, int(96 * k), title_font, k=k), -44 * k, -60 * k)
        art.outlined_text(img, (146 * k, 68 * k), name, fnt, (255, 255, 255), self.deep, max(1, int(5 * k)), k=k)
        return img

    def f30(self, f: int) -> float:
        return f * 30 / self.fps

    def teaser(self, img: Image.Image, style: str, i: int, n: int, idx: int) -> Image.Image:
        k, rng = self.k, self.rng
        i30, idx30 = self.f30(i), self.f30(idx)
        if style == "blast":
            sh = (26 * (1 - i / n) + 6) * k
            img = art.zoom(img, 1.12 + 0.04 * math.sin(i30 * 2.3))
            img = art.ImageChops.offset(img, int(rng.uniform(-sh, sh)), int(rng.uniform(-sh, sh)))
            img = art.rgb_split(img, int((14 + 10 * math.sin(i30)) * k))
            img = art.tint(img, (255, 60, 120), 0.18 if int(i30) % 4 < 2 else 0.05)
            if i30 < 2:
                img = art.tint(img, (255, 255, 255), 0.7)
        else:
            p = art.clamp01(i30 / 7)
            img = art.zoom(img, 1.10 - 0.10 * art.ease_out_cubic(p) + 0.025 * (i / max(1, n)))
            if i30 < 3:
                img = art.tint(img, (255, 255, 255), [0.45, 0.2, 0.06][int(i30)] * (0.4 if idx30 < 3 else 1))
                if i30 < 2:
                    img = art.rgb_split(img, int((10 - int(i30) * 4) * k))
        rgba = img.convert("RGBA")
        rgba.alpha_composite(art.fade_alpha(self.bug, art.clamp01(idx30 / 10)))
        return rgba.convert("RGB")

    def glitch_frames(self) -> int:
        return max(2, round(GLITCH_F30 * self.fps / 30))

    def glitch(self, last: Image.Image, g: int, n: int) -> Image.Image:
        k, kx, w, h = self.k, self.kx, self.w, self.h
        t = g / (n - 1)
        img = art.rgb_split(last, int((20 + 40 * t) * k))
        img = art.slice_glitch(img, 10 + int(20 * t), int((60 + 260 * t) * k), self.rng, k)
        img = art.tint(img, self.purple, 0.25 + 0.7 * t)
        d = ImageDraw.Draw(img)
        x = -600 * kx + (w + 1400 * kx) * art.ease_out_cubic(t)
        d.polygon([(x - 900 * kx, 0), (x, 0), (x - 500 * kx, h), (x - 1400 * kx, h)], fill=self.lav)
        d.polygon([(x, 0), (x + 120 * kx, 0), (x - 380 * kx, h), (x - 500 * kx, h)], fill=(255, 255, 255))
        return img

    def logo_frames(self) -> int:
        return round(LOGO_F30 * self.fps / 30)

    def reveal(self, f: int, n: int) -> Image.Image:
        k, kx, w, h = self.k, self.kx, self.w, self.h
        f30, n30 = self.f30(f), self.f30(n)
        lcx, lcy = 450 * kx, h / 2
        frame = self.bg.copy()
        frame.alpha_composite(art.rays(w, h, lcx, lcy, f30 * 0.012))
        art.draw_particles(frame, self.parts, f30, art.clamp01(f30 / 12), k)
        p = art.clamp01((f30 - 1) / 16)
        if p > 0:
            s = max(0.01, art.ease_out_back(p, 2.2))
            s *= 1 + 0.02 * math.sin(f30 * 0.12) * art.clamp01((f30 - 18) / 10)
            rot = (1 - art.ease_out_cubic(p)) * -22
            lw, lh = self.logo.size
            li = self.logo.resize((max(1, int(lw * s)), max(1, int(lh * s))), Image.BICUBIC)
            li = li.rotate(rot, resample=Image.BICUBIC, expand=True)
            _paste(frame, li, lcx, lcy)
        ty = h / 2 - 50 * k
        for i, (x, (img, (ox, oy))) in enumerate(self.glyphs):
            q = art.clamp01((f30 - 12 - i * 1.6) / 9)
            if q > 0:
                yy = ty + (1 - art.ease_out_back(q, 2.4)) * 140 * k
                _paste_at(frame, art.fade_alpha(img, art.clamp01(q * 2)), x - ox, yy - oy)
        q = art.clamp01((f30 - 30) / 12)
        if q > 0:
            tx = 800 * kx
            d = ImageDraw.Draw(frame)
            bw = (self.title_end - tx) * art.ease_out_cubic(q)
            y0 = ty + self.title_size * 0.6
            if bw > 16 * k:
                d.rounded_rectangle((tx + 6 * k, y0, tx + 6 * k + bw, y0 + 16 * k), radius=int(8 * k), fill=self.ink + (255,))
            if self.tag:
                img, (ox, oy) = self.tag
                _paste_at(frame, art.fade_alpha(img, q), tx + 10 * k - ox, ty + self.title_size * 1.02 - oy)
        img = frame.convert("RGB")
        if f30 < 4:
            img = art.tint(img, (255, 255, 255), [0.85, 0.5, 0.25, 0.1][int(f30)])
        if f30 >= n30 - 10:
            t = art.clamp01((f30 - (n30 - 10) + 1) / 10)
            img = art.tint(art.zoom(img, 1 + 0.35 * t * t, lcx / w, 0.5), (255, 255, 255), t ** 2)
        return img


def _paste_at(base: Image.Image, img: Image.Image, x: float, y: float) -> None:
    """alpha_composite that allows a position partly off the canvas."""
    x, y = int(round(x)), int(round(y))
    sx, sy = max(0, -x), max(0, -y)
    if sx >= img.width or sy >= img.height or x >= base.width or y >= base.height:
        return
    part = img.crop((sx, sy, min(img.width, base.width - x), min(img.height, base.height - y)))
    base.alpha_composite(part, (x + sx, y + sy))


def _paste(base: Image.Image, img: Image.Image, cx: float, cy: float) -> None:
    _paste_at(base, img, cx - img.width / 2, cy - img.height / 2)


# ── rendering ────────────────────────────────────────────────────────────────

def _run(cmd: list[str], timeout: float, cwd: Path | None = None) -> None:
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    if r.returncode != 0:
        raise MediaError("ffmpeg failed: " + "\n".join(r.stderr.strip().splitlines()[-3:])[:500])


def render_intro(b: Brand, logo_file: Path | None, title_font: str | None, text_font: str | None, src: Path,
                 teasers: list[list[Any]], dest: Path, *, fps: float, size: tuple[int, int], has_audio: bool = True,
                 subs: list[dict[str, Any]] | None = None, sub_style: dict[str, Any] | None = None,
                 sub_font: str | None = None, crf: int = 18, preset: str = "veryfast",
                 on_progress: Callable[[float], None] | None = None) -> Rendered:
    """teasers: [[start, end, gain_db, style], …] in src time. subs: subtitle cues in src
    time, burned into the teasers in sub_style (empty = none)."""
    from chatwatcher.subs import lines
    report = on_progress or (lambda _f: None)
    fps = min(fps or 30.0, 60.0)
    w, h = (size[0] or 1920) // 2 * 2, (size[1] or 1080) // 2 * 2
    frame_bytes = w * h * 3
    tmp = dest.parent / f".render_{dest.stem}_build"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        p = Painter(b, logo_file, title_font, text_font, w, h, fps)
        counts = [max(1, round((e - s) * fps)) for s, e, *_ in teasers]
        n_glitch = p.glitch_frames() if teasers else 0
        n_logo = p.logo_frames()
        total_frames = sum(counts) + n_glitch + n_logo
        total = total_frames / fps
        audio = np.zeros((int(total * sfx.SR) + sfx.SR, 2), np.float32)
        video = tmp / "video.mkv"
        enc = subprocess.Popen(
            [_exe("ffmpeg"), "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
             "-r", f"{fps:.6g}", "-i", "-", "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
             "-pix_fmt", "yuv420p", str(video)], stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        assert enc.stdin is not None
        idx, cut_times, last = 0, [], None

        def put(img: Image.Image) -> None:
            nonlocal idx
            enc.stdin.write(img.tobytes())
            idx += 1
            if idx % 10 == 0:
                report(idx / total_frames * 0.95)

        try:
            for k_, ((s, e, gain, style), n) in enumerate(zip(teasers, counts)):
                length = n / fps
                cut_times.append(idx / fps)
                vf = f"scale={w}:{h}:flags=lanczos,fps={fps:.6g}"
                shown = lines.local(subs or [], s, length)
                if shown:
                    (tmp / f"t{k_}.ass").write_text(lines.ass(shown, sub_style or {}, w, h, font=sub_font or lines.FONT),
                                                    encoding="utf-8")
                    vf += f",ass=t{k_}.ass"
                dec = subprocess.Popen([_exe("ffmpeg"), "-v", "error", "-ss", f"{s:.3f}", "-i", str(src),
                                        "-vf", vf, "-frames:v", str(n), "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                       stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, cwd=tmp)
                assert dec.stdout is not None
                got = 0
                while got < n:
                    buf = dec.stdout.read(frame_bytes)
                    if len(buf) < frame_bytes:
                        break
                    last = Image.frombytes("RGB", (w, h), buf)
                    put(p.teaser(last, style, got, n, idx))
                    got += 1
                dec.stdout.close()
                dec.wait(timeout=60)
                while got < n and last is not None:          # the clip ended early: hold the last frame
                    put(p.teaser(last, style, got, n, idx))
                    got += 1
                if last is None:
                    raise MediaError("Couldn't read the clip's picture for the teaser.")
                if has_audio:
                    r = subprocess.run([_exe("ffmpeg"), "-v", "error", "-ss", f"{s:.3f}", "-i", str(src), "-t",
                                        f"{length:.6f}", "-vn", "-ac", "2", "-ar", str(sfx.SR), *AUDIO_FROM_ZERO,
                                        "-f", "f32le", "-"],
                                       capture_output=True, timeout=120)
                    pcm = np.frombuffer(r.stdout, np.float32)
                    pcm = pcm[:len(pcm) // 2 * 2].reshape(-1, 2).copy()
                    if len(pcm):
                        fade_in, fade_out = min(len(pcm), int(0.02 * sfx.SR)), min(len(pcm), int(0.04 * sfx.SR))
                        pcm[:fade_in] *= np.linspace(0, 1, fade_in)[:, None]
                        pcm[len(pcm) - fade_out:] *= np.linspace(1, 0, fade_out)[:, None]
                        sfx.add(audio, cut_times[-1], pcm, sfx.db(gain))
            glitch_t = idx / fps
            for g in range(n_glitch):
                put(p.glitch(last, g, n_glitch))
            logo_t = idx / fps
            for f in range(n_logo):
                put(p.reveal(f, n_logo))
        finally:
            enc.stdin.close()
            err = enc.stderr.read().decode(errors="replace") if enc.stderr else ""
            enc.wait(timeout=300)
        if enc.returncode != 0:
            raise MediaError("ffmpeg couldn't encode the intro: " + err.strip()[-400:])

        rng = random.Random(11)
        fx = np.zeros_like(audio)
        for t in cut_times[1:]:
            sfx.add(fx, t - 0.12, sfx.whoosh(rng, 0.3), 0.55)
        if teasers:
            sfx.add(fx, glitch_t - 0.35, sfx.riser(rng, 0.4), 0.5)
            sfx.add(fx, glitch_t, sfx.glitch(rng, n_glitch / fps + 0.05), 0.5)
        sfx.add(fx, logo_t, sfx.boom(), 0.9)
        sfx.add(fx, logo_t + 0.02, sfx.clap(rng), 0.8)
        sfx.add(fx, logo_t + 0.45, sfx.chime(), 0.8)
        sfx.add(fx, total - 0.4, sfx.whoosh(rng, 0.4), 0.45)
        audio += fx * sfx.db(-3)
        raw = tmp / "audio.f32"
        raw.write_bytes(audio[:int(total * sfx.SR)].astype(np.float32).tobytes())
        out = tmp / "out.mp4"
        _run([_exe("ffmpeg"), "-v", "error", "-y", "-i", str(video), "-f", "f32le", "-ar", str(sfx.SR), "-ac", "2",
              "-i", str(raw), "-map", "0:v", "-map", "1:a", "-af", "alimiter=limit=0.89:level=false",
              "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out)],
             timeout=300)
        out.replace(dest)
        report(1.0)
        pr = probe(dest)
        return Rendered(dest, pr.duration_s, pr.size_bytes)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
