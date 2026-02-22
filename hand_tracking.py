#!/usr/bin/env python3
"""Hybrid glove + camera MIDI controller.

- Reads flex + thumb touch from ESP32 over serial/Bluetooth.
- Runs MediaPipe hand tracking from webcam.
- Fuses both to trigger notes and CC controls.
- Plays evolving Markov chord pad on MIDI channel 2.
- Thumb sensor ADC value is also mapped continuously to CC27.
"""

import argparse
import os
import random
import re
import threading
import time
import urllib.request

import cv2
import math
import mediapipe as mp
import mido
import mido.backends.rtmidi
import numpy as np
import serial
from chord_library import ChordSequencePlayer

if not hasattr(serial, "Serial"):
    raise SystemExit(
        "Wrong 'serial' package installed. Run:\n"
        "  python3 -m pip uninstall -y serial\n"
        "  python3 -m pip install pyserial"
    )


# ── Madgwick AHRS (ported from imu_visualiser.py) ──────────────────────────
class MadgwickAHRS:
    """Minimal Madgwick filter (gyro + accel, no mag). q = [w, x, y, z]"""
    def __init__(self, beta: float = 0.05, freq: float = 50.0):
        self.beta = beta
        self.dt   = 1.0 / freq
        self.q    = np.array([1.0, 0.0, 0.0, 0.0])

    def reset(self):
        self.q = np.array([1.0, 0.0, 0.0, 0.0])

    def update(self, gx, gy, gz, ax, ay, az):
        """gx/gy/gz in deg/s; ax/ay/az in m/s² (or raw g-units — direction matters, not scale)."""
        gx, gy, gz = math.radians(gx), math.radians(gy), math.radians(gz)
        q = self.q
        w, x, y, z = q
        norm = math.sqrt(ax*ax + ay*ay + az*az)
        if norm == 0:
            return
        ax, ay, az = ax/norm, ay/norm, az/norm
        f1 = 2*(x*z - w*y)       - ax
        f2 = 2*(w*x + y*z)       - ay
        f3 = 2*(0.5 - x*x - y*y) - az
        J  = np.array([
            [-2*y,  2*z, -2*w, 2*x],
            [ 2*x,  2*w,  2*z, 2*y],
            [  0,  -4*x, -4*y,  0 ],
        ])
        step = J.T @ np.array([f1, f2, f3])
        n = np.linalg.norm(step)
        if n:
            step /= n
        q_dot = 0.5 * np.array([
            -x*gx - y*gy - z*gz,
             w*gx + y*gz - z*gy,
             w*gy - x*gz + z*gx,
             w*gz + x*gy - y*gx,
        ]) - self.beta * step
        q = q + q_dot * self.dt
        self.q = q / np.linalg.norm(q)

    def pitch_deg(self) -> float:
        """Return pitch angle in degrees (−90 … +90)."""
        w, x, y, z = self.q
        return math.degrees(math.asin(max(-1.0, min(1.0, 2.0 * (w*y - z*x)))))


# --- IMU Pitch → MIDI CC config ---
PITCH_CC      = 28     # CC number for Madgwick-filtered pitch
PITCH_DEG_MIN = -90.0  # pitch angle that maps to CC 0
PITCH_DEG_MAX =  90.0  # pitch angle that maps to CC 127
PITCH_CC_DEAD = 1      # minimum CC change required to send


# --- Flex calibration from measured values ---
# STRAIGHT_V = voltage when finger is straight (unbent)
# BENT_V     = voltage when finger is fully bent
# Note: ring finger is reversed — lower voltage = more bent
FINGERS = ["pointer", "middle", "ring", "pinky"]
STRAIGHT_V = {"pointer": 2.9,  "middle": 2.4,  "ring": 2.34,  "pinky": 2.8}
BENT_V     = {"pointer": 2.6,  "middle": 2.0,  "ring": 2.8, "pinky": 3.2}
FLEX_THRESH = {f: (STRAIGHT_V[f] + BENT_V[f]) / 2.0 for f in FINGERS}

# Velocity (rate-of-change) trigger parameters
# The trigger compares a fast EMA against a slow EMA of the normalised flex
# value; the difference is the instantaneous bend velocity (0..1 per frame).
FLEX_WEIGHT    = 0.7   # weight of glove sensor vs camera in fused bend
CAM_WEIGHT     = 0.3
SMOOTH_ALPHA   = 0.15  # slow EMA — tracks absolute position, filters noise
VEL_ALPHA      = 0.55  # fast EMA — tracks rapid changes
VEL_ON_THRESH  = 0.08  # positive velocity spike needed to trigger note-on
VEL_OFF_THRESH = 0.06  # negative velocity spike (magnitude) to trigger note-off
VEL_VEL_MIN    = 0.08  # velocity spike that maps to MIDI velocity 40
VEL_VEL_MAX    = 0.55  # velocity spike that maps to MIDI velocity 127
NOTE_MAX_AGE   = 4.0   # seconds — auto-release notes held longer than this

# Thumb sensor CC mapping
# -------------------------
# CC27 is sent continuously as the thumb ADC value rises/falls.
# Adjust THUMB_ADC_MIN / THUMB_ADC_MAX to match your sensor's actual range.
# The ESP32 12-bit ADC spans 0–4095; trim these if your sensor saturates earlier.
THUMB_CC        = 27    # MIDI CC number assigned to the thumb sensor
THUMB_ADC_MIN   = 0     # raw ADC value that maps to CC 0
THUMB_ADC_MAX   = 4095  # raw ADC value that maps to CC 127
THUMB_CC_DEAD   = 2     # minimum CC change required to send (avoids jitter spam)

