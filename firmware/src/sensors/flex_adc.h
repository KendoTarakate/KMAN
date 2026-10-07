// Resistive sensor in a voltage divider read by the MCU ADC (flex sensor today;
// a stretch sensor or FSR works unchanged). Raw counts are streamed; conversion
// to resistance/angle is done on the host using the divider metadata.
#pragma once

#include "sensor.h"

class FlexAdc : public Sensor {
 public:
  FlexAdc(uint8_t pin, const char* sensorDesc, uint32_t rFixedOhm, const char* topology);

  bool init() override;
  bool read(int16_t* out) override;
  uint8_t channelCount() const override { return 1; }
  const ChannelInfo& channel(uint8_t) const override { return channel_; }
  void describeConfig(JsonWriter& w) const override;
  const char* lastError() const override { return ""; }
  const char* warning() const override { return warning_; }

 private:
  uint8_t pin_;
  const char* desc_;
  uint32_t rFixedOhm_;
  const char* topology_;
  const char* warning_;
  ChannelInfo channel_;
};
