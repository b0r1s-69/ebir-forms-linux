"""Local HTTP host: static asset server + synchronous host RPC.

Why an HTTP server instead of QtWebEngine URL scheme handlers: the HTA scripts
are written against Scripting.FileSystemObject, i.e. *synchronous* file I/O.
A page can issue a blocking XMLHttpRequest to 127.0.0.1 and wait for the reply
because the QtWebEngine renderer runs in its own process, so the UI thread
stays free (it can even run a native file dialog for that same request).
Serving the assets over http:// also keeps relative ``<script src>`` and XHR
same-origin, exactly like the original mshta host.
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from .store import PathEscape, Store

# .hta is not in /etc/mime.types; without this the WebEngine downloads it
# instead of rendering it.
mimetypes.add_type("text/html", ".hta")
mimetypes.add_type("text/html", ".htm")
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/xml", ".xml")

RPC_PREFIX = "/__host/"
DEFAULT_PAGES = ("BIRForms.hta", "index.hta", "index.html")
NATIVE_TOOLS_JS = Path(__file__).with_name("ebirtools.js")

# <script language="VBScript" src="js/eBIRTools.vbs"> is skipped outright by
# Chromium (an unknown script type is simply not executed), which would leave
# every form without the helper functions.  The host serves the same module as
# JavaScript, so rewrite the attribute in flight; the asset on disk is untouched.
_VBSCRIPT_ATTR = re.compile(rb"""language\s*=\s*["']?vbscript["']?""", re.IGNORECASE)


def prepare_html(body: bytes) -> bytes:
    return _VBSCRIPT_ATTR.sub(b'type="text/javascript"', body)


class HostBridge:
    """Everything the page may ask the native side to do."""

    def __init__(self, store: Store, ui=None, log=None):
        self.store = store
        self.ui = ui
        self._log = log or (lambda *a: None)
        self.running: list[subprocess.Popen] = []
        self.started = 0

    # -- helpers ---------------------------------------------------------
    def log(self, level: str, msg: str) -> dict:
        self._log(level, msg)
        return {"ok": True}

    def env(self) -> dict:
        home = str(Path.home())
        env = {k.upper(): v for k, v in os.environ.items()}
        env.setdefault("USERPROFILE", home)
        env.setdefault("HOMEPATH", home)
        env.setdefault("APPDATA", os.environ.get("XDG_CONFIG_HOME", home + "/.config"))
        env.setdefault("LOCALAPPDATA", os.environ.get("XDG_DATA_HOME", home + "/.local/share"))
        env.setdefault("TEMP", "/tmp")
        env.setdefault("TMP", "/tmp")
        env.setdefault("SystemRoot", "C:\\Windows")
        env.setdefault("WINDIR", "C:\\Windows")
        env.setdefault("COMPUTERNAME", socket.gethostname())
        env.setdefault("USERNAME", os.environ.get("USER", "user"))
        env["PROGRAMFILES"] = "C:\\Program Files"
        return {"ok": True, "env": env, "workdir": str(self.store.root),
                "home": home, "platform": "linux"}

    def special_folder(self, name: str) -> dict:
        home = Path.home()
        key = (name or "").strip().lower().replace(" ", "")
        mapping = {
            "desktop": home / "Desktop",
            "mydocuments": home / "Documents",
            "documents": home / "Documents",
            "personal": home / "Documents",
            "mydownloads": home / "Downloads",
            "favorites": home / ".local/share/ebirforms-native/favorites",
            "applicationdata": Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")),
            "temp": Path("/tmp"),
            "windows": self.store.root,
            "system": self.store.root,
        }
        return {"ok": True, "path": str(mapping.get(key, self.store.root)),
                "windows": "C:\\eBIRForms"}

    # -- process launching ----------------------------------------------
    def shell_run(self, cmd: str, wait: bool = False, cwd: str | None = None,
                  capture: bool = False) -> dict:
        """Legacy WScript.Shell.Run / .Exec target.

        The original app shells out to helper binaries (Encrypt.exe,
        cFTPSend.exe, WinSCP.exe, chkt.exe).  Those cannot run natively, so
        requests are recorded and reported as unavailable instead of silently
        pretending to succeed.
        """
        self._log("warn", "shell.run requested: %s" % cmd)
        workdir = str(self.store.root)
        try:
            if capture or wait:
                proc = subprocess.run(cmd, shell=True, cwd=cwd or workdir,
                                      capture_output=True, text=True, timeout=120)
                return {"ok": True, "code": proc.returncode,
                        "stdout": proc.stdout, "stderr": proc.stderr}
            proc = subprocess.Popen(cmd, shell=True, cwd=cwd or workdir,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.running.append(proc)
            return {"ok": True, "code": 0, "pid": proc.pid}
        except Exception as exc:  # noqa: BLE001 - reported to the page
            return {"ok": False, "code": -1, "stdout": "", "stderr": str(exc)}

    # -- submission helpers ---------------------------------------------
    def zip_create(self, file_path: str, tag: str = "") -> dict:
        """Replace the VBScript Shell.Application zip dance with zipfile."""
        src = self.store.resolve(file_path, must_exist=True)
        target = src.with_name(src.stem + ("#" + tag + "#" if tag else "") + ".zip")
        import zipfile
        with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(src, src.name)
        return {"ok": True, "path": self.store.rel(target), "abs": str(target)}

    def submit_encrypt(self, file_path: str) -> dict:
        self._log("warn", "submission encryption requested for %r - not implemented natively "
                          "(Encrypt.exe AES packaging)" % file_path)
        return {"ok": False, "error": "encryption for submission is not implemented in the native port"}

    def submit_send(self, file_path: str, **kwargs) -> dict:
        self._log("warn", "submission upload requested for %r (%s) - not implemented natively "
                          "(ebfSFTP.exe/WinSCP)" % (file_path, kwargs.get("host")))
        return {"ok": False, "error": "submission upload is not implemented in the native port"}

    # -- UI requests (executed on the Qt main thread) --------------------
    def print_page(self) -> dict:
        if self.ui is None:
            return {"ok": False, "error": "no ui"}
        return self.ui.request_print()

    def dialog_save(self, title: str = "Save", filename: str = "", directory: str = "",
                    filters: str = "") -> dict:
        if self.ui is None:
            return {"ok": False, "error": "no ui"}
        return self.ui.request_file_dialog("save", title, filename, directory, filters)

    def dialog_open(self, title: str = "Open", filename: str = "", directory: str = "",
                    filters: str = "") -> dict:
        if self.ui is None:
            return {"ok": False, "error": "no ui"}
        return self.ui.request_file_dialog("open", title, filename, directory, filters)


class _Handler(BaseHTTPRequestHandler):
    server_version = "eBIRFormsNative/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing --------------------------------------------------------
    def log_message(self, fmt, *args):  # noqa: D401 - silence default logging
        if self.server.bridge is not None:
            self.server.bridge._log("http", fmt % args)

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: dict, code: int = 200):
        body = json.dumps(payload).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    # -- verbs -----------------------------------------------------------
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = unquote(urlparse(self.path).path)
        if path.startswith(RPC_PREFIX):
            self._json({"ok": False, "error": "RPC requires POST"}, 405)
            return
        self._serve_static(path)

    def do_POST(self):
        path = unquote(urlparse(self.path).path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            self._json({"ok": False, "error": "bad json"}, 400)
            return
        if not path.startswith(RPC_PREFIX):
            self._json({"ok": False, "error": "unknown endpoint"}, 404)
            return
        op = path[len(RPC_PREFIX):].strip("/")
        try:
            result = self.server.dispatch(op, payload)
        except PathEscape as exc:
            self._json({"ok": False, "error": str(exc)}, 403)
            return
        except FileNotFoundError as exc:
            self._json({"ok": False, "error": "not found: %s" % exc}, 404)
            return
        except Exception as exc:  # noqa: BLE001 - surface to page, keep host alive
            self.server.bridge._log("error", "rpc %s failed: %s" % (op, exc))
            self._json({"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}, 500)
            return
        self._json(result)

    # -- static ----------------------------------------------------------
    def _serve_static(self, path: str):
        store = self.server.bridge.store
        rel = path.lstrip("/")
        if not rel:
            rel = self.server.default_page
        # The BIR documents include js/eBIRTools.vbs with language="VBScript";
        # Chromium cannot parse VBScript, so serve the native JS implementation
        # at the very same URL. No BIR asset is modified on disk.
        if rel.lower() == "js/ebirtools.vbs":
            body = NATIVE_TOOLS_JS.read_bytes()
            self._send(200, body, "application/javascript; charset=utf-8")
            return
        try:
            target = store.resolve(rel)
        except PathEscape:
            self._send(403, b"forbidden", "text/plain")
            return
        if target.is_dir():
            for candidate in DEFAULT_PAGES:
                if (target / candidate).is_file():
                    target = target / candidate
                    break
            else:
                self._send(404, b"no index", "text/plain")
                return
        if not target.is_file():
            self._send(404, b"not found: " + rel.encode(), "text/plain")
            return
        try:
            body = target.read_bytes()
        except OSError as exc:
            self._send(500, str(exc).encode(), "text/plain")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "text/xml"):
            ctype += "; charset=utf-8"
        if target.suffix.lower() in (".hta", ".htm", ".html"):
            body = prepare_html(body)
        self._send(200, body, ctype)


class HostServer:
    """Threaded HTTP server bound to loopback on an ephemeral port."""

    def __init__(self, store: Store, ui=None, log=None, default_page: str = "BIRForms.hta"):
        self.bridge = HostBridge(store, ui=ui, log=log)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.bridge = self.bridge
        self.httpd.dispatch = self._dispatch
        self.httpd.default_page = default_page
        self.thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------
    def start(self):
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       name="ebir-host-http", daemon=True)
        self.thread.start()
        return self

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    @property
    def port(self) -> int:
        return self.httpd.server_address[1]

    def url(self, page: str = "") -> str:
        page = page.lstrip("/")
        return "http://127.0.0.1:%d/%s" % (self.port, page)

    # -- dispatch --------------------------------------------------------
    def _dispatch(self, op: str, p: dict) -> dict:
        b = self.bridge
        s = b.store
        if op == "fs/read":
            return s.read_text(p.get("path"))
        if op == "fs/write":
            return s.write_text(p.get("path"), p.get("text") or "",
                                p.get("encoding") or "utf-8", bool(p.get("append")))
        if op == "fs/stat":
            return s.stat(p.get("path"))
        if op == "fs/list":
            return s.list(p.get("path"))
        if op == "fs/mkdir":
            return s.mkdir(p.get("path"))
        if op == "fs/copy":
            return s.copy(p.get("src"), p.get("dst"), bool(p.get("overwrite", True)))
        if op == "fs/move":
            return s.move(p.get("src"), p.get("dst"))
        if op == "fs/delete":
            return s.delete(p.get("path"), bool(p.get("recurse")))
        if op == "fs/abs":
            return s.absname(p.get("path"))
        if op == "host/env":
            return b.env()
        if op == "host/log":
            return b.log(p.get("level") or "info", str(p.get("msg")))
        if op == "host/print":
            return b.print_page()
        if op == "shell/run":
            return b.shell_run(p.get("cmd") or "", bool(p.get("wait")),
                               p.get("cwd"), bool(p.get("capture")))
        if op == "shell/specialfolder":
            return b.special_folder(p.get("name") or "")
        if op == "zip/create":
            return b.zip_create(p.get("file") or "", p.get("tag") or "")
        if op == "submit/encrypt":
            return b.submit_encrypt(p.get("file") or "")
        if op == "submit/send":
            return b.submit_send(p.get("file") or "", email=p.get("email"),
                                 host=p.get("host"), username=p.get("username"))
        if op == "dialog/save":
            return b.dialog_save(p.get("title") or "Save", p.get("filename") or "",
                                 p.get("directory") or "", p.get("filters") or "")
        if op == "dialog/open":
            return b.dialog_open(p.get("title") or "Open", p.get("filename") or "",
                                 p.get("directory") or "", p.get("filters") or "")
        raise KeyError("unknown rpc op: %s" % op)
