// WILL-E firmware (D18): the real-time layer on a classic ESP32-WROOM-32 DevKit.
//
// Owns motors (TB6612), encoders (PCNT), pan/tilt servos, I2C sensors, cliff/bumper and the safety
// rules that must hold even when the Pi hangs: 200 ms watchdog, PWM cap, bumper/cliff/tilt stop.
// Link protocol: WILL-E.md §5.2 and willie/hal/proto.py. Every module is a header included once
// here (one translation unit).
#include <Arduino.h>
#include "proto.h"
#include "config.h"
#include "pins.h"
#include "motors.h"
#include "encoders.h"
#include "wheels.h"
#include "servos.h"
#include "sensors.h"

HardwareSerial &Link = Serial1;

static char rxBuf[256];
static size_t rxLen = 0;
static uint32_t badLines = 0, pings = 0;

// odometry (encoders only; the gyro fusion is F3)
static float odoX = 0, odoY = 0, odoTh = 0, odoV = 0, odoW = 0;   // mm, rad, mm/s, rad/s
static int32_t lastTicks[2] = {0, 0};
static uint32_t lastOdoUs = 0;

static bool prevBump[2] = {false, false}, prevCliff[2] = {false, false};
static uint32_t tiltSinceMs = 0;

// ---------------------------------------------------------------- link helpers
static void sendf(const char *fmt, ...) {
  char msg[240];
  va_list ap;
  va_start(ap, fmt);
  vsnprintf(msg, sizeof(msg), fmt, ap);
  va_end(ap);
  sendLine(Link, msg);
}

static void sendHello() { sendf("hello willie-fw %s %s", FW_VERSION, BOARD_NAME); }

static void sendStat() {
  sendf("stat pcf %d mpu %d ina %d tof %d%d%d enc %d estop %02X wd_trips %lu bad_lines %lu i2c_err %lu",
        S.pcfOk, S.mpuOk, S.inaOk, S.tofOk[0], S.tofOk[1], S.tofOk[2], encOk, estop,
        (unsigned long)wdTrips, (unsigned long)badLines, (unsigned long)S.i2cErrors);
}

// ---------------------------------------------------------------- safety events
static const char *estopName(uint8_t r) {
  switch (r) {
    case ES_BUMP_L: return "bump_l";
    case ES_BUMP_R: return "bump_r";
    case ES_CLIFF_L: return "cliff_l";
    case ES_CLIFF_R: return "cliff_r";
    case ES_TILT: return "tilt";
  }
  return "?";
}

static void triggerEstop(uint8_t reason) {
  bool fresh = !(estop & reason);
  estop |= reason;
  motorsBrake();
  if (fresh) sendf("estop %s %lu", estopName(reason), (unsigned long)millis());
}

static bool motionAllowed() {
  if (estop) {
    sendf("err estop %02X", estop);
    return false;
  }
  if (cfg("need_io") > 0.5f && !S.pcfOk) {
    sendLine(Link, "err no_io");
    return false;
  }
  return true;
}

// Bumper and cliff: an event on every edge (B11), an estop on every onset.
static void checkIo() {
  bool bump[2] = {bumpL(), bumpR()}, cliff[2] = {cliffL(), cliffR()};
  const char *side[2] = {"l", "r"};
  for (int i = 0; i < 2; i++) {
    if (bump[i] != prevBump[i]) {
      if (bump[i]) triggerEstop(i ? ES_BUMP_R : ES_BUMP_L);
      sendf("ev bump_%s %d %lu", side[i], bump[i], (unsigned long)millis());
      prevBump[i] = bump[i];
    }
    if (cliff[i] != prevCliff[i]) {
      if (cliff[i]) triggerEstop(i ? ES_CLIFF_R : ES_CLIFF_L);
      sendf("ev cliff_%s %d %lu", side[i], cliff[i], (unsigned long)millis());
      prevCliff[i] = cliff[i];
    }
  }
}

static void checkTilt() {
  if (!S.mpuOk) return;
  uint32_t now = millis();
  if (S.tiltDeg > cfg("tilt_stop")) {          // 100 ms debounce: bumps shake the IMU
    if (!tiltSinceMs) tiltSinceMs = now;
    else if (now - tiltSinceMs > 100) triggerEstop(ES_TILT);
  } else {
    tiltSinceMs = 0;
  }
}

