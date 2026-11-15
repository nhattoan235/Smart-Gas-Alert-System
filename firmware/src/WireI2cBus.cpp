#include "WireI2cBus.h"

bool WireI2cBus::begin(int sdaPin, int sclPin, uint32_t frequencyHz,
                       uint16_t timeoutMs) {
  if (!sensor_driver_checks::isValidI2cBusConfiguration(
          sdaPin, sclPin, frequencyHz, timeoutMs)) return false;
  wire_.begin(sdaPin, sclPin, frequencyHz);
  wire_.setTimeOut(timeoutMs);
  return true;
}

bool WireI2cBus::write(uint8_t address, const uint8_t *data, uint8_t length) {
  if (!data || length == 0) return false;
  wire_.beginTransmission(address);
  if (wire_.write(data, length) != length) {
    wire_.endTransmission();
    return false;
  }
  return wire_.endTransmission() == 0;
}

bool WireI2cBus::writeRead(uint8_t address, const uint8_t *writeData,
                           uint8_t writeLength, uint8_t *readData,
                           uint8_t readLength) {
  if (!readData || readLength == 0 || (writeLength && !writeData)) return false;
  if (writeLength) {
    wire_.beginTransmission(address);
    if (wire_.write(writeData, writeLength) != writeLength) {
      wire_.endTransmission();
      return false;
    }
    if (wire_.endTransmission(false) != 0) return false;
  }

  if (wire_.requestFrom(address, readLength) != readLength) return false;
  for (uint8_t i = 0; i < readLength; ++i) {
    const int value = wire_.read();
    if (value < 0) return false;
    readData[i] = static_cast<uint8_t>(value);
  }
  return true;
}
