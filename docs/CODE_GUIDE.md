# KMAN code guide

What every part of the Step 1 code does and how each function works. Sections
follow the data path: firmware → wire protocol → host decoding and recording →
live view → offline analysis → tests. Every entry was written from the current
source and then checked against it by an independent reviewer pass. Line
numbers refer to the commit this guide ships with.


## Contents

- [1. The system in one picture](#1-the-system-in-one-picture)
- [2. One sample, end to end](#2-one-sample-end-to-end)
- [3. Ideas that recur everywhere](#3-ideas-that-recur-everywhere)
- [4. Known issues found while writing this guide](#4-known-issues-found-while-writing-this-guide)
- [5. Firmware: main loop, config, build](#5-firmware-main-loop-config-build)
- [6. Firmware: hardware abstraction (nRF52840)](#6-firmware-hardware-abstraction-nrf52840)
- [7. Firmware: sensor driver interface and drivers](#7-firmware-sensor-driver-interface-and-drivers)
- [8. Firmware: wire protocol, TX ring buffer, JSON writer, native test harness](#8-firmware-wire-protocol-tx-ring-buffer-json-writer-native-test-harness)
- [9. Host: protocol decoder and labels](#9-host-protocol-decoder-and-labels)
- [10. Host: stream decoder (unwrapping, gaps, segments, labels)](#10-host-stream-decoder-unwrapping-gaps-segments-labels)
- [11. Host: session files, UDP bus, keyboard input](#11-host-session-files-udp-bus-keyboard-input)
- [12. Host: command-line tools (logger, CSV export, simulated device)](#12-host-command-line-tools-logger-csv-export-simulated-device)
- [13. Host: synthetic physiology simulator](#13-host-synthetic-physiology-simulator)
- [14. Host: live viewer](#14-host-live-viewer)
- [15. Analysis: config, preprocessing, breathing](#15-analysis-config-preprocessing-breathing)
- [16. Analysis: phase, neck events, pipeline, plotting, report](#16-analysis-phase-neck-events-pipeline-plotting-report)
- [17. Tests](#17-tests)

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
* **Loss is always measurable.** Device-side counters (STATUS frame) and
  host-side gap counting (from timestamps) together tell you where frames were lost.
* **Labels apply from the next frame.** A key press creates an event at
  `last_seq + 1`. Replay applies the same events by `seq`, so a session rebuilt
  from `X.bin` has an identical label column.
* **Calibration-relative thresholds.** The first rest-breathing segment
  (label 1) is the per-session quiet reference: gyro bias, flex noise σ,
  abdomen polarity and phase amplitude gates all come from it.
* **No heap in the firmware.** All buffers are static and sized at compile time.

## 4. Known issues found while writing this guide

The reviewer pass compares each explanation with the code, so it also surfaced
places where the code doesn't do what the README promises. The first three are
reproduced and not yet fixed.

| # | Severity | Where | Problem |
|---|---|---|---|
| A | High | `host/logger.py` `run_replay` | Recovering with `--replay data/X.bin --out data/X.parquet` (the README command) **empties `X.labels.jsonl`**. Labels are read into memory first, so the rebuilt Parquet still has them, but `SessionWriter` reopens the same `.labels.jsonl` in `"w"` mode and replay never writes events back. A second replay then has no labels. |
| B | Medium | `host/logger.py` `run_live` | On Linux/macOS an unplug makes `ser.in_waiting` raise a plain `OSError`, not `SerialException`, so the logger **exits instead of reconnecting** (the `finally` block still saves the file). Windows reconnects. |
| C | High | `host/kman_host/stream.py` `_on_header`/`_on_data` + firmware `stream_id` | Unplugging the USB cable power-cycles the XIAO. After reboot the header comes back with `stream_id` 1, the same as before, so no new segment starts. The restarted device clock then looks like time going backwards, and **every frame after the reboot is discarded as a "duplicate"**. |
| D | Medium | `analysis/kman_analysis/breathing.py` `detect_breaths` | Rate intervals come from consecutive *valid* breaths, so one invalid breath (e.g. during a cough) between two valid ones gives a single interval covering two cycles, which reads as about half the true rate. Visible as the low cough-row rate in the sample report. |
| E | Low | `host/live_view.py` `Ring.extend` | When one batch is at least the window length (20 s of data at once, e.g. after a long stall), the ring stores it with the oldest sample at index 0 while `ordered()` expects it at `count % n`, so the trace is misordered from then on. |
| F | Low | `firmware/src/main.cpp` `initSensors` | `Wire.begin()` is called without pins, so overriding `KMAN_I2C_SDA_PIN`/`SCL_PIN` only changes which pins bus recovery toggles, not the I2C pins (the defaults D4/D5 are correct). |
| G | Low | `host/live_view.py` | `--screenshot` only works together with `--exit-after`. After a `LayoutChanged` error the message shows for one refresh and the plots freeze. In `--port` mode a serial disconnect raises on every refresh. |
| H | Low | `host/logger.py` `run_replay` | A `LayoutChanged` during replay ends with a traceback before the output Parquet is finalised. |
| I | Low | `host/kman_host/simulator.py` `_smooth` | `'edge'` padding of white noise makes the simulated rate and posture wander much more within half a window of the start and end of a session. This affects only synthetic data. |
| J | Note | `analysis/kman_analysis/phase.py` | Samples inside long data gaps can still get a valid phase value if at least half of their 10 s window is clean. Breaths inside gaps are detected but marked invalid. Only neck events are suppressed entirely inside gaps. |

## 5. Firmware: main loop, config, build

- **`firmware/src/main.cpp`**: The firmware application. It owns the four static sensor-driver objects and the slot table, probes the sensors once at boot, starts the 1 MHz hardware sample timer, and runs a loop that is woken by each timer tick (or by a ~5 ms timeout). Each pass detects the USB host (DTR) and queues the JSON session header. When a new tick has arrived, it samples every present sensor once for the newest tick into a DATA frame; any ticks in between are skipped and counted as overruns. It then queues periodic STATUS frames, drains the TX ring into USB CDC and drives the LEDs. All large buffers (frame scratch, header JSON and frame, the 8 KB TX ring) are static, and nothing is heap-allocated. Only small scratch arrays (ch[19], offset[4], the 42-byte STATUS body, devId[24]) live on the loop task's stack.
- **`firmware/include/config.h`**: Compile-time configuration for the firmware. Every KMAN_\* macro is wrapped in #ifndef, so a -DNAME=value in platformio.ini build_flags overrides it. It sets the sample rate, which sensor slots are fitted (with I2C addresses, flex pin and divider metadata), IMU ranges, I2C pins and speed, LED pins and polarity, SAADC settings, TX ring size and STATUS period. static_asserts reject invalid combinations when the code compiles. main.cpp and hal/hal_nrf52.cpp include it; the sampling path never reads configuration at run time.
- **`firmware/platformio.ini`**: PlatformIO build definition. It makes xiao_nrf52840 the default environment, selects the Arduino framework, enables extra warnings for the project's own sources only, pins the nordicnrf52 platform to a fixed git commit for reproducible builds, selects the xiaoble_adafruit board (Adafruit-based Seeed core with TinyUSB CDC and FreeRTOS), and sets KMAN_BOARD_NAME. It is also where any config.h value is overridden for each environment.

**How it connects.** The Adafruit nRF52 core's FreeRTOS loop_task calls setup() once and then loop() forever, with yield() between passes. main.cpp calls into hal/hal_nrf52.cpp for almost all hardware access: hal::init, ledInit/led, i2cBusRecover, startSampleTimer, latestTick, waitForTick, nowUs, clockIsCrystal, describeClock, deviceId, mcuName, and the USB CDC link hostConnected/hostRead/hostWritable/hostWrite/hostFlush. The exceptions are Wire.begin()/Wire.setClock() and Arduino's millis(), which main.cpp calls directly. The TIMER4_IRQHandler ISR in hal_nrf52.cpp is the timing master: it advances s_tickCount/s_tickTimeUs on the exact period grid and calls vTaskNotifyGiveFromISR, which wakes hal::waitForTick() at the top of loop(). Sensors are reached only through the abstract Sensor interface (sensors/sensor.h): Lsm6dsox::init/read/channelCount/channel/describeConfig/lastError, and FlexAdc's versions of the same plus warning(). FlexAdc::init calls hal::adcInit, FlexAdc::read calls hal::adcRead, and FlexAdc::describeConfig calls hal::describeAdc. Frames are built with proto::encodeFrame, proto::encodeStatus and the putU8/putU16/putI16/putU32 helpers (protocol.h/.cpp), and the header JSON with JsonWriter (json_writer.h). Frames are queued in ByteRing (ring_buffer.h). config.h values are also read by hal_nrf52.cpp (ADC settings and LED polarity). On the host, the bytes are parsed by host/kman_host/protocol.py: FrameParser.feed, SessionHeader.from_json, decode_data and decode_status. In host/kman_host/stream.py, StreamDecoder._on_header raises LayoutChanged if the layout key (channel names, scales and period_us) changes, and otherwise starts a new segment when stream_id or device_id changes. StreamDecoder._on_data turns t_us/seq jumps into dropped-frame counts and GapEvents. host/logger.py run_live (open_port) and host/live_view.py open the serial port; pyserial asserts DTR by default, which triggers startStreaming(). Both scripts also write CMD_SEND_HEADER ('H'), and logger.py re-sends it every 2 s while it has no header. CMD_SEND_STATUS ('S') is defined in protocol.py, but no host script currently sends it. host/sim_device.py and kman_host/simulator.py (SimSession.header_frame/data_frame/status_frame) emulate this firmware's frame stream, and sim_device.py's pty mode answers 'H' by writing a header frame.

### `firmware/include/config.h`

#### `Firmware identity (KMAN_FW_VERSION, KMAN_BOARD_NAME)`
*config* · [`firmware/include/config.h:8`](../firmware/include/config.h#L8)

```cpp
#define KMAN_FW_VERSION "0.1.0"; #define KMAN_BOARD_NAME "unknown"
```

**Purpose.** String identity that goes into the session header, so every recording carries the firmware version and board.

**How it works.** KMAN_FW_VERSION defaults to "0.1.0" and KMAN_BOARD_NAME to "unknown". buildHeaderFrame() emits them as "fw_version" and "board". platformio.ini overrides KMAN_BOARD_NAME to "xiao_nrf52840". The file includes &lt;Arduino.h> first, because later defaults use core macros (A0, PIN_WIRE_SDA, LED_GREEN).

#### `Sampling (KMAN_SAMPLE_RATE_HZ)`
*config* · [`firmware/include/config.h:16`](../firmware/include/config.h#L16)

```cpp
#define KMAN_SAMPLE_RATE_HZ 100
```

**Purpose.** Sets the frame rate of the whole system: one DATA frame per hardware timer tick.

**How it works.** Default 100 Hz. main.cpp derives kPeriodUs = 1e6 / rate (10000 us) for hal::startSampleTimer(). Each Lsm6dsox constructor receives it to choose an ODR >= 2x the rate. It is reported in the header as sample_rate_hz.

**Notes.** The 1 MHz timer requires the period to be a whole number of microseconds (enforced by static_assert).

#### `Sensor slots (KMAN_CHEST_IMU, KMAN_ABDOMEN_IMU, KMAN_NECK_FLEX, KMAN_NECK_IMU and their parameters)`
*config* · [`firmware/include/config.h:23`](../firmware/include/config.h#L23)

```cpp
KMAN_CHEST_IMU 1 / _ADDR 0x6A; KMAN_ABDOMEN_IMU 0 / _ADDR 0x6B; KMAN_NECK_FLEX 1 / KMAN_FLEX_PIN A0 / KMAN_FLEX_SENSOR_DESC / KMAN_FLEX_R_FIXED_OHM 47000 / KMAN_FLEX_TOPOLOGY; KMAN_NECK_IMU 0 / _ADDR 0x6B / KMAN_NECK_IMU_ACCEL_ONLY 0
```

**Purpose.** Declares which sensors are fitted and how they are attached. A fitted slot is probed at boot; one that fails is reported in the header and left out of the frame layout.

**How it works.**

- KMAN_CHEST_IMU 1, KMAN_CHEST_IMU_ADDR 0x6A (LSM6DSOX SA0/SDO low, the usual breakout default).
- KMAN_ABDOMEN_IMU 0, KMAN_ABDOMEN_IMU_ADDR 0x6B (second LSM6DSOX with SA0/SDO high).
- KMAN_NECK_FLEX 1, KMAN_FLEX_PIN A0 (XIAO D0 = P0.02 = AIN0). KMAN_FLEX_SENSOR_DESC (placeholder text), KMAN_FLEX_R_FIXED_OHM 47000 and KMAN_FLEX_TOPOLOGY "3V3 - flex - ADC - R_fixed - GND" are metadata only; FlexAdc::describeConfig() writes them into the header so the host can convert counts to resistance.
- KMAN_NECK_IMU 0 (optional, for head-pose rejection), KMAN_NECK_IMU_ADDR 0x6B, KMAN_NECK_IMU_ACCEL_ONLY 0 (1 = 3 accel channels, 0 = 6 channels).
main.cpp turns each enable into Slot.configured (macro != 0) and passes addresses/pins to the static driver constructors. With the defaults the frame carries 7 channels (chest 6 + flex 1).

**Notes.** Only two LSM6DSOX addresses exist on one bus, so the neck IMU can share the bus with the chest IMU only if the abdomen IMU is not fitted.

#### `IMU settings (KMAN_IMU_ACCEL_RANGE_G, KMAN_IMU_GYRO_RANGE_DPS)`
*config* · [`firmware/include/config.h:70`](../firmware/include/config.h#L70)

```cpp
#define KMAN_IMU_ACCEL_RANGE_G 2; #define KMAN_IMU_GYRO_RANGE_DPS 250
```

**Purpose.** Full-scale ranges for all LSM6DSOX instances. Small-motion ranges keep resolution high for respiration and swallow signals.

**How it works.** Passed to every Lsm6dsox constructor in main.cpp, which maps them to CTRL1_XL/CTRL2_G full-scale bits and to the per-channel scale reported in the header.

**Notes.** The comment says the driver tables support only accel 2/4/8/16 g and gyro 125/250/500/1000/2000 dps; config.h does not check other values with a static_assert.

#### `Pins / buses (KMAN_I2C_SDA_PIN, KMAN_I2C_SCL_PIN, KMAN_I2C_HZ)`
*config* · [`firmware/include/config.h:80`](../firmware/include/config.h#L80)

```cpp
#define KMAN_I2C_SDA_PIN PIN_WIRE_SDA; #define KMAN_I2C_SCL_PIN PIN_WIRE_SCL; #define KMAN_I2C_HZ 400000
```

**Purpose.** I2C pin and clock settings for the IMU bus.

**How it works.** SDA defaults to the core's PIN_WIRE_SDA (XIAO D4) and SCL to PIN_WIRE_SCL (XIAO D5). initSensors() passes them to hal::i2cBusRecover() and then calls Wire.setClock(KMAN_I2C_HZ) at 400 kHz (fast mode).

**Notes.** Wire.begin() is called without pins, so changing KMAN_I2C_SDA_PIN/SCL_PIN changes only which pins bus recovery toggles, not the pins the Wire peripheral uses.

#### `LEDs (KMAN_LED_OK_PIN, KMAN_LED_ERR_PIN, KMAN_LED_ACTIVE_LOW)`
*config* · [`firmware/include/config.h:91`](../firmware/include/config.h#L91)

```cpp
#define KMAN_LED_OK_PIN LED_GREEN; #define KMAN_LED_ERR_PIN LED_RED; #define KMAN_LED_ACTIVE_LOW 1
```

**Purpose.** Status LED pins and polarity: a blinking OK LED means streaming, a solid ERR LED means a sensor error.

**How it works.** OK = LED_GREEN, ERR = LED_RED (the XIAO RGB LED). KMAN_LED_ACTIVE_LOW 1 because that LED is common-anode. hal::led() writes HIGH when (on != ACTIVE_LOW), so 'on' drives the pin LOW. setup() initialises both pins and updateLeds() drives them.

**Notes.** On single-LED boards, set both pins to the same LED; updateLeds() then gives the error state priority.

#### `ADC (KMAN_ADC_BITS, KMAN_ADC_OVERSAMPLE, KMAN_ADC_TACQ_US)`
*config* · [`firmware/include/config.h:103`](../firmware/include/config.h#L103)

```cpp
#define KMAN_ADC_BITS 14; #define KMAN_ADC_OVERSAMPLE 16; #define KMAN_ADC_TACQ_US 10
```

**Purpose.** nRF52840 SAADC settings for the flex-sensor channel.

**How it works.** hal::adcInit() (called from FlexAdc::init()) applies them: analogReadResolution(14), analogOversampling(16), which also enables burst mode (one SAMPLE task -> 16 conversions, about 12 us each), and analogSampleTime(10). hal::adcFullScale() = 2^14 - 1 = 16383. hal::describeAdc() reports all three in the header. The reference (VDD/4 with gain 1/4, ratiometric) is fixed in the HAL, not here.

**Notes.** Comments: 14 bits is meaningful only with oversampling. Oversample must be a power of two in 1..256. Tacq must be 3/5/10/15/20/40 us, and 10 us suits source impedance &lt;= 100 kOhm.

#### `Transport (KMAN_TX_RING_BYTES, KMAN_STATUS_PERIOD_MS)`
*config* · [`firmware/include/config.h:114`](../firmware/include/config.h#L114)

```cpp
#define KMAN_TX_RING_BYTES 8192; #define KMAN_STATUS_PERIOD_MS 1000
```

**Purpose.** Sets the size of the outgoing byte queue that absorbs host/USB stalls, and the interval between STATUS frames.

**How it works.** KMAN_TX_RING_BYTES is the template size of g_tx (ByteRing&lt;8192>). KMAN_STATUS_PERIOD_MS is the minimum interval queueStatusIfDue() keeps between STATUS frames when none is requested.

**Notes.** The comment estimates about 2.5 s of frames at 100 Hz. From the frame sizes: the default 28-byte DATA frames plus a 49-byte STATUS per second come to about 2.85 KB/s, so roughly 2.9 s. With all 19 channels (52-byte frames) it is about 1.6 s.

#### `Sanity static_asserts`
*config* · [`firmware/include/config.h:122`](../firmware/include/config.h#L122)

```cpp
static_assert(1000000UL % KMAN_SAMPLE_RATE_HZ == 0, ...); static_assert(KMAN_SAMPLE_RATE_HZ >= 1 && KMAN_SAMPLE_RATE_HZ <= 416, ...); three pairwise I2C address checks
```

**Purpose.** Rejects configurations that cannot work, at compile time instead of on the bench.

**How it works.**

1. 1e6 % rate == 0: the period must be a whole number of microseconds.
2. 1 &lt;= rate &lt;= 416: the IMU ODR is chosen >= 2x the frame rate and tops out at 833 Hz.
3-5. For each pair (abdomen, neck), (chest, abdomen), (chest, neck): fails if both slots are enabled and their addresses are equal.

**Notes.** Both rate asserts together make 400 Hz the highest legal rate, because 416 does not divide 1e6. Legal examples: 100, 125, 200, 250, 400. Nothing checks the IMU range values or the flex/ADC settings.

### `firmware/platformio.ini`

#### `[platformio] section`
*config* · [`firmware/platformio.ini:9`](../firmware/platformio.ini#L9)

```
default_envs = xiao_nrf52840
```

**Purpose.** Selects the environment that a bare `pio run` builds and uploads.

**How it works.** default_envs = xiao_nrf52840. The header comment gives the commands: build `pio run -e xiao_nrf52840`, flash `pio run -e xiao_nrf52840 -t upload` (double-tap RESET to enter the bootloader if upload cannot find the port).

#### `[env] common section`
*config* · [`firmware/platformio.ini:12`](../firmware/platformio.ini#L12)

```
framework = arduino; build_src_flags = -Wall -Wextra -Wno-unused-parameter
```

**Purpose.** Settings that every environment inherits.

**How it works.** framework = arduino. build_src_flags apply only to the project's own sources (src/), not to the framework or libraries: -Wall -Wextra for strict warnings, plus -Wno-unused-parameter.

**Notes.** Comments: the framework is not -Wextra clean, which is why the flags go in build_src_flags; SoftDevice headers trigger -Wunused-parameter.

#### `[env:xiao_nrf52840]`
*config* · [`firmware/platformio.ini:20`](../firmware/platformio.ini#L20)

```
platform = https://github.com/maxgerhardt/platform-nordicnrf52#cac6fcf943a41accd2aeb4f3659ae297a73f422e; board = xiaoble_adafruit; build_flags = -DKMAN_BOARD_NAME=\"xiao_nrf52840\"
```

**Purpose.** The build target for the Seeed XIAO nRF52840 on the non-mbed, Adafruit-based Seeed core (TinyUSB CDC + FreeRTOS), which the HAL is written against.

**How it works.** platform: maxgerhardt's platform-nordicnrf52 git repository pinned to commit cac6fcf9... (comment: a known-good commit, for reproducible builds). board: xiaoble_adafruit. build_flags (applied to all compiled code) defines KMAN_BOARD_NAME as the string literal "xiao_nrf52840"; the escaped quotes make it a C string. Other config.h values can be added here, e.g. -DKMAN_SAMPLE_RATE_HZ=200 -DKMAN_ABDOMEN_IMU=1.

**Notes.** There are no lib_deps; Adafruit_TinyUSB and Wire come with the core. main.cpp includes &lt;Adafruit_TinyUSB.h> because the core needs it in the sketch to link USB CDC.

### `firmware/src/main.cpp`

#### `kPeriodUs`
*config* · [`firmware/src/main.cpp:24`](../firmware/src/main.cpp#L24)

```cpp
const uint32_t kPeriodUs = 1000000UL / KMAN_SAMPLE_RATE_HZ;
```

**Purpose.** Sample period in microseconds on the 1 MHz timer timebase, derived once from the configured frame rate.

**How it works.** Integer division of 1,000,000 by KMAN_SAMPLE_RATE_HZ. With the default 100 Hz it is 10000 us. It is passed to hal::startSampleTimer() in setup() and written into the session header as "period_us" by buildHeaderFrame().

**Notes.** The static_assert in config.h guarantees that 1e6 % KMAN_SAMPLE_RATE_HZ == 0, so the division is exact and tick times fall on a whole-microsecond grid.

#### `Sensor driver objects (g_chestImu, g_abdomenImu, g_neckFlex, g_neckImu)`
*config* · [`firmware/src/main.cpp:28`](../firmware/src/main.cpp#L28)

```cpp
Lsm6dsox g_chestImu(Wire, KMAN_CHEST_IMU_ADDR, false, KMAN_SAMPLE_RATE_HZ, KMAN_IMU_ACCEL_RANGE_G, KMAN_IMU_GYRO_RANGE_DPS); Lsm6dsox g_abdomenImu(...KMAN_ABDOMEN_IMU_ADDR...); FlexAdc g_neckFlex(KMAN_FLEX_PIN, KMAN_FLEX_SENSOR_DESC, KMAN_FLEX_R_FIXED_OHM, KMAN_FLEX_TOPOLOGY); Lsm6dsox g_neckImu(...KMAN_NECK_IMU_ADDR, KMAN_NECK_IMU_ACCEL_ONLY...);
```

**Purpose.** One statically allocated driver instance for each possible sensor position, so the slot table can point at them without dynamic allocation.

**How it works.**

1. g_chestImu: LSM6DSOX on the shared `Wire` bus at KMAN_CHEST_IMU_ADDR (default 0x6A), accel+gyro (accelOnly=false), frame rate KMAN_SAMPLE_RATE_HZ, ranges KMAN_IMU_ACCEL_RANGE_G (2 g) and KMAN_IMU_GYRO_RANGE_DPS (250 dps).
2. g_abdomenImu: same settings at KMAN_ABDOMEN_IMU_ADDR (default 0x6B).
3. g_neckFlex: FlexAdc on KMAN_FLEX_PIN (A0), with description string, fixed divider resistor (47000 ohm) and topology string. The last three are header metadata only.
4. g_neckImu: LSM6DSOX at KMAN_NECK_IMU_ADDR (default 0x6B). accelOnly = KMAN_NECK_IMU_ACCEL_ONLY gives 3 channels if 1 and 6 channels if 0.
The constructors run before setup() for all four objects, whether or not their slot is fitted. The Lsm6dsox constructor selects the IMU ODR (the smallest of 208/416/833 Hz that is >= 2 x frame rate). No bus traffic happens until initSensors() calls init() on a configured slot.

**Notes.** All three IMUs share the one `Wire` (TWIM) bus, and an LSM6DSOX can only sit at 0x6A or 0x6B, so at most two can work at once. config.h's static_asserts only reject two enabled IMUs that have the same configured address. They do not check that the addresses are 0x6A/0x6B, so a third IMU given another address would compile, then fail init() at boot (no ACK) and be reported as init_failed. The Lsm6dsox constructor also silently replaces unsupported range values with 2 g / 250 dps.

#### `SlotState`
*enum* · [`firmware/src/main.cpp:36`](../firmware/src/main.cpp#L36)

```cpp
enum SlotState : uint8_t { kNotConfigured, kInitFailed, kOk };
```

**Purpose.** Records the boot-time outcome for each sensor slot.

**How it works.** kNotConfigured (0): the slot is disabled by its KMAN_\* macro and is never probed. kInitFailed (1): the slot is configured but drv->init() returned false. kOk (2): init succeeded and the slot contributes channels to every DATA frame. initSensors() sets the state. buildHeaderFrame() and sampleAndQueue() read it, and buildHeaderFrame() maps it to the header strings "not_configured" / "init_failed" / "ok".

#### `Slot`
*struct* · [`firmware/src/main.cpp:38`](../firmware/src/main.cpp#L38)

```cpp
struct Slot { const char* id; const char* prefix; uint8_t bit; bool configured; Sensor* drv; SlotState state; };
```

**Purpose.** Binds one physical sensor position to its driver, header name, channel-name prefix and mask bit, so the rest of main.cpp can loop over sensors generically.

**How it works.**

Fields:
- id: slot name in the session header ("chest_imu", "abdomen_imu", "neck_flex", "neck_imu").
- prefix: joined with each channel suffix by '_' to form channel names ("chest" + "ax" -> "chest_ax").
- bit: bit index in the header's present_mask and in each DATA frame's valid byte.
- configured: compile-time enable (KMAN_&lt;SLOT> != 0).
- drv: pointer to the driver through the abstract Sensor interface (init/read/channelCount/channel/describeConfig/lastError/warning).
- state: SlotState result of the boot probe; initial value kNotConfigured.

#### `g_slots slot table (with kNumSlots, g_presentMask, g_sensorError)`
*config* · [`firmware/src/main.cpp:47`](../firmware/src/main.cpp#L47)

```cpp
Slot g_slots[proto::kStatusSlots] = {...}; const uint8_t kNumSlots; uint8_t g_presentMask = 0; bool g_sensorError = false;
```

**Purpose.** The fixed list of sensor positions in slot order. Slot order sets the DATA payload channel order and the index into STATUS read_errors[].

**How it works.**

Four entries, sized by proto::kStatusSlots (4) so that the table and the STATUS frame's read_errors[4] cannot get out of step:
- index 0: {"chest_imu", "chest", bit 0, KMAN_CHEST_IMU != 0, &g_chestImu}
- index 1: {"abdomen_imu", "abd", bit 1, KMAN_ABDOMEN_IMU != 0, &g_abdomenImu}
- index 2: {"neck_flex", "flex", bit 2, KMAN_NECK_FLEX != 0, &g_neckFlex}
- index 3: {"neck_imu", "neck", bit 3, KMAN_NECK_IMU != 0, &g_neckImu}
Every entry starts with state kNotConfigured. kNumSlots = sizeof g_slots / sizeof g_slots[0] = 4. g_presentMask collects (1 &lt;&lt; bit) for every slot whose init succeeded; with the default config (chest IMU + flex both OK) it is 0b101 = 5. g_sensorError becomes true if any configured slot fails init. It drives the ERR LED and STATUS flag bit1.

**Notes.** bit equals the array index in every entry, and sampleAndQueue indexes read_errors[] by array index.

#### `Frame buffers and TX ring (kMaxChannels, kDataHeaderLen, g_dataBody, g_frame, g_headerJson, g_headerFrame, g_tx)`
*config* · [`firmware/src/main.cpp:60`](../firmware/src/main.cpp#L60)

```cpp
const size_t kMaxChannels = 6 + 6 + 1 + 6; const size_t kDataHeaderLen = 4 + 2 + 1; uint8_t g_dataBody[...]; uint8_t g_frame[...]; char g_headerJson[4000]; uint8_t g_headerFrame[...]; ByteRing<KMAN_TX_RING_BYTES> g_tx;
```

**Purpose.** Static storage for building frames and for queuing outgoing bytes, so the loop task's stack is never used for large buffers and nothing is heap-allocated.

**How it works.**

- kMaxChannels = 19: worst case of chest IMU (6) + abdomen IMU (6) + flex (1) + neck IMU (6).
- kDataHeaderLen = 7: t_us u32 + seq u16 + valid u8.
- g_dataBody[7 + 2\*19 = 45]: DATA body scratch used by sampleAndQueue().
- g_frame[kOverhead 7 + kDataHeaderLen 7 + 2\*kMaxChannels 38 + kStatusBodyLen 42 = 94]: encoded-frame scratch shared by sampleAndQueue() (DATA, at most 52 bytes) and queueStatusIfDue() (STATUS, 49 bytes). Its size is one frame overhead plus the largest DATA body (45) plus the STATUS body (42). That is not the sum of the two frame sizes (101), but it is bigger than either frame, so it holds whichever one is being built.
- g_headerJson[4000]: JSON text of the session header (the comment estimates about 1.7 KB with chest+flex and about 3.1 KB with three IMUs + flex).
- g_headerFrame[4000 + 7 = 4007]: the encoded HEADER frame.
- g_tx: ByteRing&lt;KMAN_TX_RING_BYTES = 8192>, a single-producer/single-consumer byte FIFO. push() is all-or-nothing (a frame is never split). peekContiguous()/consume() give zero-copy reads. highWater() tracks peak fill, and resetHighWater() sets it to the current fill.

**Notes.** Sharing g_frame is safe because producers and consumer all run in the loop task and push() copies the bytes into the ring before g_frame is reused. JsonWriter always keeps room for a NUL terminator, so the JSON text is at most 3999 bytes. The frame len field (type + body) is then at most 4000, and the whole frame at most 4006 bytes. That is under proto::kMaxLen (4096) and fits g_headerFrame (4007), so encodeFrame() cannot reject the header for size. The STATUS body is built in a 42-byte local array on the stack inside queueStatusIfDue(), not in these static buffers.

#### `Run state (g_streaming, g_headerPending, g_statusPending, g_streamId, g_lastTickCount, g_lastStatusMs, g_status, g_maxLatencyUs)`
*config* · [`firmware/src/main.cpp:72`](../firmware/src/main.cpp#L72)

```cpp
bool g_streaming, g_headerPending, g_statusPending; uint32_t g_streamId, g_lastTickCount, g_lastStatusMs; proto::Status g_status; uint16_t g_maxLatencyUs;
```

**Purpose.** The mutable state of the streaming state machine and the health counters reported in STATUS frames.

**How it works.**

All of these are static and start at zero/false.
- g_streaming: true while the host has the port open (DTR). DATA/HEADER/STATUS frames are produced only while it is true.
- g_headerPending: a HEADER frame must be queued before any further DATA frame. Set on stream start and on the 'H' command.
- g_statusPending: forces a STATUS frame on the next pass regardless of the period. Set on stream start and on the 'S' command.
- g_streamId: incremented on every startStreaming(), reported as header "stream_id".
- g_lastTickCount: the last timer tick count that loop() handled.
- g_lastStatusMs: millis() timestamp of the last STATUS frame that was successfully queued.
- g_status: proto::Status {t_us, ticks, frames_sent, overruns, tx_drops, read_errors[4], max_latency_us, tx_high_water, flags}.
- g_maxLatencyUs: largest tick-to-frame-queued latency since the last STATUS, saturated at 0xFFFF.

**Notes.** g_streamId lives in RAM, so it restarts at 1 for the first connection after every reset.

#### `initSensors`
*function* · [`firmware/src/main.cpp:83`](../firmware/src/main.cpp#L83)

```cpp
void initSensors()
```

**Purpose.** Brings up the I2C bus and probes each configured sensor once at boot. Records which slots work, so that the frame layout contains only working sensors.

**How it works.**

1. hal::i2cBusRecover(KMAN_I2C_SDA_PIN, KMAN_I2C_SCL_PIN): if SDA reads low (a slave stuck mid-transfer after an MCU reset), toggles SCL up to 9 times (stopping early once SDA is released) and then generates a STOP.
2. Wire.begin(); Wire.setClock(KMAN_I2C_HZ) (400 kHz default).
3. For each slot i in 0..3: skip it if !configured (state stays kNotConfigured). Otherwise call s.drv->init(). On true: state = kOk and g_presentMask |= (1 &lt;&lt; s.bit). On false: state = kInitFailed and g_sensorError = true.
Lsm6dsox::init() checks WHO_AM_I (0x6C), does a software reset, writes the control registers and reads each one back to verify it, waits 100 ms and does a first data read. Any failure returns false and sets lastError(). FlexAdc::init() calls hal::adcInit() (configures the SAADC) and takes 8 test readings. It sets warning() if all 8 are within fs/50 (about 2 %) of either rail, but it always returns true. With the stock drivers, only an IMU slot can end up kInitFailed and set g_sensorError (ERR LED, STATUS bit1). A disconnected flex sensor shows up only as a "warning" in the header.

**Notes.** Probing happens only once. A sensor that fails here stays out of the layout until the next reset, while the other sensors keep streaming. Wire.begin() takes no pin arguments, so the TWI peripheral uses the core's default pins; KMAN_I2C_SDA_PIN/SCL_PIN are used only by the bus-recovery routine.

#### `buildHeaderFrame`
*function* · [`firmware/src/main.cpp:103`](../firmware/src/main.cpp#L103)

```cpp
size_t buildHeaderFrame()
```

**Purpose.** Serialises the self-describing JSON session header (device identity, timing, frame format, sensor configs and exact channel layout) and encodes it as a HEADER frame in g_headerFrame. The host needs this header to decode DATA frames.

**How it works.**

1. Creates JsonWriter w over g_headerJson (4000 B). Fills devId[24] via hal::deviceId(): 16 uppercase hex chars from FICR DEVICEID[1] then DEVICEID[0].
2. Writes root members in this order: type "kman_session_header", protocol (proto::kProtocolVersion = 1), fw_version (KMAN_FW_VERSION), build (__DATE__ " " __TIME__, the compile time of main.cpp), board (KMAN_BOARD_NAME), mcu (hal::mcuName() = "nRF52840"), device_id, stream_id (g_streamId), t_us_at_header (hal::nowUs() at build time), sample_rate_hz, period_us (kPeriodUs).
3. Writes object "clock", filled by hal::describeClock(): source, hfclk (crystal vs RC), t_unit "us", t_bits 32, seq_bits 16.
4. Writes object "frame", a fixed description: sync "AA55", len and crc descriptions, data_prefix ["t_us:u32","seq:u16","valid:u8"], channel_type "i16".
5. Writes present_mask (g_presentMask).
6. Writes array "sensors" with one object for each of the 4 slots, including unconfigured ones: slot, bit, status ("ok" / "init_failed" / "not_configured"). For configured slots it adds the driver's describeConfig() members, "error": lastError() if init failed, and "warning" if warning() is non-empty.
7. Writes array "channels": for each kOk slot in slot order, for each channel c &lt; channelCount(): name = prefix + "_" + suffix (kvJoined), slot, bit, unit, and scale via kvSci. kvSci writes mantissa e exp10 (e.g. 61e-6 for the 2 g accel channels) or just the mantissa when exp10 == 0 (the flex channel's scale is written as 1). physical = raw \* scaleMantissa \* 10^scaleExp10. This order is the order of the int16 values in every DATA frame.
8. Closes the root object. If !w.ok() (buffer overflow, an attempt to nest 8 levels deep, or unbalanced), rebuilds a minimal valid header {"type":"kman_session_header","error":"header overflow"} in the same buffer and encodes that instead.
9. Returns proto::encodeFrame(kTypeHeader 0x02, json, length, g_headerFrame, 4007): JSON length + 7 bytes, or 0 if encodeFrame refuses (which the buffer sizes rule out).

**Notes.** The header is rebuilt on every request, so t_us_at_header reflects the moment of the most recent build. The comment calls the overflow fallback 'never expected' because the buffer is sized with margin.

#### `sampleAndQueue`
*function* · [`firmware/src/main.cpp:190`](../firmware/src/main.cpp#L190)

```cpp
void sampleAndQueue(uint32_t tickCount, uint32_t tickUs)
```

**Purpose.** Reads every present sensor once for one timer tick and queues the resulting DATA frame, stamped with the tick's scheduled time and the tick count as sequence number.

**How it works.**

1. Locals: int16_t ch[19], n = 0, valid = 0.
2. Layout pass in slot order 0..3: offset[i] = running total, which is increased by drv->channelCount() for each kOk slot. n = total channel count. Payload position therefore follows slot order, whatever order the reads happen in.
3. Read pass in kOrder = {2, 0, 1, 3} (neck_flex, chest_imu, abdomen_imu, neck_imu): skip slots that are not kOk. If s.drv->read(&ch[offset[i]]) returns true, set valid |= (1 &lt;&lt; s.bit). On false, zero that slot's channelCount() values (keeping the fixed layout) and ++g_status.read_errors[i].
4. Serialises the body into g_dataBody, little-endian: putU32(tickUs), putU16((uint16_t)tickCount) (the low 16 bits of the 32-bit tick count are the seq), putU8(valid), then putI16 for each of the n channels. Body length = 7 + 2n.
5. proto::encodeFrame(kTypeData 0x01, g_dataBody, len, g_frame, 94) gives a frame of 2n + 14 bytes (28 B with the default chest IMU + flex, 52 B with all 19 channels).
6. g_tx.push(): on success ++g_status.frames_sent. If the ring lacks room, the frame is dropped whole and ++g_status.tx_drops.
7. latency = hal::nowUs() - tickUs (unsigned, safe across wrap). If it exceeds g_maxLatencyUs, stores min(latency, 0xFFFF).

**Notes.** The fastest read goes first (comment: ADC ~0.2 ms, IMU burst ~0.35 ms at 400 kHz) so that all samples are taken within about 1 ms of the tick. kOrder is hard-coded for exactly 4 slots. A cleared valid bit tells the host to discard that slot's zeros. frames_sent counts frames placed in the ring, not bytes delivered over USB. seq counts ticks since the timer started, not since the stream started.

#### `startStreaming`
*function* · [`firmware/src/main.cpp:239`](../firmware/src/main.cpp#L239)

```cpp
void startStreaming()
```

**Purpose.** Begins a new stream when the host opens the port: new stream id, empty TX queue, fresh counters, and a header and status queued first.

**How it works.** Increments g_streamId; g_tx.clear(); sets g_streaming, g_headerPending and g_statusPending to true; zeroes g_status.frames_sent, overruns, tx_drops and read_errors[0..3]; g_maxLatencyUs = 0; g_tx.resetHighWater(), which sets the high-water mark to 0 because the ring was just cleared.

**Notes.** g_status.ticks and t_us are not reset because queueStatusIfDue() overwrites them every time. The tick counter, and therefore seq and t_us in DATA frames, keeps running across streams. The host tells streams apart by stream_id/device_id.

#### `serviceHost`
*function* · [`firmware/src/main.cpp:253`](../firmware/src/main.cpp#L253)

```cpp
void serviceHost()
```

**Purpose.** Follows the USB host connection to start and stop streaming, and processes single-byte commands from the host.

**How it works.**

1. connected = hal::hostConnected() (Serial.dtr(): the host has the CDC port open).
2. If connected and not streaming, calls startStreaming().
3. If disconnected and streaming, sets g_streaming = false and g_tx.clear(), discarding everything still queued.
4. Drains every pending RX byte with hal::hostRead() until it returns -1: 'H' (proto::kCmdSendHeader) sets g_headerPending; 'S' (proto::kCmdSendStatus) sets g_statusPending; all other bytes are ignored.

**Notes.** Commands are parsed even while not streaming. The flags they set have no effect until streaming starts, and startStreaming() sets both flags anyway.

#### `queueHeaderIfPending`
*function* · [`firmware/src/main.cpp:267`](../firmware/src/main.cpp#L267)

```cpp
void queueHeaderIfPending()
```

**Purpose.** Puts the HEADER frame into the TX ring when one is requested, but only once it fits whole.

**How it works.** Returns immediately unless g_headerPending && g_streaming. Otherwise len = buildHeaderFrame(). If len != 0 and g_tx.free() >= len, pushes g_headerFrame and clears g_headerPending. If not, the flag stays set and the attempt repeats on the next loop pass.

**Notes.** loop() calls sampleAndQueue() only when g_headerPending is false, so a header always comes before the DATA frames that follow it. If the ring is too full for the header (about 1.7 to 3.1 KB), ticks go unsampled and the host sees a seq gap; those ticks are not counted in g_status.overruns. While the header is waiting for room, the full JSON is rebuilt on every pass.

#### `queueStatusIfDue`
*function* · [`firmware/src/main.cpp:276`](../firmware/src/main.cpp#L276)

```cpp
void queueStatusIfDue()
```

**Purpose.** Queues a STATUS frame with the device health counters every KMAN_STATUS_PERIOD_MS, or immediately after a request.

**How it works.**

1. Returns unless g_streaming.
2. nowMs = millis(). Returns if !g_statusPending && nowMs - g_lastStatusMs &lt; KMAN_STATUS_PERIOD_MS (1000 ms default; unsigned subtraction is safe across wrap).
3. Fills g_status: ticks = hal::latestTick() (the tick time is discarded), t_us = hal::nowUs(), max_latency_us = g_maxLatencyUs, tx_high_water = min(g_tx.highWater(), 0xFFFF), flags = (hal::clockIsCrystal() ? kStatusFlagHfxo 0x01 : 0) | (g_sensorError ? kStatusFlagSensorError 0x02 : 0).
4. proto::encodeStatus() writes a 42-byte body (version u8, t_us, ticks, frames_sent, overruns, tx_drops, read_errors[4] as u32, max_latency_us u16, tx_high_water u16, flags u8). proto::encodeFrame(kTypeStatus 0x03) puts it in g_frame (49 bytes total).
5. If g_tx.push() succeeds: g_lastStatusMs = nowMs, g_statusPending = false, g_maxLatencyUs = 0, g_tx.resetHighWater(). max_latency_us and tx_high_water are therefore maxima for each status interval, while the other counters accumulate since stream start.

**Notes.** If the ring is full, nothing changes and the frame is retried on the next pass. There is no counter for dropped STATUS frames. tx_high_water is sampled before the STATUS frame itself is pushed.

#### `pumpTx`
*function* · [`firmware/src/main.cpp:300`](../firmware/src/main.cpp#L300)

```cpp
void pumpTx()
```

**Purpose.** Moves queued bytes from the TX ring into the USB CDC FIFO without ever blocking the loop.

**How it works.**

1. wrote = false.
2. While g_tx.used() > 0: room = hal::hostWritable() (Serial.availableForWrite(), clamped at 0); break if room == 0. n = g_tx.peekContiguous(&ptr), the run from the tail up to the wrap point. Clamp n to room. w = hal::hostWrite(ptr, n). g_tx.consume(w); wrote = true; break if w &lt; n.
3. If anything was written, calls hal::hostFlush() (TinyUSB flush, which per the HAL comment is non-blocking and only queues the partial USB packet).
The loop runs at most twice in the normal case: once for the run before the ring wraps and once for the run after it.

**Notes.** Writing only up to hostWritable() keeps a slow or stalled host from stalling sampling. Instead, data builds up in the 8 KB ring and later shows up as tx_drops.

#### `updateLeds`
*function* · [`firmware/src/main.cpp:318`](../firmware/src/main.cpp#L318)

```cpp
void updateLeds()
```

**Purpose.** Shows device state on the LEDs: a solid ERR LED for a failed sensor, and the OK LED pattern for streaming vs waiting for the host.

**How it works.** ms = millis(). hal::led(KMAN_LED_ERR_PIN, g_sensorError) sets the ERR LED solid on if any configured sensor failed init. OK pattern: while streaming, ok = (ms / 250) & 1, i.e. 250 ms on / 250 ms off (2 Hz). Otherwise ok = (ms % 2000) &lt; 50, a 50 ms blip every 2 s. The OK LED is written only if KMAN_LED_OK_PIN != KMAN_LED_ERR_PIN || !g_sensorError. hal::led() applies KMAN_LED_ACTIVE_LOW polarity.

**Notes.** On a single-LED board (both macros on the same pin), a sensor error leaves the LED solid on because the OK write is skipped. The function runs on every loop pass, so the pattern timing has loop-pass resolution.

#### `setup`
*function* · [`firmware/src/main.cpp:330`](../firmware/src/main.cpp#L330)

```cpp
void setup()
```

**Purpose.** Arduino one-time initialisation: clocks, USB, LEDs, sensors and the sample timer.

**How it works.**

1. hal::init(): starts the HF crystal oscillator if it is not already running (bounded busy-wait) and calls Serial.begin(115200) for the TinyUSB CDC (baud rate irrelevant).
2. hal::ledInit() on the OK and ERR pins: output mode, off.
3. initSensors().
4. hal::startSampleTimer(kPeriodUs): TIMER4 32-bit free-running at 1 MHz (PRESCALER 4), CC[0] = period, COMPARE0 IRQ at priority 3. It records the calling task's handle as the one to notify.
5. g_lastTickCount = hal::latestTick(&dummy), so loop() reacts only to ticks after this point.

**Notes.** The Adafruit nRF52 core calls setup() and then loop() from the same FreeRTOS 'loop' task. startSampleTimer() requires this, because it must be called from the task that later calls waitForTick().

#### `loop`
*function* · [`firmware/src/main.cpp:340`](../firmware/src/main.cpp#L340)

```cpp
void loop()
```

**Purpose.** One pass of the main state machine, woken by each sample-timer tick: host servicing, header, sampling, status, USB transmit and LEDs.

**How it works.**

1. hal::waitForTick(5): ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(5)) blocks until TIMER4_IRQHandler gives a task notification or about 5 ms pass (5 RTOS ticks at configTICK_RATE_HZ 1024). pdTRUE clears the count, so several pending notifications produce only one wake.
2. serviceHost(): connect/disconnect handling and host commands.
3. queueHeaderIfPending().
4. count = hal::latestTick(&tickUs), a tear-free read of the ISR's tick count and the scheduled time of the latest tick.
5. If count != g_lastTickCount: if streaming, g_status.overruns += count - g_lastTickCount - 1 (ticks that elapsed without being served, including ticks the ISR skipped after a long interrupt mask); then, if !g_headerPending, sampleAndQueue(count, tickUs), which samples only the newest tick. g_lastTickCount = count, whether or not streaming.
6. queueStatusIfDue(); pumpTx(); updateLeds().
After loop() returns, the core's loop_task calls yield().

**Notes.** The 5 ms timeout makes extra passes without a new tick (about two passes per 10 ms period at 100 Hz). Those passes service USB, commands and LEDs but do not sample. The tick counter comparison uses unsigned arithmetic, so it survives the 2^32 wrap. When not streaming, ticks are consumed and nothing is read from the sensors.

## 6. Firmware: hardware abstraction (nRF52840)

- **`firmware/src/hal/hal.h`**: The portable board/MCU interface. It declares everything in namespace hal that the rest of the firmware may call: clock and LED init, chip identity, the 1 MHz sample timer (start, latest tick, wait, now), ADC, I2C bus recovery, the USB host link, and LEDs. It contains declarations only. Each MCU family supplies one hal_&lt;mcu>.cpp; hal_nrf52.cpp is the only one so far. It includes ../json_writer.h so the describe\* functions can write their part of the session-header JSON.
- **`firmware/src/hal/hal_nrf52.cpp`**: The nRF52840 implementation of hal.h for the Adafruit-based Seeed XIAO core (TinyUSB CDC, FreeRTOS). Everything except the opening comment sits inside #if defined(NRF52_SERIES) || defined(NRF52840_XXAA), so any other build compiles to an empty translation unit. It writes the CLOCK registers directly to start the HFXO, and it programs TIMER4 directly as the free-running 1 MHz sample timebase with its COMPARE0 ISR (plus NVIC setup through CMSIS). It only reads the read-only FICR registers, to get the factory device ID. The ADC, GPIO, I2C pin wiggling and USB serial all go through the Arduino core API. The ISR wakes the loop task with a FreeRTOS task notification.

**How it connects.** This group sits under main.cpp and the sensor drivers. Upward: setup() in firmware/src/main.cpp calls hal::init(), then hal::ledInit(KMAN_LED_OK_PIN) and hal::ledInit(KMAN_LED_ERR_PIN). Next it calls initSensors(), which runs hal::i2cBusRecover(KMAN_I2C_SDA_PIN, KMAN_I2C_SCL_PIN) before Wire.begin() and then each configured driver's init(). FlexAdc::init() calls hal::adcInit(), hal::adcFullScale() and hal::adcRead() for an 8-read rail check. setup() then calls hal::startSampleTimer(kPeriodUs = 1000000 / KMAN_SAMPLE_RATE_HZ) and seeds g_lastTickCount with hal::latestTick(). Each loop() pass runs in this order. (1) hal::waitForTick(5) blocks until TIMER4_IRQHandler notifies the task or 5 ms pass. (2) serviceHost() compares hal::hostConnected() with g_streaming. Connected while not streaming calls startStreaming(), which picks a new stream_id and sets the header pending. Not connected while streaming stops streaming and clears the TX ring. It then drains hal::hostRead() until -1, handling kCmdSendHeader and kCmdSendStatus. (3) queueHeaderIfPending() calls buildHeaderFrame(), which uses hal::deviceId(), hal::mcuName(), hal::nowUs() ('t_us_at_header') and hal::describeClock() inside the 'clock' object. For the flex slot, FlexAdc::describeConfig() calls hal::describeAdc(). (4) hal::latestTick() supplies the tick count and the scheduled tick time. If the count changed while streaming, the gap goes into g_status.overruns (count - g_lastTickCount - 1). If no header is pending, sampleAndQueue() reads the sensors (FlexAdc::read() calls hal::adcRead()), writes t_us as u32 and the count as u16 into a DATA frame, and records hal::nowUs() - tickUs as latency. (5) queueStatusIfDue() reads hal::latestTick(), hal::nowUs() and hal::clockIsCrystal(), the last setting kStatusFlagHfxo. (6) pumpTx() drains the g_tx ring through hal::hostWritable() and hal::hostWrite(), then calls hal::hostFlush() once if anything was written. (7) updateLeds() calls hal::led(). Downward, hal_nrf52.cpp uses: the nRF registers NRF_CLOCK, NRF_TIMER4 and NRF_FICR (FICR is read only); the CMSIS calls NVIC_SetPriority, NVIC_ClearPendingIRQ and NVIC_EnableIRQ; the FreeRTOS calls xTaskGetCurrentTaskHandle, vTaskNotifyGiveFromISR, portYIELD_FROM_ISR and ulTaskNotifyTake; the Adafruit/Seeed Arduino core, meaning TinyUSB Serial (begin, dtr, availableForWrite, write, flush, available, read), analogReference, analogReadResolution, analogOversampling, analogSampleTime, analogRead, pinMode, digitalRead, digitalWrite and delayMicroseconds; JsonWriter::kv; and the KMAN_ADC_\* and KMAN_LED_ACTIVE_LOW macros from firmware/include/config.h. On the host, the only clock value from the header that is actually read is clock.hfclk: SessionHeader.describe() in host/kman_host/protocol.py includes it in the header summary that logger.py prints. No host or analysis code reads t_bits, seq_bits or the ADC full_scale; they appear only in the header that host/kman_host/simulator.py fabricates. The HFXO status flag becomes DeviceStatus.hfxo in protocol.py. logger.py shows it as ' NO-XTAL' in the status line and as 'HF crystal yes/NO' in the summary, and analysis/kman_analysis/pipeline.py records it as 'hf_crystal'.

### `firmware/src/hal/hal.h`

#### `hal (portable interface namespace)`
*namespace (declarations only)* · [`firmware/src/hal/hal.h:10`](../firmware/src/hal/hal.h#L10)

```cpp
namespace hal { void init(); const char* mcuName(); void deviceId(char*, size_t); bool startSampleTimer(uint32_t); uint32_t latestTick(uint32_t*); void waitForTick(uint32_t); uint32_t nowUs(); bool clockIsCrystal(); void describeClock(JsonWriter&); void adcInit(); uint16_t adcRead(uint8_t); uint16_t adcFullScale(); void describeAdc(JsonWriter&); void i2cBusRecover(uint8_t, uint8_t); bool hostConnected(); size_t hostWritable(); size_t hostWrite(const uint8_t*, size_t); void hostFlush(); int hostRead(); void ledInit(uint8_t); void led(uint8_t, bool); }
```

**Purpose.** The contract between portable firmware code (main.cpp, sensor drivers) and MCU-specific code, so that the protocol and sensor logic never touch registers or core-specific APIs.

**How it works.** Declares 21 free functions, in groups. (1) Init and identity: init() (line 12, 'clocks, LEDs, host link'), mcuName() (14), deviceId(out, cap) (15, 'stable hex id of this chip'). (2) Sample clock on a 1 MHz hardware timebase: startSampleTimer(period_us) (19), which must be called from the task that later calls waitForTick(); latestTick(&tick_us) (22), which returns the number of ticks since start and stores the scheduled time of the latest tick, wrapping every 2^32 us = 71.6 min; waitForTick(timeout_ms) (24), which sleeps until the next tick or the timeout; nowUs() (25), on the same timebase as the tick timestamps; clockIsCrystal() (26); describeClock(JsonWriter&) (27). (3) ADC: adcInit (30), adcRead(pin) (31), adcFullScale (32), describeAdc (33). (4) I2C: i2cBusRecover(sda, scl) (37), which clocks out a slave that is holding SDA low, for example after an MCU reset mid-transfer. (5) Host link: hostConnected (40, 'DTR asserted'), hostWritable (41), hostWrite (42, 'never blocks if n &lt;= hostWritable()'), hostFlush (43, 'start sending partially filled USB packets'), hostRead (44, '-1 if nothing pending'). (6) LEDs: ledInit(pin) (47), led(pin, on) (48).

**Notes.** Each function's entry below gives its nRF52 definition line and its hal.h declaration line. A port to another MCU must keep these semantics: the tick timestamps are scheduled grid times rather than ISR execution times, latestTick returns a consistent count/time pair, and hostWrite does not block when n &lt;= hostWritable().

### `firmware/src/hal/hal_nrf52.cpp`

#### `anonymous-namespace state (hal_nrf52.cpp)`
*file-scope constants and variables* · [`firmware/src/hal/hal_nrf52.cpp:15`](../firmware/src/hal/hal_nrf52.cpp#L15)

```cpp
NRF_TIMER_Type* const kTimer = NRF_TIMER4; const IRQn_Type kTimerIrq = TIMER4_IRQn; const uint32_t kTimerIrqPriority = 3; volatile uint32_t s_tickCount = 0; volatile uint32_t s_tickTimeUs = 0; uint32_t s_periodUs = 10000; TaskHandle_t s_loopTask = nullptr;
```

**Purpose.** The private state shared by the sample-timer functions and TIMER4_IRQHandler.

**How it works.** kTimer points to the TIMER4 register block (line 17). The comment says TIMER0 belongs to the SoftDevice and that timers 3 and 4 are unused by the core. kTimerIrq = TIMER4_IRQn. kTimerIrqPriority = 3: per the comment, this is below the SoftDevice's reserved levels (0, 1, 4) in case BLE is added later, and numerically >= configMAX_SYSCALL_INTERRUPT_PRIORITY (2), so the ISR may legally call FreeRTOS ...FromISR functions. s_tickCount (volatile) is the number of ticks since start, written only by the ISR. s_tickTimeUs (volatile) is the scheduled 1 MHz timestamp of the latest tick, also written only by the ISR. s_periodUs is the tick period in us; its initial value is 10000, and startSampleTimer overwrites it before the IRQ is enabled. s_loopTask is the FreeRTOS task to notify on each tick; it is set by startSampleTimer.

**Notes.** s_periodUs and s_loopTask are not volatile. They are written once in startSampleTimer, before NVIC_EnableIRQ, and only read afterwards. startSampleTimer does not reset s_tickCount or s_tickTimeUs, so they start at 0 only because of static initialization.

#### `startHfxo`
*function (file-local, nRF52 only)* · [`firmware/src/hal/hal_nrf52.cpp:28`](../firmware/src/hal/hal_nrf52.cpp#L28)

```cpp
void startHfxo()
```

**Purpose.** Switches the high-frequency clock to the external crystal oscillator (HFXO), so TIMER4 and the 1 MHz timebase get crystal accuracy instead of the internal RC oscillator.

**How it works.**

1. Computes xtal = CLOCK_HFCLKSTAT_SRC_Xtal &lt;&lt; CLOCK_HFCLKSTAT_SRC_Pos. 2. If (NRF_CLOCK->HFCLKSTAT & CLOCK_HFCLKSTAT_SRC_Msk) == xtal, the source is already the crystal and the function returns. 3. Otherwise it clears NRF_CLOCK->EVENTS_HFCLKSTARTED = 0 and triggers NRF_CLOCK->TASKS_HFCLKSTART = 1. 4. It busy-waits in an empty for loop until EVENTS_HFCLKSTARTED becomes non-zero or the guard counter reaches 1,000,000 iterations.

**Notes.** It writes the CLOCK peripheral directly, which is allowed only because the SoftDevice is never enabled. The comment says that with BLE this must become sd_clock_hfclk_request(). If the guard expires it returns silently and nothing reports a failure here; the result is visible later through clockIsCrystal() (the header's 'hfclk' string and the status HFXO flag). The early-return check looks at SRC only, not the STATE bit. Called only from hal::init().

#### `TIMER4_IRQHandler`
*ISR (extern "C", nRF52 only)* · [`firmware/src/hal/hal_nrf52.cpp:41`](../firmware/src/hal/hal_nrf52.cpp#L41)

```cpp
extern "C" void TIMER4_IRQHandler(void)
```

**Purpose.** Runs on every TIMER4 COMPARE[0] event. It records the tick (count and its exact scheduled time), schedules the next compare on a fixed period grid, and wakes the loop task. It does no sampling itself.

**How it works.**

The function name matches the weak vector-table symbol because of extern "C".
1. If kTimer->EVENTS_COMPARE[0] is 0, it returns. This guards against a spurious entry; COMPARE0 is the only interrupt enabled.
2. It clears EVENTS_COMPARE[0] = 0, then reads it back ((void)kTimer->EVENTS_COMPARE[0]). The comment says this makes the clear land before the IRQ exits, so the buffered write cannot leave the IRQ pending and cause an immediate re-entry.
3. tick = kTimer->CC[0], the compare value that just matched, which is the scheduled tick time and not the time the ISR started. count = s_tickCount + 1. next = tick + s_periodUs.
4. Skip-ahead: it triggers TASKS_CAPTURE[1] = 1 and reads now = kTimer->CC[1], the current counter value. CC[1] is reserved for the ISR. While (int32_t)(now - next) >= 0, meaning the next grid point is at or before the current time, it sets tick = next, next += s_periodUs, ++count. The signed 32-bit difference keeps the comparison correct across the 2^32 us counter wrap. When the loop ends, next is the first grid point strictly after now, tick is the latest grid point at or before now, and count includes every grid point that passed. This is needed because the COMPARE hardware only fires when the counter steps onto CC: a CC value already in the past would not match again until the counter wraps, about 71.6 min later. The comment says the case arises when interrupts were masked for more than a period.
5. It writes kTimer->CC[0] = next.
6. It publishes s_tickTimeUs = tick first, then s_tickCount = count. The order matters for the seqlock reader in latestTick: the count is the version number.
7. If s_loopTask is set, it calls vTaskNotifyGiveFromISR(s_loopTask, &woken) to increment the task's notification value, then portYIELD_FROM_ISR(woken), which switches context on exit if the woken task has higher priority than the interrupted one.

**Notes.** Ticks are exact multiples of s_periodUs from the timer start (period, 2\*period, ...), with no ISR-latency jitter and no accumulated drift beyond the crystal's (comment: ~20 ppm). Skipped ticks increase s_tickCount by more than 1. main.cpp loop() turns the gap into g_status.overruns += count - g_lastTickCount - 1, and the u16 sequence number in data frames jumps. The '>= 0' treats next == now as already passed, so CC is never written with the current counter value, which would not generate a COMPARE. There is one narrow window the code does not close: it reads the counter once, and the CC[0] write comes a few cycles later. If next is only about 1 us after now, which needs the ISR to be serviced almost exactly a whole number of periods late, the counter can pass next before the write lands, and the compare would then not fire until the 32-bit counter wraps.

#### `hal::init`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:72`](../firmware/src/hal/hal_nrf52.cpp#L72)

```cpp
void hal::init()
```

**Purpose.** One-time board bring-up: starts the crystal clock and the USB CDC host link.

**How it works.** Calls startHfxo(), then Serial.begin(115200). The comment notes that the baud value means nothing for USB CDC.

**Notes.** Declared at hal.h:12, where the comment says 'clocks, LEDs, host link'. The nRF52 version does not touch LEDs; main.cpp setup() calls hal::ledInit separately. Called first in setup().

#### `hal::mcuName`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:77`](../firmware/src/hal/hal_nrf52.cpp#L77)

```cpp
const char* hal::mcuName()
```

**Purpose.** Names the MCU for the session header.

**How it works.** Returns the string literal "nRF52840".

**Notes.** Declared at hal.h:14. Used by buildHeaderFrame() in main.cpp for the 'mcu' key.

#### `hal::deviceId`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:79`](../firmware/src/hal/hal_nrf52.cpp#L79)

```cpp
void hal::deviceId(char* out, size_t cap)
```

**Purpose.** Produces a stable per-chip hex ID, so recordings can be tied to a physical device.

**How it works.** snprintf(out, cap, "%08lX%08lX", NRF_FICR->DEVICEID[1], NRF_FICR->DEVICEID[0]). The result is the 64-bit factory device ID as 16 uppercase hex characters, high word first.

**Notes.** Declared at hal.h:15. It needs cap >= 17 to avoid truncation; main.cpp passes a 24-byte buffer. Written to the header's 'device_id'.

#### `hal::startSampleTimer`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:83`](../firmware/src/hal/hal_nrf52.cpp#L83)

```cpp
bool hal::startSampleTimer(uint32_t period_us)
```

**Purpose.** Configures TIMER4 as a free-running 32-bit 1 MHz counter that serves as the microsecond timebase, and arms COMPARE[0] to raise a tick interrupt every period_us.

**How it works.**

1. s_periodUs = period_us. s_loopTask = xTaskGetCurrentTaskHandle(), the caller's task, which the ISR will notify.
2. TASKS_STOP = 1, then TASKS_CLEAR = 1, so the counter is stopped and reset to 0.
3. MODE = TIMER_MODE_MODE_Timer &lt;&lt; Pos (timer mode, not counter mode).
4. BITMODE = TIMER_BITMODE_BITMODE_32Bit &lt;&lt; Pos, a 32-bit counter that wraps after 2^32 us.
5. PRESCALER = 4, so f = 16 MHz / 2^4 = 1 MHz and 1 count = 1 us.
6. SHORTS = 0. There is no COMPARE0->CLEAR shortcut, so the counter is never reset and stays a continuous microsecond timebase. Periodicity comes from the ISR advancing CC[0].
7. CC[0] = period_us, so the first tick is at t = period_us.
8. EVENTS_COMPARE[0] = 0, then INTENSET = TIMER_INTENSET_COMPARE0_Msk, which enables only the COMPARE0 interrupt.
9. NVIC_SetPriority(TIMER4_IRQn, 3), NVIC_ClearPendingIRQ, NVIC_EnableIRQ.
10. TASKS_START = 1. Returns true unconditionally.

**Notes.** Declared at hal.h:19 with the rule that it must be called from the task that later calls waitForTick(). main.cpp setup() calls it with kPeriodUs = 1000000 / KMAN_SAMPLE_RATE_HZ (10000 us at the default 100 Hz). The 16 MHz input is the timer's peripheral clock derived from HFCLK, so its accuracy depends on startHfxo() having switched HFCLK to the crystal. It does not reset s_tickCount or s_tickTimeUs; a second call would restart the counter at 0 while the tick count carried on.

#### `hal::latestTick`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:104`](../firmware/src/hal/hal_nrf52.cpp#L104)

```cpp
uint32_t hal::latestTick(uint32_t* tick_us)
```

**Purpose.** Returns a consistent pair (tick count, scheduled time of that tick) from task context, even though the ISR can update the two volatile variables between the reads.

**How it works.**

Seqlock-style read loop:
1. c1 = s_tickCount.
2. t = s_tickTimeUs.
3. c2 = s_tickCount.
4. Repeat while c1 != c2.
5. \*tick_us = t; return c1.
Why this is correct: the ISR always writes s_tickTimeUs before s_tickCount, and every ISR run increases the count by at least 1. On a single core the task cannot interrupt the ISR, so the ISR's two writes look atomic to the task. If an ISR runs anywhere between the c1 read and the c2 read, the count changes, c1 != c2, and the loop retries. If c1 == c2, no ISR ran during the window, so t belongs to count c1. The volatile qualifiers stop the compiler from caching or merging the three loads.

**Notes.** Declared at hal.h:22: the time is the scheduled tick time on the 1 MHz timebase and wraps every 2^32 us = 71.6 min. A false match would need the count to wrap by exactly 2^32 within the read window, which cannot happen. It returns 0 (and time 0) before the first tick. Callers: setup() (to seed g_lastTickCount), loop() (to detect new ticks and pass count and tickUs to sampleAndQueue) and queueStatusIfDue() (g_status.ticks).

#### `hal::waitForTick`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:115`](../firmware/src/hal/hal_nrf52.cpp#L115)

```cpp
void hal::waitForTick(uint32_t timeout_ms)
```

**Purpose.** Blocks the loop task until the ISR signals a tick or the timeout expires, so the CPU sleeps between samples instead of polling.

**How it works.** Calls ulTaskNotifyTake(pdTRUE, ticks), where ticks = pdMS_TO_TICKS(timeout_ms), or 1 if that conversion is 0 (a timeout shorter than one RTOS tick). With pdTRUE, the task's notification value is cleared to 0 on return, so several ticks given while the task was busy collapse into one wake-up. The return value is discarded.

**Notes.** Declared at hal.h:24. If a notification is already pending, it returns immediately. The caller cannot tell a tick from a timeout; main.cpp loop() calls waitForTick(5) and then compares hal::latestTick() with g_lastTickCount to find out whether ticks happened and how many. The notification only goes to the task saved by startSampleTimer.

#### `hal::nowUs`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:119`](../firmware/src/hal/hal_nrf52.cpp#L119)

```cpp
uint32_t hal::nowUs()
```

**Purpose.** Reads the current time on the same 1 MHz TIMER4 timebase as the tick timestamps.

**How it works.** Triggers kTimer->TASKS_CAPTURE[2] = 1, which latches the running counter into CC[2], and returns kTimer->CC[2].

**Notes.** Declared at hal.h:25. CC[2] is reserved for the loop task and the ISR uses CC[1], so an ISR that preempts between the capture and the read cannot overwrite the value. Calling it concurrently from two tasks would race on CC[2]; in practice only the loop task calls it. Before startSampleTimer runs, the timer is not started and the value has no meaning. Callers in main.cpp: buildHeaderFrame ('t_us_at_header'), sampleAndQueue (latency = nowUs() - tickUs, which feeds g_maxLatencyUs) and queueStatusIfDue (g_status.t_us).

#### `hal::clockIsCrystal`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:125`](../firmware/src/hal/hal_nrf52.cpp#L125)

```cpp
bool hal::clockIsCrystal()
```

**Purpose.** Reports whether HFCLK is currently running from the external crystal, which decides whether the timestamps are accurate.

**How it works.** Reads stat = NRF_CLOCK->HFCLKSTAT. Returns true when (stat & CLOCK_HFCLKSTAT_SRC_Msk) == (CLOCK_HFCLKSTAT_SRC_Xtal &lt;&lt; CLOCK_HFCLKSTAT_SRC_Pos) and the STATE bit (stat & CLOCK_HFCLKSTAT_STATE_Msk) is set (running).

**Notes.** Declared at hal.h:26. Used by describeClock and by queueStatusIfDue(), which sets proto::kStatusFlagHfxo in g_status.flags. On the host this becomes DeviceStatus.hfxo in protocol.py, and logger.py prints 'NO-XTAL' when it is false.

#### `hal::describeClock`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:131`](../firmware/src/hal/hal_nrf52.cpp#L131)

```cpp
void hal::describeClock(JsonWriter& w)
```

**Purpose.** Writes the timebase description into the session header's "clock" object, so the host knows the time units and field widths.

**How it works.** Writes five key/value pairs with w.kv: "source" = "TIMER4 32-bit free-running, 1 MHz"; "hfclk" = "HFXO 32 MHz crystal" if clockIsCrystal(), else "HFINT RC (inaccurate!)"; "t_unit" = "us"; "t_bits" = 32; "seq_bits" = 16.

**Notes.** Declared at hal.h:27. It writes key/value pairs only; buildHeaderFrame() in main.cpp opens and closes the "clock" object around the call. t_bits = 32 matches the u32 t_us field of data frames. seq_bits = 16 matches the tick count truncated to u16 in sampleAndQueue. SessionHeader.describe() in host/kman_host/protocol.py prints clock.hfclk. The file's top comment says '16 MHz HF crystal' while this string says '32 MHz crystal'. The nRF52840's HFXO is a 32 MHz crystal, and 16 MHz is the timer's peripheral clock derived from it.

#### `hal::adcInit`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:141`](../firmware/src/hal/hal_nrf52.cpp#L141)

```cpp
void hal::adcInit()
```

**Purpose.** Configures the SAADC for ratiometric, oversampled flex-sensor readings.

**How it works.** Four Arduino-core calls: analogReference(AR_VDD4), which uses a VDD/4 reference with gain 1/4 so full scale = VDD; analogReadResolution(KMAN_ADC_BITS), default 14; analogOversampling(KMAN_ADC_OVERSAMPLE), default 16, which per the comment also turns on burst mode (one SAMPLE task gives N conversions); analogSampleTime(KMAN_ADC_TACQ_US), default 10 us acquisition time.

**Notes.** Declared at hal.h:30. The comment's rationale: the divider is powered from the same 3V3 rail as the reference, so supply ripple cancels. The defaults come from config.h, which says 14 bits is meaningful with oversampling, about 12 us per sample in burst mode, and that 10 us suits source impedance &lt;= 100 kOhm. Called from FlexAdc::init().

#### `hal::adcRead`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:150`](../firmware/src/hal/hal_nrf52.cpp#L150)

```cpp
uint16_t hal::adcRead(uint8_t pin)
```

**Purpose.** Takes one (oversampled) ADC reading from an Arduino analog pin.

**How it works.** Returns (uint16_t)analogRead(pin). The range is 0..adcFullScale().

**Notes.** Declared at hal.h:31. Called from FlexAdc::read() once per sample, and eight times by FlexAdc::init() for its rail check. It blocks for the conversion time, which with the defaults is roughly oversample x ~12 us.

#### `hal::adcFullScale`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:152`](../firmware/src/hal/hal_nrf52.cpp#L152)

```cpp
uint16_t hal::adcFullScale()
```

**Purpose.** Gives the maximum ADC count, for rail checks and header metadata.

**How it works.** Returns (1u &lt;&lt; KMAN_ADC_BITS) - 1, which is 16383 at the default 14 bits.

**Notes.** Declared at hal.h:32. FlexAdc::init() uses it to compute a 2% rail margin (fs / 50); describeAdc writes it as 'full_scale'.

#### `hal::describeAdc`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:154`](../firmware/src/hal/hal_nrf52.cpp#L154)

```cpp
void hal::describeAdc(JsonWriter& w)
```

**Purpose.** Writes the ADC configuration into the flex sensor's entry in the session header, so recordings carry the conversion parameters.

**How it works.** w.kv pairs: "adc" = "nRF52840 SAADC"; "reference" = "VDD/4, gain 1/4 (ratiometric, full scale = VDD)"; "bits" = KMAN_ADC_BITS; "oversample" = KMAN_ADC_OVERSAMPLE; "tacq_us" = KMAN_ADC_TACQ_US; "full_scale" = adcFullScale(). The numeric values are cast to int32_t.

**Notes.** Declared at hal.h:33. Called from FlexAdc::describeConfig().

#### `hal::i2cBusRecover`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:165`](../firmware/src/hal/hal_nrf52.cpp#L165)

```cpp
void hal::i2cBusRecover(uint8_t sda, uint8_t scl)
```

**Purpose.** Frees an I2C bus whose slave (for example an IMU) is holding SDA low because the MCU reset in the middle of a transfer. It bit-bangs clock pulses and a STOP condition before Wire starts.

**How it works.**

1. pinMode(sda, INPUT_PULLUP), pinMode(scl, INPUT_PULLUP), then delayMicroseconds(10) to let the lines settle.
2. If digitalRead(sda) is high, the bus is idle and the function returns.
3. pinMode(scl, OUTPUT). Then up to 9 times, and only while SDA still reads low: digitalWrite(scl, LOW), wait 5 us, digitalWrite(scl, HIGH), wait 5 us. That is about 100 kHz, and 9 pulses are enough to finish any byte plus ACK the slave was in the middle of.
4. It always generates a STOP (SDA low to high while SCL is high): pinMode(sda, OUTPUT), SDA LOW, wait 5 us, SCL HIGH, wait 5 us, SDA HIGH, wait 5 us.
5. It releases both pins back to INPUT_PULLUP.

**Notes.** Declared at hal.h:37. Called by initSensors() in main.cpp with KMAN_I2C_SDA_PIN and KMAN_I2C_SCL_PIN, before Wire.begin(). The pins are driven push-pull (OUTPUT), not open-drain. If the slave still holds SDA low after 9 pulses, the STOP sequence drives SDA high against it for about 5 us. SCL clock stretching is not checked. The function returns nothing, so the caller does not learn whether recovery worked.

#### `hal::hostConnected`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:191`](../firmware/src/hal/hal_nrf52.cpp#L191)

```cpp
bool hal::hostConnected()
```

**Purpose.** Reports whether a host program has the USB CDC port open.

**How it works.** Returns Serial.dtr(), the TinyUSB CDC DTR line state.

**Notes.** Declared at hal.h:40. serviceHost() in main.cpp calls startStreaming() (new stream_id, header queued) on a false-to-true change and stops streaming and clears the TX ring on a true-to-false change.

#### `hal::hostWritable`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:192`](../firmware/src/hal/hal_nrf52.cpp#L192)

```cpp
size_t hal::hostWritable()
```

**Purpose.** Returns how many bytes can be written to USB right now without blocking.

**How it works.** n = Serial.availableForWrite(). Returns n if n > 0, else 0, which also clamps any negative value.

**Notes.** Declared at hal.h:41. pumpTx() in main.cpp uses it to cap each hostWrite chunk.

#### `hal::hostWrite`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:196`](../firmware/src/hal/hal_nrf52.cpp#L196)

```cpp
size_t hal::hostWrite(const uint8_t* data, size_t n)
```

**Purpose.** Queues bytes to the USB CDC TX FIFO.

**How it works.** Returns Serial.write(data, n), the number of bytes accepted.

**Notes.** Declared at hal.h:42 with the contract 'never blocks if n &lt;= hostWritable()'. pumpTx() takes the returned count off g_tx (g_tx.consume(w)) and stops if fewer than n bytes were accepted.

#### `hal::hostFlush`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:197`](../firmware/src/hal/hal_nrf52.cpp#L197)

```cpp
void hal::hostFlush()
```

**Purpose.** Sends a partly filled USB packet now instead of waiting for it to fill, which keeps latency low.

**How it works.** Calls Serial.flush(). The comment says that under TinyUSB this does not block and only queues the partial packet.

**Notes.** Declared at hal.h:43. pumpTx() calls it once after any loop pass that wrote bytes.

#### `hal::hostRead`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:198`](../firmware/src/hal/hal_nrf52.cpp#L198)

```cpp
int hal::hostRead()
```

**Purpose.** Non-blocking read of one command byte from the host.

**How it works.** Returns Serial.read() if Serial.available() is non-zero, otherwise -1.

**Notes.** Declared at hal.h:44. serviceHost() loops until it gets -1 and handles proto::kCmdSendHeader and proto::kCmdSendStatus.

#### `hal::ledInit`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:202`](../firmware/src/hal/hal_nrf52.cpp#L202)

```cpp
void hal::ledInit(uint8_t pin)
```

**Purpose.** Configures an LED pin and turns the LED off.

**How it works.** pinMode(pin, OUTPUT), then led(pin, false).

**Notes.** Declared at hal.h:47. setup() calls it for KMAN_LED_OK_PIN (LED_GREEN by default) and KMAN_LED_ERR_PIN (LED_RED by default).

#### `hal::led`
*function (hal.h interface, nRF52 impl)* · [`firmware/src/hal/hal_nrf52.cpp:207`](../firmware/src/hal/hal_nrf52.cpp#L207)

```cpp
void hal::led(uint8_t pin, bool on)
```

**Purpose.** Turns an LED on or off, taking the board's LED polarity into account.

**How it works.** digitalWrite(pin, (on != (bool)KMAN_LED_ACTIVE_LOW) ? HIGH : LOW). With KMAN_LED_ACTIVE_LOW = 1 (the default; the XIAO RGB LED is common-anode), on gives LOW and off gives HIGH. With 0, on gives HIGH.

**Notes.** Declared at hal.h:48. updateLeds() in main.cpp holds the ERR LED solid when g_sensorError is set, and drives the OK LED as a 2 Hz blink while streaming or a 50 ms blip every 2 s while waiting for a host.

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

- **`host/kman_host/protocol.py`**: Host-side implementation of the device wire protocol, mirroring firmware/src/protocol.h. It frames and unframes bytes (sync 0xAA 0x55, u16le length, type, body, CRC-16/CCITT-FALSE), parses an arbitrary byte stream into CRC-valid frames with resynchronisation, and decodes the three device payloads: the JSON session header, DATA sample records (as numpy structured records whose layout comes from the header), and the fixed 42-byte STATUS block. Every host tool that touches device bytes (logger, live viewer, simulator, replay, session files, tests) goes through this module.
- **`host/kman_host/labels.py`**: Single table of activity labels (keyboard digit 0-9 -> name) used while recording, plus the colour assignments for drawing label bands in light (report) and dark (live view) themes. The label table is copied into every session file's metadata, so later analysis uses the names that were in force at recording time rather than this table. It contains no logic beyond a name lookup with a fallback and a one-line help string.
- **`host/kman_host/__init__.py`**: Package marker for kman_host. It holds a one-line docstring describing the package contents (device protocol, stream decoding, session files, simulator) and the package version string. It imports nothing, so importing kman_host has no side effects.

**How it connects.**

The device side of protocol.py is firmware/src/protocol.h/.cpp: proto::encodeFrame, proto::crc16 and proto::encodeStatus produce the bytes that FrameParser.feed, crc16 and decode_status read. host/tests/test_crosscheck.py compiles firmware/test/native_frames.cpp natively together with protocol.cpp and checks the two implementations against each other. It parses the program's output with FrameParser, DATA_PREFIX.unpack_from and decode_status, and asserts that encode_frame(f.type, f.body) == f.raw.

On the host, the main consumer is kman_host/stream.py. StreamDecoder.__init__ creates the FrameParser, StreamDecoder.feed calls parser.feed, and handle_frame dispatches on TYPE_DATA, TYPE_HEADER, TYPE_STATUS, TYPE_LOG and TYPE_LABEL. _on_header calls SessionHeader.from_json and compares layout_key, stream_id and device_id. _on_data calls decode_data(body, header.data_dtype()) and uses period_us. STATUS frames go through decode_status. summary calls DeviceStatus.as_dict and asdict(parser.stats). set_label and schedule_labels use label_name.

logger.py (Recorder.feed, announce_label, reannounce_header, run_live, status_line, print_summary) does the following:
- re-broadcasts the Frame.raw bytes of parsed frames to viewers through udp_bus.UdpSender, and re-sends the last header frame's raw bytes every 2 s
- sends encode_frame(TYPE_LABEL, bytes([label])) frames
- writes CMD_SEND_HEADER to the serial port when it opens the port, and again every 2 s while no header has arrived
- prints SessionHeader.describe(), help_text() and label_name(), and accepts viewer label commands only when the code is in LABELS
- shows DeviceStatus.hfxo (' NO-XTAL') and the hfxo entry in the summary

live_view.py does the following:
- writes CMD_SEND_HEADER when it reads the serial port directly
- uses FrameParser, Frame.raw and SessionHeader.from_json(...).sample_rate_hz in replay_source
- uses channel_names and channels (scale, bit) in LiveProcessor
- builds encode_frame(TYPE_LABEL, ...) ground-truth frames for the simulator source
- uses LABEL_COLORS_DARK in build_label_lut, for the label strip, and directly for the current-label swatch; LABELS for the key help text; and label_name in the UI

kman_host/simulator.py builds a synthetic device stream. SimSession.header_frame, data_frame and status_frame use encode_frame, DATA_PREFIX.pack, DeviceStatus, STATUS_FLAG_HFXO and encode_status, and SimSession.label_events uses label_name. sim_device.py answers CMD_SEND_HEADER by sending the header frame.

kman_host/session_io.py does the following:
- SessionWriter._open uses header.channel_names for the Parquet schema and stores json.dumps(header.raw) as metadata
- SessionWriter.close stores LABELS as label_names metadata
- load_session rebuilds the header with SessionHeader.from_json and falls back to LABELS when a file has no label_names
- Session.has_slot calls slot_present

In the offline analysis, analysis/kman_analysis/plotting.py imports LABEL_COLORS_LIGHT and HATCHED_LABELS for its label bands and legend. kman_host/__init__.py defines only __version__, and no module reads it.

### `host/kman_host/__init__.py`

#### `__version__`
*config* · [`host/kman_host/__init__.py:3`](../host/kman_host/__init__.py#L3)

```python
__version__ = "0.1.0"
```

**Purpose.** Package version string for kman_host.

**How it works.** A module-level string constant. The package docstring on line 1 lists the package contents: device protocol, stream decoding, session files, simulator.

**Notes.** No host code reads it. pyproject.toml repeats the value (version = "0.1.0") separately, so the two must be kept in sync by hand.

### `host/kman_host/labels.py`

#### `LABELS`
*config* · [`host/kman_host/labels.py:4`](../host/kman_host/labels.py#L4)

```python
LABELS: dict[int, str]
```

**Purpose.** Map keyboard digit to activity label name, used for live annotation during recording.

**How it works.** 0 'none', 1 'rest breathing', 2 'talking', 3 'drinking/swallow', 4 'dry swallow', 5 'head movement', 6 'walking', 7 'cough', 8 'breath-hold', 9 'simulated obstruction'.

**Notes.** session_io.SessionWriter.close writes this table into the Parquet metadata as label_names. load_session uses the stored table and falls back to LABELS only when a file has none, so later edits here do not change the meaning of old recordings.

#### `label_name`
*function* · [`host/kman_host/labels.py:18`](../host/kman_host/labels.py#L18)

```python
def label_name(code: int) -> str
```

**Purpose.** Look up a label's display name, with a fallback for codes not in the table.

**How it works.** Returns LABELS.get(int(code), f'label {code}'). int(code) also accepts numpy integers and digit strings; a value that cannot be converted raises.

#### `help_text`
*function* · [`host/kman_host/labels.py:22`](../host/kman_host/labels.py#L22)

```python
def help_text() -> str
```

**Purpose.** One-line key legend for the logger console.

**How it works.** Returns '  '.join(f'{k}={v}' for k, v in LABELS.items()), e.g. '0=none  1=rest breathing  2=talking ...', in dict insertion order.

**Notes.** logger.py prints it at startup and when '?' is pressed.

#### `LABEL_COLORS_LIGHT / LABEL_COLORS_DARK / HATCHED_LABELS`
*config* · [`host/kman_host/labels.py:29`](../host/kman_host/labels.py#L29)

```python
LABEL_COLORS_LIGHT: dict[int, str]; LABEL_COLORS_DARK: dict[int, str]; HATCHED_LABELS = {9}
```

**Purpose.** Fixed colours for label bands: the light set for the offline report, the dark set for the live viewer. Label 9 also gets a hatch pattern.

**How it works.** Per the comment, labels 1-8 use categorical slots 1-8 of the reference palette in fixed order. LIGHT: 1 #2a78d6, 2 #eb6834, 3 #1baf7a, 4 #eda100, 5 #e87ba4, 6 #008300, 7 #4a3aa7, 8 #e34948, 9 #8a8984. DARK: 1 #3987e5, 2 #d95926, 3 #199e70, 4 #c98500, 5 #d55181, 6 #008300, 7 #9085e9, 8 #e66767, 9 #8a8984. The palette has no 9th hue, so label 9 is neutral gray in both sets and HATCHED_LABELS = {9} marks it for a hatch overlay. Label 0 ('none') has no entry, and callers treat a missing entry as 'no band' or a neutral colour.

**Notes.** analysis/kman_analysis/plotting.py uses LABEL_COLORS_LIGHT and HATCHED_LABELS in two places. _bands skips label 0 and any code with no colour, draws each run of a label with axvspan, overlays a '///' hatch for hatched labels, and writes the label number above each band, but only on the top axis (numbers=(i == 0)). The legend draws one colour patch per label present (hatched for label 9) with the text '&lt;code> &lt;name>', where the name comes from the session's stored label_names. live_view.py uses LABEL_COLORS_DARK in two places. build_label_lut turns it into a 256-entry RGBA lookup table, giving label 0 and unknown codes 0x38,0x38,0x35, and that table colours the label strip image (label_img), which has no text. The current-label swatch in the status text uses LABEL_COLORS_DARK.get(lab, TEXT_2) directly, next to the label number and label_name.

### `host/kman_host/protocol.py`

#### `protocol.py framing constants (SYNC, OVERHEAD, MAX_LEN)`
*config* · [`host/kman_host/protocol.py:18`](../host/kman_host/protocol.py#L18)

```python
SYNC = b"\xaa\x55"; OVERHEAD = 7; MAX_LEN = 4096
```

**Purpose.** Define the frame sync marker, the fixed per-frame overhead and the upper bound on the length field, matching proto::kSync0/kSync1, kOverhead and kMaxLen in firmware/src/protocol.h.

**How it works.** SYNC is the 2-byte marker 0xAA 0x55 that starts every frame and is what FrameParser.feed searches for. OVERHEAD = 7 is sync(2) + len(2) + type(1) + crc(2). MAX_LEN = 4096 is the largest accepted value of the len field (len counts type + body), so the largest body is 4095 bytes; encode_frame refuses larger bodies and FrameParser.feed treats a larger len as a false sync.

**Notes.** OVERHEAD is defined for documentation/parity with the firmware but is not referenced anywhere in the host code.

#### `protocol.py frame type and command constants (TYPE_*, CMD_*)`
*config* · [`host/kman_host/protocol.py:22`](../host/kman_host/protocol.py#L22)

```python
TYPE_DATA = 0x01; TYPE_HEADER = 0x02; TYPE_STATUS = 0x03; TYPE_LOG = 0x04; TYPE_LABEL = 0x10; CMD_SEND_HEADER = b"H"; CMD_SEND_STATUS = b"S"
```

**Purpose.** Name the frame type byte values and the single-byte host-to-device commands.

**How it works.** TYPE_DATA (0x01) carries one sample record, TYPE_HEADER (0x02) a UTF-8 JSON session header, TYPE_STATUS (0x03) device health counters, TYPE_LOG (0x04) UTF-8 text. TYPE_LABEL (0x10) is host-only: logger.py sends it over UDP to viewers with a 1-byte body holding the current label code; the device never emits it. CMD_SEND_HEADER (b"H") and CMD_SEND_STATUS (b"S") are written to the serial port to ask the device to (re)send its header or a status frame (firmware kCmdSendHeader/kCmdSendStatus).

**Notes.** CMD_SEND_STATUS is defined but not used by any host script; CMD_SEND_HEADER is written by logger.py and live_view.py and recognised by sim_device.py.

#### `DATA_PREFIX`
*config* · [`host/kman_host/protocol.py:32`](../host/kman_host/protocol.py#L32)

```python
DATA_PREFIX = struct.Struct("<IHB")
```

**Purpose.** Pack/unpack the fixed 7-byte prefix of every DATA body.

**How it works.** Little-endian u32 t_us (device timer time in microseconds), u16 seq (sample counter), u8 valid (per-frame bitmask: bit n set means the slot whose header 'bit' is n delivered a good reading). Size is 7 bytes. The int16 channel values follow it; their order and count come from the session header.

**Notes.** The same 7-byte prefix is reproduced as the first three fields of SessionHeader.data_dtype. The simulator packs the header's present_mask into 'valid'.

#### `protocol.py status constants (STATUS_SLOTS, _STATUS, STATUS_FLAG_*)`
*config* · [`host/kman_host/protocol.py:34`](../host/kman_host/protocol.py#L34)

```python
STATUS_SLOTS = 4; _STATUS = struct.Struct("<B5I4IHHB"); STATUS_FLAG_HFXO = 0x01; STATUS_FLAG_SENSOR_ERROR = 0x02
```

**Purpose.** Describe the binary layout of a STATUS body and the meaning of its flag bits, mirroring proto::Status / encodeStatus in the firmware.

**How it works.** _STATUS is little-endian: u8 version, 5 x u32 (t_us, ticks, frames_sent, overruns, tx_drops), 4 x u32 read_errors (one per sensor slot), u16 max_latency_us, u16 tx_high_water, u8 flags. Size = 1 + 20 + 16 + 2 + 2 + 1 = 42 bytes (equal to firmware kStatusBodyLen). Flag bit 0x01 means the HF crystal oscillator is running; 0x02 means a sensor error is present.

**Notes.** STATUS_SLOTS (4) documents the slot count but is not used in code; the '4I' in _STATUS hard-codes it.

#### `crc16`
*function* · [`host/kman_host/protocol.py:40`](../host/kman_host/protocol.py#L40)

```python
def crc16(data: bytes, crc: int = 0xFFFF) -> int
```

**Purpose.** Compute the frame checksum, CRC-16/CCITT-FALSE, identical to proto::crc16 in firmware/src/protocol.cpp.

**How it works.** Returns binascii.crc_hqx(data, crc). crc_hqx is the non-reflected CRC-16 with polynomial 0x1021 and no final XOR, implemented in C; with the default start value 0xFFFF it is exactly CCITT-FALSE. Passing a previous result as crc continues the CRC over another chunk.

**Notes.** host/tests/test_protocol.py checks the standard check value crc16(b"123456789") == 0x29B1. The firmware computes the same CRC bit by bit, with no lookup table.

#### `encode_frame`
*function* · [`host/kman_host/protocol.py:45`](../host/kman_host/protocol.py#L45)

```python
def encode_frame(ftype: int, body: bytes) -> bytes
```

**Purpose.** Build a complete wire frame from a type byte and a body; used by the simulator, by the logger for UDP label frames, and by the tests.

**How it works.**

1. length = len(body) + 1 (the len field counts type + body).
2. If length > MAX_LEN (4096), raise ValueError('frame body too large (N bytes)').
3. head = struct.pack('&lt;HB', length, ftype): 2-byte little-endian length plus 1 type byte.
4. Return SYNC + head + body + struct.pack('&lt;H', crc16(head + body)). The CRC covers len, type and body, not the sync bytes, and is appended little-endian.

**Notes.** ftype is not range-checked here; a value outside 0..255 makes struct.pack raise struct.error. Output is byte-identical to proto::encodeFrame; test_crosscheck.py asserts encode_frame(f.type, f.body) == f.raw for frames produced by the natively compiled firmware code.

#### `Frame`
*dataclass* · [`host/kman_host/protocol.py:54`](../host/kman_host/protocol.py#L54)

```python
@dataclass class Frame: type: int; body: bytes; raw: bytes
```

**Purpose.** One parsed, CRC-valid frame as returned by FrameParser.feed.

**How it works.** Fields: type is the frame type byte (raw[4]); body is the payload without sync, len, type and CRC (raw[5:-2]); raw is the complete frame from 0xAA through the CRC. raw exists so frames can be sent on byte for byte. logger.Recorder.feed joins the raw bytes of the frames from one parse into a single UDP datagram for viewers. Recorder.reannounce_header re-sends the raw bytes of the last header frame. live_view.replay_source plays frames back from their raw bytes. The logger's .bin capture is not built from Frame.raw: Recorder.feed writes each incoming serial chunk with writer.write_raw(data) before parsing it.

#### `ParserStats`
*dataclass* · [`host/kman_host/protocol.py:61`](../host/kman_host/protocol.py#L61)

```python
@dataclass class ParserStats: frames: int = 0; crc_errors: int = 0; bad_length: int = 0; bytes_discarded: int = 0
```

**Purpose.** Counters describing link quality as seen by one FrameParser.

**How it works.** frames counts CRC-valid frames emitted. crc_errors counts candidate frames whose CRC did not match. bad_length counts sync candidates whose len field was 0 or above MAX_LEN. bytes_discarded counts bytes skipped while searching for a sync marker. All start at 0 and only increase.

**Notes.** The 0xAA byte of a rejected sync candidate is not added to bytes_discarded (it is counted in crc_errors or bad_length instead); the bytes after it, up to the next sync, are. StreamDecoder.summary puts these counters into the session summary via asdict(parser.stats).

#### `FrameParser`
*class* · [`host/kman_host/protocol.py:68`](../host/kman_host/protocol.py#L68)

```python
class FrameParser
```

**Purpose.** Incremental parser that turns arbitrary byte chunks (serial reads, UDP datagrams, a whole .bin file) into complete CRC-valid frames, resynchronising after corruption.

**How it works.** State: self._buf (bytearray of bytes received but not yet consumed) and self.stats (ParserStats). The only method that does work is feed(). Per its docstring, on a CRC or length error it moves one byte past the false sync and searches again, so a corrupted frame costs only that frame.

**Notes.** Has no timeout: an incomplete frame stays in _buf until more bytes arrive.

#### `FrameParser.__init__`
*constructor* · [`host/kman_host/protocol.py:76`](../host/kman_host/protocol.py#L76)

```python
def __init__(self) -> None
```

**Purpose.** Create a parser with an empty buffer and zeroed counters.

**How it works.** Sets self._buf = bytearray() and self.stats = ParserStats().

#### `FrameParser.feed`
*method* · [`host/kman_host/protocol.py:80`](../host/kman_host/protocol.py#L80)

```python
def feed(self, data: bytes) -> list[Frame]
```

**Purpose.** Append a chunk of bytes to the internal buffer, extract every complete CRC-valid frame now available, and keep any partial frame (or a possible first sync byte) for the next call.

**How it works.**

Setup: out = []; buf = self._buf (an alias, so in-place edits change the stored buffer); buf += data extends it in place; pos = 0 is the scan cursor (everything before pos is consumed or discarded); n = len(buf).

Loop:
1. Sync search: i = buf.find(SYNC, pos) looks for 0xAA 0x55 at or after pos.
2. No sync found (i &lt; 0): decide how much of the tail to keep.
   - keep_from = n - 1 if (n - 1 >= pos and buf[-1] == 0xAA) else n. A trailing 0xAA is kept because it may be the first sync byte, with the 0x55 arriving in the next chunk.
   - The guard n - 1 >= pos stops the parser from re-keeping a byte it has already consumed. pos can equal n right after a valid frame whose last byte (the CRC high byte) is 0xAA. Without the guard keep_from would be n - 1 &lt; pos: the cursor would move backwards and that consumed 0xAA would stay in the buffer. On the next call it could pair with a leading 0x55 into a false sync, or be counted as discarded when the next real frame's 0xAA 0x55 arrives.
   - bytes_discarded += max(0, keep_from - pos); pos = keep_from; break.
3. Sync found: bytes_discarded += i - pos (garbage skipped before the marker); pos = i.
4. Header-completeness check: if n - pos &lt; 5 (fewer than sync + len + type bytes available), break and wait for more data. The bytes from pos onward are kept.
5. Length validation: length = buf[pos+2] | (buf[pos+3] &lt;&lt; 8) (u16 little-endian). If length &lt; 1 (no room for the type byte) or length > MAX_LEN (4096): bad_length += 1; pos += 1 (step one byte past the false 0xAA); continue searching.
6. Completeness: end = pos + 4 + length + 2 (sync+len, then type+body, then CRC). If end > n, the frame is not complete yet: break.
7. CRC check: crc_rx = buf[end-2] | (buf[end-1] &lt;&lt; 8). Compute crc16(bytes(buf[pos+2 : end-2])), i.e. over len, type and body. If they differ: crc_errors += 1; pos += 1 (resync by one byte); continue. Stepping by one byte, not by the claimed frame length, means a real frame that starts inside the bad candidate's span is still found.
8. Accept: frames += 1; raw = bytes(buf[pos:end]); append Frame(type=raw[4], body=raw[5:-2], raw=raw) to out; pos = end; continue.

After the loop: del buf[:pos] removes every consumed or discarded byte from the front of the stored buffer, leaving only an incomplete frame, a short sync candidate, or a trailing 0xAA. Returns out (it may be empty).

**Notes.** Sync bytes inside a payload do not matter when the parser is in sync, because a valid frame is consumed as a whole (test_sync_bytes_inside_payload). Output does not depend on how the stream is split into chunks; test_chunking_invariance_when_frame_ends_in_0xaa checks this with chunk sizes 1, 2, 3 and the whole stream, using a frame that ends in 0xAA, and requires bytes_discarded == 0. A false sync whose length field is plausible (up to 4096) makes the parser wait until pos + length + 6 bytes are buffered before the CRC check rejects it. The real frames behind it are delayed, not lost: after the rejection they are re-scanned from the buffer and emitted in the same call. Each candidate costs one bytes(...) copy for the CRC slice.

#### `Channel`
*dataclass* · [`host/kman_host/protocol.py:124`](../host/kman_host/protocol.py#L124)

```python
@dataclass class Channel: name: str; slot: str; bit: int; unit: str; scale: float
```

**Purpose.** One int16 channel of the DATA payload, as described in the session header's 'channels' list.

**How it works.** name: column name (e.g. 'chest_ax', 'flex_raw'). slot: the sensor slot that produces it (e.g. 'chest_imu'). bit: the bit index of that slot in present_mask and in the per-frame valid mask. unit: physical unit string. scale: multiplier that converts raw counts to the unit.

**Notes.** Channel order in SessionHeader.channels is the order of the int16 values on the wire.

#### `SessionHeader`
*dataclass* · [`host/kman_host/protocol.py:133`](../host/kman_host/protocol.py#L133)

```python
@dataclass class SessionHeader: raw: dict; channels: list[Channel] = field(default_factory=list)
```

**Purpose.** Parsed form of the device's JSON session header, which defines the sample rate, the sensors and the DATA payload layout.

**How it works.** raw keeps the entire decoded JSON dict (board, fw_version, device_id, sample_rate_hz, period_us, stream_id, present_mask, clock, sensors, channels, ...), so fields the host does not model are kept. channels is the typed list of Channel built from raw['channels']. The properties and methods below read from raw and channels.

**Notes.** The raw dict is stored in session metadata and parsed again with from_json by session_io.load_session.

#### `SessionHeader.from_json`
*classmethod* · [`host/kman_host/protocol.py:138`](../host/kman_host/protocol.py#L138)

```python
@classmethod def from_json(cls, text: str | bytes) -> SessionHeader
```

**Purpose.** Validate and parse a HEADER frame body (or a stored header string) into a SessionHeader.

**How it works.**

1. d = json.loads(text) (accepts str or UTF-8 bytes).
2. If d.get('type') != 'kman_session_header', raise ValueError('not a kman session header').
3. If the key 'error' exists, raise ValueError(f"device reported header error: {d['error']}"). The firmware sends this minimal header when it cannot build a full one.
4. For each entry c in d['channels'], build Channel(c['name'], c['slot'], int(c['bit']), c['unit'], float(c['scale'])).
5. Return cls(raw=d, channels=chans).

**Notes.** A missing 'channels' key or a missing channel field raises KeyError, and malformed JSON raises json.JSONDecodeError. Callers such as StreamDecoder._on_header do not catch these.

#### `SessionHeader.sample_rate_hz`
*property* · [`host/kman_host/protocol.py:148`](../host/kman_host/protocol.py#L148)

```python
@property def sample_rate_hz(self) -> float
```

**Purpose.** Nominal sample rate.

**How it works.** Returns float(self.raw['sample_rate_hz']). Raises KeyError if the field is missing.

#### `SessionHeader.period_us`
*property* · [`host/kman_host/protocol.py:152`](../host/kman_host/protocol.py#L152)

```python
@property def period_us(self) -> int
```

**Purpose.** Sample period in microseconds. StreamDecoder uses it to convert t_us differences into tick counts.

**How it works.** Returns int(self.raw['period_us']). Raises KeyError if the field is missing.

#### `SessionHeader.stream_id`
*property* · [`host/kman_host/protocol.py:156`](../host/kman_host/protocol.py#L156)

```python
@property def stream_id(self) -> int
```

**Purpose.** Identifier of the device's current stream. A change means the device rebooted or re-enumerated and its clock restarted.

**How it works.** Returns int(self.raw.get('stream_id', 0)), so 0 when the field is missing.

**Notes.** StreamDecoder._on_header compares it, together with device_id, to decide whether to start a new time segment.

#### `SessionHeader.present_mask`
*property* · [`host/kman_host/protocol.py:160`](../host/kman_host/protocol.py#L160)

```python
@property def present_mask(self) -> int
```

**Purpose.** Bitmask of sensor slots the device detected at startup.

**How it works.** Returns int(self.raw.get('present_mask', 0)), so 0 when the field is missing.

**Notes.** Not referenced by other host code; the simulator reads header['present_mask'] from its dict directly.

#### `SessionHeader.channel_names`
*property* · [`host/kman_host/protocol.py:164`](../host/kman_host/protocol.py#L164)

```python
@property def channel_names(self) -> list[str]
```

**Purpose.** Channel names in wire order.

**How it works.** Returns [c.name for c in self.channels], a new list on every access.

#### `SessionHeader.layout_key`
*method* · [`host/kman_host/protocol.py:167`](../host/kman_host/protocol.py#L167)

```python
def layout_key(self) -> tuple
```

**Purpose.** Identify the payload layout; per the docstring, two headers with equal keys decode identically.

**How it works.** Returns tuple((c.name, c.scale) for c in self.channels) + (self.period_us,): one (name, scale) pair per channel in order, followed by the period.

**Notes.** slot, bit and unit are not part of the key. StreamDecoder._on_header raises LayoutChanged when a later header's key differs from the first one's, because one session file holds one schema.

#### `SessionHeader.data_dtype`
*method* · [`host/kman_host/protocol.py:171`](../host/kman_host/protocol.py#L171)

```python
def data_dtype(self) -> np.dtype
```

**Purpose.** Build the numpy structured dtype that matches one DATA body byte for byte.

**How it works.** fields = [('t_us', '&lt;u4'), ('seq', '&lt;u2'), ('valid', 'u1')] (the same 7 bytes as DATA_PREFIX), then one ('&lt;name>', '&lt;i2') per channel in wire order. Returns np.dtype(fields). With the default align=False there is no padding, so itemsize = 7 + 2 \* len(channels).

**Notes.** A new dtype is built on every call; StreamDecoder._on_data calls it once per DATA frame.

#### `SessionHeader.slots`
*method* · [`host/kman_host/protocol.py:176`](../host/kman_host/protocol.py#L176)

```python
def slots(self) -> dict[str, dict]
```

**Purpose.** Index the header's sensor list by slot name.

**How it works.** Returns {s['slot']: s for s in self.raw.get('sensors', [])}. The dict is rebuilt on every call. Each value is the sensor's raw JSON dict (status, model, error, warning, ...).

#### `SessionHeader.slot_present`
*method* · [`host/kman_host/protocol.py:179`](../host/kman_host/protocol.py#L179)

```python
def slot_present(self, slot: str) -> bool
```

**Purpose.** Report whether the named sensor slot was detected and working when the header was built.

**How it works.** Returns self.slots().get(slot, {}).get('status') == 'ok'. Unknown slots and any status other than 'ok' give False.

**Notes.** Used by session_io.Session.has_slot.

#### `SessionHeader.describe`
*method* · [`host/kman_host/protocol.py:182`](../host/kman_host/protocol.py#L182)

```python
def describe(self) -> str
```

**Purpose.** Human-readable multi-line summary of the header. logger.py prints it when a header arrives.

**How it works.**

1. Line 1: 'board=&lt;board> fw=&lt;fw_version> device=&lt;device_id> rate=&lt;sample_rate_hz formatted with :g> Hz stream=&lt;stream_id>' (raw.get, so missing fields print None).
2. Line 2: 'clock: &lt;raw['clock']['hfclk']>', or '?' if missing.
3. For each sensor s in raw['sensors']: '  &lt;slot left-aligned to 12> &lt;status left-aligned to 15> &lt;model or empty>', with '  ERROR: &lt;error>' and/or '  WARNING: &lt;warning>' appended when those keys are truthy.
4. Last line: '  channels: ' + comma-separated channel_names.
The lines are joined with '\n'.

**Notes.** s['slot'] and s['status'] are indexed directly, so a sensor entry without them raises KeyError. sample_rate_hz is required.

#### `decode_data`
*function* · [`host/kman_host/protocol.py:200`](../host/kman_host/protocol.py#L200)

```python
def decode_data(body: bytes, dtype: np.dtype) -> np.ndarray | None
```

**Purpose.** Decode one DATA body into a 1-element structured array, or reject it if its size does not match the header layout.

**How it works.** If len(body) != dtype.itemsize, return None. Otherwise return np.frombuffer(body, dtype=dtype, count=1). Fields are read as rec['t_us'][0], rec['chest_ax'][0], and so on.

**Notes.** frombuffer does not copy: the array is a read-only view on body. StreamDecoder._on_data counts a None result in stats.bad_size and drops the frame.

#### `DeviceStatus`
*dataclass* · [`host/kman_host/protocol.py:208`](../host/kman_host/protocol.py#L208)

```python
@dataclass class DeviceStatus: t_us: int; ticks: int; frames_sent: int; overruns: int; tx_drops: int; read_errors: tuple[int, ...]; max_latency_us: int; tx_high_water: int; flags: int
```

**Purpose.** Decoded STATUS frame: the device's health counters.

**How it works.**

Field meanings, from the comments on proto::Status in firmware/src/protocol.h:
- t_us: timer time when the status was built.
- ticks: sample-timer ticks since boot.
- frames_sent: DATA frames queued since streaming started.
- overruns: ticks skipped because the main loop was late.
- tx_drops: DATA frames dropped because the TX ring was full.
- read_errors: 4 per-slot sensor read error counts.
- max_latency_us: maximum tick-to-frame-queued latency since the last status (u16).
- tx_high_water: maximum TX ring fill in bytes since the last status (u16).
- flags: bit0 HF crystal running, bit1 sensor error.
The status version byte is checked by decode_status and is not stored.

#### `DeviceStatus.hfxo`
*property* · [`host/kman_host/protocol.py:220`](../host/kman_host/protocol.py#L220)

```python
@property def hfxo(self) -> bool
```

**Purpose.** True if the device reports that its high-frequency crystal oscillator is running.

**How it works.** Returns bool(self.flags & STATUS_FLAG_HFXO) (bit 0x01).

**Notes.** logger.status_line appends ' NO-XTAL' when this is False.

#### `DeviceStatus.sensor_error`
*property* · [`host/kman_host/protocol.py:224`](../host/kman_host/protocol.py#L224)

```python
@property def sensor_error(self) -> bool
```

**Purpose.** True if the device reports a sensor error.

**How it works.** Returns bool(self.flags & STATUS_FLAG_SENSOR_ERROR) (bit 0x02).

#### `DeviceStatus.as_dict`
*method* · [`host/kman_host/protocol.py:227`](../host/kman_host/protocol.py#L227)

```python
def as_dict(self) -> dict
```

**Purpose.** JSON-friendly form of the status for the session summary.

**How it works.** Returns a dict with keys t_us, ticks, frames_sent, overruns, tx_drops, read_errors (the tuple turned into a list), max_latency_us, tx_high_water, plus the decoded booleans hfxo and sensor_error.

**Notes.** The raw flags integer is not included, so any flag bits other than 0x01 and 0x02 are lost. StreamDecoder.summary stores device_status_first and device_status_last with this method.

#### `decode_status`
*function* · [`host/kman_host/protocol.py:242`](../host/kman_host/protocol.py#L242)

```python
def decode_status(body: bytes) -> DeviceStatus | None
```

**Purpose.** Decode a STATUS frame body, or reject it if its size or version is wrong.

**How it works.**

1. If len(body) != _STATUS.size (42) or body[0] != 1 (status version 1, firmware kStatusVersion), return None.
2. v = _STATUS.unpack(body).
3. Return DeviceStatus(t_us=v[1], ticks=v[2], frames_sent=v[3], overruns=v[4], tx_drops=v[5], read_errors=tuple(v[6:10]), max_latency_us=v[10], tx_high_water=v[11], flags=v[12]). v[0] is the version and is dropped.

**Notes.** StreamDecoder.handle_frame ignores a None result silently.

#### `encode_status`
*function* · [`host/kman_host/protocol.py:259`](../host/kman_host/protocol.py#L259)

```python
def encode_status(s: DeviceStatus) -> bytes
```

**Purpose.** Serialise a DeviceStatus into a 42-byte STATUS body. It is the inverse of decode_status and is used by the simulator.

**How it works.** Returns _STATUS.pack(1, s.t_us, s.ticks, s.frames_sent, s.overruns, s.tx_drops, \*s.read_errors, s.max_latency_us, s.tx_high_water, s.flags), with version byte 1 first.

**Notes.** read_errors must hold exactly 4 values and each field must fit its width (u32/u16/u8), otherwise struct.error is raised. The caller wraps the result with encode_frame(TYPE_STATUS, ...).

## 10. Host: stream decoder (unwrapping, gaps, segments, labels)

- **`host/kman_host/stream.py`**: Turns the raw byte stream from the device (or from a .bin file, the simulator or the UDP re-broadcast) into decoded sample rows. Each row has an unwrapped 64-bit timestamp, an unwrapped tick sequence number, a segment index and a label. It is the single decoding path shared by the logger (live and replay), the simulator CLI, the live viewer and the tests, so every consumer gets the same frame validation, the same gap and duplicate accounting, the same segment bridging after a device restart and the same label alignment. Output is pulled in column batches via drain(), plus a statistics summary that is written into the session file.

**How it connects.** Upstream, the decoder calls kman_host.protocol: FrameParser.feed for framing and CRC, SessionHeader.from_json together with layout_key(), data_dtype(), period_us, stream_id and channel_names for the header, decode_data and decode_status for frame bodies, and DeviceStatus.as_dict for the summary. It calls labels.label_name for label names. Downstream, logger.Recorder owns a StreamDecoder. Recorder.feed writes the raw bytes with SessionWriter.write_raw, calls dec.feed, re-broadcasts the returned frames' raw bytes over UDP, remembers the last HEADER frame's raw bytes, calls SessionWriter.set_header(dec.header) when dec.new_header is set and then clears it, and calls SessionWriter.write_batch(dec.drain()) once dec.pending_rows() >= 100. Recorder.set_label calls dec.set_label, writes asdict(event) with SessionWriter.write_label_event and sends a TYPE_LABEL frame over UDP. Recorder.close passes the final dec.drain() to SessionWriter.write_batch (only if it is not None and the writer has a header), then passes dec.headers, dec.label_events_dicts() and dec.summary() to SessionWriter.close. run_live catches LayoutChanged, prints and pops dec.events, re-requests a header while dec.header is None, and reads dec.stats, dec.parser.stats, dec.status and dec.label for the status line. run_replay calls dec.schedule_labels(read_label_events(...)) and feeds the raw .bin through Recorder.feed in 64 KiB chunks with host_time=0.0. live_view.tick calls dec.feed(data) with no host_time on whatever its source returns: UDP from the logger, a direct serial port, the paced simulator or a paced .bin replay. TYPE_LABEL frames from the logger or the simulator source set dec.label, and in standalone modes a key press sets dec.label directly. tick catches LayoutChanged, builds a LiveProcessor from dec.header, passes dec.drain() to LiveProcessor.process, and shows dec.stats, dec.parser.stats, dec.status and dec.label. sim_device.write_session, test_session_roundtrip in host/tests/test_stream.py and analysis/tests/test_analysis.py follow the schedule_labels -> feed(host_time=0.0) -> set_header on new_header -> drain/write_batch -> SessionWriter.close pattern on simulator output. The other test_stream tests drive feed, set_label and drain directly.

### `host/kman_host/stream.py`

#### `LayoutChanged`
*class (Exception subclass)* · [`host/kman_host/stream.py:34`](../host/kman_host/stream.py#L34)

```python
class LayoutChanged(Exception)
```

**Purpose.** Exception raised when a session header arrives mid-session whose channel layout differs from the first header's. One session file must have one fixed column layout.

**How it works.** Empty Exception subclass with a docstring. StreamDecoder._on_header raises it when hdr.layout_key() != self.header.layout_key(), with the message "channel layout changed: &lt;old channel_names> -> &lt;new channel_names>". layout_key() (protocol.SessionHeader) is the tuple of (name, scale) for every channel plus period_us, so a renamed, added or removed channel, a scale change or a sample-period change all trigger it.

**Notes.** It is raised from inside feed() while feed() loops over the parsed frames, so any frames after the offending header in the same chunk are not handled, and feed() does not return the frame list. logger.run_live catches it and stops recording. live_view.tick catches it and shows the message. run_replay and sim_device do not catch it.

#### `StreamStats`
*dataclass* · [`host/kman_host/stream.py:39`](../host/kman_host/stream.py#L39)

```python
@dataclass class StreamStats
```

**Purpose.** Counters for everything the decoder accepted, rejected or inferred. They are shown in the logger status line and the viewer, and written into the session summary.

**How it works.**

All fields are int with default 0:
- frames_data: DATA frames accepted and stored as rows.
- frames_before_header: DATA frames discarded because no session header had arrived yet.
- dropped: missing ticks inside a segment, i.e. the sum of (nt - 1) over every accepted frame with nt > 1. The jump across a segment boundary is not counted.
- duplicates: DATA frames rejected because their timestamp did not advance by more than half a period: dt == 0, dt >= 2^31 (read as a backwards step), or round(dt/period) &lt;= 0 (dt &lt;= period/2, since Python's round(0.5) == 0).
- seq_mismatch: accepted frames whose 16-bit seq delta disagrees with the time-derived tick count (mod 2^16). Only checked on continuation frames, not on the first frame of a session or segment.
- bad_size: DATA bodies whose length does not equal header.data_dtype().itemsize.
- segments: number of continuous clock segments. It is 1 after the first header and goes up by 1 for each later header whose stream_id or device_id differs from the current header's, i.e. each new streaming session (the firmware assigns a new stream_id every time the host port is opened). A reboot is only detected if it produces a different stream_id.
- headers: every HEADER frame that SessionHeader.from_json accepted, including repeats with the same stream. It is incremented before the layout check, so the header that raises LayoutChanged is counted too.
- status_frames: STATUS frames that decoded successfully.
- log_messages: LOG frames received.

**Notes.** Frames lost to CRC errors in the FrameParser, and frames rejected for bad_size, also show up in `dropped`: the next good frame's timestamp delta spans them. The tests rely on this (dropped == parser crc_errors).

#### `StreamStats.drop_pct`
*method* · [`host/kman_host/stream.py:51`](../host/kman_host/stream.py#L51)

```python
def drop_pct(self) -> float
```

**Purpose.** Frame-loss percentage relative to the number of frames that should have arrived.

**How it works.** expected = frames_data + dropped. Returns 100.0 \* dropped / expected, or 0.0 when expected is 0.

**Notes.** frames_before_header, duplicates and segment-boundary gaps are not part of the denominator. logger.print_summary warns when the value is >= 0.1.

#### `LabelEvent`
*dataclass* · [`host/kman_host/stream.py:57`](../host/kman_host/stream.py#L57)

```python
@dataclass class LabelEvent
```

**Purpose.** One label change: which tick it applies from and which label. Live recording writes these to the labels sidecar, and replay reads them back to rebuild the label column.

**How it works.**

Fields:
- seq: int | None. First unwrapped tick (seq64) the label applies to. None means "from the first frame" (the key was pressed before any data frame).
- t_us: int | None. Nominal unwrapped timestamp of that tick (last_t64 + period_us at the time of the key press), or None.
- host_time: float. Host wall-clock time (time.time()) of the key press.
- label: int. Label code.
- name: str. Human-readable name from labels.label_name(code).

**Notes.** Only `seq` is used for alignment during replay. t_us and host_time are informational.

#### `GapEvent`
*dataclass* · [`host/kman_host/stream.py:66`](../host/kman_host/stream.py#L66)

```python
@dataclass class GapEvent
```

**Purpose.** Record of one run of missing ticks inside a segment, kept for the session summary.

**How it works.**

Fields:
- seq: int. First missing tick, last_seq64 + 1.
- t_us: int. Nominal time of the first missing tick, last_t64 + period_us.
- missing: int. Number of missing ticks, nt - 1.

**Notes.** t_us is on the nominal period grid. The next received frame's t64 uses the actual measured delta instead. The decoder stores at most keep_gap_events of these (default 10000); `stats.dropped` keeps counting after the cap.

#### `StreamDecoder`
*class* · [`host/kman_host/stream.py:72`](../host/kman_host/stream.py#L72)

```python
class StreamDecoder
```

**Purpose.** Stateful decoder from device bytes to labelled, time-unwrapped sample rows. It holds the frame parser, the current session header, the unwrapping state, the label state and the buffered rows waiting to be drained.

**How it works.**

Usage cycle:
1. Callers push bytes with feed() (or already-parsed frames with handle_frame()).
2. HEADER frames define the layout (_on_header). DATA frames are unwrapped and buffered (_on_data). STATUS, LOG and LABEL frames update side state.
3. Callers set labels live with set_label(), or pre-load recorded events for replay with schedule_labels().
4. Callers poll pending_rows() and pull column batches with drain().
5. At the end, summary(), headers and label_events_dicts() go into the session file.
All attributes are described under __init__.

#### `StreamDecoder.__init__`
*constructor* · [`host/kman_host/stream.py:73`](../host/kman_host/stream.py#L73)

```python
def __init__(self, keep_gap_events: int = 10000)
```

**Purpose.** Creates an empty decoder with no header, no rows and label 0.

**How it works.**

Sets these attributes:
- parser = protocol.FrameParser(): sync, length and CRC framing.
- stats = StreamStats().
- header = None: current protocol.SessionHeader.
- headers = []: the raw dict of every header that started a segment.
- status / status_first = None: latest and first decoded DeviceStatus.
- logs = deque(maxlen=200): device LOG text.
- gaps = []: GapEvent list. _keep_gaps = keep_gap_events caps it.
- label = 0: current label code.
- label_events = []: LabelEvents that took effect.
- _pending_labels = []: replay events not yet reached.
- segment = -1: becomes 0 on the first header.
- _last_t32 = None, _last_seq16 = 0: raw 32-bit time and 16-bit seq of the last accepted frame.
- last_t64 = None, last_seq64 = None: unwrapped values of the last accepted frame.
- _last_host_time = 0.0: host time of the last accepted data frame.
- _seg_gap_host = 0.0: host-measured gap used to bridge a new segment.
- _rows = []: 1-element structured arrays. _row_host = []: host times. _row_meta = []: (t64, seq64, segment, label) tuples. These are the undrained rows.
- new_header = False: set when the first header arrives. The consumer clears it.
- events = deque(maxlen=1000): human-readable notices for the UI.

**Notes.** logs and events are bounded deques, so a consumer that never reads them (the viewer never pops `events`) does not grow memory.

#### `StreamDecoder.feed`
*method* · [`host/kman_host/stream.py:101`](../host/kman_host/stream.py#L101)

```python
def feed(self, data: bytes, host_time: float | None = None) -> list[P.Frame]
```

**Purpose.** Main entry point: pushes an arbitrary chunk of bytes through the frame parser and the frame handlers.

**How it works.**

1. host_time = time.time() if None, otherwise the given value. One host time is used for every frame in the chunk.
2. frames = self.parser.feed(data). FrameParser buffers partial frames across calls, resyncs on 0xAA 0x55 and drops CRC or length failures.
3. Calls handle_frame(f, host_time) for each frame.
4. Returns the frame list. Recorder.feed uses it to re-broadcast f.raw over UDP and to remember the last header's raw bytes.

**Notes.** run_replay, sim_device, the analysis tests and most of host/tests/test_stream.py pass host_time=0.0. Those rows have host_time 0.0, and segment bridging sees a zero host gap (see _on_header and _on_data). The exception is test_device_restart_starts_new_segment, which passes increasing host times (100.0 + i\*0.01, then 102.0 for the new header) to exercise host-gap bridging and asserts a bridged gap of about 1.9 s. The live logger and live_view.tick call feed without host_time, so time.time() is used. Exceptions from handlers (LayoutChanged, or ValueError from SessionHeader.from_json) propagate out and abort the rest of the chunk. The parser has already consumed those bytes, so the frames after the failing one in that chunk are lost.

#### `StreamDecoder.handle_frame`
*method* · [`host/kman_host/stream.py:109`](../host/kman_host/stream.py#L109)

```python
def handle_frame(self, f: P.Frame, host_time: float) -> None
```

**Purpose.** Routes one parsed frame by its type byte.

**How it works.**

- TYPE_DATA (0x01): _on_data(f.body, host_time).
- TYPE_HEADER (0x02): _on_header(f.body, host_time).
- TYPE_STATUS (0x03): st = P.decode_status(f.body). If it decodes (truthy), increments stats.status_frames, sets self.status = st, and sets status_first if it is still None.
- TYPE_LOG (0x04): increments stats.log_messages, decodes the body as UTF-8 with errors='replace', appends it to logs and appends "device: &lt;msg>" to events.
- TYPE_LABEL (0x10) with a non-empty body: self.label = f.body[0]. This is viewer-side and host-only (the device never sends it). The frames come either from the logger over UDP (Recorder.announce_label / reannounce_header) or from live_view's sim_source, which injects ground-truth label frames. No LabelEvent is recorded.
- Any other type is ignored.

**Notes.** decode_status returns None when the body length is wrong or the version byte is not 1. Such frames are dropped silently and counted nowhere.

#### `StreamDecoder._on_header`
*method* · [`host/kman_host/stream.py:130`](../host/kman_host/stream.py#L130)

```python
def _on_header(self, body: bytes, host_time: float) -> None
```

**Purpose.** Handles a session header. It sets the layout on the first header, rejects layout changes, and starts a new time segment when the device's stream identity changes.

**How it works.**

1. hdr = P.SessionHeader.from_json(body). This raises ValueError if the JSON is not a kman_session_header or carries an "error" key (a JSONDecodeError, which is also a ValueError, or a KeyError for missing channel fields can also escape). Then stats.headers += 1.
2. First header (self.header is None): store it, append hdr.raw to headers, set segment = 0, stats.segments = 1, new_header = True, and return.
3. If hdr.layout_key() != self.header.layout_key(): raise LayoutChanged. stats.headers has already been incremented for this header.
4. If hdr.stream_id != self.header.stream_id, or hdr.raw.get('device_id') != self.header.raw.get('device_id') (dict .get, so a missing device_id compares as None and raises no KeyError), start a new segment:
   - header = hdr; append hdr.raw to headers.
   - segment += 1; stats.segments += 1.
   - _last_t32 = None. This makes the next DATA frame take the new-segment branch in _on_data.
   - _seg_gap_host = max(0.0, host_time - _last_host_time): host seconds between the last accepted data frame and this header.
   - Append "new stream (id N): segment K" to events.
5. Otherwise (same layout, same stream_id and device_id) it is a repeated header: only stats.headers changes, and self.header is not replaced.

**Notes.** Repeated headers are normal: the logger requests one with CMD_SEND_HEADER when it opens the port and again every 2 s until it has a header, and Recorder.reannounce_header re-sends the last header over UDP every 2 s for viewers. In firmware, g_streamId is a RAM global that starts at 0 on boot (firmware/src/main.cpp:75). startStreaming() (main.cpp:239-240) increments it, and serviceHost() calls startStreaming() whenever the host asserts DTR while the device is not streaming (main.cpp:253-255). A new stream_id therefore means the port was (re)opened. The code comment calls this "re-enumerated or rebooted: its clock restarted", but neither half is guaranteed. (a) A reconnect without a reboot does not reset the device's tick timer. The decoder still discards the device delta and bridges with the host gap. (b) After a reboot the first connection gets stream_id 1 again. If the interrupted stream also had id 1 (the logger was the first to open the port since boot), the new header counts as a repeat and no new segment starts. The restarted clock is then unwrapped against the old _last_t32: frames are either rejected as duplicates (dt >= 2^31) or accepted with a huge spurious gap. new_header is set only on the first header, never on a segment change. The comment says "(re)defines", but a redefinition is impossible because a layout change raises.

#### `StreamDecoder._on_data`
*method* · [`host/kman_host/stream.py:155`](../host/kman_host/stream.py#L155)

```python
def _on_data(self, body: bytes, host_time: float) -> None
```

**Purpose.** Core algorithm. It validates one DATA frame, unwraps its 32-bit microsecond timestamp and 16-bit tick counter into monotonically increasing int64 values, rejects duplicates, counts gaps, cross-checks the seq, applies scheduled labels and buffers the row.

**How it works.**

1. No header yet: stats.frames_before_header += 1; return. The frame is discarded.
2. rec = P.decode_data(body, header.data_dtype()). The dtype is t_us &lt;u4, seq &lt;u2, valid u1, then one &lt;i2 per channel. If the body length does not match itemsize: stats.bad_size += 1; return.
3. t32 = rec['t_us'][0], seq16 = rec['seq'][0], period = header.period_us.
4. Branch A, _last_t32 is None:
   a. Very first frame of the session (last_t64 is None): t64 = t32 and seq64 = seq16. The unwrapped axis starts at the device's raw values, not at 0.
   b. First frame of a new segment: nt = max(1, round(_seg_gap_host \* 1e6 / period)); t64 = last_t64 + nt\*period; seq64 = last_seq64 + nt. The time axis continues across the restart, offset by the host-measured gap rounded to whole periods, with at least 1 tick. No duplicate check, seq check, dropped count or GapEvent happens here.
5. Branch B, normal continuation:
   a. dt = (t32 - _last_t32) & 0xFFFFFFFF. This is the modular 32-bit difference, so the device counter wrapping every 2^32 µs (71.58 min) is transparent.
   b. Duplicate or backwards: if dt == 0 or dt >= 0x80000000 (2^31 µs = 35.79 min, read as a negative step), stats.duplicates += 1 and return.
   c. nt = round(dt / period): number of ticks elapsed. If nt &lt;= 0 (dt is at most about half a period; Python round() rounds halves to even), stats.duplicates += 1 and return.
   d. Seq cross-check: if ((seq16 - _last_seq16) & 0xFFFF) != (nt & 0xFFFF), stats.seq_mismatch += 1. The frame is still accepted. seq64 always comes from the time-derived nt, never from seq16.
   e. Gap: if nt > 1, missing = nt - 1 and stats.dropped += missing. If len(gaps) &lt; _keep_gaps, append GapEvent(last_seq64 + 1, last_t64 + period, missing). If missing >= 2, also append "gap: N frames lost at t=X.XX s" to events, with X = last_t64/1e6. Single-frame losses are not announced.
   f. t64 = last_t64 + dt, the actual measured delta, so device timing jitter is kept. seq64 = last_seq64 + nt, an integer tick index.
6. _apply_pending_labels(seq64). This runs before the row is stored, so a replay event with seq == seq64 already labels this row.
7. Update state: _last_t32 = t32, _last_seq16 = seq16, last_t64 = t64, last_seq64 = seq64, _last_host_time = host_time, stats.frames_data += 1.
8. Buffer: append rec to _rows, host_time to _row_host, and (t64, seq64, segment, label) to _row_meta.

**Notes.** Rejected frames (no header, bad size, duplicate) leave all state unchanged, so the next frame is compared with the last accepted one. A lost or rejected frame becomes a gap on the next good frame. The 16-bit seq wraps every 65536 ticks (655.36 s at period_us = 10000, the rate used in the tests). It is therefore only a cross-check, and gap size comes from the timestamp, which is unambiguous up to 2^31 µs. Limitation: if one segment (same stream_id) has a gap between 35.79 and 71.58 min, dt >= 2^31 and the frame is counted as a duplicate. Because _last_t32 is not updated, every following frame is also rejected until the device clock passes _last_t32 + 2^32. The next accepted frame is then unwrapped short by 2^32 µs. Segment bridging depends on host_time. In replay (host_time = 0.0) _seg_gap_host is 0, so nt = 1 and segments are joined with a one-period step instead of the real gap.

#### `StreamDecoder.set_label`
*method* · [`host/kman_host/stream.py:206`](../host/kman_host/stream.py#L206)

```python
def set_label(self, code: int, host_time: float | None = None) -> LabelEvent
```

**Purpose.** Live labelling (key press in the logger, or a label command from the viewer). The new label applies from the next frame after the press.

**How it works.**

1. host_time = time.time() if None.
2. nxt_seq = None if last_seq64 is None, otherwise last_seq64 + 1.
3. nxt_t = last_t64 + header.period_us if both last_t64 and header exist, otherwise None.
4. ev = LabelEvent(nxt_seq, nxt_t, host_time, int(code), label_name(code)).
5. self.label = int(code). Every row stored from now on gets this label in _row_meta.
6. Append ev to label_events and return it. Recorder.set_label writes asdict(ev) to the labels sidecar via SessionWriter.write_label_event and broadcasts a TYPE_LABEL frame over UDP.

**Notes.** Live/replay alignment rule: live, the label switches at the first frame accepted after the press, whatever its seq. Replay switches at the first frame with seq64 >= ev.seq, which by construction is the same frame. If frames are lost right after the press, both modes label the next accepted frame. A press before any data frame gives seq = None, and replay applies that event at the first frame. The guarantee assumes seq64 values match between live and replay. After a device restart (a second segment) they do not: live bridges with the host-measured gap, while replay, fed host_time = 0.0, bridges with nt = 1. Label seqs recorded live in later segments are therefore larger than replay's seq64 for the same frame, and replay applies them late, or never if they fall past the last frame.

#### `StreamDecoder.schedule_labels`
*method* · [`host/kman_host/stream.py:218`](../host/kman_host/stream.py#L218)

```python
def schedule_labels(self, events: list[dict]) -> None
```

**Purpose.** Replay: loads recorded label events so they are applied when the decoder reaches their seq. This rebuilds the label column from the raw .bin file.

**How it works.**

1. Builds a LabelEvent from each dict: seq = e.get('seq'), t_us = e.get('t_us'), host_time = e.get('host_time', 0.0), label = int(e['label']), name = e.get('name', label_name(e['label'])).
2. Sorts the list with key lambda e: -1 if e.seq is None else e.seq, so None events come first, then ascending seq. Python's sort is stable, so events with equal seq keep their recorded order.
3. Replaces _pending_labels with the sorted list. Any previously scheduled events are discarded.

**Notes.** Called by logger.run_replay with read_label_events(labels sidecar), and by sim_device and the tests with SimSession.label_events() (host/kman_host/simulator.py:63), which emits one event per label change with seq = seq0 + sample index. label_events (the effective list) is not filled here. Events move there only as _apply_pending_labels reaches them. `name` falls back to label_name(...), which is evaluated for every event even when 'name' is present.

#### `StreamDecoder.schedule_labels.<lambda>`
*nested function (lambda sort key)* · [`host/kman_host/stream.py:222`](../host/kman_host/stream.py#L222)

```python
lambda e: -1 if e.seq is None else e.seq
```

**Purpose.** Sort key that puts "from the first frame" events (seq None) before all seq-addressed events.

**How it works.** Returns -1 for seq None, otherwise e.seq. Real seq64 values are >= 0: they start from the first frame's seq16 and only increase.

#### `StreamDecoder._apply_pending_labels`
*method* · [`host/kman_host/stream.py:225`](../host/kman_host/stream.py#L225)

```python
def _apply_pending_labels(self, seq64: int) -> None
```

**Purpose.** Applies every scheduled replay label whose start tick has been reached, just before the current frame is stored.

**How it works.** While _pending_labels is non-empty and the head event has seq None or seq &lt;= seq64: pop it from the front (pop(0)), set self.label = ev.label and append ev to label_events. When several events are due at once (for example after a gap), each is applied in order and the last one wins for this row.

**Notes.** Called from _on_data for every accepted frame, in live mode too, where _pending_labels is empty and the loop does nothing. Events whose seq is never reached stay pending and are missing from label_events_dicts().

#### `StreamDecoder.pending_rows`
*method* · [`host/kman_host/stream.py:233`](../host/kman_host/stream.py#L233)

```python
def pending_rows(self) -> int
```

**Purpose.** Number of decoded rows buffered since the last drain.

**How it works.** Returns len(self._rows).

**Notes.** Consumers use it as a batching threshold: Recorder.feed drains at >= 100, sim_device at >= 2000, and test_session_roundtrip at >= 77.

#### `StreamDecoder.drain`
*method* · [`host/kman_host/stream.py:236`](../host/kman_host/stream.py#L236)

```python
def drain(self) -> dict[str, np.ndarray] | None
```

**Purpose.** Hands the buffered rows to the consumer as a dict of equal-length column arrays and clears the buffer.

**How it works.**

1. Returns None if _rows is empty.
2. recs = np.concatenate(_rows): an N-row structured array. meta = np.array(_row_meta, dtype=int64), shape (N, 4).
3. Builds the dict in this column order:
   - t_us = meta[:,0] (int64)
   - seq = meta[:,1] (int64)
   - segment = meta[:,2] as int16
   - valid = recs['valid'] as uint8. This is the per-frame valid bitmask byte; firmware clears a slot's bit when that sensor's values should be discarded.
   - one int16 column per name in header.channel_names
   - label = meta[:,3] as int8
   - host_time = float64 array of _row_host
4. Clears _rows, _row_host and _row_meta, and returns the dict.

**Notes.** Uses the current header's channel_names. This is safe because _on_header guarantees every segment has the same layout. The int8 label column assumes label codes fit in -128..127. Consumers: SessionWriter.write_batch (logger, sim_device, tests) and LiveProcessor.process (live_view.tick).

#### `StreamDecoder.summary`
*method* · [`host/kman_host/stream.py:259`](../host/kman_host/stream.py#L259)

```python
def summary(self) -> dict
```

**Purpose.** Collects every decoding and link-quality statistic in one JSON-serialisable dict for the session metadata and the end-of-run printout.

**How it works.**

Returns a dict with:
- 'stream': asdict(self.stats)
- 'drop_pct': stats.drop_pct()
- 'parser': asdict(parser.stats), i.e. frames, crc_errors, bad_length, bytes_discarded
- 'gaps': a list of asdict(GapEvent), capped at keep_gap_events
- 'device_status_last': status.as_dict(), only if a status was received
- 'device_status_first': status_first.as_dict(), only if a status was received

**Notes.** Passed as `stats` to SessionWriter.close by Recorder.close, sim_device and the tests. logger.print_summary reads 'stream', 'parser', 'drop_pct' and 'device_status_last' from it.

#### `StreamDecoder.summary_json`
*method* · [`host/kman_host/stream.py:273`](../host/kman_host/stream.py#L273)

```python
def summary_json(self) -> str
```

**Purpose.** The summary as a JSON string.

**How it works.** Returns json.dumps(self.summary()).

**Notes.** Nothing in the repository calls it. SessionWriter.close serialises the dict itself.

#### `StreamDecoder.label_events_dicts`
*method* · [`host/kman_host/stream.py:276`](../host/kman_host/stream.py#L276)

```python
def label_events_dicts(self) -> list[dict]
```

**Purpose.** The label events that actually took effect, as plain dicts, for storage in the session file.

**How it works.** Returns [asdict(e) for e in self.label_events]. Each dict has keys seq, t_us, host_time, label, name.

**Notes.** Live, this holds every set_label call. In replay it holds only the scheduled events that were reached. Passed to SessionWriter.close, which stores it as JSON in the parquet metadata.

## 11. Host: session files, UDP bus, keyboard input

- **`host/kman_host/session_io.py`**: Defines the on-disk session format and both the writer and the reader for it. SessionWriter produces up to three files sharing a stem during live recording, replay and simulation: &lt;stem>.parquet (one row per decoded frame, zstd-compressed, with JSON metadata in the Parquet key/value store), &lt;stem>.bin (every received byte, verbatim) and &lt;stem>.labels.jsonl (label events, flushed per key press). Session/load_session read a finished .parquet back into a pandas DataFrame plus parsed metadata for to_csv.py and the analysis package, and read_label_events reads the label sidecar for --replay.
- **`host/kman_host/udp_bus.py`**: Localhost UDP transport between logger.py, which is the only process that opens the serial port, and live_view.py. Logger to viewer (DEFAULT_PORT = 51515) carries verbatim device frames, the latest HEADER frame re-sent every 2 s, and host LABEL frames (type 0x10). Viewer to logger (port + 1 = 51516) carries ASCII commands such as b"LABEL 3". All sends are fire-and-forget, so a slow or missing viewer cannot stall recording.
- **`host/kman_host/keyboard.py`**: Non-blocking single-key terminal input for Windows, macOS and Linux, used by logger.py for live labelling (digits 0-9), quit (q) and help (?). On POSIX it switches stdin to cbreak mode for the duration of a with-block and reads whatever characters are available without blocking; on Windows it uses msvcrt. When stdin is not a terminal it disables itself and always returns no keys.

**How it connects.** Writers: host/logger.py's Recorder.__init__ creates SessionWriter(out, raw=not --no-raw, extra_meta={source, port, subject, argv}) and, unless --no-udp, UdpSender(udp_port) (default 51515). Recorder.feed calls writer.write_raw(data) for every serial chunk, udp.send(b"".join(f.raw for f in frames)), writer.set_header(dec.header) when StreamDecoder.new_header is set, and writer.write_batch(dec.drain()) once dec.pending_rows() >= 100. Recorder.set_label calls writer.write_label_event(asdict(ev)), then announce_label sends P.encode_frame(TYPE_LABEL, ...) over UDP; reannounce_header re-sends the last HEADER frame every 2 s. Recorder.close calls writer.close(dec.headers, dec.label_events_dicts(), dec.summary(), notes). run_live opens UdpReceiver(udp_port + 1) as the control socket, polls it every loop, runs parse_label_command on each datagram and applies codes found in LABELS. It wraps the loop in `with KeyReader() as keys` and maps keys.poll() output: digits to rec.set_label, q/Q to stop, ? to help; it also polls for q during serial reconnect. run_replay locates the labels file with sidecar_paths(src.with_suffix(".parquet"))[1], loads it with read_label_events, hands the events to StreamDecoder.schedule_labels, and records with raw=False. host/sim_device.py write_session uses SessionWriter(out, raw=True, extra_meta={source: "simulator", seed}) with 2000-row batches. The viewer side is host/live_view.py UdpSource: UdpReceiver(port) for frames (UdpSource.poll joins the datagrams, and tick() feeds them to StreamDecoder.feed) and UdpSender(port + 1), which send_label uses to send b"LABEL n". Readers: load_session is called by host/to_csv.py main (uses s.labels, s.physical(), s.label_names), analysis/report.py main (s.name, s.header.raw), and the analysis tests. kman_analysis.preprocess.to_uniform consumes the Session (df, fs, header.channels, has_slot for chest_imu/abdomen_imu/neck_flex/neck_imu, label_names), and kman_analysis.pipeline.quality reads s.stats["stream"], ["device_status_last"] and ["parser"]["crc_errors"]. This group calls out to protocol.SessionHeader (from_json, raw, channel_names, channels[].name/bit/scale, sample_rate_hz, slot_present) and labels.LABELS; the batch dicts it writes come from stream.StreamDecoder.drain.

### `host/kman_host/keyboard.py`

#### `KeyReader`
*class* · [`host/kman_host/keyboard.py:9`](../host/kman_host/keyboard.py#L9)

```python
class KeyReader
```

**Purpose.** Context manager that gives non-blocking single-key reads from the terminal; poll() returns the characters typed since the last call.

**How it works.** Fields: _posix_state = saved termios attributes (list from tcgetattr) while cbreak mode is active, else None; enabled = True only when sys.stdin is not None and sys.stdin.isatty().

**Notes.** Used by logger.run_live as `with KeyReader() as keys:` around the whole recording loop.

#### `KeyReader.__init__`
*constructor* · [`host/kman_host/keyboard.py:12`](../host/kman_host/keyboard.py#L12)

```python
__init__(self) -> None
```

**Purpose.** Decides whether keyboard input is possible.

**How it works.** Sets self._posix_state = None and self.enabled = sys.stdin is not None and sys.stdin.isatty().

**Notes.** With redirected/piped stdin, or no stdin at all (sys.stdin is None, e.g. under pythonw), the reader is disabled and poll() always returns []; in that case logger.py can only be stopped with Ctrl-C or --duration.

#### `KeyReader.__enter__`
*method* · [`host/kman_host/keyboard.py:16`](../host/kman_host/keyboard.py#L16)

```python
__enter__(self) -> "KeyReader"
```

**Purpose.** On POSIX terminals, switches stdin to cbreak mode so single key presses are readable immediately and not echoed.

**How it works.**

1. If enabled and os.name != "nt": imports termios and tty, fd = sys.stdin.fileno(), saves self._posix_state = termios.tcgetattr(fd), then tty.setcbreak(fd) (turns off canonical line mode and echo).
2. On Windows or when disabled, changes nothing.
3. Returns self.

**Notes.** cbreak leaves signal generation on, so Ctrl-C still raises KeyboardInterrupt, which logger.run_live catches.

#### `KeyReader.__exit__`
*method* · [`host/kman_host/keyboard.py:26`](../host/kman_host/keyboard.py#L26)

```python
__exit__(self, *exc) -> None
```

**Purpose.** Restores the terminal settings saved by __enter__.

**How it works.** If self._posix_state is not None: termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._posix_state) (applied after queued output has been sent), then sets _posix_state = None.

**Notes.** Returns None, so exceptions raised inside the with-block propagate.

#### `KeyReader.poll`
*method* · [`host/kman_host/keyboard.py:33`](../host/kman_host/keyboard.py#L33)

```python
poll(self) -> list[str]
```

**Purpose.** Returns all characters typed since the last call, one string per character, without blocking.

**How it works.**

1. If not enabled, returns [].
2. Windows (os.name == "nt"): while msvcrt.kbhit(): ch = msvcrt.getwch(); if ch is "\x00" or "\xe0" (function/arrow key prefix), reads and discards the next char (the scan code) and continues; otherwise appends ch.
3. POSIX: fd = sys.stdin.fileno(); while select.select([fd], [], [], 0)[0] reports data (timeout 0, no waiting): data = os.read(fd, 32); stops if data is empty (EOF); otherwise extends keys with data.decode("utf-8", "ignore") (one list element per character).
4. Returns keys.

**Notes.** On POSIX, arrow and function keys arrive as escape sequences ("\x1b", "[", "A", ...) and are returned as separate characters; only the Windows branch filters them. Reading uses os.read on the fd and bypasses sys.stdin's Python buffer. A multibyte UTF-8 character split across a 32-byte read is dropped by the "ignore" error handler. Without __enter__ on POSIX, input arrives only after Enter (canonical mode).

### `host/kman_host/session_io.py`

#### `sidecar_paths`
*function* · [`host/kman_host/session_io.py:37`](../host/kman_host/session_io.py#L37)

```python
sidecar_paths(parquet_path: str | Path) -> tuple[Path, Path]
```

**Purpose.** Derives the raw-capture and label-sidecar paths that go with a .parquet path, so writer, logger and replay all agree on file names.

**How it works.**

1. p = Path(parquet_path).
2. stem = p.with_suffix("") strips the last suffix ("data/s01.parquet" -> "data/s01"; a path with no suffix is unchanged).
3. Returns (stem.with_suffix(".bin"), Path(str(stem) + ".labels.jsonl")), e.g. (data/s01.bin, data/s01.labels.jsonl).

**Notes.** The two sidecars are built differently: the .bin uses with_suffix (which replaces any remaining dot-suffix) while the labels path uses string concatenation. For a stem that contains a dot the names diverge: "s01.v2.parquet" gives s01.bin but s01.v2.labels.jsonl (verified). logger.run_replay calls sidecar_paths(src.with_suffix(".parquet"))[1] to locate the labels file next to a .bin.

#### `SessionWriter`
*class* · [`host/kman_host/session_io.py:43`](../host/kman_host/session_io.py#L43)

```python
class SessionWriter
```

**Purpose.** Incremental writer for one recording: streams raw bytes and label events to crash-tolerant sidecars as they arrive, and buffers decoded sample columns into zstd Parquet row groups, attaching session metadata at close.

**How it works.**

Instance fields (set in __init__):
- path: Path of the .parquet output.
- raw_path: Path of &lt;stem>.bin, or None when raw=False.
- labels_path: Path of &lt;stem>.labels.jsonl (always used).
- _raw: open binary file handle for the .bin, or None.
- _labels: open text handle (utf-8) for the .labels.jsonl.
- _writer: pyarrow.parquet.ParquetWriter, created lazily by _open() on first flush; None before that and after close.
- _schema: pyarrow Schema built from the first header; None until _open().
- _pending: list of column-dict batches not yet written; _pending_rows: their total row count.
- _row_group_rows: flush threshold in rows (default 3000, i.e. 30 s at 100 Hz).
- rows_written: total rows committed to Parquet.
- header: the first SessionHeader passed to set_header, or None.
- extra_meta: copy of caller metadata plus created_unix.
- _last_raw_flush: time.monotonic() of the last .bin flush.

Parquet schema (built in _open): t_us int64, seq int64, segment int16, valid uint8, one int16 column per header.channel_names entry in header order, label int8, host_time float64. Metadata keys are listed under SessionWriter._open and SessionWriter.close.

**Notes.** Lifecycle used by all callers: construct -> set_header(first header) -> write_batch(...) repeatedly, interleaved with write_raw/write_label_event -> close(headers, label_events, stats, notes).

#### `SessionWriter.__init__`
*constructor* · [`host/kman_host/session_io.py:44`](../host/kman_host/session_io.py#L44)

```python
__init__(self, path: str | Path, raw: bool = True, row_group_rows: int = 3000, extra_meta: dict | None = None)
```

**Purpose.** Creates the output directory and opens the live sidecar files immediately so raw bytes and labels can be captured before any header or sample exists.

**How it works.**

1. self.path = Path(path); self.path.parent.mkdir(parents=True, exist_ok=True).
2. raw_path, labels_path = sidecar_paths(self.path).
3. raw_path is stored as self.raw_path only if raw is True (else None); self.labels_path = labels_path.
4. Opens raw_path with mode "wb" if raw, else self._raw = None. Always opens labels_path with mode "w", encoding utf-8.
5. Initialises _writer/_schema = None, _pending = [], _pending_rows = 0, _row_group_rows = row_group_rows, rows_written = 0, header = None.
6. extra_meta = dict(extra_meta or {}) then setdefault("created_unix", time.time()) (unix seconds, float).
7. _last_raw_flush = time.monotonic().

**Notes.** Both sidecars are opened in truncating mode, and the .labels.jsonl is opened even when raw=False, so constructing a writer on an existing stem empties that stem's label file. The .parquet file itself is not created here; it appears only on the first flush().

#### `SessionWriter.write_raw`
*method* · [`host/kman_host/session_io.py:71`](../host/kman_host/session_io.py#L71)

```python
write_raw(self, data: bytes) -> None
```

**Purpose.** Appends received device bytes verbatim to &lt;stem>.bin so a session can be rebuilt with logger.py --replay even if the Parquet file is never finalised.

**How it works.**

1. Does nothing if self._raw is None or data is empty.
2. self._raw.write(data).
3. now = time.monotonic(); if now - self._last_raw_flush > 1.0 s, calls self._raw.flush() and sets _last_raw_flush = now.

**Notes.** flush() hands Python's buffer to the OS; there is no os.fsync. Bytes also reach the OS when the Python buffer fills, and whenever SessionWriter.flush() actually writes a row group (flush() returns early and skips the .bin flush when no rows are pending). If the process dies hard, up to about 1 s of bytes still in the Python buffer can be lost. The time check runs only when write_raw is called with data, so once the stream stops, the tail stays buffered until the next row-group flush or close(), which closes and so flushes the file.

#### `SessionWriter.write_label_event`
*method* · [`host/kman_host/session_io.py:79`](../host/kman_host/session_io.py#L79)

```python
write_label_event(self, event: dict) -> None
```

**Purpose.** Persists one label event to &lt;stem>.labels.jsonl immediately, so labels survive a crash and can be re-applied during replay.

**How it works.** Writes json.dumps(event) + "\n" to self._labels and calls self._labels.flush() on every call.

**Notes.** The dict shape is the caller's. logger.Recorder.set_label passes dataclasses.asdict(LabelEvent) = {seq, t_us, host_time, label, name}; sim_device.write_session passes the simulator's label_events() dicts. Not fsynced.

#### `SessionWriter.set_header`
*method* · [`host/kman_host/session_io.py:85`](../host/kman_host/session_io.py#L85)

```python
set_header(self, header: SessionHeader) -> None
```

**Purpose.** Records the session header that defines the Parquet column layout.

**How it works.** Stores header in self.header only if self.header is None; later calls are ignored.

**Notes.** The first header wins. StreamDecoder replaces its own header when a new stream segment starts (device reboot/re-enumeration), but it raises LayoutChanged if the channel layout changes, so the first header's columns stay valid. All segment headers go to the kman.headers key at close().

#### `SessionWriter._open`
*method* · [`host/kman_host/session_io.py:89`](../host/kman_host/session_io.py#L89)

```python
_open(self) -> None
```

**Purpose.** Builds the Arrow schema from the first header and creates the ParquetWriter; called lazily on the first flush.

**How it works.**

1. assert self.header is not None.
2. fields = [t_us: int64, seq: int64, segment: int16, valid: uint8], then pa.field(n, int16) for each n in self.header.channel_names (header order), then label: int8, host_time: float64.
3. Schema metadata (stored by pyarrow as Parquet file key/value metadata, next to pyarrow's own "ARROW:schema" key):
   - "kman.format_version" = str(FORMAT_VERSION) = "1".
   - "kman.header" = json.dumps(self.header.raw), the first device session-header JSON object (type "kman_session_header", sample_rate_hz, period_us, channels [{name, slot, bit, unit, scale}], sensors [{slot, status, ...}], board, fw_version, device_id, ...).
4. self._schema = pa.schema(fields, metadata=meta).
5. self._writer = pq.ParquetWriter(self.path, self._schema, compression="zstd").

**Notes.** Column meanings (module docstring): t_us is unwrapped device time in microseconds, seq is the unwrapped tick index, segment is the stream segment number, valid is a per-slot bitmask, channel columns hold raw int16 counts (physical value = count \* channel scale), label is the activity code, host_time is unix seconds at receipt (0.0 in replay/simulator).

#### `SessionWriter.write_batch`
*method* · [`host/kman_host/session_io.py:106`](../host/kman_host/session_io.py#L106)

```python
write_batch(self, batch: dict[str, np.ndarray]) -> None
```

**Purpose.** Queues a batch of decoded sample columns and writes a Parquet row group once enough rows have accumulated.

**How it works.**

1. Raises RuntimeError("write_batch before set_header") if self.header is None.
2. Appends batch to self._pending; _pending_rows += len(batch["t_us"]).
3. If _pending_rows >= _row_group_rows (default 3000), calls self.flush().

**Notes.** The batch is the dict returned by StreamDecoder.drain(). It must contain every schema column name: flush() indexes by schema name and raises KeyError otherwise. Extra keys are ignored. Passing None (drain() of an empty decoder) raises TypeError, but only after None has already been appended to _pending (line 109 runs before line 110). That leaves the writer broken: every later flush(), including the one at the start of close(), raises TypeError at b[name]. close() then never writes the footer metadata and never closes the Parquet or sidecar files. Callers therefore check `batch is not None` first (logger.Recorder.close, sim_device.write_session). Row groups are at least _row_group_rows rows, except the last one, written by close(). For example, logger live batches of about 100 rows give groups of about 3000-3099 rows, and sim_device batches of 2000 give groups of 4000.

#### `SessionWriter.flush`
*method* · [`host/kman_host/session_io.py:114`](../host/kman_host/session_io.py#L114)

```python
flush(self) -> None
```

**Purpose.** Writes all pending batches to the Parquet file as one row group and pushes the raw capture to the OS.

**How it works.**

1. Returns immediately if self._pending is empty.
2. If self._writer is None, calls self._open() (creates the file and schema).
3. cols = {name: np.concatenate([b[name] for b in self._pending]) for name in self._schema.names}.
4. table = pa.Table.from_pydict(cols, schema=self._schema) (casts/validates against the schema types).
5. self._writer.write_table(table) (one write_table call produces one row group).
6. rows_written += table.num_rows; clears _pending and sets _pending_rows = 0.
7. If self._raw is open, self._raw.flush().

**Notes.** The .bin is flushed only when there were pending rows, because of the early return in step 1.

#### `SessionWriter.close`
*method* · [`host/kman_host/session_io.py:128`](../host/kman_host/session_io.py#L128)

```python
close(self, headers: list[dict], label_events: list[dict], stats: dict, notes: str = "") -> None
```

**Purpose.** Finalises the session: writes the remaining rows, attaches end-of-session metadata to the Parquet footer and closes all three files.

**How it works.**

1. self.flush().
2. If self._writer exists, adds key/value metadata via self._writer.add_key_value_metadata(meta):
   - "kman.headers" = json.dumps(headers): every header raw dict the decoder accepted (StreamDecoder.headers: the first header plus one per new stream segment).
   - "kman.labels" = json.dumps(label_events): list of {seq, t_us, host_time, label, name} (StreamDecoder.label_events_dicts()).
   - "kman.label_names" = json.dumps({str(k): v for k, v in LABELS.items()}), i.e. {"0": "none", ..., "9": "simulated obstruction"}, so analysis does not depend on labels.py staying the same.
   - "kman.stats" = json.dumps(stats): StreamDecoder.summary(), keys stream (StreamStats fields), drop_pct, parser (ParserStats fields), gaps (list of {seq, t_us, missing}), and optionally device_status_last / device_status_first.
   - "kman.extra" = json.dumps(self.extra_meta): caller metadata plus created_unix (logger live: source="serial", port, subject, argv; replay: source="replay", replay_of; simulator: source="simulator", seed).
   - "kman.notes" = notes as a plain string, not JSON-encoded.
   Then closes the ParquetWriter (writes the footer) and sets _writer = None.
3. Closes the .bin if open and sets _raw = None.
4. Closes the labels file.

**Notes.** If no sample was ever written (_writer is None, e.g. no header ever arrived), no .parquet file exists and all close-time metadata is silently dropped; the sidecars are still closed. Calling close() a second time does nothing harmful. Callers: logger.Recorder.close (notes = "\n".join(rec.notes): the --notes text and "reconnected at &lt;unix time>" lines) and sim_device.write_session (notes="synthetic session (simulator)").

#### `Session`
*dataclass* · [`host/kman_host/session_io.py:152`](../host/kman_host/session_io.py#L152)

```python
@dataclass class Session
```

**Purpose.** In-memory view of a finished session file, returned by load_session and consumed by to_csv.py and the analysis package.

**How it works.**

Fields:
- path: Path of the .parquet file.
- df: pandas DataFrame with every Parquet column (t_us, seq, segment, valid, raw int16 channels, label, host_time).
- header: SessionHeader parsed from kman.header (the first header).
- labels: list of label-event dicts from kman.labels.
- label_names: dict[int, str] from kman.label_names.
- stats: dict from kman.stats (default {}).
- headers: list of header dicts from kman.headers (default []).
- extra: dict from kman.extra (default {}).
- notes: str from kman.notes (default "").

**Notes.** The dict/list defaults use field(default_factory=...), so instances do not share mutable defaults.

#### `Session.fs`
*property* · [`host/kman_host/session_io.py:164`](../host/kman_host/session_io.py#L164)

```python
fs(self) -> float
```

**Purpose.** Nominal sample rate of the session in Hz.

**How it works.** Returns self.header.sample_rate_hz, which is float(header.raw["sample_rate_hz"]).

#### `Session.name`
*property* · [`host/kman_host/session_io.py:168`](../host/kman_host/session_io.py#L168)

```python
name(self) -> str
```

**Purpose.** Short session identifier for reports.

**How it works.** Returns self.path.stem (file name without the final suffix).

#### `Session.has_slot`
*method* · [`host/kman_host/session_io.py:171`](../host/kman_host/session_io.py#L171)

```python
has_slot(self, slot: str) -> bool
```

**Purpose.** Tells whether a sensor slot (e.g. "chest_imu", "abdomen_imu", "neck_flex", "neck_imu") was working in this session.

**How it works.** Returns self.header.slot_present(slot): true only if the header's sensors list has an entry with that slot whose status == "ok".

#### `Session.physical`
*method* · [`host/kman_host/session_io.py:174`](../host/kman_host/session_io.py#L174)

```python
physical(self) -> pd.DataFrame
```

**Purpose.** Converts the raw int16 channel columns to physical units and masks samples from slots flagged invalid, for CSV export and quick inspection.

**How it works.**

1. out = DataFrame with t_s = (df["t_us"] - df["t_us"].iloc[0]) / 1e6 (seconds relative to the first row, from unwrapped device time).
2. valid = df["valid"].to_numpy() (uint8 bitmask).
3. For each ch in header.channels: v = df[ch.name] as float64 \* ch.scale; sets v to NaN where (valid & (1 &lt;&lt; ch.bit)) == 0; out[ch.name] = v.
4. Copies label, seq and segment unchanged.
5. Returns out (columns: t_s, channels..., label, seq, segment).

**Notes.** valid and host_time are not included in the output. Rows are not resampled: missing ticks stay missing (use seq to detect them). An empty DataFrame raises IndexError at iloc[0]. Used by host/to_csv.py.

#### `load_session`
*function* · [`host/kman_host/session_io.py:189`](../host/kman_host/session_io.py#L189)

```python
load_session(path: str | Path) -> Session
```

**Purpose.** Reads a finished .parquet session file and its JSON metadata into a Session.

**How it works.**

1. pf = pq.ParquetFile(path).
2. kv = {k.decode(): v.decode()} over pf.metadata.metadata (or {} if absent).
3. header = SessionHeader.from_json(kv["kman.header"]).
4. df = pf.read().to_pandas() (all columns, all row groups).
5. names = json.loads(kv.get("kman.label_names", "{}")); if that is empty, falls back to {str(k): v for k, v in LABELS.items()}.
6. Returns Session(path, df, header, labels=json(kman.labels, default []), label_names={int(k): v}, stats=json(kman.stats, default {}), headers=json(kman.headers, default []), extra=json(kman.extra, default {}), notes=kman.notes or "").

**Notes.** Raises KeyError if kman.header is missing (not a KMAN file), and SessionHeader.from_json raises ValueError if the header type is wrong. A .parquet whose writer was never closed has no footer and cannot be opened; the module docstring's recovery path is logger.py --replay on the .bin. The fallback in step 5 uses the current labels.py table. The kman.format_version key is not checked.

#### `read_label_events`
*function* · [`host/kman_host/session_io.py:209`](../host/kman_host/session_io.py#L209)

```python
read_label_events(path: str | Path) -> list[dict]
```

**Purpose.** Loads label events from a .labels.jsonl sidecar, tolerating a partially written last line, so replay can re-apply labels.

**How it works.**

1. Returns [] if the file does not exist.
2. Reads the whole file as utf-8 and iterates splitlines().
3. Strips each line; skips empty lines; json.loads the rest and appends the result.
4. Silently skips any line that raises json.JSONDecodeError (comment: torn last line after a crash).
5. Returns the list in file order.

**Notes.** Any malformed line is dropped, not only the last one. Caller: logger.run_replay, which passes the result to StreamDecoder.schedule_labels.

### `host/kman_host/udp_bus.py`

#### `UdpSender`
*class* · [`host/kman_host/udp_bus.py:18`](../host/kman_host/udp_bus.py#L18)

```python
class UdpSender
```

**Purpose.** Fire-and-forget UDP sender to one localhost port.

**How it works.** Fields: addr = (host, port) destination tuple; sock = an AF_INET/SOCK_DGRAM socket, never bound explicitly (the OS assigns an ephemeral source port on the first sendto). Module constants: DEFAULT_PORT = 51515, MAX_DATAGRAM = 8192 bytes.

**Notes.** Used by logger.Recorder (to the viewer on DEFAULT_PORT) and live_view.UdpSource (to the logger's control port, port + 1).

#### `UdpSender.__init__`
*constructor* · [`host/kman_host/udp_bus.py:19`](../host/kman_host/udp_bus.py#L19)

```python
__init__(self, port: int, host: str = "127.0.0.1")
```

**Purpose.** Sets the destination and creates the UDP socket.

**How it works.** self.addr = (host, port); self.sock = socket.socket(AF_INET, SOCK_DGRAM). The socket is left in default (blocking) mode.

#### `UdpSender.send`
*method* · [`host/kman_host/udp_bus.py:23`](../host/kman_host/udp_bus.py#L23)

```python
send(self, data: bytes) -> None
```

**Purpose.** Sends a byte string, split into datagrams of at most 8192 bytes, without ever raising.

**How it works.**

1. For i in range(0, len(data), MAX_DATAGRAM): sock.sendto(data[i : i + 8192], self.addr).
2. Each sendto is wrapped in try/except OSError: pass (comment: nobody listening / transient; never block the logger).
3. Empty data sends nothing.

**Notes.** Chunks are cut at byte offsets, not frame boundaries, so one device frame can span two datagrams. The receiver (live_view.UdpSource.poll) concatenates datagrams and feeds them to StreamDecoder, whose FrameParser reassembles frames from the byte stream and resyncs via CRC if a datagram is lost.

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

**Purpose.** Non-blocking UDP listener on one localhost port that drains all queued datagrams on each poll.

**How it works.** Field: sock = AF_INET/SOCK_DGRAM socket bound to (host, port) and set non-blocking.

**Notes.** Used by logger.run_live for the control port (udp_port + 1, default 51516) and by live_view.UdpSource for the data port (default 51515).

#### `UdpReceiver.__init__`
*constructor* · [`host/kman_host/udp_bus.py:35`](../host/kman_host/udp_bus.py#L35)

```python
__init__(self, port: int, host: str = "127.0.0.1")
```

**Purpose.** Creates, binds and un-blocks the receive socket.

**How it works.**

1. self.sock = socket.socket(AF_INET, SOCK_DGRAM).
2. self.sock.bind((host, port)), which raises OSError if the port is already bound.
3. self.sock.setblocking(False).

**Notes.** No SO_REUSEADDR is set, so only one process can listen on a given port; a second viewer fails at bind. logger.run_live catches that OSError and disables viewer label control; live_view does not catch it.

#### `UdpReceiver.poll`
*method* · [`host/kman_host/udp_bus.py:40`](../host/kman_host/udp_bus.py#L40)

```python
poll(self) -> list[bytes]
```

**Purpose.** Returns every datagram that has arrived since the last call, without blocking.

**How it works.**

1. out = [].
2. Loop: data, _ = sock.recvfrom(65536); append data. The sender address is discarded.
3. Stops on BlockingIOError or InterruptedError (queue empty) or on any other OSError (comment: e.g. WSAECONNRESET on Windows).
4. Returns out in arrival order (may be empty).

**Notes.** A datagram larger than 65536 bytes would be truncated, but senders here never exceed MAX_DATAGRAM = 8192.

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

**Purpose.** Parses a viewer-to-logger control datagram of the form b"LABEL &lt;n>".

**How it works.**

1. parts = msg.decode("ascii", "ignore").strip().split() (non-ASCII bytes dropped, split on whitespace).
2. If len(parts) == 2 and parts[0] == "LABEL" (case-sensitive) and parts[1].isdigit(), returns int(parts[1]).
3. Otherwise returns None.

**Notes.** Accepts any non-negative integer ("LABEL 12" -> 12, "LABEL 03" -> 3); range checking is the caller's job: logger.run_live applies the code only if code in LABELS (0-9).

## 12. Host: command-line tools (logger, CSV export, simulated device)

- **`host/logger.py`**: The recording tool. It owns the device's serial port, sends the header request (b"H"), and passes every received byte through StreamDecoder into SessionWriter. That produces &lt;out>.parquet, a crash-safe &lt;out>.bin and &lt;out>.labels.jsonl. While recording it takes activity labels from the keyboard and from the live viewer over UDP, rebroadcasts decoded frames to live_view.py, prints a status line and reconnects after a USB unplug. With --replay it rebuilds a Parquet file offline from a .bin capture and its labels sidecar, using the same Recorder.
- **`host/to_csv.py`**: A small export tool. It loads a finished session .parquet with load_session and writes either the samples or the label events to CSV for quick inspection in a spreadsheet. Samples are written as raw int16 counts (all Parquet columns plus a relative time column) or, with --units, as physical units with invalid slots set to NaN. It only reads what logger.py or sim_device.py wrote.
- **`host/sim_device.py`**: Stands in for the wearable so the host tools can be tested without hardware. It builds a synthetic session with kman_host.simulator.generate (default 440 s activity scenario, 100 Hz) in the exact firmware frame format. It then either plays it in real time on a pseudo-terminal that logger.py can open as a serial port (--pty), or decodes it offline into the same file set the logger produces (--write-session). In the files, the simulator's ground-truth activity is used as the labels.

**How it connects.** These three scripts are run from the command line (README workflows) and contain no decoding logic of their own; they call into the kman_host package. logger.py relies on: kman_host.stream.StreamDecoder (feed/handle_frame, set_label, schedule_labels, pending_rows, drain, summary, label_events_dicts, plus its header, new_header, events, stats, parser.stats and status fields); kman_host.session_io (SessionWriter.write_raw/write_label_event/set_header/write_batch/close/rows_written, sidecar_paths, read_label_events); kman_host.protocol (CMD_SEND_HEADER, TYPE_HEADER, TYPE_LABEL, encode_frame); kman_host.udp_bus (UdpSender.send, UdpReceiver.poll, parse_label_command, DEFAULT_PORT 51515); kman_host.keyboard.KeyReader.poll; and kman_host.labels (LABELS, label_name, help_text). On the device side, open_port() and the 2 s header retry write b"H". The firmware's serviceHost() turns that into g_headerPending, and queueHeaderIfPending() sends the HEADER frame. On every USB (re)connect, startStreaming() increments g_streamId, which StreamDecoder._on_header treats as a new segment. STATUS frames from queueStatusIfDue() feed status_line and print_summary. On the viewer side, live_view.py's UdpSource listens on UdpReceiver(51515) for the frames Recorder.feed/reannounce_header send. Its send_label() sends "LABEL n" to 51516, which run_live's ctl poll applies via Recorder.set_label; announce_label then echoes a TYPE_LABEL frame that the viewer's StreamDecoder.handle_frame applies. The files written by logger.py and by sim_device.write_session are read by to_csv.main and analysis/report.py through session_io.load_session (and Session.physical), and logger.run_replay can rebuild them from the .bin. sim_device.py uses kman_host.simulator (SimConfig, DEFAULT_SCENARIO, generate, SimSession.stream/header_frame/label_events/labels). Its --pty mode is consumed by logger.run_live (or live_view.py's SerialSource) through /dev/pts/N.

### `host/logger.py`

#### `default_out`
*function* · [`host/logger.py:33`](../host/logger.py#L33)

```python
default_out() -> Path
```

**Purpose.** Gives the output path used when --out is not passed to a live recording.

**How it works.** Returns REPO / "data" / (dt.datetime.now().strftime("%Y%m%d_%H%M%S") + ".parquet"). REPO is the module constant Path(__file__).resolve().parents[1] (line 30), i.e. the repository root, so the default is &lt;repo>/data/YYYYmmdd_HHMMSS.parquet in local time.

**Notes.** Only run_live uses it. run_replay has its own default (&lt;stem>_replay.parquet next to the .bin).

#### `Recorder`
*class* · [`host/logger.py:37`](../host/logger.py#L37)

```python
class Recorder
```

**Purpose.** The glue between decoder and writer that both live recording and replay use. One feed() call does the whole per-chunk pipeline (raw capture, decode, UDP rebroadcast, Parquet batching), so both paths produce files the same way.

**How it works.**

Fields (all set in __init__):
- dec: kman_host.stream.StreamDecoder. Parses frames, unwraps t_us/seq, counts gaps, applies labels, buffers decoded rows.
- writer: kman_host.session_io.SessionWriter for `out`. Owns the .parquet, the .bin (if raw) and the .labels.jsonl.
- udp: UdpSender(udp_port) sending to 127.0.0.1:udp_port, or None when no UDP port is given.
- last_header_raw: complete bytes of the most recent HEADER frame seen (bytes | None), kept so it can be resent to viewers that join late.
- notes: list[str] of free-text notes. close() joins them with "\n" and stores them in the Parquet metadata key kman.notes (run_live adds --notes and one "reconnected at ..." line per reconnect).

#### `Recorder.__init__`
*constructor* · [`host/logger.py:40`](../host/logger.py#L40)

```python
__init__(self, out: Path, raw: bool, extra_meta: dict, udp_port: int | None)
```

**Purpose.** Creates the decoder, the session writer and, if wanted, the UDP sender for one recording.

**How it works.**

1. self.dec = StreamDecoder().
2. self.writer = SessionWriter(out, raw=raw, extra_meta=extra_meta). This immediately creates out's parent directory, opens &lt;stem>.bin for writing if raw is true, always opens &lt;stem>.labels.jsonl in "w" mode (truncating it), and adds created_unix to extra_meta.
3. self.udp = UdpSender(udp_port) if udp_port is truthy, else None.
4. last_header_raw = None, notes = [].

**Notes.** The sidecar files are created and truncated at construction, before any data arrives. A udp_port of 0 counts as falsy and also disables UDP.

#### `Recorder.feed`
*method* · [`host/logger.py:47`](../host/logger.py#L47)

```python
feed(self, data: bytes, host_time: float | None = None) -> None
```

**Purpose.** Handles one chunk of bytes from the device or from a .bin file: archives it, decodes it, forwards the frames to viewers and moves decoded rows to the Parquet writer.

**How it works.**

1. writer.write_raw(data) appends the bytes verbatim to the .bin (no-op when raw=False). The file is flushed at most once per second (time.monotonic) and on every Parquet flush.
2. frames = dec.feed(data, host_time). FrameParser returns only complete, CRC-valid frames. Each one goes through StreamDecoder.handle_frame (DATA becomes a row, HEADER sets or checks the layout, STATUS updates dec.status, LOG goes to dec.events). host_time=None means time.time() at this call, shared by every frame in the chunk.
3. If self.udp and frames is non-empty, sends b"".join(f.raw for f in frames). These are the verbatim bytes of the valid frames only; resync garbage is not forwarded. UdpSender.send cuts the buffer into 8192-byte datagrams.
4. For every frame with f.type == P.TYPE_HEADER (0x02), stores f.raw in last_header_raw.
5. If dec.new_header is set, calls writer.set_header(dec.header), clears dec.new_header, and prints "[header]" followed by SessionHeader.describe() (board, fw, device id, rate, stream id, clock, per-slot sensor status, channel list).
6. If dec.pending_rows() >= 100, calls writer.write_batch(dec.drain()). The drained column dict is queued in the writer, which writes a Parquet row group once 3000 rows have collected (row_group_rows default).

**Notes.** StreamDecoder sets new_header only for the first header of a session, so the [header] printout appears once. writer.header keeps that first header, because SessionWriter.set_header ignores later calls. A later header with the same stream_id and device_id (for example, the answer to a repeated b"H" request) changes nothing in the decoder except stats.headers += 1; dec.header is not replaced. Only a header with a new stream_id or device_id (re-plug or reboot) replaces dec.header, is appended to dec.headers, starts a new segment and adds a "new stream (id N): segment M" event. Recorder.last_header_raw is still overwritten by every HEADER frame, whether the decoder used it or not. A header whose layout_key differs raises LayoutChanged out of dec.feed, and from there out of this method. By then the bytes are already in the .bin, and frames earlier in the chunk have already been decoded into rows. Nothing from that chunk goes out over UDP, and last_header_raw/set_header are not updated. The 8192-byte datagram split can cut a frame in two; the viewer's FrameParser reassembles it.

#### `Recorder.set_label`
*method* · [`host/logger.py:62`](../host/logger.py#L62)

```python
set_label(self, code: int) -> None
```

**Purpose.** Applies a new activity label during live recording, makes it durable immediately, and tells viewers.

**How it works.**

1. ev = dec.set_label(code). This builds a LabelEvent with seq = last_seq64 + 1 (None if no data frame yet), t_us = last_t64 + period_us (None if unknown), host_time = time.time(), label = int(code), name = label_name(code), and sets dec.label so the next decoded frame carries the new label.
2. writer.write_label_event(asdict(ev)) appends one JSON line to &lt;stem>.labels.jsonl and flushes it.
3. announce_label().

**Notes.** The label applies from the next frame after the key press, the same seq rule that replay uses via schedule_labels.

#### `Recorder.announce_label`
*method* · [`host/logger.py:67`](../host/logger.py#L67)

```python
announce_label(self) -> None
```

**Purpose.** Sends the current label to live viewers so their label display matches the logger.

**How it works.** If self.udp is set, sends P.encode_frame(P.TYPE_LABEL, bytes([self.dec.label])). That is a normal AA 55 | len | type 0x10 | 1-byte body | CRC frame. In the viewer, StreamDecoder.handle_frame sets self.label = body[0] when it receives it.

**Notes.** TYPE_LABEL (0x10) is host-only; the device never sends it. live_view's UdpSource.send_label counts on this echo instead of setting the label locally.

#### `Recorder.reannounce_header`
*method* · [`host/logger.py:71`](../host/logger.py#L71)

```python
reannounce_header(self) -> None
```

**Purpose.** Lets a viewer that starts after the logger learn the channel layout and current label.

**How it works.** If self.udp is set and last_header_raw is not None, sends the stored HEADER frame bytes over UDP, then calls announce_label(). run_live calls it every 2.0 s.

#### `Recorder.close`
*method* · [`host/logger.py:76`](../host/logger.py#L76)

```python
close(self) -> dict
```

**Purpose.** Finishes the session: writes the remaining rows, writes the Parquet footer metadata, closes all files and the UDP socket, and returns the statistics.

**How it works.**

1. batch = dec.drain() (the remaining decoded rows as columns, or None).
2. If batch is not None and writer.header is not None, writer.write_batch(batch).
3. summary = dec.summary(): {"stream": StreamStats as dict, "drop_pct", "parser": ParserStats as dict, "gaps": list of GapEvent dicts, plus "device_status_last"/"device_status_first" when STATUS frames were received}.
4. writer.close(dec.headers, dec.label_events_dicts(), summary, notes="\n".join(self.notes)). This flushes the pending rows, writes the key/value metadata (kman.headers, kman.labels, kman.label_names, kman.stats, kman.extra, kman.notes) if a Parquet writer was ever opened, and closes the .parquet, .bin and .labels.jsonl.
5. Closes the UdpSender if present. Returns summary.

**Notes.** The writer.header check is defensive. It matters only when LayoutChanged interrupted feed() before set_header ran. If no data row was ever written (for example, the header never arrived), SessionWriter never opens the ParquetWriter and no .parquet is created, but the .bin and .labels.jsonl remain.

#### `status_line`
*function* · [`host/logger.py:87`](../host/logger.py#L87)

```python
status_line(rec: Recorder, t_start: float, rate: float) -> str
```

**Purpose.** Builds the one-line live status shown in place during recording.

**How it works.**

st = rec.dec.stats (StreamStats), ps = rec.dec.parser.stats (ParserStats), el = int(time.time() - t_start).
Base string: "[HH:MM:SS] frames {st.frames_data:>7}  {rate:5.1f} Hz  lost {st.dropped} ({st.drop_pct():.3f}%)  crc {ps.crc_errors}". HH = el//3600, MM = el//60 % 60, SS = el % 60. drop_pct = 100\*dropped/(frames_data+dropped).
If rec.dec.status (the last decoded DeviceStatus) exists, appends "  dev[ovr {overruns} txdrop {tx_drops} i2c {sum(read_errors)} lat {max_latency_us/1000:.1f}ms", plus " NO-XTAL" when the HFXO flag bit (0x01) is clear, then "]".
Always appends "  | {label} {label_name(label)}". Returns the string.

**Notes.** read_errors is the 4-slot tuple from the STATUS frame, shown summed. run_live cuts the result to 160 characters.

#### `print_summary`
*function* · [`host/logger.py:100`](../host/logger.py#L100)

```python
print_summary(summary: dict, out: Path, rows: int) -> None
```

**Purpose.** Prints the end-of-session report and flags sessions that miss the frame-loss acceptance limit.

**How it works.**

Takes st = summary["stream"] and ps = summary["parser"]. Prints:
1. "Saved {rows} samples to {out}".
2. Frames received (frames_data), lost (dropped) with drop_pct to 4 decimals, CRC errors, resync bytes (ps bytes_discarded) and segments.
3. If summary has "device_status_last": device overruns, tx drops, the read_errors list, and "HF crystal yes/NO".
4. If summary["drop_pct"] >= 0.1: "WARNING: frame loss >= 0.1% (acceptance limit). See README 'Troubleshooting drops'."

**Notes.** rows comes from rec.writer.rows_written. When no Parquet file was created, this still prints "Saved 0 samples to &lt;out>".

#### `run_live`
*function* · [`host/logger.py:113`](../host/logger.py#L113)

```python
run_live(args) -> int
```

**Purpose.** Records from a real (or simulated) serial device until q, Ctrl-C, --duration, a layout change, or quitting while reconnecting. It labels, rebroadcasts and monitors along the way, and always finalises the files.

**How it works.**

Setup:
1. Imports pyserial (serial) here, so replay and --list-ports do not depend on it at module import.
2. out = Path(args.out) or default_out(). If out exists and --force is not set, prints "... exists; use --force to overwrite" to stderr and returns 2.
3. meta = {source: "serial", port, subject, argv: sys.argv}. rec = Recorder(out, raw=not args.no_raw, extra_meta=meta, udp_port=None if args.no_udp else args.udp_port). Appends args.notes to rec.notes if non-empty.
4. Unless --no-udp, ctl = UdpReceiver(args.udp_port + 1): a non-blocking socket bound to 127.0.0.1:51516 by default. An OSError (for example, the port is taken by another logger) prints "(viewer label control disabled: ...)" and leaves ctl = None.
5. Prints the output path, the label table (help_text()) and the key help, then ser = open_port(). It initialises t_start = last_print = last_announce = last_rate_t = header_asked = time.time(), last_rate_n = 0, rate = 0.0, running = True.

Main loop (inside try, under `with KeyReader() as keys`, which puts a POSIX TTY into cbreak mode and restores it on exit), while running:
1. Serial read: data = ser.read(max(1, ser.in_waiting)). This takes everything already buffered, or blocks up to the 20 ms port timeout waiting for at least 1 byte. That timeout paces the loop when the line is idle. A serial.SerialException runs the reconnect logic (below).
2. Feed: if data is non-empty, rec.feed(data). On LayoutChanged it prints "[...] -- stopping; start a new recording for the new firmware." and breaks out of the loop.
3. now = time.time(). Header retry: if rec.dec.header is None and now - header_asked > 2.0, it sets header_asked = now, writes P.CMD_SEND_HEADER (b"H") again (a SerialException here is ignored; the read path handles it on the next pass), and prints "[no session header yet; {parser.stats.frames} frames, {stats.frames_before_header} data frames waiting] requested again".
4. Keys: for each character from keys.poll(), a digit calls rec.set_label(int(k)) and prints "[label -> k name]"; "q"/"Q" sets running = False (the rest of this pass still runs); "?" prints help_text().
5. Viewer UDP control: if ctl, for each datagram from ctl.poll(), code = parse_label_command(msg), which accepts exactly "LABEL &lt;digits>". If code is not None and code in LABELS (0-9), it calls rec.set_label(code) (which also echoes a LABEL frame to the viewers) and prints "[label (viewer) -> ...]".
6. Events: while rec.dec.events is non-empty, prints each popleft() as "[...]". These are device LOG messages, gap notices for 2 or more lost frames, and "new stream ... segment N".
7. Rate: if now - last_rate_t >= 2.0, rate = (stats.frames_data - last_rate_n) / (now - last_rate_t), then last_rate_n and last_rate_t are updated. This is the received DATA-frame rate averaged over about 2 s.
8. Status line: if now - last_print >= 0.5, writes "\r" + status_line(rec, t_start, rate)[:160] + "\x1b[K" (ANSI erase to end of line) and flushes. Notices start with "\n" so they do not overwrite the status line.
9. Header re-announce: if now - last_announce >= 2.0, rec.reannounce_header() (resends the last HEADER frame and the current label over UDP).
10. Duration: if args.duration is non-zero and now - t_start >= args.duration, running = False.

Reconnect logic (the except serial.SerialException branch of step 1):
1. Prints "[serial error: ...] reconnecting... (q to stop)" and calls ser.close().
2. Inner loop while running: if "q" is among keys.poll(), sets running = False and breaks. Otherwise sleeps 1.0 s, then tries ser = open_port(). On success it prints "[reconnected]", appends f"reconnected at {time.time():.3f}" to rec.notes and breaks. On another SerialException it retries.
3. continue: back to the top of the main loop, which exits if running is now False.

Shutdown: KeyboardInterrupt is caught and ignored. The finally block closes ser (ignoring any exception), closes ctl, calls summary = rec.close(), and print_summary(summary, out, rec.writer.rows_written). Returns 0, including after a LayoutChanged stop.

**Notes.**

- ser.in_waiting is evaluated inside the try, but whether an error raised there reaches the reconnect branch depends on the platform. On Windows, pyserial's in_waiting raises SerialException (ClearCommError failed), so it reconnects. On Linux/macOS, pyserial 3.5's in_waiting is a bare fcntl.ioctl(TIOCINQ) that raises a plain OSError (EIO on a hung-up USB tty). `except serial.SerialException` does not catch that (SerialException is a subclass of OSError, not the reverse), so the OSError escapes the loop. The finally block still closes the port, finalises the files and prints the summary, and then run_live ends with a traceback instead of reconnecting. On POSIX the reconnect therefore happens only when ser.read() sees the disconnect first: pyserial then raises SerialException "device reports readiness to read but returned no data" or "read failed". The header-retry write's SerialException is swallowed and never triggers a reconnect.
- In the reconnect loop only lowercase "q" stops. Any other keys typed meanwhile are read and discarded. Ctrl-C still works because KeyboardInterrupt is caught outside the loop.
- After a re-plug, the firmware's startStreaming() increments the stream id and queues a header. StreamDecoder._on_header then starts a new segment and bridges the time axis with the host-measured gap (nt = max(1, round(gap_s\*1e6/period_us)) ticks). Those ticks are not counted in `dropped`.
- Data frames that arrive before the first header are counted in frames_before_header and discarded. They are still in the .bin.
- If stdin is not a TTY, KeyReader.poll() always returns [], so only Ctrl-C, --duration or a LayoutChanged stop ends the recording.
- The first open_port() call (line 139) and the Recorder construction happen before the try/finally. A bad --port raises an uncaught SerialException after the empty .bin/.labels.jsonl sidecars have been created (and the control UDP socket bound), and rec.close() is never called.

#### `run_live.open_port`
*nested function* · [`host/logger.py:131`](../host/logger.py#L131)

```python
open_port() -> serial.Serial
```

**Purpose.** Opens (or reopens, after an unplug) the serial port and asks the device for its session header.

**How it works.**

1. ser = serial.Serial(args.port, 115200, timeout=0.02). The 20 ms read timeout sets the main loop's idle period.
2. If the Serial object has set_buffer_size (pyserial provides it on Windows only, per the comment), calls ser.set_buffer_size(rx_size=1 &lt;&lt; 16) for a 64 KiB driver receive buffer.
3. ser.write(P.CMD_SEND_HEADER) sends b"H". In the firmware, serviceHost() sets g_headerPending and queueHeaderIfPending() then pushes the header frame.
4. Returns ser.

**Notes.** It closes over args. A SerialException from opening propagates: the reconnect loop catches it, the first call at startup does not.

#### `run_replay`
*function* · [`host/logger.py:229`](../host/logger.py#L229)

```python
run_replay(args) -> int
```

**Purpose.** Rebuilds a Parquet session offline from a raw .bin capture (for example, after a crash, or with a newer decoder), reapplying the recorded label events by sequence number.

**How it works.**

1. src = Path(args.replay). out = Path(args.out), or by default src.with_name(src.stem + "_replay.parquet"). If out exists and --force is not set, prints an error to stderr and returns 2.
2. labels_path = Path(args.labels), or by default sidecar_paths(src.with_suffix(".parquet"))[1], i.e. &lt;stem>.labels.jsonl next to the .bin.
3. events = read_label_events(labels_path). Returns [] if the file is missing and skips undecodable lines (for example, a line torn by a crash).
4. rec = Recorder(out, raw=False, extra_meta={"source": "replay", "replay_of": str(src)}, udp_port=None). No new .bin and no UDP.
5. rec.dec.schedule_labels(events) sorts the events by seq (None first). StreamDecoder._apply_pending_labels applies each one when a frame with seq64 >= ev.seq is decoded.
6. Reads the whole file and calls rec.feed(chunk, host_time=0.0) on 65536-byte (1 &lt;&lt; 16) slices.
7. summary = rec.close(). Prints "replayed {N} bytes with {M} label events from {labels_path}", then print_summary.
8. Returns 0.

**Notes.**

- Every row's host_time is 0.0.
- Because host_time is constant, a stream-id change (device re-plug) is bridged by exactly one tick (nt = max(1, round(0))). Live recording bridged it by the real disconnect time. Every t_us/seq after a reconnect is therefore smaller in the replay than in the live file, so label events recorded after a reconnect apply late or never. Running StreamDecoder with a 10 s gap confirmed this: live seq 1200 vs replay seq 200, and the label column was lost.
- Label events whose seq is never reached are not stored in the output metadata.
- LayoutChanged is not caught here: it ends the replay with a traceback before rec.close().
- The label events are read before the Recorder is built, and the Recorder always opens &lt;out stem>.labels.jsonl in "w" mode. Replay never writes to it, so it ends up empty. If --out has the same stem as the source .bin, the original .labels.jsonl is truncated whether or not --force is given; --force only matters when the .parquet already exists. The README's crash-recovery command (`--replay data/X.bin --out data/X.parquet`, no --force) therefore wipes X.labels.jsonl when the crash happened before the first Parquet row group was written (no X.parquet yet). When X.parquet exists, the same command refuses with exit code 2 unless --force is added. Either way, the applied events survive only in the new Parquet's kman.labels metadata.

#### `main (logger.py)`
*function* · [`host/logger.py:248`](../host/logger.py#L248)

```python
main(argv=None) -> int
```

**Purpose.** Command-line entry point. Parses the options and dispatches to port listing, replay or live recording.

**How it works.**

1. argparse with the module docstring as the description. Options: --port, --out, --replay, --labels, --duration (float, default 0 = until q), --subject (""), --notes (""), --udp-port (int, default DEFAULT_PORT = 51515; the control port is +1), --no-udp, --no-raw, --force, --list-ports.
2. If --list-ports: imports serial.tools.list_ports, prints "{device:&lt;20} {description}  [{hwid}]" for each comport, returns 0.
3. Else if --replay: returns run_replay(args).
4. Else if --port is missing: ap.error(...), which prints usage and exits with status 2.
5. Else: returns run_live(args).

**Notes.** The precedence is --list-ports, then --replay, then --port. The `if __name__ == "__main__": sys.exit(main())` guard turns the return value into the process exit code.

### `host/sim_device.py`

#### `make_session`
*function* · [`host/sim_device.py:31`](../host/sim_device.py#L31)

```python
make_session(args) -> sim.SimSession
```

**Purpose.** Builds the synthetic session (signals, labels and header) from the command-line options.

**How it works.**

1. cfg = sim.SimConfig(seed=args.seed, abdomen=args.abdomen, neck_imu=args.neck_imu, fs=args.rate). The other model parameters keep their SimConfig defaults, for example t0_us = 1_000_000 and seq0 = 100.
2. If --wrap, sets cfg.t0_us = 2**32 - 5_000_000, so the device's 32-bit microsecond timestamp wraps 5 s into the stream (this exercises StreamDecoder's unwrapping).
3. scenario = [(lab, d \* args.time_scale) for lab, d in sim.DEFAULT_SCENARIO]. The default is 15 segments, 440 s in total: rest 60, breath-hold 20, rest 30, dry swallow 40, rest 20, head movement 40, rest 20, drinking 40, talking 30, walking 30, rest 20, cough 15, rest 20, simulated obstruction 25, rest 30.
4. Returns sim.generate(cfg, scenario). The result is a SimSession with per-sample labels, int16 raw channels in header order, the firmware-format header dict, and ground truth.

**Notes.** generate builds the header after t0_us is set, so the header's t_us_at_header reflects --wrap. period_us is 1_000_000 // fs (integer division).

#### `write_session`
*function* · [`host/sim_device.py:39`](../host/sim_device.py#L39)

```python
write_session(args, s: sim.SimSession) -> int
```

**Purpose.** Writes a complete synthetic recording offline (.parquet + .bin + .labels.jsonl) through the same decoder and writer as the logger, with the ground-truth labels as label events.

**How it works.**

1. out = Path(args.write_session). dec = StreamDecoder(), then dec.schedule_labels(s.label_events()). One event per label change, with seq = seq0 + i and t_us = t0_us + i\*period_us, "as if the operator pressed the key" at the segment boundary.
2. w = SessionWriter(out, raw=True, extra_meta={"source": "simulator", "seed": args.seed}). Every event in s.label_events() is written to &lt;stem>.labels.jsonl, so a later logger.py --replay of the .bin finds the labels.
3. Iterates s.stream(drop_prob, corrupt_prob, seed=args.seed), which yields the header frame first, then data frames, with a STATUS frame after every fs-th sample (1 s). For each frame it calls w.write_raw(fr) and dec.feed(fr, host_time=0.0). If dec.new_header is set, it calls w.set_header(dec.header) and clears the flag. When dec.pending_rows() >= 2000, it calls w.write_batch(dec.drain()).
4. After the loop it drains and writes the remainder, then calls w.close(dec.headers, dec.label_events_dicts(), dec.summary(), notes="synthetic session (simulator)").
5. Prints the rows written, the duration (s.n / fs), the lost frames (dec.stats.dropped) and the CRC errors (dec.parser.stats.crc_errors). Returns 0.

**Notes.**

- Existing output is overwritten without warning; there is no --force check.
- --drop-prob skips a data frame entirely; the decoder counts it as dropped from the timestamp gap. If that sample was the 1 s status point, the status frame is skipped too.
- --corrupt-prob XORs one byte at a random index in [5, len) of a data frame with 0xFF, so its CRC fails and FrameParser discards it.
- The drop/corrupt RNG and the signal RNG are separate numpy generators that both use args.seed.
- host_time is 0.0 for every row.

#### `run_pty`
*function* · [`host/sim_device.py:63`](../host/sim_device.py#L63)

```python
run_pty(args, s: sim.SimSession) -> int
```

**Purpose.** Plays the synthetic session in real time on a pseudo-terminal, so logger.py (or live_view.py --port) can open it like the real USB serial device.

**How it works.**

1. If os.name == "nt", prints that --pty needs Linux/macOS (use com0com on Windows) and returns 2.
2. master, slave = os.openpty(). tty.setraw(slave) puts the slave in raw mode (no echo, no line-ending translation) so binary frames pass through unchanged. os.set_blocking(master, False). Prints the slave name (os.ttyname(slave), e.g. /dev/pts/N) and the scenario length.
3. gen = s.stream(drop_prob, corrupt_prob, seed=args.seed). write_all(next(gen)[1]) sends the header frame immediately.
4. t0 = time.monotonic(), last_label = None. For each (i, fr) from gen:
   a. due = t0 + (i + 1)/fs. Sleeps for due - monotonic() if that is positive.
   b. Reads up to 64 bytes from master (bytes the logger wrote to the slave). BlockingIOError/OSError gives b"".
   c. If P.CMD_SEND_HEADER (b"H") is in those bytes, write_all(s.header_frame()).
   d. write_all(fr).
   e. lab = int(s.labels[max(i, 0)]). If it differs from last_label, prints "t={i/fs:7.1f} s  true activity: {lab} {name}" so you can compare with your key presses.
5. KeyboardInterrupt ends playback. Returns 0 (also when the scenario finishes).

**Notes.**

- Pacing is by sample index, so dropped frames leave a real-time gap. A STATUS frame shares i with the data frame before it and goes out immediately.
- write_all retries every 1 ms until the pty accepts the bytes. With no reader, the stream stalls once the pty input queue is full. When a reader starts draining, the due times are already past and frames go out back-to-back until the schedule catches up. The device timestamps in the frames are unaffected.
- Only b"H" is handled; b"S" (status request) is ignored.
- max(i, 0) is defensive: the header (i = -1) was already taken by next(gen).

#### `run_pty.write_all`
*nested function* · [`host/sim_device.py:74`](../host/sim_device.py#L74)

```python
write_all(data: bytes) -> None
```

**Purpose.** Writes a whole frame to the non-blocking pty master despite short writes and a full buffer.

**How it works.** view = memoryview(data). While view is non-empty: n = os.write(master, view), then view = view[n:]. On BlockingIOError it sleeps 0.001 s and retries. Returns once every byte is written.

**Notes.** It closes over master. There is no timeout: it blocks the playback loop for as long as the pty stays full.

#### `main (sim_device.py)`
*function* · [`host/sim_device.py:109`](../host/sim_device.py#L109)

```python
main(argv=None) -> int
```

**Purpose.** Command-line entry point. Parses the options, builds the session and runs the selected mode.

**How it works.**

1. argparse with a required, mutually exclusive group: --pty (real-time virtual serial device) or --write-session OUT.parquet. Other options: --abdomen, --neck-imu, --rate (int, 100), --seed (int, 1), --time-scale (float, 1.0), --drop-prob (float, 0.0), --corrupt-prob (float, 0.0), --wrap.
2. s = make_session(args).
3. Returns run_pty(args, s) if --pty, else write_session(args, s).

**Notes.** sys.exit(main()) under the __main__ guard makes the return value the exit code. A non-Windows check exists only in run_pty.

### `host/to_csv.py`

#### `main (to_csv.py)`
*function* · [`host/to_csv.py:20`](../host/to_csv.py#L20)

```python
main(argv=None) -> int
```

**Purpose.** Exports a session .parquet to CSV, as samples (raw counts or physical units) or as the label-event list.

**How it works.**

1. Parses the positional `session` and --out, --units, --labels-only.
2. s = load_session(args.session). This reads the whole Parquet file into a DataFrame, parses the kman.header metadata into a SessionHeader, and loads kman.labels, kman.label_names (falling back to LABELS), stats, headers, extra and notes. src = Path(args.session).
3. --labels-only: out = --out or &lt;stem>_labels.csv. Writes pd.DataFrame(s.labels).to_csv(out, index=False), one row per label event with columns seq, t_us, host_time, label, name. Prints the count and returns 0.
4. Samples: out = --out or src.with_suffix(".csv").
   - --units: df = s.physical(). Columns: t_s = (t_us - t_us[0])/1e6, then each channel as float64 raw\*ch.scale (g, dps, or counts for flex_raw with scale 1). A value becomes NaN where bit ch.bit of that row's `valid` mask is 0. Then label, seq, segment.
   - otherwise: df = s.df.copy() (t_us, seq, segment, valid, the int16 channels, label, host_time), with t_s = (t_us - t_us.iloc[0])/1e6 inserted as column 0.
5. Inserts label_name right after label: df["label"].map(s.label_names), using the names stored in the file. Unknown codes give an empty cell.
6. df.to_csv(out, index=False, float_format="%.6g"). Prints "wrote {rows} rows x {cols} columns to {out}" and returns 0.

**Notes.**

- float_format "%.6g" keeps only 6 significant digits for every float column. In raw mode host_time (Unix seconds, about 1.7e9) is printed like "1.72846e+09", which loses all sub-second and most second resolution. t_s keeps 10 ms resolution only below 10000 s (about 2.8 h); above that it rounds to 0.1 s.
- --units drops t_us, valid and host_time.
- The Parquet file must have been closed properly (header metadata and footer); a file from a crashed recording cannot be loaded. Rebuild it first with logger.py --replay.

## 13. Host: synthetic physiology simulator

- **`host/kman_host/simulator.py`**: Software stand-in for the KMAN device. It builds a labelled synthetic session (chest IMU, optional abdomen and neck IMUs, neck flex sensor) from simple physiological models, quantises it to the firmware's int16 LSB scales, and serialises it byte-for-byte in the firmware frame format: a JSON header frame, DATA frames and periodic STATUS frames. The logger, live viewer, stream decoder and analysis code can therefore run and be tested without hardware. The `truth` dict it returns (breath waveform, swallow and head-movement times, rate, amplitude) is the reference the analysis tests check against.

**How it connects.** The rest of the system uses this module in four places. host/sim_device.py: make_session() builds SimConfig(seed, abdomen, neck_imu, fs=--rate), sets t0_us = 2**32 - 5_000_000 for --wrap, scales DEFAULT_SCENARIO by --time-scale, and calls generate(). write_session() passes s.label_events() to StreamDecoder.schedule_labels() and SessionWriter.write_label_event(), then feeds every frame from s.stream(drop_prob, corrupt_prob, seed) to SessionWriter.write_raw() and StreamDecoder.feed(). run_pty() writes the same stream to a pseudo-terminal at real-time pace and re-sends s.header_frame() when it reads P.CMD_SEND_HEADER, so host/logger.py can attach to it as if it were the board. host/live_view.py: sim_source() calls generate(SimConfig(abdomen=...)) and iterates s.stream(); whenever s.labels changes it inserts a P.TYPE_LABEL frame, and it hands the result to a PacedFrameSource. host/tests/test_stream.py uses generate(), stream(), data_frame(), header_frame(), label_events() and raw to test timestamp/seq wrap, gap accounting, CRC corruption, layout change, device restart and the session round trip. analysis/tests/test_analysis.py runs generate() through stream(), StreamDecoder, SessionWriter, load_session and kman_analysis.analyze, then checks results against s.truth['resp'], swallow_times and head_times. Going the other way, the module calls kman_host.protocol (encode_frame, TYPE_HEADER / TYPE_DATA / TYPE_STATUS, DATA_PREFIX, DeviceStatus, encode_status, STATUS_FLAG_HFXO) and kman_host.labels.label_name. build_header() reproduces the header schema of firmware/src/main.cpp buildHeaderFrame(). The chest gyro and gravity sign convention matches the dg/dt = -omega x g prediction in kman_analysis.breathing.gravity_complementary.

### `host/kman_host/simulator.py`

#### `ACCEL_SCALE / GYRO_SCALE / ADC_FULL_SCALE / DEFAULT_SCENARIO`
*module constants* · [`host/kman_host/simulator.py:22`](../host/kman_host/simulator.py#L22)

```python
ACCEL_SCALE = 61e-6; GYRO_SCALE = 8.75e-3; ADC_FULL_SCALE = 16383; DEFAULT_SCENARIO: list[tuple[int, float]]
```

**Purpose.** Sensor scale factors that match the firmware configuration, plus the default labelled protocol that generate() synthesises.

**How it works.** ACCEL_SCALE = 61e-6 g/LSB (LSM6DSOX at +-2 g). GYRO_SCALE = 8.75e-3 dps/LSB (at 250 dps). ADC_FULL_SCALE = 16383, the 14-bit flex ADC maximum. DEFAULT_SCENARIO is a list of (label code, seconds): rest 60, breath-hold 20, rest 30, dry swallow 40, rest 20, head movement 40, rest 20, drinking 40, talking 30, walking 30, rest 20, cough 15, rest 20, simulated obstruction 25, rest 30. The total is 440 s, which is 44,000 samples at 100 Hz. Label codes follow kman_host.labels.LABELS: 1 rest, 2 talking, 3 drinking/swallow, 4 dry swallow, 5 head movement, 6 walking, 7 cough, 8 breath-hold, 9 simulated obstruction.

**Notes.** The same scale factors are written into the header channel entries, so decoders turn raw counts back into g, dps and counts using values that agree with the simulator.

#### `SimConfig`
*dataclass* · [`host/kman_host/simulator.py:33`](../host/kman_host/simulator.py#L33)

```python
@dataclass class SimConfig
```

**Purpose.** Holds every tunable parameter of a synthetic session: sample rate, RNG seed, which optional sensors are present, signal amplitudes, noise levels, and the starting timestamp and sequence number.

**How it works.**

Fields and defaults:
- fs = 100: sample rate in Hz.
- seed = 1: seed for numpy default_rng in generate().
- abdomen = False: adds the abdomen IMU (abd_\* channels, header bit 1).
- neck_imu = False: adds the neck IMU (neck_\* channels, header bit 3).
- resp_rate_bpm = 14.0: base breathing rate.
- resp_tilt_deg_pp = 1.0: chest tilt about x, peak-to-peak per breath at amplitude 1.
- flex_baseline = 8200.0: flex ADC baseline in counts.
- flex_noise = 6.0: flex white noise, counts rms.
- swallow_counts = 70.0: nominal flex bump height of one swallow.
- head_counts = (400.0, 1200.0): uniform range of head-movement flex deflection magnitude.
- accel_noise_g = 0.35e-3: accel white noise per axis, g rms.
- gyro_noise_dps = 0.03: gyro white noise per axis, dps rms.
- gyro_bias_dps = (0.4, -0.25, 0.15): constant chest gyro bias, x/y/z.
- t0_us = 1_000_000: device timestamp of sample 0.
- seq0 = 100: sequence number of sample 0.

**Notes.** The comment on t0_us says to set it near 2**32 to exercise the u32 timestamp wrap. test_stream uses t0_us = 2**32 - 2_000_000 with seq0 = 65530, and sim_device --wrap uses 2**32 - 5_000_000. The dataclass __init__ is auto-generated.

#### `SimSession`
*dataclass* · [`host/kman_host/simulator.py:52`](../host/kman_host/simulator.py#L52)

```python
@dataclass class SimSession(cfg: SimConfig, labels: np.ndarray, raw: dict[str, np.ndarray], header: dict, truth: dict = field(default_factory=dict))
```

**Purpose.** Holds one generated session and turns it into device frames (header, data, status) on demand, so callers get the same bytes the firmware would send.

**How it works.**

Fields:
- cfg: the SimConfig used.
- labels: int8 array, one label code per sample.
- raw: dict mapping channel name to an int16 array, ordered exactly as header['channels'].
- header: the session header dict from build_header().
- truth: ground-truth dict for tests. Keys: resp, swallow_times (s), head_times (s), rate_bpm, amp, plus resp_abd when the abdomen IMU is enabled.
Frames are built per sample index by the methods below. Nothing is precomputed.

**Notes.** Constructed only by generate().

#### `SimSession.n`
*property* · [`host/kman_host/simulator.py:60`](../host/kman_host/simulator.py#L60)

```python
@property def n(self) -> int
```

**Purpose.** Number of samples in the session.

**How it works.** Returns len(self.labels).

#### `SimSession.label_events`
*method* · [`host/kman_host/simulator.py:63`](../host/kman_host/simulator.py#L63)

```python
def label_events(self) -> list[dict]
```

**Purpose.** Produces label-change events as if an operator had pressed the label key exactly at each segment boundary. Replay decoders and session writers use them to attach ground-truth labels.

**How it works.**

1. Computes segment-start indices: np.flatnonzero(np.r_[True, labels[1:] != labels[:-1]]). Index 0 is always included.
2. For each start index i with label lab different from the previous event's label, appends {"seq": cfg.seq0 + i, "t_us": cfg.t0_us + i \* period_us, "host_time": 0.0, "label": lab, "name": label_name(lab)}.
3. Returns the list. Example with defaults: seq 100 / t_us 1,000,000 for label 1, then seq 6100 / t_us 61,000,000 for label 8.

**Notes.** seq and t_us are unwrapped: they are not masked to 16/32 bits. They line up with StreamDecoder's 64-bit unwrapped seq64, which _apply_pending_labels compares against after schedule_labels(). The `lab != prev` check never filters anything, because boundary indices are by construction label changes.

#### `SimSession.period_us`
*property* · [`host/kman_host/simulator.py:76`](../host/kman_host/simulator.py#L76)

```python
@property def period_us(self) -> int
```

**Purpose.** Sample period in microseconds, used for timestamps.

**How it works.** Returns 1_000_000 // cfg.fs, which is 10000 at 100 Hz.

**Notes.** Integer division. If fs does not divide 1e6, timestamps run on the truncated period, which is the same value the header announces.

#### `SimSession.header_frame`
*method* · [`host/kman_host/simulator.py:79`](../host/kman_host/simulator.py#L79)

```python
def header_frame(self) -> bytes
```

**Purpose.** Encodes the session header as a device HEADER frame.

**How it works.** Returns P.encode_frame(P.TYPE_HEADER (0x02), json.dumps(self.header).encode()). encode_frame emits sync AA 55, then u16le len (= body + 1), then the type byte, then the body, then a u16le CRC-16/CCITT-FALSE over len+type+body.

**Notes.** With the default config the frame is 1887 bytes. encode_frame raises ValueError when len(body) + 1 exceeds MAX_LEN (4096), which means a JSON body longer than 4095 bytes.

#### `SimSession.data_frame`
*method* · [`host/kman_host/simulator.py:82`](../host/kman_host/simulator.py#L82)

```python
def data_frame(self, i: int) -> bytes
```

**Purpose.** Encodes sample i as a device DATA frame with the same wrapped 32-bit timestamp and 16-bit sequence the firmware sends.

**How it works.**

1. t32 = (cfg.t0_us + i \* period_us) & 0xFFFFFFFF.
2. seq16 = (cfg.seq0 + i) & 0xFFFF.
3. body = P.DATA_PREFIX.pack(t32, seq16, header['present_mask']). DATA_PREFIX is struct '&lt;IHB' (t_us u32, seq u16, valid u8). The valid byte is set to the present mask, so every present sensor is marked valid.
4. Appends, in header channel order, the int16 values raw[name][i] as little-endian ('&lt;i2').
5. Returns P.encode_frame(P.TYPE_DATA (0x01), body).

**Notes.** Frame size is 5 + 7 + 2\*channels + 2 bytes: 28 bytes for the default 7 channels (chest 6 + flex), 52 bytes with all 19 channels.

#### `SimSession.status_frame`
*method* · [`host/kman_host/simulator.py:89`](../host/kman_host/simulator.py#L89)

```python
def status_frame(self, i: int, sent: int, drops: int = 0) -> bytes
```

**Purpose.** Encodes a device STATUS frame for the time of sample i, with fixed health values, so the status path of the host is exercised.

**How it works.**

Builds P.DeviceStatus with these fields:
- t_us = (t0_us + i\*period_us) & 0xFFFFFFFF
- ticks = seq0 + i
- frames_sent = sent
- overruns = 0
- tx_drops = drops
- read_errors = (0, 0, 0, 0)
- max_latency_us = 620
- tx_high_water = 180
- flags = P.STATUS_FLAG_HFXO (0x01)
Packs it with P.encode_status (struct '&lt;B5I4IHHB', leading version byte 1) and returns P.encode_frame(P.TYPE_STATUS (0x03), ...).

**Notes.** ticks is not masked to 32 bits. stream() never passes drops, so tx_drops is always 0 in streamed sessions.

#### `SimSession.stream`
*generator method* · [`host/kman_host/simulator.py:97`](../host/kman_host/simulator.py#L97)

```python
def stream(self, drop_prob: float = 0.0, corrupt_prob: float = 0.0, seed: int = 0, status_every_s: float = 1.0)
```

**Purpose.** Yields the whole session as the byte frames a device would emit, in device order, with optional random frame loss and byte corruption to test decoder robustness.

**How it works.**

1. rng = np.random.default_rng(seed). This RNG is separate from generate()'s.
2. Yields (-1, header_frame()) first.
3. every = int(status_every_s \* fs), which is 100 at the defaults; sent = 0.
4. For i in range(n):
   a. If drop_prob is nonzero and rng.random() &lt; drop_prob, the sample is skipped (continue).
   b. fr = data_frame(i).
   c. If corrupt_prob is nonzero and rng.random() &lt; corrupt_prob, one byte at a random index in [5, len(fr)) is XORed with 0xFF. Index 5 onward is the body and CRC, so sync, length and type are never touched.
   d. sent += 1, then yields (i, fr).
   e. If every is nonzero and i % every == every - 1 (i = 99, 199, ...), also yields (i, status_frame(i, sent)).

**Notes.** A corrupted frame always fails the CRC but keeps its framing, so exactly one sample is lost per corruption. test_corruption_costs_only_the_corrupted_frame relies on this. A dropped sample skips the status check for that index, so the status frame due on a dropped index is lost too. sent counts corrupted frames but not dropped ones. Status frames themselves are never dropped or corrupted. RNG draws happen only for nonzero probabilities, so enabling one option changes the random sequence of the other.

#### `_smooth`
*function* · [`host/kman_host/simulator.py:120`](../host/kman_host/simulator.py#L120)

```python
def _smooth(x: np.ndarray, n: int) -> np.ndarray
```

**Purpose.** Moving-average (boxcar) low-pass filter that returns an array the same length as the input. Used to make slow random processes and to soften step transitions between labels.

**How it works.**

1. If n &lt;= 1, returns x unchanged.
2. Kernel k = ones(n)/n.
3. Pads x by repeating edge values: n//2 samples before, n-1-n//2 after.
4. Returns np.convolve(pad, k, 'valid'), which has length len(x). Output[k] is the mean of x[k-n//2 .. k+n-1-n//2].

**Notes.** Edge padding avoids droop at the ends for slowly varying inputs such as the 0/1 label masks that generate() smooths. For even n the window reaches one sample further into the past than the future. In the interior, smoothing white noise of unit std with window N gives a std of about 1/sqrt(N), and generate() multiplies by constant factors to restore usable amplitudes. The caveat is noise input: 'edge' padding repeats the single first sample n//2 times and the last sample n-1-n//2 times. Within about n/2 samples of each end the output is therefore dominated by that one sample (output[0] is roughly 0.5\*x[0] plus a small average) instead of a mean of n independent values. For n = 1000 that end excursion is about 15x the interior rms. In generate() this inflates the respiration-rate wander (10 s window) and the postural sway (20 s window) in the first and last few seconds of the session. With seed 1, rate_bpm ends at about 21.8 bpm during the final rest segment, and the chest tilt at sample 0 is offset by about 2.7 deg.

#### `_rot_x`
*function* · [`host/kman_host/simulator.py:128`](../host/kman_host/simulator.py#L128)

```python
def _rot_x(v: np.ndarray, a: np.ndarray) -> np.ndarray
```

**Purpose.** Rotates each row of an (N,3) vector array about the x axis by its own per-sample angle.

**How it works.** With c = cos(a), s = sin(a): x' = x, y' = c\*y - s\*z, z' = s\*y + c\*z. This is a right-handed active rotation by +a. Returns an (N,3) array built with np.stack(axis=1).

#### `_rot_z`
*function* · [`host/kman_host/simulator.py:133`](../host/kman_host/simulator.py#L133)

```python
def _rot_z(v: np.ndarray, a: np.ndarray) -> np.ndarray
```

**Purpose.** Rotates each row of an (N,3) vector array about the z axis by its own per-sample angle.

**How it works.** With c = cos(a), s = sin(a): x' = c\*x - s\*y, y' = s\*x + c\*y, z' = z. Returns (N,3).

#### `_bandnoise`
*function* · [`host/kman_host/simulator.py:138`](../host/kman_host/simulator.py#L138)

```python
def _bandnoise(rng, n, fs, lo, hi, rms)
```

**Purpose.** Generates Gaussian noise limited to a frequency band and scaled to a target rms. Used for the talking vibration and the talking flex signal.

**How it works.**

1. x = rng.standard_normal(n).
2. f = rfftfreq(n, 1/fs); X = rfft(x).
3. Zeroes every bin with f &lt; lo or f > hi (brick-wall filter).
4. y = irfft(X, n).
5. If y.std() > 0, returns y \* (rms / std); otherwise returns y.

**Notes.** The rms is normalised over the whole n-sample record before generate() multiplies it by a label mask, so the stated rms applies inside the masked region. Being FFT-based, the noise is circularly periodic over the record.

#### `_bump`
*function* · [`host/kman_host/simulator.py:148`](../host/kman_host/simulator.py#L148)

```python
def _bump(n_total: int, start: int, dur: int, amp: float, shape: str = "cos") -> tuple[slice, np.ndarray]
```

**Purpose.** Builds a smooth transient pulse to add into a signal at a given position. A raised-cosine bump models swallows and coughs; a plateau models head movements and cup-to-lips.

**How it works.**

1. end = min(n_total, start + dur). t = arange(end - start) / max(dur, 1), so t runs from 0 to just under 1 over the intended duration.
2. shape 'plateau': y = clip(min(t/0.25, (1-t)/0.25), 0, 1) gives a trapezoid that ramps up over the first 25% of dur, holds at 1 for the middle 50%, and ramps down over the last 25%. Then y = 0.5 - 0.5\*cos(pi\*y) rounds the ramp corners.
3. Any other shape ('cos'): y = 0.5 - 0.5\*cos(2\*pi\*t), a Hann-shaped bump from 0 up to 1 at t = 0.5 and back.
4. Returns (slice(start, end), amp \* y).

**Notes.** t is normalised by dur, not by the truncated length, so a bump that runs past the array end is cut off rather than compressed. A negative start would produce a wrong slice, but generate() never passes one. The smallest offset from a segment start is a cough bump at +1.5 s. Head bumps start at +2 s and swallow bumps at +3 s or later. The cup-to-lips plateau is the only bump that starts before its event index k: it starts at k - 1 s, which is segment start + 2 s.

#### `_resp_shape`
*function* · [`host/kman_host/simulator.py:159`](../host/kman_host/simulator.py#L159)

```python
def _resp_shape(phase_frac: np.ndarray, ie_split: float = 0.38) -> np.ndarray
```

**Purpose.** Breath waveform with inspiration/expiration asymmetry: one breath per unit of phase, fast rise and slower fall.

**How it works.**

1. p = phase_frac % 1.0.
2. Inspiration (p &lt; ie_split = 0.38): r = 0.5 - 0.5\*cos(pi\*p/0.38), rising from 0 to 1.
3. Expiration: r = 0.5 + 0.5\*cos(pi\*(p - 0.38)/0.62), falling from 1 to 0.
4. Returns r - 0.5, in the range -0.5..0.5. The trough is at p = 0 and the peak at p = 0.38.

**Notes.** I:E is 0.38:0.62, about 1:1.63. At 14 bpm (4.29 s per breath) that is 1.63 s up and 2.66 s down. The waveform is continuous at both phase boundaries.

#### `generate`
*function* · [`host/kman_host/simulator.py:167`](../host/kman_host/simulator.py#L167)

```python
def generate(cfg: SimConfig | None = None, scenario: list[tuple[int, float]] | None = None) -> SimSession
```

**Purpose.** Synthesises a complete labelled multi-sensor session from a scenario of (label, seconds) segments. Every signal is in the device's int16 raw format, and the ground truth is returned alongside.

**How it works.**

0. Setup: cfg defaults to SimConfig() and scenario to DEFAULT_SCENARIO (an empty list also falls back to the default). rng = default_rng(cfg.seed). labels = concatenation of np.full(int(d\*fs), lab, int8) per segment. n = len(labels); t = arange(n)/fs. Masks come from is_(lab).

1. Respiration rate, amplitude and phase:
   - rate = resp_rate_bpm \* (1 + 0.08 \* _smooth(randn(n), 10\*fs) \* 8). This is a slow random wander with a 10 s boxcar. In the interior it is about 2% rms (1/sqrt(1000) \* 0.64). Because _smooth edge-pads by repeating the single first or last noise sample, the wander is much larger within about 5 s of either end of the session, roughly 0.32\*|x[0]| or 0.32\*|x[-1]|. With seed 1, rate_bpm starts at about 15.4 bpm and ends at about 21.8 bpm (+55%) during the final rest segment.
   - Rate is then multiplied by step factors: (1 + 0.5\*is_(6)) for walking (14 to 21 bpm), (1 + 0.45\*is_(9)) for obstruction (about 20.3 bpm), and (1 + 0.15\*is_(2)) for talking (about 16.1 bpm).
   - phase = cumsum(rate/60)/fs, in breaths elapsed. It stays continuous across rate changes.
   - amp = 1.0 + 0.3\*is_(6) + 0.6\*is_(9) - 0.25\*is_(2) - 1.0\*is_(8). That gives walking 1.3, obstruction 1.6, talking 0.75, breath-hold 0, and 1.0 otherwise.
   - Per-breath variability: breath_idx = floor(phase); amp \*= per_breath[breath_idx], where per_breath = 1 + 0.12\*randn(max+2) is held constant within each breath.
   - While talking (label 2): amp \*= clip(talk_mod, 0.3, 1.6), where talk_mod = 1 + 0.5\*_smooth(randn(n), 2\*fs)\*4, about 14% rms with roughly 2 s correlation.
   - Finally amp = clip(_smooth(clip(amp, 0, None), fs), 0, None). The 1 s boxcar ramps label transitions such as entering breath-hold.

2. Swallows and swallow apnea. apnea = ones(n). The code loops over (label 4, gap 8.0 s) and then (label 3, gap 10.0 s). For each contiguous segment of that label, k starts 3 s in and runs while k &lt; segment end - 3 s:
   - count = 1 for a dry swallow. For drinking, count = rng.integers(1, 3), which is 1 or 2.
   - Drinking only (cup to lips): a plateau _bump of -180 counts on flex_delta starts 1.0 s before k and lasts 3.5 s. head_pitch gets -15 deg times the same normalised shape (y/-180).
   - For each swallow j: ks = k + j\*1.6 s. dur = uniform(0.6, 1.2) s. A cos _bump of swallow_counts \* uniform(0.8, 1.25) (56-87.5 counts at default) is added to flex_delta. ks/fs is appended to swallow_times. apnea[ks - 0.3 s : ks + 0.9 s] = 0.15, which is a 1.2 s window where breathing drops to 15%.
   - k advances by gap_s \* uniform(0.8, 1.2): 6.4-9.6 s for dry swallows, 8-12 s for drinking.
   After the loop: amp \*= _smooth(apnea, 0.3 s), which softens the apnea edges.

3. Head movements (label 5). In each segment, k starts 2 s in and runs while k &lt; end - 4 s:
   - dur = uniform(1.5, 3.0) s.
   - a = uniform(400, 1200) \* random sign.
   - A plateau _bump of amplitude a is added to flex_delta.
   - head_pitch += 35 deg \* y / head_counts[1] (1200), so pitch reaches +-(11.7..35) deg with the same sign as the flex deflection.
   - chest_jolt[:,1] += 0.004 \* y/|a|, a 'shoulder coupling' of up to 4 mg on chest y. Its sign follows the sign of a.
   - k/fs is appended to head_times, and k advances by uniform(3.5, 5.0) s.

4. Coughs (label 7). In each segment, k starts 1.5 s in and runs while k &lt; end - 1 s. Over a 0.3 s window with env = exp(-tt/0.06), a 60 ms decay:
   - chest_jolt z += 0.35 g \* env \* sin(2\*pi\*14\*tt)
   - chest_jolt y += 0.12 g \* env
   - gyro_jolt x += 25 dps \* env \* sin(2\*pi\*9\*tt)
   - a 0.35 s cos flex bump of 250 counts
   k advances by uniform(2.5, 4.5) s. Cough times are not recorded in truth.

5. Chest tilt:
   - resp = amp \* _resp_shape(phase) is the ground-truth breath waveform. tilt_amp = deg2rad(resp_tilt_deg_pp).
   - th_x = tilt_amp \* resp, which is 1 deg peak-to-peak at amp 1. th_z = 0.25 \* tilt_amp \* resp, which is 0.25 deg peak-to-peak.
   - th_x += deg2rad(0.012) \* sin(2\*pi\*1.15\*t), a cardiac ballistic term at 69 bpm. The code comment says it is visible during breath-hold.
   - th_x += deg2rad(0.3) \* _smooth(randn(n), 20\*fs) \* 10, a slow postural sway of about 0.07 deg rms in the interior. Because of _smooth's edge padding, the sway near the start and end of the session is dominated by a single noise sample and can reach several degrees. With seed 1 the chest tilt at sample 0 is offset by about 2.7 deg.

6. Walking (label 6). walk = _smooth(is_(6), fs), which ramps on and off over 1 s.
   - acc_walk (g): x = 0.10 sin(2\*pi\*0.9t); y = 0.25 sin(2\*pi\*1.8t) + 0.08 sin(2\*pi\*3.6t + 0.4); z = 0.06 sin(2\*pi\*1.8t + 1.0).
   - gyr_walk (dps): x = 5.0 sin(2\*pi\*1.8t); y = 3.0 sin(2\*pi\*0.9t + 0.3); z = 8.0 sin(2\*pi\*0.9t).
   Both are multiplied by walk.

7. Talking (label 2). talk = _smooth(is_(2), fs).
   - acc_talk = three independent _bandnoise(5-15 Hz, 0.004 g rms) columns times talk.
   - flex_delta += _bandnoise(0.8-4.0 Hz, 12 counts rms) times talk.

8. Chest IMU synthesis and sign convention:
   - up = [0, 0.97, 0.243], normalised. The resting specific force sits mostly on +y and tilts about 14 deg toward +z.
   - g = _rot_z(_rot_x(up, -th_x), -th_z). Gravity is rotated by -tilt because the body rotates by +tilt. The comment points to the analysis complementary filter.
   - acc = g + acc_walk + acc_talk + chest_jolt + randn\*0.35 mg.
   - gyr = [gradient(th_x)\*fs, 0, gradient(th_z)\*fs] \* 180/pi + gyr_walk + gyro_jolt + gyro_bias_dps (0.4, -0.25, 0.15) + randn\*0.03 dps.
   The gyro therefore reads +d(theta)/dt while gravity in the sensor frame turns by -theta. That satisfies dg/dt = -omega x g, the prediction step of kman_analysis.breathing.gravity_complementary.
   Derived magnitudes at rest: about +-8.5 mg on az and +-2 mg on ay and ax per breath, and about 1 dps peak on gx during inspiration.

9. Quantisation (chest): chest_a{x,y,z} = clip(round(acc/61e-6), -32768, 32767).astype(int16), which saturates at about +-2.0 g. chest_g{x,y,z} = clip(round(gyr/8.75e-3), ...), which saturates at about +-286.7 dps.
   truth = {resp, swallow_times, head_times, rate_bpm (per-sample rate), amp (final amplitude including apnea)}.

10. Abdomen paradox model (cfg.abdomen):
   - paradox = _smooth(is_(9), fs).
   - resp_ab = amp \* _resp_shape(phase + 0.03) \* 1.2 \* (1 - 2\*paradox). The abdomen leads by 0.03 breath (about 11 deg) with 1.2x excursion. Its sign inverts to anti-phase during obstruction, with a 1 s transition.
   - th_ab = tilt_amp \* 1.3 \* resp_ab, about 1.56 deg peak-to-peak at rest.
   - up_ab = [0, 0.9, 0.436] normalised (about 26 deg toward +z) is rotated with _rot_x(-th_ab) only. There is no z tilt.
   - acc_ab = ga + 1.1\*acc_walk + 0.5\*chest_jolt + accel noise. No talk vibration is added.
   - gyr_ab = [gradient(th_ab)\*fs\*180/pi, 0, 0] + 0.8\*gyr_walk + bias (-0.2, 0.3, 0.1) + gyro noise. No cough gyro jolt is added.
   - Quantised to abd_a\*/abd_g\* with the same scales. truth['resp_abd'] = resp_ab.

11. Flex model:
   - flex = flex_baseline (8200) + drift + pulse + flex_delta + randn\*flex_noise (6 counts).
   - drift = 50\*sin(2\*pi\*t/900): 50 counts amplitude with a 15 min period.
   - pulse = 3.0\*sin(2\*pi\*1.15\*t), a 3-count cardiac term.
   - flex_delta holds the swallow, drinking, head, cough and talk contributions.
   - flex_raw = clip(round(flex), 0, 16383).astype(int16).
   Flex has no breathing component.

12. Neck IMU (cfg.neck_imu):
   - hp = head_pitch + deg2rad(0.2)\*sin(2\*pi\*0.25\*t).
   - Gravity [0, 1, 0] is rotated with _rot_x(-hp).
   - acc_n = gn + acc_walk + accel noise.
   - gyr_n = [gradient(hp)\*fs\*180/pi, 0, 0] + gyro noise. There is no bias and no walking gyro.
   - Quantised to neck_a\*/neck_g\*.

13. header = build_header(cfg). raw is re-keyed in header channel order (chest, [abd], flex_raw, [neck]). Returns SimSession(cfg, labels, raw, header, truth).

**Notes.** The output is deterministic for a given seed. The abdomen block draws random numbers after the chest channels are computed, so enabling abdomen leaves chest_\* identical but changes the flex_raw noise and the neck signals; this was verified by running the code. swallow_times and head_times are bump start times, not peak times. The swallow peak is dur/2 later; the analysis test allows 1.5 s of tolerance. Walking, talk and jolt accelerations are added on top of the gravity rotation and are not kinematically consistent with the gyro. In the default scenario the run produces 11 swallows and 8 head movements.

#### `generate.is_`
*nested function* · [`host/kman_host/simulator.py:176`](../host/kman_host/simulator.py#L176)

```python
def is_(lab) -> np.ndarray
```

**Purpose.** Per-sample indicator for a label code, used to switch model terms on and off by multiplication.

**How it works.** Returns (labels == lab).astype(float): 1.0 where the sample carries that label and 0.0 elsewhere. It closes over generate()'s labels array.

#### `build_header`
*function* · [`host/kman_host/simulator.py:345`](../host/kman_host/simulator.py#L345)

```python
def build_header(cfg: SimConfig, stream_id: int = 1) -> dict
```

**Purpose.** Builds the session header dict in the same JSON schema the firmware emits (the docstring points to firmware/src/main.cpp buildHeaderFrame). Host code discovers sensors, channel order and scales from this header.

**How it works.**

1. Slot entries, in order:
   - chest_imu: imu('chest_imu', 'chest', bit 0, '0x6A').
   - abdomen_imu: imu('abdomen_imu', 'abd', bit 1, '0x6B') if cfg.abdomen, else {slot: 'abdomen_imu', bit: 1, status: 'not_configured'}.
   - neck_flex: bit 2, always status 'ok', model 'resistive divider', sensor 'simulated flex', r_fixed_ohm 47000, topology '3V3 - flex - ADC - R_fixed - GND', pin 0, adc 'simulated', bits 14, full_scale 16383. It adds channel {name 'flex_raw', slot 'neck_flex', bit 2, unit 'counts', scale 1}.
   - neck_imu: imu('neck_imu', 'neck', bit 3, '0x6B') if cfg.neck_imu, else not_configured.
2. present_mask = sum(1 &lt;&lt; bit) over sensors with status 'ok': 5 by default, 7 with abdomen, 13 with neck, 15 with both.
3. Returns the dict with these keys:
   - type 'kman_session_header', protocol 1, fw_version 'sim', build 'simulator', board 'simulator', mcu 'none', device_id 'SIMULATOR0000000'
   - stream_id
   - t_us_at_header = cfg.t0_us
   - sample_rate_hz = cfg.fs, period_us = 1_000_000 // fs
   - clock {source/hfclk 'simulated', t_unit 'us', t_bits 32, seq_bits 16}
   - frame {sync 'AA55', len 'u16le, bytes of type+body', crc 'CRC-16/CCITT-FALSE over len,type,body; u16le', data_prefix ['t_us:u32', 'seq:u16', 'valid:u8'], channel_type 'i16'}
   - present_mask, sensors, channels

**Notes.** Channel order here defines the DATA frame layout and the order of SimSession.raw: chest(6), abd(6) if present, flex_raw, neck(6) if present. Abdomen and neck IMUs both report address 0x6B. The odr_hz 208 in the sensor entries is the IMU output rate, separate from sample_rate_hz (cfg.fs). t_us_at_header is the unmasked t0_us. Changing abdomen or neck_imu changes the layout, which StreamDecoder raises LayoutChanged on (test_layout_change_raises).

#### `build_header.imu`
*nested function* · [`host/kman_host/simulator.py:348`](../host/kman_host/simulator.py#L348)

```python
def imu(slot, prefix, bit, addr) -> tuple[dict, list[dict]]
```

**Purpose.** Builds the sensor descriptor and the six channel descriptors for one LSM6DSOX IMU slot.

**How it works.**

Sensor dict fields:
- slot, bit, addr; status 'ok'; model 'LSM6DSOX'; bus 'i2c'
- mode 'timer-master register read (no FIFO)'; odr_hz 208
- accel_range_g 2; accel_lpf 'LPF2 ODR/10'; accel_lpf_hz 20.8
- gyro_range_dps 250; gyro_lpf 'LPF1 FTYPE=6 (~26 Hz at 208/416 Hz ODR)'
- block_data_update True
Channels, in order ax, ay, az, gx, gy, gz: {name f'{prefix}_{s}', slot, bit, unit 'g' for a\* else 'dps', scale ACCEL_SCALE (61e-6) or GYRO_SCALE (8.75e-3)}. Returns (sensor, chans).

## 14. Host: live viewer

- **`host/live_view.py`**: Standalone pyqtgraph application that shows the KMAN signals scrolling in real time: chest and abdomen respiration (band-passed accelerometer tilt, as PC1 or as 3 axes), neck flex (raw, or minus a 10 s-time-constant EMA baseline), chest motion energy, and a colour strip of the current activity label. It reads bytes from one of four sources (the logger's UDP re-broadcast, the serial port directly, the built-in simulator, or a raw .bin replay), decodes them with the shared kman_host.stream.StreamDecoder, and filters them causally in LiveProcessor. It is a display and labelling tool only. It records no session data. The only file it can write is the optional --screenshot PNG, which is saved only when --exit-after fires. Labels are recorded only in UDP mode, where they are forwarded to logger.py. Its respiration trace is a display proxy, not the offline complementary-filter pipeline.

**How it connects.** The user runs live_view.py from the command line (`python host/live_view.py ...`, which calls main()); no other module imports it. Its main partner is host/logger.py. Recorder.feed re-broadcasts every decoded device frame verbatim through UdpSender to 127.0.0.1:51515. Recorder.reannounce_header re-sends the last HEADER plus a TYPE_LABEL (0x10) frame every 2 s, so a viewer that joins late can start decoding. Recorder.announce_label sends the label after every change. In the other direction, UdpSource.send_label sends b"LABEL n" to port 51516. The logger's control UdpReceiver picks it up, parse_label_command decodes it, and Recorder.set_label (which calls StreamDecoder.set_label and SessionWriter.write_label_event) records it before announcing it back. In --port mode the viewer instead talks to the firmware directly: it writes 'H' (P.CMD_SEND_HEADER), which serviceHost() in firmware/src/main.cpp turns into g_headerPending and then queueHeaderIfPending(). Inbound, the viewer depends on kman_host. From stream it uses StreamDecoder.feed, handle_frame (through feed), drain, .header, .label, .status, .stats.frames_data/dropped/drop_pct(), .parser.stats.crc_errors, and LayoutChanged, raised by StreamDecoder._on_header. From protocol it uses FrameParser.feed, encode_frame, TYPE_DATA/TYPE_HEADER/TYPE_LABEL, SessionHeader.from_json/sample_rate_hz/channel_names/channels (with Channel.scale and .bit). From udp_bus it uses UdpReceiver.poll, UdpSender.send and DEFAULT_PORT. From labels it uses LABELS, LABEL_COLORS_DARK and label_name. From simulator it uses generate, SimConfig, and SimSession.stream/.labels/.cfg.fs. Its filtering is a display-only stand-in. The offline analysis does not call this code; it runs its own complementary-filter pipeline on the recorded session files.

### `host/live_view.py`

#### `UdpSource`
*class* · [`host/live_view.py:45`](../host/live_view.py#L45)

```python
class UdpSource
```

**Purpose.** Byte source for the default mode: attaches to a running logger.py, which owns the serial port and re-broadcasts the device frames over localhost UDP.

**How it works.** Fields: `name` (class attribute "logger (udp)", replaced in __init__ with "logger udp:&lt;port>"; shown in the window title and stats line), `rx` (kman_host.udp_bus.UdpReceiver bound to 127.0.0.1:port, non-blocking), `tx` (UdpSender aimed at 127.0.0.1:port+1, the logger's control port). It implements the same duck-typed interface as the other sources: `poll() -> bytes` and `send_label(code) -> bool`.

**Notes.** Default port is DEFAULT_PORT = 51515 (control 51516).

#### `UdpSource.__init__`
*constructor* · [`host/live_view.py:48`](../host/live_view.py#L48)

```python
def __init__(self, port: int)
```

**Purpose.** Opens the receive socket for frames and the send socket for label commands.

**How it works.**

1. `self.rx = UdpReceiver(port)`: creates an AF_INET/SOCK_DGRAM socket, binds it to ("127.0.0.1", port) and sets it non-blocking.
2. `self.tx = UdpSender(port + 1)`: unbound UDP socket whose destination is ("127.0.0.1", port+1).
3. `self.name = f"logger udp:{port}"`.

**Notes.** UdpReceiver binds without SO_REUSEADDR, so a second viewer on the same port fails at bind. Binding does not require the logger to be running; the viewer just waits for frames.

#### `UdpSource.poll`
*method* · [`host/live_view.py:53`](../host/live_view.py#L53)

```python
def poll(self) -> bytes
```

**Purpose.** Returns all bytes received since the last call without blocking.

**How it works.** `b"".join(self.rx.poll())`. UdpReceiver.poll loops `recvfrom(65536)` until BlockingIOError/InterruptedError/OSError and returns the list of datagrams; they are concatenated. Returns b"" when nothing is pending.

**Notes.** The logger may split a large send into 8192-byte datagrams (MAX_DATAGRAM), so a frame can span datagrams; the FrameParser inside StreamDecoder reassembles it. Datagram order is assumed (localhost).

#### `UdpSource.send_label`
*method* · [`host/live_view.py:56`](../host/live_view.py#L56)

```python
def send_label(self, code: int) -> bool
```

**Purpose.** Forwards a label key press to the logger so the label is recorded in the session, not only displayed.

**How it works.** Sends the ASCII command `f"LABEL {code}"` (e.g. b"LABEL 3") to port+1 via UdpSender.send and returns True. True tells Window.keyPressEvent not to set the label locally: the logger parses the command (parse_label_command), calls Recorder.set_label, and then echoes the current label back as a TYPE_LABEL (0x10) frame, which the viewer's StreamDecoder.handle_frame applies to `dec.label`.

**Notes.** If no logger is listening, UdpSender swallows the OSError and the displayed label does not change, because the viewer waits for the echo.

#### `SerialSource`
*class* · [`host/live_view.py:61`](../host/live_view.py#L61)

```python
class SerialSource
```

**Purpose.** Byte source that reads the device's USB CDC port directly, for viewing without recording.

**How it works.** Fields: `ser` (pyserial Serial opened at 115200 baud with timeout=0, i.e. non-blocking reads), `name` ("serial &lt;port> (not recording)"). Implements poll() and send_label().

**Notes.** Nothing is written to disk in this mode, and labels are display-only. The port cannot be shared with logger.py at the same time.

#### `SerialSource.__init__`
*constructor* · [`host/live_view.py:62`](../host/live_view.py#L62)

```python
def __init__(self, port: str)
```

**Purpose.** Opens the serial port and asks the device for a session header so decoding can start mid-stream.

**How it works.**

1. `import serial` inside the constructor, so pyserial is needed only for this mode.
2. `serial.Serial(port, 115200, timeout=0)`.
3. Writes `P.CMD_SEND_HEADER` (b"H"). The firmware's serviceHost() sets g_headerPending on 'H' and queueHeaderIfPending() emits a HEADER frame.
4. Sets `name`.

**Notes.** The baud rate has no effect on USB CDC. The firmware also queues a header itself when streaming starts (startStreaming, when the host connects), so the 'H' is an extra request. The viewer sends it only once; logger.py repeats its request every 2 s while it has no header.

#### `SerialSource.poll`
*method* · [`host/live_view.py:69`](../host/live_view.py#L69)

```python
def poll(self) -> bytes
```

**Purpose.** Returns whatever bytes are buffered on the port without blocking.

**How it works.** `self.ser.read(self.ser.in_waiting or 0)`; with timeout=0 this returns immediately, b"" when nothing is waiting.

**Notes.** There is no error handling or reconnect logic (logger.py has both). If the device is unplugged or reset, `in_waiting` or `read` raises (a SerialException/OSError). tick() catches only LayoutChanged, so the exception escapes the QTimer slot on every tick. What happens next depends on the Qt binding: PySide prints it and keeps running, while PyQt may abort.

#### `SerialSource.send_label`
*method* · [`host/live_view.py:72`](../host/live_view.py#L72)

```python
def send_label(self, code: int) -> bool
```

**Purpose.** Signals that this source cannot record labels.

**How it works.** Returns False, so Window.keyPressEvent sets `dec.label` locally (display-only).

#### `PacedFrameSource`
*class* · [`host/live_view.py:76`](../host/live_view.py#L76)

```python
class PacedFrameSource
```

**Purpose.** Plays a pre-built list of encoded frames in real time (times a speed factor). Used by both the simulator and replay modes.

**How it works.** Fields: `frames` (list of complete encoded frames, bytes), `data_index` (per frame, the running count of DATA frames up to that point; used as the frame's due time in samples), `fs` (nominal sample rate, Hz), `speed` (playback factor), `pos` (index of the next frame to emit), `t0` (time.monotonic() at construction), `name` (display string). send_label always returns False.

**Notes.** The docstring says it uses "the DATA timestamps", but the code paces by DATA-frame count at the nominal `fs`, not by t_us. A gap of dropped frames in a recording is therefore not replayed as a pause. The plotted time axis is still correct, because StreamDecoder derives time from t_us.

#### `PacedFrameSource.__init__`
*constructor* · [`host/live_view.py:79`](../host/live_view.py#L79)

```python
def __init__(self, frames: list[bytes], data_index: list[int], fs: float, speed: float, name: str)
```

**Purpose.** Stores the frame list and pacing parameters and starts the playback clock.

**How it works.** Assigns the arguments to fields, sets `pos = 0` and `t0 = time.monotonic()`.

**Notes.** `t0` is taken when the source is built in main(), before the Qt app and window exist. The first poll() in tick() therefore returns a burst covering that start-up time (elapsed x speed x fs samples).

#### `PacedFrameSource.poll`
*method* · [`host/live_view.py:88`](../host/live_view.py#L88)

```python
def poll(self) -> bytes
```

**Purpose.** Emits every frame whose due time has passed.

**How it works.**

1. `due = (time.monotonic() - self.t0) * self.speed * self.fs`, the number of DATA frames that should have been played by now.
2. While `pos < len(frames)` and `data_index[pos] <= due`: append `frames[pos]` and increment `pos`.
3. Return the concatenation; b"" when nothing is due or playback has ended.

**Notes.** Non-DATA frames (header, status, label) carry the DATA count of their neighbourhood, so they go out together with the adjacent data. After the last frame, poll returns b"" forever. Playback does not loop.

#### `PacedFrameSource.send_label`
*method* · [`host/live_view.py:96`](../host/live_view.py#L96)

```python
def send_label(self, code: int) -> bool
```

**Purpose.** Signals that labels cannot be recorded in sim/replay modes.

**How it works.** Returns False; the key press sets `dec.label` locally (display-only).

#### `sim_source`
*function* · [`host/live_view.py:100`](../host/live_view.py#L100)

```python
def sim_source(abdomen: bool, speed: float) -> PacedFrameSource
```

**Purpose.** Builds a PacedFrameSource from a synthetic session, with the simulator's ground-truth labels injected as LABEL frames.

**How it works.**

1. Lazily imports kman_host.simulator and calls `sim.generate(sim.SimConfig(abdomen=abdomen))`. This builds the whole session in memory: chest IMU + flex, plus an abdomen IMU if requested, at 100 Hz by default.
2. Iterates `s.stream()`, which yields `(-1, header_frame)` first, then `(i, data_frame)` for every sample, plus `(i, status_frame)` once per second after data frame i.
3. For each item with `i >= 0` whose `s.labels[i]` differs from `last_label`, it appends `P.encode_frame(P.TYPE_LABEL, bytes([label]))` with index `n_data` (the DATA count before frame i). The label frame therefore goes out before data frame i, and StreamDecoder stamps the new label on exactly that sample.
4. Appends the frame itself. If `i >= 0` and `fr[4] == P.TYPE_DATA` (byte 4 is the frame type after 0xAA 0x55 len16), it increments `n_data`. It then appends `n_data` to `idx`, so data frame i gets index i+1.
5. Returns `PacedFrameSource(frames, idx, s.cfg.fs, speed, "simulator (labels = ground truth)")`.

**Notes.** The header frame gets index 0, so it is emitted on the first poll. A status frame yielded with the same i never triggers a second label frame, because the label equals `last_label`. A digit key in this mode overrides `dec.label` only until the next ground-truth LABEL frame.

#### `replay_source`
*function* · [`host/live_view.py:119`](../host/live_view.py#L119)

```python
def replay_source(path: str, speed: float) -> PacedFrameSource
```

**Purpose.** Builds a PacedFrameSource that replays a raw .bin capture (the verbatim device byte stream written by the logger).

**How it works.**

1. Reads the whole file and parses it in one call with a fresh `P.FrameParser().feed(...)`, which returns only CRC-valid frames.
2. `fs` starts at 100.0. Each HEADER frame overwrites it with `SessionHeader.from_json(f.body).sample_rate_hz`.
3. For each frame it increments `n_data` on TYPE_DATA, appends `f.raw` (the complete frame bytes) to `out`, and appends `n_data` to `idx`.
4. Returns `PacedFrameSource(out, idx, fs, speed, f"replay {name} x{speed:g}")`.

**Notes.** The rate of the last header in the file paces the whole file. Corrupted frames are dropped at load time, so the viewer's crc counter reads 0 in replay. The .bin file holds device bytes only; labels live in &lt;stem>.labels.jsonl, which this function does not read. Replay therefore shows label 0 unless digit keys are pressed (display-only).

#### `Ring`
*class* · [`host/live_view.py:138`](../host/live_view.py#L138)

```python
class Ring
```

**Purpose.** Fixed-size circular buffer of float rows that holds the last `n` samples of a signal for the scrolling plot.

**How it works.** Fields: `buf` (ndarray of shape (n, width), initialised to NaN), `n` (capacity in samples), `count` (total number of rows ever appended; `count % n` is the next write slot once the buffer has wrapped).

**Notes.** LiveProcessor creates every ring with n = int(window_s \* fs), i.e. 2000 rows at the defaults (20 s, 100 Hz). No tests in host/tests cover Ring.

#### `Ring.__init__`
*constructor* · [`host/live_view.py:139`](../host/live_view.py#L139)

```python
def __init__(self, n: int, width: int = 1)
```

**Purpose.** Allocates the buffer.

**How it works.** `buf = np.full((n, width), np.nan)`, `n = n`, `count = 0`.

#### `Ring.extend`
*method* · [`host/live_view.py:144`](../host/live_view.py#L144)

```python
def extend(self, x: np.ndarray) -> None
```

**Purpose.** Appends a batch of rows, overwriting the oldest ones.

**How it works.**

1. `x = np.asarray(x, float).reshape(len(x), -1)`, so a 1-D input becomes one column.
2. If `len(x) >= n`: `buf[:] = x[-n:]`, `count += len(x)`, return.
3. Otherwise: `start = count % n`; `first = min(len(x), n - start)`; write `x[:first]` to `buf[start:start+first]`; write the remainder `x[first:]` to `buf[:len(x)-first]` (the wrap-around); `count += len(x)`.

**Notes.** Bug in the `len(x) >= n` branch: it stores the newest n rows with the oldest at index 0, but ordered() and later writes assume the oldest row sits at `count % n`. That only holds when the new `count` is a multiple of n. Verified with a copy of the class: Ring(5), extend(0..2), then extend(3..8) gives ordered() = [8,4,5,6,7] instead of [4..8]. A further extend([9]) gives [4,5,6,7,9], so sample 8 is lost. The scramble clears after n more rows are written in small batches. In the viewer, a single drain of 2000 or more samples is enough to trigger it. That happens with --sim/--replay at a high --speed (the first poll returns the start-up burst), or after an event-loop stall of 20 s or more. All rings get the same batch length, so they are scrambled consistently, but x = t - t[-1] in tick() then uses a sample that is not the newest.

#### `Ring.ordered`
*method* · [`host/live_view.py:156`](../host/live_view.py#L156)

```python
def ordered(self) -> np.ndarray
```

**Purpose.** Returns the stored rows oldest-first for plotting.

**How it works.** If `count < n`, returns the view `buf[:count]` (only filled rows, so no NaN). Otherwise `s = count % n` and it returns `np.concatenate([buf[s:], buf[:s]])`, a new array of n rows.

#### `LiveProcessor`
*class* · [`host/live_view.py:163`](../host/live_view.py#L163)

```python
class LiveProcessor
```

**Purpose.** Causal, incremental signal processing for the display. It turns each decoded sample batch into respiration, motion-energy, flex and label rings, and computes the PC1 projection on demand.

**How it works.** Fields: `h` (the SessionHeader), `fs` (header sample rate, Hz), `groups` (dict key -> 3 accel column names: "chest" -> [chest_ax, chest_ay, chest_az] and "abd" -> [abd_ax, abd_ay, abd_az], each present only if all 3 channels exist), `has_flex` ("flex_raw" in channel names), `scale` (channel -> physical units per LSB from the header, g/LSB for accel), `bit` (channel -> valid-mask bit of its sensor slot), `sos_resp` (Butterworth band-pass, butter order 2 => 4th order, [hp_hz, 1.0] Hz, SOS), `sos_motion` (2nd-order Butterworth high-pass at 1.5 Hz, SOS), `zi` (dict of filter states keyed "chest_resp", "abd_resp", "chest_motion", "motion", "flex_base"), `last_valid` (dict key -> last good sample, for the invalid-sample hold), rings `t` (seconds), `resp` (dict key -> Ring(n, 3) of band-passed accel in g), `flex`, `flex_detr`, `motion` (mg rms), `label`, and `pc` (dict key -> last PC1 unit vector, for sign continuity).

**Notes.** Gyro channels and the neck IMU are ignored. The filter state persists across batches, so the output does not depend on how the stream was split into batches. Only the first-sample initialisation depends on the first batch.

#### `LiveProcessor.__init__`
*constructor* · [`host/live_view.py:164`](../host/live_view.py#L164)

```python
def __init__(self, header: P.SessionHeader, window_s: float, hp_hz: float)
```

**Purpose.** Configures channel groups, filters and buffers from the session header.

**How it works.**

1. `fs = header.sample_rate_hz`; `n = int(window_s * fs)`.
2. Builds `scale` and `bit` dicts from `header.channels`.
3. For ("chest", "chest_a") and ("abd", "abd_a"), it forms the names prefix+x/y/z. If all three are in `header.channel_names`, it adds the group.
4. `has_flex = "flex_raw" in names`.
5. `sos_resp = signal.butter(2, [hp_hz, 1.0], btype="bandpass", fs=fs, output="sos")` (2 biquads); `sos_motion = signal.butter(2, 1.5, btype="highpass", fs=fs, output="sos")` (1 biquad).
6. Empty `zi`, `last_valid`, `pc` dicts. Rings: `t`, `flex`, `flex_detr`, `motion`, `label` of size n, and `resp[key] = Ring(n, 3)` per group.

**Notes.** hp_hz comes from --hp (default 0.05 Hz). The 1.0 Hz upper corner is fixed. The `motion` ring is created even without a chest IMU but is only filled when "chest" is a group.

#### `LiveProcessor._held`
*method* · [`host/live_view.py:191`](../host/live_view.py#L191)

```python
def _held(self, key: str, x: np.ndarray, ok: np.ndarray) -> np.ndarray
```

**Purpose.** Invalid-sample hold. Samples from frames whose valid bit for this sensor is clear are replaced with the last good sample before they enter the IIR filters. The docstring gives the reason: to keep the IIR state finite.

**How it works.**

1. `last = last_valid.get(key, np.zeros(x.shape[1:]))`, the last good row from the previous batch, or zeros if none has been seen yet.
2. If every sample is valid (`ok.all()`), x is neither copied nor modified.
3. Otherwise it copies x and walks i = 0..len-1: if `ok[i]`, `last = x[i]`; else `x[i] = last`. This is sample-and-hold of the most recent valid row, carried across batch boundaries.
4. In both cases (there is no early return) it then stores `last_valid[key] = x[-1] if ok[-1] else last`, so an all-valid batch also moves the hold value to its own last row.
5. Returns x.

**Notes.** If a sensor is invalid from the first sample, the held value is 0 (0 g for accel, 0 counts for flex). When valid data then arrives, it is a step input to the filters and produces a transient. Batches are never empty (StreamDecoder.drain returns None instead), so `ok[-1]` is safe.

#### `LiveProcessor._ema`
*method* · [`host/live_view.py:204`](../host/live_view.py#L204)

```python
def _ema(self, key: str, x: np.ndarray, tau_s: float) -> np.ndarray
```

**Purpose.** Causal first-order exponential moving average with time constant tau_s. Its state persists across batches under `zi[key]`.

**How it works.**

1. `a = 1 - exp(-1 / (tau_s * fs))`, the discrete equivalent of an RC low-pass with time constant tau_s. At 100 Hz: tau 0.5 s gives a ~= 0.0198; tau 10 s gives a ~= 0.0009995.
2. On first use: `zi[key] = [(1 - a) * x[0]]`. For lfilter's transposed direct form II with b=[a], a=[1, -(1-a)], that state equals y[-1] = x[0]. The first output is a\*x[0] + (1-a)\*x[0] = x[0], so the EMA starts settled at the first input instead of ramping up from 0.
3. `y, zi[key] = signal.lfilter([a], [1.0, -(1.0 - a)], x, zi=zi[key])`, which implements y[k] = a\*x[k] + (1-a)\*y[k-1].
4. Returns y.

**Notes.** Used with key "motion" (tau 0.5 s, on the summed squared high-passed accel) and key "flex_base" (tau 10 s, flex baseline). x must be 1-D.

#### `LiveProcessor._filt`
*method* · [`host/live_view.py:211`](../host/live_view.py#L211)

```python
def _filt(self, key: str, sos: np.ndarray, x: np.ndarray) -> np.ndarray
```

**Purpose.** Causal SOS filtering of an (N, 3) batch along time. The state persists per key, and the filter is initialised in steady state for the first sample so gravity does not cause a start-up transient.

**How it works.**

1. On first use for `key`: `zi = signal.sosfilt_zi(sos)`, shape (n_sections, 2). This is the internal state of each biquad for a unit-step input held forever. It is scaled per axis by the first sample: `zi[:, :, None] * x[0][None, None, :]`, shape (n_sections, 2, 3), which is what sosfilt expects for axis=0 on (N, 3) data. The filter then behaves as if the input had been constant at x[0] (about 1 g of gravity on some axis) since minus infinity. For the band-pass and high-pass filters that means the output starts at about 0, instead of ringing from a 1 g step that would take tens of seconds to decay through the 0.05 Hz corner.
2. `y, zi[key] = signal.sosfilt(sos, x, axis=0, zi=zi[key])`, a forward-only (causal) filter. The final state is stored for the next batch.
3. Returns y with the same shape as x.

**Notes.** It uses sosfilt, not sosfiltfilt, so there is no look-ahead and each output sample depends only on current and past input. Keys used: "chest_resp", "abd_resp" (sos_resp) and "chest_motion" (sos_motion).

#### `LiveProcessor.process`
*method* · [`host/live_view.py:218`](../host/live_view.py#L218)

```python
def process(self, b: dict[str, np.ndarray]) -> None
```

**Purpose.** Consumes one batch from StreamDecoder.drain() and appends the derived signals to the rings.

**How it works.**

Input `b` has keys t_us (int64 unwrapped microseconds), valid (uint8 slot bitmask), one int16 column per channel, label (int8), plus seq/segment/host_time (unused here).
1. `t.extend(b["t_us"] / 1e6)` (seconds); `label.extend(b["label"])`.
2. For each accel group (key, cols):
   a. `ok = (valid & (1 << bit[cols[0]])) != 0`, the sensor slot's valid bit (chest bit 0, abdomen bit 1 in the header).
   b. `g = np.stack([b[c] * scale[c] for c in cols], axis=1)`: (N, 3) acceleration in g (the scale is 61e-6 g/LSB at +-2 g in the simulator header).
   c. `g = _held(key, g, ok)`.
   d. `resp[key].extend(_filt(key + "_resp", sos_resp, g))`: the 0.05-1 Hz band-passed acceleration per axis. This is the low-frequency tilt change of the gravity vector with the DC part removed.
   e. For key "chest" only: `hp = _filt("chest_motion", sos_motion, g)` (>1.5 Hz); `e = sum(hp**2, axis=1)` (instantaneous squared magnitude, g^2); `motion.extend(sqrt(max(_ema("motion", e, 0.5), 0)) * 1000)`, the RMS over a ~0.5 s exponential window in mg.
3. If `has_flex`: `ok` from the flex_raw bit; `f = _held("flex", flex_raw as float (N, 1), ok)[:, 0]`; `flex.extend(f)` (raw ADC counts); `flex_detr.extend(f - _ema("flex_base", f, 10.0))` (deviation from a 10 s EMA baseline).

**Notes.** `np.maximum(..., 0)` only guards against rounding. All rings get exactly len(batch) rows, so they stay index-aligned with `t`.

#### `LiveProcessor.resp_pc1_deg`
*method* · [`host/live_view.py:237`](../host/live_view.py#L237)

```python
def resp_pc1_deg(self, key: str) -> np.ndarray
```

**Purpose.** Projects the visible window of 3-axis band-passed accel onto its first principal component, keeping the sign stable, and returns it as degrees of tilt. This reduces respiration to one trace whatever the sensor orientation.

**How it works.**

1. `x = resp[key].ordered()` (N, 3). If N &lt; 10, it returns `np.zeros(N)`.
2. `xc = x - x.mean(axis=0)`.
3. `w, v = np.linalg.eigh(np.cov(xc.T))`, the eigen-decomposition of the 3x3 covariance. eigh returns eigenvalues in ascending order, so `pc = v[:, -1]` is the unit direction of largest variance.
4. Sign stabilisation: eigenvectors have an arbitrary sign, so a fresh eigh could flip the trace on any redraw. If a previous vector exists and `pc @ prev < 0`, then `pc = -pc`. The result is stored in `self.pc[key]`.
5. Returns `np.degrees(xc @ pc)`. The projection is in g, and for small tilt angles the change in a gravity component is about g\*theta, so the g-fraction approximates radians (code comment: "small-angle: g-fraction ~ radians").

**Notes.** PC1 is recomputed over the whole visible window (window_s) on every redraw, so the plotted history can change shape slightly as the window slides. Only the filtering is causal, not the projection. Sign continuity is relative to the vector stored by the previous call. In PC1 mode that is normally the redraw about 33 ms earlier, but tick() does not call this method while paused or in axes mode, so after either of those the reference may be much older. If PC1 swings by more than 90 degrees between calls, or two eigenvalues are nearly equal, the trace can still flip or jump.

#### `LiveProcessor.resp_axes_deg`
*method* · [`host/live_view.py:250`](../host/live_view.py#L250)

```python
def resp_axes_deg(self, key: str) -> np.ndarray
```

**Purpose.** Per-axis alternative to PC1, shown when the 'a' key toggles axes mode.

**How it works.** `x = resp[key].ordered()`. It returns `np.degrees(x - x.mean(axis=0))` (N, 3), or the empty x when N == 0. This uses the same small-angle g-to-radian approximation per axis.

#### `build_label_lut`
*function* · [`host/live_view.py:258`](../host/live_view.py#L258)

```python
def build_label_lut() -> np.ndarray
```

**Purpose.** Builds the 256-entry RGBA lookup table that turns label codes into colours in the label strip image.

**How it works.**

1. `lut = np.zeros((256, 4), np.ubyte)` with every row set to (0x38, 0x38, 0x35, 255), a neutral dark grey for label 0 and unknown codes.
2. For each `k, hexcol` in `LABEL_COLORS_DARK` (labels 1-9), it parses "#rrggbb" into (r, g, b, 255) at row k.
3. Returns lut.

**Notes.** Label 9 uses the grey #8a8984 from the palette. HATCHED_LABELS (hatching for label 9) is not used here.

#### `main`
*function* · [`host/live_view.py:266`](../host/live_view.py#L266)

```python
def main(argv=None) -> int
```

**Purpose.** Entry point: parses arguments, picks the source, builds the Qt window, and runs the 33 ms refresh timer until the window closes.

**How it works.**

1. argparse. A mutually exclusive source group: `--udp [PORT]` (nargs='?', const=DEFAULT_PORT), `--port`, `--sim`, `--replay PATH`. Other options: `--abdomen`, `--speed` (1.0), `--window` (20.0 s), `--hp` (0.05 Hz), `--exit-after` (0 = off), `--screenshot PATH`.
2. Imports pyqtgraph and its Qt wrappers only after parsing, so --help works without Qt.
3. Source selection in priority order: --port -> SerialSource; --sim -> sim_source(abdomen, speed); --replay -> replay_source(path, speed); otherwise UdpSource(args.udp or DEFAULT_PORT).
4. `pg.setConfigOptions(antialias=True, background=SURFACE, foreground=TEXT_2)` and `pg.mkQApp("KMAN live view")`.
5. Shared state: `state = {paused: False, axes: False, detrend: True, proc: None, built: False}` and `dec = StreamDecoder()`.
6. Defines the nested Window class and creates the layout. The top row holds `label_lbl` (20 pt bold, initially "waiting for session header...") and a right-aligned `stats_lbl`. Below it, a `pg.GraphicsLayoutWidget` (glw, stretch 1) and a 10 px help line listing the keys and LABELS. Size 1280x860, then show().
7. `plots`/`curves` dicts, plus `label_img = pg.ImageItem()` with the LUT from build_label_lut().
8. Defines the nested make_plot, build_ui and tick functions, and `rate_hist`.
9. `QtCore.QTimer` connected to tick and started at 33 ms (~30 Hz).
10. If --exit-after is set, `QTimer.singleShot(exit_after*1000, finish)`.
11. `app.exec()`, then return 0. The module calls `sys.exit(main())` under `__main__`.

**Notes.** `state["built"]` is never read. `--udp 0` falls back to DEFAULT_PORT because 0 is falsy. `--screenshot` does nothing on its own: the PNG is saved only by finish(), which exists only when `--exit-after` is non-zero. Quitting with q/Esc or closing the window does not save it. The source is created before the QApplication, which matters for PacedFrameSource's start-up burst. Everything runs on the Qt main thread: there are no worker threads, and all sources are non-blocking.

#### `Window`
*nested class* · [`host/live_view.py:299`](../host/live_view.py#L299)

```python
class Window(QtWidgets.QWidget)  (inside main)
```

**Purpose.** Top-level QWidget subclass that exists to receive keyboard shortcuts.

**How it works.** It has no fields of its own. It closes over `source`, `dec` and `state` from main() and overrides only keyPressEvent. Its layout (labels, plot area, help line) is attached in main().

#### `Window.keyPressEvent`
*method* · [`host/live_view.py:300`](../host/live_view.py#L300)

```python
def keyPressEvent(self, ev)
```

**Purpose.** Handles the viewer's keys: label digits, pause, PC1/axes toggle, flex detrend toggle, quit.

**How it works.**

`k = ev.text()`.
- Digit 0-9: `code = int(k)`. If `source.send_label(code)` returns False (serial, sim, replay), it sets `dec.label = code` directly. The decoder then stamps that label on subsequent rows, so it appears in the label strip, but it is not recorded anywhere. With UdpSource (returns True), the label changes only when the logger's LABEL frame echo arrives.
- " ": toggles `state["paused"]`.
- "a": toggles `state["axes"]`.
- "d": toggles `state["detrend"]`.
- "q", "\x1b", or `ev.key() == Key_Escape`: `self.close()`, which ends app.exec() because it is the only window.

**Notes.** The keys are case-sensitive: "A", "D" and "Q" do nothing. Pausing stops only redrawing; tick() keeps polling and processing, so the view jumps to the present when unpaused.

#### `make_plot`
*nested function* · [`host/live_view.py:345`](../host/live_view.py#L345)

```python
def make_plot(row: int, title: str, units: str, height: int | None = None)  (inside main)
```

**Purpose.** Creates one uniformly styled strip-chart row in the GraphicsLayoutWidget.

**How it works.**

1. `p = glw.addPlot(row=row, col=0)`.
2. If a title is given: `setTitle(title, color=TEXT, size="11pt")`.
3. Left label = units. Auto SI prefix disabled. Left axis width fixed at 60 px so all rows align.
4. Grid on both axes at alpha GRID_ALPHA (0.15).
5. `setXRange(-args.window, 0, padding=0)`, so x is seconds before now.
6. Y auto-range with `setAutoVisible(y=True)`, so the Y range follows only the data inside the visible X range.
7. Mouse: x pan/zoom disabled, y enabled.
8. `setMaximumHeight(height)` if given.
9. Returns the PlotItem.

#### `build_ui`
*nested function* · [`host/live_view.py:361`](../host/live_view.py#L361)

```python
def build_ui(proc: LiveProcessor) -> None  (inside main)
```

**Purpose.** Lays out the plot rows once the session header (and therefore the channel set) is known.

**How it works.**

Rows are added top to bottom, `row` incrementing:
1. For "chest" and "abd" if present in proc.groups: a plot titled "&lt;Chest|Abdomen> respiration  (0.05-1 Hz tilt)" in "deg". `curves[key]` is a list of 4 curves: index 0 is the PC1 curve (SERIES colour, width 2); indices 1-3 are the x/y/z axis curves (#3987e5, #d95926, #199e70, width 1).
2. If has_flex: "Neck flex" in "counts", one curve (SERIES["flex"], width 2).
3. If chest present: "Chest motion energy  (>1.5 Hz, rms)" in "mg", max height 150, one curve.
4. Label strip: untitled, max height 80, no left ticks (`setTicks([[]])`), mouse disabled, holds `label_img`, Y fixed to 0..1, bottom axis label "seconds".
5. Every other plot is X-linked to the label plot.

**Notes.** The respiration title always says 0.05-1 Hz, even when --hp changes the lower corner.

#### `tick`
*nested function* · [`host/live_view.py:394`](../host/live_view.py#L394)

```python
def tick() -> None  (inside main)
```

**Purpose.** The 33 ms refresh path. On every QTimer timeout it polls the source, decodes, processes and redraws, all on the GUI thread.

**How it works.**

1. Input: `data = source.poll()`. If non-empty, `dec.feed(data)` runs FrameParser, then StreamDecoder.handle_frame per frame: DATA rows are queued with unwrapped t_us/seq and the current label, HEADER sets dec.header, STATUS sets dec.status, LABEL sets dec.label. If StreamDecoder raises `LayoutChanged` (a header with a different channel layout), it shows "layout changed: ..." in label_lbl and returns.
2. Lazy setup: on the first tick where `dec.header` exists and `state["proc"]` is None, it creates `LiveProcessor(dec.header, args.window, args.hp)` and calls `build_ui`.
3. `b = dec.drain()` takes all rows decoded since the last drain as columns, or None. If there is no processor yet, it returns. If b is not None, `proc.process(b)` filters the batch into the rings.
4. Rate: appends `(monotonic now, dec.stats.frames_data)` to `rate_hist`, drops entries older than 3 s, and computes `rate = (frames_last - frames_first) / max(1e-6, t_last - t_first)` in Hz.
5. Header text: a colored square (&#9632;) in LABEL_COLORS_DARK[label] (TEXT_2 for label 0), then the label number and `label_name`, plus "[PAUSED]" when paused.
6. Stats text (HTML): source name, rate, `frames_data`, lost = `dec.stats.dropped` with `drop_pct()`, CRC errors from `dec.parser.stats.crc_errors`, plus device `overruns` and `tx_drops` from the last STATUS frame if one has arrived.
7. Returns here if paused or `proc.t.count == 0`. Text keeps updating while paused.
8. `t = proc.t.ordered()[:, 0]`; `x = t - t[-1]`, so the newest sample is at 0 s and older ones are negative.
9. Per respiration group: in axes mode, curves 1-3 get `resp_axes_deg` columns and curve 0 is cleared. Otherwise curve 0 gets `resp_pc1_deg(key)` and curves 1-3 are cleared.
10. Flex: plots `flex_detr` when `state["detrend"]`, otherwise raw `flex`, and retitles to "Neck flex (minus 10 s baseline)" or "(raw)".
11. Motion curve from `proc.motion.ordered()`.
12. Label strip: `labs = proc.label.ordered()[:, 0].astype(uint8)`. `label_img.setImage(labs[:, None], autoLevels=False, levels=(0, 255))` makes an N x 1 image where value k picks LUT row k. `setRect(QRectF(x[0], 0, (x[-1]-x[0]) or 1e-3, 1))` stretches it across the visible time span.

**Notes.** The label image spreads its N pixels evenly between x[0] and x[-1], while the curves use the actual timestamps. Across a time gap the strip is therefore slightly misaligned with the curves. LayoutChanged handling is minimal. StreamDecoder raises it before replacing its header, so dec.header keeps the old layout. The rest of the frames from that feed() call are never handled. state["proc"] is not reset and the plots are never rebuilt. The 'layout changed: ...' text stays only until the next tick that does not raise, because that tick rewrites label_lbl with the label text. In practice it shows for one tick, and with the logger it comes back for one tick each time the header is re-announced (every 2 s). Later DATA frames are decoded with the old dtype: if their size differs they are counted as bad_size, no rows are produced, the curves freeze and the rate falls to 0. The viewer has to be restarted for a new layout. Only LayoutChanged is caught; any other exception from source.poll() or dec.feed() (for example a serial disconnect) escapes the timer slot. Per-tick cost is a few array copies of up to window\*fs rows and one 3x3 eigh per respiration group (PC1 mode).

#### `finish`
*nested function* · [`host/live_view.py:457`](../host/live_view.py#L457)

```python
def finish()  (inside main, defined only when --exit-after is set)
```

**Purpose.** Timed shutdown for automated testing and screenshots.

**How it works.** If `--screenshot` is set, it saves `win.grab()` (a QPixmap of the window) to that path. Then `win.close()`, which ends app.exec(). It is scheduled with `QTimer.singleShot(int(args.exit_after * 1000), finish)`.

## 15. Analysis: config, preprocessing, breathing

- **`analysis/kman_analysis/__init__.py`**: Marks `kman_analysis` as a package. The docstring calls it the "KMAN Step 1 offline analysis: breathing, chest-abdomen phase, neck events". It re-exports `load_config` (from config.py) and `Analysis` / `analyze` (from pipeline.py) through `__all__`, so report.py and the tests can write `from kman_analysis import analyze, load_config`. It defines no functions of its own.
- **`analysis/kman_analysis/config.py`**: Builds the configuration dict that every analysis stage receives as `cfg`. It always loads analysis/config.yaml as the defaults. It can then deep-merge a user YAML file and an in-code overrides dict on top, and it raises an error on any key that is not in the defaults. Nothing is validated except key names.
- **`analysis/config.yaml`**: Holds every threshold and parameter the offline analysis uses (its header says "Every threshold the analysis uses lives here"). Units are part of the key names. It has seven top-level sections, read by preprocess.py, breathing.py, pipeline.py, phase.py, swallow.py and plotting.py/report.py. The header says the defaults were tuned on the simulator and should be re-tuned on real recordings.
- **`analysis/kman_analysis/preprocess.py`**: This is the first stage of the analysis. It turns a logged `Session` into a `Uniform`: every channel on the complete tick grid (one slot per `seq` value), in physical units. Lost frames, and samples whose sensor slot was flagged invalid, are filled by linear interpolation; edges take the nearest value. Holes of up to `max_interp_gap_s` are only interpolated. Longer holes are also flagged in `gap`, which later stages use as a quality mask. Breaths inside it are marked invalid, phase treats it as invalid, and swallow events touching it are dropped. The module also picks the per-session calibration (quiet-reference) samples, and provides a helper that finds runs of True in a boolean mask.
- **`analysis/kman_analysis/breathing.py`**: Turns one IMU's accelerometer and gyro (chest or abdomen) into a respiratory waveform in degrees of tilt, then detects breaths. The steps are: complementary-filter estimate of the gravity direction, decimation to `resp_fs_hz`, zero-phase band-pass, windowed PCA down to one dimension, sign flip so inspiration rises, adaptive-prominence peak detection, breath rate, and seconds since the last valid breath. It also computes chest high-pass acceleration RMS in mg, which pipeline.analyze thresholds into the motion-artifact mask.

**How it connects.** The one entry point is pipeline.analyze(session, cfg). report.py main() calls load_config(args.config), then kman_host.session_io.load_session, then analyze. The tests call load_config() and load_config(overrides=...).\n\nanalyze runs these steps in order:\n(1) to_uniform(session, cfg).\n(2) calibration_mask(u, cfg).\n(3) If u.vec("chest","a") exists, motion_energy_mg(acc, u.fs, cfg), thresholded at cfg["motion"]["threshold_mg"] to give the motion mask. Otherwise motion_mg is None and motion is all False.\n(4) analyze_breathing(u, "chest", ...) if the chest accelerometer exists, and analyze_breathing(u, "abd", ...) if the abdomen accelerometer exists. Both receive the same chest-derived motion mask.\n(5) phase.chest_abdomen_phase, only when both are present.\n(6) swallow.analyze_neck.\n(7) per_label_summary.\n(8) quality.\n\nUpstream, to_uniform depends on the host Session from kman_host/session_io.py. It reads:\n- df["seq"], df["label"], df["valid"] and the raw channel columns\n- session.fs (header.sample_rate_hz)\n- header.channels (protocol.Channel: name, bit, scale)\n- has_slot and label_names\nUniform.vec relies on the firmware/simulator channel names: slot prefix plus "ax"/"gx" and so on, for example chest_ax, abd_gx, neck_az.\n\nDownstream, other modules use the results:\n(a) pipeline passes the chest t, chest and abdomen wave_deg (cut to their common length), chest.fs, and the calibration and motion|gap masks decimated with [::q] to phase.chest_abdomen_phase.\n(b) per_label_summary reads breath_t, breath_valid, breath_amp_deg, rate_t and rate_bpm, and calls _max_breath_gap, which uses runs.\n(c) quality reads chest.sign_flipped, u.gap, calib and motion.\n(d) swallow.analyze_neck uses u.ch["flex_raw"], u.gap, u.label, calib and runs. swallow.head_pose_change uses u.vec("neck", "a"/"g").\n(e) plotting.make_figure plots these results; its helpers _bands and _artifact_spans use runs.\n(f) report.py writes breath_t, breath_amp_deg and breath_valid to &lt;session>_breaths.csv.\n\nEvery stage reads its thresholds from the cfg dict built by load_config.

### `analysis/config.yaml`

#### `preprocess (config section)`
*config* · [`analysis/config.yaml:6`](../analysis/config.yaml#L6)

```
preprocess: {max_interp_gap_s: 0.5}
```

**Purpose.** Sets how lost or invalid samples are treated when building the uniform grid.

**How it works.**

- `max_interp_gap_s` (0.5 s): preprocess.to_uniform turns this into `max_gap = int(round(0.5 * fs))` samples, which is 50 at the default 100 Hz. Every hole is linearly interpolated whatever its length, so the filters stay stable; leading and trailing holes take the nearest valid value. A run of missing samples longer than `max_gap` also sets `Uniform.gap` to True. The run can be lost frames or one channel flagged invalid. Shorter holes are interpolated and not masked. Each stage uses `gap` differently. Breathing still detects peaks inside it, but marks them invalid (`breath_valid = False`) and leaves them out of rate, since-last and the per-label counts. Phase drops gap samples from the windowed circular mean, and needs at least half of each window clean to call a sample valid. Swallow drops any event that touches `gap`.

#### `calibration (config section)`
*config* · [`analysis/config.yaml:11`](../analysis/config.yaml#L11)

```
calibration: {label: 1, fallback_first_s: 30, max_s: 120}
```

**Purpose.** Chooses the per-session quiet reference segment that relative thresholds and bias estimates are computed from.

**How it works.**

All three keys are read by preprocess.calibration_mask.
- `label` (1 = rest breathing): selects samples where `u.label == 1` and not `gap`.
- `fallback_first_s` (30): if that gives fewer than 5 s of samples, uses `t < 30 s` (not gap) instead.
- `max_s` (120): keeps at most the first 120 s worth of selected samples.
The resulting mask is used by breathing.respiratory_waveform (gyro bias), breathing.orient_inspiration_up (skewness), phase.chest_abdomen_phase (abdomen polarity and envelope medians), swallow.analyze_neck (`sigma_cal`), and pipeline.quality (`calibration_s`).

#### `breathing (config section)`
*config* · [`analysis/config.yaml:16`](../analysis/config.yaml#L16)

```
breathing: {gyro_bias_s, comp_filter_tau_s, resp_fs_hz, band_hz, filter_order, pca_window_s, peaks: {min_interval_s, rel_prominence, abs_min_prominence_deg, ref_window_s, ref_quantile}, max_rate_interval_s}
```

**Purpose.** Parameters for respiratory waveform extraction and breath detection.

**How it works.**

- `gyro_bias_s` (10): respiratory_waveform takes the gyro bias as the median over the first `10*fs` calibration samples.
- `comp_filter_tau_s` (1.0): the `tau_s` of gravity_complementary. `alpha = tau/(tau+dt)`, and the crossover is about 1/(2\*pi\*tau), roughly 0.16 Hz.
- `resp_fs_hz` (20): decimation factor `q = max(1, round(fs/20))`, which is 5 at 100 Hz.
- `band_hz` ([0.05, 1.0]): band-pass corners in Hz. The low corner also sets the edge pad, `padlen = 3*fs/0.05` samples.
- `filter_order` (2): Butterworth `N` passed to `signal.butter`. The YAML comment says filtfilt doubles the effective order.
- `pca_window_s` (30): windowed_pca window `L = 30*20 = 600` samples, hop 300.
- `peaks.min_interval_s` (1.5): `distance` for `find_peaks` (30 samples at 20 Hz), so at most 40 breaths/min.
- `peaks.rel_prominence` (0.3): minimum prominence as a fraction of the reference peak-to-peak amplitude.
- `peaks.abs_min_prominence_deg` (0.08): absolute floor on prominence (the noise/cardiac floor).
- `peaks.ref_window_s` (60): length of the rolling-quantile window for the reference amplitude.
- `peaks.ref_quantile` (0.8): quantile of |waveform| used as the reference.
- `max_rate_interval_s` (15): intervals between consecutive valid breaths longer than 15 s produce no rate value.

#### `motion (config section)`
*config* · [`analysis/config.yaml:31`](../analysis/config.yaml#L31)

```
motion: {highpass_hz: 1.5, window_s: 2.0, threshold_mg: 15.0}
```

**Purpose.** Defines the chest motion-artifact detector.

**How it works.**

- `highpass_hz` (1.5): corner of the 4th-order Butterworth high-pass in breathing.motion_energy_mg, applied zero-phase with sosfiltfilt. The YAML comment cites the spec: energy above 1.5 Hz.
- `window_s` (2.0): length of the centred rolling mean of the summed squared high-pass acceleration (200 samples at 100 Hz, with `min_periods=1`).
- `threshold_mg` (15.0): if a chest accelerometer exists, pipeline.analyze sets `motion = motion_mg > 15.0`. Without one, `motion_mg` is None and `motion` is all False. plotting.make_figure also draws the threshold as a dashed line.
The motion mask (OR'd with gap where relevant) does several things. It marks breaths invalid. It leaves those samples out of the windowed_pca covariance (weight 0) when at least 30 % of the window is clean. It is left out of the detect_breaths reference amplitude. It counts as invalid for phase. It also feeds the per-label summary (`artifact_pct`, clean minutes, `_max_breath_gap`), quality's `artifact_pct`, and the hatched artifact spans in the plot.

#### `phase (config section)`
*config* · [`analysis/config.yaml:36`](../analysis/config.yaml#L36)

```
phase: {window_s: 10, min_amplitude_frac: 0.25, min_coherence: 0.5, paradox_deg: 120}
```

**Purpose.** Chest-abdomen phase thresholds. Used only when an abdomen IMU is present.

**How it works.**

All four keys are read by phase.chest_abdomen_phase. The abdomen waveform may first be negated, if its calibration correlation with the chest is negative.
- `window_s` (10): window length, `win = max(3, int(10*fs))`, which is 200 samples at 20 Hz. It is a centred rolling window used three ways: for the circular mean of `exp(j*(angle(hilbert(chest)) - angle(hilbert(abd))))` over clean samples (not motion, not gap); for the count `cnt` of clean samples; and to smooth the amplitude check.
- `min_amplitude_frac` (0.25): both Hilbert envelopes must exceed 0.25 times their median over calibration, or over the whole record if calibration has 10 or fewer samples. They must do so in more than half of the window.
- `min_coherence` (0.5): the resultant length `|z|` of the windowed mean phasor must be >= 0.5.
A sample is valid only if it passes the amplitude check, passes the coherence check, and has `cnt >= 0.5*win` (at least half the window clean).
- `paradox_deg` (120): a valid sample with `|phase| > 120 deg` is flagged paradoxical. plotting.make_figure also draws this threshold.

#### `swallow (config section)`
*config* · [`analysis/config.yaml:42`](../analysis/config.yaml#L42)

```
swallow: {baseline_window_s, polarity, k_on, k_off, sigma_floor_counts, min_duration_s, max_duration_s, merge_gap_s, head_amplitude_k, rate_window_s, head_imu: {tilt_change_deg, yaw_change_deg, margin_s}}
```

**Purpose.** Detects and classifies neck flex-sensor events (swallow vs head motion).

**How it works.**

Read by swallow.analyze_neck unless noted.
- `baseline_window_s` (10): centred rolling-median window (forced odd) on `flex_raw`. `dev = x - baseline`.
- `polarity` (abs | positive | negative): which deviation sign counts. `mag` is `|dev|`, `dev` or `-dev`.
- `k_on` (5.0): an event needs at least one sample with `mag > k_on*sigma`.
- `k_off` (2.0): the event extends over the whole contiguous run where `mag > k_off*sigma` (hysteresis).
- `sigma_floor_counts` (2.0): `sigma = max(2.0, 1.4826*MAD(dev over calibration))`.
- `min_duration_s` (0.3): shorter events are dropped. Events touching `gap` are also dropped.
- `max_duration_s` (2.5): longer events become `head_motion` (reason "duration").
- `merge_gap_s` (0.2): candidates whose start is at most 0.2 s after the previous end are merged.
- `head_amplitude_k` (25.0): `peak mag/sigma > 25` makes the event `head_motion` (reason "amplitude").
- `rate_window_s` (10): window of the centred rolling event count (`swallow_rate_10s`, `head_rate_10s`). pipeline.per_label_summary uses it for the per-10-s rates, and plotting uses it too.
- `head_imu.tilt_change_deg` (8.0): read by analyze_neck; swallow.head_pose_change computes the value compared against it. It is the largest angle between the low-passed neck gravity direction and its mean over the first 0.2 s of the examined span.
- `head_imu.yaw_change_deg` (10.0): compared against the largest |integral| of the neck gyro rate about the gravity axis (median removed).
- `head_imu.margin_s` (0.5): the head IMU is examined over `[start - 0.5 s, end + 0.5 s]`.
If either head-IMU limit is exceeded, the event becomes `head_motion` with a "neck imu" reason.

#### `report (config section)`
*config* · [`analysis/config.yaml:58`](../analysis/config.yaml#L58)

```
report: {width_in: 18, panel_height_in: 1.9, dpi: 110, band_alpha: 0.16}
```

**Purpose.** Size and styling of the report figure.

**How it works.**

- `width_in` (18): figure width in inches (plotting.make_figure).
- `panel_height_in` (1.9): height per unit of panel weight. Figure height = `panel_height_in * sum(heights) + 3.2` in.
- `dpi` (110): PNG resolution in report.py `fig.savefig(..., dpi=...)`.
- `band_alpha` (0.16): opacity of the label background bands drawn by plotting._bands.

### `analysis/kman_analysis/__init__.py`

#### `kman_analysis (package exports)`
*module* · [`analysis/kman_analysis/__init__.py:3`](../analysis/kman_analysis/__init__.py#L3)

```python
from .config import load_config; from .pipeline import Analysis, analyze; __all__ = ["Analysis", "analyze", "load_config"]
```

**Purpose.** Gives callers one import point for the analysis entry points.

**How it works.** Imports `load_config` from `.config` and `Analysis`, `analyze` from `.pipeline`, and lists those three names in `__all__`. Because the package imports `.pipeline`, importing `kman_analysis` also loads pipeline, breathing, phase, preprocess and swallow (and, through preprocess, `kman_host.session_io`).

**Notes.** plotting.py is not imported here. report.py imports `kman_analysis.plotting.make_figure` lazily, only after the analysis has run.

### `analysis/kman_analysis/breathing.py`

#### `Breathing`
*dataclass* · [`analysis/kman_analysis/breathing.py:31`](../analysis/kman_analysis/breathing.py#L31)

```python
@dataclass class Breathing: fs, t, wave_deg, ref_pp_deg, breath_t, breath_amp_deg, breath_valid, rate_t, rate_bpm, since_last_s, sign_flipped
```

**Purpose.** Holds the breathing results for one IMU (chest or abdomen).

**How it works.**

Fields:
- `fs: float`: waveform rate (resp_fs; 20 Hz at the defaults).
- `t: np.ndarray`: waveform time in s, `arange(len(wave))/fs`.
- `wave_deg: np.ndarray`: 1-D respiratory waveform in degrees of tilt, inspiration up.
- `ref_pp_deg: np.ndarray`: per-sample reference peak-to-peak amplitude used for the prominence threshold.
- `breath_t: np.ndarray`: time in s of each detected peak (end-inspiration).
- `breath_amp_deg: np.ndarray`: prominence of each peak, in degrees.
- `breath_valid: np.ndarray` (bool): False where the peak falls in motion or gap.
- `rate_t: np.ndarray`: time of each rate value, which is the second breath of the interval.
- `rate_bpm: np.ndarray`: 60 / interval between consecutive valid breaths.
- `since_last_s: np.ndarray`: seconds since the last valid breath, on the full-rate grid (length `u.n`; NaN before the first valid breath).
- `sign_flipped: bool`: True if orient_inspiration_up negated the waveform.

#### `gravity_complementary`
*function* · [`analysis/kman_analysis/breathing.py:45`](../analysis/kman_analysis/breathing.py#L45)

```python
def gravity_complementary(acc_g: np.ndarray, gyro_dps: np.ndarray, fs: float, tau_s: float) -> np.ndarray
```

**Purpose.** Estimates the unit gravity-direction vector in the sensor frame for each sample. Gyro integration supplies the fast content and the normalised accelerometer the slow content. Tracking the vector instead of Euler angles avoids a singularity in any posture.

**How it works.**

Inputs are `acc_g` (n,3) in g and bias-corrected `gyro_dps` (n,3) in deg/s.
1. `dt = 1/fs`. `alpha = tau_s/(tau_s + dt)` and `beta = 1 - alpha = dt/(tau_s + dt)`. At 100 Hz with tau 1 s: alpha ≈ 0.9901, beta ≈ 0.0099.
2. Accel direction: `norm = |acc_g|` per row, with zero norms replaced by 1. `a[k] = acc_g[k]/norm[k]`.
3. Rotation increment per sample: `w[k] = radians(gyro_dps[k]) * dt`, in rad.
4. Initialise `g = a[0]`.
5. For each k = 0..n-1:
   - Prediction, a forward-Euler step of dg/dt = -omega x g: `g_pred = g - w[k] x g`, i.e.
     `px = gx - (wy*gz - wz*gy)`
     `py = gy - (wz*gx - wx*gz)`
     `pz = gz - (wx*gy - wy*gx)`
   - Correction: `g = alpha*g_pred + beta*a[k]`.
   - Renormalise: `g = g/|g|`.
   - `out[k] = g`.
6. Returns `out` (n,3), unit vectors.
The accelerometer path acts as a first-order low-pass with time constant tau and the gyro path as the complementary high-pass, crossing over near 1/(2\*pi\*tau) Hz (about 0.16 Hz at tau = 1 s).

**Notes.** The loop runs in pure Python over lists (`.tolist()`) to avoid per-element numpy overhead. Step 5 is applied at k = 0 too, starting from a[0]. A row of exactly zero acceleration contributes a zero vector to the blend. The module docstring explains why: while walking, the gyro path keeps step accelerations out of the tilt estimate. tests/test_analysis.py::test_complementary_filter_tracks_rotation checks it against a 5-degree, 0.3 Hz rotation with noisy accel (max error &lt; 0.5 deg).

#### `bandpass`
*function* · [`analysis/kman_analysis/breathing.py:79`](../analysis/kman_analysis/breathing.py#L79)

```python
def bandpass(x: np.ndarray, fs: float, band: tuple[float, float], order: int) -> np.ndarray
```

**Purpose.** Zero-phase Butterworth band-pass along axis 0, with edge padding long enough for the slow 0.05 Hz corner.

**How it works.**

1. `sos = signal.butter(order, band, btype="bandpass", fs=fs, output="sos")`.
2. Subtracts the per-column median: `x = x - np.median(x, axis=0)`.
3. `padlen = min(len(x) - 1, int(3 * fs / band[0]))`, i.e. three periods of the low corner. At 20 Hz and 0.05 Hz that is 1200 samples (60 s).
4. Returns `signal.sosfiltfilt(sos, x, axis=0, padtype="even", padlen=padlen)`, same shape as `x`.

**Notes.** The docstring explains the padding: a 0.05 Hz corner rings for about 20 s, and scipy's default short pad would leave about 30 s of edge transient that swamps the breathing. For a band-pass, scipy's `butter(N, ...)` returns a filter of order 2N, so `filter_order: 2` gives a 4th-order band-pass that sosfiltfilt applies forward and backward.

#### `windowed_pca`
*function* · [`analysis/kman_analysis/breathing.py:89`](../analysis/kman_analysis/breathing.py#L89)

```python
def windowed_pca(x: np.ndarray, fs: float, window_s: float, weight: np.ndarray | None = None) -> np.ndarray
```

**Purpose.** Reduces the (n, d) band-passed tilt to one waveform by projecting onto the first principal axis. The axis is re-estimated in each window, so it can follow posture changes.

**How it works.**

1. `L = max(8, int(window_s*fs))` (600 at the defaults).
2. Window starts: if `n <= L`, one window `[0]` with `L = n`. Otherwise `hop = L//2` (50 % overlap) and `starts = range(0, n-L+1, hop)`. If the last window ends before n (`starts[-1] + L < n`), an extra window starting at `n - L` is appended so the tail is covered; it overlaps its neighbour by more than 50 %.
3. Taper: `win = np.hanning(L+2)[1:-1]`, a Hann window with its two zero end-points dropped, so every weight is > 0.
4. For each start `s`:
   a. `seg = x[s:s+L]` and `use = seg`.
   b. If `weight` is given: `good = weight[s:s+L] > 0`. If `good.sum() >= 0.3*L`, then `use = seg[good]`. Otherwise all samples are used.
   c. Covariance `c = np.cov((use - use.mean(0)).T)` (d x d). `np.linalg.eigh(c)` returns eigenvalues in ascending order, so `v = vecs[:, -1]` is PC1.
   d. Sign continuity: if a previous axis exists and `v @ prev < 0`, then `v = -v`. Set `prev = v`. This stops the projection flipping polarity between windows, since eigenvectors have arbitrary sign.
   e. Overlap-add: `y[s:s+L] += win * ((seg - seg.mean(0)) @ v)` and `wsum[s:s+L] += win`.
5. Returns `y / np.maximum(wsum, 1e-9)`, the Hann-weighted average of every window's projection at each sample.

**Notes.** The weight only affects the axis estimate. The projection in 4e uses every sample of `seg`, centred on the mean of the full segment, so motion and gap samples still produce output values. The global sign comes from the first window's eigenvector and is arbitrary; orient_inspiration_up fixes it afterwards. Dividing by `wsum` normalises exactly at the edges and where the extra end window adds overlap.

#### `motion_energy_mg`
*function* · [`analysis/kman_analysis/breathing.py:128`](../analysis/kman_analysis/breathing.py#L128)

```python
def motion_energy_mg(acc_g: np.ndarray, fs: float, cfg: dict) -> np.ndarray
```

**Purpose.** Computes the chest high-frequency acceleration RMS in mg. pipeline.analyze thresholds it into the motion-artifact mask.

**How it works.**

1. `m = cfg["motion"]`. `sos = butter(4, m["highpass_hz"], btype="highpass", fs=fs)`, a 4th-order Butterworth at 1.5 Hz.
2. `hp = sosfiltfilt(sos, acc_g, axis=0)`, zero-phase on all three axes (g).
3. `e = sum(hp**2, axis=1)`, the squared vector magnitude in g^2.
4. `win = max(1, int(m["window_s"]*fs))` (200 at 100 Hz). `ms` is the centred pandas rolling mean of `e` with `min_periods=1`, so the edges use partial windows.
5. Returns `sqrt(ms) * 1000`, the RMS in mg, length n.

**Notes.** Called only on the chest accelerometer, by pipeline.analyze. The same `motion` mask is then used for both the chest and abdomen breathing.

#### `respiratory_waveform`
*function* · [`analysis/kman_analysis/breathing.py:138`](../analysis/kman_analysis/breathing.py#L138)

```python
def respiratory_waveform(u: Uniform, prefix: str, cfg: dict, calib: np.ndarray, motion: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]
```

**Purpose.** Produces the 1-D respiratory tilt waveform in degrees for the IMU with channel prefix 'chest' or 'abd', at the reduced rate resp_fs.

**How it works.**

1. `b = cfg["breathing"]`, `acc = u.vec(prefix, "a")`, `gyr = u.vec(prefix, "g")`.
2. If a gyro is present: `nb = int(gyro_bias_s * u.fs)` (1000 at 100 Hz). `cal_idx = flatnonzero(calib)[:nb]`, the first nb calibration samples, which need not be contiguous. `bias = median(gyr[cal_idx], axis=0)`, or `median(gyr[:nb], axis=0)` if the calibration mask is empty. Then `g = gravity_complementary(acc, gyr - bias, u.fs, comp_filter_tau_s)`.
   Otherwise `g = acc / |acc|` per row (accelerometer only).
3. Decimation: `q = max(1, round(u.fs / resp_fs_hz))` (5 at 100 Hz) and `rfs = u.fs / q` (20 Hz). `gd = signal.decimate(g, q, ftype="fir", axis=0, zero_phase=True)` (FIR anti-alias, zero phase) if `q > 1`, else `gd = g`.
4. `tilt = bandpass(gd, rfs, tuple(band_hz), filter_order)`, three band-passed gravity-direction components.
5. `clean = ~motion[::q][:len(gd)] & ~u.gap[::q][:len(gd)]`, the full-rate masks subsampled by stride q.
6. `wave = np.degrees(windowed_pca(tilt, rfs, pca_window_s, weight=clean.astype(float)))`. For small angles a change in a unit-vector component equals the rotation in radians, so `degrees()` gives tilt in degrees.
7. `t = arange(len(wave)) / rfs`. Returns `(t, wave, rfs)`.

**Notes.** The accelerometer-only branch has no zero-norm guard, unlike gravity_complementary. An accel channel that was never valid (filled with zeros by `_fill`) would produce NaN there. The waveform sign is not yet oriented.

#### `orient_inspiration_up`
*function* · [`analysis/kman_analysis/breathing.py:162`](../analysis/kman_analysis/breathing.py#L162)

```python
def orient_inspiration_up(wave: np.ndarray, mask: np.ndarray) -> tuple[np.ndarray, bool]
```

**Purpose.** Sets the waveform's global sign so that inspiration rises, which makes the detected peaks end-inspiration.

**How it works.**

1. If `mask[1:].sum() > 50`, then `d = np.diff(wave)[mask[1:]]` (derivative over calibration samples only). Otherwise `d = np.diff(wave)` over the whole recording.
2. If `len(d) < 10` or `d.std() == 0`, returns `(wave, False)`.
3. `skew = mean(((d - d.mean())/d.std())**3)`, the population skewness of the derivative.
4. If `skew < 0`, returns `(-wave, True)`. Otherwise returns `(wave, False)`.

**Notes.** From the docstring: inspiration is shorter than expiration, so the rising slope is steeper and the derivative is positively skewed when inspiration points up. The caller passes the calibration mask decimated to the waveform rate (`calib[::q]`).

#### `detect_breaths`
*function* · [`analysis/kman_analysis/breathing.py:172`](../analysis/kman_analysis/breathing.py#L172)

```python
def detect_breaths(t: np.ndarray, wave: np.ndarray, fs: float, invalid: np.ndarray, cfg: dict, n_full: int, fs_full: float) -> Breathing
```

**Purpose.** Finds breaths as waveform peaks with an adaptive prominence threshold, then derives breath rate and seconds since the last valid breath.

**How it works.**

1. `p = cfg["breathing"]["peaks"]`. `win = max(3, int(ref_window_s * fs))` (1200 samples at 20 Hz).
2. Reference amplitude from clean samples only: `mag = where(invalid, NaN, |wave|)`. `ref` is the centred pandas rolling quantile (`ref_quantile` = 0.8) of `mag` with `min_periods = win//4`. NaNs are skipped, so a window needs at least win//4 clean samples or it yields NaN.
3. `ref_pp = 2 * ref`, back-filled and then forward-filled to cover NaN stretches. This is the reference peak-to-peak amplitude in degrees, taking the band-passed wave as roughly symmetric about zero.
4. Adaptive prominence, per sample: `prom_min[i] = max(abs_min_prominence_deg, rel_prominence * ref_pp[i])`, i.e. `max(0.08 deg, 0.3 * 2 * Q0.8(|wave| over 60 s, clean))`.
5. `peaks, props = signal.find_peaks(wave, prominence=(prom_min, None), distance=max(1, int(min_interval_s*fs)))`. With a per-sample array, each peak is compared against `prom_min` at its own index. The distance is 30 samples (1.5 s).
6. `bt = t[peaks]`, `amp = props["prominences"]`, and `valid = ~invalid[peaks]` (an empty bool array if there are no peaks).
7. Rate: `vt = bt[valid]`, `iv = diff(vt)`, `keep = iv <= max_rate_interval_s` (15 s). `rate_t = vt[1:][keep]` and `rate = 60/iv[keep]` bpm.
8. Since-last on the full-rate grid: `tf = arange(n_full)/fs_full` and `since` starts as NaN. If valid breaths exist: `k = searchsorted(vt, tf, side="right") - 1` gives the last valid breath at or before each time, and `since[k >= 0] = tf - vt[k]`.
9. Returns `Breathing(..., sign_flipped=False)`; the caller sets the flag.

**Notes.** Breaths in motion or gap are kept but marked invalid, and the rate and since-last use valid breaths only. Rate intervals come from consecutive valid breaths, so an invalid breath between two valid ones yields one interval spanning both cycles (about half the true rate), unless it exceeds 15 s. The code comment explains the clean-only reference: walking sway or coughs must not raise the threshold for the breathing around them.

#### `analyze_breathing`
*function* · [`analysis/kman_analysis/breathing.py:203`](../analysis/kman_analysis/breathing.py#L203)

```python
def analyze_breathing(u: Uniform, prefix: str, cfg: dict, calib: np.ndarray, motion: np.ndarray) -> Breathing
```

**Purpose.** Runs the breathing pipeline for one IMU. This is the module's entry point, called from pipeline.analyze.

**How it works.**

1. `t, wave, rfs = respiratory_waveform(u, prefix, cfg, calib, motion)`.
2. `q = round(u.fs / rfs)` recovers the decimation factor.
3. `cal_r = calib[::q][:len(wave)]`, then `wave, flipped = orient_inspiration_up(wave, cal_r)`.
4. `invalid = (motion[::q] | u.gap[::q])[:len(wave)]`.
5. `br = detect_breaths(t, wave, rfs, invalid, cfg, u.n, u.fs)`, so `since_last_s` is on the full-rate grid.
6. Sets `br.sign_flipped = flipped` and returns `br`.

**Notes.** pipeline.analyze calls it with prefix "chest" when `u.vec("chest", "a")` exists and with "abd" when `u.vec("abd", "a")` exists. Both calls receive the chest-derived `motion` mask.

### `analysis/kman_analysis/config.py`

#### `DEFAULT_PATH`
*constant* · [`analysis/kman_analysis/config.py:10`](../analysis/kman_analysis/config.py#L10)

```python
DEFAULT_PATH = Path(__file__).resolve().parents[1] / "config.yaml"
```

**Purpose.** Absolute path of the default config file.

**How it works.** `Path(__file__).resolve()` is .../analysis/kman_analysis/config.py. `parents[0]` is the kman_analysis directory and `parents[1]` is analysis/, so the path is analysis/config.yaml. It does not depend on the current working directory.

#### `_merge`
*function* · [`analysis/kman_analysis/config.py:13`](../analysis/kman_analysis/config.py#L13)

```python
def _merge(base: dict, over: dict) -> dict
```

**Purpose.** Recursively merges a partial config over a full one, so a user file only needs the keys it changes. Misspelled keys raise an error instead of being ignored.

**How it works.**

1. `out = copy.deepcopy(base)`, so `base` is never modified.
2. Iterates `(over or {}).items()`; a `None` override counts as empty.
3. If the override value `v` is a dict and `out.get(k)` is also a dict, it recurses: `out[k] = _merge(out[k], v)`.
4. Otherwise, if `k` is not in `out`, it raises `KeyError(f"unknown config key: {k}")`. If it is, it replaces the value: `out[k] = v`.
5. Returns `out`.

**Notes.** Unknown keys are caught at any depth, because the recursion applies the same check. tests/test_analysis.py::test_config_rejects_unknown_keys covers `{"swallow": {"k_onn": 3}}`. Types are not checked. A scalar override replaces a whole subsection (for example `{"breathing": 5}`). A dict override of a scalar key replaces the scalar. Lists such as `band_hz` are replaced as a whole, not merged element by element.

#### `load_config`
*function* · [`analysis/kman_analysis/config.py:25`](../analysis/kman_analysis/config.py#L25)

```python
def load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict
```

**Purpose.** Returns the effective analysis configuration: defaults, then an optional user YAML file, then optional in-code overrides.

**How it works.**

1. Opens `DEFAULT_PATH` as UTF-8 and parses it with `yaml.safe_load`, giving `cfg`.
2. If `path` is truthy, parses that file and sets `cfg = _merge(cfg, loaded or {})`. An empty file loads as `None`, which becomes `{}`.
3. If `overrides` is truthy, sets `cfg = _merge(cfg, overrides)`.
4. Returns the nested dict.

**Notes.** The defaults file is re-read on every call; nothing is cached. report.py calls `load_config(args.config)`. The tests call `load_config()` and `load_config(overrides=...)`.

### `analysis/kman_analysis/preprocess.py`

#### `Uniform`
*dataclass* · [`analysis/kman_analysis/preprocess.py:21`](../analysis/kman_analysis/preprocess.py#L21)

```python
@dataclass class Uniform: fs, t, label, gap, ch, label_names, slots
```

**Purpose.** The session resampled onto a gap-free uniform tick grid. Every later stage (filtfilt, Hilbert, PCA) assumes this grid.

**How it works.**

Fields:
- `fs: float`: frame rate in Hz (`session.fs`, which is `header.sample_rate_hz`).
- `t: np.ndarray`: seconds from the first sample, `arange(n)/fs`. It is rebuilt from the tick index; device `t_us` is not used.
- `label: np.ndarray` (int64): protocol label per sample, forward-filled across holes.
- `gap: np.ndarray` (bool): True inside any hole longer than `max_interp_gap_s`.
- `ch: dict[str, np.ndarray]`: one float64 array per channel name (for example `chest_ax`, `chest_gx`, `flex_raw`), in physical units (g, dps, counts), with holes filled.
- `label_names: dict[int, str]`: label id to name, from the session metadata.
- `slots: dict[str, bool]`: presence of `chest_imu`, `abdomen_imu`, `neck_flex`, `neck_imu`. Defaults to an empty dict.

#### `Uniform.n`
*property* · [`analysis/kman_analysis/preprocess.py:31`](../analysis/kman_analysis/preprocess.py#L31)

```python
@property def n(self) -> int
```

**Purpose.** Number of samples on the grid.

**How it works.** Returns `len(self.t)`.

#### `Uniform.vec`
*method* · [`analysis/kman_analysis/preprocess.py:34`](../analysis/kman_analysis/preprocess.py#L34)

```python
def vec(self, prefix: str, kind: str = "a") -> np.ndarray | None
```

**Purpose.** Returns a 3-axis sensor as an (n, 3) array, or None if that sensor is missing.

**How it works.** Builds `names = [f"{prefix}_{kind}x", f"{prefix}_{kind}y", f"{prefix}_{kind}z"]`; for example `vec("chest", "g")` gives `chest_gx/gy/gz`. If all three are keys of `self.ch`, it returns `np.stack([...], axis=1)` with shape (n, 3). Otherwise it returns `None`.

**Notes.** Relies on the firmware/simulator naming scheme, slot prefix plus suffix ("chest" + "ax" gives "chest_ax"). Prefixes used in the analysis are "chest", "abd" and "neck". The pipeline treats a `None` return as "sensor absent".

#### `_fill`
*function* · [`analysis/kman_analysis/preprocess.py:41`](../analysis/kman_analysis/preprocess.py#L41)

```python
def _fill(x: np.ndarray) -> np.ndarray
```

**Purpose.** Fills NaN holes in a 1-D signal so it can be filtered.

**How it works.**

1. `bad = np.isnan(x)`.
2. If there are no NaNs, returns `x` itself (not a copy).
3. If every sample is NaN, returns `np.zeros_like(x)`.
4. Otherwise copies `x` and sets `x[bad] = np.interp(idx[bad], idx[~bad], x[~bad])`, where `idx = arange(len(x))`. Interior holes get linear interpolation. Because `np.interp` clamps outside the known range, leading and trailing holes take the nearest valid value.

**Notes.** A channel that is never valid becomes all zeros. to_uniform also marks such a channel entirely in `gap` (if n > max_gap).

#### `_runs`
*function* · [`analysis/kman_analysis/preprocess.py:54`](../analysis/kman_analysis/preprocess.py#L54)

```python
def _runs(mask: np.ndarray) -> list[tuple[int, int]]
```

**Purpose.** Finds the contiguous True runs in a boolean mask as half-open [start, end) index pairs.

**How it works.** Returns `[]` if `mask` has no True. Otherwise computes `d = np.diff(np.r_[0, mask.astype(np.int8), 0])`. Run starts are `flatnonzero(d == 1)` and run ends (exclusive) are `flatnonzero(d == -1)`. Returns `list(zip(starts, ends))`; the elements are numpy integers.

**Notes.** Padding with 0 at both ends makes runs that touch the array edges close correctly.

#### `to_uniform`
*function* · [`analysis/kman_analysis/preprocess.py:62`](../analysis/kman_analysis/preprocess.py#L62)

```python
def to_uniform(session: Session, cfg: dict) -> Uniform
```

**Purpose.** Converts the logged frames, which have holes where frames were lost, into a Uniform on the complete tick grid in physical units. Long holes are flagged.

**How it works.**

1. `df = session.df` and `fs = session.fs`. Grid index `idx = seq - seq[0]` (int64), and `n = idx[-1] + 1`. `present[idx] = True` marks ticks that have a frame.
2. Labels: `label` starts as all -1, then `label[idx] = df["label"]`. Forward fill: `pos = where(label >= 0, arange(n), 0)`, then `np.maximum.accumulate(pos)`, then `label = label[pos]`. Each hole takes the label of the last present frame before it.
3. `max_gap = round(cfg["preprocess"]["max_interp_gap_s"] * fs)` samples. For each run `[a, b)` of `~present` (lost frames) with `b - a > max_gap`, sets `gap[a:b] = True`.
4. `valid = df["valid"]` (per-slot bitmask). For each `c` in `session.header.channels`: start `x` as NaN of length n. Compute `v = df[c.name] * c.scale` (raw int16 to g / dps / counts). `ok = (valid & (1 << c.bit)) != 0`, and `x[idx[ok]] = v[ok]`. Runs of NaN in `x` (lost frames plus frames where this slot was invalid) longer than `max_gap` also set `gap`. Store `ch[c.name] = _fill(x)`.
5. `slots = {s: session.has_slot(s)}` for chest_imu, abdomen_imu, neck_flex and neck_imu.
6. Returns `Uniform(fs, t=arange(n)/fs, label, gap, ch, label_names=session.label_names, slots)`.

**Notes.** `gap` is shared by all channels, so a long invalid run on any one channel masks every stage at those samples. The function assumes `seq` is the unwrapped, increasing tick index written by the logger and that the last row has the largest `seq`. Time comes from `seq/fs` only. Short holes (&lt;= max_gap) are interpolated and not masked.

#### `calibration_mask`
*function* · [`analysis/kman_analysis/preprocess.py:101`](../analysis/kman_analysis/preprocess.py#L101)

```python
def calibration_mask(u: Uniform, cfg: dict) -> np.ndarray
```

**Purpose.** Chooses the samples used as the session's quiet reference, normally rest breathing, for biases and calibration-relative thresholds.

**How it works.**

1. `c = cfg["calibration"]`. `m = (u.label == c["label"]) & ~u.gap`.
2. If `m.sum() < 5 * u.fs` (less than 5 s of that label), falls back to `m = (u.t < c["fallback_first_s"]) & ~u.gap`.
3. `limit = int(c["max_s"] * u.fs)`. If `m.sum() > limit`, it takes `cut = flatnonzero(m)[limit]` and sets `m[cut:] = False`, keeping only the first `limit` selected samples.
4. Returns a boolean array of length `u.n`.

**Notes.** If the label occurs in several segments, the cap takes the earliest selected samples across all of them. The fallback is not re-checked against the 5-s minimum, so if the first 30 s are all gap the mask can be empty. Consumers handle an empty mask with their own fallbacks.

#### `runs`
*function* · [`analysis/kman_analysis/preprocess.py:114`](../analysis/kman_analysis/preprocess.py#L114)

```python
def runs(mask: np.ndarray) -> list[tuple[int, int]]
```

**Purpose.** Public alias of `_runs` for other modules.

**How it works.** Returns `_runs(mask)`, the [start, end) pairs of True runs.

**Notes.** Imported by pipeline (`_max_breath_gap`), swallow.analyze_neck (hysteresis runs) and plotting (`_bands`, `_artifact_spans`).

## 16. Analysis: phase, neck events, pipeline, plotting, report

- **`analysis/kman_analysis/phase.py`**: Computes the phase relationship between the chest and abdomen respiratory waveforms (both produced by breathing.py on the 20 Hz respiration grid) using the Hilbert transform, and flags paradoxical breathing: chest and abdomen moving in opposition (|phase| near 180 deg), the signature of an obstructed airway with continued effort. pipeline.analyze calls it only when both a chest and an abdomen IMU are present. Its Phase output feeds the per-label phase columns, the abd_polarity_flipped QA key and the phase panel of the report figure.
- **`analysis/kman_analysis/swallow.py`**: Detects deflection events on the neck flex channel (flex_raw, ADC counts) with a calibration-relative hysteresis detector, then classifies each event as 'swallow' or 'head_motion' using amplitude, duration and, if a neck IMU is fitted, head tilt/yaw change. It also produces rolling event-count traces. pipeline.analyze calls analyze_neck. The NeckEvents result feeds the per-label swallow/head columns, the flex_sigma_cal_counts QA key, the flex and neck_rate figure panels and report.py's &lt;session>_neck_events.csv.
- **`analysis/kman_analysis/pipeline.py`**: Orchestrates the whole Step 1 offline analysis of one recorded Session. It builds the uniform grid and calibration mask, computes the chest motion-artifact mask, runs breathing analysis per IMU, chest-abdomen phase and neck events, then reduces everything to a per-label summary table and a data-quality dict. analyze() is the package's public entry point (re-exported by kman_analysis/__init__.py) and returns an Analysis bundle that plotting.py and report.py consume.
- **`analysis/kman_analysis/plotting.py`**: Renders one stacked matplotlib figure for an Analysis. Each available signal gets its own panel on a shared time axis, with activity labels drawn as coloured background bands, artifact/gap regions hatched, a label legend, a QA headline and a per-label summary table at the bottom. It forces the headless 'Agg' backend at import, and data marks use neutral ink so that colour belongs only to the label bands. report.main calls make_figure.
- **`analysis/report.py`**: Command-line entry point for offline analysis. It loads the config and a session .parquet, runs analyze(), saves the figure PNG plus summary/breaths/neck-event CSVs to reports/, and prints the session header, the QA dict, the per-label table and a frame-loss warning. At import it inserts &lt;repo>/host and &lt;repo>/analysis at the front of sys.path, so kman_host and kman_analysis import without being installed. It runs main() under the __main__ guard.

**How it connects.** The only production caller of this group is analysis/report.py:main. It calls kman_analysis.config.load_config, then kman_host.session_io.load_session (which reads a .parquet written by SessionWriter, from host/logger.py or host/sim_device.py --write-session), then pipeline.analyze, then plotting.make_figure. analysis/tests/test_analysis.py also calls analyze, sometimes with load_config overrides, and checks summary columns and neck event rows against simulator ground truth. analyze calls into preprocess.py (to_uniform, calibration_mask, Uniform.vec) and breathing.py (motion_energy_mg, analyze_breathing). It then calls phase.chest_abdomen_phase, which takes the two Breathing waveforms on the decimated resp grid, and swallow.analyze_neck, which uses preprocess.runs and Uniform.vec/ch/gap/label and calls head_pose_change for the neck-IMU checks. Last, it calls per_label_summary (whose _max_breath_gap also uses preprocess.runs) and quality. quality reads the stats dict that StreamDecoder.summary() produced at logging time: 'stream' = asdict(StreamStats) with dropped/segments, 'parser' with crc_errors, and 'device_status_last' = DeviceStatus.as_dict() with overruns/tx_drops/hfxo. plotting.py takes its label colours and the hatched label set from kman_host.labels (LABEL_COLORS_LIGHT, HATCHED_LABELS), uses preprocess.runs for the band and artifact spans, and reads every field of the Analysis bundle. The configurable thresholds come from analysis/config.yaml: these files read the phase, swallow, motion and report sections directly, and the stages they call read preprocess, calibration and breathing. Several constants are hardcoded rather than configurable, though: the >10-sample calibration minimum and the 0.5 amplitude-majority and window-coverage fractions in phase.py; the 2 Hz head-IMU low-pass, the 0.2 s reference period and the 15-sample filter guard in swallow.py; the 10 s clean-data minimum for breaths_per_min and the 1.999 g clipping level in pipeline.py; and the 600 s minutes/seconds switch in plotting.py.

### `analysis/kman_analysis/phase.py`

#### `Phase`
*dataclass* · [`analysis/kman_analysis/phase.py:21`](../analysis/kman_analysis/phase.py#L21)

```python
@dataclass class Phase
```

**Purpose.** Holds the result of the chest-abdomen phase analysis on the respiration-rate grid.

**How it works.** Fields, all arrays of the same length n (the resp-rate grid): t (np.ndarray, seconds; passed through unchanged from the caller, chest.t[:n]); phase_deg (windowed circular mean of the chest-minus-abdomen instantaneous phase difference, degrees in (-180, 180]); coherence (resultant length |z| of that windowed mean, 0..1, where 1 = constant phase difference over the window); valid (bool, the amplitude + coherence + coverage gate passed); paradox (bool, valid & |phase_deg| > cfg['phase']['paradox_deg']); abd_flipped (bool scalar, True if the abdomen waveform was negated by the polarity calibration).

#### `chest_abdomen_phase`
*function* · [`analysis/kman_analysis/phase.py:30`](../analysis/kman_analysis/phase.py#L30)

```python
chest_abdomen_phase(t: np.ndarray, chest: np.ndarray, abd: np.ndarray, fs: float, calib: np.ndarray, invalid: np.ndarray, cfg: dict) -> Phase
```

**Purpose.** Measures how far the abdomen waveform lags or leads the chest waveform over time, and gates out samples where that measurement is unreliable. Its paradox flag marks valid samples where chest and abdomen move in opposition.

**How it works.**

Inputs: chest and abd are the 1-D PCA respiratory waveforms (deg) on the resp grid, fs the resp rate (20 Hz default), calib the calibration mask on that grid, invalid = (motion | gap) on that grid; c = cfg['phase'].
1. Polarity calibration. Each waveform's PCA sign is arbitrary. If calib.sum() > 10 and np.corrcoef(chest[calib], abd[calib])[0,1] &lt; 0, then abd = -abd and flipped = True. This assumes chest and abdomen are in phase during rest breathing.
2. Analytic signals: zc = signal.hilbert(chest), za = signal.hilbert(abd), computed over the whole record.
3. Unit phasor of the instantaneous phase difference: d = exp(1j\*(angle(zc) - angle(za))).
4. Windowed circular mean over valid samples only. ok = ~invalid; win = max(3, int(c['window_s']\*fs)) (10 s -> 200 samples). Centred pandas rolling sums with min_periods=1 give cnt = number of ok samples in the window, and re/im = sums of d.real/d.imag with invalid samples set to 0. Then z = (re + 1j\*im) / max(cnt, 1).
5. phase = degrees(angle(z)) in (-180, 180]; coh = |z|.
6. Amplitude gate. The Hilbert envelopes are ec = |zc| and ea = |za|. Their reference medians mc and ma are taken over calib when calib.sum() > 10, otherwise over the whole record. Per sample, amp_ok = (ec > min_amplitude_frac\*mc) & (ea > min_amplitude_frac\*ma) (0.25 default). This is then smoothed by majority vote: the centred rolling mean of amp_ok over win must be > 0.5.
7. valid = amp_ok & (coh >= c['min_coherence']) (0.5) & (cnt >= 0.5\*win). The last term requires at least half of the window to be artifact-free.
8. paradox = valid & (|phase| > c['paradox_deg']) (120 deg).
9. Returns Phase(t, phase, coh, valid, paradox, flipped).

**Notes.** Invalid samples still go into the Hilbert transform; they are excluded only from the windowed average. A window with no ok samples gives z = 0, so np.angle returns phase 0 with coherence 0, and the sample fails the gate. The polarity calibration assumes the calibration segment (label 1, rest breathing) is genuinely in phase; a paradoxical calibration segment would invert all results. If the correlation is NaN (e.g. a constant waveform), NaN &lt; 0 is False and no flip happens. With 10 or fewer calibration samples (0.5 s at 20 Hz) there is no polarity calibration and the amplitude reference becomes the whole-record median. Near the record ends the centred window is truncated, so cnt is about half a window there.

### `analysis/kman_analysis/pipeline.py`

#### `Analysis`
*dataclass* · [`analysis/kman_analysis/pipeline.py:19`](../analysis/kman_analysis/pipeline.py#L19)

```python
@dataclass class Analysis
```

**Purpose.** Bundles every intermediate and final result of one session's analysis so the figure, CSV export and tests can use any of it.

**How it works.** Fields: session (kman_host Session as loaded); cfg (merged config dict); u (preprocess.Uniform: full-rate grid, channels, label, gap); calib (bool full-rate calibration mask); motion_mg (full-rate chest high-pass accel RMS in mg, or None without a chest IMU); motion (bool full-rate artifact mask, motion_mg > threshold, all False without a chest IMU); chest and abd (breathing.Breathing or None); phase (Phase or None); neck (NeckEvents or None); summary (per-label DataFrame); qa (data-quality dict).

#### `analyze`
*function* · [`analysis/kman_analysis/pipeline.py:34`](../analysis/kman_analysis/pipeline.py#L34)

```python
analyze(session: Session, cfg: dict) -> Analysis
```

**Purpose.** Runs the complete Step 1 analysis on a session: it calls each stage in order and computes the summary and QA.

**How it works.**

1. u = to_uniform(session, cfg): samples are placed on the tick grid and holes are interpolated. Long holes, including long invalid-slot runs in any channel, are masked in u.gap.
2. calib = calibration_mask(u, cfg): samples with label cfg['calibration']['label'] (1) outside gaps. If that gives fewer than 5 s of samples, it falls back to the first fallback_first_s (30 s) of the record, still outside gaps. Either way it keeps only the first max_s (120 s) worth of mask samples.
3. acc = u.vec('chest', 'a'). If present, motion_mg = motion_energy_mg(acc, u.fs, cfg) (RMS above 1.5 Hz, mg) and motion = motion_mg > cfg['motion']['threshold_mg'] (15 mg). Otherwise motion_mg = None and motion = zeros(u.n, bool).
4. chest = analyze_breathing(u, 'chest', cfg, calib, motion) if the chest accel exists; abd = analyze_breathing(u, 'abd', ...) if u.vec('abd', 'a') exists. Both use the chest-derived motion mask.
5. If both chest and abd exist: q = round(u.fs / chest.fs) (decimation factor, e.g. 100/20 = 5); n = min(len(chest.wave_deg), len(abd.wave_deg)); inv = (motion[::q] | u.gap[::q])[:n]; phase = chest_abdomen_phase(chest.t[:n], chest.wave_deg[:n], abd.wave_deg[:n], chest.fs, calib[::q][:n], inv, cfg). Otherwise phase = None.
6. neck = analyze_neck(u, cfg, calib) (None without flex_raw).
7. It builds Analysis with an empty summary and qa, then fills a.summary = per_label_summary(a) and a.qa = quality(a), and returns a.

**Notes.** The abdomen has no motion mask of its own; its breath validity uses the chest accelerometer's artifact mask. The decimation for phase/calibration masks uses plain [::q] striding to match the decimated breathing grid.

#### `_circmean_deg`
*function* · [`analysis/kman_analysis/pipeline.py:64`](../analysis/kman_analysis/pipeline.py#L64)

```python
_circmean_deg(x: np.ndarray) -> float
```

**Purpose.** Averages angles correctly across the +/-180 wrap (e.g. +170 and -170 average to 180, not 0).

**How it works.** Returns NaN for an empty input. Otherwise it returns degrees(angle(mean(exp(1j\*radians(x))))), the direction of the mean unit phasor, in (-180, 180].

#### `_max_breath_gap`
*function* · [`analysis/kman_analysis/pipeline.py:70`](../analysis/kman_analysis/pipeline.py#L70)

```python
_max_breath_gap(m: np.ndarray, bad: np.ndarray, valid_breaths_t: np.ndarray, fs: float) -> float
```

**Purpose.** Finds the longest interval without a valid breath within the segments of one label. This is the key breath-hold / apnea metric.

**How it works.**

best = NaN. For each contiguous run [a, b) of the label mask m (full-rate indices):
1. Skip the run if bad[a:b].mean() > 0.5 (more than half of it is motion artifact or gap).
2. t0 = a/fs, t1 = b/fs. pts = [t0, the valid breath times t with t0 &lt;= t &lt; t1, t1].
3. g = max(diff(pts)). best = max(best, g).
Returns best, which is NaN if no run qualified.

**Notes.** Segment edges count as boundaries, so a segment with no valid breath reports its full length, and the time from the segment start to the first breath counts as a gap. Inside a qualifying segment, an artifact stretch of up to 50 % contains no valid breaths and therefore shows up as a gap.

#### `per_label_summary`
*function* · [`analysis/kman_analysis/pipeline.py:84`](../analysis/kman_analysis/pipeline.py#L84)

```python
per_label_summary(a: Analysis) -> pd.DataFrame
```

**Purpose.** Reduces the analysis to one row per activity label: the statistics used to compare rest vs breath-hold, swallow vs head movement and normal vs obstruction.

**How it works.**

There is one row per value in sorted(unique(u.label)), including label 0. m = (u.label == lab) is full-rate, dur = m.sum()/fs, clean = m & ~motion & ~gap, and clean_min = clean.sum()/fs/60. Columns:
Always:
- label: int(lab).
- name: u.label_names.get(lab, str(lab)).
- duration_s: round(dur, 1), all samples of the label including artifact/gap.
- artifact_pct: 100 \* mean(motion[m]), the share of label samples flagged as motion artifact (gaps not included).
- gap_pct: 100 \* mean(gap[m]), the share inside long data holes.
If a.chest exists (bi = full-rate index round(breath_t\*fs), clipped to [0, n-1]; in_lab = m[bi] & breath_valid):
- breaths: count of valid chest breaths whose peak lies in the label.
- breaths_per_min: breaths / clean_min if clean_min > 10/60 (more than 10 s of clean data), else NaN.
- rate_median_bpm: median of chest rate_bpm values whose rate_t (time of the second breath of the interval) maps into m; NaN if none. Rates exist only for consecutive valid breaths at most max_rate_interval_s apart.
- amp_median_deg: median breath prominence (breath_amp_deg, deg) over the in_lab breaths; NaN if none.
- max_breath_gap_s: _max_breath_gap(m, motion | gap, breath_t[breath_valid], fs).
If a.phase exists (q = round(fs / chest.fs); mp = m[::q][:len(phase.t)] is the label mask on the resp grid; v = mp & phase.valid):
- phase_valid_pct: 100 \* v.sum() / max(1, mp.sum()).
- phase_mean_deg: _circmean_deg(phase_deg[v]); NaN if v is empty.
- paradox_pct: 100 \* (mp & paradox).sum() / max(1, v.sum()) if v.any() else NaN, i.e. the percentage of valid phase samples in the label that are paradoxical.
If a.neck exists (each event is assigned to the label at its peak sample round(peak_s\*fs); win = cfg['swallow']['rate_window_s']):
- swallows: number of 'swallow' events with their peak in the label.
- swallows_per_10s: swallows / dur \* win (NaN if dur == 0).
- head_events: number of 'head_motion' events with their peak in the label.
- head_events_per_10s: head_events / dur \* win.
It returns a DataFrame of the rows; which columns exist depends on which sensors were present.

**Notes.** Breath rates use clean minutes as the denominator, but neck event rates use the total label duration (including artifact/gap). The '_per_10s' columns are per rate_window_s (10 s by default). duration_s is rounded, but the rate computations use the unrounded dur.

#### `quality`
*function* · [`analysis/kman_analysis/pipeline.py:129`](../analysis/kman_analysis/pipeline.py#L129)

```python
quality(a: Analysis) -> dict
```

**Purpose.** Collects session-level data-quality numbers (link loss, CRC errors, device counters, calibration length, artifact share, sensor polarity decisions, accel clipping) for the console report and the figure headline.

**How it works.**

st = session.stats['stream'] and dev = session.stats['device_status_last'] (both default to {}). Keys:
- duration_s: round(u.n/u.fs, 1), the uniform-grid length (first to last tick, holes included).
- samples: len(session.df), frames actually stored.
- lost_frames: st['dropped'] (logger's count of missing ticks inside segments) if present, else u.n - len(df).
- lost_pct: round(100\*(u.n - len(df)) / max(1, u.n), 4), always computed from the grid.
- crc_errors: stats['parser']['crc_errors'], default 0.
- segments: st['segments'], default 1.
- long_gap_pct: round(100\*mean(u.gap), 3), the share of the grid inside masked long holes.
- device_overruns: dev.get('overruns') (firmware status counter; None if no status frame was logged).
- device_tx_drops: dev.get('tx_drops').
- hf_crystal: dev.get('hfxo'), the HFXO status flag (bool or None).
- calibration_s: round(calib.sum()/fs, 1).
- artifact_pct: round(100\*mean(motion), 2), over the whole session.
Conditional:
- flex_sigma_cal_counts: round(neck.sigma_cal, 2) if neck.
- chest_sign_flipped: chest.sign_flipped (orient_inspiration_up negated the waveform) if chest.
- chest_accel_clipped_pct: round(100\*mean(|acc| >= 1.999), 4) over all three chest accel axes pooled, i.e. the share of axis-samples at the +/-2 g full scale, if chest.
- abd_polarity_flipped: phase.abd_flipped if phase.
Returns the dict.

**Notes.** lost_frames and lost_pct come from different sources (logger stats vs grid arithmetic), so they need not agree exactly. The chest_accel_clipped_pct branch assumes the chest accel exists whenever a.chest does, which holds because analyze only creates chest when that accel is present.

### `analysis/kman_analysis/plotting.py`

#### `_style`
*function* · [`analysis/kman_analysis/plotting.py:34`](../analysis/kman_analysis/plotting.py#L34)

```python
_style(ax, ylabel: str) -> None
```

**Purpose.** Applies the shared neutral axis style to a panel.

**How it works.** It sets the face colour to SURFACE (#fcfcfb) and turns on a grid (GRID #e4e3de, 0.6 lw) drawn below the data. It hides the top/right spines and colours the left/bottom spines INK_3. Ticks get colour INK_2 at size 8, and the y label is set in INK_2 at 8 pt.

#### `_title`
*function* · [`analysis/kman_analysis/plotting.py:46`](../analysis/kman_analysis/plotting.py#L46)

```python
_title(ax, text: str, pad: float = 3) -> None
```

**Purpose.** Sets a panel title in the shared style.

**How it works.** ax.set_title(text, loc='left', colour INK, 9.5 pt bold, pad=pad).

#### `_bands`
*function* · [`analysis/kman_analysis/plotting.py:50`](../analysis/kman_analysis/plotting.py#L50)

```python
_bands(ax, a: Analysis, ts: float, alpha: float, numbers: bool, t_range=None) -> None
```

**Purpose.** Draws the activity labels as coloured background spans on a panel and, on the top panel, writes each span's label code.

**How it works.** lo, hi = t_range/ts if a zoom range is given, else (-inf, inf). For each unique label, skipping 0 and labels absent from LABEL_COLORS_LIGHT, and for each contiguous run (s, e) of that label: x0, x1 = s/fs/ts, e/fs/ts. It draws axvspan(x0, x1, colour, alpha, zorder 0). Labels in HATCHED_LABELS ({9}) get a second span with no face, INK_3 edge and '///' hatch. If numbers is set, xm = (max(x0, lo) + min(x1, hi))/2 is the centre of the visible part of the span; when lo &lt;= xm &lt;= hi, the label code is drawn in bold INK_2 at x = xm, y = 0.97 in axes coordinates (get_xaxis_transform), clipped to the axes.

**Notes.** make_figure passes numbers=True only for the first panel. Spans entirely outside the zoom range produce an xm outside [lo, hi], so they get no number.

#### `_artifact_spans`
*function* · [`analysis/kman_analysis/plotting.py:68`](../analysis/kman_analysis/plotting.py#L68)

```python
_artifact_spans(ax, a: Analysis, ts: float) -> None
```

**Purpose.** Marks the samples that breath counting excludes (motion artifact or long gap) with a hatch.

**How it works.** For each run of a.motion | a.u.gap, it draws axvspan(s/fs/ts, e/fs/ts) with no face, INK_3 edge, hatch 'xx', zorder 0.5 (above the label bands).

#### `make_figure`
*function* · [`analysis/kman_analysis/plotting.py:75`](../analysis/kman_analysis/plotting.py#L75)

```python
make_figure(a: Analysis, t_range: tuple[float, float] | None = None)
```

**Purpose.** Builds the complete session report figure: stacked signal panels on a shared time axis, label legend, QA headline and per-label table.

**How it works.**

1. Time unit: ts = 60 if the full session is longer than 600 s (minutes), else 1 (seconds).
2. Panel list, in order and only if the data exist: resp_chest (chest), resp_abd (abd), rate and since (chest), phase, flex and neck_rate (neck), motion (motion_mg). Height ratios are 1.25 for resp_\* and flex and 0.85 otherwise. The figure is (report.width_in, report.panel_height_in\*sum(heights) + 3.2) inches, with a gridspec of len(panels)+1 rows; the extra row, ratio 1.6, holds the table.
3. Per panel (x shared with the first): _bands (numbers only on panel 0); _artifact_spans on resp_chest, resp_abd, rate and since.
- resp_\*: waveform br.wave_deg vs br.t. Breath peaks at index round(breath_t\*br.fs): valid ones as filled circles ('breath'), invalid ones as open circles ('... not counted'). ylim = +/- (99.5th percentile of |wave| \* 1.3), or 1.0 if that is 0. The legend (resp_chest only) adds a hatch patch for 'motion artifact / gap'.
- rate: rate_bpm dots at rate_t; ylim (0, max(30, 99th percentile\*1.15)), or 30 if there are no rates.
- since: chest.since_last_s vs u.t, ylim bottom 0.
- phase: phase_deg with invalid samples set to NaN (line breaks), dashed lines at +/-paradox_deg with a ' paradox > thr' note at the right edge, ylim +/-185, ticks -180..180 step 90.
- flex: neck.deviation vs u.t. Dashed lines at +/-k_on\*sigma_cal and dotted lines at +/-head_amplitude_k\*sigma_cal; the negative side is skipped for polarity 'positive' and the positive side for 'negative'. Swallow peaks are filled '^' and head-motion peaks open squares, both at (peak_s, peak_dev), with counts in the legend. Dummy lines put the two thresholds in the legend. ylim = +/- (99.8th percentile of |dev| \* 1.25), or 1.0.
- neck_rate: swallow_rate_10s solid and head_rate_10s dashed vs u.t, ylim bottom 0.
- motion: semilogy of max(motion_mg, 0.01) with a dashed line at motion.threshold_mg.
4. X tick labels are hidden on all but the last panel, which gets 'time (min|s)'. xlim = t_range/ts if zoomed, else (0, dur/ts).
5. Label legend: one Patch per present label in LABEL_COLORS_LIGHT (alpha 0.5, hatched for 9), text 'code name', placed at the upper right with up to 9 columns.
6. suptitle: session name, qa duration_s, lost_pct, crc_errors, segments, artifact_pct.
7. Table in the last gridspec row (axis off). Columns, (summary key -> header, format), kept only if present in a.summary: label->label {:d}, name->activity {}, duration_s->dur s {:.0f}, breaths_per_min->breaths/min {:.1f}, amp_median_deg->amp deg {:.2f}, max_breath_gap_s->max gap s {:.1f}, paradox_pct->paradox % {:.0f}, swallows_per_10s->swallows/10s {:.2f}, head_events_per_10s->head/10s {:.2f}, artifact_pct->artifact % {:.1f}. Cells are formatted with the nested fmt. Table style: horizontal edges only, 8 pt, row scale 1.15, GRID edge colour, header text INK_2, body text INK, activity column left-aligned (set_ha plus the private cell._loc = 'left').
8. Returns the Figure (not saved here).

**Notes.** ts depends on the whole session length, not the zoom range. The summary table always covers the full session, even when zoomed. Colour encodes only the label bands; event types differ by marker shape/fill (stated in the module docstring). The figure uses the Agg backend set at module import.

#### `make_figure.fmt`
*nested function* · [`analysis/kman_analysis/plotting.py:218`](../analysis/kman_analysis/plotting.py#L218)

```python
fmt(v, f)
```

**Purpose.** Formats one summary-table cell, tolerating missing values and type mismatches.

**How it works.** Returns '-' if v is None or a float NaN (np.float64 counts as float). Otherwise it returns f.format(v). If formatting raises ValueError or TypeError (e.g. '{:d}' applied to a float), it returns str(v).

### `analysis/kman_analysis/swallow.py`

#### `NeckEvents`
*dataclass* · [`analysis/kman_analysis/swallow.py:27`](../analysis/kman_analysis/swallow.py#L27)

```python
@dataclass class NeckEvents
```

**Purpose.** Holds the neck-flex baseline, the deviation trace, the calibration noise level, the detected/classified event table and the rolling event-rate traces.

**How it works.** Fields: t (u.t, seconds on the full-rate uniform grid); baseline (centred moving median of flex_raw, counts); deviation (flex_raw - baseline, counts); sigma_cal (float, robust std of the deviation over calibration, floored at sigma_floor_counts); events (pd.DataFrame with columns start_s, end_s, peak_s, duration_s, peak_dev, peak_k, kind, reason, label; the inline comment omits 'label', but the code always adds it); swallow_rate_10s and head_rate_10s (full-rate arrays: number of 'swallow' / 'head_motion' event peaks inside a centred window of cfg['swallow']['rate_window_s'] around each sample).

**Notes.** The '_10s' suffix reflects the default rate_window_s = 10. The real window is whatever the config says.

#### `_robust_std`
*function* · [`analysis/kman_analysis/swallow.py:37`](../analysis/kman_analysis/swallow.py#L37)

```python
_robust_std(x: np.ndarray) -> float
```

**Purpose.** Outlier-resistant noise estimate for the flex deviation, so that occasional real events in the calibration segment do not inflate sigma.

**How it works.** Returns NaN for an empty array. Otherwise it returns 1.4826 \* median(|x - median(x)|), the MAD scaled to equal the standard deviation for Gaussian noise.

#### `head_pose_change`
*function* · [`analysis/kman_analysis/swallow.py:43`](../analysis/kman_analysis/swallow.py#L43)

```python
head_pose_change(u: Uniform, a: int, b: int) -> tuple[float, float]
```

**Purpose.** Quantifies how much the head tilted (change of gravity direction) and turned (rotation about the vertical) during a sample window [a, b), using the neck IMU, so that flex events caused by head movement can be reclassified.

**How it works.**

1. acc = u.vec('neck', 'a') ((n,3) in g). If the neck accelerometer channels are missing, it returns (0.0, 0.0).
2. Low-pass: sos = butter(2, 2.0 Hz, lowpass, fs=u.fs). seg = sosfiltfilt(sos, acc[a:b], axis=0) when b - a > 15 samples; shorter windows use the raw acc[a:b].
3. Unit gravity direction per sample: g = seg / ||seg|| (row-wise).
4. Reference direction: ref = mean of the first max(1, int(0.2\*u.fs)) rows of g (the first 0.2 s), renormalised.
5. tilt = degrees(max(arccos(clip(g @ ref, -1, 1)))), the largest angle between the gravity direction and its starting direction within the window.
6. Yaw, only if gyr = u.vec('neck', 'g') (dps) exists. w_vert = sum(gyr[a:b] \* g, axis=1) is the angular rate projected on the gravity axis (rotation about vertical, dps). Subtracting median(w_vert) removes bias. Then yaw = max(|cumsum(w_vert) / u.fs|), the peak integrated rotation in degrees. Without a gyro, yaw = 0.0.
7. Returns (tilt, yaw).

**Notes.** The gyro gets no bias correction other than this per-window median subtraction. That subtraction also distorts long steady turns: if a constant-rate turn fills more than half the window, the median equals the turn rate. The turn samples then go to about 0 and the rest of the window gets -rate, so yaw comes out as rate x (time outside the turn) and underestimates the turn. If seg contains an all-zero row, the normalisation gives NaN, tilt and yaw become NaN, and NaN > threshold is False, so the event is not reclassified. That cannot come from a zero-filled channel: preprocess._fill zero-fills only a channel that is NaN for the whole record, and to_uniform then marks u.gap True everywhere, so analyze_neck drops every event before calling this function. The unfiltered branch (b - a &lt;= 15) protects sosfiltfilt, which needs the input to be longer than its default pad length (9 samples for this single-section filter, so 15 leaves some margin). From analyze_neck the window is at least min_duration_s + 2\*margin_s (1.3 s, 130 samples at the defaults), so that branch is effectively never taken.

#### `analyze_neck`
*function* · [`analysis/kman_analysis/swallow.py:63`](../analysis/kman_analysis/swallow.py#L63)

```python
analyze_neck(u: Uniform, cfg: dict, calib: np.ndarray) -> NeckEvents | None
```

**Purpose.** Finds neck-flex deflection events with a calibration-relative Schmitt-trigger detector and labels each one 'swallow' or 'head_motion'.

**How it works.**

Returns None if 'flex_raw' is not in u.ch. c = cfg['swallow']; x = u.ch['flex_raw'] (counts, holes interpolated).
1. Baseline: win = max(3, int(baseline_window_s\*fs) | 1); the '| 1' forces an odd length (10 s at 100 Hz -> 1001). base = centred rolling median (min_periods=1). dev = x - base.
2. Noise: sigma = max(sigma_floor_counts (2.0), _robust_std(dev[calib]) if calib.any() else _robust_std(dev)).
3. Polarity: mag = dev for 'positive', -dev for 'negative', |dev| for anything else (default 'abs').
4. Hysteresis: on = mag > k_on\*sigma (5.0); above_off = mag > k_off\*sigma (2.0). For every contiguous run [a, b) of above_off (preprocess.runs), the run becomes a candidate if on[a:b].any(). An event therefore starts at the k_off crossing before the first k_on crossing and ends at the first sample back below k_off, so it grows outwards from the 'on' core in both directions. Runs that never reach k_on are discarded.
5. Merge: gap = int(merge_gap_s\*fs) (0.2 s). Candidates are taken in order; if a - previous_end &lt;= gap, the previous end is extended to b, otherwise a new event starts.
6. Filter and classify each merged [a, b): dur = (b-a)/fs. Skip the event if dur &lt; min_duration_s (0.3 s, treated as a spike) or if any u.gap[a:b] (overlaps a long data hole). Peak index k = a + argmax(mag[a:b]); peak_k = mag[k]/sigma. Default kind 'swallow', reason ''. If peak_k > head_amplitude_k (25): 'head_motion', reason 'amplitude'. Else if dur > max_duration_s (2.5 s): 'head_motion', reason 'duration'. Then, if a neck accelerometer exists, (tilt, yaw) = head_pose_change(u, max(0, a - margin), min(u.n, b + margin)) with margin = int(head_imu.margin_s\*fs) (0.5 s). If tilt > tilt_change_deg (8) or yaw > yaw_change_deg (10), kind = 'head_motion' and reason = 'neck imu (tilt T deg, yaw Y deg)', which overrides an amplitude/duration reason.
7. Row per kept event: start_s = a/fs, end_s = b/fs (exclusive end), peak_s = k/fs, duration_s = dur, peak_dev = dev[k] (signed counts), peak_k, kind, reason, label = u.label[k]. ev is built with explicit columns, so an empty table still has them.
8. rw = int(rate_window_s\*fs). It builds the rate traces with the nested rolling_count('swallow') and rolling_count('head_motion') and returns NeckEvents(t=u.t, baseline=base, deviation=dev, sigma_cal=float(sigma), events=ev, swallow_rate_10s, head_rate_10s).

**Notes.** The rationale given in the module docstring: a swallow is a short, small deflection, while head flexion/rotation bends the sensor more and for longer. The neck IMU can only promote a swallow to head_motion, never demote one. The min-duration test runs after merging, so two short bursts within 0.2 s can survive together. u.gap is a global mask, so a long hole in any channel suppresses neck events. The detector is non-causal (offline, whole record at once). With polarity 'abs', a positive and a negative lobe are separated by a sub-k_off zero crossing and become one event only if the merge gap joins them.

#### `analyze_neck.rolling_count`
*nested function* · [`analysis/kman_analysis/swallow.py:122`](../analysis/kman_analysis/swallow.py#L122)

```python
rolling_count(kind: str) -> np.ndarray
```

**Purpose.** Turns the event table into a continuous 'events per rate window' trace for one event kind, for plotting.

**How it works.** It creates imp = zeros(u.n). For each event with ev['kind'] == kind it adds 1 at index min(u.n - 1, round(peak_s\*fs)). It returns the centred pandas rolling sum of imp over rw = int(rate_window_s\*fs) samples (min_periods=1), i.e. the count of that kind's peaks within +/- half a window of each sample.

**Notes.** It closes over ev, u and rw from analyze_neck. The window is truncated at the record edges.

### `analysis/report.py`

#### `main (report.py)`
*function* · [`analysis/report.py:31`](../analysis/report.py#L31)

```python
main(argv=None) -> int
```

**Purpose.** CLI driver: analyses one recorded session and writes the figure and CSVs, then prints QA and per-label statistics.

**How it works.**

1. argparse: positional session (.parquet path); --config (YAML deep-merged over analysis/config.yaml); --out-dir (default &lt;repo>/reports); --start / --end (float seconds, zoom); --show.
2. cfg = load_config(args.config); s = load_session(args.session); a = analyze(s, cfg).
3. Creates the out dir (parents=True). stem = s.name, plus '_{start:g}-{end:g}s' only when both --start and --end are given.
4. Imports make_figure inside main. rng = (start, end) if both are given, else None. fig = make_figure(a, rng), saved to &lt;out>/&lt;stem>.png at cfg['report']['dpi'] with the figure facecolor.
5. CSVs, float_format '%.4g', always named from s.name (no zoom suffix) and always covering the full session: &lt;name>_summary.csv (a.summary); &lt;name>_breaths.csv with columns t_s, amplitude_deg, valid from a.chest (chest only); &lt;name>_neck_events.csv (a.neck.events, neck only).
6. Prints the session name, header.raw['board'], header.raw['fw_version'] and the channel names; 'Data quality:' as JSON of a.qa (numpy scalars converted with .item()); the summary rounded to 2 decimals; 'WARNING: frame loss >= 0.1 % (acceptance limit)' when qa['lost_pct'] >= 0.1; and the PNG path.
7. With --show: matplotlib.use('TkAgg', force=True) switches pyplot to the interactive backend, make_figure(a, rng) is called again, and plt.show() runs.
8. Returns 0. The module calls sys.exit(main()) under the __main__ guard.

**Notes.** If only one of --start/--end is given, it is silently ignored (no zoom, no suffix). Before the kman_\* imports, the module inserts &lt;repo>/host and &lt;repo>/analysis into sys.path, so the script runs from a checkout without installation. --config with an unknown key raises KeyError from config._merge.

## 17. Tests

- **`host/tests/test_protocol.py`**: Unit tests for the wire-protocol layer in kman_host.protocol: the CRC variant, frame encode/parse round trips, resynchronisation after garbage or corruption, chunk-boundary handling and session-header/DATA decoding. These tests pin the byte-level contract the firmware and every host consumer (logger, replay, live viewer) depend on. They use only hand-built byte strings, so they run without the simulator or a compiler.
- **`host/tests/test_stream.py`**: Tests for kman_host.stream.StreamDecoder and the Parquet session round trip, driven by the simulator (kman_host.simulator). They check timestamp/sequence unwrapping across 32-bit and 16-bit wraps, gap accounting under frame loss and corruption, live and replayed label alignment, segment handling on device restart, layout-change detection, and that SessionWriter + load_session preserve every row, header slot and label event. The decode/write loop in test_session_roundtrip follows the same pattern as host/logger.py Recorder.feed and host/sim_device.py write_session.
- **`host/tests/test_crosscheck.py`**: Cross-language golden-vector test. A module fixture compiles firmware/test/native_frames.cpp together with the firmware's real firmware/src/protocol.cpp and json_writer.h using the host C++ compiler, runs it, and parses its stdout with the Python FrameParser. The tests then check that the Python decoder reads the C++ DATA, STATUS and JSON frames field for field, that JsonWriter overflow detection works, and that the Python encoder gives the same bytes as the C++ encoder. If no g++/clang++ is found, all of them are skipped.
- **`analysis/tests/test_analysis.py`**: End-to-end acceptance tests for the offline analysis (kman_analysis), checked against the simulator's ground truth. Simulated sessions go through the full host path (StreamDecoder, then SessionWriter to Parquet, then load_session) and then through analyze(). The tests check breathing rate and breath detection, breath-hold gaps, swallow vs head-movement classification (flex only and with a neck IMU), chest-abdomen phase in normal vs obstructed breathing, motion-artifact flagging, robustness to frame loss, masking of long gaps, the complementary filter in isolation, and config key validation.
- **`pyproject.toml`**: Packaging and test configuration for the whole Python side. It declares the 'kman' distribution, its runtime dependencies and optional extras. It tells setuptools to find the kman_host and kman_analysis packages under host/ and analysis/, and tells pytest to put both directories on sys.path and collect tests from host/tests and analysis/tests, so `python -m pytest` at the repo root runs all 28 tests without installing the package.

**How it connects.** The tests are run by pytest, which reads pyproject.toml's [tool.pytest.ini_options]: pythonpath puts host/ and analysis/ on sys.path and testpaths points at host/tests and analysis/tests. No production code calls them. test_protocol.py calls kman_host.protocol directly: crc16, encode_frame, FrameParser.feed with ParserStats, SessionHeader.from_json, data_dtype and slot_present, DATA_PREFIX and decode_data. test_stream.py generates input with kman_host.simulator: generate, SimConfig, and SimSession.stream, label_events, header_frame, data_frame and header. It feeds that input to kman_host.stream.StreamDecoder (feed, schedule_labels, set_label, drain, pending_rows, new_header, headers, label_events_dicts, summary, stats, parser.stats, last_seq64) and checks for LayoutChanged. It writes and reads sessions with kman_host.session_io: SessionWriter.write_raw, set_header, write_batch and close, then load_session and Session.has_slot, labels and physical. Its decode-and-write loop is the same one used by host/logger.py Recorder.feed/close and host/sim_device.py write_session. test_crosscheck.py compiles firmware/test/native_frames.cpp against the firmware's firmware/src/protocol.cpp (proto::encodeFrame, proto::crc16, proto::encodeStatus, plus the putU8/putU16/putI16/putU32 helpers from protocol.h) and firmware/src/json_writer.h (JsonWriter: kv, kvSci, kvHex, kvJoined, item, beginObject/beginArray, ok). It decodes the output with FrameParser, DATA_PREFIX and decode_status, and compares it with encode_frame. It is the only place where the C++ firmware code and the Python host code are checked against each other. analysis/tests/test_analysis.py builds Parquet sessions through simulator, StreamDecoder, SessionWriter and load_session. It then calls kman_analysis.analyze (pipeline.analyze, which runs preprocess.to_uniform, calibration_mask, breathing.motion_energy_mg, analyze_breathing, phase.chest_abdomen_phase, swallow.analyze_neck, per_label_summary and quality) together with config.load_config/_merge, the same sequence analysis/report.py main runs (load_config, load_session, analyze). It also unit-tests breathing.gravity_complementary on its own.

### `analysis/tests/test_analysis.py`

#### `_session`
*helper* · [`analysis/tests/test_analysis.py:16`](../analysis/tests/test_analysis.py#L16)

```python
def _session(tmp_path, name, cfg=None, **stream_kw)
```

**Purpose.** Produces a simulated recording through the same path the logger uses and loads it back as a Session, so the analysis tests run on exactly what a real recording would contain.

**How it works.**

1. s = sim.generate(cfg or sim.SimConfig()) with no scenario argument, so DEFAULT_SCENARIO is used: 15 segments totalling 440 s (44,000 samples at 100 Hz) with labels 1,8,1,4,1,5,1,3,2,6,1,7,1,9,1.
2. dec = StreamDecoder(); dec.schedule_labels(s.label_events()).
3. w = SessionWriter(tmp_path / f"{name}.parquet", raw=False). No .bin is written; the empty .labels.jsonl sidecar is still created.
4. For each frame from s.stream(**stream_kw): dec.feed(fr, 0.0); if dec.new_header, call w.set_header(dec.header) and clear the flag.
5. After the loop, one w.write_batch(dec.drain()) with all rows. That is at least the default row_group_rows of 3000, so it flushes immediately.
6. w.close(dec.headers, dec.label_events_dicts(), dec.summary()), so stream and parser stats (dropped, crc_errors, segments) go into the metadata that quality() reads.
7. Returns (s, load_session(path)).

**Notes.** stream_kw (drop_prob, corrupt_prob, seed) is passed through to SimSession.stream, so the loaded session contains the induced losses.

#### `abd`
*fixture* · [`analysis/tests/test_analysis.py:32`](../analysis/tests/test_analysis.py#L32)

```python
@pytest.fixture(scope="module") def abd(tmp_path_factory)
```

**Purpose.** Runs the 440-s simulated session with an abdomen IMU through the full pipeline once and shares the result with five tests.

**How it works.** s, sess = _session(tmp_path_factory.mktemp("abd"), "abd", sim.SimConfig(abdomen=True)). Returns (s, analyze(sess, load_config())): the SimSession (ground truth: labels, truth["resp"], truth["swallow_times"], truth["head_times"], cfg) and the Analysis (summary DataFrame, chest and abd Breathing, phase, neck events, qa).

**Notes.** Module scope avoids rerunning simulation and analysis for each test. The decorator is on line 31. Here the chest waveform sign is flipped by orient_inspiration_up (qa chest_sign_flipped True) and the abdomen polarity is not.

#### `row`
*helper* · [`analysis/tests/test_analysis.py:37`](../analysis/tests/test_analysis.py#L37)

```python
def row(a, label)
```

**Purpose.** Gets one label's row from the per-label summary table.

**How it works.** Returns a.summary.set_index("label").loc[label], a pandas Series. Its columns come from pipeline.per_label_summary: name, duration_s, artifact_pct, gap_pct, breaths, breaths_per_min, rate_median_bpm, amp_median_deg, max_breath_gap_s; phase_valid_pct, phase_mean_deg, paradox_pct (only when there is an abdomen IMU); swallows, swallows_per_10s, head_events, head_events_per_10s (only when there is a flex channel).

**Notes.** Raises KeyError if the label does not occur in the session.

#### `test_rest_breathing_rate_and_detection`
*test* · [`analysis/tests/test_analysis.py:41`](../analysis/tests/test_analysis.py#L41)

```python
def test_rest_breathing_rate_and_detection(abd)
```

**Purpose.** Checks that the chest breathing pipeline recovers the simulated resting rate and detects nearly every true breath during rest.

**How it works.**

1. Asserts row(a, 1)["rate_median_bpm"] == approx(14.0, abs=1.0). SimConfig.resp_rate_bpm is 14. The analysis value is the median of 60/interval over consecutive valid breaths (intervals of 15 s or less) whose rate_t falls in label 1; it is 13.95 here.
2. Ground truth: resp = s.truth["resp"], the simulated waveform amp × _resp_shape(phase). pk, _ = scipy.signal.find_peaks(resp, prominence=0.1 \* resp.std()). truth_t = pk / s.cfg.fs. rest = the truth_t values where s.labels[pk] == 1.
3. det = a.chest.breath_t[a.chest.breath_valid], the valid detected end-inspiration peaks in seconds.
4. matched = the mean over rest truth peaks x of (min |det - x| &lt; 1.0 s). Asserts matched > 0.95.

**Notes.** This checks sensitivity (recall) only; extra detections are not penalised. Truth times (sample index / fs) and detection times (uniform grid from the first sample) line up because this session lost no frames.

#### `test_rest_vs_breath_hold`
*test* · [`analysis/tests/test_analysis.py:53`](../analysis/tests/test_analysis.py#L53)

```python
def test_rest_vs_breath_hold(abd)
```

**Purpose.** Checks that a breath-hold shows up as a long gap with no breaths and a low breath count, clearly separated from rest breathing.

**How it works.**

rest = row(a, 1), hold = row(a, 8). Asserts:
- hold["max_breath_gap_s"] > 15. The breath-hold in DEFAULT_SCENARIO lasts 20 s with simulated amplitude 0; observed 19.95.
- rest["max_breath_gap_s"] &lt; 8 (observed 4.75).
- hold["breaths_per_min"] &lt; 5 &lt; 10 &lt; rest["breaths_per_min"] (observed 3.0 and 14.8).
max_breath_gap_s comes from pipeline._max_breath_gap: the longest interval between consecutive valid breaths inside each run of the label, counting the run's start and end as boundaries and skipping runs that are more than 50 % motion or gap. breaths_per_min = valid breaths in the label / clean minutes, where clean = label & ~motion & ~gap; it is NaN if there are fewer than 10 clean seconds.

#### `test_swallow_vs_head_movement`
*test* · [`analysis/tests/test_analysis.py:61`](../analysis/tests/test_analysis.py#L61)

```python
def test_swallow_vs_head_movement(abd)
```

**Purpose.** Checks that the flex channel alone separates swallows from head movements: dry swallows are found and are not called head events, head movements are found and are not called swallows, and detected swallow peaks sit on true swallows.

**How it works.**

dry = row(a, 4), head = row(a, 5).
1. n_truth = the number of s.truth["swallow_times"] whose sample index (time × fs, truncated) has label 4. This excludes the label-3 drinking swallows; it is 5 in the default scenario.
2. Asserts dry["swallows"] >= 0.8 \* n_truth and dry["head_events"] == 0.
3. Asserts head["head_events"] >= 0.8 \* len(s.truth["head_times"]) (8 simulated head movements) and head["swallows"] == 0.
4. ev = a.neck.events. sw = the peak_s values of rows with kind == "swallow" and label == 4. Asserts each is within 1.5 s of some true swallow time.

**Notes.** Simulated swallow bumps are 70 counts × U(0.8, 1.25), lasting 0.6–1.2 s. Head movements are 400–1200 counts, as 1.5–3 s plateau bumps. sigma_cal is about 6 counts (qa flex_sigma_cal_counts 5.93) and head_amplitude_k is 25, so the head_motion threshold is about 148 counts: swallows stay below it and head movements exceed it (analyze_neck's amplitude rule). truth swallow_times are bump start times (ks / fs); the cosine bump peaks at mid-duration, 0.3–0.6 s later, which is inside the 1.5 s tolerance. per_label_summary assigns each event to a label by its peak sample.

#### `test_normal_vs_obstruction_phase`
*test* · [`analysis/tests/test_analysis.py:77`](../analysis/tests/test_analysis.py#L77)

```python
def test_normal_vs_obstruction_phase(abd)
```

**Purpose.** Checks the obstruction signature: chest and abdomen move roughly in phase during rest and in anti-phase (paradoxical effort) during simulated obstruction.

**How it works.**

rest = row(a, 1), obs = row(a, 9). Asserts abs(rest["phase_mean_deg"]) &lt; 30 and rest["paradox_pct"] &lt; 5, and abs(obs["phase_mean_deg"]) > 150 and obs["paradox_pct"] > 80.
phase_mean_deg is the circular mean (pipeline._circmean_deg) of Phase.phase_deg over the label's valid samples. paradox_pct = 100 × count(label & paradox) / count(label & valid), where paradox = valid & |phase| > phase.paradox_deg (120°). Observed: rest −10.7° and 0 %; obstruction 169.7° and 100 %.

**Notes.** The simulator builds the abdomen waveform as amp × _resp_shape(phase + 0.03) × 1.2 × (1 − 2·paradox), where paradox is the 1-s-smoothed indicator of label 9. So the abdomen normally leads by 0.03 cycle and is inverted during obstruction. chest_abdomen_phase calibrates abdomen polarity on label-1 data, flipping it if the correlation is negative.

#### `test_motion_artifact_flag`
*test* · [`analysis/tests/test_analysis.py:84`](../analysis/tests/test_analysis.py#L84)

```python
def test_motion_artifact_flag(abd)
```

**Purpose.** Checks that the motion-artifact mask flags walking almost entirely, leaves rest nearly untouched, and does not treat talking as motion.

**How it works.** Asserts row(a, 6)["artifact_pct"] > 90 (walking; observed 100), row(a, 1)["artifact_pct"] &lt; 5 (rest; observed 0.57) and row(a, 2)["artifact_pct"] &lt; 20 (talking; observed 3.8). artifact_pct is 100 × the mean of the motion mask over the label's samples. motion = motion_energy_mg(chest accel) > motion.threshold_mg (15). motion_energy_mg is the square root of a centred 2-s rolling mean of the summed squared accel after a 4th-order 1.5 Hz high-pass, times 1000.

**Notes.** Simulated walking adds 0.06–0.25 g accelerations at 0.9, 1.8 and 3.6 Hz. Simulated talking adds 5–15 Hz band noise at 4 mg rms per axis (about 7 mg total), which is below the 15 mg threshold. The test's comment reads "talking is not motion".

#### `test_robust_to_frame_loss`
*test* · [`analysis/tests/test_analysis.py:91`](../analysis/tests/test_analysis.py#L91)

```python
def test_robust_to_frame_loss(tmp_path)
```

**Purpose.** Checks that realistic frame loss and corruption do not change the breathing results.

**How it works.**

s, sess = _session(tmp_path, "lossy", drop_prob=0.01, corrupt_prob=0.002, seed=4). This uses the default SimConfig (chest IMU and flex, no abdomen); 1 % of frames are dropped and 0.2 % corrupted, which become CRC failures. a = analyze(sess, load_config()). Asserts:
- a.qa["lost_pct"] > 0.5. lost_pct is 100 × (u.n − len(df)) / u.n, a percentage; observed 1.15 % (508 frames lost, 88 CRC errors).
- row(a, 1)["rate_median_bpm"] == approx(14.0, abs=1.0) (observed 13.95).
- row(a, 8)["max_breath_gap_s"] > 15 (observed 19.95).

**Notes.** The first assertion confirms that loss actually happened. Isolated lost frames are shorter than preprocess.max_interp_gap_s (0.5 s), so preprocess.to_uniform interpolates them linearly without masking, and breath detection continues through them.

#### `test_long_gap_is_masked`
*test* · [`analysis/tests/test_analysis.py:99`](../analysis/tests/test_analysis.py#L99)

```python
def test_long_gap_is_masked(tmp_path)
```

**Purpose.** Checks that a data outage longer than max_interp_gap_s is marked as a gap and that no valid breath is reported from the interpolated stretch.

**How it works.**

1. s = sim.generate(sim.SimConfig()). The decoder and SessionWriter(raw=False) are set up inline, the same way as in _session.
2. While iterating s.stream(), skips every item with 3000 &lt;= i &lt; 3300. That removes data frames 3000–3299 (30.00–32.99 s, inside the first 60-s rest segment) and the STATUS frames yielded with i = 3099, 3199 and 3299.
3. Writes, closes and loads the session, then a = analyze(...).
4. Asserts a.u.gap[3000:3300].all(). The 300-sample hole exceeds max_gap = round(0.5 s × 100) = 50 samples, so preprocess.to_uniform marks it.
5. Asserts not a.u.gap[:2990].any(): nothing before the hole is marked.
6. bt = a.chest.breath_t; inside = (bt >= 30) & (bt &lt; 33). Asserts not a.chest.breath_valid[inside].any(). detect_breaths marks a breath invalid when invalid = motion | gap at its decimated sample.

**Notes.** The hole is still filled by linear interpolation, so filtfilt and PCA stay stable; the test checks only that nothing in the filled stretch counts as a valid breath. If no breath is detected between 30 and 33 s, the last assertion passes trivially. The uniform grid index is seq − first seq, so index 3000 is t = 30 s.

#### `test_neck_imu_flags_head_motion`
*test* · [`analysis/tests/test_analysis.py:120`](../analysis/tests/test_analysis.py#L120)

```python
def test_neck_imu_flags_head_motion(tmp_path)
```

**Purpose.** Checks that the neck-IMU rule on its own classifies head movements as head_motion and leaves dry swallows as swallows.

**How it works.**

1. s, sess = _session(tmp_path, "neck", sim.SimConfig(neck_imu=True)). The header gains a neck_imu slot (bit 3) with neck_ax..neck_gz. Simulated neck pitch is head_pitch (35° × y / 1200 during head movements, i.e. 11.7–35°; −15° during drinking) plus a 0.2° sway at 0.25 Hz.
2. a = analyze(sess, load_config(overrides={"swallow": {"head_amplitude_k": 1e9, "max_duration_s": 1e9}})). These overrides disable analyze_neck's amplitude and duration rules, so only the neck-IMU rule can produce "head_motion": swallow.head_pose_change finds tilt > 8° or yaw > 10° over [start − 0.5 s, end + 0.5 s].
3. ev = a.neck.events. For head = ev[ev["label"] == 5], asserts (head["kind"] == "head_motion").mean() > 0.8 and head["reason"].str.startswith("neck imu").all().
4. For dry = ev[ev["label"] == 4], asserts (dry["kind"] == "swallow").all().

**Notes.** The reason check is stricter than the 0.8 fraction check. Swallow events have reason "", so it requires every label-5 event to have been reclassified by the IMU. If label 5 had no events, .mean() would be NaN and the test would fail. If label 4 had no events, .all() on the empty Series would be True.

#### `test_complementary_filter_tracks_rotation`
*test* · [`analysis/tests/test_analysis.py:131`](../analysis/tests/test_analysis.py#L131)

```python
def test_complementary_filter_tracks_rotation()
```

**Purpose.** Unit test of breathing.gravity_complementary on a synthetic rotation with known truth: it must track the tilt closely and reject accelerometer noise better than raw accelerometer tilt.

**How it works.**

1. fs = 100.0; t = np.arange(0, 20, 1/fs), which is 2000 samples. th = radians(5) × sin(2π · 0.3 · t), a body rotation about x.
2. up = [0, 1, 0]; g = [0, cos(−th), sin(−th)], the gravity direction rotated by −th. acc = g + N(0, 0.01) g per axis (np.random.default_rng(0)). gyr = [degrees(np.gradient(th) × fs), 0, 0] dps, with no noise and no bias.
3. est = gravity_complementary(acc, gyr, fs, tau_s=1.0), so alpha = 1 / (1 + 0.01) ≈ 0.990.
4. err = degrees(|atan2(est[:,2], est[:,1]) − (−th)|) from sample 200 on. acc_err is the same quantity computed from raw acc. The first 2 s are skipped while the filter, which starts from the first noisy accel sample, converges.
5. Asserts err.max() &lt; 0.5 and err.std() &lt; 0.3 × acc_err.std().

**Notes.** This also confirms that the prediction step g_pred = g − (ω·dt) × g uses the same sign convention as simulator.generate, where body rotation +tilt rotates the gravity vector by −tilt.

#### `test_config_rejects_unknown_keys`
*test* · [`analysis/tests/test_analysis.py:147`](../analysis/tests/test_analysis.py#L147)

```python
def test_config_rejects_unknown_keys()
```

**Purpose.** Checks that a misspelled config key raises an error instead of being silently ignored.

**How it works.** Calls load_config(overrides={"swallow": {"k_onn": 3}}) inside pytest.raises(KeyError). load_config reads analysis/config.yaml; config._merge recurses into the "swallow" dict (both base and override are dicts) and raises KeyError("unknown config key: k_onn") because that key is not in the defaults.

**Notes.** The same _merge path handles user files passed to analysis/report.py --config, so typos there are rejected too.

### `host/tests/test_crosscheck.py`

#### `FW / CXX (test_crosscheck.py)`
*constant* · [`host/tests/test_crosscheck.py:12`](../host/tests/test_crosscheck.py#L12)

```python
FW = Path(__file__).resolve().parents[2] / "firmware"; CXX = shutil.which("g++") or shutil.which("clang++")
```

**Purpose.** Locate the firmware sources and a native C++ compiler for the cross-check build.

**How it works.** FW goes two directories up from host/tests/test_crosscheck.py to the repo root and appends firmware/. CXX is the path of g++, or else clang++, or None if neither is on PATH.

#### `frames`
*fixture* · [`host/tests/test_crosscheck.py:17`](../host/tests/test_crosscheck.py#L17)

```python
@pytest.fixture(scope="module") def frames(tmp_path_factory)
```

**Purpose.** Builds and runs the golden-vector generator firmware/test/native_frames.cpp, linked against the firmware's own protocol.cpp and json_writer.h, and returns the frames it prints as parsed by the Python FrameParser. All five tests in the module share it.

**How it works.**

1. If CXX is None, calls pytest.skip("no native C++ compiler"), so every test using the fixture is skipped.
2. exe = tmp_path_factory.mktemp("native") / "native_frames".
3. subprocess.run([CXX, "-std=c++11", "-Wall", "-Wextra", "-Werror", f"-I{FW/'src'}", FW/test/native_frames.cpp, FW/src/protocol.cpp, "-o", exe], check=True).
4. out = subprocess.run([exe], check=True, capture_output=True).stdout, the binary frame stream (282 bytes).
5. parser = P.FrameParser(); got = parser.feed(out); asserts parser.stats.crc_errors == 0; returns got, a list of 4 Frames in emission order.

**Notes.**

native_frames.cpp main() emits, in order:
[0] a DATA frame built with proto::putU32/putU16/putU8/putI16;
[1] a STATUS frame from proto::encodeStatus;
[2] a TYPE_HEADER frame whose body is JSON from JsonWriter (main returns 1 if !w.ok());
[3] a LOG frame containing "overflow-detected" or "overflow-not-detected", from an 8-byte JsonWriter.
All frames go through proto::encodeFrame. A compile error, any warning (because of -Werror) or a nonzero exit raises CalledProcessError, so the tests error rather than skip. The decorator is on line 16.

#### `test_data_frame`
*test* · [`host/tests/test_crosscheck.py:33`](../host/tests/test_crosscheck.py#L33)

```python
def test_data_frame(frames)
```

**Purpose.** Checks that the firmware's DATA layout and its little-endian put\* helpers decode correctly with Python's struct and numpy, including large u32/u16 values and the int16 extremes.

**How it works.** f = frames[0]. Asserts f.type == P.TYPE_DATA. Unpacks P.DATA_PREFIX ("&lt;IHB") from f.body and asserts (t, seq, valid) == (0xFFFFFF00, 0xFFFE, 0x05). Decodes f.body[7:] with np.frombuffer(..., "&lt;i2") and asserts the list equals [-32768, 32767, 16393, -1, 0, 1234, 8191], the array ch[7] in native_frames.cpp.

**Notes.** The length field on the wire is 0x0016 = 22: the type byte, the 7-byte prefix and 14 channel bytes. numpy is imported inside the test body.

#### `test_status_frame`
*test* · [`host/tests/test_crosscheck.py:44`](../host/tests/test_crosscheck.py#L44)

```python
def test_status_frame(frames)
```

**Purpose.** Checks that the C++ proto::encodeStatus layout matches the Python _STATUS struct and decode_status field for field.

**How it works.**

st = P.decode_status(frames[1].body). Asserts st is not None; decode_status returns None unless len(body) == _STATUS.size and body[0] == 1 (the version byte). _STATUS.size is 42 for "&lt;B5I4IHHB", matching kStatusBodyLen = 1 + 5\*4 + 4\*4 + 2 + 2 + 1. Then asserts:
- st.t_us == 123456789 and st.ticks == 4000000000 (a value above 2^31, which checks unsigned decoding);
- (frames_sent, overruns, tx_drops) == (42, 3, 7);
- read_errors == (1, 0, 0, 9);
- (max_latency_us, tx_high_water) == (650, 280);
- st.hfxo is True (flags bit 0, kStatusFlagHfxo) and st.sensor_error is False (bit 1).

#### `test_json_writer`
*test* · [`host/tests/test_crosscheck.py:54`](../host/tests/test_crosscheck.py#L54)

```python
def test_json_writer(frames)
```

**Purpose.** Checks that the firmware's heap-free JsonWriter, used to build the session header, produces valid JSON that Python parses to the intended values.

**How it works.**

d = json.loads(frames[2].body). The C++ program produced {"type":"kman_session_header","quote":"a\"b\\c\u000a","scale":61e-6,"lpf":208e-1,"int":7,"addr":"0x6A","neg":-5,"big":4000000000,"flag":true,"list":["x",{"name":"chest_ax"}]}. Asserts:
- d["quote"] == 'a"b\\c\n'. JsonWriter::esc backslash-escapes " and \, and writes control characters (&lt; 0x20) as \u00XX.
- d["scale"] ≈ 61e-6 and d["lpf"] ≈ 20.8. kvSci writes mantissa e exponent because newlib-nano printf has no %f.
- d["int"] == 7. kvSci with exp10 == 0 prints a plain integer.
- d["addr"] == "0x6A". kvHex writes 2 uppercase hex digits in quotes.
- d["neg"] == -5 (the long overload), d["big"] == 4000000000 (the unsigned long overload), d["flag"] is True.
- d["list"] == ["x", {"name": "chest_ax"}]. This uses beginArray, item, a nested beginObject, and kvJoined joining "chest" and "ax" with '_'.

**Notes.** json.loads succeeding also confirms comma placement (the first_[depth] flags) and bracket nesting. The test does not call SessionHeader.from_json, because this JSON has no channels list.

#### `test_json_overflow_detected`
*test* · [`host/tests/test_crosscheck.py:65`](../host/tests/test_crosscheck.py#L65)

```python
def test_json_overflow_detected(frames)
```

**Purpose.** Checks that JsonWriter reports a buffer overflow through ok() instead of silently producing truncated JSON.

**How it works.** Asserts frames[3].body == b"overflow-detected". In native_frames.cpp a JsonWriter over char tiny[8] writes '{' and then kv("too", "long for the buffer"). JsonWriter::raw refuses any write where len_ + n + 1 > cap_ and sets overflow_. Here that first happens at the value's opening quote, after `{"too":` (7 bytes). So t.ok() returns false and the program emits the LOG frame "overflow-detected".

#### `test_python_encoder_matches_cpp`
*test* · [`host/tests/test_crosscheck.py:69`](../host/tests/test_crosscheck.py#L69)

```python
def test_python_encoder_matches_cpp(frames)
```

**Purpose.** Checks that the Python frame encoder produces exactly the same bytes as the firmware encoder for every frame type the C++ program emits.

**How it works.** For each Frame f in the fixture, asserts P.encode_frame(f.type, f.body) == f.raw. The Python side builds SYNC + struct.pack("&lt;HB", len(body)+1, type) + body + the CRC from binascii.crc_hqx. The C++ side uses proto::encodeFrame with its bitwise CRC-16 loop. The four bodies are 21, 42, 174 and 17 bytes long.

### `host/tests/test_protocol.py`

#### `test_crc_reference_vector`
*test* · [`host/tests/test_protocol.py:8`](../host/tests/test_protocol.py#L8)

```python
def test_crc_reference_vector()
```

**Purpose.** Pins P.crc16 to CRC-16/CCITT-FALSE, the CRC the firmware uses, by checking the variant's standard check value.

**How it works.** Calls P.crc16(b"123456789") with the default init (crc=0xFFFF) and asserts the result is 0x29B1. 0x29B1 is the published check value for CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no xorout). P.crc16 just returns binascii.crc_hqx(data, crc), so the test fails if the init value changes or the function is swapped for another CRC.

**Notes.** The firmware CRC (proto::crc16, a bitwise loop in firmware/src/protocol.cpp) is checked indirectly by test_python_encoder_matches_cpp, which needs byte-identical CRC fields.

#### `test_roundtrip_and_split_chunks`
*test* · [`host/tests/test_protocol.py:13`](../host/tests/test_protocol.py#L13)

```python
def test_roundtrip_and_split_chunks()
```

**Purpose.** Checks that frames from P.encode_frame come back unchanged from P.FrameParser when the input arrives one byte at a time, which is the worst case for chunking.

**How it works.**

1. Setup: builds three DATA frames with P.encode_frame(P.TYPE_DATA, bytes(range(n))) for body lengths n = 1, 7, 30, and concatenates them into one stream.
2. Action: creates a fresh FrameParser and calls p.feed(bytes([b])) once per byte, collecting the returned Frame lists in `got`.
3. Assertions: [f.raw for f in got] equals the three encoded frames in order; p.stats.crc_errors == 0 and p.stats.bytes_discarded == 0.

**Notes.** Feeding one byte at a time exercises the partial-frame exits in FrameParser.feed: `n - pos < 5` (header not complete) and `end > n` (body not complete) both break out and keep the buffer. When only the first sync byte has arrived, the trailing 0xAA is kept (keep_from = n - 1) rather than counted as discarded, which is why bytes_discarded must stay 0.

#### `test_resync_after_garbage_and_corruption`
*test* · [`host/tests/test_protocol.py:24`](../host/tests/test_protocol.py#L24)

```python
def test_resync_after_garbage_and_corruption()
```

**Purpose.** Checks that the parser skips leading garbage, rejects a CRC-corrupted frame and a false sync with an impossible length, and still returns every valid frame after them.

**How it works.**

1. good = P.encode_frame(P.TYPE_DATA, b"\x01\x02\x03"), which is AA 55 04 00 01 01 02 03 34 1F. bad = a copy with bad[6] ^= 0x40: the second body byte goes from 0x02 to 0x42 while the CRC field is unchanged, so the CRC no longer matches.
2. stream = b"\x00\xaa\x13garbage\xaa" + bad + good + b"\xaa\x55\xff\xff" + good. The 11 junk bytes contain 0xAA bytes that are not followed by 0x55; the last junk 0xAA plus bad's AA 55 gives AA AA 55. AA 55 FF FF is a sync followed by length 0xFFFF, which is above MAX_LEN 4096.
3. Action: one p.feed(stream) call on a fresh FrameParser.
4. Assertions: the returned raws == [good, good]; p.stats.crc_errors == 1 (the bad frame); p.stats.bad_length == 1 (the AA 55 FF FF false header).

**Notes.** This exercises the 'advance one byte past the false sync and search again' rule (pos += 1 after a length or CRC failure): neither the corrupted frame nor the false header uses up the valid frame that follows it. bytes_discarded is not asserted; it comes to 23 (11 junk bytes, plus 9 and 3 bytes skipped after the two false syncs).

#### `test_chunking_invariance_when_frame_ends_in_0xaa`
*test* · [`host/tests/test_protocol.py:36`](../host/tests/test_protocol.py#L36)

```python
def test_chunking_invariance_when_frame_ends_in_0xaa()
```

**Purpose.** Regression test for the parser's tail-retention rule: a frame whose last wire byte is 0xAA must not have that byte kept and recounted when a chunk boundary falls right after it.

**How it works.**

1. Brute-forces the first 2-byte body bytes([i, j]) (i, j in 0..255) for which P.encode_frame(1, body)[-1] == 0xAA, i.e. the CRC high byte is 0xAA. With the current CRC this is body 00 17, giving the frame AA 55 03 00 01 00 17 38 AA.
2. fr = encode_frame(TYPE_DATA, body). stream = fr + encode_frame(TYPE_DATA, b"\x55\x01") + fr: three 9-byte frames, 27 bytes, so the stream also ends in 0xAA.
3. For chunk in (1, 2, 3, len(stream)): a fresh FrameParser is fed stream[i:i+chunk] slices in order.
4. Assertions for each chunk size: len(got) == 3, and p.stats.bytes_discarded == 0 and p.stats.crc_errors == 0 (chunk is the assertion message).

**Notes.** The code under test is `keep_from = n - 1 if n - 1 >= pos and buf[-1] == 0xAA else n` in FrameParser.feed (comment at protocol.py line 90). With chunk sizes 1 and 3, a chunk boundary falls exactly after the first frame's trailing 0xAA, so pos == n when the search fails. With chunk size 2 the boundaries fall on even offsets, so none comes right after a 0xAA-terminated frame except at the end of the stream. The `n - 1 >= pos` guard stops the parser from keeping that already-consumed byte. Without the guard the consumed 0xAA would stay in the buffer. With chunk 3, the next feed would then see AA AA 55 03, find the sync at offset 1 and count one discarded byte. With chunk 1, the next feed would see AA AA, find no sync, keep the trailing 0xAA and count the first one as discarded. Either way bytes_discarded would become nonzero and the assertion would fail.

#### `test_sync_bytes_inside_payload`
*test* · [`host/tests/test_protocol.py:50`](../host/tests/test_protocol.py#L50)

```python
def test_sync_bytes_inside_payload()
```

**Purpose.** Checks that sync patterns (AA 55) inside a frame body do not confuse the parser, which matters because the protocol uses no byte stuffing.

**How it works.** body = b"\xaa\x55\xaa\x55" \* 4 (16 bytes of sync pattern). fr = P.encode_frame(P.TYPE_LOG, body). Feeds fr + fr to a fresh FrameParser in one call and asserts [f.body for f in ...] == [body, body].

**Notes.** This works because the parser finds the real sync at the frame start, validates length and CRC from there, and after a valid frame sets pos = end. It never looks for sync inside a frame it has accepted.

#### `test_header_dtype`
*test* · [`host/tests/test_protocol.py:57`](../host/tests/test_protocol.py#L57)

```python
def test_header_dtype()
```

**Purpose.** Checks SessionHeader.from_json, SessionHeader.data_dtype, decode_data and SessionHeader.slot_present on a minimal hand-written header.

**How it works.**

1. Builds a header dict: type "kman_session_header", sample_rate_hz 100, period_us 10000, present_mask 5, sensors [{slot chest_imu, status ok}], and channels chest_ax (slot chest_imu, bit 0, unit g, scale 61e-6) and flex_raw (slot neck_flex, bit 2, unit counts, scale 1).
2. h = P.SessionHeader.from_json(json.dumps(hdr)); dt = h.data_dtype().
3. Asserts dt.itemsize == 7 + 4: the packed prefix t_us &lt;u4, seq &lt;u2, valid u1 plus two &lt;i2 channels.
4. body = P.DATA_PREFIX.pack(5, 6, 5) (t_us 5, seq 6, valid 5) + np.array([-100, 9000], "&lt;i2").tobytes(). rec = P.decode_data(body, dt); asserts rec["chest_ax"][0] == -100 and rec["flex_raw"][0] == 9000.
5. Asserts P.decode_data(body + b"\x00", dt) is None (length != itemsize).
6. Asserts h.slot_present("chest_imu") is True (status "ok") and h.slot_present("abdomen_imu") is False (that slot is missing from sensors, so slots().get returns {}).

### `host/tests/test_stream.py`

#### `SHORT (test_stream.py)`
*constant* · [`host/tests/test_stream.py:9`](../host/tests/test_stream.py#L9)

```python
SHORT = [(1, 6), (8, 4), (4, 6)]
```

**Purpose.** A short simulator scenario that keeps the stream tests fast.

**How it works.** A list of (label, seconds) pairs passed as the scenario argument to sim.generate: 6 s rest breathing (label 1), 4 s breath-hold (label 8), 6 s dry swallow (label 4). At the default fs = 100 Hz that is 1600 samples.

**Notes.** Only 6 s of label 4 means the simulator places no swallows: its loop starts 3 s into a segment and stops 3 s before the end. These tests do not need swallows.

#### `_decode`
*helper* · [`host/tests/test_stream.py:12`](../host/tests/test_stream.py#L12)

```python
def _decode(s, **stream_kw)
```

**Purpose.** Runs a whole simulated session through a fresh StreamDecoder the way replay does, and returns the decoder and all decoded rows.

**How it works.**

1. dec = StreamDecoder().
2. dec.schedule_labels(s.label_events()), so the simulator's label changes are applied when their seq is reached.
3. For each (index, frame) from s.stream(**stream_kw), calls dec.feed(fr, 0.0) with host time 0.0. s.stream yields the header frame first, then the data frames, plus a STATUS frame after every 100th data frame; stream_kw can be drop_prob, corrupt_prob or seed.
4. Returns (dec, dec.drain()). The drain is a dict of column arrays (t_us, seq, segment, valid, one per channel, label, host_time), or None if no rows were decoded.

#### `test_wrap_and_gaps_are_exact`
*test* · [`host/tests/test_stream.py:20`](../host/tests/test_stream.py#L20)

```python
def test_wrap_and_gaps_are_exact()
```

**Purpose.** Checks that 32-bit timestamp wrap, 16-bit sequence wrap and random frame loss produce exact int64 time and tick columns, and that payloads and labels land on the right ticks.

**How it works.**

1. Setup: cfg = sim.SimConfig(t0_us=2**32 - 2_000_000, seq0=65530). t32 wraps 2 s (200 samples) into the session and seq16 wraps after 6 samples. s = sim.generate(cfg, SHORT) gives 1600 samples.
2. Action: dec, b = _decode(s, drop_prob=0.01, seed=5), which drops about 1 % of DATA frames (19 with this seed).
3. Assertions:
   a. np.all(np.diff(b["t_us"]) > 0): unwrapped time strictly increases across the wrap.
   b. np.diff(t_us) == np.diff(seq) \* 10000: every time step equals the ticks advanced times period_us 10000, including steps across gaps.
   c. idx = b["seq"] - cfg.seq0 gives the original sample indices, and dec.stats.dropped == s.n - len(idx): every missing frame is counted as dropped.
   d. dec.stats.seq_mismatch == 0: at every step, the 16-bit seq delta equals the tick count derived from the timestamp, mod 2^16, including across the seq wrap.
   e. b["chest_gx"] equals s.raw["chest_gx"][idx]: payload values sit on the right ticks.
   f. b["label"] equals s.labels[idx]: scheduled label events (seq = seq0 + i, unwrapped: 65530, 66130, 66530) are applied at the right ticks.

**Notes.** Assertion (c) holds only because seed 5 drops neither the first nor the last sample (the first decoded seq is 65530 and the last is 67129). StreamStats.dropped counts only missing ticks between received frames. The first received frame sets t64 = t32 and seq64 = seq16 (StreamDecoder._on_data); after that, seq64 only increases and never wraps.

#### `test_corruption_costs_only_the_corrupted_frame`
*test* · [`host/tests/test_stream.py:33`](../host/tests/test_stream.py#L33)

```python
def test_corruption_costs_only_the_corrupted_frame()
```

**Purpose.** Checks that a corrupted frame costs exactly one sample: resync must not lose the good frame after it, and must not produce extra CRC errors from false syncs.

**How it works.**

1. s = sim.generate(sim.SimConfig(), SHORT).
2. dec, b = _decode(s, corrupt_prob=0.01, seed=2). In SimSession.stream, about 1 % of DATA frames get one byte, at a random index in [5, len), XORed with 0xFF. That byte is in the body or the CRC (sync, length and type are untouched), so each corrupted frame fails its CRC.
3. Assertions: dec.parser.stats.crc_errors > 0 (16 with this seed); dec.stats.dropped == dec.parser.stats.crc_errors, meaning each CRC failure shows up as exactly one missing tick in the decoded time series; len(b["t_us"]) + dec.stats.dropped == s.n, meaning every other frame was decoded.

**Notes.** Only DATA frames are corrupted; STATUS frames never are. parser.stats.bytes_discarded is nonzero here (the skipped rest of each bad frame) but is not asserted. Equality between dropped and crc_errors would break if a false sync inside a corrupted frame produced a second CRC error, or if resync swallowed the next frame.

#### `test_live_label_applies_from_next_frame`
*test* · [`host/tests/test_stream.py:41`](../host/tests/test_stream.py#L41)

```python
def test_live_label_applies_from_next_frame()
```

**Purpose.** Checks the live-labelling rule: a label set with StreamDecoder.set_label is stamped with the next tick and applies from the next decoded frame.

**How it works.**

1. s = sim.generate(sim.SimConfig(), SHORT). dec = StreamDecoder() with no scheduled labels, so the label starts at 0.
2. stream = list(s.stream()). Feeds stream[:51]: the header plus data frames 0..49 (the first STATUS frame comes after sample 99).
3. ev = dec.set_label(4); asserts ev.seq == dec.last_seq64 + 1 (here 150).
4. Feeds stream[51:60] (data frames 50..58), then b = dec.drain().
5. Asserts (b["label"][:50] == 0).all() and (b["label"][50:] == 4).all().

**Notes.** This is the path used by host/logger.py Recorder.set_label. Replay reproduces the same label column by applying the stored event by seq through schedule_labels.

#### `test_frames_before_header_are_counted`
*test* · [`host/tests/test_stream.py:55`](../host/tests/test_stream.py#L55)

```python
def test_frames_before_header_are_counted()
```

**Purpose.** Checks that DATA frames arriving before any session header are counted and discarded instead of decoded.

**How it works.** Feeds s.data_frame(i) for i = 0..4 to a new StreamDecoder without sending s.header_frame() first. Asserts dec.stats.frames_before_header == 5 and dec.drain() is None.

**Notes.** Without a header there is no channel layout (dtype) to decode the payload with, so StreamDecoder._on_data increments the counter and returns without buffering a row.

#### `test_layout_change_raises`
*test* · [`host/tests/test_stream.py:63`](../host/tests/test_stream.py#L63)

```python
def test_layout_change_raises()
```

**Purpose.** Checks that a second header with a different channel layout raises LayoutChanged instead of being decoded silently with the wrong dtype.

**How it works.** s = sim.generate(sim.SimConfig(), SHORT) has 7 channels (chest_ax..chest_gz, flex_raw). s2 = sim.generate(sim.SimConfig(abdomen=True), SHORT) has 13 channels (it adds abd_ax..abd_gz). Feeds s.header_frame(), then s2.header_frame() inside pytest.raises(LayoutChanged). StreamDecoder._on_header raises because SessionHeader.layout_key() (the (name, scale) of each channel plus period_us) differs from the first header's.

#### `test_device_restart_starts_new_segment`
*test* · [`host/tests/test_stream.py:72`](../host/tests/test_stream.py#L72)

```python
def test_device_restart_starts_new_segment()
```

**Purpose.** Checks that a new header with a different stream_id (device rebooted or re-enumerated, so its clock restarted) starts a new segment and that the time axis is bridged using the gap measured on the host.

**How it works.**

1. Feeds s.header_frame() at host_time 100.0, then data frames 0..9 at host times 100.0 + i\*0.01 (the last is 100.09).
2. hdr2 = dict(s.header, stream_id=2). The simulator header has stream_id 1; device_id and channels are unchanged. hdr2 is encoded as a TYPE_HEADER frame (JSON via __import__("json"), because the module does not import json) and fed at host_time 102.0. In _on_header the layout_key matches but stream_id differs, so segment becomes 1, _last_t32 is reset to None and _seg_gap_host = 102.0 - 100.09 = 1.91 s.
3. Feeds data frames 0..4 again, with the same t32 and seq16 values as at the start (a restarted device clock), at host times 102.0 + i\*0.01.
4. b = dec.drain(). Asserts list(np.unique(b["segment"])) == [0, 1]; np.all(np.diff(b["t_us"]) > 0); gap = t_us[10] - t_us[9] satisfies 1.8e6 &lt; gap &lt; 2.2e6.
Mechanism checked: the first frame of the new segment gets nt = max(1, round(1.91e6 / 10000)) = 191 ticks, so t64 = last_t64 + 1,910,000 µs and seq64 = last_seq64 + 191.

**Notes.** Without the segment logic, the repeated t32 values would give dt = (t32 - last_t32) & 0xFFFFFFFF >= 0x80000000, and those frames would be counted as duplicates and dropped.

#### `test_session_roundtrip`
*test* · [`host/tests/test_stream.py:89`](../host/tests/test_stream.py#L89)

```python
def test_session_roundtrip(tmp_path)
```

**Purpose.** End-to-end check of the logger write path (decode, SessionWriter, Parquet plus .bin sidecar) and of reading it back with load_session and Session.physical.

**How it works.**

1. Setup: s = sim.generate(sim.SimConfig(abdomen=True), SHORT): 1600 samples, 13 channels, present_mask 7. dec = StreamDecoder() with dec.schedule_labels(s.label_events()). w = SessionWriter(tmp_path / "x.parquet", row_group_rows=200); raw=True by default, so x.bin and x.labels.jsonl are opened.
2. Loop over s.stream(): w.write_raw(fr); dec.feed(fr, 0.0); if dec.new_header, call w.set_header(dec.header) and set dec.new_header = False; if dec.pending_rows() >= 77, call w.write_batch(dec.drain()).
3. After the loop: w.write_batch(dec.drain()) for the last 60 rows, then w.close(dec.headers, dec.label_events_dicts(), dec.summary()).
4. ss = load_session(tmp_path / "x.parquet"). Assertions:
   - len(ss.df) == s.n: no rows lost or duplicated across 21 batches and several row groups.
   - ss.has_slot("abdomen_imu"): the header metadata survives the round trip.
   - [e["label"] for e in ss.labels] == [1, 8, 4]: label events are stored in the kman.labels metadata.
   - ss.physical()["chest_ay"].abs().mean() == approx(0.97, abs=0.02): int16 counts times scale 61e-6 give g. The simulator's chest gravity direction [0, 0.97, 0.243], normalised, has a y component of about 0.970.
   - (tmp_path / "x.bin").stat().st_size > 0: the raw sidecar was written.

**Notes.** With 77-row batches and row_group_rows=200, SessionWriter.write_batch flushes each time pending rows reach 200, i.e. after 3 batches (231 rows). The last row group has 214 rows (2 × 77 + 60), so the file has several row groups of uneven size. The loop follows the same pattern as host/sim_device.py write_session and host/logger.py Recorder.feed/close.

### `pyproject.toml`

#### `pyproject.toml`
*config* · [`pyproject.toml:1`](../pyproject.toml#L1)

```
[build-system] / [project] / [tool.setuptools.packages.find] / [tool.pytest.ini_options]
```

**Purpose.** Declares the Python distribution, its dependencies and package layout, and configures pytest so that both test directories run from the repo root against the source tree.

**How it works.**

1. [build-system] (line 1): requires setuptools>=64, backend setuptools.build_meta.
2. [project] (line 5): name "kman", version 0.1.0, requires-python >=3.11. Runtime dependencies: numpy>=1.24, scipy>=1.10, pandas>=2.0, pyarrow>=14, pyserial>=3.5, matplotlib>=3.7, pyyaml>=6.0.
3. [project.optional-dependencies] (line 20): live = pyqtgraph>=0.13.3, PySide6>=6.5 (for the live viewer); dev = pytest>=7.
4. [tool.setuptools.packages.find] (line 24): where = ["host", "analysis"], include = ["kman_host\*", "kman_analysis\*"]. Only the two library packages are packaged; the top-level scripts (host/logger.py, live_view.py, sim_device.py, to_csv.py, analysis/report.py) are not.
5. [tool.pytest.ini_options] (line 28): pythonpath = ["host", "analysis"] puts both source roots on sys.path, so `from kman_host import ...` and `from kman_analysis import ...` work without installing. This also lets analysis/tests import kman_host (simulator, session_io, stream). testpaths = ["host/tests", "analysis/tests"] means a bare `python -m pytest` at the repo root collects all four test modules (28 tests).

**Notes.** No package data is declared. analysis/config.yaml sits outside the kman_analysis package directory, and load_config finds it through Path(__file__).resolve().parents[1] / "config.yaml". That resolves to analysis/config.yaml when running from the source tree (as pytest does with this pythonpath) or from an editable install. A regular non-editable install would not include the file at that location. The pytest pythonpath option needs pytest >= 7, which matches the dev extra.
