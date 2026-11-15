#pragma once

#include <stdint.h>

enum class AlarmState : uint8_t { WARMUP, SAFE, WATCH, WARNING, DANGER, FAULT };

const char *stateName(AlarmState state);

struct DetectorTimer {
  uint32_t startedAt = 0;
  bool active = false;

  void start(uint32_t nowMs) {
    if (!active) {
      startedAt = nowMs;
      active = true;
    }
  }
  void restart(uint32_t nowMs) {
    startedAt = nowMs;
    active = true;
  }
  void reset() {
    startedAt = 0;
    active = false;
  }
  bool elapsed(uint32_t nowMs, uint32_t durationMs) const {
    return active && (uint32_t)(nowMs - startedAt) >= durationMs;
  }
};

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
    return state_ == AlarmState::WATCH && inspectionRequired_ && clearSince_.elapsed(nowMs, 10000);
  }
  void markInspected(uint32_t nowMs) { if (canConfirmInspection(nowMs)) inspected_ = true; }
  bool takeTransitionDwellMs(uint32_t &dwellMs) {
    if (!transitionDwellPending_) return false;
    dwellMs = lastTransitionDwellMs_;
    transitionDwellPending_ = false;
    return true;
  }

 private:
  void recordUpwardTransition(AlarmState previousState, uint32_t nowMs);

  float recent_[5] = {};
  uint8_t index_ = 0;
  uint8_t samples_ = 0;
  float filtered_ = 0;
  float baseline_ = 0;
  AlarmState state_ = AlarmState::WARMUP;
  DetectorTimer watchSince_;
  DetectorTimer warningSince_;
  DetectorTimer dangerSince_;
  DetectorTimer slowRiseSince_;
  DetectorTimer clearSince_;
  DetectorTimer recoverySince_;
  DetectorTimer invalidSince_;
  DetectorTimer validSince_;
  bool inspectionRequired_ = false;
  bool inspected_ = false;
  bool reseedFiltered_ = false;
  uint32_t lastTransitionDwellMs_ = 0;
  bool transitionDwellPending_ = false;
};
