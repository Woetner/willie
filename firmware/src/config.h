// Runtime settings, changed from the Pi with `cfg <key> <value>` (D13: no magic numbers).
// Defaults here are bench-safe; the Pi pushes willie.yaml values after connecting.
// Limits are enforced here, so the Pi, the AI or the dashboard can never go past them (§10.2).
// All firmware modules are headers included once by main.cpp (one translation unit), so these
// statics exist once.
#pragma once
#include <Arduino.h>

#define PWM_CAP_HARD 60.0f   // % — 6 V motors (3-7.5 V) on a 12.6 V pack: 60 % = 7.5 V average, the motors' maximum (D8, raised from 50 on 1 Oct by Wouter). The ONLY place this number lives.

struct Setting {
  const char *key;
  float value, min, max;
};

// order matters only for `cfg?` output
static Setting SETTINGS[] = {
  {"pwm_cap",      60,    0,   PWM_CAP_HARD},  // % duty, soft cap below the hard cap
  {"wd_ms",        200,   50,  1000},          // no drive/pwm command for this long = brake
  {"v_full",       600,   50,  3000},          // mm/s of a wheel at 100 % duty (the PID's feedforward)
  {"pid_on",       0,     0,   1},             // F2 wheel speed loop (wheels.h); on after B12 checked einv_*
  {"kp",           1.0,   0,   20},            // PID gains on the speed error in duty % (unitless)
  {"ki",           2.0,   0,   20},            // 1/s
  {"kd",           0.0,   0,   5},             // s
  {"wheel_d",      100,   30,  200},           // mm
  {"track",        170,   50,  400},           // mm between wheel contact lines
  {"cpr",          3840,  1,   100000},        // encoder counts per WHEEL revolution: x4 quadrature, 10 marked turns gave 3860 (B12, 1 Oct)
  {"inv_l",        1,     0,   1},             // flip left motor direction (B12, 1 Oct: the left wheel ran backward on "forward")
  {"inv_r",        1,     0,   1},             // flip right motor direction (mirrored mount)
  {"einv_l",       0,     0,   1},             // flip left encoder count
  {"einv_r",       0,     0,   1},             // flip right encoder count (B12, 1 Oct: forward counted negative)
  {"pan_c",        1500,  500, 2500},          // µs at pan 0°
  {"tilt_c",       1500,  500, 2500},          // µs at tilt 0°
  {"us_deg",       10.5,  5,   15},            // µs per degree (SG90 ≈ 2000 µs / 180°)
  {"pan_min",      -90,   -95, 0},             // deg
  {"pan_max",      90,    0,   95},
  {"tilt_min",     -30,   -90, 0},
  {"tilt_max",     45,    0,   90},
  {"servo_dps",    180,   10,  600},           // top speed, deg/s
  {"servo_smooth", 9,     0,   40},            // rad/s of the smoothing spring (higher = snappier, lower = softer); 0 = constant-speed slew
  {"servo_idle",   800,   0,   10000},         // ms at target before the pulses stop (0 = never)
  {"cliff_on",     1,     0,   1},             // 0: the down-looking ToF never stop the wheels (bench with the wheels in the air)
  {"cliff_mm",     80,    30,  400},           // down-looking ToF reads more than this = no floor = cliff
  {"cliff_ms",     250,   0,   2000},          // "no floor" must last this long before it is a cliff (a dropout or a dip of the nose is not)
  {"cliff_inv",    0,     0,   1},             // 1: an invalid ToF answer (8190/8191) counts as "no floor" too; 0: only a valid far reading does
  {"tilt_stop",    25,    5,   60},            // deg from level = estop
  {"need_io",      0,     0,   1},             // 1: refuse to drive without cliff/bumper (PCF8574) — set in F1
  {"shunt_mohm",   100,   1,   1000},          // INA219 shunt resistor
  {"stream_hz",    50,    0,   100},           // `st` line rate (0 = off)
  {"sleep_hz",     1,     0.2, 50},            // `st` line rate while asleep (Phase P)
};
static const int N_SETTINGS = sizeof(SETTINGS) / sizeof(SETTINGS[0]);

static inline Setting *findSetting(const char *key) {
  for (int i = 0; i < N_SETTINGS; i++)
    if (strcmp(SETTINGS[i].key, key) == 0) return &SETTINGS[i];
  return nullptr;
}

static inline float cfg(const char *key) {
  Setting *s = findSetting(key);
  return s ? s->value : 0;
}

// Returns false for an unknown key; clamps to [min, max].
static inline bool setCfg(const char *key, float v) {
  Setting *s = findSetting(key);
  if (!s) return false;
  s->value = constrain(v, s->min, s->max);
  return true;
}

// Which of the five VL53L0X are used: front L, front C, front R, cliff L, cliff R. A disabled sensor stays in
// reset (XSHUT low), is never retried and reports 0. Front C is off since 2 Oct: its module does not answer on
// I2C (wiring or dead). Set it to 1 again after the repair.
static const bool TOF_ENABLED[5] = {true, false, true, true, true};