// ---------------------------------------------------------------- odometry
static void odoUpdate() {
  uint32_t now = micros();
  float dt = (now - lastOdoUs) / 1e6f;
  lastOdoUs = now;
  int32_t t[2] = {encoderTicks(0), encoderTicks(1)};
  float mmPerTick = PI * cfg("wheel_d") / cfg("cpr");
  float dl = (t[0] - lastTicks[0]) * mmPerTick, dr = (t[1] - lastTicks[1]) * mmPerTick;
  lastTicks[0] = t[0];
  lastTicks[1] = t[1];
  float d = (dl + dr) / 2, dth = (dr - dl) / cfg("track");
  odoX += d * cosf(odoTh + dth / 2);
  odoY += d * sinf(odoTh + dth / 2);
  odoTh = remainderf(odoTh + dth, 2 * PI);
  if (dt > 0) {
    odoV = d / dt;
    odoW = dth / dt;
  }
}

static void odoReset() {
  odoX = odoY = odoTh = 0;
  lastTicks[0] = encoderTicks(0);
  lastTicks[1] = encoderTicks(1);
}

// ---------------------------------------------------------------- telemetry
// st <ms> <ticksL> <ticksR> <x mm> <y mm> <th mrad> <v mm/s> <w mrad/s> <tofL> <tofC> <tofR mm>
//    <io hex> <mV> <mA> <ax> <ay> <az mg> <gx> <gy> <gz 0.1 dps> <tilt 0.1 deg>
//    <pan> <tilt 0.1 deg> <pwmL> <pwmR %> <flags hex> <i2c errors>
// flags: bits 0-4 estop (bump_l, bump_r, cliff_l, cliff_r, tilt), 5 pcf ok, 6 mpu ok, 7 ina ok,
//        8-10 tof L/C/R ok, 11 encoders ok, 12 motors active
static void sendState() {
  uint32_t flags = estop | S.pcfOk << 5 | S.mpuOk << 6 | S.inaOk << 7 | S.tofOk[0] << 8 |
                   S.tofOk[1] << 9 | S.tofOk[2] << 10 | encOk << 11 | motionActive << 12;
  sendf("st %lu %ld %ld %d %d %d %d %d %u %u %u %02X %ld %ld %d %d %d %d %d %d %d %d %d %d %d %X %lu",
        (unsigned long)millis(), (long)encoderTicks(0), (long)encoderTicks(1),
        (int)odoX, (int)odoY, (int)(odoTh * 1000), (int)odoV, (int)(odoW * 1000),
        S.tof[0], S.tof[1], S.tof[2], S.io, (long)S.mv, (long)S.ma,
        S.acc[0], S.acc[1], S.acc[2], S.gyro[0], S.gyro[1], S.gyro[2], (int)(S.tiltDeg * 10),
        (int)(servos[0].pos * 10), (int)(servos[1].pos * 10),
        (int)motorPct[0], (int)motorPct[1], (unsigned)flags, (unsigned long)S.i2cErrors);
}

// ---------------------------------------------------------------- commands
static float argf(bool &ok) {
  char *w = strtok(nullptr, " ");
  if (!w) { ok = false; return 0; }
  char *end;
  float v = strtof(w, &end);
  if (*end) ok = false;
  return v;
}

static void handleLine(char *line) {
  if (!checkLine(line)) {
    badLines++;
    sendLine(Link, "err crc");
    return;
  }
  char *cmd = strtok(line, " ");
  if (!cmd) return;
  bool ok = true;

  if (!strcmp(cmd, "ping")) {
    char *seq = strtok(nullptr, " ");
    sendf("pong %s %lu", seq ? seq : "0", (unsigned long)millis());
    pings++;
  } else if (!strcmp(cmd, "drive")) {              // drive <v m/s> <w rad/s>, open loop until F2
    float v = argf(ok), w = argf(ok);
    if (!ok) { sendLine(Link, "err args"); return; }
    if (!motionAllowed()) { motorsBrake(); return; }
    float half = w * cfg("track") / 2.0f;           // mm/s at each wheel from turning
    wheelsDrive(v * 1000 - half, v * 1000 + half);  // F2: closed loop when pid_on, else open
  } else if (!strcmp(cmd, "pwm")) {                // pwm <left %> <right %>, bench (B12)
    float l = argf(ok), r = argf(ok);
    if (!ok) { sendLine(Link, "err args"); return; }
    if (!motionAllowed()) { motorsBrake(); return; }
    wheelsOpen();
    motorsSet(l, r);
  } else if (!strcmp(cmd, "stop")) {
    wheelsOpen();
    motorsBrake();
  } else if (!strcmp(cmd, "look")) {               // look <pan deg> <tilt deg>
    float p = argf(ok), t = argf(ok);
    if (!ok) { sendLine(Link, "err args"); return; }
    servosLook(p, t);
  } else if (!strcmp(cmd, "led")) {
    ledSet(argf(ok) > 0.5f);
  } else if (!strcmp(cmd, "cfg")) {                // cfg <key> <value>  ->  cfg <key> <clamped value>
    char *key = strtok(nullptr, " ");
    float v = argf(ok);
    if (!key || !ok) { sendLine(Link, "err args"); return; }
    if (!setCfg(key, v)) { sendf("err cfg %.24s", key); return; }
    sendf("cfg %s %g", key, cfg(key));
  } else if (!strcmp(cmd, "cfg?")) {
    for (int i = 0; i < N_SETTINGS; i++) sendf("cfg %s %g", SETTINGS[i].key, SETTINGS[i].value);
  } else if (!strcmp(cmd, "clear")) {              // clear the estop latch (the Pi decides when)
    estop = 0;
    tiltSinceMs = 0;
    sendLine(Link, "ok clear");
  } else if (!strcmp(cmd, "cal")) {                // gyro bias, robot must stand still ~1 s
    motorsBrake();
    sendLine(Link, mpuCalibrate() ? "ok cal" : "err cal");
  } else if (!strcmp(cmd, "odo0")) {
    odoReset();
    sendLine(Link, "ok odo0");
  } else if (!strcmp(cmd, "stat?")) {
    sendStat();
  } else if (!strcmp(cmd, "hello?")) {
    sendHello();
  } else {
    sendf("err unknown %.24s", cmd);
  }
}

