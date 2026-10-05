"""
Insta360 Link 2 - live feed + pan/tilt/zoom control (Windows).

The Windows counterpart of link2_control.py. Video and pan/tilt/zoom both go
through OpenCV's DirectShow backend, which exposes the camera's own UVC
controls - no uvcc, Node or ffmpeg needed.

Setup (no admin rights needed)
  Install Python from python.org ("Install for me only"), then:
  py -m pip install --user opencv-python numpy pygrabber
  Settings > Privacy & security > Camera: allow desktop apps to use the camera.
  Quit Insta360 Link Controller, or at least turn off AI tracking.

Run
  py link2_windows.py --probe     # first time: check video and pan/tilt/zoom work
  py link2_windows.py             # live view + steering

Keys (click the video window first)
  A / D  pan left / right        W / S  tilt up / down
  Z / X  zoom in / out           C      re-center
  1-9    step size (1 = fine, 9 = coarse)
  Esc / Q quit

Only one program can use the camera at a time on Windows - close this window
before running gauge_monitor.py.
"""
import argparse
import sys
import time

import cv2
import numpy as np

CAMERA_NAME = "Insta360"  # matched against DirectShow device names
WIDTH, HEIGHT, FPS = 1920, 1080, 30
PROPS = {"pan": cv2.CAP_PROP_PAN, "tilt": cv2.CAP_PROP_TILT, "zoom": cv2.CAP_PROP_ZOOM}
# DirectShow reports pan/tilt in degrees. If --probe says the camera didn't
# visibly move on a 1-unit nudge, its driver uses arc-seconds: set this to 3600.
PAN_TILT_UNIT = 1
STEP = {"pan": PAN_TILT_UNIT, "tilt": PAN_TILT_UNIT, "zoom": 10}  # per step-size level


def find_camera_index(name=CAMERA_NAME):
    """DirectShow index of the first camera whose name contains `name`."""
    try:
        from pygrabber.dshow_graph import FilterGraph
    except ImportError:
        raise RuntimeError("pygrabber is missing: py -m pip install --user pygrabber "
                           "(or pass --camera-index N)")
    devices = FilterGraph().get_input_devices()  # same order as OpenCV's DirectShow indices
    for i, device in enumerate(devices):
        if name.lower() in device.lower():
            return i
    raise RuntimeError(f"No camera matching {name!r}. Found: {devices}")


class Camera:
    """A DirectShow capture of the Link 2, plus its pan/tilt/zoom controls."""

    def __init__(self, index=None):
        self.index = find_camera_index() if index is None else index
        self.cap = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
        if not self.cap.isOpened():
            raise RuntimeError(f"Can't open camera {self.index} - is another program using it?")
        # MJPG first: uncompressed 1080p doesn't fit USB 2 and DirectShow silently drops to 640x480.
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
        self.cap.set(cv2.CAP_PROP_FPS, FPS)

    def read(self):
        ok, frame = self.cap.read()
        return frame if ok else None

    def grab(self, warmup=10, timeout=8):
        """One frame, after skipping `warmup` so auto exposure / white balance settle."""
        deadline, frame, good = time.time() + timeout, None, 0
        while good <= warmup:
            frame = self.read()
            if frame is not None:
                good += 1
            elif time.time() > deadline:  # the Link 2 can take ~3 s for its first frame
                raise RuntimeError(f"No frames from camera {self.index}")
        return frame

    def get(self, axis):
        return self.cap.get(PROPS[axis])

    def set(self, axis, value):
        """Move one axis; returns where it ended up (unchanged if past the camera's limit)."""
        self.cap.set(PROPS[axis], value)
        return self.get(axis)

    def ptz(self):
        return {axis: self.get(axis) for axis in PROPS}

    def set_ptz(self, ptz):
        for axis, value in ptz.items():
            self.set(axis, value)

    def close(self):
        self.cap.release()


