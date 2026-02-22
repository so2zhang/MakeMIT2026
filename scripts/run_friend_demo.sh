#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

PORT="${1:-/dev/cu.usbserial-0001}"
CAMERA_INDEX="${2:-0}"

if [[ ! -d ".venv" ]]; then
  python3 -m venv .venv
fi

source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

echo "Starting GestureHand..."
echo "  serial port: $PORT"
echo "  camera index: $CAMERA_INDEX"

python3 "$ROOT_DIR/hand_tracking.py" --port "$PORT" --camera-index "$CAMERA_INDEX"
