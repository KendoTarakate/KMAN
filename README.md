# KMAN — Silent Choking Detector, Step 1: Sensor Logger & Signal Analysis

Bench prototype for acquiring clean breathing (chest IMU) and neck-tension
(flex sensor) signals, recording labelled sessions, and analysing them offline.
No detection logic, BLE or alerts yet.

```
firmware/   PlatformIO project (XIAO nRF52840, Arduino/Adafruit-Seeed core)
host/       logger.py, live_view.py, to_csv.py, sim_device.py + kman_host package
analysis/   report.py, config.yaml (all thresholds) + kman_analysis package
data/       recordings (gitignored) + a small synthetic sample
docs/       screenshots
```

**Status.** Everything below was built and exercised end-to-end against the
included device simulator (which speaks the exact firmware protocol), and the
firmware compiles for the XIAO nRF52840 (all valid sensor configurations). It has
**not yet run on real hardware**. The hardware test steps per stage are in
[Testing on hardware](#testing-on-hardware-stage-by-stage).

---

## Design in one page

**Timing ("timer-master").** TIMER4 runs free at 1 MHz from the 32 MHz crystal.
A compare interrupt every 10 ms (100 Hz, `KMAN_SAMPLE_RATE_HZ`) only records the
tick and wakes the loop task. The loop reads every sensor once and queues one
frame stamped with the tick's *scheduled* time. Ticks are exact multiples of
the period, with no ISR jitter and no drift beyond crystal tolerance (~20 ppm).

*Why not the IMU FIFO (the spec asked for it):* the LSM6DSOX runs its ODR from
its own ±1.5 % oscillator, so FIFO samples drift against the MCU-timed ADC by
20–30 s over 30 minutes. Instead the IMU free-runs at 208 Hz (≥ 2× frame rate)
with its digital LPFs well below 50 Hz (accel LPF2 = ODR/10 = 20.8 Hz, gyro
LPF1 ≈ 26 Hz), and each tick reads the latest output registers in one 12-byte
burst (Block Data Update on). Worst-case sample age is 4.8 ms, which is
irrelevant below 15 Hz. (Decision agreed before implementation.)

**Lost data is always visible.** `seq` is the tick counter, so any frame that
doesn't reach the host (loop overrun, TX-ring overflow, USB loss, CRC error)
shows up as a sequence gap. The device also reports its own counters once per
second (STATUS frame), so you can tell device-side from transport-side loss.

**Transport.** `AA 55 | len u16 | type u8 | body | CRC-16/CCITT-FALSE`, little-endian.
| type | body |
|---|---|
| `0x01` DATA | `t_us u32, seq u16, valid u8`, then int16 channels in header order |
| `0x02` HEADER | JSON session header: board, device id, sensors (model, address, ranges, filters, status/error), channel names/units/scales |
| `0x03` STATUS | overruns, TX drops, per-slot read errors, max latency, TX-ring high water, HF-crystal flag |
| `0x04` LOG | text |

The header is sent when the host opens the port (DTR) and on request (`H`).
Sensors that fail init are reported in the header and **left out of the frame
layout** (not zero-filled). A sensor that fails a single read keeps its slot,
but its bit in the per-frame `valid` mask is cleared and the host turns those
values into NaN. Frame size with chest IMU + flex is 28 bytes, about 2.8 KB/s.

**Sensor drivers** implement `init() / read() / describeConfig()`
(`firmware/src/sensors/sensor.h`). Swapping the flex sensor for a piezo or
stretch sensor means adding one class; nothing else changes.

**No heap.** All buffers are static: 8 KB TX ring in front of USB CDC (writes
never block), 4 KB header buffer. RAM use is about 25 KB of 232 KB.

---

## Hardware & wiring (bench prototype)

| Signal | XIAO nRF52840 pin | nRF52840 | Connect to |
|---|---|---|---|
| I2C SDA | D4 | P0.04 | LSM6DSOX SDA |
| I2C SCL | D5 | P0.05 | LSM6DSOX SCL |
| Flex ADC | D0 / A0 | P0.02 / AIN0 | divider midpoint (+10–100 nF to GND) |
| 3V3 | 3V3 | | LSM6DSOX VIN, flex sensor top |
| GND | GND | | LSM6DSOX GND, R_fixed bottom |

* **LSM6DSOX (chest):** I2C address **0x6A** (SA0/SDO to GND; Adafruit breakout default).
  CS must be high (I2C mode); breakouts pull it up. Needs SDA/SCL pull-ups
  (on most breakouts; otherwise 4.7 kΩ to 3V3). Mount on the lower sternum, axes
  don't matter: the analysis finds the breathing axis itself.
* **Flex divider:** `3V3 — flex — A0 — R_fixed (47 kΩ) — GND`. The ADC reference is
  VDD/4 with gain 1/4, so readings are ratiometric (supply noise cancels).
  `R_flex = R_fixed · (16383/counts − 1)`. For best sensitivity pick
  R_fixed ≈ √(R_min·R_max) over the bend range you actually use.
* **Status LEDs** (on-board RGB): **green blinking 2 Hz = streaming**, **red solid
  = a configured sensor failed init** (the rest keep streaming), green blip every
  2 s = alive, waiting for the host to open the port.
* **Optional abdomen IMU:** second LSM6DSOX on the same bus with SA0 high
  (**0x6B**), build with `-DKMAN_ABDOMEN_IMU=1`. **Strongly recommended**, see
  [Limitations](#limitations--what-to-decide-next).

Pins, addresses, ranges, rate and metadata (`KMAN_FLEX_SENSOR_DESC`,
`KMAN_FLEX_R_FIXED_OHM`) are all in `firmware/include/config.h` and can be
overridden with `build_flags` in `platformio.ini`.

---

## Setup

**Firmware**
```bash
cd firmware
pio run -e xiao_nrf52840              # build (first run fetches the Seeed nRF52 platform from GitHub)
pio run -e xiao_nrf52840 -t upload    # flash; if the port isn't found, double-tap RESET and retry
```

**Python (3.11+)**
```bash
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
pip install -e .                                        # optional: makes kman_host/kman_analysis importable anywhere
python -m pytest                                        # 28 tests, incl. decoding frames built by the firmware's C++ encoder
```
Linux: add yourself to the serial group (`sudo usermod -aG dialout $USER`, then re-login).

**No hardware yet?** Everything works against the simulator:
```bash
python host/sim_device.py --pty                 # prints /dev/pts/N (Linux/macOS)
python host/logger.py --port /dev/pts/N         # record from it like a real device
python host/live_view.py --sim                  # or just watch synthetic signals
python analysis/report.py data/sample_synthetic.parquet
```

---

## Usage

```bash
python host/logger.py --list-ports
python host/logger.py --port COM5 --out data/s01_rest.parquet --subject S01 --notes "sternum, seated"
python host/live_view.py                       # second terminal: attaches to the logger over UDP
python analysis/report.py data/s01_rest.parquet
python host/to_csv.py data/s01_rest.parquet --units
```

**Labelling keys** (in the logger terminal *or* the live-view window):
`1` rest breathing · `2` talking · `3` drinking/swallow · `4` dry swallow · `5` head movement ·
`6` walking · `7` cough · `8` breath-hold · `9` simulated obstruction · `0` none. `q` quits.
A label applies from the next frame after the key press. Changes are stored with
their device timestamp and tick number.

**Files per recording**
* `X.parquet`: one row per frame: `t_us` (unwrapped device µs), `seq` (unwrapped tick),
  `segment`, `valid`, raw int16 channels, `label`, `host_time`. Key/value metadata
  holds the session header(s), label events, label names, logger + device statistics.
* `X.bin`: every received byte, written live. If the logger dies, rebuild with
  `python host/logger.py --replay data/X.bin --out data/X.parquet`; labels come from `X.labels.jsonl`.
* `X.labels.jsonl`: label events, flushed on every key press.

The logger status line shows rate, lost frames (%), CRC errors and device counters.
It reconnects automatically if the cable is pulled; the time axis is bridged and
a new `segment` starts.

---

## Testing on hardware (stage by stage)

### Stage 1: firmware streaming
1. Wire the IMU and divider as above, flash, and keep the USB cable connected.
   Expect a **green blip every 2 s** (waiting for host). **Solid red** = sensor init failed.
2. `python host/logger.py --list-ports` shows the board, e.g. `COM5` / `/dev/ttyACM0` / `/dev/cu.usbmodem…`.
3. `python host/logger.py --port <port> --out data/bringup.parquet --duration 60`
   * The `[header]` block must list `chest_imu ok LSM6DSOX` and `neck_flex ok`, and
     `clock: HFXO 32 MHz crystal`. An `init_failed` line includes the reason
     (no ACK → wiring/address; WHO_AM_I → wrong part). A `WARNING: reading at ADC rail` on the
     flex means it is open, shorted or unpowered.
   * The status line should settle at **100.0 Hz, lost 0**, `dev[ovr 0 txdrop 0 i2c 0 lat ≲1 ms]`.
     The LED blinks green.
4. Sanity-check the signals: `python host/to_csv.py data/bringup.parquet --units`
   and open the CSV. Board flat and still: one accel axis ≈ ±1.0 g, the others ≈ 0;
   gyro ≈ 0 ± 0.5 dps. Bend the flex sensor: `flex_raw` moves by hundreds of counts.
5. **Acceptance soak (30 min):** `python host/logger.py --port <port> --out data/soak.parquet --duration 1800`.
   Pass: the summary shows **lost < 0.1 %** (expect 0) and device overruns/TX drops 0.
   Losses with device counters at 0 point to USB/host (hub, power saving, CPU load).

### Stage 2: host logger
1. Record 2 minutes and press `1`, `8`, `1`, `4` … during it. Each press prints `[label -> …]`.
2. Ctrl-C (or `q`) and check the summary. Then
   `python host/logger.py --replay data/<name>.bin --out data/<name>_replay.parquet`
   must report the same frame and loss counts (the rebuild path works).
3. Unplug the USB cable mid-recording and plug it back in: the logger prints `reconnecting…` then
   `[reconnected]` and keeps recording into the same file (new segment).

### Stage 3: live view
1. Start the logger (as above), then `python host/live_view.py` in a second terminal.
2. Seated, quiet breathing, IMU on the lower sternum: **Chest respiration** shows a clean
   oscillation of roughly ±0.2–1° at your breathing rate. Hold your breath and it flattens.
3. Dry swallow: **Neck flex** shows a short bump (0.5–1.5 s). Nod or turn your head: a much larger,
   longer deflection, and **motion energy** stays low (the chest doesn't move).
4. Walk on the spot: motion energy jumps above about 15 mg. Press digits in the plot window: the label
   banner updates and the logger prints `[label (viewer) -> …]`.
5. No lag: the trace should keep pace with your breathing indefinitely (the header shows ~100 Hz).

### Stages 4–5: analysis & report
Record one labelled session with this protocol (press the label key at each change):

| label | activity | how |
|---|---|---|
| 1 | rest breathing | 2 min seated, quiet (first rest segment = calibration for thresholds) |
| 8 | breath-hold | 20 s after a normal exhale; rest 30 s after |
| 4 | dry swallow | 5–6 swallows, ~8 s apart |
| 5 | head movement | nod, extend, rotate left/right, ~4 s apart |
| 3 | drinking | sips of water |
| 2 | talking | read aloud 30 s |
| 6 | walking | 30 s on the spot |
| 7 | cough | 4–5 voluntary coughs |
| 9 | simulated obstruction | **≤ 10 s** inspiratory efforts against a closed mouth and nose (Müller manoeuvre); healthy volunteers only, stop if dizzy |

Then `python analysis/report.py data/<session>.parquet` (zoom: `--start 100 --end 200`).
Expected separation (this is what the simulator produces, see `docs/report_sample_synthetic.png`):
* **rest vs breath-hold:** breaths/min ≈ your rate vs ≈ 0; *max gap s* ≈ 4–6 vs ≈ hold length.
* **swallows vs head movement:** label 4 gives swallows/10 s > 0 and head/10 s = 0; label 5 the reverse.
* **normal vs obstruction (abdomen IMU only):** chest–abdomen phase ≈ 0° vs ≈ ±180°, paradox % ≈ 0 vs > 80.

If the real data doesn't separate, tune `analysis/config.yaml`. Thresholds are
relative to the per-session calibration where possible (flex `k_on`, `head_amplitude_k`),
with absolute floors for motion (`threshold_mg`) and breath prominence.

---

## Analysis summary
* **Breathing:** gravity-vector complementary filter (τ = 1 s; gyro bias from calibration) →
  decimate to 20 Hz → zero-phase 0.05–1 Hz band-pass (edges padded by 3 corner periods) →
  windowed PCA (30 s, Hann overlap-add, sign kept continuous) → inspiration-up orientation
  (I:E asymmetry) → peaks with prominence ≥ max(0.08°, 0.3 × rolling reference p-p).
  Outputs: breath times, amplitude, rate, seconds since last valid breath. Breaths during
  motion artifacts or data gaps are shown but not counted.
* **Motion artifact:** > 1.5 Hz accel energy, 2 s RMS, threshold in mg.
* **Chest–abdomen phase:** Hilbert phase difference, 10 s circular mean, gated by amplitude
  and coherence. Abdomen polarity is calibrated at rest (assumed in phase).
* **Neck events:** 10 s moving-median baseline, hysteresis at 5σ/2σ (σ from calibration),
  0.3–2.5 s; peak > 25σ, longer than 2.5 s, or neck-IMU tilt/yaw → head motion.
* `report.py`: stacked figure with label bands (`reports/<session>.png`), per-label table
  (also `_summary.csv`), breath and event lists (`_breaths.csv`, `_neck_events.csv`).

---

## Limitations & what to decide next
1. **Chest IMU alone cannot see obstruction.** During airway obstruction effort continues,
   so the chest waveform looks like (faster, deeper) breathing. Only chest–abdomen *phase*
   reveals paradoxical effort. The firmware and analysis already support a second LSM6DSOX
   at 0x6B; I'd add it before recording the dataset.
2. **A flex sensor is a weak swallow sensor.** Laryngeal elevation is a ~2 cm vertical glide,
   not a bend, and head motion dominates the signal. Swallows that happen during head motion
   (drinking from a cup) are absorbed into the head-motion event: see label 3 in the sample
   report. Worth evaluating: a piezo film or contact accelerometer over the thyroid cartilage, and/or the
   optional neck IMU (`-DKMAN_NECK_IMU=1`, 0x6B when no abdomen IMU) for real head-pose rejection.
3. **Thresholds are tuned on simulated data.** Expect to re-tune on the first real sessions.
4. **±2 g can clip** during vigorous coughing or running. `report.py` prints `chest_accel_clipped_pct`.
5. The firmware streams only while the host holds the port open (DTR). It is a logger, not yet a standalone device.

## Troubleshooting
* **No header / "data frames waiting":** wrong port, or old firmware. The logger re-requests every 2 s.
* **`init_failed: no I2C ACK`:** SDA/SCL swapped, no pull-ups, SA0 high (→ 0x6B), or VIN not powered.
* **`NO-XTAL` in the status line:** HF clock is on the RC oscillator, so timing is inaccurate. Report it; it shouldn't happen with USB connected.
* **Frame loss with device `ovr 0 txdrop 0`:** host-side. Try another USB port (no hub), disable USB selective suspend, close heavy apps.
* **Live view shows nothing:** start the logger first (it owns the port). The view needs the header, which the logger re-broadcasts every 2 s.