class Gimbal:
    """Step-wise moves. DirectShow gives OpenCV no ranges, so a refused move means a limit."""

    def __init__(self, cam):
        self.cam = cam
        self.pos = cam.ptz()
        self.note = ""

    def move(self, axis, steps):
        before = self.pos[axis]
        self.pos[axis] = self.cam.set(axis, before + steps * STEP[axis])
        self.note = f"{axis} limit" if self.pos[axis] == before else ""

    def center(self):
        self.cam.set("pan", 0)
        self.cam.set("tilt", 0)
        for _ in range(100):  # zoom all the way out: step down until the camera refuses
            zoom = self.cam.get("zoom")
            if self.cam.set("zoom", zoom - STEP["zoom"]) >= zoom:
                break
        self.pos, self.note = self.cam.ptz(), ""


def probe(index):
    """First-run self-test: camera found, real resolution, and whether PTZ responds."""
    try:
        from pygrabber.dshow_graph import FilterGraph
        print("DirectShow cameras:", FilterGraph().get_input_devices())
    except ImportError:
        print("(pygrabber not installed - can't list cameras by name)")
    cam = Camera(index)
    print(f"Opened camera {cam.index}")
    try:
        frame = cam.grab(warmup=5)
        fourcc = int(cam.cap.get(cv2.CAP_PROP_FOURCC)).to_bytes(4, "little").decode(errors="replace")
        print(f"Frame {frame.shape[1]}x{frame.shape[0]}, {cam.cap.get(cv2.CAP_PROP_FPS):.0f} fps, {fourcc}"
              + ("" if frame.shape[1] == WIDTH else f"  <- expected {WIDTH}x{HEIGHT}"))
        print("PTZ now:", cam.ptz())
        for axis in PROPS:
            start = cam.get(axis)
            moved = cam.set(axis, start + STEP[axis])
            time.sleep(1)
            cam.set(axis, start)
            ok = "responds" if moved != start else "did NOT change (unsupported or at its limit)"
            print(f"  {axis}: {start} -> {moved} -> back   {ok}")
        print("If pan/tilt changed in numbers but the view didn't visibly move, set PAN_TILT_UNIT = 3600.")
    finally:
        cam.close()


def main():
    parser = argparse.ArgumentParser(description="Insta360 Link 2 live view + PTZ (Windows)")
    parser.add_argument("--camera-index", type=int, help="DirectShow index, if name lookup fails")
    parser.add_argument("--probe", action="store_true", help="check video and pan/tilt/zoom, then exit")
    args = parser.parse_args()
    if args.probe:
        return probe(args.camera_index)

    cam = Camera(args.camera_index)
    gimbal = Gimbal(cam)
    level = 2
    keymap = {"a": ("pan", -1), "d": ("pan", 1), "w": ("tilt", 1), "s": ("tilt", -1),
              "z": ("zoom", 1), "x": ("zoom", -1)}
    STARTUP_SECONDS, STALL_SECONDS = 6, 2  # the Link 2 can take ~3 s to deliver its first frame
    started, last_frame = time.time(), None

    try:
        while True:
            frame = cam.read()
            if frame is not None:
                last_frame, status = time.time(), ""
            else:
                frame = np.zeros((HEIGHT, WIDTH, 3), np.uint8)
                status = "waiting for camera..." if last_frame is None else "reconnecting..."
                waited = time.time() - (last_frame or started)
                if waited > (STALL_SECONDS if last_frame else STARTUP_SECONDS):
                    print(f"No frames for {waited:.0f} s - reopening the camera.")
                    cam.close()
                    cam = gimbal.cam = Camera(cam.index)
                    started, last_frame = time.time(), None

            p = gimbal.pos
            cv2.putText(frame, f"pan {p['pan']:+.0f}  tilt {p['tilt']:+.0f}  zoom {p['zoom']:.0f}  "
                        f"step {level}  {gimbal.note}  {status}",
                        (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
            cv2.imshow("Insta360 Link 2", frame)

            ch = chr(cv2.waitKey(1) & 0xFF).lower()
            if ch in ("\x1b", "q"):
                break
            if ch in keymap:
                axis, sign = keymap[ch]
                gimbal.move(axis, sign * level)
            elif ch == "c":
                gimbal.center()
            elif ch in "123456789":
                level = int(ch)
    finally:
        cam.close()
        cv2.destroyAllWindows()


if __name__ == "__main__":
    try:
        main()
    except RuntimeError as e:
        sys.exit(str(e))
