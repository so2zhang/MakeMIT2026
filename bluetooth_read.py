#!/usr/bin/env python3
"""
imu_visualiser.py — Read MPU-6050 data from ESP32 over Bluetooth,
estimate orientation with a Madgwick AHRS filter, and render a live
3-D model of the IMU board using PyOpenGL + pygame.

Requirements:
    pip install pyserial pygame PyOpenGL PyOpenGL_accelerate numpy

Usage:
    python imu_visualiser.py --port /dev/rfcomm0      # Linux (pair first)
    python imu_visualiser.py --port COM7               # Windows
    python imu_visualiser.py --mac AA:BB:CC:DD:EE:FF  # auto-connect (Linux)

The Bluetooth device is named "ESP32_IMU".  Pair it with your OS first,
then note the serial port it maps to (rfcomm0, COM7, etc.)

Controls:
    R  – reset orientation to current reading
    Q  – quit
"""

import argparse
import math
import socket
import sys
import threading
import time
from collections import deque

import numpy as np
import serial


calib = None

# ── Madgwick AHRS ──────────────────────────────────────────────────────────
class MadgwickAHRS:
    """
    Minimal Madgwick filter (gyro + accel, no mag).
    q = [w, x, y, z]
    """
    def __init__(self, beta: float = 0.1, freq: float = 50.0):
        self.beta = beta
        self.dt   = 1.0 / freq
        self.q    = np.array([1.0, 0.0, 0.0, 0.0])

    def reset(self):
        self.q = np.array([1.0, 0.0, 0.0, 0.0])

    def update(self, gx, gy, gz, ax, ay, az):
        """
        gx,gy,gz in deg/s  →  converted to rad/s internally
        ax,ay,az in m/s²
        """
        gx, gy, gz = math.radians(gx), math.radians(gy), math.radians(gz)
        q = self.q
        w, x, y, z = q

        # Normalise accel
        norm = math.sqrt(ax*ax + ay*ay + az*az)
        if norm == 0:
            return
        ax, ay, az = ax/norm, ay/norm, az/norm

        # Gradient-descent objective function (gravity only)
        f1 = 2*(x*z - w*y)       - ax
        f2 = 2*(w*x + y*z)       - ay
        f3 = 2*(0.5 - x*x - y*y) - az

        J = np.array([
            [-2*y,  2*z, -2*w, 2*x],
            [ 2*x,  2*w,  2*z, 2*y],
            [ 0,   -4*x, -4*y, 0  ],
        ])
        step = J.T @ np.array([f1, f2, f3])
        n = np.linalg.norm(step)
        if n:
            step /= n

        # Rate of change of quaternion from gyro
        q_dot = 0.5 * np.array([
            -x*gx - y*gy - z*gz,
             w*gx + y*gz - z*gy,
             w*gy - x*gz + z*gx,
             w*gz + x*gy - y*gx,
        ]) - self.beta * step

        q = q + q_dot * self.dt
        self.q = q / np.linalg.norm(q)

    def rotation_matrix(self) -> np.ndarray:
        """Return 3×3 rotation matrix from quaternion."""
        w, x, y, z = self.q
        return np.array([
            [1-2*(y*y+z*z),  2*(x*y-w*z),   2*(x*z+w*y)],
            [2*(x*y+w*z),    1-2*(x*x+z*z), 2*(y*z-w*x)],
            [2*(x*z-w*y),    2*(y*z+w*x),   1-2*(x*x+y*y)],
        ])


# ── Bluetooth / Serial reader ──────────────────────────────────────────────
class IMUReader(threading.Thread):
    def __init__(self, port: str, baud: int = 115200):
        super().__init__(daemon=True)
        self.port  = port
        self.baud  = baud
        self._buf  = deque(maxlen=1)   # keep only latest reading
        self._lock = threading.Lock()
        self.connected = False
        self.error: str | None = None

    def get_sample(self):
        """Return latest (ax,ay,az,gx,gy,gz) or None."""
        with self._lock:
            return self._buf[-1] if self._buf else None

    def run(self):
        global calib
        while True:   # outer loop: reconnect on any error
            try:
                ser = serial.Serial(self.port, self.baud, timeout=2)
                self.connected = True
                self.error = None
                consecutive_timeouts = 0
                while True:
                    line = ser.readline().decode(errors='ignore').strip()
                    if not line:
                        consecutive_timeouts += 1
                        if consecutive_timeouts > 5:
                            # No data for ~10 s — treat as disconnect
                            raise serial.SerialTimeoutException("No data received")
                        continue
                    consecutive_timeouts = 0
                    parts = line.split(',')[-6:]
                    print(parts)
                    if len(parts) == 6:
                        try:
                            vals = list(float(p) for p in parts)
                            if calib is None:
                                # Use first reading as calibration reference (zero orientation)
                                calib = vals
                                print(f"Calibration set: {calib}")
                            
                            # Subtract calibration reference to get relative motion
                            vals = [v - c for v, c in zip(vals, calib)]
                            with self._lock:
                                self._buf.append(vals)
                        except ValueError:
                            pass
            except Exception as e:
                self.connected = False
                self.error = str(e)
                print(f"[IMU] Disconnected: {e} — retrying in 2 s…")
                time.sleep(2)


