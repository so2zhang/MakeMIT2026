#!/usr/bin/env python3
"""
imu_visualizer.py
=================
Reads CSV IMU data from an ESP32 over Bluetooth Serial, applies a
Kalman filter to estimate orientation (roll/pitch/yaw) and position,
then renders a live 3-D axes display using Pygame + PyOpenGL.

INSTALL DEPENDENCIES
--------------------
    pip install pyserial numpy pygame PyOpenGL PyOpenGL_accelerate

USAGE
-----
    # Auto-discover the BT serial port:
    python imu_visualizer.py

    # Or specify the port explicitly:
    python imu_visualizer.py --port /dev/rfcomm0          # Linux
    python imu_visualizer.py --port COM7                  # Windows
    python imu_visualizer.py --port /dev/tty.ESP32_IMU-1  # macOS

    # Run without hardware (simulated motion):
    python imu_visualizer.py --demo

PAIRING (do once before running)
---------------------------------
    Linux:   bluetoothctl -> pair/connect "ESP32_IMU"
             then: sudo rfcomm bind 0 <MAC>   (gives /dev/rfcomm0)
    Windows: Pair via Settings -> Bluetooth; note the outgoing COM port
    macOS:   Pair via System Preferences; port appears in /dev/tty.*

DATA FORMAT EXPECTED FROM ESP32
---------------------------------
    timestamp_ms,ax_g,ay_g,az_g,gx_dps,gy_dps,gz_dps,temp_c

KALMAN FILTER DESIGN
---------------------
Orientation:  6-state KF  [roll, pitch, yaw, bias_gx, bias_gy, bias_gz]
              Gyro integrates angles in predict step; accelerometer
              corrects roll+pitch in update step. Yaw is gyro-only
              (no magnetometer available).

Position:     9-state KF  [px,py,pz, vx,vy,vz, abias_x,abias_y,abias_z]
              World-frame accel (gravity removed) double-integrated.
              NOTE: bare-IMU position drifts over time -- this is a
              fundamental limitation without GPS/vision correction.

CONTROLS
--------
    Left-drag    : orbit camera
    Scroll wheel : zoom in/out
    R            : reset Kalman filters and position trail
    ESC          : quit
"""

import sys
import time
import math
import argparse
import threading
import queue

import numpy as np
import serial
import serial.tools.list_ports

import pygame
from pygame.locals import DOUBLEBUF, OPENGL, RESIZABLE, QUIT, MOUSEBUTTONDOWN
from pygame.locals import MOUSEBUTTONUP, MOUSEMOTION, KEYDOWN, K_r, K_ESCAPE
from OpenGL.GL import *
from OpenGL.GLU import *

# ── Constants ────────────────────────────────────────────────────────────────

WINDOW_W    = 900
WINDOW_H    = 700
AXIS_LEN    = 1.2       # length of drawn body axes
TRAIL_LEN   = 500       # position history points
BT_BAUD     = 9600      # BT SPP is baud-agnostic at the RF level; this is for the driver
GRAVITY     = 9.80665   # m/s²

# ZUPT (Zero Velocity Update) detection thresholds
# Tune these if ZUPT triggers too eagerly or not enough:
#   Lower ZUPT_GYRO_THRESH  = only trigger when very still (less aggressive)
#   Lower ZUPT_ACCEL_THRESH = only trigger when very still
ZUPT_GYRO_THRESH  = 0.04   # rad/s  (~2.3 deg/s)  max gyro magnitude when stationary
ZUPT_ACCEL_THRESH = 0.04   # g      max deviation from 1g when stationary
ZUPT_WINDOW       = 5      # number of consecutive stationary samples before ZUPT fires

# ── Orientation Kalman Filter ────────────────────────────────────────────────

