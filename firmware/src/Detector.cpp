#include "Detector.h"
#include <math.h>

const char *stateName(AlarmState s) {
  switch (s) {
    case AlarmState::WARMUP: return "WARMUP";
    case AlarmState::SAFE: return "SAFE";
    case AlarmState::WATCH: return "WATCH";
    case AlarmState::WARNING: return "WARNING";
    case AlarmState::DANGER: return "DANGER";
    default: return "FAULT";
  }
}

static float median5(const float a[5]) {
  float b[5];
  for (int i=0;i<5;i++) b[i]=a[i];
  for (int i=1;i<5;i++) { float v=b[i]; int j=i-1; while(j>=0 && b[j]>v) { b[j+1]=b[j]; j--; } b[j+1]=v; }
  return b[2];
}

static int stateSeverity(AlarmState state) {
  switch (state) {
    case AlarmState::SAFE: return 1;
    case AlarmState::WATCH: return 2;
    case AlarmState::WARNING: return 3;
    case AlarmState::FAULT: return 4;
    case AlarmState::DANGER: return 5;
    default: return 0;
  }
}

void Detector::recordUpwardTransition(AlarmState previousState, uint32_t now) {
  if (state_ == previousState ||
      (state_ != AlarmState::FAULT && stateSeverity(state_) <= stateSeverity(previousState))) return;
  const DetectorTimer *conditionTimer = nullptr;
  switch (state_) {
    case AlarmState::WATCH: conditionTimer = &watchSince_; break;
    case AlarmState::WARNING: conditionTimer = &warningSince_; break;
    case AlarmState::DANGER: conditionTimer = &dangerSince_; break;
    case AlarmState::FAULT: conditionTimer = &invalidSince_; break;
    default: return;
  }
  if (!conditionTimer->active) return;
  lastTransitionDwellMs_ = (uint32_t)(now - conditionTimer->startedAt);
  transitionDwellPending_ = true;
}

AlarmState Detector::update(float signal, bool valid, uint32_t now) {
  const AlarmState previousState = state_;
  transitionDwellPending_ = false;
  if (!valid || !isfinite(signal) || signal<0 || signal>100) {
    validSince_.reset();
    if (state_==AlarmState::WARMUP) recoverySince_.reset();
    clearSince_.reset(); warningSince_.reset(); dangerSince_.reset();
    watchSince_.reset(); slowRiseSince_.reset();
    samples_=0; index_=0; reseedFiltered_=true;
    inspected_=false;
    invalidSince_.start(now);
    if (invalidSince_.elapsed(now,3000)) { state_=AlarmState::FAULT; inspectionRequired_=true; inspected_=false; }
    recordUpwardTransition(previousState,now);
    return state_;
  }
  invalidSince_.reset();
  validSince_.start(now);
  recent_[index_++ % 5]=signal;
  if (samples_<5) samples_++;
  if (samples_<5) return state_;
  float smooth=median5(recent_);
  if (!recoverySince_.active) {
    filtered_=baseline_=smooth;
    recoverySince_.start(now);
    reseedFiltered_=false;
  } else if (reseedFiltered_) {
    filtered_=smooth;
    reseedFiltered_=false;
  } else filtered_=0.35f*smooth+0.65f*filtered_;
  float delta=filtered_-baseline_;
  bool high=smooth>=65 || filtered_>=62;
  // The EMA approaches its steady value asymptotically. Allow only a tiny
  // float-rounding tolerance so an input exactly on the declared relative
  // warning boundary remains reachable after filtering.
  const float warningDeltaFloatTolerance = 0.00001f;
  bool warning=filtered_>=28 || delta + warningDeltaFloatTolerance >= 20;
  // A small but persistent rise is worth watching, never a calibrated leak claim.
  if (delta>=4 && filtered_>=7) slowRiseSince_.start(now);
  else slowRiseSince_.reset();
  bool slowRise=slowRiseSince_.elapsed(now,30000);
  bool watch=filtered_>=12 || delta>=8 || slowRise;
  if (high) dangerSince_.start(now);
  else dangerSince_.reset();
  if (warning) warningSince_.start(now);
  else warningSince_.reset();
  if (watch) watchSince_.start(now);
  else watchSince_.reset();
  if (dangerSince_.elapsed(now,1000)) {
    state_=AlarmState::DANGER; inspectionRequired_=true; inspected_=false;
  } else if (state_!=AlarmState::DANGER && warningSince_.elapsed(now,5000)) {
    state_=AlarmState::WARNING; inspectionRequired_=true; inspected_=false;
  } else if ((state_==AlarmState::SAFE || state_==AlarmState::WARMUP) && watchSince_.elapsed(now,6000)) state_=AlarmState::WATCH;
  // Once input has been valid for the recovery interval, leave FAULT through WATCH
  // unless a warning or danger condition is active. A WATCH-band signal is still
  // a monitored state; it must not keep the sensor fault latched or jump to SAFE.
  if (state_==AlarmState::FAULT && validSince_.elapsed(now,5000) && !warning && !high) state_=AlarmState::WATCH;
  if (state_==AlarmState::WARMUP && recoverySince_.elapsed(now,5000) && !watch && !warning && !high) state_=AlarmState::SAFE;
  if (state_==AlarmState::WATCH || state_==AlarmState::WARNING || state_==AlarmState::DANGER) {
    bool clear=!high && !warning && !watch && delta<5;
    if (clear) {
      clearSince_.start(now);
      if (clearSince_.elapsed(now,10000)) {
        if (state_==AlarmState::WARNING || state_==AlarmState::DANGER) {
          state_=AlarmState::WATCH;
          clearSince_.restart(now);
        } else if (!inspectionRequired_ || inspected_) {
          state_=AlarmState::SAFE;
          inspectionRequired_=false;
          inspected_=false;
          baseline_=filtered_;
          warningSince_.reset(); watchSince_.reset(); dangerSince_.reset();
        }
      }
    } else clearSince_.reset();
  }
  if (state_==AlarmState::SAFE && !watch && !warning && !high && !slowRiseSince_.active)
    baseline_=0.995f*baseline_+0.005f*filtered_;
  recordUpwardTransition(previousState,now);
  return state_;
}