# Parse both new and legacy ESP32 formats
FLEX_CSV_RE = re.compile(
    r"^FLEX,([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),(\d+)\s*$"
)
GLOVE_CSV_RE = re.compile(
    r"^GLOVE,([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),(\d+),([01])\s*$"
)
GLOVE_IMU_CSV_RE = re.compile(
    r"^GLOVE,([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),(\d+),([01]),"
    r"(-?[0-9]*\.?[0-9]+),(-?[0-9]*\.?[0-9]+),(-?[0-9]*\.?[0-9]+),"
    r"(-?[0-9]*\.?[0-9]+),(-?[0-9]*\.?[0-9]+),(-?[0-9]*\.?[0-9]+)\s*$"
)
GLOVE_IMU_HALL3_CSV_RE = re.compile(
    r"^GLOVE,([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),([0-9]*\.?[0-9]+),(\d+),([01]),([01]),([01]),"
    r"(-?[0-9]*\.?[0-9]+),(-?[0-9]*\.?[0-9]+),(-?[0-9]*\.?[0-9]+),"
    r"(-?[0-9]*\.?[0-9]+),(-?[0-9]*\.?[0-9]+),(-?[0-9]*\.?[0-9]+)\s*$"
)
HALL_3_RE = re.compile(r"^HALL,([01]),([01]),([01])\s*$")
LEGACY_FINGER_RE = re.compile(
    r"^(Pointer|Middle|Ring|Pinky) Finger:\s*([0-9]*\.?[0-9]+)\s*V$", re.IGNORECASE
)
LEGACY_THUMB_RE = re.compile(r"^Thumb Touch Value:\s*(\d+)\s*$", re.IGNORECASE)


# --- Chord generation ---
def midi_to_pc(midi_notes):
    return sorted(set(n % 12 for n in midi_notes))


CHORD_TEMPLATES = {
    "maj7": [0, 4, 7, 11],
    "min7": [0, 3, 7, 10],
    "7": [0, 4, 7, 10],
    "maj": [0, 4, 7],
    "min": [0, 3, 7],
    "sus2": [0, 2, 7],
    "sus4": [0, 5, 7],
    "add9": [0, 4, 7, 2],
    "dim": [0, 3, 6],
}

MAJOR_SCALE = [0, 2, 4, 5, 7, 9, 11]
DEGREE_MAP = {"I": 0, "ii": 1, "iii": 2, "IV": 3, "V": 4, "vi": 5, "vii": 6}

MARKOV_RELATIVE_CLOSED = {
    "Imaj7": {"vim7": 0.25, "IVmaj7": 0.25, "iiim7": 0.20, "Vsus2": 0.15, "Iadd9": 0.15},
    "vim7": {"IVmaj7": 0.30, "Imaj7": 0.25, "iiim7": 0.20, "iim7": 0.15, "vim9": 0.10},
    "IVmaj7": {"Imaj7": 0.30, "Vsus2": 0.20, "vim7": 0.20, "iiim7": 0.15, "IVadd9": 0.15},
    "iiim7": {"vim7": 0.30, "IVmaj7": 0.25, "Imaj7": 0.20, "iim7": 0.15, "Vsus2": 0.10},
    "iim7": {"IVmaj7": 0.30, "vim7": 0.25, "Imaj7": 0.20, "Vsus2": 0.15, "iiadd9": 0.10},
    "Vsus2": {"Imaj7": 0.35, "vim7": 0.25, "IVmaj7": 0.20, "Vsus4": 0.10, "Vadd9": 0.10},
    "Iadd9": {"vim7": 0.30, "IVmaj7": 0.25, "Vsus2": 0.25, "iiim7": 0.20},
    "vim9": {"Imaj7": 0.30, "IVmaj7": 0.25, "Vsus2": 0.25, "iiim7": 0.20},
    "IVadd9": {"Imaj7": 0.30, "vim7": 0.25, "Vsus2": 0.25, "iiim7": 0.20},
    "Vsus4": {"Imaj7": 0.35, "vim7": 0.25, "IVmaj7": 0.20, "Vsus2": 0.20},
    "Vadd9": {"Imaj7": 0.35, "vim7": 0.25, "IVmaj7": 0.20, "Vsus2": 0.20},
    "iiadd9": {"IVmaj7": 0.40, "vim7": 0.30, "Imaj7": 0.30},
}


def detect_chord(midi_notes):
    pcs = midi_to_pc(midi_notes)
    for root in pcs:
        intervals = sorted((p - root) % 12 for p in pcs)
        for name, template in CHORD_TEMPLATES.items():
            if set(template).issubset(intervals):
                return root, name
    return pcs[0], "maj"


def relative_to_midi(key_root, relative_chord):
    degree_str = None
    chord_type = None
    for degree in DEGREE_MAP:
        if relative_chord.startswith(degree):
            degree_str = degree
            chord_type = relative_chord[len(degree):]
            break

    if degree_str is None:
        degree_str = "I"
        chord_type = "maj"

    if chord_type == "m7":
        chord_type = "min7"
    elif chord_type == "m":
        chord_type = "min"
    elif chord_type in ("9", "m9"):
        chord_type = "min7" if "m" in chord_type else "maj7"
    elif chord_type == "":
        chord_type = "maj"

    root_pc = key_root + MAJOR_SCALE[DEGREE_MAP[degree_str]]
    template = CHORD_TEMPLATES.get(chord_type, [0, 4, 7])
    return [root_pc + interval for interval in template]


def next_relative_chord(current, last=None):
    choices = dict(MARKOV_RELATIVE_CLOSED.get(current, {}))
    if not choices:
        return random.choice(list(MARKOV_RELATIVE_CLOSED.keys()))
    if last in choices:
        del choices[last]
    return random.choices(list(choices.keys()), weights=list(choices.values()))[0]


def generate_next_chord_midi(current_midi_notes, key_root_pc):
    root, chord_type = detect_chord(current_midi_notes)
    semitone_diff = (root - key_root_pc) % 12
    degree_index = MAJOR_SCALE.index(semitone_diff) if semitone_diff in MAJOR_SCALE else 0
    degree_name = list(DEGREE_MAP.keys())[list(DEGREE_MAP.values()).index(degree_index)]
    relative_chord = degree_name + chord_type
    next_rel_chord = next_relative_chord(relative_chord)
    return relative_to_midi(key_root_pc, next_rel_chord), next_rel_chord


def clamp_midi(v):
    return max(0, min(127, int(v)))


def pitch_to_octave(pitch_deg: float) -> int:
    """Return a randomly sampled octave offset (−2 … +2) whose probability
    distribution is centred on a value that tracks IMU pitch.

    pitch_deg = 0   → centre = 0  (uniform-ish, slightly prefers mid octaves)
    pitch_deg = +90 → centre = +2 (strongly biased toward high octaves)
    pitch_deg = −90 → centre = −2 (strongly biased toward low octaves)

    A softmax over evenly-spaced logits gives a smooth, bell-shaped
    distribution that shifts with pitch while always leaving every octave
    at least some non-zero probability — so surprises still happen.
    """
    OCTAVES   = [-2, -1, 0, 1, 2]
    # Map pitch linearly: −90° → −2.0 centre, +90° → +2.0 centre
    centre    = (pitch_deg / 90.0) * 2.0
    # Temperature: lower = more deterministic; 1.0 feels nicely probabilistic
    TEMP      = 1.0
    logits    = [-(o - centre) ** 2 / (2 * TEMP ** 2) for o in OCTAVES]
    # Softmax
    max_l     = max(logits)
    exps      = [math.exp(l - max_l) for l in logits]
    total     = sum(exps)
    weights   = [e / total for e in exps]
    return random.choices(OCTAVES, weights=weights)[0]


