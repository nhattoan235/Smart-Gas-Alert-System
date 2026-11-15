// Failure-injection tests for the production NVS pointer record and replacement path.
// Run from firmware/ with a host C++ compiler:
//   g++ -std=c++11 -Iinclude tests/event_queue_nvs_failure_test.cpp -o event_queue_nvs_failure_test
//   ./event_queue_nvs_failure_test

#include <cassert>
#include <cstdio>
#include <cstring>
#include <map>
#include <string>
#include <vector>

#include "../include/EventQueueNvs.h"

class FakePreferences {
 public:
  enum class Operation { NONE, PUT_BYTES, PUT_STRING, PUT_UCHAR, REMOVE };

  std::map<std::string, std::vector<uint8_t>> blobs;
  std::map<std::string, std::string> strings;
  std::map<std::string, uint8_t> uchars;
  Operation failOperation = Operation::NONE;
  unsigned failAfter = 0;
  unsigned putBytesCalls = 0;
  unsigned putStringCalls = 0;
  unsigned putUCharCalls = 0;
  unsigned removeCalls = 0;
  bool commitPutBytesThenFail = false;
  bool removeAfterErase = false;
  std::string failPutStringAfterWriteKey;
  std::string corruptPutStringOnceKey;

  void failNext(Operation operation, unsigned after = 1) {
    failOperation = operation;
    failAfter = after;
  }

  bool shouldFail(Operation operation) {
    if (failOperation != operation) return false;
    if (failAfter > 1) {
      --failAfter;
      return false;
    }
    failOperation = Operation::NONE;
    failAfter = 0;
    return true;
  }

  bool isKey(const char *key) const {
    return blobs.count(key) || strings.count(key) || uchars.count(key);
  }

  size_t putBytes(const char *key, const void *value, size_t length) {
    ++putBytesCalls;
    const bool fail=shouldFail(Operation::PUT_BYTES);
    if (fail && !commitPutBytesThenFail) return 0;
    const uint8_t *first = static_cast<const uint8_t *>(value);
    blobs[key] = std::vector<uint8_t>(first, first + length);
    if (fail) {
      commitPutBytesThenFail=false;
      return 0;  // Durable write completed, but caller sees an ambiguous failure.
    }
    return length;
  }

  size_t getBytes(const char *key, void *value, size_t maxLength) const {
    auto found = blobs.find(key);
    if (found == blobs.end() || found->second.size() > maxLength) return 0;
    std::memcpy(value, found->second.data(), found->second.size());
    return found->second.size();
  }

  size_t putString(const char *key, const std::string &value) {
    ++putStringCalls;
    if (shouldFail(Operation::PUT_STRING)) return 0;
    if (corruptPutStringOnceKey == key) {
      corruptPutStringOnceKey.clear();
      strings[key] = "partial-corrupt-write";
      return 0;
    }
    strings[key] = value;
    if (failPutStringAfterWriteKey == key) {
      failPutStringAfterWriteKey.clear();
      return 0;
    }
    return value.size() + 1;
  }

  std::string getString(const char *key, const char *fallback = "") const {
    auto found = strings.find(key);
    return found == strings.end() ? std::string(fallback) : found->second;
  }

  bool remove(const char *key) {
    ++removeCalls;
    if (shouldFail(Operation::REMOVE)) {
      if (removeAfterErase) {
        strings.erase(key);
        blobs.erase(key);
        uchars.erase(key);
      }
      return false;
    }
    const size_t removed = strings.erase(key) + blobs.erase(key) + uchars.erase(key);
    return removed != 0;
  }

  size_t putUChar(const char *key, uint8_t value) {
    ++putUCharCalls;
    if (shouldFail(Operation::PUT_UCHAR)) return 0;
    uchars[key] = value;
    return 1;
  }

  uint8_t getUChar(const char *key, uint8_t fallback) const {
    auto found = uchars.find(key);
    return found == uchars.end() ? fallback : found->second;
  }
};

static bool isEvent(const FakePreferences &preferences, uint8_t slot) {
  const std::string key = std::string("e") + std::to_string(slot);
  const std::string value = preferences.getString(key.c_str());
  return value.compare(0, 3, "id:") == 0;
}

