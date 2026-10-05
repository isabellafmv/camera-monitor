# Nitrogen tank pressure monitor

Watches the pressure gauge on a nitrogen tank's regulator with an Insta360 Link 2 webcam and sends an alert when the pressure drops below 50.

It doesn't read the numbers on the dial. A strip of **red tape** marks 50 on the gauge, and each check asks one question: is the needle above or below the tape?

| Verdict | Meaning | Alert? |
|---|---|---|
| `OK` | Needle is above the tape | no |
| `COVERED` | Needle is under the tape | no (counts as OK) |
| `LOW` | Needle is below the tape | yes, after 2 checks in a row |
| `UNKNOWN` | Gauge can't be read (camera off, view blocked, too dark, camera pointing elsewhere) | yes, after 3 checks in a row |

Each check also saves an annotated picture in `snapshots/` showing what it saw.

---

## 1. Physical setup

**The red tape**
- Stick a strip of red tape on the dial at the **50** position. Run it from outside the bezel in toward the needle hub, stopping about a third of the way from the hub.
- It has to be clearly red, and the needle must be dark (black).
- Put it on the **right-hand gauge**, the one being monitored.

**The camera**
- Mount the Link 2 on something solid that nobody bumps, such as a clamp or small tripod. Don't put it on a desk where people work.
- Zoom in so the dial fills a good part of the picture, but leave some margin around it.
- Avoid direct sunlight or strong glare on the gauge glass. Steady lighting works best.

## 2. Install (Windows, no admin rights needed)

In this folder:
   ```
   py -m pip install --user opencv-python numpy pygrabber
   ```

First time only, check that the camera works:
```
py link2_windows.py --probe
```
It should list the Link 2, report a **1920x1080** frame, and say `responds` for pan, tilt and zoom. If the pan/tilt numbers change but the picture doesn't visibly move, open `link2_windows.py` and set `PAN_TILT_UNIT = 3600`.

## 3. Calibrate

```
py gauge_monitor.py calibrate
```
1. A live view opens. Aim at the gauge with the keys below, then press **Space**.
2. Click three points:
   1. the **center** of the needle hub
   2. the **edge of the printed dial face** (not the chrome bezel)
   3. the **0** on the scale
3. A preview shows what it found:
   - **cyan** for the zero
   - **magenta** for the red tape
   - **yellow** for the needle

   Press **Enter** to save, or any other key to cancel.

| Key | Action |
|---|---|
| A / D | pan left / right |
| W / S | tilt up / down |
| Z / X | zoom in / out |
| 1–9 | step size (1 = fine, 9 = coarse) |
| C | re-center |
| Space | use this view (calibrate only) |
| Esc / Q | cancel / quit |

This saves `gauge_config.json` (dial position and camera position) and `gauge_reference.png` (a picture of the dial, used to check the camera is still looking at it).

> Aim inside `calibrate`

## 4. Check a single reading

```
py gauge_monitor.py check
```
It prints the verdict and a snapshot path. Open the snapshot and check that:
- the **yellow** line is on the needle
- the **magenta** line is on the tape

To test that it reacts, move the tape above the needle and run `check` again. It should say `LOW`. Put the tape back afterwards, then recalibrate if the tape isn't exactly where it was.

## 5. Alerts

Alerts go to a Slack incoming webhook:
1. At [api.slack.com/apps](https://api.slack.com/apps), choose **Create New App → From scratch**.
2. Turn on **Incoming Webhooks**, choose **Add New Webhook to Workspace**, and pick a channel.
3. Copy the URL and save it on the PC:
   ```
   setx GAUGE_WEBHOOK_URL "https://hooks.slack.com/services/..."
   ```
4. Open a **new** terminal, since `setx` only applies to new windows, and test it:
   ```
   py gauge_monitor.py run --test-alert
   ```

The monitor sends:
- ⚠️ when the pressure drops below 50
- ❔ when the gauge can't be read
- ✅ when the reading is fine again
- a "Still: …" reminder while it stays bad

It doesn't alert on startup when everything is fine. It remembers its last state in `monitor_state.json`, so a restart doesn't resend alerts.

Without `GAUGE_WEBHOOK_URL`, alerts are only printed in the terminal.

## 6. Run it permanently

- Double-click **`start_gauge_monitor.bat`**. It runs the monitor and restarts it if it stops.
- **Start at logon:** press Win+R, type `shell:startup`, and put a shortcut to `start_gauge_monitor.bat` in that folder.
- In **Power settings**, set sleep to **Never**. A sleeping PC doesn't check anything.
- While it runs, **don't open other camera apps** (`link2_windows.py`, Insta360 Link Controller, video calls). Windows lets only one program use the camera at a time.

**Check interval:** the shipped settings are for testing: a check every 10 s and a reminder every ~3 min. Once you trust it, change the top of `gauge_monitor.py`:
```python
CHECK_EVERY_S = 300   # every 5 minutes
REMIND_HOURS = 6
```
Then restart the `.bat`. Both can also be overridden per run: `py gauge_monitor.py run --every 300 --remind-hours 6`.

---

## When to recalibrate

Calibration stores where the dial is **in the picture**. Recalibrate when:
- the **camera mount** was moved or bumped hard
- the **cylinder was swapped**, since the regulator moves to the new tank
- the **tape** was moved
- lighting at the tank changed a lot for good (new lamp, blinds)

Two things are handled automatically:
- **The camera turning on its own** (parking, Link Controller, AI tracking). The monitor points it back at the saved position.
- **Small shifts of the picture.** The monitor finds the dial and corrects for the shift.

If the dial isn't in view, the verdict is `UNKNOWN: camera isn't looking at the gauge`.

## Files

| File | What it is |
|---|---|
| `gauge_monitor.py` | The monitor: `calibrate`, `check`, `run` |
| `link2_windows.py` | Windows camera access and PTZ; manual live view (`py link2_windows.py`) and `--probe` self-test |
| `start_gauge_monitor.bat` | Runs the monitor and restarts it if it stops (Windows) |
| `link2_control.py`, `link2_keys.py` | macOS live view and terminal steering (ffmpeg + uvcc) |
| `gauge_config.json`, `gauge_reference.png` | Created by `calibrate`; specific to one camera setup |
| `monitor_state.json` | Last alert state, so restarts don't re-alert |
| `snapshots/` | Annotated pictures of the latest checks (the last 100 are kept) |
