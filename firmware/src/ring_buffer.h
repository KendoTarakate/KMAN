// Fixed-capacity byte FIFO for staging outgoing frames. Producer and consumer
// both run in the loop task, so no locking is needed. Pushes are
// all-or-nothing so a frame is never split by an overflow.
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

template <size_t N>
class ByteRing {
 public:
  ByteRing() : head_(0), tail_(0), used_(0), highWater_(0) {}

  size_t used() const { return used_; }
  size_t free() const { return N - used_; }
  size_t highWater() const { return highWater_; }
  void resetHighWater() { highWater_ = used_; }
  void clear() { head_ = tail_ = used_ = 0; }

  bool push(const uint8_t* data, size_t n) {
    if (n > free()) return false;
    const size_t first = (n < N - head_) ? n : N - head_;
    memcpy(buf_ + head_, data, first);
    memcpy(buf_, data + first, n - first);
    head_ = (head_ + n) % N;
    used_ += n;
    if (used_ > highWater_) highWater_ = used_;
    return true;
  }

  // Longest contiguous readable run starting at the tail.
  size_t peekContiguous(const uint8_t** ptr) const {
    *ptr = buf_ + tail_;
    const size_t toEnd = N - tail_;
    return used_ < toEnd ? used_ : toEnd;
  }

  void consume(size_t n) {
    if (n > used_) n = used_;
    tail_ = (tail_ + n) % N;
    used_ -= n;
  }

 private:
  uint8_t buf_[N];
  size_t head_, tail_, used_, highWater_;
};