static event_queue_nvs::Pointers pointers(uint8_t head, uint8_t count) {
  event_queue_nvs::Pointers state={event_queue_nvs::FORMAT_VERSION,16,head,count};
  return state;
}

static event_queue_nvs::Pointers pointers(uint8_t capacity, uint8_t head,
                                          uint8_t count) {
  event_queue_nvs::Pointers state={event_queue_nvs::FORMAT_VERSION,capacity,
                                   head,count};
  return state;
}

static std::map<std::string, std::string> snapshotLegacyEvents(
    const FakePreferences &preferences, uint8_t capacity) {
  std::map<std::string, std::string> snapshot;
  for (uint8_t slot=0; slot<capacity; ++slot) {
    const std::string key=std::string("e")+std::to_string(slot);
    snapshot[key]=preferences.getString(key.c_str());
  }
  return snapshot;
}

static void seedLegacyQueue(FakePreferences &preferences, uint8_t capacity,
                           uint8_t head, uint8_t count) {
  assert(capacity==8 || capacity==16 || capacity==32);
  assert(head<capacity && count<=capacity);
  assert(preferences.putUChar("cap",capacity)==1);
  assert(preferences.putUChar("head",head)==1);
  assert(preferences.putUChar("count",count)==1);
  for (uint8_t slot=0; slot<capacity; ++slot) {
    const std::string key=std::string("e")+std::to_string(slot);
    const std::string record=std::string("legacy:")+std::to_string(capacity)+
        ":slot:"+std::to_string(slot);
    assert(preferences.putString(key.c_str(),record)!=0);
  }
}

static void assertLegacyMetadataAndEventsUnchanged(
    const FakePreferences &preferences, uint8_t capacity, uint8_t head,
    uint8_t count, const std::map<std::string, std::string> &events) {
  assert(preferences.getUChar("cap",0)==capacity);
  assert(preferences.getUChar("head",0)==head);
  assert(preferences.getUChar("count",0)==count);
  assert(snapshotLegacyEvents(preferences,capacity)==events);
}

static void testLegacyCapacityImportCrashSafety(uint8_t legacyCapacity,
                                                uint8_t legacyHead,
                                                uint8_t legacyCount) {
  const uint8_t maxCapacity=32;
  FakePreferences preferences;
  seedLegacyQueue(preferences,legacyCapacity,legacyHead,legacyCount);
  const std::map<std::string, std::string> originalEvents=
      snapshotLegacyEvents(preferences,legacyCapacity);

  // A failed qstate commit must leave the legacy metadata and all event keys
  // in place. This guards the import API against delete-before-copy migration.
  const unsigned putsBefore=preferences.putBytesCalls;
  const unsigned stringsBefore=preferences.putStringCalls;
  const unsigned removesBefore=preferences.removeCalls;
  preferences.failNext(FakePreferences::Operation::PUT_BYTES);
  event_queue_nvs::Pointers imported{};
  assert(!event_queue_nvs::importLegacyPointers(
      preferences,legacyCapacity,legacyHead,legacyCount,maxCapacity,imported));
  assert(preferences.putBytesCalls==putsBefore+1);
  assert(preferences.putStringCalls==stringsBefore);
  assert(preferences.removeCalls==removesBefore);
  assert(!preferences.isKey(event_queue_nvs::POINTERS_KEY));
  assertLegacyMetadataAndEventsUnchanged(
      preferences,legacyCapacity,legacyHead,legacyCount,originalEvents);

  // Copying the fake NVS storage models a reboot. The failed transaction is
  // not visible; retrying import must succeed without first deleting legacy
  // keys, and the imported pointer record must survive another reload.
  FakePreferences rebooted=preferences;
  event_queue_nvs::Pointers loaded{};
  assert(!event_queue_nvs::loadPointersUpTo(
      rebooted,maxCapacity,loaded));
  assert(event_queue_nvs::importLegacyPointers(
      rebooted,legacyCapacity,legacyHead,legacyCount,maxCapacity,imported));
  assert(imported.capacity==legacyCapacity);
  assert(imported.head==legacyHead && imported.count==legacyCount);
  assert(event_queue_nvs::loadPointersUpTo(rebooted,maxCapacity,loaded));
  assert(event_queue_nvs::samePointers(imported,loaded));
  assertLegacyMetadataAndEventsUnchanged(
      rebooted,legacyCapacity,legacyHead,legacyCount,originalEvents);
}

