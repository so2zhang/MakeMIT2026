print("script started")
import cv2
import mediapipe as mp
import numpy as np
import urllib.request
import os
import mido
import random
import time
import threading
from collections import deque

# =============================================================================
# CHORD GENERATION (from chord_markov.py)
# =============================================================================

def midi_to_pc(midi_notes):
    return sorted(set([n % 12 for n in midi_notes]))

CHORD_TEMPLATES = {
    "maj7": [0, 4, 7, 11],
    "min7": [0, 3, 7, 10],
    "7":    [0, 4, 7, 10],
    "maj":  [0, 4, 7],
    "min":  [0, 3, 7],
    "sus2": [0, 2, 7],
    "sus4": [0, 5, 7],
    "add9": [0, 4, 7, 2],
    "dim":  [0, 3, 6],
}

def detect_chord(midi_notes):
    pcs = midi_to_pc(midi_notes)
    for root in pcs:
        intervals = sorted([(p - root) % 12 for p in pcs])
        for name, template in CHORD_TEMPLATES.items():
            if set(template).issubset(intervals):
                return root, name
    return pcs[0], "maj"

MAJOR_SCALE = [0, 2, 4, 5, 7, 9, 11]
DEGREE_MAP  = {"I": 0, "ii": 1, "iii": 2, "IV": 3, "V": 4, "vi": 5, "vii": 6}

MARKOV_RELATIVE_CLOSED = {
    "Imaj7":  {"vim7": 0.25, "IVmaj7": 0.25, "iiim7": 0.20, "Vsus2": 0.15, "Iadd9": 0.15},
    "vim7":   {"IVmaj7": 0.30, "Imaj7": 0.25, "iiim7": 0.20, "iim7": 0.15, "vim9": 0.10},
    "IVmaj7": {"Imaj7": 0.30, "Vsus2": 0.20, "vim7": 0.20, "iiim7": 0.15, "IVadd9": 0.15},
    "iiim7":  {"vim7": 0.30, "IVmaj7": 0.25, "Imaj7": 0.20, "iim7": 0.15, "Vsus2": 0.10},
    "iim7":   {"IVmaj7": 0.30, "vim7": 0.25, "Imaj7": 0.20, "Vsus2": 0.15, "iiadd9": 0.10},
    "Vsus2":  {"Imaj7": 0.35, "vim7": 0.25, "IVmaj7": 0.20, "Vsus4": 0.10, "Vadd9": 0.10},
    "Iadd9":  {"vim7": 0.30, "IVmaj7": 0.25, "Vsus2": 0.25, "iiim7": 0.20},
    "vim9":   {"Imaj7": 0.30, "IVmaj7": 0.25, "Vsus2": 0.25, "iiim7": 0.20},
    "IVadd9": {"Imaj7": 0.30, "vim7": 0.25, "Vsus2": 0.25, "iiim7": 0.20},
    "Vsus4":  {"Imaj7": 0.35, "vim7": 0.25, "IVmaj7": 0.20, "Vsus2": 0.20},
    "Vadd9":  {"Imaj7": 0.35, "vim7": 0.25, "IVmaj7": 0.20, "Vsus2": 0.20},
    "iiadd9": {"IVmaj7": 0.40, "vim7": 0.30, "Imaj7": 0.30},
}

CHORD_TO_TEMPLATE = {
    "maj7": [0, 4, 7, 11],
    "min7": [0, 3, 7, 10],
    "7":    [0, 4, 7, 10],
    "maj":  [0, 4, 7],
    "min":  [0, 3, 7],
    "sus2": [0, 2, 7],
    "sus4": [0, 5, 7],
    "add9": [0, 4, 7, 2],
    "dim":  [0, 3, 6],
}

