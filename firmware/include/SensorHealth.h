#pragma once

#include <stdint.h>

#include "SensorDrivers.h"

enum class CalibrationState : uint8_t { UNCALIBRATED, CALIBRATED };

// Metadata only. No gas response curve, Rs/R0 ratio, ppm coefficient, or
// threshold is implied by an uncalibrated profile.
struct CalibrationRecord {
  CalibrationState state = CalibrationState::UNCALIBRATED;
  uint16_t schemaVersion = 0;
  uint32_t calibratedAtUnixSeconds = 0;
  char profileId[24] = {};
  char provenance[24] = {};
  int16_t rawReferenceCounts = 0;
  bool rawReferenceValid = false;

  bool usable() const {
    return state == CalibrationState::CALIBRATED && schemaVersion != 0 &&
           profileId[0] != '\0' && provenance[0] != '\0';
  }
};

class IRelativeGasSignalTransform {
 public:
  virtual ~IRelativeGasSignalTransform() {}
  // Implementations require a verified profile and must document their
  // calibration data. No default transform is provided by this project.
  virtual bool toRelativeSignal(int16_t rawCounts,
                                const CalibrationRecord &calibration,
                                float &signalPct) const = 0;
};

// A reference may be recorded only by a separately verified calibration
// procedure. This argument is volatile/external to SensorHealthMonitor; no
// reference is created or persisted automatically by the firmware.
struct CalibrationReference {
  int16_t rawCounts = 0;
  bool valid = false;
};

// All limits default to disabled. A deployed profile must set them from a
// documented test plan; these diagnostics never change the alarm state.
struct SensorHealthPolicy {
  uint32_t maxDataAgeMs = 0;
  uint32_t stuckAfterMs = 0;
  uint16_t stuckMaxDeltaCounts = 0;
  uint32_t referenceCheckAfterMs = 0;
  uint16_t maxReferenceDeviationCounts = 0;
};

struct SensorHealthStatus {
  bool dataStale = false;
  bool adcRail = false;
  bool unchangedCandidate = false;
  bool calibrationRequired = true;
  bool referenceDriftSuspected = false;
  uint32_t dataAgeMs = 0;
};

// These are advisory sensor-health checks. In particular, an unchanged
// reading is not rejected: a stable environment and a physically stuck sensor
// are not distinguishable without independent test context.
class SensorHealthMonitor {
 public:
  explicit SensorHealthMonitor(const SensorHealthPolicy &policy)
      : policy_(policy), haveAnchor_(false), anchorRaw_(0), anchorSinceMs_(0),
        referenceSinceMs_(0), referenceWindowActive_(false) {}

  SensorHealthStatus update(const RawSensorSample &sample, uint32_t nowMs,
                            const CalibrationRecord &calibration,
                            const CalibrationReference &reference,
                            bool verifiedReferenceCondition = false,
                            bool verifiedUnchangedContext = false) {
    SensorHealthStatus result{};
    result.calibrationRequired = !calibration.usable();

    if (sample.valid()) {
      result.dataAgeMs = static_cast<uint32_t>(nowMs - sample.sampledAtMs);
      result.dataStale = policy_.maxDataAgeMs != 0 &&
                         result.dataAgeMs > policy_.maxDataAgeMs;
      result.adcRail = sample.rawCounts == INT16_MIN ||
                       sample.rawCounts == INT16_MAX;
      if (!result.dataStale) {
        updateUnchangedCandidate(sample, nowMs, verifiedUnchangedContext, result);
        updateReferenceDrift(sample, nowMs, calibration, reference,
                             verifiedReferenceCondition, result);
      } else {
        haveAnchor_ = false;
        referenceWindowActive_ = false;
      }
    } else {
      result.dataStale = true;
      haveAnchor_ = false;
      referenceWindowActive_ = false;
    }
    return result;
  }

 private:
  static uint32_t absoluteDifference(int16_t a, int16_t b) {
    const int32_t delta = static_cast<int32_t>(a) - static_cast<int32_t>(b);
    return static_cast<uint32_t>(delta < 0 ? -delta : delta);
  }

  void updateUnchangedCandidate(const RawSensorSample &sample, uint32_t nowMs,
                                bool verifiedContext,
                                SensorHealthStatus &result) {
    if (!verifiedContext || policy_.stuckAfterMs == 0) {
      haveAnchor_ = false;
      return;
    }
    if (!haveAnchor_ || absoluteDifference(sample.rawCounts, anchorRaw_) >
                            policy_.stuckMaxDeltaCounts) {
      anchorRaw_ = sample.rawCounts;
      anchorSinceMs_ = sample.sampledAtMs;
      haveAnchor_ = true;
    }
    result.unchangedCandidate =
        haveAnchor_ && static_cast<uint32_t>(nowMs - anchorSinceMs_) >=
                           policy_.stuckAfterMs;
  }

  void updateReferenceDrift(const RawSensorSample &sample, uint32_t nowMs,
                            const CalibrationRecord &calibration,
                            const CalibrationReference &reference,
                            bool verifiedReferenceCondition,
                            SensorHealthStatus &result) {
    const bool configured = calibration.usable() && reference.valid &&
        verifiedReferenceCondition && policy_.referenceCheckAfterMs != 0 &&
        policy_.maxReferenceDeviationCounts != 0;
    if (!configured) {
      referenceWindowActive_ = false;
      return;
    }
    if (!referenceWindowActive_) {
      referenceSinceMs_ = sample.sampledAtMs;
      referenceWindowActive_ = true;
    }
    const bool checkWindowElapsed = static_cast<uint32_t>(nowMs - referenceSinceMs_) >=
                                    policy_.referenceCheckAfterMs;
    result.referenceDriftSuspected = checkWindowElapsed &&
        absoluteDifference(sample.rawCounts, reference.rawCounts) >
        policy_.maxReferenceDeviationCounts;
  }

  SensorHealthPolicy policy_;
  bool haveAnchor_;
  int16_t anchorRaw_;
  uint32_t anchorSinceMs_;
  uint32_t referenceSinceMs_;
  bool referenceWindowActive_;
};
