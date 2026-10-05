"""
Nitrogen tank pressure monitor - is the gauge needle above or below the red line?

A red tick is stuck on the dial face at 50. Each check unwraps the dial into
polar coordinates (one row per angle), finds the red tick and the dark needle,
and compares where they sit along the scale. No OCR, no reading of numbers.

Works on macOS (ffmpeg + uvcc, see link2_control.py) and Windows (DirectShow,
see link2_windows.py). Calibrate on the computer that will do the monitoring.

Setup
  python gauge_monitor.py calibrate      # aim, then click: dial center, dial rim, scale zero
    Aim so the dial is big in the picture. Windows: calibrate opens a live view
    with the steering keys - aim there and press SPACE. macOS: aim beforehand
    with python link2_keys.py (it can stay open).
  Webhook for alerts:
    macOS: export GAUGE_WEBHOOK_URL=https://hooks.slack.com/services/...
    Windows: setx GAUGE_WEBHOOK_URL "https://hooks.slack.com/services/..." (then open a new terminal)

Run (on Windows type `py` instead of `python`)
  python gauge_monitor.py check                  # one reading + annotated snapshot
  python gauge_monitor.py check --image x.jpg    # offline, from a photo
  python gauge_monitor.py run                    # monitor loop with webhook alerts
  python gauge_monitor.py run --test-alert       # send a test message first
  Windows, keep it running: start_gauge_monitor.bat (shortcut in shell:startup)
"""
import argparse
import json
import math
import os
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "gauge_config.json"
STATE_PATH = HERE / "monitor_state.json"
SNAPSHOT_DIR = HERE / "snapshots"
KEEP_SNAPSHOTS = 100

# Testing values - raise to ~300 s and ~6 h once it is proven to work.
CHECK_EVERY_S = 10
REMIND_HOURS = 0.05
# Consecutive readings needed before a state counts (and alerts).
CONFIRM = {"OK": 2, "LOW": 2, "UNKNOWN": 3}

ANGLE_STEPS = 720  # polar rows, 0.5 deg each
MIN_TAPE_ROW_PIXELS = 3  # red pixels an angle needs to count as under the tape
TAPE_MARGIN_DEG = 1.0
FALLBACK_TAPE_DEG = 3.0  # tape half-width to assume when it isn't seen
COVER_TOLERANCE_DEG = 10.0  # a hidden needle's axis (from the counterweight alone) is this rough
BELOW_ZERO_DEG = 30  # dial dead zone: gauges sweep <= ~300 deg, so this is never on the scale
NEEDLE_BAND = (0.3, 0.7)  # fraction of dial radius: skips the hub and the printed scale
TAIL_BAND = (0.12, 0.3)  # counterweight behind the hub, opposite the needle
OUTER_NEEDLE_BAND = (0.45, 0.7)  # past the end of any counterweight: only the needle reaches here
HUB_BAND = (0.02, 0.08)  # the needle's pivot cap
NEEDLE_VS_HUB = 0.7  # visible needles measured 0.9-1.07x as dark as the hub, labels+ticks ~0.5x
MARK_BAND = (0.55, 1.05)
MIN_MARK_PIXELS = 15
MIN_NEEDLE_CONTRAST = 25  # gray levels darker than the typical angle
MIN_FRAME_BRIGHTNESS = 15
PTZ_SETTLE_S = 2
REFERENCE_PATH = HERE / "gauge_reference.png"
ALIGN_SEARCH = 0.6  # look for the dial up to this many radii away from where it was
ALIGN_MIN_SCORE = 0.5  # template match below this = the gauge isn't in view

FONT = cv2.FONT_HERSHEY_SIMPLEX
VERDICT_COLORS = {"OK": (0, 180, 0), "COVERED": (0, 180, 255), "LOW": (0, 0, 255), "UNKNOWN": (128, 128, 128)}


# ---------------------------------------------------------------- vision