def normalize_flex(finger, voltage):
    """Returns 0.0 (straight) to 1.0 (fully bent), regardless of which
    direction the voltage moves for this particular sensor."""
    lo = min(STRAIGHT_V[finger], BENT_V[finger])
    hi = max(STRAIGHT_V[finger], BENT_V[finger])
    if hi == lo:
        return 0.0
    norm = (voltage - lo) / (hi - lo)
    # If this sensor reads lower when bent, invert so 1.0 still means bent.
    if BENT_V[finger] < STRAIGHT_V[finger]:
        norm = 1.0 - norm
    return max(0.0, min(1.0, norm))


def estimate_bend(landmarks, mcp, pip, dip):
    a = np.array([landmarks[mcp].x, landmarks[mcp].y])
    b = np.array([landmarks[pip].x, landmarks[pip].y])
    c = np.array([landmarks[dip].x, landmarks[dip].y])
    ba = a - b
    bc = c - b
    cosine = np.dot(ba, bc) / (np.linalg.norm(ba) * np.linalg.norm(bc) + 1e-6)
    angle = np.degrees(np.arccos(np.clip(cosine, -1, 1)))
    return 1.0 - (angle / 180.0)


def get_camera_bends(hand_landmarks):
    return {
        "pointer": estimate_bend(hand_landmarks, 5, 6, 7),
        "middle": estimate_bend(hand_landmarks, 9, 10, 11),
        "ring": estimate_bend(hand_landmarks, 13, 14, 15),
        "pinky": estimate_bend(hand_landmarks, 17, 18, 19),
    }


def get_hand_position(hand_landmarks):
    xs = [lm.x for lm in hand_landmarks]
    ys = [lm.y for lm in hand_landmarks]
    return float(np.mean(xs)), float(np.mean(ys))


def thumb_adc_to_cc(raw_value):
    """Map raw ESP32 ADC thumb value to a MIDI CC value (0–127)."""
    if raw_value is None:
        return 0
    span = THUMB_ADC_MAX - THUMB_ADC_MIN
    if span == 0:
        return 0
    normalized = (raw_value - THUMB_ADC_MIN) / span
    return clamp_midi(normalized * 127)


class FlexReader(threading.Thread):
    def __init__(self, port, baud):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.lock = threading.Lock()
        self.values = {
            "pointer": 0.0,
            "middle": 0.0,
            "ring": 0.0,
            "pinky": 0.0,
            "thumb": None,
            "hall": 0,
            "hall1": 0,
            "hall2": 0,
            "hall3": 0,
            "ax": 0.0,
            "ay": 0.0,
            "az": 0.0,
            "gx": 0.0,
            "gy": 0.0,
            "gz": 0.0,
        }
        self.last_update = 0.0
        self.connected = False
        self.error = None
        # IMU zero-reference: set to the first valid reading, subtracted from all
        # subsequent readings so the glove starts at a neutral (0,0,0,0,0,0) pose.
        # Call recalibrate() to reset to the current orientation at any time.
        self._imu_calib = None   # (ax, ay, az, gx, gy, gz) or None

    def recalibrate(self):
        """Reset IMU calibration — next reading becomes the new zero reference."""
        with self.lock:
            self._imu_calib = None
        print("[IMU] Calibration reset — next reading will be the new zero reference.")

    def _apply_imu_calib(self, ax, ay, az, gx, gy, gz):
        """Subtract the zero-reference from raw IMU values.
        Sets the reference on the first call (mirrors imu_visualiser.py behaviour)."""
        if self._imu_calib is None:
            self._imu_calib = (ax, ay, az, gx, gy, gz)
            print(f"[IMU] Calibration locked: ax={ax:.3f} ay={ay:.3f} az={az:.3f} "
                  f"gx={gx:.3f} gy={gy:.3f} gz={gz:.3f}")
        cx, cy, cz, cgx, cgy, cgz = self._imu_calib
        return ax-cx, ay-cy, az-cz, gx-cgx, gy-cgy, gz-cgz

    def snapshot(self):
        with self.lock:
            return dict(self.values), self.last_update, self.connected, self.error

    def run(self):
        partial = {}
        while True:
            try:
                with serial.Serial(self.port, self.baud, timeout=1) as ser:
                    self.connected = True
                    self.error = None
                    while True:
                        line = ser.readline().decode(errors="ignore").strip()
                        if not line:
                            continue

                        gh3 = GLOVE_IMU_HALL3_CSV_RE.match(line)
                        if gh3:
                            p, m2, r, pk, t, h1, h2, h3, ax, ay, az, gx, gy, gz = gh3.groups()
                            h1i = int(h1)
                            h2i = int(h2)
                            h3i = int(h3)
                            cax, cay, caz, cgx, cgy, cgz = self._apply_imu_calib(
                                float(ax), float(ay), float(az),
                                float(gx), float(gy), float(gz))
                            with self.lock:
                                self.values["pointer"] = float(p)
                                self.values["middle"] = float(m2)
                                self.values["ring"] = float(r)
                                self.values["pinky"] = float(pk)
                                self.values["thumb"] = int(t)
                                self.values["hall1"] = h1i
                                self.values["hall2"] = h2i
                                self.values["hall3"] = h3i
                                self.values["hall"] = 1 if (h1i or h2i or h3i) else 0
                                self.values["ax"] = cax
                                self.values["ay"] = cay
                                self.values["az"] = caz
                                self.values["gx"] = cgx
                                self.values["gy"] = cgy
                                self.values["gz"] = cgz
                                self.last_update = time.time()
                            continue

                        gi = GLOVE_IMU_CSV_RE.match(line)
                        if gi:
                            p, m2, r, pk, t, h, ax, ay, az, gx, gy, gz = gi.groups()
                            cax, cay, caz, cgx, cgy, cgz = self._apply_imu_calib(
                                float(ax), float(ay), float(az),
                                float(gx), float(gy), float(gz))
                            with self.lock:
                                self.values["pointer"] = float(p)
                                self.values["middle"] = float(m2)
                                self.values["ring"] = float(r)
                                self.values["pinky"] = float(pk)
                                self.values["thumb"] = int(t)
                                self.values["hall"] = int(h)
                                self.values["hall1"] = int(h)
                                self.values["hall2"] = 0
                                self.values["hall3"] = 0
                                self.values["ax"] = cax
                                self.values["ay"] = cay
                                self.values["az"] = caz
                                self.values["gx"] = cgx
                                self.values["gy"] = cgy
                                self.values["gz"] = cgz
                                self.last_update = time.time()
                            continue

                        g = GLOVE_CSV_RE.match(line)
                        if g:
                            p, m2, r, pk, t, h = g.groups()
                            with self.lock:
                                self.values["pointer"] = float(p)
                                self.values["middle"] = float(m2)
                                self.values["ring"] = float(r)
                                self.values["pinky"] = float(pk)
                                self.values["thumb"] = int(t)
                                self.values["hall"] = int(h)
                                self.values["hall1"] = int(h)
                                self.values["hall2"] = 0
                                self.values["hall3"] = 0
                                self.values["ax"] = 0.0
                                self.values["ay"] = 0.0
                                self.values["az"] = 0.0
                                self.values["gx"] = 0.0
                                self.values["gy"] = 0.0
                                self.values["gz"] = 0.0
                                self.last_update = time.time()
                            continue

                        m = FLEX_CSV_RE.match(line)
                        if m:
                            p, m2, r, pk, t = m.groups()
                            with self.lock:
                                self.values["pointer"] = float(p)
                                self.values["middle"] = float(m2)
                                self.values["ring"] = float(r)
                                self.values["pinky"] = float(pk)
                                self.values["thumb"] = int(t)
                                self.values["hall"] = 0
                                self.values["hall1"] = 0
                                self.values["hall2"] = 0
                                self.values["hall3"] = 0
                                self.values["ax"] = 0.0
                                self.values["ay"] = 0.0
                                self.values["az"] = 0.0
                                self.values["gx"] = 0.0
                                self.values["gy"] = 0.0
                                self.values["gz"] = 0.0
                                self.last_update = time.time()
                            continue

                        lm = LEGACY_FINGER_RE.match(line)
                        if lm:
                            partial[lm.group(1).lower()] = float(lm.group(2))
                            continue

                        tm = LEGACY_THUMB_RE.match(line)
                        if tm:
                            if all(f in partial for f in FINGERS):
                                with self.lock:
                                    for f in FINGERS:
                                        self.values[f] = partial[f]
                                    self.values["thumb"] = int(tm.group(1))
                                    self.values["hall"] = 0
                                    self.values["hall1"] = 0
                                    self.values["hall2"] = 0
                                    self.values["hall3"] = 0
                                    self.values["ax"] = 0.0
                                    self.values["ay"] = 0.0
                                    self.values["az"] = 0.0
                                    self.values["gx"] = 0.0
                                    self.values["gy"] = 0.0
                                    self.values["gz"] = 0.0
                                    self.last_update = time.time()
                            partial = {}
            except Exception as exc:
                self.connected = False
                self.error = str(exc)
                time.sleep(1.0)