static void readLink() {
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
      rxLen = 0;                                    // overlong garbage: drop it
      badLines++;
    }
  }
}

// ---------------------------------------------------------------- main
struct Every {                                      // simple fixed-rate scheduler
  uint32_t periodUs, next = 0;
  bool due(uint32_t now) {
    if ((int32_t)(now - next) < 0) return false;
    next = now + periodUs;
    return true;
  }
};
static Every tPcf{2000}, tImu{10000}, tTof{10000}, tIna{20000}, tOdo{10000}, tServo{5000},
    tState{20000}, tBlink{500000}, tDebug{5000000};

void setup() {
  motorsInit();                                     // first: pins to brake before anything else
  Serial.begin(115200);                             // debug (USB)
  pinMode(PIN_LED, OUTPUT);
  Link.setRxBufferSize(1024);
  Link.setTxBufferSize(2048);
  Link.begin(LINK_BAUD, SERIAL_8N1, PIN_LINK_RX, PIN_LINK_TX);
  encodersInit();
  wheelsInit();                                     // after the encoders: reads their counts
  servosInit();
  sensorsInit();
  odoReset();
  lastOdoUs = micros();
  sendHello();
  sendStat();
  Serial.printf("WILL-E fw %s on %s, link %d baud RX=%d TX=%d\n", FW_VERSION, BOARD_NAME, LINK_BAUD,
                PIN_LINK_RX, PIN_LINK_TX);
  Serial.printf("pcf %d mpu %d ina %d tof %d%d%d enc %d\n", S.pcfOk, S.mpuOk, S.inaOk, S.tofOk[0],
                S.tofOk[1], S.tofOk[2], encOk);
}

void loop() {
  readLink();
  uint32_t now = micros();

  if (S.pcfOk && tPcf.due(now) && pcfRead()) checkIo();
  if (S.mpuOk && tImu.due(now)) { mpuUpdate(); checkTilt(); }
  if (tTof.due(now)) tofUpdate();
  if (S.inaOk && tIna.due(now)) inaUpdate();
  if (tOdo.due(now)) odoUpdate();
  if (tServo.due(now)) servosUpdate();

  if (pidSignFault) {                               // set by the wheel task (wheels.h)
    pidSignFault = false;
    sendLine(Link, "err pid_sign");
  }
  if (wdTripped) {                                  // set by the watchdog task, reported here
    wdTripped = false;
    sendf("ev wd %lu", (unsigned long)millis());
  }
  float hz = cfg("stream_hz");
  if (hz > 0) {
    tState.periodUs = (uint32_t)(1e6f / hz);
    if (tState.due(now)) sendState();
  }
  if (tBlink.due(now)) digitalWrite(PIN_LED, !digitalRead(PIN_LED));   // 1 Hz = alive
  if (tDebug.due(now))
    Serial.printf("up %lus pings %lu bad %lu wd %lu i2c_err %lu estop %02X\n", millis() / 1000,
                  (unsigned long)pings, (unsigned long)badLines, (unsigned long)wdTrips,
                  (unsigned long)S.i2cErrors, estop);
}