def relative_to_midi(key_root, relative_chord):
    degree_str = None
    chord_type = None
    for deg in DEGREE_MAP.keys():
        if relative_chord.startswith(deg):
            degree_str = deg
            chord_type = relative_chord[len(deg):]
            break
    if degree_str is None:
        degree_str = "I"
        chord_type = "maj"
    if chord_type == "m7":
        chord_type = "min7"
    elif chord_type == "m":
        chord_type = "min"
    elif chord_type in ("9", "m9"):
        chord_type = "maj7" if "m" not in chord_type else "min7"
    elif chord_type == "":
        chord_type = "maj"

    degree_index = DEGREE_MAP[degree_str]
    root_pc = (key_root + MAJOR_SCALE[degree_index])
    template = CHORD_TO_TEMPLATE.get(chord_type, [0, 4, 7])
    return [(root_pc + interval) for interval in template]

def next_relative_chord(current, last=None):
    choices = dict(MARKOV_RELATIVE_CLOSED.get(current, {}))
    if not choices:
        return random.choice(list(MARKOV_RELATIVE_CLOSED.keys()))
    if last in choices:
        del choices[last]
    chords = list(choices.keys())
    probs  = list(choices.values())
    return random.choices(chords, weights=probs)[0]

def generate_next_chord_midi(current_midi_notes, key_root_pc):
    root, chord_type = detect_chord(current_midi_notes)
    semitone_diff = (root - key_root_pc) % 12
    degree_index  = MAJOR_SCALE.index(semitone_diff) if semitone_diff in MAJOR_SCALE else 0
    degree_name   = list(DEGREE_MAP.keys())[list(DEGREE_MAP.values()).index(degree_index)]
    relative_chord = degree_name + chord_type
    next_rel_chord = next_relative_chord(relative_chord)
    next_chord_midi = relative_to_midi(key_root_pc, next_rel_chord)
    return next_chord_midi, next_rel_chord


# =============================================================================
# MIDI SETUP
# =============================================================================

midi_out = mido.open_output("GestureHand MIDI", virtual=True)

KEY_ROOT   = 60   # C4
NOTES      = [60, 64, 67, 71, 74]   # Cmaj9
chord_name = "Imaj7"

notes_on = {finger: None for finger in ["thumb", "index", "middle", "ring", "pinky"]}

BEND_THRESHOLD = 0.5

HAND_CC      = {"x": 25, "y": 26}
last_hand_cc = {"x": -1, "y": -1}

next_chord_change_time = time.time() + random.uniform(5, 15)

# Thread-safe state
state_lock   = threading.Lock()
latest_frame = None           # most recent decoded frame for display
latest_result = None          # most recent landmark result
frame_counter = 0             # for skipping frames

# =============================================================================
# CHORD PLAYBACK THREAD  (channel 2, MIDI ch index 1)
# =============================================================================

CHORD_CHANNEL   = 1          # 0-indexed → MIDI channel 2
CHORD_OCTAVE    = 48         # C3 base — keeps chord pad low and out of the way
chord_notes_on  = []         # notes currently held on ch2
chord_changed   = threading.Event()

def chord_pad_thread():
    """Holds the current chord voicing on MIDI channel 2 and re-voices on change."""
    global chord_notes_on
    while True:
        chord_changed.wait()          # block until a chord change fires
        chord_changed.clear()

        with state_lock:
            new_notes = list(NOTES)

        # Release old chord notes
        for n in chord_notes_on:
            midi_out.send(mido.Message('note_off', note=n, channel=CHORD_CHANNEL, velocity=0))
        chord_notes_on = []

        # Voice new chord with humanized stagger (random delay between each note)
        voiced = []
        pcs = sorted(set(n % 12 for n in new_notes))
        for i, pc in enumerate(pcs):
            midi_note = CHORD_OCTAVE + pc
            # Velocity: slight random variation around a tapering base
            base_vel  = 70 - i * 4
            velocity  = int(np.clip(base_vel + random.randint(-8, 8), 40, 100))
            midi_out.send(mido.Message('note_on', note=midi_note, channel=CHORD_CHANNEL, velocity=velocity))
            voiced.append(midi_note)
            # Stagger: first note lands immediately, subsequent notes follow with
            # a small random gap (10–40 ms) so the chord rolls in organically.
            if i < len(pcs) - 1:
                time.sleep(random.uniform(0.010, 0.040))
        chord_notes_on = voiced

pad_thread = threading.Thread(target=chord_pad_thread, daemon=True)
pad_thread.start()

# Signal the initial chord
chord_changed.set()


