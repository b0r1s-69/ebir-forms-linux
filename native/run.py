"""Application entry point for the native eBIRForms port.

Usage:
    python3 native/run.py                 # start the app
    python3 native/run.py --page forms/BIR-Form0605.hta
    python3 native/run.py --headless --screenshot /tmp/x.png --wait 8   # test harness
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# QtWebEngine needs these before QApplication is constructed.
# --disable-web-security mirrors the trust level an HTA has under mshta: the
# original scripts issue cross-origin XMLHttpRequests to bir.gov.ph (version
# check, submission) which a normal web origin would block.
_DEFAULT_CHROMIUM_FLAGS = ("--no-sandbox --disable-web-security "
                           "--allow-running-insecure-content --disable-site-isolation-trials")
os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS",
                      os.environ.get("EBIR_CHROMIUM_FLAGS", _DEFAULT_CHROMIUM_FLAGS))
os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ebirforms_host import paths  # noqa: E402
from ebirforms_host.server import HostServer  # noqa: E402
from ebirforms_host.store import Store  # noqa: E402


def build_logger():
    logfile = paths.log_dir() / "host.log"

    def log(level: str, msg: str):
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        line = "%s %-5s %s\n" % (stamp, level.upper(), msg)
        try:
            with open(logfile, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError:
            pass
        if os.environ.get("EBIR_VERBOSE") or level in ("error", "warn"):
            sys.stderr.write(line)
            sys.stderr.flush()

    return log, logfile


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Offline eBIRForms Package - native Linux port")
    ap.add_argument("--page", default="BIRForms.hta", help="HTA page to open")
    ap.add_argument("--headless", action="store_true", help="run offscreen (testing)")
    ap.add_argument("--wait", type=float, default=6.0,
                    help="headless: seconds to let the page settle before capture")
    ap.add_argument("--screenshot", default="", help="headless: write a PNG capture")
    ap.add_argument("--pdf", default="", help="headless: write a PDF capture")
    ap.add_argument("--dump-console", action="store_true", help="headless: print page console")
    ap.add_argument("--eval", action="append", default=[], metavar="JS",
                    help="headless: run JS in the active page before capture (repeatable)")
    ap.add_argument("--screenshot-prefix", default="",
                    help="headless: write one PNG per open window using this prefix")
    ap.add_argument("--workdir", default="", help="override the live application directory")
    args = ap.parse_args(argv)

    log, logfile = build_logger()
    log("info", "starting eBIRForms native port; page=%s log=%s" % (args.page, logfile))

    if args.workdir:
        os.environ["EBIR_WORKDIR"] = args.workdir
    workdir = paths.ensure_workdir()
    log("info", "application directory: %s" % workdir)

    store = Store(workdir)
    server = HostServer(store, log=log).start()
    log("info", "host server listening on %s" % server.url())

    from PySide6.QtCore import QTimer, QUrl
    from PySide6.QtWidgets import QApplication

    from ebirforms_host.ui import AppState, EbirWindow

    app = QApplication(sys.argv[:1] + [])
    app.setApplicationName("eBIRForms")
    app.setApplicationDisplayName("Offline eBIRForms Package")
    app.setOrganizationName("BIR-Forms-Native")

    state = AppState(log)
    server.bridge.ui = state.ui
    url = QUrl(server.url(args.page))
    window = EbirWindow(state, url)

    exit_code = 0
    if args.headless:
        def interact():
            for js in args.eval:
                log("info", "eval: %s" % js)
                window.view.page().runJavaScript(js)

        def capture():
            view = state.active_view() or window.view
            if args.screenshot:
                pixmap = view.grab()
                ok = pixmap.save(args.screenshot)
                log("info", "screenshot %s -> %s" % (args.screenshot, ok))
            for index, win in enumerate(list(state.windows)):
                if args.screenshot_prefix:
                    shot = "%s-%d.png" % (args.screenshot_prefix, index)
                    log("info", "window capture %s -> %s" % (shot, win.view.grab().save(shot)))
                log("info", "window %d url=%s title=%s" % (index, win.view.url().toString(), win.windowTitle()))
            if args.pdf:
                from PySide6.QtCore import QMarginsF
                from PySide6.QtGui import QPageLayout, QPageSize
                layout = QPageLayout(QPageSize(QPageSize.PageSizeId.Letter),
                                     QPageLayout.Orientation.Portrait,
                                     QMarginsF(8, 8, 8, 8))
                view.page().printToPdf(args.pdf, layout)
                log("info", "pdf requested -> %s" % args.pdf)
            if args.dump_console:
                for level, message, source, line in state.console.messages:
                    print("[%s] %s (%s:%s)" % (level, message, source, line))
            window.close()
            app.quit()

        QTimer.singleShot(int(args.wait * 400), interact)
        QTimer.singleShot(int(args.wait * 1000), capture)
        QTimer.singleShot(int(args.wait * 1000) + 5000, app.quit)
    else:
        def watch():
            if not state.windows:
                app.quit()

        timer = QTimer()
        timer.timeout.connect(watch)
        timer.start(500)

    try:
        app.exec()
    finally:
        server.stop()
        log("info", "shutdown (exit=%d)" % exit_code)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
