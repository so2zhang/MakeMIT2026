print("script started")
import cv2
import mediapipe as mp
import numpy as np
import urllib.request
import os
import mido
import random

# --- MIDI SETUP ---
midi_out = mido.open_output("GestureHand MIDI", virtual=True)

# --- Notes for Cmaj9 chord ---
CMAJ9_NOTES = [60, 64, 67, 71, 74]  # C4, E4, G4, B4, D5

# Track which note is currently on per finger
notes_on = {finger: None for finger in ["thumb", "index", "middle", "ring", "pinky"]}

# Bend threshold to trigger note
BEND_THRESHOLD = 0.5

# --- Hand position CCs ---
HAND_CC = {
    "x": 25,  # horizontal hand position
    "y": 26,  # vertical hand position
}
last_hand_cc = {"x": -1, "y": -1}

# --- FINGER BEND DETECTION ---
def estimate_bend(landmarks, mcp, pip, dip, tip):
    a = np.array([landmarks[mcp].x, landmarks[mcp].y])
    b = np.array([landmarks[pip].x, landmarks[pip].y])
    c = np.array([landmarks[dip].x, landmarks[dip].y])
    ba = a - b
    bc = c - b
    cosine = np.dot(ba, bc) / (np.linalg.norm(ba) * np.linalg.norm(bc) + 1e-6)
    angle = np.degrees(np.arccos(np.clip(cosine, -1, 1)))
    return 1 - (angle / 180)

def get_finger_bends(landmarks):
    return {
        "thumb":  estimate_bend(landmarks, 1,  2,  3,  4),
        "index":  estimate_bend(landmarks, 5,  6,  7,  8),
        "middle": estimate_bend(landmarks, 9,  10, 11, 12),
        "ring":   estimate_bend(landmarks, 13, 14, 15, 16),
        "pinky":  estimate_bend(landmarks, 17, 18, 19, 20),
    }

# --- HAND POSITION DETECTION ---
def get_hand_position(landmarks):
    xs = [lm.x for lm in landmarks]
    ys = [lm.y for lm in landmarks]
    return np.mean(xs), np.mean(ys)

# --- MEDIAPIPE SETUP ---
if not os.path.exists("hand_landmarker.task"):
    print("Downloading model...")
    urllib.request.urlretrieve(
        "https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/1/hand_landmarker.task",
        "hand_landmarker.task"
    )
    print("Model downloaded!")

BaseOptions = mp.tasks.BaseOptions
HandLandmarker = mp.tasks.vision.HandLandmarker
HandLandmarkerOptions = mp.tasks.vision.HandLandmarkerOptions
VisionRunningMode = mp.tasks.vision.RunningMode

options = HandLandmarkerOptions(
    base_options=BaseOptions(model_asset_path="hand_landmarker.task"),
    running_mode=VisionRunningMode.IMAGE,
    num_hands=1
)

cap = cv2.VideoCapture(0)
print("Camera opened — MIDI controller active!")

with HandLandmarker.create_from_options(options) as landmarker:
    while True:
        ret, frame = cap.read()
        if not ret:
            break

        frame = cv2.flip(frame, 1)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
        result = landmarker.detect(mp_image)

        if result.hand_landmarks:
            for hand_landmarks in result.hand_landmarks:
                h, w, _ = frame.shape

                # Draw landmarks
                for lm in hand_landmarks:
                    cx, cy = int(lm.x * w), int(lm.y * h)
                    cv2.circle(frame, (cx, cy), 5, (0, 255, 0), -1)

                # --- Hand position for octave scaling ---
                hand_x, hand_y = get_hand_position(hand_landmarks)
                octave_offset = int(hand_y * 4) * 12 - 12  # 0–2 octaves

                # --- Finger note triggering ---
                bends = get_finger_bends(hand_landmarks)
                for finger, bend in bends.items():
                    # Finger bent past threshold → trigger a random note if none is active
                    if bend > BEND_THRESHOLD and notes_on[finger] is None:
                        note = random.choice(CMAJ9_NOTES) + octave_offset
                        velocity = random.randint(80, 127)
                        msg = mido.Message('note_on', note=note, velocity=velocity)
                        midi_out.send(msg)
                        notes_on[finger] = note

                    # Finger straightened → release the note if one is active
                    elif bend <= BEND_THRESHOLD and notes_on[finger] is not None:
                        msg = mido.Message('note_off', note=notes_on[finger], velocity=0)
                        midi_out.send(msg)
                        notes_on[finger] = None

                # --- Hand position MIDI CCs ---
                cc_x = int(np.clip(hand_x * 127, 0, 127))
                cc_y = int(np.clip((1 - hand_y) * 127, 0, 127))
                if abs(cc_x - last_hand_cc["x"]) > 1:
                    msg = mido.Message('control_change', control=HAND_CC["x"], value=cc_x)
                    midi_out.send(msg)
                    last_hand_cc["x"] = cc_x
                if abs(cc_y - last_hand_cc["y"]) > 1:
                    msg = mido.Message('control_change', control=HAND_CC["y"], value=cc_y)
                    midi_out.send(msg)
                    last_hand_cc["y"] = cc_y

                # --- DISPLAY ---
                y = 30
                for fname, bend in bends.items():
                    text = f"{fname}: {bend:.2f}"
                    cv2.putText(frame, text, (10, y),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0, 255, 255), 2)
                    y += 25

                cv2.putText(frame, f"Hand X: {cc_x}", (10, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
                y += 25
                cv2.putText(frame, f"Hand Y: {cc_y}", (10, y),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)

        else:
            cv2.putText(frame, "No hand detected", (10, 30),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

        cv2.imshow("Gesture MIDI Controller", frame)
        if cv2.waitKey(1) & 0xFF == ord('q'):
            break

cap.release()
cv2.destroyAllWindows()