#pragma once
#include <OneWire.h>

#define DEVICE_DISCONNECTED_C -127.0f

float hostTempC();

struct DallasTemperature {
  explicit DallasTemperature(OneWire *) {}
  void begin() {}
  void setResolution(int) {}
  void setWaitForConversion(bool) {}
  void requestTemperatures() {}
  float getTempCByIndex(int) { return hostTempC(); }
};
