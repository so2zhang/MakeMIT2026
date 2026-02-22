#!/bin/bash
# 🎵 GestureHand Startup Script
# Single entry point to run everything
#
# Usage:
#   ./run.sh                    # Local mode (default)
#   ./run.sh --vultr            # Cloud mode (streams to Vultr)
#   ./run.sh --help             # Show help

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Configuration
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONDA_ENV="gesture-hand"
DASHBOARD_PORT=${DASHBOARD_PORT:-8888}
BLUETOOTH_PORT=""  # Will be auto-detected if not provided
MODE="local"
SHOW_DASHBOARD=true

# Parse arguments
EXTRA_ARGS=""
while [[ $# -gt 0 ]]; do
    case $1 in
        --vultr)
            MODE="vultr"
            shift
            ;;
        --port)
            BLUETOOTH_PORT="$2"
            shift 2
            ;;
        --dashboard-port)
            DASHBOARD_PORT="$2"
            shift 2
            ;;
        --no-dashboard)
            SHOW_DASHBOARD=false
            shift
            ;;
        --chord-source)
            EXTRA_ARGS="$EXTRA_ARGS --chord-source $2"
            shift 2
            ;;
        --chord-min|--chord-max|--key-root|--performer-id|--baud|--midi-port|--thumb-threshold|--thumb-cc|--thumb-adc-min|--thumb-adc-max|--camera-index)
            # Pass through hand_tracking.py arguments
            EXTRA_ARGS="$EXTRA_ARGS $1 $2"
            shift 2
            ;;
        --list-midi|--list-cameras)
            # Pass through hand_tracking.py flags
            EXTRA_ARGS="$EXTRA_ARGS $1"
            shift
            ;;
        --thumb-mode)
            # Special case: takes a choice argument
            EXTRA_ARGS="$EXTRA_ARGS $1 $2"
            shift 2
            ;;
        --help)
            cat << 'EOF'
🎵 GestureHand Startup

USAGE:
    ./run.sh [OPTIONS]

OPTIONS:
    --port PORT              Bluetooth serial port (e.g., /dev/cu.usbserial-0001)
    --dashboard-port PORT    Dashboard port (default: 8888)
    --vultr                  Stream to Vultr cloud (requires backend running)
    --no-dashboard           Skip dashboard startup
    --help                   Show this help

EXAMPLES:
    ./run.sh --port /dev/cu.usbserial-0001
    ./run.sh --port /dev/cu.usbserial-0001 --dashboard-port 9000
    ./run.sh --port /dev/cu.usbserial-0001 --vultr

WHAT HAPPENS:
    1. Activates conda environment (gesture-hand)
    2. Starts performance engine (hand_tracking.py)
       - Connects to glove via Bluetooth
       - Reads flex + IMU + hand tracking sensors
       - Generates MIDI and logs events
    3. Saves sessions to sessions/ directory
    4. (Optional) Streams to Vultr API
    5. (Optional) Starts dashboard

FINDING YOUR BLUETOOTH PORT:
    $ ls /dev/cu.* | grep -i usb
    Or check System Preferences → Bluetooth

STOPPING:
    Press Ctrl+C in the terminal

MORE INFO:
    - README.md           Quick start guide
    - START_HERE.md       Quick reference
    - WINNING_DEMO.md     Judge strategy
EOF
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Banner
cat << EOF
╔════════════════════════════════════════════════════════════════╗
║                    🎵 GestureHand                              ║
║          Hybrid Glove + Camera Music Interface                 ║
╚════════════════════════════════════════════════════════════════╝

📍 Mode: $(echo "$MODE" | tr '[:lower:]' '[:upper:]')
🔌 Dashboard Port: $DASHBOARD_PORT
📁 Project: $PROJECT_DIR

EOF

# Step 1: Check conda
echo -e "${BLUE}[1/4]${NC} Checking conda environment..."
if ! command -v conda &> /dev/null; then
    echo -e "${RED}✗ Conda not found${NC}"
    echo "Install Miniforge from: https://github.com/conda-forge/miniforge"
    exit 1
fi

if ! conda env list | grep -q "^$CONDA_ENV "; then
    echo -e "${RED}✗ Environment '$CONDA_ENV' not found${NC}"
    echo "Create it with: conda create -n gesture-hand python=3.11"
    exit 1
fi
echo -e "${GREEN}✓ Conda environment found${NC}"

