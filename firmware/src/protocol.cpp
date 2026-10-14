#include "protocol.h"

#include <string.h>

namespace proto {

uint16_t crc16(const uint8_t* data, size_t n, uint16_t crc) {
  // Bitwise CRC-16/CCITT-FALSE. ~25 bytes per frame at 100 Hz, so a table is not worth 512 B.
  for (size_t i = 0; i < n; ++i) {
    crc ^= (uint16_t)data[i] << 8;
    for (uint8_t b = 0; b < 8; ++b) {
      crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
    }
  }
  return crc;
}

size_t encodeFrame(uint8_t type, const uint8_t* body, size_t body_len, uint8_t* out, size_t out_cap) {
  const size_t len = body_len + 1;
  if (len > kMaxLen || body_len + kOverhead > out_cap) return 0;
  uint8_t* p = out;
  p = putU8(p, kSync0);
  p = putU8(p, kSync1);
  p = putU16(p, (uint16_t)len);
  p = putU8(p, type);
  if (body_len) memcpy(p, body, body_len);
  p += body_len;
  const uint16_t crc = crc16(out + 2, len + 2);  // len field + type + body
  p = putU16(p, crc);
  return (size_t)(p - out);
}

size_t encodeStatus(const Status& s, uint8_t* body) {
  uint8_t* p = body;
  p = putU8(p, kStatusVersion);
  p = putU32(p, s.t_us);
  p = putU32(p, s.ticks);
  p = putU32(p, s.frames_sent);
  p = putU32(p, s.overruns);
  p = putU32(p, s.tx_drops);
  for (uint8_t i = 0; i < kStatusSlots; ++i) p = putU32(p, s.read_errors[i]);
  p = putU16(p, s.max_latency_us);
  p = putU16(p, s.tx_high_water);
  p = putU8(p, s.flags);
  return (size_t)(p - body);
}

}  // namespace proto
