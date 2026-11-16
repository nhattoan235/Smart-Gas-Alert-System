#include "FlameMonitor.h"

FlameMonitor::FlameMonitor(const FlameTiming &timing)
    : timing_(timing), f1Alarm_(false), f1SensorFault_(false), gasInitialized_(false),
      priorGasOn_(false), f2SensorFault_(false), gasStateFault_(false),
      ignitionTimeout_(false), flameLost_(false), startupUnconfirmed_(false),
      f2State_(F2State::UNKNOWN) {}

void FlameMonitor::Timer::start(uint32_t nowMs) {
  if (!active) {
    startedAt = nowMs;
    active = true;
  }
}

void FlameMonitor::Timer::reset() {
  active = false;
  startedAt = 0;
}

bool FlameMonitor::Timer::elapsed(uint32_t nowMs, uint32_t durationMs) const {
  return active && static_cast<uint32_t>(nowMs - startedAt) >= durationMs;
}

bool FlameMonitor::setEvent(FlameChannel channel, FlameCause cause,
                            bool active, FlameSeverity severity,
                            FlameOutput &output) {
  bool *current = nullptr;
  switch (cause) {
    case FlameCause::F1_FIRE: current = &f1Alarm_; break;
    case FlameCause::F1_SENSOR_FAULT: current = &f1SensorFault_; break;
    case FlameCause::F2_IGNITION_TIMEOUT: current = &ignitionTimeout_; break;
    case FlameCause::F2_FLAME_LOST: current = &flameLost_; break;
    case FlameCause::F2_STARTUP_UNCONFIRMED: current = &startupUnconfirmed_; break;
    case FlameCause::F2_SENSOR_FAULT: current = &f2SensorFault_; break;
    case FlameCause::F2_GAS_STATE_FAULT: current = &gasStateFault_; break;
  }
  if (*current == active) return false;
  *current = active;
  if (output.eventCount < 6) {
    FlameEvent &event = output.events[output.eventCount++];
    event.channel = channel;
    event.cause = cause;
    event.active = active;
    event.severity = severity;
  }
  return true;
}

void FlameMonitor::closeF2Events(FlameOutput &output) {
  setEvent(FlameChannel::F2, FlameCause::F2_IGNITION_TIMEOUT, false,
           FlameSeverity::DANGER, output);
  setEvent(FlameChannel::F2, FlameCause::F2_FLAME_LOST, false,
           FlameSeverity::DANGER, output);
  setEvent(FlameChannel::F2, FlameCause::F2_STARTUP_UNCONFIRMED, false,
           FlameSeverity::DANGER, output);
  setEvent(FlameChannel::F2, FlameCause::F2_GAS_STATE_FAULT, false,
           FlameSeverity::FAULT, output);
}

