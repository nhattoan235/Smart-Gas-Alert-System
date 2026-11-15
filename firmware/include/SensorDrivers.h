#pragma once

#include <stdint.h>

// A hardware read reports raw sensor values and provenance. Gas calibration is
// deliberately outside these drivers; raw ADS1115 counts are never ppm.
enum class SensorReadStatus : uint8_t {
  OK,
  NOT_CONFIGURED,
  NOT_READY,
  TIMEOUT,
  BUS_ERROR,
  CRC_ERROR
};

struct RawSensorSample {
  int16_t rawCounts;
  uint32_t sampledAtMs;
  SensorReadStatus status;

  bool valid() const { return status == SensorReadStatus::OK; }
};

struct EnvironmentSample {
  float temperatureC;
  float relativeHumidityPct;
  uint32_t sampledAtMs;
  SensorReadStatus status;

  bool valid() const { return status == SensorReadStatus::OK; }
};

namespace sensor_driver_checks {

inline bool isUsableRawGasSample(const RawSensorSample &sample) {
  return sample.valid() && sample.rawCounts != INT16_MIN &&
         sample.rawCounts != INT16_MAX;
}

inline bool isUsableEnvironmentSample(const EnvironmentSample &sample) {
  // SHT31's specified operating range and the physical range of RH values.
  // Comparisons also reject NaN and infinities without normalizing the data.
  return sample.valid() && sample.temperatureC >= -40.0f &&
         sample.temperatureC <= 125.0f &&
         sample.relativeHumidityPct >= 0.0f &&
         sample.relativeHumidityPct <= 100.0f;
}

inline bool isValidI2cBusConfiguration(int sdaPin, int sclPin,
                                       uint32_t frequencyHz,
                                       uint16_t timeoutMs) {
  return sdaPin >= 0 && sclPin >= 0 && frequencyHz != 0 && timeoutMs != 0;
}

}  // namespace sensor_driver_checks

class IRawSensor {
 public:
  virtual ~IRawSensor() {}
  virtual RawSensorSample sample(uint32_t nowMs) = 0;
};

class IEnvironmentSensor {
 public:
  virtual ~IEnvironmentSensor() {}
  virtual EnvironmentSample sample(uint32_t nowMs) = 0;
};

// Mockable transport boundary. A production adapter can wrap Arduino Wire;
// tests can supply a deterministic fake without touching GPIO or a real bus.
class II2cBus {
 public:
  virtual ~II2cBus() {}
  virtual bool write(uint8_t address, const uint8_t *data, uint8_t length) = 0;
  virtual bool writeRead(uint8_t address, const uint8_t *writeData,
                         uint8_t writeLength, uint8_t *readData,
                         uint8_t readLength) = 0;
};

enum class Ads1115Channel : uint8_t { A0 = 0, A1 = 1, A2 = 2, A3 = 3 };
// MQ-6 AO -> ADS1115 A0 and MQ-7B AO -> ADS1115 A1 are the documented target.
static const Ads1115Channel ADS1115_MQ6_CHANNEL = Ads1115Channel::A0;
static const Ads1115Channel ADS1115_MQ7B_CHANNEL = Ads1115Channel::A1;

// Caller must select the PGA range and data rate for the confirmed analog
// front-end. The driver returns ADC counts and does not infer sensor voltage.
enum class Ads1115Gain : uint8_t {
  FS_6V144 = 0,
  FS_4V096 = 1,
  FS_2V048 = 2,
  FS_1V024 = 3,
  FS_0V512 = 4,
  FS_0V256 = 5
};

enum class Ads1115Rate : uint8_t {
  SPS_8 = 0,
  SPS_16 = 1,
  SPS_32 = 2,
  SPS_64 = 3,
  SPS_128 = 4,
  SPS_250 = 5,
  SPS_475 = 6,
  SPS_860 = 7
};

class Ads1115ChannelSensor : public IRawSensor {
 public:
  Ads1115ChannelSensor(II2cBus &bus, uint8_t address, Ads1115Channel channel,
                       Ads1115Gain gain, Ads1115Rate rate);
  RawSensorSample sample(uint32_t nowMs) override;

 private:
  bool configIsValid() const;
  bool readRegister(uint8_t reg, uint16_t &value);
  bool writeRegister(uint8_t reg, uint16_t value);

  II2cBus &bus_;
  uint8_t address_;
  Ads1115Channel channel_;
  Ads1115Gain gain_;
  Ads1115Rate rate_;
  bool conversionPending_;
  uint32_t conversionStartedAtMs_;
};

class Sht31Sensor : public IEnvironmentSensor {
 public:
  // Address is explicit because the project wiring source does not specify
  // the module's address-select strap.
  Sht31Sensor(II2cBus &bus, uint8_t address);
  EnvironmentSample sample(uint32_t nowMs) override;

 private:
  static uint8_t crc8(const uint8_t *data, uint8_t length);
  bool addressIsValid() const;

  II2cBus &bus_;
  uint8_t address_;
  bool conversionPending_;
  uint32_t conversionStartedAtMs_;
};
