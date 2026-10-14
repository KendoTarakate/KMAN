# Changelog

All notable changes to this project. Versions follow [Semantic Versioning](https://semver.org/);
the firmware (`KMAN_FW_VERSION`), the Python package (`pyproject.toml`) and the git tag
always carry the same number.

## [1.0.0] - 2026-10-14

First release: bench sensor logger and offline signal analysis (Step 1).
Built and tested against the included device simulator; not yet validated on hardware.

### Added
- **Ready-to-flash firmware** for the Seeed XIAO nRF52840, one image per sensor set:
  `xiao_nrf52840` (chest IMU + neck flex), `xiao_nrf52840_abdomen` (+ abdomen IMU),
  `xiao_nrf52840_neck` (+ neck IMU). Each is a `.uf2` for drag-and-drop flashing (no tools needed)
  and a `.hex` for SWD programmers. Every `pio run` now writes the `.uf2` too.
- **[docs/FIRMWARE_GUIDE.md](docs/FIRMWARE_GUIDE.md)**: how to flash, verify, configure, update
  and recover the firmware.
- **[docs/CODE_GUIDE.md](docs/CODE_GUIDE.md)**: what every function does and how it works.
- `boot_id` in the session header: a random number per power-up, so the host can tell a reboot
  from a reconnect.
- GitHub Actions: tests and firmware builds on every push; pushing a `v*` tag publishes a release
  with the firmware images. The release step refuses to run unless the tag, the firmware version,
  the Python package version and this changelog agree.
- `host/tests/test_v1_fixes.py`: 23 regression tests for the fixes below (51 tests in total).
  The I2C-pin fix and the live-view UI changes are checked by building and running, not by tests.
- Platform *and* Seeed Arduino core pinned to fixed commits, so release images are reproducible.

### Fixed
- Unplugging and re-plugging the USB cable no longer discards the rest of the recording. The
  board reboots with the same `stream_id`, and its restarted clock was taken for duplicate
  frames. The host now starts a new segment when `boot_id` changes, when a header's timestamp
  doesn't follow the stream, or when the data clock jumps by more than 1 s in a way the tick
  counter doesn't confirm. That last rule also catches a reboot after more than 35.8 min of
  uptime whose start-up header was missed, which previously looked like 30 min of lost frames.
- Labels stay on the right frames when a session with a reconnect is rebuilt with `--replay`.
  Label events now record their segment and their offset within it. Before, every label after a
  reconnect landed late by the length of the disconnection. `.labels.jsonl` files from 0.1.0
  still replay as before.
- While the logger waits to reconnect, label keys are kept (they apply from the first frame after
  the reconnect), and `--duration` and `q` still end the recording.
- Recovering a session with `logger.py --replay X.bin --out X.parquet` no longer empties
  `X.labels.jsonl`; the events are written back to the new sidecar.
- The logger now reconnects after an unplug on Linux and macOS too. pyserial raises a plain
  `OSError` there, which wasn't caught, so the logger exited.
- The breath rate only uses intervals between consecutive detected breaths with no artifact or
  gap sample anywhere between them. Before, a breath hidden by an artifact or a data gap left one
  double-length interval, reported as half the true rate.
- A channel-layout change during replay or recording now stops cleanly and still saves the data
  received before it. Previously the samples could be lost if the change came in the same read
  as the first header.
- Chest–abdomen phase is no longer reported for samples that are themselves in a motion artifact
  or data gap.
- Live view: the ring buffer kept the wrong order after a batch longer than the window;
  `--screenshot` now works however the window is closed; a layout change or a lost serial port
  shows a persistent message instead of failing on every refresh. Attached to the logger, it no
  longer starts a spurious segment every 2 s when the logger re-announces the header (the
  re-announced copy now carries no device timestamp).
- Firmware: `KMAN_I2C_SDA_PIN`/`KMAN_I2C_SCL_PIN` overrides now set the I2C pins (they only
  affected bus recovery).
- Simulator: smoothed noise no longer swings far more near the start and end of a session
  (reflect instead of edge padding). The sample session and screenshots were regenerated.
- `report.py` rejects `--start` without `--end` (and vice versa) instead of ignoring it.

## [0.1.0] - 2026-10-07

Initial implementation: timer-master firmware for the XIAO nRF52840 with LSM6DSOX and flex
sensor, binary framing with CRC and JSON session header, logger with keyboard labelling,
live viewer, device simulator, offline analysis (breathing, chest–abdomen phase, swallow and
head-motion events) and session report.
