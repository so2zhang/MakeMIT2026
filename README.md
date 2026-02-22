# GesturePlay
python3 /Users/patliu/Desktop/Coding/MakeMIT2026/hand_tracking.py --port /dev/cu.usbserial-0001 --camera-index 0

A hand-tracking piano that plays notes based on finger gestures. Built with MediaPipe and OpenCV for MakeMIT 2026.

## Setup

1. Create a virtual environment and activate it:
   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Run the app (the MediaPipe hand model downloads automatically on first run):
   ```bash
   python3 hand_tracking.py
   ```

4. Show your hand to the camera and use finger gestures to play notes. Press **Q** to quit.

## How it works

- **MediaPipe** detects 21 hand landmarks in real time
- Finger curl detection determines which fingers are "up" or "down"
- Different finger combinations map to different musical notes (C through C3)
- **Pygame** generates and plays the corresponding tones
