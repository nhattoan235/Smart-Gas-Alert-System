// Host regression harness for the bounded NVS queue replacement policy.
// Run from firmware/ with a host C++ compiler:
//   g++ -std=c++11 -Iinclude tests/event_queue_policy_test.cpp -o event_queue_policy_test
//   ./event_queue_policy_test

#include <cassert>
#include <cstdio>
#include <stdint.h>

#include "../include/EventQueuePolicy.h"

int main() {
  int fullDangerQueue[16];
  for (int &priority : fullDangerQueue) priority = 5;
  // At capacity, a 17th DANGER is deliberately not retained: there is no
  // strictly lower-priority victim, so main.cpp increments and persists lostEvents.
  assert(event_queue_policy::lowestStrictlyLowerVictim(fullDangerQueue, 16, 5) == -1);
  uint32_t lostEvents = 0;
  if (event_queue_policy::lowestStrictlyLowerVictim(fullDangerQueue, 16, 5) < 0)
    ++lostEvents;  // Mirrors queueEvent's full-queue caller branch.
  assert(lostEvents == 1);

  int mixedQueue[16];
  for (int &priority : mixedQueue) priority = 5;
  mixedQueue[7] = 2;
  assert(event_queue_policy::lowestStrictlyLowerVictim(mixedQueue, 16, 5) == 7);
  assert(event_queue_policy::priority("{\"state\":\"DANGER\"}") == 5);
  assert(event_queue_policy::priority(
             "{\"state\":\"SAFE\",\"flame_severity\":\"DANGER\",\"flame_active\":true}") == 5);
  assert(event_queue_policy::priority(
             "{\"state\":\"SAFE\",\"flame_severity\":\"FAULT\",\"flame_active\":true}") == 4);
  assert(event_queue_policy::priority(
             "{\"state\":\"SAFE\",\"flame_severity\":\"DANGER\",\"flame_active\":false}") == 1);
  std::puts("Event queue priority regression cases passed.");
  return 0;
}
