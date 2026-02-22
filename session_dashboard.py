#!/usr/bin/env python3
"""
GestureHand Session Dashboard - Flask web app to view performances.
Run: python3 session_dashboard.py
Then visit: http://localhost:5000
"""

import json
import os
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, jsonify

app = Flask(__name__)
app.config['TEMPLATES_AUTO_RELOAD'] = True

SESSIONS_DIR = Path(__file__).parent / "sessions"


def load_session(filepath):
    """Load a session JSON file."""
    with open(filepath) as f:
        return json.load(f)


def get_all_sessions():
    """Get all saved sessions, sorted by date."""
    if not SESSIONS_DIR.exists():
        return []
    
    sessions = []
    for file in sorted(SESSIONS_DIR.glob("*.json"), reverse=True):
        try:
            data = load_session(file)
            sessions.append({
                "id": data.get("session_id"),
                "file": file.name,
                "performer": data.get("metadata", {}).get("performer_id", "Unknown"),
                "started": data.get("started_at"),
                "ended": data.get("ended_at"),
                "event_count": data.get("event_count", 0),
            })
        except Exception as e:
            print(f"Error loading {file}: {e}")
    
    return sessions


@app.route("/")
def index():
    """Dashboard homepage - list all sessions."""
    sessions = get_all_sessions()
    return render_template("index.html", sessions=sessions)


@app.route("/session/<session_file>")
def session_detail(session_file):
    """Detailed view of a single session."""
    filepath = SESSIONS_DIR / session_file
    
    if not filepath.exists():
        return "Session not found", 404
    
    data = load_session(filepath)
    return render_template("session.html", session=data)


@app.route("/api/sessions")
def api_sessions():
    """API endpoint for all sessions."""
    return jsonify(get_all_sessions())


@app.route("/api/session/<session_file>")
def api_session(session_file):
    """API endpoint for a single session."""
    filepath = SESSIONS_DIR / session_file
    
    if not filepath.exists():
        return {"error": "Not found"}, 404
    
    return jsonify(load_session(filepath))


if __name__ == "__main__":
    import sys
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8888
    print(f"📊 Starting GestureHand Dashboard...")
    print(f"   Sessions directory: {SESSIONS_DIR}")
    print(f"   Open http://localhost:{port} in your browser")
    app.run(debug=True, host="0.0.0.0", port=port)