# ── OpenGL 3-D visualiser ─────────────────────────────────────────────────
def run_visualiser(reader: IMUReader):
    import pygame
    from pygame.locals import DOUBLEBUF, OPENGL, QUIT, KEYDOWN, K_q, K_r
    from OpenGL.GL import (
        glBegin, glEnd, glVertex3f, glColor3f, glLineWidth,
        glClear, glClearColor, glEnable, glDisable,
        glMatrixMode, glLoadIdentity, glMultMatrixf,
        glPushMatrix, glPopMatrix,
        GL_LINES, GL_QUADS, GL_DEPTH_TEST, GL_COLOR_BUFFER_BIT,
        GL_DEPTH_BUFFER_BIT, GL_MODELVIEW, GL_PROJECTION,
        glViewport, glFrustum,
    )
    from OpenGL.GLU import gluLookAt

    pygame.init()
    display = (900, 700)
    pygame.display.set_mode(display, DOUBLEBUF | OPENGL)
    pygame.display.set_caption("IMU Orientation Visualiser")

    # Projection
    glMatrixMode(GL_PROJECTION)
    glLoadIdentity()
    glFrustum(-0.5, 0.5, -0.4, 0.4, 1.0, 100.0)
    glMatrixMode(GL_MODELVIEW)

    glEnable(GL_DEPTH_TEST)
    glClearColor(0.12, 0.12, 0.15, 1.0)

    # Board geometry (flat box: 1.6 × 1.0 × 0.05 approx PCB ratio)
    W, H, D = 1.6, 1.0, 0.08
    hw, hh, hd = W/2, H/2, D/2

    # Faces: each is (color_rgb, [4 vertices])
    faces = [
        # Top (green PCB)
        ((0.1, 0.55, 0.1), [(-hw,-hh, hd),(hw,-hh, hd),(hw, hh, hd),(-hw, hh, hd)]),
        # Bottom
        ((0.05,0.30,0.05),[(-hw, hh,-hd),(hw, hh,-hd),(hw,-hh,-hd),(-hw,-hh,-hd)]),
        # Front
        ((0.08,0.45,0.08),[(hw,-hh,-hd),(hw,-hh, hd),(hw, hh, hd),(hw, hh,-hd)]),
        # Back
        ((0.08,0.45,0.08),[(-hw, hh,-hd),(-hw, hh, hd),(-hw,-hh, hd),(-hw,-hh,-hd)]),
        # Left
        ((0.06,0.40,0.06),[(-hw,-hh,-hd),(hw,-hh,-hd),(hw,-hh, hd),(-hw,-hh, hd)]),
        # Right
        ((0.06,0.40,0.06),[(-hw, hh, hd),(hw, hh, hd),(hw, hh,-hd),(-hw, hh,-hd)]),
    ]

    # Edge lines
    verts = [
        (-hw,-hh,-hd),(hw,-hh,-hd),(hw, hh,-hd),(-hw, hh,-hd),
        (-hw,-hh, hd),(hw,-hh, hd),(hw, hh, hd),(-hw, hh, hd),
    ]
    edges = [
        (0,1),(1,2),(2,3),(3,0),
        (4,5),(5,6),(6,7),(7,4),
        (0,4),(1,5),(2,6),(3,7),
    ]

    ahrs  = MadgwickAHRS(beta=0.05, freq=50.0)
    clock = pygame.time.Clock()
    last_t = time.time()

    def draw_axes():
        """Draw X(red) Y(green) Z(blue) reference axes."""
        glLineWidth(2.0)
        glBegin(GL_LINES)
        glColor3f(1,0,0); glVertex3f(0,0,0); glVertex3f(2.5,0,0)
        glColor3f(0,1,0); glVertex3f(0,0,0); glVertex3f(0,2.5,0)
        glColor3f(0,0,1); glVertex3f(0,0,0); glVertex3f(0,0,2.5)
        glEnd()
        glLineWidth(1.0)

    def draw_board(R):
        # Build 4×4 column-major matrix for OpenGL from rotation matrix
        m = np.eye(4)
        m[:3,:3] = R
        m44 = m.T.flatten().astype(np.float32)

        glPushMatrix()
        glMultMatrixf(m44)

        # Faces
        for color, vlist in faces:
            glColor3f(*color)
            glBegin(GL_QUADS)
            for v in vlist:
                glVertex3f(*v)
            glEnd()

        # Edges
        glColor3f(0.0, 0.9, 0.0)
        glLineWidth(1.5)
        glBegin(GL_LINES)
        for e in edges:
            for i in e:
                glVertex3f(*verts[i])
        glEnd()
        glLineWidth(1.0)

        # Forward arrow (X axis on board)
        glLineWidth(3.0)
        glBegin(GL_LINES)
        glColor3f(1, 0.3, 0.3)
        glVertex3f(0,0,hd+0.01)
        glVertex3f(hw+0.4,0,hd+0.01)
        glEnd()
        glLineWidth(1.0)

        glPopMatrix()

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == QUIT:
                running = False
            elif event.type == KEYDOWN:
                if event.key == K_q:
                    running = False
                elif event.key == K_r:
                    ahrs.reset()

        sample = reader.get_sample()
        if sample:
            ax, ay, az, gx, gy, gz = sample
            now = time.time()
            dt  = now - last_t
            last_t = now

            # Zero-rate deadband: clamp gyro noise below ~0.5 deg/s to zero.
            # The ESP32 already subtracts the bulk bias; this catches residual
            # noise that would otherwise cause slow phantom rotation at rest.
            DEADBAND = 0.5
            gx = 0.0 if abs(gx) < DEADBAND else gx
            gy = 0.0 if abs(gy) < DEADBAND else gy
            gz = 0.0 if abs(gz) < DEADBAND else gz

            ahrs.dt = max(0.005, min(dt, 0.1))
            ahrs.update(gx, gy, gz, ax, ay, az)

        R = ahrs.rotation_matrix()

        glClear(GL_COLOR_BUFFER_BIT | GL_DEPTH_BUFFER_BIT)
        glLoadIdentity()
        gluLookAt(0, -5, 2,   0, 0, 0,   0, 0, 1)

        draw_axes()
        draw_board(R)

        # HUD: print roll/pitch/yaw to console (cheap HUD)
        w, x, y, z = ahrs.q
        roll  =  math.degrees(math.atan2(2*(w*x+y*z), 1-2*(x*x+y*y)))
        pitch =  math.degrees(math.asin(max(-1, min(1, 2*(w*y-z*x)))))
        yaw   =  math.degrees(math.atan2(2*(w*z+x*y), 1-2*(y*y+z*z)))

        pygame.display.set_caption(
            f"IMU Vis  |  Roll: {roll:+6.1f}°  Pitch: {pitch:+6.1f}°  Yaw: {yaw:+7.1f}°"
            + ("  [NOT CONNECTED]" if not reader.connected else "")
        )

        pygame.display.flip()
        clock.tick(60)

    pygame.quit()


