// Lightweight standalone regression harness for Detector fault recovery.
// Run from firmware/ with a host C++ compiler:
//   g++ -std=c++11 -Iinclude tests/detector_recovery_test.cpp src/Detector.cpp -o detector_recovery_test
//   ./detector_recovery_test

#include <cassert>
#include <algorithm>
#include <cstdio>
#include <stdint.h>
#include <vector>

#include "../include/Detector.h"

static void enterFault(Detector &detector, uint32_t &recoveryStart) {
  for (uint32_t now = 0; now <= 6000; now += 100)
    detector.update(5.0f, true, now);
  assert(detector.state() == AlarmState::SAFE);

  for (uint32_t now = 6100; now <= 9100; now += 100)
    detector.update(5.0f, false, now);
  assert(detector.state() == AlarmState::FAULT);
  recoveryStart = 9200;
}

static AlarmState recoverAt(float signal) {
  Detector detector;
  uint32_t recoveryStart = 0;
  enterFault(detector, recoveryStart);

  for (uint32_t now = recoveryStart; now <= recoveryStart + 5000; now += 100)
    detector.update(signal, true, now);
  return detector.state();
}

static void testBelowWatchRecoveryEntersWatch() {
  assert(recoverAt(5.0f) == AlarmState::WATCH);
}

static void testWatchBandRecoveryEntersWatch() {
  assert(recoverAt(15.0f) == AlarmState::WATCH);
}

static void testWarningPrecedesFaultRecovery() {
  Detector detector;
  uint32_t recoveryStart = 0;
  enterFault(detector, recoveryStart);

  for (uint32_t now = recoveryStart; now <= recoveryStart + 9000; now += 100)
    detector.update(30.0f, true, now);
  assert(detector.state() == AlarmState::WARNING);
}

static void testDangerPrecedesFaultRecovery() {
  Detector detector;
  uint32_t recoveryStart = 0;
  enterFault(detector, recoveryStart);

  for (uint32_t now = recoveryStart; now <= recoveryStart + 2500; now += 100)
    detector.update(70.0f, true, now);
  assert(detector.state() == AlarmState::DANGER);
}

static void testInvalidSampleRestartsFaultRecoveryDwell() {
  Detector detector;
  uint32_t recoveryStart = 0;
  enterFault(detector, recoveryStart);
  for (uint32_t now = recoveryStart; now <= recoveryStart + 4800; now += 100)
    assert(detector.update(5.0f, true, now) == AlarmState::FAULT);
  assert(detector.update(5.0f, false, recoveryStart + 4900) == AlarmState::FAULT);
  for (uint32_t now = recoveryStart + 5000; now <= recoveryStart + 9800; now += 100)
    assert(detector.update(5.0f, true, now) == AlarmState::FAULT);
  assert(detector.update(5.0f, true, recoveryStart + 9900) == AlarmState::FAULT);
  assert(detector.update(5.0f, true, recoveryStart + 10000) == AlarmState::WATCH);
}

static void testInvalidWarmupRestartsRecoveryInterval() {
  Detector detector;
  for (uint32_t now = 0; now <= 1800; now += 100)
    detector.update(5.0f, true, now);
  for (uint32_t now = 1900; now <= 2100; now += 100)
    detector.update(5.0f, false, now);
  for (uint32_t now = 2200; now <= 5500; now += 100)
    detector.update(5.0f, true, now);
  assert(detector.state() == AlarmState::WARMUP);
  for (uint32_t now = 5600; now <= 8000; now += 100)
    detector.update(5.0f, true, now);
  assert(detector.state() == AlarmState::SAFE);
}

static void testInvalidResetsWarningDwell() {
  Detector warning;
  for (uint32_t now = 0; now <= 6000; now += 100)
    warning.update(5.0f, true, now);
  for (uint32_t now = 6100; now <= 10000; now += 100)
    warning.update(30.0f, true, now);
  for (uint32_t now = 10100; now <= 10300; now += 100)
    warning.update(30.0f, false, now);
  for (uint32_t now = 10400; now <= 15700; now += 100)
    warning.update(30.0f, true, now);
  assert(warning.state() != AlarmState::WARNING);
  assert(warning.update(30.0f, true, 15800) == AlarmState::WARNING);
}

