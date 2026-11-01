// Just enough of the Arduino-ESP32 API to run the real sketch on a PC against simulated sensors.
#pragma once
#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdarg>
#include <cstdlib>

#define IRAM_ATTR
#define INPUT 0x01
#define INPUT_PULLUP 0x05
#define INPUT_PULLDOWN 0x09
#define RISING 1
#define FALLING 2
#define LOW 0
#define HIGH 1
#define ADC_11db 3
#define digitalPinToInterrupt(p) (p)

using std::isnan;
using std::sqrt;
template <class T> T min(T a, T b) { return a < b ? a : b; }
template <class T> T max(T a, T b) { return a > b ? a : b; }

uint32_t micros();
uint32_t millis();
void delayMicroseconds(uint32_t us);
inline void noInterrupts() {}
inline void interrupts() {}
void analogReadResolution(int);
void analogSetPinAttenuation(int, int);
uint32_t analogReadMilliVolts(int pin);
void pinMode(int pin, int mode);
int digitalRead(int pin);
void attachInterrupt(int pin, void (*fn)(), int mode);

struct SerialPort {
  void begin(int) {}
  void printf(const char *fmt, ...) {
    va_list a; va_start(a, fmt); vfprintf(stdout, fmt, a); va_end(a); fflush(stdout);
  }
};
extern SerialPort Serial;
