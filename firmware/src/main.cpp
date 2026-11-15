#include <Arduino.h>
#include <WiFi.h>
#include <Preferences.h>
#include <math.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <freertos/FreeRTOS.h>
#include <freertos/event_groups.h>
#include <freertos/queue.h>
#include <freertos/task.h>
#include "Detector.h"
#include "EventQueuePolicy.h"
#include "EventQueueNvs.h"
#include "MqttLite.h"
#include "MqttCredentials.h"
#if defined(NVS_RUNTIME_TEST) && NVS_RUNTIME_TEST
#include "esp_attr.h"
#endif
#ifndef SIMULATION
#define SIMULATION 1
#endif
#ifndef HARDWARE_N8R8
#define HARDWARE_N8R8 0
#endif
#ifndef N8R8_SENSOR_CONFIGURED
#define N8R8_SENSOR_CONFIGURED 0
#endif
#ifndef NVS_RUNTIME_TEST
#define NVS_RUNTIME_TEST 0
#endif
#if NVS_RUNTIME_TEST && !SIMULATION
#error "NVS runtime test controls are available only in the isolated simulation build."
#endif
#if !SIMULATION && !HARDWARE_N8R8
#error "Select either a simulation environment or an explicit hardware environment."
#endif
#if HARDWARE_N8R8 && SIMULATION
#error "Hardware and simulation acquisition modes cannot be enabled together."
#endif
#if HARDWARE_N8R8
#include "SensorDrivers.h"
#include "SensorHealth.h"
#if N8R8_SENSOR_CONFIGURED
#include <Wire.h>
#include "WireI2cBus.h"
#if !defined(N8R8_ADS1115_ADDRESS) || !defined(N8R8_SHT31_ADDRESS) || \
    !defined(N8R8_ADS1115_GAIN) || !defined(N8R8_ADS1115_RATE) || \
    !defined(N8R8_I2C_CLOCK_HZ) || !defined(N8R8_I2C_TIMEOUT_MS) || \
    !defined(N8R8_MAX_DATA_AGE_MS)
#error "Set verified device addresses, ADS1115 range/rate, I2C clock/timeout, and data-age policy before enabling physical acquisition."
#endif
static WireI2cBus hardwareI2cBus(Wire);
static Ads1115ChannelSensor lpgHardwareAdc(
    hardwareI2cBus, N8R8_ADS1115_ADDRESS, ADS1115_MQ6_CHANNEL,
    static_cast<Ads1115Gain>(N8R8_ADS1115_GAIN),
    static_cast<Ads1115Rate>(N8R8_ADS1115_RATE));
static Ads1115ChannelSensor coHardwareAdc(
    hardwareI2cBus, N8R8_ADS1115_ADDRESS, ADS1115_MQ7B_CHANNEL,
    static_cast<Ads1115Gain>(N8R8_ADS1115_GAIN),
    static_cast<Ads1115Rate>(N8R8_ADS1115_RATE));
static Sht31Sensor environmentHardwareSensor(hardwareI2cBus,
                                               N8R8_SHT31_ADDRESS);
static CalibrationRecord lpgCalibration;
static CalibrationRecord coCalibration;
// Until an independently reviewed gas transform/profile is supplied, this
// remains null and neither ADC result can enter the demo-threshold detector.
static const IRelativeGasSignalTransform *lpgSignalTransform = nullptr;
static const IRelativeGasSignalTransform *coSignalTransform = nullptr;
static RawSensorSample lpgHardwareRaw = {0, 0, SensorReadStatus::NOT_CONFIGURED};
static RawSensorSample coHardwareRaw = {0, 0, SensorReadStatus::NOT_CONFIGURED};
static EnvironmentSample environmentHardwareRaw = {
    0.0f, 0.0f, 0, SensorReadStatus::NOT_CONFIGURED};
static bool hardwareBusReady = false;
#endif
static bool calibrationRequired = true;
#endif
#ifndef WIFI_SSID
#define WIFI_SSID "Wokwi-GUEST"
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""
#endif
#ifndef MQTT_USER
#define MQTT_USER ""
#endif
static constexpr int BUZZER=4,GREEN=5,AMBER=6,RED=7;
#if SIMULATION
static constexpr int LPG=1,CO=2,TEMP=8,HUM=9,INTERFERENCE=10,LPG_FAULT=13,ACK=14,VALVE=15,STOVE=16,CO_FAULT=21,INSPECT=12,LPG_TRUTH=11,CO_TRUTH=17;
#endif
// Initial sample period from the design; tune only after measured scenario tests.
static constexpr uint32_t SAMPLE_INTERVAL_MS=100;
static Detector lpgDetector,coDetector;
static MqttLite mqtt;
static Preferences eventStore;
static String prefix,bootId;
static uint32_t lastSample=0,lastPublish=0,seq=0,muteUntil=0;
static AlarmState priorLpg=AlarmState::WARMUP,priorCo=AlarmState::WARMUP;
static bool priorAck=false,priorInspect=false;
static float lpgSignal=0,coSignal=0,tempC=0,humidity=0;
static float lpgTruth=0,interferencePct=0,environmentBias=0;
static bool lpgValid=false,coValid=false,environmentValid=false;
static uint8_t eventHead=0,eventCount=0;
static String eventRemoveRetryPayload;
static uint8_t eventRemoveRetrySlot=0xFF;
// Default ESP32-S3 partition has a 20 KiB NVS area. Keep the queue bounded within it.
static constexpr uint8_t EVENT_CAPACITY=16;
static constexpr uint8_t EVENT_MAX_CAPACITY=32;
static uint8_t eventCapacity=EVENT_CAPACITY;
static constexpr size_t MQTT_TOPIC_CAPACITY=96;
static constexpr size_t MQTT_PAYLOAD_CAPACITY=768;
static constexpr EventBits_t MQTT_CONNECTED_BIT=static_cast<EventBits_t>(1U);
enum class PublishKind : uint8_t { EVENT=1, TELEMETRY=2 };
struct PublishRequest {
  uint32_t token;
  PublishKind kind;
  char bootId[32];
  uint32_t seq;
  uint8_t queueCount;
  char topic[MQTT_TOPIC_CAPACITY];
  char payload[MQTT_PAYLOAD_CAPACITY];
};
struct PublishResult {
  uint32_t token;
  PublishKind kind;
  bool accepted;
  uint16_t packetId;
  char bootId[32];
  uint32_t seq;
};
struct CommitAck {
  char bootId[32];
  uint32_t seq;
};
static QueueHandle_t eventPublishQueue=nullptr;
static QueueHandle_t telemetryPublishQueue=nullptr;
static QueueHandle_t publishResultQueue=nullptr;
static QueueHandle_t commitAckQueue=nullptr;
static EventGroupHandle_t mqttEventGroup=nullptr;
static bool mqttTaskReady=false;
static bool eventQueueReady=true;
static uint32_t nextPublishToken=1,activeEventToken=0;
static bool eventPublishPending=false;
static uint32_t lostEvents=0;
static uint16_t eventInFlight=0;
static uint32_t eventSentAt=0;
static uint32_t eventLastAttemptAt=0;
static uint32_t lastSampleUs=0,maxSampleGapUs=0,maxAcquireToAlarmUs=0,maxSampleConditionToAlarmUs=0,missedSamplePeriods=0;
static uint32_t lastMetricsPrint=0;
#if NVS_RUNTIME_TEST
static constexpr uint32_t NVS_TEST_RTC_MAGIC=0x4E565354UL;
RTC_DATA_ATTR static uint32_t nvsTestRtcMagic=0;
RTC_DATA_ATTR static int nvsTestLpgRaw=-1;
static bool nvsTestLpgInvalid=false;
static bool nvsTestCoInvalid=false;
static char nvsTestCommand[64];
static size_t nvsTestCommandLength=0;
static void dumpNvsRuntimeTestQueue();