static void testInvalidResetsDangerDwellAndMedianWindow() {
  Detector detector;
  for (uint32_t now = 0; now <= 6000; now += 100)
    detector.update(5.0f, true, now);
  for (uint32_t now = 6100; now <= 6500; now += 100)
    detector.update(70.0f, true, now);
  for (uint32_t now = 6600; now <= 8600; now += 100)
    detector.update(70.0f, false, now);
  assert(detector.update(70.0f, true, 8700) != AlarmState::DANGER);
}

static void testInvalidResetsInspectionClearTimer() {
  Detector detector;
  for (uint32_t now = 0; now <= 6000; now += 100)
    detector.update(5.0f, true, now);
  for (uint32_t now = 6100; now <= 16000; now += 100)
    detector.update(30.0f, true, now);
  for (uint32_t now = 16100; now <= 50000; now += 100)
    detector.update(5.0f, true, now);
  assert(detector.state() == AlarmState::WATCH);
  assert(detector.canConfirmInspection(50000));
  detector.update(5.0f, false, 50100);
  assert(!detector.canConfirmInspection(50100));
  for (uint32_t now = 50200; now <= 60500; now += 100)
    detector.update(5.0f, true, now);
  assert(!detector.canConfirmInspection(60500));
  detector.update(5.0f, true, 60600);
  assert(detector.canConfirmInspection(60600));
}

static void testIsolatedImpulseDoesNotRaiseAlarm() {
  Detector detector;
  for (uint32_t now = 0; now <= 6000; now += 100)
    detector.update(5.0f, true, now);
  detector.update(70.0f, true, 6100);
  for (uint32_t now = 6200; now <= 20000; now += 100)
    detector.update(5.0f, true, now);
  assert(detector.state() == AlarmState::SAFE);
}

static void testSlowSustainedRiseReachesWatch() {
  Detector detector;
  for (uint32_t now = 0; now <= 6000; now += 100)
    detector.update(5.0f, true, now);
  for (uint32_t now = 6100; now <= 52000; now += 100)
    detector.update(10.0f, true, now);
  assert(detector.state() == AlarmState::WATCH);
}

static void testTimersSurviveMillisWrap() {
  Detector detector;
  const uint32_t start = 0xFFFFF123UL;
  for (uint32_t elapsed = 0; elapsed <= 7000; elapsed += 100)
    detector.update(5.0f, true, start + elapsed);
  assert(detector.state() == AlarmState::SAFE);
}

static void testInvalidTimerStartsAtZero() {
  Detector detector;
  assert(detector.update(5.0f, false, 0) == AlarmState::WARMUP);
  assert(detector.update(5.0f, false, 0) == AlarmState::WARMUP);
  for (uint32_t now = 100; now <= 2900; now += 100)
    assert(detector.update(5.0f, false, now) == AlarmState::WARMUP);
  assert(detector.update(5.0f, false, 2999) == AlarmState::WARMUP);
  assert(detector.update(5.0f, false, 3000) == AlarmState::FAULT);
  uint32_t dwellMs = 0;
  assert(detector.takeTransitionDwellMs(dwellMs));
  assert(dwellMs == 3000);
}

static void testValidRecoveryTimerStartsAtZero() {
  Detector detector;
  const uint32_t start = 0U - 10000U;
  for (uint32_t elapsed = 0; elapsed <= 6000; elapsed += 100)
    detector.update(5.0f, true, start + elapsed);
  for (uint32_t elapsed = 6100; elapsed <= 9900; elapsed += 100)
    detector.update(5.0f, false, start + elapsed);
  assert(detector.state() == AlarmState::FAULT);
  for (uint32_t elapsed = 10000; elapsed <= 14900; elapsed += 100)
    assert(detector.update(5.0f, true, start + elapsed) == AlarmState::FAULT);
  assert(detector.update(5.0f, true, 4999) == AlarmState::FAULT);
  assert(detector.update(5.0f, true, 5000) == AlarmState::WATCH);
}

