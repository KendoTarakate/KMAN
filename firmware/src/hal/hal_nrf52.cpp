// nRF52840 HAL for the Adafruit-based Seeed core (TinyUSB CDC, FreeRTOS).
//
// Sample clock: TIMER4 free-running at 1 MHz from the 16 MHz HF crystal, 32-bit.
// CC[0] is advanced by the period on every compare, so tick times are exact
// multiples of the period (no ISR-latency jitter, no accumulated drift beyond
// the crystal's ~20 ppm). The ISR only records the tick and wakes the loop task.
#if defined(NRF52_SERIES) || defined(NRF52840_XXAA)

#include <Adafruit_TinyUSB.h>
#include <Arduino.h>

#include "../../include/config.h"
#include "hal.h"

namespace {

NRF_TIMER_Type* const kTimer = NRF_TIMER4;  // TIMER0 belongs to the SoftDevice; 3/4 are unused by the core
const IRQn_Type kTimerIrq = TIMER4_IRQn;
// Priority 3: below the SoftDevice's reserved levels (0,1,4) should BLE be added
// later, and >= configMAX_SYSCALL_INTERRUPT_PRIORITY (2) so FromISR calls are legal.
const uint32_t kTimerIrqPriority = 3;

volatile uint32_t s_tickCount = 0;
volatile uint32_t s_tickTimeUs = 0;
uint32_t s_periodUs = 10000;
TaskHandle_t s_loopTask = nullptr;

void startHfxo() {
  // The SoftDevice is never enabled in this firmware, so CLOCK may be driven
  // directly. (With BLE this must become sd_clock_hfclk_request().)
  const uint32_t xtal = CLOCK_HFCLKSTAT_SRC_Xtal << CLOCK_HFCLKSTAT_SRC_Pos;
  if ((NRF_CLOCK->HFCLKSTAT & CLOCK_HFCLKSTAT_SRC_Msk) == xtal) return;
  NRF_CLOCK->EVENTS_HFCLKSTARTED = 0;
  NRF_CLOCK->TASKS_HFCLKSTART = 1;
  for (uint32_t guard = 0; !NRF_CLOCK->EVENTS_HFCLKSTARTED && guard < 1000000; ++guard) {
  }
}

}  // namespace

extern "C" void TIMER4_IRQHandler(void) {
  if (!kTimer->EVENTS_COMPARE[0]) return;
  kTimer->EVENTS_COMPARE[0] = 0;
  (void)kTimer->EVENTS_COMPARE[0];  // read back so the clear lands before IRQ exit

  uint32_t tick = kTimer->CC[0];
  uint32_t count = s_tickCount + 1;
  uint32_t next = tick + s_periodUs;
  // If interrupts were masked for more than a period the compare for `next`
  // may already have passed; skip ahead so ticks keep their exact grid.
  kTimer->TASKS_CAPTURE[1] = 1;
  const uint32_t now = kTimer->CC[1];
  while ((int32_t)(now - next) >= 0) {
    tick = next;
    next += s_periodUs;
    ++count;
  }
  kTimer->CC[0] = next;

  s_tickTimeUs = tick;  // written before the count: the reader detects a torn read via the count
  s_tickCount = count;

  if (s_loopTask) {
    BaseType_t woken = pdFALSE;
    vTaskNotifyGiveFromISR(s_loopTask, &woken);
    portYIELD_FROM_ISR(woken);
  }
}

namespace hal {

void init() {
  startHfxo();
  Serial.begin(115200);  // baud is irrelevant for USB CDC
}

const char* mcuName() { return "nRF52840"; }

void deviceId(char* out, size_t cap) {
  snprintf(out, cap, "%08lX%08lX", (unsigned long)NRF_FICR->DEVICEID[1], (unsigned long)NRF_FICR->DEVICEID[0]);
}

bool startSampleTimer(uint32_t period_us) {
  s_periodUs = period_us;
  s_loopTask = xTaskGetCurrentTaskHandle();

  kTimer->TASKS_STOP = 1;
  kTimer->TASKS_CLEAR = 1;
  kTimer->MODE = TIMER_MODE_MODE_Timer << TIMER_MODE_MODE_Pos;
  kTimer->BITMODE = TIMER_BITMODE_BITMODE_32Bit << TIMER_BITMODE_BITMODE_Pos;
  kTimer->PRESCALER = 4;  // 16 MHz / 2^4 = 1 MHz
  kTimer->SHORTS = 0;     // free-running: the counter is the microsecond timebase
  kTimer->CC[0] = period_us;
  kTimer->EVENTS_COMPARE[0] = 0;
  kTimer->INTENSET = TIMER_INTENSET_COMPARE0_Msk;

  NVIC_SetPriority(kTimerIrq, kTimerIrqPriority);
  NVIC_ClearPendingIRQ(kTimerIrq);
  NVIC_EnableIRQ(kTimerIrq);
  kTimer->TASKS_START = 1;
  return true;
}

uint32_t latestTick(uint32_t* tick_us) {
  uint32_t c1, c2, t;
  do {
    c1 = s_tickCount;
    t = s_tickTimeUs;
    c2 = s_tickCount;
  } while (c1 != c2);
  *tick_us = t;
  return c1;
}

void waitForTick(uint32_t timeout_ms) {
  ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(timeout_ms) ? pdMS_TO_TICKS(timeout_ms) : 1);
}

