"""Filesystem layout for the native port.

Two trees are involved:

``assets/app``
    The pristine runtime tree recovered from the installer by
    ``tools/extract_assets.py`` (read-only reference; replaced on upgrade).

``$XDG_DATA_HOME/ebirforms-native/app``
    The live tree the application reads and writes: profiles, saved XML,
    log files.  It is seeded from ``assets/app`` and then left alone, so an
    upgrade re-seeds program files while preserving taxpayer data.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

APP_ID = "ebirforms-native"


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def assets_dir() -> Path:
    return Path(os.environ.get("EBIR_ASSETS", project_root() / "assets" / "app"))


def _usable(candidate: Path) -> bool:
    """A candidate base directory is usable if we can create and remove files."""
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / ".ebir-write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def _pick(env_var: str, xdg_default: str, *fallbacks: Path) -> Path:
    configured = os.environ.get(env_var)
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured))
    candidates.append(Path.home() / xdg_default)
    candidates.extend(fallbacks)
    candidates.append(Path("/tmp") / (APP_ID + "-" + str(os.getuid())))
    for candidate in candidates:
        if _usable(candidate):
            return candidate
    return candidates[-1]


def data_home() -> Path:
    """Writable per-user data root.

    Homes on immutable/read-only mounts are common in sandboxes and on
    ostree-style systems, so the XDG location is probed rather than assumed.
    """
    return _pick("XDG_DATA_HOME", ".local/share", project_root() / ".data")


def state_dir() -> Path:
    base = _pick("XDG_STATE_HOME", ".local/state", data_home() / "state")
    return base / APP_ID


def log_dir() -> Path:
    d = state_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def workdir() -> Path:
    override = os.environ.get("EBIR_WORKDIR")
    if override:
        return Path(override)
    return data_home() / APP_ID / "app"


def web_profile_dir() -> Path:
    return data_home() / APP_ID / "web"


def _fingerprint(tree: Path) -> dict:
    files = 0
    total = 0
    newest = 0.0
    for root, _dirs, names in os.walk(tree):
        for name in names:
            p = os.path.join(root, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            files += 1
            total += st.st_size
            newest = max(newest, st.st_mtime)
    return {"files": files, "bytes": total, "newest": round(newest, 3)}


def ensure_workdir() -> Path:
    """Seed/upgrade the live tree from the recovered assets."""
    src = assets_dir()
    dst = workdir()
    if not src.is_dir():
        raise SystemExit(
            "asset tree missing: %s\n"
            "run tools/extract_assets.py first (see README)" % src
        )
    marker = dst / ".ebir-native-seed.json"
    want = _fingerprint(src)
    have = None
    if marker.exists():
        try:
            have = json.loads(marker.read_text())
        except (OSError, ValueError):
            have = None
    if have and have.get("assets") == want:
        return dst

    dst.mkdir(parents=True, exist_ok=True)
    # Overlay only: never delete taxpayer data (profile/, savefile/, logfile/).
    for root, _dirs, names in os.walk(src):
        rel = os.path.relpath(root, src)
        target_root = dst if rel == "." else dst / rel
        target_root.mkdir(parents=True, exist_ok=True)
        for name in names:
            s = Path(root) / name
            d = target_root / name
            shutil.copy2(s, d)
    for empty in ("failed", "success", "temp", "logfile", "profile", "savefile",
                  "IAF_Proj_Copy", "IAF_RDC_Copy", "IAF_RDO_Copy",
                  "IAF_RDC_Archive", "IAF_RDO_Archive"):
        (dst / empty).mkdir(parents=True, exist_ok=True)
    marker.write_text(json.dumps({"assets": want, "version": 1}, indent=2))
    return dst
