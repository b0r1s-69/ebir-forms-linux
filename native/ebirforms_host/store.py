"""Sandboxed filesystem with Windows Scripting.FileSystemObject semantics.

The HTA scripts pass Windows style paths (``profile\\123.xml``, ``C:\\eBIRForms\\
xml\\atcCodes.xml``).  Everything is resolved inside one writable application
root, so a script can never reach outside the app directory.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

INSTALL_PREFIXES = (
    "/ebirforms",
    "/program files/ebirforms",
    "/program files (x86)/ebirforms",
)


class PathEscape(Exception):
    pass


def detect_encoding(raw: bytes) -> tuple[str, str]:
    """Return (text, encoding) for the bytes of a text file."""
    if raw.startswith(b"\xff\xfe"):
        return raw[2:].decode("utf-16-le", "replace"), "utf-16-le"
    if raw.startswith(b"\xfe\xff"):
        return raw[2:].decode("utf-16-be", "replace"), "utf-16-be"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "utf-8"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace"), "ansi"


def encode_text(text: str, encoding: str) -> bytes:
    enc = (encoding or "utf-8").lower()
    if enc in ("utf-16", "unicode", "utf-16-le", "utf16le"):
        return b"\xff\xfe" + text.encode("utf-16-le", "replace")
    if enc in ("ansi", "ascii", "cp1252"):
        return text.encode("cp1252", "replace")
    data = text.encode("utf-8")
    return data


class Store:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ paths
    def resolve(self, raw: Any, must_exist: bool = False) -> Path:
        s = str(raw if raw is not None else "").strip().strip('"')
        if not s:
            s = "."
        s = s.replace("\\", "/")
        # Absolute host paths handed back to the page by us.
        candidate = Path(s)
        if candidate.is_absolute():
            resolved = candidate.resolve()
            if str(resolved) == str(self.root) or str(resolved).startswith(str(self.root) + os.sep):
                return resolved
        s = re.sub(r"^[A-Za-z]:", "", s)
        s = re.sub(r"/{2,}", "/", s).lstrip("/")
        lowered = "/" + s.lower()
        for prefix in INSTALL_PREFIXES:
            if lowered.startswith(prefix + "/") or lowered == prefix:
                s = s[len(prefix):].lstrip("/")
                break
        target = (self.root / s).resolve()
        if not (str(target) == str(self.root) or str(target).startswith(str(self.root) + os.sep)):
            raise PathEscape("path outside application directory: %r" % raw)
        if must_exist and not target.exists():
            raise FileNotFoundError(str(raw))
        return target

    def rel(self, p: Path) -> str:
        try:
            return str(p.relative_to(self.root)).replace(os.sep, "/")
        except ValueError:
            return str(p)

    # ------------------------------------------------------------------- read
    def read_text(self, raw: Any) -> dict:
        p = self.resolve(raw, must_exist=True)
        if not p.is_file():
            raise FileNotFoundError(str(raw))
        raw_bytes = p.read_bytes()
        text, enc = detect_encoding(raw_bytes)
        return {
            "ok": True,
            "path": self.rel(p),
            "abs": str(p),
            "text": text,
            "encoding": enc,
            "size": len(raw_bytes),
            "mtime": self._mtime(p),
        }

    def write_text(self, raw: Any, text: str, encoding: str = "utf-8", append: bool = False) -> dict:
        p = self.resolve(raw)
        p.parent.mkdir(parents=True, exist_ok=True)
        payload = encode_text(text, encoding)
        if append and p.exists():
            existing = p.read_bytes()
            if existing.startswith(b"\xff\xfe"):
                # Appending to a UTF-16 stream: keep the existing BOM only.
                payload = payload[2:] if payload.startswith(b"\xff\xfe") else payload
            elif payload.startswith(b"\xef\xbb\xbf") and not existing.startswith(b"\xef\xbb\xbf"):
                payload = payload[3:]
            mode = "ab"
        else:
            mode = "wb"
        with open(p, mode) as fh:
            fh.write(payload)
        return {"ok": True, "path": self.rel(p), "abs": str(p),
                "size": p.stat().st_size, "mtime": self._mtime(p)}

    # ------------------------------------------------------------------- stat
    def stat(self, raw: Any) -> dict:
        p = self.resolve(raw)
        if not p.exists():
            return {"ok": True, "exists": False, "path": self.rel(p), "abs": str(p)}
        st = p.stat()
        return {
            "ok": True,
            "exists": True,
            "path": self.rel(p),
            "abs": str(p),
            "name": p.name,
            "is_dir": p.is_dir(),
            "size": 0 if p.is_dir() else st.st_size,
            "mtime": self._mtime(p),
        }

    def list(self, raw: Any) -> dict:
        p = self.resolve(raw)
        if not p.is_dir():
            return {"ok": True, "exists": False, "path": self.rel(p), "abs": str(p), "entries": []}
        entries = []
        for child in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            try:
                st = child.stat()
            except OSError:
                continue
            entries.append({
                "name": child.name,
                "path": self.rel(child),
                "abs": str(child),
                "is_dir": child.is_dir(),
                "size": 0 if child.is_dir() else st.st_size,
                "mtime": self._mtime(child),
            })
        return {"ok": True, "exists": True, "path": self.rel(p), "abs": str(p), "entries": entries}

    # ----------------------------------------------------------------- mutate
    def mkdir(self, raw: Any) -> dict:
        p = self.resolve(raw)
        p.mkdir(parents=True, exist_ok=True)
        return {"ok": True, "abs": str(p), "path": self.rel(p)}

    def copy(self, src: Any, dst: Any, overwrite: bool = True) -> dict:
        s = self.resolve(src, must_exist=True)
        d = self.resolve(dst)
        if d.is_dir():
            d = d / s.name
        if d.exists() and not overwrite:
            raise FileExistsError(str(dst))
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_dir():
            shutil.copytree(s, d, dirs_exist_ok=True)
        else:
            shutil.copy2(s, d)
        return {"ok": True, "abs": str(d), "path": self.rel(d)}

    def move(self, src: Any, dst: Any) -> dict:
        s = self.resolve(src, must_exist=True)
        d = self.resolve(dst)
        if d.is_dir():
            d = d / s.name
        d.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(s), str(d))
        return {"ok": True, "abs": str(d), "path": self.rel(d)}

    def delete(self, raw: Any, recurse: bool = False) -> dict:
        p = self.resolve(raw, must_exist=True)
        if p.is_dir():
            if recurse:
                shutil.rmtree(p)
            else:
                p.rmdir()
        else:
            p.unlink()
        return {"ok": True, "path": self.rel(p)}

    def absname(self, raw: Any) -> dict:
        p = self.resolve(raw)
        # Present a Windows-looking path to the scripts, which often paste it
        # into messages or log files.
        win = "C:\\eBIRForms"
        rel = self.rel(p)
        win = win if rel in ("", ".") else win + "\\" + rel.replace("/", "\\")
        return {"ok": True, "abs": str(p), "path": rel, "windows": win}

    @staticmethod
    def _mtime(p: Path) -> float:
        try:
            return time.mktime(time.localtime(p.stat().st_mtime))
        except OSError:
            return 0.0
