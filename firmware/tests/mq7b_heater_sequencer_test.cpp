// Synthetic policy timing only. These cases validate software sequencing,
// not heater voltage, module interlock, or MQ-7B electrical operation.
// Run from firmware/ with a host C++ compiler:
//   g++ -std=c++11 -Iinclude tests/mq7b_heater_sequencer_test.cpp -o mq7b_heater_sequencer_test
//   ./mq7b_heater_sequencer_test

#include <assert.h>
#include <stdio.h>

#include "Mq7bHeaterSequencer.h"

static void testUnconfiguredPolicyDisablesOutputAndSampling() {
  Mq7bHeaterSequencer sequencer(Mq7bHeaterPolicy{});
  assert(!sequencer.configured());
  sequencer.start(0);
  Mq7bHeaterState state = sequencer.state(0);
  assert(state.phase == Mq7bHeaterPhase::DISABLED);
  assert(!state.measurementWindow && !state.configured);
}

static void testConfiguredSyntheticCycleAndMeasurementWindow() {
  Mq7bHeaterPolicy policy{};
  policy.highPhaseMs = 1000;
  policy.lowPhaseMs = 3000;
  policy.measurementOffsetIntoLowMs = 500;
  policy.measurementWindowMs = 500;
  Mq7bHeaterSequencer sequencer(policy);
  assert(sequencer.configured());
  sequencer.start(100);

  assert(sequencer.state(1099).phase == Mq7bHeaterPhase::HIGH_PHASE);
  assert(sequencer.state(1100).phase == Mq7bHeaterPhase::LOW_PHASE);
  assert(!sequencer.state(1599).measurementWindow);
  assert(sequencer.state(1600).measurementWindow);
  assert(sequencer.state(2099).measurementWindow);
  assert(!sequencer.state(2100).measurementWindow);
  assert(sequencer.state(4100).phase == Mq7bHeaterPhase::HIGH_PHASE);

  sequencer.stop();
  assert(sequencer.state(4101).phase == Mq7bHeaterPhase::DISABLED);
}

static void testInvalidMeasurementWindowBlocksConfiguration() {
  Mq7bHeaterPolicy policy{};
  policy.highPhaseMs = 100;
  policy.lowPhaseMs = 200;
  policy.measurementOffsetIntoLowMs = 150;
  policy.measurementWindowMs = 100;
  Mq7bHeaterSequencer sequencer(policy);
  assert(!sequencer.configured());
  sequencer.start(0);
  assert(sequencer.state(50).phase == Mq7bHeaterPhase::DISABLED);
}

static void testCycleSurvivesMillisWrap() {
  Mq7bHeaterPolicy policy{};
  policy.highPhaseMs = 1000;
  policy.lowPhaseMs = 3000;
  policy.measurementOffsetIntoLowMs = 500;
  policy.measurementWindowMs = 500;
  Mq7bHeaterSequencer sequencer(policy);
  sequencer.start(0xFFFFFF00UL);
  assert(sequencer.state(0x000002E7UL).phase == Mq7bHeaterPhase::HIGH_PHASE);
  assert(sequencer.state(0x000002E8UL).phase == Mq7bHeaterPhase::LOW_PHASE);
}

int main() {
  testUnconfiguredPolicyDisablesOutputAndSampling();
  testConfiguredSyntheticCycleAndMeasurementWindow();
  testInvalidMeasurementWindowBlocksConfiguration();
  testCycleSurvivesMillisWrap();
  puts("MQ-7B synthetic sequencer policy cases passed.");
  return 0;
}