class OrientationKalman:
    """
    State: x = [roll, pitch, yaw, bias_gx, bias_gy, bias_gz]
    Predict: integrate gyro (bias-corrected) angles.
    Update:  correct roll/pitch from accelerometer.
    """

    def __init__(self):
        self.x = np.zeros(6)
        self.P = np.eye(6) * 0.1

        qAngle = 0.001
        qBias  = 0.003
        self.Q = np.diag([qAngle, qAngle, qAngle * 4,
                          qBias,  qBias,  qBias])
        self.R = np.eye(2) * 0.03

        # Measurement selects [roll, pitch]
        self.H = np.zeros((2, 6))
        self.H[0, 0] = 1.0
        self.H[1, 1] = 1.0

    def predict(self, gx_rads, gy_rads, gz_rads, dt):
        roll, pitch = self.x[0], self.x[1]
        gx = gx_rads - self.x[3]
        gy = gy_rads - self.x[4]
        gz = gz_rads - self.x[5]

        # Avoid division by zero near 90-degree pitch
        cos_p = math.cos(pitch)
        if abs(cos_p) < 1e-6:
            cos_p = 1e-6

        d_roll  = gx + (math.sin(roll) * math.tan(pitch) * gy +
                        math.cos(roll) * math.tan(pitch) * gz)
        d_pitch = math.cos(roll) * gy - math.sin(roll) * gz
        d_yaw   = (math.sin(roll) / cos_p) * gy + (math.cos(roll) / cos_p) * gz

        self.x[0] += d_roll  * dt
        self.x[1] += d_pitch * dt
        self.x[2] += d_yaw   * dt

        F = np.eye(6)
        F[0, 3] = -dt
        F[1, 4] = -dt
        F[2, 5] = -dt

        self.P = F @ self.P @ F.T + self.Q

    def update(self, ax, ay, az):
        norm = math.sqrt(ax*ax + ay*ay + az*az)
        if norm < 0.1:
            return
        ax /= norm; ay /= norm; az /= norm

        roll_meas  = math.atan2(ay, az)
        pitch_meas = -math.asin(max(-1.0, min(1.0, ax)))

        z = np.array([roll_meas, pitch_meas])
        y = z - self.H @ self.x
        S = self.H @ self.P @ self.H.T + self.R
        K = self.P @ self.H.T @ np.linalg.inv(S)
        self.x += K @ y
        self.P  = (np.eye(6) - K @ self.H) @ self.P

    @property
    def roll(self):  return self.x[0]
    @property
    def pitch(self): return self.x[1]
    @property
    def yaw(self):   return self.x[2]


# ── Position Kalman Filter ───────────────────────────────────────────────────

class PositionKalman:
    """
    State: [px,py,pz, vx,vy,vz, abias_x,abias_y,abias_z]
    Driven by world-frame linear acceleration (gravity already removed).

    ZUPT (Zero Velocity Update): when the device is detected as stationary,
    call zupt_update() to inject a zero-velocity measurement. This prevents
    bias errors from accumulating into runaway position drift.
    """

    def __init__(self):
        self.x = np.zeros(9)
        self.P = np.eye(9) * 0.01

        # Tight process noise so the filter doesn't extrapolate wildly
        self.Q = np.diag(
            [1e-6, 1e-6, 1e-6,    # position
             1e-4, 1e-4, 1e-4,    # velocity
             1e-7, 1e-7, 1e-7]    # accel bias random walk
        )

        # ZUPT: observe velocity states [3,4,5] = 0
        self.H_zupt = np.zeros((3, 9))
        self.H_zupt[0, 3] = 1.0
        self.H_zupt[1, 4] = 1.0
        self.H_zupt[2, 5] = 1.0
        self.R_zupt = np.eye(3) * 1e-4   # low = snap to zero firmly

    def predict(self, aw_x, aw_y, aw_z, dt):
        ax = aw_x - self.x[6]
        ay = aw_y - self.x[7]
        az = aw_z - self.x[8]

        F = np.eye(9)
        F[0,3]=dt; F[1,4]=dt; F[2,5]=dt
        F[3,6]=-dt; F[4,7]=-dt; F[5,8]=-dt

        B = np.zeros((9, 3))
        B[0,0]=0.5*dt**2; B[1,1]=0.5*dt**2; B[2,2]=0.5*dt**2
        B[3,0]=dt;        B[4,1]=dt;        B[5,2]=dt

        u = np.array([ax, ay, az])
        self.x = F @ self.x + B @ u
        self.P = F @ self.P @ F.T + self.Q

    def zupt_update(self):
        """Inject a zero-velocity observation. Call when device is stationary."""
        H = self.H_zupt
        y = -H @ self.x                          # innovation: measured 0 - predicted vel
        S = H @ self.P @ H.T + self.R_zupt
        K = self.P @ H.T @ np.linalg.inv(S)
        self.x += K @ y
        self.P  = (np.eye(9) - K @ H) @ self.P

    @property
    def position(self):
        return tuple(self.x[:3])

    @property
    def velocity(self):
        return tuple(self.x[3:6])


