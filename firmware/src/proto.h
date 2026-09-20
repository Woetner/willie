// Pi <-> MCU line protocol (D18). Must match willie/hal/proto.py.
//   <payload>*<CRC8 as 2 uppercase hex digits>\n       e.g.  pong 17 5321*E0
// CRC-8: polynomial 0x07, init 0x00, no reflection, over the payload only.
#pragma once
#include <Arduino.h>

static inline uint8_t crc8(const char *data, size_t len) {
  uint8_t crc = 0;
  for (size_t i = 0; i < len; i++) {
    crc ^= (uint8_t)data[i];
    for (int b = 0; b < 8; b++) crc = (crc & 0x80) ? (uint8_t)((crc << 1) ^ 0x07) : (uint8_t)(crc << 1);
  }
  return crc;
}

// Send one framed line. `payload` must not contain '*' or '\n'.
static inline void sendLine(Stream &port, const char *payload) {
  char tail[5];
  snprintf(tail, sizeof(tail), "*%02X\n", crc8(payload, strlen(payload)));
  port.write((const uint8_t *)payload, strlen(payload));
  port.write((const uint8_t *)tail, 4);
}

// Check the CRC of a received line (without the '\n'). On success, cuts the line at '*'
// so `line` holds only the payload, and returns true.
static inline bool checkLine(char *line) {
  char *star = strrchr(line, '*');
  if (!star || star == line || strlen(star) != 3) return false;
  char *end;
  long crc = strtol(star + 1, &end, 16);
  if (*end != '\0') return false;
  if ((uint8_t)crc != crc8(line, star - line)) return false;
  *star = '\0';
  return true;
}
