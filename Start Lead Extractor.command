#!/usr/bin/env bash
APP_DIR="$(cd -- "$(dirname -- "$0")" && pwd)"
cd "$APP_DIR"
"$APP_DIR/scripts/start_lead_extractor.sh"
