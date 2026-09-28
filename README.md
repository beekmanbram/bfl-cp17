# CP17 — Open Source Film Processor

CP17 is an open-source upgrade project for a JOBO film processor.

The goal is to reuse as much of the original JOBO mechanical hardware as possible, while replacing the original control electronics with modern, serviceable components.

## Original JOBO hardware reused

The current CP17 build reuses:

- Original JOBO drum drive mechanism
- Nidec 403.394 24 V DC motor
- Original water bath / processing tank
- Original heater element
- Original Heidolph 230/240 V circulation pump
- Original mechanical parts and enclosure where possible

The circulation pump is switched through a Finder 40.52 relay with 24 V DC coil.

## New CP17 hardware

The original electronics are replaced by:

- Raspberry Pi 4
- Waveshare 4" 800×480 HDMI display
- DFRobot DFR0601 H-bridge motor driver
- Rotary encoder
- Separate OK button
- Separate BACK button
- DS18B20 temperature sensor
- 24 V DC power supply
- New wiring, connectors and protection

The display is used without touchscreen functionality.

## Motor control

The original 24 V drum motor is controlled by the DFR0601.

Connections:

```
PWM  → GPIO12
INA  → GPIO6
INB  → GPIO13
```

Motor speed uses 20 kHz hardware PWM.

The motor automatically reverses direction with a short dead time between direction changes.

## Controls

```
Encoder A → GPIO16
Encoder B → GPIO20

OK button   → GPIO23 → GND
BACK button → GPIO24 → GND
```

The encoder is used only for rotation. Its push-button function is not used.

## Temperature sensor

A DS18B20 is currently used for bath-temperature monitoring.

```
VCC  → 3.3 V
GND  → GND
DATA → GPIO4
```

A 4.7 kΩ pull-up resistor is used between DATA and 3.3 V.

## Pump

The original Heidolph pump remains a 230/240 V AC device.

It is switched using:

- Finder 40.52.9.024.0000 relay
- Finder 95.05 base
- 24 V DC coil

Protective earth remains permanently connected and is never switched.

## Current status

The current prototype supports:

- Drum motor control
- Adjustable motor speed
- Automatic left/right rotation
- Bath temperature monitoring
- Physical encoder navigation
- Process timing
- Programmable development steps

## Roadmap

1. **Heater control**  
   Add automatic temperature regulation of the original JOBO heating element, including independent over-temperature protection.

2. **Temperature sensors in the development tank**  
   Add additional sensors to measure the actual chemistry temperature, not only the surrounding water bath.

3. **Automatic lift**  
   Add a motorised mechanism to automatically raise and lower the processing drum between process steps.

## Safety

CP17 combines water, 24 V electronics and 230 V mains power.

All mains wiring must be properly enclosed, fused and earthed. Protective earth must never be switched. The heater will require independent hardware over-temperature protection in addition to software control.

This project is currently a prototype and should only be reproduced by people comfortable working safely with mains-powered equipment.
