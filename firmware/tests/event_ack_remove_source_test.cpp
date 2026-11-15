// Source-level guard for the ACK/NVS removal failure path.
// Run from firmware/ with a host C++ compiler:
//   g++ -std=c++11 tests/event_ack_remove_source_test.cpp -o event_ack_remove_source_test
//   ./event_ack_remove_source_test

#include <cassert>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <string>

int main() {
  std::ifstream sourceFile("src/main.cpp");
  assert(sourceFile && "run this harness from the firmware directory");
  const std::string source((std::istreambuf_iterator<char>(sourceFile)),
                           std::istreambuf_iterator<char>());

  const std::size_t ackMatch = source.find(
      "if(eventMatches(current.c_str(),ack.bootId,ack.seq))");
  assert(ackMatch != std::string::npos);
  const std::size_t removeCall = source.find(
      "bool removed=eventStore.remove(key.c_str());", ackMatch);
  const std::size_t removeFailure = source.find("if(!removed)", removeCall);
  const std::size_t failureReturn = source.find("return;", removeFailure);
  const std::size_t advanceHead = source.find(
      "eventHead=(eventHead+1)%EVENT_CAPACITY;", removeFailure);
  const std::size_t savePointers = source.find(
      "saveEventPointers(ack.bootId,ack.seq,\"app_ack\")", removeFailure);

  assert(removeCall != std::string::npos);
  assert(removeFailure != std::string::npos);
  assert(failureReturn != std::string::npos);
  assert(advanceHead != std::string::npos);
  assert(savePointers != std::string::npos);
  assert(removeCall < removeFailure);
  assert(removeFailure < failureReturn);
  assert(failureReturn < advanceHead);
  assert(advanceHead < savePointers);
  assert(source.find("eventRemoveRetryPayload=current", removeFailure) < failureReturn);

  std::puts("ACK remove failure retains the queue head before pointer advancement.");
  return 0;
}
