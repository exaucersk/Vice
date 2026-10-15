#!/usr/bin/env bash
# Install this checkout alongside the distro package, then select it for this
# user's launcher, CLI and existing systemd service. No sudo or downloads.
set -euo pipefail

if [[ $EUID -eq 0 ]]; then
    echo "Run this as your desktop user, without sudo." >&2
    exit 1
fi

source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin=/usr/bin/python3
user_home="$($python_bin -c 'from pathlib import Path; import pwd, os; print(pwd.getpwuid(os.getuid()).pw_dir)')"
uv_bin="$(command -v uv || true)"
if [[ -z "$uv_bin" ]]; then
    echo "This local installer needs uv and the existing Vice system dependencies." >&2
    exit 1
fi

# Use the distro's Qt and codecs, rather than downloading replacement wheels.
"$python_bin" -c 'import build, setuptools, wheel, aiohttp, click, evdev, psutil, tomli_w, webview, qtpy; import PyQt6.QtWebEngineWidgets'
fragment="$(systemctl --user show vice.service -p FragmentPath --value)"
if [[ -z "$fragment" ]]; then
    echo "Install the distro Vice package/service before running this local installer." >&2
    exit 1
fi

install_root="$user_home/.local/share/vice"
bin_dir="$user_home/.local/bin"
desktop_dir="$user_home/.local/share/applications"
dropin_dir="$user_home/.config/systemd/user/vice.service.d"
if [[ -e "$install_root/venv" && ! -L "$install_root/venv" ]]; then
    echo "An existing installation owns $install_root/venv. Back it up before switching installers." >&2
    exit 1
fi

work_dir="$(mktemp -d /tmp/vice-local-install.XXXXXXXX)"
trap 'rm -rf -- "$work_dir"' EXIT
echo "Building this checkout..."
if ! (cd "$source_dir" && "$python_bin" -m build --wheel --no-isolation --outdir "$work_dir") >"$work_dir/build.log" 2>&1; then
    cat "$work_dir/build.log" >&2
    exit 1
fi
wheels=("$work_dir"/vice-*.whl)
[[ ${#wheels[@]} -eq 1 && -f "${wheels[0]}" ]]

mkdir -p "$install_root/local-builds"
release_dir="$(mktemp -d "$install_root/local-builds/$(date +%Y%m%d-%H%M%S).XXXXXXXX")"
cp -- "${wheels[0]}" "$release_dir/"
wheel_path="$release_dir/$(basename -- "${wheels[0]}")"
echo "Installing a self-contained code snapshot with the system's Python dependencies..."
"$uv_bin" venv --no-project --system-site-packages --python "$python_bin" --no-python-downloads \
    --offline --cache-dir "$work_dir/cache" "$release_dir/venv"
"$uv_bin" pip install --python "$release_dir/venv/bin/python" --no-deps --reinstall \
    --offline --link-mode copy --cache-dir "$work_dir/cache" "$wheel_path"

# Verify the actual installed wheel outside the checkout before switching any
# launcher or stopping the old daemon. Inherited PYTHONPATH must not mask it.
(cd "$work_dir" && env -u PYTHONPATH -u PYTHONHOME "$release_dir/venv/bin/python" - <<'PY'
import sys
from pathlib import Path
import vice
from vice.config import SharingConfig
from vice.share import ShareServer
from vice.nextcloud import share_video
import PyQt6.QtWebEngineWidgets
assert Path(vice.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert SharingConfig().provider == 'builtin'
assert (Path(vice.__file__).parent / 'ui/scripts/app.js').is_file()
print('Verified installed Vice', vice.__version__)
PY
)

mkdir -p "$bin_dir" "$desktop_dir" "$dropin_dir" "$release_dir/previous"
for item in "$install_root/venv" "$bin_dir/vice" "$bin_dir/vice-app" \
    "$desktop_dir/vice.desktop" "$dropin_dir/50-local-install.conf"; do
    if [[ -e "$item" || -L "$item" ]]; then
        cp -a -- "$item" "$release_dir/previous/$(basename -- "$item")"
    fi
done

systemctl --user stop vice.service
# Also stop a daemon launched manually from a terminal, if one is running.
(cd "$work_dir" && env -u PYTHONPATH -u PYTHONHOME "$release_dir/venv/bin/vice" stop) || true
ln -sfn -- "$release_dir/venv" "$install_root/venv"
ln -sfn -- "$install_root/venv/bin/vice" "$bin_dir/vice"
ln -sfn -- "$install_root/venv/bin/vice-app" "$bin_dir/vice-app"

cat >"$dropin_dir/50-local-install.conf" <<'UNIT'
# Keep package upgrades from selecting the unmodified /usr/bin/vice.
[Service]
ExecStart=
ExecStart="%h/.local/share/vice/venv/bin/vice" start --no-open-ui
WorkingDirectory=%h
Environment="PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin"
UnsetEnvironment=PYTHONPATH PYTHONHOME
UNIT

cat >"$desktop_dir/vice.desktop" <<DESKTOP
[Desktop Entry]
Version=1.0
Type=Application
Name=Vice
GenericName=Game Clip Recorder
Comment=Record and share gameplay clips on Linux (local build with Nextcloud)
Exec="$bin_dir/vice-app" %u
Icon=vice
Terminal=false
Categories=Game;Video;Recorder;AudioVideo;
Keywords=clip;record;game;capture;gameplay;nextcloud;
StartupNotify=true
StartupWMClass=Vice
DESKTOP

if command -v desktop-file-validate >/dev/null; then
    desktop-file-validate "$desktop_dir/vice.desktop"
fi
if command -v update-desktop-database >/dev/null; then
    update-desktop-database "$desktop_dir"
fi
printf 'Source: %s\nBuilt: %s\n' "$source_dir" "$(date --iso-8601=seconds)" >"$release_dir/build-info.txt"
sha256sum "$wheel_path" >>"$release_dir/build-info.txt"

systemctl --user daemon-reload
systemctl --user reenable vice.service
systemctl --user reset-failed vice.service
systemctl --user start vice.service
echo "Local build installed. Open Vice from the app menu or run: $bin_dir/vice-app"
echo "Snapshot and previous launcher settings: $release_dir"
echo "Service status: systemctl --user status vice.service"
