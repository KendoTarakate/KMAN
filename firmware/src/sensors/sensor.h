// Sensor driver interface. A driver produces a fixed number of int16 channels
// per frame. Swapping the flex sensor for a piezo/stretch sensor means writing
// another Sensor subclass; nothing else in the firmware changes.
#pragma once

#include <stdint.h>

#include "../json_writer.h"

struct ChannelInfo {
  const char* suffix;  // joined with the slot prefix: "chest" + "ax" -> "chest_ax"
  const char* unit;    // physical unit after scaling
  int32_t scaleMantissa;  // physical = raw * scaleMantissa * 10^scaleExp10
  int8_t scaleExp10;
};

class Sensor {
 public:
  // Probe the device and apply its configuration. Returns false (and sets
  // lastError()) if the device is absent or misconfigured. Called once at boot.
  virtual bool init() = 0;

  // Read one sample of channelCount() raw values into out. Must be fast and
  // allocation-free: it runs once per sample tick. Returns false on bus error.
  virtual bool read(int16_t* out) = 0;

  virtual uint8_t channelCount() const = 0;
  virtual const ChannelInfo& channel(uint8_t i) const = 0;

  // Emit model, ranges and filter settings as JSON members into the current object.
  virtual void describeConfig(JsonWriter& w) const = 0;

  // Human-readable reason for the last init()/read() failure, or "" if none.
  virtual const char* lastError() const = 0;

  // Non-fatal init observation (e.g. "reading at rail"), or "" if none.
  virtual const char* warning() const { return ""; }

 protected:
  ~Sensor() {}  // drivers are static objects; never deleted through a base pointer
};
