#!/usr/bin/env bash
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

find_python() {
    local candidate
    for candidate in python3 python; do
        if command -v "$candidate" >/dev/null 2>&1 && \
            "$candidate" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
            command -v "$candidate"
            return 0
        fi
    done
    return 1
}

if ! PYTHON_EXE="$(find_python)"; then
    echo "Python 3.11 or newer is required. Install Python, then run this launcher again."
    echo "See docs/MACOS.md or docs/LINUX.md for platform-specific instructions."
    read -r -p "Press Enter to close..." _ || true
    exit 1
fi

if [[ ! -x ".venv/bin/python" ]]; then
    echo "Creating the app's private Python environment..."
    "$PYTHON_EXE" -m venv .venv
fi

echo "Checking and installing app packages..."
".venv/bin/python" -m pip install --disable-pip-version-check -r requirements.txt

echo "Starting Lead Extractor. Keep this terminal open while using the app."
exec ".venv/bin/python" -m streamlit run app.py "$@"