# ── Entry point ────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="IMU BT Visualiser")
    ap.add_argument('--port', default=None,
                    help='Serial/BT port, e.g. /dev/rfcomm0 or COM7')
    ap.add_argument('--baud', type=int, default=115200)
    args = ap.parse_args()

    if args.port is None:
        print("Error: specify --port /dev/rfcomm0 (Linux) or --port COM7 (Windows)")
        print("\nLinux quick-start:")
        print("  sudo rfcomm bind 0 AA:BB:CC:DD:EE:FF   # replace with your ESP32 MAC")
        print("  python imu_visualiser.py --port /dev/rfcomm0")
        print("\nWindows quick-start:")
        print("  Pair 'ESP32_IMU' in Bluetooth settings, note the COM port,")
        print("  then: python imu_visualiser.py --port COM7")
        sys.exit(1)

    print(f"Connecting to {args.port} @ {args.baud} baud …")
    reader = IMUReader(args.port, args.baud)
    reader.start()

    # Wait briefly for connection
    for _ in range(20):
        if reader.connected or reader.error:
            break
        time.sleep(0.1)

    if reader.error:
        print(f"Connection failed: {reader.error}")
        sys.exit(1)

    print("Connected! Starting visualiser.  Press R to reset, Q to quit.")
    run_visualiser(reader)


if __name__ == '__main__':
    main()