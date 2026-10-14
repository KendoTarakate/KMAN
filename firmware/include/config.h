// Compile-time configuration. Every value here can be overridden from
// platformio.ini (build_flags = -DNAME=value). Nothing in the sampling path
// reads configuration at run time.
#pragma once

#include <Arduino.h>

#ifndef KMAN_FW_VERSION
#define KMAN_FW_VERSION "1.0.0"
#endif

#ifndef KMAN_BOARD_NAME
#define KMAN_BOARD_NAME "unknown"
#endif

// ---------------------------------------------------------------- sampling
// Frame rate of the whole system. The hardware timer runs at 1 MHz, so the
// period (1e6 / rate) must be an integer number of microseconds.
#ifndef KMAN_SAMPLE_RATE_HZ
#define KMAN_SAMPLE_RATE_HZ 100
#endif

// ---------------------------------------------------------------- sensor slots
// 1 = fitted (probed at boot; on failure it is reported in the session header
// and left out of the frame layout), 0 = not fitted.
#ifndef KMAN_CHEST_IMU
#define KMAN_CHEST_IMU 1
#endif
#ifndef KMAN_CHEST_IMU_ADDR
#define KMAN_CHEST_IMU_ADDR 0x6A  // LSM6DSOX SA0/SDO low (Adafruit/most breakouts)
#endif

#ifndef KMAN_ABDOMEN_IMU
#define KMAN_ABDOMEN_IMU 0
#endif
#ifndef KMAN_ABDOMEN_IMU_ADDR
#define KMAN_ABDOMEN_IMU_ADDR 0x6B  // second LSM6DSOX with SA0/SDO tied high
#endif

#ifndef KMAN_NECK_FLEX
#define KMAN_NECK_FLEX 1
#endif
#ifndef KMAN_FLEX_PIN
#define KMAN_FLEX_PIN A0  // XIAO D0 = P0.02 = AIN0
#endif
// Metadata only (goes into the session header so the dataset is self-describing).
#ifndef KMAN_FLEX_SENSOR_DESC
#define KMAN_FLEX_SENSOR_DESC "flex sensor (edit KMAN_FLEX_SENSOR_DESC)"
#endif
#ifndef KMAN_FLEX_R_FIXED_OHM
#define KMAN_FLEX_R_FIXED_OHM 47000
#endif
#ifndef KMAN_FLEX_TOPOLOGY
#define KMAN_FLEX_TOPOLOGY "3V3 - flex - ADC - R_fixed - GND"
#endif

// Optional neck IMU for head-pose rejection. Only two LSM6DSOX addresses exist,
// so on a single bus it can share with the chest IMU only if the abdomen IMU
// is not fitted.
#ifndef KMAN_NECK_IMU
#define KMAN_NECK_IMU 0
#endif
#ifndef KMAN_NECK_IMU_ADDR
#define KMAN_NECK_IMU_ADDR 0x6B
#endif
#ifndef KMAN_NECK_IMU_ACCEL_ONLY
#define KMAN_NECK_IMU_ACCEL_ONLY 0  // 1 = stream accel only (3 ch), 0 = accel+gyro (6 ch)
#endif

// ---------------------------------------------------------------- IMU settings
// Spec: small-motion ranges. Only these values are supported by the driver
// tables: accel 2/4/8/16 g, gyro 125/250/500/1000/2000 dps.
#ifndef KMAN_IMU_ACCEL_RANGE_G
#define KMAN_IMU_ACCEL_RANGE_G 2
#endif
#ifndef KMAN_IMU_GYRO_RANGE_DPS
#define KMAN_IMU_GYRO_RANGE_DPS 250
#endif

// ---------------------------------------------------------------- pins / buses
#ifndef KMAN_I2C_SDA_PIN
#define KMAN_I2C_SDA_PIN PIN_WIRE_SDA  // XIAO D4
#endif
#ifndef KMAN_I2C_SCL_PIN
#define KMAN_I2C_SCL_PIN PIN_WIRE_SCL  // XIAO D5
#endif
#ifndef KMAN_I2C_HZ
#define KMAN_I2C_HZ 400000
#endif

// Status LEDs. Blinking OK LED = streaming, solid ERR LED = sensor error.
// On single-LED boards point both at the same pin.
#ifndef KMAN_LED_OK_PIN
#define KMAN_LED_OK_PIN LED_GREEN
#endif
#ifndef KMAN_LED_ERR_PIN
#define KMAN_LED_ERR_PIN LED_RED
#endif
#ifndef KMAN_LED_ACTIVE_LOW
#define KMAN_LED_ACTIVE_LOW 1  // XIAO RGB LED is common-anode
#endif

// ---------------------------------------------------------------- ADC
#ifndef KMAN_ADC_BITS
#define KMAN_ADC_BITS 14  // nRF52 SAADC: 8/10/12/14 (14 is meaningful with oversampling)
#endif
#ifndef KMAN_ADC_OVERSAMPLE
#define KMAN_ADC_OVERSAMPLE 16  // power of two, 1..256 (burst mode, ~12 us per sample)
#endif
#ifndef KMAN_ADC_TACQ_US
#define KMAN_ADC_TACQ_US 10  // 3/5/10/15/20/40; 10 us is good for source impedance <= 100 kOhm
#endif

// ---------------------------------------------------------------- transport
#ifndef KMAN_TX_RING_BYTES
#define KMAN_TX_RING_BYTES 8192  // ~2.5 s of frames at 100 Hz; absorbs host/USB stalls
#endif
#ifndef KMAN_STATUS_PERIOD_MS
#define KMAN_STATUS_PERIOD_MS 1000
#endif

// ---------------------------------------------------------------- sanity
static_assert(1000000UL % KMAN_SAMPLE_RATE_HZ == 0, "KMAN_SAMPLE_RATE_HZ must divide 1e6");
static_assert(KMAN_SAMPLE_RATE_HZ >= 1 && KMAN_SAMPLE_RATE_HZ <= 416,
              "IMU ODR is chosen as >= 2x the frame rate, max 833 Hz");
static_assert(!(KMAN_ABDOMEN_IMU && KMAN_NECK_IMU && KMAN_ABDOMEN_IMU_ADDR == KMAN_NECK_IMU_ADDR),
              "abdomen and neck IMU share an I2C address");
static_assert(!(KMAN_CHEST_IMU && KMAN_ABDOMEN_IMU && KMAN_CHEST_IMU_ADDR == KMAN_ABDOMEN_IMU_ADDR),
              "chest and abdomen IMU share an I2C address");
static_assert(!(KMAN_CHEST_IMU && KMAN_NECK_IMU && KMAN_CHEST_IMU_ADDR == KMAN_NECK_IMU_ADDR),
              "chest and neck IMU share an I2C address");