static void testLegacyCapacityMigrationCrashRegression() {
  // Exercise a wrapped queue at the old 8-slot capacity and a near-full queue
  // at 32 slots. Import preserves the original capacity and does not compact,
  // copy, or delete event keys before a committed pointer record exists.
  testLegacyCapacityImportCrashSafety(8,6,4);
  testLegacyCapacityImportCrashSafety(16,14,8);
  testLegacyCapacityImportCrashSafety(32,29,30);
}

static void testLoadPointersUpToRejectsMalformedCapacities() {
  const uint8_t capacities[]={1,7,17,31,33};
  for (size_t index=0; index<sizeof(capacities)/sizeof(capacities[0]); ++index) {
    const uint8_t capacity=capacities[index];
    FakePreferences preferences;
    const event_queue_nvs::Pointers malformed={
        event_queue_nvs::FORMAT_VERSION,capacity,
        static_cast<uint8_t>(capacity-1),capacity};
    assert(preferences.putBytes(event_queue_nvs::POINTERS_KEY,
                                &malformed,sizeof(malformed))==
           sizeof(malformed));

    // Version, head and count are internally consistent; only the capacity
    // is unsupported (or exceeds maxCapacity for 33).
    event_queue_nvs::Pointers output=pointers(16,3,2);
    const event_queue_nvs::Pointers originalOutput=output;
    assert(!event_queue_nvs::loadPointersUpTo(preferences,32,output));
    assert(event_queue_nvs::samePointers(output,originalOutput));
  }
}

static void testLegacyImportCommitReportedFailedButRebootFindsIt() {
  const uint8_t capacity=32, head=29, count=30, maxCapacity=32;
  FakePreferences preferences;
  seedLegacyQueue(preferences,capacity,head,count);
  const std::map<std::string, std::string> originalEvents=
      snapshotLegacyEvents(preferences,capacity);

  preferences.commitPutBytesThenFail=true;
  preferences.failNext(FakePreferences::Operation::PUT_BYTES);
  event_queue_nvs::Pointers importResult{};
  assert(!event_queue_nvs::importLegacyPointers(
      preferences,capacity,head,count,maxCapacity,importResult));
  assert(preferences.isKey(event_queue_nvs::POINTERS_KEY));
  assertLegacyMetadataAndEventsUnchanged(
      preferences,capacity,head,count,originalEvents);

  // A reported failure is ambiguous: after reboot, prefer the committed
  // qstate and preserve the source legacy metadata and payloads as backup.
  FakePreferences rebooted=preferences;
  event_queue_nvs::Pointers loaded{};
  assert(event_queue_nvs::loadPointersUpTo(
      rebooted,maxCapacity,loaded));
  assert(loaded.capacity==capacity && loaded.head==head && loaded.count==count);
  assertLegacyMetadataAndEventsUnchanged(
      rebooted,capacity,head,count,originalEvents);
}