static void testWarningTimerStartsAtZero() {
  Detector detector;
  const uint32_t start = 0U - 12000U;
  for (uint32_t elapsed = 0; elapsed <= 11600; elapsed += 100)
    detector.update(5.0f, true, start + elapsed);
  assert(detector.state() == AlarmState::SAFE);
  for (uint32_t elapsed = 11700; elapsed <= 12000; elapsed += 100) {
    AlarmState state = detector.update(50.0f, true, start + elapsed);
    if (elapsed == 12000) assert(state != AlarmState::WARNING);
  }
  for (uint32_t elapsed = 12100; elapsed <= 16900; elapsed += 100)
    assert(detector.update(50.0f, true, start + elapsed) != AlarmState::WARNING);
  assert(detector.update(50.0f, true, 5000) == AlarmState::WARNING);
  uint32_t dwellMs = 0;
  assert(detector.takeTransitionDwellMs(dwellMs));
  assert(dwellMs == 5000);
}

static void testDangerTimerStartsAtZero() {
  Detector detector;
  const uint32_t start = 0U - 12000U;
  for (uint32_t elapsed = 0; elapsed <= 11700; elapsed += 100)
    detector.update(5.0f, true, start + elapsed);
  assert(detector.state() == AlarmState::SAFE);
  for (uint32_t elapsed = 11800; elapsed <= 12000; elapsed += 100) {
    AlarmState state = detector.update(100.0f, true, start + elapsed);
    if (elapsed == 12000) assert(state != AlarmState::DANGER);
  }
  assert(detector.update(100.0f, true, 999) != AlarmState::DANGER);
  assert(detector.update(100.0f, true, 1000) == AlarmState::DANGER);
  uint32_t dwellMs = 0;
  assert(detector.takeTransitionDwellMs(dwellMs));
  assert(dwellMs == 1000);
}

static void testClearTimerStartsAtZero() {
  Detector detector;
  const uint32_t start = 0U - 20000U;
  for (uint32_t elapsed = 0; elapsed <= 6000; elapsed += 100)
    detector.update(5.0f, true, start + elapsed);
  for (uint32_t elapsed = 6100; elapsed <= 9100; elapsed += 100)
    detector.update(5.0f, false, start + elapsed);
  assert(detector.state() == AlarmState::FAULT);
  for (uint32_t elapsed = 9200; elapsed <= 14900; elapsed += 100)
    detector.update(5.0f, false, start + elapsed);
  for (uint32_t elapsed = 15000; elapsed <= 20000; elapsed += 100)
    detector.update(5.0f, true, start + elapsed);
  assert(detector.state() == AlarmState::WATCH);
  assert(!detector.canConfirmInspection(0));
  detector.update(5.0f, true, 9999);
  assert(!detector.canConfirmInspection(9999));
  detector.update(5.0f, true, 10000);
  assert(detector.canConfirmInspection(10000));
}

static void testScalarInputCannotIdentifyPhysicalFailureCause() {
  // This demonstrates an interface limit only: negative masking and a stuck
  // normal sensor can present the same scalar value/valid bit as a quiet room.
  // Detector::update has no raw-channel metadata or independent stuck/drift
  // diagnostics, so it cannot distinguish those physical cases in this harness.
  Detector quietScenario, maskedScenario, stuckScenario;
  for (uint32_t now = 0; now <= 6000; now += 100) {
    quietScenario.update(5.0f, true, now);
    maskedScenario.update(5.0f, true, now);
    stuckScenario.update(5.0f, true, now);
  }
  assert(quietScenario.state() == maskedScenario.state());
  assert(quietScenario.state() == stuckScenario.state());
}

