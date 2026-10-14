// ST LSM6DSOX 6-axis IMU, register-level I2C driver.
//
// Used in "timer-master" mode: the IMU free-runs at ODR >= 2x the frame rate
// with its digital low-pass filters set well below the frame Nyquist
// frequency, and each sample tick reads the latest output registers in one
// 12-byte burst. Block Data Update keeps the high/low bytes coherent. No FIFO:
// it would add a second clock domain (the IMU oscillator is only +-1.5 %).
#pragma once

#include <Wire.h>

#include "sensor.h"

class Lsm6dsox : public Sensor {
 public:
  Lsm6dsox(TwoWire& bus, uint8_t addr, bool accelOnly, uint16_t frameRateHz, uint8_t accelRangeG,
           uint16_t gyroRangeDps);

  bool init() override;
  bool read(int16_t* out) override;
  uint8_t channelCount() const override { return accelOnly_ ? 3 : 6; }
  const ChannelInfo& channel(uint8_t i) const override { return channels_[i]; }
  void describeConfig(JsonWriter& w) const override;
  const char* lastError() const override { return err_; }

 private:
  bool writeReg(uint8_t reg, uint8_t value);
  bool readRegs(uint8_t reg, uint8_t* buf, uint8_t n);
  bool writeVerify(uint8_t reg, uint8_t value);

  TwoWire& bus_;
  uint8_t addr_;
  bool accelOnly_;
  uint16_t odrHz_;
  uint8_t odrCode_;
  uint8_t accelRangeG_;
  uint16_t gyroRangeDps_;
  uint8_t fsXlBits_;
  uint8_t fsGBits_;
  const char* err_;
  ChannelInfo channels_[6];
};
