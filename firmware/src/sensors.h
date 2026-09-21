// I2C sensors (B10, B11): PCF8574 (cliff, bumper, ToF XSHUT, LED), MPU6050, INA219, 3× VL53L0X.
// Every missing device is reported, never fatal: the bench has only some of them wired.
#pragma once
#include <Wire.h>
#include <VL53L0X.h>
#include "config.h"
#include "pins.h"

static const uint8_t ADDR_PCF = 0x20, ADDR_MPU = 0x68, ADDR_INA = 0x40;
static const uint8_t ADDR_TOF[3] = {0x30, 0x31, 0x32};   // left, centre, right

struct SensorState {
  bool pcfOk = false, mpuOk = false, inaOk = false, tofOk[3] = {false, false, false};
  uint8_t io = 0xFF;                           // PCF8574 inputs, raw
  int16_t acc[3] = {0, 0, 0};                  // mg
  int16_t gyro[3] = {0, 0, 0};                 // 0.1 deg/s, bias removed
  float gyroBias[3] = {0, 0, 0};               // raw LSB
  float tiltDeg = 0;                           // angle between body Z and gravity
  uint16_t tof[3] = {0, 0, 0};                 // mm; 0 = no reading yet, 8190+ = nothing in range
  int32_t mv = 0, ma = 0;                      // pack volts / current from the INA219
  uint32_t i2cErrors = 0;
};
static SensorState S;
static uint8_t pcfOut = 0xFF;                  // PCF8574 output latch; 1 = input / released
static VL53L0X tofDev[3];

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
static bool cliffBit(int bit) {
  if (!S.pcfOk) return false;
  bool high = S.io & (1 << bit);
  return cfg("cliff_hi") > 0.5f ? high : !high;
}
static bool cliffL() { return cliffBit(IO_CLIFF_L); }
static bool cliffR() { return cliffBit(IO_CLIFF_R); }

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
static void mpuUpdate() {
  int16_t a[3], g[3];
  if (!mpuReadRaw(a, g)) return;
  float ax = a[0] / 8.192f, ay = a[1] / 8.192f, az = a[2] / 8.192f;   // mg
  for (int i = 0; i < 3; i++) {
    S.acc[i] = (int16_t)(a[i] / 8.192f);
    S.gyro[i] = (int16_t)((g[i] - S.gyroBias[i]) / 6.55f);          // 0.1 deg/s
  }
  float n = sqrtf(ax * ax + ay * ay + az * az);
  if (n > 200) S.tiltDeg = acosf(constrain(az / n, -1.0f, 1.0f)) * 57.2958f;
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

// ---- VL53L0X ×3: XSHUT on the PCF8574 for left and centre; right is always on ----
// A sensor keeps a changed address until it loses power, so after an MCU reset the right one
// may still sit at 0x32. Put every awake sensor back on 0x29 first, then move them one by one.
// Without the PCF8574 all XSHUTs float high: only one ToF may be connected then.
static bool tofStart(int i) {
  VL53L0X &t = tofDev[i];
  t.setBus(&Wire);
  t.setTimeout(30);
  if (!i2cProbe(0x29)) return false;
  t.setAddress(0x29);                          // library + device agree on 0x29 (no-op write)
  if (!t.init()) return false;
  t.setAddress(ADDR_TOF[i]);
  t.startContinuous();
  return true;
}
static void tofInit() {
  pcfSetBit(IO_XSHUT_L, false);
  pcfSetBit(IO_XSHUT_C, false);
  delay(10);
  for (uint8_t a : ADDR_TOF)
    if (i2cProbe(a)) i2cWriteReg(a, 0x8A, 0x29);  // I2C_SLAVE_DEVICE_ADDRESS back to default
  S.tofOk[2] = tofStart(2);
  if (S.pcfOk) {
    pcfSetBit(IO_XSHUT_C, true);  delay(10); S.tofOk[1] = tofStart(1);
    pcfSetBit(IO_XSHUT_L, true);  delay(10); S.tofOk[0] = tofStart(0);
  }
}
static void tofUpdate() {
  for (int i = 0; i < 3; i++) {
    if (!S.tofOk[i]) continue;
    VL53L0X &t = tofDev[i];
    uint8_t irq = t.readReg(VL53L0X::RESULT_INTERRUPT_STATUS);
    if (t.last_status) { S.i2cErrors++; continue; }
    if ((irq & 0x07) == 0) continue;           // no new range yet: don't block
    uint16_t mm = t.readRangeContinuousMillimeters();
    if (t.last_status || t.timeoutOccurred()) { S.i2cErrors++; continue; }
    S.tof[i] = mm;
  }
}

static void sensorsInit() {
  Wire.begin(PIN_SDA, PIN_SCL, 400000);
  Wire.setTimeOut(5);                          // ms: a stuck bus must not stall loop() for long
  S.pcfOk = i2cProbe(ADDR_PCF);
  if (S.pcfOk) { pcfWriteOut(); pcfRead(); }
  S.mpuOk = i2cProbe(ADDR_MPU) && mpuInit();
  S.inaOk = i2cProbe(ADDR_INA);
  tofInit();
}