static void assertInvalidLegacyImportDoesNotMutate(uint8_t requestedCapacity,
                                                   uint8_t requestedHead,
                                                   uint8_t requestedCount,
                                                   uint8_t maxCapacity) {
  const uint8_t storedCapacity=8, storedHead=6, storedCount=4;
  FakePreferences preferences;
  seedLegacyQueue(preferences,storedCapacity,storedHead,storedCount);
  const std::map<std::string, std::string> originalEvents=
      snapshotLegacyEvents(preferences,storedCapacity);
  const std::map<std::string, std::vector<uint8_t> > originalBlobs=
      preferences.blobs;
  const std::map<std::string, std::string> originalStrings=
      preferences.strings;
  const std::map<std::string, uint8_t> originalUchars=
      preferences.uchars;
  const unsigned putBytesBefore=preferences.putBytesCalls;
  const unsigned putStringBefore=preferences.putStringCalls;
  const unsigned putUCharBefore=preferences.putUCharCalls;
  const unsigned removeBefore=preferences.removeCalls;
  const event_queue_nvs::Pointers originalOutput=pointers(16,3,2);
  event_queue_nvs::Pointers output=originalOutput;

  assert(!event_queue_nvs::importLegacyPointers(
      preferences,requestedCapacity,requestedHead,requestedCount,
      maxCapacity,output));
  assert(preferences.putBytesCalls==putBytesBefore);
  assert(preferences.putStringCalls==putStringBefore);
  assert(preferences.putUCharCalls==putUCharBefore);
  assert(preferences.removeCalls==removeBefore);
  assert(preferences.blobs==originalBlobs);
  assert(preferences.strings==originalStrings);
  assert(preferences.uchars==originalUchars);
  assert(event_queue_nvs::samePointers(output,originalOutput));
  assertLegacyMetadataAndEventsUnchanged(
      preferences,storedCapacity,storedHead,storedCount,originalEvents);
}

static void testInvalidLegacyPointersAreRejectedWithoutMutation() {
  assertInvalidLegacyImportDoesNotMutate(0,0,0,32);    // Invalid capacity.
  assertInvalidLegacyImportDoesNotMutate(33,0,0,32);   // Over maximum.
  assertInvalidLegacyImportDoesNotMutate(8,8,1,32);    // head == capacity.
  assertInvalidLegacyImportDoesNotMutate(8,0,9,32);    // count > capacity.
}

static void testLegacySplitPointerFailureReproducesInconsistentState() {
  FakePreferences preferences;
  preferences.putUChar("head", 4);
  preferences.putUChar("count", 7);

  // Mirrors the old two-commit saveEventPointers() implementation.
  assert(preferences.putUChar("head", 5) == 1);
  preferences.failNext(FakePreferences::Operation::PUT_UCHAR);
  assert(preferences.putUChar("count", 8) == 0);

  // After reboot the reader observes new head with old count.
  assert(preferences.getUChar("head", 0) == 5);
  assert(preferences.getUChar("count", 0) == 7);
}

static void testAtomicPointerWriteFailureKeepsOldState() {
  FakePreferences preferences;
  event_queue_nvs::Pointers oldState=pointers(4,7),newState=pointers(5,8),reloaded{};
  assert(event_queue_nvs::savePointers(preferences,oldState));
  preferences.failNext(FakePreferences::Operation::PUT_BYTES);
  assert(!event_queue_nvs::savePointers(preferences,newState));
  assert(event_queue_nvs::loadPointers(preferences,16,reloaded));
  assert(reloaded.head==4 && reloaded.count==7);
}

static void testAppendRecordRecoversIfPointerCommitFails() {
  FakePreferences preferences;
  event_queue_nvs::Pointers committed=pointers(0,0),rebooted{};
  assert(event_queue_nvs::savePointers(preferences,committed));
  assert(preferences.putString("e0","id:bootA:1") != 0);

  event_queue_nvs::Pointers appended=pointers(0,1);
  preferences.failNext(FakePreferences::Operation::PUT_BYTES);
  assert(!event_queue_nvs::savePointers(preferences,appended));

  assert(event_queue_nvs::loadPointers(preferences,16,rebooted));
  assert(rebooted.count==0);
  const bool changed=event_queue_nvs::reconcile(
      rebooted,[&preferences](uint8_t slot) { return isEvent(preferences,slot); });
  assert(changed && rebooted.head==0 && rebooted.count==1);
}

static void testAppendRecordWriteFailureLeavesQueueUnchanged() {
  FakePreferences preferences;
  event_queue_nvs::Pointers committed=pointers(3,0),reloaded{};
  assert(event_queue_nvs::savePointers(preferences,committed));
  preferences.failNext(FakePreferences::Operation::PUT_STRING);
  assert(preferences.putString("e3","id:bootA:2")==0);

  assert(event_queue_nvs::loadPointers(preferences,16,reloaded));
  assert(reloaded.head==3 && reloaded.count==0);
  assert(!isEvent(preferences,3));
  assert(!event_queue_nvs::reconcile(
      reloaded,[&preferences](uint8_t slot) { return isEvent(preferences,slot); }));
  assert(reloaded.head==3 && reloaded.count==0);
}

