// Source-level guard for the ACK/NVS removal failure path.
// Run from firmware/ with a host C++ compiler:
//   g++ -std=c++11 tests/event_ack_remove_source_test.cpp -o event_ack_remove_source_test
//   ./event_ack_remove_source_test

#include <cassert>
#include <cctype>
#include <cstdio>
#include <fstream>
#include <iterator>
#include <string>

static std::string withoutWhitespace(const std::string &text) {
  std::string compact;
  compact.reserve(text.size());
  for (std::string::const_iterator it = text.begin(); it != text.end(); ++it) {
    if (!std::isspace(static_cast<unsigned char>(*it))) compact.push_back(*it);
  }
  return compact;
}

int main() {
  std::ifstream sourceFile("src/main.cpp");
  assert(sourceFile && "run this harness from the firmware directory");
  const std::string source((std::istreambuf_iterator<char>(sourceFile)),
                           std::istreambuf_iterator<char>());
  const std::string compactSource = withoutWhitespace(source);

  const std::size_t ackMatch = compactSource.find(
      "if(eventMatches(current.c_str(),ack.bootId,ack.seq))");
  assert(ackMatch != std::string::npos);
  const std::size_t removeCall = compactSource.find(
      "boolremoved=eventStore.remove(key.c_str());", ackMatch);
  const std::size_t removeFailure = compactSource.find("if(!removed)", removeCall);
  const std::size_t failureReturn = compactSource.find("return;", removeFailure);
  const std::size_t advanceHead = compactSource.find(
      "eventHead=(eventHead+1)%eventCapacity;eventCount--;", removeFailure);
  const std::size_t savePointers = compactSource.find(
      "saveEventPointers(ack.bootId,ack.seq,\"app_ack\")", advanceHead);

  assert(removeCall != std::string::npos);
  assert(removeFailure != std::string::npos);
  assert(failureReturn != std::string::npos);
  assert(advanceHead != std::string::npos);
  assert(savePointers != std::string::npos);
  assert(removeCall < removeFailure);
  assert(removeFailure < failureReturn);
  assert(failureReturn < advanceHead);
  assert(advanceHead < savePointers);
  assert(compactSource.find("eventRemoveRetryPayload=current", removeFailure) < failureReturn);

  std::puts("Matching app ACK removes the head before pointer commit; failed removal retains the head.");
  return 0;
}
