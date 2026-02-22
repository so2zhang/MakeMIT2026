#!/usr/bin/env python3
"""Hybrid glove + camera MIDI controller.

- Reads flex + thumb touch from ESP32 over serial/Bluetooth.
- Runs MediaPipe hand tracking from webcam.
- Fuses both to trigger notes and CC controls.
- Plays evolving Markov chord pad on MIDI channel 2.
"""

import argparse
import os
import random
import re
import threading
import time
import urllib.request

import cv2
import mediapipe as mp
import mido
import numpy as np
import serial
from chord_library import ChordSequencePlayer

if not hasattr(serial, "Serial"):
    raise SystemExit(
        "Wrong 'serial' package installed. Run:\n"
        "  python3 -m pip uninstall -y serial\n"
        "  python3 -m pip install pyserial"
    )


# --- Flex calibration from your measured values ---
FINGERS = ["pointer", "middle", "ring", "pinky"]
STRAIGHT_V = {"pointer": 2.85, "middle": 2.36, "ring": 2.22, "pinky": 2.59}
BENT_V = {"pointer": 3.13, "middle": 2.89, "ring": 2.75, "pinky": 3.11}
FLEX_THRESH = {f: (STRAIGHT_V[f] + BENT_V[f]) / 2.0 for f in FINGERS}

# Fused bend thresholds (0..1)
ON_THRESH = 0.62
OFF_THRESH = 0.45
FLEX_WEIGHT = 0.7
CAM_WEIGHT = 0.3
SMOOTH_ALPHA = 0.28

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


def normalize_flex(finger, voltage):
    lo = STRAIGHT_V[finger]
    hi = BENT_V[finger]
    if hi <= lo:
        return 0.0
    return max(0.0, min(1.0, (voltage - lo) / (hi - lo)))


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
                                self.values["ax"] = float(ax)
                                self.values["ay"] = float(ay)
                                self.values["az"] = float(az)
                                self.values["gx"] = float(gx)
                                self.values["gy"] = float(gy)
                                self.values["gz"] = float(gz)
                                self.last_update = time.time()
                            continue

                        gi = GLOVE_IMU_CSV_RE.match(line)
                        if gi:
                            p, m2, r, pk, t, h, ax, ay, az, gx, gy, gz = gi.groups()
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
                                self.values["ax"] = float(ax)
                                self.values["ay"] = float(ay)
                                self.values["az"] = float(az)
                                self.values["gx"] = float(gx)
                                self.values["gy"] = float(gy)
                                self.values["gz"] = float(gz)
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
    parser.add_argument("--midi-port", default="GestureHand MIDI")
    parser.add_argument("--camera-index", type=int, default=0, help="OpenCV camera index")
    parser.add_argument("--list-cameras", action="store_true", help="Probe and list available camera indexes")
    parser.add_argument("--key-root", type=int, default=60)
    parser.add_argument("--thumb-threshold", type=int, default=1440)
    parser.add_argument("--thumb-mode", choices=["below", "above", "off"], default="off")
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