static void testAckRemoveFailureRetainsRecordAndPointers() {
  FakePreferences preferences;
  event_queue_nvs::Pointers state=pointers(0,1),reloaded{};
  assert(event_queue_nvs::savePointers(preferences,state));
  assert(preferences.putString("e0","id:bootB:2") != 0);

  preferences.failNext(FakePreferences::Operation::REMOVE);
  assert(!preferences.remove("e0"));
  assert(event_queue_nvs::loadPointers(preferences,16,reloaded));
  assert(reloaded.head==0 && reloaded.count==1 && isEvent(preferences,0));
}

static void testRemoveMayReportFailureAfterEraseAndReconcileOnReboot() {
  FakePreferences preferences;
  event_queue_nvs::Pointers state=pointers(6,1),reloaded{};
  assert(event_queue_nvs::savePointers(preferences,state));
  assert(preferences.putString("e6","id:bootB:3") != 0);

  // Model a remove that erased the key but reported failure to the caller.
  preferences.removeAfterErase=true;
  preferences.failNext(FakePreferences::Operation::REMOVE);
  assert(!preferences.remove("e6"));
  assert(!isEvent(preferences,6));
  assert(event_queue_nvs::loadPointers(preferences,16,reloaded));
  assert(reloaded.head==6 && reloaded.count==1);
  assert(event_queue_nvs::reconcile(
      reloaded,[&preferences](uint8_t slot) { return isEvent(preferences,slot); }));
  assert(reloaded.head==7 && reloaded.count==0);
}

static void testSuccessfulRemoveWithFailedPointerCommitRecoversOnReboot() {
  FakePreferences preferences;
  event_queue_nvs::Pointers state=pointers(0,1),reloaded{};
  assert(event_queue_nvs::savePointers(preferences,state));
  assert(preferences.putString("e0","id:bootC:3") != 0);
  assert(preferences.remove("e0"));

  preferences.failNext(FakePreferences::Operation::PUT_BYTES);
  assert(!event_queue_nvs::savePointers(preferences,pointers(1,0)));
  assert(event_queue_nvs::loadPointers(preferences,16,reloaded));
  assert(reloaded.head==0 && reloaded.count==1);
  assert(event_queue_nvs::reconcile(
      reloaded,[&preferences](uint8_t slot) { return isEvent(preferences,slot); }));
  assert(reloaded.head==1 && reloaded.count==0);
}

static void seedFullQueue(FakePreferences &preferences,
                          event_queue_nvs::Pointers &state) {
  state=pointers(13,16);  // Exercise wraparound while the ring is full.
  assert(event_queue_nvs::savePointers(preferences,state));
  for(uint8_t offset=0;offset<state.count;offset++) {
    const uint8_t slot=static_cast<uint8_t>((state.head+offset)%state.capacity);
    const std::string key=std::string("e")+std::to_string(slot);
    const std::string value=std::string("id:boot:")+std::to_string(offset+1);
    assert(preferences.putString(key.c_str(),value)!=0);
  }
}

static void assertQueueExceptSlot(const FakePreferences &preferences,
                                  const event_queue_nvs::Pointers &state,
                                  uint8_t changedSlot,
                                  const std::string &expectedChanged) {
  assert(state.head==13 && state.count==16);
  for(uint8_t offset=0;offset<state.count;offset++) {
    const uint8_t slot=static_cast<uint8_t>((state.head+offset)%state.capacity);
    const std::string key=std::string("e")+std::to_string(slot);
    const std::string expected=slot==changedSlot ? expectedChanged :
        std::string("id:boot:")+std::to_string(offset+1);
    assert(preferences.getString(key.c_str())==expected);
  }
}

