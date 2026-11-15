#include "SensorDrivers.h"

#include <math.h>

namespace {
static const uint8_t ADS1115_REG_CONVERSION = 0x00;
static const uint8_t ADS1115_REG_CONFIG = 0x01;
static const uint32_t SHT31_HIGH_REPEATABILITY_WAIT_MS = 16;
static const uint32_t ADS1115_TIMEOUT_PERIODS = 3;

static bool validAdsAddress(uint8_t address) {
  return address >= 0x48 && address <= 0x4B;
}

static uint32_t adsConversionPeriodMs(Ads1115Rate rate) {
  static const uint16_t samplesPerSecond[8] = {8, 16, 32, 64, 128, 250, 475, 860};
  const uint16_t sps = samplesPerSecond[static_cast<uint8_t>(rate)];
  return (1000UL + sps - 1) / sps;
}
}  // namespace

Ads1115ChannelSensor::Ads1115ChannelSensor(II2cBus &bus, uint8_t address,
                                           Ads1115Channel channel,
                                           Ads1115Gain gain, Ads1115Rate rate)
    : bus_(bus),
      address_(address),
      channel_(channel),
      gain_(gain),
      rate_(rate),
      conversionPending_(false),
      conversionStartedAtMs_(0) {}

bool Ads1115ChannelSensor::configIsValid() const {
  return validAdsAddress(address_) && static_cast<uint8_t>(channel_) <= 3 &&
         static_cast<uint8_t>(gain_) <= 5 && static_cast<uint8_t>(rate_) <= 7;
}

bool Ads1115ChannelSensor::readRegister(uint8_t reg, uint16_t &value) {
  uint8_t buffer[2] = {};
  if (!bus_.writeRead(address_, &reg, 1, buffer, sizeof(buffer))) return false;
  value = static_cast<uint16_t>((static_cast<uint16_t>(buffer[0]) << 8) |
                                static_cast<uint16_t>(buffer[1]));
  return true;
}

bool Ads1115ChannelSensor::writeRegister(uint8_t reg, uint16_t value) {
  const uint8_t buffer[3] = {
      reg, static_cast<uint8_t>(value >> 8), static_cast<uint8_t>(value & 0xFF)};
  return bus_.write(address_, buffer, sizeof(buffer));
}

RawSensorSample Ads1115ChannelSensor::sample(uint32_t nowMs) {
  RawSensorSample result = {0, nowMs, SensorReadStatus::NOT_CONFIGURED};
  if (!configIsValid()) return result;

  if (!conversionPending_) {
    // Single-ended mux codes are 100b through 111b for A0 through A3.
    const uint16_t mux = static_cast<uint16_t>(4 + static_cast<uint8_t>(channel_));
    const uint16_t config = static_cast<uint16_t>(0x8000 | (mux << 12) |
        (static_cast<uint16_t>(gain_) << 9) | 0x0100 |
        (static_cast<uint16_t>(rate_) << 5) | 0x0003);
    if (!writeRegister(ADS1115_REG_CONFIG, config)) {
      result.status = SensorReadStatus::BUS_ERROR;
      return result;
    }
    conversionPending_ = true;
    conversionStartedAtMs_ = nowMs;
    result.status = SensorReadStatus::NOT_READY;
    return result;
  }

  uint16_t config = 0;
  if (!readRegister(ADS1115_REG_CONFIG, config)) {
    conversionPending_ = false;
    result.status = SensorReadStatus::BUS_ERROR;
    return result;
  }
  if ((config & 0x8000) == 0) {
    const uint32_t timeoutMs =
        adsConversionPeriodMs(rate_) * ADS1115_TIMEOUT_PERIODS;
    if (static_cast<uint32_t>(nowMs - conversionStartedAtMs_) >= timeoutMs) {
      conversionPending_ = false;
      result.status = SensorReadStatus::TIMEOUT;
    } else {
      result.status = SensorReadStatus::NOT_READY;
    }
    return result;
  }

  uint16_t conversion = 0;
  if (!readRegister(ADS1115_REG_CONVERSION, conversion)) {
    conversionPending_ = false;
    result.status = SensorReadStatus::BUS_ERROR;
    return result;
  }
  conversionPending_ = false;
  int32_t signedConversion = conversion;
  if (conversion & 0x8000) signedConversion -= 0x10000;
  result.rawCounts = static_cast<int16_t>(signedConversion);
  result.status = SensorReadStatus::OK;
  return result;
}

Sht31Sensor::Sht31Sensor(II2cBus &bus, uint8_t address)
    : bus_(bus), address_(address), conversionPending_(false),
      conversionStartedAtMs_(0) {}

bool Sht31Sensor::addressIsValid() const {
  return address_ >= 0x08 && address_ <= 0x77;
}

uint8_t Sht31Sensor::crc8(const uint8_t *data, uint8_t length) {
  uint8_t crc = 0xFF;
  for (uint8_t i = 0; i < length; ++i) {
    crc ^= data[i];
    for (uint8_t bit = 0; bit < 8; ++bit)
      crc = (crc & 0x80) ? static_cast<uint8_t>((crc << 1) ^ 0x31)
                         : static_cast<uint8_t>(crc << 1);
  }
  return crc;
}

EnvironmentSample Sht31Sensor::sample(uint32_t nowMs) {
  EnvironmentSample result = {NAN, NAN, nowMs, SensorReadStatus::NOT_CONFIGURED};
  if (!addressIsValid()) return result;

  if (!conversionPending_) {
    // 0x2400 requests high repeatability, single-shot, without clock stretching.
    const uint8_t command[2] = {0x24, 0x00};
    if (!bus_.write(address_, command, sizeof(command))) {
      result.status = SensorReadStatus::BUS_ERROR;
      return result;
    }
    conversionStartedAtMs_ = nowMs;
    conversionPending_ = true;
    result.status = SensorReadStatus::NOT_READY;
    return result;
  }

  if (static_cast<uint32_t>(nowMs - conversionStartedAtMs_) <
      SHT31_HIGH_REPEATABILITY_WAIT_MS) {
    result.status = SensorReadStatus::NOT_READY;
    return result;
  }

  uint8_t data[6] = {};
  if (!bus_.writeRead(address_, nullptr, 0, data, sizeof(data))) {
    conversionPending_ = false;
    result.status = SensorReadStatus::BUS_ERROR;
    return result;
  }
  conversionPending_ = false;
  if (crc8(data, 2) != data[2] || crc8(data + 3, 2) != data[5]) {
    result.status = SensorReadStatus::CRC_ERROR;
    return result;
  }

  const uint16_t rawTemperature =
      static_cast<uint16_t>((static_cast<uint16_t>(data[0]) << 8) | data[1]);
  const uint16_t rawHumidity =
      static_cast<uint16_t>((static_cast<uint16_t>(data[3]) << 8) | data[4]);
  result.temperatureC = -45.0f + 175.0f * rawTemperature / 65535.0f;
  result.relativeHumidityPct = 100.0f * rawHumidity / 65535.0f;
  result.status = SensorReadStatus::OK;
  return result;
}
