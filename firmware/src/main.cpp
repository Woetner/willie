// WILL-E firmware — A9: blink + ping/pong over the link UART (D18).
#include <Arduino.h>
#include "proto.h"

#ifndef LED_PIN
#define LED_PIN -1
#endif

HardwareSerial &Link = Serial1;

static char rxBuf[256];
static size_t rxLen = 0;
static uint32_t pings = 0, badLines = 0;
static uint32_t lastBlink = 0, lastStats = 0;
static bool ledOn = false;

static void setLed(bool on) {
#if defined(RGB_BUILTIN)
  neopixelWrite(RGB_BUILTIN, 0, 0, on ? 24 : 0);   // dim blue on the S3 DevKit's RGB LED
#else
  if (LED_PIN >= 0) digitalWrite(LED_PIN, on ? HIGH : LOW);
#endif
}

static void sendHello() {
  char msg[64];
  snprintf(msg, sizeof(msg), "hello willie-fw %s %s", FW_VERSION, BOARD_NAME);
  sendLine(Link, msg);
}

static void handleLine(char *line) {
  if (!checkLine(line)) {
    badLines++;
    sendLine(Link, "err crc");
    return;
  }
  char *cmd = strtok(line, " ");
  if (!cmd) return;
  if (strcmp(cmd, "ping") == 0) {
    char *seq = strtok(nullptr, " ");
    char msg[48];
    snprintf(msg, sizeof(msg), "pong %s %lu", seq ? seq : "0", (unsigned long)millis());
    sendLine(Link, msg);
    pings++;
  } else if (strcmp(cmd, "hello?") == 0) {
    sendHello();
  } else {
    char msg[48];
    snprintf(msg, sizeof(msg), "err unknown %.24s", cmd);
    sendLine(Link, msg);
  }
}

void setup() {
  Serial.begin(115200);                        // debug (USB)
  if (LED_PIN >= 0) pinMode(LED_PIN, OUTPUT);
  Link.setRxBufferSize(1024);
  Link.begin(LINK_BAUD, SERIAL_8N1, LINK_RX, LINK_TX);
  delay(50);
  sendHello();
  Serial.printf("WILL-E fw %s on %s, link %d baud RX=%d TX=%d\n",
                FW_VERSION, BOARD_NAME, LINK_BAUD, LINK_RX, LINK_TX);
}

void loop() {
  // link: assemble lines, answer immediately (keeps the round-trip short)
  while (Link.available()) {
    char c = (char)Link.read();
    if (c == '\n') {
      rxBuf[rxLen] = '\0';
      if (rxLen && rxBuf[rxLen - 1] == '\r') rxBuf[--rxLen] = '\0';
      if (rxLen) handleLine(rxBuf);
      rxLen = 0;
    } else if (rxLen < sizeof(rxBuf) - 1) {
      rxBuf[rxLen++] = c;
    } else {
      rxLen = 0;                                // overlong garbage: drop it
      badLines++;
    }
  }

  uint32_t now = millis();
  if (now - lastBlink >= 500) {                 // 1 Hz blink = firmware alive
    lastBlink = now;
    ledOn = !ledOn;
    setLed(ledOn);
  }
  if (now - lastStats >= 5000) {
    lastStats = now;
    Serial.printf("up %lus  pings %lu  bad %lu\n", now / 1000, (unsigned long)pings, (unsigned long)badLines);
  }
}
