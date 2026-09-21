// 2× SG90 pan/tilt on LEDC at 50 Hz (B9). Moves with a slew-rate limit, and stops sending pulses
// once it has been at the target for servo_idle ms, so the servos don't buzz at rest.
#pragma once
#include "config.h"
#include "pins.h"

static const uint32_t SERVO_HZ = 50;
static const uint8_t SERVO_BITS = 16;

struct Servo {
  int pin;
  const char *centreKey, *minKey, *maxKey;
  float pos, target;                           // deg
  bool active;                                 // pulses on
  uint32_t atTargetMs;
};
static Servo servos[2] = {
  {PIN_SERVO_PAN, "pan_c", "pan_min", "pan_max", 0, 0, false, 0},
  {PIN_SERVO_TILT, "tilt_c", "tilt_min", "tilt_max", 0, 0, false, 0},
};
static uint32_t servoLastMs = 0;

static void servoWrite(Servo &s) {
  float us = cfg(s.centreKey) + s.pos * cfg("us_deg");
  us = constrain(us, 500.0f, 2500.0f);
  ledcWrite(s.pin, (uint32_t)(us / 20000.0f * ((1u << SERVO_BITS) - 1)));
}

static void servosInit() {
  for (auto &s : servos) {
    ledcAttach(s.pin, SERVO_HZ, SERVO_BITS);
    ledcWrite(s.pin, 0);                       // no pulses until the first `look`
  }
}

// Returns the clamped targets through pan/tilt.
static void servosLook(float &pan, float &tilt) {
  float *t[2] = {&pan, &tilt};
  for (int i = 0; i < 2; i++) {
    Servo &s = servos[i];
    *t[i] = constrain(*t[i], cfg(s.minKey), cfg(s.maxKey));
    s.target = *t[i];
    if (!s.active) {                           // first move after idle: start from the last position
      s.active = true;
      servoWrite(s);
    }
    s.atTargetMs = 0;
  }
}

static void servosUpdate() {                   // call often; works in real time, not per call
  uint32_t now = millis();
  float dt = (now - servoLastMs) / 1000.0f;
  servoLastMs = now;
  float step = cfg("servo_dps") * dt;
  for (auto &s : servos) {
    if (!s.active) continue;
    float d = s.target - s.pos;
    if (fabsf(d) > 0.01f) {
      s.pos += constrain(d, -step, step);
      servoWrite(s);
      s.atTargetMs = 0;
    } else {
      if (!s.atTargetMs) s.atTargetMs = now;
      uint32_t idle = (uint32_t)cfg("servo_idle");
      if (idle && now - s.atTargetMs > idle) {
        ledcWrite(s.pin, 0);                   // detach: no pulses, no buzz
        s.active = false;
      }
    }
  }
}