uint32_t nowUs() {
  // CC[2] is reserved for the loop task (the ISR uses CC[1]).
  kTimer->TASKS_CAPTURE[2] = 1;
  return kTimer->CC[2];
}

bool clockIsCrystal() {
  const uint32_t stat = NRF_CLOCK->HFCLKSTAT;
  return (stat & CLOCK_HFCLKSTAT_SRC_Msk) == (CLOCK_HFCLKSTAT_SRC_Xtal << CLOCK_HFCLKSTAT_SRC_Pos) &&
         (stat & CLOCK_HFCLKSTAT_STATE_Msk);
}

void describeClock(JsonWriter& w) {
  w.kv("source", "TIMER4 32-bit free-running, 1 MHz");
  w.kv("hfclk", clockIsCrystal() ? "HFXO 32 MHz crystal" : "HFINT RC (inaccurate!)");
  w.kv("t_unit", "us");
  w.kv("t_bits", 32);
  w.kv("seq_bits", 16);
}

// ---------------------------------------------------------------- ADC

void adcInit() {
  // Ratiometric: reference VDD/4 with gain 1/4 -> full scale = VDD. The divider
  // is powered from the same 3V3 rail, so supply ripple cancels out.
  analogReference(AR_VDD4);
  analogReadResolution(KMAN_ADC_BITS);
  analogOversampling(KMAN_ADC_OVERSAMPLE);  // also enables burst mode (one SAMPLE task -> N conversions)
  analogSampleTime(KMAN_ADC_TACQ_US);
}

uint16_t adcRead(uint8_t pin) { return (uint16_t)analogRead(pin); }

uint16_t adcFullScale() { return (uint16_t)((1u << KMAN_ADC_BITS) - 1); }

void describeAdc(JsonWriter& w) {
  w.kv("adc", "nRF52840 SAADC");
  w.kv("reference", "VDD/4, gain 1/4 (ratiometric, full scale = VDD)");
  w.kv("bits", (int32_t)KMAN_ADC_BITS);
  w.kv("oversample", (int32_t)KMAN_ADC_OVERSAMPLE);
  w.kv("tacq_us", (int32_t)KMAN_ADC_TACQ_US);
  w.kv("full_scale", (int32_t)adcFullScale());
}

// ---------------------------------------------------------------- I2C

void i2cBusRecover(uint8_t sda, uint8_t scl) {
  pinMode(sda, INPUT_PULLUP);
  pinMode(scl, INPUT_PULLUP);
  delayMicroseconds(10);
  if (digitalRead(sda)) return;  // bus idle
  pinMode(scl, OUTPUT);
  for (int i = 0; i < 9 && !digitalRead(sda); ++i) {
    digitalWrite(scl, LOW);
    delayMicroseconds(5);
    digitalWrite(scl, HIGH);
    delayMicroseconds(5);
  }
  // STOP condition: SDA low -> high while SCL high.
  pinMode(sda, OUTPUT);
  digitalWrite(sda, LOW);
  delayMicroseconds(5);
  digitalWrite(scl, HIGH);
  delayMicroseconds(5);
  digitalWrite(sda, HIGH);
  delayMicroseconds(5);
  pinMode(sda, INPUT_PULLUP);
  pinMode(scl, INPUT_PULLUP);
}

// ---------------------------------------------------------------- host link

bool hostConnected() { return Serial.dtr(); }
size_t hostWritable() {
  const int n = Serial.availableForWrite();
  return n > 0 ? (size_t)n : 0;
}
size_t hostWrite(const uint8_t* data, size_t n) { return Serial.write(data, n); }
void hostFlush() { Serial.flush(); }  // TinyUSB: non-blocking, just queues the partial packet
int hostRead() { return Serial.available() ? Serial.read() : -1; }

// ---------------------------------------------------------------- LEDs

void ledInit(uint8_t pin) {
  pinMode(pin, OUTPUT);
  led(pin, false);
}

void led(uint8_t pin, bool on) { digitalWrite(pin, (on != (bool)KMAN_LED_ACTIVE_LOW) ? HIGH : LOW); }

}  // namespace hal

#endif  // NRF52
