// Host-compiled golden-vector generator for the wire protocol. Compiles the
// firmware's own protocol.cpp / json_writer.h with a native compiler and writes
// frames to stdout; host/tests/test_crosscheck.py decodes them with the Python
// implementation. Build: g++ -std=c++11 -I../src native_frames.cpp ../src/protocol.cpp
#include <stdio.h>

#include "json_writer.h"
#include "protocol.h"

static void emit(uint8_t type, const uint8_t* body, size_t n) {
  static uint8_t frame[4096 + proto::kOverhead];
  const size_t len = proto::encodeFrame(type, body, n, frame, sizeof frame);
  fwrite(frame, 1, len, stdout);
}

int main() {
  // DATA: t_us near wrap, seq, valid=0b101, 7 channels incl. extremes.
  uint8_t body[64];
  uint8_t* p = body;
  p = proto::putU32(p, 0xFFFFFF00u);
  p = proto::putU16(p, 0xFFFE);
  p = proto::putU8(p, 0x05);
  const int16_t ch[7] = {-32768, 32767, 16393, -1, 0, 1234, 8191};
  for (int i = 0; i < 7; ++i) p = proto::putI16(p, ch[i]);
  emit(proto::kTypeData, body, (size_t)(p - body));

  // STATUS
  proto::Status s = {};
  s.t_us = 123456789u;
  s.ticks = 4000000000u;
  s.frames_sent = 42;
  s.overruns = 3;
  s.tx_drops = 7;
  s.read_errors[0] = 1;
  s.read_errors[3] = 9;
  s.max_latency_us = 650;
  s.tx_high_water = 280;
  s.flags = proto::kStatusFlagHfxo;
  uint8_t sb[proto::kStatusBodyLen];
  const size_t sn = proto::encodeStatus(s, sb);
  emit(proto::kTypeStatus, sb, sn);

  // HEADER-like JSON exercising every writer feature.
  char json[1024];
  JsonWriter w(json, sizeof json);
  w.beginObject();
  w.kv("type", "kman_session_header");
  w.kv("quote", "a\"b\\c\n");
  w.kvSci("scale", 61, -6);
  w.kvSci("lpf", 208, -1);
  w.kvSci("int", 7, 0);
  w.kvHex("addr", 0x6A);
  w.kv("neg", (int32_t)-5);
  w.kv("big", (uint32_t)4000000000u);
  w.kv("flag", true);
  w.beginArray("list");
  w.item("x");
  w.beginObject();
  w.kvJoined("name", "chest", "ax");
  w.endObject();
  w.endArray();
  w.endObject();
  if (!w.ok()) return 1;
  emit(proto::kTypeHeader, (const uint8_t*)w.c_str(), w.length());

  // Overflowing writer must report !ok().
  char tiny[8];
  JsonWriter t(tiny, sizeof tiny);
  t.beginObject();
  t.kv("too", "long for the buffer");
  t.endObject();
  const char* verdict = t.ok() ? "overflow-not-detected" : "overflow-detected";
  emit(proto::kTypeLog, (const uint8_t*)verdict, strlen(verdict));
  return 0;
}
