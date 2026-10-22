#!/bin/sh
# One-command setup for the native Linux port of the BIR Offline eBIRForms
# Package. Idempotent: safe to re-run after updating the repository.
#
#   ./install.sh [path-to-installer.exe|setup.zip]
#
# It (1) checks the runtime dependency, (2) recovers the BIR asset tree from the
# official installer when it is missing, and (3) installs a desktop entry when a
# writable applications directory exists - otherwise it tells you the exact
# command to launch instead.
set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
say() { printf '%s\n' "$*"; }
warn() { printf '%s\n' "$*" >&2; }

# ---------------------------------------------------------------- 1. runtime
if python3 -c 'import PySide6.QtWebEngineWidgets' 2>/dev/null; then
    say "[1/3] PySide6 with QtWebEngine: OK"
else
    warn "[1/3] PySide6 with QtWebEngine is missing."
    warn "      Install it with:  python3 -m pip install PySide6"
    warn "      (on Debian/Ubuntu you may need: sudo apt install python3-pyside6.qtwidgets)"
    exit 1
fi

# ----------------------------------------------------------------- 2. assets
if [ -d "$here/assets/app" ] && [ -f "$here/assets/app/BIRForms.hta" ]; then
    say "[2/3] asset tree: already present ($here/assets/app)"
else
    say "[2/3] recovering the asset tree from the installer..."
    if ! command -v innoextract >/dev/null 2>&1; then
        warn "      innoextract is needed once:  sudo dnf install innoextract"
        warn "      (Debian/Ubuntu: sudo apt install innoextract)"
    fi
    if ! python3 -c 'import pefile' 2>/dev/null; then
        warn "      the pefile module is needed once:  python3 -m pip install pefile"
    fi
    if [ "$#" -ge 1 ]; then
        python3 "$here/tools/extract_assets.py" "$1"
    else
        python3 "$here/tools/extract_assets.py"
    fi
fi

# -------------------------------------------------------- 3. desktop entry
data_home=${XDG_DATA_HOME:-$HOME/.local/share}
app_dir="$data_home/applications"
icon_dir="$data_home/icons/hicolor/256x256/apps"
installed=no

if mkdir -p "$app_dir" "$icon_dir" 2>/dev/null && [ -w "$app_dir" ]; then
    if [ -f "$here/packaging/ebirforms-native.png" ]; then
        cp "$here/packaging/ebirforms-native.png" "$icon_dir/ebirforms-native.png"
        target_icon="$icon_dir/ebirforms-native.png"
    else
        target_icon="$here/packaging/ebirforms-native.png"
    fi
    sed -e "s#^Exec=.*#Exec=$here/run.sh#" \
        -e "s#^Path=.*#Path=$here#" \
        -e "s#^Icon=.*#Icon=$target_icon#" \
        "$here/packaging/ebirforms-native.desktop" > "$app_dir/ebirforms-native.desktop"
    chmod 644 "$app_dir/ebirforms-native.desktop"
    command -v update-desktop-database >/dev/null 2>&1 &&
        update-desktop-database "$app_dir" 2>/dev/null || true
    installed=yes
    say "[3/3] desktop entry: installed as 'Offline eBIRForms' (menu entry)"
else
    say "[3/3] desktop entry: skipped, $app_dir is not writable on this system"
    say "      (a read-only home means menu entries cannot be added without root)"
fi

# ---------------------------------------------------------------- 4. summary
say ""
say "Installation ready."
if [ "$installed" = yes ]; then
    say "  start it from your application menu, or with:  $here/run.sh"
else
    say "  start it with:  $here/run.sh"
    say "  optional: create your own shortcut to that command (panel launcher,"
    say "            KDE 'Add to Panel', a terminal alias, etc.)"
fi
say ""
say "Your profiles, saved returns and the host log live in:"
say "  \$XDG_DATA_HOME/ebirforms-native  (fallback: <repo>/.data)"
say "The first run copies the recovered program tree there; later runs reuse it."
