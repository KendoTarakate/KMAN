// KMAN Step 1: fixed-rate multi-sensor streamer.
//
// Timing model ("timer-master"): a hardware timer ticks at KMAN_SAMPLE_RATE_HZ
// on the crystal-derived 1 MHz timebase. Each tick wakes the loop task, which
// reads every present sensor once and queues one DATA frame stamped with the
// tick's scheduled time. seq is the tick counter, so ticks the loop could not
// serve show up on the host as sequence gaps (and as `overruns` in STATUS).
//
// No dynamic allocation anywhere; all buffers are static.
#include <Adafruit_TinyUSB.h>  // must be included in the sketch for the core to link USB CDC
#include <Arduino.h>
#include <Wire.h>

#include "../include/config.h"
#include "hal/hal.h"
#include "json_writer.h"
#include "protocol.h"
#include "ring_buffer.h"
#include "sensors/flex_adc.h"
#include "sensors/lsm6dsox.h"

namespace {

const uint32_t kPeriodUs = 1000000UL / KMAN_SAMPLE_RATE_HZ;

// ---------------------------------------------------------------- sensor slots

Lsm6dsox g_chestImu(Wire, KMAN_CHEST_IMU_ADDR, false, KMAN_SAMPLE_RATE_HZ, KMAN_IMU_ACCEL_RANGE_G,
                    KMAN_IMU_GYRO_RANGE_DPS);
Lsm6dsox g_abdomenImu(Wire, KMAN_ABDOMEN_IMU_ADDR, false, KMAN_SAMPLE_RATE_HZ, KMAN_IMU_ACCEL_RANGE_G,
                      KMAN_IMU_GYRO_RANGE_DPS);
FlexAdc g_neckFlex(KMAN_FLEX_PIN, KMAN_FLEX_SENSOR_DESC, KMAN_FLEX_R_FIXED_OHM, KMAN_FLEX_TOPOLOGY);
Lsm6dsox g_neckImu(Wire, KMAN_NECK_IMU_ADDR, KMAN_NECK_IMU_ACCEL_ONLY, KMAN_SAMPLE_RATE_HZ, KMAN_IMU_ACCEL_RANGE_G,
                   KMAN_IMU_GYRO_RANGE_DPS);

enum SlotState : uint8_t { kNotConfigured, kInitFailed, kOk };

struct Slot {
  const char* id;      // slot name in the session header
  const char* prefix;  // channel name prefix
  uint8_t bit;         // bit in present_mask / per-frame valid mask
  bool configured;
  Sensor* drv;
  SlotState state;
};

Slot g_slots[proto::kStatusSlots] = {
    {"chest_imu", "chest", 0, KMAN_CHEST_IMU != 0, &g_chestImu, kNotConfigured},
    {"abdomen_imu", "abd", 1, KMAN_ABDOMEN_IMU != 0, &g_abdomenImu, kNotConfigured},
    {"neck_flex", "flex", 2, KMAN_NECK_FLEX != 0, &g_neckFlex, kNotConfigured},
    {"neck_imu", "neck", 3, KMAN_NECK_IMU != 0, &g_neckImu, kNotConfigured},
};
const uint8_t kNumSlots = sizeof g_slots / sizeof g_slots[0];

uint8_t g_presentMask = 0;
bool g_sensorError = false;  // a configured sensor failed init

// ---------------------------------------------------------------- frame buffers

const size_t kMaxChannels = 6 + 6 + 1 + 6;
const size_t kDataHeaderLen = 4 + 2 + 1;  // t_us, seq, valid
uint8_t g_dataBody[kDataHeaderLen + 2 * kMaxChannels];
uint8_t g_frame[proto::kOverhead + kDataHeaderLen + 2 * kMaxChannels + proto::kStatusBodyLen];

char g_headerJson[4000];  // ~1.7 KB with chest+flex, ~3.1 KB with three IMUs + flex
uint8_t g_headerFrame[sizeof g_headerJson + proto::kOverhead];

ByteRing<KMAN_TX_RING_BYTES> g_tx;

// ---------------------------------------------------------------- run state

bool g_streaming = false;
bool g_headerPending = false;
bool g_statusPending = false;
uint32_t g_streamId = 0;
uint32_t g_lastTickCount = 0;
uint32_t g_lastStatusMs = 0;
proto::Status g_status;
uint16_t g_maxLatencyUs = 0;

// ---------------------------------------------------------------- sensors

void initSensors() {
  hal::i2cBusRecover(KMAN_I2C_SDA_PIN, KMAN_I2C_SCL_PIN);
  Wire.setPins(KMAN_I2C_SDA_PIN, KMAN_I2C_SCL_PIN);  // honour pin overrides from config.h
  Wire.begin();
  Wire.setClock(KMAN_I2C_HZ);

  for (uint8_t i = 0; i < kNumSlots; ++i) {
    Slot& s = g_slots[i];
    if (!s.configured) continue;
    if (s.drv->init()) {
      s.state = kOk;
      g_presentMask |= (uint8_t)(1u << s.bit);
    } else {
      s.state = kInitFailed;
      g_sensorError = true;
    }
  }
}

// ---------------------------------------------------------------- session header

size_t buildHeaderFrame() {
  JsonWriter w(g_headerJson, sizeof g_headerJson);
  char devId[24];
  hal::deviceId(devId, sizeof devId);

  w.beginObject();
  w.kv("type", "kman_session_header");
  w.kv("protocol", (int32_t)proto::kProtocolVersion);
  w.kv("fw_version", KMAN_FW_VERSION);
  w.kv("build", __DATE__ " " __TIME__);
  w.kv("board", KMAN_BOARD_NAME);
  w.kv("mcu", hal::mcuName());
  w.kv("device_id", devId);
  w.kvHex("boot_id", hal::bootId(), 8);
  w.kv("stream_id", g_streamId);
  w.kv("t_us_at_header", hal::nowUs());
  w.kv("sample_rate_hz", (int32_t)KMAN_SAMPLE_RATE_HZ);
  w.kv("period_us", kPeriodUs);

  w.beginObject("clock");
  hal::describeClock(w);
  w.endObject();

  w.beginObject("frame");
  w.kv("sync", "AA55");
  w.kv("len", "u16le, bytes of type+body");
  w.kv("crc", "CRC-16/CCITT-FALSE over len,type,body; u16le");
  w.beginArray("data_prefix");
  w.item("t_us:u32");
  w.item("seq:u16");
  w.item("valid:u8");
  w.endArray();
  w.kv("channel_type", "i16");
  w.endObject();

  w.kv("present_mask", (int32_t)g_presentMask);

  w.beginArray("sensors");
  for (uint8_t i = 0; i < kNumSlots; ++i) {
    const Slot& s = g_slots[i];
    w.beginObject();
    w.kv("slot", s.id);
    w.kv("bit", (int32_t)s.bit);
    w.kv("status", s.state == kOk ? "ok" : (s.state == kInitFailed ? "init_failed" : "not_configured"));
    if (s.configured) {
      s.drv->describeConfig(w);
      if (s.state == kInitFailed) w.kv("error", s.drv->lastError());
      if (s.drv->warning()[0]) w.kv("warning", s.drv->warning());
    }
    w.endObject();
  }
  w.endArray();

  // Payload channel order: present slots in slot order, each slot's channels in driver order.
  w.beginArray("channels");
  for (uint8_t i = 0; i < kNumSlots; ++i) {
    const Slot& s = g_slots[i];
    if (s.state != kOk) continue;
    for (uint8_t c = 0; c < s.drv->channelCount(); ++c) {
      const ChannelInfo& ch = s.drv->channel(c);
      w.beginObject();
      w.kvJoined("name", s.prefix, ch.suffix);
      w.kv("slot", s.id);
      w.kv("bit", (int32_t)s.bit);
      w.kv("unit", ch.unit);
      w.kvSci("scale", ch.scaleMantissa, ch.scaleExp10);
      w.endObject();
    }
  }
  w.endArray();
  w.endObject();

  if (!w.ok()) {
    // Never expected (buffer sized with margin); send a minimal, still-valid header.
    JsonWriter e(g_headerJson, sizeof g_headerJson);
    e.beginObject();
    e.kv("type", "kman_session_header");
    e.kv("error", "header overflow");
    e.endObject();
    return proto::encodeFrame(proto::kTypeHeader, (const uint8_t*)e.c_str(), e.length(), g_headerFrame,
                              sizeof g_headerFrame);
  }
  return proto::encodeFrame(proto::kTypeHeader, (const uint8_t*)w.c_str(), w.length(), g_headerFrame,
                            sizeof g_headerFrame);
}

// ---------------------------------------------------------------- per-tick sampling

void sampleAndQueue(uint32_t tickCount, uint32_t tickUs) {
  int16_t ch[kMaxChannels];
  uint8_t n = 0;
  uint8_t valid = 0;

  // Fastest read first (ADC ~0.2 ms, IMU burst ~0.35 ms at 400 kHz) so all
  // samples sit within ~1 ms of the tick.
  static const uint8_t kOrder[kNumSlots] = {2, 0, 1, 3};
  uint8_t offset[kNumSlots];
  {
    uint8_t o = 0;
    for (uint8_t i = 0; i < kNumSlots; ++i) {
      offset[i] = o;
      if (g_slots[i].state == kOk) o += g_slots[i].drv->channelCount();
    }
    n = o;
  }
  for (uint8_t k = 0; k < kNumSlots; ++k) {
    const uint8_t i = kOrder[k];
    Slot& s = g_slots[i];
    if (s.state != kOk) continue;
    if (s.drv->read(&ch[offset[i]])) {
      valid |= (uint8_t)(1u << s.bit);
    } else {
      // Keep the fixed layout; the cleared valid bit tells the host to discard these values.
      for (uint8_t c = 0; c < s.drv->channelCount(); ++c) ch[offset[i] + c] = 0;
      ++g_status.read_errors[i];
    }
  }

  uint8_t* p = g_dataBody;
  p = proto::putU32(p, tickUs);
  p = proto::putU16(p, (uint16_t)tickCount);
  p = proto::putU8(p, valid);
  for (uint8_t i = 0; i < n; ++i) p = proto::putI16(p, ch[i]);

  const size_t len = proto::encodeFrame(proto::kTypeData, g_dataBody, (size_t)(p - g_dataBody), g_frame, sizeof g_frame);
  if (g_tx.push(g_frame, len)) {
    ++g_status.frames_sent;
  } else {
    ++g_status.tx_drops;
  }

  const uint32_t latency = hal::nowUs() - tickUs;
  if (latency > g_maxLatencyUs) g_maxLatencyUs = latency > 0xFFFF ? 0xFFFF : (uint16_t)latency;
}

// ---------------------------------------------------------------- host link

void startStreaming() {
  ++g_streamId;
  g_tx.clear();
  g_streaming = true;
  g_headerPending = true;
  g_statusPending = true;
  g_status.frames_sent = 0;
  g_status.overruns = 0;
  g_status.tx_drops = 0;
  for (uint8_t i = 0; i < proto::kStatusSlots; ++i) g_status.read_errors[i] = 0;
  g_maxLatencyUs = 0;
  g_tx.resetHighWater();
}

void serviceHost() {
  const bool connected = hal::hostConnected();
  if (connected && !g_streaming) startStreaming();
  if (!connected && g_streaming) {
    g_streaming = false;
    g_tx.clear();
  }

  for (int c = hal::hostRead(); c >= 0; c = hal::hostRead()) {
    if (c == proto::kCmdSendHeader) g_headerPending = true;
    else if (c == proto::kCmdSendStatus) g_statusPending = true;
  }
}

void queueHeaderIfPending() {
  if (!g_headerPending || !g_streaming) return;
  const size_t len = buildHeaderFrame();
  if (len && g_tx.free() >= len) {
    g_tx.push(g_headerFrame, len);
    g_headerPending = false;
  }
}

void queueStatusIfDue() {
  if (!g_streaming) return;
  const uint32_t nowMs = millis();
  if (!g_statusPending && nowMs - g_lastStatusMs < KMAN_STATUS_PERIOD_MS) return;

  uint32_t tickUs;
  g_status.ticks = hal::latestTick(&tickUs);
  g_status.t_us = hal::nowUs();
  g_status.max_latency_us = g_maxLatencyUs;
  g_status.tx_high_water = g_tx.highWater() > 0xFFFF ? 0xFFFF : (uint16_t)g_tx.highWater();
  g_status.flags = (hal::clockIsCrystal() ? proto::kStatusFlagHfxo : 0) |
                   (g_sensorError ? proto::kStatusFlagSensorError : 0);

  uint8_t body[proto::kStatusBodyLen];
  const size_t bodyLen = proto::encodeStatus(g_status, body);
  const size_t len = proto::encodeFrame(proto::kTypeStatus, body, bodyLen, g_frame, sizeof g_frame);
  if (g_tx.push(g_frame, len)) {
    g_lastStatusMs = nowMs;
    g_statusPending = false;
    g_maxLatencyUs = 0;
    g_tx.resetHighWater();
  }
}

void pumpTx() {
  bool wrote = false;
  while (g_tx.used()) {
    const size_t room = hal::hostWritable();
    if (!room) break;
    const uint8_t* ptr;
    size_t n = g_tx.peekContiguous(&ptr);
    if (n > room) n = room;
    const size_t w = hal::hostWrite(ptr, n);
    g_tx.consume(w);
    wrote = true;
    if (w < n) break;
  }
  if (wrote) hal::hostFlush();
}

// ---------------------------------------------------------------- LED

void updateLeds() {
  const uint32_t ms = millis();
  // Solid ERR = a configured sensor failed init (streaming of the rest continues).
  hal::led(KMAN_LED_ERR_PIN, g_sensorError);
  bool ok;
  if (g_streaming) ok = (ms / 250) & 1;  // 2 Hz blink: streaming
  else ok = (ms % 2000) < 50;            // short blip every 2 s: alive, waiting for host
  if (KMAN_LED_OK_PIN != KMAN_LED_ERR_PIN || !g_sensorError) hal::led(KMAN_LED_OK_PIN, ok);
}

}  // namespace

void setup() {
  hal::init();
  hal::ledInit(KMAN_LED_OK_PIN);
  hal::ledInit(KMAN_LED_ERR_PIN);
  initSensors();
  hal::startSampleTimer(kPeriodUs);
  uint32_t dummy;
  g_lastTickCount = hal::latestTick(&dummy);
}

void loop() {
  hal::waitForTick(5);
  serviceHost();
  queueHeaderIfPending();

  uint32_t tickUs;
  const uint32_t count = hal::latestTick(&tickUs);
  if (count != g_lastTickCount) {
    if (g_streaming) {
      g_status.overruns += count - g_lastTickCount - 1;
      if (!g_headerPending) sampleAndQueue(count, tickUs);
    }
    g_lastTickCount = count;
  }

  queueStatusIfDue();
  pumpTx();
  updateLeds();
}
