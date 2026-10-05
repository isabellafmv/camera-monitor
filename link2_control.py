"""
Insta360 Link 2 - live feed + pan/tilt/zoom control (macOS).

Setup
  pip install opencv-python numpy
  brew install ffmpeg node && npm install -g uvcc

Run
  python link2_control.py

Keys (click the video window first)
  A / D  pan left / right        W / S  tilt up / down
  Z / X  zoom in / out           C      re-center
  1-9    step size (1 = fine, 9 = coarse)
  Esc / Q quit

Video comes from ffmpeg, which opens the camera by name. OpenCV's macOS backend
doesn't list external USB cameras, so it only ever finds the built-in/iPhone cams.

Before running: quit Insta360 Link Controller, or at least turn off AI tracking,
otherwise it fights your moves.
"""
import json
import queue
import subprocess
import threading
import time

import cv2
import numpy as np

CAMERA_NAME = "Insta360 Link 2"
WIDTH, HEIGHT, FPS = 1920, 1080, 30
UVCC = ["uvcc", "--vendor", "0x2e1a", "--product", "0x4c04"]


def uvcc(*args):
    return json.loads(subprocess.run([*UVCC, *args], capture_output=True, text=True, check=True).stdout)


class Gimbal:
    """Tracks pan/tilt/zoom and sends moves from a thread, since each uvcc call takes a moment."""

    def __init__(self):
        pt = uvcc("range", "absolute_pan_tilt")
        zoom = uvcc("range", "absolute_zoom")
        self.ranges = {
            "pan": (pt["min"][0], pt["max"][0]),
            "tilt": (pt["min"][1], pt["max"][1]),
            "zoom": (zoom["min"], zoom["max"]),
        }
        pan, tilt = uvcc("get", "absolute_pan_tilt")
        self.pos = {"pan": pan, "tilt": tilt, "zoom": uvcc("get", "absolute_zoom")}
        self._q = queue.Queue()
        threading.Thread(target=self._worker, daemon=True).start()

    def move(self, axis, frac):
        lo, hi = self.ranges[axis]
        value = self.pos[axis] + (hi - lo) * frac
        if axis != "zoom":
            value = round(value / 3600) * 3600  # pan/tilt move in whole degrees
        self.pos[axis] = int(max(lo, min(hi, value)))
        self._q.put(dict(self.pos))

    def center(self):
        self.pos = {"pan": 0, "tilt": 0, "zoom": self.ranges["zoom"][0]}
        self._q.put(dict(self.pos))

    def _worker(self):
        while True:
            pos = self._q.get()
            while not self._q.empty():  # skip straight to the newest target
                pos = self._q.get_nowait()
            subprocess.run([*UVCC, "set", "absolute_pan_tilt", str(pos["pan"]), str(pos["tilt"])])
            subprocess.run([*UVCC, "set", "absolute_zoom", str(pos["zoom"])])


def open_video():
    return subprocess.Popen(
        ["ffmpeg", "-hide_banner", "-loglevel", "error",
         "-f", "avfoundation", "-pixel_format", "nv12", "-framerate", str(FPS), "-video_size", f"{WIDTH}x{HEIGHT}",
         "-i", f"{CAMERA_NAME}:none",
         # passthrough: don't pad to a constant frame rate - with the Link 2's
         # timestamps that floods the pipe with copies of one frame (frozen video)
         "-fps_mode", "passthrough",
         "-f", "rawvideo", "-pix_fmt", "bgr24", "-"],
        stdout=subprocess.PIPE,
    )


class Video:
    """Reads frames on a thread and restarts ffmpeg when the camera stops sending them."""

    STARTUP_SECONDS = 6  # the Link 2 can take ~3 s to deliver its first frame
    STALL_SECONDS = 2

    def __init__(self):
        self.frame = None
        self._start()

    def _start(self):
        self.proc = open_video()
        self.started, self.last_frame = time.time(), None
        threading.Thread(target=self._read, args=(self.proc,), daemon=True).start()

    def _read(self, proc):
        frame_bytes = WIDTH * HEIGHT * 3
        while len(raw := proc.stdout.read(frame_bytes)) == frame_bytes:
            self.frame = np.frombuffer(raw, np.uint8).reshape(HEIGHT, WIDTH, 3)
            self.last_frame = time.time()

    def check(self):
        """Return True if frames are arriving; otherwise restart ffmpeg and return False."""
        if self.last_frame:
            waited, limit = time.time() - self.last_frame, self.STALL_SECONDS
        else:
            waited, limit = time.time() - self.started, self.STARTUP_SECONDS
        if waited < limit:
            return True
        print(f"No frames from {CAMERA_NAME!r} for {waited:.0f} s - restarting video.")
        self.close()
        self._start()
        return False

    def close(self):
        self.proc.kill()


def main():
    gimbal = Gimbal()
    video = Video()
    step = 0.02
    keymap = {"a": ("pan", -1), "d": ("pan", 1), "w": ("tilt", 1), "s": ("tilt", -1),
              "z": ("zoom", 1), "x": ("zoom", -1)}

    try:
        while True:
            live = video.check()
            if video.frame is None:
                frame = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
                status = "waiting for camera..."
            else:
                frame = video.frame.copy()
                status = "" if live else "reconnecting..."

            p = gimbal.pos
            cv2.putText(frame, f"pan {p['pan'] / 3600:+.0f}  tilt {p['tilt'] / 3600:+.0f}  "
                        f"zoom {p['zoom']}  step {step:.2f}  {status}",
                        (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            cv2.imshow(CAMERA_NAME, frame)

            ch = chr(cv2.waitKey(30) & 0xFF).lower()
            if ch in ("\x1b", "q"):
                break
            if ch in keymap:
                axis, sign = keymap[ch]
                gimbal.move(axis, sign * step)
            elif ch == "c":
                gimbal.center()
            elif ch in "123456789":
                step = int(ch) * 0.01
    finally:
        video.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
