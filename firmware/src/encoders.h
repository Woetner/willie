// 2× quadrature encoder on the hardware pulse counter (PCNT), x4 decoding.
// The PCNT counts on its own, so no interrupt per edge and no missed counts when loop() is busy.
#pragma once
#include <driver/pulse_cnt.h>
#include "config.h"
#include "pins.h"

static pcnt_unit_handle_t encUnit[2];
static bool encOk = false;

static bool encoderInit(int side, int pinA, int pinB) {
  pcnt_unit_config_t u = {};
  u.low_limit = -30000;
  u.high_limit = 30000;
  u.flags.accum_count = 1;                     // keep counting past the limits (32-bit total)
  if (pcnt_new_unit(&u, &encUnit[side]) != ESP_OK) return false;
  pcnt_glitch_filter_config_t f = {.max_glitch_ns = 1000};
  pcnt_unit_set_glitch_filter(encUnit[side], &f);

  pcnt_chan_config_t ca = {}, cb = {};
  ca.edge_gpio_num = pinA; ca.level_gpio_num = pinB;
  cb.edge_gpio_num = pinB; cb.level_gpio_num = pinA;
  pcnt_channel_handle_t a, b;
  if (pcnt_new_channel(encUnit[side], &ca, &a) != ESP_OK) return false;
  if (pcnt_new_channel(encUnit[side], &cb, &b) != ESP_OK) return false;
  pcnt_channel_set_edge_action(a, PCNT_CHANNEL_EDGE_ACTION_DECREASE, PCNT_CHANNEL_EDGE_ACTION_INCREASE);
  pcnt_channel_set_level_action(a, PCNT_CHANNEL_LEVEL_ACTION_KEEP, PCNT_CHANNEL_LEVEL_ACTION_INVERSE);
  pcnt_channel_set_edge_action(b, PCNT_CHANNEL_EDGE_ACTION_INCREASE, PCNT_CHANNEL_EDGE_ACTION_DECREASE);
  pcnt_channel_set_level_action(b, PCNT_CHANNEL_LEVEL_ACTION_KEEP, PCNT_CHANNEL_LEVEL_ACTION_INVERSE);
  // accum_count needs the limits as watch points
  pcnt_unit_add_watch_point(encUnit[side], u.low_limit);
  pcnt_unit_add_watch_point(encUnit[side], u.high_limit);
  pcnt_unit_enable(encUnit[side]);
  pcnt_unit_clear_count(encUnit[side]);
  pcnt_unit_start(encUnit[side]);
  return true;
}

static void encodersInit() {
  encOk = encoderInit(0, PIN_ENC_LA, PIN_ENC_LB) && encoderInit(1, PIN_ENC_RA, PIN_ENC_RB);
}

// Signed ticks since boot (+ = wheel forward, after the einv_* flips).
static int32_t encoderTicks(int side) {
  if (!encOk) return 0;
  int v = 0;
  pcnt_unit_get_count(encUnit[side], &v);
  return cfg(side ? "einv_r" : "einv_l") > 0.5f ? -v : v;
}
