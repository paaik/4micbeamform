# reSpeaker XVF3800 direction tracker (Raspberry Pi 5)

One service that reads the XVF3800's beamformed azimuth and:

- turns a TowerPro SG90 pan servo so its horn points at the sound,
- tilts a Camera v2 on a second SG90 (0° to 90°): it scans for a drone and
  follows the one the PC detector finds,
- streams the camera as MJPEG (`/camera.mjpg`) and accepts detections from
  [`../drone_detector`](../drone_detector) (`POST /api/detections`),
- shows the azimuth and camera elevation on the Waveshare 1.69" ST7789 LCD
  (`../display_config`),
- serves the live 3D dashboard, with the camera feed and detection boxes, at
  `http://<pi>.local:8765/`.

Each part runs on its own thread. If the LCD, a servo or the camera is missing
or fails, the others keep running and the dashboard shows the error.

## Wiring (Raspberry Pi 5, physical header pins)

| Device | Wire | Pi pin | Signal |
|---|---|---|---|
| Pan SG90 | orange (signal) | **33** | GPIO13, hardware PWM0 channel 1 |
| Pan SG90 | brown (ground) | **34** | GND |
| Pan SG90 | red (+5 V) | **4** | 5 V (or an external 5 V supply, see below) |
| Tilt SG90 | orange (signal) | **32** | GPIO12, hardware PWM0 channel 0 |
| Tilt SG90 | brown (ground) | **30** | GND |
| Tilt SG90 | red (+5 V) | **2** | 5 V (or an external 5 V supply, see below) |
| Camera v2 | ribbon | CAM/DISP 0 | Pi 5 22-to-15 pin camera cable |
| LCD | VCC | 17 | 3.3 V |
| LCD | GND | 20 | GND |
| LCD | DIN | 19 | GPIO10 SPI0 MOSI |
| LCD | CLK | 23 | GPIO11 SPI0 SCLK |
| LCD | CS | 24 | GPIO8 SPI0 CE0 |
| LCD | DC | 22 | GPIO25 |
| LCD | RST | 13 | GPIO27 |
| LCD | BL | 18 | GPIO24 |

GPIO12 and GPIO13 drive the servos because, of the four RP1 pins with a
hardware PWM channel, GPIO18 and GPIO19 are the I2S clocks. Hardware PWM keeps
the 50 Hz pulse steady, so the SG90s don't twitch the way they do with
software-timed pulses. The LCD backlight only needs on/off, so it moved to
GPIO24 to free GPIO12 for the tilt servo.

**Servo power.** An SG90 draws a few hundred mA while moving and up to about
700 mA when stalled, and two of them can move at once. Pins 2 and 4 are fine
for a quick test. For normal use, power both red wires from a separate 5 V
supply whose ground is tied to Pi ground, so the signals have a shared
reference. A 470 µF capacitor across the servos' 5 V and ground also helps.

### Connecting the reSpeaker

- **USB firmware** (what the board runs now, v2.1.x): plug it into a Pi USB port.
  Direction is read over USB control transfers. No header pins are used.
- **I2S firmware** (32-bit, 48 kHz I2S master): control moves to I2C at address
  `0x2C`. Wire XVF3800 SDA to pin 3 (GPIO2), SCL to pin 5 (GPIO3) and GND to
  pin 9. The I2S lines go to BCLK pin 12 (GPIO18), LRCLK pin 35 (GPIO19),
  XVF3800 data out to pin 38 (GPIO20) and data in to pin 40 (GPIO21). The
  tracker itself only needs I2C; the I2S pins are left free for your audio
  capture.

`transport = "auto"` in `doa_tracker.toml` tries USB first, then I2C.

## Install on the Pi

[`../deploy/README.md`](../deploy/README.md) walks through the whole
deployment step by step: flashing the OS, copying the project, wiring, running
`deploy/install_pi.sh`, checking with `--check`, calibrating the servo,
updating and troubleshooting. The short version:

```bash
cd ~/python_control
sudo bash deploy/install_pi.sh
sudo reboot
.venv/bin/python -m doa_tracker --check
```

## Calibrate the servos

See step 7 of [`../deploy/README.md`](../deploy/README.md). For the tilt servo,
`--servo-test tilt` sweeps the elevation range, `level_servo_deg` is the servo
angle that holds the camera level, and `direction = -1` reverses it. For the
pan servo:
`--servo-test` sweeps the servo, `center_azimuth_deg` sets the azimuth the
horn faces at servo 90°, and `direction = -1` reverses it. With
`out_of_range = "flip"`, the horn's rear end points at sounds the front end
can't reach, which covers the full 360°. The dashboard's 0°
mark is the array's +X axis, and angles increase toward +Y.

## Run on a PC (dashboard only)

```bash
python doa_dashboard.py                      # Windows/macOS/Linux, opens a browser
python -m doa_tracker --no-servo --no-tilt --no-camera --no-lcd    # same thing with all options
```