static void testAdversarialNegativeInterferenceCanMaskRelativeRise() {
  Detector unobstructed, masked;
  for (uint32_t now = 0; now <= 6000; now += 100) {
    unobstructed.update(5.0f, true, now);
    masked.update(5.0f, true, now);
  }
  assert(unobstructed.state() == AlarmState::SAFE);
  assert(masked.state() == AlarmState::SAFE);

  // Adversarial simulation values use the existing maximum negative noise and
  // environment-bias terms: 28 - 15 - 8 == the learned baseline of 5. The
  // scalar reaching Detector is therefore indistinguishable from a quiet room.
  // This test documents the identifiability limit; it does not tune thresholds
  // or claim that software can recover information canceled before sampling.
  for (uint32_t now = 6100; now <= 15100; now += 100) {
    const float leakWithoutMask = 28.0f;
    const float simulatedInterference = -15.0f;
    const float simulatedEnvironmentBias = -8.0f;
    const float leakAfterMask = leakWithoutMask + simulatedInterference +
                                simulatedEnvironmentBias;
    unobstructed.update(leakWithoutMask, true, now);
    masked.update(leakAfterMask, true, now);
  }
  assert(unobstructed.state() == AlarmState::WARNING);
  assert(masked.state() == AlarmState::SAFE);
}

struct InterferenceEpisodeResult {
  bool watchPlusDetected;
  bool alarmActiveDetected;
  bool fixedThresholdDetected;
  uint32_t watchPlusLatencyMs;
  uint32_t alarmActiveLatencyMs;
  uint32_t fixedThresholdLatencyMs;
  float finalRise;
  AlarmState finalState;
};

static bool isAlarmPositive(AlarmState state) {
  return state == AlarmState::WATCH || state == AlarmState::WARNING ||
         state == AlarmState::DANGER;
}

static double medianLatencyMs(const std::vector<uint32_t> &values) {
  assert(!values.empty());
  const size_t middle = values.size() / 2;
  if (values.size() % 2 != 0) return values[middle];
  return (static_cast<double>(values[middle - 1]) + values[middle]) / 2.0;
}

static InterferenceEpisodeResult runSyntheticInterferenceEpisode(
    float hazardSignal, float negativeInterference, float environmentBias) {
  Detector detector;
  for (uint32_t now = 0; now <= 6000; now += 100)
    detector.update(5.0f, true, now);
  assert(detector.state() == AlarmState::SAFE);

  const uint32_t hazardStartMs = 6100;
  const float observed = hazardSignal + negativeInterference + environmentBias;
  InterferenceEpisodeResult result = {false, false, false, 0, 0, 0, 0.0f, AlarmState::WARMUP};
  bool fixedThresholdTimer = false;
  uint32_t fixedThresholdSinceMs = 0;
  for (uint32_t now = hazardStartMs; now <= hazardStartMs + 30000; now += 100) {
    const AlarmState state = detector.update(observed, true, now);
    if (detector.filtered() >= 28.0f) {
      if (!fixedThresholdTimer) {
        fixedThresholdTimer = true;
        fixedThresholdSinceMs = now;
      }
      if (!result.fixedThresholdDetected &&
          static_cast<uint32_t>(now - fixedThresholdSinceMs) >= 5000) {
        result.fixedThresholdDetected = true;
        result.fixedThresholdLatencyMs = now - hazardStartMs;
      }
    } else {
      fixedThresholdTimer = false;
    }
    if (!result.watchPlusDetected && isAlarmPositive(state)) {
      result.watchPlusDetected = true;
      result.watchPlusLatencyMs = now - hazardStartMs;
    }
    if (!result.alarmActiveDetected &&
        (state == AlarmState::WARNING || state == AlarmState::DANGER)) {
      result.alarmActiveDetected = true;
      result.alarmActiveLatencyMs = now - hazardStartMs;
    }
    result.finalRise = detector.rise();
    result.finalState = state;
  }
  return result;
}

