#include <Arduino.h>
#include <WiFi.h>
#include <Preferences.h>
#include <math.h>
#include "Detector.h"
#include "MqttLite.h"
#include "MqttCredentials.h"
#ifndef WIFI_SSID
#define WIFI_SSID "Wokwi-GUEST"
#endif
#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD ""
#endif
#ifndef MQTT_USER
#define MQTT_USER ""
#endif
static constexpr int LPG=1,CO=2,TEMP=8,HUM=9,INTERFERENCE=10,BUZZER=4,GREEN=5,AMBER=6,RED=7,LPG_FAULT=13,ACK=14,VALVE=15,STOVE=16,CO_FAULT=21,INSPECT=12,LPG_TRUTH=11,CO_TRUTH=17;
static Detector lpgDetector,coDetector;
static MqttLite mqtt;
static Preferences eventStore;
static String prefix,bootId;
static uint32_t lastSample=0,lastPublish=0,lastConnect=0,lastWifi=0,seq=0,muteUntil=0;
static AlarmState priorLpg=AlarmState::WARMUP,priorCo=AlarmState::WARMUP;
static bool priorAck=false,priorInspect=false;
static float lpgSignal=0,coSignal=0,tempC=0,humidity=0;
static float lpgTruth=0,interferencePct=0,environmentBias=0;
static bool lpgValid=false,coValid=false;
static uint8_t eventHead=0,eventCount=0;
// Default ESP32-S3 partition has a 20 KiB NVS area. Keep the queue bounded within it.
static constexpr uint8_t EVENT_CAPACITY=16;
static uint32_t lostEvents=0;
static uint16_t eventInFlight=0;
static uint32_t eventSentAt=0;

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
  if(data.indexOf("\"state\":\"DANGER\"")>=0) return 5;
  if(data.indexOf("\"state\":\"FAULT\"")>=0) return 4;
  if(data.indexOf("\"state\":\"WARNING\"")>=0) return 3;
  if(data.indexOf("\"state\":\"WATCH\"")>=0) return 2;
  return 1;
}
static void saveEventPointers() {
  eventStore.putUChar("head",eventHead);
  eventStore.putUChar("count",eventCount);
}
static void queueEvent(const String &data) {
  if(eventCount>=EVENT_CAPACITY) {
    int victim=-1,lowest=eventPriority(data);
    for(uint8_t i=0;i<eventCount;i++) {
      int priority=eventPriority(eventStore.getString(eventKey((eventHead+i)%EVENT_CAPACITY).c_str(),""));
      if(priority<lowest) { lowest=priority; victim=i; }
    }
    if(victim<0) {
      eventStore.putUInt("lost",++lostEvents);
      Serial.println("Event queue full: new event not stored");
      return;
    }
    for(int i=victim;i<eventCount-1;i++) {
      String next=eventStore.getString(eventKey((eventHead+i+1)%EVENT_CAPACITY).c_str(),"");
      eventStore.putString(eventKey((eventHead+i)%EVENT_CAPACITY).c_str(),next);
    }
    eventStore.remove(eventKey((eventHead+eventCount-1)%EVENT_CAPACITY).c_str());
    eventCount--; eventInFlight=0;
    eventStore.putUInt("lost",++lostEvents);
    Serial.println("Event queue full: lower-priority event replaced");
  }
  uint8_t slot=(eventHead+eventCount)%EVENT_CAPACITY;
  if(!eventStore.putString(eventKey(slot).c_str(),data)) {
    eventStore.putUInt("lost",++lostEvents);
    Serial.println("Event queue write failed");
    saveEventPointers();
    return;
  }
  eventCount++; saveEventPointers();
}
static void pumpEvents(uint32_t now) {
  if(!mqtt.connected()) { eventInFlight=0; return; }
  char acknowledgedBoot[32]; uint32_t acknowledgedSeq=0;
  if(eventCount && mqtt.takeCommitAck(acknowledgedBoot,sizeof(acknowledgedBoot),acknowledgedSeq)) {
    String current=eventStore.getString(eventKey(eventHead).c_str(),"");
    String bootField=String("\"boot_id\":\"")+acknowledgedBoot+"\"";
    String seqField=String("\"seq\":")+String(acknowledgedSeq)+",";
    if(current.indexOf(bootField)>=0 && current.indexOf(seqField)>=0) {
      eventStore.remove(eventKey(eventHead).c_str());
      eventHead=(eventHead+1)%EVENT_CAPACITY; eventCount--; saveEventPointers();
      eventInFlight=0;
    }
  }
  if(eventInFlight && now-eventSentAt<5000) return;
  if(eventCount) {
    String data=eventStore.getString(eventKey(eventHead).c_str(),"");
    if(data.length()) {
      uint16_t id=mqtt.publishQos1((prefix+"event").c_str(),data.c_str());
      if(id) { eventInFlight=id; eventSentAt=now; }
    } else {
      Serial.println("Event queue corrupt entry skipped");
      eventHead=(eventHead+1)%EVENT_CAPACITY; eventCount--; saveEventPointers();
    }
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
static void readInputs() {
  int lpg=analogRead(LPG),co=analogRead(CO),t=analogRead(TEMP),h=analogRead(HUM),noise=analogRead(INTERFERENCE);
  lpgTruth=100.0f*lpg/4095.0f;
  interferencePct=30.0f*(noise/4095.0f-0.5f);
  if(fabsf(interferencePct)<1.0f) interferencePct=0;
  coSignal=100.0f*co/4095.0f;
  tempC=5.0f+45.0f*t/4095.0f;
  humidity=20.0f+70.0f*h/4095.0f;
  environmentBias=constrain((tempC-25.0f)*0.12f+(humidity-55.0f)*0.10f,-8.0f,8.0f);
  lpgSignal=constrain(lpgTruth+interferencePct+environmentBias,0.0f,100.0f);
  lpgValid=digitalRead(LPG_FAULT)==HIGH && lpg>=0 && lpg<=4095;
  coValid=digitalRead(CO_FAULT)==HIGH && co>=0 && co<=4095;
}
static String payload(const char *kind,uint32_t now,const char *changedGas="NONE",const char *action="") {
  char buf[768];
  int written=snprintf(buf,sizeof(buf),"{\"schema\":2,\"kind\":\"%s\",\"home_id\":\"%s\",\"device_id\":\"%s\",\"boot_id\":\"%s\",\"seq\":%lu,\"uptime_ms\":%lu,\"state\":\"%s\",\"changed_gas\":\"%s\",\"action\":\"%s\",\"lpg\":{\"state\":\"%s\",\"signal_pct\":%.1f,\"baseline_pct\":%.1f,\"rise_pct\":%.1f,\"valid\":%s},\"co\":{\"state\":\"%s\",\"signal_pct\":%.1f,\"baseline_pct\":%.1f,\"rise_pct\":%.1f,\"valid\":%s},\"temperature_c\":%.1f,\"humidity_pct\":%.1f,\"lpg_truth_sim_pct\":%.1f,\"interference_sim_pct\":%.1f,\"environment_bias_sim_pct\":%.1f,\"lpg_leak_truth_sim\":%s,\"co_hazard_truth_sim\":%s,\"valve_open_sim\":%s,\"stove_on_sim\":%s,\"inspection_required\":%s,\"lost_events\":%lu,\"simulated\":true}",
    kind,HOME_ID,DEVICE_ID,bootId.c_str(),(unsigned long)++seq,(unsigned long)now,stateName(overallState()),changedGas,action,
    stateName(lpgDetector.state()),lpgSignal,lpgDetector.baseline(),lpgDetector.rise(),lpgValid?"true":"false",
    stateName(coDetector.state()),coSignal,coDetector.baseline(),coDetector.rise(),coValid?"true":"false",
    tempC,humidity,lpgTruth,interferencePct,environmentBias,digitalRead(LPG_TRUTH)==HIGH?"true":"false",digitalRead(CO_TRUTH)==HIGH?"true":"false",digitalRead(VALVE)==HIGH?"true":"false",digitalRead(STOVE)==HIGH?"true":"false",
    lpgDetector.inspectionRequired()||coDetector.inspectionRequired()?"true":"false",(unsigned long)lostEvents);
  if(written<0 || written>=(int)sizeof(buf)) return String();
  return String(buf);
}
void setup() {
  Serial.begin(115200);
  pinMode(GREEN,OUTPUT); pinMode(AMBER,OUTPUT); pinMode(RED,OUTPUT); pinMode(BUZZER,OUTPUT);
  pinMode(LPG_FAULT,INPUT_PULLUP); pinMode(CO_FAULT,INPUT_PULLUP); pinMode(ACK,INPUT_PULLUP); pinMode(INSPECT,INPUT_PULLUP);
  pinMode(VALVE,INPUT); pinMode(STOVE,INPUT); pinMode(LPG_TRUTH,INPUT); pinMode(CO_TRUTH,INPUT);
  analogReadResolution(12);
  prefix=String("home/")+HOME_ID+"/device/"+DEVICE_ID+"/";
  bootId=String((uint32_t)esp_random(),HEX);
  eventStore.begin("gas-events-v2",false);
  if(eventStore.getUChar("cap",8)!=EVENT_CAPACITY) {
    uint8_t oldCap=eventStore.getUChar("cap",8);
    if(oldCap!=8 && oldCap!=32) oldCap=8;
    uint8_t oldHead=eventStore.getUChar("head",0)%oldCap;
    uint8_t oldCount=min(oldCap,eventStore.getUChar("count",0));
    uint8_t kept=min(EVENT_CAPACITY,oldCount);
    String pending[EVENT_CAPACITY];
    for(uint8_t i=0;i<kept;i++) pending[i]=eventStore.getString(eventKey((oldHead+i)%oldCap).c_str(),"");
    for(uint8_t i=0;i<oldCap;i++) eventStore.remove(eventKey(i).c_str());
    for(uint8_t i=0;i<kept;i++) eventStore.putString(eventKey(i).c_str(),pending[i]);
    if(oldCount>kept) eventStore.putUInt("lost",eventStore.getUInt("lost",0)+oldCount-kept);
    eventStore.putUChar("head",0); eventStore.putUChar("count",kept);
    eventStore.putUChar("cap",EVENT_CAPACITY);
  }
  eventHead=eventStore.getUChar("head",0)%EVENT_CAPACITY;
  eventCount=min(EVENT_CAPACITY,eventStore.getUChar("count",0));
  lostEvents=eventStore.getUInt("lost",0);
  WiFi.mode(WIFI_STA); localAlarm(AlarmState::WARMUP,0);
  Serial.printf("%s starting: two independent relative gas signals, not ppm\n",DEVICE_ID);
}
void loop() {
  uint32_t now=millis();
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
  if(now-lastSample>=500) {
    lastSample=now; readInputs();
    AlarmState lpg=lpgDetector.update(lpgSignal,lpgValid,now);
    AlarmState co=coDetector.update(coSignal,coValid,now);
    localAlarm(overallState(),now);
    if(lpg!=priorLpg || co!=priorCo) {
      const char *changed=lpg!=priorLpg && co!=priorCo ? "BOTH" : lpg!=priorLpg ? "LPG" : "CO";
      Serial.printf("%s: LPG %s %.1f%%, CO %s %.1f%%\n",DEVICE_ID,stateName(lpg),lpgSignal,stateName(co),coSignal);
      String event=payload("event",now,changed);
      if(event.length()) queueEvent(event);
      priorLpg=lpg; priorCo=co;
    }
  }
  if(WiFi.status()!=WL_CONNECTED && now-lastWifi>5000) {
    lastWifi=now; Serial.println("WiFi connecting..."); WiFi.begin(WIFI_SSID,WIFI_PASSWORD);
  }
  if(WiFi.status()==WL_CONNECTED && !mqtt.connected() && now-lastConnect>3000) {
    lastConnect=now;
    String will=String("{\"device_id\":\"")+DEVICE_ID+"\",\"online\":false}";
    if(mqtt.connect(MQTT_HOST,MQTT_PORT,DEVICE_ID,MQTT_USER,MQTT_PASSWORD,(prefix+"availability").c_str(),will.c_str())) {
      mqtt.subscribe((prefix+"ack").c_str());
      Serial.printf("MQTT connected: %s:%d\n",MQTT_HOST,MQTT_PORT);
      String online=String("{\"device_id\":\"")+DEVICE_ID+"\",\"online\":true}";
      mqtt.publish((prefix+"availability").c_str(),online.c_str(),true);
    } else Serial.println("MQTT connect failed: check gateway, address and credentials");
  }
  mqtt.loop();
  pumpEvents(now);
  if(mqtt.connected() && now-lastPublish>=2000) {
    lastPublish=now; String p=payload("telemetry",now); if(p.length()) mqtt.publish((prefix+"telemetry").c_str(),p.c_str());
  }
  delay(10);
}
