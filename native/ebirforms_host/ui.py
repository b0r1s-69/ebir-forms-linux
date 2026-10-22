"""Qt/Chromium windows that host the original HTA documents."""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QIcon
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineScript, QWebEngineSettings
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QFileDialog, QMainWindow, QMessageBox

from . import paths

COMPAT_JS = Path(__file__).with_name("compat.js")


class ConsoleSink:
    """Collects page console output; the host prints it to the log file."""

    def __init__(self, log, echo: bool = True):
        self._log = log
        self.echo = echo
        self.messages: list[tuple[str, str, str, int]] = []

    def add(self, level: str, message: str, source: str, line: int):
        self.messages.append((level, message, source, line))
        text = "[js:%s] %s (%s:%s)" % (level, message, source, line)
        if level in ("error", "fatal"):
            self._log("error", text)
        else:
            self._log("debug" if level == "info" else level, text)
        if self.echo and os.environ.get("EBIR_ECHO_JS"):
            print(text, flush=True)


class UiRequests(QObject):
    """Marshals host-RPC UI work onto the Qt main thread.

    The renderer is a separate process, so a page blocked in a synchronous
    XMLHttpRequest leaves the main thread free to run modal dialogs. The HTTP
    thread hands work over with a queued signal and waits on an event.
    """

    _dialogWanted = Signal()
    _printWanted = Signal()

    def __init__(self, app_state):
        super().__init__()
        self.state = app_state
        self._dialog_request: dict = {}
        self._dialog_result: dict = {"ok": False, "path": "", "cancelled": True}
        self._print_result: dict = {"ok": False}
        self._done = threading.Event()
        self._dialogWanted.connect(self._run_dialog)
        self._printWanted.connect(self._run_print)

    # -- called from the HTTP thread ------------------------------------
    def request_file_dialog(self, kind: str, title: str, filename: str,
                            directory: str, filters: str) -> dict:
        self._dialog_request = {"kind": kind, "title": title, "filename": filename,
                                "directory": directory, "filters": filters}
        self._done.clear()
        self._dialogWanted.emit()
        if not self._done.wait(timeout=300):
            return {"ok": False, "error": "dialog timed out"}
        return self._dialog_result

    def request_print(self) -> dict:
        self._done.clear()
        self._printWanted.emit()
        if not self._done.wait(timeout=120):
            return {"ok": False, "error": "print timed out"}
        return self._print_result

    # -- main thread slots ----------------------------------------------
    @Slot()
    def _run_dialog(self):
        req = self._dialog_request or {}
        kind = req.get("kind", "open")
        title = req.get("title") or ("Save file" if kind == "save" else "Open file")
        start_dir = req.get("directory") or str(Path.home())
        pattern = req.get("filters") or "All files (*)"
        filename = req.get("filename") or ""
        initial = str(Path(start_dir) / filename) if filename else start_dir
        try:
            if kind == "save":
                path, _ = QFileDialog.getSaveFileName(self.state.active_window(), title, initial, pattern)
            else:
                path, _ = QFileDialog.getOpenFileName(self.state.active_window(), title, initial, pattern)
        except Exception as exc:  # noqa: BLE001
            self._dialog_result = {"ok": False, "error": str(exc)}
            self._done.set()
            return
        if path:
            self._dialog_result = {"ok": True, "path": path, "cancelled": False}
        else:
            self._dialog_result = {"ok": True, "path": "", "cancelled": True}
        self._done.set()

    @Slot()
    def _run_print(self):
        view = self.state.active_view()
        if view is None:
            self._print_result = {"ok": False, "error": "no active page"}
            self._done.set()
            return
        out = Path(tempfile.gettempdir()) / ("ebirforms-print-%d.pdf" % int(time.time() * 1000))
        self._print_result = {"ok": True, "pdf": str(out)}
        self.state.print_to_pdf(view, out)
        self._done.set()


