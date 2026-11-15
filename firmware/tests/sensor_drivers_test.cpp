// Mocked driver checks; no GPIO, sensor module, or Wokwi session is used.
// Run from firmware/ with a host C++ compiler:
//   g++ -std=c++11 -Iinclude tests/sensor_drivers_test.cpp src/SensorDrivers.cpp -o sensor_drivers_test
//   ./sensor_drivers_test

#include <assert.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "SensorDrivers.h"

class FakeI2cBus : public II2cBus {
 public:
  FakeI2cBus()
      : failWrite(false), failRead(false), adsReady(true), adsConfig(0),
        conversion(0x1234), lastAddress(0), lastLength(0), writes(0), reads(0),
        corruptShtCrc(false) {
    memset(lastWrite, 0, sizeof(lastWrite));
    // Midscale temperature and quarter-scale relative humidity data words.
    shtData[0] = 0x80; shtData[1] = 0x00;
    shtData[3] = 0x40; shtData[4] = 0x00;
    shtData[2] = crc8(shtData, 2);
    shtData[5] = crc8(shtData + 3, 2);
  }

  bool write(uint8_t address, const uint8_t *data, uint8_t length) override {
    if (failWrite || !data || !length || length > sizeof(lastWrite)) return false;
    lastAddress = address;
    lastLength = length;
    memcpy(lastWrite, data, length);
    ++writes;
    if (address == 0x48 && length == 3 && data[0] == 0x01)
      adsConfig = static_cast<uint16_t>((data[1] << 8) | data[2]);
    return true;
  }

  bool writeRead(uint8_t address, const uint8_t *writeData,
                 uint8_t writeLength, uint8_t *readData,
                 uint8_t readLength) override {
    if (failRead || !readData) return false;
    lastAddress = address;
    ++reads;
    if (address == 0x48 && writeLength == 1 && readLength == 2) {
      const uint8_t reg = writeData[0];
      uint16_t value = 0;
      if (reg == 0x01) value = adsReady ? static_cast<uint16_t>(adsConfig | 0x8000) :
                                          static_cast<uint16_t>(adsConfig & 0x7FFF);
      else if (reg == 0x00) value = conversion;
      else return false;
      readData[0] = static_cast<uint8_t>(value >> 8);
      readData[1] = static_cast<uint8_t>(value);
      return true;
    }
    if (address == 0x44 && writeLength == 0 && readLength == 6) {
      memcpy(readData, shtData, sizeof(shtData));
      if (corruptShtCrc) readData[2] ^= 0x01;
      return true;
    }
    return false;
  }

  static uint8_t crc8(const uint8_t *data, uint8_t length) {
    uint8_t crc = 0xFF;
    for (uint8_t i = 0; i < length; ++i) {
      crc ^= data[i];
      for (uint8_t bit = 0; bit < 8; ++bit)
        crc = (crc & 0x80) ? static_cast<uint8_t>((crc << 1) ^ 0x31) :
                             static_cast<uint8_t>(crc << 1);
    }
    return crc;
  }

  bool failWrite;
  bool failRead;
  bool adsReady;
  uint16_t adsConfig;
  uint16_t conversion;
  uint8_t lastAddress;
  uint8_t lastLength;
  uint8_t lastWrite[8];
  unsigned writes;
  unsigned reads;
  bool corruptShtCrc;
  uint8_t shtData[6];
};

static void testAds1115ConfigAndRead() {
  FakeI2cBus bus;
  bus.conversion = 0xFEDC;
  Ads1115ChannelSensor sensor(bus, 0x48, ADS1115_MQ7B_CHANNEL,
                              Ads1115Gain::FS_4V096, Ads1115Rate::SPS_128);
  RawSensorSample sample = sensor.sample(100);
  assert(sample.status == SensorReadStatus::NOT_READY);
  assert(bus.lastAddress == 0x48 && bus.lastLength == 3);
  assert(bus.lastWrite[0] == 0x01);
  assert(((bus.adsConfig >> 12) & 0x07) == 0x05);  // Single-ended A1.
  assert(((bus.adsConfig >> 9) & 0x07) == 0x01);   // Caller-selected PGA.
  assert(((bus.adsConfig >> 5) & 0x07) == 0x04);   // Caller-selected 128 SPS.

  sample = sensor.sample(101);
  assert(sample.status == SensorReadStatus::OK);
  assert(sample.rawCounts == static_cast<int16_t>(0xFEDC));
  assert(sample.sampledAtMs == 101);
}

static void testAds1115TimeoutUsesConfiguredRate() {
  FakeI2cBus bus;
  bus.adsReady = false;
  Ads1115ChannelSensor sensor(bus, 0x48, Ads1115Channel::A0,
                              Ads1115Gain::FS_2V048, Ads1115Rate::SPS_8);
  assert(sensor.sample(1000).status == SensorReadStatus::NOT_READY);
  assert(sensor.sample(1374).status == SensorReadStatus::NOT_READY);
  assert(sensor.sample(1375).status == SensorReadStatus::TIMEOUT);
  assert(sensor.sample(1376).status == SensorReadStatus::NOT_READY);
}

