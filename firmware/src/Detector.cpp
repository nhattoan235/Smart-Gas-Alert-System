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

AlarmState Detector::update(float signal, bool valid, uint32_t now) {
  if (!valid || !isfinite(signal) || signal<0 || signal>100) {
    validSince_=0;
    if (!invalidSince_) invalidSince_=now ? now : 1;
    if (now-invalidSince_>=3000) { state_=AlarmState::FAULT; inspectionRequired_=true; inspected_=false; }
    return state_;
  }
  invalidSince_=0;
  if (!validSince_) validSince_=now ? now : 1;
  recent_[index_++ % 5]=signal;
  if (samples_<5) samples_++;
  if (samples_<5) return state_;
  float smooth=median5(recent_);
  if (!recoverySince_) {
    filtered_=baseline_=smooth;
    recoverySince_=now ? now : 1;
  } else filtered_=0.35f*smooth+0.65f*filtered_;
  float delta=filtered_-baseline_;
  bool high=smooth>=65 || filtered_>=62;
  bool warning=filtered_>=28 || delta>=20;
  // A small but persistent rise is worth watching, never a calibrated leak claim.
  if (delta>=4 && filtered_>=7) { if (!slowRiseSince_) slowRiseSince_=now ? now : 1; }
  else slowRiseSince_=0;
  bool slowRise=slowRiseSince_ && now-slowRiseSince_>=30000;
  bool watch=filtered_>=12 || delta>=8 || slowRise;
  if (high) { if (!dangerSince_) dangerSince_=now ? now : 1; }
  else dangerSince_=0;
  if (warning) { if (!warningSince_) warningSince_=now ? now : 1; }
  else warningSince_=0;
  if (watch) { if (!watchSince_) watchSince_=now ? now : 1; }
  else watchSince_=0;
  if (dangerSince_ && now-dangerSince_>=1000) {
    state_=AlarmState::DANGER; inspectionRequired_=true; inspected_=false;
  } else if (state_!=AlarmState::DANGER && warningSince_ && now-warningSince_>=5000) {
    state_=AlarmState::WARNING; inspectionRequired_=true; inspected_=false;
  } else if ((state_==AlarmState::SAFE || state_==AlarmState::WARMUP) && watchSince_ && now-watchSince_>=6000) state_=AlarmState::WATCH;
  if (state_==AlarmState::FAULT && now-validSince_>=5000 && !watch && !warning && !high) state_=AlarmState::WATCH;
  if (state_==AlarmState::WARMUP && now-recoverySince_>=5000 && !watch && !warning && !high) state_=AlarmState::SAFE;
  if (state_==AlarmState::WATCH || state_==AlarmState::WARNING || state_==AlarmState::DANGER) {
    bool clear=!high && !warning && !watch && delta<5;
    if (clear) {
      if (!clearSince_) clearSince_=now ? now : 1;
      if (now-clearSince_>=10000) {
        if (state_==AlarmState::WARNING || state_==AlarmState::DANGER) {
          state_=AlarmState::WATCH;
          clearSince_=now ? now : 1;
        } else if (!inspectionRequired_ || inspected_) {
          state_=AlarmState::SAFE;
          inspectionRequired_=false;
          inspected_=false;
          baseline_=filtered_;
          warningSince_=watchSince_=dangerSince_=0;
        }
      }
    } else clearSince_=0;
  }
  if (state_==AlarmState::SAFE && !watch && !warning && !high && !slowRiseSince_)
    baseline_=0.995f*baseline_+0.005f*filtered_;
  return state_;
}