static void printNvsRuntimeTestEventRecord(const char *device,const char *eventBoot,
                                           uint32_t eventSeq,const char *data) {
  // Keep each test-only UART record short; the host validates every fragment
  // and fails closed if a concurrent ESP-IDF diagnostic corrupts one.
  constexpr size_t chunkBytes=16;
  const size_t payloadBytes=strlen(data);
  const unsigned chunks=(unsigned)((payloadBytes+chunkBytes-1)/chunkBytes);
  Serial.printf("nvs_test_event_record_begin device_id=%s boot_id=%s seq=%lu payload_bytes=%u chunks=%u\n",
    device,eventBoot,(unsigned long)eventSeq,(unsigned)payloadBytes,chunks);
  for(unsigned part=0;part<chunks;part++) {
    const size_t offset=(size_t)part*chunkBytes;
    const size_t remaining=payloadBytes-offset;
    const int length=(int)(remaining<chunkBytes ? remaining : chunkBytes);
    Serial.printf("nvs_test_event_record_chunk device_id=%s boot_id=%s seq=%lu part=%u data=%.*s\n",
      device,eventBoot,(unsigned long)eventSeq,part,length,data+offset);
  }
  Serial.printf("nvs_test_event_record_end device_id=%s boot_id=%s seq=%lu chunks=%u\n",
    device,eventBoot,(unsigned long)eventSeq,chunks);
}

static void processNvsRuntimeTestCommand(const char *command) {
  int raw=-1;
  char extra=0;
  if(sscanf(command,"NVS_TEST SET_LPG_ADC %d %c",&raw,&extra)==1 &&
     raw>=0 && raw<=4095) {
    nvsTestLpgRaw=raw;
    Serial.printf("nvs_test_control outcome=accepted command=set_lpg_adc raw=%d\n",raw);
    return;
  }
  if(strcmp(command,"NVS_TEST CLEAR_LPG_ADC")==0) {
    nvsTestLpgRaw=-1;
    Serial.println("nvs_test_control outcome=accepted command=clear_lpg_adc");
    return;
  }
  char sensor[12]={0};
  int invalid=-1;
  char sensorExtra=0;
  if(sscanf(command,"NVS_TEST SET_SENSOR_INVALID %11s %d %c",sensor,&invalid,&sensorExtra)==2 &&
     (invalid==0 || invalid==1)) {
    if(strcmp(sensor,"LPG")==0) {
      nvsTestLpgInvalid=invalid!=0;
    } else if(strcmp(sensor,"CO")==0) {
      nvsTestCoInvalid=invalid!=0;
    } else if(strcmp(sensor,"BOTH")==0) {
      nvsTestLpgInvalid=invalid!=0;
      nvsTestCoInvalid=invalid!=0;
    } else {
      Serial.println("nvs_test_control outcome=rejected command=set_sensor_invalid reason=unknown_sensor");
      return;
    }
    Serial.printf("nvs_test_control outcome=accepted command=set_sensor_invalid sensor=%s invalid=%u\n",
      sensor,(unsigned)invalid);
    return;
  }
  if(strcmp(command,"NVS_TEST STATUS")==0) {
    const bool mqttConnected=mqttEventGroup &&
        (xEventGroupGetBits(mqttEventGroup)&MQTT_CONNECTED_BIT);
    Serial.printf("nvs_test_status device_id=%s boot_id=%s seq=%lu queue_count=%u mqtt_connected=%u lpg_override=%d lpg_invalid_test=%u co_invalid_test=%u red_pin=%d buzzer_duty=%u\n",
      DEVICE_ID,bootId.c_str(),(unsigned long)seq,(unsigned)eventCount,
      mqttConnected ? 1U : 0U,nvsTestLpgRaw,(unsigned)nvsTestLpgInvalid,
      (unsigned)nvsTestCoInvalid,digitalRead(RED),
      (unsigned)ledcRead(0));
    return;
  }
  if(strcmp(command,"NVS_TEST DUMP_QUEUE")==0) {
    dumpNvsRuntimeTestQueue();
    return;
  }
  if(strcmp(command,"NVS_TEST REBOOT")==0) {
    Serial.printf("nvs_test_control outcome=accepted command=reboot boot_id=%s queue_count=%u\n",
      bootId.c_str(),(unsigned)eventCount);
    Serial.flush();
    delay(50);
    ESP.restart();
    return;
  }
  Serial.println("nvs_test_control outcome=rejected command=unknown_or_invalid");
}

static void pollNvsRuntimeTestSerial() {
  while(Serial.available()>0) {
    const char c=(char)Serial.read();
    if(c=='\r') continue;
    if(c=='\n') {
      nvsTestCommand[nvsTestCommandLength]='\0';
      if(nvsTestCommandLength) processNvsRuntimeTestCommand(nvsTestCommand);
      nvsTestCommandLength=0;
      continue;
    }
    if(nvsTestCommandLength+1<sizeof(nvsTestCommand)) {
      nvsTestCommand[nvsTestCommandLength++]=c;
    } else {
      nvsTestCommandLength=0;
      Serial.println("nvs_test_control outcome=rejected command=line_too_long");
    }
  }
}
#endif

static int severity(AlarmState s) {
  switch(s) {
    case AlarmState::DANGER: return 5;
    case AlarmState::FAULT: return 4;
    case AlarmState::WARNING: return 3;
    case AlarmState::WATCH: return 2;
    case AlarmState::SAFE: return 1;
    default: return 0;
  }
}
static AlarmState overallState() {
  return severity(lpgDetector.state())>=severity(coDetector.state()) ? lpgDetector.state() : coDetector.state();
}