class EbirPage(QWebEnginePage):
    def __init__(self, profile: QWebEngineProfile, console: ConsoleSink, window=None):
        super().__init__(profile, window)
        self.console = console
        self.window_ref = window
        settings = self.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanOpenWindows, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptCanAccessClipboard, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
        settings.setAttribute(QWebEngineSettings.WebAttribute.ErrorPageEnabled, False)

    # -- diagnostics -----------------------------------------------------
    def javaScriptConsoleMessage(self, level, message, line_number, source_id):
        names = {
            QWebEnginePage.JavaScriptConsoleMessageLevel.InfoMessageLevel: "info",
            QWebEnginePage.JavaScriptConsoleMessageLevel.WarningMessageLevel: "warn",
            QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel: "error",
        }
        self.console.add(names.get(level, "info"), message, source_id, line_number)

    # -- modal dialogs ---------------------------------------------------
    def __auto_dismiss(self) -> bool:
        return bool(os.environ.get("EBIR_AUTO_DISMISS"))

    def javaScriptAlert(self, security_origin, msg):
        if self.__auto_dismiss():
            self.console.add("alert", str(msg), "dialog", 0)
            return
        QMessageBox.information(self.view().window() if self.view() else None,
                                "eBIRForms", msg)

    def javaScriptConfirm(self, security_origin, msg):
        self.console.add("confirm", str(msg), "dialog", 0)
        if self.__auto_dismiss():
            return False
        res = QMessageBox.question(self.view().window() if self.view() else None,
                                   "eBIRForms", msg,
                                   QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        return res == QMessageBox.StandardButton.Yes

    def javaScriptPrompt(self, security_origin, msg, default_value):
        from PySide6.QtWidgets import QInputDialog
        self.console.add("prompt", str(msg), "dialog", 0)
        if self.__auto_dismiss():
            return (False, default_value)
        text, ok = QInputDialog.getText(self.view().window() if self.view() else None,
                                        "eBIRForms", msg, text=default_value)
        return (bool(ok), text)

    # -- popups ----------------------------------------------------------
    def createWindow(self, window_type):
        if self.window_ref is not None:
            return self.window_ref.state.create_window(self, window_type)
        return None


class EbirView(QWebEngineView):
    def __init__(self, state, parent=None):
        super().__init__(parent)
        self.state = state
        self.setPage(EbirPage(state.profile, state.console, window=self))

    def createWindow(self, window_type):
        return self.state.create_window(self.page(), window_type)


class EbirWindow(QMainWindow):
    def __init__(self, state, url: QUrl | None = None, fixed_size: tuple[int, int] | None = None):
        super().__init__()
        self.state = state
        self.setWindowTitle("Offline eBIRForms Package")
        self.view = EbirView(state, self)
        self.setCentralWidget(self.view)
        icon_path = state.icon_path()
        if icon_path:
            self.setWindowIcon(QIcon(str(icon_path)))
        self.view.titleChanged.connect(self._on_title)
        self.view.page().windowCloseRequested.connect(self.close)
        if fixed_size:
            self.resize(*fixed_size)
        else:
            self.resize(1280, 900)
            self.showMaximized()
        if url is not None:
            self.view.load(url)
        state.register_window(self)

    def _on_title(self, title: str):
        if title:
            self.setWindowTitle(title)

    def closeEvent(self, event):
        self.state.unregister_window(self)
        super().closeEvent(event)


class AppState:
    """Owns the profile, injected scripts, windows and native services."""

    def __init__(self, log):
        self.log = log
        self.console = ConsoleSink(log)
        data_root = paths.web_profile_dir()
        store_dir = data_root / "web"
        store_dir.mkdir(parents=True, exist_ok=True)
        self.profile = QWebEngineProfile("ebirforms", None)
        self.profile.setPersistentStoragePath(str(store_dir / "storage"))
        self.profile.setCachePath(str(store_dir / "cache"))
        self.profile.setPersistentCookiesPolicy(
            QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        self.windows: list[EbirWindow] = []
        self.ui = UiRequests(self)
        self._install_compat()
        self.profile.downloadRequested.connect(self._on_download)

    # -- scripts ---------------------------------------------------------
    def _install_compat(self):
        source = COMPAT_JS.read_text(encoding="utf-8")
        script = QWebEngineScript()
        script.setName("ebir-compat")
        script.setSourceCode(source)
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
        script.setRunsOnSubFrames(True)
        self.profile.scripts().insert(script)

    # -- windows ---------------------------------------------------------
    def register_window(self, window: EbirWindow):
        self.windows.append(window)

    def unregister_window(self, window: EbirWindow):
        if window in self.windows:
            self.windows.remove(window)
        if not self.windows:
            from PySide6.QtWidgets import QApplication
            QApplication.instance().quit()

    def active_window(self):
        from PySide6.QtWidgets import QApplication
        active = QApplication.activeWindow()
        if isinstance(active, EbirWindow) and active in self.windows:
            return active
        return self.windows[-1] if self.windows else None

    def active_view(self):
        window = self.active_window()
        return window.view if window else None

    def create_window(self, source_page, window_type):
        """Handle window.open()/target=_blank from the HTA."""
        window = EbirWindow(self)
        return window.view.page()

    def icon_path(self):
        assets = Path(os.environ.get("EBIR_ASSETS", ""))
        for name in ("img/bir_icon.ico", "img/bir.ico"):
            candidate = assets / name
            if candidate.is_file():
                return candidate
        return None

    # -- native services -------------------------------------------------
    def print_to_pdf(self, view: EbirView, out_path: Path):
        from PySide6.QtCore import QMarginsF
        from PySide6.QtGui import QPageLayout, QPageSize

        def finished(pdf_path: str, ok: bool):
            if not ok:
                self.log("error", "printToPdf failed")
                return
            self.log("info", "rendered print output: %s" % pdf_path)
            printer = os.environ.get("EBIR_PRINT_CMD")
            if printer:
                subprocess.Popen(printer.split() + [pdf_path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            if Path("/usr/bin/lp").exists() and os.environ.get("EBIR_PRINT_AS_PDF") != "1":
                subprocess.Popen(["/usr/bin/lp", pdf_path],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            subprocess.Popen(["xdg-open", pdf_path],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        layout = QPageLayout(QPageSize(QPageSize.PageSizeId.Letter),
                             QPageLayout.Orientation.Portrait,
                             QMarginsF(10, 10, 10, 10))
        view.page().printToPdf(str(out_path), layout)
        self.log("info", "print requested -> %s" % out_path)

    def _on_download(self, item):
        suggested = item.downloadFileName() or "download.bin"
        target, _ = QFileDialog.getSaveFileName(self.active_window(), "Save file",
                                                str(Path.home() / suggested))
        if not target:
            item.cancel()
            return
        item.setDownloadDirectory(str(Path(target).parent))
        item.setDownloadFileName(Path(target).name)
        item.accept()
        self.log("info", "download accepted: %s" % target)
