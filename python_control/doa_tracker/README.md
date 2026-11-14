# reSpeaker XVF3800 direction tracker (Raspberry Pi 5)

One service that reads the XVF3800's beamformed azimuth and:

- turns a TowerPro SG90 servo so its horn points at the sound,
- shows the azimuth on the Waveshare 1.69" ST7789 LCD (`../display_config`),
- serves the live 3D dashboard at `http://<pi>.local:8765/`.

Each part runs on its own thread. If the LCD or servo is missing or fails, the
others keep running and the dashboard shows the error.

## Wiring (Raspberry Pi 5, physical header pins)

| Device | Wire | Pi pin | Signal |
|---|---|---|---|
| SG90 servo | orange (signal) | **33** | GPIO13, hardware PWM0 channel 1 |
| SG90 servo | brown (ground) | **34** | GND |
| SG90 servo | red (+5 V) | **4** | 5 V (or an external 5 V supply, see below) |
| LCD | VCC | 17 | 3.3 V |
| LCD | GND | 20 | GND |
| LCD | DIN | 19 | GPIO10 SPI0 MOSI |
| LCD | CLK | 23 | GPIO11 SPI0 SCLK |
| LCD | CS | 24 | GPIO8 SPI0 CE0 |
| LCD | DC | 22 | GPIO25 |
| LCD | RST | 13 | GPIO27 |
| LCD | BL | 32 | GPIO12 |

GPIO13 was picked for the servo because it is one of the four RP1 pins with a
hardware PWM channel. The others are taken: GPIO12 drives the LCD backlight, and
GPIO18 and GPIO19 are the I2S clocks. Hardware PWM keeps the 50 Hz pulse steady,
so the SG90 does not twitch the way it does with software-timed pulses. Pin 34
is the ground right next to the signal pin.

**Servo power.** An SG90 draws a few hundred mA while moving and up to about
700 mA when stalled. Pin 4 is fine with the official 27 W Pi 5 supply. If the Pi
resets, the LCD flickers or USB audio drops out when the servo moves, power the
red wire from a separate 5 V supply instead. Keep the brown wire tied to Pi
ground (pin 34) so the signal has a shared reference. A 470 µF capacitor across
the servo's 5 V and ground also helps.

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

## Calibrate the servo

See step 7 of [`../deploy/README.md`](../deploy/README.md). In short:
`--servo-test` sweeps the servo, `center_azimuth_deg` sets the azimuth the
horn faces at servo 90°, and `direction = -1` reverses it. The dashboard's 0°
mark is the array's +X axis, and angles increase toward +Y.

## Run on a PC (dashboard only)

```bash
python doa_dashboard.py                      # Windows/macOS/Linux, opens a browser
python -m doa_tracker --no-servo --no-lcd    # same thing with all options
```