# Step 2: Activate conda
echo -e "${BLUE}[2/4]${NC} Activating conda environment..."
eval "$(conda shell.bash hook)"
conda activate "$CONDA_ENV"
echo -e "${GREEN}✓ Environment activated${NC}"

# Step 3: Check required files
echo -e "${BLUE}[3/4]${NC} Checking project files..."
if [[ ! -f "$PROJECT_DIR/hand_tracking.py" ]]; then
    echo -e "${RED}✗ hand_tracking.py not found${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Project files OK${NC}"

# Step 4: Show mode info
echo -e "${BLUE}[4/4]${NC} Starting in $MODE mode..."
if [[ "$MODE" == "vultr" ]]; then
    echo -e "${YELLOW}⚠ Vultr mode enabled${NC}"
    echo "   Events will stream to Vultr API (if running)"
    echo "   Database: Vultr Managed PostgreSQL"
    echo "   If API is down, sessions save locally (automatic fallback)"
else
    echo -e "${GREEN}✓ Local mode${NC}"
    echo "   Sessions save to: sessions/"
    echo "   Works offline, always reliable"
fi

# Create sessions directory if needed
mkdir -p "$PROJECT_DIR/sessions"

# Step 5: Start dashboard in background (if enabled)
if [[ "$SHOW_DASHBOARD" == true ]]; then
    echo ""
    echo -e "${BLUE}Starting dashboard...${NC}"
    # Kill any existing process on this port
    if command -v lsof &> /dev/null; then
        lsof -ti:$DASHBOARD_PORT 2>/dev/null | xargs kill -9 2>/dev/null || true
    fi
    
    # Start dashboard
    cd "$PROJECT_DIR"
    nohup python3 session_dashboard.py "$DASHBOARD_PORT" > /tmp/gesture_dashboard.log 2>&1 &
    DASHBOARD_PID=$!
    sleep 2
    
    if kill -0 $DASHBOARD_PID 2>/dev/null; then
        echo -e "${GREEN}✓ Dashboard running on http://localhost:$DASHBOARD_PORT${NC}"
        echo "   Sessions appear here automatically"
    else
        echo -e "${YELLOW}⚠ Dashboard failed to start${NC}"
        cat /tmp/gesture_dashboard.log
    fi
fi

# Step 6: Start main performance engine
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo -e "${GREEN}✓ Starting performance engine...${NC}"
echo ""
echo "Waiting for Bluetooth connection..."
echo ""
echo "Controls:"
echo "  • Press 'Q' to end session"
echo "  • Press 'C' to recalibrate IMU"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""

cd "$PROJECT_DIR"

# Detect Bluetooth port if not provided
if [[ -z "$BLUETOOTH_PORT" ]]; then
    if [[ -e "/dev/cu.HC-05-SerialPort" ]]; then
        BLUETOOTH_PORT="/dev/cu.HC-05-SerialPort"
    elif [[ -e "/dev/cu.usbserial-"* ]]; then
        BLUETOOTH_PORT=$(ls -1 /dev/cu.usbserial-* 2>/dev/null | head -1)
    elif [[ -e "/dev/ttyUSB0" ]]; then
        BLUETOOTH_PORT="/dev/ttyUSB0"
    fi
fi

# If still no port found, provide helpful message
if [[ -z "$BLUETOOTH_PORT" ]]; then
    echo -e "${YELLOW}⚠️  No Bluetooth port detected${NC}"
    echo ""
    echo "Available serial ports:"
    ls -1 /dev/cu.* /dev/ttyUSB* 2>/dev/null || echo "  (none found)"
    echo ""
    echo "Usage with custom port:"
    echo "  ./run.sh --bluetooth-port /dev/cu.usbserial-0001"
    echo ""
    exit 1
fi

echo -e "${GREEN}✓ Using Bluetooth port: $BLUETOOTH_PORT${NC}"
echo ""

# Build the command
CMD="python3 hand_tracking.py --port $BLUETOOTH_PORT --camera-index 0 --performer-id guest"

# Add any extra arguments (chord-source, etc.)
if [[ -n "$EXTRA_ARGS" ]]; then
    CMD="$CMD $EXTRA_ARGS"
fi

# Add Vultr flag if in vultr mode
if [[ "$MODE" == "vultr" ]]; then
    CMD="$CMD --vultr"
fi

# Run it
eval "$CMD"

# Cleanup on exit
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "${BLUE}Session ended${NC}"
echo ""
echo "Your session has been saved!"
echo "View in dashboard: http://localhost:$DASHBOARD_PORT"
echo ""