def smooth(values, width):
    """Circular moving average (angles wrap around)."""
    padded = np.concatenate([values[-width:], values, values[:width]])
    return np.convolve(padded, np.ones(width) / width, "same")[width:-width]


def red_mask(bgr):
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    return cv2.inRange(hsv, (0, 100, 80), (10, 255, 255)) | cv2.inRange(hsv, (170, 100, 80), (180, 255, 255))


def grow(mask, n):
    """Widen a circular boolean mask by n rows on each side."""
    return np.convolve(np.concatenate([mask[-n:], mask, mask[:n]]), np.ones(2 * n + 1), "same")[n:-n] > 0


def tape_rows(red_per_row, mark_row):
    """Angles (polar rows) the red tape covers: the run of red rows around its peak."""
    tape = np.zeros(len(red_per_row), bool)
    for direction in (1, -1):
        row = mark_row
        while red_per_row[row % len(tape)] >= MIN_TAPE_ROW_PIXELS and not tape[row % len(tape)]:
            tape[row % len(tape)] = True
            row += direction
    tape[mark_row] = True
    return grow(tape, int(TAPE_MARGIN_DEG * ANGLE_STEPS / 360))


def scale_pos(angle, cfg):
    """Degrees along the scale from its zero, in the direction pressure increases.

    Angles just short of the zero (needle resting on its stop, or a sloppy zero
    click) come out slightly negative instead of wrapping to ~360.
    """
    sign = 1 if cfg["clockwise"] else -1
    pos = (sign * (angle - cfg["zero_angle"])) % 360
    return pos - 360 if pos > 360 - BELOW_ZERO_DEG else pos


