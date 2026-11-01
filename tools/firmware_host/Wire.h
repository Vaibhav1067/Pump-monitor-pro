#pragma once
#include <cstdint>

struct TwoWire {
  void begin(int, int, uint32_t) {}
  void beginTransmission(uint8_t);
  void write(uint8_t);
  uint8_t endTransmission(bool stop = true);
  uint8_t requestFrom(uint8_t addr, uint8_t n);
  int available();
  int read();
};
extern TwoWire Wire;