# ── Rotation ─────────────────────────────────────────────────────────────────

def euler_to_R(roll, pitch, yaw):
    """ZYX Euler -> 3x3 rotation matrix (body frame to world frame)."""
    cr, sr = math.cos(roll),  math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw),   math.sin(yaw)
    return np.array([
        [cy*cp,  cy*sp*sr - sy*cr,  cy*sp*cr + sy*sr],
        [sy*cp,  sy*sp*sr + cy*cr,  sy*sp*cr - cy*sr],
        [-sp,    cp*sr,             cp*cr            ],
    ])


def rotation_align(v_from, v_to):
    """
    Return the rotation matrix R such that R @ v_from aligns with v_to.
    Uses Rodrigues' rotation formula. Both vectors are normalised internally.
    """
    a = v_from / np.linalg.norm(v_from)
    b = v_to   / np.linalg.norm(v_to)
    cross = np.cross(a, b)
    dot   = np.dot(a, b)
    sin_a = np.linalg.norm(cross)
    if sin_a < 1e-8:
        # Already aligned (or anti-parallel)
        return np.eye(3) if dot > 0 else -np.eye(3)
    axis = cross / sin_a
    # Rodrigues
    K = np.array([[ 0,       -axis[2],  axis[1]],
                  [ axis[2],  0,       -axis[0]],
                  [-axis[1],  axis[0],  0      ]])
    return np.eye(3) + sin_a * K + (1 - dot) * K @ K


# ── Bluetooth serial reader thread ───────────────────────────────────────────

def find_bt_port():
    for p in serial.tools.list_ports.comports():
        desc = (p.description or "").lower()
        name = (p.name or "").lower()
        if any(kw in desc or kw in name
               for kw in ["bluetooth","rfcomm","esp32","imu","spp"]):
            return p.device
    return None


def serial_reader(port, baud, q, stop):
    ser = None
    while not stop.is_set():
        try:
            if ser is None:
                print(f"[BT] Connecting to {port} @ {baud} baud ...")
                ser = serial.Serial(port, baud, timeout=2)
                print("[BT] Connected.")
            line = ser.readline().decode("utf-8", errors="ignore").strip()
            if not line or line.startswith("timestamp") or line.startswith("="):
                continue
            parts = line.split(",")
            if len(parts) < 7:
                continue
            ts  = float(parts[0])
            ax  = float(parts[1]); ay = float(parts[2]); az = float(parts[3])
            gx  = float(parts[4]); gy = float(parts[5]); gz = float(parts[6])
            tmp = float(parts[7]) if len(parts) > 7 else 0.0
            q.put((ts, ax, ay, az,
                   math.radians(gx), math.radians(gy), math.radians(gz), tmp))
        except serial.SerialException as e:
            print(f"[BT] {e} -- retrying in 2 s ...")
            if ser: ser.close(); ser = None
            time.sleep(2)
        except (ValueError, IndexError):
            pass
    if ser: ser.close()


