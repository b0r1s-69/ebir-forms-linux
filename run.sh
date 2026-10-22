#!/bin/sh
# Launcher for the native Linux port of the BIR Offline eBIRForms Package.
#
# Everything runs natively (QtWebEngine/Chromium); no Wine, no Microsoft
# binaries. On first start the recovered asset tree is copied into a writable
# application directory and reused afterwards.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ ! -d "$here/assets/app" ]; then
    echo "Asset tree missing: $here/assets/app" >&2
    echo "Recover it from the installer first:" >&2
    echo "  python3 $here/tools/extract_assets.py" >&2
    exit 1
fi

if ! python3 -c 'import PySide6.QtWebEngineWidgets' 2>/dev/null; then
    echo "PySide6 with QtWebEngine is required (python3 -m pip install PySide6)." >&2
    exit 1
fi

exec python3 "$here/native/run.py" "$@"
