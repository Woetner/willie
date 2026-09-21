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
#define PIN_PWMB       13   // right motor
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

// PCF8574 (0x20) bits
#define IO_XSHUT_L     0    // ToF left   -> 0x30
#define IO_XSHUT_C     1    // ToF centre -> 0x31   (ToF right has no XSHUT, -> 0x32)
#define IO_CLIFF_L     2
#define IO_CLIFF_R     3
#define IO_BUMP_L      4    // micro switch NO+COM to GND: pressed = low
#define IO_BUMP_R      5
#define IO_LED         6    // camera LED, active low (PCF8574 can sink, not source)
