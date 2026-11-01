// Runs firmware/esp32_pump_node/esp32_pump_node.ino unchanged, on a PC, with simulated sensors on a
// virtual clock. Built and checked by tools/test_firmware.py. Arguments are key=value pairs.
#include <Arduino.h>
#include <Wire.h>
#include <DallasTemperature.h>
#include <cstring>
#include <map>
#include <random>
#include <string>

#include "esp32_pump_node.ino"

static const double M_PI = 3.14159265358979323846;
static std::map<std::string, double> P;
static double arg(const char *k, double d) { auto i = P.find(k); return i == P.end() ? d : i->second; }

SerialPort Serial;
TwoWire Wire;
static uint64_t nowUs = 0;
static std::mt19937 rng(7);
static std::normal_distribution<double> gauss(0, 1);
static void (*flowIsr)() = nullptr, (*hallIsr)() = nullptr;

static double tS() { return nowUs / 1e6; }
// The pump starts after boot, because the sketch measures the current sensor's zero at boot.
static bool pumpOn() { return tS() >= arg("start_s", 1.0) && tS() < arg("stop_s", 1e9); }

uint32_t micros() { return (uint32_t)nowUs; }
uint32_t millis() { return (uint32_t)(nowUs / 1000); }
void delayMicroseconds(uint32_t us) { nowUs += us; }
void analogReadResolution(int) {}
void analogSetPinAttenuation(int, int) {}
void pinMode(int, int) {}
int digitalRead(int pin) { return pin == PIN_FLOAT && arg("float_low", 0) == 0 ? LOW : HIGH; }
void attachInterrupt(int pin, void (*fn)(), int) {
  if (pin == PIN_FLOW) flowIsr = fn;
  if (pin == PIN_HALL) hallIsr = fn;
}

uint32_t analogReadMilliVolts(int pin) {
  double v = 0;
  nowUs += 40;                                  // an ADC read takes real time
  if (pin == PIN_ACS712) {
    if (arg("no_acs", 0)) return 0;
    double amps = pumpOn() ? arg("amps", 2.0) : 0;
    double wave = arg("ac", 1) ? std::sqrt(2.0) * amps * std::sin(2 * M_PI * 50 * tS()) : amps;
    v = (2.5 + wave * ACS_SENS_V_PER_A) * ACS_DIVIDER;
  } else if (pin == PIN_PRESSURE) {
    if (arg("no_pressure", 0)) return 0;
    double bar = pumpOn() ? arg("bar", 2.0) : 0;
    v = (P_V_MIN + bar / P_BAR_MAX * (P_V_MAX - P_V_MIN)) * P_DIVIDER;
  }
  double mv = v * 1000 + gauss(rng) * arg("adc_noise_mv", 4);
  mv = std::fmax(0.0, std::fmin(3100.0, mv));   // the ESP32 ADC tops out near 3.1 V at 11 dB
  return (uint32_t)mv;
}

float hostTempC() {
  if (arg("no_temp", 0) || tS() >= arg("lose_temp_s", 1e9)) return DEVICE_DISCONNECTED_C;
  return (float)arg("temp", 40);
}

// ---------------------------------------------------------------- simulated ADXL345
static uint8_t reg = 0, rx[6];
static int rxN = 0, rxI = 0;
static bool adxlThere() { return !arg("no_adxl", 0) && (tS() < arg("lose_adxl_s", 1e9) || tS() >= arg("back_adxl_s", 1e9)); }
void TwoWire::beginTransmission(uint8_t) { rxN = 0; rxI = 0; }
void TwoWire::write(uint8_t v) { if (rxN == 0) reg = v; rxN++; }
uint8_t TwoWire::endTransmission(bool) { rxN = 0; nowUs += 30; return adxlThere() ? 0 : 2; }
uint8_t TwoWire::requestFrom(uint8_t, uint8_t n) {
  nowUs += 160;
  rxI = rxN = 0;
  if (!adxlThere()) return 0;
  if (reg == 0x00) { rx[0] = 0xE5; rxN = 1; return 1; }
  double vib = pumpOn() ? arg("vib", 0.12) * std::sqrt(2.0) : 0;
  double g[3] = {vib * std::sin(2 * M_PI * 24.7 * tS()), vib * std::sin(2 * M_PI * 24.7 * tS() + 1),
                 1.0 + vib * std::sin(2 * M_PI * 24.7 * tS() + 2)};       // gravity on Z must be removed
  for (int i = 0; i < 3; i++) {
    int16_t raw = (int16_t)std::lround((g[i] + gauss(rng) * 0.004) / 0.0039);
    rx[2 * i] = raw & 0xFF; rx[2 * i + 1] = (raw >> 8) & 0xFF;
  }
  rxN = n;
  return n;
}
int TwoWire::available() { return rxN - rxI; }
int TwoWire::read() { return rxI < rxN ? rx[rxI++] : -1; }

// ---------------------------------------------------------------- main loop on the virtual clock
int main(int argc, char **argv) {
  for (int i = 1; i < argc; i++) {
    const char *eq = strchr(argv[i], '=');
    if (eq) P[std::string(argv[i], eq - argv[i])] = atof(eq + 1);
  }
  double seconds = arg("seconds", 10);
  setup();
  uint64_t nextFlow = 0, nextHall = 0;
  while (tS() < seconds) {
    loop();
    nowUs += 20;
    double flowHz = pumpOn() ? arg("lpm", 18) * FLOW_HZ_PER_LPM : 0;
    double hallHz = pumpOn() ? arg("rpm", 1480) * HALL_MAGNETS / 60.0 : 0;
    // Fire on schedule, however long the last loop took: real interrupts don't wait for loop().
    if (nextFlow < nowUs && flowHz > 0 && nowUs - nextFlow > 100000) nextFlow = nowUs;
    if (nextHall < nowUs && hallHz > 0 && nowUs - nextHall > 100000) nextHall = nowUs;
    while (flowHz > 0 && nowUs >= nextFlow) { flowIsr(); nextFlow += (uint64_t)(1e6 / flowHz); }
    while (hallHz > 0 && nowUs >= nextHall) { hallIsr(); nextHall += (uint64_t)(1e6 / hallHz); }
  }
  return 0;
}
