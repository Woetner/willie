// I2C sensors (B10, B11): PCF8574 (bumper, ToF XSHUT, LED), MPU6050, INA219, 5× VL53L0X
// (front left/centre/right + 2 looking down as cliff sensors).
// Every missing device is reported, never fatal: the bench has only some of them wired.
#pragma once
#include <Wire.h>
#include <VL53L0X.h>
#include "config.h"
#include "pins.h"

static const uint8_t ADDR_PCF = 0x20, ADDR_MPU = 0x68, ADDR_INA = 0x40;
#define N_TOF 5                                          // front L, C, R, cliff L, cliff R
static const uint8_t ADDR_TOF[N_TOF] = {0x30, 0x31, 0x32, 0x33, 0x34};
static const uint8_t XSHUT_TOF[N_TOF] = {IO_XSHUT_L, IO_XSHUT_C, IO_XSHUT_R, IO_XSHUT_CL, IO_XSHUT_CR};
enum { TOF_L, TOF_C, TOF_R, TOF_CL, TOF_CR };

struct SensorState {
  bool pcfOk = false, mpuOk = false, inaOk = false, tofOk[N_TOF] = {false, false, false, false, false};
  uint8_t io = 0xFF;                           // PCF8574 inputs, raw
  int16_t acc[3] = {0, 0, 0};                  // mg
  int16_t gyro[3] = {0, 0, 0};                 // 0.1 deg/s, bias removed
  float gyroBias[3] = {0, 0, 0};               // raw LSB
  float tiltDeg = 0;                           // angle between body Z and gravity
  uint16_t tof[N_TOF] = {0, 0, 0, 0, 0};       // mm; 0 = no reading yet, 8190+ = nothing in range
  int32_t mv = 0, ma = 0;                      // pack volts / current from the INA219
  uint32_t i2cErrors = 0, i2cRecoveries = 0;
};
static SensorState S;
static uint8_t pcfOut = 0xFF;                  // PCF8574 output latch; 1 = input / released
static VL53L0X tofDev[N_TOF];

// ---- I2C helpers that count errors (B10 "done when": no I2C error in 10 min) ----
static bool i2cWrite(uint8_t addr, const uint8_t *data, size_t n) {
  Wire.beginTransmission(addr);
  Wire.write(data, n);
  if (Wire.endTransmission() == 0) return true;
  S.i2cErrors++;
  return false;
}
static bool i2cWriteReg(uint8_t addr, uint8_t reg, uint8_t v) {
  uint8_t d[2] = {reg, v};
  return i2cWrite(addr, d, 2);
}
static bool i2cRead(uint8_t addr, uint8_t reg, uint8_t *buf, size_t n) {
  Wire.beginTransmission(addr);
  Wire.write(reg);
  if (Wire.endTransmission(false) != 0 || Wire.requestFrom(addr, (uint8_t)n) != n) {
    S.i2cErrors++;
    return false;
  }
  for (size_t i = 0; i < n; i++) buf[i] = Wire.read();
  return true;
}
static bool i2cProbe(uint8_t addr) {
  Wire.beginTransmission(addr);
  return Wire.endTransmission() == 0;
}

// ---- PCF8574 ----
static bool pcfWriteOut() { return i2cWrite(ADDR_PCF, &pcfOut, 1); }
static void pcfSetBit(int bit, bool high) {
  pcfOut = high ? (pcfOut | (1 << bit)) : (pcfOut & ~(1 << bit));
  if (S.pcfOk) pcfWriteOut();
}
static bool pcfRead() {
  if (Wire.requestFrom(ADDR_PCF, (uint8_t)1) != 1) { S.i2cErrors++; return false; }
  S.io = Wire.read();
  return true;
}
static void ledSet(bool on) { pcfSetBit(IO_LED, !on); }  // active low

