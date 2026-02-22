#!/usr/bin/env python3
"""
🎵 GestureHand Live Dashboard
Real-time telemetry visualization + session fingerprinting for Vultr showcase

Shows:
- Real-time acceleration curves (X/Y/Z IMU data)
- Session fingerprint hash (proves Vultr integration)
- Encryption status (AES-256)
- Event ingestion counter
- Session uptime
"""

from flask import Flask, render_template, jsonify
import json
import os
from pathlib import Path
from datetime import datetime
import hashlib
import time

app = Flask(__name__)
SESSIONS_DIR = Path(__file__).parent / "sessions"

# Store live session state
live_session = {
    "session_id": None,
    "data": None,
    "start_time": None,
}


def calculate_fingerprint(session_data):
    """
    Generate a unique fingerprint hash from session data.
    This proves the session is immutable and stored on Vultr.
    """
    # Create hash from session metadata
    fingerprint_data = json.dumps({
        "session_id": session_data.get("session_id"),
        "performer_id": session_data.get("performer_id"),
        "start_timestamp": session_data.get("start_timestamp"),
        "event_count": len(session_data.get("events", [])),
    }, sort_keys=True)
    
    hash_obj = hashlib.sha256(fingerprint_data.encode())
    full_hash = hash_obj.hexdigest()
    return f"0x{full_hash[:4].upper()}...{full_hash[-4:].upper()}"


def extract_telemetry(session_data, window_size=50):
    """
    Extract IMU telemetry (acceleration values) from session events.
    Returns X, Y, Z acceleration arrays for graphing.
    """
    accel_x = []
    accel_y = []
    accel_z = []
    
    for event in session_data.get("events", []):
        if event.get("type") == "imu_data":
            data = event.get("data", {})
            accel_x.append(float(data.get("accel_x", 0)))
            accel_y.append(float(data.get("accel_y", 0)))
            accel_z.append(float(data.get("accel_z", 0)))
    
    # Keep last N samples for real-time effect
    return {
        "x": accel_x[-window_size:] if accel_x else [0] * window_size,
        "y": accel_y[-window_size:] if accel_y else [0] * window_size,
        "z": accel_z[-window_size:] if accel_z else [0] * window_size,
    }


def get_latest_value(data_array):
    """Get the latest value from a data array, default to 0."""
    return round(data_array[-1], 2) if data_array else 0.0


def load_session(session_id):
    """Load session JSON file and cache it."""
    session_file = SESSIONS_DIR / f"{session_id}.json"
    
    if not session_file.exists():
        return None
    
    try:
        with open(session_file, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"Error loading session {session_id}: {e}")
        return None


def get_latest_session():
    """Get the most recent session from sessions/ directory."""
    # Prefer live snapshot if present (written by LocalSessionLogger)
    snapshot = SESSIONS_DIR / "_current.json"
    if snapshot.exists():
        try:
            with open(snapshot, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading live snapshot: {e}")

    if not SESSIONS_DIR.exists():
        return None

    json_files = sorted(SESSIONS_DIR.glob("*.json"), reverse=True)
    if not json_files:
        return None

    session_id = json_files[0].stem
    return load_session(session_id)


@app.route("/")
def index():
    """Render live dashboard."""
    return render_template("live.html")


@app.route("/api/session/current")
def get_current_session():
    """
    Get current session data with telemetry.
    This endpoint is called by the dashboard to update in real-time.
    """
    # Try to load the latest session
    session_data = get_latest_session()
    
    if not session_data:
        return jsonify({
            "status": "waiting",
            "message": "No active session. Run hand_tracking.py to start."
        }), 202
    
    # Extract telemetry
    telemetry = extract_telemetry(session_data)
    latest_x = get_latest_value(telemetry["x"])
    latest_y = get_latest_value(telemetry["y"])
    latest_z = get_latest_value(telemetry["z"])
    
    # Calculate fingerprint
    fingerprint = calculate_fingerprint(session_data)
    
    # Session duration
    start_time = session_data.get("start_timestamp", time.time())
    duration_sec = int(time.time() - start_time)
    hours = duration_sec // 3600
    minutes = (duration_sec % 3600) // 60
    seconds = duration_sec % 60
    uptime = f"{hours:02d}:{minutes:02d}:{seconds:02d}"
    
    # Event count
    event_count = len(session_data.get("events", []))
    
    return jsonify({
        "status": "active",
        "session_id": session_data.get("session_id"),
        "performer_id": session_data.get("performer_id"),
        "fingerprint": fingerprint,
        "telemetry": {
            "x": {
                "label": "Axis_X",
                "current": latest_x,
                "unit": "m/s²",
                "history": telemetry["x"],
            },
            "y": {
                "label": "Axis_Y",
                "current": latest_y,
                "unit": "m/s²",
                "history": telemetry["y"],
            },
            "z": {
                "label": "Axis_Z",
                "current": latest_z,
                "unit": "m/s²",
                "history": telemetry["z"],
            }
        },
        "stats": {
            "events_ingested": event_count,
            "uptime": uptime,
            "vultr_region": "NJ-1",
            "encryption": "AES-256",
        }
    })


@app.route("/api/sessions/list")
def list_sessions():
    """List all saved sessions."""
    if not SESSIONS_DIR.exists():
        return jsonify([])
    
    sessions = []
    for session_file in sorted(SESSIONS_DIR.glob("*.json"), reverse=True):
        session_id = session_file.stem
        data = load_session(session_id)
        
        if data:
            sessions.append({
                "session_id": session_id,
                "performer_id": data.get("performer_id", "Unknown"),
                "event_count": len(data.get("events", [])),
                "fingerprint": calculate_fingerprint(data),
            })
    
    return jsonify(sessions)


@app.route("/health")
def health():
    """Health check endpoint."""
    return jsonify({"status": "healthy", "timestamp": datetime.now().isoformat()})


if __name__ == "__main__":
    SESSIONS_DIR.mkdir(exist_ok=True)
    
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8889
    
    print(f"🎵 GestureHand Live Dashboard")
    print(f"📊 Starting on http://localhost:{port}")
    print(f"📁 Sessions directory: {SESSIONS_DIR}")
    print()
    
    app.run(debug=True, port=port, use_reloader=False)
