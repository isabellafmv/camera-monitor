"""
Steer the Insta360 Link 2 from the terminal - no video window, so it works
while Zoom, FaceTime, OBS etc. are using the camera.

Run
  python link2_keys.py

Keys (in this terminal)
  Arrows or W/A/S/D  pan / tilt         + / -  or Z / X  zoom in / out
  C  re-center       1-9  step size (1 = fine, 9 = coarse)       Q / Esc  quit
"""
import select
import sys
import termios
import tty

from link2_control import Gimbal

KEYMAP = {
    "\x1b[D": ("pan", -1), "a": ("pan", -1),
    "\x1b[C": ("pan", 1), "d": ("pan", 1),
    "\x1b[A": ("tilt", 1), "w": ("tilt", 1),
    "\x1b[B": ("tilt", -1), "s": ("tilt", -1),
    "+": ("zoom", 1), "=": ("zoom", 1), "z": ("zoom", 1),
    "-": ("zoom", -1), "x": ("zoom", -1),
}


def read_key():
    ch = sys.stdin.read(1)
    # Arrow keys arrive as Esc + "[A".."[D"; a lone Esc has nothing following it.
    if ch == "\x1b" and select.select([sys.stdin], [], [], 0.05)[0]:
        return ch + sys.stdin.read(2)
    return ch.lower()


def main():
    gimbal = Gimbal()
    step = 0.02
    print(__doc__.split("Keys", 1)[1].split("\n", 1)[1])

    old = termios.tcgetattr(sys.stdin)
    tty.setcbreak(sys.stdin)
    try:
        while True:
            key = read_key()
            if key in ("q", "\x1b"):
                break
            if key in KEYMAP:
                axis, sign = KEYMAP[key]
                gimbal.move(axis, sign * step)
            elif key == "c":
                gimbal.center()
            elif key in "123456789":
                step = int(key) * 0.01
            p = gimbal.pos
            print(f"\rpan {p['pan'] / 3600:+4.0f}°  tilt {p['tilt'] / 3600:+4.0f}°  "
                  f"zoom {p['zoom']:3d}  step {step:.2f}  ", end="", flush=True)
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old)
        print()


if __name__ == "__main__":
    main()