// Cliff/bumper as booleans. Without the PCF8574 nothing reads as triggered.
static bool bumpL() { return S.pcfOk && !(S.io & (1 << IO_BUMP_L)); }
static bool bumpR() { return S.pcfOk && !(S.io & (1 << IO_BUMP_R)); }
// Cliff = a down-looking ToF sees no floor within cliff_mm (8190+ = nothing in range counts too).
// A missing sensor or one without a reading yet never reads as a cliff; F1's boot self-test and
// need_io decide whether the robot may drive without it (§10 rule 4).
static bool cliffTof(int i) {
  return cfg("cliff_on") > 0.5 && S.tofOk[i] && S.tof[i] && S.tof[i] > cfg("cliff_mm");
}
// "No floor" must last cliff_ms before it counts: the down-looking VL53L0X gives short dropouts (invalid 8190/8191)
// when the floor is dark or very close, the nose dips under braking, or the motors add noise, and a sensor hit by a
// bus error can stay stuck on "invalid". So at half the time the sensor is restarted once (tofReset): a stuck one
// reads the floor again and nothing locks, a real edge is still "far" after the restart and locks at cliff_ms.
// Checked every 2 ms from checkIo().
static bool tofReset(int i);
static bool cliffDebounced(int side) {
  static uint32_t since[2] = {0, 0};
  static bool healed[2] = {false, false};
  int idx = side ? TOF_CR : TOF_CL;
  if (cfg("cliff_on") < 0.5f || !S.tofOk[idx]) { since[side] = 0; healed[side] = false; return false; }
  uint32_t now = millis();
  // 0 = no reading yet (just after a restart). 8190/8191 = the sensor could not range (floor too close, dark, noise):
  // unknown unless cliff_inv says it counts as no floor (a real edge on a dark floor can answer that too).
  bool valid = S.tof[idx] > 0 && (S.tof[idx] < 8190 || cfg("cliff_inv") > 0.5f);
  if (valid) {
    if (S.tof[idx] <= cfg("cliff_mm")) { since[side] = 0; healed[side] = false; return false; }   // floor seen
    if (!since[side]) since[side] = now;
  } else if (!since[side]) {
    return false;                                       // unknown and no episode running
  }
  if (!healed[side] && now - since[side] >= (uint32_t)cfg("cliff_ms") / 2) { healed[side] = true; tofReset(idx); }
  return valid && now - since[side] >= (uint32_t)cfg("cliff_ms");
}
static bool cliffL() { return cliffDebounced(0); }
static bool cliffR() { return cliffDebounced(1); }

// ---- MPU6050: ±4 g, ±500 deg/s, 44 Hz low-pass ----
static bool mpuInit() {
  return i2cWriteReg(ADDR_MPU, 0x6B, 0x00)      // wake up, internal clock
      && i2cWriteReg(ADDR_MPU, 0x1A, 0x03)      // DLPF 44 Hz
      && i2cWriteReg(ADDR_MPU, 0x1B, 0x08)      // gyro ±500 deg/s  -> 65.5 LSB/(deg/s)
      && i2cWriteReg(ADDR_MPU, 0x1C, 0x08);     // accel ±4 g       -> 8192 LSB/g
}
static bool mpuReadRaw(int16_t a[3], int16_t g[3]) {
  uint8_t b[14];
  if (!i2cRead(ADDR_MPU, 0x3B, b, 14)) return false;
  for (int i = 0; i < 3; i++) {
    a[i] = (int16_t)(b[i * 2] << 8 | b[i * 2 + 1]);
    g[i] = (int16_t)(b[8 + i * 2] << 8 | b[9 + i * 2]);
  }
  return true;
}
static uint8_t mpuFails = 0;                     // consecutive failed reads: 25 in a row (0.5 s) = lost, re-probe
static void mpuUpdate() {
  int16_t a[3], g[3];
  if (!mpuReadRaw(a, g)) {
    if (++mpuFails >= 25) { S.mpuOk = false; S.tiltDeg = 0; }
    return;
  }
  mpuFails = 0;
  float ax = a[0] / 8.192f, ay = a[1] / 8.192f, az = a[2] / 8.192f;   // mg
  for (int i = 0; i < 3; i++) {
    S.acc[i] = (int16_t)(a[i] / 8.192f);
    S.gyro[i] = (int16_t)((g[i] - S.gyroBias[i]) / 6.55f);          // 0.1 deg/s
  }
  float n = sqrtf(ax * ax + ay * ay + az * az);
  if (n > 200) S.tiltDeg = acosf(constrain(az / n, -1.0f, 1.0f)) * 57.2958f;
}
// The MPU6050 is looked for again every 2 s when it is missing or lost (a loose plug at boot used to leave it
// dead until the next restart). The gyro bias is not re-measured here: `cal` does that.
static void mpuRetry() {
  static uint32_t last = 0;
  uint32_t now = millis();                       // not the loop's micros(): 2000 here is 2 s
  if (S.mpuOk || now - last < 2000) return;
  last = now;
  S.mpuOk = i2cProbe(ADDR_MPU) && mpuInit();
  if (S.mpuOk) mpuFails = 0;
}
// Average the gyro for ~1 s while the robot stands still (`cal` command).
static bool mpuCalibrate() {
  if (!S.mpuOk) return false;
  float sum[3] = {0, 0, 0};
  int n = 0;
  for (int k = 0; k < 200; k++) {
    int16_t a[3], g[3];
    if (mpuReadRaw(a, g)) {
      for (int i = 0; i < 3; i++) sum[i] += g[i];
      n++;
    }
    delay(5);
  }
  if (n < 100) return false;
  for (int i = 0; i < 3; i++) S.gyroBias[i] = sum[i] / n;
  return true;
}

