// TB6612FNG motor driver + the safety layer that owns it (D18, §10.2).
//
// Every duty cycle goes through motorsSet(), which applies the PWM cap. The watchdog runs in an
// esp_timer task, not in loop(), so a stalled loop (hung I2C bus, long print) still stops the
// motors: no drive/pwm command for wd_ms = brake.
#pragma once
#include <esp_timer.h>
#include "config.h"
#include "pins.h"

static const uint32_t MOTOR_HZ = 20000;
static const uint8_t MOTOR_BITS = 10;
static const uint32_t MOTOR_MAX = (1u << MOTOR_BITS) - 1;

static SemaphoreHandle_t motorLock;
static float motorPct[2] = {0, 0};            // applied duty, signed % (+ = forward), after the cap

// ---- safety state (read by loop, set by the watchdog task and the sensors) ----
enum EstopReason : uint8_t { ES_BUMP_L = 1, ES_BUMP_R = 2, ES_CLIFF_L = 4, ES_CLIFF_R = 8, ES_TILT = 16 };
static uint8_t estop = 0;                     // latched reasons (loop only); `clear` resets
static volatile uint32_t lastMotionMs = 0;
static volatile bool motionActive = false;
static volatile bool wdTripped = false;       // set by the watchdog, reported + cleared by loop
static volatile uint32_t wdTrips = 0;

static void motorPins(int side, float pct) {
  const int pwm = side ? PIN_PWMB : PIN_PWMA;
  const int in1 = side ? PIN_BIN1 : PIN_AIN1;
  const int in2 = side ? PIN_BIN2 : PIN_AIN2;
  if (pct == 0) {                              // short brake: IN1 = IN2 = high
    ledcWrite(pwm, 0);
    digitalWrite(in1, HIGH);
    digitalWrite(in2, HIGH);
    return;
  }
  bool fwd = pct > 0;
  digitalWrite(in1, fwd ? HIGH : LOW);
  digitalWrite(in2, fwd ? LOW : HIGH);
  ledcWrite(pwm, (uint32_t)(fabsf(pct) / 100.0f * MOTOR_MAX + 0.5f));
}

static float capPct(float pct) {
  float cap = min(cfg("pwm_cap"), PWM_CAP_HARD);
  return constrain(pct, -cap, cap);
}

static void motorsApply(float l, float r) {     // + = wheel forward; inv_* fixes the wiring
  xSemaphoreTake(motorLock, portMAX_DELAY);
  motorPct[0] = capPct(l);
  motorPct[1] = capPct(r);
  motorPins(0, cfg("inv_l") > 0.5f ? -motorPct[0] : motorPct[0]);
  motorPins(1, cfg("inv_r") > 0.5f ? -motorPct[1] : motorPct[1]);
  xSemaphoreGive(motorLock);
}

static void motorsBrake() {
  motionActive = false;
  motorsApply(0, 0);
}

// Signed % per wheel (+ = forward). Only called for a fresh motion command from the Pi.
static void motorsSet(float l, float r) {
  lastMotionMs = millis();
  motionActive = (l != 0 || r != 0);
  motorsApply(l, r);
}

static void watchdogTick(void *) {
  if (motionActive && millis() - lastMotionMs > (uint32_t)cfg("wd_ms")) {
    motorsBrake();
    wdTrips = wdTrips + 1;
    wdTripped = true;
  }
}

static void motorsInit() {
  motorLock = xSemaphoreCreateMutex();
  for (int p : {PIN_AIN1, PIN_AIN2, PIN_BIN1, PIN_BIN2}) {
    pinMode(p, OUTPUT);
    digitalWrite(p, HIGH);                     // brake from the first microsecond
  }
  ledcAttach(PIN_PWMA, MOTOR_HZ, MOTOR_BITS);
  ledcAttach(PIN_PWMB, MOTOR_HZ, MOTOR_BITS);
  motorsApply(0, 0);

  esp_timer_handle_t t;
  esp_timer_create_args_t args = {};
  args.callback = watchdogTick;
  args.name = "wd";
  esp_timer_create(&args, &t);
  esp_timer_start_periodic(t, 10 * 1000);     // 10 ms -> worst case wd_ms + 10 ms
}