# =============================================================================
# FINGER / HAND HELPERS
# =============================================================================

def estimate_bend(landmarks, mcp, pip, dip, tip):
    a  = np.array([landmarks[mcp].x, landmarks[mcp].y])
    b  = np.array([landmarks[pip].x, landmarks[pip].y])
    c  = np.array([landmarks[dip].x, landmarks[dip].y])
    ba = a - b
    bc = c - b
    cosine = np.dot(ba, bc) / (np.linalg.norm(ba) * np.linalg.norm(bc) + 1e-6)
    angle  = np.degrees(np.arccos(np.clip(cosine, -1, 1)))
    return 1 - (angle / 180)

def get_finger_bends(landmarks):
    return {
        "thumb":  estimate_bend(landmarks, 1,  2,  3,  4),
        "index":  estimate_bend(landmarks, 5,  6,  7,  8),
        "middle": estimate_bend(landmarks, 9,  10, 11, 12),
        "ring":   estimate_bend(landmarks, 13, 14, 15, 16),
        "pinky":  estimate_bend(landmarks, 17, 18, 19, 20),
    }

def get_hand_position(landmarks):
    xs = [lm.x for lm in landmarks]
    ys = [lm.y for lm in landmarks]
    return np.mean(xs), np.mean(ys)


# =============================================================================
# CHORD CHANGE HELPER
# =============================================================================

def do_chord_change():
    global NOTES, chord_name, next_chord_change_time

    for finger in list(notes_on.keys()):
        if notes_on[finger] is not None:
            midi_out.send(mido.Message('note_off', note=notes_on[finger], velocity=0))
            notes_on[finger] = None

    new_midi, new_name = generate_next_chord_midi(NOTES, KEY_ROOT)

    with state_lock:
        NOTES      = new_midi
        chord_name = new_name

    print(f"[chord change] → {chord_name}  MIDI: {NOTES}")
    chord_changed.set()          # wake pad thread to re-voice

    next_chord_change_time = time.time() + random.uniform(5, 15)


# =============================================================================
# MEDIAPIPE SETUP  — IMAGE mode is used but we skip alternate frames to cut lag
# =============================================================================

if not os.path.exists("hand_landmarker.task"):
    print("Downloading model...")
    urllib.request.urlretrieve(
        "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
        "hand_landmarker.task"
    )
    print("Model downloaded!")

BaseOptions           = mp.tasks.BaseOptions
HandLandmarker        = mp.tasks.vision.HandLandmarker
HandLandmarkerOptions = mp.tasks.vision.HandLandmarkerOptions
VisionRunningMode     = mp.tasks.vision.RunningMode

options = HandLandmarkerOptions(
    base_options=BaseOptions(model_asset_path="hand_landmarker.task"),
    running_mode=VisionRunningMode.IMAGE,
    num_hands=1,
)

# Reduce camera buffer to 1 so we always grab the freshest frame
cap = cv2.VideoCapture(0)
cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

# Optional: lower resolution for speed (comment out if quality matters)
cap.set(cv2.CAP_PROP_FRAME_WIDTH,  640)
cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

print("Camera opened — Gesture Chord MIDI controller active!")
print("  Ch1: finger notes  |  Ch2: chord pad voicing")


# =============================================================================
# MAIN LOOP
# =============================================================================

SKIP_FRAMES = 1   # run inference every N+1 frames (0 = every frame, 1 = every other)
result       = None   # cached landmark result

