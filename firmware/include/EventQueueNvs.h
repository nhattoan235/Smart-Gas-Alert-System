#pragma once

#include <stdint.h>
#include <stddef.h>

namespace event_queue_nvs {

static const uint8_t FORMAT_VERSION = 1;
static const char *const POINTERS_KEY = "qstate";

inline bool supportedCapacity(uint8_t capacity) {
  return capacity == 8 || capacity == 16 || capacity == 32;
}

// Stored as one NVS blob so head/count cannot be committed separately.
struct Pointers {
  uint8_t version;
  uint8_t capacity;
  uint8_t head;
  uint8_t count;
};

static_assert(sizeof(Pointers) == 4, "queue pointer record must remain fixed-size");

enum class ReplaceResult : uint8_t {
  REPLACED,
  FAILED_OLD_INTACT,
  FAILED_RESTORED,
  FAILED_CORRUPTED
};

enum class DiscardHeadResult : uint8_t {
  NO_PENDING_EVENT,
  REMOVE_FAILED_RETAINED,
  REMOVED_POINTERS_SAVED,
  REMOVED_POINTERS_SAVE_FAILED
};

inline bool valid(const Pointers &pointers, uint8_t expectedCapacity) {
  return pointers.version == FORMAT_VERSION &&
         pointers.capacity == expectedCapacity &&
         supportedCapacity(pointers.capacity) &&
         pointers.head < pointers.capacity &&
         pointers.count <= pointers.capacity;
}

inline bool validUpTo(const Pointers &pointers, uint8_t maxCapacity) {
  return pointers.version == FORMAT_VERSION &&
         supportedCapacity(pointers.capacity) &&
         pointers.capacity <= maxCapacity &&
         pointers.head < pointers.capacity &&
         pointers.count <= pointers.capacity;
}

inline bool samePointers(const Pointers &left, const Pointers &right) {
  return left.version == right.version && left.capacity == right.capacity &&
         left.head == right.head && left.count == right.count;
}

template <typename PreferencesLike>
bool savePointers(PreferencesLike &preferences, const Pointers &pointers) {
  if (!valid(pointers, pointers.capacity)) return false;
  return preferences.putBytes(POINTERS_KEY, &pointers, sizeof(pointers)) ==
         sizeof(pointers);
}

template <typename PreferencesLike>
bool loadPointers(PreferencesLike &preferences, uint8_t expectedCapacity,
                  Pointers &pointers) {
  if (!preferences.isKey(POINTERS_KEY)) return false;
  Pointers candidate{};
  if (preferences.getBytes(POINTERS_KEY, &candidate, sizeof(candidate)) !=
      sizeof(candidate)) return false;
  if (!valid(candidate, expectedCapacity)) return false;
  pointers = candidate;
  return true;
}

// Load a previously committed ring regardless of whether an older firmware
// used capacity 8, 16, or 32. The stored capacity defines the ring modulus.
template <typename PreferencesLike>
bool loadPointersUpTo(PreferencesLike &preferences, uint8_t maxCapacity,
                      Pointers &pointers) {
  if (!preferences.isKey(POINTERS_KEY)) return false;
  Pointers candidate{};
  if (preferences.getBytes(POINTERS_KEY, &candidate, sizeof(candidate)) !=
      sizeof(candidate)) return false;
  if (!validUpTo(candidate, maxCapacity)) return false;
  pointers = candidate;
  return true;
}

// Import split legacy metadata by committing one pointer blob without moving
// or deleting any event keys. If the commit fails, callers can keep the legacy
// metadata and retry on the next boot; the original ring remains readable.
template <typename PreferencesLike>
bool importLegacyPointers(PreferencesLike &preferences,
                          uint8_t legacyCapacity, uint8_t legacyHead,
                          uint8_t legacyCount, uint8_t maxCapacity,
                          Pointers &pointers) {
  if (!supportedCapacity(legacyCapacity) || legacyCapacity > maxCapacity ||
      legacyHead >= legacyCapacity || legacyCount > legacyCapacity) {
    return false;
  }
  Pointers candidate = {FORMAT_VERSION, legacyCapacity, legacyHead,
                       legacyCount};
  if (!savePointers(preferences, candidate)) return false;
  pointers = candidate;
  return true;
}

// Drop one unreadable head record without allowing it to block later events.
// Removing the key precedes the single-blob pointer commit. If power fails
// between those operations, reconcile() advances past the missing head after
// reboot. The caller receives the advanced in-memory state even when its
// pointer commit reports failure.
template <typename PreferencesLike>
DiscardHeadResult discardHead(PreferencesLike &preferences, Pointers &pointers,
                              const char *key) {
  if (!valid(pointers, pointers.capacity) || pointers.count == 0 || !key)
    return DiscardHeadResult::NO_PENDING_EVENT;

  const bool removed = preferences.remove(key);
  if (!removed && preferences.isKey(key))
    return DiscardHeadResult::REMOVE_FAILED_RETAINED;

  Pointers next = pointers;
  next.head = static_cast<uint8_t>((next.head + 1) % next.capacity);
  --next.count;
  const bool saved = savePointers(preferences, next);
  pointers = next;
  return saved ? DiscardHeadResult::REMOVED_POINTERS_SAVED
               : DiscardHeadResult::REMOVED_POINTERS_SAVE_FAILED;
}

// Replace one full-queue victim in place. This keeps ring metadata and queue
// order unchanged, so there is no multi-slot compaction crash window. If a
// failed write leaves an unexpected value, best-effort restore the old event.
template <typename PreferencesLike, typename StringLike>
ReplaceResult replaceRecord(PreferencesLike &preferences, const char *key,
                            const StringLike &oldRecord,
                            const StringLike &newRecord) {
  (void)preferences.putString(key, newRecord);
  StringLike current = preferences.getString(key, "");
  if (current == newRecord) return ReplaceResult::REPLACED;
  if (current == oldRecord) return ReplaceResult::FAILED_OLD_INTACT;

  (void)preferences.putString(key, oldRecord);
  current = preferences.getString(key, "");
  return current == oldRecord ? ReplaceResult::FAILED_RESTORED
                              : ReplaceResult::FAILED_CORRUPTED;
}

// Reconcile committed ring metadata with append writes or ACK removals that
// reached NVS immediately before power loss. Record validation is supplied by
// the caller, which can apply the event identity checks used by the device.
template <typename IsValidRecord>
bool reconcile(Pointers &pointers, IsValidRecord isValidRecord) {
  if (!valid(pointers, pointers.capacity)) return false;
  bool changed = false;
  while (pointers.count && !isValidRecord(pointers.head)) {
    pointers.head = static_cast<uint8_t>((pointers.head + 1) % pointers.capacity);
    --pointers.count;
    changed = true;
  }
  while (pointers.count) {
    const uint8_t tail = static_cast<uint8_t>(
        (pointers.head + pointers.count - 1) % pointers.capacity);
    if (isValidRecord(tail)) break;
    --pointers.count;
    changed = true;
  }
  while (pointers.count < pointers.capacity) {
    const uint8_t next = static_cast<uint8_t>(
        (pointers.head + pointers.count) % pointers.capacity);
    if (!isValidRecord(next)) break;
    ++pointers.count;
    changed = true;
  }
  return changed;
}

}  // namespace event_queue_nvs
