#!/usr/bin/env python3
"""
Local session logger - saves glove + hand tracking data to a JSON file
so you can review your performance without needing the cloud backend.
"""

import json
import os
import time
from datetime import datetime


class LocalSessionLogger:
    """Log sensor events to a local JSON file during performance."""
    
    def __init__(self, session_id=None, output_dir="sessions"):
        """
        Args:
            session_id: Unique session identifier (auto-generated if None)
            output_dir: Directory to store session files
        """
        if session_id is None:
            session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        self.session_id = session_id
        self.output_dir = output_dir
        self.events = []
        self.started_at = datetime.now().isoformat()
        self.start_timestamp = time.time()  # Unix timestamp for live dashboard
        self.metadata = {}
        
        os.makedirs(output_dir, exist_ok=True)
        self.filepath = os.path.join(output_dir, f"{session_id}.json")
    
    def log_event(self, event_type, **data):
        """Log a single event with timestamp."""
        event = {
            "timestamp": datetime.now().isoformat(),
            "type": event_type,
            **data
        }
        self.events.append(event)
        # Also write a lightweight live snapshot so dashboards can read
        # current session state while the performance is running.
        try:
            self._write_snapshot()
        except Exception:
            pass
    
    def log_metadata(self, **kw):
        """Store session metadata (performer name, etc)."""
        self.metadata.update(kw)
    
    def save(self):
        """Write session to disk."""
        session_data = {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "start_timestamp": self.start_timestamp,  # Unix timestamp for live dashboard
            "ended_at": datetime.now().isoformat(),
            "event_count": len(self.events),
            "metadata": self.metadata,
            "events": self.events
        }
        
        with open(self.filepath, "w") as f:
            json.dump(session_data, f, indent=2)
        
        print(f"✅ Session saved: {self.filepath}")
        return self.filepath

    def _write_snapshot(self):
        """Write a lightweight snapshot file used by the live dashboard.

        The snapshot contains current metadata and recent events so the
        web UI can display live telemetry without waiting for the full
        session file to be saved at the end.
        """
        snapshot = {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "start_timestamp": self.start_timestamp,
            "event_count": len(self.events),
            "metadata": self.metadata,
            # keep only the last 500 events for the snapshot
            "events": self.events[-500:],
        }

        snapshot_path = os.path.join(self.output_dir, "_current.json")
        with open(snapshot_path, "w") as f:
            json.dump(snapshot, f)
    
    def summary(self):
        """Print a quick summary of the session."""
        print(f"\n📊 Session: {self.session_id}")
        print(f"   Events logged: {len(self.events)}")
        print(f"   Metadata: {self.metadata}")
        if self.events:
            print(f"   First event: {self.events[0]['type']}")
            print(f"   Last event:  {self.events[-1]['type']}")


def load_session(filepath):
    """Load and display a saved session file."""
    if not os.path.exists(filepath):
        print(f"❌ File not found: {filepath}")
        return
    
    with open(filepath) as f:
        session = json.load(f)
    
    print(f"\n📂 Loaded: {filepath}\n")
    print(f"Session ID:     {session['session_id']}")
    print(f"Started:        {session['started_at']}")
    print(f"Ended:          {session['ended_at']}")
    print(f"Total Events:   {session['event_count']}")
    print(f"Metadata:       {session['metadata']}\n")
    
    # Show event breakdown
    event_types = {}
    for event in session['events']:
        et = event.get('type', 'unknown')
        event_types[et] = event_types.get(et, 0) + 1
    
    print("Event Types:")
    for et, count in sorted(event_types.items()):
        print(f"  - {et}: {count}")
    
    # Show first/last few events
    if session['events']:
        print("\nSample Events:")
        for event in session['events'][:3]:
            print(f"  {event['timestamp']}: {event['type']}")
        if len(session['events']) > 6:
            print("  ...")
        for event in session['events'][-3:]:
            print(f"  {event['timestamp']}: {event['type']}")


def list_sessions(output_dir="sessions"):
    """List all saved sessions."""
    if not os.path.exists(output_dir):
        print(f"No sessions directory found: {output_dir}")
        return
    
    files = sorted([f for f in os.listdir(output_dir) if f.endswith('.json')])
    
    if not files:
        print(f"No sessions found in {output_dir}")
        return
    
    print(f"\n📋 Sessions in {output_dir}:\n")
    for i, fname in enumerate(files, 1):
        fpath = os.path.join(output_dir, fname)
        size = os.path.getsize(fpath)
        print(f"[{i}] {fname} ({size} bytes)")
    
    print(f"\nTo view a session: python3 view_session.py sessions/{files[-1]}")


if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] != "--list":
        # Load and display a specific session
        load_session(sys.argv[1])
    else:
        # List all sessions
        list_sessions()
