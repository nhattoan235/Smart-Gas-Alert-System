#pragma once

#include <stdint.h>

enum class AlarmState : uint8_t { WARMUP, SAFE, WATCH, WARNING, DANGER, FAULT };

const char *stateName(AlarmState state);

// These relative-signal values are demonstration parameters, not gas-safety limits.
class Detector {
 public:
  AlarmState update(float signal, bool valid, uint32_t nowMs);
  AlarmState state() const { return state_; }
  float filtered() const { return filtered_; }
  float baseline() const { return baseline_; }
  float rise() const { return filtered_ - baseline_; }
  bool ready() const { return samples_ >= 5; }
  bool inspectionRequired() const { return inspectionRequired_; }
  bool canConfirmInspection(uint32_t nowMs) const {
    return state_ == AlarmState::WATCH && inspectionRequired_ && clearSince_ && nowMs - clearSince_ >= 10000;
  }
  void markInspected(uint32_t nowMs) { if (canConfirmInspection(nowMs)) inspected_ = true; }

 private:
  float recent_[5] = {};
  uint8_t index_ = 0;
  uint8_t samples_ = 0;
  float filtered_ = 0;
  float baseline_ = 0;
  AlarmState state_ = AlarmState::WARMUP;
  uint32_t watchSince_ = 0;
  uint32_t warningSince_ = 0;
  uint32_t dangerSince_ = 0;
  uint32_t slowRiseSince_ = 0;
  uint32_t clearSince_ = 0;
  uint32_t recoverySince_ = 0;
  uint32_t invalidSince_ = 0;
  uint32_t validSince_ = 0;
  bool inspectionRequired_ = false;
  bool inspected_ = false;
};