static void testSyntheticNegativeInterferenceFNRAndLatencyAgainstFixedThreshold() {
  // Synthetic detector-input cases only. The comparator uses the same
  // Detector::filtered() output and requires >=28 for 5 seconds.
  // WATCH+ means WATCH/WARNING/DANGER; alarm-active means WARNING/DANGER.
  // The three masked cases use interference/bias magnitudes available in the
  // Wokwi signal model; they are not calibrated MQ sensor cross-sensitivity.
  struct Case { const char *name; float hazard; float interference; float bias; };
  const Case cases[] = {
      {"unmasked", 35.0f, 0.0f, 0.0f},
      {"partial_negative", 36.0f, -10.0f, 0.0f},
      {"warning_delta_boundary", 35.0f, -10.0f, 0.0f},
      {"strong_negative", 28.0f, -15.0f, 0.0f},
      {"fully_cancelled", 28.0f, -15.0f, -8.0f},
  };

  uint32_t totalWatchFn = 0, totalAlarmFn = 0, totalFixedFn = 0;
  uint32_t totalMaskedWatchFn = 0, totalMaskedAlarmFn = 0, totalMaskedFixedFn = 0;
  std::vector<uint32_t> totalWatchDelays, totalAlarmDelays, totalFixedDelays;
  std::vector<uint32_t> totalMaskedWatchDelays, totalMaskedAlarmDelays;
  for (const char *gas : {"LPG", "CO"}) {
    uint32_t watchFn = 0, alarmFn = 0, fixedFn = 0;
    std::vector<uint32_t> watchDelays, alarmDelays, fixedDelays;
    uint32_t maskedWatchFn = 0, maskedAlarmFn = 0, maskedFixedFn = 0;
    std::vector<uint32_t> maskedWatchDelays, maskedAlarmDelays, maskedFixedDelays;
    for (size_t i = 0; i < sizeof(cases) / sizeof(cases[0]); ++i) {
      const InterferenceEpisodeResult result = runSyntheticInterferenceEpisode(
          cases[i].hazard, cases[i].interference, cases[i].bias);
      if (!result.watchPlusDetected) watchFn++;
      else watchDelays.push_back(result.watchPlusLatencyMs);
      if (!result.alarmActiveDetected) alarmFn++;
      else alarmDelays.push_back(result.alarmActiveLatencyMs);
      if (!result.fixedThresholdDetected) fixedFn++;
      else fixedDelays.push_back(result.fixedThresholdLatencyMs);
      if (i != 0) {
        if (!result.watchPlusDetected) maskedWatchFn++;
        else maskedWatchDelays.push_back(result.watchPlusLatencyMs);
        if (!result.alarmActiveDetected) maskedAlarmFn++;
        else maskedAlarmDelays.push_back(result.alarmActiveLatencyMs);
        if (!result.fixedThresholdDetected) maskedFixedFn++;
        else maskedFixedDelays.push_back(result.fixedThresholdLatencyMs);
      }
      std::printf("negative_interference_episode gas=%s case=%s observed_pct=%.1f final_state=%s final_rise_pct=%.7f watch_plus=%s watch_plus_latency_ms=%u alarm_active=%s alarm_active_latency_ms=%u fixed_28_dwell5s=%s fixed_latency_ms=%u source=synthetic fully_cancelled_limit=%s\n",
          gas, cases[i].name,
          cases[i].hazard + cases[i].interference + cases[i].bias,
          stateName(result.finalState), result.finalRise,
          result.watchPlusDetected ? "DETECTED" : "MISSED",
          static_cast<unsigned>(result.watchPlusLatencyMs),
          result.alarmActiveDetected ? "DETECTED" : "MISSED",
          static_cast<unsigned>(result.alarmActiveLatencyMs),
          result.fixedThresholdDetected ? "DETECTED" : "MISSED",
          static_cast<unsigned>(result.fixedThresholdLatencyMs),
          i == 4 ? "UNIDENTIFIABLE_EXPECTED_FALSE_NEGATIVE" : "not_applicable");
      std::printf("negative_interference_label gas=%s case=%s truth_hazard=true onset_ms=6100 evaluation_window_ms=30000\n",
          gas, cases[i].name);
      if (i == 2) {
        // Regression requirement: the declared delta>=20 warning boundary
        // must remain reachable after Median/EMA filtering.
        assert(result.alarmActiveDetected);
      }
    }
    std::fflush(stdout);
    assert(watchFn == 1 && alarmFn == 2 && fixedFn == 4);
    assert(maskedWatchFn == 1 && maskedAlarmFn == 2 && maskedFixedFn == 4);
    assert(watchFn < fixedFn && alarmFn < fixedFn);
    std::sort(watchDelays.begin(), watchDelays.end());
    std::sort(alarmDelays.begin(), alarmDelays.end());
    std::sort(maskedWatchDelays.begin(), maskedWatchDelays.end());
    std::sort(maskedAlarmDelays.begin(), maskedAlarmDelays.end());
    const double watchP50 = medianLatencyMs(watchDelays);
    const double alarmP50 = medianLatencyMs(alarmDelays);
    const double maskedWatchP50 = medianLatencyMs(maskedWatchDelays);
    const double maskedAlarmP50 = medianLatencyMs(maskedAlarmDelays);
    const double fixedP50 = medianLatencyMs(fixedDelays);
    std::printf("negative_interference_metrics gas=%s episodes=5 watch_plus_fn=%u watch_plus_fnr=%.6f alarm_active_fn=%u alarm_active_fnr=%.6f fixed_filtered_28_dwell5s_fn=%u fixed_filtered_28_dwell5s_fnr=%.6f watch_plus_p50_latency_ms=%.1f alarm_active_p50_latency_ms=%.1f fixed_filtered_28_dwell5s_latency_samples=%u fixed_filtered_28_dwell5s_p50_latency_ms=%.1f masked_episodes=4 masked_watch_plus_fn=%u masked_watch_plus_fnr=%.6f masked_alarm_active_fn=%u masked_alarm_active_fnr=%.6f masked_fixed_filtered_28_dwell5s_fn=%u masked_fixed_filtered_28_dwell5s_fnr=%.6f masked_watch_plus_p50_latency_ms=%.1f masked_alarm_active_p50_latency_ms=%.1f masked_fixed_latency_samples=%u scope=synthetic_detector_input\n",
        gas, static_cast<unsigned>(watchFn), watchFn / 5.0,
        static_cast<unsigned>(alarmFn), alarmFn / 5.0,
        static_cast<unsigned>(fixedFn), fixedFn / 5.0,
        watchP50, alarmP50, static_cast<unsigned>(fixedDelays.size()), fixedP50,
        static_cast<unsigned>(maskedWatchFn), maskedWatchFn / 4.0,
        static_cast<unsigned>(maskedAlarmFn), maskedAlarmFn / 4.0,
        static_cast<unsigned>(maskedFixedFn), maskedFixedFn / 4.0,
        maskedWatchP50, maskedAlarmP50,
        static_cast<unsigned>(maskedFixedDelays.size()));

    const InterferenceEpisodeResult belowBoundary =
        runSyntheticInterferenceEpisode(24.99f, 0.0f, 0.0f);
    assert(belowBoundary.watchPlusDetected);
    assert(!belowBoundary.alarmActiveDetected);
    std::printf("negative_interference_boundary_guard gas=%s observed_pct=24.99 watch_plus=%s alarm_active=%s source=synthetic assertion=below_delta_20_does_not_raise_alarm\n",
        gas, belowBoundary.watchPlusDetected ? "DETECTED" : "MISSED",
        belowBoundary.alarmActiveDetected ? "DETECTED" : "MISSED");

    totalWatchFn += watchFn;
    totalAlarmFn += alarmFn;
    totalFixedFn += fixedFn;
    totalMaskedWatchFn += maskedWatchFn;
    totalMaskedAlarmFn += maskedAlarmFn;
    totalMaskedFixedFn += maskedFixedFn;
    totalWatchDelays.insert(totalWatchDelays.end(), watchDelays.begin(), watchDelays.end());
    totalAlarmDelays.insert(totalAlarmDelays.end(), alarmDelays.begin(), alarmDelays.end());
    totalFixedDelays.insert(totalFixedDelays.end(), fixedDelays.begin(), fixedDelays.end());
    totalMaskedWatchDelays.insert(totalMaskedWatchDelays.end(), maskedWatchDelays.begin(), maskedWatchDelays.end());
    totalMaskedAlarmDelays.insert(totalMaskedAlarmDelays.end(), maskedAlarmDelays.begin(), maskedAlarmDelays.end());
  }
  assert(totalWatchFn == 2 && totalAlarmFn == 4 && totalFixedFn == 8);
  assert(totalMaskedWatchFn == 2 && totalMaskedAlarmFn == 4 && totalMaskedFixedFn == 8);
  std::sort(totalWatchDelays.begin(), totalWatchDelays.end());
  std::sort(totalAlarmDelays.begin(), totalAlarmDelays.end());
  std::sort(totalFixedDelays.begin(), totalFixedDelays.end());
  std::sort(totalMaskedWatchDelays.begin(), totalMaskedWatchDelays.end());
  std::sort(totalMaskedAlarmDelays.begin(), totalMaskedAlarmDelays.end());
  std::printf("negative_interference_metrics gas=ALL episodes=10 watch_plus_fn=%u watch_plus_fnr=%.6f alarm_active_fn=%u alarm_active_fnr=%.6f fixed_filtered_28_dwell5s_fn=%u fixed_filtered_28_dwell5s_fnr=%.6f watch_plus_p50_latency_ms=%.1f alarm_active_p50_latency_ms=%.1f fixed_filtered_28_dwell5s_p50_latency_ms=%.1f masked_episodes=8 masked_watch_plus_fn=%u masked_watch_plus_fnr=%.6f masked_alarm_active_fn=%u masked_alarm_active_fnr=%.6f masked_fixed_filtered_28_dwell5s_fn=%u masked_fixed_filtered_28_dwell5s_fnr=%.6f masked_watch_plus_p50_latency_ms=%.1f masked_alarm_active_p50_latency_ms=%.1f scope=synthetic_detector_input\n",
      static_cast<unsigned>(totalWatchFn), totalWatchFn / 10.0,
      static_cast<unsigned>(totalAlarmFn), totalAlarmFn / 10.0,
      static_cast<unsigned>(totalFixedFn), totalFixedFn / 10.0,
      medianLatencyMs(totalWatchDelays), medianLatencyMs(totalAlarmDelays), medianLatencyMs(totalFixedDelays),
      static_cast<unsigned>(totalMaskedWatchFn), totalMaskedWatchFn / 8.0,
      static_cast<unsigned>(totalMaskedAlarmFn), totalMaskedAlarmFn / 8.0,
      static_cast<unsigned>(totalMaskedFixedFn), totalMaskedFixedFn / 8.0,
      medianLatencyMs(totalMaskedWatchDelays), medianLatencyMs(totalMaskedAlarmDelays));
}

int main() {
  testBelowWatchRecoveryEntersWatch();
  testWatchBandRecoveryEntersWatch();
  testWarningPrecedesFaultRecovery();
  testDangerPrecedesFaultRecovery();
  testInvalidSampleRestartsFaultRecoveryDwell();
  testInvalidWarmupRestartsRecoveryInterval();
  testInvalidResetsWarningDwell();
  testInvalidResetsDangerDwellAndMedianWindow();
  testInvalidResetsInspectionClearTimer();
  testIsolatedImpulseDoesNotRaiseAlarm();
  testSlowSustainedRiseReachesWatch();
  testTimersSurviveMillisWrap();
  testInvalidTimerStartsAtZero();
  testValidRecoveryTimerStartsAtZero();
  testWarningTimerStartsAtZero();
  testDangerTimerStartsAtZero();
  testClearTimerStartsAtZero();
  testScalarInputCannotIdentifyPhysicalFailureCause();
  testAdversarialNegativeInterferenceCanMaskRelativeRise();
  testSyntheticNegativeInterferenceFNRAndLatencyAgainstFixedThreshold();
  std::puts("Detector recovery regression cases passed.");
  return 0;
}
