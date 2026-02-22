#!/bin/bash
# 🎵 GestureHand Live Dashboard Demo
# 
# This script starts the live telemetry dashboard
# Pair it with hand_tracking.py running in another terminal

set -e

# Colors
BLUE='\033[0;34m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DASHBOARD_PORT=${1:-8889}

# Banner
cat << EOF
╔════════════════════════════════════════════════════════════════╗
║         🎵 GestureHand Live Dashboard                          ║
║                                                                ║
║     Real-time Telemetry + Session Fingerprinting              ║
╚════════════════════════════════════════════════════════════════╝

EOF

echo -e "${BLUE}[Dashboard]${NC} Starting live telemetry viewer..."
echo -e "${BLUE}[Dashboard]${NC} Port: ${DASHBOARD_PORT}"
echo ""
echo "Steps:"
echo "  1. In another terminal, run: ./run.sh"
echo "  2. Perform with your glove"
echo "  3. Press Q to end session"
echo "  4. Dashboard will show telemetry data:"
echo "     • X/Y/Z acceleration curves"
echo "     • Session fingerprint hash"
echo "     • Event counter + uptime"
echo ""
echo -e "${YELLOW}Dashboard will be at: http://localhost:${DASHBOARD_PORT}${NC}"
echo ""
echo "Press Ctrl+C to stop"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

# Activate conda if available
if command -v conda &> /dev/null; then
    eval "$(conda shell.bash hook)"
    if conda env list | grep -q "^gesture-hand "; then
        conda activate gesture-hand
    fi
fi

# Start dashboard
cd "$PROJECT_DIR"
python3 live_dashboard.py "$DASHBOARD_PORT"
