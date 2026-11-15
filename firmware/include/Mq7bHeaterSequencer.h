#pragma once

#include <stdint.h>

// Abstract policy only. This code does not select a GPIO or energize a heater.
// Physical high/low voltages and phase durations must be supplied from the
// verified MQ-7B module and interlock design before a hardware adapter exists.
struct Mq7bHeaterPolicy {
  uint32_t highPhaseMs = 0;
  uint32_t lowPhaseMs = 0;
  uint32_t measurementOffsetIntoLowMs = 0;
  uint32_t measurementWindowMs = 0;
};

enum class Mq7bHeaterPhase : uint8_t { DISABLED, HIGH_PHASE, LOW_PHASE };

struct Mq7bHeaterState {
  Mq7bHeaterPhase phase;
  bool measurementWindow;
  bool configured;
};

class Mq7bHeaterSequencer {
 public:
  explicit Mq7bHeaterSequencer(const Mq7bHeaterPolicy &policy)
      : policy_(policy), cycleMs_(0), cycleStartedAtMs_(0), active_(false) {
    const uint64_t cycle = static_cast<uint64_t>(policy_.highPhaseMs) +
                           static_cast<uint64_t>(policy_.lowPhaseMs);
    configured_ = policy_.highPhaseMs != 0 && policy_.lowPhaseMs != 0 &&
        policy_.measurementWindowMs != 0 && cycle <= UINT32_MAX &&
        policy_.measurementOffsetIntoLowMs < policy_.lowPhaseMs &&
        policy_.measurementWindowMs <= policy_.lowPhaseMs -
                                            policy_.measurementOffsetIntoLowMs;
    if (configured_) cycleMs_ = static_cast<uint32_t>(cycle);
  }

  bool configured() const { return configured_; }

  void start(uint32_t nowMs) {
    active_ = configured_;
    cycleStartedAtMs_ = nowMs;
  }

  void stop() { active_ = false; }

  Mq7bHeaterState state(uint32_t nowMs) const {
    if (!configured_ || !active_)
      return {Mq7bHeaterPhase::DISABLED, false, configured_};

    const uint32_t cyclePosition =
        static_cast<uint32_t>(nowMs - cycleStartedAtMs_) % cycleMs_;
    if (cyclePosition < policy_.highPhaseMs)
      return {Mq7bHeaterPhase::HIGH_PHASE, false, true};

    const uint32_t lowPosition = cyclePosition - policy_.highPhaseMs;
    const bool measurementWindow =
        lowPosition >= policy_.measurementOffsetIntoLowMs &&
        lowPosition - policy_.measurementOffsetIntoLowMs <
            policy_.measurementWindowMs;
    return {Mq7bHeaterPhase::LOW_PHASE, measurementWindow, true};
  }

 private:
  Mq7bHeaterPolicy policy_;
  uint32_t cycleMs_;
  uint32_t cycleStartedAtMs_;
  bool configured_;
  bool active_;
};
