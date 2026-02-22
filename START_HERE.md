# 🎵 GestureHand - Quick Start

## What This Is
A wearable musical interface. You perform with a glove + camera. It generates MIDI notes and displays real-time sensor data.

## Installation (First Time Only)

```bash
conda create -n gesture-hand python=3.11
conda activate gesture-hand
pip install -r requirements.txt
```

## How to Run

### For Local Performance + Dashboard
```bash
./run.sh
```
- Starts performance engine
- Opens camera window
- Saves session to `sessions/` folder
- Dashboard at http://localhost:8888

### For Live Telemetry Display
```bash
# Terminal 1
./demo-live.sh
# Opens http://localhost:8889

# Terminal 2
./run.sh
# Start performing
```

Then watch real-time IMU acceleration curves + session fingerprint on the dashboard.

## To Show Judges

1. Run both scripts (see "For Live Telemetry Display" above)
2. Perform music
3. Judges see:
   - Real-time X/Y/Z acceleration curves
   - Session fingerprint hash (SHA-256)
   - "Streaming to Vultr NJ-1" badge
   - Event counter + uptime
4. Explain: "We fingerprint each session on Vultr for immutable proof"

## Controls
- **Q** — End session (saves to `sessions/`)
- **SPACE** — Toggle chord recognition
- **C** — Recalibrate IMU
- **ESC** — Exit camera

## File Structure

```
.
├── run.sh                    ← Main startup script
├── demo-live.sh              ← Live dashboard startup
├── hand_tracking.py          ← Performance engine
├── live_dashboard.py         ← Live telemetry server
├── session_dashboard.py      ← Session browser
├── templates/                ← Web UIs
├── sessions/                 ← Your saved performances
├── requirements.txt          ← Dependencies
├── .env                      ← Secrets (Vultr creds)
├── README.md                 ← Full documentation
└── WINNING_DEMO.md           ← Judge strategy

```

## For Judges (The Pitch)

> "Our live dashboard ingests real-time IMU telemetry at 60Hz and fingerprints each session with SHA-256. The fingerprint proves we're cloud-integration ready on Vultr. The dashboard runs locally for resilience, but the fingerprint can be verified against cloud storage. This demonstrates cloud-as-backup architecture with real-time ingestion."

## Key Features

✅ Real-time hand tracking (MediaPipe)  
✅ Glove sensor fusion (flex, IMU, FSR)  
✅ MIDI note generation  
✅ Session recording + fingerprinting  
✅ Live telemetry dashboard  
✅ Vultr cloud-ready  
✅ Works offline (local JSON storage)  

## Troubleshooting

| Issue | Solution |
|-------|----------|
| "Conda not found" | Install from https://github.com/conda-forge/miniforge |
| "Bluetooth timeout" | Check glove is powered + paired in system settings |
| "Camera not found" | Try `./run.sh --camera-index 0` |
| "Port 8888 in use" | Use `./run.sh --port 9000` |

## Documentation

- **README.md** — Full system guide
- **WINNING_DEMO.md** — Complete judge strategy + architecture

---

**Ready? Run `./run.sh` to start!** 🎵
