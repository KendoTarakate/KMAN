#include "lsm6dsox.h"

#include <Arduino.h>

namespace {

// Register map (DS12814 rev. 4)
const uint8_t REG_WHO_AM_I = 0x0F;
const uint8_t REG_CTRL1_XL = 0x10;
const uint8_t REG_CTRL2_G = 0x11;
const uint8_t REG_CTRL3_C = 0x12;
const uint8_t REG_CTRL4_C = 0x13;
const uint8_t REG_CTRL6_C = 0x15;
const uint8_t REG_CTRL8_XL = 0x17;
const uint8_t REG_CTRL9_XL = 0x18;
const uint8_t REG_OUTX_L_G = 0x22;  // gyro X/Y/Z then accel X/Y/Z, 12 bytes
const uint8_t REG_OUTX_L_A = 0x28;

const uint8_t WHO_AM_I_VALUE = 0x6C;

const uint8_t CTRL3_SW_RESET = 0x01;
const uint8_t CTRL3_IF_INC = 0x04;
const uint8_t CTRL3_BDU = 0x40;
const uint8_t CTRL1_LPF2_XL_EN = 0x02;
const uint8_t CTRL4_LPF1_SEL_G = 0x02;
const uint8_t CTRL9_I3C_DISABLE = 0x02;
// Accel LPF2 cutoff = ODR/10 (HPCF_XL = 001, HP_SLOPE_XL_EN = 0): 20.8 Hz at 208 Hz ODR.
const uint8_t CTRL8_HPCF_ODR_DIV10 = 0x1 << 5;
// Gyro LPF1 FTYPE = 110: ~26 Hz at 208 Hz ODR (DS table "Gyroscope LPF1 + LPF2 bandwidth").
const uint8_t CTRL6_FTYPE = 0x6;

struct OdrEntry {
  uint16_t hz;
  uint8_t code;
};
const OdrEntry kOdrs[] = {{208, 0x5}, {416, 0x6}, {833, 0x7}};

}  // namespace

Lsm6dsox::Lsm6dsox(TwoWire& bus, uint8_t addr, bool accelOnly, uint16_t frameRateHz, uint8_t accelRangeG,
                   uint16_t gyroRangeDps)
    : bus_(bus),
      addr_(addr),
      accelOnly_(accelOnly),
      odrHz_(833),
      odrCode_(0x7),
      accelRangeG_(accelRangeG),
      gyroRangeDps_(gyroRangeDps),
      fsXlBits_(0),
      fsGBits_(0),
      err_("") {
  // Smallest ODR >= 2x the frame rate, so reading the latest register value
  // at each tick is at most half a frame period old.
  for (size_t i = 0; i < sizeof kOdrs / sizeof kOdrs[0]; ++i) {
    if (kOdrs[i].hz >= 2u * frameRateHz) {
      odrHz_ = kOdrs[i].hz;
      odrCode_ = kOdrs[i].code;
      break;
    }
  }

  // Full-scale bits and sensitivity (DS table "Mechanical characteristics").
  int32_t accelUg = 61;  // micro-g per LSB
  switch (accelRangeG) {
    case 4: fsXlBits_ = 0x2; accelUg = 122; break;
    case 8: fsXlBits_ = 0x3; accelUg = 244; break;
    case 16: fsXlBits_ = 0x1; accelUg = 488; break;
    default: fsXlBits_ = 0x0; accelUg = 61; accelRangeG_ = 2; break;
  }
  int32_t gyroUdps = 8750;  // micro-dps per LSB
  switch (gyroRangeDps) {
    case 125: fsGBits_ = 0x1; gyroUdps = 4375; break;  // FS_125 bit
    case 500: fsGBits_ = 0x1 << 1; gyroUdps = 17500; break;
    case 1000: fsGBits_ = 0x2 << 1; gyroUdps = 35000; break;
    case 2000: fsGBits_ = 0x3 << 1; gyroUdps = 70000; break;
    default: fsGBits_ = 0x0; gyroUdps = 8750; gyroRangeDps_ = 250; break;
  }

  static const char* const kSuffix[6] = {"ax", "ay", "az", "gx", "gy", "gz"};
  for (uint8_t i = 0; i < 6; ++i) {
    channels_[i].suffix = kSuffix[i];
    channels_[i].unit = i < 3 ? "g" : "dps";
    channels_[i].scaleMantissa = i < 3 ? accelUg : gyroUdps;
    channels_[i].scaleExp10 = -6;
  }
}

bool Lsm6dsox::writeReg(uint8_t reg, uint8_t value) {
  bus_.beginTransmission(addr_);
  bus_.write(reg);
  bus_.write(value);
  return bus_.endTransmission() == 0;
}