def analyze(frame, cfg):
    """Find needle and red mark angles (degrees, clockwise from +x on screen) and decide."""
    result = {"verdict": "UNKNOWN", "reason": "", "needle_angle": None, "mark_angle": None,
              "diff": None, "contrast": None, "mark_detected": False}
    if frame.mean() < MIN_FRAME_BRIGHTNESS:
        result["reason"] = "frame too dark"
        return result

    center, radius = tuple(cfg["center"]), cfg["radius"]
    max_r = radius * 1.1
    # Row i = angle i * 360 / ANGLE_STEPS, column = distance from center.
    pol = cv2.warpPolar(frame, (int(max_r), ANGLE_STEPS), center, max_r, cv2.WARP_POLAR_LINEAR)
    col = lambda frac: int(frac * radius / max_r * pol.shape[1])
    step = 360 / ANGLE_STEPS
    red = red_mask(pol) > 0

    lo, hi = col(MARK_BAND[0]), col(MARK_BAND[1])
    red_counts = smooth(red[:, lo:hi].sum(axis=1).astype(float), 5) * 5
    if red_counts.max() >= MIN_MARK_PIXELS:
        result["mark_angle"], result["mark_detected"] = float(red_counts.argmax() * step), True
        tape = tape_rows(red[:, col(NEEDLE_BAND[0]):hi].sum(axis=1), int(red_counts.argmax()))
    elif cfg.get("mark_angle") is not None:
        result["mark_angle"] = cfg["mark_angle"]  # fall back to the calibrated position
        tape = np.zeros(ANGLE_STEPS, bool)
        tape[int(cfg["mark_angle"] / step)] = True
        tape = grow(tape, int(FALLBACK_TAPE_DEG / step))
    else:
        result["reason"] = "no red mark found"
        return result

    gray = cv2.cvtColor(pol, cv2.COLOR_BGR2GRAY).astype(float)
    gray[red] = 255  # the red mark must never look like the needle

    def band_darkness(band):
        return 255 - gray[:, col(band[0]):col(band[1])].mean(axis=1)

    def line_darkness(band):
        """Per angle: how dark an unbroken line through the whole band is.

        A needle is dark along all of it; labels and ticks only in patches, so
        take a low percentile. ±1 deg slack for a slightly off-center needle.
        """
        dark = 255 - gray[:, col(band[0]):col(band[1])]
        dark = np.max([np.roll(dark, s, axis=0) for s in range(-2, 3)], axis=0)
        return np.percentile(dark, 10, axis=1)

    # The real needle runs through the hub into a counterweight on the opposite
    # side; thin secondary pointers don't, so the tail decides between them.
    darkness = smooth(band_darkness(NEEDLE_BAND) + np.roll(band_darkness(TAIL_BAND), ANGLE_STEPS // 2), 5)
    peak = int(darkness.argmax())
    result["contrast"] = float(darkness[peak] - np.median(darkness))
    if result["contrast"] < MIN_NEEDLE_CONTRAST:
        result["needle_angle"] = float(peak * step)
        result["reason"] = f"needle not clear (contrast {result['contrast']:.0f})"
        return result

    # The peak is the needle's axis, but which end is the needle? Only the needle
    # itself is dark far out from the hub (the counterweight is short). When the
    # tape hides the needle, neither end is, and only the counterweight shows -
    # pointing away from the tape, so the axis lines up with it.
    outer = line_darkness(OUTER_NEEDLE_BAND)
    face = np.median(outer)
    # The needle is painted like the hub cap, so judge "dark" relative to the hub
    # rather than in fixed gray levels - that follows the lighting.
    hub = np.median(band_darkness(HUB_BAND)) - face
    needle_min = max(MIN_NEEDLE_CONTRAST, NEEDLE_VS_HUB * hub)
    ends = [e for e in (peak, (peak + ANGLE_STEPS // 2) % ANGLE_STEPS) if outer[e] - face >= needle_min]
    if ends:
        peak = max(ends, key=lambda e: outer[e])
        covered = bool(tape[peak])
    else:
        near_tape = grow(tape, int(COVER_TOLERANCE_DEG / step))
        taped = [e for e in (peak, (peak + ANGLE_STEPS // 2) % ANGLE_STEPS) if near_tape[e]]
        if not taped:
            result["needle_angle"] = float(peak * step)
            result["reason"] = "needle not visible and not under the tape"
            return result
        peak, covered = taped[0], True
    result["needle_angle"] = float(peak * step)

    diff = scale_pos(result["needle_angle"], cfg) - scale_pos(result["mark_angle"], cfg)
    result["diff"] = float(diff)
    result["verdict"] = "COVERED" if covered else "OK" if diff > 0 else "LOW"
    if not result["mark_detected"]:
        result["reason"] = "red mark not seen, used calibrated position"
    return result


def put_text(img, text, org, color, scale=1.0):
    cv2.putText(img, text, org, FONT, scale, (0, 0, 0), 5)
    cv2.putText(img, text, org, FONT, scale, color, 2)


def annotate(frame, cfg, result):
    """Crop around the dial and draw center, needle ray, mark ray and the verdict."""
    img = frame.copy()
    (cx, cy), r = cfg["center"], cfg["radius"]
    cv2.circle(img, (cx, cy), int(r), (255, 200, 0), 2)
    cv2.circle(img, (cx, cy), 5, (255, 200, 0), -1)

    def ray(angle, color, length):
        a = math.radians(angle)
        cv2.line(img, (cx, cy), (int(cx + length * math.cos(a)), int(cy + length * math.sin(a))), color, 3)

    ray(cfg["zero_angle"], (255, 200, 0), r)
    if result["mark_angle"] is not None:
        ray(result["mark_angle"], (255, 0, 255), r * 1.05)
    if result["needle_angle"] is not None:
        ray(result["needle_angle"], (0, 255, 255), r * 0.9)

    pad = int(r * 1.3)
    y0, x0 = max(cy - pad, 0), max(cx - pad, 0)
    img = img[y0:cy + pad, x0:cx + pad].copy()
    scale = max(img.shape[1] / 700, 0.5)
    put_text(img, result["verdict"], (10, int(40 * scale)), VERDICT_COLORS[result["verdict"]], scale * 1.2)
    detail = f"diff {result['diff']:+.1f} deg" if result["diff"] is not None else result["reason"]
    put_text(img, detail, (10, int(80 * scale)), (255, 255, 255), scale * 0.7)
    return img


def save_snapshot(img, verdict):
    SNAPSHOT_DIR.mkdir(exist_ok=True)
    path = SNAPSHOT_DIR / f"{datetime.now():%Y%m%d-%H%M%S}_{verdict}.jpg"
    cv2.imwrite(str(path), img)
    for old in sorted(SNAPSHOT_DIR.glob("*.jpg"))[:-KEEP_SNAPSHOTS]:
        old.unlink()
    return path


# ---------------------------------------------------------------- camera

def read_image(path):
    frame = cv2.imread(path)
    if frame is None:
        raise RuntimeError(f"Can't read image {path!r}")
    return frame


def grab_frame(restore_ptz=None, camera_index=None):
    """One camera frame, after pointing the camera back at the gauge if it moved.

    Returns (frame, ptz) where ptz is the camera position now (None if unknown).
    PTZ problems only warn - a fixed camera can still read the gauge.
    """
    if sys.platform == "win32":
        return _grab_windows(restore_ptz, camera_index)
    return _grab_mac(restore_ptz)


def _grab_mac(restore_ptz):
    from insta360 import get_insta360_image
    from link2_control import UVCC, uvcc

    def read_ptz():
        pan, tilt = uvcc("get", "absolute_pan_tilt")
        return {"pan": pan, "tilt": tilt, "zoom": uvcc("get", "absolute_zoom")}

    if restore_ptz:
        try:
            if read_ptz() != restore_ptz:
                subprocess.run([*UVCC, "set", "absolute_pan_tilt",
                                str(restore_ptz["pan"]), str(restore_ptz["tilt"])], check=True)
                subprocess.run([*UVCC, "set", "absolute_zoom", str(restore_ptz["zoom"])], check=True)
                time.sleep(PTZ_SETTLE_S)
        except Exception as e:  # uvcc missing etc.
            print(f"  (couldn't restore camera position: {e})")
    frame = get_insta360_image()
    try:
        return frame, read_ptz()
    except Exception:
        return frame, None


def _grab_windows(restore_ptz, camera_index):
    from link2_windows import Camera
    cam = Camera(camera_index)  # one DirectShow handle does both PTZ and video
    try:
        if restore_ptz:
            try:
                cam.aim(restore_ptz)  # always: the parked Link 2 misreports where it points
            except Exception as e:
                print(f"  (couldn't restore camera position: {e})")
        frame = cam.grab()
        try:
            return frame, cam.ptz()
        except Exception:
            return frame, None
    finally:
        cam.close()


def reference_crop(frame, cfg):
    """Grayscale square around the dial, saved at calibration to recognise the view later."""
    (cx, cy), pad = cfg["center"], int(cfg["radius"] * 1.2)
    x0, y0 = max(cx - pad, 0), max(cy - pad, 0)
    crop = cv2.cvtColor(frame[y0:cy + pad, x0:cx + pad], cv2.COLOR_BGR2GRAY)
    return crop, [x0, y0]


def align(frame, cfg, reference):
    """Find the calibrated dial in this frame; returns (cfg with corrected center, shift)
    or (cfg, error) when the camera isn't looking at the gauge."""
    x0, y0 = cfg["reference_origin"]
    h, w = reference.shape
    pad = int(cfg["radius"] * ALIGN_SEARCH)
    sx, sy = max(x0 - pad, 0), max(y0 - pad, 0)
    window = cv2.cvtColor(frame[sy:y0 + h + pad, sx:x0 + w + pad], cv2.COLOR_BGR2GRAY)
    if window.shape[0] < h or window.shape[1] < w:
        return cfg, "camera image is smaller than at calibration"
    scores = cv2.matchTemplate(window, reference, cv2.TM_CCOEFF_NORMED)
    _, score, _, (mx, my) = cv2.minMaxLoc(scores)
    if score < ALIGN_MIN_SCORE:
        return cfg, f"camera isn't looking at the gauge (view match {score:.2f})"
    dx, dy = sx + mx - x0, sy + my - y0
    return dict(cfg, center=[cfg["center"][0] + dx, cfg["center"][1] + dy]), (dx, dy)


class FrameSource:
    """Where readings get their frames: a photo, a one-off camera grab, or (Windows
    `run`) a camera kept open between checks.

    Keeping the camera open matters on Windows: every close makes the Link 2 park
    itself, and every open costs a wake-up + re-aim of several seconds.
    """

    def __init__(self, cfg, image_path=None, use_ptz=True, camera_index=None, keep_open=False):
        self.image_path, self.camera_index = image_path, camera_index
        self.ptz = cfg.get("ptz") if use_ptz else None
        self.keep_open = keep_open and sys.platform == "win32" and not image_path
        self.cam = None

    def frame(self, reaim=False):
        if self.image_path:
            return read_image(self.image_path)
        if not self.keep_open:
            return grab_frame(self.ptz, self.camera_index)[0]
        try:
            if self.cam is None:
                from link2_windows import Camera
                self.cam, reaim = Camera(self.camera_index), True
            if reaim and self.ptz:
                self.cam.aim(self.ptz)
            self.cam.flush(0.5)  # drop frames buffered since the last check
            return self.cam.grab(warmup=2)
        except Exception:
            self.close()  # reopen from scratch next time
            raise

    def close(self):
        if self.cam is not None:
            self.cam.close()
            self.cam = None


def take_reading(cfg, source):
    """One full check. Never raises: failures come back as UNKNOWN."""
    reference = cv2.imread(str(REFERENCE_PATH), cv2.IMREAD_GRAYSCALE) if cfg.get("reference_origin") else None
    try:
        frame = source.frame()
        if reference is not None and isinstance(align(frame, cfg, reference)[1], str) and not source.image_path:
            frame = source.frame(reaim=True)  # something moved the camera - point it back and look again
    except Exception as e:
        return {"verdict": "UNKNOWN", "reason": f"capture failed: {e}", "diff": None}, None

    if reference is not None:
        cfg, shift = align(frame, cfg, reference)
        if isinstance(shift, str):
            result = {"verdict": "UNKNOWN", "reason": shift, "needle_angle": None, "mark_angle": None, "diff": None}
            return result, save_snapshot(annotate(frame, cfg, result), "UNKNOWN")
    else:
        shift = None
    result = analyze(frame, cfg)
    if shift and max(map(abs, shift)) > 2:
        result["reason"] = f"{result['reason']} (view shifted {shift[0]:+d},{shift[1]:+d} px, corrected)".strip()
    return result, save_snapshot(annotate(frame, cfg, result), result["verdict"])


def load_config():
    if not CONFIG_PATH.exists():
        sys.exit(f"No {CONFIG_PATH.name} yet - run: python gauge_monitor.py calibrate")
    return json.loads(CONFIG_PATH.read_text())


# ---------------------------------------------------------------- commands

CLICK_PROMPTS = ["dial CENTER", "the dial RIM (edge of the printed face)", "the ZERO / minimum of the scale"]


def click_points(frame):
    pts = []
    win = "calibrate - r: restart, Esc: cancel"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, 1280, int(1280 * frame.shape[0] / frame.shape[1]))
    cv2.setMouseCallback(win, lambda event, x, y, *_: event == cv2.EVENT_LBUTTONDOWN
                         and len(pts) < 3 and pts.append((x, y)))
    while len(pts) < 3:
        view = frame.copy()
        for p in pts:
            cv2.circle(view, p, 8, (0, 255, 0), -1)
        put_text(view, f"Click {CLICK_PROMPTS[len(pts)]}", (20, 50), (0, 255, 0), 1.2)
        cv2.imshow(win, view)
        key = cv2.waitKey(30) & 0xFF
        if key == 27:
            sys.exit("Calibration cancelled.")
        if key == ord("r"):
            pts.clear()
    cv2.destroyWindow(win)
    return pts


def aim_and_grab(camera_index):
    """Windows: steer in a live view and capture with the camera still open.

    Closing the camera between aiming and capturing makes the Link 2 park
    itself, so aiming has to happen here rather than in link2_windows.py.
    """
    from link2_windows import Camera, steer
    cam = Camera(camera_index)
    try:
        print("Aim at the gauge (A/D/W/S, Z/X, 1-9 step size), then press SPACE. Esc cancels.")
        got = steer(cam, capture=True)
        if got is None:
            sys.exit("Calibration cancelled.")
        return got
    finally:
        cam.close()


def cmd_calibrate(args):
    if args.image:
        frame, ptz = read_image(args.image), None
    else:
        if sys.platform == "win32":
            frame, ptz = aim_and_grab(args.camera_index)
        else:
            frame, ptz = grab_frame(camera_index=args.camera_index)
        if ptz is None:
            print("(couldn't read camera position, it won't be restored before checks)")
    if args.points:
        v = [int(n) for n in args.points.split(",")]
        pts = [(v[0], v[1]), (v[2], v[3]), (v[4], v[5])]
    else:
        pts = click_points(frame)
    (cx, cy), (rx, ry), (zx, zy) = pts
    cfg = {
        "center": [cx, cy],
        "radius": math.hypot(rx - cx, ry - cy),
        "zero_angle": math.degrees(math.atan2(zy - cy, zx - cx)) % 360,
        "clockwise": not args.ccw,
        "mark_angle": None,
        "ptz": ptz,
    }
    result = analyze(frame, cfg)
    if not result["mark_detected"]:
        sys.exit(f"Calibration failed: {result['reason'] or 'no red mark found inside the dial'}.")
    cfg["mark_angle"] = result["mark_angle"]

    preview = annotate(frame, cfg, result)
    cv2.imwrite(str(HERE / "gauge_calibration.jpg"), preview)
    print(f"Red mark at {scale_pos(cfg['mark_angle'], cfg):.0f} deg along the scale, "
          f"needle reads {result['verdict']} ({result['reason'] or 'diff %+.1f deg' % (result['diff'] or 0)}).")
    print("Preview: gauge_calibration.jpg (cyan = zero, magenta = red mark, yellow = needle)")

    if not args.points:
        win = "calibration - Enter: save, any other key: cancel"
        cv2.imshow(win, preview)
        key = cv2.waitKey(0) & 0xFF
        cv2.destroyAllWindows()
        if key not in (13, 10):
            sys.exit("Not saved.")
    reference, cfg["reference_origin"] = reference_crop(frame, cfg)
    cv2.imwrite(str(REFERENCE_PATH), reference)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2))
    print(f"Saved {CONFIG_PATH.name} and {REFERENCE_PATH.name}")


def format_reading(result):
    if result["diff"] is not None:
        return (f"{result['verdict']:7} diff {result['diff']:+6.1f} deg  "
                f"needle {result['needle_angle']:5.1f}  mark {result['mark_angle']:5.1f}  "
                f"contrast {result['contrast']:.0f}  {result['reason']}")
    return f"{result['verdict']:7} {result['reason']}"


def cmd_check(args):
    cfg = load_config()
    result, snap = take_reading(cfg, FrameSource(cfg, args.image, not args.no_ptz, args.camera_index))
    print(format_reading(result))
    if snap:
        print(f"snapshot: {snap}")
    sys.exit({"OK": 0, "COVERED": 0, "LOW": 1}.get(result["verdict"], 2))


def send_alert(text):
    print(f"  ALERT: {text}")
    url = os.environ.get("GAUGE_WEBHOOK_URL")
    if not url:
        print("  (GAUGE_WEBHOOK_URL not set - not sent)")
        return
    req = urllib.request.Request(url, data=json.dumps({"text": text}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(req, timeout=10).close()
    except Exception as e:
        print(f"  webhook failed: {e}")


def alert_text(state, result, snap, reminder=False):
    prefix = "Still: " if reminder else ""
    if state == "LOW":
        text = f":warning: {prefix}Nitrogen pressure is BELOW 50 (needle {result['diff']:+.0f} deg from the red line)."
    elif state == "UNKNOWN":
        text = f":grey_question: {prefix}Can't read the nitrogen gauge: {result['reason']}."
    else:
        text = ":white_check_mark: Nitrogen gauge reads above 50 again."
    return f"{text} Snapshot: {snap}" if snap else text


def cmd_run(args):
    cfg = load_config()
    state = {"state": None, "since": None, "last_alert": 0, "streak_state": None, "streak": 0}
    if STATE_PATH.exists():
        state.update(json.loads(STATE_PATH.read_text()))
    if args.test_alert:
        send_alert("Test alert from gauge_monitor - the webhook works.")
    print(f"Checking every {args.every} s, reminders every {args.remind_hours} h. Ctrl+C to stop.")
    source = FrameSource(cfg, args.image, not args.no_ptz, args.camera_index, keep_open=True)
    try:
        monitor(cfg, source, state, args)
    finally:
        source.close()


def monitor(cfg, source, state, args):
    while True:
        result, snap = take_reading(cfg, source)
        current = "OK" if result["verdict"] in ("OK", "COVERED") else result["verdict"]
        if current == state["streak_state"]:
            state["streak"] += 1
        else:
            state["streak_state"], state["streak"] = current, 1
        print(f"{datetime.now():%H:%M:%S} {format_reading(result)}")

        now = time.time()
        if state["streak"] >= CONFIRM[current] and current != state["state"]:
            if state["state"] is not None or current != "OK":  # no "all good" on first start
                send_alert(alert_text(current, result, snap))
                state["last_alert"] = now
            state["state"], state["since"] = current, now
        elif current == state["state"] != "OK" and now - state["last_alert"] >= args.remind_hours * 3600:
            send_alert(alert_text(current, result, snap, reminder=True))
            state["last_alert"] = now

        STATE_PATH.write_text(json.dumps(state, indent=2))
        time.sleep(args.every)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("calibrate", help="click the dial center, rim and scale zero; detect the red mark")
    p.add_argument("--image", help="calibrate on a photo instead of the camera")
    p.add_argument("--points", help="cx,cy,rimx,rimy,zerox,zeroy - skip clicking")
    p.add_argument("--ccw", action="store_true", help="pressure increases counter-clockwise")
    p.add_argument("--camera-index", type=int, help="Windows: DirectShow index, if name lookup fails")
    p.set_defaults(func=cmd_calibrate)

    for name, func, help_ in [("check", cmd_check, "take one reading"),
                              ("run", cmd_run, "monitor in a loop and send webhook alerts")]:
        p = sub.add_parser(name, help=help_)
        p.add_argument("--image", help="read a photo instead of the camera")
        p.add_argument("--no-ptz", action="store_true", help="don't move the camera back to the saved position")
        p.add_argument("--camera-index", type=int, help="Windows: DirectShow index, if name lookup fails")
        p.set_defaults(func=func)
    p.add_argument("--every", type=float, default=CHECK_EVERY_S, help="seconds between checks")
    p.add_argument("--remind-hours", type=float, default=REMIND_HOURS, help="repeat alerts while still bad")
    p.add_argument("--test-alert", action="store_true", help="send a test webhook message on start")

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