static void testAds1115BusAndConfigurationErrors() {
  FakeI2cBus bus;
  bus.failWrite = true;
  Ads1115ChannelSensor busError(bus, 0x48, Ads1115Channel::A0,
                                Ads1115Gain::FS_2V048, Ads1115Rate::SPS_128);
  assert(busError.sample(0).status == SensorReadStatus::BUS_ERROR);

  Ads1115ChannelSensor notConfigured(bus, 0x47, Ads1115Channel::A0,
                                     Ads1115Gain::FS_2V048, Ads1115Rate::SPS_128);
  assert(notConfigured.sample(0).status == SensorReadStatus::NOT_CONFIGURED);
}

static void testSht31CrcAndConversion() {
  FakeI2cBus bus;
  Sht31Sensor sensor(bus, 0x44);
  assert(sensor.sample(0).status == SensorReadStatus::NOT_READY);
  assert(bus.lastWrite[0] == 0x24 && bus.lastWrite[1] == 0x00);
  assert(sensor.sample(15).status == SensorReadStatus::NOT_READY);
  EnvironmentSample result = sensor.sample(16);
  assert(result.status == SensorReadStatus::OK);
  assert(fabsf(result.temperatureC - 42.5f) < 0.1f);
  assert(fabsf(result.relativeHumidityPct - 25.0f) < 0.1f);

  FakeI2cBus badCrcBus;
  badCrcBus.corruptShtCrc = true;
  Sht31Sensor badCrcSensor(badCrcBus, 0x44);
  badCrcSensor.sample(0);
  assert(badCrcSensor.sample(16).status == SensorReadStatus::CRC_ERROR);
}

static void testSht31BusAndAddressErrors() {
  FakeI2cBus bus;
  bus.failWrite = true;
  Sht31Sensor busError(bus, 0x44);
  assert(busError.sample(0).status == SensorReadStatus::BUS_ERROR);

  Sht31Sensor notConfigured(bus, 0x00);
  assert(notConfigured.sample(0).status == SensorReadStatus::NOT_CONFIGURED);
}

static void testRawGasRailCodesAreRejected() {
  RawSensorSample normal = {1234, 0, SensorReadStatus::OK};
  RawSensorSample lowRail = {INT16_MIN, 0, SensorReadStatus::OK};
  RawSensorSample highRail = {INT16_MAX, 0, SensorReadStatus::OK};
  RawSensorSample invalid = {1234, 0, SensorReadStatus::BUS_ERROR};
  assert(sensor_driver_checks::isUsableRawGasSample(normal));
  assert(!sensor_driver_checks::isUsableRawGasSample(lowRail));
  assert(!sensor_driver_checks::isUsableRawGasSample(highRail));
  assert(!sensor_driver_checks::isUsableRawGasSample(invalid));
}

static void testSht31PhysicalRangesAreEnforced() {
  EnvironmentSample edgeLow = {-40.0f, 0.0f, 0, SensorReadStatus::OK};
  EnvironmentSample edgeHigh = {125.0f, 100.0f, 0, SensorReadStatus::OK};
  EnvironmentSample lowTemp = {-40.1f, 50.0f, 0, SensorReadStatus::OK};
  EnvironmentSample highTemp = {125.1f, 50.0f, 0, SensorReadStatus::OK};
  EnvironmentSample negativeHumidity = {20.0f, -0.1f, 0, SensorReadStatus::OK};
  EnvironmentSample excessiveHumidity = {20.0f, 100.1f, 0, SensorReadStatus::OK};
  EnvironmentSample notFinite = {NAN, NAN, 0, SensorReadStatus::OK};
  EnvironmentSample notRead = {20.0f, 50.0f, 0, SensorReadStatus::CRC_ERROR};
  assert(sensor_driver_checks::isUsableEnvironmentSample(edgeLow));
  assert(sensor_driver_checks::isUsableEnvironmentSample(edgeHigh));
  assert(!sensor_driver_checks::isUsableEnvironmentSample(lowTemp));
  assert(!sensor_driver_checks::isUsableEnvironmentSample(highTemp));
  assert(!sensor_driver_checks::isUsableEnvironmentSample(negativeHumidity));
  assert(!sensor_driver_checks::isUsableEnvironmentSample(excessiveHumidity));
  assert(!sensor_driver_checks::isUsableEnvironmentSample(notFinite));
  assert(!sensor_driver_checks::isUsableEnvironmentSample(notRead));
}

static void testI2cBusRequiresExplicitNonzeroTimeout() {
  assert(!sensor_driver_checks::isValidI2cBusConfiguration(8, 9, 100000, 0));
  assert(sensor_driver_checks::isValidI2cBusConfiguration(8, 9, 100000, 50));
}

int main() {
  testAds1115ConfigAndRead();
  testAds1115TimeoutUsesConfiguredRate();
  testAds1115BusAndConfigurationErrors();
  testSht31CrcAndConversion();
  testSht31BusAndAddressErrors();
  testRawGasRailCodesAreRejected();
  testSht31PhysicalRangesAreEnforced();
  testI2cBusRequiresExplicitNonzeroTimeout();
  puts("Sensor driver mock cases passed.");
  return 0;
}
