// Wheel speed control (F2): closed loop per wheel on the encoders, 50 Hz, in an esp_timer task
// like the watchdog, so a busy loop() does not change its timing.
//
// drive v w -> a target speed per wheel (mm/s). Output per wheel, in duty %:
//   feedforward (target / v_full * 100)  +  kp * e  +  ki * integral(e)  +  kd * de/dt
// with e = the speed error expressed in duty % (e = (target - measured) / v_full * 100), so the
// gains have no units: kp 1 doubles the correction the feedforward would give for that error.
//
// Safety stays where it was (motors.h): every output goes through motorsApply() = the PWM cap,
// and only a fresh command from the Pi refreshes the watchdog; this loop never does. When the
// watchdog or an estop brakes (motionActive false), the loop resets and stays quiet.
// Sign guard: a wheel turning the wrong way while the loop pushes hard means a mirrored encoder
// (einv_*); the loop then switches itself off (open loop again) and reports `err pid_sign`.
#pragma once
#include <esp_timer.h>
#include "config.h"
#include "motors.h"
#include "encoders.h"

static const uint32_t WHEEL_PERIOD_US = 20000;          // 50 Hz
static volatile bool wheelsClosed = false;              // a `drive` target is being tracked
static volatile bool pidSignFault = false;              // set by the task, reported by loop()
static float wheelTarget[2] = {0, 0};                   // mm/s, + = forward
static float wheelSpeed[2] = {0, 0};                    // mm/s, filtered
static float wheelInt[2] = {0, 0}, wheelErrPrev[2] = {0, 0};
static int32_t wheelTicksPrev[2] = {0, 0};
static uint32_t wheelWrongMs[2] = {0, 0};

static void wheelsReset() {
  for (int i = 0; i < 2; i++) wheelInt[i] = wheelErrPrev[i] = 0, wheelWrongMs[i] = 0;
}

// Called by `drive`: open loop with the feedforward at once, the task refines it.
static void wheelsDrive(float left_mms, float right_mms) {
  float full = cfg("v_full");
  bool closed = cfg("pid_on") > 0.5f && encOk;
  if (!closed || (left_mms == 0 && right_mms == 0)) wheelsReset();
  wheelTarget[0] = left_mms;
  wheelTarget[1] = right_mms;
  wheelsClosed = closed && (left_mms != 0 || right_mms != 0);
  motorsSet(left_mms / full * 100, right_mms / full * 100);   // refreshes the watchdog
}

// `pwm` (bench) and `stop` leave closed loop.
static void wheelsOpen() {
  wheelsClosed = false;
  wheelsReset();
}

static void wheelsTick(void *) {
  const float dt = WHEEL_PERIOD_US / 1e6f;
  const float mmPerTick = PI * cfg("wheel_d") / cfg("cpr");
  float out[2];
  for (int i = 0; i < 2; i++) {
    int32_t t = encoderTicks(i);
    float raw = (t - wheelTicksPrev[i]) * mmPerTick / dt;
    wheelTicksPrev[i] = t;
    wheelSpeed[i] += 0.5f * (raw - wheelSpeed[i]);        // light low-pass: ~9 ticks per period
  }
  if (!wheelsClosed || !motionActive) {
    wheelsReset();
    return;
  }
  const float full = cfg("v_full"), cap = min(cfg("pwm_cap"), PWM_CAP_HARD);
  const uint32_t now = millis();
  for (int i = 0; i < 2; i++) {
    float e = (wheelTarget[i] - wheelSpeed[i]) / full * 100;
    float d = (e - wheelErrPrev[i]) / dt;
    wheelErrPrev[i] = e;
    float ff = wheelTarget[i] / full * 100;
    float u = ff + cfg("kp") * e + cfg("ki") * wheelInt[i] + cfg("kd") * d;
    if (fabsf(u) < cap) wheelInt[i] += e * dt;             // anti-windup: no integrating at the cap
    out[i] = u;
    bool wrong = wheelTarget[i] * wheelSpeed[i] < 0 && fabsf(wheelSpeed[i]) > 30 && fabsf(u) >= cap * 0.9f;
    wheelWrongMs[i] = wrong ? (wheelWrongMs[i] ? wheelWrongMs[i] : now) : 0;
    if (wrong && now - wheelWrongMs[i] > 300) {
      wheelsClosed = false;
      pidSignFault = true;
      setCfg("pid_on", 0);
      motorsApply(wheelTarget[0] / full * 100, wheelTarget[1] / full * 100);   // open loop again
      return;
    }
  }
  motorsApply(out[0], out[1]);
}

static void wheelsInit() {
  for (int i = 0; i < 2; i++) wheelTicksPrev[i] = encoderTicks(i);
  esp_timer_handle_t t;
  esp_timer_create_args_t args = {};
  args.callback = wheelsTick;
  args.name = "wheels";
  esp_timer_create(&args, &t);
  esp_timer_start_periodic(t, WHEEL_PERIOD_US);
}
