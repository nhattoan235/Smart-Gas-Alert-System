#include "MqttLite.h"
#include "MqttCredentials.h"
#include <Arduino.h>
#include <string.h>
#include <stdlib.h>
#ifndef MQTT_CA_CERT
#define MQTT_CA_CERT ""
#endif
static size_t putString(uint8_t *out,const char *s) { size_t n=strlen(s); out[0]=n>>8; out[1]=n&255; memcpy(out+2,s,n); return n+2; }
bool MqttLite::writePacket(uint8_t header,const uint8_t *body,size_t length) {
  if (!client_.connected()) return false;
  client_.write(header); size_t x=length;
  do { uint8_t b=x%128; x/=128; if(x) b|=128; client_.write(b); } while(x);
  if(length && client_.write(body,length)!=length) return false;
  lastActivity_=millis(); return true;
}
bool MqttLite::connect(const char *host,uint16_t port,const char *id,const char *user,const char *pass,const char *willTopic,const char *willPayload) {
  if(!host || !*host) return false;
  size_t payloadSize=16+strlen(id)+strlen(willTopic)+strlen(willPayload);
  if(user && *user) payloadSize+=2+strlen(user);
  if(pass && *pass) payloadSize+=2+strlen(pass);
  if(payloadSize>512) return false;
  client_.stop(); rxLen_=0; ackHead_=ackCount_=0; client_.setTimeout(2);
#if MQTT_TLS
  // TLS mode fails closed: a missing CA is a configuration error, never a
  // reason to fall back to plaintext or disable certificate verification.
  if(!MQTT_CA_CERT[0]) return false;
  client_.setCACert(MQTT_CA_CERT);
#endif
  if(!client_.connect(host,port,750)) return false;
  uint8_t body[512]; size_t i=0;
  i+=putString(body+i,"MQTT"); body[i++]=4;
  uint8_t flags=0x26; // clean session and retained will
  if(user && *user) flags|=0x80;
  if(pass && *pass) flags|=0x40;
  body[i++]=flags; body[i++]=0; body[i++]=30;
  i+=putString(body+i,id); i+=putString(body+i,willTopic); i+=putString(body+i,willPayload);
  if(user && *user) i+=putString(body+i,user);
  if(pass && *pass) i+=putString(body+i,pass);
  if(i>sizeof(body) || !writePacket(0x10,body,i)) { client_.stop(); return false; }
  uint32_t start=millis(); while(client_.available()<4 && millis()-start<750) delay(10);
  if(client_.available()<4 || client_.read()!=0x20 || client_.read()!=2 || client_.read()!=0 || client_.read()!=0) { client_.stop(); return false; }
  return true;
}
bool MqttLite::publish(const char *topic,const char *payload,bool retain) {
  size_t a=strlen(topic),b=strlen(payload); if(a+b+2>1024) return false;
  uint8_t body[1024]; size_t i=putString(body,topic); memcpy(body+i,payload,b);
  return writePacket(retain?0x31:0x30,body,i+b);
}
uint16_t MqttLite::publishQos1(const char *topic,const char *payload) {
  size_t a=strlen(topic),b=strlen(payload); if(a+b+4>1024) return 0;
  uint8_t body[1024]; size_t i=putString(body,topic);
  uint16_t id=nextId_++; if(nextId_==0) nextId_=1;
  body[i++]=id>>8; body[i++]=id&255; memcpy(body+i,payload,b);
  return writePacket(0x32,body,i+b)?id:0;
}
bool MqttLite::subscribe(const char *topic) {
  size_t n=strlen(topic);
  if(n>=sizeof(ackTopic_) || n+5>128) return false;
  memcpy(ackTopic_,topic,n+1);
  uint8_t body[128];
  uint16_t id=nextId_++; if(nextId_==0) nextId_=1;
  body[0]=id>>8; body[1]=id&255;
  size_t i=2+putString(body+2,topic);
  body[i++]=0;
  return writePacket(0x82,body,i);
}
bool MqttLite::takeCommitAck(char *boot,size_t capacity,uint32_t &seq) {
  if(!ackCount_ || !capacity) return false;
  strncpy(boot,ackBoot_[ackHead_],capacity-1); boot[capacity-1]=0;
  seq=ackSeq_[ackHead_];
  ackSeq_[ackHead_]=0;
  ackHead_=(ackHead_+1)%COMMIT_ACK_CAPACITY;
  ackCount_--;
  return true;
}
void MqttLite::loop() {
  if(!client_.connected()) return;
  while(client_.available() && rxLen_<sizeof(rx_)) rx_[rxLen_++]=client_.read();
  size_t consumed=0;
  while(rxLen_-consumed>=2) {
    size_t p=consumed+1,remain=0,multiplier=1;
    uint8_t b;
    do {
      if(p>=rxLen_ || p-consumed>4) goto done;
      b=rx_[p++]; remain+=(b&127)*multiplier; multiplier*=128;
    } while(b&128);
    if(p+remain>sizeof(rx_)) { client_.stop(); rxLen_=0; return; }
    if(p+remain>rxLen_) break;
    uint8_t kind=rx_[consumed]&0xF0;
    if(kind==0x40 && remain==2) lastAck_=(rx_[p]<<8)|rx_[p+1];
    if(kind==0x30 && remain>=2) {
      size_t topicLen=(rx_[p]<<8)|rx_[p+1];
      if(topicLen+2<=remain && topicLen==strlen(ackTopic_) && memcmp(rx_+p+2,ackTopic_,topicLen)==0) {
        size_t payloadLen=remain-topicLen-2;
        if(payloadLen<48 && payloadLen>2) {
          char value[48]; memcpy(value,rx_+p+2+topicLen,payloadLen); value[payloadLen]=0;
          char *separator=strchr(value,':');
          if(separator && separator-value>0 && separator-value<(int)sizeof(ackBoot_[0])) {
            *separator=0;
            char *end=nullptr; unsigned long parsed=strtoul(separator+1,&end,10);
            if(end && *end==0 && parsed>0 && ackCount_<COMMIT_ACK_CAPACITY) {
              uint8_t slot=(ackHead_+ackCount_)%COMMIT_ACK_CAPACITY;
              strcpy(ackBoot_[slot],value); ackSeq_[slot]=(uint32_t)parsed; ackCount_++;
            }
          }
        }
      }
    }
    consumed=p+remain;
  }
done:
  if(consumed) { memmove(rx_,rx_+consumed,rxLen_-consumed); rxLen_-=consumed; }
  if(rxLen_==sizeof(rx_)) { client_.stop(); rxLen_=0; }
  if(millis()-lastActivity_>12000) writePacket(0xC0,nullptr,0);
}
