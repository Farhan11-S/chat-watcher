"""The MMD model library: <data_dir>/mmd/<slug>/, one unpacked zip each, with profile.json:

    name      as shown ("Hakos Baelz")
    vtuber    the VTuber it is (the VTubers list's name), to pick it for her clips
    pmx       the model file, relative to the folder (a zip can hold props and outfits too)
    pmx_all   every .pmx in the zip
    faces     {face: [[morph, weight], …]}: this model's own way of making a face, where the
              shared names (figure.js FACES) don't fit it
    seated    what the figure does when she sits (figure.js): pin = "lower" (every simulated
              part under 下半身: skirts, tails, coat tails stop being simulated and are laid
              over her lap) or a list of bone-name patterns; keep = patterns to keep simulated
              anyway; fold = {bone: [x, y, z] degrees} extra turns while seated
    mouth     how wide her mouth opens on every word (1 = as the voice says)
    checked   you looked at it on its check page
    notes     what you found out about it

The files are COVER's (mmd.hololive.tv/terms): they never leave <data_dir> except to the
dashboard's own 3D page."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from chatwatcher.config import Config

DIR = "mmd"
PROFILE = "profile.json"
MAX_UNPACKED = 2 * 1024 ** 3          # refuse zips that unpack to more than this
SEATED_DEFAULT: dict[str, Any] = {"pin": "lower", "keep": [], "fold": {}}
EDITABLE = ("name", "vtuber", "pmx", "faces", "seated", "checked", "notes", "mouth")


class ModelError(Exception):
    def __init__(self, message: str, kind: str = "error"):
        super().__init__(message)
        self.kind = kind       # not_found | invalid | error


def root(cfg: Config) -> Path:
    return Path(cfg.paths.data_dir) / DIR


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "model"


def _squash(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _spaced(stem: str) -> str:
    """HakosBaelz → Hakos Baelz (a name that isn't on the VTubers list)."""
    return re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", " ", stem.replace("_", " ")).strip()


def _zip_name(info: zipfile.ZipInfo) -> str:
    """Japanese file names in zips made on Windows are Shift-JIS without the UTF-8 flag;
    zipfile reads them as cp437 (Kiara's 'キアラチキン' arrives as 'âLâAâëâ`âLâô')."""
    if info.flag_bits & 0x800:
        return info.filename
    try:
        return info.filename.encode("cp437").decode("cp932")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return info.filename


def _safe_parts(name: str) -> list[str] | None:
    parts = [p for p in name.replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or ":" in p for p in parts) or name.startswith(("/", "\\")):
        return None
    return parts


def _main_pmx(found: list[str], stem: str) -> str | None:
    """The talent herself in her usual outfit, not her props or a variant: the .pmx named
    like the zip, else one marked STD, else one whose name starts the others' ("La+Darknesss"
    before "La+Darknesss_short"), else the biggest (found is biggest first)."""
    if not found:
        return None
    names = [_squash(Path(p).stem) for p in found]

    def rank(i: int) -> tuple[int, int, int, int]:
        n = names[i]
        return (n == _squash(stem), "std" in n or "standard" in n,
                any(o != n and o.startswith(n) for o in names), -i)
    return found[max(range(len(found)), key=rank)]


def vtuber_for(conn: sqlite3.Connection | None, stem: str) -> str | None:
    """The VTuber a zip is, from its name: "HakosBaelz", "AZKi_4th", "Luna_211118_1",
    "夏色まつり公式mmd_ver1.0" (her Japanese name on the VTubers list)."""
    if conn is None:
        return None
    from chatwatcher.subs import speakers
    s, best = _squash(stem), (0, None)
    for v in speakers.vtubers(conn).values():
        n, jp = _squash(v["name"]), (v.get("japanese_name") or "").replace("・", "").strip()
        score = 0
        if n and n == s:
            score = 100
        elif jp and len(jp) >= 2 and jp in stem.replace("・", ""):
            score = 90 + len(jp)
        elif n and len(n) >= 4 and s.startswith(n):
            score = 80
        elif len(s) >= 5 and n.startswith(s):
            score = 70
        else:
            words = [_squash(w) for w in re.split(r"[\s+]+", v["name"])]
            hit = max((len(w) for w in words if len(w) >= 4 and w in s), default=0)
            score = 40 + hit if hit else 0
        if score and v.get("japanese_name"):
            score += 0.5                  # the VTubers list's own entries win over stray spellings
        if score > best[0]:
            best = (score, v["name"])
    return best[1]


def _variant(stem: str, conn: sqlite3.Connection | None, who: str) -> str:
    """What sets this zip apart from her other models ("2" of さくらみこ2公式mmd_ver1.0)."""
    rest = stem
    if conn is not None:
        from chatwatcher.subs import speakers
        v = speakers.find(speakers.vtubers(conn), who) or {}
        for n in (v.get("japanese_name") or "", who, who.replace(" ", "")):
            if n:
                rest = re.sub(re.escape(n), "", rest, flags=re.I)
    rest = re.sub(r"公式|mmd|ver[\d.]+|[_\-.]+", " ", rest, flags=re.I)
    return " ".join(rest.split())


def _free_slug(cfg: Config, base: str, source: str) -> str:
    """base, or base-2, -3…: a folder that is free or holds this same zip already."""
    for n in range(1, 100):
        slug = base if n == 1 else f"{base}-{n}"
        f = root(cfg) / slug / PROFILE
        if not f.is_file():
            return slug
        try:
            if json.loads(f.read_text(encoding="utf-8")).get("source") == source:
                return slug
        except (OSError, json.JSONDecodeError):
            continue
    raise ModelError("Too many models with that name.", "invalid")


def import_zip(cfg: Config, src: Path, *, conn: sqlite3.Connection | None = None,
               name: str | None = None) -> dict[str, Any]:
    """Unpack a model zip from mmd.hololive.tv into the library. Returns its profile.
    Importing the same zip again replaces the files and keeps what you set in the profile."""
    src = Path(src)
    stem = Path(name or src.name).stem
    try:
        zf = zipfile.ZipFile(src)
    except (zipfile.BadZipFile, OSError) as e:
        raise ModelError(f"Not a zip file: {e}", "invalid") from e
    with zf:
        members = [(i, _zip_name(i)) for i in zf.infolist() if not i.is_dir()]
        if sum(i.file_size for i, _ in members) > MAX_UNPACKED:
            raise ModelError("That zip unpacks to more than 2 GB.", "invalid")
        if not any(n.lower().endswith(".pmx") for _, n in members):
            raise ModelError("No .pmx model in that zip (PMD models aren't supported).", "invalid")
        who = vtuber_for(conn, stem)
        display = who or _spaced(stem)
        base = _slug(display) if re.search(r"[a-z0-9]", _slug(display)) and _slug(display) != "model" \
            else "m-" + hashlib.sha1(stem.encode()).hexdigest()[:8]
        slug = _free_slug(cfg, base, src.name)
        if slug != base and who:
            display = f"{who} {_variant(stem, conn, who)}".strip()
        dest = root(cfg) / slug
        old = load(cfg, slug) if (dest / PROFILE).is_file() else {}
        tmp = root(cfg) / f".{slug}.unpacking"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            for info, n in members:
                parts = _safe_parts(n)
                if parts is None:
                    continue
                out = tmp.joinpath(*parts)
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as fin, open(out, "wb") as fout:
                    shutil.copyfileobj(fin, fout, 1 << 20)
        except Exception:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    found = sorted((p for p in tmp.rglob("*") if p.suffix.lower() == ".pmx"), key=lambda p: -p.stat().st_size)
    rel = [p.relative_to(tmp).as_posix() for p in found]
    profile = {"name": display, "vtuber": who or "", "pmx": _main_pmx(rel, stem),
               "pmx_all": rel, "faces": {}, "seated": dict(SEATED_DEFAULT), "checked": False, "notes": "",
               "source": src.name, "imported_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")}
    for k in ("name", "vtuber", "faces", "seated", "checked", "notes"):
        if k in old:
            profile[k] = old[k]
    if old.get("pmx") in rel:
        profile["pmx"] = old["pmx"]
    shutil.rmtree(dest, ignore_errors=True)
    tmp.rename(dest)
    _write(dest, profile)
    return profile | {"slug": slug}


