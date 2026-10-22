#!/usr/bin/env python3
"""Recover the eBIRForms runtime asset tree from the official Windows installer.

The installer is an Inno Setup 5.5.7 package whose payload is a single native
executable, ``BIRForms.exe``.  That executable is only a ~180 KiB C++ IE host:
every BIR form, its JScript, the jQuery/AES helpers and all images live in its
PE resource section as ``RT_HTML`` entries (465 of them, ~58 MiB).

Each ``RT_HTML`` blob is stored bitwise-complemented (every byte XOR 0xFF); the
lowest-numbered blob is a UTF-16LE manifest ``{GUID}|name|name|...`` and the
remaining blobs, in ascending id order, are the files named in that manifest.

Usage:
    python3 tools/extract_assets.py [installer.exe|setup.zip] [-o assets/app]

Requires: ``innoextract`` on PATH and the ``pefile`` module
(``pip install --user pefile``).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile

RT_HTML = 23
CIPHER_KEY = 0xFF
HOST_EXE = "BIRForms.exe"


def die(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    sys.stderr.write("error: %s\n" % msg)
    raise SystemExit(1)


def find_installer(path: str | None) -> str:
    """Resolve the installer executable, unpacking it if a zip was given."""
    if path is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for cand in sorted(os.listdir(here)):
            if cand.lower().startswith("offline_ebirforms_package") and cand.lower().endswith(".zip"):
                path = os.path.join(here, cand)
                break
        if path is None:
            for cand in sorted(os.listdir(here)):
                if cand.lower().endswith(".exe") and "ebirforms" in cand.lower():
                    path = os.path.join(here, cand)
                    break
    if path is None:
        die("no installer given and none found in the project directory")

    if zipfile.is_zipfile(path):
        tmp = tempfile.mkdtemp(prefix="ebir-outer-")
        with zipfile.ZipFile(path) as zf:
            exes = [n for n in zf.namelist() if n.lower().endswith(".exe")]
            if not exes:
                die("zip contains no .exe")
            zf.extract(exes[0], tmp)
        return os.path.join(tmp, exes[0])
    return path


def innoextract_payload(installer: str, workdir: str) -> str:
    """Extract the Inno Setup payload and return the payload 'app' directory."""
    tool = shutil.which("innoextract")
    if tool is None:
        for cand in (
            os.path.expanduser("~/Desktop/Workspace/toolchain/bin/innoextract"),
            "/usr/local/bin/innoextract",
        ):
            if os.path.exists(cand):
                tool = cand
                break
    if tool is None:
        die("innoextract not found (dnf download innoextract, or place it on PATH)")

    out = os.path.join(workdir, "payload")
    cmd = [tool, "-e", "-s", "-d", out, installer]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        die("innoextract failed:\n%s\n%s" % (res.stdout, res.stderr))
    app = os.path.join(out, "app")
    if not os.path.isdir(app):
        die("unexpected payload layout: %s missing" % app)
    return app


def read_rt_html(exe_path: str) -> dict[int, bytes]:
    try:
        import pefile
    except ImportError:
        die("the 'pefile' module is required (pip install --user pefile)")

    pe = pefile.PE(exe_path, fast_load=True)
    pe.parse_data_directories(
        directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_RESOURCE"]]
    )
    blobs: dict[int, bytes] = {}

    def walk(directory, path):
        # Resource tree is type -> name/id -> language; the stable identifier is
        # the name level (path[1]), the leaf is a language id.
        for entry in directory.entries:
            label = entry.id if entry.name is None else entry.name.string
            if hasattr(entry, "directory"):
                walk(entry.directory, path + [label])
            elif len(path) >= 2 and int(path[0]) == RT_HTML:
                data = entry.data.struct
                blobs[int(path[1])] = pe.get_data(data.OffsetToData, data.Size)

    walk(pe.DIRECTORY_ENTRY_RESOURCE, [])
    if not blobs:
        die("no RT_HTML resources found in %s" % exe_path)
    return blobs


def manifest_names(blob: bytes) -> list[str]:
    text = blob.decode("utf-16le")
    if text.startswith("\ufeff"):
        text = text[1:]
    parts = text.split("|")
    if not parts[0].startswith("{"):
        die("unexpected manifest header: %r" % parts[0])
    names = parts[1:]
    # Directory placeholders (.gitkeep) and the trailing empty field have no
    # stored resource, so they must not consume a position when the remaining
    # names are matched against the resource ids in ascending order.
    return [n for n in names if n and not n.lower().endswith(".gitkeep")]


def extract(installer: str, outdir: str) -> dict:
    workdir = tempfile.mkdtemp(prefix="ebir-extract-")
    try:
        app = innoextract_payload(installer, workdir)
        exe = os.path.join(app, HOST_EXE)
        if not os.path.exists(exe):
            die("%s not found in payload" % HOST_EXE)

        blobs = read_rt_html(exe)
        ids = sorted(blobs)
        names = manifest_names(blobs[ids[0]])
        file_ids = ids[1:]
        if len(names) != len(file_ids):
            die("manifest/resource count mismatch: %d names vs %d resources"
                % (len(names), len(file_ids)))

        if os.path.isdir(outdir):
            shutil.rmtree(outdir)
        os.makedirs(outdir, exist_ok=True)

        index = []
        written = 0
        for pos, res_id in enumerate(file_ids):
            if pos >= len(names):
                sys.stderr.write("warning: resource %d has no manifest name; skipped\n" % res_id)
                continue
            name = names[pos].replace("\\", "/")
            data = bytes(b ^ CIPHER_KEY for b in blobs[res_id])
            target = os.path.join(outdir, name)
            os.makedirs(os.path.dirname(target) or outdir, exist_ok=True)
            with open(target, "wb") as fh:
                fh.write(data)
            index.append({"resource_id": res_id, "path": name, "bytes": len(data)})
            written += 1

        # The installer ships live data files (xml lookups, proxy.ini, profile
        # placeholders) next to the executable; they take precedence over the
        # copies baked into the resource section.
        overlaid = []
        for root, _dirs, files in os.walk(app):
            for fn in files:
                src = os.path.join(root, fn)
                rel = os.path.relpath(src, app).replace(os.sep, "/")
                if rel.lower() == HOST_EXE.lower():
                    continue
                if rel.lower().endswith((".exe", ".dll", ".pdb")):
                    continue
                dst = os.path.join(outdir, rel)
                os.makedirs(os.path.dirname(dst) or outdir, exist_ok=True)
                shutil.copy2(src, dst)
                overlaid.append(rel)

        meta = {
            "installer": os.path.basename(installer),
            "host_exe": HOST_EXE,
            "rt_html_resources": len(blobs),
            "manifest_names": len(names),
            "files_written": written,
            "unmatched_names": names[written:],
            "overlaid_from_installer": sorted(overlaid),
        }
        with open(os.path.join(os.path.dirname(outdir.rstrip("/")) or ".", "recovered.json"), "w") as fh:
            json.dump(meta, fh, indent=2)
        return meta
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main(argv: list[str]) -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("installer", nargs="?", help="installer .exe or setup .zip")
    ap.add_argument("-o", "--out", default=os.path.join(here, "assets", "app"),
                    help="output directory for the recovered asset tree")
    args = ap.parse_args(argv)

    installer = find_installer(args.installer)
    meta = extract(installer, os.path.abspath(args.out))
    print("recovered %d files from %d RT_HTML resources -> %s"
          % (meta["files_written"], meta["rt_html_resources"], args.out))
    if meta["unmatched_names"]:
        print("note: %d manifest names had no resource (supplied by the installer instead): %s"
              % (len(meta["unmatched_names"]), ", ".join(meta["unmatched_names"][:8])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