with HandLandmarker.create_from_options(options) as landmarker:
    while True:
        # ── Chord-change timer ──────────────────────────────────────────────
        if time.time() >= next_chord_change_time:
            do_chord_change()

        # ── Grab the freshest frame (drain buffer quickly) ──────────────────
        cap.grab()   # discard buffered frame
        ret, frame = cap.retrieve()
        if not ret:
            break

        frame = cv2.flip(frame, 1)

        # ── Run MediaPipe only on selected frames ───────────────────────────
        frame_counter += 1
        if frame_counter % (SKIP_FRAMES + 1) == 0:
            rgb    = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_img = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = landmarker.detect(mp_img)

        secs_left = max(0, next_chord_change_time - time.time())

        if result and result.hand_landmarks:
            for hand_landmarks in result.hand_landmarks:
                h, w, _ = frame.shape

                for lm in hand_landmarks:
                    cx, cy = int(lm.x * w), int(lm.y * h)
                    cv2.circle(frame, (cx, cy), 5, (0, 255, 0), -1)

                hand_x, hand_y  = get_hand_position(hand_landmarks)
                octave_offset    = int(hand_y * 6) * 12 - 3 * 12

                # ── Finger note triggering ──────────────────────────────────
                bends = get_finger_bends(hand_landmarks)
                for finger, bend in bends.items():
                    if bend > BEND_THRESHOLD and notes_on[finger] is None:
                        with state_lock:
                            note_pool = list(NOTES)
                        note     = random.choice(note_pool) + octave_offset
                        note     = max(0, min(127, note))
                        velocity = random.randint(72, 127)
                        # Mark slot as taken immediately so the next frame
                        # doesn't re-trigger while the delay is pending.
                        notes_on[finger] = note
                        # Fire note_on after a tiny humanizing delay (0–30 ms).
                        # Re-check notes_on[finger] before sending so that if the
                        # hand disappears during the delay the note is suppressed.
                        delay = random.uniform(0.0, 0.030)
                        def _send_on(n=note, v=velocity, d=delay, f=finger):
                            time.sleep(d)
                            if notes_on[f] == n:   # still wanted
                                midi_out.send(mido.Message('note_on', note=n, velocity=v))
                        threading.Thread(target=_send_on, daemon=True).start()

                    elif bend <= BEND_THRESHOLD and notes_on[finger] is not None:
                        midi_out.send(mido.Message('note_off', note=notes_on[finger], velocity=0))
                        notes_on[finger] = None

                # ── Hand-position CCs ───────────────────────────────────────
                cc_x = int(np.clip(hand_x * 127, 0, 127))
                cc_y = int(np.clip((1 - hand_y) * 127, 0, 127))
                if abs(cc_x - last_hand_cc["x"]) > 1:
                    midi_out.send(mido.Message('control_change', control=HAND_CC["x"], value=cc_x))
                    last_hand_cc["x"] = cc_x
                if abs(cc_y - last_hand_cc["y"]) > 1:
                    midi_out.send(mido.Message('control_change', control=HAND_CC["y"], value=cc_y))
                    last_hand_cc["y"] = cc_y

                # ── HUD ─────────────────────────────────────────────────────
                y = 30
                for fname, bend in bends.items():
                    cv2.putText(frame, f"{fname}: {bend:.2f}", (10, y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
                    y += 25
                cv2.putText(frame, f"Hand X: {cc_x}", (10, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
                y += 25
                cv2.putText(frame, f"Hand Y: {cc_y}", (10, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)

        else:
            # Release any finger notes still held from the last detected frame
            for finger in list(notes_on.keys()):
                if notes_on[finger] is not None:
                    midi_out.send(mido.Message('note_off', note=notes_on[finger], velocity=0))
                    notes_on[finger] = None
            cv2.putText(frame, "No hand detected", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

        # Chord name + countdown + channel legend always visible
        with state_lock:
            display_chord = chord_name
        cv2.putText(frame, f"Chord: {display_chord}", (10, frame.shape[0] - 70),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 180, 0), 2)
        cv2.putText(frame, f"Next change: {secs_left:.1f}s", (10, frame.shape[0] - 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200, 200, 200), 2)
        cv2.putText(frame, "Ch1: fingers  Ch2: chord pad", (10, frame.shape[0] - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (150, 150, 255), 1)

        cv2.imshow("Gesture Chord MIDI Controller", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break


# =============================================================================
# CLEANUP
# =============================================================================

# Release finger notes (ch1)
for finger in notes_on:
    if notes_on[finger] is not None:
        midi_out.send(mido.Message('note_off', note=notes_on[finger], velocity=0))

# Release chord pad notes (ch2)
for n in chord_notes_on:
    midi_out.send(mido.Message('note_off', note=n, channel=CHORD_CHANNEL, velocity=0))

cap.release()
cv2.destroyAllWindows()