def import_folder(cfg: Config, src: Path, *, conn: sqlite3.Connection | None = None) -> tuple[list[str], list[str]]:
    """Every model zip in a folder on the server that isn't in the library yet (a zip is
    known by its file name). Returns (imported names, problems)."""
    src = Path(src).expanduser()
    if not src.is_dir():
        raise ModelError(f"No folder {src}.", "invalid")
    have = {p.get("source") for p in list_models(cfg)}
    done, problems = [], []
    for z in sorted(src.glob("*.zip")):
        if z.name in have:
            continue
        try:
            done.append(import_zip(cfg, z, conn=conn)["name"])
        except ModelError as e:
            problems.append(f"{z.name}: {e}")
    return done, problems


def _write(folder: Path, profile: dict[str, Any]) -> None:
    data = {k: v for k, v in profile.items() if k != "slug"}
    tmp = folder / (PROFILE + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(folder / PROFILE)


def folder(cfg: Config, slug: str) -> Path:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug or ""):
        raise ModelError("No such model.", "not_found")
    f = root(cfg) / slug
    if not (f / PROFILE).is_file():
        raise ModelError("No such model.", "not_found")
    return f


def load(cfg: Config, slug: str) -> dict[str, Any]:
    f = folder(cfg, slug)
    try:
        data = json.loads((f / PROFILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ModelError(f"{slug}/profile.json is unreadable: {e}") from e
    data.setdefault("faces", {})
    data["seated"] = {**SEATED_DEFAULT, **(data.get("seated") or {})}
    return data | {"slug": slug}


def list_models(cfg: Config) -> list[dict[str, Any]]:
    out = []
    if root(cfg).is_dir():
        for f in sorted(root(cfg).iterdir()):
            if f.is_dir() and not f.name.startswith(".") and (f / PROFILE).is_file():
                try:
                    out.append(load(cfg, f.name))
                except ModelError:
                    continue
    return sorted(out, key=lambda p: p["name"].lower())


def save(cfg: Config, slug: str, changes: dict[str, Any]) -> dict[str, Any]:
    """Change what you may change in a profile (EDITABLE). Raises ModelError."""
    p = load(cfg, slug)
    for k, v in (changes or {}).items():
        if k not in EDITABLE:
            continue
        if k == "pmx" and v not in p.get("pmx_all", []):
            raise ModelError("That .pmx isn't in this model's folder.", "invalid")
        if k in ("name", "vtuber", "notes"):
            v = str(v or "").strip()[:2000 if k == "notes" else 60]
        if k == "checked":
            v = bool(v)
        if k == "mouth":
            try:
                v = max(0.5, min(2.0, float(v)))
            except (TypeError, ValueError) as e:
                raise ModelError("mouth must be a number (1 = as her voice says).", "invalid") from e
        if k == "faces":
            v = clean_faces(v)
        if k == "seated":
            v = clean_seated(v)
        p[k] = v
    _write(folder(cfg, slug), p)
    return p


def clean_faces(raw: Any) -> dict[str, list[list[Any]]]:
    out: dict[str, list[list[Any]]] = {}
    if not isinstance(raw, dict):
        raise ModelError("faces must be {face: [[morph, weight], …]}.", "invalid")
    for face, parts in raw.items():
        if not isinstance(parts, list):
            raise ModelError(f"faces.{face} must be a list.", "invalid")
        out[str(face)[:30]] = [[str(m)[:40], max(0.0, min(1.0, float(w)))] for m, w in parts[:12]]
    return out


def clean_seated(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ModelError("seated must be an object.", "invalid")
    pin = raw.get("pin", "lower")
    pats = [pin] if isinstance(pin, str) and pin != "lower" else pin
    for pat in (pats if isinstance(pats, list) else []) + list(raw.get("keep") or []):
        try:
            re.compile(str(pat))
        except re.error as e:
            raise ModelError(f"Bad bone pattern {pat!r}: {e}", "invalid") from e
    fold = {}
    for bone, xyz in (raw.get("fold") or {}).items():
        if not (isinstance(xyz, list) and len(xyz) == 3):
            raise ModelError(f"seated.fold.{bone} must be [x, y, z] degrees.", "invalid")
        fold[str(bone)[:40]] = [max(-180.0, min(180.0, float(v))) for v in xyz]
    return {"pin": pin if pin == "lower" else [str(x) for x in (pats or [])],
            "keep": [str(x) for x in raw.get("keep") or []], "fold": fold}


def file(cfg: Config, slug: str, rel: str) -> Path | None:
    """A file inside a model's folder, or None. Windows-made models name their textures
    in any case ('Face.PNG' for face.png), so a miss is looked up again ignoring case."""
    try:
        base = folder(cfg, slug).resolve()
    except ModelError:
        return None
    parts = _safe_parts(rel)
    if parts is None:
        return None
    p = base.joinpath(*parts).resolve()
    if base not in p.parents:
        return None
    if p.is_file():
        return p
    here = base
    for part in parts:                     # case-insensitive walk
        try:
            match = next((c for c in here.iterdir() if c.name.lower() == part.lower()), None)
        except OSError:
            return None
        if match is None:
            return None
        here = match
    return here if here.is_file() and base in here.resolve().parents else None


def pick_for(cfg: Config, conn: sqlite3.Connection, moment_pk: int) -> str | None:
    """The model of the VTuber whose channel the moment is from, if there is one."""
    row = conn.execute("""SELECT c.title FROM moments m JOIN videos v ON v.id = m.video_id
                          LEFT JOIN channels c ON c.id = v.channel_id WHERE m.id = ?""", (moment_pk,)).fetchone()
    title = _squash((row["title"] if row else "") or "")
    best = None
    for p in list_models(cfg):
        for n in (p.get("vtuber"), p.get("name")):
            if n and _squash(n) and _squash(n) in title and (best is None or len(_squash(n)) > best[0]):
                best = (len(_squash(n)), p["slug"])
    return best[1] if best else None
