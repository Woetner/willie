// ESP32-WROOM-32 DevKit (30-pin) pin map. Must match WILL-E.md §5.2.
// Avoided: 6-11 (flash), 0/2/5/12/15 (strapping; 2 = on-board LED only), 14 (PWM glitch at boot).
#pragma once

#define PIN_LED        2

#define PIN_LINK_TX    17
#define PIN_LINK_RX    16

// TB6612FNG, STBY tied to 3.3 V (§5.2)
#define PIN_PWMA       25   // left motor
#define PIN_AIN1       26
#define PIN_AIN2       27
#define PIN_PWMB       23   // right motor (was 13: D13 is shorted to D26 on this ESP32 board, 1 Oct)
#define PIN_BIN1       32
#define PIN_BIN2       33

// encoders via the level shifter; input-only pins, the shifter provides the pull-ups
#define PIN_ENC_LA     34
#define PIN_ENC_LB     35
#define PIN_ENC_RA     36   // VP
#define PIN_ENC_RB     39   // VN

#define PIN_SERVO_PAN  18
#define PIN_SERVO_TILT 19

#define PIN_SDA        21
#define PIN_SCL        22

// PCF8574 (0x20) bits, as wired 28 Sep (WILL-E.md §13). Every VL53L0X has its own XSHUT.
#define IO_XSHUT_CL    0    // cliff ToF left   -> 0x33
#define IO_XSHUT_L     1    // front ToF left   -> 0x30
#define IO_XSHUT_C     2    // front ToF centre -> 0x31
#define IO_XSHUT_R     3    // front ToF right  -> 0x32
#define IO_XSHUT_CR    4    // cliff ToF right  -> 0x34
#define IO_BUMP_L      5    // micro switch NO+COM to GND: pressed = low
#define IO_BUMP_R      6
#define IO_LED         7    // camera LED, active low (PCF8574 can sink, not source)