def parse_args():
    parser = argparse.ArgumentParser(description="Hybrid flex + MediaPipe MIDI glove")
    parser.add_argument("--port", help="Bluetooth/serial port for ESP32")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--midi-port", default="GestureHand MIDI",
                        help="Name of the virtual MIDI output port to create (default: 'GestureHand MIDI')")
    parser.add_argument("--list-midi", action="store_true",
                        help="List available MIDI output ports and exit")
    parser.add_argument("--camera-index", type=int, default=0, help="OpenCV camera index")
    parser.add_argument("--list-cameras", action="store_true", help="Probe and list available camera indexes")
    parser.add_argument("--key-root", type=int, default=60)
    parser.add_argument("--thumb-threshold", type=int, default=1440)
    parser.add_argument("--thumb-mode", choices=["below", "above", "off"], default="off")
    parser.add_argument("--thumb-cc", type=int, default=THUMB_CC,
                        help=f"MIDI CC number for continuous thumb pressure (default: {THUMB_CC})")
    parser.add_argument("--thumb-adc-min", type=int, default=THUMB_ADC_MIN,
                        help=f"Raw ADC value that maps to CC 0 (default: {THUMB_ADC_MIN})")
    parser.add_argument("--thumb-adc-max", type=int, default=THUMB_ADC_MAX,
                        help=f"Raw ADC value that maps to CC 127 (default: {THUMB_ADC_MAX})")
    parser.add_argument("--chord-min", type=float, default=5.0)
    parser.add_argument("--chord-max", type=float, default=15.0)
    parser.add_argument("--chord-source", choices=["library", "markov"], default="library")
    return parser.parse_args()


def list_cameras(max_index=8):
    print("Probing cameras...")
    found = []
    for idx in range(max_index + 1):
        cap = cv2.VideoCapture(idx)
        ok = bool(cap.isOpened())
        if ok:
            ret, _ = cap.read()
            if ret:
                found.append(idx)
                print(f"  camera index {idx}: OK")
            else:
                print(f"  camera index {idx}: opened but no frame")
        cap.release()
    if not found:
        print("  no working cameras found")
    return found


class MidiOutput:
    """Pure MIDI output — opens a virtual (or existing) port and forwards all messages.
    No audio synthesis; sound comes from whatever synth/DAW is listening on the other end."""

    def __init__(self, port_name: str):
        self.port_name = port_name
        try:
            self._port = mido.open_output(port_name, virtual=True)
            print(f"MIDI virtual port opened: '{port_name}'")
            print("  Connect your DAW or synth to this port to hear sound.")
        except Exception as exc:
            available = mido.get_output_names()
            print(f"Could not open virtual port '{port_name}': {exc}")
            if available:
                self._port = mido.open_output(available[0])
                print(f"Using existing MIDI port: '{available[0]}'")
            else:
                raise SystemExit(
                    "No MIDI output ports available. Connect a MIDI device or install a "
                    "virtual MIDI driver (e.g. IAC Driver on macOS, loopMIDI on Windows)."
                )

    def send(self, msg: mido.Message) -> None:
        self._port.send(msg)

    def close(self) -> None:
        # All-notes-off on every channel before closing.
        for ch in range(16):
            self._port.send(mido.Message("control_change", channel=ch, control=123, value=0))
        self._port.close()