static void testFullQueueReplaceFailureBeforeWriteKeepsQueueOnReboot() {
  FakePreferences preferences;
  event_queue_nvs::Pointers state{},reloaded{};
  seedFullQueue(preferences,state);
  const uint8_t victimOffset=3;
  const uint8_t victimSlot=static_cast<uint8_t>(
      (state.head+victimOffset)%state.capacity);
  const std::string key=std::string("e")+std::to_string(victimSlot);
  const std::string oldRecord=preferences.getString(key.c_str());
  preferences.failNext(FakePreferences::Operation::PUT_STRING);
  const event_queue_nvs::ReplaceResult result=event_queue_nvs::replaceRecord(
      preferences,key.c_str(),oldRecord,std::string("id:incoming:99"));
  assert(result==event_queue_nvs::ReplaceResult::FAILED_OLD_INTACT);

  assert(event_queue_nvs::loadPointers(preferences,16,reloaded));
  assert(event_queue_nvs::samePointers(state,reloaded));
  assertQueueExceptSlot(preferences,reloaded,victimSlot,oldRecord);
}

static void testFullQueueReplaceSurvivesFalseReturnAfterWriteAndReboot() {
  FakePreferences preferences;
  event_queue_nvs::Pointers state{},reloaded{};
  seedFullQueue(preferences,state);
  const uint8_t victimOffset=3;
  const uint8_t victimSlot=static_cast<uint8_t>(
      (state.head+victimOffset)%state.capacity);
  const std::string key=std::string("e")+std::to_string(victimSlot);
  const std::string oldRecord=preferences.getString(key.c_str());
  const std::string incoming="id:incoming:99";
  preferences.failPutStringAfterWriteKey=key;
  const event_queue_nvs::ReplaceResult result=event_queue_nvs::replaceRecord(
      preferences,key.c_str(),oldRecord,incoming);
  assert(result==event_queue_nvs::ReplaceResult::REPLACED);

  assert(event_queue_nvs::loadPointers(preferences,16,reloaded));
  assert(event_queue_nvs::samePointers(state,reloaded));
  assertQueueExceptSlot(preferences,reloaded,victimSlot,incoming);
}

static void testFailedPartialReplaceRestoresOldRecord() {
  FakePreferences preferences;
  event_queue_nvs::Pointers state{},reloaded{};
  seedFullQueue(preferences,state);
  const uint8_t victimSlot=static_cast<uint8_t>((state.head+2)%state.capacity);
  const std::string key=std::string("e")+std::to_string(victimSlot);
  const std::string oldRecord=preferences.getString(key.c_str());
  preferences.corruptPutStringOnceKey=key;
  const event_queue_nvs::ReplaceResult result=event_queue_nvs::replaceRecord(
      preferences,key.c_str(),oldRecord,std::string("id:incoming:100"));
  assert(result==event_queue_nvs::ReplaceResult::FAILED_RESTORED);

  assert(event_queue_nvs::loadPointers(preferences,16,reloaded));
  assert(event_queue_nvs::samePointers(state,reloaded));
  assertQueueExceptSlot(preferences,reloaded,victimSlot,oldRecord);
}

