// Wire protocol shared with host/kman_host/protocol.py. Arduino-free so it can
// be compiled natively for cross-checking against the Python decoder.
//
// Frame:   0xAA 0x55 | len (u16 LE) | type (u8) | body[len-1] | crc (u16 LE)
//   len  = number of bytes in type + body
//   crc  = CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection, no xorout)
//          computed over the len, type and body bytes
//
// Frame types:
//   DATA   (0x01)  t_us u32 | seq u16 | valid u8 | int16 channels (layout given by HEADER)
//   HEADER (0x02)  UTF-8 JSON session header
//   STATUS (0x03)  device counters, see encodeStatus()
//   LOG    (0x04)  UTF-8 text
// All multi-byte fields are little-endian.
#pragma once

#include <stddef.h>
#include <stdint.h>

namespace proto {

const uint8_t kSync0 = 0xAA;
const uint8_t kSync1 = 0x55;
const uint8_t kProtocolVersion = 1;

enum FrameType : uint8_t {
  kTypeData = 0x01,
  kTypeHeader = 0x02,
  kTypeStatus = 0x03,
  kTypeLog = 0x04,
};

// sync(2) + len(2) + type(1) + crc(2)
const size_t kOverhead = 7;
// Upper bound on len; the host treats anything larger as a false sync.
const size_t kMaxLen = 4096;

// Host -> device single-byte commands.
const uint8_t kCmdSendHeader = 'H';
const uint8_t kCmdSendStatus = 'S';

uint16_t crc16(const uint8_t* data, size_t n, uint16_t crc = 0xFFFF);

// Writes a complete frame into out (capacity >= body_len + kOverhead).
// Returns the number of bytes written, or 0 if body_len is too large.
size_t encodeFrame(uint8_t type, const uint8_t* body, size_t body_len, uint8_t* out, size_t out_cap);

inline uint8_t* putU8(uint8_t* p, uint8_t v) {
  *p++ = v;
  return p;
}
inline uint8_t* putU16(uint8_t* p, uint16_t v) {
  *p++ = (uint8_t)(v & 0xFF);
  *p++ = (uint8_t)(v >> 8);
  return p;
}
inline uint8_t* putI16(uint8_t* p, int16_t v) { return putU16(p, (uint16_t)v); }
inline uint8_t* putU32(uint8_t* p, uint32_t v) {
  *p++ = (uint8_t)(v & 0xFF);
  *p++ = (uint8_t)((v >> 8) & 0xFF);
  *p++ = (uint8_t)((v >> 16) & 0xFF);
  *p++ = (uint8_t)(v >> 24);
  return p;
}

// Device health counters, sent periodically in a STATUS frame.
const uint8_t kStatusVersion = 1;
const uint8_t kStatusSlots = 4;
struct Status {
  uint32_t t_us;                      // timer time when the status was built
  uint32_t ticks;                     // sample-timer ticks since boot
  uint32_t frames_sent;               // DATA frames queued since streaming started
  uint32_t overruns;                  // ticks skipped because the loop was late
  uint32_t tx_drops;                  // DATA frames dropped because the TX ring was full
  uint32_t read_errors[kStatusSlots]; // per sensor slot
  uint16_t max_latency_us;            // max tick->frame-queued latency since last status
  uint16_t tx_high_water;             // max TX ring fill (bytes) since last status
  uint8_t flags;                      // bit0 HF crystal running, bit1 sensor error
};
const uint8_t kStatusFlagHfxo = 0x01;
const uint8_t kStatusFlagSensorError = 0x02;
// Body size of a STATUS frame.
const size_t kStatusBodyLen = 1 + 5 * 4 + kStatusSlots * 4 + 2 + 2 + 1;

// Serialises s into body (capacity >= kStatusBodyLen). Returns bytes written.
size_t encodeStatus(const Status& s, uint8_t* body);

}  // namespace proto
