#!/usr/bin/env python3
"""Read ESP32 serial stream and send events to Vultr backend."""

import argparse
import json
import re
import time
from urllib import request

import serial


GLOVE_RE = re.compile(
    r"^GLOVE,([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),(\d+),([01]),([01]),([01]),"
    r"(-?[0-9]*\.?[0-9]+),(-?[0-9]*\.?[0-9]+),(-?[0-9]*\.?[0-9]+),"
    r"(-?[0-9]*\.?[0-9]+),(-?[0-9]*\.?[0-9]+),(-?[0-9]*\.?[0-9]+)\s*$"
)


def post_json(url: str, payload: dict):
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with request.urlopen(req, timeout=8) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", required=True)
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--api-base", default="http://104.207.143.159:8000")
    parser.add_argument("--performer-id", default="live-performer")
    parser.add_argument("--batch-size", type=int, default=20)
    args = parser.parse_args()

    started = post_json(f"{args.api_base}/api/session/start", {"performer_id": args.performer_id})
    session_id = started["session_id"]
    print("Session started:", session_id)

    batch = []
    with serial.Serial(args.port, args.baud, timeout=1) as ser:
        try:
            while True:
                line = ser.readline().decode(errors="ignore").strip()
                if not line:
                    continue
                m = GLOVE_RE.match(line)
                if not m:
                    continue
                p, m2, r, pk, fsr, h1, h2, h3, ax, ay, az, gx, gy, gz = m.groups()
                batch.append(
                    {
                        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "pointer": float(p),
                        "middle": float(m2),
                        "ring": float(r),
                        "pinky": float(pk),
                        "fsr": int(fsr),
                        "hall1": int(h1),
                        "hall2": int(h2),
                        "hall3": int(h3),
                        "ax": float(ax),
                        "ay": float(ay),
                        "az": float(az),
                        "gx": float(gx),
                        "gy": float(gy),
                        "gz": float(gz),
                    }
                )

                if len(batch) >= args.batch_size:
                    resp = post_json(f"{args.api_base}/api/session/{session_id}/ingest", {"events": batch})
                    print(f"ingested {resp.get('inserted', 0)}")
                    batch = []
        except KeyboardInterrupt:
            pass

    if batch:
        post_json(f"{args.api_base}/api/session/{session_id}/ingest", {"events": batch})
    final = post_json(f"{args.api_base}/api/session/{session_id}/stop", {})
    print("Session stopped:", final)


if __name__ == "__main__":
    main()
