#pragma once
#include <WiFiClient.h>
#include <stdint.h>
class MqttLite {
 public:
  bool connect(const char *host, uint16_t port, const char *id, const char *user, const char *pass, const char *willTopic, const char *willPayload);
  bool publish(const char *topic, const char *payload, bool retain=false);
  uint16_t publishQos1(const char *topic, const char *payload);
  bool subscribe(const char *topic);
  uint16_t takeAck() { uint16_t id=lastAck_; lastAck_=0; return id; }
  bool takeCommitAck(char *boot, size_t capacity, uint32_t &seq);
  void loop();
  bool connected() { return client_.connected(); }
 private:
  WiFiClient client_;
  uint32_t lastActivity_=0;
  uint16_t nextId_=1,lastAck_=0;
  uint8_t rx_[256] = {};
  size_t rxLen_=0;
  char ackTopic_[96] = {};
  char ackBoot_[32] = {};
  uint32_t ackSeq_=0;
  bool writePacket(uint8_t header, const uint8_t *body, size_t length);
};
