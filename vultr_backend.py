#!/usr/bin/env python3
"""Vultr-hosted backend for live glove performance ingestion and verification."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, render_template, request
import psycopg
from psycopg.rows import dict_row


def _env(name: str, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if value is None:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


DB_CONFIG = {
    "host": _env("DB_HOST", "localhost"),
    "port": int(_env("DB_PORT", "5432")),
    "dbname": _env("DB_NAME", "defaultdb"),
    "user": _env("DB_USER", "postgres"),
    "password": _env("DB_PASSWORD", ""),
    "sslmode": _env("DB_SSLMODE", "require"),
}

app = Flask(__name__, template_folder="vultr_templates", static_folder="vultr_static")


def get_conn():
    return psycopg.connect(**DB_CONFIG, row_factory=dict_row)


def now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def compute_fingerprint(events: list[dict[str, Any]]) -> str:
    # Deterministic session fingerprint for verification + NFT metadata anchoring.
    canonical = json.dumps(events, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@app.get("/")
def index():
    return render_template("index.html")


@app.get("/health")
def health():
    try:
        with get_conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            _ = cur.fetchone()
        return jsonify({"ok": True, "time": now_iso()})
    except Exception as exc:  # pragma: no cover - operational endpoint
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.post("/api/session/start")
def session_start():
    body = request.get_json(silent=True) or {}
    performer_id = body.get("performer_id", "anonymous")
    session_id = str(uuid.uuid4())

    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO sessions (id, performer_id, started_at, status)
            VALUES (%s, %s, NOW(), 'active')
            """,
            (session_id, performer_id),
        )
        conn.commit()

    return jsonify({"session_id": session_id, "started_at": now_iso(), "status": "active"})


@app.post("/api/session/<session_id>/ingest")
def session_ingest(session_id: str):
    body = request.get_json(silent=True) or {}
    events = body.get("events", [])
    if not isinstance(events, list) or not events:
        return jsonify({"error": "events must be a non-empty list"}), 400

    rows = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        ts = ev.get("ts", now_iso())
        rows.append(
            (
                session_id,
                ts,
                ev.get("pointer"),
                ev.get("middle"),
                ev.get("ring"),
                ev.get("pinky"),
                ev.get("fsr"),
                ev.get("hall1"),
                ev.get("hall2"),
                ev.get("hall3"),
                ev.get("ax"),
                ev.get("ay"),
                ev.get("az"),
                ev.get("gx"),
                ev.get("gy"),
                ev.get("gz"),
                json.dumps(ev),
            )
        )

    if not rows:
        return jsonify({"error": "no valid events"}), 400

    with get_conn() as conn, conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO events (
              session_id, ts, pointer, middle, ring, pinky, fsr, hall1, hall2, hall3,
              ax, ay, az, gx, gy, gz, payload
            ) VALUES (
              %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb
            )
            """,
            rows,
        )
        conn.commit()

    return jsonify({"ok": True, "session_id": session_id, "inserted": len(rows)})


@app.post("/api/session/<session_id>/stop")
def session_stop(session_id: str):
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT payload
            FROM events
            WHERE session_id = %s
            ORDER BY ts ASC
            """,
            (session_id,),
        )
        events = [r["payload"] for r in cur.fetchall()]
        fingerprint = compute_fingerprint(events) if events else None

        cur.execute(
            """
            UPDATE sessions
            SET ended_at = NOW(),
                status = 'stopped',
                event_count = %s,
                fingerprint = %s
            WHERE id = %s
            """,
            (len(events), fingerprint, session_id),
        )
        conn.commit()

    return jsonify(
        {
            "ok": True,
            "session_id": session_id,
            "event_count": len(events),
            "fingerprint": fingerprint,
        }
    )


@app.get("/api/sessions/recent")
def sessions_recent():
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, performer_id, started_at, ended_at, status, event_count, fingerprint
            FROM sessions
            ORDER BY started_at DESC
            LIMIT 50
            """
        )
        rows = cur.fetchall()
    return jsonify({"sessions": rows})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "8000")), debug=False)