FlameOutput FlameMonitor::update(const FlameInput &input) {
  FlameOutput output = {};
  if (input.f1Valid) {
    if (input.f1Detected) {
      f1Clear_.reset();
      f1Assert_.start(input.nowMs);
      if (f1Assert_.elapsed(input.nowMs, timing_.f1AssertMs))
        setEvent(FlameChannel::F1, FlameCause::F1_FIRE, true,
                 FlameSeverity::DANGER, output);
    } else {
      f1Assert_.reset();
      if (f1Alarm_) {
        f1Clear_.start(input.nowMs);
        if (f1Clear_.elapsed(input.nowMs, timing_.f1ClearMs)) {
          setEvent(FlameChannel::F1, FlameCause::F1_FIRE, false,
                   FlameSeverity::DANGER, output);
          f1Clear_.reset();
        }
      } else {
        f1Clear_.reset();
      }
    }
  } else {
    f1Assert_.reset();
    f1Clear_.reset();
    setEvent(FlameChannel::F1, FlameCause::F1_SENSOR_FAULT, true,
             FlameSeverity::FAULT, output);
  }
  if (input.f1Valid)
    setEvent(FlameChannel::F1, FlameCause::F1_SENSOR_FAULT, false,
             FlameSeverity::FAULT, output);

  if (input.f2Valid) {
    setEvent(FlameChannel::F2, FlameCause::F2_SENSOR_FAULT, false,
             FlameSeverity::FAULT, output);
  } else {
    setEvent(FlameChannel::F2, FlameCause::F2_SENSOR_FAULT, true,
             FlameSeverity::FAULT, output);
  }

  if (!input.burnerGasValid) {
    f2State_ = F2State::UNKNOWN;
    gasWait_.reset();
    flameConfirm_.reset();
    flameLoss_.reset();
    setEvent(FlameChannel::F2, FlameCause::F2_GAS_STATE_FAULT, true,
             FlameSeverity::FAULT, output);
  } else if (!input.burnerGasOn) {
    closeF2Events(output);
    gasInitialized_ = true;
    priorGasOn_ = false;
    f2State_ = F2State::OFF;
    gasWait_.reset();
    flameConfirm_.reset();
    flameLoss_.reset();
  } else {
    setEvent(FlameChannel::F2, FlameCause::F2_GAS_STATE_FAULT, false,
             FlameSeverity::FAULT, output);
    if (!gasInitialized_) {
      f2State_ = F2State::STARTUP_WAIT;
      gasWait_.start(input.nowMs);
    } else if (!priorGasOn_) {
      f2State_ = F2State::IGNITION_WAIT;
      gasWait_.start(input.nowMs);
    }
    gasInitialized_ = true;
    priorGasOn_ = true;

    if (f2State_ == F2State::UNKNOWN && input.f2Valid) {
      f2State_ = F2State::STARTUP_WAIT;
      gasWait_.start(input.nowMs);
    }

    if (!input.f2Valid) {
      f2State_ = F2State::UNKNOWN;
      flameConfirm_.reset();
      flameLoss_.reset();
    } else {
      if (f2State_ == F2State::STARTUP_WAIT ||
          f2State_ == F2State::IGNITION_WAIT) {
        if (input.f2Detected) {
          flameConfirm_.start(input.nowMs);
          if (flameConfirm_.elapsed(input.nowMs, timing_.flameRecoveryMs)) {
            setEvent(FlameChannel::F2, FlameCause::F2_STARTUP_UNCONFIRMED,
                     false, FlameSeverity::DANGER, output);
            setEvent(FlameChannel::F2, FlameCause::F2_IGNITION_TIMEOUT,
                     false, FlameSeverity::DANGER, output);
            f2State_ = F2State::LIT;
            gasWait_.reset();
            flameConfirm_.reset();
          }
        } else {
          flameConfirm_.reset();
        }
        const bool startup = f2State_ == F2State::STARTUP_WAIT;
        const uint32_t waitLimit = startup ? timing_.startupConfirmMs
                                           : timing_.ignitionTimeoutMs;
        if (gasWait_.elapsed(input.nowMs, waitLimit)) {
          setEvent(FlameChannel::F2,
                   startup ? FlameCause::F2_STARTUP_UNCONFIRMED
                           : FlameCause::F2_IGNITION_TIMEOUT,
                   true, FlameSeverity::DANGER, output);
          f2State_ = F2State::LOSS_CONFIRM;
          gasWait_.reset();
          flameLoss_.start(input.nowMs);
        }
      } else if (f2State_ == F2State::LIT) {
        if (input.f2Detected) {
          flameLoss_.reset();
        } else {
          f2State_ = F2State::LOSS_CONFIRM;
          flameLoss_.start(input.nowMs);
        }
      } else if (f2State_ == F2State::LOSS_CONFIRM) {
        if (input.f2Detected) {
          setEvent(FlameChannel::F2, FlameCause::F2_FLAME_LOST, false,
                   FlameSeverity::DANGER, output);
          setEvent(FlameChannel::F2, FlameCause::F2_IGNITION_TIMEOUT, false,
                   FlameSeverity::DANGER, output);
          setEvent(FlameChannel::F2, FlameCause::F2_STARTUP_UNCONFIRMED,
                   false, FlameSeverity::DANGER, output);
          f2State_ = F2State::LIT;
          flameLoss_.reset();
        } else if (flameLoss_.elapsed(input.nowMs,
                                      timing_.flameLossConfirmMs)) {
          const FlameCause cause = ignitionTimeout_
                                       ? FlameCause::F2_IGNITION_TIMEOUT
                                       : startupUnconfirmed_
                                             ? FlameCause::F2_STARTUP_UNCONFIRMED
                                             : FlameCause::F2_FLAME_LOST;
          setEvent(FlameChannel::F2, cause, true,
                   FlameSeverity::DANGER, output);
        }
      }
    }
  }

  output.f1Alarm = f1Alarm_;
  const bool missingFlameAlarm = ignitionTimeout_ || flameLost_ ||
                                 startupUnconfirmed_ ||
                                 (f2SensorFault_ && priorGasOn_);
  output.f2Alarm = (input.burnerGasValid && input.burnerGasOn &&
                    (!input.f2Valid || missingFlameAlarm)) ||
                   (!input.burnerGasValid && priorGasOn_ && missingFlameAlarm);
  output.faultActive = !input.f1Valid || !input.f2Valid ||
                       !input.burnerGasValid;
  output.f2State = f2State_;
  return output;
}