bool Lsm6dsox::readRegs(uint8_t reg, uint8_t* buf, uint8_t n) {
  bus_.beginTransmission(addr_);
  bus_.write(reg);
  if (bus_.endTransmission(false) != 0) return false;  // repeated start
  if (bus_.requestFrom(addr_, n) != n) return false;
  for (uint8_t i = 0; i < n; ++i) buf[i] = (uint8_t)bus_.read();
  return true;
}

bool Lsm6dsox::writeVerify(uint8_t reg, uint8_t value) {
  uint8_t back = 0;
  return writeReg(reg, value) && readRegs(reg, &back, 1) && back == value;
}

bool Lsm6dsox::init() {
  uint8_t who = 0;
  if (!readRegs(REG_WHO_AM_I, &who, 1)) {
    err_ = "no I2C ACK (check wiring, pull-ups and SA0 address)";
    return false;
  }
  if (who != WHO_AM_I_VALUE) {
    err_ = "unexpected WHO_AM_I (not an LSM6DSOX?)";
    return false;
  }

  if (!writeReg(REG_CTRL3_C, CTRL3_SW_RESET)) {
    err_ = "reset write failed";
    return false;
  }
  uint8_t ctrl3 = CTRL3_SW_RESET;
  for (int i = 0; i < 50 && (ctrl3 & CTRL3_SW_RESET); ++i) {
    delay(1);
    if (!readRegs(REG_CTRL3_C, &ctrl3, 1)) ctrl3 = CTRL3_SW_RESET;
  }
  if (ctrl3 & CTRL3_SW_RESET) {
    err_ = "software reset did not complete";
    return false;
  }

  uint8_t ctrl9 = 0;
  const bool ok = writeVerify(REG_CTRL3_C, CTRL3_BDU | CTRL3_IF_INC) &&
                  readRegs(REG_CTRL9_XL, &ctrl9, 1) &&
                  writeVerify(REG_CTRL9_XL, ctrl9 | CTRL9_I3C_DISABLE) &&
                  writeVerify(REG_CTRL8_XL, CTRL8_HPCF_ODR_DIV10) &&
                  writeVerify(REG_CTRL6_C, CTRL6_FTYPE) &&  // XL_HM_MODE = 0: high-performance
                  writeVerify(REG_CTRL4_C, accelOnly_ ? 0 : CTRL4_LPF1_SEL_G) &&
                  writeVerify(REG_CTRL1_XL, (uint8_t)((odrCode_ << 4) | (fsXlBits_ << 2) | CTRL1_LPF2_XL_EN)) &&
                  writeVerify(REG_CTRL2_G, accelOnly_ ? 0 : (uint8_t)((odrCode_ << 4) | (fsGBits_ << 1)));
  if (!ok) {
    err_ = "configuration write/verify failed";
    return false;
  }

  delay(100);  // gyro turn-on time + LPF settling (boot only)
  int16_t probe[6];
  if (!read(probe)) {
    err_ = "first data read failed";
    return false;
  }
  err_ = "";
  return true;
}

bool Lsm6dsox::read(int16_t* out) {
  uint8_t b[12];
  if (accelOnly_) {
    if (!readRegs(REG_OUTX_L_A, b, 6)) {
      err_ = "accel read failed";
      return false;
    }
    for (uint8_t i = 0; i < 3; ++i) out[i] = (int16_t)(b[2 * i] | (b[2 * i + 1] << 8));
    return true;
  }
  if (!readRegs(REG_OUTX_L_G, b, 12)) {
    err_ = "data read failed";
    return false;
  }
  // Registers hold gyro then accel; frames carry accel then gyro.
  for (uint8_t i = 0; i < 3; ++i) {
    out[i] = (int16_t)(b[6 + 2 * i] | (b[6 + 2 * i + 1] << 8));
    out[3 + i] = (int16_t)(b[2 * i] | (b[2 * i + 1] << 8));
  }
  return true;
}

void Lsm6dsox::describeConfig(JsonWriter& w) const {
  w.kv("model", "LSM6DSOX");
  w.kv("bus", "i2c");
  w.kvHex("addr", addr_);
  w.kv("mode", "timer-master register read (no FIFO)");
  w.kv("odr_hz", (int32_t)odrHz_);
  w.kv("accel_range_g", (int32_t)accelRangeG_);
  w.kv("accel_lpf", "LPF2 ODR/10");
  w.kvSci("accel_lpf_hz", odrHz_, -1);
  if (!accelOnly_) {
    w.kv("gyro_range_dps", (int32_t)gyroRangeDps_);
    w.kv("gyro_lpf", "LPF1 FTYPE=6 (~26 Hz at 208/416 Hz ODR)");
  }
  w.kv("block_data_update", true);
}