static void testFailedCorruptReplaceReconcilesHeadAfterReboot() {
  FakePreferences preferences;
  event_queue_nvs::Pointers state{},reloaded{};
  seedFullQueue(preferences,state);
  const uint8_t victimSlot=state.head;
  const std::string key=std::string("e")+std::to_string(victimSlot);
  const std::string oldRecord=preferences.getString(key.c_str());

  // First write leaves a partial record; restoration also fails. The model
  // can prove the resulting key is corrupt, but cannot recover its payload.
  preferences.corruptPutStringOnceKey=key;
  preferences.failNext(FakePreferences::Operation::PUT_STRING,2);
  const event_queue_nvs::ReplaceResult result=event_queue_nvs::replaceRecord(
      preferences,key.c_str(),oldRecord,std::string("id:incoming:101"));
  assert(result==event_queue_nvs::ReplaceResult::FAILED_CORRUPTED);
  assert(preferences.getString(key.c_str())=="partial-corrupt-write");

  // After reboot, reconciliation may discard the unreadable head and persist
  // the remaining 15 records. This test does not claim the corrupt event was
  // recovered or that this path is lossless.
  FakePreferences rebooted=preferences;
  assert(event_queue_nvs::loadPointers(rebooted,16,reloaded));
  assert(event_queue_nvs::samePointers(state,reloaded));
  const bool changed=event_queue_nvs::reconcile(
      reloaded,[&rebooted](uint8_t slot) { return isEvent(rebooted,slot); });
  assert(changed);
  assert(reloaded.head==static_cast<uint8_t>((state.head+1)%state.capacity));
  assert(reloaded.count==state.count-1);
  assert(rebooted.getString(key.c_str())=="partial-corrupt-write");
  for (uint8_t offset=0; offset<reloaded.count; ++offset) {
    const uint8_t slot=static_cast<uint8_t>(
        (reloaded.head+offset)%reloaded.capacity);
    const std::string eventKeyName=std::string("e")+std::to_string(slot);
    const std::string expected=std::string("id:boot:")+std::to_string(offset+2);
    assert(rebooted.getString(eventKeyName.c_str())==expected);
  }
  assert(event_queue_nvs::savePointers(rebooted,reloaded));
  event_queue_nvs::Pointers persisted{};
  assert(event_queue_nvs::loadPointers(rebooted,16,persisted));
  assert(event_queue_nvs::samePointers(reloaded,persisted));
}

static void seedQueueWithMalformedMiddle(FakePreferences &preferences,
                                         event_queue_nvs::Pointers &state) {
  state=pointers(4,3);
  assert(event_queue_nvs::savePointers(preferences,state));
  assert(preferences.putString("e4","id:boot:10")!=0);
  assert(preferences.putString("e5","malformed-event")!=0);
  assert(preferences.putString("e6","id:boot:12")!=0);

  // The first valid event has already been consumed. The formerly-middle bad
  // record is now the queue head, with a later valid event behind it.
  assert(preferences.remove("e4"));
  state.head=5;
  state.count=2;
  assert(event_queue_nvs::savePointers(preferences,state));
}

static void testDiscardMalformedHeadPreservesLaterEvents() {
  FakePreferences preferences;
  event_queue_nvs::Pointers state{},reloaded{};
  seedQueueWithMalformedMiddle(preferences,state);
  assert(state.head==5 && state.count==2);

  const event_queue_nvs::DiscardHeadResult result=
      event_queue_nvs::discardHead(preferences,state,"e5");
  assert(result==event_queue_nvs::DiscardHeadResult::REMOVED_POINTERS_SAVED);
  assert(state.head==6 && state.count==1);
  assert(!preferences.isKey("e5"));
  assert(preferences.getString("e6")=="id:boot:12");
  assert(event_queue_nvs::loadPointers(preferences,16,reloaded));
  assert(event_queue_nvs::samePointers(state,reloaded));
  assert(isEvent(preferences,reloaded.head));
}

static void testDiscardHeadRemoveFailureBeforeDeleteKeepsQueueUnchanged() {
  FakePreferences preferences;
  event_queue_nvs::Pointers state{},reloaded{};
  seedQueueWithMalformedMiddle(preferences,state);
  const event_queue_nvs::Pointers originalState=state;
  const std::string laterEvent=preferences.getString("e6");
  preferences.failNext(FakePreferences::Operation::REMOVE);

  const event_queue_nvs::DiscardHeadResult result=
      event_queue_nvs::discardHead(preferences,state,"e5");
  assert(result==event_queue_nvs::DiscardHeadResult::REMOVE_FAILED_RETAINED);
  assert(event_queue_nvs::samePointers(state,originalState));
  assert(preferences.getString("e5")=="malformed-event");
  assert(preferences.getString("e6")==laterEvent);
  assert(event_queue_nvs::loadPointers(preferences,16,reloaded));
  assert(event_queue_nvs::samePointers(reloaded,originalState));
}

