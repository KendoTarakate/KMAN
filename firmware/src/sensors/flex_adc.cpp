#include "flex_adc.h"

#include "../hal/hal.h"

FlexAdc::FlexAdc(uint8_t pin, const char* sensorDesc, uint32_t rFixedOhm, const char* topology)
    : pin_(pin), desc_(sensorDesc), rFixedOhm_(rFixedOhm), topology_(topology), warning_("") {
  channel_.suffix = "raw";
  channel_.unit = "counts";
  channel_.scaleMantissa = 1;
  channel_.scaleExp10 = 0;
}

bool FlexAdc::init() {
  hal::adcInit();
  // An ADC cannot prove a divider is wired, but a reading pinned to a rail
  // almost always means an open or shorted sensor; report it, keep streaming.
  const uint16_t fs = hal::adcFullScale();
  const uint16_t margin = fs / 50;
  uint8_t atRail = 0;
  for (uint8_t i = 0; i < 8; ++i) {
    const uint16_t v = hal::adcRead(pin_);
    if (v <= margin || v >= fs - margin) ++atRail;
  }
  warning_ = atRail == 8 ? "reading at ADC rail: sensor open/shorted or divider not powered?" : "";
  return true;
}

bool FlexAdc::read(int16_t* out) {
  out[0] = (int16_t)hal::adcRead(pin_);
  return true;
}

void FlexAdc::describeConfig(JsonWriter& w) const {
  w.kv("model", "resistive divider");
  w.kv("sensor", desc_);
  w.kv("r_fixed_ohm", rFixedOhm_);
  w.kv("topology", topology_);
  w.kv("pin", (int32_t)pin_);
  hal::describeAdc(w);
}
