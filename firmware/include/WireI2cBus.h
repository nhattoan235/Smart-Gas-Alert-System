#pragma once

#include <Arduino.h>
#include <Wire.h>

#include "SensorDrivers.h"

class WireI2cBus : public II2cBus {
 public:
  explicit WireI2cBus(TwoWire &wire) : wire_(wire) {}

  // No pin, bus-speed, or transaction-timeout defaults are supplied: pass
  // values verified for the actual board wiring and attached modules.
  bool begin(int sdaPin, int sclPin, uint32_t frequencyHz,
             uint16_t timeoutMs);
  bool write(uint8_t address, const uint8_t *data, uint8_t length) override;
  bool writeRead(uint8_t address, const uint8_t *writeData,
                 uint8_t writeLength, uint8_t *readData,
                 uint8_t readLength) override;

 private:
  TwoWire &wire_;
};