# ── OpenGL draw helpers ───────────────────────────────────────────────────────

def draw_grid(size=5, step=0.5):
    glColor4f(0.22, 0.22, 0.26, 0.7)
    glLineWidth(1.0)
    glBegin(GL_LINES)
    for i in np.arange(-size, size + step, step):
        glVertex3f(float(i), 0, float(-size))
        glVertex3f(float(i), 0, float( size))
        glVertex3f(float(-size), 0, float(i))
        glVertex3f(float( size), 0, float(i))
    glEnd()


def draw_world_axes(length=0.7):
    glLineWidth(1.2)
    glBegin(GL_LINES)
    glColor4f(0.6,0.15,0.15,0.5); glVertex3f(0,0,0); glVertex3f(length,0,0)
    glColor4f(0.15,0.6,0.15,0.5); glVertex3f(0,0,0); glVertex3f(0,length,0)
    glColor4f(0.15,0.15,0.6,0.5); glVertex3f(0,0,0); glVertex3f(0,0,length)
    glEnd()


def draw_body_axes(R, origin=(0.0, 0.0, 0.0), length=AXIS_LEN):
    ox, oy, oz = origin
    colors = [(1.0,0.25,0.25), (0.25,1.0,0.25), (0.35,0.55,1.0)]
    glLineWidth(3.5)
    for i, col in enumerate(colors):
        tip  = R[:, i] * length
        perp = R[:, (i+1) % 3] * 0.07
        glColor3f(*col)
        glBegin(GL_LINES)
        glVertex3f(ox, oy, oz)
        glVertex3f(ox + tip[0], oy + tip[1], oz + tip[2])
        glEnd()
        glBegin(GL_LINES)
        base = tip * 0.85
        glVertex3f(ox + tip[0], oy + tip[1], oz + tip[2])
        glVertex3f(ox + base[0] + perp[0], oy + base[1] + perp[1], oz + base[2] + perp[2])
        glVertex3f(ox + tip[0], oy + tip[1], oz + tip[2])
        glVertex3f(ox + base[0] - perp[0], oy + base[1] - perp[1], oz + base[2] - perp[2])
        glEnd()


def draw_board(R, origin=(0.0, 0.0, 0.0), sx=0.32, sy=0.07, sz=0.52):
    ox, oy, oz = origin
    vl = np.array([
        [-sx,-sy,-sz],[ sx,-sy,-sz],[ sx, sy,-sz],[-sx, sy,-sz],
        [-sx,-sy, sz],[ sx,-sy, sz],[ sx, sy, sz],[-sx, sy, sz],
    ])
    vw = (R @ vl.T).T + np.array([ox, oy, oz])
    faces = [(0,1,2,3),(4,5,6,7),(0,1,5,4),(2,3,7,6),(0,3,7,4),(1,2,6,5)]
    colors = [
        (0.18,0.52,0.18,0.88),(0.18,0.52,0.18,0.88),
        (0.16,0.44,0.16,0.78),(0.16,0.44,0.16,0.78),
        (0.13,0.38,0.13,0.78),(0.13,0.38,0.13,0.78),
    ]
    glEnable(GL_BLEND)
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
    glBegin(GL_QUADS)
    for face, c in zip(faces, colors):
        glColor4f(*c)
        for vi in face:
            glVertex3fv(vw[vi].tolist())
    glEnd()
    edges = [(0,1),(1,2),(2,3),(3,0),(4,5),(5,6),(6,7),(7,4),
             (0,4),(1,5),(2,6),(3,7)]
    glColor4f(0.45, 0.95, 0.45, 1.0)
    glLineWidth(1.0)
    glBegin(GL_LINES)
    for a, b in edges:
        glVertex3fv(vw[a].tolist())
        glVertex3fv(vw[b].tolist())
    glEnd()


