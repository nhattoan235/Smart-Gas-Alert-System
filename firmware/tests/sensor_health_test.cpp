// Advisory sensor-health regression cases; no thresholds here are gas limits.
// Run from firmware/ with a host C++ compiler:
//   g++ -std=c++11 -Iinclude tests/sensor_health_test.cpp src/Detector.cpp -o sensor_health_test
//   ./sensor_health_test

#include <assert.h>
#include <stdio.h>
#include <string.h>

#include "Detector.h"
#include "SensorHealth.h"

static RawSensorSample validRaw(int16_t raw, uint32_t at) {
  RawSensorSample sample = {raw, at, SensorReadStatus::OK};
  return sample;
}

static void makeCalibrated(CalibrationRecord &record) {
  record.state = CalibrationState::CALIBRATED;
  record.schemaVersion = 1;
  record.calibratedAtUnixSeconds = 1234;
  strcpy(record.profileId, "test-reference");
  strcpy(record.provenance, "synthetic-test-only");
  record.rawReferenceCounts = 1000;
  record.rawReferenceValid = true;
}

static void testCalibrationStartsUncalibrated() {
  CalibrationRecord record{};
  assert(record.state == CalibrationState::UNCALIBRATED);
  assert(!record.usable());
  assert(record.schemaVersion == 0 && record.profileId[0] == '\0');
  assert(record.provenance[0] == '\0' && !record.rawReferenceValid);
}

static void testDataAgeAndInvalidStatus() {
  SensorHealthPolicy policy{};
  policy.maxDataAgeMs = 50;
  SensorHealthMonitor monitor(policy);
  CalibrationRecord calibration{};
  CalibrationReference reference{};

  assert(!monitor.update(validRaw(1000, 100), 150, calibration, reference).dataStale);
  assert(monitor.update(validRaw(1000, 100), 151, calibration, reference).dataStale);
  RawSensorSample invalid = {1000, 152, SensorReadStatus::BUS_ERROR};
  assert(monitor.update(invalid, 152, calibration, reference).dataStale);
}

static void testUnchangedCandidateIsAdvisoryOnly() {
  SensorHealthPolicy policy{};
  policy.stuckAfterMs = 300;
  policy.stuckMaxDeltaCounts = 0;
  SensorHealthMonitor monitor(policy);
  CalibrationRecord calibration{};
  CalibrationReference reference{};
  Detector detector;
  SensorHealthStatus health{};

  for (uint32_t now = 0; now <= 6000; now += 100) {
    RawSensorSample raw = validRaw(1234, now);
    health = monitor.update(raw, now, calibration, reference, false, true);
    assert(raw.valid());
    assert(detector.update(5.0f, true, now) ==
           (now < 5400 ? AlarmState::WARMUP : AlarmState::SAFE));
  }
  assert(health.unchangedCandidate);
  assert(detector.state() == AlarmState::SAFE);

  health = monitor.update(validRaw(1235, 6100), 6100, calibration, reference);
  assert(!health.unchangedCandidate);
}

static void testNormalStableSignalNeedsExplicitTestContext() {
  SensorHealthPolicy policy{};
  policy.stuckAfterMs = 100;
  SensorHealthMonitor monitor(policy);
  CalibrationRecord calibration{};
  CalibrationReference reference{};
  SensorHealthStatus status{};
  for (uint32_t now = 0; now <= 1000; now += 100)
    status = monitor.update(validRaw(2000, now), now, calibration, reference);
  assert(!status.unchangedCandidate);
}

static void testStaleSampleDoesNotFeedUnchangedDiagnostic() {
  SensorHealthPolicy policy{};
  policy.maxDataAgeMs = 10;
  policy.stuckAfterMs = 10;
  SensorHealthMonitor monitor(policy);
  CalibrationRecord calibration{};
  CalibrationReference reference{};
  monitor.update(validRaw(2000, 0), 0, calibration, reference, false, true);
  SensorHealthStatus status =
      monitor.update(validRaw(2000, 0), 20, calibration, reference, false, true);
  assert(status.dataStale && !status.unchangedCandidate);
}

static void testAdcRailIsReportedSeparately() {
  SensorHealthPolicy policy{};
  SensorHealthMonitor monitor(policy);
  CalibrationRecord calibration{};
  CalibrationReference reference{};
  assert(monitor.update(validRaw(INT16_MIN, 10), 10, calibration, reference).adcRail);
  assert(monitor.update(validRaw(100, 20), 20, calibration, reference).adcRail == false);
}

static void testReferenceDriftNeedsCalibrationAndVerifiedCondition() {
  SensorHealthPolicy policy{};
  policy.referenceCheckAfterMs = 500;
  policy.maxReferenceDeviationCounts = 50;
  SensorHealthMonitor monitor(policy);
  CalibrationRecord calibration{};
  CalibrationReference reference = {1000, true};

  SensorHealthStatus status = monitor.update(validRaw(1200, 0), 0,
                                              calibration, reference, true);
  assert(status.calibrationRequired && !status.referenceDriftSuspected);

  makeCalibrated(calibration);
  status = monitor.update(validRaw(1200, 100), 100, calibration, reference, false);
  assert(!status.calibrationRequired && !status.referenceDriftSuspected);
  status = monitor.update(validRaw(1200, 200), 200, calibration, reference, true);
  assert(!status.referenceDriftSuspected);
  status = monitor.update(validRaw(1200, 699), 699, calibration, reference, true);
  assert(!status.referenceDriftSuspected);
  status = monitor.update(validRaw(1200, 700), 700, calibration, reference, true);
  assert(status.referenceDriftSuspected);

  status = monitor.update(validRaw(1200, 701), 701, calibration, reference, false);
  assert(!status.referenceDriftSuspected);
}

int main() {
  testCalibrationStartsUncalibrated();
  testDataAgeAndInvalidStatus();
  testUnchangedCandidateIsAdvisoryOnly();
  testNormalStableSignalNeedsExplicitTestContext();
  testStaleSampleDoesNotFeedUnchangedDiagnostic();
  testAdcRailIsReportedSeparately();
  testReferenceDriftNeedsCalibrationAndVerifiedCondition();
  puts("Sensor health mock cases passed.");
  return 0;
}