static void testDiscardHeadRemoveReportsFailureAfterDelete() {
  FakePreferences preferences;
  event_queue_nvs::Pointers state{},reloaded{};
  seedQueueWithMalformedMiddle(preferences,state);
  const std::string laterEvent=preferences.getString("e6");
  preferences.removeAfterErase=true;
  preferences.failNext(FakePreferences::Operation::REMOVE);

  const event_queue_nvs::DiscardHeadResult result=
      event_queue_nvs::discardHead(preferences,state,"e5");
  assert(result==event_queue_nvs::DiscardHeadResult::REMOVED_POINTERS_SAVED);
  assert(state.head==6 && state.count==1);
  assert(!preferences.isKey("e5"));
  assert(preferences.getString("e6")==laterEvent);

  FakePreferences rebooted=preferences;
  assert(event_queue_nvs::loadPointers(rebooted,16,reloaded));
  assert(event_queue_nvs::samePointers(state,reloaded));
  assert(rebooted.getString("e6")==laterEvent);
}

static void testDiscardHeadPointerSaveFailureRecoversOnReboot() {
  FakePreferences preferences;
  event_queue_nvs::Pointers state{},reloaded{};
  seedQueueWithMalformedMiddle(preferences,state);
  const event_queue_nvs::Pointers persistedBefore=state;
  const std::string laterEvent=preferences.getString("e6");
  preferences.failNext(FakePreferences::Operation::PUT_BYTES);

  const event_queue_nvs::DiscardHeadResult result=
      event_queue_nvs::discardHead(preferences,state,"e5");
  assert(result==event_queue_nvs::DiscardHeadResult::REMOVED_POINTERS_SAVE_FAILED);
  assert(state.head==6 && state.count==1);  // Advanced in-memory retry state.
  assert(!preferences.isKey("e5"));
  assert(preferences.getString("e6")==laterEvent);

  // Flash retained the old pointers but the bad head key is gone. Reboot
  // reconciliation must advance once and preserve the next readable event.
  FakePreferences rebooted=preferences;
  assert(event_queue_nvs::loadPointers(rebooted,16,reloaded));
  assert(event_queue_nvs::samePointers(reloaded,persistedBefore));
  assert(event_queue_nvs::reconcile(
      reloaded,[&rebooted](uint8_t slot) { return isEvent(rebooted,slot); }));
  assert(reloaded.head==6 && reloaded.count==1);
  assert(rebooted.getString("e6")==laterEvent);
  assert(isEvent(rebooted,reloaded.head));
  assert(event_queue_nvs::savePointers(rebooted,reloaded));
  event_queue_nvs::Pointers recovered{};
  assert(event_queue_nvs::loadPointers(rebooted,16,recovered));
  assert(event_queue_nvs::samePointers(reloaded,recovered));
}

int main() {
  testLegacySplitPointerFailureReproducesInconsistentState();
  testAtomicPointerWriteFailureKeepsOldState();
  testAppendRecordRecoversIfPointerCommitFails();
  testAppendRecordWriteFailureLeavesQueueUnchanged();
  testAckRemoveFailureRetainsRecordAndPointers();
  testRemoveMayReportFailureAfterEraseAndReconcileOnReboot();
  testSuccessfulRemoveWithFailedPointerCommitRecoversOnReboot();
  testFullQueueReplaceFailureBeforeWriteKeepsQueueOnReboot();
  testFullQueueReplaceSurvivesFalseReturnAfterWriteAndReboot();
  testFailedPartialReplaceRestoresOldRecord();
  testFailedCorruptReplaceReconcilesHeadAfterReboot();
  testDiscardMalformedHeadPreservesLaterEvents();
  testDiscardHeadRemoveFailureBeforeDeleteKeepsQueueUnchanged();
  testDiscardHeadRemoveReportsFailureAfterDelete();
  testDiscardHeadPointerSaveFailureRecoversOnReboot();
  testLegacyCapacityMigrationCrashRegression();
  testLegacyImportCommitReportedFailedButRebootFindsIt();
  testInvalidLegacyPointersAreRejectedWithoutMutation();
  testLoadPointersUpToRejectsMalformedCapacities();
  std::puts("Event queue NVS failure-injection cases passed.");
  return 0;
}
