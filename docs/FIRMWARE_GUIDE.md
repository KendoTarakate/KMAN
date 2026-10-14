# Firmware guide: flash, check, configure, update

How to get the KMAN firmware onto a **Seeed Studio XIAO nRF52840** (plain or Sense) and confirm
it works. You need a USB-C **data** cable; many charging cables have no data wires.

| I want to… | Use |
|---|---|
| Just flash it, no programming tools | [Method 1: drag-and-drop UF2](#method-1-drag-and-drop-uf2-no-tools) |
| Change settings (pins, rate, sensors) or develop | [Method 2: VS Code + PlatformIO](#method-2-vs-code--platformio) |
| Script it / headless machine | [Method 3: PlatformIO command line](#method-3-platformio-command-line) |
| Use a J-Link / DAPLink probe | [Method 4: SWD programmer](#method-4-swd-programmer-hex) |

After flashing, always run [Check that it works](#check-that-it-works).

---

## 1. Pick the firmware variant

Each variant is the same firmware, built for a different set of wired sensors. A sensor listed
for a variant but not connected is reported as failed (solid red LED) while the rest keep
streaming, so pick the one that matches your wiring.

| Image file (version 1.0.0) | Chest IMU 0x6A | Neck flex (A0) | Abdomen IMU 0x6B | Neck IMU 0x6B |
|---|:-:|:-:|:-:|:-:|
| `kman-fw-1.0.0-xiao_nrf52840.uf2` (bench default) | ✓ | ✓ | | |
| `kman-fw-1.0.0-xiao_nrf52840_abdomen.uf2` | ✓ | ✓ | ✓ | |
| `kman-fw-1.0.0-xiao_nrf52840_neck.uf2` | ✓ | ✓ | | ✓ |

The part after the version is the variant name, which is also the PlatformIO environment name.

Wiring is in the [README](../README.md#hardware--wiring-bench-prototype). The second IMU uses
address 0x6B: tie its SA0/SDO pin to 3V3.

---

## Method 1: drag-and-drop UF2 (no tools)

The XIAO's bootloader shows up as a USB drive. Copying a `.uf2` file onto that drive flashes it.

1. **Download** `kman-fw-<version>-<variant>.uf2` from the
   [Releases page](https://github.com/KendoTarakate/KMAN/releases) (under *Assets*).
   *No release yet?* Open the latest green run under
   [Actions → CI](https://github.com/KendoTarakate/KMAN/actions/workflows/ci.yml) and download the
   `firmware-<variant>` artifact (a zip containing the `.uf2`). You need to be signed in to GitHub.
2. **Connect** the XIAO to the computer.
3. **Enter the bootloader:** press the tiny **RESET** button twice, quickly (like a double-click).
   It is next to the USB-C connector, on the same side. The LED starts pulsing and a USB drive
   appears, named `XIAO-SENSE` on most boards.
   * Nothing appears? Try a faster double-tap, another cable (data, not charge-only) or another USB port.
4. *(Optional, first time)* open `INFO_UF2.TXT` on that drive and check it lists **SoftDevice S140
   7.x** (Seeed boards ship with 7.3.0). The images are built for that layout. If yours lists S140
   6.x, update the bootloader first ([Seeed wiki](https://wiki.seeedstudio.com/XIAO_BLE/)).
5. **Copy** the `.uf2` onto the drive (drag it in Finder/Explorer or use `cp`). The drive disappears
   by itself within a few seconds: the board has flashed and restarted.
   * macOS may warn "Disk Not Ejected Properly" and Windows may report the copy failed at the very end.
     Both are expected, because the board restarts before the computer finishes the copy.
6. Go to [Check that it works](#check-that-it-works).

---

## Method 2: VS Code + PlatformIO

1. Install [Visual Studio Code](https://code.visualstudio.com/) and [Git](https://git-scm.com/downloads).
   PlatformIO uses Git to download the pinned nRF52 board support, even if you got the code as a ZIP.
   Then in VS Code open **Extensions** (Ctrl/Cmd+Shift+X), search **PlatformIO IDE** and install it.
   Restart VS Code when asked.
2. Get the code: `git clone https://github.com/KendoTarakate/KMAN.git`, or *Code → Download ZIP*
   on GitHub and unzip it.
3. **File → Open Folder…** and choose the **`KMAN/firmware`** folder itself, the one that contains
   `platformio.ini`. PlatformIO only finds a project at the top of an opened folder. If you opened
   the repository root, use **File → Add Folder to Workspace… → firmware**.
4. Click the PlatformIO (ant) icon in the left bar. Under **Project Tasks**, expand the variant you need
   (e.g. `xiao_nrf52840`) and click **Build**. The first build downloads the toolchain and board
   support (a few hundred MB) and takes a few minutes; later builds take seconds.
5. Connect the board and click **Upload** (same list). PlatformIO resets the board into its bootloader
   and flashes it.
   * *"Error: Please specify `upload_port`"* or *"failed to touch serial port"*: double-tap RESET,
     wait for the drive to appear, then click Upload again.
   * Close anything that holds the serial port first: `logger.py`, `live_view.py --port`, the
     PlatformIO Serial Monitor.
6. Every build also writes the drag-and-drop file to
   `firmware/.pio/build/<variant>/kman-fw-<version>-<variant>.uf2`, so you can flash other boards with Method 1.

**Don't use the Serial Monitor to "see" the data.** The firmware sends binary frames, which look like
garbage there. Use `host/logger.py` or `host/live_view.py`.

---

## Method 3: PlatformIO command line

Needs Python 3 and [Git](https://git-scm.com/downloads) (PlatformIO fetches the pinned nRF52
board support with Git).

```bash
python -m pip install platformio
cd KMAN
pio run -d firmware -e xiao_nrf52840                 # build only
pio run -d firmware -e xiao_nrf52840 -t upload       # build + flash
pio run -d firmware -e xiao_nrf52840 -t upload --upload-port COM5    # if several boards are connected
pio device list                                      # find the port
```
Without `-e`, PlatformIO uses the default environment (`xiao_nrf52840`). Build all three variants with
`pio run -d firmware -e xiao_nrf52840 -e xiao_nrf52840_abdomen -e xiao_nrf52840_neck`.

---

## Method 4: SWD programmer (.hex)

`kman-fw-<version>-<variant>.hex` holds the application only, at address `0x27000`. The board's
bootloader and SoftDevice S140 7.x must already be on the chip; they are on every XIAO nRF52840
from the factory. Flash the `.hex` without a full-chip erase, e.g. `nrfjprog --program
kman-fw-….hex --sectorerase --reset`, or the equivalent in J-Flash / pyOCD. A full erase removes
the bootloader and makes Methods 1–3 unusable until you restore it.

---

## Check that it works

1. **LED:** about one second after power-up the green LED **blips once every 2 s**. That means
   it's running and waiting for the computer.
   **Solid red** means a configured sensor didn't respond (step 3 tells you which).
2. **Port:** `python host/logger.py --list-ports` lists the board (USB ID `2886:8044`, described as
   XIAO nRF52840 or *USB Serial Device*). Windows: `COM…`; Linux: `/dev/ttyACM…`;
   macOS: `/dev/cu.usbmodem…`.
3. **Stream:** `python host/logger.py --port <port> --duration 30 --out data/flash_check.parquet`
   * The `[header]` block shows `fw=1.0.0`, `clock: HFXO 32 MHz crystal`, and `ok` for every sensor you
     wired. A sensor marked `init_failed` comes with the reason (e.g. *no I2C ACK*: check wiring and
     address).
   * The status line settles at **100.0 Hz, lost 0**, and the green LED blinks steadily (2 Hz) while recording.
4. Optional: `python host/live_view.py --port <port>` shows the signals live. Breathe slowly with the
   IMU on your sternum and you should see the waveform.

The full bring-up and acceptance tests (including the 30-minute soak) are in the README:
[Testing on hardware](../README.md#testing-on-hardware-stage-by-stage).

---

## Change settings

The prebuilt images use the defaults in `firmware/include/config.h`: 100 Hz, ±2 g, ±250 dps,
I2C on D4/D5, flex on A0. To change anything, add flags to the variant's `build_flags` in
`firmware/platformio.ini` and flash with Method 2 or 3. For example:

```ini
[env:xiao_nrf52840]
build_flags =
    -DKMAN_BOARD_NAME=\"xiao_nrf52840\"
    -DKMAN_SAMPLE_RATE_HZ=200            ; must divide 1,000,000; max 416
    -DKMAN_FLEX_R_FIXED_OHM=33000        ; metadata only: recorded in each session header
    '-DKMAN_FLEX_SENSOR_DESC="Spectra Symbol 2.2in"'
```

A text value that contains spaces needs the whole flag in single quotes, as in the last line.
Without them PlatformIO splits it at the spaces and the build fails.

Every option, with what it does, is listed in `config.h` and in
[CODE_GUIDE.md §5](CODE_GUIDE.md#5-firmware-main-loop-config-build). Invalid combinations
(e.g. two IMUs on the same address) stop the build with a clear error.

---

## Update to a new version

Flash the new image the same way. Recordings on your computer are unaffected. Each session file
stores the firmware version that produced it (`fw_version` in the header), so older recordings
stay identifiable.

## Troubleshooting

| Symptom | Fix |
|---|---|
| No drive appears after double-tapping RESET | Use a data cable; tap faster; try another port; avoid hubs. |
| Copy "fails" or "Disk not ejected" message at the end | Expected, because the board restarts once flashed. Check the LED. |
| Upload in PlatformIO can't find the port | Double-tap RESET, then Upload again. Close the logger, live view and serial monitor. |
| `Please install Git client` on the first PlatformIO build | Install [Git](https://git-scm.com/downloads), restart VS Code / the terminal, build again. |
| Linux: permission denied on `/dev/ttyACM0` | `sudo usermod -aG dialout $USER` then log out and back in; for PlatformIO uploads also install its [udev rules](https://docs.platformio.org/en/latest/core/installation/udev-rules.html). |
| Green LED never blips after flashing | Double-tap RESET and flash again. Make sure the file is a `kman-fw-…uf2` for the nRF52840. |
| Solid red LED | A configured sensor isn't answering. Run the logger: the header names it and gives the reason. Wrong variant? Re-flash the right one. |
| `NO-XTAL` in the logger status line | Timing is running on the internal RC oscillator, which is inaccurate. Re-plug; if it persists, report it. |
| Want the original Seeed demo back | Flash any other `.uf2` or Arduino sketch the same way; KMAN leaves the bootloader untouched. |
