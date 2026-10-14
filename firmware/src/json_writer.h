// Minimal bounded JSON builder over a caller-owned buffer (no heap, no floats:
// newlib-nano printf has no %f, so fractional numbers are emitted as
// mantissa/exponent, e.g. 61e-6, which is valid JSON).
#pragma once

#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

class JsonWriter {
 public:
  JsonWriter(char* buf, size_t cap) : buf_(buf), cap_(cap), len_(0), depth_(0), overflow_(false) {
    if (cap_) buf_[0] = '\0';
    first_[0] = true;
  }

  const char* c_str() const { return buf_; }
  size_t length() const { return len_; }
  bool ok() const { return !overflow_ && depth_ == 0; }

  void beginObject(const char* key = nullptr) { open(key, '{'); }
  void endObject() { close('}'); }
  void beginArray(const char* key = nullptr) { open(key, '['); }
  void endArray() { close(']'); }

  void kv(const char* key, const char* value) {
    sep(key);
    str(value);
  }
  // String value made of two parts joined by '_' (channel names: slot prefix + suffix).
  void kvJoined(const char* key, const char* a, const char* b) {
    sep(key);
    raw("\"");
    esc(a);
    raw("_");
    esc(b);
    raw("\"");
  }
  // Overloads on fundamental types (int32_t is `long` on ARM newlib but `int` on
  // x86-64, so overloading on the fixed-width typedefs is not portable).
  void kv(const char* key, long value) {
    sep(key);
    fmt("%ld", value);
  }
  void kv(const char* key, unsigned long value) {
    sep(key);
    fmt("%lu", value);
  }
  void kv(const char* key, int value) { kv(key, (long)value); }
  void kv(const char* key, unsigned int value) { kv(key, (unsigned long)value); }
  void kv(const char* key, bool value) {
    sep(key);
    raw(value ? "true" : "false");
  }
  // mantissa * 10^exp10, e.g. (875, -5) -> 875e-5
  void kvSci(const char* key, int32_t mantissa, int8_t exp10) {
    sep(key);
    if (exp10 == 0) fmt("%ld", (long)mantissa);
    else fmt("%lde%d", (long)mantissa, (int)exp10);
  }
  void kvHex(const char* key, uint32_t value, int digits = 2) {
    sep(key);
    fmt("\"0x%0*lX\"", digits, (unsigned long)value);
  }
  // Array element (string).
  void item(const char* value) {
    sep(nullptr);
    str(value);
  }

 private:
  static const int kMaxDepth = 8;

  void open(const char* key, char c) {
    sep(key);
    char s[2] = {c, '\0'};
    raw(s);
    if (depth_ + 1 < kMaxDepth) first_[++depth_] = true;
    else overflow_ = true;
  }
  void close(char c) {
    char s[2] = {c, '\0'};
    raw(s);
    if (depth_ > 0) --depth_;
    else overflow_ = true;
  }
  void sep(const char* key) {
    if (!first_[depth_]) raw(",");
    first_[depth_] = false;
    if (key) {
      str(key);
      raw(":");
    }
  }
  void str(const char* s) {
    raw("\"");
    esc(s);
    raw("\"");
  }
  void esc(const char* s) {
    for (; s && *s; ++s) {
      const char c = *s;
      if (c == '"' || c == '\\') {
        char e[3] = {'\\', c, '\0'};
        raw(e);
      } else if ((unsigned char)c < 0x20) {
        fmt("\\u%04x", (unsigned)(unsigned char)c);
      } else {
        char e[2] = {c, '\0'};
        raw(e);
      }
    }
  }
  void raw(const char* s) {
    const size_t n = strlen(s);
    if (len_ + n + 1 > cap_) {
      overflow_ = true;
      return;
    }
    memcpy(buf_ + len_, s, n + 1);
    len_ += n;
  }
  void fmt(const char* f, ...) {
    char tmp[32];
    va_list ap;
    va_start(ap, f);
    vsnprintf(tmp, sizeof tmp, f, ap);
    va_end(ap);
    raw(tmp);
  }

  char* buf_;
  size_t cap_;
  size_t len_;
  int depth_;
  bool first_[kMaxDepth];
  bool overflow_;
};
