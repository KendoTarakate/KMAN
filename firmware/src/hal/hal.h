// Board abstraction. Everything board/MCU-specific lives behind this header;
// one hal_<mcu>.cpp per supported MCU family.
#pragma once

#include <stddef.h>
#include <stdint.h>

#include "../json_writer.h"

namespace hal {

void init();  // clocks, LEDs, host link

const char* mcuName();
void deviceId(char* out, size_t cap);  // stable hex id of this chip
uint32_t bootId();                     // random per boot: lets the host tell a reboot from a reconnect

// ---- sample clock (hardware timer, 1 MHz timebase) ----
// Starts a periodic tick. Must be called from the task that later calls waitForTick().
bool startSampleTimer(uint32_t period_us);
// Number of ticks since start; *tick_us receives the scheduled time of the
// latest tick on the 1 MHz timebase (wraps every 2^32 us = 71.6 min).
uint32_t latestTick(uint32_t* tick_us);
// Sleeps until the next tick or timeout_ms, whichever comes first.
void waitForTick(uint32_t timeout_ms);
uint32_t nowUs();  // same timebase as the tick timestamps
bool clockIsCrystal();
void describeClock(JsonWriter& w);

// ---- ADC ----
void adcInit();
uint16_t adcRead(uint8_t pin);
uint16_t adcFullScale();
void describeAdc(JsonWriter& w);

// ---- I2C ----
// Clocks out a slave that is holding SDA low (e.g. after an MCU reset mid-transfer).
void i2cBusRecover(uint8_t sda, uint8_t scl);

// ---- host link ----
bool hostConnected();  // host has the port open (DTR asserted)
size_t hostWritable();
size_t hostWrite(const uint8_t* data, size_t n);  // never blocks if n <= hostWritable()
void hostFlush();                                 // start sending partially filled USB packets
int hostRead();                                   // -1 if nothing pending

// ---- LEDs ----
void ledInit(uint8_t pin);
void led(uint8_t pin, bool on);

}  // namespace hal
