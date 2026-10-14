# KMAN code guide

What every part of the Step 1 code does and how each function works. Sections
follow the data path: firmware → wire protocol → host decoding and recording →
live view → offline analysis → tests. Every entry was written from the current
source and then checked against it by an independent reviewer pass. Line
numbers refer to version 1.0.0, the release this guide ships with.


## Contents

- [1. The system in one picture](#1-the-system-in-one-picture)
- [2. One sample, end to end](#2-one-sample-end-to-end)
- [3. Ideas that recur everywhere](#3-ideas-that-recur-everywhere)
- [4. Changes in 1.0](#4-changes-in-10)
- [5. Firmware: main loop, config, build](#5-firmware-main-loop-config-build)
- [6. Firmware: hardware abstraction (nRF52840)](#6-firmware-hardware-abstraction-nrf52840)
- [7. Firmware: sensor driver interface and drivers](#7-firmware-sensor-driver-interface-and-drivers)
- [8. Firmware: wire protocol, TX ring buffer, JSON writer, native test harness](#8-firmware-wire-protocol-tx-ring-buffer-json-writer-native-test-harness)
- [9. Host: protocol decoder and labels](#9-host-protocol-decoder-and-labels)
- [10. Host: stream decoder (unwrapping, gaps, segments, restarts, labels)](#10-host-stream-decoder-unwrapping-gaps-segments-restarts-labels)
- [11. Host: session files, UDP bus, keyboard input](#11-host-session-files-udp-bus-keyboard-input)
- [12. Host: command-line tools (logger, CSV export, simulated device)](#12-host-command-line-tools-logger-csv-export-simulated-device)
- [13. Host: synthetic physiology simulator](#13-host-synthetic-physiology-simulator)
- [14. Host: live viewer](#14-host-live-viewer)
- [15. Analysis: config, preprocessing, breathing](#15-analysis-config-preprocessing-breathing)
- [16. Analysis: phase, neck events, pipeline, plotting, report](#16-analysis-phase-neck-events-pipeline-plotting-report)
- [17. Build and release automation](#17-build-and-release-automation)
- [18. Tests: protocol, stream, C++ cross-check](#18-tests-protocol-stream-c-cross-check)
- [19. Tests: 1.0 regression tests and analysis tests](#19-tests-10-regression-tests-and-analysis-tests)

## 1. The system in one picture

```
 XIAO nRF52840 (firmware/)                              PC (host/, analysis/)
 ─────────────────────────                              ─────────────────────
 TIMER4 @1 MHz ──compare every 10 ms──► ISR             logger.py
   (crystal timebase)                   records tick,     Recorder.feed()
                                        wakes loop          ├─ SessionWriter.write_raw()  → X.bin (every byte)
 loop():                                                    ├─ StreamDecoder.feed()
   serviceHost()   DTR / 'H' / 'S'                          │    FrameParser.feed()  sync+len+CRC, resync
   sampleAndQueue()                                         │    _on_header()  layout, segments
     FlexAdc::read()   SAADC, 14-bit                        │    _on_data()    unwrap t/seq, count gaps, label
     Lsm6dsox::read()  12-byte I2C burst                    │    drain()       → column batch
     encodeFrame()     AA 55 len type body CRC16            ├─ SessionWriter.write_batch() → X.parquet
     ByteRing.push()   8 KB TX ring                         └─ UdpSender.send()  frames → live_view.py
   queueStatusIfDue()  STATUS once per second          keys 0-9 → StreamDecoder.set_label() → X.labels.jsonl
   pumpTx()  ──USB CDC (non-blocking)──────────────►
                                                        analysis/report.py
                                                          load_session() → analyze():
                                                            to_uniform()  tick grid, gaps, physical units
                                                            motion_energy_mg()
                                                            analyze_breathing()  chest (and abdomen)
                                                            chest_abdomen_phase()  if abdomen IMU
                                                            analyze_neck()  swallow / head-motion events
                                                            per_label_summary(), quality()
                                                          make_figure() → reports/X.png + CSVs
```

## 2. One sample, end to end

1. **Tick.** `TIMER4` counts microseconds from the 32 MHz crystal. When it
   reaches `CC[0]`, `TIMER4_IRQHandler` records the scheduled time and the tick
   count, moves `CC[0]` one period ahead, and wakes the loop task. The timestamp
   is the *scheduled* time, so it has no interrupt jitter.
2. **Sample.** `loop()` wakes in `hal::waitForTick()`, reads the tick with
   `hal::latestTick()` (a torn-read-safe pair of loads), and calls
   `sampleAndQueue()`. That reads the flex ADC first (≈0.2 ms), then the IMU's
   latest registers (≈0.35 ms), so all channels sit within about 1 ms of the tick.
3. **Frame.** The body is `t_us u32 | seq u16 | valid u8 | int16 channels`.
   `seq` is the low 16 bits of the tick counter, so any tick that never becomes a
   received frame shows up on the host as a gap. `encodeFrame()` adds
   `AA 55 | len | type` and a CRC-16 over len+type+body.
4. **Transmit.** The frame goes into an 8 KB ring (`ByteRing::push`, all or
   nothing). `pumpTx()` copies only as much as the USB CDC FIFO can take, so the
   loop never blocks on USB. If the ring is full the frame is dropped and
   counted (`tx_drops`).
5. **Receive.** On the PC, `logger.py` reads whatever bytes arrived and passes
   them to `Recorder.feed()`. The raw bytes are appended to `X.bin` first, then
   `FrameParser.feed()` finds complete, CRC-valid frames.
6. **Decode.** `StreamDecoder._on_data()` decodes the body with a numpy dtype
   built from the session header, unwraps the 32-bit time and 16-bit seq, counts
   missing ticks from the time step, and tags the row with the current label.
7. **Store.** Every 100 rows `drain()` hands a column batch to
   `SessionWriter.write_batch()`, which writes Parquet row groups. At the end
   `close()` stores the header(s), label events and statistics as file metadata.
8. **Analyse.** `report.py` loads the file, puts samples on a uniform tick grid
   (`to_uniform`), extracts the breathing waveform and breaths, the chest-abdomen
   phase (if there is an abdomen IMU), and swallow/head-motion events, then
   summarises per label and draws the figure.

## 3. Ideas that recur everywhere

* **Timer-master timebase.** One crystal clock drives every channel. The IMU
  free-runs faster (208 Hz) and is sampled on each tick, so there is no second
  clock domain to align.
* **The header defines the layout.** The device describes its sensors and the
  exact channel order/scales in a JSON header. The host never hard-codes the
  frame layout; a sensor that failed init is simply absent from it.
* **Restarts start a new segment.** A reboot (new `boot_id`), re-opened port (new
  `stream_id`) or restarted device clock starts a new segment, and recording continues.
* **Loss is always measurable.** Device-side counters (STATUS frame) and
  host-side gap counting (from timestamps) together tell you where frames were lost.
* **Labels apply from the next frame.** A key press creates an event for the next
  frame, recorded both as `seq` and as (segment, offset within the segment). Replay
  applies events by segment and offset, so a session rebuilt from `X.bin` gets the
  same label column even after a reconnect, whose gap live recording bridges with the PC clock.
* **Calibration-relative thresholds.** The first rest-breathing segment
  (label 1) is the per-session quiet reference: gyro bias, flex noise σ,
  abdomen polarity and phase amplitude gates all come from it.
* **No heap in the firmware.** All buffers are static and sized at compile time.

## 4. Changes in 1.0

The review that produced the first version of this guide found ten places where
the code didn't do what the README promised. All are fixed in 1.0 and each has a
regression test (`host/tests/test_v1_fixes.py`). The most important ones:

* **Device restarts.** Re-plugging the cable reboots the XIAO, and its clock and
  `stream_id` start again. `StreamDecoder._new_stream()` now treats a header as a
  new stream when `device_id`, `boot_id` (a random number the firmware draws at
  power-up) or `stream_id` changes, or when the header's timestamp doesn't follow
  the running stream. `_on_data()` also starts a segment when the data clock jumps
  back by more than 1 s. Before 1.0, every frame after a re-plug was discarded as a
  "duplicate".
* **Replay keeps the labels.** `logger.py --replay` writes the label events back to
  the new `.labels.jsonl`, so recovering a session no longer empties that file.
* **Reconnect on Linux/macOS.** `run_live()` catches the plain `OSError` that
  pyserial raises there after an unplug.
* **Breath rate.** `detect_breaths()` takes intervals between consecutive detected
  breaths whose both ends are valid, so one breath during an artifact no longer
  produces a half-rate value.

The full list is in [CHANGELOG.md](../CHANGELOG.md).

## 5. Firmware: main loop, config, build

- **`firmware/src/main.cpp`**: The firmware application for the XIAO nRF52840. It holds the sensor slot table, every static buffer and the run state, and defines the Arduino entry points setup() and loop(). It implements timer-master sampling (at most one DATA frame per hardware-timer tick), builds the JSON session header, sends periodic STATUS counters, drains the TX ring to USB CDC and drives the status LEDs. Nearly all MCU-specific work goes through hal::. The exceptions are the Adafruit_TinyUSB.h include, which the core needs to link USB CDC, and the direct Wire.setPins()/begin()/setClock() calls in initSensors(); Wire.setPins() is a core extension, not standard Arduino TwoWire. The code never allocates memory dynamically.
- **`firmware/include/config.h`**: Compile-time configuration. Each KMAN_\* macro sits inside an #ifndef guard, so a -D build flag in platformio.ini overrides it. The settings cover firmware identity, frame rate, which sensor slots are fitted and their I2C addresses, IMU ranges, I2C pins and clock, LED pins and polarity, SAADC settings, TX ring size and status period. The file ends with static_asserts on the sample rate and on I2C address collisions. main.cpp and hal_nrf52.cpp include it. scripts/make_uf2.py and release.yml read the version string from it.
- **`firmware/platformio.ini`**: The PlatformIO project file. It sets the default environment and shared settings (Arduino framework, warning flags for project sources, and the post-build UF2 script). It also pins the nordicnrf52 platform and the Seeed Adafruit-based Arduino core to exact git commits. Three environments, one per sensor wiring, differ only in which KMAN_\* slot macro they switch on.

**How it connects.**

The Adafruit/Seeed Arduino core calls setup() once and then loop() repeatedly from its FreeRTOS loop task. No ISR is defined in this group. TIMER4_IRQHandler in firmware/src/hal/hal_nrf52.cpp advances s_tickCount and s_tickTimeUs on the exact period grid. It wakes loop() with vTaskNotifyGiveFromISR, which unblocks hal::waitForTick(), and loop() then reads the tick with hal::latestTick(). main.cpp calls into the following:
- hal:: functions: init, ledInit, led, startSampleTimer, waitForTick, latestTick, nowUs, clockIsCrystal, describeClock, deviceId, bootId, mcuName, i2cBusRecover, hostConnected, hostRead, hostWritable, hostWrite, hostFlush.
- The Arduino Wire object directly (setPins, begin, setClock) and millis().
- The Sensor virtual interface implemented by Lsm6dsox and FlexAdc: init, read, channelCount, channel, describeConfig, lastError, warning. FlexAdc in turn uses hal::adcInit, adcRead, adcFullScale and describeAdc.
- JsonWriter for the header.
- proto::encodeFrame, proto::encodeStatus and the putU8/putU16/putU32/putI16 helpers.
- ByteRing (push, free, used, peekContiguous, consume, clear, highWater, resetHighWater).

The frames travel over USB CDC to the host. In host/kman_host/protocol.py, FrameParser.feed splits them, and SessionHeader.from_json, decode_data and decode_status decode them. In host/kman_host/stream.py, StreamDecoder._on_header/_new_stream (device_id, boot_id, stream_id, t_us_at_header) and _on_data (t_us, seq gaps) consume them. host/logger.py open_port() and host/live_view.py SerialSource write CMD_SEND_HEADER (b"H") right after opening the port. Opening the port also asserts DTR, which triggers startStreaming(). The logger re-sends 'H' every 2 s until it has a header. host/sim_device.py and host/kman_host/simulator.py imitate this device's frames. config.h is also included by hal_nrf52.cpp (ADC and LED-polarity macros). The build side works as follows:
- platformio.ini build_flags override config.h macros.
- scripts/make_uf2.py (extra_scripts) reads KMAN_FW_VERSION from config.h to name the images.
- The firmware job in .github/workflows/ci.yml builds all three environments with `pio run -d firmware -e <env>` and uploads kman-fw-\*.uf2/.hex.
- .github/workflows/release.yml checks that the release tag equals KMAN_FW_VERSION.

### `firmware/include/config.h`

#### `identity settings`
*config* · [`firmware/include/config.h:8`](../firmware/include/config.h#L8)

```cpp
#define KMAN_FW_VERSION "1.0.0" / #define KMAN_BOARD_NAME "unknown"
```

**Purpose.** Firmware version and board name strings that identify the build in the session header.

**How it works.** KMAN_FW_VERSION defaults to "1.0.0" and goes into the header as "fw_version". KMAN_BOARD_NAME defaults to "unknown" and goes in as "board". All platformio.ini environments set it to "xiao_nrf52840" via -DKMAN_BOARD_NAME=\"xiao_nrf52840\".

**Notes.** scripts/make_uf2.py reads KMAN_FW_VERSION from the config.h text with the regex #define\s+KMAN_FW_VERSION\s+"([^"]+)" to name kman-fw-&lt;version>-&lt;env>.uf2/.hex, and release.yml checks that the pushed tag matches it. A -DKMAN_FW_VERSION build-flag override changes the header string but not the image file name. The board name is the same for all three environments.

#### `sampling settings`
*config* · [`firmware/include/config.h:19`](../firmware/include/config.h#L19)

```cpp
#define KMAN_SAMPLE_RATE_HZ 100
```

**Purpose.** The frame rate of the whole system: one DATA frame per hardware-timer tick.

**How it works.** Defaults to 100 Hz. main.cpp derives kPeriodUs = 1e6 / rate (10000 us) and writes "sample_rate_hz". Each Lsm6dsox constructor uses it to choose an ODR of at least 2x the rate from 208/416/833 Hz (208 Hz at 100 Hz).

**Notes.** The comment notes that the timer runs at 1 MHz, so the period must be a whole number of microseconds. The static_asserts enforce this and the 1..416 Hz range.

#### `sensor slot settings`
*config* · [`firmware/include/config.h:26`](../firmware/include/config.h#L26)

```cpp
KMAN_CHEST_IMU, KMAN_CHEST_IMU_ADDR, KMAN_ABDOMEN_IMU, KMAN_ABDOMEN_IMU_ADDR, KMAN_NECK_FLEX, KMAN_FLEX_PIN, KMAN_FLEX_SENSOR_DESC, KMAN_FLEX_R_FIXED_OHM, KMAN_FLEX_TOPOLOGY, KMAN_NECK_IMU, KMAN_NECK_IMU_ADDR, KMAN_NECK_IMU_ACCEL_ONLY
```

**Purpose.** Selects which sensor slots are fitted, and sets their I2C addresses and flex-divider metadata.

**How it works.**

Fitted flags (1 = probed at boot; a failure is reported in the header and the slot is left out of the frame layout; 0 = not fitted):
- KMAN_CHEST_IMU 1, at KMAN_CHEST_IMU_ADDR 0x6A (LSM6DSOX SA0/SDO low).
- KMAN_ABDOMEN_IMU 0, at KMAN_ABDOMEN_IMU_ADDR 0x6B (SA0 high).
- KMAN_NECK_FLEX 1, on KMAN_FLEX_PIN A0 (XIAO D0 = P0.02 = AIN0).
- KMAN_NECK_IMU 0, at KMAN_NECK_IMU_ADDR 0x6B. KMAN_NECK_IMU_ACCEL_ONLY 0 streams accel+gyro (6 ch); 1 streams accel only (3 ch).

Flex metadata (written to the header only): KMAN_FLEX_SENSOR_DESC ("flex sensor (edit KMAN_FLEX_SENSOR_DESC)"), KMAN_FLEX_R_FIXED_OHM 47000, KMAN_FLEX_TOPOLOGY "3V3 - flex - ADC - R_fixed - GND". These feed the g_slots configured flags and the driver constructors in main.cpp.

**Notes.** The comment explains that the optional neck IMU exists for head-pose rejection. Because the LSM6DSOX has only two addresses, on one bus it can share with the chest IMU only if the abdomen IMU is not fitted. The address static_asserts enforce this.

#### `IMU settings`
*config* · [`firmware/include/config.h:73`](../firmware/include/config.h#L73)

```cpp
#define KMAN_IMU_ACCEL_RANGE_G 2 / #define KMAN_IMU_GYRO_RANGE_DPS 250
```

**Purpose.** Full-scale ranges applied to every LSM6DSOX slot. The defaults are the small-motion ranges.

**How it works.** Accel defaults to 2 g and gyro to 250 dps, both passed to each Lsm6dsox constructor. The driver maps them to FS bits and per-LSB scale: accel 61/122/244/488 ug/LSB for 2/4/8/16 g; gyro 4375/8750/17500/35000/70000 udps/LSB for 125/250/500/1000/2000 dps. These scales appear in the header as each channel's "scale" (e.g. 61e-6, 8750e-6).

**Notes.** Only the listed values are supported. The driver's switch statement silently falls back to 2 g or 250 dps for any other value and reports the fallback range in describeConfig. No static_assert checks these values.

#### `pin / bus settings`
*config* · [`firmware/include/config.h:81`](../firmware/include/config.h#L81)

```cpp
#define KMAN_I2C_SDA_PIN PIN_WIRE_SDA / #define KMAN_I2C_SCL_PIN PIN_WIRE_SCL / #define KMAN_I2C_HZ 400000
```

**Purpose.** I2C pins and bus clock for the IMUs.

**How it works.** SDA defaults to the variant's PIN_WIRE_SDA (XIAO D4), SCL to PIN_WIRE_SCL (XIAO D5), and the clock to 400 kHz. initSensors() uses them for hal::i2cBusRecover(), Wire.setPins() and Wire.setClock().

**Notes.** The comment in sampleAndQueue() estimates a 12-byte IMU burst read at ~0.35 ms at 400 kHz.

#### `LED settings`
*config* · [`firmware/include/config.h:93`](../firmware/include/config.h#L93)

```cpp
#define KMAN_LED_OK_PIN LED_GREEN / #define KMAN_LED_ERR_PIN LED_RED / #define KMAN_LED_ACTIVE_LOW 1
```

**Purpose.** Status LED pins and polarity: a blinking OK LED means streaming, and a solid ERR LED means a sensor error.

**How it works.** OK defaults to LED_GREEN and ERR to LED_RED. KMAN_LED_ACTIVE_LOW is 1 because the XIAO RGB LED is common-anode. hal::led() writes digitalWrite(pin, (on != ACTIVE_LOW) ? HIGH : LOW), so 'on' drives the pin LOW. updateLeds() and setup() use the pins.

**Notes.** For single-LED boards the comment says to point both macros at the same pin. updateLeds() then gives the error state priority.

#### `ADC settings`
*config* · [`firmware/include/config.h:104`](../firmware/include/config.h#L104)

```cpp
#define KMAN_ADC_BITS 14 / #define KMAN_ADC_OVERSAMPLE 16 / #define KMAN_ADC_TACQ_US 10
```

**Purpose.** nRF52840 SAADC configuration for the flex divider channel.

**How it works.** KMAN_ADC_BITS 14 (allowed 8/10/12/14; 14 is meaningful with oversampling). KMAN_ADC_OVERSAMPLE 16 (a power of two from 1 to 256; burst mode, ~12 us per sample). KMAN_ADC_TACQ_US 10 (allowed 3/5/10/15/20/40; 10 us suits source impedance &lt;= 100 kOhm). hal::adcInit() applies them via analogReference(AR_VDD4), analogReadResolution, analogOversampling and analogSampleTime. hal::adcFullScale() = 2^bits - 1 (16383), and hal::describeAdc() writes all three to the flex slot's header entry.

**Notes.** FlexAdc::init() flags a rail warning when all 8 probe reads fall within fullScale/50 of either rail.

#### `transport settings`
*config* · [`firmware/include/config.h:115`](../firmware/include/config.h#L115)

```cpp
#define KMAN_TX_RING_BYTES 8192 / #define KMAN_STATUS_PERIOD_MS 1000
```

**Purpose.** Size of the outgoing frame buffer and the STATUS frame interval.

**How it works.** KMAN_TX_RING_BYTES 8192 sets the capacity of ByteRing g_tx in main.cpp. The comment describes it as ~2.5 s of frames at 100 Hz to absorb host/USB stalls; a default-build DATA frame is 28 B. KMAN_STATUS_PERIOD_MS 1000 is the minimum gap queueStatusIfDue() keeps between unrequested STATUS frames.

**Notes.** The ring must hold one whole HEADER frame (~1.7-3.1 KB of JSON + 7 B). No static_assert checks this.

#### `static_asserts (sanity)`
*config* · [`firmware/include/config.h:123`](../firmware/include/config.h#L123)

```cpp
static_assert(...) x5
```

**Purpose.** Rejects at compile time a frame rate that the 1 MHz timer cannot divide exactly, or that the IMU ODR table cannot cover (2x the rate must be &lt;= 833 Hz). It also rejects any pair of enabled IMU slots that share an I2C address.

**How it works.**

1. 1000000UL % KMAN_SAMPLE_RATE_HZ == 0: "KMAN_SAMPLE_RATE_HZ must divide 1e6".
2. 1 &lt;= KMAN_SAMPLE_RATE_HZ &lt;= 416: "IMU ODR is chosen as >= 2x the frame rate, max 833 Hz".
3. Abdomen and neck IMUs both enabled at the same address is rejected.
4. The same check for chest and abdomen.
5. The same check for chest and neck.
Each address check fires only when both slots of the pair are enabled.

**Notes.** With the default addresses (chest 0x6A, abdomen 0x6B, neck 0x6B), enabling both abdomen and neck fails to compile unless one address is overridden. Ranges, ADC values and ring size are not checked here.

### `firmware/platformio.ini`

#### `[platformio] default_envs`
*config* · [`firmware/platformio.ini:16`](../firmware/platformio.ini#L16)

```
[platformio]
default_envs = xiao_nrf52840
```

**Purpose.** Picks the environment that a bare `pio run` builds.

**How it works.** Sets default_envs to xiao_nrf52840, the bench default of chest IMU + neck flex. Other environments must be selected with -e.

#### `[env] (shared settings)`
*config* · [`firmware/platformio.ini:19`](../firmware/platformio.ini#L19)

```
[env]
framework = arduino
build_src_flags = ...
extra_scripts = post:scripts/make_uf2.py
```

**Purpose.** Settings that every environment inherits.

**How it works.** framework = arduino. build_src_flags and extra_scripts are described in their own entries. Every [env:\*] section inherits these keys automatically.

#### `build_src_flags`
*config* · [`firmware/platformio.ini:22`](../firmware/platformio.ini#L22)

```
build_src_flags = -Wall -Wextra -Wno-unused-parameter
```

**Purpose.** Turns on strict warnings for the project's own sources only.

**How it works.** PlatformIO applies build_src_flags only to files under src/, not to the framework or libraries. -Wall and -Wextra enable warnings, and -Wno-unused-parameter silences one class.

**Notes.** From the comments: the framework is not -Wextra clean, and SoftDevice headers trigger unused-parameter warnings.

#### `extra_scripts`
*config* · [`firmware/platformio.ini:26`](../firmware/platformio.ini#L26)

```
extra_scripts = post:scripts/make_uf2.py
```

**Purpose.** Adds a post-build step that writes drag-and-drop UF2 and named HEX images for every build.

**How it works.** The 'post:' script runs after the main SCons script and registers env.AddPostAction("$BUILD_DIR/${PROGNAME}.hex", make_uf2). After firmware.hex is linked, make_uf2 runs the framework package's tools/uf2conv/uf2conv.py with -c -f 0xADA52840 -o kman-fw-&lt;KMAN_FW_VERSION>-&lt;PIOENV>.uf2 and copies the hex to kman-fw-&lt;version>-&lt;env>.hex, both in .pio/build/&lt;env>/.

**Notes.** The UF2 is flashed by copying it to the Adafruit nRF52 bootloader drive (double-tap RESET). The CI firmware job uploads both files as artifacts.

#### `[env:xiao_nrf52840]`
*config* · [`firmware/platformio.ini:28`](../firmware/platformio.ini#L28)

```
[env:xiao_nrf52840]
platform = ...
platform_packages = ...
board = xiaoble_adafruit
build_flags = -DKMAN_BOARD_NAME=\"xiao_nrf52840\"
```

**Purpose.** The base environment for the Seeed XIAO nRF52840 using the non-mbed, Adafruit-based Seeed core (TinyUSB CDC + FreeRTOS). It builds the default sensor set.

**How it works.** Uses the pinned platform and core (next entry) and board = xiaoble_adafruit. build_flags defines KMAN_BOARD_NAME as the string literal "xiao_nrf52840" (the quotes are escaped). Every sensor macro keeps its config.h default: chest IMU at 0x6A plus neck flex, 7 channels.

**Notes.** The other two environments extend this one.

#### `pinned platform / platform_packages`
*config* · [`firmware/platformio.ini:32`](../firmware/platformio.ini#L32)

```
platform = https://github.com/maxgerhardt/platform-nordicnrf52#cac6fcf943a41accd2aeb4f3659ae297a73f422e
platform_packages = platformio/framework-arduinoadafruitnrf52-seeed @ https://github.com/maxgerhardt/framework-arduinoadafruitnrf52-seeed.git#3ae21c3c6c544fed6e949f00986324dd95fb2b89
```

**Purpose.** Pins the build platform and the Seeed Arduino core to known-good git commits so builds are reproducible.

**How it works.** platform takes maxgerhardt/platform-nordicnrf52 at commit cac6fcf9... . platform_packages replaces the package framework-arduinoadafruitnrf52-seeed with maxgerhardt/framework-arduinoadafruitnrf52-seeed at commit 3ae21c3c... .

**Notes.** The comment explains that the platform alone would pull the core's latest commit, and that Git must be installed. make_uf2.py finds uf2conv.py through this package's directory. The CI PlatformIO cache key hashes platformio.ini, so changing a pin invalidates the cache.

#### `[env:xiao_nrf52840_abdomen]`
*config* · [`firmware/platformio.ini:39`](../firmware/platformio.ini#L39)

```
[env:xiao_nrf52840_abdomen]
extends = env:xiao_nrf52840
build_flags = ${env:xiao_nrf52840.build_flags} -DKMAN_ABDOMEN_IMU=1
```

**Purpose.** Build for chest IMU (0x6A) + abdomen IMU (0x6B) + neck flex.

**How it works.** Inherits platform, packages and board from env:xiao_nrf52840. It re-includes the parent's build_flags by interpolation, because overriding build_flags replaces the inherited value, then adds -DKMAN_ABDOMEN_IMU=1. The result has 13 channels (6 + 6 + 1).

#### `[env:xiao_nrf52840_neck]`
*config* · [`firmware/platformio.ini:45`](../firmware/platformio.ini#L45)

```
[env:xiao_nrf52840_neck]
extends = env:xiao_nrf52840
build_flags = ${env:xiao_nrf52840.build_flags} -DKMAN_NECK_IMU=1
```

**Purpose.** Build for chest IMU (0x6A) + neck IMU (0x6B) + neck flex.

**How it works.** Extends env:xiao_nrf52840, re-includes its build_flags and adds -DKMAN_NECK_IMU=1. The neck IMU uses KMAN_NECK_IMU_ADDR 0x6B and, with KMAN_NECK_IMU_ACCEL_ONLY 0, streams 6 channels, for 13 in total.

**Notes.** The abdomen IMU stays off (default 0), so the abdomen/neck address static_assert does not fire.

### `firmware/src/main.cpp`

#### `kPeriodUs`
*config* · [`firmware/src/main.cpp:24`](../firmware/src/main.cpp#L24)

```cpp
const uint32_t kPeriodUs = 1000000UL / KMAN_SAMPLE_RATE_HZ;
```

**Purpose.** The sample period in microseconds on the 1 MHz timer timebase, from which the tick grid and header timing derive.

**How it works.** Integer division of 1,000,000 by KMAN_SAMPLE_RATE_HZ: 10000 us at the default 100 Hz. setup() passes it to hal::startSampleTimer(), and buildHeaderFrame() writes it as "period_us".

**Notes.** A static_assert in config.h requires 1e6 % KMAN_SAMPLE_RATE_HZ == 0, so the division is exact.

#### `sensor driver objects + g_slots slot table`
*config* · [`firmware/src/main.cpp:28`](../firmware/src/main.cpp#L28)

```cpp
Lsm6dsox g_chestImu, g_abdomenImu, g_neckImu; FlexAdc g_neckFlex; Slot g_slots[proto::kStatusSlots]; const uint8_t kNumSlots; uint8_t g_presentMask; bool g_sensorError;
```

**Purpose.** Statically constructs one driver object per possible sensor and maps each to a fixed slot (name, channel prefix, bit). The header, frame layout and STATUS read_errors all follow this slot order.

**How it works.**

Drivers (constructed at static-init time, whether the slot is configured or not):
- g_chestImu = Lsm6dsox(Wire, KMAN_CHEST_IMU_ADDR (0x6A), accelOnly=false, KMAN_SAMPLE_RATE_HZ, KMAN_IMU_ACCEL_RANGE_G, KMAN_IMU_GYRO_RANGE_DPS).
- g_abdomenImu = Lsm6dsox(Wire, KMAN_ABDOMEN_IMU_ADDR (0x6B), false, same rate and ranges).
- g_neckFlex = FlexAdc(KMAN_FLEX_PIN, KMAN_FLEX_SENSOR_DESC, KMAN_FLEX_R_FIXED_OHM, KMAN_FLEX_TOPOLOGY).
- g_neckImu = Lsm6dsox(Wire, KMAN_NECK_IMU_ADDR (0x6B), KMAN_NECK_IMU_ACCEL_ONLY, same rate and ranges).

g_slots[4] (array size proto::kStatusSlots), as {id, prefix, bit, configured, drv, state}:
0 {"chest_imu", "chest", 0, KMAN_CHEST_IMU != 0, &g_chestImu, kNotConfigured}
1 {"abdomen_imu", "abd", 1, KMAN_ABDOMEN_IMU != 0, &g_abdomenImu, kNotConfigured}
2 {"neck_flex", "flex", 2, KMAN_NECK_FLEX != 0, &g_neckFlex, kNotConfigured}
3 {"neck_imu", "neck", 3, KMAN_NECK_IMU != 0, &g_neckImu, kNotConfigured}

kNumSlots = sizeof g_slots / sizeof g_slots[0] = 4. g_presentMask (initially 0) collects the bit of every slot whose init() succeeded. g_sensorError (initially false) is set when any configured slot fails init().

**Notes.** Each slot's index equals its bit, and sampleAndQueue() indexes g_status.read_errors[] by slot index. Each Lsm6dsox constructor selects its ODR (the smallest of 208/416/833 Hz that is at least 2x the frame rate) and the scale factors from the range macros.

#### `SlotState`
*enum* · [`firmware/src/main.cpp:36`](../firmware/src/main.cpp#L36)

```cpp
enum SlotState : uint8_t { kNotConfigured, kInitFailed, kOk };
```

**Purpose.** The per-slot outcome of boot-time probing. It decides whether a slot is sampled and what the header reports for it.

**How it works.** kNotConfigured (0): the slot is disabled at compile time and never probed. kInitFailed (1): the slot is configured but drv->init() returned false. kOk (2): the slot is present, sampled each tick, and its channels appear in the frame layout. buildHeaderFrame() maps these to the strings "not_configured", "init_failed" and "ok".

#### `Slot`
*struct* · [`firmware/src/main.cpp:38`](../firmware/src/main.cpp#L38)

```cpp
struct Slot { const char* id; const char* prefix; uint8_t bit; bool configured; Sensor* drv; SlotState state; };
```

**Purpose.** One entry of the sensor slot table. It ties a driver to its header name, channel-name prefix and mask bit.

**How it works.**

Fields:
- id: slot name written as "slot" in the header's sensors[] and channels[] entries.
- prefix: joined with each driver ChannelInfo.suffix by '_' to form channel names (e.g. "chest" + "ax" -> "chest_ax").
- bit: bit position in present_mask and in each DATA frame's valid byte.
- configured: compile-time KMAN_&lt;SLOT> != 0.
- drv: pointer to the static driver, called through the Sensor virtual interface.
- state: SlotState. initSensors() sets it; it does not change after boot.

**Notes.** Only initSensors() writes state, so a slot that fails at boot stays out of the layout until the next reset.

#### `frame buffers and TX ring`
*config* · [`firmware/src/main.cpp:60`](../firmware/src/main.cpp#L60)

```cpp
kMaxChannels; kDataHeaderLen; uint8_t g_dataBody[]; uint8_t g_frame[]; char g_headerJson[4000]; uint8_t g_headerFrame[]; ByteRing<KMAN_TX_RING_BYTES> g_tx;
```

**Purpose.** Statically sized scratch buffers for building DATA, STATUS and HEADER frames, plus the byte FIFO that stages outgoing frames for USB.

**How it works.**

- kMaxChannels = 6 + 6 + 1 + 6 = 19 (chest IMU, abdomen IMU, flex, neck IMU).
- kDataHeaderLen = 4 + 2 + 1 = 7 (t_us u32, seq u16, valid u8).
- g_dataBody[7 + 2\*19 = 45]: DATA body scratch.
- g_frame[proto::kOverhead (7) + 7 + 38 + proto::kStatusBodyLen (42) = 94]: encoded-frame scratch shared by sampleAndQueue() (DATA, at most 52 bytes) and queueStatusIfDue() (STATUS, 49 bytes).
- g_headerJson[4000]: the session header JSON text. The code comment puts it at ~1.7 KB with chest+flex and ~3.1 KB with three IMUs + flex.
- g_headerFrame[4000 + 7]: the encoded HEADER frame.
- g_tx: ByteRing&lt;KMAN_TX_RING_BYTES> (8192 B by default). Each push is all-or-nothing, so a frame is never split, and highWater tracks the peak fill.

**Notes.** The loop task is both producer and consumer for g_tx, so it needs no locking. A HEADER frame must fit in g_tx as a whole. If KMAN_TX_RING_BYTES is set below the header frame size, queueHeaderIfPending() can never push it, and no static_assert catches that.

#### `run state`
*config* · [`firmware/src/main.cpp:72`](../firmware/src/main.cpp#L72)

```cpp
bool g_streaming, g_headerPending, g_statusPending; uint32_t g_streamId, g_lastTickCount, g_lastStatusMs; proto::Status g_status; uint16_t g_maxLatencyUs;
```

**Purpose.** The mutable state of the host session and of tick bookkeeping, shared by the loop helpers.

**How it works.**

- g_streaming: true while the host holds DTR. Frames are produced only while it is set.
- g_headerPending: a HEADER must be queued before any further DATA. startStreaming() and the 'H' command set it.
- g_statusPending: forces a STATUS frame now instead of waiting for the period. startStreaming() and the 'S' command set it.
- g_streamId: incremented by startStreaming(), so the first stream is 1. Reported as "stream_id".
- g_lastTickCount: the last hal::latestTick() count seen by loop(), used to detect new and skipped ticks.
- g_lastStatusMs: millis() of the last successfully queued STATUS.
- g_status: proto::Status counters (t_us, ticks, frames_sent, overruns, tx_drops, read_errors[4], max_latency_us, tx_high_water, flags).
- g_maxLatencyUs: the largest tick-to-queued latency since the last STATUS, saturated at 0xFFFF.

**Notes.** All of these start at zero or false. startStreaming() resets the cumulative counters at the start of each stream.

#### `initSensors`
*function* · [`firmware/src/main.cpp:83`](../firmware/src/main.cpp#L83)

```cpp
void initSensors()
```

**Purpose.** Brings up the I2C bus and probes every configured sensor slot once at boot. The results fix the frame layout for the whole run.

**How it works.**

1. hal::i2cBusRecover(KMAN_I2C_SDA_PIN, KMAN_I2C_SCL_PIN): if a slave holds SDA low, clocks SCL up to 9 times and issues a STOP.
2. Wire.setPins(SDA, SCL) (honours any pin overrides from config.h), Wire.begin(), Wire.setClock(KMAN_I2C_HZ) (400 kHz by default).
3. For each of the kNumSlots slots: skip it if !configured (its state stays kNotConfigured). Otherwise call s.drv->init(). On true, set state = kOk and g_presentMask |= 1 &lt;&lt; s.bit. On false, set state = kInitFailed and g_sensorError = true.

**Notes.** setup() calls it once, and it never retries. FlexAdc::init() always returns true; it can only set a warning() for a reading at the ADC rail. Lsm6dsox::init() fails on no I2C ACK, a wrong WHO_AM_I, a reset timeout, a register write/verify mismatch or a failed first read, and it records the reason in lastError().

#### `buildHeaderFrame`
*function* · [`firmware/src/main.cpp:104`](../firmware/src/main.cpp#L104)

```cpp
size_t buildHeaderFrame()
```

**Purpose.** Serialises the self-describing JSON session header into g_headerJson and encodes it as a HEADER frame in g_headerFrame. The host needs it to decode DATA frames.

**How it works.**

1. Creates JsonWriter w over g_headerJson (capacity 4000) and reads hal::deviceId() into devId[24].
2. Writes the top-level object: "type":"kman_session_header"; "protocol": proto::kProtocolVersion (1); "fw_version": KMAN_FW_VERSION; "build": __DATE__ " " __TIME__; "board": KMAN_BOARD_NAME; "mcu": hal::mcuName() ("nRF52840"); "device_id": devId (16 hex chars from FICR DEVICEID); "boot_id": hal::bootId() as "0x%08X" via kvHex; "stream_id": g_streamId; "t_us_at_header": hal::nowUs(); "sample_rate_hz"; "period_us": kPeriodUs.
3. "clock" object: filled by hal::describeClock(w) (source, hfclk crystal/RC, t_unit, t_bits 32, seq_bits 16).
4. "frame" object: "sync":"AA55", textual "len" and "crc" descriptions, "data_prefix":["t_us:u32","seq:u16","valid:u8"], "channel_type":"i16".
5. "present_mask": g_presentMask.
6. "sensors" array: one object for every slot (all 4) with "slot", "bit" and "status" ("ok"/"init_failed"/"not_configured"). If the slot is configured, it also gets the driver's describeConfig(w) members, "error": lastError() when kInitFailed, and "warning" when warning() is non-empty.
7. "channels" array: for each kOk slot in slot order, for each channel c &lt; channelCount() in driver order, an object with "name" = prefix_suffix (kvJoined), "slot", "bit", "unit" and "scale" = scaleMantissa e scaleExp10 (kvSci, e.g. 61e-6). This array is the DATA payload order.
8. If !w.ok() (buffer overflow or unbalanced nesting), rewrites the buffer as {"type":"kman_session_header","error":"header overflow"} and encodes that instead.
9. Returns proto::encodeFrame(kTypeHeader, json, length, g_headerFrame, sizeof g_headerFrame): the frame length in bytes, or 0 if encodeFrame rejects it (body+1 > kMaxLen 4096, or the frame does not fit the output buffer).

**Notes.** queueHeaderIfPending() calls it on every send attempt, so t_us_at_header and stream_id are current each time. The host (StreamDecoder._new_stream) compares device_id, boot_id, stream_id and t_us_at_header to detect a new stream. The overflow branch is not expected to run; per the comment, the buffer is sized with margin.

#### `sampleAndQueue`
*function* · [`firmware/src/main.cpp:192`](../firmware/src/main.cpp#L192)

```cpp
void sampleAndQueue(uint32_t tickCount, uint32_t tickUs)
```

**Purpose.** Reads every present sensor once for one timer tick and queues one DATA frame stamped with that tick's scheduled time and count.

**How it works.**

1. Layout: walks the slots in slot order and sets offset[i] to the running sum of channelCount() over kOk slots; n = the total channel count.
2. Read order kOrder = {2, 0, 1, 3} (neck_flex, chest_imu, abdomen_imu, neck_imu). For each kOk slot, calls drv->read(&ch[offset[i]]). On success, valid |= 1 &lt;&lt; s.bit. On failure, zeroes that slot's channelCount() values (keeping the fixed layout) and increments g_status.read_errors[i].
3. Body in g_dataBody: putU32(tickUs), putU16((uint16_t)tickCount), putU8(valid), then n x putI16(ch[i]), all little-endian.
4. proto::encodeFrame(kTypeData, ...) into g_frame. If g_tx.push(g_frame, len) succeeds, ++g_status.frames_sent. Otherwise (fewer than len bytes free) the whole frame is dropped and ++g_status.tx_drops.
5. latency = hal::nowUs() - tickUs. If it exceeds g_maxLatencyUs, g_maxLatencyUs = min(latency, 0xFFFF).

**Notes.** The comment gives the reason for the read order: the fastest read goes first (ADC ~0.2 ms, IMU burst ~0.35 ms at 400 kHz), so all samples sit within ~1 ms of the tick. seq is the low 16 bits of the tick count since the timer started, not since the stream started; t_us wraps every 2^32 us. When every read succeeds, valid equals present_mask. A DATA frame is 14 + 2n bytes: 28 B for chest+flex, 40 B for two IMUs + flex. loop() is the only caller, and only while streaming with no header pending.

#### `startStreaming`
*function* · [`firmware/src/main.cpp:241`](../firmware/src/main.cpp#L241)

```cpp
void startStreaming()
```

**Purpose.** Starts a new stream when the host opens the port. It gives the stream a new id, discards stale TX data and resets the per-stream counters.

**How it works.** ++g_streamId. g_tx.clear(). Sets g_streaming, g_headerPending and g_statusPending to true. Zeroes g_status.frames_sent, overruns, tx_drops and read_errors[0..3], and sets g_maxLatencyUs = 0. Calls g_tx.resetHighWater(), which sets highWater to the current fill: 0 after the clear.

**Notes.** It does not reset g_status.t_us, ticks, max_latency_us, tx_high_water or flags, because queueStatusIfDue() overwrites them on every STATUS. Because g_headerPending is set and the ring is empty, the HEADER is the first frame of every stream.

#### `serviceHost`
*function* · [`firmware/src/main.cpp:255`](../firmware/src/main.cpp#L255)

```cpp
void serviceHost()
```

**Purpose.** Tracks the host connection by DTR and handles the single-byte host commands.

**How it works.**

1. connected = hal::hostConnected() (Serial.dtr()).
2. If connected && !g_streaming, calls startStreaming(). If !connected && g_streaming, sets g_streaming = false and clears g_tx.
3. Drains every pending input byte with hal::hostRead() until it returns -1. proto::kCmdSendHeader ('H') sets g_headerPending; proto::kCmdSendStatus ('S') sets g_statusPending. Other bytes are ignored.

**Notes.** It drains commands even when not streaming, but those flags have no effect until the next startStreaming() sets both anyway. Each port open (DTR rising edge) therefore starts a new stream_id.

#### `queueHeaderIfPending`
*function* · [`firmware/src/main.cpp:269`](../firmware/src/main.cpp#L269)

```cpp
void queueHeaderIfPending()
```

**Purpose.** Queues a freshly built HEADER frame when one is pending and the ring can take all of it.

**How it works.** Returns immediately unless g_headerPending && g_streaming. Otherwise calls len = buildHeaderFrame(). If len != 0 and g_tx.free() >= len, pushes g_headerFrame and clears g_headerPending. If not, the flag stays set and the next loop() iteration retries, rebuilding the JSON.

**Notes.** loop() skips sampleAndQueue() while g_headerPending is set, so a header always precedes the DATA that follows it. Those suppressed ticks are not added to overruns, but they show up on the host as seq gaps. loop() calls this before its tick check, so an 'H' request normally goes out in the same iteration without losing a tick, unless the ring lacks len free bytes.

#### `queueStatusIfDue`
*function* · [`firmware/src/main.cpp:278`](../firmware/src/main.cpp#L278)

```cpp
void queueStatusIfDue()
```

**Purpose.** Queues a STATUS frame of device health counters every KMAN_STATUS_PERIOD_MS, or immediately when one is requested.

**How it works.**

1. Returns if !g_streaming.
2. nowMs = millis(). Returns if !g_statusPending && nowMs - g_lastStatusMs &lt; KMAN_STATUS_PERIOD_MS (1000).
3. Fills g_status: ticks = hal::latestTick(&tickUs) (ticks since the timer started); t_us = hal::nowUs(); max_latency_us = g_maxLatencyUs; tx_high_water = min(g_tx.highWater(), 0xFFFF); flags = (hal::clockIsCrystal() ? kStatusFlagHfxo 0x01 : 0) | (g_sensorError ? kStatusFlagSensorError 0x02 : 0).
4. proto::encodeStatus(g_status, body[42]), then encodeFrame(kTypeStatus, ...) into g_frame.
5. If g_tx.push succeeds: g_lastStatusMs = nowMs, g_statusPending = false, g_maxLatencyUs = 0, g_tx.resetHighWater(). If it fails, nothing is reset and the frame is retried on the next iteration with refreshed values.

**Notes.** frames_sent, overruns, tx_drops and read_errors accumulate from startStreaming(). max_latency_us and tx_high_water are maxima over the interval since the last queued STATUS. A dropped STATUS is not counted anywhere. The period is measured from the last successful push, so an 'S' request also restarts the period.

#### `pumpTx`
*function* · [`firmware/src/main.cpp:302`](../firmware/src/main.cpp#L302)

```cpp
void pumpTx()
```

**Purpose.** Moves as much of the TX ring into the USB CDC endpoint as fits, without blocking.

**How it works.** While g_tx.used() > 0: room = hal::hostWritable() (Serial.availableForWrite, clamped at 0), and the loop breaks if room is 0. n = g_tx.peekContiguous(&ptr), limited to room. w = hal::hostWrite(ptr, n), then g_tx.consume(w) and wrote = true. The loop breaks if w &lt; n. After the loop, if anything was attempted, calls hal::hostFlush().

**Notes.** Per hal.h, hostWrite never blocks when n &lt;= hostWritable(). Under TinyUSB, hostFlush() is non-blocking and only queues the partially filled packet. peekContiguous returns at most the run up to the ring's end, so a wrapped ring takes two passes.

#### `updateLeds`
*function* · [`firmware/src/main.cpp:320`](../firmware/src/main.cpp#L320)

```cpp
void updateLeds()
```

**Purpose.** Shows the device state on the OK and ERR LEDs.

**How it works.** ms = millis(). hal::led(KMAN_LED_ERR_PIN, g_sensorError): solid on when a configured sensor failed init. OK pattern: while streaming, ok = (ms / 250) & 1, which toggles every 250 ms (a 2 Hz blink); otherwise ok = (ms % 2000) &lt; 50, a 50 ms blip every 2 s (alive, waiting for the host). The OK LED is written only if KMAN_LED_OK_PIN != KMAN_LED_ERR_PIN || !g_sensorError.

**Notes.** On a single-LED build (both pins equal), a sensor error leaves the LED solid on and suppresses the blink. hal::led() applies KMAN_LED_ACTIVE_LOW. Streaming of the remaining sensors continues during a sensor error.

#### `setup`
*function* · [`firmware/src/main.cpp:332`](../firmware/src/main.cpp#L332)

```cpp
void setup()
```

**Purpose.** The Arduino one-time entry point. It initialises the board, LEDs and sensors, then starts the hardware sample timer.

**How it works.** hal::init(): starts the HFXO crystal, draws a random boot id from the RNG and runs Serial.begin(115200). hal::ledInit() on KMAN_LED_OK_PIN and KMAN_LED_ERR_PIN (output, off). initSensors(). hal::startSampleTimer(kPeriodUs): TIMER4 at 1 MHz, 32-bit, CC[0] = period, IRQ priority 3, and records the calling task as the one to notify. Finally g_lastTickCount = hal::latestTick(&dummy).

**Notes.** hal.h requires startSampleTimer() to be called from the task that later calls waitForTick(). In the Arduino core setup() and loop() run in the same loop task, so the requirement holds. Sensors are probed before the timer starts, and g_lastTickCount is synced to the timer afterwards. So the IMU init delays do not count as overruns: up to ~150 ms per configured IMU (a reset poll of up to ~50 ms plus a 100 ms settle), about 300 ms with two IMUs. In any case, overruns are only counted while streaming.

#### `loop`
*function* · [`firmware/src/main.cpp:342`](../firmware/src/main.cpp#L342)

```cpp
void loop()
```

**Purpose.** The Arduino main loop. Each pass waits for a tick (or a timeout), services the host, samples the tick if it is new and streaming is on, then sends status and drains TX.

**How it works.**

1. hal::waitForTick(5): ulTaskNotifyTake blocks until the TIMER4 ISR notifies or 5 ms pass. Several ticks collapse into one wake.
2. serviceHost().
3. queueHeaderIfPending().
4. count = hal::latestTick(&tickUs). If count != g_lastTickCount and g_streaming: g_status.overruns += count - g_lastTickCount - 1 (ticks skipped since the last pass), and if !g_headerPending, calls sampleAndQueue(count, tickUs) for the latest tick only. Then g_lastTickCount = count, whether streaming or not.
5. queueStatusIfDue().
6. pumpTx().
7. updateLeds().

**Notes.** The 5 ms timeout makes host servicing and pumpTx run even between ticks; at 100 Hz the loop runs about twice per period. Skipped ticks are never back-filled. The host sees them as seq gaps, and they are counted in overruns, except a tick suppressed by a pending header, which is not counted. g_lastTickCount advances while not streaming, so the first pass of a new stream adds no spurious overruns.

## 6. Firmware: hardware abstraction (nRF52840)

- **`firmware/src/hal/hal.h`**: The board-abstraction interface. It declares the free functions in namespace `hal` that main.cpp and the sensor drivers use for clocks, chip identity, the hardware sample timer, the ADC, I2C bus recovery, the USB host link and the LEDs. Each supported MCU family gets one hal_&lt;mcu>.cpp; in 1.0.0 the only one is hal_nrf52.cpp. The comments in this header are the contract: startSampleTimer must run on the task that later calls waitForTick, tick timestamps wrap every 2^32 us (71.6 min), hostWrite never blocks when n &lt;= hostWritable(), and hostRead returns -1 when nothing is pending. It includes json_writer.h for the describe\* functions.
- **`firmware/src/hal/hal_nrf52.cpp`**: The nRF52840 implementation of hal.h. It compiles only when NRF52_SERIES or NRF52840_XXAA is defined. It drives peripheral registers directly for the HFXO start (NRF_CLOCK), the hardware-RNG boot id (NRF_RNG), the factory device id (NRF_FICR) and the 1 MHz TIMER4 sample clock with its compare ISR, and it wakes the loop task through a FreeRTOS direct-to-task notification. ADC, GPIO and USB CDC go through the Adafruit-based Seeed Arduino core (analogRead and its setup calls, pinMode/digitalWrite, TinyUSB `Serial`). Build settings come from include/config.h: KMAN_ADC_BITS, KMAN_ADC_OVERSAMPLE, KMAN_ADC_TACQ_US and KMAN_LED_ACTIVE_LOW.

**How it connects.** This group sits between main.cpp and the hardware. main.cpp setup() calls hal::init(), then hal::ledInit() for the OK and ERR pins, then initSensors(), which calls hal::i2cBusRecover(KMAN_I2C_SDA_PIN, KMAN_I2C_SCL_PIN) before Wire.setPins()/Wire.begin() and runs each configured driver's init(). FlexAdc::init() calls hal::adcInit(), hal::adcFullScale() and hal::adcRead(). setup() then calls hal::startSampleTimer(kPeriodUs) from the loop task and hal::latestTick() to seed g_lastTickCount. Each loop() pass works as follows. hal::waitForTick(5) blocks until TIMER4_IRQHandler's vTaskNotifyGiveFromISR wakes the task or 5 ms pass, whichever comes first, so the loop also runs without ticks. serviceHost() calls hal::hostConnected() and drains hal::hostRead() for 'H'/'S' commands. hal::latestTick() tells loop() whether a new tick arrived; while streaming, gaps become g_status.overruns and, unless a header is pending, sampleAndQueue() stamps the DATA frame with the tick's scheduled time and measures latency with hal::nowUs(); FlexAdc::read() calls hal::adcRead(). queueStatusIfDue() uses hal::latestTick(), hal::nowUs() and hal::clockIsCrystal() (kStatusFlagHfxo). pumpTx() uses hal::hostWritable(), hal::hostWrite() and hal::hostFlush(). updateLeds() uses hal::led(). buildHeaderFrame() (called from queueHeaderIfPending()) calls hal::deviceId(), hal::mcuName(), hal::bootId() (as a kvHex string), hal::nowUs() and hal::describeClock(); FlexAdc::describeConfig() adds hal::describeAdc(). Downward, the HAL calls nRF52840 registers (NRF_CLOCK, NRF_RNG, NRF_FICR, NRF_TIMER4), CMSIS NVIC_\*, FreeRTOS (xTaskGetCurrentTaskHandle, vTaskNotifyGiveFromISR, portYIELD_FROM_ISR, ulTaskNotifyTake), the Arduino core (analogReference/analogReadResolution/analogOversampling/analogSampleTime/analogRead, pinMode/digitalRead/digitalWrite/delayMicroseconds), the TinyUSB `Serial` CDC object, and JsonWriter::kv. On the host side, the header fields it produces drive kman_host/stream.py: _new_stream() splits segments on a changed device_id, boot_id or stream_id (or, failing that, a restarted device clock), and DeviceStatus.hfxo in kman_host/protocol.py decodes the crystal flag. logger.py shows that flag as 'NO-XTAL', and analysis/kman_analysis/pipeline.py records it as hf_crystal.

### `firmware/src/hal/hal.h`

#### `hal (namespace interface)`
*namespace* · [`firmware/src/hal/hal.h:10`](../firmware/src/hal/hal.h#L10)

```cpp
namespace hal { ... }
```

**Purpose.** Declares the board/MCU-specific surface of the firmware, so main.cpp and the sensor drivers never touch MCU registers or core-specific APIs directly.

**How it works.** Free-function declarations only, in six groups: (1) setup and identity: init() (comment: clocks, LEDs, host link), mcuName(), deviceId(out, cap) ('stable hex id of this chip'), bootId() ('random per boot: lets the host tell a reboot from a reconnect'); (2) sample clock on a 1 MHz hardware-timer timebase: startSampleTimer, latestTick, waitForTick, nowUs, clockIsCrystal, describeClock; (3) ADC: adcInit, adcRead, adcFullScale, describeAdc; (4) I2C: i2cBusRecover; (5) host link: hostConnected (DTR asserted), hostWritable, hostWrite, hostFlush, hostRead; (6) LEDs: ledInit, led. Each declaration is implemented in hal_nrf52.cpp; the per-function entries below cover them. The header includes ../json_writer.h because describeClock and describeAdc take a JsonWriter&.

**Notes.** The header is MCU-agnostic. A port to another MCU family supplies its own hal_&lt;mcu>.cpp behind its own preprocessor guard.

### `firmware/src/hal/hal_nrf52.cpp`

#### `(anonymous namespace) timer/boot state: kTimer, kTimerIrq, kTimerIrqPriority, s_tickCount, s_tickTimeUs, s_periodUs, s_loopTask, s_bootId`
*config block* · [`firmware/src/hal/hal_nrf52.cpp:17`](../firmware/src/hal/hal_nrf52.cpp#L17)

```cpp
NRF_TIMER_Type* const kTimer = NRF_TIMER4; const IRQn_Type kTimerIrq = TIMER4_IRQn; const uint32_t kTimerIrqPriority = 3; volatile uint32_t s_tickCount = 0; volatile uint32_t s_tickTimeUs = 0; uint32_t s_periodUs = 10000; TaskHandle_t s_loopTask = nullptr; uint32_t s_bootId = 0;
```

**Purpose.** File-private constants and state shared by the TIMER4 ISR, the sample-clock functions and the boot id.

**How it works.** kTimer points at NRF_TIMER4. The comment says TIMER0 belongs to the SoftDevice and TIMERs 3 and 4 are unused by the core. kTimerIrq = TIMER4_IRQn. kTimerIrqPriority = 3. The comment explains the choice: it is below the SoftDevice's reserved levels (0, 1, 4) in case BLE is added later, and >= configMAX_SYSCALL_INTERRUPT_PRIORITY (2), so FreeRTOS ...FromISR calls are legal. s_tickCount (volatile) is the number of ticks the ISR has recorded since the timer started. s_tickTimeUs (volatile) is the scheduled 1 MHz timestamp of the latest recorded tick. s_periodUs is the tick period in us. It defaults to 10000, and startSampleTimer overwrites it. s_loopTask is the FreeRTOS task the ISR notifies; it is set by startSampleTimer. s_bootId holds the 32-bit random id drawn once in init().

**Notes.** Only s_tickCount and s_tickTimeUs are volatile, because only these two are written by the ISR and read by the task. s_periodUs and s_loopTask are written once, before the IRQ is enabled.

#### `startHfxo`
*function (file-local)* · [`firmware/src/hal/hal_nrf52.cpp:29`](../firmware/src/hal/hal_nrf52.cpp#L29)

```cpp
void startHfxo()
```

**Purpose.** Switches HFCLK to the external high-frequency crystal (HFXO). TIMER4's 16 MHz input then gets crystal accuracy (~20 ppm per the file header) instead of the internal RC oscillator's.

**How it works.**

1. xtal = CLOCK_HFCLKSTAT_SRC_Xtal &lt;&lt; CLOCK_HFCLKSTAT_SRC_Pos.
2. If (NRF_CLOCK->HFCLKSTAT & CLOCK_HFCLKSTAT_SRC_Msk) == xtal, the crystal is already the source and the function returns.
3. Otherwise it clears NRF_CLOCK->EVENTS_HFCLKSTARTED, writes TASKS_HFCLKSTART = 1, and busy-waits until EVENTS_HFCLKSTARTED is set or a loop counter `guard` reaches 1,000,000 iterations.
4. It returns nothing, whether or not the crystal started.

**Notes.** The comment says the SoftDevice is never enabled, so CLOCK may be driven directly; with BLE this must become sd_clock_hfclk_request(). A timeout is silent: HFCLK stays on HFINT, and the condition is reported later by clockIsCrystal() (header 'hfclk' field and the HFXO bit in STATUS flags). The early-return check tests only SRC, not STATE.

#### `randomWord`
*function (file-local)* · [`firmware/src/hal/hal_nrf52.cpp:40`](../firmware/src/hal/hal_nrf52.cpp#L40)

```cpp
uint32_t randomWord()
```

**Purpose.** Draws a 32-bit true-random value from the nRF52840 hardware RNG. init() uses it as the per-boot id.

**How it works.**

1. NRF_RNG->CONFIG = RNG_CONFIG_DERCEN_Enabled &lt;&lt; RNG_CONFIG_DERCEN_Pos enables the RNG's digital bias correction.
2. Clear EVENTS_VALRDY, then write TASKS_START = 1. The RNG now produces bytes continuously.
3. Four times (i = 0..3): busy-wait while EVENTS_VALRDY is 0 and `guard` &lt; 100000; clear EVENTS_VALRDY; v = (v &lt;&lt; 8) | (NRF_RNG->VALUE & 0xFF). The first byte read ends up in bits 31..24 and the last in bits 7..0.
4. TASKS_STOP = 1; return v.

**Notes.** The comment says the source is thermal noise and the cost is about 4 bytes x ~120 us (~0.5 ms total) with bias correction. Direct peripheral access is allowed because the SoftDevice is never enabled. If a guard expires, the code clears the event and reads VALUE anyway, so that byte may repeat the previous one. No error is reported, and v = 0 is not excluded.

#### `TIMER4_IRQHandler`
*ISR* · [`firmware/src/hal/hal_nrf52.cpp:59`](../firmware/src/hal/hal_nrf52.cpp#L59)

```cpp
extern "C" void TIMER4_IRQHandler(void)
```

**Purpose.** Handles each TIMER4 COMPARE0 event. It records the tick's exact scheduled time and count, re-arms the next compare on a fixed grid (skipping ahead past compares missed while interrupts were masked), and wakes the loop task. It does no sensor work.

**How it works.**

1. If kTimer->EVENTS_COMPARE[0] is 0, return. Only COMPARE0 is enabled in INTENSET, so this filters spurious entries.
2. Write EVENTS_COMPARE[0] = 0, then read it back with `(void)kTimer->EVENTS_COMPARE[0]`. The comment: 'read back so the clear lands before IRQ exit'. Without the read-back, the still-set event could re-pend the IRQ.
3. tick = kTimer->CC[0]: the compare value that just matched, i.e. the scheduled tick time, not the time the ISR ran. count = s_tickCount + 1. next = tick + s_periodUs, in uint32 arithmetic that wraps mod 2^32.
4. Skip-ahead: write TASKS_CAPTURE[1] = 1 to latch the live counter into CC[1], and set now = CC[1]. While (int32_t)(now - next) >= 0, i.e. `next` is at or before the current counter in a wrap-safe signed comparison, the compare for `next` can no longer fire. That tick is treated as already happened: tick = next; next += s_periodUs; ++count. The test is `>=`, not `>`, because a COMPARE is generated only when the counter increments onto the CC value, so a CC equal to the current count would not fire until the counter wraps.
5. kTimer->CC[0] = next arms the next compare. Ticks therefore stay at t = k \* period_us (mod 2^32) from timer start, with no ISR-latency jitter and no accumulated drift.
6. Publish s_tickTimeUs = tick first, then s_tickCount = count. The code comment notes this order lets the reader detect a torn read via the count (see hal::latestTick).
7. If s_loopTask is non-null: woken = pdFALSE; vTaskNotifyGiveFromISR(s_loopTask, &woken) increments the task's notification value; portYIELD_FROM_ISR(woken) requests a context switch on exit if the loop task now outranks the interrupted one.

**Notes.** Ticks skipped in step 4 still advance s_tickCount, so main.cpp loop() sees the count jump by more than 1. While streaming (g_streaming true), it adds the difference minus 1 to g_status.overruns (reset by startStreaming()), and the host sees the missing ticks as seq gaps. When not streaming, loop() only updates g_lastTickCount and nothing is counted. s_tickTimeUs is always the latest grid point &lt;= now, consistent with s_tickCount. The signed comparison is valid while now and next are within 2^31 us (~35.8 min) of each other. CC[1] is reserved for this ISR's capture; CC[2] belongs to nowUs(). One edge case: the counter is not re-checked after CC[0] is written. If it reaches `next` between the CC[1] capture and the CC[0] write (possible only when next - now is a few microseconds, e.g. the ISR ran almost exactly k periods late or a higher-priority interrupt preempted it at that point), COMPARE0 would not fire again until the counter wraps 2^32 us later. Runs at NVIC priority 3.

#### `hal::init`
*function* · [`firmware/src/hal/hal_nrf52.cpp:90`](../firmware/src/hal/hal_nrf52.cpp#L90)

```cpp
void hal::init()
```

**Purpose.** One-time board bring-up at the start of setup(): crystal clock, boot id, USB CDC host link.

**How it works.** Calls startHfxo(), then s_bootId = randomWord(), then Serial.begin(115200). The comment notes the baud is irrelevant for USB CDC.

**Notes.** Declared at hal.h:12. That comment says 'clocks, LEDs, host link', but this implementation does not touch LEDs; main.cpp setup() calls ledInit separately right after init().

#### `hal::bootId`
*function* · [`firmware/src/hal/hal_nrf52.cpp:96`](../firmware/src/hal/hal_nrf52.cpp#L96)

```cpp
uint32_t hal::bootId()
```

**Purpose.** Returns this boot's random 32-bit id so the host can tell a device reboot from a mere port re-open.

**How it works.** Returns s_bootId, which init() set once from randomWord(). It returns 0 if called before init().

**Notes.** Declared at hal.h:16. buildHeaderFrame() writes it as w.kvHex("boot_id", bootId(), 8), i.e. the JSON string "0x%08lX". The host's stream.py _new_stream() starts a new segment when device_id, boot_id or stream_id differs from the previous header. stream_id restarts after every reboot, so boot_id is what tells the cases apart.

#### `hal::mcuName`
*function* · [`firmware/src/hal/hal_nrf52.cpp:98`](../firmware/src/hal/hal_nrf52.cpp#L98)

```cpp
const char* hal::mcuName()
```

**Purpose.** Names the MCU for the session header.

**How it works.** Returns the string literal "nRF52840".

**Notes.** Declared at hal.h:14. Used as the 'mcu' key in buildHeaderFrame().

#### `hal::deviceId`
*function* · [`firmware/src/hal/hal_nrf52.cpp:100`](../firmware/src/hal/hal_nrf52.cpp#L100)

```cpp
void hal::deviceId(char* out, size_t cap)
```

**Purpose.** Writes the chip's stable, factory-programmed 64-bit unique id as a hex string.

**How it works.** snprintf(out, cap, "%08lX%08lX", NRF_FICR->DEVICEID[1], NRF_FICR->DEVICEID[0]) writes 16 uppercase hex digits plus NUL, high word first.

**Notes.** Declared at hal.h:15. If cap &lt; 17, snprintf truncates the output (still NUL-terminated when cap > 0). buildHeaderFrame() passes a char[24] and emits it as 'device_id'.

#### `hal::startSampleTimer`
*function* · [`firmware/src/hal/hal_nrf52.cpp:104`](../firmware/src/hal/hal_nrf52.cpp#L104)

```cpp
bool hal::startSampleTimer(uint32_t period_us)
```

**Purpose.** Configures TIMER4 as a free-running 32-bit 1 MHz counter (the firmware's microsecond timebase) with a periodic COMPARE0 interrupt every period_us, and binds the calling task as the one the ISR wakes.

**How it works.**

1. s_periodUs = period_us; s_loopTask = xTaskGetCurrentTaskHandle(). This is why hal.h requires the caller to be the task that later calls waitForTick().
2. kTimer->TASKS_STOP = 1, then TASKS_CLEAR = 1: the counter is stopped and reset to 0.
3. MODE = TIMER_MODE_MODE_Timer &lt;&lt; TIMER_MODE_MODE_Pos (timer mode: counts the prescaled clock, not COUNT tasks).
4. BITMODE = TIMER_BITMODE_BITMODE_32Bit &lt;&lt; TIMER_BITMODE_BITMODE_Pos (32-bit counter, wraps at 2^32).
5. PRESCALER = 4: f = 16 MHz / 2^4 = 1 MHz, so 1 count = 1 us.
6. SHORTS = 0: no COMPARE->CLEAR/STOP short. The counter free-runs and is itself the microsecond timebase (comment).
7. CC[0] = period_us: the first tick fires at counter = period_us; the ISR then advances CC[0] by period_us each time.
8. EVENTS_COMPARE[0] = 0; INTENSET = TIMER_INTENSET_COMPARE0_Msk (only COMPARE0 raises the IRQ).
9. NVIC_SetPriority(TIMER4_IRQn, 3), NVIC_ClearPendingIRQ, NVIC_EnableIRQ.
10. TASKS_START = 1. Returns true unconditionally.

**Notes.** Declared at hal.h:20. It does not reset s_tickCount or s_tickTimeUs; main.cpp calls it once in setup() with kPeriodUs = 1000000 / KMAN_SAMPLE_RATE_HZ (10000 us at the default 100 Hz). nowUs() returns meaningful times only after this call, since TIMER4 is not started before it.

#### `hal::latestTick`
*function* · [`firmware/src/hal/hal_nrf52.cpp:125`](../firmware/src/hal/hal_nrf52.cpp#L125)

```cpp
uint32_t hal::latestTick(uint32_t* tick_us)
```

**Purpose.** Returns the tick count and the scheduled timestamp of the latest tick as a consistent pair, even though the ISR may update them at any moment.

**How it works.**

Seqlock-style read without locking or masking interrupts:
1. c1 = s_tickCount; t = s_tickTimeUs; c2 = s_tickCount (volatile loads, in program order).
2. Repeat while c1 != c2.
3. \*tick_us = t; return c1.
This works because the ISR stores s_tickTimeUs before s_tickCount, and every ISR pass that records anything increments the count by at least 1. If the ISR runs anywhere between the two count loads, c2 != c1 and the loop retries. If c1 == c2, no ISR ran in between, so t belongs to tick c1. Each aligned 32-bit load is atomic on the single-core Cortex-M4, and the ISR runs to completion before the task resumes, so no memory barriers are needed.

**Notes.** Declared at hal.h:23. It returns 0 with \*tick_us = 0 before the first tick. The count wraps at 2^32 ticks and the timestamp every 2^32 us (71.6 min). main.cpp uses it in setup() (initial g_lastTickCount), in loop() to detect new ticks and overruns, and in queueStatusIfDue() for g_status.ticks.

#### `hal::waitForTick`
*function* · [`firmware/src/hal/hal_nrf52.cpp:136`](../firmware/src/hal/hal_nrf52.cpp#L136)

```cpp
void hal::waitForTick(uint32_t timeout_ms)
```

**Purpose.** Blocks the loop task until the TIMER4 ISR notifies it or the timeout elapses, whichever comes first.

**How it works.** ulTaskNotifyTake(pdTRUE, x) with x = pdMS_TO_TICKS(timeout_ms), or 1 FreeRTOS tick if that conversion yields 0, so the wait is never zero-length. pdTRUE clears the task's notification value to 0 on return, so ticks given while the task was busy produce a single immediate return, not a backlog of wake-ups. The returned notification count is discarded; the caller learns what happened from latestTick().

**Notes.** Declared at hal.h:25. main.cpp loop() calls waitForTick(5), so the loop also runs at least every ~5 ms without ticks (servicing the host link and TX pump). It wakes only the task captured by startSampleTimer().

#### `hal::nowUs`
*function* · [`firmware/src/hal/hal_nrf52.cpp:140`](../firmware/src/hal/hal_nrf52.cpp#L140)

```cpp
uint32_t hal::nowUs()
```

**Purpose.** Reads the current time on the same 1 MHz TIMER4 timebase as the tick timestamps.

**How it works.** Writes kTimer->TASKS_CAPTURE[2] = 1 to latch the live counter into CC[2], and returns CC[2].

**Notes.** Declared at hal.h:26. The comment says CC[2] is reserved for the loop task and the ISR uses CC[1], so an ISR running between the capture and the read cannot overwrite the value. Because CC[2] is a single shared register, the function is not safe to call from two tasks concurrently; only the loop task calls it. Uses in main.cpp: per-frame latency (nowUs() - tickUs) in sampleAndQueue, 't_us_at_header' in buildHeaderFrame, and g_status.t_us.

#### `hal::clockIsCrystal`
*function* · [`firmware/src/hal/hal_nrf52.cpp:146`](../firmware/src/hal/hal_nrf52.cpp#L146)

```cpp
bool hal::clockIsCrystal()
```

**Purpose.** Reports whether HFCLK is actually running from the external crystal, which is what makes timestamps accurate.

**How it works.** Reads stat = NRF_CLOCK->HFCLKSTAT once and returns true only if (stat & SRC_Msk) == (SRC_Xtal &lt;&lt; SRC_Pos) and the STATE bit (stat & CLOCK_HFCLKSTAT_STATE_Msk) is set (clock running).

**Notes.** Declared at hal.h:27. Used by describeClock() and by queueStatusIfDue(), which sets proto::kStatusFlagHfxo (0x01) in STATUS flags. The host decodes the flag as DeviceStatus.hfxo; logger.py prints 'NO-XTAL' when it is false.

#### `hal::describeClock`
*function* · [`firmware/src/hal/hal_nrf52.cpp:152`](../firmware/src/hal/hal_nrf52.cpp#L152)

```cpp
void hal::describeClock(JsonWriter& w)
```

**Purpose.** Writes the timebase description into the session header's 'clock' object.

**How it works.** Writes five key/value pairs into the caller's open JSON object: source = "TIMER4 32-bit free-running, 1 MHz"; hfclk = "HFXO 32 MHz crystal" if clockIsCrystal(), else "HFINT RC (inaccurate!)" (evaluated at call time); t_unit = "us"; t_bits = 32; seq_bits = 16.

**Notes.** Declared at hal.h:28. seq_bits is 16 because main.cpp sends the tick count truncated to u16 as the DATA frame's seq; t_bits 32 matches the u32 t_us field. buildHeaderFrame() wraps the call in w.beginObject("clock") / w.endObject().

#### `hal::adcInit`
*function* · [`firmware/src/hal/hal_nrf52.cpp:162`](../firmware/src/hal/hal_nrf52.cpp#L162)

```cpp
void hal::adcInit()
```

**Purpose.** Configures the SAADC (through the Arduino core) for ratiometric, oversampled flex-sensor readings.

**How it works.** analogReference(AR_VDD4) uses reference VDD/4 with gain 1/4, so full scale = VDD. Then analogReadResolution(KMAN_ADC_BITS) (default 14), analogOversampling(KMAN_ADC_OVERSAMPLE) (default 16), and analogSampleTime(KMAN_ADC_TACQ_US) (default 10 us acquisition time).

**Notes.** Declared at hal.h:31. The comment explains that the divider runs from the same 3V3 rail, so supply ripple cancels out, and that the oversampling call also enables burst mode (one SAMPLE task -> N conversions). These are global core settings that apply to every later analogRead. FlexAdc::init() calls it.

#### `hal::adcRead`
*function* · [`firmware/src/hal/hal_nrf52.cpp:171`](../firmware/src/hal/hal_nrf52.cpp#L171)

```cpp
uint16_t hal::adcRead(uint8_t pin)
```

**Purpose.** Takes one (oversampled) ADC reading on an Arduino pin.

**How it works.** Returns (uint16_t)analogRead(pin), a blocking conversion in the range 0..adcFullScale().

**Notes.** Declared at hal.h:32. A main.cpp comment puts the ADC read at ~0.2 ms with the default settings, so it is sampled first in each tick. Called by FlexAdc::read() (per tick) and FlexAdc::init() (8-sample rail check).

#### `hal::adcFullScale`
*function* · [`firmware/src/hal/hal_nrf52.cpp:173`](../firmware/src/hal/hal_nrf52.cpp#L173)

```cpp
uint16_t hal::adcFullScale()
```

**Purpose.** Returns the maximum ADC code for the configured resolution.

**How it works.** Returns (uint16_t)((1u &lt;&lt; KMAN_ADC_BITS) - 1), which is 16383 at the default 14 bits.

**Notes.** Declared at hal.h:33. FlexAdc::init() uses it for its rail check (margin = fs / 50), and describeAdc() reports it as full_scale.

#### `hal::describeAdc`
*function* · [`firmware/src/hal/hal_nrf52.cpp:175`](../firmware/src/hal/hal_nrf52.cpp#L175)

```cpp
void hal::describeAdc(JsonWriter& w)
```

**Purpose.** Writes the ADC configuration into a sensor's description in the session header.

**How it works.** Writes adc = "nRF52840 SAADC"; reference = "VDD/4, gain 1/4 (ratiometric, full scale = VDD)"; bits = KMAN_ADC_BITS; oversample = KMAN_ADC_OVERSAMPLE; tacq_us = KMAN_ADC_TACQ_US; full_scale = adcFullScale(). The numbers are cast to int32_t.

**Notes.** Declared at hal.h:34. Called from FlexAdc::describeConfig().

#### `hal::i2cBusRecover`
*function* · [`firmware/src/hal/hal_nrf52.cpp:186`](../firmware/src/hal/hal_nrf52.cpp#L186)

```cpp
void hal::i2cBusRecover(uint8_t sda, uint8_t scl)
```

**Purpose.** Frees an I2C bus that a slave is holding low (SDA stuck low, e.g. after an MCU reset mid-transfer) by clocking SCL manually and issuing a STOP condition before Wire starts.

**How it works.**

1. pinMode(sda, INPUT_PULLUP), pinMode(scl, INPUT_PULLUP), delayMicroseconds(10).
2. If digitalRead(sda) is high, the bus is idle and the function returns.
3. pinMode(scl, OUTPUT). Up to 9 times, while SDA still reads low: SCL LOW, wait 5 us, SCL HIGH, wait 5 us (a ~100 kHz clock). Nine clocks are enough to finish any byte in progress (8 data bits + ACK).
4. STOP condition (SDA low -> high while SCL is high): pinMode(sda, OUTPUT), SDA LOW, wait 5 us, SCL HIGH, wait 5 us, SDA HIGH, wait 5 us.
5. Release both lines: pinMode(sda, INPUT_PULLUP), pinMode(scl, INPUT_PULLUP).

**Notes.** Declared at hal.h:38. The lines are driven as Arduino OUTPUT pins with digitalWrite HIGH/LOW, not emulated open-drain. There is no result and no check that SDA was actually released. main.cpp initSensors() calls it with KMAN_I2C_SDA_PIN / KMAN_I2C_SCL_PIN before Wire.setPins/begin.

#### `hal::hostConnected`
*function* · [`firmware/src/hal/hal_nrf52.cpp:212`](../firmware/src/hal/hal_nrf52.cpp#L212)

```cpp
bool hal::hostConnected()
```

**Purpose.** Tells whether the host has the USB CDC port open.

**How it works.** Returns Serial.dtr(), the TinyUSB CDC DTR line state.

**Notes.** Declared at hal.h:41. main.cpp serviceHost() starts streaming (startStreaming(): new stream_id, header and status pending) on the transition to connected, and stops streaming and clears the TX ring on disconnect.

#### `hal::hostWritable`
*function* · [`firmware/src/hal/hal_nrf52.cpp:213`](../firmware/src/hal/hal_nrf52.cpp#L213)

```cpp
size_t hal::hostWritable()
```

**Purpose.** Returns how many bytes can be written to the host link without blocking.

**How it works.** n = Serial.availableForWrite(); returns n if n > 0, else 0. Negative values are clamped to 0.

**Notes.** Declared at hal.h:42. pumpTx() never passes hostWrite() more than this.

#### `hal::hostWrite`
*function* · [`firmware/src/hal/hal_nrf52.cpp:217`](../firmware/src/hal/hal_nrf52.cpp#L217)

```cpp
size_t hal::hostWrite(const uint8_t* data, size_t n)
```

**Purpose.** Queues bytes into the USB CDC TX FIFO.

**How it works.** Returns Serial.write(data, n), the number of bytes accepted.

**Notes.** Declared at hal.h:43. The contract comment there says it never blocks if n &lt;= hostWritable(). pumpTx() consumes exactly the returned count from the TX ring and stops on a short write.

#### `hal::hostFlush`
*function* · [`firmware/src/hal/hal_nrf52.cpp:218`](../firmware/src/hal/hal_nrf52.cpp#L218)

```cpp
void hal::hostFlush()
```

**Purpose.** Starts transmission of a partially filled USB packet, so frames do not wait for a full packet.

**How it works.** Calls Serial.flush(). The comment says that with TinyUSB this is non-blocking and only queues the partial packet.

**Notes.** Declared at hal.h:44. pumpTx() calls it once per pass, only if anything was written.

#### `hal::hostRead`
*function* · [`firmware/src/hal/hal_nrf52.cpp:219`](../firmware/src/hal/hal_nrf52.cpp#L219)

```cpp
int hal::hostRead()
```

**Purpose.** Non-blocking read of one byte from the host.

**How it works.** Returns Serial.read() (0..255) if Serial.available() is non-zero, else -1.

**Notes.** Declared at hal.h:45. serviceHost() drains it in a loop and acts on 'H' (proto::kCmdSendHeader, re-send header) and 'S' (proto::kCmdSendStatus, send status now); other bytes are ignored.

#### `hal::ledInit`
*function* · [`firmware/src/hal/hal_nrf52.cpp:223`](../firmware/src/hal/hal_nrf52.cpp#L223)

```cpp
void hal::ledInit(uint8_t pin)
```

**Purpose.** Configures an LED pin as an output and turns the LED off.

**How it works.** pinMode(pin, OUTPUT), then led(pin, false).

**Notes.** Declared at hal.h:48. setup() calls it for KMAN_LED_OK_PIN and KMAN_LED_ERR_PIN (LED_GREEN / LED_RED by default). Calling it twice for the same pin is harmless.

#### `hal::led`
*function* · [`firmware/src/hal/hal_nrf52.cpp:228`](../firmware/src/hal/hal_nrf52.cpp#L228)

```cpp
void hal::led(uint8_t pin, bool on)
```

**Purpose.** Sets an LED on or off, taking the board's LED polarity into account.

**How it works.** digitalWrite(pin, (on != (bool)KMAN_LED_ACTIVE_LOW) ? HIGH : LOW), an XOR with the polarity. With the default KMAN_LED_ACTIVE_LOW = 1 (XIAO RGB LED is common-anode), on drives the pin LOW and off drives it HIGH.

**Notes.** Declared at hal.h:49. updateLeds() in main.cpp calls it every loop pass: ERR solid when a configured sensor failed init, OK blinking at 2 Hz while streaming or a 50 ms blip every 2 s while waiting for the host.

## 7. Firmware: sensor driver interface and drivers

- **`firmware/src/sensors/sensor.h`**: Defines the ChannelInfo descriptor and the abstract Sensor base class that every sensor driver implements. main.cpp builds the concrete drivers as static globals, stores them as Sensor\* in its g_slots[] table and after that uses only this interface: init() once at boot; read() once per served sample tick while streaming, into a local int16 channel array in sampleAndQueue() that is then packed into the DATA frame; channelCount() for payload offsets; and channelCount()/channel()/describeConfig()/lastError()/warning() to build the JSON session header. The header comment says that replacing the flex sensor with another sensor type means writing another Sensor subclass and that nothing else in the firmware changes. That is true of the sampling and header code, but main.cpp's static driver declaration (g_neckFlex is declared as FlexAdc with FlexAdc constructor arguments), and possibly config.h, would still have to be edited to create the new subclass.
- **`firmware/src/sensors/lsm6dsox.h`**: Declares Lsm6dsox, the register-level I2C driver for the ST LSM6DSOX 6-axis IMU, with its private I2C helpers (writeReg, readRegs, writeVerify), its configuration state and inline accessors. The header comment states the operating model: the IMU free-runs at an ODR of at least 2x the frame rate with its digital low-pass filters well below the frame Nyquist frequency, each tick reads the latest output registers in one burst, Block Data Update keeps high/low bytes coherent, and the FIFO is not used because it would add a second clock domain (the IMU oscillator is only +-1.5 %).
- **`firmware/src/sensors/lsm6dsox.cpp`**: Implements the LSM6DSOX driver: the register map and bit constants (DS12814 rev. 4), ODR and full-scale/sensitivity selection in the constructor, the boot sequence in init() (WHO_AM_I probe, software reset, write-and-verify of CTRL3_C/CTRL9_XL/CTRL8_XL/CTRL6_C/CTRL4_C/CTRL1_XL/CTRL2_G, settle, test read), the per-tick 6- or 12-byte burst read, and the JSON description of the configuration. It talks to the chip only through Arduino TwoWire calls.
- **`firmware/src/sensors/flex_adc.h`**: Declares FlexAdc, the driver for a resistive sensor (flex sensor today; a stretch sensor or FSR works unchanged) in a voltage divider read by the MCU ADC. It exposes one channel of raw ADC counts; conversion to resistance or angle is left to the host using the divider metadata the driver puts in the session header. Its init never fails; it can only raise a non-fatal warning.
- **`firmware/src/sensors/flex_adc.cpp`**: Implements FlexAdc: the constructor fills the single channel descriptor ("raw", "counts", scale 1), init() configures the ADC through the HAL and checks whether 8 readings are stuck at a rail, read() takes one ADC conversion per tick, and describeConfig() emits the sensor description, fixed resistor, topology, pin and the HAL's ADC settings. All MCU-specific ADC work goes through hal::adcInit/adcRead/adcFullScale/describeAdc.

**How it connects.** The only firmware caller is firmware/src/main.cpp. It builds the drivers as static globals from config.h macros (g_chestImu, g_abdomenImu and g_neckImu as Lsm6dsox on Wire; g_neckFlex as FlexAdc) and stores them as Sensor\* in the g_slots[] table. setup() calls initSensors(), which calls hal::i2cBusRecover(), Wire.begin() and Wire.setClock(KMAN_I2C_HZ) and then calls drv->init() once for each configured slot. On true the slot becomes kOk and its bit is set in g_presentMask. On false the slot becomes kInitFailed and g_sensorError is set, which turns on the ERR LED and sets the STATUS flag kStatusFlagSensorError. init() is never retried. buildHeaderFrame() writes one "sensors" object per slot. For every configured slot, including failed ones, it calls describeConfig(); it adds lastError() as "error" only for kInitFailed slots and warning() only when it is non-empty. For kOk slots only, it then uses channelCount() and channel() to write the "channels" array. Each channel entry has the name prefix_suffix (via JsonWriter::kvJoined), then slot, bit, unit and the scale (via kvSci). Sampling is not done on every timer tick. loop() calls sampleAndQueue() only while streaming (the host has the port open) and no header is pending, and only once per loop pass for the latest tick: missed ticks are counted as overruns, not sampled. sampleAndQueue() uses channelCount() of the kOk slots to compute payload offsets. It then calls read(&ch[offset]) on each kOk slot in the order flex, chest, abdomen, neck, writing into a local int16 array. A false return zeroes those channels, leaves the slot's bit unset in the frame's valid mask and increments g_status.read_errors[i]. Going down the stack, Lsm6dsox calls Arduino TwoWire (beginTransmission, write, endTransmission, requestFrom, read) and delay(). FlexAdc calls hal::adcInit, hal::adcRead, hal::adcFullScale and hal::describeAdc in firmware/src/hal/hal_nrf52.cpp. These wrap analogReference(AR_VDD4), analogReadResolution, analogOversampling, analogSampleTime and analogRead. For JSON output, Lsm6dsox::describeConfig uses JsonWriter::kv, kvHex and kvSci; FlexAdc::describeConfig and hal::describeAdc use only kv (firmware/src/json_writer.h). On the host, kman_host.protocol.SessionHeader.from_json parses the "channels" entries into Channel(name, slot, bit, unit, scale), and data_dtype() turns them into int16 fields. LiveProcessor.process in host/live_view.py, Session.physical in host/kman_host/session_io.py and to_uniform in analysis/kman_analysis/preprocess.py multiply raw counts by Channel.scale. SessionHeader.describe prints each slot's "error" and "warning". kman_host.simulator.build_header does not call or copy the firmware. It hard-codes an imitation of these drivers' describeConfig output for the default configuration (208 Hz ODR, +-2 g, 250 dps; flex with "adc":"simulated" and pin 0) and the default scales (ACCEL_SCALE = 61e-6, GYRO_SCALE = 8.75e-3, flex scale 1).

### `firmware/src/sensors/flex_adc.cpp`

#### `FlexAdc::FlexAdc`
*constructor* · [`firmware/src/sensors/flex_adc.cpp:5`](../firmware/src/sensors/flex_adc.cpp#L5)

```cpp
FlexAdc(uint8_t pin, const char* sensorDesc, uint32_t rFixedOhm, const char* topology)
```

**Purpose.** Stores the pin and divider metadata and fills the channel descriptor; does not touch hardware, so it can be a static global.

**How it works.** Initialises pin_, desc_, rFixedOhm_, topology_ from the arguments and warning_ = "". Sets channel_ = {suffix "raw", unit "counts", scaleMantissa 1, scaleExp10 0}, so the header announces scale 1 (raw counts).

**Notes.** The string pointers are stored, not copied; main.cpp passes string literals from config.h.

#### `FlexAdc::init`
*method (override)* · [`firmware/src/sensors/flex_adc.cpp:13`](../firmware/src/sensors/flex_adc.cpp#L13)

```cpp
bool init() override
```

**Purpose.** Configure the ADC and check whether the divider looks open or shorted. It always succeeds; a suspicious reading only sets a warning.

**How it works.**

1. hal::adcInit(): on nRF52 sets reference VDD/4 with gain 1/4 (ratiometric, full scale = VDD), resolution KMAN_ADC_BITS (14), oversampling KMAN_ADC_OVERSAMPLE (16, burst mode) and acquisition time KMAN_ADC_TACQ_US (10 us).
2. fs = hal::adcFullScale() = 2^bits - 1 (16383 at 14 bits); margin = fs / 50 in integer division (327 at 14 bits, about 2 %).
3. Take 8 readings v = hal::adcRead(pin_) back to back; count atRail for each v &lt;= margin or v >= fs - margin (&lt;= 327 or >= 16056 at 14 bits).
4. warning_ = "reading at ADC rail: sensor open/shorted or divider not powered?" if all 8 readings were at a rail, else "".
5. Return true.

**Notes.** The code comment gives the rationale: an ADC cannot prove a divider is wired, but a reading pinned to a rail almost always means an open or shorted sensor, so it reports this and keeps streaming. Readings may be split between the low and high rail and still count. Because init() always returns true, a configured flex slot is always kOk and its lastError() is always "". The ADC is configured here, so it is set up only when the neck_flex slot is enabled.

#### `FlexAdc::read`
*method (override)* · [`firmware/src/sensors/flex_adc.cpp:28`](../firmware/src/sensors/flex_adc.cpp#L28)

```cpp
bool read(int16_t* out) override
```

**Purpose.** Per-tick sample: one ADC conversion of the divider voltage.

**How it works.** out[0] = (int16_t)hal::adcRead(pin_) (analogRead on nRF52, with the oversampling set in init); returns true.

**Notes.** Never reports failure. 14-bit counts (0..16383) fit in int16 without loss. main.cpp reads this slot first each tick (about 0.2 ms per its comment) so all samples sit close to the tick time.

#### `FlexAdc::describeConfig`
*method (override)* · [`firmware/src/sensors/flex_adc.cpp:33`](../firmware/src/sensors/flex_adc.cpp#L33)

```cpp
void describeConfig(JsonWriter& w) const override
```

**Purpose.** Write the divider metadata and ADC settings into the slot's header object so the host can turn counts into resistance or angle.

**How it works.** Emits "model":"resistive divider", "sensor": desc_, "r_fixed_ohm": rFixedOhm_, "topology": topology_, "pin": (int32_t)pin_, then calls hal::describeAdc(w), which on nRF52 adds "adc":"nRF52840 SAADC", "reference":"VDD/4, gain 1/4 (ratiometric, full scale = VDD)", "bits", "oversample", "tacq_us" and "full_scale".

**Notes.** "pin" is the Arduino pin number passed to the constructor, not the SAADC AIN index. With the default topology and ratiometric reference, counts are about full_scale \* R_fixed / (R_flex + R_fixed). No host or analysis code in the repository performs that conversion today: analysis/kman_analysis/swallow.py works on flex_raw counts directly.

### `firmware/src/sensors/flex_adc.h`

#### `FlexAdc`
*class* · [`firmware/src/sensors/flex_adc.h:8`](../firmware/src/sensors/flex_adc.h#L8)

```cpp
class FlexAdc : public Sensor
```

**Purpose.** Sensor implementation for a resistive sensor in a voltage divider, read by the MCU ADC as one channel of raw counts.

**How it works.** Private fields: pin_ (Arduino analog pin number, KMAN_FLEX_PIN = A0 by default); desc_ (free-text sensor description for the header); rFixedOhm_ (fixed divider resistor in ohms, 47000 by default); topology_ (text describing the divider wiring, default "3V3 - flex - ADC - R_fixed - GND"); warning_ (rail-check result from init(), "" if fine); channel_ (the single ChannelInfo). Inline overrides: channelCount() = 1, channel() returns channel_, lastError() returns "", warning() returns warning_.

**Notes.** main.cpp creates one static instance, g_neckFlex, in slot "neck_flex" with prefix "flex", so its channel is named "flex_raw".

#### `FlexAdc::channelCount`
*inline method (override)* · [`firmware/src/sensors/flex_adc.h:14`](../firmware/src/sensors/flex_adc.h#L14)

```cpp
uint8_t channelCount() const override { return 1; }
```

**Purpose.** The flex driver contributes one channel per frame.

**How it works.** Returns 1.

#### `FlexAdc::channel`
*inline method (override)* · [`firmware/src/sensors/flex_adc.h:15`](../firmware/src/sensors/flex_adc.h#L15)

```cpp
const ChannelInfo& channel(uint8_t) const override { return channel_; }
```

**Purpose.** Descriptor of the single raw-counts channel.

**How it works.** Ignores the index and returns channel_.

#### `FlexAdc::lastError`
*inline method (override)* · [`firmware/src/sensors/flex_adc.h:17`](../firmware/src/sensors/flex_adc.h#L17)

```cpp
const char* lastError() const override { return ""; }
```

**Purpose.** The driver has no failure modes, so there is never an error message.

**How it works.** Always returns "".

#### `FlexAdc::warning`
*inline method (override)* · [`firmware/src/sensors/flex_adc.h:18`](../firmware/src/sensors/flex_adc.h#L18)

```cpp
const char* warning() const override { return warning_; }
```

**Purpose.** Exposes the rail-check result from init() so main.cpp can put it in the session header.

**How it works.** Returns warning_, which is "" or "reading at ADC rail: sensor open/shorted or divider not powered?".

### `firmware/src/sensors/lsm6dsox.cpp`

#### `LSM6DSOX register and bit constants (anonymous namespace)`
*constants* · [`firmware/src/sensors/lsm6dsox.cpp:7`](../firmware/src/sensors/lsm6dsox.cpp#L7)

```cpp
const uint8_t REG_WHO_AM_I = 0x0F; REG_CTRL1_XL = 0x10; REG_CTRL2_G = 0x11; REG_CTRL3_C = 0x12; REG_CTRL4_C = 0x13; REG_CTRL6_C = 0x15; REG_CTRL8_XL = 0x17; REG_CTRL9_XL = 0x18; REG_OUTX_L_G = 0x22; REG_OUTX_L_A = 0x28; WHO_AM_I_VALUE = 0x6C; CTRL3_SW_RESET = 0x01; CTRL3_IF_INC = 0x04; CTRL3_BDU = 0x40; CTRL1_LPF2_XL_EN = 0x02; CTRL4_LPF1_SEL_G = 0x02; CTRL9_I3C_DISABLE = 0x02; CTRL8_HPCF_ODR_DIV10 = 0x20; CTRL6_FTYPE = 0x6;
```

**Purpose.** File-private register addresses and bit masks used by the driver, taken from datasheet DS12814 rev. 4.

**How it works.** Registers: WHO_AM_I 0x0F (expected 0x6C); CTRL1_XL 0x10 (accel ODR/full scale/LPF2 enable); CTRL2_G 0x11 (gyro ODR/full scale); CTRL3_C 0x12 (reset, BDU, auto-increment); CTRL4_C 0x13 (gyro LPF1 enable); CTRL6_C 0x15 (gyro LPF1 bandwidth FTYPE, accel high-performance mode); CTRL8_XL 0x17 (accel LPF2 cutoff); CTRL9_XL 0x18 (I3C disable); OUTX_L_G 0x22 (start of 12 output bytes: gyro X/Y/Z then accel X/Y/Z, each little-endian); OUTX_L_A 0x28 (start of the 6 accel bytes). Bits: CTRL3_SW_RESET 0x01 (bit 0, software reset, self-clearing); CTRL3_IF_INC 0x04 (bit 2, register address auto-increment on multi-byte access); CTRL3_BDU 0x40 (bit 6, Block Data Update: output registers are not updated until both bytes have been read); CTRL1_LPF2_XL_EN 0x02 (bit 1, accel second-stage low-pass LPF2 on the output path); CTRL4_LPF1_SEL_G 0x02 (bit 1, enables gyro LPF1); CTRL9_I3C_DISABLE 0x02 (bit 1, disables the MIPI I3C interface); CTRL8_HPCF_ODR_DIV10 = 0x1 &lt;&lt; 5 = 0x20 (HPCF_XL bits 7:5 = 001 with HP_SLOPE_XL_EN = 0, giving LPF2 cutoff ODR/10, i.e. 20.8 Hz at 208 Hz ODR); CTRL6_FTYPE = 0x6 (FTYPE bits 2:0 = 110, about 26 Hz gyro LPF1 bandwidth at 208 Hz ODR per the datasheet table cited in the comment).

**Notes.** REG_OUTX_L_G + 12 bytes covers 0x22..0x2D, which only works as one burst because IF_INC is set.

#### `OdrEntry / kOdrs`
*struct + constant table* · [`firmware/src/sensors/lsm6dsox.cpp:32`](../firmware/src/sensors/lsm6dsox.cpp#L32)

```cpp
struct OdrEntry { uint16_t hz; uint8_t code; }; const OdrEntry kOdrs[] = {{208, 0x5}, {416, 0x6}, {833, 0x7}};
```

**Purpose.** Lookup table of the output data rates the driver may choose, with the 4-bit ODR register code for each.

**How it works.** Fields: hz (ODR in Hz), code (value for the ODR_XL / ODR_G nibble, bits 7:4 of CTRL1_XL and CTRL2_G). Entries in ascending order: 208 Hz -> 0x5 (0101), 416 Hz -> 0x6 (0110), 833 Hz -> 0x7 (0111). The constructor picks the first entry whose hz >= 2 \* frameRateHz.

**Notes.** Lower ODRs (e.g. 104 Hz) are not in the table, so the minimum ODR is 208 Hz even at low frame rates.

#### `Lsm6dsox::Lsm6dsox`
*constructor* · [`firmware/src/sensors/lsm6dsox.cpp:40`](../firmware/src/sensors/lsm6dsox.cpp#L40)

```cpp
Lsm6dsox(TwoWire& bus, uint8_t addr, bool accelOnly, uint16_t frameRateHz, uint8_t accelRangeG, uint16_t gyroRangeDps)
```

**Purpose.** Stores bus and address, and computes everything that depends only on configuration (ODR code, full-scale register bits, sensitivity, channel descriptors) without touching the bus, so the object can be a static global.

**How it works.**

1. Member initialisers: bus_, addr_, accelOnly_ from arguments; odrHz_ = 833, odrCode_ = 0x7 (fallback if no table entry fits); accelRangeG_, gyroRangeDps_ from arguments; fsXlBits_ = fsGBits_ = 0; err_ = "".
2. ODR selection: loop over kOdrs; the first entry with kOdrs[i].hz >= 2u \* frameRateHz sets odrHz_ and odrCode_ and breaks. Rationale in the comment: with ODR >= 2x frame rate, the latest register value read at a tick is at most half a frame period old. Results: frame rate 1..104 Hz -> 208 Hz (0x5); 105..208 Hz -> 416 Hz (0x6); 209..416 Hz -> 833 Hz (0x7). Default config (KMAN_SAMPLE_RATE_HZ = 100) -> 208 Hz.
3. Accel full scale and sensitivity (micro-g per LSB, local accelUg), mapped to FS_XL bits 3:2 of CTRL1_XL: 4 -> fsXlBits_ 0x2, 122 ug/LSB; 8 -> 0x3, 244; 16 -> 0x1, 488; any other value (including 2) -> 0x0, 61, and accelRangeG_ is forced to 2.
4. Gyro full scale and sensitivity (micro-dps per LSB, local gyroUdps). fsGBits_ is a 3-bit field [FS1_G FS0_G FS_125] that init() shifts left by 1 into CTRL2_G bits 3:1: 125 -> 0x1 (FS_125 bit), 4375 udps/LSB; 500 -> 0x1 &lt;&lt; 1 = 0x2 (FS_G = 01), 17500; 1000 -> 0x2 &lt;&lt; 1 = 0x4 (FS_G = 10), 35000; 2000 -> 0x3 &lt;&lt; 1 = 0x6 (FS_G = 11), 70000; any other value (including 250) -> 0x0 (FS_G = 00), 8750, and gyroRangeDps_ is forced to 250.
5. Channel descriptors: for i = 0..5, channels_[i].suffix = {"ax","ay","az","gx","gy","gz"}[i] (static local kSuffix); unit = "g" for i &lt; 3 else "dps"; scaleMantissa = accelUg for i &lt; 3 else gyroUdps; scaleExp10 = -6. So at +-2 g, 1 LSB = 61e-6 g (1 g is about 16393 counts); at 250 dps, 1 LSB = 8750e-6 = 8.75e-3 dps.

**Notes.** Invalid range arguments are silently replaced by the defaults (2 g / 250 dps), and describeConfig() reports the effective values. config.h static_asserts 1 &lt;= KMAN_SAMPLE_RATE_HZ &lt;= 416, so the loop always finds an entry in normal builds; the 833 Hz initial value is only a fallback. The FS_XL mapping (16 g = 01) assumes XL_FS_MODE = 0 in CTRL8_XL, which init() guarantees by writing CTRL8_XL = 0x20. All six descriptors are filled even when accelOnly is true.

#### `Lsm6dsox::writeReg`
*private method* · [`firmware/src/sensors/lsm6dsox.cpp:88`](../firmware/src/sensors/lsm6dsox.cpp#L88)

```cpp
bool writeReg(uint8_t reg, uint8_t value)
```

**Purpose.** Write one byte to one IMU register over I2C.

**How it works.** bus_.beginTransmission(addr_); bus_.write(reg); bus_.write(value); returns bus_.endTransmission() == 0 (Arduino Wire returns 0 on success, non-zero on NACK or bus error). The transaction ends with a STOP.

**Notes.** No read-back; use writeVerify for configuration registers.

#### `Lsm6dsox::readRegs`
*private method* · [`firmware/src/sensors/lsm6dsox.cpp:95`](../firmware/src/sensors/lsm6dsox.cpp#L95)

```cpp
bool readRegs(uint8_t reg, uint8_t* buf, uint8_t n)
```

**Purpose.** Read n consecutive bytes starting at register reg (one I2C burst).

**How it works.**

1. bus_.beginTransmission(addr_); bus_.write(reg).
2. bus_.endTransmission(false): sends the register address without a STOP so the read follows with a repeated START; non-zero return -> return false.
3. bus_.requestFrom(addr_, n); if it does not return exactly n -> return false.
4. Copy n bytes with buf[i] = (uint8_t)bus_.read().
5. Return true.

**Notes.** Multi-byte reads rely on CTRL3_C.IF_INC (address auto-increment), which is the chip's reset default and is written again in init().

#### `Lsm6dsox::writeVerify`
*private method* · [`firmware/src/sensors/lsm6dsox.cpp:104`](../firmware/src/sensors/lsm6dsox.cpp#L104)

```cpp
bool writeVerify(uint8_t reg, uint8_t value)
```

**Purpose.** Write a configuration register and confirm the chip holds the written value.

**How it works.** back = 0; returns writeReg(reg, value) && readRegs(reg, &back, 1) && back == value. Short-circuit: a failed write skips the read.

**Notes.** Not suitable for registers with self-clearing bits; init() writes the SW_RESET bit with plain writeReg and polls instead.

#### `Lsm6dsox::init`
*method (override)* · [`firmware/src/sensors/lsm6dsox.cpp:109`](../firmware/src/sensors/lsm6dsox.cpp#L109)

```cpp
bool init() override
```

**Purpose.** Boot-time probe, reset and configuration of the IMU, ending with one test read. Returns false and sets err_ on any failure.

**How it works.**

1. Probe: readRegs(0x0F WHO_AM_I, &who, 1). Bus failure -> err_ = "no I2C ACK (check wiring, pull-ups and SA0 address)", return false. who != 0x6C -> err_ = "unexpected WHO_AM_I (not an LSM6DSOX?)", return false.
2. Software reset: writeReg(CTRL3_C 0x12, 0x01). Failure -> "reset write failed".
3. Wait for reset: ctrl3 = 0x01; loop up to 50 times while bit 0 is set: delay(1) then readRegs(CTRL3_C); a failed read sets ctrl3 = 0x01 so polling continues. If bit 0 is still set after the loop (about 50 ms) -> "software reset did not complete".
4. Configuration, one && chain (stops at the first failure):
  a. writeVerify(CTRL3_C, 0x44) = BDU (bit 6) | IF_INC (bit 2). All other bits 0 (BOOT, H_LACTIVE, PP_OD, SIM, SW_RESET).
  b. readRegs(CTRL9_XL 0x18, &ctrl9, 1), then writeVerify(CTRL9_XL, ctrl9 | 0x02): read-modify-write that sets I3C_disable (bit 1) and keeps the other bits.
  c. writeVerify(CTRL8_XL 0x17, 0x20): HPCF_XL (bits 7:5) = 001, HP_SLOPE_XL_EN = 0, XL_FS_MODE = 0, so the accel LPF2 cutoff is ODR/10 (20.8 Hz at 208 Hz ODR).
  d. writeVerify(CTRL6_C 0x15, 0x06): FTYPE (bits 2:0) = 110 selects the gyro LPF1 bandwidth (about 26 Hz at 208 Hz ODR per the code comment); XL_HM_MODE (bit 4) = 0 keeps the accelerometer in high-performance mode.
  e. writeVerify(CTRL4_C 0x13, accelOnly_ ? 0x00 : 0x02): LPF1_SEL_G (bit 1) enables gyro LPF1 when the gyro is used.
  f. writeVerify(CTRL1_XL 0x10, (odrCode_ &lt;&lt; 4) | (fsXlBits_ &lt;&lt; 2) | 0x02): ODR_XL in bits 7:4, FS_XL in bits 3:2, LPF2_XL_EN in bit 1. Default config (208 Hz, +-2 g) -> 0x52.
  g. writeVerify(CTRL2_G 0x11, accelOnly_ ? 0x00 : (odrCode_ &lt;&lt; 4) | (fsGBits_ &lt;&lt; 1)): ODR_G in bits 7:4, FS_G in bits 3:2, FS_125 in bit 1. Default (208 Hz, 250 dps) -> 0x50; 0x00 puts the gyro in power-down.
  Any failure -> err_ = "configuration write/verify failed", return false.
5. delay(100): gyro turn-on time plus LPF settling (boot only).
6. Test read: read(probe) into a local int16_t probe[6]; failure -> err_ = "first data read failed", return false.
7. err_ = "", return true.

**Notes.** The filters (CTRL8_XL, CTRL6_C, CTRL4_C) are configured before the ODR registers, so the sensors start sampling only in the last two writes. The configuration error message does not say which register failed. CTRL7_G (gyro high-performance mode) is not written and stays at its reset value. init() blocks for up to about 150 ms (reset poll plus 100 ms settle) and is only called at boot by main.cpp initSensors(), after Wire.begin() and Wire.setClock(KMAN_I2C_HZ).

#### `Lsm6dsox::read`
*method (override)* · [`firmware/src/sensors/lsm6dsox.cpp:158`](../firmware/src/sensors/lsm6dsox.cpp#L158)

```cpp
bool read(int16_t* out) override
```

**Purpose.** Per-tick sample: read the latest output registers in one I2C burst and write the raw values in frame order (accel, then gyro).

**How it works.**

Local buffer uint8_t b[12].
Accel-only path (accelOnly_): readRegs(0x28 OUTX_L_A, b, 6); failure -> err_ = "accel read failed", return false. Else for i = 0..2: out[i] = (int16_t)(b[2i] | (b[2i+1] &lt;&lt; 8)) (little-endian X, Y, Z). Return true.
Full path: readRegs(0x22 OUTX_L_G, b, 12); failure -> err_ = "data read failed", return false. The registers hold gyro X/Y/Z in b[0..5] then accel X/Y/Z in b[6..11]; frames carry accel first, so for i = 0..2: out[i] = (int16_t)(b[6+2i] | (b[7+2i] &lt;&lt; 8)) (accel) and out[3+i] = (int16_t)(b[2i] | (b[2i+1] &lt;&lt; 8)) (gyro). Return true.

**Notes.** No allocation and no delay. main.cpp's comment puts the 12-byte burst at about 0.35 ms at 400 kHz. BDU (set in init) keeps each axis's low and high bytes from the same sample. A successful read does not clear err_, so after a runtime read failure lastError() keeps the old message; main.cpp reports runtime failures through STATUS read_errors counters, not through lastError().

#### `Lsm6dsox::describeConfig`
*method (override)* · [`firmware/src/sensors/lsm6dsox.cpp:180`](../firmware/src/sensors/lsm6dsox.cpp#L180)

```cpp
void describeConfig(JsonWriter& w) const override
```

**Purpose.** Write this IMU's configuration into the slot's object in the session header so a recording documents its own sensor settings.

**How it works.** Emits, in order: "model":"LSM6DSOX"; "bus":"i2c"; "addr" via kvHex (e.g. "0x6A"); "mode":"timer-master register read (no FIFO)"; "odr_hz" (odrHz_); "accel_range_g" (accelRangeG_); "accel_lpf":"LPF2 ODR/10"; "accel_lpf_hz" via kvSci(odrHz_, -1), e.g. 208e-1 = 20.8. If !accelOnly_: "gyro_range_dps" (gyroRangeDps_) and "gyro_lpf":"LPF1 FTYPE=6 (~26 Hz at 208/416 Hz ODR)". Finally "block_data_update":true.

**Notes.** Values come from the constructor's configuration, not from reading the chip, so they are emitted even when init() failed. The gyro_lpf text is a fixed string and does not change with the selected ODR.

### `firmware/src/sensors/lsm6dsox.h`

#### `Lsm6dsox`
*class* · [`firmware/src/sensors/lsm6dsox.h:14`](../firmware/src/sensors/lsm6dsox.h#L14)

```cpp
class Lsm6dsox : public Sensor
```

**Purpose.** Sensor implementation for one ST LSM6DSOX IMU on an I2C bus, producing either 3 accel channels or 3 accel + 3 gyro channels per frame.

**How it works.** Private fields: bus_ (TwoWire&, the I2C bus, Wire in main.cpp); addr_ (7-bit I2C address, 0x6A with SA0 low or 0x6B with SA0 high per config.h); accelOnly_ (true = gyro powered down, 3 channels); odrHz_ and odrCode_ (selected output data rate in Hz and its 4-bit register code); accelRangeG_ and gyroRangeDps_ (effective full-scale ranges after fallback to defaults); fsXlBits_ (2-bit FS_XL code for CTRL1_XL); fsGBits_ (3-bit [FS1_G FS0_G FS_125] code for CTRL2_G); err_ (last error string, "" if none); channels_[6] (ChannelInfo for ax, ay, az, gx, gy, gz; only the first 3 are exposed when accelOnly_). Public: constructor, init, read, describeConfig and inline channelCount/channel/lastError. Private helpers: writeReg, readRegs, writeVerify.

**Notes.** main.cpp creates three static instances: g_chestImu, g_abdomenImu and g_neckImu (the neck one may be accel-only via KMAN_NECK_IMU_ACCEL_ONLY).

#### `Lsm6dsox::channelCount`
*inline method (override)* · [`firmware/src/sensors/lsm6dsox.h:21`](../firmware/src/sensors/lsm6dsox.h#L21)

```cpp
uint8_t channelCount() const override { return accelOnly_ ? 3 : 6; }
```

**Purpose.** Number of channels this IMU contributes per frame.

**How it works.** Returns 3 when accelOnly_ is true, otherwise 6.

#### `Lsm6dsox::channel`
*inline method (override)* · [`firmware/src/sensors/lsm6dsox.h:22`](../firmware/src/sensors/lsm6dsox.h#L22)

```cpp
const ChannelInfo& channel(uint8_t i) const override { return channels_[i]; }
```

**Purpose.** Descriptor of channel i (ax, ay, az, gx, gy, gz).

**How it works.** Returns channels_[i] with no bounds check.

**Notes.** Valid for i &lt; channelCount(); index 3..5 exist in the array even in accel-only mode but are never requested by main.cpp.

#### `Lsm6dsox::lastError`
*inline method (override)* · [`firmware/src/sensors/lsm6dsox.h:24`](../firmware/src/sensors/lsm6dsox.h#L24)

```cpp
const char* lastError() const override { return err_; }
```

**Purpose.** Last init/read failure message.

**How it works.** Returns err_ ("" after a successful init()).

### `firmware/src/sensors/sensor.h`

#### `ChannelInfo`
*struct* · [`firmware/src/sensors/sensor.h:10`](../firmware/src/sensors/sensor.h#L10)

```cpp
struct ChannelInfo { const char* suffix; const char* unit; int32_t scaleMantissa; int8_t scaleExp10; };
```

**Purpose.** Describes one int16 output channel of a driver: its name suffix, physical unit and raw-to-physical scale factor. main.cpp turns each one into an entry of the session header's "channels" array.

**How it works.** Fields: (1) suffix: joined with the slot prefix by '_' to form the channel name, e.g. "chest" + "ax" -> "chest_ax" (main.cpp uses JsonWriter::kvJoined for this). (2) unit: physical unit after scaling ("g", "dps", "counts"). (3) scaleMantissa and (4) scaleExp10: physical = raw \* scaleMantissa \* 10^scaleExp10. Example: Lsm6dsox at +-2 g uses {61, -6}, so 1 LSB = 61e-6 g; FlexAdc uses {1, 0}.

**Notes.** The scale is stored as an integer mantissa/exponent pair because JsonWriter does not print floats (newlib-nano printf has no %f); JsonWriter::kvSci prints it as e.g. "61e-6", which is valid JSON and parsed as a float by the host (kman_host.protocol.SessionHeader.from_json).

#### `Sensor`
*abstract class* · [`firmware/src/sensors/sensor.h:17`](../firmware/src/sensors/sensor.h#L17)

```cpp
class Sensor { public: virtual bool init() = 0; virtual bool read(int16_t* out) = 0; virtual uint8_t channelCount() const = 0; virtual const ChannelInfo& channel(uint8_t i) const = 0; virtual void describeConfig(JsonWriter& w) const = 0; virtual const char* lastError() const = 0; virtual const char* warning() const; protected: ~Sensor(); };
```

**Purpose.** Common interface for all sensor drivers so main.cpp can initialise, sample and describe every slot (IMUs, flex ADC) through a Sensor\* without knowing the concrete type.

**How it works.** Has no data members. Six pure virtual methods (init, read, channelCount, channel, describeConfig, lastError) and one virtual with a default body (warning returns ""). The destructor is protected and non-virtual (see Sensor::~Sensor). A driver produces a fixed number of int16 channels per frame.

**Notes.** Subclasses in this group: Lsm6dsox (3 or 6 channels) and FlexAdc (1 channel).

#### `Sensor::init`
*pure virtual method* · [`firmware/src/sensors/sensor.h:21`](../firmware/src/sensors/sensor.h#L21)

```cpp
virtual bool init() = 0;
```

**Purpose.** Probe the device and apply its configuration once at boot.

**How it works.** Contract: return true if the device is present and configured; return false and set lastError() if it is absent or misconfigured. main.cpp initSensors() calls it once per configured slot.

**Notes.** There is no retry: a slot whose init() returns false stays in state kInitFailed for the whole run.

#### `Sensor::read`
*pure virtual method* · [`firmware/src/sensors/sensor.h:25`](../firmware/src/sensors/sensor.h#L25)

```cpp
virtual bool read(int16_t* out) = 0;
```

**Purpose.** Read one sample of channelCount() raw values into out.

**How it works.** Contract: write exactly channelCount() int16 values starting at out; return false on a bus error. It must be fast and allocation-free because it runs once per sample tick (called from main.cpp sampleAndQueue()).

**Notes.** On false, main.cpp zeroes that slot's channels, clears its bit in the frame's valid mask and increments g_status.read_errors for the slot.

#### `Sensor::channelCount`
*pure virtual method* · [`firmware/src/sensors/sensor.h:27`](../firmware/src/sensors/sensor.h#L27)

```cpp
virtual uint8_t channelCount() const = 0;
```

**Purpose.** Number of int16 channels the driver contributes to each DATA frame.

**How it works.** Pure virtual; main.cpp uses it to compute each slot's offset in the frame payload and to iterate channels when writing the header.

**Notes.** Must be constant after construction because the frame layout announced in the header depends on it.

#### `Sensor::channel`
*pure virtual method* · [`firmware/src/sensors/sensor.h:28`](../firmware/src/sensors/sensor.h#L28)

```cpp
virtual const ChannelInfo& channel(uint8_t i) const = 0;
```

**Purpose.** Returns the descriptor (suffix, unit, scale) of channel i.

**How it works.** Pure virtual; called by main.cpp buildHeaderFrame() for i = 0 .. channelCount()-1.

**Notes.** Implementations do not bounds-check i.

#### `Sensor::describeConfig`
*pure virtual method* · [`firmware/src/sensors/sensor.h:31`](../firmware/src/sensors/sensor.h#L31)

```cpp
virtual void describeConfig(JsonWriter& w) const = 0;
```

**Purpose.** Emit the driver's model, ranges and filter settings as JSON members into the JSON object currently open in w.

**How it works.** Pure virtual; main.cpp buildHeaderFrame() opens one object per slot in the "sensors" array, writes slot/bit/status, then calls describeConfig(w) for every configured slot (including slots whose init failed).

**Notes.** Implementations write key/value members only; they must not open or close the enclosing object.

#### `Sensor::lastError`
*pure virtual method* · [`firmware/src/sensors/sensor.h:34`](../firmware/src/sensors/sensor.h#L34)

```cpp
virtual const char* lastError() const = 0;
```

**Purpose.** Human-readable reason for the last init()/read() failure, or "" if none.

**How it works.** Pure virtual; main.cpp writes it into the header as "error" only for slots in state kInitFailed.

**Notes.** Returned pointers are string literals; nothing is allocated.

#### `Sensor::warning`
*virtual method* · [`firmware/src/sensors/sensor.h:37`](../firmware/src/sensors/sensor.h#L37)

```cpp
virtual const char* warning() const { return ""; }
```

**Purpose.** Non-fatal observation made during init (e.g. a reading at an ADC rail), or "" if none.

**How it works.** Default implementation returns "". main.cpp writes it into the header as "warning" when its first character is non-zero (s.drv->warning()[0]).

**Notes.** Lsm6dsox uses the default; FlexAdc overrides it.

#### `Sensor::~Sensor`
*destructor (protected)* · [`firmware/src/sensors/sensor.h:40`](../firmware/src/sensors/sensor.h#L40)

```cpp
protected: ~Sensor() {}
```

**Purpose.** Prevents deleting a driver through a Sensor\* while avoiding a virtual destructor.

**How it works.** Empty, protected, non-virtual. Code outside a subclass cannot call delete on a Sensor\*, so the missing virtual destructor cannot cause undefined behaviour.

**Notes.** The code comment gives the rationale: drivers are static objects (g_chestImu, g_neckFlex, ... in main.cpp) and are never deleted through a base pointer.

## 8. Firmware: wire protocol, TX ring buffer, JSON writer, native test harness

- **`firmware/src/protocol.h`**: Declares the device-to-host wire protocol: sync bytes, frame types, size limits, host command bytes, the Status counter struct with its 42-byte serialized size, and inline little-endian put helpers. It has no Arduino dependency, so the same header compiles into the nRF52840 firmware and into the native test generator. host/kman_host/protocol.py mirrors these definitions in Python.
- **`firmware/src/protocol.cpp`**: Implements the three non-inline protocol functions: bitwise CRC-16/CCITT-FALSE, the frame encoder that wraps a body in sync, length, type and CRC, and the STATUS body serializer. main.cpp calls these for every DATA, HEADER and STATUS frame. The native test compiles this same file to produce golden vectors for the Python decoder.
- **`firmware/src/ring_buffer.h`**: Header-only template ByteRing&lt;N>, a fixed-capacity byte FIFO used as the firmware's TX staging buffer (g_tx, N = KMAN_TX_RING_BYTES = 8192 by default). Encoded frames are pushed whole or not at all, and pumpTx() drains the ring to USB in contiguous chunks as fast as the host link accepts them. This absorbs USB or host stalls without blocking the sampling loop. There is no locking because producer and consumer both run in loop().
- **`firmware/src/json_writer.h`**: Header-only JsonWriter: builds a JSON document in a caller-owned char buffer with no heap and no floating point. Fractional values are written as mantissa and exponent (for example 61e-6) because newlib-nano printf has no %f. It tracks nesting and comma placement, escapes strings, and reports overflow or unclosed containers through ok(). The firmware uses it to build the session-header JSON carried in the HEADER frame.
- **`firmware/test/native_frames.cpp`**: Golden-vector generator compiled for the host. It is built with g++ or clang++ against the firmware's own protocol.cpp and json_writer.h and writes four frames (DATA, STATUS, a HEADER-like JSON, LOG) to stdout. host/tests/test_crosscheck.py decodes them with the Python implementation, checks every field, and checks that the Python encoder reproduces the same bytes.

**How it connects.**

On the firmware side, everything is called from firmware/src/main.cpp. buildHeaderFrame() builds the session-header JSON with JsonWriter (beginObject/endObject, kv, beginArray/endArray, item, kvJoined, kvSci). It calls hal::describeClock(w) directly, and calls each configured sensor's describeConfig(w) through the Sensor interface. Lsm6dsox::describeConfig uses kvHex and kvSci. FlexAdc::describeConfig calls hal::describeAdc(w); buildHeaderFrame() does not call hal::describeAdc itself. buildHeaderFrame() then checks ok() and wraps the text with proto::encodeFrame(kTypeHeader) into g_headerFrame. sampleAndQueue() writes each DATA body with putU32(tickUs), putU16((uint16_t)tickCount), putU8(valid) and putI16 per channel, frames it with encodeFrame(kTypeData) into g_frame, and calls g_tx.push(). A successful push increments g_status.frames_sent; a failed one increments g_status.tx_drops. queueStatusIfDue() fills g_status (a proto::Status), clamps g_tx.highWater() into tx_high_water, calls encodeStatus and then encodeFrame(kTypeStatus), and pushes the frame. Only if the push succeeds does it call g_tx.resetHighWater(). queueHeaderIfPending() pushes the header only when len != 0 and g_tx.free() >= len. startStreaming() calls g_tx.clear() on connect and serviceHost() calls it on disconnect. serviceHost() also maps the bytes kCmdSendHeader ('H') and kCmdSendStatus ('S') read from hal::hostRead() to g_headerPending and g_statusPending. pumpTx() drains the ring with used(), peekContiguous() and consume(), writing through hal::hostWritable() and hal::hostWrite(). It calls hal::hostFlush() only if something was written.

On the host side, host/kman_host/protocol.py mirrors the protocol:
- crc16 via binascii.crc_hqx(data, 0xFFFF)
- encode_frame
- FrameParser.feed, which searches for AA 55, counts len &lt; 1 or len > MAX_LEN as bad_length, and checks the CRC over buf[pos+2:end-2]
- decode_data, plus DATA_PREFIX '&lt;IHB'
- decode_status and encode_status with struct '&lt;B5I4IHHB' and version byte 1

logger.py and live_view.py send CMD_SEND_HEADER (b"H"), and sim_device.py checks incoming bytes for it. host/tests/test_crosscheck.py compiles native_frames.cpp plus protocol.cpp with g++ (or clang++ if g++ is not found) using -std=c++11 -Wall -Wextra -Werror. It runs the binary and feeds its stdout to FrameParser, asserting zero CRC errors. It then checks the DATA prefix and channels, every STATUS field, and the JSON values (quote, scale, lpf, int, addr, neg, big, flag, list; the "type" key is not asserted), and that the LOG body is "overflow-detected". Finally it asserts P.encode_frame(f.type, f.body) == f.raw for each frame, so the C++ and Python encoders must produce identical bytes.

### `firmware/src/json_writer.h`

#### `JsonWriter`
*class* · [`firmware/src/json_writer.h:11`](../firmware/src/json_writer.h#L11)

```cpp
class JsonWriter
```

**Purpose.** Bounded JSON builder over a caller-owned buffer, with no heap and no floats. It produces the session-header JSON sent in HEADER frames.

**How it works.**

Fields:
- buf_: caller buffer.
- cap_: capacity in bytes, including the NUL.
- len_: characters written, excluding the NUL.
- depth_: current container nesting, 0 = top level.
- first_[kMaxDepth]: one flag per depth, true until the first element at that level is written. It controls comma insertion.
- overflow_: sticky error flag. It is set by buffer overflow, by nesting too deep, or by closing at depth 0.
kMaxDepth = 8 (private static const, line 73), which allows container depths 1..7.
Public API: c_str, length and ok; beginObject/endObject/beginArray/endArray; kv overloads for const char\*, long, unsigned long, int, unsigned int and bool; kvJoined; kvSci; kvHex; item. Private helpers: open, close, sep, str, esc, raw, fmt. Only raw() writes to buf_, apart from the constructor's initial NUL.

**Notes.** The header comment says newlib-nano printf has no %f, so fractional numbers are written as mantissa e exponent (for example 61e-6), which is valid JSON. The writer does not check JSON grammar: it does not verify that endObject matches beginObject rather than beginArray, that keys are supplied inside objects, or that only one top-level value is written.

#### `JsonWriter::JsonWriter`
*constructor* · [`firmware/src/json_writer.h:13`](../firmware/src/json_writer.h#L13)

```cpp
JsonWriter(char* buf, size_t cap)
```

**Purpose.** Binds the writer to a buffer and starts an empty document.

**How it works.** Stores buf and cap and sets len_ = 0, depth_ = 0, overflow_ = false. If cap_ != 0 it writes buf_[0] = '\0', so c_str() is an empty string. It sets first_[0] = true. The other first_ entries are set when open() enters that depth.

**Notes.** With cap == 0, every raw() call overflows and buf_ is never written, so c_str() points at an unterminated buffer.

#### `JsonWriter::c_str`
*method* · [`firmware/src/json_writer.h:18`](../firmware/src/json_writer.h#L18)

```cpp
const char* c_str() const
```

**Purpose.** Returns the built text.

**How it works.** Returns buf_. The text is NUL-terminated at len_ whenever cap_ > 0.

#### `JsonWriter::length`
*method* · [`firmware/src/json_writer.h:19`](../firmware/src/json_writer.h#L19)

```cpp
size_t length() const
```

**Purpose.** Returns the text length, used as the HEADER body length.

**How it works.** Returns len_. The NUL is not counted.

#### `JsonWriter::ok`
*method* · [`firmware/src/json_writer.h:20`](../firmware/src/json_writer.h#L20)

```cpp
bool ok() const
```

**Purpose.** Reports whether the document is complete and was not truncated.

**How it works.** Returns !overflow_ && depth_ == 0, so every opened container must have been closed and nothing may have overflowed.

**Notes.** If ok() is false, buildHeaderFrame() in main.cpp replaces the header with a minimal {"type":"kman_session_header","error":"header overflow"}. native_frames.cpp main() returns 1 when its test document is not ok().

#### `JsonWriter::beginObject`
*method* · [`firmware/src/json_writer.h:22`](../firmware/src/json_writer.h#L22)

```cpp
void beginObject(const char* key = nullptr)
```

**Purpose.** Opens a JSON object, optionally as a named member of the enclosing object.

**How it works.** Calls open(key, '{'). Pass a key for an object inside an object. Use nullptr at top level or for an array element.

#### `JsonWriter::endObject`
*method* · [`firmware/src/json_writer.h:23`](../firmware/src/json_writer.h#L23)

```cpp
void endObject()
```

**Purpose.** Closes the current object.

**How it works.** Calls close('}').

#### `JsonWriter::beginArray`
*method* · [`firmware/src/json_writer.h:24`](../firmware/src/json_writer.h#L24)

```cpp
void beginArray(const char* key = nullptr)
```

**Purpose.** Opens a JSON array, optionally as a named member.

**How it works.** Calls open(key, '[').

#### `JsonWriter::endArray`
*method* · [`firmware/src/json_writer.h:25`](../firmware/src/json_writer.h#L25)

```cpp
void endArray()
```

**Purpose.** Closes the current array.

**How it works.** Calls close(']').

#### `JsonWriter::kv(const char*, const char*)`
*method* · [`firmware/src/json_writer.h:27`](../firmware/src/json_writer.h#L27)

```cpp
void kv(const char* key, const char* value)
```

**Purpose.** Writes a string member "key":"value".

**How it works.** Calls sep(key), which writes a comma if needed plus the key and colon, then str(value), which writes the quoted, escaped value. A nullptr value produces an empty string.

#### `JsonWriter::kvJoined`
*method* · [`firmware/src/json_writer.h:32`](../firmware/src/json_writer.h#L32)

```cpp
void kvJoined(const char* key, const char* a, const char* b)
```

**Purpose.** Writes a string member whose value is a + '_' + b without a temporary buffer. It is used for channel names (slot prefix + channel suffix).

**How it works.** sep(key); raw("\""); esc(a); raw("_"); esc(b); raw("\""). For example ("name", "chest", "ax") produces "name":"chest_ax".

**Notes.** Called in buildHeaderFrame() as w.kvJoined("name", s.prefix, ch.suffix).

#### `JsonWriter::kv(const char*, long)`
*method* · [`firmware/src/json_writer.h:42`](../firmware/src/json_writer.h#L42)

```cpp
void kv(const char* key, long value)
```

**Purpose.** Writes a signed integer member.

**How it works.** sep(key), then fmt("%ld", value).

**Notes.** The code comment explains why the overloads use fundamental types: int32_t is long on ARM newlib but int on x86-64, so overloading on the fixed-width typedefs would not be portable. With these overloads, (int32_t) and (uint32_t) arguments resolve on both targets.

#### `JsonWriter::kv(const char*, unsigned long)`
*method* · [`firmware/src/json_writer.h:46`](../firmware/src/json_writer.h#L46)

```cpp
void kv(const char* key, unsigned long value)
```

**Purpose.** Writes an unsigned integer member.

**How it works.** sep(key), then fmt("%lu", value).

#### `JsonWriter::kv(const char*, int)`
*method* · [`firmware/src/json_writer.h:50`](../firmware/src/json_writer.h#L50)

```cpp
void kv(const char* key, int value)
```

**Purpose.** Accepts int arguments (including int32_t on x86-64 and promoted smaller integer types).

**How it works.** Forwards to kv(key, (long)value).

#### `JsonWriter::kv(const char*, unsigned int)`
*method* · [`firmware/src/json_writer.h:51`](../firmware/src/json_writer.h#L51)

```cpp
void kv(const char* key, unsigned int value)
```

**Purpose.** Accepts unsigned int arguments (uint32_t on x86-64).

**How it works.** Forwards to kv(key, (unsigned long)value).

#### `JsonWriter::kv(const char*, bool)`
*method* · [`firmware/src/json_writer.h:52`](../firmware/src/json_writer.h#L52)

```cpp
void kv(const char* key, bool value)
```

**Purpose.** Writes a boolean member.

**How it works.** sep(key), then raw("true") or raw("false").

#### `JsonWriter::kvSci`
*method* · [`firmware/src/json_writer.h:57`](../firmware/src/json_writer.h#L57)

```cpp
void kvSci(const char* key, int32_t mantissa, int8_t exp10)
```

**Purpose.** Writes the fractional number mantissa x 10^exp10 as a JSON number without floating point.

**How it works.** sep(key). If exp10 == 0, fmt("%ld", (long)mantissa). Otherwise fmt("%lde%d", (long)mantissa, (int)exp10). Examples: (61, -6) gives 61e-6, (208, -1) gives 208e-1 (= 20.8), (7, 0) gives 7. Each is a valid JSON number token.

**Notes.** Used for channel scale factors (ch.scaleMantissa, ch.scaleExp10) in buildHeaderFrame() and for accel_lpf_hz in Lsm6dsox::describeConfig().

#### `JsonWriter::kvHex`
*method* · [`firmware/src/json_writer.h:62`](../firmware/src/json_writer.h#L62)

```cpp
void kvHex(const char* key, uint32_t value, int digits = 2)
```

**Purpose.** Writes a value as a hexadecimal string such as "0x6A", for register addresses.

**How it works.** sep(key), then fmt("\"0x%0\*lX\"", digits, (unsigned long)value). The output is uppercase hex, zero-padded to at least digits characters, inside quotes. It is a JSON string, not a number.

**Notes.** Used by Lsm6dsox::describeConfig() for "addr". A digits value above 27 would make the output longer than fmt()'s 31-character limit, and it would be cut off without any error.

#### `JsonWriter::item`
*method* · [`firmware/src/json_writer.h:67`](../firmware/src/json_writer.h#L67)

```cpp
void item(const char* value)
```

**Purpose.** Writes a string element inside an array.

**How it works.** sep(nullptr) writes the comma if needed and no key. Then str(value).

**Notes.** Used for the "data_prefix" array entries ("t_us:u32", "seq:u16", "valid:u8") in buildHeaderFrame().

#### `JsonWriter::open`
*method* · [`firmware/src/json_writer.h:75`](../firmware/src/json_writer.h#L75)

```cpp
void open(const char* key, char c)
```

**Purpose.** Shared implementation of beginObject and beginArray.

**How it works.**

1. sep(key) writes the comma and the optional "key":.
2. raw() writes the single character c ('{' or '[').
3. If depth_ + 1 &lt; kMaxDepth (8), ++depth_ and first_[depth_] = true. Otherwise overflow_ = true; the bracket has already been written and depth_ is unchanged.

**Notes.** After a too-deep open, the matching close decrements one level too early, so depth tracking is off by one from then on. overflow_ is sticky, so ok() still returns false.

#### `JsonWriter::close`
*method* · [`firmware/src/json_writer.h:82`](../firmware/src/json_writer.h#L82)

```cpp
void close(char c)
```

**Purpose.** Shared implementation of endObject and endArray.

**How it works.** raw() writes the character c. Then, if depth_ > 0, --depth_; otherwise overflow_ = true (an unbalanced close).

**Notes.** It does not check that c matches the bracket that opened the level.

#### `JsonWriter::sep`
*method* · [`firmware/src/json_writer.h:88`](../firmware/src/json_writer.h#L88)

```cpp
void sep(const char* key)
```

**Purpose.** Puts commas between sibling elements and writes the member key. Every value-writing call goes through it.

**How it works.** If first_[depth_] is false, raw(","). Then set first_[depth_] = false. If key is non-null, str(key) followed by raw(":"). Keys are escaped the same way as values.

**Notes.** sep(nullptr) inside an object writes a bare value, which is invalid JSON and is not detected. At depth 0, a second top-level value gets a leading comma.

#### `JsonWriter::str`
*method* · [`firmware/src/json_writer.h:96`](../firmware/src/json_writer.h#L96)

```cpp
void str(const char* s)
```

**Purpose.** Writes a quoted, escaped JSON string.

**How it works.** raw("\""), esc(s), raw("\"").

#### `JsonWriter::esc`
*method* · [`firmware/src/json_writer.h:101`](../firmware/src/json_writer.h#L101)

```cpp
void esc(const char* s)
```

**Purpose.** Writes string content with the escaping JSON requires.

**How it works.**

Reads characters until the NUL; a nullptr s writes nothing. For each character c:
- double quote or backslash: write a backslash followed by c.
- (unsigned char)c &lt; 0x20: fmt("\\u%04x"), which writes a backslash-u escape with 4 lowercase hex digits. A newline becomes \u000a.
- anything else, including 0x7F and bytes >= 0x80 (UTF-8 sequences): written unchanged.
Each character is written by its own raw() or fmt() call.

**Notes.** Short escapes such as \n are not used. All control characters use the \u form, which is still valid JSON. '/' is not escaped.

#### `JsonWriter::raw`
*method* · [`firmware/src/json_writer.h:115`](../firmware/src/json_writer.h#L115)

```cpp
void raw(const char* s)
```

**Purpose.** The only function that writes into buf_. It enforces the capacity limit and keeps the text NUL-terminated.

**How it works.** n = strlen(s). If len_ + n + 1 > cap_, set overflow_ = true and return without writing. Otherwise memcpy(buf_ + len_, s, n + 1), which copies the NUL as well, and len_ += n.

**Notes.** A fragment that does not fit is dropped whole, not truncated. A later, shorter fragment can still fit and be appended, so after an overflow the buffer is not a clean prefix of the intended document. ok() is the reliable check. Worked example from the native test, with an 8-byte buffer: the 7 characters {"too": plus the NUL fill all 8 bytes. The next quote needs 9 bytes and sets overflow_.

#### `JsonWriter::fmt`
*method* · [`firmware/src/json_writer.h:124`](../firmware/src/json_writer.h#L124)

```cpp
void fmt(const char* f, ...)
```

**Purpose.** printf-style formatting for numbers and escapes, routed through raw() so the capacity check applies.

**How it works.** Declares char tmp[32], then va_start, vsnprintf(tmp, sizeof tmp, f, ap), va_end, and raw(tmp).

**Notes.** vsnprintf output longer than 31 characters is cut off without setting overflow_. When long is 32 bits (the ARM firmware target), the longest output from these formats is 16 characters: %lde%d with mantissa -2147483648 and exp -128. On a 64-bit host, %ld and %lu in kv(long) and kv(unsigned long) can produce up to 20 characters. \u%04x produces 6 characters, and kvHex produces digits + 4 (at least 6 with the default digits = 2, at most 12 for a 32-bit value). Only kvHex with a digits value above 27 can reach the limit.

### `firmware/src/protocol.cpp`

#### `proto::crc16`
*function* · [`firmware/src/protocol.cpp:7`](../firmware/src/protocol.cpp#L7)

```cpp
uint16_t crc16(const uint8_t* data, size_t n, uint16_t crc = 0xFFFF)
```

**Purpose.** Computes the frame checksum (CRC-16/CCITT-FALSE) over n bytes, so the receiver can reject corrupted frames and false sync matches.

**How it works.**

1. Start from crc. The default 0xFFFF comes from the declaration at protocol.h:42.
2. For each byte data[i], i = 0..n-1: crc ^= (uint16_t)data[i] &lt;&lt; 8, which puts the byte into the high 8 bits.
3. Repeat 8 times: if bit 15 (crc & 0x8000) is set, crc = (crc &lt;&lt; 1) ^ 0x1021; otherwise crc = crc &lt;&lt; 1. Each result is cast to uint16_t, which truncates to 16 bits.
4. Return crc. Bits are processed MSB first, with no input or output reflection and no final XOR.
Passing an earlier result as crc continues the CRC over concatenated data.

**Notes.** The code comment says a 512-byte lookup table is not worth it for about 25 bytes per frame at 100 Hz, so the CRC is computed bit by bit. The standard check value (ASCII "123456789" gives 0x29B1) matches Python's binascii.crc_hqx(data, 0xFFFF), which host/kman_host/protocol.py uses as its crc16.

#### `proto::encodeFrame`
*function* · [`firmware/src/protocol.cpp:18`](../firmware/src/protocol.cpp#L18)

```cpp
size_t encodeFrame(uint8_t type, const uint8_t* body, size_t body_len, uint8_t* out, size_t out_cap)
```

**Purpose.** Wraps a frame body in the full wire frame (sync, length, type, CRC) inside a caller buffer. Every frame the device sends is built by this function.

**How it works.**

1. len = body_len + 1, counting the type byte plus the body.
2. If len > kMaxLen (4096) or body_len + kOverhead (7) > out_cap, return 0 and write nothing.
3. Write 0xAA and 0x55 with putU8, len as u16 LE with putU16, then type with putU8.
4. If body_len is nonzero, memcpy the body to offset 5. An empty body may be a null pointer.
5. crc = crc16(out + 2, len + 2): this starts at the len field and covers the 2 len bytes, the type byte and body_len body bytes.
6. Append crc as u16 LE.
7. Return p - out, which equals body_len + 7.
The largest accepted body is 4095 bytes, giving a 4102-byte frame.

**Notes.** Declared at protocol.h:46. type is not checked against FrameType. body and out must not overlap (memcpy). The main.cpp callers size g_frame and g_headerFrame so that 0 is not expected; queueHeaderIfPending() still checks for len != 0.

#### `proto::encodeStatus`
*function* · [`firmware/src/protocol.cpp:33`](../firmware/src/protocol.cpp#L33)

```cpp
size_t encodeStatus(const Status& s, uint8_t* body)
```

**Purpose.** Serializes a Status into the packed STATUS frame body, which is then passed to encodeFrame(kTypeStatus, ...).

**How it works.**

Writes, in order, at these body offsets:
- 0: kStatusVersion (1), u8
- 1: t_us, u32
- 5: ticks, u32
- 9: frames_sent, u32
- 13: overruns, u32
- 17: tx_drops, u32
- 21, 25, 29, 33: read_errors[0..3], u32 each
- 37: max_latency_us, u16
- 39: tx_high_water, u16
- 41: flags, u8
All values are little-endian, written through the put\* helpers. Returns p - body = 42 (kStatusBodyLen).

**Notes.** Declared at protocol.h:86. There is no capacity argument; the caller must pass a buffer of at least kStatusBodyLen bytes (main.cpp and native_frames.cpp both declare uint8_t[kStatusBodyLen]).

### `firmware/src/protocol.h`

#### `proto wire constants (kSync0, kSync1, kProtocolVersion, FrameType, kOverhead, kMaxLen, kCmdSendHeader, kCmdSendStatus)`
*config* · [`firmware/src/protocol.h:22`](../firmware/src/protocol.h#L22)

```cpp
const uint8_t kSync0 = 0xAA, kSync1 = 0x55, kProtocolVersion = 1; enum FrameType : uint8_t { kTypeData = 0x01, kTypeHeader = 0x02, kTypeStatus = 0x03, kTypeLog = 0x04 }; const size_t kOverhead = 7, kMaxLen = 4096; const uint8_t kCmdSendHeader = 'H', kCmdSendStatus = 'S';
```

**Purpose.** Fixes the framing constants, frame type codes, size limits and host-to-device command bytes that the firmware encoder and the Python decoder must agree on.

**How it works.**

Values: kSync0 = 0xAA, kSync1 = 0x55, kProtocolVersion = 1. FrameType is a uint8_t enum (line 26): kTypeData = 0x01, kTypeHeader = 0x02, kTypeStatus = 0x03, kTypeLog = 0x04. kOverhead = 7 (sync 2 + len 2 + type 1 + crc 2). kMaxLen = 4096. kCmdSendHeader = 'H' (0x48), kCmdSendStatus = 'S' (0x53).

Frame byte layout (all multi-byte fields little-endian):
- offset 0: 0xAA (kSync0)
- offset 1: 0x55 (kSync1)
- offsets 2-3: len, u16 LE = number of bytes in type + body = body_len + 1. Valid range is 1..kMaxLen (4096), so body_len &lt;= 4095.
- offset 4: type (a FrameType value)
- offsets 5 .. 3+len: body (len-1 bytes)
- offsets 4+len, 5+len: crc, u16 LE
- total frame size = len + 6 = body_len + kOverhead.

CRC coverage: CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no final XOR) over offsets 2 .. 3+len, that is the two len bytes, the type byte and the body. The two sync bytes and the CRC itself are not covered.

Body per type:
- DATA (0x01): t_us u32 at body offset 0, seq u16 at offset 4, valid u8 bitmask at offset 6, then int16 LE channels from offset 7. The HEADER JSON gives the channel count, order and scaling.
- HEADER (0x02): UTF-8 JSON session header, no NUL terminator.
- STATUS (0x03): the 42-byte body written by encodeStatus().
- LOG (0x04): UTF-8 text.

Worked example (the DATA frame from native_frames.cpp, checked by running it): aa 55 | 16 00 | 01 | 00 ff ff ff | fe ff | 05 | 00 80 ff 7f 09 40 ff ff 00 00 d2 04 ff 1f | 79 f4. Here len = 0x16 = 22 (1 type byte + 21 body bytes), the total is 28 bytes, and the CRC 0xF479 is computed over the 24 bytes from '16 00' through 'ff 1f'.

Commands: the host writes the raw single bytes 'H' and 'S' to the device. They are not framed. In main.cpp, serviceHost() sets g_headerPending or g_statusPending for these bytes and ignores every other byte.

**Notes.** buildHeaderFrame() writes kProtocolVersion into the HEADER JSON as "protocol". kMaxLen exists for the receiver: FrameParser.feed in protocol.py counts a len of 0 or above 4096 as bad_length and resyncs one byte later, and encodeFrame() refuses to build such a frame. The Python mirrors are SYNC, MAX_LEN, TYPE_\*, CMD_SEND_HEADER and CMD_SEND_STATUS.

#### `proto::putU8`
*function* · [`firmware/src/protocol.h:48`](../firmware/src/protocol.h#L48)

```cpp
inline uint8_t* putU8(uint8_t* p, uint8_t v)
```

**Purpose.** Appends one byte to a serialization cursor. It is the base primitive used to build frame headers and bodies.

**How it works.** Writes v at \*p, advances p by 1 and returns the advanced pointer, so calls chain as p = putU8(p, ...).

**Notes.** No bounds checking. The caller must size the buffer.

#### `proto::putU16`
*function* · [`firmware/src/protocol.h:52`](../firmware/src/protocol.h#L52)

```cpp
inline uint8_t* putU16(uint8_t* p, uint16_t v)
```

**Purpose.** Appends a 16-bit value in little-endian order, which the protocol uses for all multi-byte fields.

**How it works.** Writes (v & 0xFF) and then (v >> 8), advances p by 2 and returns it. The result does not depend on host endianness.

**Notes.** No bounds checking.

#### `proto::putI16`
*function* · [`firmware/src/protocol.h:57`](../firmware/src/protocol.h#L57)

```cpp
inline uint8_t* putI16(uint8_t* p, int16_t v)
```

**Purpose.** Appends a signed 16-bit sample (a DATA channel value) in little-endian order.

**How it works.** Casts v to uint16_t, which keeps the two's-complement bit pattern, and returns putU16(p, (uint16_t)v). For example -32768 becomes bytes 00 80 and -1 becomes ff ff.

**Notes.** No bounds checking.

#### `proto::putU32`
*function* · [`firmware/src/protocol.h:58`](../firmware/src/protocol.h#L58)

```cpp
inline uint8_t* putU32(uint8_t* p, uint32_t v)
```

**Purpose.** Appends a 32-bit value in little-endian order (t_us and the STATUS counters).

**How it works.** Writes v & 0xFF, (v >> 8) & 0xFF, (v >> 16) & 0xFF and v >> 24, in that order, then returns p advanced by 4.

**Notes.** No bounds checking.

#### `proto STATUS constants (kStatusVersion, kStatusSlots, kStatusFlagHfxo, kStatusFlagSensorError, kStatusBodyLen)`
*config* · [`firmware/src/protocol.h:67`](../firmware/src/protocol.h#L67)

```cpp
const uint8_t kStatusVersion = 1, kStatusSlots = 4, kStatusFlagHfxo = 0x01, kStatusFlagSensorError = 0x02; const size_t kStatusBodyLen = 1 + 5 * 4 + kStatusSlots * 4 + 2 + 2 + 1;
```

**Purpose.** Defines the STATUS body version, the number of per-slot error counters, the flag bit values and the serialized STATUS body size.

**How it works.** kStatusVersion = 1 is the first byte of every STATUS body. kStatusSlots = 4 is the number of read_errors counters, one per sensor slot (main.cpp also sizes g_slots with it). kStatusFlagHfxo = 0x01 (line 80) is bit0: the high-frequency crystal oscillator is running. kStatusFlagSensorError = 0x02 is bit1: a sensor error. kStatusBodyLen (line 83) = 1 version + 5x4 u32 counters + 4x4 read_errors + 2 + 2 + 1 = 42 bytes.

**Notes.** The Python decoder's struct format "&lt;B5I4IHHB" is also 42 bytes. decode_status() rejects a body whose size is not 42 or whose first byte is not 1.

#### `proto::Status`
*struct* · [`firmware/src/protocol.h:69`](../firmware/src/protocol.h#L69)

```cpp
struct Status { uint32_t t_us, ticks, frames_sent, overruns, tx_drops; uint32_t read_errors[kStatusSlots]; uint16_t max_latency_us, tx_high_water; uint8_t flags; }
```

**Purpose.** Holds the device health counters that are sent in a STATUS frame, so the host can see timing overruns, dropped frames, sensor read failures and TX buffer pressure.

**How it works.**

Plain aggregate. Fields, with meanings from the code comments:
- t_us (u32): timer time in microseconds when the status was built.
- ticks (u32): sample-timer ticks since boot.
- frames_sent (u32): DATA frames queued since streaming started.
- overruns (u32): ticks skipped because the loop was late.
- tx_drops (u32): DATA frames dropped because the TX ring was full.
- read_errors[4] (u32 each): failed reads per sensor slot.
- max_latency_us (u16): largest tick-to-frame-queued latency since the last status.
- tx_high_water (u16): largest TX ring fill in bytes since the last status.
- flags (u8): bit0 kStatusFlagHfxo, bit1 kStatusFlagSensorError.
The struct has no version field. encodeStatus() writes kStatusVersion in front of the fields.

**Notes.** The in-memory struct has compiler padding, so it is never copied to the wire with memcpy. encodeStatus() writes each field explicitly into the packed 42-byte form. main.cpp keeps the only instance (g_status): sampleAndQueue() saturates the latency to 0xFFFF, and queueStatusIfDue() clamps g_tx.highWater() to 0xFFFF before storing it in tx_high_water.

### `firmware/src/ring_buffer.h`

#### `ByteRing`
*class* · [`firmware/src/ring_buffer.h:10`](../firmware/src/ring_buffer.h#L10)

```cpp
template <size_t N> class ByteRing
```

**Purpose.** Fixed-capacity byte FIFO that stages encoded frames for USB transmission, so a slow or stalled host does not block sampling and frames are never split.

**How it works.**

N is the template capacity in bytes. Fields:
- buf_[N]: storage.
- head_: index of the next byte to write.
- tail_: index of the next byte to read.
- used_: number of bytes currently stored, 0..N.
- highWater_: largest used_ since construction or the last resetHighWater().
used_ is stored separately so that full (used_ == N) and empty (used_ == 0) can be told apart, since head_ == tail_ in both cases. Indices wrap with % N.

**Notes.** Not interrupt-safe. The header comment says producer and consumer both run in the loop task, so the class has no locking and no volatile. The firmware creates one instance, ByteRing&lt;KMAN_TX_RING_BYTES> g_tx. The default size is 8192 from include/config.h, commented there as about 2.5 s of frames at 100 Hz.

#### `ByteRing::ByteRing`
*constructor* · [`firmware/src/ring_buffer.h:13`](../firmware/src/ring_buffer.h#L13)

```cpp
ByteRing()
```

**Purpose.** Creates an empty ring.

**How it works.** Sets head_, tail_, used_ and highWater_ to 0. buf_ is not initialized.

#### `ByteRing::used`
*method* · [`firmware/src/ring_buffer.h:15`](../firmware/src/ring_buffer.h#L15)

```cpp
size_t used() const
```

**Purpose.** Returns how many bytes are queued.

**How it works.** Returns used_.

**Notes.** pumpTx() loops while used() is nonzero.

#### `ByteRing::free`
*method* · [`firmware/src/ring_buffer.h:16`](../firmware/src/ring_buffer.h#L16)

```cpp
size_t free() const
```

**Purpose.** Returns the remaining capacity in bytes.

**How it works.** Returns N - used_.

**Notes.** queueHeaderIfPending() checks g_tx.free() >= len before pushing the header.

#### `ByteRing::highWater`
*method* · [`firmware/src/ring_buffer.h:17`](../firmware/src/ring_buffer.h#L17)

```cpp
size_t highWater() const
```

**Purpose.** Returns the peak fill level since the last reset. It is reported in STATUS as tx_high_water.

**How it works.** Returns highWater_.

#### `ByteRing::resetHighWater`
*method* · [`firmware/src/ring_buffer.h:18`](../firmware/src/ring_buffer.h#L18)

```cpp
void resetHighWater()
```

**Purpose.** Starts a new high-water measurement window.

**How it works.** Sets highWater_ = used_. The new peak starts from the bytes still queued, not from 0.

**Notes.** Called by startStreaming() and by queueStatusIfDue() after a STATUS frame is queued successfully, so each STATUS reports the peak since the previous one.

#### `ByteRing::clear`
*method* · [`firmware/src/ring_buffer.h:19`](../firmware/src/ring_buffer.h#L19)

```cpp
void clear()
```

**Purpose.** Discards everything queued.

**How it works.** Sets head_ = tail_ = used_ = 0. highWater_ is not changed.

**Notes.** startStreaming() calls it on host connect and serviceHost() calls it on disconnect.

#### `ByteRing::push`
*method* · [`firmware/src/ring_buffer.h:21`](../firmware/src/ring_buffer.h#L21)

```cpp
bool push(const uint8_t* data, size_t n)
```

**Purpose.** Appends n bytes all-or-nothing, so a frame is either queued whole or not at all.

**How it works.**

1. If n > free(), return false. Nothing is written.
2. first = min(n, N - head_): the number of bytes that fit before the end of storage.
3. memcpy(buf_ + head_, data, first).
4. memcpy(buf_, data + first, n - first): the remainder wraps to the start (0 bytes when there is no wrap).
5. head_ = (head_ + n) % N; used_ += n.
6. If used_ > highWater_, set highWater_ = used_.
7. Return true.

**Notes.** n == 0 returns true and changes nothing. In main.cpp, sampleAndQueue() counts true as frames_sent and false as tx_drops. queueStatusIfDue() leaves g_statusPending and the timer unchanged on failure, so it retries on a later loop.

#### `ByteRing::peekContiguous`
*method* · [`firmware/src/ring_buffer.h:33`](../firmware/src/ring_buffer.h#L33)

```cpp
size_t peekContiguous(const uint8_t** ptr) const
```

**Purpose.** Exposes the longest run of queued bytes that is contiguous in memory, so the caller can pass it straight to a write call without copying.

**How it works.** Sets \*ptr = buf_ + tail_. toEnd = N - tail_. Returns min(used_, toEnd). Nothing is consumed. When the data wraps, the first call returns the run up to the end of storage. After consume(), the next call returns the rest starting at index 0.

**Notes.** Returns 0 when the ring is empty (\*ptr is still set). pumpTx() limits the returned length to hal::hostWritable(), calls hal::hostWrite(ptr, n), then consume(w).

#### `ByteRing::consume`
*method* · [`firmware/src/ring_buffer.h:39`](../firmware/src/ring_buffer.h#L39)

```cpp
void consume(size_t n)
```

**Purpose.** Removes n bytes from the front after they have been written to the host.

**How it works.** Clamps n to used_. Then tail_ = (tail_ + n) % N and used_ -= n.

**Notes.** head_ is not touched, and the indices are not reset to 0 when the ring becomes empty. pumpTx() passes the actual byte count returned by hal::hostWrite, so partial writes are handled.

### `firmware/test/native_frames.cpp`

#### `emit (native_frames.cpp)`
*function* · [`firmware/test/native_frames.cpp:10`](../firmware/test/native_frames.cpp#L10)

```cpp
static void emit(uint8_t type, const uint8_t* body, size_t n)
```

**Purpose.** Frames a body with the firmware's own encoder and writes the raw frame bytes to stdout for the Python cross-check.

**How it works.** Uses a static buffer uint8_t frame[4096 + proto::kOverhead] (4103 bytes, at least as large as the largest legal 4102-byte frame). len = proto::encodeFrame(type, body, n, frame, sizeof frame), then fwrite(frame, 1, len, stdout).

**Notes.** If encodeFrame returns 0, nothing is written, and the Python test then fails because a frame is missing.

#### `main (native_frames.cpp)`
*function* · [`firmware/test/native_frames.cpp:16`](../firmware/test/native_frames.cpp#L16)

```cpp
int main()
```

**Purpose.** Produces four golden frames that exercise every protocol helper and JsonWriter feature. test_crosscheck.py decodes them with the Python implementation.

**How it works.**

1. DATA: in body[64], putU32(0xFFFFFF00) (t_us near the u32 wrap), putU16(0xFFFE) (seq near the wrap), putU8(0x05) (valid = 0b101), then putI16 for 7 channels {-32768, 32767, 16393, -1, 0, 1234, 8191}. That is a 21-byte body, sent with emit(kTypeData). Output: len 22, 28 bytes total.
2. STATUS: proto::Status s = {} (all zero), then t_us = 123456789, ticks = 4000000000, frames_sent = 42, overruns = 3, tx_drops = 7, read_errors[0] = 1, read_errors[3] = 9, max_latency_us = 650, tx_high_water = 280, flags = kStatusFlagHfxo. encodeStatus into sb[kStatusBodyLen], then emit(kTypeStatus). Output: 42-byte body, len 43, 49 bytes total.
3. HEADER-like JSON: JsonWriter over char json[1024] writes an object containing: kv type; kv quote = a"b\c plus a newline (tests escaping); kvSci scale (61,-6), lpf (208,-1) and int (7,0); kvHex addr 0x6A; kv neg (int32_t)-5; kv big (uint32_t)4000000000u; kv flag true; and an array "list" holding item "x" and an object with kvJoined("name","chest","ax"). If !w.ok(), return 1. Otherwise emit(kTypeHeader, json bytes, w.length()). The text produced is {"type":"kman_session_header","quote":"a\"b\\c\u000a","scale":61e-6,"lpf":208e-1,"int":7,"addr":"0x6A","neg":-5,"big":4000000000,"flag":true,"list":["x",{"name":"chest_ax"}]}. It is 174 bytes, so the frame len field is 175 (0xAF) and the frame is 181 bytes.
4. Overflow check: JsonWriter over char tiny[8] writes beginObject, kv("too", "long for the buffer"), endObject. verdict = "overflow-detected" if !t.ok(), otherwise "overflow-not-detected". emit(kTypeLog, verdict, strlen(verdict)). With the expected 17-byte "overflow-detected", the frame is len 18, 24 bytes.
5. Return 0. Total stdout is 28 + 49 + 181 + 24 = 282 bytes (checked by building and running it).

**Notes.** Build command from the file comment: g++ -std=c++11 -I../src native_frames.cpp ../src/protocol.cpp. test_crosscheck.py adds -Wall -Wextra -Werror, so the firmware headers must also compile without warnings on the host. On x86-64, the (int32_t) and (uint32_t) casts exercise the kv(int) and kv(unsigned int) overloads described in json_writer.h. A return value of 1 makes subprocess.run(check=True) in the test fixture raise, which fails every cross-check test.

## 9. Host: protocol decoder and labels

- **`host/kman_host/protocol.py`**: Host-side mirror of firmware/src/protocol.h. It encodes frames with CRC-16/CCITT-FALSE, parses an arbitrary byte stream into CRC-valid frames with one-byte resynchronisation, models the JSON session header (channel layout, sample rate, sensor slots), and decodes DATA bodies into numpy structured records and STATUS bodies into DeviceStatus; it also encodes STATUS bodies for the simulator. It does no I/O; the stream decoder, logger, live viewer, simulator and session reader all build on it.
- **`host/kman_host/labels.py`**: Fixed table of activity label codes (keyboard digits 0-9) and their names, two string helpers, and the label colour palettes: a light theme for the offline report and a dark theme for the live viewer, plus the set of labels drawn hatched. The module docstring says the table is copied into every session file, so analysis does not depend on this table staying unchanged.
- **`host/kman_host/__init__.py`**: Package marker for kman_host with a one-line docstring and `__version__ = "1.0.0"`. It imports no submodules, so callers import kman_host.protocol, kman_host.labels and the others explicitly. The release workflow reads `__version__` with sed and requires it to match the git tag.

**How it connects.** protocol.py is the bottom layer of the host stack. It imports only standard-library modules (binascii, json, struct, dataclasses) and numpy. StreamDecoder (kman_host/stream.py) owns a FrameParser and calls FrameParser.feed from StreamDecoder.feed. handle_frame then dispatches on TYPE_DATA/HEADER/STATUS/LOG/LABEL: _on_header calls SessionHeader.from_json and layout_key, _on_data calls SessionHeader.data_dtype and decode_data, and status frames go through decode_status. StreamDecoder.summary calls DeviceStatus.as_dict. Recorder in logger.py wraps StreamDecoder. After each feed it sends the `Frame.raw` bytes of the decoded frames over UDP, joined into one datagram. It rebuilds the header that is re-announced every 2 s with encode_frame(TYPE_HEADER, ...) after dropping `t_us_at_header`, sends TYPE_LABEL frames in announce_label, and prints SessionHeader.describe for the session's first header. The serial-side CMD_SEND_HEADER writes are in logger.run_live, not in Recorder: once in open_port at start and on every reconnect, and again every 2 s until a header has been decoded. live_view.py runs its own FrameParser in replay_source, emits TYPE_LABEL frames in sim_source, and writes CMD_SEND_HEADER in SerialSource. The simulator (SimSession.header_frame/data_frame/status_frame) produces device-identical frames with encode_frame, DATA_PREFIX, DeviceStatus and encode_status. session_io stores `SessionHeader.raw` in the parquet metadata and rebuilds it with SessionHeader.from_json in load_session; Session.has_slot calls slot_present. test_crosscheck compiles firmware/test/native_frames.cpp with firmware/src/protocol.cpp, the C++ code this module mirrors, and checks the Python side against that native output. FrameParser must parse it with no CRC errors, DATA_PREFIX and decode_status must decode it, and encode_frame must reproduce every native frame byte for byte. encode_status is not cross-checked there. labels.py feeds names and colours to the rest of the system. label_name is used by StreamDecoder.set_label/schedule_labels, SimSession.label_events, logger, live_view and sim_device. help_text and LABELS are used by logger.run_live (key help, and validation of viewer `LABEL n` commands parsed by udp_bus.parse_label_command). SessionWriter.close copies LABELS into the session file, and load_session falls back to it. LABEL_COLORS_DARK drives live_view.build_label_lut and the current-label indicator. LABEL_COLORS_LIGHT and HATCHED_LABELS drive kman_analysis.plotting._bands and the report legend in make_figure. In __init__.py, `__version__` is read only by the release workflow's version check.

### `host/kman_host/__init__.py`

#### `__version__`
*config* · [`host/kman_host/__init__.py:3`](../host/kman_host/__init__.py#L3)

```python
__version__ = "1.0.0"
```

**Purpose.** Version string of the kman_host package.

**How it works.** A string constant. The check-version job in .github/workflows/release.yml extracts it with `sed -n 's/^__version__ = "\(.*\)"/\1/p'` and fails the release if it differs from the tag (without the leading v), from KMAN_FW_VERSION in firmware/include/config.h, or from `version` in pyproject.toml.

**Notes.** No Python code imports it. The package docstring lists the package contents: device protocol, stream decoding, session files, simulator.

### `host/kman_host/labels.py`

#### `LABELS`
*config* · [`host/kman_host/labels.py:4`](../host/kman_host/labels.py#L4)

```python
LABELS: dict[int, str] = {0: "none", ..., 9: "simulated obstruction"}
```

**Purpose.** Maps keyboard digit label codes to activity names.

**How it works.** 0 none, 1 rest breathing, 2 talking, 3 drinking/swallow, 4 dry swallow, 5 head movement, 6 walking, 7 cough, 8 breath-hold, 9 simulated obstruction. Insertion order is 0-9, and help_text relies on it.

**Notes.** SessionWriter writes it into the parquet metadata as `label_names`, and load_session falls back to it when that metadata is missing. logger.handle_inputs accepts a viewer LABEL command only if `code in LABELS`. live_view lists it in the help text.

#### `label_name`
*function* · [`host/kman_host/labels.py:18`](../host/kman_host/labels.py#L18)

```python
def label_name(code: int) -> str
```

**Purpose.** Returns the name for a label code, with a fallback for unknown codes.

**How it works.** Returns `LABELS.get(int(code), f"label {code}")`. The code is converted with int(), so numpy integers and digit strings work, while a non-numeric string raises ValueError.

**Notes.** Callers: StreamDecoder.set_label and schedule_labels (LabelEvent.name), SimSession.label_events, logger (status line, key echo), live_view (label display), sim_device.

#### `help_text`
*function* · [`host/kman_host/labels.py:22`](../host/kman_host/labels.py#L22)

```python
def help_text() -> str
```

**Purpose.** Builds a one-line legend of all labels for the console.

**How it works.** Returns `"  ".join(f"{k}={v}" for k, v in LABELS.items())`, e.g. `0=none  1=rest breathing  ...`.

**Notes.** logger.run_live prints it at start-up and when the user presses `?`.

#### `LABEL_COLORS_LIGHT`
*config* · [`host/kman_host/labels.py:29`](../host/kman_host/labels.py#L29)

```python
LABEL_COLORS_LIGHT: dict[int, str] = {1: "#2a78d6", ..., 9: "#8a8984"}
```

**Purpose.** Light-theme colours for labels 1-9, used to draw the label bands in the offline report.

**How it works.** Hex colours: 1 #2a78d6, 2 #eb6834, 3 #1baf7a, 4 #eda100, 5 #e87ba4, 6 #008300, 7 #4a3aa7, 8 #e34948, 9 #8a8984. Label 0 has no entry. The comment says labels 1-8 use categorical slots 1-8 of the validated reference palette in fixed order, and that there is no 9th hue: label 9 is neutral gray, drawn with a hatch, and every band also carries its name as text.

**Notes.** kman_analysis.plotting._bands skips label 0 and any label missing from this dict. The plotting legend also builds its patches from it.

#### `LABEL_COLORS_DARK`
*config* · [`host/kman_host/labels.py:33`](../host/kman_host/labels.py#L33)

```python
LABEL_COLORS_DARK: dict[int, str] = {1: "#3987e5", ..., 9: "#8a8984"}
```

**Purpose.** Dark-theme colours for labels 1-9, used by the live viewer.

**How it works.** Hex colours: 1 #3987e5, 2 #d95926, 3 #199e70, 4 #c98500, 5 #d55181, 6 #008300, 7 #9085e9, 8 #e66767, 9 #8a8984. They are the same categorical slots as the light palette, adjusted for a dark background. Label 0 has no entry.

**Notes.** live_view.build_label_lut converts it into a 256x4 RGBA lookup table, with label 0 and unknown codes set to (0x38,0x38,0x35). The current-label indicator in live_view uses `.get(lab, TEXT_2)`.

#### `HATCHED_LABELS`
*config* · [`host/kman_host/labels.py:37`](../host/kman_host/labels.py#L37)

```python
HATCHED_LABELS = {9}
```

**Purpose.** The set of labels drawn with a hatch pattern because they have no distinct hue.

**How it works.** A set containing only 9 (simulated obstruction, neutral gray). kman_analysis.plotting._bands overlays a `///` hatch with edge colour INK_3 for these labels, and the report legend gives their patches the same hatch.

### `host/kman_host/protocol.py`

#### `SYNC`
*config* · [`host/kman_host/protocol.py:18`](../host/kman_host/protocol.py#L18)

```python
SYNC = b"\xaa\x55"
```

**Purpose.** The two-byte frame start marker that every frame begins with.

**How it works.** A bytes constant 0xAA 0x55. encode_frame prepends it to every frame, and FrameParser.feed searches for it with `buf.find(SYNC, pos)`.

**Notes.** Mirrors proto::kSync0/kSync1 in firmware/src/protocol.h.

#### `OVERHEAD`
*config* · [`host/kman_host/protocol.py:19`](../host/kman_host/protocol.py#L19)

```python
OVERHEAD = 7
```

**Purpose.** Number of framing bytes around a body: sync(2) + len(2) + type(1) + crc(2).

**How it works.** An integer constant 7 that mirrors proto::kOverhead.

**Notes.** No code in this module or the rest of the host code reads it. FrameParser.feed uses literal offsets instead (len at pos+2, `end = pos + 4 + length + 2`, type at raw[4], body at raw[5:-2]). encode_frame simply concatenates SYNC, the packed len/type, the body and the packed CRC.

#### `MAX_LEN`
*config* · [`host/kman_host/protocol.py:20`](../host/kman_host/protocol.py#L20)

```python
MAX_LEN = 4096
```

**Purpose.** Upper bound on the `len` field (type + body bytes). A larger value is treated as a false sync.

**How it works.** encode_frame raises ValueError when `len(body) + 1 > MAX_LEN`. FrameParser.feed counts a bad_length error and resyncs when `length < 1 or length > MAX_LEN`.

**Notes.** Mirrors proto::kMaxLen. Because of it, a complete frame is at most 4096 + 6 = 4102 bytes on the wire.

#### `TYPE_DATA / TYPE_HEADER / TYPE_STATUS / TYPE_LOG`
*config* · [`host/kman_host/protocol.py:22`](../host/kman_host/protocol.py#L22)

```python
TYPE_DATA = 0x01; TYPE_HEADER = 0x02; TYPE_STATUS = 0x03; TYPE_LOG = 0x04
```

**Purpose.** Frame type codes that the device sends.

**How it works.** DATA (0x01) carries one sample record (DATA_PREFIX plus int16 channels). HEADER (0x02) carries the UTF-8 JSON session header. STATUS (0x03) carries device counters in the _STATUS layout. LOG (0x04) carries UTF-8 text. StreamDecoder.handle_frame dispatches on these codes.

**Notes.** Mirrors the proto::FrameType enum.

#### `TYPE_LABEL`
*config* · [`host/kman_host/protocol.py:27`](../host/kman_host/protocol.py#L27)

```python
TYPE_LABEL = 0x10
```

**Purpose.** A host-only frame type that the logger sends to viewers over UDP to announce the current activity label.

**How it works.** The body is one byte, the label code. The comment says the device never sends it. Recorder.announce_label (logger.py) and live_view.sim_source build these frames with `encode_frame(TYPE_LABEL, bytes([code]))`. StreamDecoder.handle_frame sets `self.label = f.body[0]` when it receives one.

#### `CMD_SEND_HEADER / CMD_SEND_STATUS`
*config* · [`host/kman_host/protocol.py:29`](../host/kman_host/protocol.py#L29)

```python
CMD_SEND_HEADER = b"H"; CMD_SEND_STATUS = b"S"
```

**Purpose.** Single-byte host-to-device commands that ask the device to resend its session header or a status frame.

**How it works.** Bytes constants 'H' and 'S', written raw to the serial port. In logger.run_live, open_port writes CMD_SEND_HEADER each time the port is opened: at start and again after every reconnect, because the reconnect loop calls open_port. The main loop also writes it again every 2 s while `rec.dec.header is None`, and prints a 'no session header yet ... requested again' notice when it does. live_view.SerialSource writes CMD_SEND_HEADER once when it opens the port. sim_device.py checks whether it appears in the bytes it receives (`P.CMD_SEND_HEADER in cmd`).

**Notes.** No host code currently sends CMD_SEND_STATUS. Mirrors kCmdSendHeader/kCmdSendStatus.

#### `DATA_PREFIX`
*config* · [`host/kman_host/protocol.py:32`](../host/kman_host/protocol.py#L32)

```python
DATA_PREFIX = struct.Struct("<IHB")
```

**Purpose.** The fixed 7-byte prefix of every DATA body: t_us u32, seq u16, valid u8, little-endian.

**How it works.** A struct.Struct of size 7. The first three fields of SessionHeader.data_dtype match it. SimSession.data_frame (simulator.py) packs it, and test_crosscheck unpacks it.

**Notes.** `valid` is the per-frame mask of slots that delivered good data, with bits as given by Channel.bit / present_mask (firmware kDataHeaderLen = 4+2+1).

#### `STATUS_SLOTS`
*config* · [`host/kman_host/protocol.py:34`](../host/kman_host/protocol.py#L34)

```python
STATUS_SLOTS = 4
```

**Purpose.** Number of per-slot read-error counters in a STATUS body (mirrors kStatusSlots).

**How it works.** An integer constant 4.

**Notes.** Nothing reads it. _STATUS hard-codes `4I`, so changing STATUS_SLOTS alone would not change the decoder.

#### `_STATUS`
*config* · [`host/kman_host/protocol.py:35`](../host/kman_host/protocol.py#L35)

```python
_STATUS = struct.Struct("<B5I4IHHB")
```

**Purpose.** Binary layout of a STATUS body, version 1, 42 bytes.

**How it works.** Fields in order: version u8 (must be 1), five u32 (t_us, ticks, frames_sent, overruns, tx_drops), four u32 read_errors, max_latency_us u16, tx_high_water u16, flags u8. Size = 1+20+16+2+2+1 = 42, which equals firmware kStatusBodyLen. decode_status and encode_status use it.

#### `STATUS_FLAG_HFXO / STATUS_FLAG_SENSOR_ERROR`
*config* · [`host/kman_host/protocol.py:36`](../host/kman_host/protocol.py#L36)

```python
STATUS_FLAG_HFXO = 0x01; STATUS_FLAG_SENSOR_ERROR = 0x02
```

**Purpose.** Bit masks for DeviceStatus.flags.

**How it works.** Bit 0 means the high-frequency crystal oscillator (HFXO) is running. Bit 1 means a sensor error. The properties DeviceStatus.hfxo and DeviceStatus.sensor_error test them, and SimSession.status_frame sets HFXO.

#### `crc16`
*function* · [`host/kman_host/protocol.py:40`](../host/kman_host/protocol.py#L40)

```python
def crc16(data: bytes, crc: int = 0xFFFF) -> int
```

**Purpose.** Computes CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no xorout), the frame checksum.

**How it works.** Returns `binascii.crc_hqx(data, crc)`. crc_hqx implements exactly this non-reflected 0x1021 CRC in C, and passing the 0xFFFF default as the initial value makes it CCITT-FALSE. The `crc` parameter allows chaining across chunks.

**Notes.** test_protocol checks `crc16(b"123456789") == 0x29B1`, the standard check value. Callers are encode_frame and FrameParser.feed.

#### `encode_frame`
*function* · [`host/kman_host/protocol.py:45`](../host/kman_host/protocol.py#L45)

```python
def encode_frame(ftype: int, body: bytes) -> bytes
```

**Purpose.** Builds one complete wire frame from a type code and a body.

**How it works.**

1. `length = len(body) + 1` (type + body).
2. If `length > MAX_LEN` (4096), raises `ValueError("frame body too large (N bytes)")`.
3. `head = struct.pack("<HB", length, ftype)`: len as u16 LE, then the type byte.
4. Returns `SYNC + head + body + struct.pack("<H", crc16(head + body))`, so the CRC covers len, type and body and is appended little-endian.

**Notes.** A ftype outside 0..255 raises struct.error. test_crosscheck asserts that `encode_frame(f.type, f.body) == f.raw` for frames produced by the native firmware build.

#### `Frame`
*dataclass* · [`host/kman_host/protocol.py:54`](../host/kman_host/protocol.py#L54)

```python
@dataclass class Frame: type: int; body: bytes; raw: bytes
```

**Purpose.** One CRC-validated frame as FrameParser.feed returns it.

**How it works.** Fields: `type` is the type byte (raw[4]) as an int. `body` holds the bytes after the type and before the CRC (raw[5:-2]). `raw` holds the complete frame from the 0xAA through the CRC; the field comment says it is kept for re-broadcasting, and Recorder.feed sends `f.raw` over UDP.

#### `ParserStats`
*dataclass* · [`host/kman_host/protocol.py:61`](../host/kman_host/protocol.py#L61)

```python
@dataclass class ParserStats: frames: int = 0; crc_errors: int = 0; bad_length: int = 0; bytes_discarded: int = 0
```

**Purpose.** Running counters for one FrameParser.

**How it works.** `frames` counts CRC-valid frames emitted. `crc_errors` counts candidate frames whose CRC did not match. `bad_length` counts sync hits whose len was 0 or above MAX_LEN. `bytes_discarded` counts bytes skipped while searching for sync. All start at 0 and only FrameParser.feed increments them.

**Notes.** bytes_discarded does not include the single 0xAA byte skipped by `pos += 1` after a CRC or length error; only the bytes between it and the next sync are counted. logger.status_line and the live_view status bar show `parser.stats.crc_errors`.

#### `FrameParser`
*class* · [`host/kman_host/protocol.py:68`](../host/kman_host/protocol.py#L68)

```python
class FrameParser
```

**Purpose.** Incremental frame parser with resynchronisation. It accepts arbitrary byte chunks and returns complete, CRC-valid frames.

**How it works.** State: `_buf` (bytearray) holds unconsumed bytes between calls, and `stats` is a ParserStats. The docstring describes the recovery rule: on a CRC or length error the parser advances one byte past the false sync and searches again, so a corrupted frame costs exactly that frame.

**Notes.** StreamDecoder.__init__ creates one as `self.parser`, and live_view.replay_source creates one to split a recorded .bin file.

#### `FrameParser.__init__`
*constructor* · [`host/kman_host/protocol.py:76`](../host/kman_host/protocol.py#L76)

```python
def __init__(self) -> None
```

**Purpose.** Creates an empty parser.

**How it works.** Sets `self._buf = bytearray()` and `self.stats = ParserStats()` with all counters at 0.

#### `FrameParser.feed`
*method* · [`host/kman_host/protocol.py:80`](../host/kman_host/protocol.py#L80)

```python
def feed(self, data: bytes) -> list[Frame]
```

**Purpose.** Appends a chunk of received bytes, extracts every complete CRC-valid frame now in the buffer, and keeps any incomplete tail for the next call.

**How it works.**

1. Setup: `out = []`. `buf = self._buf` and `buf += data` extend the persistent bytearray in place. `pos = 0` marks the first unconsumed byte, and `n = len(buf)`.
2. Sync search: `i = buf.find(SYNC, pos)` finds the next 0xAA 0x55 at or after pos.
3. No sync (`i < 0`): decides whether to keep the last byte. `keep_from = n - 1` if `n - 1 >= pos and buf[-1] == 0xAA`, else `keep_from = n`. A trailing lone 0xAA may be the first sync byte of a frame whose 0x55 arrives in the next chunk. The guard `n - 1 >= pos` means a byte already consumed is never kept again. When the previous iteration consumed a frame that ends exactly at the end of the buffer (pos == n) and that frame's CRC high byte is 0xAA, that byte belongs to the consumed frame and must not be retained as a potential sync start. The guard also short-circuits before `buf[-1]` is evaluated on an empty buffer. Then `bytes_discarded += max(0, keep_from - pos)`, `pos = keep_from`, and the loop breaks.
4. Sync found: `bytes_discarded += i - pos` counts the junk skipped, and `pos = i`.
5. Header completeness: if `n - pos < 5` (sync, len and type not all present yet), the loop breaks and the partial frame from pos is kept.
6. Length validation: `length = buf[pos+2] | (buf[pos+3] << 8)`. If `length < 1 or length > MAX_LEN`, `bad_length += 1` and `pos += 1` resyncs one byte past the 0xAA of the false sync, then the loop continues at step 2.
7. Frame completeness: `end = pos + 4 + length + 2` (sync + len + len bytes of type/body + CRC). If `end > n`, the loop breaks and the parser waits for more bytes, keeping the partial frame.
8. CRC check: `crc_rx = buf[end-2] | (buf[end-1] << 8)`. It is compared with `crc16(bytes(buf[pos+2 : end-2]))`, computed over len, type and body. On mismatch, `crc_errors += 1` and `pos += 1` resync by one byte, and the loop continues.
9. Emit: `frames += 1`, `raw = bytes(buf[pos:end])`, append `Frame(type=raw[4], body=raw[5:-2], raw=raw)`, set `pos = end`, and continue the loop.
10. Compaction: after the loop, `del buf[:pos]` drops everything consumed or discarded. What remains is a partial frame starting at a sync, fewer than 5 bytes after a sync, or a lone trailing 0xAA. Returns `out` in stream order.

**Notes.** Because a rejected sync advances only one byte, a real frame that starts inside a corrupted one is still found. A false sync with a plausible length (1..4096) holds the parser at step 7 until length+6 bytes have arrived and the CRC fails. Frames behind it are delayed, not lost. After each call the retained buffer is shorter than one maximum frame (4102 bytes). Feeding b"" on an empty buffer is a no-op. Callers: StreamDecoder.feed (stream.py) and live_view.replay_source.

#### `Channel`
*dataclass* · [`host/kman_host/protocol.py:124`](../host/kman_host/protocol.py#L124)

```python
@dataclass class Channel: name: str; slot: str; bit: int; unit: str; scale: float
```

**Purpose.** Describes one int16 channel of the DATA payload, as listed in the session header.

**How it works.** `name` is the column/field name, for example a chest gyro axis, and is used as the numpy dtype field and parquet column. `slot` is the sensor slot it belongs to (e.g. "chest_imu"). `bit` is the bit of that slot in present_mask and in the per-frame `valid` mask. `unit` is the physical unit string. `scale` is the factor that converts raw counts to physical units: Session.physical multiplies the int16 value by it and sets NaN where `valid & (1 << bit) == 0`.

#### `SessionHeader`
*dataclass* · [`host/kman_host/protocol.py:133`](../host/kman_host/protocol.py#L133)

```python
@dataclass class SessionHeader: raw: dict; channels: list[Channel] = field(default_factory=list)
```

**Purpose.** Parsed device session header. It defines the DATA layout and timing and carries the device and sensor metadata.

**How it works.** `raw` is the full decoded JSON dict, kept verbatim. StreamDecoder appends it to `headers`, and SessionWriter stores `json.dumps(header.raw)` in the parquet metadata. `channels` is the ordered list of Channel objects built from raw["channels"]; its order is the order of the int16 values in a DATA body. The properties and methods below read raw keys: sample_rate_hz, period_us, stream_id, present_mask, sensors, clock, board, fw_version, device_id.

#### `SessionHeader.from_json`
*classmethod* · [`host/kman_host/protocol.py:138`](../host/kman_host/protocol.py#L138)

```python
@classmethod def from_json(cls, text: str | bytes) -> SessionHeader
```

**Purpose.** Parses and validates a HEADER frame body, or a stored header string, into a SessionHeader.

**How it works.**

1. `d = json.loads(text)` accepts str or UTF-8 bytes.
2. If `d.get("type") != "kman_session_header"`, raises `ValueError("not a kman session header")`.
3. If the key "error" is present, raises `ValueError("device reported header error: ...")`.
4. Builds `Channel(c["name"], c["slot"], int(c["bit"]), c["unit"], float(c["scale"]))` for each entry of `d["channels"]`.
5. Returns `cls(raw=d, channels=chans)`.

**Notes.** Malformed JSON raises json.JSONDecodeError, a ValueError subclass. A missing "channels" key or channel field raises KeyError. Callers: StreamDecoder._on_header, session_io.load_session (from the stored metadata), live_view.replay_source.

#### `SessionHeader.sample_rate_hz`
*property* · [`host/kman_host/protocol.py:148`](../host/kman_host/protocol.py#L148)

```python
@property def sample_rate_hz(self) -> float
```

**Purpose.** The nominal sample rate in Hz.

**How it works.** Returns `float(self.raw["sample_rate_hz"])` and raises KeyError if the key is absent.

**Notes.** Session.fs and live_view.replay_source use it.

#### `SessionHeader.period_us`
*property* · [`host/kman_host/protocol.py:152`](../host/kman_host/protocol.py#L152)

```python
@property def period_us(self) -> int
```

**Purpose.** The nominal sample period in microseconds.

**How it works.** Returns `int(self.raw["period_us"])` and raises KeyError if the key is absent.

**Notes.** layout_key includes it. StreamDecoder uses it for timestamp and gap logic.

#### `SessionHeader.stream_id`
*property* · [`host/kman_host/protocol.py:156`](../host/kman_host/protocol.py#L156)

```python
@property def stream_id(self) -> int
```

**Purpose.** The device's stream identifier.

**How it works.** Returns `int(self.raw.get("stream_id", 0))`, which is 0 when absent.

**Notes.** describe() prints it. StreamDecoder._new_stream compares raw["stream_id"] directly rather than through this property.

#### `SessionHeader.present_mask`
*property* · [`host/kman_host/protocol.py:160`](../host/kman_host/protocol.py#L160)

```python
@property def present_mask(self) -> int
```

**Purpose.** Bit mask of slots that initialised at stream start.

**How it works.** Returns `int(self.raw.get("present_mask", 0))`, which is 0 when absent.

#### `SessionHeader.channel_names`
*property* · [`host/kman_host/protocol.py:164`](../host/kman_host/protocol.py#L164)

```python
@property def channel_names(self) -> list[str]
```

**Purpose.** The channel names in payload order.

**How it works.** Returns `[c.name for c in self.channels]`.

**Notes.** SessionWriter._open uses it to build the parquet schema (one int16 column per channel). Note that set_header only stores the header. StreamDecoder.drain uses it to extract the columns, and StreamDecoder._on_header puts the old and new lists in the LayoutChanged message. LiveProcessor (live_view.py) uses it to detect which channel groups are present (chest_a\*/abd_a\* accelerometer axes, flex_raw). analysis/report.py and describe() print it.

#### `SessionHeader.layout_key`
*method* · [`host/kman_host/protocol.py:167`](../host/kman_host/protocol.py#L167)

```python
def layout_key(self) -> tuple
```

**Purpose.** Identity of the payload layout. The docstring states that two headers with equal keys decode identically.

**How it works.** Returns `tuple((c.name, c.scale) for c in self.channels) + (self.period_us,)`, a tuple of (name, scale) pairs in order, followed by the period.

**Notes.** slot, bit and unit are not part of the key. StreamDecoder._on_header raises LayoutChanged when a later header's key differs from the current one.

#### `SessionHeader.data_dtype`
*method* · [`host/kman_host/protocol.py:171`](../host/kman_host/protocol.py#L171)

```python
def data_dtype(self) -> np.dtype
```

**Purpose.** Builds the numpy structured dtype that one DATA body maps onto.

**How it works.** Fields are `[("t_us", "<u4"), ("seq", "<u2"), ("valid", "u1")]` followed by `(c.name, "<i2")` for each channel, passed as `np.dtype(fields)`. A list-built dtype is packed, so `itemsize = 7 + 2 * len(channels)`, which matches DATA_PREFIX plus the int16 channels.

**Notes.** StreamDecoder._on_data calls it for every DATA frame and passes the result to decode_data.

#### `SessionHeader.slots`
*method* · [`host/kman_host/protocol.py:176`](../host/kman_host/protocol.py#L176)

```python
def slots(self) -> dict[str, dict]
```

**Purpose.** Indexes the header's sensor entries by slot name.

**How it works.** Returns `{s["slot"]: s for s in self.raw.get("sensors", [])}`. Each value is the raw sensor dict (status, model, error, warning and so on). With no "sensors" key the dict is empty.

#### `SessionHeader.slot_present`
*method* · [`host/kman_host/protocol.py:179`](../host/kman_host/protocol.py#L179)

```python
def slot_present(self, slot: str) -> bool
```

**Purpose.** Reports whether a named sensor slot initialised successfully.

**How it works.** Returns `self.slots().get(slot, {}).get("status") == "ok"`, so an unknown slot or any status other than "ok" gives False.

**Notes.** Session.has_slot (session_io.py) delegates to it, and test_protocol covers it.

#### `SessionHeader.describe`
*method* · [`host/kman_host/protocol.py:182`](../host/kman_host/protocol.py#L182)

```python
def describe(self) -> str
```

**Purpose.** Formats the header as a multi-line, human-readable summary for the console.

**How it works.**

1. Line 1: `board=<board> fw=<fw_version> device=<device_id> rate=<sample_rate_hz:g> Hz stream=<stream_id>`. board, fw_version and device_id are read with raw.get and print as None when missing. rate goes through the sample_rate_hz property, which raises KeyError when the key is absent. stream goes through the stream_id property, which defaults to 0.
2. Line 2: `clock: <raw["clock"]["hfclk"]>`, or `?` when either key is absent.
3. One line per entry in raw["sensors"]: two spaces, then `slot` left-aligned in a 12-character field (padded on the right), a space, `status` left-aligned in a 15-character field, a space, then `model` (default ""). `  ERROR: <error>` is appended if error is truthy, and `  WARNING: <warning>` if warning is truthy.
4. Final line: `  channels: ` followed by the comma-joined channel_names.
5. Returns the lines joined with "\n".

**Notes.** A sensor entry without "slot" or "status" raises KeyError, and so does a header without sample_rate_hz. Recorder.feed (logger.py) prints it once per session, when the first header is accepted and StreamDecoder.new_header is set. Later headers that StreamDecoder treats as a new stream (new segment) do not set new_header, so they are not printed.

#### `decode_data`
*function* · [`host/kman_host/protocol.py:200`](../host/kman_host/protocol.py#L200)

```python
def decode_data(body: bytes, dtype: np.dtype) -> np.ndarray | None
```

**Purpose.** Decodes one DATA body into a one-element structured array, rejecting bodies whose size does not match the header layout.

**How it works.** If `len(body) != dtype.itemsize`, returns None. Otherwise returns `np.frombuffer(body, dtype=dtype, count=1)`.

**Notes.** The result is a read-only view over the body bytes, not a copy. StreamDecoder._on_data counts a None result as `stats.bad_size`.

#### `DeviceStatus`
*dataclass* · [`host/kman_host/protocol.py:208`](../host/kman_host/protocol.py#L208)

```python
@dataclass class DeviceStatus: t_us: int; ticks: int; frames_sent: int; overruns: int; tx_drops: int; read_errors: tuple[int, ...]; max_latency_us: int; tx_high_water: int; flags: int
```

**Purpose.** Device health counters carried by a STATUS frame.

**How it works.** Field meanings follow the mirrored proto::Status struct in firmware/src/protocol.h. `t_us`: device timer time when the status was built. `ticks`: sample-timer ticks since boot. `frames_sent`: DATA frames queued since streaming started. `overruns`: ticks skipped because the loop was late. `tx_drops`: DATA frames dropped because the TX ring was full. `read_errors`: per-slot read error counts (4 entries). `max_latency_us`: largest tick-to-frame-queued latency since the last status (u16). `tx_high_water`: largest TX ring fill in bytes since the last status (u16). `flags`: bit0 HFXO running, bit1 sensor error.

**Notes.** decode_status builds it. SimSession.status_frame constructs it directly.

#### `DeviceStatus.hfxo`
*property* · [`host/kman_host/protocol.py:220`](../host/kman_host/protocol.py#L220)

```python
@property def hfxo(self) -> bool
```

**Purpose.** Reports whether the high-frequency crystal oscillator is running.

**How it works.** Returns `bool(self.flags & STATUS_FLAG_HFXO)`, testing bit 0x01.

**Notes.** logger.status_line appends ` NO-XTAL` when it is False.

#### `DeviceStatus.sensor_error`
*property* · [`host/kman_host/protocol.py:224`](../host/kman_host/protocol.py#L224)

```python
@property def sensor_error(self) -> bool
```

**Purpose.** Reports whether the device has flagged a sensor error.

**How it works.** Returns `bool(self.flags & STATUS_FLAG_SENSOR_ERROR)`, testing bit 0x02.

#### `DeviceStatus.as_dict`
*method* · [`host/kman_host/protocol.py:227`](../host/kman_host/protocol.py#L227)

```python
def as_dict(self) -> dict
```

**Purpose.** Converts the status into a JSON-serialisable dict for session summaries.

**How it works.** Returns a dict with every field: read_errors is converted from a tuple to a list, and the derived booleans `hfxo` and `sensor_error` are added next to the raw `flags`.

**Notes.** StreamDecoder.summary stores it under device_status_last and device_status_first.

#### `decode_status`
*function* · [`host/kman_host/protocol.py:243`](../host/kman_host/protocol.py#L243)

```python
def decode_status(body: bytes) -> DeviceStatus | None
```

**Purpose.** Decodes a STATUS frame body. It returns None for a wrong size or an unknown status version.

**How it works.**

1. If `len(body) != _STATUS.size` (42) or `body[0] != 1` (status version), returns None. The length test runs first, so an empty body never reaches the index.
2. `v = _STATUS.unpack(body)`.
3. Returns `DeviceStatus(t_us=v[1], ticks=v[2], frames_sent=v[3], overruns=v[4], tx_drops=v[5], read_errors=tuple(v[6:10]), max_latency_us=v[10], tx_high_water=v[11], flags=v[12])`. v[0], the version, is dropped.

**Notes.** StreamDecoder.handle_frame calls it for TYPE_STATUS and ignores None results.

#### `encode_status`
*function* · [`host/kman_host/protocol.py:260`](../host/kman_host/protocol.py#L260)

```python
def encode_status(s: DeviceStatus) -> bytes
```

**Purpose.** Serialises a DeviceStatus into a 42-byte STATUS body. It is the inverse of decode_status.

**How it works.** Returns `_STATUS.pack(1, s.t_us, s.ticks, s.frames_sent, s.overruns, s.tx_drops, *s.read_errors, s.max_latency_us, s.tx_high_water, s.flags)`, with version 1 first. It returns only the body; the caller wraps it with `encode_frame(TYPE_STATUS, ...)`.

**Notes.** read_errors must contain exactly 4 values and the u16/u8 fields must be in range, or struct.error is raised. SimSession.status_frame (simulator.py) is the caller.

## 10. Host: stream decoder (unwrapping, gaps, segments, restarts, labels)

- **`host/kman_host/stream.py`**: The single decoder that turns raw device bytes into timestamped, labelled sample columns. The live logger, raw-file replay, the simulator and the live viewer all use it, so decoding, gap accounting, restart handling and label alignment are identical everywhere. It wraps protocol.FrameParser. It unwraps the 32-bit microsecond timestamp and the 16-bit tick counter to int64, splits the stream into segments at device restarts, places label key presses on exact frames, and buffers rows until a consumer calls drain().

**How it connects.**

Each host consumer calls feed(), hands dec.header to its writer when new_header is set and then clears the flag, drains rows into a writer or a processor, and at the end reads headers, label_events_dicts() and summary(). The writers drain once pending_rows() reaches a threshold. The live viewer drains on every timer tick and never looks at new_header.

- logger.Recorder: __init__ creates one decoder. Recorder.feed calls dec.feed(data, host_time) and re-broadcasts the returned frames' raw bytes over UDP. It also keeps a copy of each HEADER frame with t_us_at_header stripped, to re-announce to viewers. On new_header it calls SessionWriter.set_header(dec.header). When pending_rows() >= 100 it calls SessionWriter.write_batch(dec.drain()). Recorder.set_label calls dec.set_label and passes asdict(event) to SessionWriter.write_label_event. announce_label sends dec.label to viewers as a TYPE_LABEL frame. Recorder.close hands over the header if feed() was interrupted, does a final drain, then calls SessionWriter.close(dec.headers, dec.label_events_dicts(), dec.summary(), notes=...).
- logger.run_live and logger.status_line: run_live catches LayoutChanged and stops, pops dec.events for printing, and re-sends CMD_SEND_HEADER every 2 s while dec.header is None. status_line reads dec.stats, drop_pct(), parser.stats, dec.status and dec.label.
- logger.run_replay: reads the events with read_label_events and passes them both to dec.schedule_labels and to SessionWriter, so the output's .labels.jsonl carries all of them. It then feeds the .bin file in 64 KiB chunks with host_time=0.0, and on LayoutChanged it keeps what was decoded so far.
- sim_device.write_session and the analysis tests: schedule the simulator's seq-only SimSession.label_events() and feed frames with host_time 0.0. write_session drains at 2000 rows; the analysis tests drain once at the end. The host tests drive the decoder directly. Most pass host_time 0.0. test_v1_fixes.py also feeds increasing host times to exercise bridging, records labels live with set_label, and replays them with schedule_labels to check the (segment, seg_seq) placement.
- live_view: tick() feeds bytes from UdpSource (the logger), SerialSource (--port) or a PacedFrameSource (--sim or --replay). It passes no host_time, so time.time() is used. It builds LiveProcessor(dec.header, ...) once a header exists, passes dec.drain() to LiveProcessor.process, and shows dec.label, dec.status and dec.stats. It catches LayoutChanged and any other source error, and stays on the error until the viewer is restarted. Labels reach the viewer through the decoder's TYPE_LABEL branch, either from the logger's announce_label frames over UDP or from the simulator source's ground-truth LABEL frames. When the source cannot forward a label (serial, sim, replay), the window's key handler sets dec.label directly.

Outbound, the decoder calls into kman_host.protocol: FrameParser.feed, SessionHeader.from_json, layout_key, data_dtype, period_us, channel_names, decode_data, decode_status and DeviceStatus.as_dict. It also calls labels.label_name. The drained columns (t_us, seq, segment, valid, channels, label, host_time) become the Parquet schema in SessionWriter, which load_session and the analysis code read back.

### `host/kman_host/stream.py`

#### `LayoutChanged`
*exception class* · [`host/kman_host/stream.py:34`](../host/kman_host/stream.py#L34)

```python
class LayoutChanged(Exception)
```

**Purpose.** Signals that the device announced a channel layout different from the session's first header. One output file cannot hold two layouts, so the session cannot continue.

**How it works.** A plain Exception subclass with no extra fields. StreamDecoder._on_header raises it when hdr.layout_key() != self.header.layout_key(). layout_key is a tuple of (channel name, scale) pairs plus period_us. The message is "channel layout changed: {old channel_names} -> {new channel_names}".

**Notes.** It is raised inside feed(), so frames that come after the header in the same chunk are not processed and feed() returns nothing. The decoder keeps the old header. logger.run_live stops recording on it, logger.run_replay stops and keeps the data decoded so far, and live_view.tick shows an error until the viewer is restarted.

#### `StreamStats`
*dataclass* · [`host/kman_host/stream.py:39`](../host/kman_host/stream.py#L39)

```python
@dataclass class StreamStats
```

**Purpose.** Counters for the decoder's frame accounting. summary() reports them and the logger shows them on its status line.

**How it works.**

All fields are int and default to 0:
- frames_data: DATA frames accepted into the row buffer (incremented at the end of _on_data).
- frames_before_header: DATA frames that arrived while no header was known. They are discarded.
- dropped: missing ticks inside a segment, the sum of (nt - 1) over forward steps with nt > 1. A segment bridge never counts here.
- duplicates: frames rejected because dt == 0, the clock stepped back by at most BACKWARD_RESTART_US, or round(dt/period) &lt;= 0.
- seq_mismatch: frames that were accepted although their 16-bit seq delta disagrees with nt mod 2^16 and dt &lt;= 1 s.
- bad_size: DATA bodies whose length != header.data_dtype().itemsize.
- segments: set to 1 by the first header; _start_segment adds 1 each time it runs.
- restarts_detected: segments started because the data itself showed a restart (a backward jump > 1 s, or a forward jump > 1 s whose seq disagrees). Restarts announced by a header do not count.
- headers: every HEADER frame that parsed, including repeats and headers that started nothing.
- status_frames: STATUS frames that decode_status accepted.
- log_messages: LOG frames.

**Notes.** summary() serializes it with dataclasses.asdict under the key "stream".

#### `StreamStats.drop_pct`
*method* · [`host/kman_host/stream.py:52`](../host/kman_host/stream.py#L52)

```python
def drop_pct(self) -> float
```

**Purpose.** Returns the percentage of expected frames lost inside segments.

**How it works.** expected = frames_data + dropped. It returns 100.0 \* dropped / expected, or 0.0 when expected is 0.

**Notes.** Duplicates, bad_size and frames_before_header are not in the denominator. Ticks skipped by a segment bridge are not counted as lost. Used by summary() (key "drop_pct") and logger.status_line.

#### `LabelEvent`
*dataclass* · [`host/kman_host/stream.py:58`](../host/kman_host/stream.py#L58)

```python
@dataclass class LabelEvent
```

**Purpose.** One label change, positioned so that a live recording and a later rebuild from the raw file assign it to the same frame.

**How it works.**

Fields:
- seq (int | None): the first unwrapped tick (seq64) the label applies to. None means "from the first frame" (the key was pressed before any frame).
- t_us (int | None): the nominal unwrapped timestamp of that tick, last_t64 + period_us. None when no frame or header existed yet.
- host_time (float): host wall-clock seconds at the key press. In replay it is 0.0 unless the stored event carries a value.
- label (int): the label code.
- name (str): the label's name from labels.label_name.
- segment (int | None, default None): the segment number.
- seg_seq (int | None, default None): the tick offset from that segment's first frame.

The code comment says (segment, seg_seq) is a position independent of how segment gaps were bridged: by the host clock live, and not at all in replay.

**Notes.** set_label creates these live. schedule_labels rebuilds them from .labels.jsonl dicts. label_events_dicts turns them back into dicts for SessionWriter.close, and logger.Recorder.set_label streams them with SessionWriter.write_label_event(asdict(ev)).

#### `GapEvent`
*dataclass* · [`host/kman_host/stream.py:71`](../host/kman_host/stream.py#L71)

```python
@dataclass class GapEvent
```

**Purpose.** Records one run of lost frames inside a segment.

**How it works.**

Fields:
- seq (int): the first missing tick, last_seq64 + 1.
- t_us (int): the nominal unwrapped timestamp of that tick, last_t64 + period.
- missing (int): the number of missing ticks, nt - 1.

**Notes.** _on_data appends one only while len(gaps) &lt; keep_gap_events. stats.dropped keeps counting after the list is full. summary() reports them under "gaps".

#### `StreamDecoder`
*class* · [`host/kman_host/stream.py:77`](../host/kman_host/stream.py#L77)

```python
class StreamDecoder
```

**Purpose.** A stateful decoder from a device byte stream to columns of unwrapped, segmented, labelled samples. It also collects headers, device status, device logs, gaps and UI notices.

**How it works.**

Public state:
- parser: protocol.FrameParser.
- stats: StreamStats.
- header: the current SessionHeader.
- headers: raw dicts of the first header and of each header that started a new stream.
- status / status_first: the last and first DeviceStatus.
- logs: deque of the last 200 device log lines.
- gaps: list of GapEvent.
- label: the current label code.
- label_events: applied or pressed LabelEvents.
- segment: -1 until the first header.
- last_t64 / last_seq64: the unwrapped time and tick of the last accepted frame.
- new_header: a flag the consumer clears.
- events: deque of the last 1000 human-readable notices.

Private state:
- _last_t32 / _last_seq16: raw values of the last accepted frame. _last_t32 is None at the start of the session and of each new segment.
- _seg_first_seq64: seq64 of the current segment's first frame.
- _last_host_time and _seg_gap_host: host times used to bridge segments.
- _pending_labels: replay events not applied yet.
- _rows / _row_host / _row_meta: the row buffers.

Class constants: HEADER_LEAD_MAX_US = 10_000_000, HEADER_LAG_MAX_US = 1_000_000 and BACKWARD_RESTART_US = 1_000_000, all in microseconds.

**Notes.** Not thread-safe. Every consumer drives it from one loop.

#### `StreamDecoder.__init__`
*constructor* · [`host/kman_host/stream.py:78`](../host/kman_host/stream.py#L78)

```python
def __init__(self, keep_gap_events: int = 10000)
```

**Purpose.** Creates a decoder in the state before any header has arrived.

**How it works.**

It sets:
- parser = P.FrameParser(), stats = StreamStats().
- header, status and status_first = None. headers = [].
- logs = deque(maxlen=200), gaps = [], _keep_gaps = keep_gap_events.
- label = 0, label_events = [], _pending_labels = [].
- segment = -1.
- _last_t32 = None, _last_seq16 = 0, _seg_first_seq64 = 0.
- last_t64 = None, last_seq64 = None.
- _last_host_time = 0.0, _seg_gap_host = 0.0.
- _rows, _row_host and _row_meta = []. Each _row_meta entry is a tuple (t64, seq64, segment, label).
- new_header = False, events = deque(maxlen=1000).

**Notes.** keep_gap_events only caps the number of GapEvent records kept, not the drop counting.

#### `StreamDecoder.feed`
*method* · [`host/kman_host/stream.py:107`](../host/kman_host/stream.py#L107)

```python
def feed(self, data: bytes, host_time: float | None = None) -> list[P.Frame]
```

**Purpose.** The entry point for raw bytes. It parses complete frames and decodes each one, and returns the frames so the caller can re-broadcast them.

**How it works.**

1. If host_time is None, it uses time.time().
2. frames = self.parser.feed(data). The parser buffers partial frames across calls and drops frames with a bad CRC or length.
3. It calls handle_frame(f, host_time) for each frame. All frames in one chunk share the same host_time.
4. It returns frames.

**Notes.** Exceptions from handle_frame propagate and abort the rest of the chunk. They are LayoutChanged and the errors from SessionHeader.from_json: ValueError (including json.JSONDecodeError for malformed JSON), or KeyError/TypeError for a header with missing or malformed channel entries. The parser has already consumed every frame in the chunk, so the frames after the failing one are lost. They are not parsed again on the next call. Replay, the simulator and most tests pass host_time=0.0, which turns host-gap bridging off (see _on_data). Some tests in test_v1_fixes.py pass increasing host times on purpose to exercise the bridge. The live logger and live_view pass nothing, so time.time() is used.

#### `StreamDecoder.handle_frame`
*method* · [`host/kman_host/stream.py:115`](../host/kman_host/stream.py#L115)

```python
def handle_frame(self, f: P.Frame, host_time: float) -> None
```

**Purpose.** Routes one parsed frame to its handler by frame type.

**How it works.**

- TYPE_DATA (0x01): calls _on_data(f.body, host_time).
- TYPE_HEADER (0x02): calls _on_header(f.body, host_time).
- TYPE_STATUS (0x03): st = P.decode_status(f.body). If it is not None: stats.status_frames += 1, status = st, and status_first = st if it was None. A malformed status (wrong size or version byte != 1) is ignored silently.
- TYPE_LOG (0x04): stats.log_messages += 1. It decodes the body as UTF-8 with errors="replace", appends it to logs, and appends "device: &lt;msg>" to events.
- TYPE_LABEL (0x10) with a non-empty body: label = f.body[0]. This is the viewer side, where the logger announces its current label over UDP.
- Any other type, or an empty LABEL body, is ignored.

**Notes.** A label set through TYPE_LABEL is written into the meta of the following rows. It creates no LabelEvent.

#### `StreamDecoder._on_header`
*method* · [`host/kman_host/stream.py:136`](../host/kman_host/stream.py#L136)

```python
def _on_header(self, body: bytes, host_time: float) -> None
```

**Purpose.** Handles a session header. The first one defines the layout. A later one is either ignored (a repeat of the running stream), rejected (a different layout), or starts a new segment (a new device stream).

**How it works.**

1. hdr = P.SessionHeader.from_json(body), then stats.headers += 1.
2. If self.header is None (the first header): header = hdr, headers.append(hdr.raw), segment = 0, stats.segments = 1, new_header = True, then return. DATA frames that arrived earlier have already been counted in frames_before_header and dropped.
3. If hdr.layout_key() != self.header.layout_key(): raise LayoutChanged.
4. restarted, why = _new_stream(hdr). If restarted: header = hdr, headers.append(hdr.raw), and _start_segment(host_time, f"new stream ({why})").
5. If not restarted, the new header is discarded: self.header and headers stay unchanged.

**Notes.** new_header is set only by the first header. A restart keeps the same layout, so consumers (SessionWriter.set_header) are not called again. SessionHeader.from_json raises json.JSONDecodeError (a ValueError) for malformed JSON, ValueError for a header whose type is not "kman_session_header" or that has an "error" key, and KeyError/TypeError/ValueError for malformed channel entries. All of these happen before stats.headers is incremented, and the error propagates out of feed(). The layout check runs before _new_stream, so a real restart that also changes the layout raises LayoutChanged and does not start a segment.

#### `StreamDecoder.HEADER_LEAD_MAX_US / HEADER_LAG_MAX_US / BACKWARD_RESTART_US`
*class constants* · [`host/kman_host/stream.py:158`](../host/kman_host/stream.py#L158)

```python
HEADER_LEAD_MAX_US = 10_000_000; HEADER_LAG_MAX_US = 1_000_000; BACKWARD_RESTART_US = 1_000_000
```

**Purpose.** The tolerances, in microseconds, that separate a continuing stream from a restarted device clock.

**How it works.**

- HEADER_LEAD_MAX_US (10 s): how far a header's t_us_at_header may be ahead of the last received frame's timestamp.
- HEADER_LAG_MAX_US (1 s): how far it may be behind.
- BACKWARD_RESTART_US (1 s): a backward step in frame timestamps larger than this is a clock restart, not a duplicate. A forward step larger than this whose 16-bit seq disagrees is also a restart.

**Notes.** From the code comment: a re-sent header (the answer to 'H') is built while the device is streaming, so its timestamp sits just after the last frame received. The TX-ring backlog is under 3 s, and 10 s is that plus a margin.

#### `StreamDecoder._new_stream`
*method* · [`host/kman_host/stream.py:163`](../host/kman_host/stream.py#L163)

```python
def _new_stream(self, hdr: P.SessionHeader) -> tuple[bool, str]
```

**Purpose.** Decides whether a later header with the same layout starts a new device stream (port re-open, re-enumeration or reboot). Returns (restarted, reason).

**How it works.**

old = self.header.raw. The rules are checked in order:
1. Identical copy: if hdr.raw == old (dict equality of the parsed JSON), return (False, ""). This runs before the timestamp rule, so a repeated copy with a stale t_us_at_header (for example, the same header delivered twice to a viewer) never counts as a clock restart.
2. Ids: for key in ("device_id", "boot_id", "stream_id"), if hdr.raw.get(key) != old.get(key), return (True, f"{key} changed"). A key present in one header and missing in the other counts as a change (None != value).
3. Timestamp continuity: if t_hdr = hdr.raw.get("t_us_at_header") is not None and _last_t32 is not None, compute ahead = (int(t_hdr) - _last_t32) & 0xFFFFFFFF, the forward distance mod 2^32. The header is continuous when ahead &lt;= HEADER_LEAD_MAX_US (up to 10 s ahead) or ahead >= 0x100000000 - HEADER_LAG_MAX_US (up to 1 s behind). Otherwise it returns (True, "device clock restarted").
4. Otherwise it returns (False, "").

**Notes.** Rule 3 covers firmware without boot_id that reboots and reuses stream_id 1 (code comment). It is skipped when the header has no t_us_at_header, as with the logger's re-announced headers, which strip it. It is also skipped when no frame has arrived since the last segment start (_last_t32 is None). In those cases only the ids can signal a restart.

#### `StreamDecoder._start_segment`
*method* · [`host/kman_host/stream.py:180`](../host/kman_host/stream.py#L180)

```python
def _start_segment(self, host_time: float, why: str) -> None
```

**Purpose.** Starts a new segment for the next DATA frame and records the host-measured gap used to bridge its time axis to the previous segment.

**How it works.** segment += 1; stats.segments += 1; _last_t32 = None, which marks the next accepted frame as a segment start; _seg_gap_host = max(0.0, host_time - _last_host_time), the host seconds since the last accepted DATA frame; events.append(f"{why}: segment {segment}").

**Notes.** last_t64 and last_seq64 are kept, so _on_data can continue the unwrapped axes from them. For a header-triggered restart the gap ends at the header's arrival time. For a data-detected restart it ends at the triggering frame's host_time. Frames that arrive in the same serial chunk share one host_time, so the gap can be 0. Replay passes host_time 0.0, so the gap is always 0.

#### `StreamDecoder._on_data`
*method* · [`host/kman_host/stream.py:188`](../host/kman_host/stream.py#L188)

```python
def _on_data(self, body: bytes, host_time: float) -> None
```

**Purpose.** Decodes one DATA frame. It unwraps the timestamp and the tick counter, rejects duplicates, counts gaps, detects clock restarts the header did not announce, applies due replay labels, and buffers the row.

**How it works.**

1. If header is None: stats.frames_before_header += 1 and return; the frame is discarded.
2. rec = P.decode_data(body, header.data_dtype()). The dtype is t_us &lt;u4, seq &lt;u2, valid u1, then one &lt;i2 per channel. If the size mismatches: stats.bad_size += 1 and return.
3. t32 = rec["t_us"][0], seq16 = rec["seq"][0], period = header.period_us.
4. First frame of the session or segment (_last_t32 is None):
   a. If last_t64 is None (the very first frame of the session): t64 = t32, seq64 = seq16. Both axes start from the raw values, not from 0.
   b. Otherwise (the first frame of a new segment, the bridge): nt = max(1, int(round(_seg_gap_host \* 1e6 / period))), t64 = last_t64 + nt \* period, seq64 = last_seq64 + nt. The new segment continues monotonically after the host-measured gap, rounded to whole periods with at least one. No duplicate, seq or gap checks run on this frame, and it adds nothing to stats.dropped or gaps. In replay _seg_gap_host is 0, so nt = 1 and segments abut.
5. Continuation frame:
   a. dt = (t32 - _last_t32) & 0xFFFFFFFF, the forward distance mod 2^32. Values >= 0x80000000 mean a backward step of 0x100000000 - dt.
   b. Backward restart: if dt >= 0x80000000 and 0x100000000 - dt > BACKWARD_RESTART_US (back by more than 1 s): _start_segment(host_time, "device clock jumped back"), stats.restarts_detected += 1, then return self._on_data(body, host_time). The recursive call takes the bridge path in step 4b.
   c. Duplicate: if dt == 0 or dt >= 0x80000000 (identical timestamp, or back by at most 1 s): stats.duplicates += 1 and return.
   d. nt = int(round(dt / period)), with Python round (half to even). If nt &lt;= 0 (dt about half a period or less): stats.duplicates += 1 and return.
   e. Seq cross-check: if ((seq16 - _last_seq16) & 0xFFFF) != (nt & 0xFFFF):
      - if dt > BACKWARD_RESTART_US: this is a forward restart. _start_segment(host_time, "device clock jumped (seq disagrees)"), restarts_detected += 1, and recurse as in 5b.
      - otherwise stats.seq_mismatch += 1 and the frame is still accepted.
   f. Gap: if nt > 1, missing = nt - 1 and stats.dropped += missing. If len(gaps) &lt; _keep_gaps, it appends GapEvent(last_seq64 + 1, last_t64 + period, missing). If missing >= 2, it appends "gap: {missing} frames lost at t={last_t64/1e6:.2f} s" to events.
   g. t64 = last_t64 + dt, using the actual timestamp delta so device timing jitter is kept. seq64 = last_seq64 + nt, so seq64 follows the timestamp-derived tick count and never seq16.
6. If _last_t32 is still None (the frame starts the session or a segment): _seg_first_seq64 = seq64.
7. _apply_pending_labels(seq64), so a replay label due at this frame already applies to it.
8. Commit state: _last_t32 = t32, _last_seq16 = seq16, last_t64 = t64, last_seq64 = seq64, _last_host_time = host_time, stats.frames_data += 1. It appends rec to _rows, host_time to _row_host, and (t64, seq64, segment, label) to _row_meta.

**Notes.** Gap size comes from the timestamp, which is unambiguous for forward gaps below 2^31 us (about 35.8 min; the module docstring says 35 minutes). A larger forward jump looks like a backward step: above 1 s back it starts a restart segment, and within 1 s of 2^32 it counts as a duplicate. The forward-restart rule exists because t_us and seq come from one tick counter, so a real gap always agrees mod 2^16. A long jump that disagrees is a restarted clock that only looks like a forward step, which happens when the old uptime exceeds 35.8 min (code comment). A rejected frame (duplicate, bad size, before header) changes only its own StreamStats counter. It leaves the position state (_last_t32, _last_seq16, last_t64, last_seq64, _last_host_time), the label state and the row buffers untouched.

#### `StreamDecoder.set_label`
*method* · [`host/kman_host/stream.py:254`](../host/kman_host/stream.py#L254)

```python
def set_label(self, code: int, host_time: float | None = None) -> LabelEvent
```

**Purpose.** Live labelling. A key press applies from the next frame. It records the event with both a seq position and a (segment, seg_seq) position so replay can reproduce it.

**How it works.**

1. If host_time is None, it uses time.time().
2. nxt_seq = None if last_seq64 is None, else last_seq64 + 1.
3. nxt_t = last_t64 + header.period_us if both last_t64 and header exist, else None.
4. seg_seq = 0 if _last_t32 is None (no frame yet in the current segment: before the first frame, or between _start_segment and the segment's first frame), else nxt_seq - _seg_first_seq64.
5. ev = LabelEvent(nxt_seq, nxt_t, host_time, int(code), label_name(code), max(segment, 0), seg_seq).
6. label = int(code), so the next appended row's meta carries the new label. It appends ev to label_events and returns it.

**Notes.** The label takes effect at the next accepted frame whatever its actual seq64. A press during a reconnect gap or before a bridged segment start therefore lands on the first frame after it, and (segment, seg_seq) points at that same frame in replay. Before the first header, segment is -1 and is stored as 0.

#### `StreamDecoder.schedule_labels`
*method* · [`host/kman_host/stream.py:267`](../host/kman_host/stream.py#L267)

```python
def schedule_labels(self, events: list[dict]) -> None
```

**Purpose.** Replay labelling. It loads recorded label events so _on_data applies each one at the frame it was pressed before.

**How it works.**

1. It builds LabelEvent(e.get("seq"), e.get("t_us"), e.get("host_time", 0.0), int(e["label"]), e.get("name", label_name(e["label"])), e.get("segment"), e.get("seg_seq")) for each dict.
2. If the list is non-empty and every event has both segment and seg_seq, it sorts by the key lambda e: (e.segment, e.seg_seq).
3. Otherwise it sorts by lambda e: -1 if e.seq is None else e.seq.
4. It replaces _pending_labels with the sorted list. Nothing is applied until DATA frames arrive.

**Notes.** From the docstring: events with (segment, seg_seq) are placed by segment and offset, which does not depend on how a reconnect gap was bridged. Older seq-only events are placed by seq, "as before 1.0". In a mixed list the order is by seq, but each event is still tested with its own fields in _label_due. Because _apply_pending_labels stops at the first event that is not due, a due event behind a head that is not due waits. The module docstring's Labels paragraph describes only the seq method.

#### `StreamDecoder._label_due`
*method* · [`host/kman_host/stream.py:283`](../host/kman_host/stream.py#L283)

```python
def _label_due(self, ev: LabelEvent, seq64: int) -> bool
```

**Purpose.** Returns whether a pending replay label event applies at the frame with unwrapped tick seq64.

**How it works.** If ev.segment and ev.seg_seq are both set, it returns the lexicographic tuple comparison (ev.segment, ev.seg_seq) &lt;= (max(segment, 0), seq64 - _seg_first_seq64). The right side is the current frame's segment and its offset within that segment (0 for the segment's first frame). Otherwise (a legacy event) it returns ev.seq is None or ev.seq &lt;= seq64.

**Notes.** _on_data sets _seg_first_seq64 for the current frame before this runs. With the tuple comparison, an event from an earlier segment becomes due at any frame of a later segment. That matches live behaviour, where a press after a segment's last frame lands on the next segment's first frame.

#### `StreamDecoder._apply_pending_labels`
*method* · [`host/kman_host/stream.py:288`](../host/kman_host/stream.py#L288)

```python
def _apply_pending_labels(self, seq64: int) -> None
```

**Purpose.** Applies every pending replay label that is due at the current frame, before the frame's row is stored.

**How it works.** While _pending_labels is non-empty and _label_due(_pending_labels[0], seq64) is true: it pops the head with pop(0), sets label = ev.label, and appends ev to label_events.

**Notes.** If several events are due at one frame, all are logged and the last one's label wins. In live use _pending_labels is empty, so this is a no-op.

#### `StreamDecoder.pending_rows`
*method* · [`host/kman_host/stream.py:296`](../host/kman_host/stream.py#L296)

```python
def pending_rows(self) -> int
```

**Purpose.** Returns how many decoded rows are buffered since the last drain.

**How it works.** Returns len(_rows). Each entry is a 1-element structured array, so this is the number of frames.

**Notes.** Consumers flush when it reaches a threshold: 100 in logger.Recorder.feed, 2000 in sim_device.write_session.

#### `StreamDecoder.drain`
*method* · [`host/kman_host/stream.py:299`](../host/kman_host/stream.py#L299)

```python
def drain(self) -> dict[str, np.ndarray] | None
```

**Purpose.** Hands the buffered samples to the consumer as columns and empties the buffers.

**How it works.**

1. If _rows is empty, it returns None.
2. recs = np.concatenate(_rows) and meta = np.array(_row_meta, dtype=np.int64), shape (n, 4).
3. It builds the columns in this order:
   - t_us = meta[:,0] (int64, unwrapped us)
   - seq = meta[:,1] (int64)
   - segment = meta[:,2] as int16
   - valid = recs["valid"] as uint8 (per-slot validity bitmask)
   - one int16 column per name in header.channel_names, from recs
   - label = meta[:,3] as int8
   - host_time = _row_host as float64
4. It clears _rows, _row_host and _row_meta and returns the dict.

**Notes.** The int8 cast wraps label codes above 127. The decoder never validates codes: set_label, schedule_labels (from a possibly hand-edited .labels.jsonl) and the TYPE_LABEL branch all pass any value through. The shipped consumers only produce the defined labels 0-9. The logger accepts digit keys and viewer codes in LABELS, and the viewer accepts digit keys. The layout cannot change mid-session (LayoutChanged), so the current header's channel names apply to every buffered row.

#### `StreamDecoder.summary`
*method* · [`host/kman_host/stream.py:322`](../host/kman_host/stream.py#L322)

```python
def summary(self) -> dict
```

**Purpose.** Builds the session statistics dict stored in the output metadata and printed at the end of a run.

**How it works.** It returns {"stream": asdict(stats), "drop_pct": stats.drop_pct(), "parser": asdict(parser.stats), "gaps": [asdict(g) for g in gaps]}. The parser stats are frames, crc_errors, bad_length and bytes_discarded. If status is set it adds "device_status_last": status.as_dict(), and if status_first is set it adds "device_status_first": status_first.as_dict().

**Notes.** "gaps" holds at most keep_gap_events entries.

#### `StreamDecoder.summary_json`
*method* · [`host/kman_host/stream.py:336`](../host/kman_host/stream.py#L336)

```python
def summary_json(self) -> str
```

**Purpose.** Returns summary() as a JSON string.

**How it works.** Returns json.dumps(self.summary()).

**Notes.** Nothing in the repository calls it. Consumers pass summary() to SessionWriter.close instead.

#### `StreamDecoder.label_events_dicts`
*method* · [`host/kman_host/stream.py:339`](../host/kman_host/stream.py#L339)

```python
def label_events_dicts(self) -> list[dict]
```

**Purpose.** Returns the label events as plain dicts for the Parquet metadata (the "labels" key written by SessionWriter.close). The .labels.jsonl sidecar is not written from here.

**How it works.** Returns [asdict(e) for e in label_events]. Each dict has the keys seq, t_us, host_time, label, name, segment and seg_seq.

**Notes.** Live, label_events holds every key press in press order. In replay it holds only the scheduled events that became due at some frame, in the order they were applied (sorted order). An event that never becomes due is left out of the Parquet metadata of the rebuilt output, for example a press after the session's last frame or a due event stuck behind a head that is never due. logger.run_replay still writes every recorded event to the output's .labels.jsonl, because it passes them to SessionWriter(label_events=...).

## 11. Host: session files, UDP bus, keyboard input

- **`host/kman_host/session_io.py`**: Defines the on-disk session format and both sides of it: SessionWriter writes the Parquet sample file plus the two live sidecars (&lt;stem>.bin raw bytes, &lt;stem>.labels.jsonl label events), and load_session/read_label_events read them back. The Parquet schema is t_us int64, seq int64, segment int16, valid uint8, one int16 column per header channel, label int8, host_time float64, zstd-compressed. All metadata lives in the Parquet footer key/value store under the prefix META_PREFIX = "kman.": kman.format_version ("1", FORMAT_VERSION) and kman.header (first session header JSON) are written when the file is opened; kman.headers, kman.labels, kman.label_names, kman.stats, kman.extra and kman.notes are added at close.
- **`host/kman_host/udp_bus.py`**: Localhost UDP transport between logger.py (which owns the serial port) and live_view.py. Data flows logger -> viewer on DEFAULT_PORT = 51515 (verbatim device frames, the HEADER re-sent every 2 s, host LABEL frames type 0x10); commands flow viewer -> logger on port+1 (51516) as ASCII text such as b"LABEL 3". Every send ignores errors and every receive is non-blocking, so a slow or absent viewer cannot stall recording.
- **`host/kman_host/keyboard.py`**: Cross-platform non-blocking single-key terminal input for the logger's live labelling (digits 0-9, q, ?). KeyReader is a context manager: on POSIX it puts the terminal into cbreak mode on entry and restores the saved termios state on exit; on Windows it uses msvcrt directly. poll() returns the characters typed since the previous call without blocking.

**How it connects.**

These three modules are used by the host scripts.

**session_io**
- host/logger.py writes sessions through its Recorder class. Recorder.__init__ creates SessionWriter(out, raw, extra_meta, label_events).
  - Recorder.feed calls write_raw for every serial chunk, set_header when StreamDecoder.new_header is set, and write_batch(dec.drain()) once 100 rows are pending.
  - Recorder.set_label calls write_label_event(asdict(ev)).
  - Recorder.close calls writer.close(dec.headers, dec.label_events_dicts(), dec.summary(), notes). Before that it hands over the header if a LayoutChanged interrupted feed(), and writes the final drained batch.
  - run_replay reads the label events with read_label_events. The path is --labels when given, otherwise the source's sidecar from sidecar_paths(src.with_suffix(".parquet"))[1]. It passes the events both to SessionWriter(label_events=...) and to StreamDecoder.schedule_labels, and uses raw=False.
- host/sim_device.py write_session drives SessionWriter the same way with simulator frames. It writes s.label_events() with write_label_event and drains every 2000 rows.
- load_session is the read side. host/to_csv.py uses Session.physical (or Session.df with --raw), Session.labels and Session.label_names. analysis/report.py uses Session.name and Session.header.
- analysis/kman_analysis/preprocess.py uses Session.df, fs, header.channels, has_slot and label_names. pipeline.py reads Session.stats["stream"/"parser"/"device_status_last"]. host/tests and analysis/tests also use SessionWriter, load_session, read_label_events and sidecar_paths.
- session_io calls into protocol.SessionHeader (raw, channel_names, channels[].name/.scale/.bit, sample_rate_hz, slot_present, from_json) and labels.LABELS.

**udp_bus**
- logger.Recorder owns a UdpSender(udp_port, default DEFAULT_PORT 51515). It has none when --no-udp is given, or in replay, where udp_port=None. Recorder.feed uses it to re-broadcast the parsed frames' raw bytes. announce_label sends P.encode_frame(TYPE_LABEL, ...) after each set_label. reannounce_header re-sends the latest header (with t_us_at_header removed) plus the current label every 2 s from run_live.
- run_live binds UdpReceiver(udp_port + 1) unless --no-udp is given; an OSError from bind disables viewer control. In handle_inputs it passes each datagram to parse_label_command, then to Recorder.set_label when the code is in LABELS.
- host/live_view.py UdpSource is the other end. Its rx is a UdpReceiver(port) whose poll() output is joined and fed to StreamDecoder. Its tx is a UdpSender(port + 1), which send_label uses to send b"LABEL &lt;code>".

**keyboard**
- KeyReader is used only by logger.run_live, as `with KeyReader() as keys`. handle_inputs calls keys.poll(): digits go to Recorder.set_label, "q"/"Q" stops the loop, and "?" prints labels.help_text(). This also runs while the serial port is reconnecting.
- host/tests/test_v1_fixes.py monkeypatches logger.KeyReader with a fake.

### `host/kman_host/keyboard.py`

#### `KeyReader`
*class* · [`host/kman_host/keyboard.py:9`](../host/kman_host/keyboard.py#L9)

```python
class KeyReader
```

**Purpose.** Context manager for non-blocking single-key terminal input. poll() returns the characters typed since the last call.

**How it works.** Fields: _posix_state, the termios attribute list saved on POSIX entry (None otherwise); enabled, True only when sys.stdin exists and isatty(). When stdin is piped or absent, the reader is inert and poll() always returns [].

#### `KeyReader.__init__`
*constructor* · [`host/kman_host/keyboard.py:12`](../host/kman_host/keyboard.py#L12)

```python
__init__(self) -> None
```

**Purpose.** Decides whether keyboard input is possible. It does not touch the terminal yet.

**How it works.** self._posix_state = None; self.enabled = sys.stdin is not None and sys.stdin.isatty().

#### `KeyReader.__enter__`
*method* · [`host/kman_host/keyboard.py:16`](../host/kman_host/keyboard.py#L16)

```python
__enter__(self) -> "KeyReader"
```

**Purpose.** On POSIX terminals, switches stdin to cbreak mode so single key presses are readable at once, without Enter.

**How it works.**

1. If enabled and os.name != "nt": it imports termios and tty, takes fd = sys.stdin.fileno(), saves _posix_state = termios.tcgetattr(fd), and calls tty.setcbreak(fd). That turns off canonical line buffering and echo; signal keys such as Ctrl-C still raise SIGINT.
2. On Windows, or when disabled, it changes nothing.
3. Returns self.

#### `KeyReader.__exit__`
*method* · [`host/kman_host/keyboard.py:26`](../host/kman_host/keyboard.py#L26)

```python
__exit__(self, *exc) -> None
```

**Purpose.** Restores the terminal settings saved on entry.

**How it works.** If _posix_state is not None, calls termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, _posix_state), then sets _posix_state = None. Returns None, so exceptions propagate.

**Notes.** TCSADRAIN applies the change after pending output has been sent. Because logger.run_live uses `with KeyReader()`, the terminal is restored on KeyboardInterrupt and on errors too.

#### `KeyReader.poll`
*method* · [`host/kman_host/keyboard.py:33`](../host/kman_host/keyboard.py#L33)

```python
poll(self) -> list[str]
```

**Purpose.** Returns all characters typed since the previous call, without blocking.

**How it works.**

1. Returns [] if not enabled.
2. Windows (os.name == "nt"): while msvcrt.kbhit(), ch = msvcrt.getwch(). If ch is "\x00" or "\xe0" (function/arrow key prefix), it reads and discards the next scan code and continues. Otherwise it appends ch.
3. POSIX: fd = sys.stdin.fileno(). While select.select([fd], [], [], 0)[0] is non-empty, data = os.read(fd, 32). If data is empty (EOF) it breaks. Otherwise it extends keys with data.decode("utf-8", "ignore"), one string per character.
4. Returns keys.

**Notes.** On POSIX, terminal escape sequences are not filtered: an arrow or function key arrives as separate characters ("\x1b", "[", "A" ...). For example, in xterm-compatible terminals F2 sends ESC O Q, whose "Q" logger.run_live treats as quit. A multi-byte UTF-8 character split across a 32-byte read is dropped by the "ignore" decode. Without entering the context on POSIX, the terminal stays in canonical mode and input becomes readable only after Enter.

### `host/kman_host/session_io.py`

#### `META_PREFIX`
*constant* · [`host/kman_host/session_io.py:33`](../host/kman_host/session_io.py#L33)

```python
META_PREFIX = "kman."
```

**Purpose.** Namespace prefix for every key that KMAN writes into, and reads from, the Parquet file's key/value metadata store. It keeps the KMAN keys separate from metadata that pyarrow or other tools add to the same store.

**How it works.** A plain str that is concatenated with a key suffix wherever metadata is built or looked up. In SessionWriter._open it is prepended to "format_version" and "header", which go into the schema metadata when the file is opened. In SessionWriter.close it is prepended to "headers", "labels", "label_names", "stats", "extra" and "notes", which are added through ParquetWriter.add_key_value_metadata just before the footer is written. In load_session, after the metadata bytes are decoded to str, it builds the lookup keys: kv[META_PREFIX + "header"] is required, and the other keys are read with kv.get(...) and a default ("{}", "[]" or "").

**Notes.** The full keys on disk are therefore kman.format_version, kman.header, kman.headers, kman.labels, kman.label_names, kman.stats, kman.extra and kman.notes. Changing this value makes files written earlier unreadable to load_session, because the required kman.header lookup raises KeyError.

#### `FORMAT_VERSION`
*constant* · [`host/kman_host/session_io.py:34`](../host/kman_host/session_io.py#L34)

```python
FORMAT_VERSION = 1
```

**Purpose.** Version number of the session-file layout, recorded in every Parquet file so that a reader can tell which layout it is looking at.

**How it works.** An int. SessionWriter._open stores it in the schema metadata as META_PREFIX + "format_version" = str(FORMAT_VERSION), so the value on disk is the string "1". It is written once, when the ParquetWriter is created on the first flush.

**Notes.** Nothing reads it back. load_session does not look up kman.format_version, and no other module in host/ references it, so a file with a different version loads the same way, with no check or warning.

#### `sidecar_paths`
*function* · [`host/kman_host/session_io.py:37`](../host/kman_host/session_io.py#L37)

```python
sidecar_paths(parquet_path: str | Path) -> tuple[Path, Path]
```

**Purpose.** Derives the raw-capture and label-sidecar paths that belong to a Parquet session path, so writer, replay and tests agree on file names.

**How it works.**

1. p = Path(parquet_path); stem = p.with_suffix("") (removes the last suffix only).
2. Returns (stem.with_suffix(".bin"), Path(str(stem) + ".labels.jsonl")). Example: data/s01.parquet -> (data/s01.bin, data/s01.labels.jsonl).

**Notes.** The two paths are built differently. with_suffix(".bin") replaces any remaining suffix, but the labels path appends to the string. For a dotted name such as data/s01.rest.parquet the result is data/s01.bin and data/s01.rest.labels.jsonl. For a path with no suffix, stem equals the path itself.

#### `SessionWriter`
*class* · [`host/kman_host/session_io.py:43`](../host/kman_host/session_io.py#L43)

```python
class SessionWriter
```

**Purpose.** Writes one recording: raw device bytes and label events to crash-safe sidecars as they arrive, and decoded sample batches to a Parquet file in row groups, then the session-wide metadata at close.

**How it works.**

Instance fields:
- path: Path of the .parquet output.
- raw_path: Path of &lt;stem>.bin, or None when raw=False.
- labels_path: Path of &lt;stem>.labels.jsonl (always created).
- _raw: open binary file handle for .bin, or None.
- _labels: open text handle (utf-8, mode "w") for .labels.jsonl.
- _writer: pq.ParquetWriter, created lazily on the first flush.
- _schema: pa.Schema built from the first header.
- _pending: list of column-dict batches not yet written; _pending_rows: their row total.
- _row_group_rows: flush threshold in rows (default 3000).
- rows_written: count of rows written to Parquet so far.
- header: the first SessionHeader handed over by set_header, else None.
- extra_meta: copy of the caller's dict plus created_unix; written as kman.extra.
- _last_raw_flush: time.monotonic() of the last periodic .bin flush.

Parquet schema (built in _open from header.channel_names): t_us int64 (unwrapped device time, us), seq int64 (unwrapped tick index), segment int16, valid uint8 (per-slot bitmask), one int16 column per raw channel in header order, label int8, host_time float64 (unix seconds at receipt). Compression is zstd.

Footer key/value metadata, all prefixed "kman.":
- kman.format_version: "1" (str(FORMAT_VERSION)), set at open via the schema metadata.
- kman.header: json.dumps(header.raw), the first session header, set at open.
- kman.headers: JSON list of every header dict passed to close().
- kman.labels: JSON list of the label events passed to close().
- kman.label_names: JSON object {"0": "none", ..., "9": "simulated obstruction"} from labels.LABELS, with string keys.
- kman.stats: JSON of the stats dict passed to close() (the decoder summary).
- kman.extra: JSON of extra_meta (created_unix plus the caller's keys).
- kman.notes: the notes string, stored as is (not JSON-encoded).

**Notes.** If no batch is ever flushed (no header or no data), close() never creates the Parquet file. Only the .bin and .labels.jsonl sidecars exist then. A process crash leaves a Parquet file with no footer, which cannot be read; logger.py --replay rebuilds it from the .bin.

#### `SessionWriter.__init__`
*constructor* · [`host/kman_host/session_io.py:44`](../host/kman_host/session_io.py#L44)

```python
__init__(self, path: str | Path, raw: bool = True, row_group_rows: int = 3000, extra_meta: dict | None = None, label_events: list[dict] | None = None)
```

**Purpose.** Creates the output directory, opens the sidecar files and initialises the writer state. The Parquet file itself waits until a header and the first batch exist.

**How it works.**

1. self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True).
2. raw_path, labels_path = sidecar_paths(self.path). self.raw_path = raw_path if raw else None; self.labels_path = labels_path.
3. self._raw = open(raw_path, "wb") if raw else None. Opening truncates any existing .bin.
4. self._labels = open(labels_path, "w", encoding="utf-8"). This always truncates the labels sidecar.
5. label_events parameter: each dict in label_events (if given) is written right away with write_label_event, one JSON line each, flushed. The code comment says "replay: carry the recorded events over". logger.run_replay reads the source session's labels with read_label_events and passes them here, so the rebuilt session's .labels.jsonl holds the same events. When --out has the source's stem, this rewrites the file that was just read with the same content. sim_device.write_session passes nothing here and calls write_label_event itself instead. These events go only to the sidecar; the kman.labels footer key comes from close()'s own label_events argument.
6. Sets _writer = None, _schema = None, _pending = [], _pending_rows = 0, _row_group_rows = row_group_rows, rows_written = 0, header = None.
7. extra_meta = dict(extra_meta or {}), then extra_meta.setdefault("created_unix", time.time()).
8. _last_raw_flush = time.monotonic().

**Notes.** With raw=False the .bin is never opened. This is what makes a replay into the source's own stem safe: the source .bin is not truncated.

#### `SessionWriter.write_raw`
*method* · [`host/kman_host/session_io.py:74`](../host/kman_host/session_io.py#L74)

```python
write_raw(self, data: bytes) -> None
```

**Purpose.** Appends received device bytes verbatim to the .bin capture, flushing at most once per second, so a crash loses at most about 1 s of buffered bytes.

**How it works.**

1. Does nothing if self._raw is None (raw=False) or data is empty.
2. self._raw.write(data).
3. now = time.monotonic(); if now - self._last_raw_flush > 1.0 s, calls self._raw.flush() and sets _last_raw_flush = now.

**Notes.** flush() also flushes the .bin after each Parquet row group but does not reset _last_raw_flush. The flush pushes Python's buffer to the OS and does not fsync.

#### `SessionWriter.write_label_event`
*method* · [`host/kman_host/session_io.py:82`](../host/kman_host/session_io.py#L82)

```python
write_label_event(self, event: dict) -> None
```

**Purpose.** Appends one label event to the .labels.jsonl sidecar and flushes right away, so every key press survives a crash.

**How it works.** Writes json.dumps(event) + "\n" to self._labels, then calls self._labels.flush().

**Notes.** Event dicts from logger.Recorder.set_label are asdict(LabelEvent): seq, t_us, host_time, label, name, segment, seg_seq. Simulator events carry seq, t_us, host_time, label and name. Any JSON-serialisable dict is accepted.

#### `SessionWriter.set_header`
*method* · [`host/kman_host/session_io.py:88`](../host/kman_host/session_io.py#L88)

```python
set_header(self, header: SessionHeader) -> None
```

**Purpose.** Records the session header that defines the Parquet column layout. Only the first one counts.

**How it works.** If self.header is None, sets self.header = header. Later calls are ignored.

**Notes.** The schema is fixed by the first header. If a later header has a different layout_key() (channel names, channel scales or period_us), StreamDecoder raises LayoutChanged. That ends the live recording or the replay, so one file never mixes layouts. kman.headers comes from close()'s headers argument. Callers pass dec.headers, which holds the first header plus one header for each new device stream (a segment start after device_id/boot_id/stream_id changed or the device clock restarted). Repeated headers for the same stream, such as answers to 'H' or re-announcements, are not recorded.

#### `SessionWriter._open`
*method* · [`host/kman_host/session_io.py:92`](../host/kman_host/session_io.py#L92)

```python
_open(self) -> None
```

**Purpose.** Builds the Arrow schema from the header and opens the Parquet writer. Called lazily by flush() the first time it has rows.

**How it works.**

1. assert self.header is not None.
2. fields = [t_us int64, seq int64, segment int16, valid uint8] + [pa.field(n, pa.int16()) for n in header.channel_names] + [label int8, host_time float64].
3. meta = {"kman.format_version": str(FORMAT_VERSION) = "1", "kman.header": json.dumps(header.raw)}.
4. self._schema = pa.schema(fields, metadata=meta). pyarrow stores schema metadata in the file's key/value footer, which is where load_session reads kman.header.
5. self._writer = pq.ParquetWriter(self.path, self._schema, compression="zstd").

**Notes.** Opening the ParquetWriter creates or truncates the .parquet file.

#### `SessionWriter.write_batch`
*method* · [`host/kman_host/session_io.py:109`](../host/kman_host/session_io.py#L109)

```python
write_batch(self, batch: dict[str, np.ndarray]) -> None
```

**Purpose.** Queues a batch of decoded samples (column name -> array, as StreamDecoder.drain returns) and writes a row group once enough rows have built up.

**How it works.**

1. Raises RuntimeError("write_batch before set_header") if self.header is None.
2. Appends batch to _pending; _pending_rows += len(batch["t_us"]).
3. If _pending_rows >= _row_group_rows (default 3000), calls flush().

**Notes.** batch must not be None, since len(batch["t_us"]) would raise TypeError. Recorder.feed and sim_device.write_session pass dec.drain() directly, but only once dec.pending_rows() >= 100 (logger) or >= 2000 (simulator), so the batch is never empty. The final drains in Recorder.close and write_session check 'is not None' explicitly. Each batch must contain every schema column, because flush() looks them up by name.

#### `SessionWriter.flush`
*method* · [`host/kman_host/session_io.py:117`](../host/kman_host/session_io.py#L117)

```python
flush(self) -> None
```

**Purpose.** Writes all pending batches to the Parquet file as one table and also flushes the raw capture.

**How it works.**

1. Returns at once if _pending is empty.
2. If _writer is None, calls _open().
3. cols = {name: np.concatenate([b[name] for b in _pending]) for name in _schema.names}. Only schema columns are taken; extra keys in a batch are ignored, and a missing column raises KeyError.
4. table = pa.Table.from_pydict(cols, schema=_schema); _writer.write_table(table). With pyarrow's default row-group size this normally produces one row group per flush.
5. rows_written += table.num_rows; _pending.clear(); _pending_rows = 0.
6. If _raw is open, calls _raw.flush().

**Notes.** close() calls this, so the final partial row group (fewer than row_group_rows rows) is written at close.

#### `SessionWriter.close`
*method* · [`host/kman_host/session_io.py:131`](../host/kman_host/session_io.py#L131)

```python
close(self, headers: list[dict], label_events: list[dict], stats: dict, notes: str = "") -> None
```

**Purpose.** Writes the remaining samples, adds the session-wide metadata to the Parquet footer, and closes all three files.

**How it works.**

1. Calls flush().
2. If _writer is not None (at least one row was written), it builds meta = {kman.headers: json.dumps(headers), kman.labels: json.dumps(label_events), kman.label_names: json.dumps({str(k): v for k, v in LABELS.items()}), kman.stats: json.dumps(stats), kman.extra: json.dumps(self.extra_meta), kman.notes: notes}. It then calls _writer.add_key_value_metadata(meta), then _writer.close(), and sets _writer = None.
3. If _raw is open, closes it and sets _raw = None.
4. Closes _labels.

**Notes.** Callers (logger.Recorder.close, sim_device.write_session) pass dec.headers, dec.label_events_dicts() (events as actually applied by the decoder), dec.summary() (stream/parser/gap/device-status statistics) and a notes string. dec.headers holds the raw dict of the first header plus one dict for each header that started a new device stream. Repeated headers for the same stream are not included. If nothing was written, no metadata is stored and no .parquet is created. A second call is harmless: flush() returns early, the handles are already None, and closing the already-closed labels file does nothing. stats and label_events must be JSON-serialisable.

#### `Session`
*dataclass* · [`host/kman_host/session_io.py:155`](../host/kman_host/session_io.py#L155)

```python
@dataclass class Session
```

**Purpose.** In-memory view of a recorded session returned by load_session: the sample table plus the decoded footer metadata, with helpers for analysis code.

**How it works.**

Fields:
- path: Path of the .parquet file.
- df: pd.DataFrame of the raw sample columns (t_us, seq, segment, valid, int16 channels, label, host_time).
- header: SessionHeader parsed from kman.header (the first header).
- labels: list[dict] from kman.labels (label events as applied).
- label_names: dict[int, str] from kman.label_names (int keys).
- stats: dict from kman.stats, default {}.
- headers: list[dict] from kman.headers, default [].
- extra: dict from kman.extra, default {}.
- notes: str from kman.notes, default "".

#### `Session.fs`
*property* · [`host/kman_host/session_io.py:167`](../host/kman_host/session_io.py#L167)

```python
fs -> float
```

**Purpose.** Sample rate of the session in Hz.

**How it works.** Returns self.header.sample_rate_hz, which is float(header.raw["sample_rate_hz"]).

#### `Session.name`
*property* · [`host/kman_host/session_io.py:171`](../host/kman_host/session_io.py#L171)

```python
name -> str
```

**Purpose.** Short session name, used for output file names in reports.

**How it works.** Returns self.path.stem, the file name without its last suffix.

#### `Session.has_slot`
*method* · [`host/kman_host/session_io.py:174`](../host/kman_host/session_io.py#L174)

```python
has_slot(self, slot: str) -> bool
```

**Purpose.** Tells whether a sensor slot (for example "chest_imu", "neck_flex") was working in this session.

**How it works.** Returns self.header.slot_present(slot). That is True when the header's "sensors" list has an entry with that "slot" whose "status" == "ok".

#### `Session.physical`
*method* · [`host/kman_host/session_io.py:177`](../host/kman_host/session_io.py#L177)

```python
physical(self) -> pd.DataFrame
```

**Purpose.** Converts the raw int16 channels to physical units as float64, and masks samples whose slot was flagged invalid in that frame.

**How it works.**

1. out = DataFrame with t_s = (df.t_us - df.t_us.iloc[0]) / 1e6, seconds from the first sample.
2. valid = df["valid"].to_numpy() (uint8 bitmask).
3. For each ch in header.channels: v = df[ch.name] as float64 \* ch.scale; v[(valid & (1 &lt;&lt; ch.bit)) == 0] = NaN; out[ch.name] = v.
4. Copies label, seq and segment unchanged.
5. Returns out with columns t_s, &lt;channels...>, label, seq, segment.

**Notes.** valid and host_time are not in the output. An empty df raises IndexError at iloc[0]. t_s is measured across segments from the first row, so segment gaps show up as jumps in t_s.

#### `load_session`
*function* · [`host/kman_host/session_io.py:192`](../host/kman_host/session_io.py#L192)

```python
load_session(path: str | Path) -> Session
```

**Purpose.** Reads a finished session .parquet file into a Session, decoding all kman.\* footer metadata.

**How it works.**

1. pf = pq.ParquetFile(path).
2. kv = {k.decode(): v.decode()} over pf.metadata.metadata (or {} if None).
3. header = SessionHeader.from_json(kv["kman.header"]). The key is required: KeyError if absent, and ValueError if the JSON type is not "kman_session_header".
4. df = pf.read().to_pandas().
5. names = json.loads(kv.get("kman.label_names", "{}")). If that is empty, it falls back to {str(k): v for k, v in LABELS.items()}.
6. Returns Session(path, df, header, labels=json.loads(kv.get("kman.labels", "[]")), label_names={int(k): v}, stats=json.loads(kv.get("kman.stats", "{}")), headers=json.loads(kv.get("kman.headers", "[]")), extra=json.loads(kv.get("kman.extra", "{}")), notes=kv.get("kman.notes", "")).

**Notes.** kman.format_version is read into kv but not checked. Reading the whole table loads everything into memory. A file without a footer (writer never closed) cannot be opened by pq.ParquetFile.

#### `read_label_events`
*function* · [`host/kman_host/session_io.py:212`](../host/kman_host/session_io.py#L212)

```python
read_label_events(path: str | Path) -> list[dict]
```

**Purpose.** Reads a .labels.jsonl sidecar back into a list of event dicts, tolerating a torn line left by a crash.

**How it works.**

1. Returns [] if the path does not exist.
2. Reads the file as utf-8 and splits it into lines. Each line is stripped and empty lines are skipped.
3. json.loads each line and appends the result. A json.JSONDecodeError is silently skipped (comment: "torn last line after a crash").
4. Returns the list in file order.

**Notes.** A malformed line anywhere in the file is skipped silently, not only the last one.

### `host/kman_host/udp_bus.py`

#### `DEFAULT_PORT`
*constant* · [`host/kman_host/udp_bus.py:14`](../host/kman_host/udp_bus.py#L14)

```python
DEFAULT_PORT = 51515
```

**Purpose.** Default localhost UDP port for the logger-to-viewer data link. The viewer-to-logger control link uses this port + 1 (51516).

**How it works.** An int that is not used inside udp_bus.py itself; the two entry scripts import it. logger.py uses it as the default of --udp-port (help text: "control = port+1"). Recorder sends to it with UdpSender(udp_port), and run_live binds the control socket with UdpReceiver(args.udp_port + 1). live_view.py uses it as the const of --udp (nargs='?') and as the fallback in UdpSource(args.udp or DEFAULT_PORT). UdpSource receives on UdpReceiver(port) and sends commands with UdpSender(port + 1). Every socket uses host 127.0.0.1.

**Notes.** The module docstring describes the traffic: logger -> viewer on 51515 carries verbatim device frames, the latest HEADER re-sent every 2 s, and host LABEL frames (type 0x10); viewer -> logger on 51516 carries ASCII commands such as b"LABEL 3". The control-port offset of +1 is coded in logger.py and live_view.py, not here.

#### `MAX_DATAGRAM`
*constant* · [`host/kman_host/udp_bus.py:15`](../host/kman_host/udp_bus.py#L15)

```python
MAX_DATAGRAM = 8192
```

**Purpose.** Largest payload, in bytes, that UdpSender.send puts in one UDP datagram. Larger buffers are split into several datagrams instead of being sent as one oversized packet.

**How it works.** UdpSender.send loops i over range(0, len(data), MAX_DATAGRAM) and calls sock.sendto(data[i : i + MAX_DATAGRAM], self.addr) for each slice. Every datagram is therefore 8192 bytes, except the last, which holds the remainder (1 to 8192 bytes). Empty data sends nothing. An OSError on any slice is swallowed and the loop continues with the next slice.

**Notes.** The receiver does not reassemble datagrams: UdpReceiver.poll reads each one with recvfrom(65536) and returns them as a list, so a device frame can be split across two datagrams. The consumer has to rejoin them, which it does by feeding the bytes into a stream parser. 8192 is well below the 65536-byte receive buffer, so no datagram is truncated on receipt.

#### `UdpSender`
*class* · [`host/kman_host/udp_bus.py:18`](../host/kman_host/udp_bus.py#L18)

```python
class UdpSender
```

**Purpose.** Fire-and-forget UDP sender to a fixed localhost address. The logger uses it to broadcast frames to viewers, and live_view uses it to send label commands to the logger.

**How it works.** Fields: addr = (host, port), the destination tuple; sock = an AF_INET/SOCK_DGRAM socket, unbound, which gets an ephemeral source port on the first sendto.

#### `UdpSender.__init__`
*constructor* · [`host/kman_host/udp_bus.py:19`](../host/kman_host/udp_bus.py#L19)

```python
__init__(self, port: int, host: str = "127.0.0.1")
```

**Purpose.** Stores the destination and creates the UDP socket.

**How it works.** self.addr = (host, port); self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM). The socket is not bound or connected.

#### `UdpSender.send`
*method* · [`host/kman_host/udp_bus.py:23`](../host/kman_host/udp_bus.py#L23)

```python
send(self, data: bytes) -> None
```

**Purpose.** Sends a byte string as one or more datagrams without ever raising or blocking the caller.

**How it works.**

1. For i in range(0, len(data), MAX_DATAGRAM = 8192): sock.sendto(data[i:i+8192], addr).
2. Each sendto is wrapped in try/except OSError: pass (comment: "nobody listening / transient; never block the logger").
3. Empty data sends nothing.

**Notes.** Chunks are cut at fixed 8192-byte offsets with no regard to frame boundaries. The receiver feeds the joined bytes to a stream FrameParser, which reassembles frames and resyncs on CRC errors if a datagram is lost.

#### `UdpSender.close`
*method* · [`host/kman_host/udp_bus.py:30`](../host/kman_host/udp_bus.py#L30)

```python
close(self) -> None
```

**Purpose.** Releases the socket.

**How it works.** Calls self.sock.close().

#### `UdpReceiver`
*class* · [`host/kman_host/udp_bus.py:34`](../host/kman_host/udp_bus.py#L34)

```python
class UdpReceiver
```

**Purpose.** Non-blocking UDP listener bound to a localhost port. The viewer uses it to receive the logger's frames, and the logger uses it to receive viewer label commands.

**How it works.** Field: sock, an AF_INET/SOCK_DGRAM socket bound to (host, port) and set non-blocking.

#### `UdpReceiver.__init__`
*constructor* · [`host/kman_host/udp_bus.py:35`](../host/kman_host/udp_bus.py#L35)

```python
__init__(self, port: int, host: str = "127.0.0.1")
```

**Purpose.** Creates, binds and un-blocks the receive socket.

**How it works.** sock = socket.socket(AF_INET, SOCK_DGRAM); sock.bind((host, port)); sock.setblocking(False).

**Notes.** bind raises OSError if the port is already in use. logger.run_live catches that and prints "(viewer label control disabled: ...)".

#### `UdpReceiver.poll`
*method* · [`host/kman_host/udp_bus.py:40`](../host/kman_host/udp_bus.py#L40)

```python
poll(self) -> list[bytes]
```

**Purpose.** Drains every datagram currently queued on the socket without blocking.

**How it works.**

1. out = [].
2. Loop: data, _ = sock.recvfrom(65536). The sender address is discarded.
3. It stops on BlockingIOError or InterruptedError (queue empty), or on any other OSError (comment: "e.g. WSAECONNRESET on Windows").
4. Each payload is appended to out. Returns out in arrival order, or [] if nothing was queued.

**Notes.** Datagrams larger than 65536 bytes would be truncated. The sender never exceeds 8192.

#### `UdpReceiver.close`
*method* · [`host/kman_host/udp_bus.py:52`](../host/kman_host/udp_bus.py#L52)

```python
close(self) -> None
```

**Purpose.** Releases the socket and its port.

**How it works.** Calls self.sock.close().

#### `parse_label_command`
*function* · [`host/kman_host/udp_bus.py:56`](../host/kman_host/udp_bus.py#L56)

```python
parse_label_command(msg: bytes) -> int | None
```

**Purpose.** Parses a viewer -> logger control datagram of the form b"LABEL &lt;n>" into the label code.

**How it works.**

1. parts = msg.decode("ascii", "ignore").strip().split(). Non-ASCII bytes are dropped and the text is split on whitespace.
2. If len(parts) == 2 and parts[0] == "LABEL" (case-sensitive) and parts[1].isdigit(), returns int(parts[1]).
3. Otherwise returns None.

**Notes.** It does not check the range: "LABEL 42" returns 42. logger.run_live.handle_inputs accepts the code only if it is in LABELS. Negative numbers and extra tokens are rejected.

## 12. Host: command-line tools (logger, CSV export, simulated device)

- **`host/logger.py`**: The recording CLI and the only host process that owns the device's serial port. It reads raw bytes from the nRF52840 over USB serial, decodes them with StreamDecoder, and writes &lt;out>.parquet plus the crash-safe &lt;out>.bin and &lt;out>.labels.jsonl sidecars through SessionWriter. Digit keys (and "LABEL n" commands from live_view.py over UDP) set activity labels; decoded frames, the session header (every 2 s) and the current label are re-broadcast on localhost UDP. It survives unplug/replug, and `--replay` rebuilds a session offline from a .bin capture with its recorded labels.
- **`host/to_csv.py`**: A one-function CLI that loads a session .parquet with load_session and writes a CSV next to it. By default it writes raw counts with a relative-time column t_s. `--units` writes physical units with NaN for invalid samples, and `--labels-only` writes the label-event list instead of the samples. Every sample export gets a label_name column taken from the label table stored in the file.
- **`host/sim_device.py`**: A simulated KMAN device for testing the host tools without hardware. It builds a synthetic session with kman_host.simulator. It then either serves the session in real time on a POSIX pseudo-terminal (`--pty`) that logger.py or live_view.py can open like a real port, or writes a complete session (.parquet + .bin + .labels.jsonl) offline (`--write-session`). Both modes can inject frame drops and corruption.

**How it connects.**

All three tools are thin CLIs over the kman_host package.

logger.py's Recorder wraps two classes:
- StreamDecoder: feed, set_label, schedule_labels, drain, pending_rows, summary and label_events_dicts, plus the header, headers, new_header, events, stats, parser, status and label fields.
- SessionWriter: write_raw, write_label_event, set_header, write_batch and close, plus header and rows_written.

logger.py also uses:
- read_label_events and sidecar_paths from session_io
- KeyReader.poll for keys
- LABELS, label_name and help_text from labels
- encode_frame, TYPE_HEADER, TYPE_LABEL and CMD_SEND_HEADER from protocol

It talks to the firmware only over USB serial. It reads the byte stream and sends b"H" when it opens the port and while no header has arrived yet.

It talks to host/live_view.py over localhost UDP. UdpSender sends three things to port 51515: verbatim CRC-valid frames, the header (re-announced every 2 s without t_us_at_header) and LABEL frames. The viewer's UdpSource reads them with UdpReceiver.poll and decodes them with the viewer's own StreamDecoder. In the other direction, logger.py's UdpReceiver on port 51516 accepts the "LABEL n" commands sent by UdpSource.send_label and parses them with parse_label_command.

to_csv.py only calls load_session and Session.physical.

sim_device.py calls simulator.SimConfig, generate and DEFAULT_SCENARIO, and uses the SimSession methods and attributes stream, header_frame, label_events, labels, n and cfg. It then does one of two things:
- It drives StreamDecoder and SessionWriter itself, using the same header-handover, drain and close sequence as logger.py's Recorder. It does not use the Recorder class, uses host_time=0.0 like replay, drains every 2000 rows instead of 100, and sends nothing over UDP.
- It serves the stream on a pty that logger.run_live (via --port /dev/pts/N) or live_view.py's SerialSource opens like real hardware.

Callers: users run all three from the shell; the commands are in README.md. host/tests/test_v1_fixes.py imports logger and sim_device directly, because pyproject sets pythonpath = ["host", "analysis"]. It calls logger.main, sim_device.main and logger.Recorder.feed. CI (.github/workflows/ci.yml, also called by release.yml) reaches these files only through `python -m pytest`. The .parquet files they write are read by load_session, in analysis/report.py and in to_csv.py.

### `host/logger.py`

#### `default_out`
*function* · [`host/logger.py:34`](../host/logger.py#L34)

```python
default_out() -> Path
```

**Purpose.** Builds the default output path used when `--out` is not given in live mode.

**How it works.** Returns REPO / "data" / "&lt;YYYYmmdd_HHMMSS>.parquet". The timestamp is dt.datetime.now() (local time) formatted with "%Y%m%d_%H%M%S". REPO is the module constant Path(__file__).resolve().parents[1], i.e. the repository root.

**Notes.** The directory is not created here; SessionWriter.__init__ creates the parent with mkdir(parents=True, exist_ok=True). run_replay does not use this function: its default is &lt;src stem>_replay.parquet.

#### `Recorder`
*class* · [`host/logger.py:38`](../host/logger.py#L38)

```python
class Recorder
```

**Purpose.** Glue between the stream decoder, the session writer and the UDP re-broadcast. Live recording (run_live) and replay (run_replay) both use it, so they decode and write identically.

**How it works.**

Instance fields (set in __init__):
- dec: StreamDecoder. Parses frames, unwraps time/seq, accounts gaps and applies labels.
- writer: SessionWriter for &lt;out>.parquet and its sidecars.
- udp: UdpSender to 127.0.0.1:udp_port, or None when broadcasting is off.
- last_header_raw: bytes | None. The most recent session header re-encoded as a HEADER frame without its device timestamp; this is what gets re-announced to viewers.
- notes: list[str]. Free-text notes (the --notes argument, reconnect times), joined with "\n" into the Parquet kman.notes metadata on close().

**Notes.** The test test_reannounced_header_does_not_start_viewer_segments builds a Recorder with __new__ and sets dec/udp/last_header_raw/writer by hand. feed() therefore relies only on those four attributes.

#### `Recorder.__init__`
*constructor* · [`host/logger.py:41`](../host/logger.py#L41)

```python
__init__(self, out: Path, raw: bool, extra_meta: dict, udp_port: int | None, label_events: list[dict] | None = None)
```

**Purpose.** Creates the decoder, opens the output files and, optionally, the UDP sender.

**How it works.**

1. self.dec = StreamDecoder().
2. self.writer = SessionWriter(out, raw=raw, extra_meta=extra_meta, label_events=label_events). This immediately creates out's parent directory and opens &lt;stem>.bin with "wb" (only if raw) and &lt;stem>.labels.jsonl with "w". Every dict in label_events is written into the new .labels.jsonl right away. The Parquet file itself is opened lazily on the first row-group flush.
3. self.udp = UdpSender(udp_port) if udp_port is truthy, else None. Passing 0 also disables it.
4. last_header_raw = None, notes = [].

**Notes.** Because the sidecars are opened in write mode, constructing a Recorder truncates any existing .bin/.labels.jsonl with the same stem. Callers check only whether the .parquet exists (--force).

#### `Recorder.feed`
*method* · [`host/logger.py:49`](../host/logger.py#L49)

```python
feed(self, data: bytes, host_time: float | None = None) -> None
```

**Purpose.** Processes one chunk of received bytes: archives it raw, decodes it, re-broadcasts the frames, hands the header to the writer and writes sample batches.

**How it works.**

1. self.writer.write_raw(data). The bytes go to the .bin before any decoding; SessionWriter flushes the .bin at most once per second and on every Parquet row-group flush.
2. frames = self.dec.feed(data, host_time). host_time None means the decoder uses time.time(). This can raise LayoutChanged, which propagates out of feed().
3. If self.udp and frames is non-empty, it sends b"".join(f.raw for f in frames). Only complete, CRC-valid frames are sent (resync garbage is not), and UdpSender splits the bytes into 8192-byte datagrams.
4. For every frame with f.type == P.TYPE_HEADER (0x02), it parses the JSON body, removes "t_us_at_header" and stores P.encode_frame(P.TYPE_HEADER, json.dumps(hdr).encode()) in self.last_header_raw.
5. If self.dec.new_header is set (the decoder accepted its first header), it calls writer.set_header(dec.header), clears dec.new_header and prints "[header]" followed by dec.header.describe().
6. If dec.pending_rows() >= 100, it calls writer.write_batch(dec.drain()). The writer buffers batches and writes a Parquet row group once 3000 rows are pending.

**Notes.** Code comment on step 4: the header is re-announced to viewers every 2 s, and the device timestamp is dropped because it goes stale and the viewer's decoder would read it as a clock restart. If LayoutChanged is raised in step 2, steps 3-6 are skipped for that chunk; close() covers the missing header handover.

#### `Recorder.set_label`
*method* · [`host/logger.py:68`](../host/logger.py#L68)

```python
set_label(self, code: int) -> None
```

**Purpose.** Applies a live label change, persists it immediately and tells viewers.

**How it works.**

1. ev = self.dec.set_label(code). The decoder builds a LabelEvent with these fields:
   - seq = last_seq64 + 1 (None before the first frame).
   - t_us = last_t64 + period_us.
   - host_time = time.time().
   - label, name.
   - segment = max(dec.segment, 0).
   - seg_seq = the offset from the current segment's first frame (0 if no frame has arrived in the current segment yet).
   The decoder then sets dec.label = code, so every following frame carries it.
2. self.writer.write_label_event(asdict(ev)) appends one JSON line to .labels.jsonl and flushes it.
3. self.announce_label().

**Notes.** The label applies from the next frame decoded after the key press, not from the frame being received when it was pressed.

#### `Recorder.announce_label`
*method* · [`host/logger.py:73`](../host/logger.py#L73)

```python
announce_label(self) -> None
```

**Purpose.** Sends the current label to live viewers.

**How it works.** If self.udp exists, it sends P.encode_frame(P.TYPE_LABEL, bytes([self.dec.label])): a host-only frame type 0x10 whose 1-byte body is the label code. Otherwise it does nothing.

**Notes.** bytes([label]) requires 0 &lt;= label &lt;= 255. All codes in LABELS (0-9) fit. The viewer's StreamDecoder.handle_frame sets its label from body[0].

#### `Recorder.reannounce_header`
*method* · [`host/logger.py:77`](../host/logger.py#L77)

```python
reannounce_header(self) -> None
```

**Purpose.** Re-sends the session header and the current label so a viewer that joins late can start decoding.

**How it works.** If self.udp exists and self.last_header_raw is set, it sends last_header_raw and then calls announce_label(). Otherwise it does nothing. run_live calls it every >= 2.0 s.

**Notes.** The re-sent header has no t_us_at_header, so a viewer's StreamDecoder._new_stream sees no device-clock jump and does not start a new segment.

#### `Recorder.close`
*method* · [`host/logger.py:82`](../host/logger.py#L82)

```python
close(self) -> dict
```

**Purpose.** Writes any remaining samples, finalises the Parquet file with all metadata, closes the sidecars and the UDP socket, and returns the decoder summary.

**How it works.**

1. If writer.header is None but dec.header is not None, it calls writer.set_header(dec.header). Code comment: feed() was interrupted by LayoutChanged before it could hand the header over.
2. batch = dec.drain(). If batch is not None and writer.header is set, it calls writer.write_batch(batch).
3. summary = dec.summary(). This is a plain, JSON-serialisable dict with these keys:
   - "stream": asdict(StreamStats)
   - "drop_pct": float
   - "parser": asdict(ParserStats)
   - "gaps": a list of GapEvent dicts
   - "device_status_last" / "device_status_first": DeviceStatus.as_dict(), present only when status frames were seen
   The nested values are dicts, not dataclass objects, which is why print_summary reads them with st['frames_data'].
4. writer.close(dec.headers, dec.label_events_dicts(), summary, notes="\n".join(self.notes)). This flushes pending rows. If a Parquet writer was ever opened (including by that flush), it adds the key/value metadata kman.headers, kman.labels, kman.label_names, kman.stats, kman.extra and kman.notes, then closes the writer. It then closes the .bin and the .labels.jsonl.
5. Closes self.udp if present.
6. Returns summary.

**Notes.** If no header ever arrived, the decoder holds no rows. No .parquet file is then created, but the .bin and .labels.jsonl still exist. kman.labels holds only the events the decoder actually applied, while .labels.jsonl holds every event written.

#### `status_line`
*function* · [`host/logger.py:96`](../host/logger.py#L96)

```python
status_line(rec: Recorder, t_start: float, rate: float) -> str
```

**Purpose.** Formats the one-line live status shown during recording.

**How it works.**

Let st = rec.dec.stats, ps = rec.dec.parser.stats and el = int(time.time() - t_start). The line is built as follows:
1. "[HH:MM:SS] frames {st.frames_data:>7}  {rate:5.1f} Hz  lost {st.dropped} ({st.drop_pct():.3f}%)  crc {ps.crc_errors}". HH = el // 3600 is not wrapped at 24, MM = el // 60 % 60, SS = el % 60.
2. If rec.dec.status (the last DeviceStatus) exists, it appends "  dev[ovr {overruns} txdrop {tx_drops} i2c {sum(read_errors)} lat {max_latency_us / 1000:.1f}ms", then " NO-XTAL" if not d.hfxo, then "]".
3. It always appends "  | {rec.dec.label} {label_name(rec.dec.label)}".
Returns the string. drop_pct = 100 \* dropped / (frames_data + dropped).

**Notes.** run_live truncates the result to 160 characters before printing.

#### `print_summary`
*function* · [`host/logger.py:109`](../host/logger.py#L109)

```python
print_summary(summary: dict, out: Path, rows: int) -> None
```

**Purpose.** Prints the end-of-session report and flags frame loss at or above the acceptance limit.

**How it works.**

With st = summary["stream"] and ps = summary["parser"], it prints:
1. "Saved {rows} samples to {out}".
2. Frames received (frames_data), lost (dropped) with drop_pct to 4 decimals, CRC errors, resync bytes (ps bytes_discarded) and segments.
3. If summary has "device_status_last": overruns, tx drops, read errors (the list of per-slot counts) and "HF crystal yes/NO" from hfxo.
4. If summary["drop_pct"] >= 0.1: "WARNING: frame loss >= 0.1% (acceptance limit). See README 'Troubleshooting drops'."

**Notes.** rows is SessionWriter.rows_written, the number of rows actually written to Parquet.

#### `run_live`
*function* · [`host/logger.py:122`](../host/logger.py#L122)

```python
run_live(args) -> int
```

**Purpose.** Records from a serial port until q, Ctrl-C, --duration or a layout change. It handles labelling, the live status line, UDP re-broadcast and transparent reconnects.

**How it works.**

Setup:
1. Lazily imports serial (pyserial).
2. out = Path(args.out) or default_out(). If out exists and not args.force, prints an error to stderr and returns 2.
3. Creates rec = Recorder(out, raw=not args.no_raw, extra_meta={"source": "serial", "port", "subject", "argv": sys.argv}, udp_port=None if args.no_udp else args.udp_port). Appends args.notes to rec.notes if set.
4. Unless --no-udp, opens ctl = UdpReceiver(args.udp_port + 1) as the viewer control port (default 51516). An OSError such as port-in-use prints "(viewer label control disabled: ...)" and leaves ctl = None.
5. Prints the banner, then ser = open_port(). Initialises t_start = last_print = last_announce = last_rate_t = header_asked = time.time(), last_rate_n = 0, rate = 0.0, running = True.

Main loop (inside try, with KeyReader() as keys, while running):
1. data = ser.read(max(1, ser.in_waiting)). This takes everything already buffered, or blocks up to the 0.02 s port timeout for one byte, so an idle loop runs about every 20 ms.
2. On OSError (SerialException subclasses it; on POSIX in_waiting raises a bare OSError), it enters the reconnect wait (below), then continues the outer loop.
3. If data is non-empty, calls rec.feed(data). On LayoutChanged it prints "[...] -- stopping; start a new recording for the new firmware." and breaks out of the loop.
4. now = time.time(). If rec.dec.header is None and now - header_asked > 2.0, it sets header_asked = now, re-sends P.CMD_SEND_HEADER (b"H", ignoring OSError) and prints how many frames were parsed and how many data frames are waiting (frames_before_header). Those frames are discarded by the decoder but kept in the .bin.
5. If handle_inputs(keys) returns False, sets running = False. The rest of this iteration still runs.
6. Pops and prints every notice in rec.dec.events (gaps of 2 or more frames, new segments, device log messages).
7. If now - last_rate_t >= 2.0, sets rate = (frames_data - last_rate_n) / (now - last_rate_t) and resets the two markers.
8. If now - last_print >= 0.5, writes "\r" + status_line(...)[:160] + "\x1b[K" (ANSI erase to end of line) and flushes.
9. If now - last_announce >= 2.0, calls rec.reannounce_header().
10. If args.duration and now - t_start >= args.duration, sets running = False.

Reconnect wait (step 2 in detail):
1. Prints "[serial error: e] reconnecting... (q to stop)", calls ser.close() and sets next_try = now + 1.0.
2. while running:
   a. running = handle_inputs(keys). Labels typed now go through rec.set_label; their seq is last_seq64 + 1, so they apply from the first frame after the reconnect.
   b. If --duration has elapsed, running = False.
   c. If not running or now &lt; next_try, sleeps 0.05 s and continues (so input is polled every 50 ms).
   d. Otherwise sets next_try = now + 1.0 and tries ser = open_port(). On success it prints "[reconnected]", appends "reconnected at &lt;unix time .3f>" to rec.notes and breaks. On OSError it keeps waiting.
3. `continue` returns to the outer while, which exits if running became False.
open_port sends b"H", so the device re-sends its header. The decoder starts a new segment if device_id/boot_id/stream_id changed or the device clock is far from the running stream.

Shutdown:
- KeyboardInterrupt is swallowed.
- finally: ser.close() (any exception ignored), ctl.close(), summary = rec.close(), print_summary(summary, out, rec.writer.rows_written).
- Returns 0, including after a LayoutChanged stop.

**Notes.** The initial ser = open_port() runs before the try/finally. If the port cannot be opened at start, the exception propagates out of main; rec is never closed, and the already-created .bin/.labels.jsonl are left empty. If the user quits during a reconnect wait, ser is the old closed port object; the guarded close in finally covers that.

#### `run_live.open_port`
*nested function* · [`host/logger.py:140`](../host/logger.py#L140)

```python
open_port() -> serial.Serial
```

**Purpose.** Opens the serial port for reading and asks the device for its session header. It is used at start and on every reconnect attempt.

**How it works.**

1. ser = serial.Serial(args.port, 115200, timeout=0.02). The 20 ms read timeout bounds how long one loop iteration blocks.
2. If the object has set_buffer_size, calls ser.set_buffer_size(rx_size=1 &lt;&lt; 16) to get a 64 KiB driver receive buffer. The comment says this is Windows only.
3. ser.write(P.CMD_SEND_HEADER), i.e. b"H".
4. Returns ser.

**Notes.** Open failures raise serial.SerialException (an OSError), which the reconnect loop catches.

#### `run_live.handle_inputs`
*nested function* · [`host/logger.py:147`](../host/logger.py#L147)

```python
handle_inputs(keys) -> bool
```

**Purpose.** Processes keyboard keys and viewer label commands. Returns False when the user asked to quit.

**How it works.**

1. go = True.
2. For each character k in keys.poll() (non-blocking; KeyReader returns [] when stdin is not a TTY):
   - k.isdigit(): rec.set_label(int(k)), then prints "[label -> k name]".
   - k in ("q", "Q"): go = False. Remaining keys are still processed.
   - k == "?": prints help_text().
   - Anything else is ignored.
3. If ctl exists, for each datagram in ctl.poll(): code = parse_label_command(msg), which accepts exactly b"LABEL &lt;digits>". If code is not None and code is in LABELS (0-9), calls rec.set_label(code) and prints "[label (viewer) -> code name]".
4. Returns go.

**Notes.** Keyboard digits are not checked against LABELS; every ASCII digit 0-9 is in LABELS anyway. str.isdigit() is also True for characters such as '²', for which int() raises ValueError. Typing such a character would therefore end the recording with an exception, which run_live's finally block still cleans up.

#### `run_replay`
*function* · [`host/logger.py:251`](../host/logger.py#L251)

```python
run_replay(args) -> int
```

**Purpose.** Rebuilds a session .parquet offline from a raw .bin capture. It reapplies the recorded labels at the same frames as live recording and writes the labels back to the output's .labels.jsonl.

**How it works.**

1. src = Path(args.replay). out = Path(args.out), or src.with_name(src.stem + "_replay.parquet") when --out is not given. If out exists and not args.force, prints to stderr and returns 2.
2. labels_path = Path(args.labels), or sidecar_paths(src.with_suffix(".parquet"))[1] = &lt;src stem>.labels.jsonl next to the .bin.
3. events = read_label_events(labels_path). A missing file gives []. Undecodable lines, such as a torn last line after a crash, are skipped.
4. Label write-back: rec = Recorder(out, raw=False, extra_meta={"source": "replay", "replay_of": str(src)}, udp_port=None, label_events=events). SessionWriter opens out's .labels.jsonl in "w" mode and immediately writes every event into it. Code comment: when --out has the source's stem, this rewrites the very file just read, so recovering a session never loses its labels. raw=False means no .bin is written, so the source .bin is never truncated either.
5. rec.dec.schedule_labels(events). If every event carries segment and seg_seq, events are sorted and applied by (segment, offset within segment). Otherwise they are sorted and applied by seq (older files).
6. data = src.read_bytes() reads the whole capture into memory. It is fed in 64 KiB chunks (1 &lt;&lt; 16) with rec.feed(chunk, host_time=0.0).
7. LayoutChanged handling: the exception stops the loop and prints "[...] -- replay stopped there; the output holds the data before the change." to stderr. Execution continues to close(). If the first header and the changed header were in the same chunk, close() hands the header to the writer that feed() skipped. Rows decoded before the exception are drained there.
8. summary = rec.close(). Prints "replayed N bytes with M label events from &lt;labels_path>", then print_summary(summary, out, rec.writer.rows_written).
9. Returns 0, also after a layout change.

**Notes.** host_time = 0.0 for every chunk means a reconnect gap inside the capture cannot be bridged with host time; the decoder places a new segment's first frame one tick after the previous one. That is why label placement uses (segment, seg_seq) rather than absolute seq. The output .labels.jsonl always gets all events, while Parquet kman.labels gets only those the decoder reached (none past the end of the data or past a layout change). No UDP broadcast happens during replay.

#### `main (logger.py)`
*function* · [`host/logger.py:276`](../host/logger.py#L276)

```python
main(argv=None) -> int
```

**Purpose.** CLI entry point: parses arguments and dispatches to port listing, replay or live recording.

**How it works.**

1. argparse options:
   - --port
   - --out
   - --replay
   - --labels
   - --duration (float, default 0 = until q)
   - --subject (default "")
   - --notes (default "")
   - --udp-port (int, default DEFAULT_PORT = 51515; the control port is +1)
   - --no-udp
   - --no-raw
   - --force
   - --list-ports
   argv=None parses sys.argv[1:].
2. Precedence:
   a. --list-ports: imports serial.tools.list_ports, prints "{device:&lt;20} {description}  [{hwid}]" for each port and returns 0.
   b. --replay: returns run_replay(args).
   c. No --port: ap.error(...), which exits with status 2 via SystemExit.
   d. Otherwise returns run_live(args).
The `if __name__ == "__main__"` block calls sys.exit(main()).

**Notes.** float_format "%.6g" keeps 6 significant digits in every float column. In the default mode, host_time (unix seconds, about 1.76e9) is therefore written as e.g. 1.76046e+09, which rounds it to the nearest 10,000 s (about 2.8 hours). That makes the column practically useless in the CSV. Sessions written by replay or by the simulator have host_time 0.0 anyway. t_s keeps 0.01 s resolution only up to 9999.99 s; after that it is rounded to 0.1 s. Integer columns (t_us and seq, plus label, seq and segment in --units mode) are unaffected.

### `host/sim_device.py`

#### `make_session`
*function* · [`host/sim_device.py:31`](../host/sim_device.py#L31)

```python
make_session(args) -> sim.SimSession
```

**Purpose.** Builds the synthetic session (signals, labels, header) from the command-line options.

**How it works.**

1. cfg = sim.SimConfig(seed=args.seed, abdomen=args.abdomen, neck_imu=args.neck_imu, fs=args.rate).
2. If args.wrap, sets cfg.t0_us = 2**32 - 5_000_000, so the 32-bit device timestamp wraps 5 s into the session.
3. scenario = [(lab, d \* args.time_scale) for lab, d in sim.DEFAULT_SCENARIO]. DEFAULT_SCENARIO is 15 (label, seconds) steps totalling 440 s at scale 1.
4. Returns sim.generate(cfg, scenario). The whole session is generated in memory up front.

**Notes.** Other SimConfig fields (seq0 = 100, noise levels, respiration rate) keep their defaults.

#### `write_session`
*function* · [`host/sim_device.py:39`](../host/sim_device.py#L39)

```python
write_session(args, s: sim.SimSession) -> int
```

**Purpose.** Writes a complete synthetic session offline (.parquet, .bin and .labels.jsonl), using the same decoder and writer path as a real recording.

**How it works.**

1. out = Path(args.write_session). dec = StreamDecoder(). dec.schedule_labels(s.label_events()): one event per label change, carrying seq = seq0 + i and t_us but no segment/seg_seq, so events are placed by seq.
2. w = SessionWriter(out, raw=True, extra_meta={"source": "simulator", "seed": args.seed}). Every label event is written to .labels.jsonl.
3. For each (_, fr) in s.stream(drop_prob=args.drop_prob, corrupt_prob=args.corrupt_prob, seed=args.seed): the header frame first, then data frames with simulated drops/one-byte corruption, plus a status frame every 1 s.
   a. w.write_raw(fr).
   b. dec.feed(fr, host_time=0.0).
   c. If dec.new_header, w.set_header(dec.header) and clear the flag.
   d. If dec.pending_rows() >= 2000, w.write_batch(dec.drain()).
4. Drains the remainder and writes it, then calls w.close(dec.headers, dec.label_events_dicts(), dec.summary(), notes="synthetic session (simulator)").
5. Prints rows written, scenario length s.n / s.cfg.fs seconds, frames lost (dec.stats.dropped) and CRC errors. Returns 0.

**Notes.** An existing output is overwritten without a check, together with its .bin and .labels.jsonl. Labels are the simulator's ground truth. Each event's seq (seq0 + i) is the first tick the label applies to, which is the same convention as a key press's seq = last_seq64 + 1. The decoder therefore applies it at exactly sample i. With no drops, the Parquet label column equals s.labels sample for sample, with no one-frame lag. If the boundary frame was dropped, the label starts at the next frame received. The .bin can be fed to `logger.py --replay`.

#### `run_pty`
*function* · [`host/sim_device.py:63`](../host/sim_device.py#L63)

```python
run_pty(args, s: sim.SimSession) -> int
```

**Purpose.** Serves the synthetic stream in real time on a pseudo-terminal that behaves like the device's USB serial port, including answering the b"H" header request.

**How it works.**

1. If os.name == "nt", prints that --pty needs Linux/macOS (use com0com with --port on Windows) and returns 2.
2. master, slave = os.openpty(). tty.setraw(slave) disables echo and line processing so binary frames pass verbatim. os.set_blocking(master, False). Prints os.ttyname(slave) (e.g. /dev/pts/N) and the scenario length.
3. gen = s.stream(drop_prob, corrupt_prob, seed). write_all(next(gen)[1]) sends the initial header (index -1) immediately.
4. t0 = time.monotonic(), last_label = None.
5. For each (i, fr) in gen:
   a. due = t0 + (i + 1) / s.cfg.fs. Sleeps due - monotonic() if positive.
   b. cmd = os.read(master, 64). These are bytes the client wrote to the slave; BlockingIOError/OSError gives b"".
   c. If P.CMD_SEND_HEADER (b"H") is in cmd, sends write_all(s.header_frame(i)). That header's t_us_at_header is the device time at sample i, as the firmware would build it.
   d. write_all(fr).
   e. lab = int(s.labels[max(i, 0)]). If it differs from last_label, prints "t=... s  true activity: lab name".
6. KeyboardInterrupt ends the loop. Returns 0, also when the scenario runs out.

**Notes.** Pacing uses absolute times from t0 and the sample index. Dropped frames therefore leave a correctly sized time gap, status frames (which share i with the preceding data frame) go out without extra delay, and a late writer catches up by sending frames back-to-back. With no reader on the slave, the pty buffer fills and write_all retries every 1 ms, stalling the stream until someone reads. The fds are not closed explicitly.

#### `run_pty.write_all`
*nested function* · [`host/sim_device.py:74`](../host/sim_device.py#L74)

```python
write_all(data: bytes) -> None
```

**Purpose.** Writes a whole buffer to the non-blocking pty master, retrying on partial writes and a full buffer.

**How it works.** view = memoryview(data). While view is non-empty: n = os.write(master, view); view = view[n:]. On BlockingIOError it sleeps 0.001 s and retries.

**Notes.** Other OSErrors (e.g. EIO) propagate out of run_pty.

#### `main (sim_device.py)`
*function* · [`host/sim_device.py:109`](../host/sim_device.py#L109)

```python
main(argv=None) -> int
```

**Purpose.** CLI entry point: builds the simulated session and runs it as a live pty device or writes it to files.

**How it works.**

1. argparse with a required, mutually exclusive mode: --pty, or --write-session OUT.parquet.
2. Options:
   - --abdomen
   - --neck-imu
   - --rate (int, default 100 Hz)
   - --seed (int, default 1)
   - --time-scale (float, default 1.0)
   - --drop-prob (default 0.0)
   - --corrupt-prob (default 0.0)
   - --wrap
3. s = make_session(args).
4. Returns run_pty(args, s) if args.pty, else write_session(args, s). The `__main__` block calls sys.exit(main()).

**Notes.** Tests call main(["--write-session", path, "--time-scale", "0.1"]) to produce fixture sessions quickly.

### `host/to_csv.py`

#### `main (to_csv.py)`
*function* · [`host/to_csv.py:20`](../host/to_csv.py#L20)

```python
main(argv=None) -> int
```

**Purpose.** Exports a session .parquet to CSV, as raw counts, as physical units or as the label-event list, for quick inspection in a spreadsheet.

**How it works.**

1. Arguments: session (path), --out, --units, --labels-only.
2. s = load_session(args.session). This reads the Parquet table into s.df and the metadata into s.header, s.labels and s.label_names (from kman.label_names, falling back to LABELS).
3. --labels-only: out = --out, or &lt;stem>_labels.csv next to the session. Writes pd.DataFrame(s.labels).to_csv(out, index=False); the columns are the event dict keys (seq, t_us, host_time, label, name, segment, seg_seq where present). Prints the count and returns 0.
4. Otherwise out = --out, or the session path with suffix .csv.
   - --units: df = s.physical(). The columns are t_s, each channel as float64 count \* ch.scale (NaN where the channel's valid bit (1 &lt;&lt; ch.bit) is clear), label, seq and segment. This drops t_us, valid and host_time.
   - Default: df = s.df.copy() (t_us, seq, segment, valid, raw int16 channels, label, host_time), with a column t_s = (t_us - t_us[0]) / 1e6 inserted at position 0.
5. Inserts label_name right after the label column: df["label"].map(s.label_names). Codes not in the table become NaN (empty cells).
6. df.to_csv(out, index=False, float_format="%.6g"), then prints the row and column counts. Returns 0.

**Notes.** float_format "%.6g" keeps 6 significant digits for every float column. In the default mode host_time (unix seconds, ~1.76e9) is therefore written as e.g. 1.76046e+09, losing sub-1000-second resolution. t_s keeps 0.01 s resolution only up to 9999.99 s; longer sessions are rounded to 0.1 s. Integer columns (t_us, seq) are unaffected.

## 13. Host: synthetic physiology simulator

- **`host/kman_host/simulator.py`**: Synthetic KMAN device. `generate()` builds a whole session of physiologically plausible chest-IMU, optional abdomen-IMU, optional neck-IMU and neck-flex signals from a labelled scenario. The signals are quantised to the firmware's int16 LSB scales, and a session header is built in the firmware's JSON format. `SimSession` serialises the result as byte-exact HEADER/DATA/STATUS frames, optionally with injected drops and corruption. The logger, live viewer, stream decoder and analysis can therefore run without hardware, and the analysis tests get known ground truth (`truth`).

**How it connects.** Inbound: host/sim_device.py calls `generate`. Its `make_session` builds a `SimConfig` from the CLI (seed, abdomen, neck_imu, rate, `--wrap`) and passes `DEFAULT_SCENARIO` scaled by `--time-scale`. Its `write_session` passes `SimSession.label_events()` to `StreamDecoder.schedule_labels` and `SessionWriter.write_label_event`, then iterates `SimSession.stream(drop_prob, corrupt_prob, seed)` into `SessionWriter.write_raw` and `StreamDecoder.feed`; that is how data/sample_synthetic.parquet is produced. Its `run_pty` writes the stream to a pseudo-terminal in real time and answers `P.CMD_SEND_HEADER` with `SimSession.header_frame(i)`. host/live_view.py `sim_source` calls `generate(SimConfig(abdomen=...))`, iterates `stream()`, inserts a `P.TYPE_LABEL` frame wherever `labels` changes, and wraps the frames in a `PacedFrameSource`. Tests use the simulator too. host/tests/test_stream.py and test_v1_fixes.py feed `header_frame`, `data_frame` and `stream` output into `StreamDecoder`, the logger and the recorder; test_stream.py also replays `label_events()` through `schedule_labels`. Only test_v1_fixes.py calls `_smooth` directly, in test_simulator_noise_does_not_swing_at_the_ends. analysis/tests/test_analysis.py runs `generate`, then `label_events`/`schedule_labels`, `stream`, `StreamDecoder`, `SessionWriter`, `load_session` and `kman_analysis.analyze`, and compares the results with `truth['resp']`, `truth['swallow_times']` and `truth['head_times']`. Outbound: the module calls `kman_host.protocol` (`encode_frame`, `TYPE_HEADER`/`TYPE_DATA`/`TYPE_STATUS`, `DATA_PREFIX`, `DeviceStatus`, `encode_status`, `STATUS_FLAG_HFXO`) and `kman_host.labels.label_name`. `build_header` copies by hand the header that firmware/src/main.cpp `buildHeaderFrame` emits. The chest gyro/gravity sign convention is chosen to match `kman_analysis.breathing.gravity_complementary`, which predicts dg/dt = -ω×g.

### `host/kman_host/simulator.py`

#### `ACCEL_SCALE / GYRO_SCALE / ADC_FULL_SCALE`
*module constants* · [`host/kman_host/simulator.py:22`](../host/kman_host/simulator.py#L22)

```python
ACCEL_SCALE = 61e-6; GYRO_SCALE = 8.75e-3; ADC_FULL_SCALE = 16383
```

**Purpose.** LSB scale factors used to quantise simulated physical values into the int16 counts that the firmware sends. The same values go into the header's channel `scale` fields.

**How it works.** ACCEL_SCALE is 61e-6 g/LSB, the LSM6DSOX sensitivity at ±2 g. GYRO_SCALE is 8.75e-3 dps/LSB, the sensitivity at 250 dps. ADC_FULL_SCALE is 16383, the top code of a 14-bit ADC. It is the upper clip for `flex_raw` and is reported as `full_scale` in the neck_flex sensor entry.

**Notes.** At 61e-6 g/LSB, 1 g is about 16393 LSB.

#### `DEFAULT_SCENARIO`
*module constant* · [`host/kman_host/simulator.py:26`](../host/kman_host/simulator.py#L26)

```python
DEFAULT_SCENARIO: list[tuple[int, float]]
```

**Purpose.** The default protocol of activity segments, given as (label code, duration in s), that `generate()` turns into the per-sample label vector.

**How it works.** There are 15 segments totalling 440 s (44 000 samples at 100 Hz): rest 60, breath-hold (8) 20, rest 30, dry swallow (4) 40, rest 20, head movement (5) 40, rest 20, drinking (3) 40, talking (2) 30, walking (6) 30, rest 20, cough (7) 15, rest 20, simulated obstruction (9) 25, rest 30. Label names come from `labels.LABELS`.

**Notes.** sim_device.make_session scales every duration by `--time-scale`.

#### `SimConfig`
*dataclass* · [`host/kman_host/simulator.py:33`](../host/kman_host/simulator.py#L33)

```python
@dataclass class SimConfig
```

**Purpose.** All tunable parameters of the simulated device and the physiology model.

**How it works.**

Fields and defaults:
- `fs` = 100: sample rate in Hz.
- `seed` = 1: seed for the single numpy Generator.
- `abdomen` = False: adds the abdomen IMU slot and its `abd_*` channels.
- `neck_imu` = False: adds the neck IMU slot and its `neck_*` channels.
- `resp_rate_bpm` = 14.0: base breathing rate.
- `resp_tilt_deg_pp` = 1.0: chest x-tilt peak-to-peak per breath at amplitude 1.
- `flex_baseline` = 8200.0: flex ADC counts.
- `flex_noise` = 6.0: counts rms.
- `swallow_counts` = 70.0: nominal flex bump peak of a swallow.
- `head_counts` = (400.0, 1200.0): uniform range of head-movement flex amplitude.
- `accel_noise_g` = 0.35e-3: per-axis white noise in g.
- `gyro_noise_dps` = 0.03: per-axis white noise in dps.
- `gyro_bias_dps` = (0.4, -0.25, 0.15): chest gyro bias.
- `t0_us` = 1_000_000: device timestamp of sample 0.
- `seq0` = 100: sequence number of sample 0.

**Notes.** A code comment says to set `t0_us` near 2**32 to exercise the timestamp wrap; sim_device `--wrap` uses 2**32 - 5_000_000. The dataclass is mutable, and sim_device.make_session assigns `cfg.t0_us` after construction.

#### `SimSession`
*dataclass* · [`host/kman_host/simulator.py:52`](../host/kman_host/simulator.py#L52)

```python
@dataclass class SimSession
```

**Purpose.** The result of `generate()`: the complete simulated session plus methods that serialise it as device frames.

**How it works.**

Fields:
- `cfg`: the SimConfig used.
- `labels`: an int8 ndarray holding one label code per sample.
- `raw`: dict mapping channel name to an int16 ndarray of length n, ordered exactly as `header['channels']`.
- `header`: the session header dict from `build_header`.
- `truth`: a dict of ground-truth arrays, empty by default via `field(default_factory=dict)`. The keys `generate` fills are `resp`, `swallow_times`, `head_times`, `rate_bpm`, `amp` and, with abdomen, `resp_abd`.

**Notes.** Sample index i maps to device time `t0_us + i*period_us` and sequence number `seq0 + i`.

#### `SimSession.n`
*property* · [`host/kman_host/simulator.py:60`](../host/kman_host/simulator.py#L60)

```python
@property def n(self) -> int
```

**Purpose.** Number of samples in the session.

**How it works.** Returns `len(self.labels)`.

#### `SimSession.label_events`
*method* · [`host/kman_host/simulator.py:63`](../host/kman_host/simulator.py#L63)

```python
def label_events(self) -> list[dict]
```

**Purpose.** Produces label-change events as if an operator had pressed the label key exactly at each scenario segment boundary. Replay (StreamDecoder.schedule_labels) and SessionWriter consume these events.

**How it works.**

1. Change points are `np.flatnonzero(np.r_[True, labels[1:] != labels[:-1]])`. Index 0 is always included.
2. For each change point i with label `lab` that differs from the previous emitted label, append `{"seq": seq0 + i, "t_us": t0_us + i*period_us, "host_time": 0.0, "label": lab, "name": label_name(lab)}`.
3. Return the list. For DEFAULT_SCENARIO it has 15 events.

**Notes.** `seq` and `t_us` are not wrapped to 16 or 32 bits, unlike in `data_frame`. The events carry no `segment`/`seg_seq`, so StreamDecoder.schedule_labels places them by `seq`. The `lab != prev` check is redundant because consecutive change points always differ.

#### `SimSession.period_us`
*property* · [`host/kman_host/simulator.py:76`](../host/kman_host/simulator.py#L76)

```python
@property def period_us(self) -> int
```

**Purpose.** Sample period in microseconds.

**How it works.** Returns `1_000_000 // cfg.fs` (integer division: 10 000 at 100 Hz).

**Notes.** For an `fs` that does not divide 1e6, the period is truncated, which matches the header's `period_us`.

#### `SimSession.header_frame`
*method* · [`host/kman_host/simulator.py:79`](../host/kman_host/simulator.py#L79)

```python
def header_frame(self, i: int = -1) -> bytes
```

**Purpose.** Encodes the session header as the firmware would send it at sample index i. Its timestamp is the device time at that moment, for example when answering a host 'H' request.

**How it works.**

1. Builds a shallow copy `dict(self.header, t_us_at_header=(t0_us + max(i, 0)*period_us) & 0xFFFFFFFF)`. The default i = -1 gives `t0_us`.
2. JSON-encodes the copy with `json.dumps(...).encode()`.
3. Returns `P.encode_frame(P.TYPE_HEADER, body)`, which is sync AA55, u16le length, type 0x02, body and CRC-16/CCITT-FALSE.

**Notes.** `self.header` itself is not modified. With chest + abdomen + neck the frame is about 3.5 kB, below protocol MAX_LEN 4096.

#### `SimSession.data_frame`
*method* · [`host/kman_host/simulator.py:84`](../host/kman_host/simulator.py#L84)

```python
def data_frame(self, i: int) -> bytes
```

**Purpose.** Encodes sample i as a DATA frame in the exact firmware layout.

**How it works.**

1. `t32 = (t0_us + i*period_us) & 0xFFFFFFFF` and `seq16 = (seq0 + i) & 0xFFFF`.
2. `body = P.DATA_PREFIX.pack(t32, seq16, header['present_mask'])`. DATA_PREFIX is struct `<IHB`. The valid byte is the full present mask, so every present sensor is valid in every sample.
3. Appends one little-endian int16 per channel, `raw[name][i]`, in `header['channels']` order (`dtype='<i2'`).
4. Returns `P.encode_frame(P.TYPE_DATA, body)`. The frame is 28 bytes with 7 channels, 40 with 13 and 52 with 19.

#### `SimSession.status_frame`
*method* · [`host/kman_host/simulator.py:91`](../host/kman_host/simulator.py#L91)

```python
def status_frame(self, i: int, sent: int, drops: int = 0) -> bytes
```

**Purpose.** Encodes a device STATUS frame at sample index i with plausible fixed health counters.

**How it works.**

Builds `P.DeviceStatus` with these fields:
- `t_us` = (t0_us + i\*period_us) & 0xFFFFFFFF
- `ticks` = seq0 + i
- `frames_sent` = sent
- `overruns` = 0
- `tx_drops` = drops
- `read_errors` = (0, 0, 0, 0)
- `max_latency_us` = 620
- `tx_high_water` = 180
- `flags` = P.STATUS_FLAG_HFXO (0x01, HFXO running, no sensor error)

It then returns `P.encode_frame(P.TYPE_STATUS, P.encode_status(st))`.

**Notes.** `stream()` never passes `drops`, so `tx_drops` is always 0 even when frames are dropped.

#### `SimSession.stream`
*generator method* · [`host/kman_host/simulator.py:99`](../host/kman_host/simulator.py#L99)

```python
def stream(self, drop_prob: float = 0.0, corrupt_prob: float = 0.0, seed: int = 0, status_every_s: float = 1.0)
```

**Purpose.** Yields the whole session as the device would emit it: the header first, then DATA frames in order with a STATUS frame once per `status_every_s`. It can inject frame loss and byte corruption to exercise the decoder.

**How it works.**

1. `rng = np.random.default_rng(seed)`. This generator is independent of the generation RNG.
2. Yields `(-1, header_frame())`.
3. `every = int(status_every_s*fs)` (100 at the defaults) and `sent = 0`.
4. For each i in range(n):
   a. If `drop_prob` is non-zero and `rng.random() < drop_prob`, skip the sample (`continue`).
   b. Otherwise build `fr = data_frame(i)`.
   c. If `corrupt_prob` is non-zero and `rng.random() < corrupt_prob`, XOR one byte at a random index in [5, len) with 0xFF. Only the body or CRC is corrupted; sync, length and type (bytes 0-4) stay intact.
   d. `sent += 1`, then yield `(i, fr)`.
   e. If `every` is non-zero and `i % every == every - 1`, yield `(i, status_frame(i, sent))`.

**Notes.** A dropped sample also suppresses the STATUS frame that would have followed it. `sent` counts corrupted frames as sent. RNG draws happen only for non-zero probabilities, so the drop pattern depends on whether `corrupt_prob` is also set. At the defaults the 440 s scenario yields 1 + 44 000 + 440 frames. The stream sends the header only once; answering 'H' with `header_frame(i)` is left to the caller (sim_device.run_pty).

#### `_smooth`
*function* · [`host/kman_host/simulator.py:122`](../host/kman_host/simulator.py#L122)

```python
def _smooth(x: np.ndarray, n: int) -> np.ndarray
```

**Purpose.** Same-length moving-average (boxcar) filter. It smooths label envelopes and turns white noise into slow random wander.

**How it works.**

1. If `n <= 1`, return `x` unchanged (the same object).
2. The kernel is `ones(n)/n`.
3. Pad n//2 samples on the left and n-1-n//2 on the right. Padding uses `mode='reflect'` if `len(x) > n`, otherwise `'edge'`.
4. `np.convolve(pad, k, mode='valid')` returns exactly len(x) samples. Output k is the mean of x[k-n//2 .. k+n-1-n//2].

**Notes.** Code comment: reflect is used instead of edge because edge padding would repeat one noise sample n/2 times, making smoothed noise swing far more near the ends than in the interior. test_v1_fixes.test_simulator_noise_does_not_swing_at_the_ends checks this. For unit white noise the interior output std is about 1/sqrt(n), so all smoothed-noise amplitudes in `generate` depend on `fs`.

#### `_rot_x`
*function* · [`host/kman_host/simulator.py:132`](../host/kman_host/simulator.py#L132)

```python
def _rot_x(v: np.ndarray, a: np.ndarray) -> np.ndarray
```

**Purpose.** Rotates each row of an (N,3) vector array about the x axis by a per-sample angle.

**How it works.** With c = cos(a) and s = sin(a) (a in rad), it returns the columns [x, c\*y - s\*z, s\*y + c\*z]. This is a right-handed active rotation by +a.

#### `_rot_z`
*function* · [`host/kman_host/simulator.py:137`](../host/kman_host/simulator.py#L137)

```python
def _rot_z(v: np.ndarray, a: np.ndarray) -> np.ndarray
```

**Purpose.** Rotates each row of an (N,3) vector array about the z axis by a per-sample angle.

**How it works.** With c = cos(a) and s = sin(a), it returns the columns [c\*x - s\*y, s\*x + c\*y, z]. This is a right-handed active rotation by +a.

#### `_bandnoise`
*function* · [`host/kman_host/simulator.py:142`](../host/kman_host/simulator.py#L142)

```python
def _bandnoise(rng, n, fs, lo, hi, rms)
```

**Purpose.** Generates band-limited Gaussian noise with a given rms. It models speech vibration and talking-related flex activity.

**How it works.**

1. `x = rng.standard_normal(n)`.
2. `X = rfft(x)` with bin frequencies `rfftfreq(n, 1/fs)`.
3. Zero every bin with f &lt; lo or f > hi; the band edges are kept.
4. `y = irfft(X, n)`.
5. Return `y * (rms / y.std())`, or `y` unscaled if its std is 0.

**Notes.** The rms is normalised over the whole record. When the result is multiplied by an activity envelope, the rms inside the active segment is `rms`. Each call consumes n draws from the shared RNG.

#### `_bump`
*function* · [`host/kman_host/simulator.py:152`](../host/kman_host/simulator.py#L152)

```python
def _bump(n_total: int, start: int, dur: int, amp: float, shape: str = "cos") -> tuple[slice, np.ndarray]
```

**Purpose.** Builds a smooth transient (a swallow, cough or head movement) to add into a signal array at a given sample position.

**How it works.**

1. `end = min(n_total, start + dur)` truncates the bump at the end of the record.
2. `t = arange(end - start) / max(dur, 1)` is normalised time in [0, 1).
3. With `shape == 'plateau'`, a trapezoid `clip(min(t/0.25, (1-t)/0.25), 0, 1)` ramps over the first and last 25% of `dur`. It is then passed through `0.5 - 0.5*cos(pi*y)` to get raised-cosine edges and a flat top.
4. Any other shape gives a full Hann bump `0.5 - 0.5*cos(2*pi*t)`, which peaks at 1 at mid-duration.
5. Returns `(slice(start, end), amp*y)`.

**Notes.** The last sample is at t = (dur-1)/dur, so the bump does not quite return to exactly 0. Callers always pass start ≥ 0.

#### `_resp_shape`
*function* · [`host/kman_host/simulator.py:163`](../host/kman_host/simulator.py#L163)

```python
def _resp_shape(phase_frac: np.ndarray, ie_split: float = 0.38) -> np.ndarray
```

**Purpose.** Breath waveform with one breath per unit of phase: a fast inspiratory rise and a slower expiratory fall, range -0.5..0.5.

**How it works.**

1. `p = phase_frac % 1.0`.
2. Inspiration is `p < ie_split` (0.38): `0.5 - 0.5*cos(pi*p/0.38)`, rising 0 to 1.
3. Expiration: `0.5 + 0.5*cos(pi*(p-0.38)/0.62)`, falling 1 to 0.
4. Return the result minus 0.5.

**Notes.** I:E is 0.38:0.62 (about 1:1.63). This asymmetry is the property the analysis relies on, per the module docstring.

#### `generate`
*function* · [`host/kman_host/simulator.py:171`](../host/kman_host/simulator.py#L171)

```python
def generate(cfg: SimConfig | None = None, scenario: list[tuple[int, float]] | None = None) -> SimSession
```

**Purpose.** Builds a complete simulated session from a labelled scenario: chest IMU, flex, and optionally abdomen and neck IMU, as int16 counts, plus header and ground truth. Every consumer that needs a session (sim_device, live_view, the stream, logger and analysis tests) gets it through this function. The only direct use of a helper is one test that calls `_smooth`.

**How it works.**

Setup: `cfg = cfg or SimConfig()` and `scenario = scenario or DEFAULT_SCENARIO`; an empty list also selects the default. `rng = default_rng(cfg.seed)` is the only generator, so the call order fixes the output. `labels = concat(np.full(int(d*fs), lab, int8))`, `n = len(labels)`, `t = arange(n)/fs` in s. `[L]` below means `is_(L)`, the 0/1 label indicator. Amplitudes quoted as rms of smoothed noise are for fs = 100.

1. Respiration.
   - Rate: `rate = resp_rate_bpm*(1 + 0.08*8*_smooth(N(0,1), 10*fs))` gives slow wander of about 2% rms (about 0.28 bpm at 14 bpm). It is then multiplied by `(1 + 0.5[6])(1 + 0.45[9])(1 + 0.15[2])`: walking +50%, obstruction +45%, talking +15%. These are unsmoothed steps.
   - Phase: `phase = cumsum(rate/60)/fs`, in breath cycles.
   - Amplitude: `amp = 1 + 0.3[6] + 0.6[9] - 0.25[2] - 1.0[8]`, i.e. walking 1.3, obstruction 1.6, talking 0.75, breath-hold 0.
   - Per-breath jitter: `per_breath = 1 + 0.12*N(0,1)`, drawn `max(floor(phase)) + 2` times, and `amp *= per_breath[floor(phase)]`.
   - Talking: `talk_mod = 1 + 0.5*4*_smooth(N(0,1), 2*fs)` (about 14% rms). Where label == 2, `amp *= clip(talk_mod, 0.3, 1.6)`.
   - Finally `amp = clip(_smooth(clip(amp, 0), fs), 0)`, a 1 s moving average that softens label steps.

2. Swallows and swallow apnea.
   - `apnea = ones(n)`. For (label 4, gap 8 s) and then (label 3, gap 10 s), the label's indices are split into contiguous segments. In each segment, `k` starts at seg start + 3 s and loops while `k < seg end - 3 s`.
   - Swallow count: 1 for dry swallow, `rng.integers(1, 3)` (1 or 2) for drinking.
   - Drinking first adds a cup-to-lips plateau bump: start k - 1 s, length 3.5 s, -180 counts into `flex_delta`. The same shape, scaled to -15°, goes into `head_pitch`.
   - Each swallow j: `ks = k + int(j*1.6*fs)`. A cos bump of length U(0.6, 1.2) s and peak `swallow_counts*U(0.8, 1.25)` (56-87.5 counts at default) is added to `flex_delta`, and `ks/fs` is appended to `swallow_times`. Then `apnea[ks - 0.3 s : ks + 0.9 s] = 0.15`.
   - `k += gap*U(0.8, 1.2)`: 6.4-9.6 s for dry swallows, 8-12 s for drinking.
   - After the loops, `amp *= _smooth(apnea, int(0.3*fs))`.

3. Head movements (label 5). Per segment, `k` starts at seg start + 2 s and loops while `k < seg end - 4 s`:
   - Duration U(1.5, 3.0) s; amplitude `a = U(head_counts)` (400-1200 counts) with random sign.
   - A plateau bump of `a` goes into `flex_delta`, and `head_pitch += 35°*y/head_counts[1]` (35° at 1200 counts, signed).
   - `chest_jolt[:,1] += 0.004*y/|a|`: 0.004 g shoulder coupling on chest y, signed.
   - `k/fs` goes to `head_times`, and `k += U(3.5, 5.0)` s.

4. Coughs (label 7). Per segment, `k` starts at seg start + 1.5 s and loops while `k < seg end - 1 s`. Over a 0.3 s window with `env = exp(-tt/0.06)`:
   - chest_jolt z += 0.35 g \* env \* sin(2π\*14\*tt)
   - chest_jolt y += 0.12 g \* env
   - gyro_jolt x += 25 dps \* env \* sin(2π\*9\*tt)
   - a cos flex bump of 0.35 s and 250 counts
   - then `k += U(2.5, 4.5)` s. Cough times are not stored in `truth`.

5. Chest tilt.
   - `resp = amp*_resp_shape(phase)` and `tilt_amp = deg2rad(resp_tilt_deg_pp)`.
   - `th_x = tilt_amp*resp`, so the tilt is 1° peak-to-peak at amp 1. `th_z = 0.25*tilt_amp*resp`.
   - `th_x` also gets a cardiac ballistic term `0.012°*sin(2π*1.15*t)` (69 bpm, per the comment 'visible during breath-hold') and postural sway `0.3°*10*_smooth(N(0,1), 20*fs)` (about 0.067° rms).

6. Walking. `walk = _smooth([6], fs)`.
   - `acc_walk` (g): x 0.10 sin(2π0.9t); y 0.25 sin(2π1.8t) + 0.08 sin(2π3.6t + 0.4); z 0.06 sin(2π1.8t + 1.0).
   - `gyr_walk` (dps): x 5.0 sin(2π1.8t); y 3.0 sin(2π0.9t + 0.3); z 8.0 sin(2π0.9t).
   - Both are multiplied by `walk`. This gives a 0.9 Hz stride, 1.8 Hz step and 3.6 Hz harmonic, i.e. energy above 1.5 Hz.

7. Talking. `talk = _smooth([2], fs)`.
   - `acc_talk` = three independent `_bandnoise(5-15 Hz, 0.004 g rms)` times `talk`.
   - `flex_delta += _bandnoise(0.8-4.0 Hz, 12 counts rms)*talk`.

8. Chest IMU sign convention.
   - `up = [0, 0.97, 0.243]`, normalised: gravity mostly on +y, about 14° toward +z.
   - `g = _rot_z(_rot_x(up, -th_x), -th_z)`. Gravity is rotated by -tilt, which means the body rotates by +tilt.
   - `acc = g + acc_walk + acc_talk + chest_jolt + N*accel_noise_g`.
   - `gyr = [gradient(th_x)*fs, 0, gradient(th_z)*fs]*180/π + gyr_walk + gyro_jolt + gyro_bias_dps + N*gyro_noise_dps`.
   - A positive gyro rate therefore equals d(+tilt)/dt, consistent with the analysis filter's prediction dg/dt = -ω×g (breathing.gravity_complementary).

9. Quantisation (all IMUs).
   - Accelerometer channels: `clip(round(acc/61e-6), -32768, 32767).astype(int16)`.
   - Gyro channels: `clip(round(gyr/8.75e-3), -32768, 32767).astype(int16)`.
   - Chest channels are written to `chest_a{x,y,z}` and `chest_g{x,y,z}`.

10. Truth. `truth = {resp, swallow_times (s, bump start), head_times (s, bump start), rate_bpm (including label multipliers), amp (after smoothing and apnea)}`.

11. Abdomen paradox model (`cfg.abdomen`).
   - `paradox = _smooth([9], fs)`.
   - `resp_ab = amp*_resp_shape(phase + 0.03)*1.2*(1 - 2*paradox)`: the abdomen leads the chest by 0.03 cycle (about 11°) with 1.2× excursion. Its sign flips during obstruction (anti-phase), and the 1 s crossfade passes through zero.
   - `th_ab = 1.3*tilt_amp*resp_ab`. `up_ab = [0, 0.9, 0.436]`, normalised (about 26° toward +z). `ga = _rot_x(up_ab, -th_ab)`: x tilt only, with no cardiac or sway term and no z tilt.
   - `acc_ab = ga + 1.1*acc_walk + 0.5*chest_jolt + noise`, with no talk vibration.
   - `gyr_ab = [gradient(th_ab)*fs*180/π, 0, 0] + 0.8*gyr_walk + (-0.2, 0.3, 0.1) dps bias + noise`.
   - Quantised to `abd_a*` and `abd_g*`; `truth['resp_abd'] = resp_ab`.

12. Flex.
   - `flex = flex_baseline (8200) + 50*sin(2πt/900) + 3*sin(2π*1.15*t) + flex_delta + N*flex_noise (6)`. The terms are a 15-min drift, a cardiac pulse, the swallow, head, cough and talk events, and white noise.
   - `flex_raw = clip(round(flex), 0, 16383).astype(int16)`.

13. Neck IMU (`cfg.neck_imu`).
   - `hp = head_pitch + 0.2°*sin(2π*0.25*t)`.
   - `gn = _rot_x([0, 1, 0], -hp)`.
   - `acc_n = gn + acc_walk + noise`.
   - `gyr_n = [gradient(hp)*fs*180/π, 0, 0] + noise`, with no bias.
   - Quantised to `neck_a*` and `neck_g*`.

14. `header = build_header(cfg)`. `raw` is re-keyed into header channel order, and the function returns `SimSession(cfg, labels, raw, header, truth)`.

**Notes.** RNG order matters. The abdomen block draws noise before the flex and neck blocks, so with the same seed, `abdomen=True` changes the `flex_raw` noise and the neck channels. Chest channels, labels and the chest truth are identical (verified). `head_pitch` is always accumulated but used only when `neck_imu` is set. Gyro values clip at the int16 limits (about ±287 dps), not at the 250 dps range named in the header. Coughs do not modulate respiration. In the drinking block, the comment 'head extension ~2.5 s' matches the plateau bump's time above half level (about 2.6 s of 3.5 s).

#### `generate.is_`
*nested function* · [`host/kman_host/simulator.py:180`](../host/kman_host/simulator.py#L180)

```python
def is_(lab)
```

**Purpose.** Label indicator used to switch model terms on per activity.

**How it works.** Returns `(labels == lab).astype(float)`, which is 1.0 inside the label's samples and 0.0 elsewhere. It closes over `generate`'s `labels`.

#### `build_header`
*function* · [`host/kman_host/simulator.py:349`](../host/kman_host/simulator.py#L349)

```python
def build_header(cfg: SimConfig, stream_id: int = 1) -> dict
```

**Purpose.** Builds the session-header dict in the firmware's format (docstring: see firmware/src/main.cpp buildHeaderFrame). Its channel list defines the DATA frame layout and the order of `raw`.

**How it works.**

1. Builds `sensors` and `channels` in slot order:
   - bit 0: chest_imu via `imu('chest_imu', 'chest', 0, '0x6A')`.
   - bit 1: abdomen_imu via `imu('abdomen_imu', 'abd', 1, '0x6B')` if `cfg.abdomen`, else `{slot, bit: 1, status: 'not_configured'}`.
   - bit 2: neck_flex, a sensor entry with model 'resistive divider', sensor 'simulated flex', r_fixed_ohm 47000, topology '3V3 - flex - ADC - R_fixed - GND', pin 0, adc 'simulated', bits 14 and full_scale 16383 (ADC_FULL_SCALE). Its channel is `{name: 'flex_raw', slot: 'neck_flex', bit: 2, unit: 'counts', scale: 1}`.
   - bit 3: neck_imu via `imu('neck_imu', 'neck', 3, '0x6B')` if `cfg.neck_imu`, else not_configured.
2. `present = sum(1 << bit)` over sensors with status 'ok': 5 for chest + flex, 7 with abdomen, 13 with neck, 15 with both.
3. Returns a dict with:
   - identity: `type` 'kman_session_header', `protocol` 1, `fw_version` 'sim', `build` 'simulator', `board` 'simulator', `mcu` 'none', `device_id` 'SIMULATOR0000000', `boot_id` '0x00000001', `stream_id`
   - timing: `t_us_at_header` = cfg.t0_us (unmasked), `sample_rate_hz` = fs, `period_us` = `1_000_000 // cfg.fs` (integer division, so an int: 10000 at 100 Hz)
   - `clock`: source/hfclk 'simulated', t_unit 'us', t_bits 32, seq_bits 16
   - `frame`: a description with sync 'AA55', len 'u16le, bytes of type+body', crc 'CRC-16/CCITT-FALSE over len,type,body; u16le', data_prefix ['t_us:u32', 'seq:u16', 'valid:u8'] and channel_type 'i16'
   - `present_mask`, `sensors`, `channels`

**Notes.** The channel order is chest (6), then abd (6) if present, then flex_raw, then neck (6) if present. The abdomen and neck IMUs are both given address 0x6B. Each firmware build (platformio.ini) puts only one of them at 0x6B; the simulator lets both be enabled together. `odr_hz` 208 is the sensor ODR, while `sample_rate_hz` is the timer-driven read rate.

#### `build_header.imu`
*nested function* · [`host/kman_host/simulator.py:352`](../host/kman_host/simulator.py#L352)

```python
def imu(slot, prefix, bit, addr)
```

**Purpose.** Builds the header sensor entry and the six channel entries for one LSM6DSOX slot.

**How it works.**

Returns `(sensor, chans)`.
- `sensor` fields: slot, bit, status 'ok', model 'LSM6DSOX', bus 'i2c', addr, mode 'timer-master register read (no FIFO)', odr_hz 208, accel_range_g 2, accel_lpf 'LPF2 ODR/10', accel_lpf_hz 20.8, gyro_range_dps 250, gyro_lpf 'LPF1 FTYPE=6 (~26 Hz at 208/416 Hz ODR)', block_data_update True.
- `chans`: for s in (ax, ay, az, gx, gy, gz), `{name: f'{prefix}_{s}', slot, bit, unit: 'g' or 'dps', scale: ACCEL_SCALE or GYRO_SCALE}`, chosen by s[0] == 'a'.

## 14. Host: live viewer

- **`host/live_view.py`**: Command-line real-time viewer for the KMAN signals, built on pyqtgraph and scipy. It reads framed bytes from one of four sources: the logger over localhost UDP (the default), the device serial port, the built-in simulator, or a raw .bin replay. It decodes them with the shared StreamDecoder and runs causal display filters in LiveProcessor: a 0.05-1 Hz band-pass with a sign-stable PC1 projection for respiration tilt, >1.5 Hz chest motion energy, and flex with an EMA baseline. A 33 ms QTimer redraws a scrolling window of plots and a colour-coded label strip. Module constants set the dark theme: SURFACE #1a1a19 background, TEXT #ffffff, TEXT_2 #c3c2b7 secondary text, GRID_ALPHA 0.15, and the SERIES colours for chest, abd, flex and motion.

**How it connects.** The user runs live_view.py as a script; main() is its only entry point. host/tests/test_v1_fixes.py imports Ring directly, and pyproject.toml puts host/ on pythonpath so that import works. In the default mode the viewer depends on host/logger.py. Recorder.feed re-broadcasts every decoded frame's raw bytes with UdpSender.send to port 51515. Recorder.reannounce_header re-sends the last HEADER every 2 s, and Recorder.announce_label sends a TYPE_LABEL frame. run_live/handle_inputs reads this viewer's "LABEL n" datagrams from port 51516 through UdpReceiver.poll and parse_label_command, then calls Recorder.set_label and StreamDecoder.set_label. In --port mode the viewer opens the device's CDC port with pyserial and writes protocol.CMD_SEND_HEADER; host/sim_device.py --pty can stand in for the device. For --sim, sim_source calls kman_host.simulator.generate(SimConfig) and SimSession.stream (header_frame, data_frame, status_frame), and adds protocol.encode_frame(TYPE_LABEL) frames. For --replay, replay_source uses protocol.FrameParser.feed and SessionHeader.from_json. All sources feed one kman_host.stream.StreamDecoder. tick() calls feed (FrameParser.feed, then handle_frame, _on_header, which can raise LayoutChanged, and _on_data) and drain(), and reads header, label, stats.frames_data/dropped/drop_pct(), parser.stats.crc_errors and status (DeviceStatus.overruns/tx_drops). LiveProcessor reads SessionHeader.sample_rate_hz, channel_names and channels (Channel.name/scale/bit) and calls scipy.signal butter, sosfilt_zi, sosfilt and lfilter. The UI uses kman_host.labels LABELS, LABEL_COLORS_DARK and label_name, plus pyqtgraph and Qt (mkQApp, GraphicsLayoutWidget, ImageItem, QTimer). Nothing is written to disk except the optional --screenshot PNG. Recording stays with logger.py, and the offline analysis does not use the viewer's filters.

### `host/live_view.py`

#### `UdpSource`
*class* · [`host/live_view.py:45`](../host/live_view.py#L45)

```python
class UdpSource
```

**Purpose.** Byte source that attaches to a running logger.py. It receives the device frames the logger re-broadcasts and sends label key presses back to the logger.

**How it works.** Fields: class attribute name = "logger (udp)", which __init__ overrides per instance; rx, a kman_host.udp_bus.UdpReceiver bound to the data port; tx, a UdpSender aimed at the control port (data port + 1). It implements the source interface main()/tick() expects: a name attribute, poll() -> bytes and send_label(code) -> bool.

**Notes.**

The data port (default 51515) carries:
- verbatim device frames;
- every 2 s, the last device HEADER, re-encoded by Recorder.feed without t_us_at_header so the viewer's StreamDecoder does not read the stale timestamp as a device clock restart. The current LABEL frame follows each re-send;
- host LABEL frames (type 0x10), sent on every label change.
The control port (default 51516) carries ASCII commands such as b"LABEL 3".

#### `UdpSource.__init__`
*constructor* · [`host/live_view.py:48`](../host/live_view.py#L48)

```python
def __init__(self, port: int)
```

**Purpose.** Opens the receive and control UDP endpoints for the given data port.

**How it works.** Sets self.rx = UdpReceiver(port), which binds a non-blocking socket to 127.0.0.1:port. Sets self.tx = UdpSender(port + 1), a socket that targets 127.0.0.1:port+1. Sets self.name = f"logger udp:{port}", which is shown in the window title and the stats line.

**Notes.** Binding fails (OSError from socket.bind) if another process already holds the port, for example a second viewer. The error is raised in main() before the window is built.

#### `UdpSource.poll`
*method* · [`host/live_view.py:53`](../host/live_view.py#L53)

```python
def poll(self) -> bytes
```

**Purpose.** Returns every byte received since the last call, without blocking.

**How it works.** Returns b"".join(self.rx.poll()). UdpReceiver.poll drains the non-blocking socket with recvfrom(65536) until BlockingIOError, InterruptedError or another OSError, and returns the list of datagrams.

**Notes.** Datagram boundaries are discarded. StreamDecoder's FrameParser re-frames the bytes by sync word and CRC.

#### `UdpSource.send_label`
*method* · [`host/live_view.py:56`](../host/live_view.py#L56)

```python
def send_label(self, code: int) -> bool
```

**Purpose.** Sends a label key press to the logger, which records it.

**How it works.** Sends f"LABEL {code}".encode() through self.tx.send and returns True. True tells Window.keyPressEvent not to set dec.label locally. The displayed label changes only when the logger echoes it back as a LABEL frame (Recorder.announce_label), which StreamDecoder.handle_frame applies to dec.label.

**Notes.** The logger accepts the code only if parse_label_command parses it and the code is in LABELS. UdpSender.send swallows OSError, so a missing logger produces no error and no label change.

#### `SerialSource`
*class* · [`host/live_view.py:61`](../host/live_view.py#L61)

```python
class SerialSource
```

**Purpose.** Byte source that reads the device's USB CDC port directly. Nothing is recorded.

**How it works.** Fields: ser, a pyserial Serial opened non-blocking; name = f"serial {port} (not recording)". Implements poll() and send_label() for main()/tick().

**Notes.** The port is exclusive. logger.py and this mode cannot hold it at the same time.

#### `SerialSource.__init__`
*constructor* · [`host/live_view.py:62`](../host/live_view.py#L62)

```python
def __init__(self, port: str)
```

**Purpose.** Opens the serial port and asks the device for its session header.

**How it works.**

1. Imports serial lazily, so the other modes do not need pyserial.
2. Opens serial.Serial(port, 115200, timeout=0). timeout=0 makes read() return immediately.
3. Writes P.CMD_SEND_HEADER (b"H") so the device re-sends its HEADER frame. Without a header, StreamDecoder discards DATA frames.
4. Sets self.name.

**Notes.** The baud value does not matter for USB CDC. pyserial asserts DTR when it opens the port, and the firmware starts streaming on DTR (per docs/CODE_GUIDE.md). host/sim_device.py --pty can stand in for the device. 'H' is written only this once. logger.py re-sends CMD_SEND_HEADER every 2 s until a header arrives, but the viewer never retries. If both the DTR header and the answer to 'H' are lost, the window stays on "waiting for session header..." and StreamDecoder discards every DATA frame (frames_before_header).

#### `SerialSource.poll`
*method* · [`host/live_view.py:69`](../host/live_view.py#L69)

```python
def poll(self) -> bytes
```

**Purpose.** Returns all bytes currently buffered by the serial driver.

**How it works.** Returns self.ser.read(self.ser.in_waiting or 0). It reads exactly the waiting byte count, or 0 bytes (b"") when nothing is waiting.

**Notes.** If the port disappears, in_waiting or read raises a serial exception. tick() catches it and enters the persistent error state.

#### `SerialSource.send_label`
*method* · [`host/live_view.py:72`](../host/live_view.py#L72)

```python
def send_label(self, code: int) -> bool
```

**Purpose.** Reports that labels cannot be forwarded in direct-serial mode.

**How it works.** Returns False, so Window.keyPressEvent sets dec.label = code locally. The label is display-only and is not recorded anywhere.

#### `PacedFrameSource`
*class* · [`host/live_view.py:76`](../host/live_view.py#L76)

```python
class PacedFrameSource
```

**Purpose.** Plays a pre-built list of encoded frames in real time, multiplied by a speed factor. It backs both the simulator mode and the replay mode.

**How it works.** Fields: frames, the list of complete frame byte strings; data_index, a list of the same length holding for each frame the running count of DATA frames up to and including it; fs, the sample rate in Hz; speed, the playback factor; pos, the index of the next frame to emit (starts at 0); t0, time.monotonic() at construction; name, the display string.

**Notes.** The docstring says it plays frames "using the DATA timestamps". The code paces by DATA-frame count divided by fs, not by the t_us values in the frames, so gaps in a recording are played back without a pause. Gaps inside a segment (lost frames) still appear on the time axis, because StreamDecoder adds the full unwrapped t_us delta. A gap across a new segment does not appear. Such a segment starts at a HEADER with a changed device_id/boot_id/stream_id, for example after a logger reconnect, or at a detected device clock restart. StreamDecoder bridges segments with the host-measured time between feed() calls (nt = max(1, round(gap / period))). During paced playback that time is between zero and about one 33 ms tick, so the two segments are drawn almost back to back.

#### `PacedFrameSource.__init__`
*constructor* · [`host/live_view.py:79`](../host/live_view.py#L79)

```python
def __init__(self, frames: list[bytes], data_index: list[int], fs: float, speed: float, name: str)
```

**Purpose.** Stores the frame list and pacing parameters and starts the playback clock.

**How it works.** Assigns frames, data_index, fs, speed and name. Sets pos = 0 and t0 = time.monotonic().

**Notes.** The clock starts at construction, which happens in main() before the Qt window is built. The first poll() therefore emits a burst covering the setup time.

#### `PacedFrameSource.poll`
*method* · [`host/live_view.py:88`](../host/live_view.py#L88)

```python
def poll(self) -> bytes
```

**Purpose.** Returns all frames whose scheduled time has passed.

**How it works.**

1. due = (time.monotonic() - t0) \* speed \* fs. This is the number of DATA samples that should have been played by now.
2. While pos &lt; len(frames) and data_index[pos] &lt;= due, it appends frames[pos] and increments pos.
3. Returns b"".join(out).
The k-th DATA frame (data_index k) is released once elapsed time >= k / (fs\*speed). A non-DATA frame carries the count of DATA frames before it, which is k-1 when it sits between DATA frames k-1 and k. It is therefore released in the same poll as DATA frame k-1, up to one sample period (1/(fs\*speed)) before frame k. In the byte order it still comes right before frame k. Frames ahead of the first DATA frame (the header and the first sim LABEL frame) have index 0 and are released on the first poll.

**Notes.** Once every frame has been played, it returns b"" forever. There is no looping.

#### `PacedFrameSource.send_label`
*method* · [`host/live_view.py:96`](../host/live_view.py#L96)

```python
def send_label(self, code: int) -> bool
```

**Purpose.** Reports that labels cannot be forwarded in sim or replay mode.

**How it works.** Returns False, so Window.keyPressEvent sets dec.label locally (display-only).

**Notes.** In sim mode, the next ground-truth LABEL frame from the simulator overwrites the local label.

#### `sim_source`
*function* · [`host/live_view.py:100`](../host/live_view.py#L100)

```python
def sim_source(abdomen: bool, speed: float) -> PacedFrameSource
```

**Purpose.** Builds a PacedFrameSource from a fully generated simulator session. Ground-truth labels are inserted as LABEL frames so the label strip shows the true activity.

**How it works.**

1. Imports kman_host.simulator lazily and calls s = sim.generate(sim.SimConfig(abdomen=abdomen)), which generates the default scenario in memory.
2. Iterates s.stream(), which yields (i, frame_bytes): i = -1 for the header, then i for each DATA frame, plus STATUS frames tagged with the current i.
3. When i >= 0 and s.labels[i] != last_label, it appends P.encode_frame(P.TYPE_LABEL, bytes([label])) with index n_data, the count before this frame, so the label goes out ahead of data frame i.
4. Appends the frame. If i >= 0 and fr[4] == P.TYPE_DATA (byte 4 is the type byte after the 2-byte sync and 2-byte length), it increments n_data. It then appends n_data to idx.
5. Returns PacedFrameSource(frames, idx, s.cfg.fs, speed, "simulator (labels = ground truth)").

**Notes.** Because each LABEL frame precedes DATA frame i, StreamDecoder (which sets dec.label from TYPE_LABEL frames) tags frame i and later frames with labels[i]. STATUS frames share the index of the preceding DATA frame.

#### `replay_source`
*function* · [`host/live_view.py:119`](../host/live_view.py#L119)

```python
def replay_source(path: str, speed: float) -> PacedFrameSource
```

**Purpose.** Builds a PacedFrameSource that plays back a raw .bin capture.

**How it works.**

1. Reads the whole file and parses it with a fresh P.FrameParser().feed(...), which returns only complete, CRC-valid frames.
2. fs defaults to 100.0. For every HEADER frame, fs = SessionHeader.from_json(f.body).sample_rate_hz.
3. A DATA frame increments n_data before its index is recorded. Each frame's f.raw goes to out and n_data goes to idx.
4. Returns PacedFrameSource(out, idx, fs, speed, f"replay {filename} x{speed:g}").

**Notes.** A single fs is passed after the loop, so the last HEADER in the file sets the pacing for the whole file. Corrupt frames are dropped here, so the replay's crc counter (dec.parser.stats.crc_errors) stays 0. A trailing partial frame is dropped. The .bin holds only the device's bytes, and the recording's .labels.jsonl sidecar is never read. The label strip therefore stays at label 0 unless the user presses digit keys, and those labels are display-only.

#### `Ring`
*class* · [`host/live_view.py:138`](../host/live_view.py#L138)

```python
class Ring
```

**Purpose.** Fixed-capacity circular buffer of float rows that holds the last n samples of the visible window.

**How it works.** Fields: buf, an (n, width) float array initialised to NaN; n, the capacity in rows; count, the total number of rows ever appended. The oldest retained row is always at index count % n once the buffer has wrapped (count >= n). Before that, rows 0..count-1 are in order.

**Notes.** host/tests/test_v1_fixes.py imports Ring directly and checks ordering after a batch larger than n.

#### `Ring.__init__`
*constructor* · [`host/live_view.py:139`](../host/live_view.py#L139)

```python
def __init__(self, n: int, width: int = 1)
```

**Purpose.** Allocates the buffer.

**How it works.** buf = np.full((n, width), np.nan); self.n = n; count = 0.

#### `Ring.extend`
*method* · [`host/live_view.py:144`](../host/live_view.py#L144)

```python
def extend(self, x: np.ndarray) -> None
```

**Purpose.** Appends a batch of rows and overwrites the oldest rows when full.

**How it works.**

1. x = np.asarray(x, float).reshape(len(x), -1). A 1-D batch becomes (N, 1); an (N, 3) batch is unchanged.
2. Large batch (len(x) >= n): count += len(x), then buf[:] = np.roll(x[-n:], count % n, axis=0). Only the newest n rows are kept. np.roll moves the oldest of them to index count % n, which is where ordered() starts reading, so the invariant holds.
3. Normal batch (len(x) &lt; n): start = count % n; first = min(len(x), n - start). It writes buf[start:start+first] = x[:first] and buf[:len(x)-first] = x[first:]; the second write is the wrap-around part and is empty when nothing wraps. Then count += len(x).

**Notes.** An empty batch raises ValueError in the reshape (size 0 cannot reshape to (0, -1)). Callers never pass one, because StreamDecoder.drain returns None instead of empty columns. A large batch occurs when 20 s or more of data arrives in one tick, for example after a long stall or the first poll of a fast replay.

#### `Ring.ordered`
*method* · [`host/live_view.py:157`](../host/live_view.py#L157)

```python
def ordered(self) -> np.ndarray
```

**Purpose.** Returns the retained rows oldest first.

**How it works.** If count &lt; n, returns buf[:count], which is a view. Otherwise s = count % n and it returns np.concatenate([buf[s:], buf[:s]]), which is a new (n, width) array.

**Notes.** Always 2-D. Callers take [:, 0] for width-1 rings. NaN fill rows are never returned, because the unfilled tail is excluded while count &lt; n.

#### `LiveProcessor`
*class* · [`host/live_view.py:164`](../host/live_view.py#L164)

```python
class LiveProcessor
```

**Purpose.** Holds the causal, streaming display filters and the window ring buffers for one session layout. It is built once from the first session header.

**How it works.** Fields: h, the SessionHeader; fs, the sample rate in Hz; groups, a dict mapping "chest" -> [chest_ax, chest_ay, chest_az] and "abd" -> [abd_ax, abd_ay, abd_az], each present only if all three channels exist; has_flex, which is "flex_raw" in the channel names; scale and bit, dicts mapping channel name to Channel.scale and Channel.bit; sos_resp, a 2nd-order Butterworth band-pass [hp_hz, 1.0] Hz (two SOS sections, 4th order in total); sos_motion, a 2nd-order Butterworth high-pass at 1.5 Hz (one SOS section); zi, a dict of filter and EMA states keyed by name ("chest_resp", "abd_resp", "chest_motion", "motion", "flex_base"); last_valid, a dict holding the last good sample per key ("chest", "abd", "flex"); rings, each of n = int(window_s\*fs) rows: t (seconds), resp[key] (width 3, band-passed g), flex (counts), flex_detr (counts), motion (mg) and label; pc, a dict of the previous PC1 vector per group.

**Notes.** The processor is never reset. When the decoder starts a new segment (device restart with the same layout), filter states and hold values carry over. Lost frames are not interpolated, so the filters treat the received samples as contiguous at fs.

#### `LiveProcessor.__init__`
*constructor* · [`host/live_view.py:165`](../host/live_view.py#L165)

```python
def __init__(self, header: P.SessionHeader, window_s: float, hp_hz: float)
```

**Purpose.** Derives the channel groups, filter designs and ring buffers from the session header.

**How it works.**

1. fs = header.sample_rate_hz; n = int(window_s \* fs).
2. Builds scale and bit dicts from header.channels.
3. For ("chest", "chest_a") and ("abd", "abd_a"), adds the group if all of prefix+x, y and z are in header.channel_names.
4. has_flex = "flex_raw" in names.
5. sos_resp = signal.butter(2, [hp_hz, 1.0], btype="bandpass", fs=fs, output="sos"); sos_motion = signal.butter(2, 1.5, btype="highpass", fs=fs, output="sos").
6. Creates empty zi, last_valid and pc dicts and the Rings: t, resp (one width-3 Ring per group), flex, flex_detr, motion and label.

**Notes.** hp_hz comes from --hp (default 0.05 Hz) and window_s from --window (default 20 s). The motion Ring is created even when there is no chest group; nothing fills it then.

#### `LiveProcessor._held`
*method* · [`host/live_view.py:192`](../host/live_view.py#L192)

```python
def _held(self, key: str, x: np.ndarray, ok: np.ndarray) -> np.ndarray
```

**Purpose.** Replaces samples from frames whose valid bit is clear with the last good value, so no step is fed into the IIR filters and EMAs.

**How it works.**

1. last = last_valid.get(key, zeros(x.shape[1:])). Before any good sample the hold value is 0.
2. If not ok.all(), it copies x and walks the rows. An ok row updates last = x[i]; a bad row is overwritten with x[i] = last.
3. Stores last_valid[key] = x[-1] if ok[-1] else last, so the hold carries across batch boundaries.
4. Returns x. When every sample is valid, this is the original array and no copy is made.

**Notes.** The code comment states the purpose as "keeps IIR state finite". The firmware zeroes a failed slot's channels and clears its valid bit (docs/CODE_GUIDE.md), so without the hold the filters would see a jump from about 1 g to 0. If the very first samples are invalid, the hold value is 0, so the filters start from 0 and see a step when real data arrives.

#### `LiveProcessor._ema`
*method* · [`host/live_view.py:205`](../host/live_view.py#L205)

```python
def _ema(self, key: str, x: np.ndarray, tau_s: float) -> np.ndarray
```

**Purpose.** First-order exponential moving average with state persisted across batches. It is used for the motion-energy smoothing and the flex baseline.

**How it works.**

1. a = 1 - exp(-1 / (tau_s \* fs)). This gives the recursion y[n] = a\*x[n] + (1-a)\*y[n-1].
2. On the first call for key, zi[key] = [(1 - a) \* x[0]]. In lfilter's transposed direct form the state is z = (1-a)\*y_prev, so this sets y_prev = x[0] and y[0] = x[0]: the filter starts settled on the first input.
3. y, zi[key] = signal.lfilter([a], [1.0, -(1.0 - a)], x, zi=zi[key]), and the final state is saved for the next batch.
4. Returns y.

**Notes.** 1-D input only. At fs = 100 Hz: tau 0.5 s gives a ≈ 0.0198 and tau 10 s gives a ≈ 0.0010.

#### `LiveProcessor._filt`
*method* · [`host/live_view.py:212`](../host/live_view.py#L212)

```python
def _filt(self, key: str, sos: np.ndarray, x: np.ndarray) -> np.ndarray
```

**Purpose.** Causal SOS filtering along time for an (N, 3) batch, with per-axis state persisted across batches.

**How it works.**

1. On the first call for key: zi = signal.sosfilt_zi(sos), shape (n_sections, 2), which is the steady state for a unit step. It stores zi[:, :, None] \* x[0][None, None, :], shape (n_sections, 2, 3): the steady state for a constant input equal to the first sample on each axis.
2. y, zi[key] = signal.sosfilt(sos, x, axis=0, zi=zi[key]).
3. Returns y.

**Notes.** Both filters reject DC, so steady-state initialisation makes the output start near 0 instead of ringing from the about 1 g gravity step (code comment: "start in steady state"). This is single-pass causal filtering, not filtfilt, so the band-pass adds phase lag.

#### `LiveProcessor.process`
*method* · [`host/live_view.py:219`](../host/live_view.py#L219)

```python
def process(self, b: dict[str, np.ndarray]) -> None
```

**Purpose.** Runs one drained batch of decoded samples through the filters and appends the results to the rings.

**How it works.**

1. t.extend(b["t_us"] / 1e6), the unwrapped device time in seconds. label.extend(b["label"]).
2. For each group (key, cols): ok = (valid & (1 &lt;&lt; bit[cols[0]])) != 0, the slot bit of the x channel, shared by all three axes of one IMU. g = np.stack([b[c] \* scale[c] for c in cols], axis=1), shape (N, 3) in g. g = _held(key, g, ok). resp[key].extend(_filt(key + "_resp", sos_resp, g)).
3. If key == "chest": hp = _filt("chest_motion", sos_motion, g); e = sum(hp**2, axis=1), in g²; motion.extend(sqrt(max(_ema("motion", e, 0.5), 0)) \* 1000.0), the rms in mg over a 0.5 s EMA of energy.
4. If has_flex: ok from the flex_raw bit. f = _held("flex", flex_raw as a float column, ok)[:, 0]. flex.extend(f). flex_detr.extend(f - _ema("flex_base", f, 10.0)), the raw value minus a 10 s EMA baseline.

**Notes.** Flex stays in raw ADC counts; Channel.scale is not applied. The np.maximum(..., 0) guards against tiny negative EMA values from rounding before the sqrt. Only the chest IMU feeds the motion trace.

#### `LiveProcessor.resp_pc1_deg`
*method* · [`host/live_view.py:238`](../host/live_view.py#L238)

```python
def resp_pc1_deg(self, key: str) -> np.ndarray
```

**Purpose.** Projects the band-passed 3-axis tilt in the visible window onto its first principal component. The sign is kept continuous between redraws so the trace does not flip.

**How it works.**

1. x = resp[key].ordered(), shape (N, 3). If N &lt; 10, returns zeros(N).
2. xc = x - x.mean(axis=0).
3. w, v = np.linalg.eigh(np.cov(xc.T)). eigh returns eigenvalues in ascending order, so pc = v[:, -1] is the unit eigenvector of the largest variance.
4. Sign stabilisation: an eigenvector's sign is arbitrary and can change from one call to the next. If pc[key] from the previous call exists and pc @ prev &lt; 0, pc is negated. The result is stored in self.pc[key].
5. Returns np.degrees(xc @ pc).

**Notes.** Small-angle conversion (code comment): a change in the gravity component, as a fraction of g, approximately equals the tilt in radians. The PCA is recomputed over the whole window on every redraw, so the trace is zero-mean over the window and its axis can drift as the window content changes. The stored pc is not updated while the view is in axes mode.

#### `LiveProcessor.resp_axes_deg`
*method* · [`host/live_view.py:251`](../host/live_view.py#L251)

```python
def resp_axes_deg(self, key: str) -> np.ndarray
```

**Purpose.** Returns the three band-passed axes separately, each in degrees of tilt and centred on the window mean.

**How it works.** x = resp[key].ordered(). Returns np.degrees(x - x.mean(axis=0)) if len(x) > 0, otherwise the empty x. Shape is (N, 3).

**Notes.** Uses the same small-angle conversion as resp_pc1_deg.

#### `build_label_lut`
*function* · [`host/live_view.py:259`](../host/live_view.py#L259)

```python
def build_label_lut() -> np.ndarray
```

**Purpose.** Builds the 256-entry RGBA lookup table that colours the label strip by label code.

**How it works.** lut = zeros((256, 4), uint8), with every row set to (0x38, 0x38, 0x35, 255), a neutral dark grey used for label 0 and unknown codes. For each (k, "#rrggbb") in LABEL_COLORS_DARK (labels 1-9), it writes lut[k] = (r, g, b, 255) parsed from the hex string. Returns lut.

**Notes.** Used with ImageItem levels=(0, 255), so a label code maps directly to its LUT row.

#### `main`
*function* · [`host/live_view.py:267`](../host/live_view.py#L267)

```python
def main(argv=None) -> int
```

**Purpose.** Entry point. It parses options, selects the source, builds the Qt window and runs the 33 ms refresh loop until the window closes.

**How it works.**

1. argparse. A mutually exclusive group holds --udp [PORT] (nargs="?", const=DEFAULT_PORT 51515), --port DEV, --sim and --replay FILE. Other options: --abdomen, --speed (1.0), --window (20.0 s), --hp (0.05 Hz), --exit-after (0, meaning off), --screenshot PNG.
2. Imports pyqtgraph and its Qt modules only after parsing, so --help and importing Ring need no Qt.
3. Source precedence: --port -> SerialSource; --sim -> sim_source(abdomen, speed); --replay -> replay_source(path, speed); otherwise UdpSource(args.udp or DEFAULT_PORT).
4. pg.setConfigOptions(antialias=True, background=SURFACE, foreground=TEXT_2); app = pg.mkQApp("KMAN live view").
5. state = {paused: False, axes: False, detrend: True, proc: None, error: None}; dec = StreamDecoder().
6. Defines Window and builds the layout. The top row has label_lbl (20 pt bold, word-wrapped, initially "waiting for session header...") and stats_lbl (right-aligned). Below are a GraphicsLayoutWidget with stretch 1 and a help line listing the keys and LABELS. The window is resized to 1280x860 and shown.
7. Creates the plots and curves dicts and label_img, a pg.ImageItem with build_label_lut(). Defines make_plot, build_ui and tick, plus rate_hist.
8. QTimer(33 ms) -> tick, about 30 Hz. If --exit-after is set, QTimer.singleShot(ms, win.close) is scheduled.
9. app.exec(); return 0.

**Notes.** `args.udp or DEFAULT_PORT` falls back to 51515 for --udp 0. The module ends with `if __name__ == "__main__": sys.exit(main())`. Plots are not built until the first header arrives (in tick).

#### `Window`
*class* · [`host/live_view.py:300`](../host/live_view.py#L300)

```python
class Window(QtWidgets.QWidget)  # nested in main
```

**Purpose.** Top-level QWidget that adds the keyboard controls and the screenshot-on-close hook.

**How it works.** No fields of its own. It is defined inside main() so its handlers close over source, dec, state and args. Only keyPressEvent and closeEvent are overridden.

#### `Window.keyPressEvent`
*method* · [`host/live_view.py:301`](../host/live_view.py#L301)

```python
def keyPressEvent(self, ev)
```

**Purpose.** Handles the label keys, pause, view toggles and quit.

**How it works.**

k = ev.text().
- Digit: code = int(k). If source.send_label(code) returns False (serial, sim or replay mode), it sets dec.label = code. StreamDecoder then tags subsequently decoded DATA frames with that label, for display only. In UDP mode nothing changes locally until the logger echoes a LABEL frame.
- " ": toggles state["paused"].
- "a": toggles state["axes"] (PC1 or three axes).
- "d": toggles state["detrend"] (flex minus baseline or raw).
- "q", "\x1b", or ev.key() == Qt.Key.Key_Escape: self.close().

**Notes.** Pause only freezes drawing. tick() keeps polling and processing, so the rings stay current.

#### `Window.closeEvent`
*method* · [`host/live_view.py:316`](../host/live_view.py#L316)

```python
def closeEvent(self, ev)
```

**Purpose.** Saves a PNG of the window on close when --screenshot is given.

**How it works.** If args.screenshot is set, calls self.grab().save(args.screenshot). Then calls super().closeEvent(ev).

**Notes.** It runs however the window closes (the q/Esc keys, the window close button, or the --exit-after timer calling win.close), as the code comment states.

#### `make_plot`
*nested function* · [`host/live_view.py:352`](../host/live_view.py#L352)

```python
def make_plot(row: int, title: str, units: str, height: int | None = None)  # nested in main
```

**Purpose.** Creates one scrolling plot row with the shared styling.

**How it works.** p = glw.addPlot(row=row, col=0). Sets the title (TEXT colour, 11pt) only if title is non-empty. Sets the left axis label to units, disables the auto SI prefix, and fixes the left axis width at 60 px so the rows align. Shows the grid with alpha GRID_ALPHA. Sets the X range to [-args.window, 0] with no padding. Enables Y auto-range, with setAutoVisible(y=True) so it uses only the visible data. Mouse zoom/pan is off on x and on for y. Applies setMaximumHeight(height) if given. Returns the PlotItem.

#### `build_ui`
*nested function* · [`host/live_view.py:368`](../host/live_view.py#L368)

```python
def build_ui(proc: LiveProcessor) -> None  # nested in main
```

**Purpose.** Creates the plot rows that match the channels present in the session layout.

**How it works.**

1. For ("chest", "Chest respiration") and ("abd", "Abdomen respiration"), if the key is in proc.groups: adds a plot titled "&lt;title>  (0.05-1 Hz tilt)" with units deg. curves[key] = [PC1 curve (SERIES[key], width 2)] plus three width-1 axis curves (#3987e5, #d95926, #199e70 for x, y, z).
2. If proc.has_flex: adds a "Neck flex" plot in counts with one curve.
3. If "chest" is in groups: adds a "Chest motion energy  (>1.5 Hz, rms)" plot in mg, max height 150, with one curve.
4. Label strip: an untitled plot with units "label", max height 80. Y ticks are cleared, mouse is disabled, label_img is added, the Y range is fixed to 0..1, and the bottom axis is labelled "seconds".
5. Every other plot is X-linked to plots["label"].

**Notes.** The "0.05-1 Hz" in the respiration title is hard-coded and does not follow --hp.

#### `tick`
*nested function* · [`host/live_view.py:401`](../host/live_view.py#L401)

```python
def tick() -> None  # nested in main, QTimer slot every 33 ms
```

**Purpose.** One refresh cycle. It pulls bytes, decodes, processes, updates the status labels and redraws, and it stops for good on a source or layout error.

**How it works.**

1. Persistent error: if state["error"] is set, it returns immediately. The source is never polled again, the error text stays and the plots stay frozen until the viewer is restarted.
2. try: data = source.poll(); if data, dec.feed(data). except LayoutChanged as e (raised by StreamDecoder._on_header when a header's layout_key, i.e. channel names, scales and period_us, differs): state["error"] = "layout changed, restart the viewer: ...". except Exception (for example the serial port vanishing): "source error, restart the viewer: ...".
3. If an error is now set: label_lbl.setText(error[:300]) and return.
4. If dec.header exists and state["proc"] is None: state["proc"] = LiveProcessor(dec.header, args.window, args.hp) and build_ui(proc). This happens once, from the first header.
5. b = dec.drain(). It is called before the proc check so decoded rows never pile up. If proc is None, return. If b is not None, proc.process(b), which runs even while paused.
6. Rate: appends (monotonic now, dec.stats.frames_data) to rate_hist and drops entries older than 3.0 s. rate = Δframes / max(1e-6, Δt) across the history.
7. label_lbl shows a coloured ■ (LABEL_COLORS_DARK[lab], or TEXT_2 if missing), dec.label and label_name, plus "   [PAUSED]".
8. stats_lbl shows source.name, rate Hz, frames_data, lost = dec.stats.dropped (drop_pct with 3 decimals), and crc = dec.parser.stats.crc_errors. If a STATUS frame has arrived, it adds " | dev ovr {overruns} txdrop {tx_drops}" from dec.status.
9. Returns if paused or proc.t.count == 0.
10. t = proc.t.ordered()[:, 0]; x = t - t[-1], seconds relative to the newest sample (&lt;= 0).
11. For each group: in axes mode the PC1 curve is cleared and curves 1-3 get resp_axes_deg columns. Otherwise curve 0 gets resp_pc1_deg and the axis curves are cleared.
12. Flex: plots flex_detr or flex depending on state["detrend"], and retitles the plot "Neck flex (minus 10 s baseline)" or "Neck flex (raw)".
13. Motion curve, if present.
14. Label strip: labs = proc.label.ordered()[:, 0] as uint8. label_img.setImage(labs[:, None], autoLevels=False, levels=(0, 255)). label_img.setRect(QRectF(x[0], 0, (x[-1] - x[0]) or 1e-3, 1)); the 1e-3 fallback avoids a zero width when there is one sample.

**Notes.** Only source.poll() and dec.feed() are inside the try. Exceptions from process() or the drawing code are not caught here. Frames that arrive before the first header are discarded by StreamDecoder, so in UDP mode up to 2 s of data are skipped until the logger re-sends its header. The label image spreads its N pixels evenly across [x[0], x[-1]], so across a time gap the strip is not exactly aligned with the curves.

## 15. Analysis: config, preprocessing, breathing

- **`analysis/kman_analysis/__init__.py`**: Package entry point for the offline Step 1 analysis (breathing, chest-abdomen phase, neck events). Defines __version__ = "1.0.0" and re-exports load_config (from config.py) and Analysis/analyze (from pipeline.py) as the public API (__all__). report.py and the tests import from here.
- **`analysis/kman_analysis/config.py`**: Loads the analysis configuration. It reads the defaults from analysis/config.yaml, then deep-merges an optional user YAML file and an optional overrides dict on top. Any key that does not exist in the defaults raises KeyError, so a typo cannot be silently ignored.
- **`analysis/config.yaml`**: Holds the analysis thresholds and tuning constants, grouped into seven top-level sections (preprocess, calibration, breathing, motion, phase, swallow, report). The header comment says that every threshold lives here and that units are in the key names, but neither is fully true. Several limits are hard-coded in the modules: the 5 s minimum before calibration falls back, the 0.3 clean fraction in windowed_pca, the 50/10-sample minimums in orient_inspiration_up, the >10-sample calibration and 0.5 window-fill gates in phase, the 2 Hz low-pass and 0.2 s reference in swallow.head_pose_change, the 4th-order motion high-pass, and others. Some keys also carry no unit (k_on, rel_prominence, polarity, label). The header comment also says the defaults were tuned on the simulator and should be re-tuned on real recordings. A partial file passed with report.py --config is deep-merged over this one by load_config.
- **`analysis/kman_analysis/preprocess.py`**: Converts a recorded kman_host Session (rows of decoded frames) into a Uniform object: a gap-free sample grid in physical units, indexed by the unwrapped sequence number. Lost frames and samples flagged invalid by the device are filled by linear interpolation. Holes longer than max_interp_gap_s are also flagged in a shared gap mask, so later stages report nothing from interpolated data. The file also chooses the per-session calibration (quiet reference) samples and provides the run-length helper used across the analysis package.
- **`analysis/kman_analysis/breathing.py`**: Turns a chest or abdomen IMU into a 1-D respiratory waveform in degrees and detects breaths on it. The steps are: complementary-filter gravity direction, FIR decimation to resp_fs, zero-phase band-pass, windowed PCA, and an inspiration-up sign choice. Breaths are found by peak picking with an adaptive prominence threshold. The file also computes the chest motion-energy signal that pipeline.analyze thresholds into the motion-artifact mask. Results come back in the Breathing dataclass: breaths, per-interval rates and seconds since the last valid breath.

**How it connects.** The entry point is analysis/report.py main. It calls load_config(args.config), then kman_host.session_io.load_session, then pipeline.analyze(session, cfg); the analysis tests call load_config() or load_config(overrides=...) and analyze directly. pipeline.analyze calls to_uniform(session, cfg) and calibration_mask(u, cfg). to_uniform relies on the kman_host Session: df columns seq (unwrapped to 64 bits by kman_host.stream.StreamDecoder._on_data), label, valid and the channel columns; header.channels (name, bit, scale from protocol.Channel); fs = header.sample_rate_hz; has_slot -> SessionHeader.slot_present; and label_names. Next, analyze calls u.vec("chest", "a") and motion_energy_mg, and thresholds the result with motion.threshold_mg itself. It then calls analyze_breathing(u, "chest" / "abd", cfg, calib, motion). That runs respiratory_waveform (which uses gravity_complementary, bandpass and windowed_pca), orient_inspiration_up and detect_breaths. The returned Breathing objects are used in several places. phase.chest_abdomen_phase receives chest/abd wave_deg, t and fs from analyze, together with calib and motion|gap decimated by q. pipeline.per_label_summary and _max_breath_gap read breath_t, breath_valid, rate_t, rate_bpm and breath_amp_deg, and pipeline.quality reads sign_flipped. plotting.make_figure plots the waveform, rate and since_last_s, and report.py writes the &lt;session>_breaths.csv file. Other modules reuse the preprocess helpers. swallow.analyze_neck and swallow.head_pose_change use Uniform.vec("neck", ...), u.ch["flex_raw"], u.gap and the calibration mask. preprocess.runs is used by swallow.analyze_neck, pipeline._max_breath_gap and plotting._bands / _artifact_spans. The config sections are read by phase.chest_abdomen_phase (phase), swallow.analyze_neck (swallow), pipeline (motion.threshold_mg, swallow.rate_window_s), plotting.make_figure (report, motion, phase, swallow) and report.py (report.dpi). Direct unit tests: analysis/tests/test_analysis.py (gravity_complementary, rejection of unknown config keys) and host/tests/test_v1_fixes.py (detect_breaths).

### `analysis/config.yaml`

#### `config.yaml: preprocess`
*config* · [`analysis/config.yaml:6`](../analysis/config.yaml#L6)

```
preprocess: {max_interp_gap_s: 0.5}
```

**Purpose.** Sets how long a run of missing or invalid samples may be before it is masked.

**How it works.** max_interp_gap_s (0.5 s): preprocess.to_uniform converts it to max_gap = round(0.5 \* fs) samples (50 at 100 Hz). Every hole, whether lost frames or samples whose valid bit is clear, is linearly interpolated. Holes strictly longer than max_gap samples are additionally flagged in Uniform.gap. The YAML comment states the rationale: long gaps are still interpolated to keep the filters stable, but no breaths or events are reported inside them.

#### `config.yaml: calibration`
*config* · [`analysis/config.yaml:11`](../analysis/config.yaml#L11)

```
calibration: {label: 1, fallback_first_s: 30, max_s: 120}
```

**Purpose.** Defines the per-session quiet reference segment that thresholds are made relative to.

**How it works.**

All three keys are read by preprocess.calibration_mask.
- label (1 = rest breathing): samples with this label, excluding gap samples, form the calibration mask.
- fallback_first_s (30 s): used when the label yields fewer than 5 s of samples; the mask becomes the first 30 s, excluding gaps.
- max_s (120 s): at most this many seconds of mask samples are kept (the first ones in time).

#### `config.yaml: breathing`
*config* · [`analysis/config.yaml:16`](../analysis/config.yaml#L16)

```
breathing: {gyro_bias_s, comp_filter_tau_s, resp_fs_hz, band_hz, filter_order, pca_window_s, peaks: {min_interval_s, rel_prominence, abs_min_prominence_deg, ref_window_s, ref_quantile}, max_rate_interval_s}
```

**Purpose.** Sets the parameters of respiratory waveform extraction and breath detection.

**How it works.**

Read by breathing.respiratory_waveform:
- gyro_bias_s (10): gyro bias is the median over the first 10 s of calibration samples.
- comp_filter_tau_s (1.0 s): complementary-filter time constant; the gyro dominates above about 1/(2\*pi\*tau) ≈ 0.16 Hz and the accelerometer below.
- resp_fs_hz (20): target processing rate; decimation factor q = round(fs/20).
- band_hz ([0.05, 1.0]): Butterworth band-pass edges in Hz.
- filter_order (2): butter() order per pass; filtfilt doubles the effective order.
- pca_window_s (30): windowed-PCA window (50 % overlap, Hann overlap-add).

Read by breathing.detect_breaths:
- peaks.min_interval_s (1.5 s): find_peaks distance, which caps the rate at 40 breaths/min.
- peaks.rel_prominence (0.3): multiplier on the reference peak-to-peak amplitude.
- peaks.abs_min_prominence_deg (0.08 deg): floor on the prominence threshold (noise/cardiac floor).
- peaks.ref_window_s (60 s): centred rolling window for the reference amplitude.
- peaks.ref_quantile (0.8): quantile of |waveform| in that window.
- max_rate_interval_s (15 s): intervals longer than this produce no rate value.

#### `config.yaml: motion`
*config* · [`analysis/config.yaml:31`](../analysis/config.yaml#L31)

```
motion: {highpass_hz: 1.5, window_s: 2.0, threshold_mg: 15.0}
```

**Purpose.** Sets up the chest-accelerometer motion-artifact detector.

**How it works.**

- highpass_hz (1.5 Hz): 4th-order Butterworth high-pass cutoff in breathing.motion_energy_mg (the spec defines motion as energy above 1.5 Hz).
- window_s (2.0 s): centred sliding window of the RMS in motion_energy_mg.
- threshold_mg (15.0 mg): pipeline.analyze sets motion = motion_mg > threshold_mg.
plotting.make_figure reads all three for the motion panel title and its dashed threshold line.

#### `config.yaml: phase`
*config* · [`analysis/config.yaml:36`](../analysis/config.yaml#L36)

```
phase: {window_s: 10, min_amplitude_frac: 0.25, min_coherence: 0.5, paradox_deg: 120}
```

**Purpose.** Configures the chest-abdomen phase estimate, which runs only when an abdomen IMU is present.

**How it works.**

All four keys are read by phase.chest_abdomen_phase.
- window_s (10 s): sliding circular-mean window of the Hilbert phase difference.
- min_amplitude_frac (0.25): both Hilbert envelopes must exceed 0.25 x their calibration median.
- min_coherence (0.5): minimum resultant length of the windowed phase difference.
- paradox_deg (120 deg): valid samples with |phase| above this count as paradoxical effort.
plotting.make_figure also draws paradox_deg as threshold lines.

#### `config.yaml: swallow`
*config* · [`analysis/config.yaml:42`](../analysis/config.yaml#L42)

```
swallow: {baseline_window_s, polarity, k_on, k_off, sigma_floor_counts, min_duration_s, max_duration_s, merge_gap_s, head_amplitude_k, rate_window_s, head_imu: {tilt_change_deg, yaw_change_deg, margin_s}}
```

**Purpose.** Sets the neck-flex event detector and its classification into swallow and head motion.

**How it works.**

Read by swallow.analyze_neck:
- baseline_window_s (10 s): centred moving-median baseline that removes drift. The window is forced odd, minimum 3 samples.
- polarity (abs | positive | negative): which deflections count as events. positive uses dev, negative uses -dev, and any other value uses |dev|.
- k_on (5.0): a candidate is kept only if its polarity-adjusted deviation exceeds k_on \* sigma_cal somewhere inside it.
- k_off (2.0): the hysteresis level. Each kept event spans the whole surrounding run where the deviation is above k_off \* sigma_cal, so both its start and its end are k_off crossings.
- sigma_floor_counts (2.0): lower bound on sigma_cal.
- merge_gap_s (0.2 s): candidates separated by at most this gap (&lt;=) are merged. This happens before the duration check.
- min_duration_s (0.3 s): merged events shorter than this are discarded as spikes. Events that touch any gap sample are also discarded.
- max_duration_s (2.5 s): longer events are kept but classified as head_motion (reason "duration"), since they are a sustained bend, not a swallow.
- head_amplitude_k (25.0): a peak deviation above 25 x sigma_cal is classified as head_motion (reason "amplitude"). This test is checked before the duration test.
- rate_window_s (10 s): window of the centred rolling event counts (events per 10 s). pipeline.per_label_summary also reads this key.
- head_imu (used only when a neck IMU is present): tilt_change_deg (8.0, change of gravity direction during the event), yaw_change_deg (10.0, rotation about vertical; needs the neck gyro), margin_s (0.5, examine [start - margin, end + margin]). Exceeding either angle makes the event head_motion, overriding the other reasons.
plotting.make_figure reads baseline_window_s, k_on, head_amplitude_k, polarity and rate_window_s.

#### `config.yaml: report`
*config* · [`analysis/config.yaml:58`](../analysis/config.yaml#L58)

```
report: {width_in: 18, panel_height_in: 1.9, dpi: 110, band_alpha: 0.16}
```

**Purpose.** Sets the layout and output resolution of the report figure.

**How it works.**

- width_in (18): figure width in inches (plotting.make_figure).
- panel_height_in (1.9): height per unit of panel height ratio; the figure height is panel_height_in \* sum(heights) + 3.2 in (plotting.make_figure).
- dpi (110): report.py main passes it to fig.savefig.
- band_alpha (0.16): opacity of the label background bands (plotting._bands, called from make_figure).

### `analysis/kman_analysis/__init__.py`

#### `kman_analysis (package exports)`
*module* · [`analysis/kman_analysis/__init__.py:1`](../analysis/kman_analysis/__init__.py#L1)

```python
__version__ = "1.0.0"; __all__ = ["Analysis", "analyze", "load_config"]
```

**Purpose.** Defines the public API of the analysis package and its version string.

**How it works.** Line 3 sets __version__ = "1.0.0". Line 5 imports load_config from .config. Line 6 imports Analysis and analyze from .pipeline. __all__ lists those three names. Importing the package therefore imports pipeline.py, which imports breathing, phase, preprocess and swallow, and through them kman_host.session_io.

**Notes.** kman_host must be importable. report.py and the tests handle this by putting host/ and analysis/ on sys.path.

### `analysis/kman_analysis/breathing.py`

#### `Breathing`
*dataclass* · [`analysis/kman_analysis/breathing.py:31`](../analysis/kman_analysis/breathing.py#L31)

```python
@dataclass class Breathing(fs, t, wave_deg, ref_pp_deg, breath_t, breath_amp_deg, breath_valid, rate_t, rate_bpm, since_last_s, sign_flipped)
```

**Purpose.** Holds the result of breathing analysis for one IMU (chest or abdomen).

**How it works.**

Fields:
- fs: float, waveform rate resp_fs = u.fs / q (20 Hz at 100 Hz frames).
- t: seconds on the waveform grid, arange(len(wave))/fs.
- wave_deg: respiratory waveform in degrees, oriented so inspiration rises.
- ref_pp_deg: per-sample reference peak-to-peak amplitude (2 x rolling quantile) behind the prominence threshold.
- breath_t: times in seconds of the detected peaks (end-inspiration).
- breath_amp_deg: the scipy prominence of each peak, in degrees.
- breath_valid: bool per breath, False if the peak sample falls in motion or a gap.
- rate_t: time of each rate value, which is the second breath of the interval.
- rate_bpm: 60 / interval, only for clean intervals of at most max_rate_interval_s.
- since_last_s: on the full-rate grid (u.n samples at u.fs), seconds since the latest valid breath, NaN before the first.
- sign_flipped: True if orient_inspiration_up negated the waveform.

**Notes.** Consumers include pipeline.Analysis.chest/abd, per_label_summary, quality (sign_flipped), plotting.make_figure (wave_deg, breath_t, breath_valid, rate_t/rate_bpm, since_last_s) and report.py, which writes the breaths CSV. ref_pp_deg is not read outside this module in 1.0.0.

#### `gravity_complementary`
*function* · [`analysis/kman_analysis/breathing.py:45`](../analysis/kman_analysis/breathing.py#L45)

```python
gravity_complementary(acc_g: np.ndarray, gyro_dps: np.ndarray, fs: float, tau_s: float) -> np.ndarray
```

**Purpose.** Estimates the unit gravity direction in the sensor frame, fusing gyro integration (short term) with the normalised accelerometer (long term), so tilt can be tracked without Euler-angle singularities.

**How it works.**

Setup: dt = 1/fs, alpha = tau_s/(tau_s + dt), beta = 1 - alpha = dt/(tau_s + dt). With tau = 1.0 s at 100 Hz, alpha ≈ 0.990099. Accelerometer norms of 0 are replaced by 1, and a = acc_g/|acc_g| row-wise. w = radians(gyro_dps) \* dt is the rotation increment in rad per sample. Both are converted to Python lists for a fast scalar loop. g starts at a[0].
For each sample k:
1. Prediction, a first-order integration of dg/dt = -omega x g: g_pred = g - (w_k x g), with
   px = gx - (wy\*gz - wz\*gy)
   py = gy - (wz\*gx - wx\*gz)
   pz = gz - (wx\*gy - wy\*gx)
2. Correction: g = alpha \* g_pred + beta \* a_k.
3. Renormalise: g /= sqrt(gx² + gy² + gz²); out[k] = g.
Returns an (n, 3) array of unit vectors.

**Notes.** The crossover is about 1/(2\*pi\*tau) Hz (≈0.16 Hz at tau = 1 s): the gyro supplies content above it and the accelerometer below. The module docstring explains that the gyro path keeps step accelerations out of the tilt estimate during walking. Gyro bias must be removed by the caller (respiratory_waveform does this). Renormalising every step removes the norm growth of the first-order rotation. Tested in analysis/tests/test_analysis.py::test_complementary_filter_tracks_rotation (5 deg, 0.3 Hz rotation at 100 Hz; error &lt; 0.5 deg after 2 s).

#### `bandpass`
*function* · [`analysis/kman_analysis/breathing.py:79`](../analysis/kman_analysis/breathing.py#L79)

```python
bandpass(x: np.ndarray, fs: float, band: tuple[float, float], order: int) -> np.ndarray
```

**Purpose.** Applies a zero-phase Butterworth band-pass along axis 0, with edge padding long enough for the 0.05 Hz corner to settle.

**How it works.**

1. sos = signal.butter(order, band, btype="bandpass", fs=fs, output="sos"). With order = 2 this is a 4th-order band-pass, and forward-backward filtering squares its magnitude response.
2. Subtracts the per-column median: x - np.median(x, axis=0).
3. padlen = min(len(x) - 1, int(3 \* fs / band[0])), i.e. three periods of the low corner: 3\*20/0.05 = 1200 samples = 60 s at 20 Hz.
4. Returns signal.sosfiltfilt(sos, x, axis=0, padtype="even", padlen=padlen).

**Notes.** Per the docstring, a 0.05 Hz corner rings for about 20 s, and scipy's default short pad would leave roughly 30 s of edge transient that swamps the breathing. Removing the median before even extension reduces the step at the edges.

#### `windowed_pca`
*function* · [`analysis/kman_analysis/breathing.py:89`](../analysis/kman_analysis/breathing.py#L89)

```python
windowed_pca(x: np.ndarray, fs: float, window_s: float, weight: np.ndarray | None = None) -> np.ndarray
```

**Purpose.** Reduces an (n, d) signal (the 3-D band-passed gravity vector) to 1-D by projecting onto a slowly varying first principal axis. The sign is kept continuous across windows, and the windows are blended by Hann overlap-add.

**How it works.**

1. L = max(8, int(window_s \* fs)), which is 600 samples (30 s) at 20 Hz.
2. Window starts: if n &lt;= L there is one window, starts = [0] with L = n. Otherwise hop = L // 2 and starts = range(0, n - L + 1, hop); if starts[-1] + L &lt; n, a final window starting at n - L is appended, flush with the end and overlapping its predecessor by more than 50 %.
3. win = np.hanning(L + 2)[1:-1]: Hann without its zero endpoints, so every sample has positive weight.
4. For each start s, with seg = x[s:s+L]:
   a. use = seg. If weight is given and good = weight[s:s+L] > 0 has good.sum() >= 0.3\*L, then use = seg[good], so the covariance comes from clean samples only. Otherwise all samples are used.
   b. c = np.cov((use - use.mean(0)).T); np.linalg.eigh(c) returns eigenvectors in ascending eigenvalue order; v = vecs[:, -1] (PC1).
   c. Sign continuity: if a previous window exists and v·prev &lt; 0, v = -v. Then prev = v.
   d. Overlap-add: y[s:s+L] += win \* ((seg - seg.mean(0)) @ v) and wsum[s:s+L] += win. The projection uses the whole segment, centred on its full mean.
5. Returns y / np.maximum(wsum, 1e-9), the Hann-weighted average of the overlapping projections.

**Notes.** Sign continuity only aligns each window with the one before it, so the global sign is arbitrary; orient_inspiration_up fixes it. respiratory_waveform passes weight = clean (not motion and not gap).

#### `motion_energy_mg`
*function* · [`analysis/kman_analysis/breathing.py:128`](../analysis/kman_analysis/breathing.py#L128)

```python
motion_energy_mg(acc_g: np.ndarray, fs: float, cfg: dict) -> np.ndarray
```

**Purpose.** Computes the chest-accelerometer high-frequency RMS in mg. pipeline.analyze thresholds it into the motion-artifact mask.

**How it works.**

1. m = cfg["motion"]; sos = signal.butter(4, m["highpass_hz"], btype="highpass", fs=fs, output="sos"), a 4th-order high-pass at 1.5 Hz.
2. hp = signal.sosfiltfilt(sos, acc_g, axis=0) (zero phase, scipy default padding).
3. e = sum(hp**2, axis=1), the squared vector magnitude in g².
4. win = max(1, int(m["window_s"] \* fs)) (200 samples at 100 Hz); ms = pandas rolling(win, center=True, min_periods=1).mean() of e.
5. Returns sqrt(ms) \* 1000.0 as the RMS in mg, at full frame rate.

**Notes.** The function does not apply threshold_mg. pipeline.analyze computes motion = motion_mg > cfg["motion"]["threshold_mg"] from the chest IMU only, and the same mask is used for the abdomen breathing.

#### `respiratory_waveform`
*function* · [`analysis/kman_analysis/breathing.py:138`](../analysis/kman_analysis/breathing.py#L138)

```python
respiratory_waveform(u: Uniform, prefix: str, cfg: dict, calib: np.ndarray, motion: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]
```

**Purpose.** Produces the 1-D respiratory tilt waveform in degrees at resp_fs for the IMU with channel prefix 'chest' or 'abd'.

**How it works.**

1. b = cfg["breathing"]; acc = u.vec(prefix, "a"), gyr = u.vec(prefix, "g").
2. Gravity direction:
   - If the gyro is present: nb = int(gyro_bias_s \* u.fs); cal_idx = first nb indices where calib is True. bias = median(gyr[cal_idx], axis=0) if any exist, else median(gyr[:nb], axis=0) (the session start). Then g = gravity_complementary(acc, gyr - bias, u.fs, comp_filter_tau_s).
   - Otherwise: g = acc / |acc| (accelerometer only, with no zero-norm guard).
3. q = max(1, round(u.fs / resp_fs_hz)) (5 at 100 Hz); rfs = u.fs / q.
4. gd = signal.decimate(g, q, ftype="fir", axis=0, zero_phase=True) if q > 1, else g. This is an anti-alias FIR followed by keeping every q-th sample; the length is ceil(n/q).
5. tilt = bandpass(gd, rfs, tuple(band_hz), filter_order).
6. clean = ~motion[::q][:len(gd)] & ~u.gap[::q][:len(gd)]. The masks are decimated by plain striding, not OR-ed over the q samples.
7. wave = np.degrees(windowed_pca(tilt, rfs, pca_window_s, weight=clean.astype(float))).
8. t = arange(len(wave)) / rfs. Returns (t, wave, rfs).

**Notes.** The PCA projects changes in a unit vector, which are dimensionless. For small tilts a change of Δ is about Δ rad, so np.degrees gives approximately degrees of tilt.

#### `orient_inspiration_up`
*function* · [`analysis/kman_analysis/breathing.py:162`](../analysis/kman_analysis/breathing.py#L162)

```python
orient_inspiration_up(wave: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, bool]
```

**Purpose.** Sets the global sign of the waveform so that inspiration (the faster phase) rises and peaks mark end-inspiration.

**How it works.**

1. d = np.diff(wave)[mask[1:]] if mask[1:].sum() > 50 (more than 50 calibration samples, 2.5 s at 20 Hz). Otherwise d = np.diff(wave) over the whole recording.
2. If len(d) &lt; 10 or d.std() == 0, it returns (wave, False).
3. skew = mean(((d - d.mean()) / d.std())**3), the population skewness of the derivative.
4. Returns (-wave, True) if skew &lt; 0, otherwise (wave, False).

**Notes.** Rationale from the docstring: inspiration is shorter than expiration, so its slope is steeper, and the derivative is positively skewed when inspiration points up. analyze_breathing passes the decimated calibration mask, so mask[1:] lines up with diff index i (wave[i+1] - wave[i]).

#### `detect_breaths`
*function* · [`analysis/kman_analysis/breathing.py:172`](../analysis/kman_analysis/breathing.py#L172)

```python
detect_breaths(t: np.ndarray, wave: np.ndarray, fs: float, invalid: np.ndarray, cfg: dict, n_full: int, fs_full: float) -> Breathing
```

**Purpose.** Detects breaths as waveform peaks with an adaptive prominence threshold, derives breath-to-breath rate from clean intervals only, and computes seconds since the last valid breath on the full-rate grid.

**How it works.**

1. p = cfg["breathing"]["peaks"]; win = max(3, int(ref_window_s \* fs)) (1200 samples = 60 s at 20 Hz).
2. Reference amplitude from clean samples: mag = where(invalid, NaN, |wave|). ref = pandas rolling(win, center=True, min_periods=win // 4).quantile(ref_quantile = 0.8); NaNs are skipped and need at least win/4 non-NaN samples. ref_pp = 2.0 \* ref.bfill().ffill(), the reference peak-to-peak amplitude in degrees.
3. Adaptive prominence, per sample: prom_min = np.maximum(abs_min_prominence_deg, rel_prominence \* ref_pp), i.e. max(0.08 deg, 0.3 \* 2 \* Q0.8(|wave| over the clean samples of a centred 60 s window)).
4. peaks, props = signal.find_peaks(wave, prominence=(prom_min, None), distance=max(1, int(min_interval_s \* fs))) (30 samples = 1.5 s). scipy evaluates the array threshold at each peak's own index. bt = t[peaks]; amp = props["prominences"]; valid = ~invalid[peaks] (empty bool array if there are no peaks).
5. Clean-interval rate rule: iv = diff(bt). ci = [0, cumsum(invalid)], so ci[j] counts the invalid samples before index j. clean = (ci[peaks[1:] + 1] - ci[peaks[:-1]]) == 0 means no invalid sample in the closed range [peak_i, peak_{i+1}], ends included. keep = clean & (iv &lt;= max_rate_interval_s) (15 s). rate_t = bt[1:][keep]; rate_bpm = 60.0 / iv[keep].
6. Since-last: vt = bt[valid]; tf = arange(n_full) / fs_full; since = NaN array. If vt is non-empty: k = searchsorted(vt, tf, side="right") - 1, and for k >= 0, since = tf - vt[k].
7. Returns Breathing(..., ref_pp_deg=ref_pp, sign_flipped=False). The caller sets sign_flipped.

**Notes.** The code comments give the rationale. The reference uses clean samples only, so walking sway or coughs do not raise the threshold for nearby breathing. The clean-interval rule prevents a breath hidden by an artifact or gap from producing one double-length interval, which would show as half the rate. Breaths inside invalid regions are kept but marked invalid. scipy's find_peaks applies distance before prominence. If no sample is valid, ref_pp is NaN everywhere; np.maximum propagates NaN, so no peak passes. since_last_s keeps growing through invalid stretches. host/tests/test_v1_fixes.py calls this function directly.

#### `analyze_breathing`
*function* · [`analysis/kman_analysis/breathing.py:208`](../analysis/kman_analysis/breathing.py#L208)

```python
analyze_breathing(u: Uniform, prefix: str, cfg: dict, calib: np.ndarray, motion: np.ndarray) -> Breathing
```

**Purpose.** Runs the full breathing pipeline for one IMU: waveform, orientation, then breath detection.

**How it works.**

1. t, wave, rfs = respiratory_waveform(u, prefix, cfg, calib, motion).
2. q = int(round(u.fs / rfs)); cal_r = calib[::q][:len(wave)].
3. wave, flipped = orient_inspiration_up(wave, cal_r).
4. invalid = (motion[::q] | u.gap[::q])[:len(wave)], decimated by striding.
5. br = detect_breaths(t, wave, rfs, invalid, cfg, u.n, u.fs); br.sign_flipped = flipped; returns br.

**Notes.** pipeline.analyze calls this with prefix "chest" when chest accel exists and with "abd" when abdomen accel exists. The motion mask always comes from the chest accelerometer.

### `analysis/kman_analysis/config.py`

#### `DEFAULT_PATH`
*constant* · [`analysis/kman_analysis/config.py:10`](../analysis/kman_analysis/config.py#L10)

```python
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
```

**Purpose.** Absolute path of the default configuration file.

**How it works.** It resolves config.py's own path and takes parents[1]: kman_analysis/ is parents[0], so parents[1] is analysis/. It then appends config.yaml, giving /home/user/KMAN/analysis/config.yaml in this checkout. It does not depend on the current working directory.

#### `_merge`
*function* · [`analysis/kman_analysis/config.py:13`](../analysis/kman_analysis/config.py#L13)

```python
_merge(base: dict, over: dict) -> dict
```

**Purpose.** Recursively merges an override mapping into a copy of a base config, rejecting unknown keys.

**How it works.**

1. out = copy.deepcopy(base), so base is never mutated.
2. For each (k, v) in over (`over or {}`, so None is a no-op):
   a. If v is a dict and out.get(k) is a dict, it recurses: out[k] = _merge(out[k], v). Nested sections such as breathing.peaks or swallow.head_imu are therefore merged key by key.
   b. Otherwise, if k is not in out, it raises KeyError(f"unknown config key: {k}"). If k is in out, out[k] = v replaces the value wholesale (lists such as band_hz are replaced, not merged).
3. Returns out.

**Notes.** There is no type checking. A scalar override replaces a whole dict section, and a dict replaces a scalar, as long as the key exists. Unknown keys raise at any depth. analysis/tests/test_analysis.py::test_config_rejects_unknown_keys checks this with overrides {"swallow": {"k_onn": 3}}.

#### `load_config`
*function* · [`analysis/kman_analysis/config.py:25`](../analysis/kman_analysis/config.py#L25)

```python
load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict
```

**Purpose.** Builds the effective analysis config: defaults, then an optional user YAML file, then optional in-code overrides.

**How it works.**

1. Opens DEFAULT_PATH as UTF-8 and parses it with yaml.safe_load into cfg.
2. If path is truthy, it opens that file, parses it with yaml.safe_load (`or {}` makes an empty file a no-op) and sets cfg = _merge(cfg, user).
3. If overrides is truthy, cfg = _merge(cfg, overrides).
4. Returns the plain nested dict. Precedence is defaults &lt; file &lt; overrides.

**Notes.** Nothing is cached: every call re-reads the defaults from disk and returns a fresh dict. Unknown keys in the file or the overrides raise KeyError (from _merge). report.py main calls load_config(args.config). The tests call load_config() and load_config(overrides=...).

### `analysis/kman_analysis/preprocess.py`

#### `Uniform`
*dataclass* · [`analysis/kman_analysis/preprocess.py:21`](../analysis/kman_analysis/preprocess.py#L21)

```python
@dataclass class Uniform(fs: float, t: np.ndarray, label: np.ndarray, gap: np.ndarray, ch: dict[str, np.ndarray], label_names: dict[int, str], slots: dict[str, bool] = {})
```

**Purpose.** Holds a session resampled onto one uniform tick grid, the common input of every analysis stage.

**How it works.**

Fields (all arrays have length n = number of ticks from the first to the last received seq):
- fs: frame rate in Hz (Session.fs = header sample_rate_hz).
- t: seconds from the first sample, np.arange(n)/fs.
- label: int64 label per tick, forward-filled over lost frames.
- gap: bool per tick, True inside any hole longer than max_interp_gap_s, whether lost frames or invalid samples on any channel.
- ch: channel name -> float64 array in physical units (raw \* Channel.scale: g, dps, counts), with holes filled by _fill. Names follow the header, e.g. chest_ax..chest_az, chest_gx..chest_gz, abd_a\*, neck_a\*, flex_raw.
- label_names: label id -> name from the session.
- slots: slot presence for chest_imu, abdomen_imu, neck_flex and neck_imu (default_factory=dict).

**Notes.** No code under analysis/ reads slots in 1.0.0. Stages check for channels with vec() or `"flex_raw" in u.ch` instead.

#### `Uniform.n`
*property* · [`analysis/kman_analysis/preprocess.py:31`](../analysis/kman_analysis/preprocess.py#L31)

```python
@property n(self) -> int
```

**Purpose.** Number of samples on the uniform grid.

**How it works.** Returns len(self.t).

#### `Uniform.vec`
*method* · [`analysis/kman_analysis/preprocess.py:34`](../analysis/kman_analysis/preprocess.py#L34)

```python
vec(self, prefix: str, kind: str = "a") -> np.ndarray | None
```

**Purpose.** Returns a 3-axis sensor signal as an (n, 3) array, or None if the sensor is absent.

**How it works.** Builds the names [f"{prefix}_{kind}x", f"{prefix}_{kind}y", f"{prefix}_{kind}z"] (for example chest_ax/ay/az for kind "a", chest_gx/gy/gz for kind "g"). If all three are keys of self.ch, it returns np.stack([...], axis=1) with shape (n, 3). Otherwise it returns None.

**Notes.** Callers use the None result as the presence test for an IMU or its gyro: pipeline.analyze ("chest", "abd"), breathing.respiratory_waveform, and swallow.head_pose_change / analyze_neck ("neck").

#### `_fill`
*function* · [`analysis/kman_analysis/preprocess.py:41`](../analysis/kman_analysis/preprocess.py#L41)

```python
_fill(x: np.ndarray) -> np.ndarray
```

**Purpose.** Fills NaN holes in a 1-D signal so uniform-sampling filters can run.

**How it works.**

1. bad = np.isnan(x).
2. If nothing is bad, it returns x itself (no copy).
3. If everything is bad, it returns np.zeros_like(x).
4. Otherwise it copies x and sets x[bad] = np.interp(idx[bad], idx[~bad], x[~bad]) with idx = arange(len(x)). This interpolates linearly between valid neighbours. Because np.interp clamps outside the valid range, leading and trailing NaNs take the first or last valid value.

**Notes.** Called only by to_uniform, once per channel.

#### `_runs`
*function* · [`analysis/kman_analysis/preprocess.py:54`](../analysis/kman_analysis/preprocess.py#L54)

```python
_runs(mask: np.ndarray) -> list[tuple[int, int]]
```

**Purpose.** Finds the runs of True in a boolean mask, as half-open index ranges.

**How it works.** If mask.any() is False, it returns []. Otherwise d = np.diff(np.r_[0, mask.astype(np.int8), 0]): starts are np.flatnonzero(d == 1) and ends are np.flatnonzero(d == -1). It returns list(zip(starts, ends)), so each (a, b) covers mask[a:b] with b exclusive.

**Notes.** The indices are numpy integers. Padding with 0 at both ends closes runs that touch the array edges.

#### `to_uniform`
*function* · [`analysis/kman_analysis/preprocess.py:62`](../analysis/kman_analysis/preprocess.py#L62)

```python
to_uniform(session: Session, cfg: dict) -> Uniform
```

**Purpose.** Places every recorded frame on the tick grid given by its sequence number, converts to physical units, fills holes and builds the long-gap mask.

**How it works.**

1. df = session.df, fs = session.fs. idx = seq - seq[0] (int64), where seq is the unwrapped 64-bit sequence number written by the host decoder (kman_host.stream.StreamDecoder). n = idx[-1] + 1. present[idx] = True.
2. Labels: label = full(n, -1), then label[idx] = df.label. Forward fill: pos = where(label >= 0, arange(n), 0); np.maximum.accumulate(pos) gives the index of the latest real label; label = label[pos]. Tick 0 always holds a real frame.
3. max_gap = round(preprocess.max_interp_gap_s \* fs). For each run (a, b) of ~present (lost frames) with b - a > max_gap, it sets gap[a:b] = True.
4. For each Channel c in session.header.channels:
   - x = NaN array; v = df[c.name] as float64 \* c.scale.
   - ok = (df.valid & (1 &lt;&lt; c.bit)) != 0 (the device's per-frame slot-valid bitmask); x[idx[ok]] = v[ok].
   - Every NaN run, whether a lost frame or an invalid sample, longer than max_gap also sets gap[a:b] = True.
   - ch[c.name] = _fill(x).
5. slots = {s: session.has_slot(s)} for chest_imu, abdomen_imu, neck_flex and neck_imu (has_slot means header status == "ok").
6. Returns Uniform(fs, t=arange(n)/fs, label, gap, ch, session.label_names, slots).

**Notes.** Time comes from the tick index and the nominal fs, not from t_us. gap is a single mask shared by all channels: a long invalid stretch on any one slot (for example the neck flex) masks breaths and events in every stage. If a header channel is never valid, the whole session is marked gap. The code assumes df is ordered by seq, with idx[-1] the largest.

#### `calibration_mask`
*function* · [`analysis/kman_analysis/preprocess.py:101`](../analysis/kman_analysis/preprocess.py#L101)

```python
calibration_mask(u: Uniform, cfg: dict) -> np.ndarray
```

**Purpose.** Selects the per-session quiet reference samples. They are used for the gyro bias, the inspiration-up sign choice, the abdomen polarity check and envelope medians in phase, the neck-flex sigma_cal, and the reported calibration duration.

**How it works.**

1. c = cfg["calibration"]; m = (u.label == c["label"]) & ~u.gap.
2. If m.sum() &lt; 5 \* u.fs (less than 5 s), it falls back to m = (u.t &lt; c["fallback_first_s"]) & ~u.gap.
3. limit = int(c["max_s"] \* u.fs). If m.sum() > limit, cut = np.flatnonzero(m)[limit] and m[cut:] = False. This keeps the first max_s seconds' worth of mask samples, even when they span several label segments.
4. Returns the bool array of length u.n.

**Notes.** Motion artifacts are not excluded here, only long gaps. Consumers include breathing.respiratory_waveform (gyro bias), analyze_breathing (sign orientation), pipeline.analyze -> phase.chest_abdomen_phase, swallow.analyze_neck (sigma_cal) and pipeline.quality (calibration_s).

#### `runs`
*function* · [`analysis/kman_analysis/preprocess.py:114`](../analysis/kman_analysis/preprocess.py#L114)

```python
runs(mask: np.ndarray) -> list[tuple[int, int]]
```

**Purpose.** Public wrapper around _runs for other modules.

**How it works.** Returns _runs(mask): a list of half-open [start, end) index pairs of True runs.

**Notes.** Used by swallow.analyze_neck (hysteresis candidates), pipeline._max_breath_gap (label segments), and plotting._bands / _artifact_spans.

## 16. Analysis: phase, neck events, pipeline, plotting, report

- **`analysis/kman_analysis/phase.py`**: Computes the chest-abdomen phase difference per sample from the Hilbert analytic signals of the two breathing waveforms, as a windowed circular mean with a coherence value. It gates each sample for validity (the sample itself must be clean, plus amplitude, coherence and clean-fraction-of-window tests) and flags paradoxical samples where the absolute phase exceeds a threshold. It also applies a per-session abdomen polarity calibration against the quiet reference segment.
- **`analysis/kman_analysis/swallow.py`**: Detects neck flex-sensor events as deviations from a centred moving-median baseline, using hysteresis thresholds scaled by a robust calibration sigma. It classifies each event as 'swallow' or 'head_motion' by peak amplitude, duration and an optional neck-IMU tilt/yaw test, and produces per-sample rolling event-rate series. Head orientation change over an event is estimated by head_pose_change.
- **`analysis/kman_analysis/pipeline.py`**: Owns the top-level analysis of one session: resamples to a uniform grid, derives the chest-motion artifact mask, runs breathing analysis for chest and abdomen, then phase and neck analysis, and builds the per-label summary table and the QA dictionary. The Analysis dataclass is the single result object consumed by the CLI and the plotting code.
- **`analysis/kman_analysis/plotting.py`**: Renders the one-page session report as a matplotlib Figure: stacked panels on a shared time axis, label background bands, artifact hatching, legends, and a per-label summary table. It fixes a neutral ink palette (label colours come from kman_host.labels) and forces the Agg backend at import. It returns the figure and does not save or show it.
- **`analysis/report.py`**: Command-line entry point (python analysis/report.py SESSION.parquet, optional --config, --out-dir, --start/--end, --show). It loads the config and session and runs analyze. It writes a PNG and a per-label summary CSV, plus a breaths CSV when the chest analysis exists and a neck-events CSV when the neck analysis exists, all with float_format '%.4g'. It prints the session header, the QA dictionary, the per-label table, a frame-loss warning when lost_pct >= 0.1, and the PNG path. At import it inserts the host and analysis source roots into sys.path so kman_host and kman_analysis import without installation.

**How it connects.** Upstream, analysis/report.py main() is the CLI caller. It calls load_config (analysis/kman_analysis/config.py), kman_host.session_io.load_session, pipeline.analyze (re-exported by kman_analysis/__init__.py together with Analysis and load_config) and plotting.make_figure. It then reads Analysis.summary, the Analysis.chest breath arrays and Analysis.neck.events to write the CSVs, and Analysis.qa to print the quality report. The test suite calls analyze in analysis/tests/test_analysis.py, and host/tests/test_v1_fixes.py calls chest_abdomen_phase directly (line 413). Downstream, pipeline.analyze calls preprocess.to_uniform and preprocess.calibration_mask for the uniform grid and the quiet-reference mask. It calls breathing.motion_energy_mg for the chest motion energy and breathing.analyze_breathing twice (prefixes 'chest' and 'abd', both with the chest-derived motion mask). It then calls phase.chest_abdomen_phase and swallow.analyze_neck, and finally per_label_summary and quality. analyze_neck calls preprocess.runs (hysteresis candidates), Uniform.vec, _robust_std and swallow.head_pose_change, which also uses Uniform.vec. per_label_summary calls _max_breath_gap, which calls preprocess.runs, and _circmean_deg. quality reads Session.stats, Session.df and the Uniform fields. make_figure reads the Analysis fields, the qa dictionary and the summary DataFrame, uses the kman_host.labels palette (LABEL_COLORS_LIGHT, HATCHED_LABELS), and calls _style, _title, _bands, _artifact_spans and the nested fmt. _bands and _artifact_spans are the plotting functions that call preprocess.runs.

### `analysis/kman_analysis/phase.py`

#### `Phase`
*dataclass* · [`analysis/kman_analysis/phase.py:21`](../analysis/kman_analysis/phase.py#L21)

```python
@dataclass class Phase: t: np.ndarray, phase_deg: np.ndarray, coherence: np.ndarray, valid: np.ndarray, paradox: np.ndarray, abd_flipped: bool
```

**Purpose.** Container for the per-sample output of chest_abdomen_phase: the chest-minus-abdomen phase angle, its coherence, and the validity and paradox flags, all on the breathing-waveform time base.

**How it works.** Dataclass with no defaults and no methods; the generated __init__ takes the six fields in this order. t: time in seconds of each sample (the caller passes chest.t[:n], the breathing grid at chest.fs). phase_deg: float array, windowed circular mean of (chest phase minus abdomen phase) in degrees, range (-180, 180]; computed at every sample, valid or not. coherence: float array in 0..1, resultant length |z| of the clean unit phasors in the window. valid: bool array, True where the gate in chest_abdomen_phase passes. paradox: bool array, valid AND abs(phase_deg) > cfg['phase']['paradox_deg']. abd_flipped: bool, True if the abdomen waveform was negated by the polarity calibration.

**Notes.** All arrays have the same length as t.

#### `chest_abdomen_phase`
*function* · [`analysis/kman_analysis/phase.py:30`](../analysis/kman_analysis/phase.py#L30)

```python
def chest_abdomen_phase(t: np.ndarray, chest: np.ndarray, abd: np.ndarray, fs: float, calib: np.ndarray, invalid: np.ndarray, cfg: dict) -> Phase
```

**Purpose.** Computes the instantaneous chest-abdomen phase difference per sample and marks which samples are usable. About 0 deg is in-phase (normal) breathing; about +-180 deg is paradoxical effort, which indicates continued effort against an obstructed airway. Inputs chest and abd are breathing waveforms in degrees on the grid at fs; invalid is a boolean mask (motion or gap) on that same grid; calib is the quiet-reference mask on that grid.

**How it works.**

1. c = cfg['phase'] (keys window_s, min_amplitude_frac, min_coherence, paradox_deg). flipped = False. 2. Polarity calibration: if calib.sum() > 10 and np.corrcoef(chest[calib], abd[calib])[0,1] &lt; 0, set abd = -abd and flipped = True. A NaN correlation compares False, so no flip occurs. 3. zc = scipy.signal.hilbert(chest), za = hilbert(abd). d = exp(1j\*(angle(zc) - angle(za))) is a unit phasor per sample. 4. ok = ~invalid, the pointwise clean mask. 5. win = max(3, int(window_s\*fs)) samples. Centred rolling sums with min_periods=1 (windows truncated at the record edges): cnt = number of ok samples in the window; re and im = sums of d.real and d.imag over ok samples only (non-ok samples contribute 0). 6. z = (re + 1j\*im) / max(cnt, 1); phase = degrees(angle(z)), range (-180, 180]; coh = abs(z). With cnt == 0, z = 0, so coh = 0. 7. Envelopes ec = abs(zc), ea = abs(za). mc = median(ec[calib]) if calib.sum() > 10, else median(ec) over the whole record; ma is the same for ea. 8. amp_ok pointwise = (ec > min_amplitude_frac\*mc) & (ea > min_amplitude_frac\*ma); then amp_ok is replaced by its centred rolling mean over win (min_periods=1) > 0.5, i.e. a majority of the window has both envelopes above threshold. 9. valid = ok & amp_ok & (coh >= min_coherence) & (cnt >= 0.5\*win). 10. paradox = valid & (abs(phase) > paradox_deg). 11. Return Phase(t, phase, coh, valid, paradox, flipped).

**Notes.** Pointwise clean requirement: step 9 includes ok[i] for the sample itself, so a sample inside a motion artifact or a gap is never valid, even when most of its window is clean. host/tests/test_v1_fixes.py (test_phase_never_valid_on_invalid_samples) checks this. The window-level terms (coherence, cnt, amplitude majority) are additional. phase_deg is still computed for non-clean samples from their clean neighbours; only valid and paradox gate its use. The cnt >= 0.5\*win threshold is absolute. With centred min_periods=1 windows, the first and last samples see only about half a window (at least win/2 samples). A fully clean edge therefore still passes, but a single invalid sample in a truncated edge window makes the edge sample fail, so edges are more fragile than the interior. The Hilbert transform is applied to the whole array and is meaningful only for band-limited waveforms; the caller passes the 0.05-1 Hz breathing waveforms. When calib.sum() &lt;= 10, two fallbacks apply: no polarity calibration is attempted (flipped stays False), and the amplitude reference medians mc and ma are taken over the whole record instead of the calibration samples.

### `analysis/kman_analysis/pipeline.py`

#### `Analysis`
*dataclass* · [`analysis/kman_analysis/pipeline.py:19`](../analysis/kman_analysis/pipeline.py#L19)

```python
@dataclass class Analysis: session: Session, cfg: dict, u: Uniform, calib: np.ndarray, motion_mg: np.ndarray | None, motion: np.ndarray, chest: Breathing | None, abd: Breathing | None, phase: Phase | None, neck: NeckEvents | None, summary: pd.DataFrame, qa: dict
```

**Purpose.** Holds every intermediate and output of the analysis of one session, so the CLI and the plotting code read from one object.

**How it works.** Dataclass with no defaults and no methods; the generated __init__ takes the twelve fields in this order. session: the kman_host.session_io Session, unchanged. cfg: the configuration dict used. u: the Uniform grid version of the session. calib: bool array on the u grid, the quiet-reference mask. motion_mg: float array on the u grid, chest motion energy in mg, or None when there is no chest accelerometer. motion: bool array on the u grid, motion_mg > cfg['motion']['threshold_mg']; all False when there is no chest accelerometer. chest, abd: Breathing or None. phase: Phase or None; None unless both chest and abd exist. neck: NeckEvents or None. summary: DataFrame from per_label_summary. qa: dict from quality.

**Notes.** analyze constructs the object with summary=pd.DataFrame() and qa={} and then fills both, because per_label_summary and quality read the other fields.

#### `analyze`
*function* · [`analysis/kman_analysis/pipeline.py:34`](../analysis/kman_analysis/pipeline.py#L34)

```python
def analyze(session: Session, cfg: dict) -> Analysis
```

**Purpose.** Runs the complete offline analysis of one session and returns an Analysis containing the per-label summary and the QA dictionary. It is the public entry point used by report.py and the tests.

**How it works.**

1. u = to_uniform(session, cfg); calib = calibration_mask(u, cfg). 2. If u.vec('chest','a') exists: motion_mg = motion_energy_mg(acc, u.fs, cfg) (mg); motion = motion_mg > cfg['motion']['threshold_mg']. Otherwise motion_mg = None and motion = zeros(u.n, bool). 3. chest = analyze_breathing(u, 'chest', cfg, calib, motion) if chest accelerometer exists, else None. abd = analyze_breathing(u, 'abd', cfg, calib, motion) if u.vec('abd','a') exists, else None. The abdomen analysis uses the chest-derived motion mask. 4. phase: if chest and abd are both present: q = int(round(u.fs/chest.fs)); n = min(len(chest.wave_deg), len(abd.wave_deg)); inv = (motion[::q] | u.gap[::q])[:n]; phase = chest_abdomen_phase(chest.t[:n], chest.wave_deg[:n], abd.wave_deg[:n], chest.fs, calib[::q][:n], inv, cfg). Otherwise phase = None. 5. neck = analyze_neck(u, cfg, calib). 6. a = Analysis(session, cfg, u, calib, motion_mg, motion, chest, abd, phase, neck, summary=DataFrame(), qa={}); a.summary = per_label_summary(a); a.qa = quality(a); return a.

**Notes.** The [::q] decimation is point sampling: each breathing-grid sample takes the flag of one uniform sample, so a motion or gap run shorter than q uniform samples can fall between kept samples and go undetected. The same decimation is used inside analyze_breathing and for calib. motion_energy_mg (breathing.py) computes a 4th-order Butterworth high-pass at cfg motion.highpass_hz on the three-axis chest acceleration with sosfiltfilt, sums squares across axes, takes a centred rolling mean over cfg motion.window_s, and returns sqrt(mean) \* 1000 in mg.

#### `_circmean_deg`
*function* · [`analysis/kman_analysis/pipeline.py:64`](../analysis/kman_analysis/pipeline.py#L64)

```python
def _circmean_deg(x: np.ndarray) -> float
```

**Purpose.** Circular mean of angles in degrees, so that values near +180 and -180 average to near 180 rather than 0.

**How it works.** Returns float('nan') if len(x) == 0. Otherwise returns float(degrees(angle(mean(exp(1j\*radians(x)))))). The result is in (-180, 180]. Each sample has equal weight.

**Notes.** Used for phase_mean_deg in per_label_summary.

#### `_max_breath_gap`
*function* · [`analysis/kman_analysis/pipeline.py:70`](../analysis/kman_analysis/pipeline.py#L70)

```python
def _max_breath_gap(m: np.ndarray, bad: np.ndarray, valid_breaths_t: np.ndarray, fs: float) -> float
```

**Purpose.** Returns the longest interval without a valid breath inside one label's data, which is the max_breath_gap_s summary value. A large value indicates a breathing dropout.

**How it works.**

1. best = NaN. 2. For each contiguous run [a,b) of the label mask m: skip the run if bad[a:b].mean() > 0.5. Otherwise t0 = a/fs, t1 = b/fs, pts = [t0] + valid breath times in [t0, t1) + [t1], g = max(diff(pts)), and best = g if best is NaN else max(best, g). 3. Return best (NaN if no run qualifies).

**Notes.** bad is the caller's motion | gap mask on the u grid; valid_breaths_t is breath_t[breath_valid] in seconds. Segment edges count as boundaries, so a segment with no breath near its start or end contributes the time from that edge to its first or last breath. Gaps are not joined across separate runs of the same label.

#### `per_label_summary`
*function* · [`analysis/kman_analysis/pipeline.py:84`](../analysis/kman_analysis/pipeline.py#L84)

```python
def per_label_summary(a: Analysis) -> pd.DataFrame
```

**Purpose.** Builds one summary row per label value in u.label, with duration, data-quality, breathing, phase and neck metrics. The CSV and the figure table both come from this DataFrame.

**How it works.** For each lab in sorted(unique(u.label)): m = (u.label == lab) on the u grid; dur = m.sum()/fs (s); clean = m & ~motion & ~gap; clean_min = clean.sum()/fs/60. Columns, in insertion order: (1) label = int(lab). (2) name = u.label_names.get(lab, str(lab)). (3) duration_s = round(dur, 1), all label samples including artifact and gap. (4) artifact_pct = 100\*mean(motion[m]). (5) gap_pct = 100\*mean(u.gap[m]). Only if chest exists: (6) breaths = count of valid breaths whose u-grid sample (round(breath_t\*fs), clipped to [0, n-1]) lies in m. (7) breaths_per_min = breaths / clean_min if clean_min > 10/60 (more than 10 s of clean data), else NaN. (8) rate_median_bpm = median of rate_bpm at rate_t samples lying in m; NaN if none. (9) amp_median_deg = median of breath_amp_deg over the in-label valid breaths; NaN if none. (10) max_breath_gap_s = _max_breath_gap(m, motion | gap, breath_t[breath_valid], fs). Only if phase exists: q = int(round(fs/chest.fs)); mp = m[::q][:len(phase.t)]; v = mp & phase.valid. (11) phase_valid_pct = 100\*v.sum()/max(1, mp.sum()); the denominator includes artifact samples. (12) phase_mean_deg = _circmean_deg(phase_deg[v]). (13) paradox_pct = 100\*(mp & paradox).sum()/max(1, v.sum()) if v.any() else NaN, the share of valid samples that are paradoxical. Only if neck exists: pi = clipped round(peak_s\*fs) for each event; in_lab = m[pi]. (14) swallows = count of in-label events with kind 'swallow'. (15) swallows_per_10s = swallows/dur \* rate_window_s (NaN if dur is 0); uses total label duration, not clean duration. (16) head_events = count of in-label events with kind 'head_motion'. (17) head_events_per_10s = head_events/dur \* rate_window_s.

**Notes.** A column exists only when its analysis exists, and every row has the same set. The make_figure table shows a subset (label, name, duration_s, breaths_per_min, amp_median_deg, max_breath_gap_s, paradox_pct, swallows_per_10s, head_events_per_10s, artifact_pct). The CSV written by report.py contains all columns. The breath rate and the event rates use different denominators: clean minutes for breaths_per_min and total duration for the event rates. The '10s' in the event-rate column names is fixed text; the window is rate_window_s.

#### `quality`
*function* · [`analysis/kman_analysis/pipeline.py:129`](../analysis/kman_analysis/pipeline.py#L129)

```python
def quality(a: Analysis) -> dict
```

**Purpose.** Collects data-quality and device-health values for one session. report.py prints the dictionary, and make_figure uses some of its keys in the title line.

**How it works.** Keys, always present: duration_s = round(u.n/u.fs, 1), the uniform-grid duration. samples = int(len(s.df)), the number of received frame rows. lost_frames = int(stats['stream']['dropped']) if that key exists, else u.n - len(s.df). lost_pct = round(100\*(u.n - len(s.df))/max(1, u.n), 4), missing grid ticks as a percentage, always computed from the grid. crc_errors = int(stats['parser']['crc_errors']), default 0. segments = int(stats['stream']['segments']), default 1. long_gap_pct = round(100\*mean(u.gap), 3). device_overruns = stats['device_status_last']['overruns'] (None if absent, not converted). device_tx_drops = stats['device_status_last']['tx_drops'] (passthrough). hf_crystal = stats['device_status_last']['hfxo'] (passthrough). calibration_s = round(calib.sum()/fs, 1). artifact_pct = round(100\*mean(motion), 2). Added only when the analysis exists: flex_sigma_cal_counts = round(neck.sigma_cal, 2) if neck; chest_sign_flipped = chest.sign_flipped if chest; chest_accel_clipped_pct = round(100\*mean(abs(acc) >= 1.999), 4) if chest, where acc = u.vec('chest','a') is an n x 3 array in g, so the mean is over all axis readings; abd_polarity_flipped = phase.abd_flipped if phase.

**Notes.** lost_frames and lost_pct can disagree when stats['stream']['dropped'] differs from the grid holes; lost_pct is always grid-based. The 1.999 threshold is a literal in the code with no comment; accelerometer values are in g (Uniform.ch units). The device passthrough keys are reported exactly as the device status provides them and are not interpreted here.

### `analysis/kman_analysis/plotting.py`

#### `_style`
*function* · [`analysis/kman_analysis/plotting.py:34`](../analysis/kman_analysis/plotting.py#L34)

```python
def _style(ax, ylabel: str) -> None
```

**Purpose.** Applies the shared panel look to one matplotlib Axes.

**How it works.** Sets the facecolor to SURFACE (#fcfcfb). Turns on the grid with color GRID (#e4e3de), linewidth 0.6. Draws the grid below artists (set_axisbelow True). Hides the top and right spines. Colours the left and bottom spines INK_3 (#8a8984). Sets tick colour INK_2 (#52514e) and tick label size 8. Sets the y-label text in INK_2 at size 8.

**Notes.** Module constants: INK #0b0b0b, INK_2 #52514e, INK_3 #8a8984, GRID #e4e3de, SURFACE #fcfcfb. LEGEND = loc lower right, bbox_to_anchor (1.0, 1.0), fontsize 7, no frame, borderaxespad 0.2, which places legends in the title row.

#### `_title`
*function* · [`analysis/kman_analysis/plotting.py:46`](../analysis/kman_analysis/plotting.py#L46)

```python
def _title(ax, text: str, pad: float = 3) -> None
```

**Purpose.** Sets a left-aligned bold panel title.

**How it works.** ax.set_title(text, loc='left', color=INK (#0b0b0b), fontsize=9.5, fontweight='bold', pad=pad). pad is in points and defaults to 3.

**Notes.** No return value.

#### `_bands`
*function* · [`analysis/kman_analysis/plotting.py:50`](../analysis/kman_analysis/plotting.py#L50)

```python
def _bands(ax, a: Analysis, ts: float, alpha: float, numbers: bool, t_range=None) -> None
```

**Purpose.** Shades each contiguous run of each label as a coloured vertical band behind the data, and optionally writes the label number at the top of each band.

**How it works.**

1. u = a.u. If t_range is given, lo = t_range[0]/ts and hi = t_range[1]/ts; otherwise lo = -inf and hi = +inf. 2. For each unique lab in u.label: skip if lab == 0 or lab is not a key of LABEL_COLORS_LIGHT (kman_host.labels). 3. col = LABEL_COLORS_LIGHT[int(lab)]. For each run (s, e) of (u.label == lab): x0 = s/fs/ts, x1 = e/fs/ts; ax.axvspan(x0, x1, color=col, alpha=alpha, linewidth=0, zorder=0). 4. If lab is in HATCHED_LABELS: a second axvspan with facecolor none, edgecolor INK_3, hatch '///', linewidth 0, zorder 0. 5. xm = (max(x0, lo) + min(x1, hi))/2. If numbers and lo &lt;= xm &lt;= hi: ax.text(xm, 0.97, str(lab)) with x in data coordinates and y in axes fraction (get_xaxis_transform), ha centre, va top, fontsize 7.5, bold, colour INK_2, clip_on=True, zorder=5.

**Notes.** make_figure calls this once per panel with numbers=True only for the first panel. Label 0 is never shaded. Bands are not clipped to t_range by this function; the axis limits clip them. Band colours and hatching come from kman_host.labels.

#### `_artifact_spans`
*function* · [`analysis/kman_analysis/plotting.py:68`](../analysis/kman_analysis/plotting.py#L68)

```python
def _artifact_spans(ax, a: Analysis, ts: float) -> None
```

**Purpose.** Marks motion-artifact and data-gap intervals with hatched spans on the time-series panels.

**How it works.** q_mask = a.motion | a.u.gap. For each run (s, e) of q_mask: ax.axvspan(s/fs/ts, e/fs/ts, facecolor='none', edgecolor=INK_3, hatch='xx', linewidth=0, zorder=0.5).

**Notes.** zorder 0.5 places the hatch above the label bands (zorder 0) and below the data lines (zorder 2 or higher).

#### `make_figure`
*function* · [`analysis/kman_analysis/plotting.py:75`](../analysis/kman_analysis/plotting.py#L75)

```python
def make_figure(a: Analysis, t_range: tuple[float, float] | None = None)
```

**Purpose.** Builds the one-page session figure as a matplotlib Figure: stacked time-series panels on a shared time axis, then a per-label table. It returns the figure and does not save or close it.

**How it works.**

1. rc = cfg['report']; dur = u.n/u.fs. ts = 60.0 if dur > 600 else 1.0; tunit = 'min' or 's' to match. 2. Panels are added in this order, each only if its analysis exists: resp_chest (chest), resp_abd (abd), rate and since (chest), phase (phase), flex and neck_rate (neck), motion (motion_mg is not None). Heights: 1.25 for resp_\* and flex, 0.85 for the others. 3. figsize = (cfg report.width_in, cfg report.panel_height_in \* sum(heights) + 3.2), facecolor SURFACE. GridSpec of len(panels)+1 rows with height_ratios = heights + [1.6] (the last row is the table), hspace 0.42, left 0.05, right 0.985, top 0.95, bottom 0.03. 4. Each panel shares x with the first. _bands(ax, a, ts, report.band_alpha, numbers=(i == 0), t_range). For resp_chest, resp_abd, rate and since, _artifact_spans is also called. 5. Panel contents. resp_chest and resp_abd: _style 'tilt (deg)'; title names the chest or abdomen. Waveform wave_deg vs br.t/ts (INK, linewidth 0.8). Breath markers at breath_t, indexed into wave_deg: valid breaths are filled circles (ms 3.5); invalid breaths are hollow circles (mec INK_2), labelled 'breath during artifact/gap (not counted)'. ylim is +-(nanpercentile(abs(wave_deg), 99.5)\*1.3), or +-1.0 if that is 0. The chest panel adds a legend with a hatched 'motion artifact / gap' patch, ncol 3. rate: _style 'breaths/min'; dots of rate_bpm at rate_t/ts; ylim (0, max(30, nanpercentile(rate_bpm, 99)\*1.15)), or (0, 30) when empty. since: _style 's'; since_last_s vs u.t/ts; ylim bottom 0. phase: _style 'deg'; y = phase_deg where valid else NaN; dashed lines at +-paradox_deg with a text label 'paradox > X' on the right; ylim (-185, 185); yticks -180, -90, 0, 90, 180. flex: _style 'counts'; deviation vs u.t/ts (linewidth 0.7). Threshold lines at +-k_on\*sigma_cal (dashed) and +-head_amplitude_k\*sigma_cal (dotted); the negative side is omitted for polarity 'positive' and the positive side for 'negative'. swallow peaks as filled triangles (ms 6) at (peak_s, peak_dev); head_motion peaks as hollow squares (ms 5.5, mew 1.2). Legend with event counts and threshold entries, ncol 4. ylim is +-(nanpercentile(abs(deviation), 99.8)\*1.25), or +-1.0 if that is 0. neck_rate: _style 'events / W s' with W = rate_window_s; swallow_rate_10s solid INK (linewidth 1.2); head_rate_10s dashed INK_2; legend ncol 2; ylim bottom 0. motion: _style 'mg rms'; semilogy of max(motion_mg, 0.01); dashed line at threshold_mg. 6. Tick labels are hidden on every panel except the last. The last panel gets the xlabel 'time (unit)'. xlim is (0, dur/ts), or (t_range[0]/ts, t_range[1]/ts) when t_range is given. 7. Label legend: one Patch per label in LABEL_COLORS_LIGHT that is present in u.label (alpha 0.5; hatched for HATCHED_LABELS), text 'k name', placed at figure upper right, ncol = min(9, n). 8. Suptitle at x=0.05, left-aligned, bold 11 pt: '{session.name}   |   {duration_s:.0f} s, lost {lost_pct:.3f} %, CRC {crc_errors}, segments {segments}, artifact {artifact_pct:.1f} %'. 9. Table in the last grid row (axis off). cols is a list of (summary key, header, format): label '{:d}' -> 'label'; name '{}' -> 'activity'; duration_s '{:.0f}' -> 'dur s'; breaths_per_min '{:.1f}' -> 'breaths/min'; amp_median_deg '{:.2f}' -> 'amp deg'; max_breath_gap_s '{:.1f}' -> 'max gap s'; paradox_pct '{:.0f}' -> 'paradox %'; swallows_per_10s '{:.2f}' -> 'swallows/10s'; head_events_per_10s '{:.2f}' -> 'head/10s'; artifact_pct '{:.1f}' -> 'artifact %'. Keys missing from summary are skipped. Cells are built with fmt. tax.table(loc upper left, right-aligned cells, horizontal edges only), font size 8, scale(1, 1.15). Every cell edge is GRID; body text is INK and header text is INK_2; column index 1 (activity) is left-aligned. 10. Return fig.

**Notes.** The table omits breaths, rate_median_bpm, gap_pct, phase_valid_pct, phase_mean_deg, swallows and head_events. The table is drawn only when summary has at least one row. The caller must save and close the figure. The module sets matplotlib.use('Agg') at import. The table code assigns cell._loc, a private matplotlib attribute, to left-align column 1. t_range is in seconds.

#### `make_figure.fmt`
*nested function* · [`analysis/kman_analysis/plotting.py:218`](../analysis/kman_analysis/plotting.py#L218)

```python
def fmt(v, f) (nested in make_figure)
```

**Purpose.** Formats one summary cell for the figure table, showing '-' for missing values.

**How it works.** If v is None, or v is a float that is NaN, return '-'. Otherwise return f.format(v). If formatting raises ValueError or TypeError, return str(v).

**Notes.** The ValueError fallback applies when a format spec does not fit the value, for example '{:d}' applied to a float. numpy float64 is a float subclass, so NaN values are detected.

### `analysis/kman_analysis/swallow.py`

#### `NeckEvents`
*dataclass* · [`analysis/kman_analysis/swallow.py:27`](../analysis/kman_analysis/swallow.py#L27)

```python
@dataclass class NeckEvents: t: np.ndarray, baseline: np.ndarray, deviation: np.ndarray, sigma_cal: float, events: pd.DataFrame, swallow_rate_10s: np.ndarray, head_rate_10s: np.ndarray
```

**Purpose.** Container for the outputs of analyze_neck: the flex baseline and deviation, the calibrated sigma, the event table, and two per-sample event-rate series.

**How it works.** Dataclass with no defaults and no methods; the generated __init__ takes the seven fields in this order. t: the uniform time axis (u.t), seconds. baseline: centred moving median of flex_raw in counts. deviation: flex_raw minus baseline in counts, signed. sigma_cal: float, the floored robust sigma (max of sigma_floor_counts and the calibration robust std), in counts. events: pandas DataFrame, one row per event, columns in this order: start_s, end_s, peak_s, duration_s, peak_dev, peak_k, kind, reason, label; an empty frame with these columns when no events exist. swallow_rate_10s: per-sample centred rolling count of 'swallow' events (see analyze_neck.rolling_count). head_rate_10s: the same for 'head_motion'.

**Notes.** The class comment on events lists eight columns and omits label. The field names contain '10s', but the window length is cfg['swallow']['rate_window_s'].

#### `_robust_std`
*function* · [`analysis/kman_analysis/swallow.py:37`](../analysis/kman_analysis/swallow.py#L37)

```python
def _robust_std(x: np.ndarray) -> float
```

**Purpose.** Estimates the standard deviation of roughly Gaussian noise from the median absolute deviation, so that swallow spikes in the calibration segment do not inflate the threshold scale.

**How it works.** Returns float('nan') if len(x) == 0. Otherwise returns 1.4826 \* float(median(abs(x - median(x)))). The factor 1.4826 is the consistency constant that makes the MAD equal sigma for normal data. There is no NaN handling: any NaN in x makes the median NaN, so the result is NaN.

**Notes.** Called only by analyze_neck. Returns 0.0 for a constant input, which analyze_neck then replaces with sigma_floor_counts.

#### `head_pose_change`
*function* · [`analysis/kman_analysis/swallow.py:43`](../analysis/kman_analysis/swallow.py#L43)

```python
def head_pose_change(u: Uniform, a: int, b: int) -> tuple[float, float]
```

**Purpose.** Estimates how much the neck IMU orientation changed over the sample range [a, b), returning a tilt angle and a yaw angle in degrees. analyze_neck uses these to label an event as head motion rather than a swallow.

**How it works.**

1. acc = u.vec('neck', 'a') (n x 3, g). If None, return (0.0, 0.0). 2. sos = butter(2, 2.0 Hz, lowpass, fs=u.fs). seg = sosfiltfilt(sos, acc[a:b], axis=0) if b - a > 15, else the raw acc[a:b]. 3. g = seg / ||seg|| per row, the normalised acceleration direction, which is taken as the gravity direction. 4. ref = mean of g over its first max(1, int(0.2\*u.fs)) rows, normalised to unit length; this is the start of the range, not the event start. 5. tilt = degrees(max over rows of arccos(clip(g @ ref, -1, 1))), the largest angle between any row's direction and ref. 6. yaw = 0.0. If u.vec('neck', 'g') exists (gyro, dps): w_vert = row-sum of gyr[a:b] \* g (rate about the gravity direction, dps); w_vert = w_vert - median(w_vert); yaw = max(abs(cumsum(w_vert) / u.fs)), the largest integrated rotation about gravity in degrees. 7. Return (tilt, yaw).

**Notes.** tilt is the maximum deviation from the initial pose, not a signed net change. yaw uses the raw, unfiltered gyro. The median subtraction removes a constant rate offset only; there is no drift removal. There is no zero-norm guard on seg rows, so a zero-length acceleration vector produces NaN. Only the neck IMU channels are used.

#### `analyze_neck`
*function* · [`analysis/kman_analysis/swallow.py:63`](../analysis/kman_analysis/swallow.py#L63)

```python
def analyze_neck(u: Uniform, cfg: dict, calib: np.ndarray) -> NeckEvents | None
```

**Purpose.** Detects neck flexion events in the flex channel using hysteresis thresholds, classifies each as 'swallow' or 'head_motion', and returns the event table with per-sample rolling counts. Returns None if the session has no flex_raw channel.

**How it works.**

1. If 'flex_raw' not in u.ch, return None. c = cfg['swallow']; x = u.ch['flex_raw'] (counts). 2. Baseline: win = max(3, int(baseline_window_s\*fs) | 1), forced odd; base = centred rolling median of x (min_periods=1); dev = x - base. 3. sigma = max(sigma_floor_counts, _robust_std(dev[calib])) if calib.any(), else max(sigma_floor_counts, _robust_std(dev)). 4. Polarity: 'positive' gives mag = dev; 'negative' gives mag = -dev; any other value gives mag = abs(dev). 5. on = mag > k_on\*sigma (trigger level). above_off = mag > k_off\*sigma (continuation level). 6. Hysteresis: candidates are the maximal runs [a, b) of above_off (preprocess.runs, b exclusive) that contain at least one on sample. Each candidate's extent is therefore set by the k_off crossings; the k_on crossing only qualifies the run. 7. Merge: consecutive candidates [a,b) and [a2,b2) are joined into [a,b2) when a2 - b &lt;= int(merge_gap_s\*fs). The merged run includes the sub-threshold samples between them. 8. For each merged run [a,b): dur = (b-a)/fs, which includes any merged gap. The run is dropped if dur &lt; min_duration_s or if u.gap[a:b] has any True. The motion (artifact) mask is not an input to this function. 9. k = a + argmax(mag[a:b]) (first maximum); peak_k = mag[k]/sigma. Default kind 'swallow', reason ''. 10. Classification, in order: (a) if peak_k > head_amplitude_k, kind = 'head_motion', reason = 'amplitude'; (b) elif dur > max_duration_s, kind = 'head_motion', reason = 'duration'; (c) if the neck accelerometer exists (u.vec('neck','a') is not None, checked once before the loop): margin = int(head_imu.margin_s\*fs); tilt, yaw = head_pose_change(u, max(0, a-margin), min(u.n, b+margin)). If tilt > head_imu.tilt_change_deg or yaw > head_imu.yaw_change_deg, kind = 'head_motion' and reason = 'neck imu (tilt T deg, yaw Y deg)', with both values formatted to 0 decimals. When rule (c) fires, it overwrites the reason set by (a) or (b). 11. Row: start_s = a/fs (the first sample above k_off); end_s = b/fs (the exclusive end, i.e. the sample just after the last above-k_off sample of the merged run); peak_s = k/fs; duration_s = dur; peak_dev = float(dev[k]) (signed counts); peak_k; kind; reason; label = int(u.label[k]). 12. rw = int(rate_window_s\*fs). rolling_count (nested) builds swallow_rate_10s and head_rate_10s. 13. Return NeckEvents(t=u.t, baseline=base, deviation=dev, sigma_cal=float(sigma), events=ev, swallow_rate_10s=..., head_rate_10s=...).

**Notes.** Default classification is 'swallow'; 'head_motion' is assigned by the first matching rule, and rule (c) can replace the reason string of an event that is already head_motion. Events inside a long hole (u.gap) are discarded, not reported. Artifact (motion) samples do not suppress events. sigma_cal is the floored value, so a flat calibration segment yields sigma_floor_counts. If the robust std is NaN, max() returns the floor because a NaN argument never compares greater. The module docstring says events 'start above k_on'; in the code the start boundary is the k_off crossing, and k_on only qualifies the run. The code does not check that k_off &lt;= k_on.

#### `analyze_neck.rolling_count`
*nested function* · [`analysis/kman_analysis/swallow.py:122`](../analysis/kman_analysis/swallow.py#L122)

```python
def rolling_count(kind: str) -> np.ndarray (nested in analyze_neck; closes over u, ev, rw)
```

**Purpose.** Turns the event table into a per-sample centred count of events of one kind, used for swallow_rate_10s and head_rate_10s.

**How it works.** imp = zeros(u.n). For each peak_s of the rows where ev['kind'] == kind, imp[min(u.n - 1, int(round(peak_s\*fs)))] += 1. Returns the centred pandas rolling sum of imp over rw = int(rate_window_s\*fs) samples (min_periods=1) as a numpy array of length u.n.

**Notes.** Each event contributes one count at its peak sample. Two events with the same peak sample add 2. Values at the record edges come from truncated windows.

### `analysis/report.py`

#### `main (report.py)`
*function* · [`analysis/report.py:31`](../analysis/report.py#L31)

```python
def main(argv=None) -> int
```

**Purpose.** Command-line entry point. It analyses one session .parquet and writes the figure, the CSVs and the printed report. Module import also sets up sys.path, and the module-level code runs only when the file is executed as a script.

**How it works.**

1. Argument parser with description = module docstring and RawDescriptionHelpFormatter. Arguments: session (positional); --config; --out-dir (default REPO/reports); --start and --end (float seconds); --show (flag). 2. ap.parse_args(argv). If exactly one of --start and --end is given, ap.error(...) exits with status 2. 3. cfg = load_config(args.config) (YAML merged over analysis/config.yaml); s = load_session(args.session); a = analyze(s, cfg). 4. out.mkdir(parents=True, exist_ok=True). stem = s.name, plus '_{start:g}-{end:g}s' when zooming. 5. make_figure is imported from kman_analysis.plotting here, not at module import. rng = (start, end) when both are given, else None. fig = make_figure(a, rng); fig.savefig(out/'&lt;stem>.png', dpi=cfg report.dpi, facecolor=fig.get_facecolor()). 6. CSVs are written from the full session, regardless of zoom, all with index off and float_format '%.4g': &lt;s.name>_summary.csv from a.summary; if chest exists, &lt;s.name>_breaths.csv with columns t_s (breath_t), amplitude_deg (breath_amp_deg) and valid (breath_valid); if neck exists, &lt;s.name>_neck_events.csv from neck.events. 7. pandas display width 200 and max_columns 40. 8. Prints: 'Session {name}: {header.raw board} fw {header.raw fw_version}, channels {channel names, comma-joined}'; 'Data quality:' followed by json.dumps of qa, with values that have .item() (numpy scalars) converted through it; 'Per-label summary:' followed by summary.round(2) as text without index; 'WARNING: frame loss >= 0.1 % (acceptance limit)' when qa['lost_pct'] >= 0.1; 'Figure: &lt;png path>'. 9. If --show: matplotlib.use('TkAgg', force=True), import pyplot, call make_figure(a, rng) again, and plt.show(). The code does not explicitly close the figure saved in step 5. 10. Return 0. Module import: REPO = Path(__file__).resolve().parents[1]; inserts REPO/host and REPO/analysis at the front of sys.path when absent; imports analyze and load_config from kman_analysis and load_session from kman_host.session_io.

**Notes.** Exit status is 0 on success and 2 on argparse errors. Zoomed runs write a different PNG name, but the CSVs keep the session name and are overwritten, and they still hold full-session data. All CSVs use float_format '%.4g' (4 significant digits). Times in the breaths and neck-events CSVs are therefore rounded to whole seconds between 1000 and 9999 s, and to 10 s above that. The breaths CSV and the neck-events CSV are written only when the chest or neck analysis exists. json.dumps uses default settings, so NaN values print as NaN. The name main is shared with host/logger.py, host/sim_device.py, host/to_csv.py and host/live_view.py, so the file suffix is included.

## 17. Build and release automation

- **`firmware/scripts/make_uf2.py`**: PlatformIO post extra script, loaded by firmware/platformio.ini through extra_scripts = post:scripts/make_uf2.py, that registers an SCons post-action on $BUILD_DIR/firmware.hex. The action converts the Intel HEX to a UF2 image for the nRF52840 family (0xADA52840) with the framework's uf2conv.py and copies the HEX, both named kman-fw-&lt;fw_version>-&lt;PIOENV>. It defines two functions (fw_version, make_uf2), one constant (NRF52840_FAMILY) and one module-level registration call.
- **`.github/workflows/ci.yml`**: GitHub Actions workflow that runs the Python test suite on Python 3.11 and 3.13 and builds the three PlatformIO firmware environments, uploading each environment's UF2 and HEX as an artifact. It runs on branch pushes, pull requests and manual dispatch, and it is also a reusable workflow (workflow_call) that release.yml calls so that a release builds exactly what CI builds. Its two jobs, python and firmware, have no dependency on each other.
- **`.github/workflows/release.yml`**: GitHub Actions workflow that publishes a GitHub Release for a v&lt;version> tag. It is started by pushing a v\* tag or by manual dispatch with a version input. The chain check-version, then build (which reuses ci.yml), then publish verifies that the tag matches the firmware, Python and host version strings and has a CHANGELOG.md section, then attaches the UF2 and HEX files with release notes extracted from CHANGELOG.md.
- **`pyproject.toml`**: Packaging and tooling configuration for the kman distribution at version 1.0.0. It selects the setuptools build backend, lists the runtime dependencies, defines the live and dev optional extras, limits package discovery to kman_host\* and kman_analysis\* under host/ and analysis/, and sets pytest's pythonpath and testpaths. CI installs the project editable with the dev extra, and release.yml reads the version field to compare it with the tag.
- **`requirements.txt`**: Flat pip requirements file listing the seven runtime dependencies, the live GUI packages pyqtgraph and PySide6, and pytest, with the same version floors as pyproject.toml. README.md documents it as the install command for the Python tools. Nothing installs from it in CI; its only automated use is as the default pip-cache key file for actions/setup-python (cache: pip) in the ci.yml python job. It is a manually maintained duplicate of the pyproject.toml dependency lists.

**How it connects.** make_uf2.py has no Python callers in the repository. PlatformIO loads it because firmware/platformio.ini sets extra_scripts = post:scripts/make_uf2.py in the [env] section, so its module body runs once per build of xiao_nrf52840, xiao_nrf52840_abdomen and xiao_nrf52840_neck. The module body calls env.AddPostAction, and SCons then calls make_uf2 after firmware.hex is built. make_uf2 calls fw_version, which reads the KMAN_FW_VERSION define from the text of firmware/include/config.h (value 1.0.0). The same define is emitted as the fw_version key at firmware/src/main.cpp line 112. make_uf2 then runs, through subprocess, the uf2conv.py shipped in the framework-arduinoadafruitnrf52-seeed package, which platformio.ini pins to a commit. Its outputs go to firmware/.pio/build/&lt;env>/, where the ci.yml firmware job uploads them as firmware-&lt;env> artifacts (kman-fw-\*.uf2 and kman-fw-\*.hex). The ci.yml python job installs the kman package from pyproject.toml with pip install -e '.[dev]' and runs pytest against host/tests and analysis/tests, using the testpaths and pythonpath set in pyproject.toml. release.yml does not call fw_version or make_uf2. Its check-version step reads the same three version values with sed (config.h, the pyproject.toml version field, and __version__ at line 3 of host/kman_host/__init__.py) and greps CHANGELOG.md. The build job calls ci.yml through workflow_call, so the python and firmware jobs run inside the release run. The publish job downloads the firmware-\* artifacts into dist/ and attaches dist/\*.uf2 and dist/\*.hex to the GitHub Release, with notes.md built from the CHANGELOG.md section and a footer that links docs/FIRMWARE_GUIDE.md. No workflow installs from requirements.txt. Its only automated use is indirect: the ci.yml python job's actions/setup-python@v5 step (cache: pip, no cache-dependency-path) hashes it by default for the pip cache key. README.md documents it as the manual install command, and it duplicates the dependency lists in pyproject.toml.

### `.github/workflows/ci.yml`

#### `CI workflow`
*workflow* · [`github/workflows/ci.yml:3`](../github/workflows/ci.yml#L3)

```
name: CI; on: push (branches ['**']), pull_request, workflow_dispatch, workflow_call; permissions: contents read
```

**Purpose.** Runs the Python tests and builds all three firmware environments on pushes to any branch, on pull requests and on manual dispatch. The same file is reused by release.yml through workflow_call, so a release builds with the same jobs as CI.

**How it works.**

1. Triggers: push, filtered to branches ['**'], so tag pushes do not match; pull_request; workflow_dispatch; workflow_call with no declared inputs or secrets.
2. The workflow-level permissions block grants contents: read only.
3. Jobs python and firmware have no needs key, so they run in parallel. Each sets runs-on ubuntu-latest and fail-fast: false, so one failing matrix entry does not cancel the others.
4. The run's results are the python job's pass or fail and the firmware-&lt;env> artifacts from the firmware job.

**Notes.** Only the branch filter is set on push, so a tag push does not start this workflow; a tag push reaches it only through release.yml.

#### `job python`
*job* · [`github/workflows/ci.yml:16`](../github/workflows/ci.yml#L16)

```
jobs.python: runs-on ubuntu-latest; strategy matrix python-version ['3.11', '3.13']; fail-fast: false
```

**Purpose.** Installs the kman package with its dev extra and runs the pytest suite on Python 3.11 and 3.13.

**How it works.**

1. actions/checkout@v4.
2. actions/setup-python@v5 with python-version from the matrix and cache: pip.
3. Step 'Install' (lines 29-30): python -m pip install -e '.[dev]'. This installs the project from pyproject.toml in editable mode with pytest from the dev extra. The live extra (pyqtgraph, PySide6) is not installed.
4. Step 'Test' (lines 31-32): python -m pytest -q. Pytest collects host/tests and analysis/tests (pyproject testpaths) with host and analysis placed on sys.path (pyproject pythonpath). The step name notes that the tests decode frames built by the firmware's C++ encoder.

**Notes.** The 3.11 and 3.13 matrix entries are both within requires-python >=3.11 in pyproject.toml. cache: pip has no cache-dependency-path, so actions/setup-python@v5 keys the pip cache on a hash of **/requirements.txt by default, even though the install itself uses pyproject.toml.

#### `job firmware`
*job* · [`github/workflows/ci.yml:34`](../github/workflows/ci.yml#L34)

```
jobs.firmware: runs-on ubuntu-latest; strategy matrix env [xiao_nrf52840, xiao_nrf52840_abdomen, xiao_nrf52840_neck]; fail-fast: false
```

**Purpose.** Builds each PlatformIO firmware environment and uploads its UF2 and HEX outputs as a workflow artifact.

**How it works.**

1. actions/checkout@v4, then actions/setup-python@v5 with Python 3.11.
2. actions/cache@v4 caches ~/.platformio with the key pio-&lt;runner.os>-&lt;hashFiles('firmware/platformio.ini')>. No restore-keys are set, so any change to platformio.ini misses the cache.
3. Step 'Install PlatformIO' (lines 50-52): python -m pip install platformio adafruit-nrfutil. Neither package is version-pinned. The comment in the file says adafruit-nrfutil provides the Python modules the DFU-package step imports.
4. Step 'Build' (lines 53-54): pio run -d firmware -e &lt;env>. The make_uf2 post-action runs at the end of this build.
5. actions/upload-artifact@v4 (lines 55-61): artifact name firmware-&lt;env>; paths firmware/.pio/build/&lt;env>/kman-fw-\*.uf2 and firmware/.pio/build/&lt;env>/kman-fw-\*.hex; if-no-files-found: error, so a build that produces no images fails the job.

**Notes.** The job has no needs key and runs in parallel with python. The per-environment artifact names are what the firmware-\* download pattern in release.yml matches. The three env values match the [env:...] sections in firmware/platformio.ini.

### `.github/workflows/release.yml`

#### `Release workflow`
*workflow* · [`github/workflows/release.yml:5`](../github/workflows/release.yml#L5)

```
name: Release; on: push (tags ['v*']), workflow_dispatch (required input version); workflow env TAG; permissions: contents write
```

**Purpose.** Publishes a GitHub Release for one version tag, with the firmware images attached and the CHANGELOG.md section as the release notes. It is started by pushing a v\* tag or by manual dispatch with a version.

**How it works.**

1. Triggers: a push of a tag matching v\*, or workflow_dispatch with a required input named version.
2. Workflow-level env TAG (line 20): when the event is workflow_dispatch, TAG is 'v' + inputs.version (1.0.0 gives v1.0.0); otherwise TAG is github.ref_name, the name of the pushed tag.
3. permissions: contents: write, which the publish job needs to create the release.
4. Jobs run in a chain: check-version, then build (needs check-version), then publish (needs build).

**Notes.** On dispatch, the input is used as typed. Entering 'v1.0.0' gives TAG 'vv1.0.0'. check-version strips one leading 'v', compares 'v1.0.0' with the version strings, and fails. The version checks are exact string equality, so a suffixed version such as 1.0.0-rc1 passes only when the firmware, pyproject and host version fields hold that identical string. A dispatch run creates the tag only in the publish job, at the commit given by github.sha of the selected branch.

#### `job check-version`
*job* · [`github/workflows/release.yml:23`](../github/workflows/release.yml#L23)

```
jobs.check-version: runs-on ubuntu-latest; steps: actions/checkout@v4, 'Tag matches firmware and package versions'
```

**Purpose.** Stops a release before any build when the tag does not match the versions recorded in the repository.

**How it works.**

1. actions/checkout@v4.
2. Runs the single step described in 'step check-version / Tag matches firmware and package versions' (line 27). build declares needs: check-version and publish declares needs: build, so a failure here stops build directly and publish through it.

**Notes.** The CI workflow does not perform this check; it runs only in the release path.

#### `step check-version / Tag matches firmware and package versions`
*step* · [`github/workflows/release.yml:27`](../github/workflows/release.yml#L27)

```
- name: Tag matches firmware and package versions; run: bash script (lines 28-40)
```

**Purpose.** Verifies that the tag equals the version in firmware/include/config.h, pyproject.toml and host/kman_host/__init__.py, and that CHANGELOG.md has a section for that version.

**How it works.**

1. tag = TAG with one leading 'v' removed (${TAG#v}).
2. fw = sed -n output of the line that starts with '#define KMAN_FW_VERSION "' in firmware/include/config.h. The sed expression is anchored at column 0 and prints only the captured quoted value.
3. py = the same method applied to the line starting with 'version = "' in pyproject.toml.
4. host = the same method applied to the line starting with '__version__ = "' in host/kman_host/__init__.py.
5. Prints one line: tag=... firmware=... python=... kman_host=...
6. fail=0. Three checks, one per line: tag = fw, tag = py, tag = host. Each mismatch prints a ::error:: annotation naming the file and sets fail=1. An empty capture, for example from a missing define, counts as a mismatch.
7. grep -q for a line beginning with '## [' followed by the tag and ']' in CHANGELOG.md. If it is missing, an error annotation is printed and fail=1.
8. exit $fail, so the step fails if any check failed.

**Notes.** Each check has its own line because the default shell is bash -e, where a failing command inside an && list does not stop the script (the comment on line 34 says so). The firmware value is read by sed, separately from fw_version() in make_uf2.py, which uses a whitespace-tolerant regex not anchored to column 0. The comparison is on the version text only; the CHANGELOG heading date and section content are not checked.

#### `job build`
*job* · [`github/workflows/release.yml:42`](../github/workflows/release.yml#L42)

```
jobs.build: needs check-version; uses ./.github/workflows/ci.yml
```

**Purpose.** Runs the CI workflow as a reusable workflow, so the release build consists of the same jobs as CI.

**How it works.**

1. Waits for check-version (needs).
2. uses: ./.github/workflows/ci.yml calls the workflow_call trigger of ci.yml. No inputs or secrets are passed. The called workflow's python and firmware jobs run inside this release run.
3. The job has no steps of its own. The job fails if any called job fails, and publish waits for it (needs: build).

**Notes.** A tag push does not match the branch filter of ci.yml, so this called run is the only CI run for a tag push. The test suite therefore gates the release. The firmware-&lt;env> artifacts produced here are the ones publish downloads.

#### `job publish`
*job* · [`github/workflows/release.yml:46`](../github/workflows/release.yml#L46)

```
jobs.publish: needs build; runs-on ubuntu-latest; steps: actions/checkout@v4, actions/download-artifact@v4, 'Release notes from CHANGELOG.md' (ends with ls -la dist), softprops/action-gh-release@v2
```

**Purpose.** Downloads the firmware artifacts, builds the release notes and creates the GitHub Release with the UF2 and HEX files attached.

**How it works.**

1. actions/checkout@v4, needed for CHANGELOG.md and the notes build.
2. actions/download-artifact@v4 with pattern firmware-\* into path dist with merge-multiple: true. All artifact files are placed flat in dist/, with no per-artifact subdirectories.
3. Step 'Release notes from CHANGELOG.md' (line 56) writes notes.md in the repository root. Its last command, ls -la dist (line 65), lists the downloaded files in the log; it is part of this step, not a step of its own.
4. Step softprops/action-gh-release@v2 (line 66) creates the release.

**Notes.** With three environments, the release has six attachments: three .uf2 and three .hex files. files: dist/\*.uf2 and dist/\*.hex with fail_on_unmatched_files: true make the step fail if either pattern matches nothing.

#### `step publish / Release notes from CHANGELOG.md`
*step* · [`github/workflows/release.yml:56`](../github/workflows/release.yml#L56)

```
- name: Release notes from CHANGELOG.md; run: awk section extraction, sed link rewrite, printf footer, ls -la dist (lines 57-65)
```

**Purpose.** Writes notes.md from the CHANGELOG.md section for this tag. Relative .md links become absolute links to the tagged revision, and a footer links to the flashing guide.

**How it works.**

1. tag = TAG without its leading 'v'. base = https://github.com/&lt;GITHUB_REPOSITORY>/blob/&lt;TAG>.
2. awk -v v=&lt;tag> prints CHANGELOG.md from the line '## [&lt;tag>]' up to the next line beginning with '## ['. The heading line itself is skipped. Subheadings such as '### Added' fall inside the section and are printed.
3. sed -E rewrites each inline markdown link of the form ](...) whose target is an optional './' followed by a path that has no ':' or ')' before a '.md'. The rewritten target is &lt;base>/&lt;path>, and anchors or query text after the .md are kept (that trailing part may contain ':'). Absolute URLs (they have ':' before the .md) and targets that do not contain .md are left unchanged. Reference-style links are not matched.
4. The result is written to notes.md, overwriting any previous file.
5. printf appends a blank line, a line of '---', and the line 'Flashing: see [docs/FIRMWARE_GUIDE.md](https://github.com/&lt;repo>/blob/&lt;TAG>/docs/FIRMWARE_GUIDE.md).'
6. ls -la dist (line 65) lists the downloaded firmware files in the job log.

**Notes.** The step does not check that the section exists; check-version guarantees it. The rewritten links point at the tag, not at a branch. If the section were empty, notes.md would contain only the footer.

#### `step publish / Create GitHub Release`
*step* · [`github/workflows/release.yml:66`](../github/workflows/release.yml#L66)

```
uses: softprops/action-gh-release@v2 with tag_name, target_commitish, name, body_path, files, fail_on_unmatched_files (lines 66-75)
```

**Purpose.** Creates the GitHub Release for TAG with the firmware images attached and notes.md as the body.

**How it works.**

1. tag_name = env.TAG. target_commitish = github.sha, the commit a new tag is created on: the tagged commit for a tag push, or the head of the selected branch for dispatch.
2. name = 'KMAN ' + env.TAG, for example KMAN v1.0.0.
3. body_path = notes.md.
4. files = dist/\*.uf2 and dist/\*.hex.
5. fail_on_unmatched_files: true.

**Notes.** This is the only step in the repository's workflows that writes to the repository, and it depends on the contents: write permission set at workflow level.

### `firmware/scripts/make_uf2.py`

#### `NRF52840_FAMILY`
*constant* · [`firmware/scripts/make_uf2.py:17`](../firmware/scripts/make_uf2.py#L17)

```python
NRF52840_FAMILY = "0xADA52840"
```

**Purpose.** The UF2 family ID for the nRF52840 image, passed to uf2conv.py as the -f argument.

**How it works.** A string constant used unchanged in the uf2conv argument list in make_uf2 ('-f', NRF52840_FAMILY).

**Notes.** The module docstring identifies the family as the one for the XIAO nRF52840 Adafruit bootloader.

#### `fw_version`
*function* · [`firmware/scripts/make_uf2.py:20`](../firmware/scripts/make_uf2.py#L20)

```python
def fw_version() -> str
```

**Purpose.** Returns the firmware version string defined as KMAN_FW_VERSION in firmware/include/config.h. make_uf2 uses it to build the output file names.

**How it works.**

1. cfg = Path(env.subst('$PROJECT_INCLUDE_DIR')) / 'config.h'. The env used here is the module-level SCons environment bound by Import('env') on line 15, not a parameter. The '# noqa: F821' comment silences the undefined-name warning from linters.
2. cfg.read_text() reads the file with the default text encoding.
3. re.search(r'#define\s+KMAN_FW_VERSION\s+"([^"]+)"', text) finds the define anywhere in the file. The pattern is not anchored to column 0 and tolerates any run of whitespace.
4. Returns group(1), the text between the quotes. When there is no match it returns the literal string 'unknown'.

**Notes.** A missing define does not raise an error; the outputs are named kman-fw-unknown-&lt;env>.\*. A missing config.h raises FileNotFoundError from read_text, which is not caught. config.h puts the define inside an #ifndef guard (config.h lines 8-10), and the regex matches it there. The function reads only the text of config.h. If KMAN_FW_VERSION is overridden with -DKMAN_FW_VERSION=... in build_flags, which the guard and platformio.ini allow, the compiled firmware reports the overridden version but the files are still named after the config.h value. The regex does not know about comments, so a commented-out define placed earlier in the file would be matched first. release.yml parses the same define with a stricter sed expression that requires a column-0 match and exactly one space, so a define with leading whitespace passes here and fails the check-version step.

#### `make_uf2`
*function* · [`firmware/scripts/make_uf2.py:26`](../firmware/scripts/make_uf2.py#L26)

```python
def make_uf2(source, target, env)
```

**Purpose.** SCons post-action run after firmware.hex is built. It converts the HEX to a UF2 image for the nRF52840 bootloader family and writes a second copy of the HEX under the versioned name.

**How it works.**

1. hex_path = Path(target[0].get_abspath()), the absolute path of $BUILD_DIR/firmware.hex. The source argument is not used.
2. framework = env.PioPlatform().get_package_dir('framework-arduinoadafruitnrf52-seeed'), the directory of the installed framework package.
3. uf2conv = framework / 'tools' / 'uf2conv' / 'uf2conv.py'.
4. stem = 'kman-fw-' + fw_version() + '-' + $PIOENV, for example kman-fw-1.0.0-xiao_nrf52840.
5. uf2 = hex_path.with_name(stem + '.uf2'), placed in the same build directory as the HEX.
6. subprocess.check_call([$PYTHONEXE, uf2conv, hex_path, '-c', '-f', '0xADA52840', '-o', uf2]). It runs uf2conv with PlatformIO's Python interpreter. -c selects convert-only mode (no flashing), -f sets the UF2 family ID to NRF52840_FAMILY, and -o names the output file. A non-zero exit status raises CalledProcessError and fails the build.
7. shutil.copyfile(hex_path, hex_path.with_name(stem + '.hex')) copies the unmodified HEX for SWD programmers.
8. Prints 'UF2 image: &lt;uf2 path>'. Returns None.

**Notes.** Outputs, all in firmware/.pio/build/&lt;env>/: kman-fw-&lt;version>-&lt;env>.uf2 for drag-and-drop flashing and kman-fw-&lt;version>-&lt;env>.hex for SWD tools. The .hex is a byte copy of firmware.hex, not a new conversion. The family ID 0xADA52840 is the value the module docstring gives for the XIAO nRF52840 bootloader. uf2conv.py comes from the framework revision pinned by platform_packages in firmware/platformio.ini. If get_package_dir yields no path, Path(None) raises TypeError before uf2conv runs. The action runs only when SCons rebuilds firmware.hex.

#### `make_uf2.py module body (env.AddPostAction registration)`
*registration* · [`firmware/scripts/make_uf2.py:38`](../firmware/scripts/make_uf2.py#L38)

```python
env.AddPostAction("$BUILD_DIR/${PROGNAME}.hex", make_uf2)
```

**Purpose.** Registers make_uf2 as the post-action of the firmware.hex target, so the UF2 and HEX step runs after each HEX build.

**How it works.**

1. Line 15, Import('env'), binds the SCons construction environment to the module global env. PlatformIO runs this file as a post extra script (firmware/platformio.ini, [env] section, extra_scripts = post:scripts/make_uf2.py), once for each environment build.
2. Line 17 defines NRF52840_FAMILY (see its entry).
3. Line 38 calls env.AddPostAction with the target string '$BUILD_DIR/${PROGNAME}.hex', which SCons expands to the firmware.hex of the current environment, and the callable make_uf2. SCons invokes the action after it builds that target.

**Notes.** Each of the three PlatformIO environments executes this file and adds its own registration. The '# noqa: F821' comment on line 15 marks Import as a name provided by SCons, not by Python.

### `pyproject.toml`

#### `pyproject.toml [build-system]`
*config* · [`pyproject.toml:1`](../pyproject.toml#L1)

```
[build-system] requires = ['setuptools>=64']; build-backend = 'setuptools.build_meta'
```

**Purpose.** Declares the build backend that pip uses to build or install the kman distribution, including the editable install in CI.

**How it works.** pip installs setuptools>=64 into an isolated build environment, then calls setuptools.build_meta to produce the wheel or the editable install.

#### `pyproject.toml [project]`
*config* · [`pyproject.toml:5`](../pyproject.toml#L5)

```
[project] name = 'kman'; version = '1.0.0'; requires-python = '>=3.11'; dependencies = [numpy, scipy, pandas, pyarrow, pyserial, matplotlib, pyyaml]
```

**Purpose.** Defines the distribution name, version, Python requirement and runtime dependencies.

**How it works.** name = kman. version = 1.0.0 (line 7), the value that release.yml's check-version step compares with the tag. requires-python = '>=3.11'. dependencies: numpy>=1.24, scipy>=1.10, pandas>=2.0, pyarrow>=14, pyserial>=3.5, matplotlib>=3.7, pyyaml>=6.0 (lines 11-17).

**Notes.** The same version string is also kept in firmware/include/config.h and host/kman_host/__init__.py. release.yml requires all three to equal the tag.

#### `pyproject.toml [project.optional-dependencies]`
*config* · [`pyproject.toml:20`](../pyproject.toml#L20)

```
[project.optional-dependencies] live = ['pyqtgraph>=0.13.3', 'PySide6>=6.5']; dev = ['pytest>=7']
```

**Purpose.** Defines two optional extras: live for the GUI packages and dev for the test tooling.

**How it works.** live (line 21): pyqtgraph>=0.13.3 and PySide6>=6.5, the plotting library and its Qt binding. dev (line 22): pytest>=7. CI installs the dev extra with pip install -e '.[dev]' in the python job. The live extra is not installed there.

#### `pyproject.toml [tool.setuptools.packages.find]`
*config* · [`pyproject.toml:24`](../pyproject.toml#L24)

```
[tool.setuptools.packages.find] where = ['host', 'analysis']; include = ['kman_host*', 'kman_analysis*']
```

**Purpose.** Limits the packages included in the distribution to the kman_host and kman_analysis packages.

**How it works.** setuptools searches the host/ and analysis/ directories for packages and keeps only names that match kman_host\* or kman_analysis\* (lines 25-26). Modules placed directly in host/ or analysis/ and not inside one of those packages are not matched by this table.

#### `pyproject.toml [tool.pytest.ini_options]`
*config* · [`pyproject.toml:28`](../pyproject.toml#L28)

```
[tool.pytest.ini_options] pythonpath = ['host', 'analysis']; testpaths = ['host/tests', 'analysis/tests']
```

**Purpose.** Sets pytest's import path and default test locations.

**How it works.** pythonpath (line 29) adds host/ and analysis/ to sys.path before collection, so tests can import the kman packages and the CLI scripts (logger, live_view, sim_device, as the inline comment lists) from the source tree without installing them. testpaths (line 30) limits default collection to host/tests and analysis/tests, so a bare pytest run covers the same tests as the CI python job. Both paths are relative to the directory that holds pyproject.toml.

### `requirements.txt`

#### `requirements.txt`
*config* · [`requirements.txt:1`](../requirements.txt#L1)

```
flat pip requirements list: numpy, scipy, pandas, pyarrow, pyserial, matplotlib, pyyaml, pyqtgraph, PySide6, pytest
```

**Purpose.** Installs the runtime dependencies, the live GUI packages and pytest with pip install -r requirements.txt.

**How it works.** Lines 1-7 are the seven runtime dependencies from pyproject.toml [project], with the same version floors. Lines 8-9 are pyqtgraph>=0.13.3 and PySide6>=6.5, the live extra; the comment on line 9 says PyQt6 or PyQt5 also work as the Qt binding. Line 10 is pytest>=7, the dev extra. The file does not install the kman packages themselves.

**Notes.** No workflow or script installs from this file; CI installs from pyproject.toml. Its only automated use is indirect: the CI python job's actions/setup-python@v5 step has cache: pip and no cache-dependency-path, so by default it hashes **/requirements.txt to key the pip download cache. Editing only pyproject.toml therefore leaves that cache key unchanged. The two dependency lists are maintained by hand, so a version floor change must be made in both files. README.md (lines 30 and 129) documents pip install -r requirements.txt as the install command.

## 18. Tests: protocol, stream, C++ cross-check

- **`host/tests/test_protocol.py`**: Unit tests for the Python wire protocol in kman_host/protocol.py, the host mirror of firmware/src/protocol.h. They check the CRC-16/CCITT-FALSE reference value, frame round trips with byte-at-a-time feeding, chunking invariance for a frame ending in 0xAA, resynchronisation after garbage, a corrupted frame and a bad length field, sync bytes inside a payload, and the session header to numpy record layout. The file defines no classes or helper functions. The CRC test uses the literal byte string b'123456789', the framing tests build frames with protocol.encode_frame (plus literal garbage bytes), and the header test builds the header JSON directly.
- **`host/tests/test_stream.py`**: Integration tests for StreamDecoder in kman_host/stream.py, driven by the synthetic device in kman_host/simulator.py. They cover 32-bit time and 16-bit sequence unwrapping across a wrap, exact gap and corruption accounting, live and scheduled label alignment, DATA frames received before any header (counted, not decoded), header-driven segmentation, the LayoutChanged error, and a full record-and-reload round trip through kman_host/session_io.py. The module constant SHORT (line 9) is a three-segment label scenario of (label, seconds) pairs (1, 6), (8, 4), (4, 6), which gives 600, 400 and 600 samples (1600 in total at 100 Hz). The _decode helper (line 12) is the shared replay driver. The file defines no classes. It imports session_io at module level, so the whole module needs pandas and pyarrow installed, not only the round-trip test.
- **`host/tests/test_crosscheck.py`**: Cross-language test. It compiles firmware/test/native_frames.cpp with firmware/src/protocol.cpp (and the json_writer.h it includes) using the host's g++ or clang++ under -Werror, runs the binary, and decodes its stdout with the Python FrameParser. It checks the decoded values, the JSON writer output, and that the Python encoder reproduces each firmware frame byte for byte. Module constants: FW (line 12) is Path(__file__).resolve().parents[2] / 'firmware', and CXX (line 13) is the first of g++ or clang++ on PATH, or None. The file defines no classes.

**How it connects.** These files have no production callers. pytest collects them from host/tests and they import the kman_host package. test_protocol.py calls protocol.crc16, protocol.encode_frame, FrameParser.feed and its stats, SessionHeader.from_json, SessionHeader.data_dtype, SessionHeader.slot_present, the DATA_PREFIX struct and decode_data. test_stream.py drives simulator.SimConfig and simulator.generate, then uses SimSession.stream, data_frame, header_frame, label_events, header, n, raw and labels. test_device_restart_starts_new_segment also builds its second header frame itself with protocol.encode_frame. The frames go into StreamDecoder.feed, which runs FrameParser.feed and passes each parsed frame to handle_frame. handle_frame sends DATA frames to _on_data and HEADER frames to _on_header, which raises LayoutChanged or calls _new_stream and _start_segment. The tests also call StreamDecoder.set_label, schedule_labels, drain, pending_rows, summary and label_events_dicts, and read its stats, parser.stats, last_seq64, header, headers and new_header attributes. They write through session_io.SessionWriter (write_raw, set_header, write_batch, close) and read back with load_session, Session.has_slot and Session.physical. The code under test is shared production code. StreamDecoder is imported by host/logger.py (live capture and --replay), host/live_view.py and host/sim_device.py; kman_host/simulator.py does not use it. protocol.py mirrors firmware/src/protocol.h. test_crosscheck.py is the only test that links the firmware and the host code. It compiles firmware/test/native_frames.cpp against firmware/src/protocol.cpp (proto::encodeFrame, proto::crc16, proto::encodeStatus), the inline put\* helpers in firmware/src/protocol.h, and firmware/src/json_writer.h (JsonWriter). It runs the binary and parses its stdout with FrameParser.feed. It decodes the frame bodies with DATA_PREFIX.unpack_from, decode_status and json.loads, and re-encodes every frame with protocol.encode_frame for the byte-for-byte comparison.

### `host/tests/test_crosscheck.py`

#### `frames`
*pytest fixture* · [`host/tests/test_crosscheck.py:17`](../host/tests/test_crosscheck.py#L17)

```python
def frames(tmp_path_factory)  (decorated @pytest.fixture(scope='module') at line 16)
```

**Purpose.** Compiles the firmware's own C++ wire encoder natively, runs it, and decodes its stdout with the Python FrameParser. It provides the parsed frames to the cross-check tests, once per module.

**How it works.**

1. If CXX is None (no g++ or clang++ on PATH), calls pytest.skip('no native C++ compiler').
2. exe = tmp_path_factory.mktemp('native') / 'native_frames'.
3. subprocess.run with check=True: CXX -std=c++11 -Wall -Wextra -Werror -I&lt;FW>/src firmware/test/native_frames.cpp firmware/src/protocol.cpp -o exe.
4. out = subprocess.run([exe], check=True, capture_output=True).stdout.
5. parser = P.FrameParser(); got = parser.feed(out).
6. Asserts parser.stats.crc_errors == 0.
7. Returns got, the Frame objects in emitted order: DATA, STATUS, HEADER (JSON), LOG.

**Notes.** -Werror makes any warning in native_frames.cpp, json_writer.h or protocol.cpp a build failure. The fixture does not assert the frame count. The tests index frames[0] to frames[3], so a short result raises IndexError, while test_python_encoder_matches_cpp loops over whatever is returned. The fixture is module-scoped, so it compiles once per pytest run of this module.

#### `test_data_frame`
*test* · [`host/tests/test_crosscheck.py:33`](../host/tests/test_crosscheck.py#L33)

```python
def test_data_frame(frames)
```

**Purpose.** Checks that the C++ DATA frame parses to the expected timestamp, sequence and valid mask, and that its seven int16 channels hold the extreme and sign-boundary values written by the native generator.

**How it works.**

1. f = frames[0].
2. Asserts f.type == P.TYPE_DATA (0x01).
3. t, seq, valid = P.DATA_PREFIX.unpack_from(f.body), format '&lt;IHB'. Asserts (t, seq, valid) == (0xFFFFFF00, 0xFFFE, 0x05).
4. ch = numpy.frombuffer(f.body[7:], dtype '&lt;i2').tolist(). Asserts ch == [-32768, 32767, 16393, -1, 0, 1234, 8191].

**Notes.** These values are set in native_frames.cpp: t_us just below the 32-bit wrap, seq 0xFFFE, valid bits 0b101, and the int16 minimum and maximum. numpy is imported inside the function body, not at module level.

#### `test_status_frame`
*test* · [`host/tests/test_crosscheck.py:44`](../host/tests/test_crosscheck.py#L44)

```python
def test_status_frame(frames)
```

**Purpose.** Checks that the C++ STATUS frame decodes through decode_status to the exact counters written by the native generator, including 32-bit values above 2^31, per-slot read errors, the latency and high-water fields, and the HFXO flag.

**How it works.**

1. st = P.decode_status(frames[1].body). Asserts st is not None, which requires the version byte to be 1 and the body to be 42 bytes.
2. Asserts st.t_us == 123456789 and st.ticks == 4000000000 (above 2^31).
3. Asserts (st.frames_sent, st.overruns, st.tx_drops) == (42, 3, 7).
4. Asserts st.read_errors == (1, 0, 0, 9).
5. Asserts (st.max_latency_us, st.tx_high_water) == (650, 280).
6. Asserts st.hfxo is True and st.sensor_error is False (flags 0x01).

**Notes.** The 42-byte body is version u8, five u32, four u32 read-error slots, two u16, and one u8 flags. This matches decode_status and the C++ kStatusBodyLen formula.

#### `test_json_writer`
*test* · [`host/tests/test_crosscheck.py:54`](../host/tests/test_crosscheck.py#L54)

```python
def test_json_writer(frames)
```

**Purpose.** Checks that the firmware's JsonWriter output, carried in a HEADER frame, is valid JSON whose values round-trip with the expected types. The forms covered are escaping, mantissa/exponent numbers, a hex string, negative and large integers, a boolean, an array holding a nested object, and a joined string.

**How it works.**

1. d = json.loads(frames[2].body).
2. Asserts d['quote'] equals the string a, double quote, b, backslash, c, newline. The writer escapes the quote and the backslash, and writes the newline as a \u000a escape.
3. Asserts d['scale'] is approximately 61e-6, from the text 61e-6 (kvSci(61, -6)).
4. Asserts d['lpf'] is approximately 20.8, from the text 208e-1 (kvSci(208, -1)).
5. Asserts d['int'] == 7 (kvSci with exponent 0 is written as a plain integer).
6. Asserts d['addr'] == '0x6A' (kvHex with two digits, uppercase).
7. Asserts d['neg'] == -5, d['big'] == 4000000000, and d['flag'] is True.
8. Asserts d['list'] == ['x', {'name': 'chest_ax'}]. kvJoined writes its two parts joined with an underscore.

**Notes.** JsonWriter never uses floats, because newlib-nano printf has no %f. Fractional values are written as mantissa and exponent, which is valid JSON. The test checks the values only, not the type key.

#### `test_json_overflow_detected`
*test* · [`host/tests/test_crosscheck.py:65`](../host/tests/test_crosscheck.py#L65)

```python
def test_json_overflow_detected(frames)
```

**Purpose.** Checks that the firmware's JsonWriter reports buffer overflow through ok(), as reported by the native generator's verdict in a LOG frame.

**How it works.**

1. Asserts frames[3].body == b'overflow-detected'.
The native generator builds a JsonWriter over an 8-byte buffer, writes one key/value pair that does not fit, and emits 'overflow-detected' if ok() is false or 'overflow-not-detected' otherwise.

**Notes.** This test checks the verdict string only. The Python side does not run JsonWriter. Overflow is set in raw() when len + n + 1 would exceed the capacity, which makes ok() return false.

#### `test_python_encoder_matches_cpp`
*test* · [`host/tests/test_crosscheck.py:69`](../host/tests/test_crosscheck.py#L69)

```python
def test_python_encoder_matches_cpp(frames)
```

**Purpose.** Checks that the Python encoder reproduces every firmware frame byte for byte, so frames the Python simulator generates are wire-identical to firmware output.

**How it works.**

1. Loops over each Frame f in the frames fixture result, which is all four native frames.
2. Asserts P.encode_frame(f.type, f.body) == f.raw. f.raw holds the exact bytes the C++ side emitted and the parser extracted.

**Notes.** Covers the sync bytes, the little-endian length, the type byte, the body and the CRC for DATA, STATUS, HEADER and LOG frames. The loop has no count check: an empty result would pass this test, although test_data_frame would fail first.

### `host/tests/test_protocol.py`

#### `test_crc_reference_vector`
*test* · [`host/tests/test_protocol.py:8`](../host/tests/test_protocol.py#L8)

```python
def test_crc_reference_vector()
```

**Purpose.** Pins the Python CRC-16 implementation to the standard CRC-16/CCITT-FALSE check value, so every frame checksum in the host protocol is anchored to a published reference.

**How it works.**

1. No setup and no fixtures.
2. Calls P.crc16(b'123456789') with the default initial value 0xFFFF. crc16 delegates to binascii.crc_hqx (polynomial 0x1021, no reflection, no final XOR).
3. Asserts the result equals 0x29B1, the published check value for CRC-16/CCITT-FALSE over the ASCII bytes '123456789'.

**Notes.** Checks only the checksum function, not the frame layout. Frame-level CRC agreement with the firmware is checked in test_python_encoder_matches_cpp (test_crosscheck.py).

#### `test_roundtrip_and_split_chunks`
*test* · [`host/tests/test_protocol.py:13`](../host/tests/test_protocol.py#L13)

```python
def test_roundtrip_and_split_chunks()
```

**Purpose.** Checks that frames built by encode_frame come back byte-identical and in order when the stream arrives one byte per feed() call, with no CRC errors and no discarded bytes.

**How it works.**

1. Builds three DATA frames with P.encode_frame(P.TYPE_DATA, bytes(range(n))) for n = 1, 7 and 30, so the bodies are 0..n-1. Each frame is body length + 7 bytes.
2. Concatenates them into stream and creates a fresh P.FrameParser p.
3. Calls p.feed(bytes([b])) once per byte of stream and appends each returned list to got.
4. Asserts [f.raw for f in got] == frames, which means three frames, in order, byte-identical.
5. Asserts p.stats.crc_errors == 0 and p.stats.bytes_discarded == 0.

**Notes.** Exercises the incremental path of FrameParser.feed: a partial header (under 5 bytes) or a partial frame stays in the internal buffer until it is complete. The bodies contain no 0xAA byte, so payload sync bytes are not covered here; see test_sync_bytes_inside_payload.

#### `test_resync_after_garbage_and_corruption`
*test* · [`host/tests/test_protocol.py:24`](../host/tests/test_protocol.py#L24)

```python
def test_resync_after_garbage_and_corruption()
```

**Purpose.** Checks that FrameParser skips leading garbage, rejects a CRC-corrupted frame and a frame with an out-of-range length field, still returns every valid frame, and increments each error counter exactly once.

**How it works.**

1. good = encode_frame(TYPE_DATA, 3 body bytes 0x01 0x02 0x03). It is 10 bytes with length field 4.
2. bad = a bytearray copy of good with bad[6] ^= 0x40. Index 6 is the second body byte, so 0x02 becomes 0x42 and the CRC no longer matches.
3. stream = garbage prefix (0x00, 0xAA, 0x13, the ASCII bytes of 'garbage', 0xAA) + bad + good + the 4 bytes 0xAA 0x55 0xFF 0xFF + good.
4. One p.feed(stream) call.
5. Asserts the returned raw frames equal [good, good].
6. Asserts stats.crc_errors == 1 (from the bad frame) and stats.bad_length == 1 (the 0xAA 0x55 0xFF 0xFF sync declares length 0xFFFF, above MAX_LEN 4096).

**Notes.** After a CRC or length failure the parser advances exactly one byte and resumes the sync search, so a corrupted frame costs only itself. Bytes skipped as garbage are added to stats.bytes_discarded, which this test does not assert. The 0xAA bytes in the garbage that are not followed by 0x55 do not start frames.

#### `test_chunking_invariance_when_frame_ends_in_0xaa`
*test* · [`host/tests/test_protocol.py:36`](../host/tests/test_protocol.py#L36)

```python
def test_chunking_invariance_when_frame_ends_in_0xaa()
```

**Purpose.** Checks that a frame whose last byte is 0xAA parses the same way for every chunking of the stream, so the parser's retention of a trailing 0xAA at a chunk boundary neither loses bytes nor double-counts them.

**How it works.**

1. Setup: searches body pairs bytes([i, j]) in row-major order (i outer, j inner, both 0..255) for the first body whose encode_frame(1, body) ends in 0xAA. That last byte is the high byte of the CRC. The body is found at runtime, not hard-coded. The literal 1 is TYPE_DATA.
2. fr = encode_frame(TYPE_DATA, body), 9 bytes. stream = fr + encode_frame(TYPE_DATA, 0x55 0x01) + fr, 27 bytes. At the join, the byte sequence is 0xAA (end of fr), then 0xAA 0x55 (start of the middle frame).
3. For chunk in (1, 2, 3, len(stream)): creates a new parser and feeds stream[i:i+chunk] for each i in steps of chunk, accumulating the returned frames.
4. Asserts len(got) == 3 for every chunk size.
5. Asserts bytes_discarded == 0 and crc_errors == 0. The chunk size is the assertion message.

**Notes.** Targets the trailing-0xAA rule in FrameParser.feed: a buffer ending in 0xAA keeps that byte as a possible sync start (keep_from = n-1) only if n-1 is at or after pos. A byte already consumed by a completed frame is never re-kept. One-byte feeds make every 0xAA at a chunk end a candidate sync start.

#### `test_sync_bytes_inside_payload`
*test* · [`host/tests/test_protocol.py:50`](../host/tests/test_protocol.py#L50)

```python
def test_sync_bytes_inside_payload()
```

**Purpose.** Checks that 0xAA 0x55 pairs inside a frame body are not taken as frame starts, so frame boundaries come from the length field and CRC rather than from a scan for sync bytes.

**How it works.**

1. body = the bytes 0xAA 0x55 0xAA 0x55 repeated 4 times (16 bytes).
2. fr = encode_frame(TYPE_LOG, body), 23 bytes with length field 17.
3. p.feed(fr + fr) in a single call.
4. Asserts [f.body for f in result] == [body, body].

**Notes.** The type byte is TYPE_LOG, but the parser does not inspect the type during framing. After a valid frame the parser resumes at the frame's end, so payload bytes are never searched for sync.

#### `test_header_dtype`
*test* · [`host/tests/test_protocol.py:57`](../host/tests/test_protocol.py#L57)

```python
def test_header_dtype()
```

**Purpose.** Checks that SessionHeader.from_json, data_dtype, decode_data and slot_present agree on the DATA record layout of a two-channel header: a 7-byte prefix followed by little-endian int16 channels, with wrong-size bodies rejected.

**How it works.**

1. hdr dict: type 'kman_session_header', sample_rate_hz 100, period_us 10000, present_mask 5, one sensor {slot 'chest_imu', status 'ok'}, and channels chest_ax (slot chest_imu, bit 0, unit g, scale 61e-6) and flex_raw (slot neck_flex, bit 2, unit counts, scale 1).
2. h = SessionHeader.from_json(json.dumps(hdr)); dt = h.data_dtype().
3. Asserts dt.itemsize == 11: t_us u4, seq u2, valid u1, then two i2 channels.
4. body = DATA_PREFIX.pack(5, 6, 5) followed by little-endian int16 values -100 and 9000 (11 bytes).
5. rec = decode_data(body, dt). Asserts rec['chest_ax'][0] == -100 and rec['flex_raw'][0] == 9000.
6. Asserts decode_data(body + one zero byte, dt) is None, because the size does not match the itemsize.
7. Asserts h.slot_present('chest_imu') is True and h.slot_present('abdomen_imu') is False, since that slot is absent from sensors.

**Notes.** Does not check the decoded t_us, seq or valid fields, present_mask, or the channel bit and scale semantics. The error paths of from_json (a non-header type, or an 'error' key) are not exercised.

### `host/tests/test_stream.py`

#### `_decode`
*helper function* · [`host/tests/test_stream.py:12`](../host/tests/test_stream.py#L12)

```python
def _decode(s, **stream_kw)
```

**Purpose.** Replays a simulated session through a fresh StreamDecoder and returns the decoder together with all decoded samples. It is shared by the wrap/gap test and the corruption test.

**How it works.**

1. dec = StreamDecoder().
2. dec.schedule_labels(s.label_events()), so label changes apply by seq as in a replay.
3. for _, fr in s.stream(**stream_kw): dec.feed(fr, 0.0). Keyword arguments such as drop_prob, corrupt_prob and seed go to SimSession.stream. host_time is fixed at 0.0 for every frame.
4. Returns (dec, dec.drain()): the decoder, and the column dict from drain(), or None if no rows were decoded.

**Notes.** Because host_time is constant, the host-measured segment bridging has no effect in these runs. Label events from the simulator carry only seq (no segment fields), so they are applied by seq.

#### `test_wrap_and_gaps_are_exact`
*test* · [`host/tests/test_stream.py:20`](../host/tests/test_stream.py#L20)

```python
def test_wrap_and_gaps_are_exact()
```

**Purpose.** Checks that StreamDecoder reconstructs exact unwrapped time and tick indices across the 32-bit microsecond wrap and the 16-bit sequence wrap while 1% of data frames are randomly dropped, and that decoded values and labels line up with their source samples.

**How it works.**

1. cfg = SimConfig(t0_us = 2**32 - 2,000,000, seq0 = 65530). Device time starts 2 s before the 32-bit wrap, and the 16-bit seq wraps after 6 samples.
2. s = sim.generate(cfg, SHORT): 1600 samples at 100 Hz.
3. dec, b = _decode(s, drop_prob=0.01, seed=5).
4. Asserts np.diff(b['t_us']) > 0 everywhere.
5. Asserts np.diff(b['t_us']) == np.diff(b['seq']) \* 10000. The period is 10 000 us per tick.
6. idx = b['seq'] - cfg.seq0 maps each row back to its source index.
7. Asserts dec.stats.dropped == s.n - len(idx).
8. Asserts dec.stats.seq_mismatch == 0.
9. Asserts b['chest_gx'] equals s.raw['chest_gx'][idx] and b['label'] equals s.labels[idx], element-wise.

**Notes.** The dropped count is exact only if the first and last data frames arrive. A missing first frame leaves no earlier frame to reveal a gap, and a missing last frame has no later frame to reveal it. Seed 5 avoids both, and the test passes. Simulator label events use unwrapped seq values (seq0 + index), which match the decoder's unwrapped seq64.

#### `test_corruption_costs_only_the_corrupted_frame`
*test* · [`host/tests/test_stream.py:33`](../host/tests/test_stream.py#L33)

```python
def test_corruption_costs_only_the_corrupted_frame()
```

**Purpose.** Checks that each CRC-corrupted data frame costs exactly one sample: it is counted as one missing tick, and every other sample is decoded in place.

**How it works.**

1. s = sim.generate(sim.SimConfig(), SHORT). Defaults: 100 Hz, t0 1,000,000 us, seq0 100, 1600 samples.
2. dec, b = _decode(s, corrupt_prob=0.01, seed=2). In SimSession.stream, each data frame has probability 0.01 of having one byte XORed with 0xFF at a random index in [5, len(frame)). That index is always a body or CRC byte, so sync, length and type stay intact. Header and status frames are never corrupted.
3. Asserts dec.parser.stats.crc_errors > 0.
4. Asserts dec.stats.dropped == dec.parser.stats.crc_errors.
5. Asserts len(b['t_us']) + dec.stats.dropped == s.n.

**Notes.** The one-to-one equality holds because the frame after a corrupted frame shows a two-tick timestamp step, which the decoder records as one missing tick. Two consecutive corrupted frames give two missing ticks. Status frames do not affect tick accounting and are not counted. Like the wrap test, the equality would fail if the first or last data frame were corrupted. Seed 2 avoids that, and the test passes.

#### `test_live_label_applies_from_next_frame`
*test* · [`host/tests/test_stream.py:41`](../host/tests/test_stream.py#L41)

```python
def test_live_label_applies_from_next_frame()
```

**Purpose.** Checks that a live label set with set_label applies from the next frame (seq = last_seq64 + 1) and not retroactively to earlier frames.

**How it works.**

1. s = sim.generate(sim.SimConfig(), SHORT). dec = StreamDecoder() with no schedule. stream = list(s.stream()): the header, then data frames with a status frame after every 100th sample.
2. Feeds stream[:51]: the header and data frames 0 to 49. No status frame falls in this range.
3. ev = dec.set_label(4). Asserts ev.seq == dec.last_seq64 + 1. The last delivered seq is 149, so ev.seq is 150.
4. Feeds stream[51:60]: data frames 50 to 58.
5. b = dec.drain(). Asserts b['label'][:50] are all 0 and b['label'][50:] are all 4, for 59 rows in total.

**Notes.** set_label changes self.label immediately, and _on_data stamps each row with the label current when that row is appended. The switch therefore takes effect at the next data frame processed. The test does not check the event's t_us or segment fields.

#### `test_frames_before_header_are_counted`
*test* · [`host/tests/test_stream.py:55`](../host/tests/test_stream.py#L55)

```python
def test_frames_before_header_are_counted()
```

**Purpose.** Checks that DATA frames received before any header are counted in stats.frames_before_header, produce no samples, and raise no error.

**How it works.**

1. s = sim.generate(sim.SimConfig(), SHORT). dec = StreamDecoder() with no header installed.
2. For i in 0..4: dec.feed(s.data_frame(i), 0.0).
3. Asserts dec.stats.frames_before_header == 5.
4. Asserts dec.drain() is None.

**Notes.** _on_data returns before decoding the body when self.header is None. The parser still CRC-checks these frames, but they are not counted in frames_data, dropped, or seq tracking.

#### `test_layout_change_raises`
*test* · [`host/tests/test_stream.py:63`](../host/tests/test_stream.py#L63)

```python
def test_layout_change_raises()
```

**Purpose.** Checks that a header announcing a different channel layout mid-session raises LayoutChanged instead of decoding with the old layout.

**How it works.**

1. s = sim.generate(sim.SimConfig(), SHORT): 7 channels (six chest IMU channels and flex_raw). s2 = sim.generate(sim.SimConfig(abdomen=True), SHORT): 13 channels.
2. dec.feed(s.header_frame(), 0.0) installs the first header.
3. Inside pytest.raises(LayoutChanged): dec.feed(s2.header_frame(), 0.0). _on_header compares SessionHeader.layout_key() of the new header with the stored one. The key is the tuple of (name, scale) per channel plus period_us. The keys differ, so the method raises.

**Notes.** stats.headers is incremented before the layout comparison, so the rejected header is still counted. self.header is not replaced. The test does not check the message text or the decoder state after the exception.

#### `test_device_restart_starts_new_segment`
*test* · [`host/tests/test_stream.py:72`](../host/tests/test_stream.py#L72)

```python
def test_device_restart_starts_new_segment()
```

**Purpose.** Checks that a header with a new stream_id starts a new segment, that a device clock restarting at the same timestamps yields no duplicate or backward samples, and that the time gap across the restart is bridged with the host-measured gap of about 1.9 s.

**How it works.**

1. s = sim.generate(sim.SimConfig(), SHORT); dec = StreamDecoder().
2. dec.feed(s.header_frame(), 100.0) installs the header (stream_id 1, segment 0).
3. Feeds data frames 0 to 9 at host times 100.00 to 100.09. Their t_us runs from 1,000,000 to 1,090,000 and their seq from 100 to 109.
4. hdr2 = dict(s.header, stream_id=2) is sent as a header frame at host time 102.0. _new_stream sees stream_id changed, so _start_segment runs. The segment becomes 1 and _seg_gap_host = 102.0 - 100.09 = 1.91 s.
5. Feeds data frames 0 to 4 again, with the same t_us and seq values as the first stream, at host times 102.00 to 102.04.
6. b = dec.drain(). Asserts np.unique(b['segment']) == [0, 1].
7. Asserts np.all(np.diff(b['t_us']) > 0).
8. gap = b['t_us'][10] - b['t_us'][9] is asserted to lie within (1.8e6, 2.2e6). The first frame of segment 1 takes nt = max(1, round(1.91e6 / 10000)) = 191 ticks, so t64 = 1,090,000 + 191 x 10,000 = 3,000,000 and the gap is 1,910,000 us.

**Notes.** The seq64 of segment 1 also advances by nt (109 + 191 = 300), so seq does not restart. The assertion window is loose: any gap from 1.8 to 2.2 s passes. The test calls __import__('json') inline instead of importing json at module level.

#### `test_session_roundtrip`
*test* · [`host/tests/test_stream.py:89`](../host/tests/test_stream.py#L89)

```python
def test_session_roundtrip(tmp_path)
```

**Purpose.** Checks the complete record path: simulator stream, then StreamDecoder, then SessionWriter (Parquet plus raw .bin), then load_session. It verifies the row count, the abdomen slot flag, the label history, and that physical-unit conversion gives a plausible gravity component.

**How it works.**

1. s = sim.generate(sim.SimConfig(abdomen=True), SHORT): 13 channels, 1600 samples.
2. dec = StreamDecoder(); dec.schedule_labels(s.label_events()).
3. w = SessionWriter(tmp_path / 'x.parquet', row_group_rows=200). This also opens x.bin and x.labels.jsonl.
4. For each (_, fr) in s.stream() (no loss or corruption): w.write_raw(fr) appends the verbatim frame to x.bin. dec.feed(fr, 0.0). If dec.new_header, w.set_header(dec.header) runs and the flag is cleared. If dec.pending_rows() >= 77, w.write_batch(dec.drain()) runs.
5. After the loop, w.write_batch(dec.drain()). Then w.close(dec.headers, dec.label_events_dicts(), dec.summary()) flushes the rest and writes headers, labels, label names, stats and notes into the Parquet key/value metadata.
6. ss = load_session(tmp_path / 'x.parquet').
7. Asserts len(ss.df) == s.n (1600).
8. Asserts ss.has_slot('abdomen_imu').
9. Asserts [e['label'] for e in ss.labels] == [1, 8, 4].
10. ph = ss.physical(). Asserts the mean of abs(ph['chest_ay']) is 0.97 within 0.02.
11. Asserts (tmp_path / 'x.bin').stat().st_size > 0.

**Notes.** The labels are the three scheduled changes from SHORT, at seq 100, 700 and 1100. physical() multiplies each int16 channel by its header scale (61e-6 g per LSB for the accelerometers). It sets samples to NaN where the frame's valid bit for that channel's bit is 0; here present_mask is 7, so nothing is NaN. The 0.97 g target is the y component of the simulator's gravity vector (0, 0.97, 0.243), normalised. The .labels.jsonl sidecar is opened but gets no events in this test, because the labels go only into the Parquet metadata. close() also writes the extra metadata (created_unix) next to headers, labels, label names, stats and notes. Dependency: session_io.py imports pandas and pyarrow at module level, and test_stream.py imports session_io at module level with no importorskip guard, so the whole test_stream.py module needs both packages. In the documentation environment pandas 3.0.5 is installed but pyarrow is not (pytest itself is not installed either). Under real pytest, collecting test_stream.py would fail with ImportError and every test in the file would error; nothing would be skipped. This test was not executed. The other seven test_stream.py tests pass when session_io is stubbed out.

## 19. Tests: 1.0 regression tests and analysis tests

- **`host/tests/test_v1_fixes.py`**: Regression tests for the issues found by the code review before 1.0 (the file docstring points to CHANGELOG.md 'Fixed'). They are not only host-side: they also cover analysis fixes (breath rate in kman_analysis.breathing, chest-abdomen phase in kman_analysis.phase), the simulator's _smooth and report.py. It holds 21 test functions that collect as 23 cases, because test_ring_large_batch_keeps_order is parametrized three ways; the CHANGELOG counts these 23. Section comments group the tests as 'C: device restarts', 'A, H: replay', 'B: reconnect', 'D: breath rate', 'E: live view ring' and 'review round 2'. The decoder, logger, replay and live-view tests run the real StreamDecoder, logger.main, logger.Recorder.feed, sim_device.main and live_view.Ring on simulator frames, with pyserial and the key reader replaced by in-memory fakes. They assert on decoded rows, decoder stats and the written session and sidecar files. The breath-rate and phase tests use synthetic sine waves rather than simulator frames, the smoothing test uses random noise, and the report test checks argument validation only. The module constant SHORT = [(1, 6), (8, 4), (4, 6)] is the scenario most tests pass to sim.generate: 6 s of label 1, 4 s of label 8 and 6 s of label 4, which is 1600 samples at 100 Hz.
- **`analysis/tests/test_analysis.py`**: Acceptance checks for the analysis package against simulator ground truth. They mirror the Step 1 criteria named in the module docstring: rest breathing versus breath hold, swallows versus head movement, and normal versus simulated obstruction using the abdomen IMU. Eight of the ten tests build a session by running simulator frames through StreamDecoder and SessionWriter, load it with load_session and run kman_analysis.analyze: the five that use the abd fixture, plus the frame-loss, gap and neck-IMU tests. They compare the summary, chest, neck, u and qa results with the simulator's truth arrays and per-sample labels. The labels used are 1 rest, 2 talking, 4 dry swallow, 5 head movement, 6 walking, 8 breath hold and 9 obstruction. Five tests sit outside the abd fixture: frame loss, a 3 s data gap, the neck-IMU classifier, the gravity complementary filter, and rejection of unknown config keys.

**How it connects.** These files are not called by production code. pytest collects them from host/tests and analysis/tests, and they call into the code under test. test_v1_fixes.py uses kman_host.simulator: SimConfig, generate, the SimSession attributes header and cfg, the methods header_frame, data_frame and stream, and the private _smooth. It does not call label_events. From kman_host.protocol it uses encode_frame and TYPE_HEADER. From kman_host.stream.StreamDecoder it uses feed, drain, pending_rows, schedule_labels, set_label and stats. From kman_host.session_io it uses load_session, read_label_events and sidecar_paths. It also uses live_view.Ring (extend and ordered), logger.main (the live --port path and the --replay path), logger.Recorder.feed (on a Recorder built with __new__ and its dec, udp, last_header_raw and writer attributes set by hand), sim_device.main, kman_analysis.load_config, kman_analysis.breathing.detect_breaths, kman_analysis.phase.chest_abdomen_phase and report.main. It monkeypatches logger.KeyReader, logger.time.sleep (the stdlib time.sleep, so the patch applies process-wide) and serial.Serial. This works because run_live imports serial and calls serial.Serial when it opens the port, and looks up KeyReader and time.sleep when it calls them. test_analysis.py uses simulator generate, SimSession.stream and label_events, and StreamDecoder: feed, schedule_labels, new_header, header, headers, drain, label_events_dicts and summary. It also uses SessionWriter (set_header, write_batch, close), load_session, kman_analysis.analyze and load_config, and kman_analysis.breathing.gravity_complementary. It reads the analyze() result attributes summary, chest, neck, u and qa, and depends on the simulator's truth keys (resp, swallow_times, head_times) and per-sample labels. In production, StreamDecoder.feed is the decoder behind logger.Recorder, which serves both live recording and --replay, and behind live_view's stream handling. So the code paths these tests cover are the ones that run in the field.

### `analysis/tests/test_analysis.py`

#### `_session`
*helper* · [`analysis/tests/test_analysis.py:16`](../analysis/tests/test_analysis.py#L16)

```python
def _session(tmp_path, name, cfg=None, **stream_kw)
```

**Purpose.** Simulates a device stream, decodes it, writes it as a session Parquet file, and returns the simulator object with the loaded session.

**How it works.**

1. s = sim.generate(cfg or sim.SimConfig()); the default scenario is used when cfg is None. 2. dec = StreamDecoder(); dec.schedule_labels(s.label_events()) applies the simulator's label changes. 3. w = SessionWriter(tmp_path / f'{name}.parquet', raw=False). 4. For each (_, fr) in s.stream(**stream_kw): dec.feed(fr, 0.0); if dec.new_header is set, call w.set_header(dec.header) and reset dec.new_header to False. The host time is 0.0 throughout. 5. w.write_batch(dec.drain()); w.close(dec.headers, dec.label_events_dicts(), dec.summary()). 6. Returns (s, load_session(path)). stream_kw is forwarded to SimSession.stream (drop_prob, corrupt_prob, seed, status_every_s).

**Notes.** Used by the abd fixture and by test_robust_to_frame_loss and test_neck_imu_flags_head_motion. test_long_gap_is_masked repeats the same loop with some frames skipped.

#### `abd`
*fixture* · [`analysis/tests/test_analysis.py:32`](../analysis/tests/test_analysis.py#L32)

```python
@pytest.fixture(scope='module') def abd(tmp_path_factory)
```

**Purpose.** Builds one abdomen-IMU session and its analysis once for the whole test module.

**How it works.** d = tmp_path_factory.mktemp('abd'); s, sess = _session(d, 'abd', sim.SimConfig(abdomen=True)); returns (s, analyze(sess, load_config())). Five tests take it as an argument: test_rest_breathing_rate_and_detection, test_rest_vs_breath_hold, test_swallow_vs_head_movement, test_normal_vs_obstruction_phase and test_motion_artifact_flag.

**Notes.** Module scope means one simulation and one analyze() call per test run. The tests read the result and do not modify it.

#### `row`
*helper* · [`analysis/tests/test_analysis.py:37`](../analysis/tests/test_analysis.py#L37)

```python
def row(a, label)
```

**Purpose.** Returns the summary row for one label from an analysis result.

**How it works.** Returns a.summary.set_index('label').loc[label], a pandas Series holding that label's summary values. The columns the tests read include rate_median_bpm, breaths_per_min, max_breath_gap_s, swallows, head_events, phase_mean_deg, paradox_pct and artifact_pct.

#### `test_rest_breathing_rate_and_detection`
*test* · [`analysis/tests/test_analysis.py:41`](../analysis/tests/test_analysis.py#L41)

```python
def test_rest_breathing_rate_and_detection(abd)
```

**Purpose.** Checks that the rest breathing rate is about 14 breaths per minute and that the chest breath detector finds more than 95% of the true rest breaths.

**How it works.** Setup: s, a = abd. Assertion 1: row(a, 1)['rate_median_bpm'] == pytest.approx(14.0, abs=1.0). Truth: resp = s.truth['resp']; pk = signal.find_peaks(resp, prominence=0.1 \* resp.std())[0]; truth_t = pk / s.cfg.fs; rest = truth_t[s.labels[pk] == 1]. Detected: det = a.chest.breath_t[a.chest.breath_valid]. A true rest time x counts as matched when np.min(np.abs(det - x)) &lt; 1.0 s. Assertion 2: the matched fraction is > 0.95.

**Notes.** The truth peaks come from the simulator's respiration waveform, not from the analysis.

#### `test_rest_vs_breath_hold`
*test* · [`analysis/tests/test_analysis.py:53`](../analysis/tests/test_analysis.py#L53)

```python
def test_rest_vs_breath_hold(abd)
```

**Purpose.** Checks that rest (label 1) and breath hold (label 8) separate on maximum breath gap and on breath rate.

**How it works.** rest = row(a, 1); hold = row(a, 8). Assertions: hold['max_breath_gap_s'] > 15; rest['max_breath_gap_s'] &lt; 8; and the chained comparison hold['breaths_per_min'] &lt; 5 &lt; 10 &lt; rest['breaths_per_min'].

**Notes.** Python evaluates the chain as hold &lt; 5 and 5 &lt; 10 and 10 &lt; rest. The 5 &lt; 10 part is always true, so the effective checks are hold &lt; 5 and rest > 10.

#### `test_swallow_vs_head_movement`
*test* · [`analysis/tests/test_analysis.py:61`](../analysis/tests/test_analysis.py#L61)

```python
def test_swallow_vs_head_movement(abd)
```

**Purpose.** Checks that swallows are found in the dry-swallow segment (label 4) and not in the head-movement segment (label 5), that head events appear only in label 5, and that detected swallow peaks sit on true swallows.

**How it works.** Setup: s, a = abd; dry = row(a, 4); head = row(a, 5). n_truth = the number of truth swallow times whose sample index int(t \* fs) has label 4. Assertions: dry['swallows'] >= 0.8 \* n_truth; dry['head_events'] == 0; head['head_events'] >= 0.8 \* len(s.truth['head_times']); head['swallows'] == 0. Last, ev = a.neck.events; sw = peak_s of the rows with kind 'swallow' and label 4; every peak must lie within 1.5 s of some entry in s.truth['swallow_times'].

**Notes.** The final all() is vacuously true when no label-4 swallow events exist. The 0.8 check catches that case only when n_truth > 0.

#### `test_normal_vs_obstruction_phase`
*test* · [`analysis/tests/test_analysis.py:77`](../analysis/tests/test_analysis.py#L77)

```python
def test_normal_vs_obstruction_phase(abd)
```

**Purpose.** Checks chest-abdomen phase: normal rest (label 1) is in phase, with the mean near 0 degrees and almost no paradox; simulated obstruction (label 9) is anti-phase, with the mean beyond 150 degrees and paradox above 80%.

**How it works.** rest = row(a, 1); obs = row(a, 9). Assertions: abs(rest['phase_mean_deg']) &lt; 30 and rest['paradox_pct'] &lt; 5; abs(obs['phase_mean_deg']) > 150 and obs['paradox_pct'] > 80.

**Notes.** Relies on the abdomen IMU stream in the abd fixture. The simulator's generate() comments say obstruction is generated in anti-phase, which is the paradox case the test checks.

#### `test_motion_artifact_flag`
*test* · [`analysis/tests/test_analysis.py:84`](../analysis/tests/test_analysis.py#L84)

```python
def test_motion_artifact_flag(abd)
```

**Purpose.** Checks that the artifact flag covers walking (label 6) and does not cover rest (label 1) or talking (label 2).

**How it works.** Assertions: row(a, 6)['artifact_pct'] > 90; row(a, 1)['artifact_pct'] &lt; 5; row(a, 2)['artifact_pct'] &lt; 20.

**Notes.** Talking is allowed a looser bound (under 20%) than rest (under 5%).

#### `test_robust_to_frame_loss`
*test* · [`analysis/tests/test_analysis.py:91`](../analysis/tests/test_analysis.py#L91)

```python
def test_robust_to_frame_loss(tmp_path)
```

**Purpose.** Checks that the rest rate and the breath-hold gap survive 1% frame loss and 0.2% corrupted frames, and that the loss itself is reported.

**How it works.** Setup: s, sess = _session(tmp_path, 'lossy', drop_prob=0.01, corrupt_prob=0.002, seed=4), which uses the default scenario and no abdomen IMU. a = analyze(sess, load_config()). Assertions: a.qa['lost_pct'] > 0.5; row(a, 1)['rate_median_bpm'] is approximately 14.0 within 1.0; row(a, 8)['max_breath_gap_s'] > 15.

**Notes.** seed=4 fixes which frames are dropped or corrupted. The lost_pct check confirms that loss actually occurred, so the other assertions are not met trivially on a clean stream.

#### `test_long_gap_is_masked`
*test* · [`analysis/tests/test_analysis.py:99`](../analysis/tests/test_analysis.py#L99)

```python
def test_long_gap_is_masked(tmp_path)
```

**Purpose.** Checks that a 3 s hole in the stream (samples 3000-3299) is reported as a gap and that no breath is marked valid inside it.

**How it works.** Setup: s = sim.generate(SimConfig()); dec = StreamDecoder() with schedule_labels(s.label_events()); w = SessionWriter(tmp_path / 'gap.parquet', raw=False). Action: for i, fr in s.stream(), skip frames with 3000 &lt;= i &lt; 3300 (30-33 s; status frames in that range are skipped too); feed the rest and call w.set_header when dec.new_header is set, as in _session. Then w.write_batch(dec.drain()), w.close(...), and a = analyze(load_session(...), load_config()). Assertions: a.u.gap[3000:3300].all(); not a.u.gap[:2990].any(); with bt = a.chest.breath_t and inside = (bt >= 30) & (bt &lt; 33), not a.chest.breath_valid[inside].any().

**Notes.** The in-line comment places the hole inside rest breathing (the default scenario opens with 60 s of label 1). The 2990 bound leaves a 10-sample (0.1 s) margin: samples 2990-2999, just before the hole, may be flagged as gap, but every sample before index 2990 must be unflagged. a.u.gap is indexed by sample. Status frames are yielded with the index of the data sample they follow, so the ones at 3099, 3199 and 3299 are skipped too.

#### `test_neck_imu_flags_head_motion`
*test* · [`analysis/tests/test_analysis.py:120`](../analysis/tests/test_analysis.py#L120)

```python
def test_neck_imu_flags_head_motion(tmp_path)
```

**Purpose.** Checks that, with a neck IMU present and the flex-based head-motion rules (peak amplitude above head_amplitude_k x sigma, duration above max_duration_s) switched off, the neck IMU alone classifies flex events in head movement (label 5) as head_motion with a 'neck imu' reason, and that events in dry swallow (label 4) stay classified as swallow.

**How it works.** Setup: s, sess = _session(tmp_path, 'neck', sim.SimConfig(neck_imu=True)). a = analyze(sess, load_config(overrides={'swallow': {'head_amplitude_k': 1e9, 'max_duration_s': 1e9}})). These two values switch off the flex rules in swallow.py that would otherwise mark an event head_motion with reason 'amplitude' or 'duration', so only the neck-IMU tilt/yaw check (reason 'neck imu (tilt ..., yaw ...)') can make an event head_motion. ev = a.neck.events, where each event's label is the label at its peak sample. head = rows with label 5; dry = rows with label 4. Assertions: (head['kind'] == 'head_motion').mean() > 0.8; head['reason'].str.startswith('neck imu').all(); (dry['kind'] == 'swallow').all().

**Notes.** load_config would raise KeyError if these keys were absent from the swallow section (see test_config_rejects_unknown_keys). The all() checks pass trivially on an empty subset, and an empty label-5 set makes the mean NaN, which fails the first check.

#### `test_complementary_filter_tracks_rotation`
*test* · [`analysis/tests/test_analysis.py:131`](../analysis/tests/test_analysis.py#L131)

```python
def test_complementary_filter_tracks_rotation()
```

**Purpose.** Checks that gravity_complementary tracks a 5 degree, 0.3 Hz rotation about the x axis: after the first 2 s the tilt error stays under 0.5 degree at its peak, and its spread is under 30% of the spread of the raw accelerometer tilt.

**How it works.** Setup: fs = 100.0; t = arange(0, 20, 1/fs); th = radians(5) \* sin(2\*pi\*0.3\*t). Gravity model: g = [0, cos(-th), sin(-th)], a unit vector in the y-z plane; acc = g + 0.01 \* standard_normal, from default_rng(0). gyr = [degrees(gradient(th) \* fs), 0, 0] in deg/s about x. Action: est = gravity_complementary(acc, gyr, fs, tau_s=1.0). Tilt is arctan2(z, y) of est and of acc, in degrees, compared with -th; the first 200 samples are excluded. Assertions: err.max() &lt; 0.5; err.std() &lt; 0.3 \* acc_err.std().

**Notes.** The test fixes the tilt convention: angle = atan2(z, y), with the truth at -th. The first 200 samples (2 s) are excluded from both errors.

#### `test_config_rejects_unknown_keys`
*test* · [`analysis/tests/test_analysis.py:147`](../analysis/tests/test_analysis.py#L147)

```python
def test_config_rejects_unknown_keys()
```

**Purpose.** Checks that an unknown key in a config override raises KeyError instead of being silently ignored.

**How it works.** with pytest.raises(KeyError): load_config(overrides={'swallow': {'k_onn': 3}}). The key 'k_onn' is used as the unknown key.

### `host/tests/test_v1_fixes.py`

#### `_feed`
*helper* · [`host/tests/test_v1_fixes.py:19`](../host/tests/test_v1_fixes.py#L19)

```python
def _feed(dec, s, frames, t0_host)
```

**Purpose.** Feeds the data frames with the given sample indices from a simulated stream into a StreamDecoder, one frame per 10 ms of synthetic host time.

**How it works.** Loops 'for k, i in enumerate(frames)' and calls dec.feed(s.data_frame(i), t0_host + k \* 0.01). The host time uses the enumeration index k, not the sample index i, so the host clock advances 0.01 s per frame even when frames is not contiguous (for example range(400, 600)). Only data frames are sent; headers and status frames are the caller's responsibility. Returns None.

**Notes.** The 0.01 s step matches the simulator's 100 Hz sample rate (SimConfig.fs defaults to 100).

#### `test_reboot_with_same_stream_id_keeps_data`
*test* · [`host/tests/test_v1_fixes.py:27`](../host/tests/test_v1_fixes.py#L27)

```python
def test_reboot_with_same_stream_id_keeps_data()
```

**Purpose.** Checks that a device restart on firmware without boot_id (same stream_id, clock starting again from a lower value) keeps every frame from both runs and records the restart as a second segment.

**How it works.** Setup: s1 = sim.generate(SimConfig(t0_us=60_000_000), SHORT) and s2 = sim.generate(SimConfig(t0_us=1_500_000, seq0=150), SHORT); the 'boot_id' key is removed from both s.header dicts, so their header frames carry no boot_id. Action: dec = StreamDecoder(); feed the s1 header at host 100.0; _feed 500 frames of s1 from 100.0; feed the s2 header at 110.0; _feed 500 frames of s2 from 110.0; b = dec.drain(). Assertions: len(b['t_us']) == 1000; dec.stats.duplicates == 0; dec.stats.segments == 2; np.diff(b['t_us']) > 0 everywhere, so timestamps increase across the restart.

**Notes.** Guards CHANGELOG 1.0.0 Fixed item 1: after a replug the board reboots with the same stream_id and the restarted clock was taken for duplicate frames. The sequence ranges overlap (s1 seq 100-599, s2 seq 150-649), so a sequence-based duplicate rule would drop frames here; zero duplicates is the check. Section 'C: device restarts'.

#### `test_reboot_after_long_uptime_is_not_a_forward_gap`
*test* · [`host/tests/test_v1_fixes.py:45`](../host/tests/test_v1_fixes.py#L45)

```python
def test_reboot_after_long_uptime_is_not_a_forward_gap()
```

**Purpose.** Checks that a restart after about 50 minutes of uptime (device clock near 3e9 us) is taken as a new segment and not as a forward jump that counts lost frames.

**How it works.** Setup: s1 = sim.generate(SimConfig(t0_us=3_000_000_000), SHORT) and s2 = sim.generate(SimConfig(t0_us=1_200_000, seq0=120), SHORT). Both headers keep the simulator's default boot_id 0x00000001, so the restart is visible only through the clock and sequence. Action: s1 header at 100.0; _feed 300 frames of s1 from 100.0; s2 header at 105.0; _feed 300 frames of s2 from 105.0. Assertions: dec.stats.dropped == 0; dec.stats.segments == 2; dec.pending_rows() == 600.

**Notes.** Guards CHANGELOG 1.0.0 Fixed item 1 (uptime case). The docstring states that modular 32-bit arithmetic alone reads this restart as a 21-minute forward jump (about 1.29e9 us) and counts about 130k lost frames. The sequence ranges overlap (s1 100-399, s2 120-419).

#### `test_boot_id_change_starts_segment`
*test* · [`host/tests/test_v1_fixes.py:59`](../host/tests/test_v1_fixes.py#L59)

```python
def test_boot_id_change_starts_segment()
```

**Purpose.** Checks that a header with a different boot_id starts a new segment even though the stream's clock and sequence continue without a break.

**How it works.** Setup: s = sim.generate(SimConfig(), SHORT); feed the header at 0.0; _feed 50 frames from 0.0. Builds hdr = dict(s.header, boot_id='0xDEADBEEF', t_us_at_header = s.cfg.t0_us + 50 \* 10000), that is the device time of the next sample at 100 Hz (10000 us per sample), and feeds P.encode_frame(P.TYPE_HEADER, json.dumps(hdr).encode()) at host time 1.0. Assertion: dec.stats.segments == 2.

**Notes.** Section 'C: device restarts'. The new header's timestamp continues the stream, so the boot_id change is the only signal in this test. CHANGELOG item 1 lists a boot_id change as a segment trigger.

#### `test_resent_header_mid_stream_is_not_a_new_segment`
*test* · [`host/tests/test_v1_fixes.py:69`](../host/tests/test_v1_fixes.py#L69)

```python
def test_resent_header_mid_stream_is_not_a_new_segment()
```

**Purpose.** Checks that a header sent again in the middle of a stream, with the same boot_id and a consistent timestamp, neither starts a segment nor drops data.

**How it works.** Setup: s = sim.generate(SimConfig(), SHORT). Action: feed s.header_frame() at 0.0; _feed frames 0-399 from 0.0; feed s.header_frame(400) at host 4.0, a header whose t_us_at_header is t0 + 400 \* 10000 (the test comment calls it the answer to an 'H' request); _feed frames 400-599 from 4.0. Assertions: dec.stats.segments == 1; dec.stats.dropped == 0; dec.pending_rows() == 600.

**Notes.** Section 'C: device restarts'. The re-announcement path used by the live viewer is covered separately by test_reannounced_header_does_not_start_viewer_segments.

#### `test_restart_without_header_is_detected_from_the_data`
*test* · [`host/tests/test_v1_fixes.py:80`](../host/tests/test_v1_fixes.py#L80)

```python
def test_restart_without_header_is_detected_from_the_data()
```

**Purpose.** Checks that a restart whose start-up header was missed is detected from the data itself, and that frames whose sequence numbers were already seen are not rejected as duplicates.

**How it works.** Setup: s1 = sim.generate(SimConfig(t0_us=30_000_000), SHORT) (sequence 100-299 for 200 frames) and s2 = sim.generate(SimConfig(t0_us=900_000, seq0=90), SHORT); both keep boot_id 0x00000001. Action: feed the s1 header at 0.0; _feed 200 frames of s1 from 0.0; _feed 200 frames of s2 from 5.0 with no header sent. Assertions: dec.pending_rows() == 400; dec.stats.restarts_detected == 1; dec.stats.duplicates == 0.

**Notes.** Section 'C: device restarts'. The clock falls from about 32 s to 0.9 s, and the s2 sequence numbers 90-289 overlap s1's 100-289. The duplicate counter must stay at zero despite the overlap.

#### `test_true_duplicate_is_still_rejected`
*test* · [`host/tests/test_v1_fixes.py:91`](../host/tests/test_v1_fixes.py#L91)

```python
def test_true_duplicate_is_still_rejected()
```

**Purpose.** Checks that a repeated frame inside one continuous stream is still dropped as a duplicate, so the restart logic does not accept every repeat.

**How it works.** Setup: s = sim.generate(SimConfig(), SHORT). Action: feed the header at 0.0, then feed data frames 0, 1, 2, 2, 3 all at host time 0.0. Assertions: dec.pending_rows() == 4 and dec.stats.duplicates == 1.

**Notes.** Section 'C: device restarts'. This is the counterpart to the restart tests above.

#### `_write_sim_session`
*helper* · [`host/tests/test_v1_fixes.py:103`](../host/tests/test_v1_fixes.py#L103)

```python
def _write_sim_session(tmp_path, name='X')
```

**Purpose.** Writes a sample capture and session with the sim_device command-line entry point and returns the Parquet path.

**How it works.** Imports sim_device inside the function, then calls sim_device.main(['--write-session', str(tmp_path / f'{name}.parquet'), '--time-scale', '0.1']). Returns tmp_path / f'{name}.parquet'. The returned path is the one requested; the function does not check that the file exists.

**Notes.** The argument '--time-scale 0.1' is passed unchanged; its effect is defined by sim_device.main. The calling tests then use the .bin and labels sidecars written next to the Parquet file.

#### `test_replay_into_same_stem_keeps_labels`
*test* · [`host/tests/test_v1_fixes.py:110`](../host/tests/test_v1_fixes.py#L110)

```python
def test_replay_into_same_stem_keeps_labels(tmp_path)
```

**Purpose.** Checks that recovering a capture into a Parquet path with the same stem as the original keeps the label sidecar intact, including on a second recovery with --force.

**How it works.** Setup: pq = _write_sim_session(tmp_path); bin_path, labels_path = sidecar_paths(pq); before = read_label_events(labels_path); asserts len(before) > 3; then pq.unlink() to simulate a crash before the Parquet file was finalised. Action 1: logger.main(['--replay', str(bin_path), '--out', str(pq)]) must return 0. Assertions: read_label_events(labels_path) == before; the label values in load_session(pq).labels, in order, equal those in before. Action 2: the same call with '--force' added must return 0; the sidecar must still equal before.

**Notes.** Guards CHANGELOG 1.0.0 Fixed item: replay into the same stem used to empty X.labels.jsonl. Section 'A, H: replay'.

#### `test_replay_stops_cleanly_on_layout_change`
*test* · [`host/tests/test_v1_fixes.py:124`](../host/tests/test_v1_fixes.py#L124)

```python
def test_replay_stops_cleanly_on_layout_change(tmp_path)
```

**Purpose.** Checks that a replay of a capture that contains a channel-layout change stops at the change, finalises the Parquet file with the data before it, and returns 0.

**How it works.** Setup: s1 = sim.generate(SimConfig(), SHORT) (no abdomen IMU) and s2 = sim.generate(SimConfig(abdomen=True), SHORT). Writes mixed.bin as s1 header + 300 frames of s1 + s2 header + 100 frames of s2. Action: logger.main(['--replay', src, '--out', mixed_out.parquet]). Assertions: the return value is 0 and len(load_session(out).df) == 300.

**Notes.** Guards CHANGELOG 1.0.0 Fixed item: a layout change during replay or recording now stops cleanly and keeps the data received before it. Section 'A, H: replay'. The 100 frames after the change are not expected in the output.

#### `FakeSerial`
*class* · [`host/tests/test_v1_fixes.py:139`](../host/tests/test_v1_fixes.py#L139)

```python
class FakeSerial:
```

**Purpose.** In-memory stand-in for serial.Serial that serves queued byte chunks and imitates the pyserial 3.5 unplug on the first port.

**How it works.** Class attributes shared by all instances: instances (list of every constructed fake port), chunks (list of bytes, consumed first-in first-out by read()) and unplugged (False; set to True when the first port raises). test_logger_reconnects_after_posix_unplug and test_label_typed_while_reconnecting_is_kept reset all three. test_duration_is_honoured_while_waiting_to_reconnect resets only instances and chunks. Instance attributes set in __init__: n (index of this port in instances, so 0 for the first open) and reads (count of read() calls). Behaviour: the port with n == 0 raises OSError(5) from in_waiting once reads >= 20. Every other port serves chunks normally until the list is empty.

**Notes.** The class docstring says pyserial 3.5 on Linux and macOS raises a bare OSError from in_waiting when the device is unplugged, and that this fake reproduces it.

#### `FakeSerial.__init__`
*constructor* · [`host/tests/test_v1_fixes.py:146`](../host/tests/test_v1_fixes.py#L146)

```python
def __init__(self, port, baud, timeout=None)
```

**Purpose.** Registers a newly opened fake port.

**How it works.** Sets self.n = len(FakeSerial.instances) (0 for the first open), sets self.reads = 0, and appends self to FakeSerial.instances. The port, baud and timeout arguments are not used.

**Notes.** n is how in_waiting and FakeKeys.poll tell the first port from a reopened one.

#### `FakeSerial.in_waiting`
*property* · [`host/tests/test_v1_fixes.py:154`](../host/tests/test_v1_fixes.py#L154)

```python
@property def in_waiting(self)
```

**Purpose.** Emulates pyserial's in_waiting: the number of bytes ready to read, or the unplug error.

**How it works.** If self.n == 0 and self.reads >= 20, sets FakeSerial.unplugged = True and raises OSError(5, 'Input/output error'). Otherwise returns len(FakeSerial.chunks[0]) when chunks is non-empty, else 0. The value is the length of the next chunk only, not the total queued.

**Notes.** In test_label_typed_while_reconnecting_is_kept the first port serves 20 chunks before the unplug: the header plus nineteen 10-frame chunks. That accounts for the 190-row boundary asserted there.

#### `FakeSerial.read`
*method* · [`host/tests/test_v1_fixes.py:160`](../host/tests/test_v1_fixes.py#L160)

```python
def read(self, n)
```

**Purpose.** Returns the next queued byte chunk and counts the call.

**How it works.** Increments self.reads. Returns FakeSerial.chunks.pop(0) when the list is non-empty, otherwise b''. The n argument is ignored: a whole chunk comes back regardless of the requested size.

**Notes.** The read count drives the unplug threshold checked in in_waiting.

#### `FakeSerial.write`
*method* · [`host/tests/test_v1_fixes.py:164`](../host/tests/test_v1_fixes.py#L164)

```python
def write(self, data)
```

**Purpose.** Accepts writes and discards them.

**How it works.** Returns len(data). Nothing is stored, and no test in this file inspects written bytes.

#### `FakeSerial.close`
*method* · [`host/tests/test_v1_fixes.py:167`](../host/tests/test_v1_fixes.py#L167)

```python
def close(self)
```

**Purpose.** Stands in for closing the port.

**How it works.** Does nothing and returns None.

#### `test_logger_reconnects_after_posix_unplug`
*test* · [`host/tests/test_v1_fixes.py:171`](../host/tests/test_v1_fixes.py#L171)

```python
def test_logger_reconnects_after_posix_unplug(tmp_path, monkeypatch)
```

**Purpose.** Checks that the logger reopens the port after the OSError of a POSIX unplug, keeps all 600 frames, and records the reconnect in the session notes.

**How it works.** Setup: s = sim.generate(SimConfig(), SHORT); FakeSerial.instances = [] and FakeSerial.unplugged = False; FakeSerial.chunks = [s.header_frame()] followed by 60 chunks of 10 data frames each (frames 0-599); monkeypatch serial.Serial to FakeSerial. Action: logger.main(['--port', 'FAKE', '--out', rec.parquet, '--duration', '2.5', '--no-udp']) must return 0. Assertions: len(FakeSerial.instances) >= 2; len(load_session(out).df) == 600; 'reconnected' in sess.notes.

**Notes.** Guards CHANGELOG 1.0.0 Fixed item: the logger now reconnects after an unplug on Linux and macOS, where pyserial raises a plain OSError that was not caught and the logger exited. Section 'B: reconnect'. The patch works only because the logger opens its port through the serial.Serial attribute at open time.

#### `test_rate_does_not_bridge_an_invalid_breath`
*test* · [`host/tests/test_v1_fixes.py:190`](../host/tests/test_v1_fixes.py#L190)

```python
def test_rate_does_not_bridge_an_invalid_breath()
```

**Purpose.** Checks that a breath under an artifact is excluded from the rate: exactly one breath is marked invalid and no interval spanning it is used.

**How it works.** Setup: fs = 20.0; t = np.arange(0, 60, 1/fs); wave = 0.5 \* sin(2\*pi\*(15/60)\*t), which is 15 breaths per minute with peaks at 1, 5, 9 ... s. invalid is True for samples [580, 620), that is 29.0-31.0 s, covering the peak at 29 s. Action: detect_breaths(t, wave, fs, invalid, load_config(), n_full=len(t)\*5, fs_full=100.0), where n_full and fs_full describe the same 60 s at 100 Hz. Assertions: (~br.breath_valid).sum() == 1; np.allclose(br.rate_bpm, 15.0, atol=0.5).

**Notes.** Guards the CHANGELOG 1.0.0 Fixed bullet on breath rate: the rate now uses only intervals between consecutive breaths with no artifact or gap sample between them. Before, a breath hidden by an artifact left one double-length interval, here 25 s to 33 s (8 s, reported as 7.5 bpm, half the true rate). Section 'D: breath rate' (D is the test file's label, not a CHANGELOG number).

#### `test_ring_large_batch_keeps_order`
*test (parametrized)* · [`host/tests/test_v1_fixes.py:208`](../host/tests/test_v1_fixes.py#L208)

```python
@pytest.mark.parametrize('pre', [0, 3, 7]) def test_ring_large_batch_keeps_order(pre)
```

**Purpose.** Checks that live_view.Ring returns the newest samples in arrival order after a batch longer than its capacity, with 0, 3 or 7 samples added earlier.

**How it works.** Setup: r = Ring(5); seen = []; if pre is non-zero, r.extend(np.arange(pre)) and seen gets 0..pre-1. Action: r.extend(np.arange(100, 112)) (12 values, more than the capacity of 5), appending the same values to seen; then r.extend([7, 8]) and seen += [7, 8]. Assertion: r.ordered()[:, 0].tolist() == seen[-5:].

**Notes.** With pre = 7 the first extend is already longer than the capacity, so it goes through Ring.extend's large-batch branch before the 12-value batch arrives. Guards the live-view bullet under CHANGELOG 1.0.0 Fixed: the ring buffer kept the wrong order after a batch longer than the window. Section 'E: live view ring' (E is the test file's label).

#### `_reboot_stream`
*helper* · [`host/tests/test_v1_fixes.py:225`](../host/tests/test_v1_fixes.py#L225)

```python
def _reboot_stream(gap_host_s=5.0, label_at=300)
```

**Purpose.** Builds a live session with a reboot after a host-time gap and a label pressed inside the new segment, and returns the raw bytes, the label events and the live decoder's label column.

**How it works.** Setup: s1 = sim.generate(SimConfig(t0_us=40_000_000), SHORT) (seq0 default 100); s2 = sim.generate(SimConfig(t0_us=1_100_000, seq0=110), SHORT) with s2.header['boot_id'] = '0x00000002'. dec = StreamDecoder(); raw = bytearray(); t = 100.0. Loop 1: for the s1 header and its 1000 data frames, append the frame to raw, call dec.feed(frame, t), then t += 0.01. Then t += gap_host_s (5.0 by default). Loop 2: for the s2 header and its 1000 data frames, enumerated as i; when i == label_at + 1 (301, which is data frame 300 of s2), events.append(asdict(dec.set_label(4, t))) runs before that frame is fed; then the frame is appended to raw and fed at t, and t += 0.01. Returns (bytes(raw), events, dec.drain()['label']).

**Notes.** The label applies from s2 data frame 300 onward, which is row 1300 of the 2000-row session. Section 'review round 2'.

#### `test_replay_places_labels_like_live_after_a_reboot_gap`
*test* · [`host/tests/test_v1_fixes.py:250`](../host/tests/test_v1_fixes.py#L250)

```python
def test_replay_places_labels_like_live_after_a_reboot_gap()
```

**Purpose.** Checks that replaying the raw bytes with the recorded label events puts every label on the same row the live decoder used, across a reboot with a host-time gap.

**How it works.** Setup: raw, events, live = _reboot_stream(). rep = StreamDecoder(); rep.schedule_labels(json.loads(json.dumps(events))), a JSON round trip like a .labels.jsonl line read back. Action: rep.feed(raw, 0.0), the whole byte string in one call with the host clock fixed at 0.0; replay = rep.drain()['label']. Assertions: len(live) == len(replay) == 2000; np.array_equal(live, replay); int(np.argmax(live == 4)) == 1300.

**Notes.** Replay has no host clock, so label placement must come from the device-side data the events carry, not from host times. Guards CHANGELOG 1.0.0 Fixed item 2: before the fix, every label after a reconnect landed late by the length of the disconnection when a session was rebuilt with --replay. Section 'review round 2'.

#### `test_legacy_seq_only_label_events_still_replay`
*test* · [`host/tests/test_v1_fixes.py:261`](../host/tests/test_v1_fixes.py#L261)

```python
def test_legacy_seq_only_label_events_still_replay()
```

**Purpose.** Checks that label events in the pre-1.0 sidecar form, which carry only seq and label, still apply from the right sample on replay.

**How it works.** Setup: s = sim.generate(SimConfig(), SHORT); dec.schedule_labels([{'seq': s.cfg.seq0 + 50, 'label': 2}]), which is seq 150 with the default seq0 of 100. Action: for every (_, fr) in s.stream() (header, data and status frames), dec.feed(fr, 0.0); lab = dec.drain()['label']. Assertions: lab[:50] is all 0 and lab[50:] is all 2.

**Notes.** Covers the CHANGELOG 1.0.0 statement that .labels.jsonl files written by 0.1.0 still replay. Section 'review round 2'.

#### `test_reboot_after_long_uptime_without_header_is_detected`
*test* · [`host/tests/test_v1_fixes.py:271`](../host/tests/test_v1_fixes.py#L271)

```python
def test_reboot_after_long_uptime_without_header_is_detected()
```

**Purpose.** Checks that a restart after 50 minutes of uptime, with the start-up header missed, is detected from the data (restarts_detected == 1) and not counted as a forward gap.

**How it works.** Setup: count = 50 \* 60 \* 100 = 300000 ticks. s1 = sim.generate(SimConfig(t0_us=(count \* 10000) % 2**32, seq0=count % 2**16), SHORT), which gives t0_us = 3,000,000,000 and seq0 = 37856. s2 = sim.generate(SimConfig(t0_us=1_200_000, seq0=120), SHORT). Both keep boot_id 0x00000001. Action: feed the s1 header at 0.0; _feed 200 frames of s1 from 0.0; _feed 200 frames of s2 from 10.0 with no header. Assertions: dec.stats.restarts_detected == 1; dec.stats.dropped == 0; dec.pending_rows() == 400.

**Notes.** The test comment says that past 2**31 us (about 35.8 min) the new clock looks like a forward step, but the tick counter disagrees, so the event is a restart. CHANGELOG 1.0.0 Fixed item 1 gives the 35.8 min figure and the 30 minutes of lost frames this case used to produce. Section 'review round 2'.

#### `test_reannounced_header_does_not_start_viewer_segments`
*test* · [`host/tests/test_v1_fixes.py:285`](../host/tests/test_v1_fixes.py#L285)

```python
def test_reannounced_header_does_not_start_viewer_segments()
```

**Purpose.** Checks that a header the logger re-announces during a recording does not start a new segment in a viewer's decoder, and that the logger's re-announced copy carries no device timestamp.

**How it works.** Setup: s = sim.generate(SimConfig(), SHORT). rec = logger.Recorder.__new__(logger.Recorder), which skips __init__; then rec.dec = StreamDecoder(), rec.udp = None, rec.last_header_raw = None and rec.writer = _W(). Action 1: rec.feed(s.header_frame(), 0.0). Assertion 1: b't_us_at_header' not in rec.last_header_raw. Viewer: viewer = StreamDecoder(); viewer.feed(s.header_frame(), 0.0). Loop k = 0..4: _feed 100 frames from k\*100 at host time k; then viewer.feed(rec.last_header_raw, k + 0.99) and viewer.feed(s.header_frame(), k + 0.99), which is an identical copy of the original header. Assertions: viewer.stats.segments == 1 and viewer.stats.dropped == 0.

**Notes.** Guards the CHANGELOG 1.0.0 live-view item: the viewer started a spurious segment every time the logger re-announced the header, and the re-announced copy now carries no device timestamp. The docstring says the header is re-broadcast every 2 s; the loop comment says every second, and the loop feeds one re-announcement per second of data. Section 'review round 2'. The test sets Recorder's attributes by hand, so renaming dec, udp, last_header_raw or writer in logger.Recorder breaks it.

#### `_W`
*nested class* · [`host/tests/test_v1_fixes.py:291`](../host/tests/test_v1_fixes.py#L291)

```python
class _W:
```

**Purpose.** Stand-in writer given to rec.writer so Recorder.feed can run without opening a session file.

**How it works.** Class attribute header = None. Methods: write_raw(d) and write_batch(b) discard their argument; set_header(h) stores h as self.header. These are the only writer methods on the stand-in.

**Notes.** Defined inside test_reannounced_header_does_not_start_viewer_segments.

#### `_W.write_raw`
*method* · [`host/tests/test_v1_fixes.py:294`](../host/tests/test_v1_fixes.py#L294)

```python
def write_raw(self, d)
```

**Purpose.** Discards raw frame bytes.

**How it works.** Does nothing and returns None.

**Notes.** Nested in test_reannounced_header_does_not_start_viewer_segments.

#### `_W.set_header`
*method* · [`host/tests/test_v1_fixes.py:297`](../host/tests/test_v1_fixes.py#L297)

```python
def set_header(self, h)
```

**Purpose.** Records the header dict passed by the recorder.

**How it works.** Assigns self.header = h.

**Notes.** Nested in test_reannounced_header_does_not_start_viewer_segments.

#### `_W.write_batch`
*method* · [`host/tests/test_v1_fixes.py:300`](../host/tests/test_v1_fixes.py#L300)

```python
def write_batch(self, b)
```

**Purpose.** Discards a batch of decoded rows.

**How it works.** Does nothing and returns None.

**Notes.** Nested in test_reannounced_header_does_not_start_viewer_segments.

#### `FakeKeys`
*class* · [`host/tests/test_v1_fixes.py:315`](../host/tests/test_v1_fixes.py#L315)

```python
class FakeKeys:
```

**Purpose.** KeyReader stand-in that types the key '7' once, while the logger waits to reconnect.

**How it works.** Class attribute typed = False, set to True the first time a key is returned. Implements the context-manager methods __enter__ and __exit__. poll() returns ['7'] only when FakeSerial.unplugged is True, len(FakeSerial.instances) == 1 (the first port has been lost and not yet reopened), and FakeKeys.typed is False; otherwise it returns [].

**Notes.** Used by test_label_typed_while_reconnecting_is_kept, which installs it as logger.KeyReader.

#### `FakeKeys.__init__`
*constructor* · [`host/tests/test_v1_fixes.py:320`](../host/tests/test_v1_fixes.py#L320)

```python
def __init__(self)
```

**Purpose.** Creates the stand-in key reader.

**How it works.** Takes no arguments and stores nothing.

#### `FakeKeys.__enter__`
*method* · [`host/tests/test_v1_fixes.py:323`](../host/tests/test_v1_fixes.py#L323)

```python
def __enter__(self)
```

**Purpose.** Starts the context.

**How it works.** Returns self.

#### `FakeKeys.__exit__`
*method* · [`host/tests/test_v1_fixes.py:326`](../host/tests/test_v1_fixes.py#L326)

```python
def __exit__(self, *a)
```

**Purpose.** Ends the context without suppressing exceptions.

**How it works.** Does nothing and returns None, which is falsy, so an exception inside the with-block propagates.

#### `FakeKeys.poll`
*method* · [`host/tests/test_v1_fixes.py:329`](../host/tests/test_v1_fixes.py#L329)

```python
def poll(self)
```

**Purpose.** Reports one keypress during the reconnect wait.

**How it works.** Returns ['7'] exactly once: when FakeSerial.unplugged is True, exactly one fake port has been opened (the first one, now unplugged), and FakeKeys.typed is False. It sets FakeKeys.typed = True before returning. In every other case it returns [].

**Notes.** The in-line comment says the key is typed only after the unplug and before the port reopens.

#### `test_label_typed_while_reconnecting_is_kept`
*test* · [`host/tests/test_v1_fixes.py:337`](../host/tests/test_v1_fixes.py#L337)

```python
def test_label_typed_while_reconnecting_is_kept(tmp_path, monkeypatch)
```

**Purpose.** Checks that a label key pressed while the logger waits to reconnect is kept and applies from the first frame after the reconnect.

**How it works.** Setup: s = sim.generate(SimConfig(), SHORT); FakeSerial.instances = [], FakeKeys.typed = False, FakeSerial.unplugged = False; chunks as in the reconnect test (header plus 60 chunks of 10 frames); serial.Serial is replaced by FakeSerial and logger.KeyReader by FakeKeys. Action: logger.main(['--port', 'FAKE', '--out', rec.parquet, '--duration', '2.5', '--no-udp']) must return 0. Assertions: [e['label'] for e in sess.labels] == [7]; with lab = sess.df['label'], lab[:190] is all 0 and lab[-100:] is all 7.

**Notes.** The 190-row boundary is the 190 frames the first port delivers before the unplug (see FakeSerial.in_waiting). Guards CHANGELOG 1.0.0 Fixed item: label keys are kept while the logger waits to reconnect. Section 'B: reconnect'.

#### `DeadSerial`
*class* · [`host/tests/test_v1_fixes.py:354`](../host/tests/test_v1_fixes.py#L354)

```python
class DeadSerial(FakeSerial):
```

**Purpose.** FakeSerial whose reopen always fails, so the device never comes back.

**How it works.** Subclass of FakeSerial. Its __init__ raises OSError(2, 'No such file or directory') when FakeSerial.instances is non-empty, meaning a port was already opened; otherwise it calls FakeSerial.__init__. So the first open succeeds and unplugs like FakeSerial, and every later open fails.

#### `DeadSerial.__init__`
*constructor* · [`host/tests/test_v1_fixes.py:357`](../host/tests/test_v1_fixes.py#L357)

```python
def __init__(self, port, baud, timeout=None)
```

**Purpose.** Fails every reopen with ENOENT.

**How it works.** If FakeSerial.instances is non-empty, raises OSError(2, 'No such file or directory'). Otherwise calls super().__init__(port, baud, timeout).

#### `test_duration_is_honoured_while_waiting_to_reconnect`
*test* · [`host/tests/test_v1_fixes.py:363`](../host/tests/test_v1_fixes.py#L363)

```python
def test_duration_is_honoured_while_waiting_to_reconnect(tmp_path, monkeypatch)
```

**Purpose.** Checks that --duration ends the recording with exit code 0 while the device never reappears, and that the run does not hang.

**How it works.** Setup: s = sim.generate(SimConfig(), SHORT); FakeSerial.instances = []; FakeSerial.chunks = the header plus 400 single data frames; serial.Serial is replaced by DeadSerial; t0 = time.time(); real_sleep = time.sleep; time.sleep is replaced by watchdog_sleep. logger.time is the stdlib time module, so this patch is process-wide until monkeypatch restores it. Action: logger.main(['--port', 'FAKE', '--out', x.parquet, '--duration', '2', '--no-udp']) must return 0. Assertion: time.time() - t0 &lt; 4.0.

**Notes.** The test does not patch logger.KeyReader, so the real key reader runs. It does not reset FakeSerial.unplugged, which the preceding test leaves True. Only FakeKeys.poll reads that flag, and this test does not install FakeKeys, so the leftover value has no effect. Section 'B: reconnect'.

#### `watchdog_sleep`
*nested function* · [`host/tests/test_v1_fixes.py:375`](../host/tests/test_v1_fixes.py#L375)

```python
def watchdog_sleep(dt)
```

**Purpose.** Deadline guard installed in place of time.sleep during the --duration reconnect test.

**How it works.** If time.time() - t0 > 8.0, raises RuntimeError('logger did not stop at --duration while reconnecting'). Otherwise calls real_sleep(dt), the original time.sleep captured before patching. t0 is a closure variable from the test.

**Notes.** A logger that ignores --duration fails here instead of hanging the test run. Defined inside test_duration_is_honoured_while_waiting_to_reconnect.

#### `test_rate_does_not_bridge_an_interpolated_gap`
*test* · [`host/tests/test_v1_fixes.py:386`](../host/tests/test_v1_fixes.py#L386)

```python
def test_rate_does_not_bridge_an_interpolated_gap()
```

**Purpose.** Checks the breath rate when a 4 s artifact hole has been linearly filled: no interval spanning the hole is counted, and the rate is still about 15 bpm.

**How it works.** Setup: fs = 20.0; wave and t as in the previous test (15 bpm). a = int(27 \* fs) = 540 and b = int(31 \* fs) + 1 = 621; invalid[a:b] = True; wave[a:b] = np.linspace(wave[a], wave[b], b - a), the straight-line fill that the in-line comment says preprocessing applies over a long hole. Action: detect_breaths(t, wave, fs, invalid, load_config(), n_full=len(t)\*5, fs_full=100.0). Assertions: len(br.rate_bpm) > 5 and np.allclose(br.rate_bpm, 15.0, atol=0.5).

**Notes.** The breath at 29 s disappears under the fill. If the detector bridged the hole, an interval from 25 s to 33 s (8 s, 7.5 bpm) would appear. Guards the CHANGELOG 1.0.0 Fixed bullet on breath rate (a breath hidden by an artifact or data gap). Section 'review round 2'.

#### `test_phase_never_valid_on_invalid_samples`
*test* · [`host/tests/test_v1_fixes.py:401`](../host/tests/test_v1_fixes.py#L401)

```python
def test_phase_never_valid_on_invalid_samples()
```

**Purpose.** Checks that chest_abdomen_phase never marks an invalid sample valid, and that more than 80% of clean samples are still marked valid.

**How it works.** Setup: fs = 20.0; t = arange(0, 120, 1/fs); chest = sin(2\*pi\*0.25\*t) (4 s period); abd = -chest (exact anti-phase). invalid is True for samples [1000, 1060), that is 50-53 s. calib is True for the first 30 s (samples [0, 600)). Action: ph = chest_abdomen_phase(t, chest, abd, fs, calib, invalid, load_config()). Assertions: not (ph.valid & invalid).any(); ph.valid[~invalid].mean() > 0.8.

**Notes.** Guards CHANGELOG 1.0.0 Fixed item: chest-abdomen phase is no longer reported for samples that are themselves in a motion artifact or data gap. Section 'review round 2'. The in-line comment describes the hole as a 3 s artifact inside a clean 10 s window.

#### `test_report_needs_both_zoom_bounds`
*test* · [`host/tests/test_v1_fixes.py:418`](../host/tests/test_v1_fixes.py#L418)

```python
def test_report_needs_both_zoom_bounds(capsys)
```

**Purpose.** Checks that report.py exits with an error when --start is given without --end, and that the error message names both options.

**How it works.** Imports report. Inside pytest.raises(SystemExit) it calls report.main(['data/sample_synthetic.parquet', '--start', '10']). report.main checks (args.start is None) != (args.end is None) right after parsing and calls argparse's ap.error('--start and --end must be given together'), which writes to stderr and exits with status 2. Assertion: '--start and --end' in capsys.readouterr().err. The exit happens before load_config or load_session, so the session path is never opened and the test does not need that file to exist.

**Notes.** Guards CHANGELOG 1.0.0 Fixed item: report.py rejects --start without --end, and the reverse. Only the --start-without-end direction is tested here. Section 'review round 2'.

#### `test_simulator_noise_does_not_swing_at_the_ends`
*test* · [`host/tests/test_v1_fixes.py:426`](../host/tests/test_v1_fixes.py#L426)

```python
def test_simulator_noise_does_not_swing_at_the_ends()
```

**Purpose.** Checks that the simulator's smoothing (sim._smooth) does not make the noise larger in the first and last 50 samples than in the interior.

**How it works.** Setup: rng = np.random.default_rng(0); x = rng.standard_normal(20000); y = sim._smooth(x, 1000), a 1000-sample moving average with reflect padding because len(x) > n. interior = y[2000:-2000].std(). Assertions: abs(y[:50]).max() &lt; 6 \* interior and abs(y[-50:]).max() &lt; 6 \* interior.

**Notes.** Guards CHANGELOG 1.0.0 Fixed item: simulator smoothing used edge padding, which made the noise swing near the ends. The reflect mode and its reason are commented in host/kman_host/simulator.py at _smooth (line 122). The factor 6 is the tolerance.