// ---- INA219: power-on defaults (32 V, ±320 mV shunt, 12 bit, continuous) ----
static void inaUpdate() {
  uint8_t b[2];
  if (i2cRead(ADDR_INA, 0x02, b, 2)) S.mv = ((b[0] << 8 | b[1]) >> 3) * 4;
  if (i2cRead(ADDR_INA, 0x01, b, 2)) {
    int16_t shunt = (int16_t)(b[0] << 8 | b[1]);                    // 10 µV per LSB
    S.ma = (int32_t)(shunt * 10.0f / cfg("shunt_mohm"));
  }
}

// ---- VL53L0X ×5: every sensor has an XSHUT on the PCF8574 ----
// All start at 0x29. Hold every XSHUT low (the sensors lose their changed address), then wake
// them one at a time and give each its own address. Without the PCF8574 all XSHUTs float high:
// only one ToF may be connected then; it is started as front right.
static uint8_t tofWhy[N_TOF] = {0, 0, 0, 0, 0};          // why the last start failed: 0 ok, 1 no answer on 0x29, 2 init failed, 3 disabled in config.h
static bool tofStart(int i) {
  VL53L0X &t = tofDev[i];
  t.setBus(&Wire);
  t.setTimeout(30);
  tofWhy[i] = 1;
  if (!i2cProbe(0x29)) return false;
  t.setAddress(0x29);                          // library + device agree on 0x29 (no-op write)
  tofWhy[i] = 2;
  if (!t.init()) return false;
  t.setAddress(ADDR_TOF[i]);
  t.startContinuous();
  tofWhy[i] = 0;
  return true;
}
static uint8_t tofBad[N_TOF] = {0, 0, 0, 0, 0};          // consecutive failed reads
static uint32_t tofSeen[N_TOF] = {0, 0, 0, 0, 0};       // millis() of the last fresh range
static void tofInit() {
  if (!S.pcfOk) { S.tofOk[TOF_R] = tofStart(TOF_R); return; }
  for (int i = 0; i < N_TOF; i++) pcfSetBit(XSHUT_TOF[i], false);
  delay(10);
  for (int i = 0; i < N_TOF; i++) {
    if (!TOF_ENABLED[i]) { tofWhy[i] = 3; continue; }   // stays in reset
    pcfSetBit(XSHUT_TOF[i], true);
    delay(10);                                 // boot time after XSHUT goes high (datasheet: 1.2 ms)
    S.tofOk[i] = tofStart(i);
    if (S.tofOk[i]) tofSeen[i] = millis();
  }
}
// A sensor that fails to start at boot, or stops answering while running, is dropped (tofOk false, reading 0,
// ignored by the cliff stop and the Pi gate) and started again by tofRetry() every 5 s. Before 0.4.9 a ToF
// that missed its start-up (loose plug, power dip) stayed dead until the next power cycle.
static void tofDrop(int i) {
  S.tofOk[i] = false;
  S.tof[i] = 0;
  tofBad[i] = 0;
}
static void tofUpdate() {
  uint32_t now = millis();
  for (int i = 0; i < N_TOF; i++) {
    if (!S.tofOk[i]) continue;
    if (now - tofSeen[i] > 1000) { tofDrop(i); continue; }   // no fresh range for 1 s
    VL53L0X &t = tofDev[i];
    uint8_t irq = t.readReg(VL53L0X::RESULT_INTERRUPT_STATUS);
    if (t.last_status) { S.i2cErrors++; if (++tofBad[i] >= 25) tofDrop(i); continue; }
    if ((irq & 0x07) == 0) continue;           // no new range yet: don't block
    uint16_t mm = t.readRangeContinuousMillimeters();
    if (t.last_status || t.timeoutOccurred()) { S.i2cErrors++; if (++tofBad[i] >= 25) tofDrop(i); continue; }
    S.tof[i] = mm;
    tofBad[i] = 0;
    tofSeen[i] = now;
  }
}
// One dropped sensor at a time: hold every dropped sensor in reset except this one, so two sensors never
// answer on 0x29 together, then run the normal start-up for it. A dead module costs about 25 ms every 5 s.
static void tofRetry() {
  static uint32_t last = 0;
  uint32_t now = millis();
  if (!S.pcfOk || now - last < 5000) return;
  for (int i = 0; i < N_TOF; i++) {
    if (S.tofOk[i] || !TOF_ENABLED[i]) continue;
    last = now;
    for (int j = 0; j < N_TOF; j++) if (!S.tofOk[j]) pcfSetBit(XSHUT_TOF[j], false);
    delay(5);
    pcfSetBit(XSHUT_TOF[i], true);
    delay(10);
    S.tofOk[i] = tofStart(i);
    if (S.tofOk[i]) { tofSeen[i] = millis(); tofBad[i] = 0; }
    else pcfSetBit(XSHUT_TOF[i], false);
    return;
  }
}

