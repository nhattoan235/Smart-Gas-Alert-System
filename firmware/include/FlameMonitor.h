#pragma once

#include <stdint.h>

enum class FlameChannel : uint8_t { F1, F2 };
enum class FlameCause : uint8_t {
  F1_FIRE,
  F1_SENSOR_FAULT,
  F2_IGNITION_TIMEOUT,
  F2_FLAME_LOST,
  F2_STARTUP_UNCONFIRMED,
  F2_SENSOR_FAULT,
  F2_GAS_STATE_FAULT
};
enum class FlameSeverity : uint8_t { FAULT, DANGER };
enum class F2State : uint8_t {
  OFF, IGNITION_WAIT, STARTUP_WAIT, LIT, LOSS_CONFIRM, UNKNOWN
};

struct FlameInput {
  uint32_t nowMs;
  bool f1Valid;
  bool f1Detected;
  bool f2Valid;
  bool f2Detected;
  bool burnerGasValid;
  bool burnerGasOn;
};

struct FlameEvent {
  FlameChannel channel;
  FlameCause cause;
  bool active;  // true opens an event, false closes it
  FlameSeverity severity;
};

struct FlameOutput {
  bool f1Alarm;
  bool f2Alarm;
  bool faultActive;
  F2State f2State;
  uint8_t eventCount;
  FlameEvent events[6];
};

struct FlameTiming {
  uint32_t f1AssertMs;
  uint32_t f1ClearMs;
  uint32_t ignitionTimeoutMs;
  uint32_t flameLossConfirmMs;
  uint32_t flameRecoveryMs;
  uint32_t startupConfirmMs;

  FlameTiming(uint32_t assertMs = 500, uint32_t clearMs = 1000,
              uint32_t ignitionMs = 5000, uint32_t lossMs = 1000,
              uint32_t recoveryMs = 300, uint32_t startupMs = 2000)
      : f1AssertMs(assertMs), f1ClearMs(clearMs),
        ignitionTimeoutMs(ignitionMs), flameLossConfirmMs(lossMs),
        flameRecoveryMs(recoveryMs), startupConfirmMs(startupMs) {}
};

class FlameMonitor {
 public:
  explicit FlameMonitor(const FlameTiming &timing = FlameTiming());
  FlameOutput update(const FlameInput &input);

 private:
  struct Timer {
    uint32_t startedAt;
    bool active;
    Timer() : startedAt(0), active(false) {}
    void start(uint32_t nowMs);
    void reset();
    bool elapsed(uint32_t nowMs, uint32_t durationMs) const;
  };

  bool setEvent(FlameChannel channel, FlameCause cause, bool active,
                FlameSeverity severity, FlameOutput &output);
  void closeF2Events(FlameOutput &output);

  FlameTiming timing_;
  bool f1Alarm_;
  bool f1SensorFault_;
  bool gasInitialized_;
  bool priorGasOn_;
  bool f2SensorFault_;
  bool gasStateFault_;
  bool ignitionTimeout_;
  bool flameLost_;
  bool startupUnconfirmed_;
  F2State f2State_;
  Timer f1Assert_;
  Timer f1Clear_;
  Timer gasWait_;
  Timer flameConfirm_;
  Timer flameLoss_;
};
