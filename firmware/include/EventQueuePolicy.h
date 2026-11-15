#pragma once

#include <stddef.h>
#include <string.h>

namespace event_queue_policy {

inline int priority(const char *data) {
  if (!data) return 1;
  if (strstr(data, "\"state\":\"DANGER\"") != nullptr) return 5;
  if (strstr(data, "\"state\":\"FAULT\"") != nullptr) return 4;
  if (strstr(data, "\"state\":\"WARNING\"") != nullptr) return 3;
  if (strstr(data, "\"state\":\"WATCH\"") != nullptr) return 2;
  return 1;
}

// Strictly lower-priority entries may be replaced. Equal-priority DANGER
// records are kept; if none is replaceable, the caller records overflow.
inline int lowestStrictlyLowerVictim(const int *stored, size_t count, int incoming) {
  int victim = -1;
  int lowest = incoming;
  for (size_t i = 0; i < count; ++i) {
    if (stored[i] < lowest) {
      lowest = stored[i];
      victim = static_cast<int>(i);
    }
  }
  return victim;
}

}  // namespace event_queue_policy