// Restart one running sensor: reset it with its XSHUT and run the normal start-up. Dropped sensors stay in reset so
// nothing else answers on 0x29. About 25 ms.
static bool tofReset(int i) {
  for (int j = 0; j < N_TOF; j++) if (j != i && !S.tofOk[j]) pcfSetBit(XSHUT_TOF[j], false);
  pcfSetBit(XSHUT_TOF[i], false);
  delay(5);
  pcfSetBit(XSHUT_TOF[i], true);
  delay(10);
  S.tof[i] = 0;
  bool ok = tofStart(i);
  S.tofOk[i] = ok;
  if (ok) { tofSeen[i] = millis(); tofBad[i] = 0; }
  else pcfSetBit(XSHUT_TOF[i], false);
  return ok;
}

// I2C pins in use and how many addresses answered. If nothing answers on 21/22,
// try the wires the other way round (SDA/SCL swapped is harmless, just silent).
// 50 kHz: 8 devices on 10-30 cm jumper wires gave truncated MPU reads at 400 kHz (1 Oct, then 100 kHz); lowered
// again on 2 Oct while the motors (no suppression capacitors yet) were corrupting transfers.
static const uint32_t I2C_HZ = 50000;
static uint8_t i2cSda = PIN_SDA, i2cScl = PIN_SCL, i2cFound = 0;

static uint8_t i2cScan() {
  uint8_t n = 0;
  for (uint8_t a = 0x08; a < 0x78; a++) n += i2cProbe(a);
  return n;
}

static void i2cStart(uint8_t sda, uint8_t scl) {
  Wire.end();
  Wire.begin(sda, scl, I2C_HZ);
  Wire.setTimeOut(5);                          // ms: a stuck bus must not stall loop() for long
  i2cSda = sda; i2cScl = scl;
  i2cFound = i2cScan();
}

// A transfer corrupted by noise can leave a sensor holding SDA low mid-byte, and it stays stuck until power-cycled.
// Standard recovery: stop the bus, clock SCL up to 9 times so the slave finishes its byte, send a STOP, restart.
static void i2cRecover() {
  Wire.end();
  pinMode(i2cScl, OUTPUT_OPEN_DRAIN);
  pinMode(i2cSda, OUTPUT_OPEN_DRAIN);
  digitalWrite(i2cSda, HIGH);
  for (int k = 0; k < 9; k++) {
    digitalWrite(i2cScl, LOW);  delayMicroseconds(10);
    digitalWrite(i2cScl, HIGH); delayMicroseconds(10);
  }
  digitalWrite(i2cSda, LOW);  delayMicroseconds(10);   // STOP: SDA rises while SCL is high
  digitalWrite(i2cScl, HIGH); delayMicroseconds(10);
  digitalWrite(i2cSda, HIGH); delayMicroseconds(10);
  Wire.begin(i2cSda, i2cScl, I2C_HZ);
  Wire.setTimeOut(5);
  S.i2cRecoveries++;
}
// 6 or more failed transfers within 200 ms = a stuck bus.
static void i2cWatch() {
  static uint32_t lastMs = 0, errAt = 0;
  uint32_t now = millis();
  if (now - lastMs < 200) return;
  lastMs = now;
  if (S.i2cErrors - errAt >= 6) i2cRecover();
  errAt = S.i2cErrors;
}

static void sensorsInit() {
  i2cStart(PIN_SDA, PIN_SCL);
  if (!i2cFound) {
    i2cStart(PIN_SCL, PIN_SDA);
    if (!i2cFound) i2cStart(PIN_SDA, PIN_SCL);
  }
  S.pcfOk = i2cProbe(ADDR_PCF);
  if (S.pcfOk) { pcfWriteOut(); pcfRead(); }
  S.mpuOk = i2cProbe(ADDR_MPU) && mpuInit();
  S.inaOk = i2cProbe(ADDR_INA);
  tofInit();
}