static String eventKey(uint8_t slot) { return String("e")+String(slot); }
static int eventPriority(const String &data) {
  return event_queue_policy::priority(data.c_str());
}
static bool copyCString(char *destination,size_t capacity,const char *source) {
  if(!capacity || !source) return false;
  size_t length=strlen(source);
  if(length>=capacity) return false;
  memcpy(destination,source,length+1);
  return true;
}
static bool parseEventIdentity(const char *data,char *boot,size_t bootCapacity,uint32_t &eventSeq) {
  static const char bootKey[]="\"boot_id\":\"";
  static const char seqKey[]="\"seq\":";
  const char *bootStart=strstr(data,bootKey);
  if(!bootStart) return false;
  bootStart+=sizeof(bootKey)-1;
  const char *bootEnd=strchr(bootStart,'\"');
  if(!bootEnd || bootEnd==bootStart) return false;
  size_t bootLength=(size_t)(bootEnd-bootStart);
  if(bootLength>=bootCapacity) return false;
  memcpy(boot,bootStart,bootLength); boot[bootLength]=0;
  const char *seqStart=strstr(bootEnd,seqKey);
  if(!seqStart) return false;
  seqStart+=sizeof(seqKey)-1;
  char *end=nullptr;
  unsigned long parsed=strtoul(seqStart,&end,10);
  if(end==seqStart || parsed==0 || parsed>0xFFFFFFFFUL) return false;
  eventSeq=(uint32_t)parsed;
  return true;
}
static bool eventMatches(const char *data,const char *boot,uint32_t eventSeq) {
  char storedBoot[32]; uint32_t storedSeq=0;
  return parseEventIdentity(data,storedBoot,sizeof(storedBoot),storedSeq) &&
         storedSeq==eventSeq && strcmp(storedBoot,boot)==0;
}
#if NVS_RUNTIME_TEST
static void dumpNvsRuntimeTestQueue() {
  const uint8_t expectedCount=eventCount;
  uint8_t records=0;
  bool valid=true;
  Serial.printf("nvs_test_queue_snapshot_begin device_id=%s boot_id=%s queue_count=%u head=%u capacity=%u\n",
    DEVICE_ID,bootId.c_str(),(unsigned)expectedCount,(unsigned)eventHead,
    (unsigned)eventCapacity);
  for(uint8_t index=0;index<expectedCount;index++) {
    const uint8_t slot=(eventHead+index)%eventCapacity;
    String payload=eventStore.getString(eventKey(slot).c_str(),"");
    char storedBoot[32]="-";
    uint32_t storedSeq=0;
    if(!payload.length() || !parseEventIdentity(payload.c_str(),storedBoot,
                                                sizeof(storedBoot),storedSeq)) {
      valid=false;
      Serial.printf("nvs_test_queue_record device_id=%s index=%u slot=%u queue_count=%u outcome=invalid\n",
        DEVICE_ID,(unsigned)index,(unsigned)slot,(unsigned)expectedCount);
      continue;
    }
    Serial.printf("nvs_test_queue_record device_id=%s event_boot_id=%s event_seq=%lu index=%u slot=%u queue_count=%u payload=%s\n",
      DEVICE_ID,storedBoot,(unsigned long)storedSeq,(unsigned)index,
      (unsigned)slot,(unsigned)expectedCount,payload.c_str());
    records++;
  }
  const bool complete=valid && records==expectedCount;
  Serial.printf("nvs_test_queue_snapshot_end device_id=%s boot_id=%s queue_count=%u records=%u outcome=%s\n",
    DEVICE_ID,bootId.c_str(),(unsigned)expectedCount,(unsigned)records,
    complete ? "complete" : "invalid");
}
#endif
static bool hasValidStoredEventSlot(uint8_t slot) {
  String key=eventKey(slot);
  if(!eventStore.isKey(key.c_str())) return false;
  String data=eventStore.getString(key.c_str(),"");
  char storedBoot[32]; uint32_t storedSeq=0;
  return parseEventIdentity(data.c_str(),storedBoot,sizeof(storedBoot),storedSeq);
}
static void eventDiag(const char *stage,const char *outcome,const char *boot,
                      uint32_t eventSeq,const char *identityKind,
                      uint8_t queueCount,const char *detail="-",
                      uint16_t packetId=0) {
  Serial.printf("event_diag device_id=%s boot_id=%s seq=%lu identity=%s millis=%lu stage=%s outcome=%s queue_count=%u packet_id=%u detail=%s\n",
    DEVICE_ID,boot && *boot ? boot : "-",(unsigned long)eventSeq,
    identityKind ? identityKind : "unknown",(unsigned long)millis(),stage,outcome,
    (unsigned)queueCount,(unsigned)packetId,detail ? detail : "-");
}
static bool saveEventPointers(const char *boot,uint32_t eventSeq,
                              const char *identityKind) {
  event_queue_nvs::Pointers pointers={event_queue_nvs::FORMAT_VERSION,
                                      eventCapacity,eventHead,eventCount};
  bool saved=event_queue_nvs::savePointers(eventStore,pointers);
  eventDiag("nvs_pointer_state",saved ? "write_ok" : "write_failed",
            boot,eventSeq,identityKind,eventCount,"single_blob_commit_head_count_together");
  return saved;
}
static bool saveLostEventCount(const char *boot,uint32_t eventSeq,
                               const char *identityKind) {
  bool saved=eventStore.putUInt("lost",lostEvents)==sizeof(lostEvents);
  eventDiag("nvs_lost_counter",saved ? "write_ok" : "write_failed",boot,
            eventSeq,identityKind,eventCount,"individual_commit");
  return saved;
}
static event_queue_nvs::ReplaceResult replaceQueueVictim(uint8_t slot,
                                                          const String &oldData,
                                                          const String &newData) {
  String key=eventKey(slot);
  event_queue_nvs::ReplaceResult result=event_queue_nvs::replaceRecord(
      eventStore,key.c_str(),oldData,newData);
  char newBoot[32]="-"; uint32_t newSeq=0;
  bool newIdentityValid=parseEventIdentity(newData.c_str(),newBoot,sizeof(newBoot),newSeq);
  const char *outcome=result==event_queue_nvs::ReplaceResult::REPLACED ? "replaced" :
      result==event_queue_nvs::ReplaceResult::FAILED_OLD_INTACT ? "write_failed_old_intact" :
      result==event_queue_nvs::ReplaceResult::FAILED_RESTORED ? "write_failed_old_restored" :
      "write_failed_slot_corrupted";
  char detail[64];
  snprintf(detail,sizeof(detail),"slot=%s;one_key_atomic_replace",key.c_str());
  eventDiag("nvs_replace",outcome,newBoot,newSeq,
            newIdentityValid ? "stored_event" : "unparsed_event",
            eventCount,detail);
  if(result==event_queue_nvs::ReplaceResult::FAILED_CORRUPTED)
    eventQueueReady=false;
  return result;
}
static void queueEvent(const String &data) {
  char eventBoot[32]="-";
  uint32_t eventSeq=0;
  bool identityValid=parseEventIdentity(data.c_str(),eventBoot,sizeof(eventBoot),eventSeq);
  const char *identityKind=identityValid ? "stored_event" : "unparsed_event";
  if(!eventQueueReady) {
    eventDiag("enqueue","queue_recovery_blocked",eventBoot,eventSeq,
              identityKind,eventCount,"persistent_queue_transaction_incomplete");
    return;
  }
  if(!eventCapacity || eventCapacity>EVENT_MAX_CAPACITY) {
    eventQueueReady=false;
    eventDiag("enqueue","queue_capacity_invalid",eventBoot,eventSeq,
              identityKind,eventCount,"persistent_queue_disabled");
    return;
  }
  if(eventCount>=eventCapacity) {
    int priorities[EVENT_MAX_CAPACITY];
    for(uint8_t i=0;i<eventCount;i++) {
      String queued=eventStore.getString(eventKey((eventHead+i)%eventCapacity).c_str(),"");
      if(!queued.length() && i==0 && eventRemoveRetrySlot==eventHead)
        queued=eventRemoveRetryPayload;
      priorities[i]=eventPriority(queued);
    }
    int victim=event_queue_policy::lowestStrictlyLowerVictim(priorities,eventCount,eventPriority(data));
    if(victim<0) {
      ++lostEvents;
      bool lostSaved=saveLostEventCount(eventBoot,eventSeq,identityKind);
      eventDiag("enqueue","queue_full_drop",eventBoot,eventSeq,identityKind,
                eventCount,lostSaved ? "lost_counter_saved" : "lost_counter_write_failed");
      return;
    }
    const uint8_t victimSlot=(eventHead+(uint8_t)victim)%eventCapacity;
    String victimKey=eventKey(victimSlot);
    String oldData=eventStore.getString(victimKey.c_str(),"");
    if(!oldData.length() && victim==0 && eventRemoveRetrySlot==victimSlot)
      oldData=eventRemoveRetryPayload;
    if(!oldData.length()) {
      eventQueueReady=false;
      eventDiag("enqueue","full_queue_victim_unreadable",eventBoot,eventSeq,
                identityKind,eventCount,"queue_blocked_without_modifying_victim");
      return;
    }
    event_queue_nvs::ReplaceResult replacement=replaceQueueVictim(
        victimSlot,oldData,data);
    if(replacement!=event_queue_nvs::ReplaceResult::REPLACED) {
      ++lostEvents;
      bool lostSaved=saveLostEventCount(eventBoot,eventSeq,identityKind);
      eventDiag("enqueue","full_queue_replace_failed",eventBoot,eventSeq,
                identityKind,eventCount,lostSaved ? "incoming_event_dropped" :
                  "incoming_event_and_lost_counter_write_failed");
      return;
    }
    if(victimSlot==eventRemoveRetrySlot) {
      eventRemoveRetryPayload="";
      eventRemoveRetrySlot=0xFF;
    }
    if(victim==0) {
      if(eventPublishQueue) xQueueReset(eventPublishQueue);
      eventInFlight=0; eventPublishPending=false; activeEventToken=0;
    }
    ++lostEvents;
    bool lostSaved=saveLostEventCount(eventBoot,eventSeq,identityKind);
    eventDiag("enqueue","queue_full_replaced_lower_priority",eventBoot,eventSeq,
              identityKind,eventCount,
              lostSaved ? "lost_counter_saved" : "lost_counter_write_failed");
    return;
  }
  uint8_t slot=(eventHead+eventCount)%eventCapacity;
  String key=eventKey(slot);
  if(!eventStore.putString(key.c_str(),data)) {
    ++lostEvents;
    bool lostSaved=saveLostEventCount(eventBoot,eventSeq,identityKind);
    bool pointersSaved=saveEventPointers(eventBoot,eventSeq,identityKind);
    eventDiag("enqueue","nvs_event_write_failed",eventBoot,eventSeq,identityKind,
              eventCount,!lostSaved ? "lost_counter_write_failed" :
                pointersSaved ? "pointers_saved" : "pointer_write_failed");
    return;
  }
  eventCount++;
  bool pointersSaved=saveEventPointers(eventBoot,eventSeq,identityKind);
  eventDiag("enqueue",pointersSaved ? "stored" : "stored_pointer_write_failed",
            eventBoot,eventSeq,identityKind,eventCount,
            identityValid ? "nvs_event_slot_written" : "identity_parse_failed");
#if NVS_RUNTIME_TEST
  if(pointersSaved && identityValid)
    printNvsRuntimeTestEventRecord(DEVICE_ID,eventBoot,eventSeq,data.c_str());
#endif
}
static void clearActiveEvent() {
  eventInFlight=0; eventSentAt=0; eventPublishPending=false; activeEventToken=0;
}
static void pumpEvents(uint32_t now) {
  static bool publishQueueBusyReported=false;
  if(!mqttTaskReady || !eventQueueReady) return;
  // Process app ACKs first. A valid app commit ACK is the only signal that
  // removes an event from NVS; broker PUBACK is never treated as app commit.
  CommitAck ack{};
  while(xQueueReceive(commitAckQueue,&ack,0)==pdTRUE) {
    eventDiag("app_ack","received_by_main",ack.bootId,ack.seq,"app_ack",
              eventCount,"commit_ack_queue");
    if(!eventCount) {
      eventDiag("app_ack","rejected_no_pending_event",ack.bootId,ack.seq,
                "app_ack",eventCount);
      continue;
    }
    String current=eventStore.getString(eventKey(eventHead).c_str(),"");
    if(!current.length() && eventRemoveRetrySlot==eventHead)
      current=eventRemoveRetryPayload;
    if(eventMatches(current.c_str(),ack.bootId,ack.seq)) {
      uint8_t removedSlot=eventHead;
      String key=eventKey(removedSlot);
      if(!eventStore.isKey(key.c_str())) {
        size_t restored=eventStore.putString(key.c_str(),current);
        eventDiag("nvs_restore",restored ? "ok" : "failed",ack.bootId,ack.seq,
                  "app_ack",eventCount,"retry_after_failed_remove");
        if(!restored) {
          eventRemoveRetryPayload=current;
          eventRemoveRetrySlot=removedSlot;
          clearActiveEvent();
          eventLastAttemptAt=now;
          eventDiag("app_ack","matched_restore_failed_retained",ack.bootId,ack.seq,
                    "app_ack",eventCount,"head_count_unchanged");
          return;
        }
      }
      bool removed=eventStore.remove(key.c_str());
      char detail[48];
      snprintf(detail,sizeof(detail),"slot=%s",key.c_str());
      eventDiag("nvs_remove",removed ? "ok" : "failed",ack.bootId,ack.seq,
                "app_ack",eventCount,detail);
      if(!removed) {
        eventRemoveRetryPayload=current;
        eventRemoveRetrySlot=removedSlot;
        clearActiveEvent();
        eventLastAttemptAt=now;
        eventDiag("app_ack","matched_remove_failed_retained",ack.bootId,ack.seq,
                  "app_ack",eventCount,"head_count_unchanged;retry_armed");
        return;
      }
      eventRemoveRetryPayload="";
      eventRemoveRetrySlot=0xFF;
      eventHead=(eventHead+1)%eventCapacity; eventCount--;
      bool pointersSaved=saveEventPointers(ack.bootId,ack.seq,"app_ack");
      clearActiveEvent();
      eventDiag("app_ack","matched_remove_ok",
                ack.bootId,ack.seq,"app_ack",eventCount,
                pointersSaved ? "pointers_saved" : "pointer_write_failed");
    } else {
      char headBoot[32]="-"; uint32_t headSeq=0;
      bool headIdentityValid=parseEventIdentity(current.c_str(),headBoot,sizeof(headBoot),headSeq);
      char detail[80];
      snprintf(detail,sizeof(detail),"head_boot_id=%s head_seq=%lu",
               headIdentityValid ? headBoot : "-",(unsigned long)headSeq);
      eventDiag("app_ack","rejected_head_mismatch",ack.bootId,ack.seq,
                "app_ack",eventCount,detail);
    }
  }
  if(!eventCount) return;
  // A result can arrive after a newer app ACK because they use independent
  // fixed queues. Bind every result to the persistent head identity and token.
  PublishResult result{};
  while(xQueueReceive(publishResultQueue,&result,0)==pdTRUE) {
    if(result.kind!=PublishKind::EVENT) continue;
    if(result.token!=activeEventToken || !eventCount) {
      eventDiag("publish_result","stale_token_or_no_head",result.bootId,result.seq,
                "stored_event",eventCount,"result_ignored",result.packetId);
      continue;
    }
    String current=eventStore.getString(eventKey(eventHead).c_str(),"");
    if(!eventMatches(current.c_str(),result.bootId,result.seq)) {
      eventDiag("publish_result","rejected_head_mismatch",result.bootId,result.seq,
                "stored_event",eventCount,"result_ignored",result.packetId);
      continue;
    }
    eventPublishPending=false;
    eventDiag("publish_result",result.accepted ? "local_write_accepted" : "local_write_failed",
              result.bootId,result.seq,"stored_event",eventCount,
              "not_broker_puback_or_app_commit_ack",result.packetId);
    if(result.accepted) {
      eventInFlight=result.packetId;
      eventSentAt=now;
    } else {
      eventInFlight=0;
      eventLastAttemptAt=now;
    }
  }
  bool connected=(xEventGroupGetBits(mqttEventGroup)&MQTT_CONNECTED_BIT)!=0;
  if(!connected) {
    eventInFlight=0;
    return;
  }
  if(eventInFlight && now-eventSentAt<5000) return;
  if(eventPublishPending || now-eventLastAttemptAt<250) return;
  String data=eventStore.getString(eventKey(eventHead).c_str(),"");
  if(!data.length() && eventRemoveRetrySlot==eventHead)
    data=eventRemoveRetryPayload;
  if(!data.length()) {
    char headBoot[32]="-"; uint32_t headSeq=0;
    bool headIdentityValid=parseEventIdentity(data.c_str(),headBoot,sizeof(headBoot),headSeq);
    eventHead=(eventHead+1)%eventCapacity; eventCount--;
    bool pointersSaved=saveEventPointers(headBoot,headSeq,
                                          headIdentityValid ? "stored_event" : "unparsed_event");
    clearActiveEvent();
    eventDiag("queue_replay","corrupt_head_skipped",headBoot,headSeq,
              headIdentityValid ? "stored_event" : "unparsed_event",eventCount,
              pointersSaved ? "pointers_saved" : "pointer_write_failed");
    return;
  }
  PublishRequest request{};
  request.token=nextPublishToken++;
  if(!request.token) request.token=nextPublishToken++;
  request.kind=PublishKind::EVENT;
  const bool identityValid=parseEventIdentity(data.c_str(),request.bootId,
                                               sizeof(request.bootId),request.seq);
  const bool requestCopied=identityValid &&
      copyCString(request.topic,sizeof(request.topic),(prefix+"event").c_str()) &&
      copyCString(request.payload,sizeof(request.payload),data.c_str());
  if(!requestCopied) {
    const uint8_t invalidSlot=eventHead;
    const char *identityKind=identityValid ? "stored_event" : "unparsed_event";
    event_queue_nvs::Pointers queueState={event_queue_nvs::FORMAT_VERSION,
                                           eventCapacity,eventHead,eventCount};
    event_queue_nvs::DiscardHeadResult discarded=event_queue_nvs::discardHead(
        eventStore,queueState,eventKey(invalidSlot).c_str());
    if(discarded==event_queue_nvs::DiscardHeadResult::REMOVE_FAILED_RETAINED) {
      eventDiag("queue_replay","invalid_head_remove_failed_retry",
                request.bootId,request.seq,identityKind,eventCount,
                "nvs_key_retained_head_unchanged");
      eventLastAttemptAt=now;
      return;
    }
    if(discarded==event_queue_nvs::DiscardHeadResult::NO_PENDING_EVENT) {
      eventDiag("queue_replay","invalid_head_discard_rejected",
                request.bootId,request.seq,identityKind,eventCount,
                "queue_state_invalid_or_empty");
      eventLastAttemptAt=now;
      return;
    }
    eventHead=queueState.head;
    eventCount=queueState.count;
    ++lostEvents;
    const bool lostSaved=saveLostEventCount(request.bootId,request.seq,identityKind);
    clearActiveEvent();
    char detail[72];
    snprintf(detail,sizeof(detail),"slot=%s;invalid_payload",
             eventKey(invalidSlot).c_str());
    eventDiag("nvs_remove","invalid_head_removed",request.bootId,request.seq,
              identityKind,eventCount,detail);
    eventDiag("queue_replay",
              discarded==event_queue_nvs::DiscardHeadResult::REMOVED_POINTERS_SAVED
                  ? "invalid_head_discarded" : "invalid_head_discarded_pointer_retry",
              request.bootId,request.seq,identityKind,eventCount,
              lostSaved ? "remaining_events_unblocked" :
                          "remaining_events_unblocked_lost_counter_write_failed");
    return;
  }
  request.queueCount=eventCount;
  if(xQueueSend(eventPublishQueue,&request,0)==pdTRUE) {
    activeEventToken=request.token;
    eventPublishPending=true;
    eventLastAttemptAt=now;
    publishQueueBusyReported=false;
  } else if(!publishQueueBusyReported) {
    eventDiag("publish_attempt","request_queue_busy",request.bootId,request.seq,
              "stored_event",eventCount,"publish_request_not_queued");
    publishQueueBusyReported=true;
  }
}
static void localAlarm(AlarmState s,uint32_t now) {
  digitalWrite(GREEN,s==AlarmState::SAFE);
  digitalWrite(AMBER,s==AlarmState::WARMUP || s==AlarmState::WATCH || s==AlarmState::WARNING);
  digitalWrite(RED,s==AlarmState::DANGER || s==AlarmState::FAULT);
  bool sound=s==AlarmState::DANGER || s==AlarmState::FAULT || s==AlarmState::WARNING;
  if(s!=AlarmState::DANGER && now<muteUntil) sound=false;
  bool pulse=sound && (s==AlarmState::DANGER || ((now/350)%2==0));
#if SIMULATION
  static bool toneActive=false;
  if(pulse && !toneActive) { tone(BUZZER,2400); toneActive=true; }
  else if(!pulse && toneActive) { noTone(BUZZER); toneActive=false; }
#else
  digitalWrite(BUZZER,pulse);
#endif
}
static void mqttWorker(void *) {
  WiFi.mode(WIFI_STA);
  uint32_t lastWifiAttempt=0,lastConnectAttempt=0;
#if MQTT_TLS
  bool tlsClockSyncRequested=false;
  uint32_t lastTlsClockLog=0;
#endif
  PublishRequest pendingRequest{};
  bool haveRequest=false;
  PublishResult pendingResult{};
  bool haveResult=false;
  CommitAck pendingAck{};
  bool haveAck=false;
  for(;;) {
    uint32_t now=millis();
    bool wifiReady=WiFi.status()==WL_CONNECTED;
#if MQTT_TLS
    if(wifiReady && !tlsClockSyncRequested) {
      configTime(0,0,"pool.ntp.org","time.cloudflare.com");
      tlsClockSyncRequested=true;
      Serial.println("TLS certificate clock synchronization requested");
    }
    bool tlsClockReady=time(nullptr)>=1735689600; // 2025-01-01 UTC; TLS validation remains enabled.
#endif
    if(haveResult && xQueueSend(publishResultQueue,&pendingResult,0)==pdTRUE) haveResult=false;
    if(haveAck && xQueueSend(commitAckQueue,&pendingAck,0)==pdTRUE) haveAck=false;

    if(!wifiReady) {
      if(mqttEventGroup) xEventGroupClearBits(mqttEventGroup,MQTT_CONNECTED_BIT);
      if(now-lastWifiAttempt>=5000) {
        lastWifiAttempt=now;
        WiFi.begin(WIFI_SSID,WIFI_PASSWORD);
      }
    } else if(!mqtt.connected()) {
      if(mqttEventGroup) xEventGroupClearBits(mqttEventGroup,MQTT_CONNECTED_BIT);
#if MQTT_TLS
      if(!tlsClockReady) {
        if(now-lastTlsClockLog>=15000) {
          lastTlsClockLog=now;
          Serial.println("TLS MQTT waiting for a synchronized system clock");
        }
      } else
#endif
      if(now-lastConnectAttempt>=3000) {
        lastConnectAttempt=now;
        String will=String("{\"device_id\":\"")+DEVICE_ID+"\",\"online\":false}";
        if(mqtt.connect(MQTT_HOST,MQTT_PORT,DEVICE_ID,MQTT_USER,MQTT_PASSWORD,
                        (prefix+"availability").c_str(),will.c_str())) {
          mqtt.subscribe((prefix+"ack").c_str());
          String online=String("{\"device_id\":\"")+DEVICE_ID+"\",\"online\":true}";
          mqtt.publish((prefix+"availability").c_str(),online.c_str(),true);
          if(mqttEventGroup) xEventGroupSetBits(mqttEventGroup,MQTT_CONNECTED_BIT);
          Serial.printf("MQTT connected: %s:%d\n",MQTT_HOST,MQTT_PORT);
        } else {
#if !NVS_RUNTIME_TEST
          Serial.println("MQTT connect failed: check gateway, address and credentials");
#endif
        }
      }
    }

    bool mqttReady=wifiReady && mqtt.connected();
    if(mqttReady) {
      mqtt.loop();
      (void)mqtt.takeAck(); // broker PUBACK is transport feedback, not app commit
      if(!haveAck) haveAck=mqtt.takeCommitAck(pendingAck.bootId,sizeof(pendingAck.bootId),pendingAck.seq);
    }

    if(!haveRequest) haveRequest=xQueueReceive(eventPublishQueue,&pendingRequest,0)==pdTRUE;
    if(haveRequest && mqttReady && !haveResult) {
      pendingResult={};
      pendingResult.token=pendingRequest.token;
      pendingResult.kind=pendingRequest.kind;
      copyCString(pendingResult.bootId,sizeof(pendingResult.bootId),pendingRequest.bootId);
      pendingResult.seq=pendingRequest.seq;
      if(pendingRequest.kind==PublishKind::EVENT) {
        eventDiag("publish_attempt","mqtt_qos1_local_send",pendingRequest.bootId,
                  pendingRequest.seq,"stored_event",pendingRequest.queueCount,
                  "result_is_not_broker_or_app_ack");
        uint16_t packetId=mqtt.publishQos1(pendingRequest.topic,pendingRequest.payload);
        pendingResult.accepted=packetId!=0;
        pendingResult.packetId=packetId;
      } else {
        pendingResult.accepted=mqtt.publish(pendingRequest.topic,pendingRequest.payload,false);
      }
      haveResult=true;
      haveRequest=false;
    }

    PublishRequest telemetry{};
    if(mqttReady && xQueueReceive(telemetryPublishQueue,&telemetry,0)==pdTRUE)
      mqtt.publish(telemetry.topic,telemetry.payload,false);
    vTaskDelay(pdMS_TO_TICKS(10));
  }
}
static void readInputs() {
#if SIMULATION
  int lpg=analogRead(LPG),co=analogRead(CO),t=analogRead(TEMP),h=analogRead(HUM),noise=analogRead(INTERFERENCE);
#if NVS_RUNTIME_TEST
  if(nvsTestLpgRaw>=0) lpg=nvsTestLpgRaw;
#endif
  lpgTruth=100.0f*lpg/4095.0f;
  interferencePct=30.0f*(noise/4095.0f-0.5f);
  if(fabsf(interferencePct)<1.0f) interferencePct=0;
  coSignal=100.0f*co/4095.0f;
  tempC=5.0f+45.0f*t/4095.0f;
  humidity=20.0f+70.0f*h/4095.0f;
  environmentValid=t>=0 && t<=4095 && h>=0 && h<=4095;
  environmentBias=constrain((tempC-25.0f)*0.12f+(humidity-55.0f)*0.10f,-8.0f,8.0f);
  lpgSignal=constrain(lpgTruth+interferencePct+environmentBias,0.0f,100.0f);
  lpgValid=digitalRead(LPG_FAULT)==HIGH && lpg>=0 && lpg<=4095;
  coValid=digitalRead(CO_FAULT)==HIGH && co>=0 && co<=4095;
#if NVS_RUNTIME_TEST
  // Test-build-only fault injection follows the same invalid-sample path as a
  // failed physical read. Production and Wokwi node targets use their inputs.
  if(nvsTestLpgInvalid) lpgValid=false;
  if(nvsTestCoInvalid) coValid=false;
#endif
#elif HARDWARE_N8R8 && N8R8_SENSOR_CONFIGURED
  const uint32_t sampledAtMs = millis();
  const bool environmentFresh =
      sensor_driver_checks::isUsableEnvironmentSample(environmentHardwareRaw) &&
      static_cast<uint32_t>(sampledAtMs - environmentHardwareRaw.sampledAtMs) <=
          N8R8_MAX_DATA_AGE_MS;
  environmentValid = hardwareBusReady && environmentFresh;
  tempC = environmentValid ? environmentHardwareRaw.temperatureC : 0.0f;
  humidity = environmentValid ? environmentHardwareRaw.relativeHumidityPct : 0.0f;

  const bool lpgFresh = sensor_driver_checks::isUsableRawGasSample(lpgHardwareRaw) &&
      static_cast<uint32_t>(sampledAtMs - lpgHardwareRaw.sampledAtMs) <=
          N8R8_MAX_DATA_AGE_MS;
  const bool coFresh = sensor_driver_checks::isUsableRawGasSample(coHardwareRaw) &&
      static_cast<uint32_t>(sampledAtMs - coHardwareRaw.sampledAtMs) <=
          N8R8_MAX_DATA_AGE_MS;

  float transformedLpg = 0.0f;
  float transformedCo = 0.0f;
  const bool lpgCalibrationReady = lpgCalibration.usable() && lpgSignalTransform;
  const bool coCalibrationReady = coCalibration.usable() && coSignalTransform;
  lpgValid = hardwareBusReady && lpgFresh && lpgCalibrationReady &&
      lpgSignalTransform && lpgSignalTransform->toRelativeSignal(
          lpgHardwareRaw.rawCounts, lpgCalibration, transformedLpg) &&
      isfinite(transformedLpg) && transformedLpg >= 0.0f && transformedLpg <= 100.0f;
  coValid = hardwareBusReady && coFresh && coCalibrationReady &&
      coSignalTransform && coSignalTransform->toRelativeSignal(
          coHardwareRaw.rawCounts, coCalibration, transformedCo) &&
      isfinite(transformedCo) && transformedCo >= 0.0f && transformedCo <= 100.0f;
  lpgSignal = lpgValid ? transformedLpg : 0.0f;
  coSignal = coValid ? transformedCo : 0.0f;
  calibrationRequired = !lpgCalibrationReady || !coCalibrationReady;
#else
  // The hardware target deliberately stays invalid until the actual ADC
  // front-end settings and reviewed gas calibration transforms are supplied.
  // No Wokwi analog pins, heater outputs, or arbitrary relative thresholds are
  // used as a substitute for physical sensor acquisition.
  lpgSignal = 0.0f;
  coSignal = 0.0f;
  tempC = 0.0f;
  humidity = 0.0f;
  environmentValid = false;
  lpgValid = false;
  coValid = false;
  calibrationRequired = true;
#endif
}
#if HARDWARE_N8R8 && N8R8_SENSOR_CONFIGURED
static void pollHardwareSensors(uint32_t nowMs) {
  if (!hardwareBusReady) return;

  const RawSensorSample lpgSample = lpgHardwareAdc.sample(nowMs);
  if (lpgSample.status != SensorReadStatus::NOT_READY)
    lpgHardwareRaw = lpgSample;

  const RawSensorSample coSample = coHardwareAdc.sample(nowMs);
  if (coSample.status != SensorReadStatus::NOT_READY)
    coHardwareRaw = coSample;

  const EnvironmentSample environment =
      environmentHardwareSensor.sample(nowMs);
  if (environment.status != SensorReadStatus::NOT_READY)
    environmentHardwareRaw = environment;
}
#endif
static String payload(const char *kind,uint32_t now,const char *changedGas="NONE",const char *action="") {
  char buf[768];
#if SIMULATION
  const char *lpgTruthFlag = digitalRead(LPG_TRUTH)==HIGH ? "true" : "false";
  const char *coTruthFlag = digitalRead(CO_TRUTH)==HIGH ? "true" : "false";
  const char *valveFlag = digitalRead(VALVE)==HIGH ? "true" : "false";
  const char *stoveFlag = digitalRead(STOVE)==HIGH ? "true" : "false";
  const char *simulatedFlag = "true";
#else
  const char *lpgTruthFlag = "false";
  const char *coTruthFlag = "false";
  const char *valveFlag = "false";
  const char *stoveFlag = "false";
  const char *simulatedFlag = "false";
#endif
#if HARDWARE_N8R8
  char hardwareStatusFields[80];
  snprintf(hardwareStatusFields, sizeof(hardwareStatusFields),
           ",\"calibration_required\":%s,\"environment_valid\":%s",
           calibrationRequired ? "true" : "false",
           environmentValid ? "true" : "false");
#else
  const char *hardwareStatusFields = "";
#endif
  int written=snprintf(buf,sizeof(buf),"{\"schema\":2,\"kind\":\"%s\",\"home_id\":\"%s\",\"device_id\":\"%s\",\"boot_id\":\"%s\",\"seq\":%lu,\"uptime_ms\":%lu,\"state\":\"%s\",\"changed_gas\":\"%s\",\"action\":\"%s\",\"lpg\":{\"state\":\"%s\",\"signal_pct\":%.1f,\"baseline_pct\":%.1f,\"rise_pct\":%.1f,\"valid\":%s},\"co\":{\"state\":\"%s\",\"signal_pct\":%.1f,\"baseline_pct\":%.1f,\"rise_pct\":%.1f,\"valid\":%s},\"temperature_c\":%.1f,\"humidity_pct\":%.1f,\"lpg_truth_sim_pct\":%.1f,\"interference_sim_pct\":%.1f,\"environment_bias_sim_pct\":%.1f,\"lpg_leak_truth_sim\":%s,\"co_hazard_truth_sim\":%s,\"valve_open_sim\":%s,\"stove_on_sim\":%s,\"inspection_required\":%s,\"lost_events\":%lu%s,\"simulated\":%s}",
    kind,HOME_ID,DEVICE_ID,bootId.c_str(),(unsigned long)++seq,(unsigned long)now,stateName(overallState()),changedGas,action,
    stateName(lpgDetector.state()),lpgSignal,lpgDetector.baseline(),lpgDetector.rise(),lpgValid?"true":"false",
    stateName(coDetector.state()),coSignal,coDetector.baseline(),coDetector.rise(),coValid?"true":"false",
    tempC,humidity,lpgTruth,interferencePct,environmentBias,lpgTruthFlag,coTruthFlag,valveFlag,stoveFlag,
    lpgDetector.inspectionRequired()||coDetector.inspectionRequired()?"true":"false",(unsigned long)lostEvents,hardwareStatusFields,simulatedFlag);
  if(written<0 || written>=(int)sizeof(buf)) return String();
  return String(buf);
}
void setup() {
  Serial.begin(115200);
#if NVS_RUNTIME_TEST
  if(nvsTestRtcMagic!=NVS_TEST_RTC_MAGIC) {
    nvsTestRtcMagic=NVS_TEST_RTC_MAGIC;
    nvsTestLpgRaw=-1;
  }
#endif
  pinMode(GREEN,OUTPUT); pinMode(AMBER,OUTPUT); pinMode(RED,OUTPUT); pinMode(BUZZER,OUTPUT);
#if SIMULATION
  pinMode(LPG_FAULT,INPUT_PULLUP); pinMode(CO_FAULT,INPUT_PULLUP); pinMode(ACK,INPUT_PULLUP); pinMode(INSPECT,INPUT_PULLUP);
  pinMode(VALVE,INPUT); pinMode(STOVE,INPUT); pinMode(LPG_TRUTH,INPUT); pinMode(CO_TRUTH,INPUT);
  analogReadResolution(12);
  // Tone.cpp defaults to LEDC channel 0, timer 0, and configures that timer
  // only after attaching the pin. Initialize the matching timer/channel first.
  ledcSetup(0,2400,10);
  ledcAttachPin(BUZZER,0);
#elif HARDWARE_N8R8 && N8R8_SENSOR_CONFIGURED
  // GPIO8/9 are the documented physical I2C pins. They are never used as
  // analog inputs in hardware mode. The caller must supply a verified bus rate.
  hardwareBusReady=hardwareI2cBus.begin(8,9,N8R8_I2C_CLOCK_HZ,
                                        N8R8_I2C_TIMEOUT_MS);
  if(!hardwareBusReady)
    Serial.println("I2C initialization failed; gas inputs remain invalid");
#endif
  prefix=String("home/")+HOME_ID+"/device/"+DEVICE_ID+"/";
  bootId=String((uint32_t)esp_random(),HEX);
  bool eventStoreReady=eventStore.begin("gas-events-v2",false);
  eventQueueReady=eventStoreReady;
  eventDiag("nvs_begin",eventStoreReady ? "ready" : "failed",bootId.c_str(),0,
            "runtime_boot",0,"preferences_namespace");
  event_queue_nvs::Pointers persistedPointers{};
  bool pointerStateLoaded=false;
  bool legacyPointersImported=false;
  if(eventQueueReady) {
    pointerStateLoaded=event_queue_nvs::loadPointersUpTo(
        eventStore,EVENT_MAX_CAPACITY,persistedPointers);
    if(pointerStateLoaded) {
      eventCapacity=persistedPointers.capacity;
      eventHead=persistedPointers.head;
      eventCount=persistedPointers.count;
    } else if(eventStore.isKey(event_queue_nvs::POINTERS_KEY)) {
      // A present but invalid committed state is not equivalent to a legacy
      // store: the split head/count keys may be stale. Preserve every event
      // key and disable queue mutation rather than guessing a recovery state.
      eventQueueReady=false;
      eventDiag("queue_recovery","pointer_state_invalid",bootId.c_str(),0,
                "runtime_boot",0,"all_event_keys_retained_queue_disabled");
    } else {
      bool hasLegacyMetadata=eventStore.isKey("head") ||
                             eventStore.isKey("count") ||
                             eventStore.isKey("cap");
      bool hasEventSlots=false;
      for(uint8_t slot=0;slot<EVENT_MAX_CAPACITY;slot++) {
        String key=eventKey(slot);
        if(eventStore.isKey(key.c_str())) { hasEventSlots=true; break; }
      }
      if(!hasLegacyMetadata && !hasEventSlots) {
        // Fresh namespace: create an empty v1 pointer record using the current
        // default capacity. If persistence fails, keep local alarms active but
        // do not accept events into a queue that cannot survive reboot.
        eventCapacity=EVENT_CAPACITY;
        eventHead=0;
        eventCount=0;
      } else if(hasLegacyMetadata) {
        uint8_t legacyCapacity=eventStore.getUChar("cap",8);
        uint8_t legacyHead=eventStore.getUChar("head",0);
        uint8_t legacyCount=eventStore.getUChar("count",0);
        if(legacyCapacity!=8 && legacyCapacity!=16 && legacyCapacity!=32) {
          eventQueueReady=false;
          eventDiag("queue_recovery","legacy_capacity_invalid",bootId.c_str(),0,
                    "runtime_boot",0,"legacy_keys_retained_queue_disabled");
        } else if(event_queue_nvs::importLegacyPointers(
                      eventStore,legacyCapacity,legacyHead,legacyCount,
                      EVENT_MAX_CAPACITY,persistedPointers)) {
          pointerStateLoaded=true;
          legacyPointersImported=true;
          eventCapacity=persistedPointers.capacity;
          eventHead=persistedPointers.head;
          eventCount=persistedPointers.count;
          eventDiag("queue_recovery","legacy_imported_without_event_move",
                    bootId.c_str(),0,"runtime_boot",eventCount,
                    "qstate_committed_legacy_event_keys_unchanged");
        } else {
          eventQueueReady=false;
          eventDiag("queue_recovery","legacy_import_failed",bootId.c_str(),0,
                    "runtime_boot",0,"legacy_event_keys_retained_queue_disabled");
        }
      } else {
        // Event records without either committed or legacy ring metadata are
        // ambiguous. Retain every key instead of guessing an order or deleting.
        eventQueueReady=false;
        eventDiag("queue_recovery","orphan_event_slots",bootId.c_str(),0,
                  "runtime_boot",0,"event_keys_retained_queue_disabled");
      }
    }
  }
  event_queue_nvs::Pointers recoveredPointers={event_queue_nvs::FORMAT_VERSION,
                                                eventCapacity,eventHead,eventCount};
  bool queueReconciled=false;
  if(eventQueueReady) {
    queueReconciled=event_queue_nvs::reconcile(recoveredPointers,
                                                hasValidStoredEventSlot);
    eventHead=recoveredPointers.head;
    eventCount=recoveredPointers.count;
  }
  if(eventQueueReady && (!pointerStateLoaded || queueReconciled)) {
    bool pointersSaved=saveEventPointers(bootId.c_str(),0,"nvs_boot");
    eventDiag("queue_recovery",queueReconciled ? "slots_reconciled" :
                legacyPointersImported ? "legacy_imported_without_event_move" : "initialized",
              bootId.c_str(),0,"runtime_boot",eventCount,
              pointersSaved ? "qstate_persisted" : "qstate_write_failed");
    if(!pointerStateLoaded && !pointersSaved) {
      eventQueueReady=false;
      eventDiag("queue_recovery","initial_pointer_commit_failed",bootId.c_str(),0,
                "runtime_boot",eventCount,"event_queue_disabled_local_alarm_retained");
    }
  }
  lostEvents=eventStore.getUInt("lost",0);
  if(eventCount) {
    String replayHead=eventStore.getString(eventKey(eventHead).c_str(),"");
    char replayBoot[32]="-"; uint32_t replaySeq=0;
    bool replayIdentityValid=parseEventIdentity(replayHead.c_str(),replayBoot,
                                                 sizeof(replayBoot),replaySeq);
    eventDiag("queue_replay",replayIdentityValid ? "pending" : "head_invalid",
              replayBoot,replaySeq,replayIdentityValid ? "stored_event" : "unparsed_event",
              eventCount,"loaded_from_nvs");
  } else {
    eventDiag("queue_replay","empty","-",0,"none",eventCount,"loaded_from_nvs");
  }
  eventPublishQueue=xQueueCreate(1,sizeof(PublishRequest));
  telemetryPublishQueue=xQueueCreate(1,sizeof(PublishRequest));
  publishResultQueue=xQueueCreate(4,sizeof(PublishResult));
  commitAckQueue=xQueueCreate(4,sizeof(CommitAck));
  mqttEventGroup=xEventGroupCreate();
  localAlarm(AlarmState::WARMUP,0);
  if(eventPublishQueue && telemetryPublishQueue && publishResultQueue && commitAckQueue && mqttEventGroup) {
    mqttTaskReady=xTaskCreatePinnedToCore(mqttWorker,"mqtt-worker",8192,nullptr,1,nullptr,0)==pdPASS;
  }
  if(!mqttTaskReady) Serial.println("MQTT worker unavailable; local detector remains active");
#if SIMULATION
  Serial.printf("%s starting: two independent relative gas signals, not ppm\n",DEVICE_ID);
#else
  Serial.printf("%s starting: hardware target; calibrated gas transform required before detection\n",DEVICE_ID);
#endif
}
void loop() {
  uint32_t now=millis();
#if NVS_RUNTIME_TEST
  pollNvsRuntimeTestSerial();
#endif
#if HARDWARE_N8R8 && N8R8_SENSOR_CONFIGURED
  pollHardwareSensors(now);
#endif
#if SIMULATION
  bool ackPressed=digitalRead(ACK)==LOW;
  if(ackPressed && !priorAck) {
    AlarmState current=overallState();
    if(current==AlarmState::WARNING || current==AlarmState::FAULT || current==AlarmState::DANGER) {
      if(current!=AlarmState::DANGER) muteUntil=now+30000;
      String event=payload("event",now,"NONE",current==AlarmState::DANGER?"ACK_DENIED_DANGER":"MUTE_30S");
      if(event.length()) queueEvent(event);
    }
  }
  priorAck=ackPressed;
  bool inspectPressed=digitalRead(INSPECT)==LOW;
  if(inspectPressed && !priorInspect) {
    bool eligible=lpgDetector.canConfirmInspection(now) || coDetector.canConfirmInspection(now);
    lpgDetector.markInspected(now); coDetector.markInspected(now);
    if(eligible) {
      String event=payload("event",now,"NONE","INSPECTION_CONFIRMED");
      if(event.length()) queueEvent(event);
    }
  }
  priorInspect=inspectPressed;
#endif
  if(now-lastSample>=SAMPLE_INTERVAL_MS) {
    lastSample=now;
    uint32_t sampleNowMs=millis();
    uint32_t acquisitionStart=micros();
    if(lastSampleUs) {
      uint32_t gap=acquisitionStart-lastSampleUs;
      if(gap>maxSampleGapUs) maxSampleGapUs=gap;
      uint32_t periodUs=SAMPLE_INTERVAL_MS*1000UL;
      if(gap>periodUs) missedSamplePeriods+=gap/periodUs-1;
    }
    lastSampleUs=acquisitionStart;
    readInputs();
    AlarmState lpg=lpgDetector.update(lpgSignal,lpgValid,sampleNowMs);
    AlarmState co=coDetector.update(coSignal,coValid,sampleNowMs);
    uint32_t lpgDwellMs=0,coDwellMs=0;
    bool lpgTransitionTimed=lpgDetector.takeTransitionDwellMs(lpgDwellMs);
    bool coTransitionTimed=coDetector.takeTransitionDwellMs(coDwellMs);
    localAlarm(overallState(),sampleNowMs);
    uint32_t alarmLatency=micros()-acquisitionStart;
    if(alarmLatency>maxAcquireToAlarmUs) maxAcquireToAlarmUs=alarmLatency;
    if(lpgTransitionTimed) {
      uint32_t conditionToAlarmUs=lpgDwellMs*1000UL+alarmLatency;
      if(conditionToAlarmUs>maxSampleConditionToAlarmUs) maxSampleConditionToAlarmUs=conditionToAlarmUs;
    }
    if(coTransitionTimed) {
      uint32_t conditionToAlarmUs=coDwellMs*1000UL+alarmLatency;
      if(conditionToAlarmUs>maxSampleConditionToAlarmUs) maxSampleConditionToAlarmUs=conditionToAlarmUs;
    }
    if(lpg!=priorLpg || co!=priorCo) {
      const char *changed=lpg!=priorLpg && co!=priorCo ? "BOTH" : lpg!=priorLpg ? "LPG" : "CO";
      Serial.printf("%s: LPG %s %.1f%%, CO %s %.1f%%\n",DEVICE_ID,stateName(lpg),lpgSignal,stateName(co),coSignal);
      String event=payload("event",sampleNowMs,changed);
      if(event.length()) queueEvent(event);
      priorLpg=lpg; priorCo=co;
    }
  }
  pumpEvents(now);
  if(mqttTaskReady && now-lastPublish>=2000 && (xEventGroupGetBits(mqttEventGroup)&MQTT_CONNECTED_BIT)) {
    lastPublish=now;
    String p=payload("telemetry",now);
    if(p.length()) {
      PublishRequest telemetry{};
      telemetry.kind=PublishKind::TELEMETRY;
      copyCString(telemetry.topic,sizeof(telemetry.topic),(prefix+"telemetry").c_str());
      copyCString(telemetry.payload,sizeof(telemetry.payload),p.c_str());
      xQueueOverwrite(telemetryPublishQueue,&telemetry);
    }
  }
  if(now-lastMetricsPrint>=10000) {
    lastMetricsPrint=now;
    Serial.printf("timing: max sample gap=%lu us, max acquire-to-localAlarm=%lu us, max sampled-condition-to-localAlarm=%lu us, missed periods=%lu\n",
      (unsigned long)maxSampleGapUs,(unsigned long)maxAcquireToAlarmUs,
      (unsigned long)maxSampleConditionToAlarmUs,(unsigned long)missedSamplePeriods);
    maxSampleGapUs=0; maxAcquireToAlarmUs=0; maxSampleConditionToAlarmUs=0; missedSamplePeriods=0;
  }
  delay(10);
}