def main():
    args = parse_args()
    if args.list_cameras:
        list_cameras()
        return
    if not args.port:
        raise SystemExit("Missing --port. Example: --port /dev/cu.usbserial-0001")

    midi_out = mido.open_output(args.midi_port, virtual=True)

    # Download model if missing
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
    notes_on = {f: None for f in note_pool_order}
    state_on = {f: False for f in note_pool_order}
    smooth_v = {}
    last_cc = {25: -1, 26: -1, 1: -1, 74: -1, 64: -1, 10: -1, 11: -1}

    chord_channel = 1  # MIDI channel 2
    melody_channel = 0

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
    print(f"Serial/BT: {args.port} @ {args.baud}")
    print(f"MIDI out: {args.midi_port}")
    print(f"Camera index: {args.camera_index}")
    print("Q to quit")
    print("Hall sensors -> CC64 (sustain), IMU -> CC10/CC11")
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

            # camera
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

            # flex snapshot
            flex, flex_ts, connected, serial_err = reader.snapshot()
            flex_fresh = (now - flex_ts) < 1.0

            # Smooth flex
            for f in note_pool_order:
                if f not in smooth_v:
                    smooth_v[f] = flex[f]
                smooth_v[f] = (1.0 - SMOOTH_ALPHA) * smooth_v[f] + SMOOTH_ALPHA * flex[f]

            if flex_fresh and thumb_allows_play(flex["thumb"]):
                with chord_lock:
                    pool = sorted(set(notes))

                for idx, f in enumerate(note_pool_order):
                    flex_norm = normalize_flex(f, smooth_v[f])
                    cam_norm = camera_bends.get(f, 0.0) if hand_detected else 0.0
                    fused = (FLEX_WEIGHT * flex_norm) + (CAM_WEIGHT * cam_norm)

                    if (not state_on[f]) and fused >= ON_THRESH:
                        state_on[f] = True
                        octave = int((1.0 - hand_y) * 3) - 1 if hand_detected else 0
                        base_note = pool[idx % len(pool)]
                        note = clamp_midi(base_note + (12 * octave))
                        vel = clamp_midi(80 + int(40 * fused) + random.randint(-8, 8))
                        notes_on[f] = note
                        midi_out.send(mido.Message("note_on", note=note, velocity=vel, channel=melody_channel))
                    elif state_on[f] and fused <= OFF_THRESH:
                        state_on[f] = False
                        if notes_on[f] is not None:
                            midi_out.send(mido.Message("note_off", note=notes_on[f], velocity=0, channel=melody_channel))
                            notes_on[f] = None

                # CC from camera (XY)
                if hand_detected:
                    cc_x = clamp_midi(hand_x * 127)
                    cc_y = clamp_midi((1.0 - hand_y) * 127)
                    if abs(cc_x - last_cc[25]) > 1:
                        midi_out.send(mido.Message("control_change", control=25, value=cc_x, channel=melody_channel))
                        last_cc[25] = cc_x
                    if abs(cc_y - last_cc[26]) > 1:
                        midi_out.send(mido.Message("control_change", control=26, value=cc_y, channel=melody_channel))
                        last_cc[26] = cc_y

                # CC from flex/combined
                cc_mod = clamp_midi(normalize_flex("pointer", smooth_v["pointer"]) * 127)
                cc_bri = clamp_midi(normalize_flex("ring", smooth_v["ring"]) * 127)
                cc_sus = 127 if int(flex.get("hall", 0)) == 1 else 0
                # Map IMU tilt/rotation to extra expression controls.
                cc_pan = clamp_midi(((float(flex.get("ay", 0.0)) + 10.0) / 20.0) * 127.0)   # CC10
                cc_exp = clamp_midi(((float(flex.get("gz", 0.0)) + 250.0) / 500.0) * 127.0) # CC11
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

            # HUD
            y = 25
            for f in note_pool_order:
                text = f"{f[:3]} {smooth_v[f]:.2f}V cam:{camera_bends.get(f, 0.0):.2f} {'ON' if state_on[f] else 'OFF'}"
                cv2.putText(frame, text, (10, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 255, 255), 2)
                y += 22

            status = "BT OK" if connected and flex_fresh else "BT WAIT"
            if serial_err:
                status = f"BT ERR: {serial_err[:40]}"

            imu_text = f"ay:{float(flex.get('ay', 0.0)):+.2f} gz:{float(flex.get('gz', 0.0)):+.2f}"
            hall_text = f"h:{flex.get('hall', 0)}({flex.get('hall1', 0)},{flex.get('hall2', 0)},{flex.get('hall3', 0)})"
            cv2.putText(frame, f"{status} thumb:{flex['thumb']} {hall_text} {imu_text} mode:{args.thumb_mode}", (10, y + 5), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 220, 120), 2)
            mode_text = "fusion" if camera_enabled else "flex-only"
            cv2.putText(frame, f"Chord: {chord_name}", (10, frame.shape[0] - 45), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 180, 0), 2)
            cv2.putText(frame, f"Ch1 melody ({mode_text}) | Ch2 pad", (10, frame.shape[0] - 15), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (150, 200, 255), 1)

            cv2.imshow("Hybrid Glove + Camera MIDI", frame)
            if cv2.waitKey(1) & 0xFF == ord("q"):
                break

    stop_event.set()
    chord_changed.set()

    release_melody()
    for n in chord_notes_on:
        midi_out.send(mido.Message("note_off", note=n, velocity=0, channel=chord_channel))

    cap.release()
    cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
