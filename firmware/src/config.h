// Runtime settings, changed from the Pi with `cfg <key> <value>` (D13: no magic numbers).
// Defaults here are bench-safe; the Pi pushes willie.yaml values after connecting.
// Limits are enforced here, so the Pi, the AI or the dashboard can never go past them (§10.2).
// All firmware modules are headers included once by main.cpp (one translation unit), so these
// statics exist once.
#pragma once
#include <Arduino.h>

#define PWM_CAP_HARD 50.0f   // % — 6 V motors on a 12.6 V pack (D8). The ONLY place this number lives.

struct Setting {
  const char *key;
  float value, min, max;
};

// order matters only for `cfg?` output
static Setting SETTINGS[] = {
  {"pwm_cap",      50,    0,   PWM_CAP_HARD},  // % duty, soft cap below the hard cap
  {"wd_ms",        200,   50,  1000},          // no drive/pwm command for this long = brake
  {"v_full",       600,   50,  3000},          // mm/s of a wheel at 100 % duty (open-loop drive until F2 PID)
  {"wheel_d",      100,   30,  200},           // mm
  {"track",        170,   50,  400},           // mm between wheel contact lines
  {"cpr",          960,   1,   100000},        // encoder counts per WHEEL revolution (measure in B12)
  {"inv_l",        0,     0,   1},             // flip left motor direction
  {"inv_r",        1,     0,   1},             // flip right motor direction (mirrored mount)
  {"einv_l",       0,     0,   1},             // flip left encoder count
  {"einv_r",       1,     0,   1},             // flip right encoder count
  {"pan_c",        1500,  500, 2500},          // µs at pan 0°
  {"tilt_c",       1500,  500, 2500},          // µs at tilt 0°
  {"us_deg",       10.5,  5,   15},            // µs per degree (SG90 ≈ 2000 µs / 180°)
  {"pan_min",      -90,   -95, 0},             // deg
  {"pan_max",      90,    0,   95},
  {"tilt_min",     -30,   -90, 0},
  {"tilt_max",     45,    0,   90},
  {"servo_dps",    180,   10,  600},           // slew rate, deg/s
  {"servo_idle",   800,   0,   10000},         // ms at target before the pulses stop (0 = never)
  {"cliff_hi",     1,     0,   1},             // 1: TCRT5000 DO high = no floor = cliff
  {"tilt_stop",    25,    5,   60},            // deg from level = estop
  {"need_io",      0,     0,   1},             // 1: refuse to drive without cliff/bumper (PCF8574) — set in F1
  {"shunt_mohm",   100,   1,   1000},          // INA219 shunt resistor
  {"stream_hz",    50,    0,   100},           // `st` line rate (0 = off)
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