def draw_trail(trail):
    if len(trail) < 2:
        return
    n = len(trail)
    glLineWidth(1.8)
    glBegin(GL_LINE_STRIP)
    for i, p in enumerate(trail):
        alpha = (i / n) * 0.9
        glColor4f(0.3, 0.75, 1.0, alpha)
        glVertex3f(*p)
    glEnd()


def upload_surface_as_quad(surf, x, y):
    """Upload a pygame RGBA surface as a textured OpenGL quad at screen coords."""
    data  = pygame.image.tostring(surf, "RGBA", True)
    tid   = glGenTextures(1)
    glBindTexture(GL_TEXTURE_2D, tid)
    glTexImage2D(GL_TEXTURE_2D, 0, GL_RGBA,
                 surf.get_width(), surf.get_height(),
                 0, GL_RGBA, GL_UNSIGNED_BYTE, data)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_LINEAR)
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_LINEAR)
    glEnable(GL_TEXTURE_2D)
    glEnable(GL_BLEND)
    glBlendFunc(GL_SRC_ALPHA, GL_ONE_MINUS_SRC_ALPHA)
    tw, th = surf.get_width(), surf.get_height()
    glColor4f(1, 1, 1, 1)
    # pygame.image.tostring(..., True) flips vertically, so V=0 is the bottom
    # of the surface. Map V=1 (top of texture) to the top screen edge of the quad.
    glBegin(GL_QUADS)
    glTexCoord2f(0, 1); glVertex2f(x,      y)
    glTexCoord2f(1, 1); glVertex2f(x + tw, y)
    glTexCoord2f(1, 0); glVertex2f(x + tw, y + th)
    glTexCoord2f(0, 0); glVertex2f(x,      y + th)
    glEnd()
    glDisable(GL_TEXTURE_2D)
    glDeleteTextures([tid])


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="ESP32 IMU BT Visualizer")
    parser.add_argument("--port",  default=None)
    parser.add_argument("--baud",  type=int, default=BT_BAUD)
    parser.add_argument("--demo",  action="store_true",
                        help="Simulate IMU data (no hardware needed)")
    args = parser.parse_args()

    port = args.port
    if not args.demo:
        if port is None:
            port = find_bt_port()
        if port is None:
            print("[ERROR] No Bluetooth serial port found.")
            print("  Pair ESP32 first, then use --port <device>.")
            print("  Or run with --demo to test without hardware.")
            sys.exit(1)

    data_q   = queue.Queue(maxsize=300)
    stop_evt = threading.Event()
    ori_kf   = OrientationKalman()
    pos_kf   = PositionKalman()

    prev_ts_ms  = None
    pos_trail   = []
    pos_display = (0.0, 0.0, 0.0)
    R_cur       = np.eye(3)
    roll_d = pitch_d = yaw_d = 0.0
    temp_c = 0.0
    hz_buf = []
    sample_hz = 0.0
    demo_t = 0.0

    # Gravity calibration: average the first N accel samples (body-frame g vector)
    CALIB_SAMPLES   = 50
    calib_acc       = []          # list of (ax,ay,az) in g during calibration
    g_body_ref      = None        # body-frame gravity reference (in m/s²), set after calib
    R_offset        = np.eye(3)   # corrects initial tilt so world-Y == up after calibration

    # ZUPT state
    zupt_count      = 0           # consecutive stationary sample counter
    is_stationary   = False       # for HUD display

    if not args.demo:
        t = threading.Thread(target=serial_reader,
                             args=(port, args.baud, data_q, stop_evt),
                             daemon=True)
        t.start()
    else:
        print("[DEMO] Simulated IMU data active.")

    # ── Pygame / OpenGL setup ───────────────────────────────────────────────
    pygame.init()
    screen = pygame.display.set_mode((WINDOW_W, WINDOW_H),
                                     DOUBLEBUF | OPENGL | RESIZABLE)
    pygame.display.set_caption("ESP32 IMU  |  Live 3-D Orientation")
    font = pygame.font.SysFont("monospace", 14)

    glEnable(GL_DEPTH_TEST)
    glEnable(GL_LINE_SMOOTH)
    glHint(GL_LINE_SMOOTH_HINT, GL_NICEST)

    def set_proj(w, h):
        glViewport(0, 0, w, h)
        glMatrixMode(GL_PROJECTION)
        glLoadIdentity()
        gluPerspective(50.0, w / max(1, h), 0.01, 100.0)
        glMatrixMode(GL_MODELVIEW)

    set_proj(WINDOW_W, WINDOW_H)

    cam_yaw   = 35.0
    cam_pitch = 22.0      # positive = looking down at the scene (was negative = upside-down)
    cam_dist  =  6.0      # start a bit further out so the trail has room
    dragging  = False
    last_m    = (0, 0)
    clock     = pygame.time.Clock()

    running = True
    while running:
        dt_frame = clock.tick(60) / 1000.0

        # ── Events ─────────────────────────────────────────────────────────
        for ev in pygame.event.get():
            if ev.type == QUIT:
                running = False
            elif ev.type == pygame.VIDEORESIZE:
                set_proj(ev.w, ev.h)
            elif ev.type == MOUSEBUTTONDOWN:
                if   ev.button == 1: dragging = True; last_m = ev.pos
                elif ev.button == 4: cam_dist  = max(1.5, cam_dist - 0.25)
                elif ev.button == 5: cam_dist  = min(20.0, cam_dist + 0.25)
            elif ev.type == MOUSEBUTTONUP:
                if ev.button == 1: dragging = False
            elif ev.type == MOUSEMOTION and dragging:
                dx, dy = ev.pos[0]-last_m[0], ev.pos[1]-last_m[1]
                cam_yaw   += dx * 0.45
                cam_pitch  = max(-89, min(89, cam_pitch + dy * 0.45))
                last_m = ev.pos
            elif ev.type == KEYDOWN:
                if ev.key == K_r:
                    ori_kf = OrientationKalman()
                    pos_kf = PositionKalman()
                    pos_trail.clear(); prev_ts_ms = None
                    hz_buf.clear(); R_cur = np.eye(3)
                    pos_display = (0.0, 0.0, 0.0)
                    calib_acc.clear(); g_body_ref = None
                    R_offset = np.eye(3)
                    zupt_count = 0; is_stationary = False
                    print("[RESET] Filters, trail, and gravity calibration cleared.")
                elif ev.key == K_ESCAPE:
                    running = False

        # ── Demo data injection ─────────────────────────────────────────────
        if args.demo:
            demo_t += dt_frame
            ts_ms = demo_t * 1000.0
            # Gentle oscillating tilt (produces visible rotation)
            ax =  math.sin(demo_t * 0.7)  * 0.18
            ay =  math.cos(demo_t * 0.5)  * 0.12
            az = -1.0 + math.sin(demo_t * 0.4) * 0.03
            gx = math.radians(25 * math.sin(demo_t * 1.1))
            gy = math.radians(18 * math.sin(demo_t * 0.85))
            gz = math.radians(12 * math.sin(demo_t * 0.55))
            # Add a small forward acceleration pulse so position actually moves
            # (simulates picking up and putting down the board)
            ax += 0.04 * math.sin(demo_t * 0.3)
            temp_c = 25.3
            data_q.put((ts_ms, ax, ay, az, gx, gy, gz, temp_c))

        # ── Consume queued samples ──────────────────────────────────────────
        processed = 0
        while not data_q.empty() and processed < 10:
            ts, ax, ay, az, gx, gy, gz, temp_c = data_q.get_nowait()
            processed += 1

            if prev_ts_ms is None:
                prev_ts_ms = ts; continue
            dt = (ts - prev_ts_ms) / 1000.0
            prev_ts_ms = ts
            if dt <= 0 or dt > 0.5: continue

            hz_buf.append(dt)
            if len(hz_buf) > 40: hz_buf.pop(0)
            sample_hz = 1.0 / (sum(hz_buf)/len(hz_buf)) if hz_buf else 0.0

            # Orientation filter
            ori_kf.predict(gx, gy, gz, dt)
            ori_kf.update(ax, ay, az)
            roll_d  = math.degrees(ori_kf.roll)
            pitch_d = math.degrees(ori_kf.pitch)
            yaw_d   = math.degrees(ori_kf.yaw)
            # R_cur is built after calibration (with R_offset applied)

            # Position filter
            # -- Gravity calibration: collect first CALIB_SAMPLES at rest --
            if g_body_ref is None:
                calib_acc.append(np.array([ax, ay, az]))
                if len(calib_acc) >= CALIB_SAMPLES:
                    mean_g = np.mean(calib_acc, axis=0)
                    g_body_ref = mean_g * GRAVITY   # convert g -> m/s²

                    # Compute R_offset: rotates the KF's world frame so that
                    # the measured gravity direction maps to (0,-1,0) (world down).
                    # This makes the grid flat and the box upright regardless of
                    # which way the board was facing at power-on.
                    R_raw       = euler_to_R(ori_kf.roll, ori_kf.pitch, ori_kf.yaw)
                    g_world_now = R_raw @ (mean_g / np.linalg.norm(mean_g))
                    R_offset    = rotation_align(g_world_now, np.array([0.0, -1.0, 0.0]))
                    print(f"[CALIB] Gravity reference (body): {g_body_ref}")
                    print(f"[CALIB] R_offset computed to level the scene.")
                # Skip position update until calibrated
                continue

            # Apply offset so world-Y always points opposite to gravity
            R_cur = R_offset @ euler_to_R(ori_kf.roll, ori_kf.pitch, ori_kf.yaw)

            # Convert accel from g to m/s², rotate to world frame,
            # then subtract the gravity vector (also rotated to world frame).
            a_body_ms2  = np.array([ax, ay, az]) * GRAVITY
            a_world     = R_cur @ a_body_ms2
            g_world     = R_cur @ g_body_ref      # gravity in world frame
            a_linear    = a_world - g_world        # true linear acceleration

            pos_kf.predict(*a_linear, dt)

            # ── ZUPT detection ──────────────────────────────────────────────
            # Check if device appears stationary:
            #   1. Gyro magnitude below threshold (not rotating)
            #   2. Accel magnitude close to 1g (not translating, just gravity)
            gyro_mag  = math.sqrt(gx**2 + gy**2 + gz**2)
            accel_mag = math.sqrt(ax**2 + ay**2 + az**2)
            device_still = (gyro_mag  < ZUPT_GYRO_THRESH and
                            abs(accel_mag - 1.0) < ZUPT_ACCEL_THRESH)

            if device_still:
                zupt_count += 1
            else:
                zupt_count = 0

            if zupt_count >= ZUPT_WINDOW:
                pos_kf.zupt_update()
                is_stationary = True
            else:
                is_stationary = False

            px, py, pz = pos_kf.position
            pos_display = (
                float(np.clip(px, -50, 50)),
                float(np.clip(py, -50, 50)),
                float(np.clip(pz, -50, 50)),
            )
            pos_trail.append(pos_display)
            if len(pos_trail) > TRAIL_LEN: pos_trail.pop(0)

        # ── 3-D Render ──────────────────────────────────────────────────────
        glClearColor(0.05, 0.05, 0.09, 1.0)
        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glLoadIdentity()

        cy_r = math.radians(cam_yaw)
        cp_r = math.radians(cam_pitch)
        ex = cam_dist * math.cos(cp_r) * math.sin(cy_r)
        ey = cam_dist * math.sin(cp_r)
        ez = cam_dist * math.cos(cp_r) * math.cos(cy_r)
        # Look at the current board position so camera follows the object
        lx, ly, lz = pos_display
        gluLookAt(lx + ex, ly + ey, lz + ez,  lx, ly, lz,  0, 1, 0)

        draw_grid()
        draw_world_axes()
        draw_board(R_cur, origin=pos_display)
        draw_body_axes(R_cur, origin=pos_display)
        draw_trail(pos_trail)

        # ── 2-D HUD ─────────────────────────────────────────────────────────
        w, h = pygame.display.get_surface().get_size()

        glMatrixMode(GL_PROJECTION)
        glPushMatrix(); glLoadIdentity()
        glOrtho(0, w, h, 0, -1, 1)
        glMatrixMode(GL_MODELVIEW)
        glPushMatrix(); glLoadIdentity()
        glDisable(GL_DEPTH_TEST)

        line_h = font.get_linesize()
        px_d, py_d, pz_d = pos_display
        vx_d, vy_d, vz_d = pos_kf.velocity
        vmag = math.sqrt(vx_d**2 + vy_d**2 + vz_d**2)
        calib_pct = int(100 * min(len(calib_acc), CALIB_SAMPLES) / CALIB_SAMPLES)
        calib_str = "OK" if g_body_ref is not None else f"calibrating {calib_pct}%"
        zupt_str  = "STATIONARY" if is_stationary else "moving"
        zupt_col  = (120, 255, 120) if is_stationary else (255, 180, 80)
        hud_lines = [
            (" IMU  ORIENTATION ", (160, 200, 255)),
            (f" Roll  : {roll_d:+7.2f} deg", (220, 220, 220)),
            (f" Pitch : {pitch_d:+7.2f} deg", (220, 220, 220)),
            (f" Yaw   : {yaw_d:+7.2f} deg", (220, 220, 220)),
            (" IMU  POSITION (m)", (160, 200, 255)),
            (f" X     : {px_d:+7.3f} m",     (220, 220, 220)),
            (f" Y     : {py_d:+7.3f} m",     (220, 220, 220)),
            (f" Z     : {pz_d:+7.3f} m",     (220, 220, 220)),
            (f" Speed : {vmag:.3f} m/s",      (200, 200, 220)),
            (f" ZUPT  : {zupt_str}",           zupt_col),
            (f" Gravity: {calib_str}", (180, 220, 160) if g_body_ref is not None else (255, 200, 80)),
            (f" Temp  : {temp_c:.1f} C", (180, 200, 180)),
            (f" Rate  : {sample_hz:.1f} Hz", (180, 200, 180)),
            ("", (0,0,0)),
            (" [R] Reset  [Esc] Quit", (110, 110, 130)),
            (" Drag: orbit  Scroll: zoom", (110, 110, 130)),
        ]
        pad = 8
        box_w = max(font.size(l)[0] for l, _ in hud_lines) + pad * 2
        box_h = line_h * len(hud_lines) + pad * 2

        hud = pygame.Surface((box_w, box_h), pygame.SRCALPHA)
        hud.fill((8, 8, 18, 200))
        # top accent bar
        pygame.draw.rect(hud, (60, 100, 200, 220), (0, 0, box_w, 2))
        for i, (txt, col) in enumerate(hud_lines):
            s = font.render(txt, True, col)
            hud.blit(s, (pad, pad + i * line_h))

        margin = 14
        upload_surface_as_quad(hud, margin, h - box_h - margin)

        glEnable(GL_DEPTH_TEST)
        glPopMatrix()
        glMatrixMode(GL_PROJECTION)
        glPopMatrix()
        glMatrixMode(GL_MODELVIEW)

        pygame.display.flip()

    stop_evt.set()
    pygame.quit()
    print("Done.")


if __name__ == "__main__":
    main()