def run_flex_calibration(reader: "FlexReader") -> tuple[dict, dict]:
    """Interactive OpenCV calibration wizard.

    Shows a live voltage display and walks the user through two poses:
      1. OPEN hand  → becomes STRAIGHT_V  (fingers fully extended)
       2. CLOSED fist → becomes BENT_V    (fingers fully curled)

    Returns (straight_v, bent_v) dicts keyed by finger name.
    Blocks until both poses are captured or the user skips with S.
    """
    SAMPLE_COUNT  = 60      # frames averaged per pose (~1 s at 60 fps)
    BAR_W, BAR_H  = 400, 28
    WIN           = "Flex Calibration"
    COLORS = {
        "pointer": (255, 100, 100),
        "middle":  (100, 255, 100),
        "ring":    (100, 180, 255),
        "pinky":   (220, 100, 255),
    }
    POSE_NAMES  = ["OPEN (straight)", "CLOSED (fist)"]
    POSE_KEYS   = ["straight", "bent"]

    captured: dict[str, dict] = {}   # "straight" / "bent" → {finger: voltage}

    cv2.namedWindow(WIN, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(WIN, 700, 500)

    pose_idx   = 0
    samples    = {f: [] for f in FINGERS}
    collecting = False
    done       = False

    while not done:
        # ── pull latest voltages ──────────────────────────────────────────
        flex, _, connected, _ = reader.snapshot()

        frame = np.zeros((500, 700, 3), dtype=np.uint8)

        # Title
        title = f"Step {pose_idx+1}/2: Hold hand {POSE_NAMES[pose_idx].upper()}"
        cv2.putText(frame, title, (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.85, (255, 255, 255), 2)

        # Connection status
        conn_col = (0, 220, 0) if connected else (0, 60, 220)
        cv2.putText(frame, "BT OK" if connected else "waiting for BT…",
                    (20, 75), cv2.FONT_HERSHEY_SIMPLEX, 0.6, conn_col, 1)

        # Per-finger live bars + voltage text
        y0 = 110
        for i, f in enumerate(FINGERS):
            v   = flex[f]
            col = COLORS[f]
            lo  = min(STRAIGHT_V[f], BENT_V[f])
            hi  = max(STRAIGHT_V[f], BENT_V[f])
            span = hi - lo or 0.5
            bar_fill = int(BAR_W * max(0.0, min(1.0, (v - lo + 0.1) / (span + 0.2))))
            y = y0 + i * 65

            cv2.rectangle(frame, (20, y), (20 + BAR_W, y + BAR_H), (50, 50, 50), -1)
            cv2.rectangle(frame, (20, y), (20 + bar_fill, y + BAR_H), col, -1)
            cv2.rectangle(frame, (20, y), (20 + BAR_W, y + BAR_H), (160, 160, 160), 1)
            cv2.putText(frame, f"{f.capitalize():8s}  {v:.3f} V",
                        (440, y + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.65, col, 2)

            # Show collected sample count while collecting
            if collecting:
                n = len(samples[f])
                pct = int(BAR_W * n / SAMPLE_COUNT)
                cv2.rectangle(frame, (20, y + BAR_H + 2),
                              (20 + pct, y + BAR_H + 7), (0, 220, 220), -1)

        # Instructions
        inst_y = 390
        if not collecting:
            lines = [
                f"Hold your hand {POSE_NAMES[pose_idx]}  then press  SPACE  to capture.",
                "Press  S  to skip calibration and use defaults.",
            ]
        else:
            pct_done = int(100 * len(samples[FINGERS[0]]) / SAMPLE_COUNT)
            lines = [f"Capturing… {pct_done}%  — keep holding!", ""]

        for li, line in enumerate(lines):
            cv2.putText(frame, line, (20, inst_y + li * 32),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.62, (220, 220, 60), 1)

        # Already-captured poses tick
        for pi, pk in enumerate(POSE_KEYS):
            if pk in captured:
                cv2.putText(frame, f"✓ {POSE_NAMES[pi]} captured",
                            (20, 460 + pi * 0), cv2.FONT_HERSHEY_SIMPLEX,
                            0.55, (0, 220, 0), 1)

        cv2.imshow(WIN, frame)
        key = cv2.waitKey(16) & 0xFF   # ~60 fps

        # ── collect samples ───────────────────────────────────────────────
        if collecting:
            for f in FINGERS:
                samples[f].append(flex[f])
            if len(samples[FINGERS[0]]) >= SAMPLE_COUNT:
                # Average and store
                captured[POSE_KEYS[pose_idx]] = {
                    f: float(np.mean(samples[f])) for f in FINGERS
                }
                print(f"[CAL] {POSE_NAMES[pose_idx]} captured: "
                      + ", ".join(f"{f}={captured[POSE_KEYS[pose_idx]][f]:.3f}V"
                                  for f in FINGERS))
                samples    = {f: [] for f in FINGERS}
                collecting = False
                pose_idx  += 1
                if pose_idx >= 2:
                    done = True

        # ── key handling ──────────────────────────────────────────────────
        if key == ord(" ") and not collecting:
            collecting = True
            samples    = {f: [] for f in FINGERS}
        elif key == ord("s") or key == ord("S"):
            print("[CAL] Skipped — using default calibration values.")
            cv2.destroyWindow(WIN)
            return dict(STRAIGHT_V), dict(BENT_V)

    cv2.destroyWindow(WIN)

    straight = captured.get("straight", dict(STRAIGHT_V))
    bent     = captured.get("bent",     dict(BENT_V))

    # Sanity check: if a finger's straight/bent are too close, warn and use defaults
    for f in FINGERS:
        if abs(straight[f] - bent[f]) < 0.05:
            print(f"[CAL] Warning: {f} finger range too small "
                  f"({straight[f]:.3f}V vs {bent[f]:.3f}V) — using defaults.")
            straight[f] = STRAIGHT_V[f]
            bent[f]     = BENT_V[f]

    print("[CAL] Calibration complete.")
    print("[CAL] STRAIGHT_V = " + str({f: round(straight[f], 3) for f in FINGERS}))
    print("[CAL] BENT_V     = " + str({f: round(bent[f],     3) for f in FINGERS}))
    return straight, bent


def main():
    args = parse_args()

    if args.list_cameras:
        list_cameras()
        return

    if args.list_midi:
        print("Available MIDI output ports:")
        for name in mido.get_output_names():
            print(f"  {name}")
        return

    if not args.port:
        raise SystemExit("Missing --port. Example: --port /dev/cu.usbserial-0001")

    midi_out = MidiOutput(args.midi_port)

    # Allow CLI overrides for thumb CC config
    thumb_cc_num   = args.thumb_cc
    thumb_adc_min  = args.thumb_adc_min
    thumb_adc_max  = args.thumb_adc_max

    def _thumb_adc_to_cc(raw_value):
        """Map raw ESP32 ADC thumb value to a MIDI CC value (0–127)."""
        if raw_value is None:
            return 0
        span = thumb_adc_max - thumb_adc_min
        if span == 0:
            return 0
        normalized = (raw_value - thumb_adc_min) / span
        return clamp_midi(normalized * 127)

    # Download MediaPipe model if missing
    if not os.path.exists("hand_landmarker.task"):
        print("Downloading hand_landmarker.task...")
        urllib.request.urlretrieve(
            "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
            "hand_landmarker.task",
        )

    BaseOptions = mp.tasks.BaseOptions
    HandLandmarker = mp.tasks.vision.HandLandmarker
    HandLandmarkerOptions = mp.tasks.vision.HandLandmarkerOptions
    VisionRunningMode = mp.tasks.vision.RunningMode

    options = HandLandmarkerOptions(
        base_options=BaseOptions(model_asset_path="hand_landmarker.task"),
        running_mode=VisionRunningMode.IMAGE,
        num_hands=1,
    )

    cap = cv2.VideoCapture(args.camera_index)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    camera_enabled = bool(cap.isOpened())
    if not camera_enabled:
        print(f"Camera index {args.camera_index} unavailable. Running flex-only mode.")

    reader = FlexReader(args.port, args.baud)
    reader.start()

    # ── Startup flex calibration ──────────────────────────────────────────
    # Wait briefly for BT connection before showing the calibration UI
    print("Waiting for Bluetooth connection…")
    for _ in range(50):
        _, _, connected, _ = reader.snapshot()
        if connected:
            break
        time.sleep(0.1)

    cal_straight, cal_bent = run_flex_calibration(reader)

    # Build per-finger normalization closures using calibrated values
    def normalize_flex_cal(finger: str, voltage: float) -> float:
        """normalize_flex() using runtime-calibrated STRAIGHT_V / BENT_V."""
        lo = min(cal_straight[finger], cal_bent[finger])
        hi = max(cal_straight[finger], cal_bent[finger])
        if hi == lo:
            return 0.0
        norm = (voltage - lo) / (hi - lo)
        if cal_bent[finger] < cal_straight[finger]:
            norm = 1.0 - norm
        return max(0.0, min(1.0, norm))

    chord_player = ChordSequencePlayer()
    if args.chord_source == "library":
        start = chord_player.current()
        notes = list(start.notes)
        chord_name = start.name
    else:
        notes = [args.key_root, args.key_root + 4, args.key_root + 7, args.key_root + 11]
        chord_name = "Imaj7"
    next_chord_change = time.time() + random.uniform(args.chord_min, args.chord_max)

    chord_lock = threading.Lock()
    chord_notes_on = []
    chord_changed = threading.Event()
    stop_event = threading.Event()

    note_pool_order = ["pointer", "middle", "ring", "pinky"]
    notes_on  = {f: None for f in note_pool_order}
    state_on  = {f: False for f in note_pool_order}
    smooth_v  = {}   # slow EMA of normalised flex — tracks position
    fast_v    = {}   # fast EMA of normalised flex — tracks rapid changes
    note_on_t = {f: 0.0 for f in note_pool_order}  # timestamp of last note-on
    _smooth_pitch = [0.0]   # EMA of Madgwick pitch for octave selection; list so lambda can write it
    PITCH_SMOOTH  = 0.05    # EMA alpha — slow enough to ignore quick wrist flicks
    # CC27 added for continuous thumb pressure; CC28 for Madgwick pitch
    last_cc = {25: -1, 26: -1, 1: -1, 74: -1, 64: -1, 10: -1, 11: -1,
               thumb_cc_num: -1, PITCH_CC: -1}

    # Madgwick filter instance — one per run, persists across frames
    ahrs = MadgwickAHRS(beta=0.05, freq=50.0)
    _ahrs_last_t = [time.time()]   # mutable container so inner closure can update it

    chord_channel = 1  # MIDI channel 2
    melody_channel = 0  # MIDI channel 1

    def thumb_allows_play(thumb_val):
        if args.thumb_mode == "off":
            return True
        if thumb_val is None:
            return False
        if args.thumb_mode == "below":
            return thumb_val < args.thumb_threshold
        return thumb_val > args.thumb_threshold

    def release_melody():
        for f in note_pool_order:
            if notes_on[f] is not None:
                midi_out.send(mido.Message("note_off", note=notes_on[f], velocity=0, channel=melody_channel))
                notes_on[f] = None
                state_on[f] = False

    def chord_thread_fn():
        nonlocal chord_notes_on
        while not stop_event.is_set():
            chord_changed.wait(0.25)
            if stop_event.is_set():
                break
            if not chord_changed.is_set():
                continue
            chord_changed.clear()

            with chord_lock:
                src = list(notes)

            for n in chord_notes_on:
                midi_out.send(mido.Message("note_off", note=n, velocity=0, channel=chord_channel))
            chord_notes_on = []

            pcs = sorted(set(n % 12 for n in src))
            voiced = []
            for i, pc in enumerate(pcs):
                note = clamp_midi(48 + pc)
                vel = clamp_midi(70 - i * 4 + random.randint(-8, 8))
                midi_out.send(mido.Message("note_on", note=note, velocity=max(40, vel), channel=chord_channel))
                voiced.append(note)
                if i < len(pcs) - 1:
                    time.sleep(random.uniform(0.01, 0.04))
            chord_notes_on = voiced

    chord_worker = threading.Thread(target=chord_thread_fn, daemon=True)
    chord_worker.start()
    chord_changed.set()

    print("Hybrid controller active")
    print(f"Serial/BT:  {args.port} @ {args.baud}")
    print(f"MIDI out:   {midi_out.port_name}")
    print(f"Camera:     index {args.camera_index}")
    print(f"Thumb CC:   CC{thumb_cc_num}  (ADC {thumb_adc_min}–{thumb_adc_max} → 0–127)")
    print(f"Pitch CC:   CC{PITCH_CC}  (Madgwick {PITCH_DEG_MIN:.0f}°–{PITCH_DEG_MAX:.0f}° → 0–127)")
    print("Q to quit  |  C to recalibrate IMU zero reference")
    print("Finger bends -> note on/off (Ch1)  |  Hall -> CC64  |  IMU ay/gz -> CC10/CC11")
    print(f"Thumb ADC -> CC{thumb_cc_num} (continuous, always active)")
    print(f"[chord] {chord_name} notes={notes}")

    frame_counter = 0
    camera_bends = {f: 0.0 for f in note_pool_order}
    hand_x, hand_y = 0.5, 0.5
    hand_detected = False

    with HandLandmarker.create_from_options(options) as landmarker:
        camera_failures = 0
        while True:
            now = time.time()
            if now >= next_chord_change:
                release_melody()
                if args.chord_source == "library":
                    nxt = chord_player.next()
                    new_notes, new_name = list(nxt.notes), nxt.name
                else:
                    new_notes, new_name = generate_next_chord_midi(notes, args.key_root)
                with chord_lock:
                    notes = new_notes
                    chord_name = new_name
                print(f"[chord] {chord_name} notes={notes}")
                chord_changed.set()
                next_chord_change = now + random.uniform(args.chord_min, args.chord_max)

            # --- Camera frame ---
            if camera_enabled:
                cap.grab()
                ret, frame = cap.retrieve()
                if not ret:
                    camera_failures += 1
                    if camera_failures > 25:
                        print("Camera read failed repeatedly. Switching to flex-only mode.")
                        camera_enabled = False
                        hand_detected = False
                    frame = np.zeros((480, 640, 3), dtype=np.uint8)
                else:
                    camera_failures = 0
                    frame = cv2.flip(frame, 1)
            else:
                frame = np.zeros((480, 640, 3), dtype=np.uint8)

            frame_counter += 1
            if camera_enabled and frame_counter % 2 == 0:
                rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                result = landmarker.detect(mp_image)
                if result.hand_landmarks:
                    hand_detected = True
                    lm = result.hand_landmarks[0]
                    camera_bends = get_camera_bends(lm)
                    hand_x, hand_y = get_hand_position(lm)
                    h, w, _ = frame.shape
                    for p in lm:
                        cx, cy = int(p.x * w), int(p.y * h)
                        cv2.circle(frame, (cx, cy), 3, (0, 255, 0), -1)
                else:
                    hand_detected = False
                    camera_bends = {f: 0.0 for f in note_pool_order}

            # --- Flex snapshot ---
            flex, flex_ts, connected, serial_err = reader.snapshot()
            flex_fresh = (now - flex_ts) < 1.0

            # --- EMA updates (slow + fast) for velocity detection ---
            for f in note_pool_order:
                raw_norm = normalize_flex_cal(f, flex[f])
                if f not in smooth_v:
                    smooth_v[f] = raw_norm
                    fast_v[f]   = raw_norm
                smooth_v[f] = (1.0 - SMOOTH_ALPHA) * smooth_v[f] + SMOOTH_ALPHA * raw_norm
                fast_v[f]   = (1.0 - VEL_ALPHA)    * fast_v[f]   + VEL_ALPHA   * raw_norm

            # ----------------------------------------------------------------
            # Thumb CC — sent unconditionally every loop (independent of
            # thumb_mode gate) so the knob always tracks the physical sensor.
            # ----------------------------------------------------------------
            thumb_raw = flex.get("thumb")
            cc_thumb = _thumb_adc_to_cc(thumb_raw)
            if abs(cc_thumb - last_cc[thumb_cc_num]) > THUMB_CC_DEAD:
                midi_out.send(mido.Message(
                    "control_change",
                    control=thumb_cc_num,
                    value=cc_thumb,
                    channel=melody_channel,
                ))
                last_cc[thumb_cc_num] = cc_thumb

            # ----------------------------------------------------------------
            # Madgwick pitch -> CC28 — always active, independent of gate
            # ----------------------------------------------------------------
            _now_ahrs = time.time()
            _dt_ahrs  = max(0.005, min(_now_ahrs - _ahrs_last_t[0], 0.1))
            _ahrs_last_t[0] = _now_ahrs
            ahrs.dt = _dt_ahrs

            _ax = float(flex.get("ax", 0.0))
            _ay = float(flex.get("ay", 0.0))
            _az = float(flex.get("az", 0.0))
            _gx = float(flex.get("gx", 0.0))
            _gy = float(flex.get("gy", 0.0))
            _gz = float(flex.get("gz", 0.0))

            # Zero-rate gyro deadband — matches imu_visualiser.py
            GYRO_DEAD = 0.5
            _gx = 0.0 if abs(_gx) < GYRO_DEAD else _gx
            _gy = 0.0 if abs(_gy) < GYRO_DEAD else _gy
            _gz = 0.0 if abs(_gz) < GYRO_DEAD else _gz

            if flex_fresh:
                ahrs.update(_gx, _gy, _gz, _ax, _ay, _az)

            _pitch_deg = ahrs.pitch_deg()
            # Smooth pitch for octave selection — filters out fast wrist twitches
            _smooth_pitch[0] = (1.0 - PITCH_SMOOTH) * _smooth_pitch[0] + PITCH_SMOOTH * _pitch_deg
            _pitch_span = PITCH_DEG_MAX - PITCH_DEG_MIN
            cc_pitch = clamp_midi(((_pitch_deg - PITCH_DEG_MIN) / _pitch_span) * 127.0)
            if abs(cc_pitch - last_cc[PITCH_CC]) > PITCH_CC_DEAD:
                midi_out.send(mido.Message(
                    "control_change",
                    control=PITCH_CC,
                    value=cc_pitch,
                    channel=melody_channel,
                ))
                last_cc[PITCH_CC] = cc_pitch

            # ----------------------------------------------------------------
            # Velocity-based note triggers
            # fast_v - smooth_v = instantaneous bend velocity (positive = closing)
            # ----------------------------------------------------------------
            if flex_fresh and thumb_allows_play(flex["thumb"]):
                with chord_lock:
                    pool = sorted(set(notes))

                for idx, f in enumerate(note_pool_order):
                    cam_norm        = camera_bends.get(f, 0.0) if hand_detected else 0.0
                    # Blend camera and glove velocity signals
                    vel_signal      = (FLEX_WEIGHT * (fast_v[f] - smooth_v[f])
                                       + CAM_WEIGHT * cam_norm * 0.3)

                    # Auto-release: drop note if held longer than NOTE_MAX_AGE
                    if state_on[f] and (now - note_on_t[f]) > NOTE_MAX_AGE:
                        state_on[f] = False
                        if notes_on[f] is not None:
                            midi_out.send(mido.Message("note_off", note=notes_on[f],
                                                       velocity=0, channel=melody_channel))
                            notes_on[f] = None

                    # Note-on: positive spike above threshold (finger closing fast)
                    if (not state_on[f]) and vel_signal > VEL_ON_THRESH:
                        state_on[f]  = True
                        note_on_t[f] = now
                        octave       = pitch_to_octave(_smooth_pitch[0])
                        base_note    = pool[idx % len(pool)]
                        note         = clamp_midi(base_note + (12 * octave))
                        vel_norm     = (vel_signal - VEL_VEL_MIN) / max(VEL_VEL_MAX - VEL_VEL_MIN, 1e-6)
                        vel          = clamp_midi(40 + int(87 * max(0.0, min(1.0, vel_norm))))
                        notes_on[f]  = note
                        midi_out.send(mido.Message("note_on", note=note, velocity=vel,
                                                   channel=melody_channel))

                    # Note-off: negative spike (finger opening fast)
                    elif state_on[f] and vel_signal < -VEL_OFF_THRESH:
                        state_on[f] = False
                        if notes_on[f] is not None:
                            midi_out.send(mido.Message("note_off", note=notes_on[f],
                                                       velocity=0, channel=melody_channel))
                            notes_on[f] = None

                # CC from camera XY position
                if hand_detected:
                    cc_x = clamp_midi(hand_x * 127)
                    cc_y = clamp_midi((1.0 - hand_y) * 127)
                    if abs(cc_x - last_cc[25]) > 1:
                        midi_out.send(mido.Message("control_change", control=25, value=cc_x, channel=melody_channel))
                        last_cc[25] = cc_x
                    if abs(cc_y - last_cc[26]) > 1:
                        midi_out.send(mido.Message("control_change", control=26, value=cc_y, channel=melody_channel))
                        last_cc[26] = cc_y

                # CC from flex sensors and IMU
                cc_mod = clamp_midi(normalize_flex_cal("pointer", smooth_v["pointer"]) * 127)
                cc_bri = clamp_midi(normalize_flex_cal("ring", smooth_v["ring"]) * 127)
                cc_sus = 127 if int(flex.get("hall", 0)) == 1 else 0
                cc_pan = clamp_midi(((float(flex.get("ay", 0.0)) + 10.0) / 20.0) * 127.0)
                cc_exp = clamp_midi(((float(flex.get("gz", 0.0)) + 250.0) / 500.0) * 127.0)
                if abs(cc_mod - last_cc[1]) > 1:
                    midi_out.send(mido.Message("control_change", control=1, value=cc_mod, channel=melody_channel))
                    last_cc[1] = cc_mod
                if abs(cc_bri - last_cc[74]) > 1:
                    midi_out.send(mido.Message("control_change", control=74, value=cc_bri, channel=melody_channel))
                    last_cc[74] = cc_bri
                if cc_sus != last_cc[64]:
                    midi_out.send(mido.Message("control_change", control=64, value=cc_sus, channel=melody_channel))
                    last_cc[64] = cc_sus
                if abs(cc_pan - last_cc[10]) > 1:
                    midi_out.send(mido.Message("control_change", control=10, value=cc_pan, channel=melody_channel))
                    last_cc[10] = cc_pan
                if abs(cc_exp - last_cc[11]) > 1:
                    midi_out.send(mido.Message("control_change", control=11, value=cc_exp, channel=melody_channel))
                    last_cc[11] = cc_exp
            else:
                release_melody()

            # --- HUD overlay ---
            y = 25
            for f in note_pool_order:
                vel_sig = (fast_v.get(f, 0.0) - smooth_v.get(f, 0.0)) * FLEX_WEIGHT
                state_str = "ON " if state_on[f] else "---"
                text = (f"{f[:3]} slow:{smooth_v.get(f,0):.2f} fast:{fast_v.get(f,0):.2f} "
                        f"vel:{vel_sig:+.3f} {state_str}")
                y += 22

            status = "BT OK" if connected and flex_fresh else "BT WAIT"
            if serial_err:
                status = f"BT ERR: {serial_err[:40]}"

            imu_text = (
                f"ay:{float(flex.get('ay', 0.0)):+.2f} gz:{float(flex.get('gz', 0.0)):+.2f} "
                f"pitch:{_pitch_deg:+.1f}° (oct~{(_smooth_pitch[0]/90.0*2.0):+.1f}) CC{PITCH_CC}:{last_cc[PITCH_CC]}"
            )
            hall_text = f"h:{flex.get('hall', 0)}({flex.get('hall1', 0)},{flex.get('hall2', 0)},{flex.get('hall3', 0)})"
            thumb_cc_display = last_cc[thumb_cc_num] if last_cc[thumb_cc_num] >= 0 else 0
            cv2.putText(
                frame,
                f"{status} thumb:{flex['thumb']} CC{thumb_cc_num}:{thumb_cc_display} {hall_text} {imu_text} mode:{args.thumb_mode}",
                (10, y + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 220, 120), 2,
            )
            mode_text = "fusion" if camera_enabled else "flex-only"
            cv2.putText(frame, f"Chord: {chord_name}",
                        (10, frame.shape[0] - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 180, 0), 2)
            cv2.putText(frame, f"Ch1 melody ({mode_text}) | Ch2 pad  ->  {midi_out.port_name}  [C=recal IMU]",
                        (10, frame.shape[0] - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (150, 200, 255), 1)

            cv2.imshow("Hybrid Glove + Camera MIDI", frame)
            key = cv2.waitKey(1) & 0xFF
            if key == ord("q"):
                break
            elif key == ord("c"):
                # Re-zero IMU relative to current pose; also reset Madgwick so
                # the filter starts fresh from the new reference orientation.
                reader.recalibrate()
                ahrs.reset()
                print("[IMU] Madgwick filter reset.")

    stop_event.set()
    chord_changed.set()

    release_melody()
    for n in chord_notes_on:
        midi_out.send(mido.Message("note_off", note=n, velocity=0, channel=chord_channel))
    midi_out.close()

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()