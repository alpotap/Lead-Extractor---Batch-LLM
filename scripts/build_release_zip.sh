#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PLATFORM="${1:-}"
case "$PLATFORM" in
    macos|linux) ;;
    *) echo "Usage: $0 macos|linux [output.zip]" >&2; exit 2 ;;
esac

PACKAGE_NAME="Lead-Extractor-$PLATFORM"
OUTPUT_PATH="${2:-$REPO_ROOT/dist/$PACKAGE_NAME.zip}"
if [[ "$OUTPUT_PATH" != /* ]]; then OUTPUT_PATH="$REPO_ROOT/$OUTPUT_PATH"; fi
mkdir -p "$(dirname -- "$OUTPUT_PATH")"
STAGE_ROOT="$(mktemp -d)"
PACKAGE_DIR="$STAGE_ROOT/$PACKAGE_NAME"
mkdir -p "$PACKAGE_DIR"
trap 'rm -rf "$STAGE_ROOT"' EXIT

for file in app.py README.md REFERENCE.md LICENSE VERSION requirements.txt; do
    cp "$REPO_ROOT/$file" "$PACKAGE_DIR/"
done
if [[ "$PLATFORM" == macos ]]; then
    cp "$REPO_ROOT/Start Lead Extractor.command" "$PACKAGE_DIR/"
else
    cp "$REPO_ROOT/Start Lead Extractor.sh" "$PACKAGE_DIR/"
fi
mkdir -p "$PACKAGE_DIR/docs"
cp "$REPO_ROOT/docs/MACOS.md" "$REPO_ROOT/docs/LINUX.md" "$PACKAGE_DIR/docs/"

for directory in assets config models services scripts; do
    mkdir -p "$PACKAGE_DIR/$directory"
    while IFS= read -r -d '' file; do
        case "$(basename "$file")" in settings.json|secrets.json|*.pyc) continue ;; esac
        relative="${file#"$REPO_ROOT/"}"
        mkdir -p "$PACKAGE_DIR/$(dirname "$relative")"
        cp "$file" "$PACKAGE_DIR/$relative"
    done < <(find "$REPO_ROOT/$directory" -type f -print0)
done

chmod +x "$PACKAGE_DIR/Start Lead Extractor."* "$PACKAGE_DIR/scripts/start_lead_extractor.sh"
rm -f "$OUTPUT_PATH"
python3 - "$STAGE_ROOT" "$OUTPUT_PATH" <<'PY'
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile
import sys

stage_root = Path(sys.argv[1])
output_path = Path(sys.argv[2])
with ZipFile(output_path, "w", compression=ZIP_DEFLATED) as archive:
    for path in sorted(stage_root.rglob("*")):
        if path.is_file():
            archive.write(path, path.relative_to(stage_root).as_posix())
PY
echo "Created $OUTPUT_PATH"
