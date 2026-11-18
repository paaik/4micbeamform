# Deploying the direction tracker on a Raspberry Pi 5

This guide takes a fresh Raspberry Pi 5 to a running tracker that starts at
boot. Once running, it:

- turns the SG90 servo toward the sound,
- shows the azimuth on the Waveshare 1.69" LCD,
- serves the live 3D dashboard on your network.

Expect about 30 minutes, most of it package installs.

## What you need

- Raspberry Pi 5 with the official 27 W USB-C power supply. The servo and the
  reSpeaker both draw from the Pi.
- microSD card (16 GB or more) and a computer with
  [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
- reSpeaker XVF3800 4-mic array and a USB cable.
- Waveshare 1.69" LCD (ST7789) with its 8-wire cable.
- TowerPro SG90 servo and three female-to-female jumper wires.
- Optional: a separate 5 V supply for the servo, plus a 470 µF capacitor (see
  step 3).

## 1. Install Raspberry Pi OS

1. In Raspberry Pi Imager choose **Raspberry Pi 5**, then **Raspberry Pi OS
   (64-bit)**. It must be Bookworm or newer; the tracker needs Python 3.11+.
2. Open **Edit settings** before writing the card:
   - Set a hostname, for example `respeaker`. The dashboard address will be
     `http://respeaker.local:8765/`.
   - Create your user account. Don't use `root`; the service runs as this user.
   - Enter your Wi-Fi details, or plan to use Ethernet.
   - Under **Services**, enable SSH.
3. Write the card, put it in the Pi, power it on and wait a minute or two.
4. From your computer, connect:

   ```bash
   ssh <user>@respeaker.local
   ```

   If `.local` names don't resolve on your network, look up the Pi's IP address
   in your router and use that instead.

## 2. Copy the project to the Pi

Copy the whole `python_control` folder, including `display_config`, into your
home directory on the Pi. From the repository folder on your computer
(Windows PowerShell, macOS or Linux):

```bash
scp -r python_control <user>@respeaker.local:~/
```

On the Pi, `ls ~/python_control` should list `doa_tracker`, `deploy`,
`display_config`, `xvf_host.py` and `doa_tracker.toml`.

The files in `deploy/` must keep Unix line endings. They already do, and
`deploy/.gitattributes` keeps them that way in git. If `install_pi.sh` fails
with `$'\r': command not found`, run `sed -i 's/\r$//' deploy/*` on the Pi.

## 3. Wire the hardware

Shut the Pi down before wiring (`sudo poweroff`), then unplug the power.

Pin numbers are **physical header pins**: pin 1 is the corner pin nearest the
microSD card slot, odd pins run along the inner row and even pins along the
board edge.

| Part | Wire | Pi pin | Signal |
|---|---|---|---|
| SG90 servo | orange (signal) | 33 | GPIO13, hardware PWM |
| SG90 servo | brown (ground) | 34 | GND |
| SG90 servo | red (+5 V) | 4 | 5 V |
| LCD | VCC | 17 | 3.3 V |
| LCD | GND | 20 | GND |
| LCD | DIN | 19 | GPIO10, SPI0 MOSI |
| LCD | CLK | 23 | GPIO11, SPI0 SCLK |
| LCD | CS | 24 | GPIO8, SPI0 CE0 |
| LCD | DC | 22 | GPIO25 |
| LCD | RST | 13 | GPIO27 |
| LCD | BL | 32 | GPIO12 |

Connect the reSpeaker according to its firmware:

- **USB firmware**: plug it into one of the Pi's USB ports. No header pins
  are needed. This is the firmware the board shipped with.
- **I2S firmware** (32-bit, 48 kHz): connect the reSpeaker's SDA to pin 3,
  SCL to pin 5 and GND to pin 9. The tracker reads direction over I2C. If you
  also capture I2S audio, those lines go to pins 12 (BCLK), 35 (LRCLK),
  38 (reSpeaker data out) and 40 (reSpeaker data in).

**Servo power.** Pin 4 is fine for testing. If the Pi reboots, the LCD
flickers, or the reSpeaker drops off USB when the servo moves, power the red
wire from a separate 5 V supply instead. Keep the brown wire on pin 34 so the
servo and Pi share a ground. A 470 µF capacitor across the servo's 5 V and
ground also helps.

## 4. Run the installer

Power the Pi back on, SSH in, and run:

```bash
cd ~/python_control
sudo bash deploy/install_pi.sh
```

Run it with `sudo` from your normal account, not from a root shell. It needs
to know which user will own the service. The installer:

1. installs system packages (Python tools, lgpio, spidev, numpy, libusb,
   fonts, i2c-tools),
2. enables SPI for the LCD and I2C for the reSpeaker's I2S firmware,
3. adds `dtoverlay=pwm,pin=13,func=4` to `/boot/firmware/config.txt`, which
   routes GPIO13 to hardware PWM,
4. creates a Python environment in `~/python_control/.venv` and installs the
   Python packages,
5. installs a udev rule so your user can talk to the reSpeaker over USB without
   `sudo`,
6. adds your user to the `gpio`, `spi`, `i2c` and `plugdev` groups,
7. installs and enables `doa-tracker.service` so the tracker starts at boot.

Then reboot so SPI, I2C, the PWM overlay and the new group memberships take
effect:

```bash
sudo reboot
```

The installer is safe to run again, for example after updating the project.

## 5. Check everything is reachable

After the reboot, SSH in again and run:

```bash
cd ~/python_control
.venv/bin/python -m doa_tracker --check
```

A healthy setup looks roughly like this:

```text
[--  ] python     3.11.2 on Linux-6.6...-aarch64
[ok  ] reSpeaker  USB firmware 2.1.1, DOA 207°, speech 0
[ok  ] lcd        /dev/spidev0.0 present
[ok  ] servo      GPIO13 -> /sys/class/pwm/pwmchip0/pwm1 (writable)
[ok  ] pin mux    13: a0 pd | lo // GPIO13 = PWM0_CHAN1
```

The `pwmchip` number depends on the kernel version. Any `FAIL` line is
explained in the troubleshooting section below.

## 6. Confirm the service is running

The service starts by itself after the reboot:

```bash
systemctl status doa-tracker
journalctl -u doa-tracker -f
```

The log should show lines like `Connected to the XVF3800 over USB`,
`Servo on GPIO13 via hardware PWM`, `LCD ready (240x280)` and `Dashboard at
http://respeaker.local:8765/`. Every 10 seconds there is a status line with the
current direction and poll rate. Press Ctrl+C to stop following the log; the
service keeps running.

Open `http://respeaker.local:8765/` in a browser on any device on the same
network. The LCD should show the azimuth, and the servo should centre and then
follow speech.

## 7. Calibrate the servo

An SG90 covers 180°, so it can only face half of the array's 360°. Calibration
sets which half that is. Run these commands from `~/python_control`.

1. Stop the service so it doesn't fight the test:

   ```bash
   sudo systemctl stop doa-tracker
   ```

2. Sweep the servo:

   ```bash
   sudo .venv/bin/python -m doa_tracker --servo-test
   ```

   It moves to 90, 0, 90, 180 and back to 90 degrees, printing the pulse width
   for each. It needs `sudo` because the PWM channel is only handed to your
   user while the service is starting. If the servo buzzes or strains at either end, open
   `doa_tracker.toml` and raise `min_pulse_us` or lower `max_pulse_us` by about
   100 µs, then sweep again.

3. Leave the servo at 90° (where the sweep ends). Fit the horn so it faces the
   direction you care about most.

4. Find that direction's azimuth. Start the service
   (`sudo systemctl start doa-tracker`), stand where the horn points and talk.
   Note the angle on the LCD or dashboard. In `doa_tracker.toml`, set
   `center_azimuth_deg` to that angle.

5. Talk from one side. If the horn turns away from you, set `direction = -1`.

6. Apply the changes:

   ```bash
   sudo systemctl restart doa-tracker
   ```

For sounds behind the servo, `out_of_range = "clamp"` (the default) holds the
horn at the nearer end. `"mirror"` points it at the same angle reflected to
the front instead.


## Day-to-day use

| Task | Command |
|---|---|
| Restart after editing `doa_tracker.toml` | `sudo systemctl restart doa-tracker` |
| Stop / start | `sudo systemctl stop doa-tracker` / `sudo systemctl start doa-tracker` |
| Don't start at boot | `sudo systemctl disable doa-tracker` |
| Follow the log | `journalctl -u doa-tracker -f` |
| Run in the foreground for debugging | stop the service, then `.venv/bin/python -m doa_tracker --verbose` |

LCD orientation, offsets and SPI speed are set in `display_config/config.py`,
not in `doa_tracker.toml`.

### Updating the project

Copy the new `python_control` folder over the old one (step 2), then:

```bash
cd ~/python_control
sudo bash deploy/install_pi.sh
sudo systemctl restart doa-tracker
```

Copying over the old folder keeps `.venv` and `cloud.env`. Back up your `doa_tracker.toml`
before copying if you've changed it.

## Troubleshooting

**`reSpeaker  No XVF3800 on USB`**
Run `lsusb | grep 2886`. If nothing is listed, check the cable and use a
different USB port. If the board runs the I2S firmware, set
`transport = "i2c"` in `doa_tracker.toml` and check the I2C wiring with
`i2cdetect -y 1`. It should show `2c`.

**`XVF3800 found on USB but not readable ... udev rule`**
The permission rule hasn't applied yet. Unplug and replug the reSpeaker, or
reboot. Run `groups` and confirm `plugdev` is listed.

**`lcd  /dev/spidev0.0 missing`**
SPI isn't enabled. Run `sudo raspi-config nonint do_spi 0` and reboot.

**LCD stays black, or the picture is shifted**
Check the BL wire is on pin 32. See the tuning table in
`display_config/README.md` for `Y_OFFSET`, `ROTATION` and `SPI_BAUDRATE`.

**`--check` says `no hardware PWM for GPIO13`, or the dashboard shows the
servo using "lgpio software PWM"**
The PWM overlay isn't loaded, so the servo falls back to software pulses and
twitches. Check that `/boot/firmware/config.txt` ends with
`dtoverlay=pwm,pin=13,func=4` under `[all]`, then reboot. If the installer
warned about a different `dtoverlay=pwm,` line, edit that line to match.

**The Pi reboots or USB drops out when the servo moves**
The servo is pulling the 5 V rail down. Power it from a separate 5 V supply,
keeping the ground on pin 34 (see step 3).

**The dashboard doesn't load**
Use the IP address from `hostname -I` instead of the `.local` name. Check that
`host = "0.0.0.0"` in `doa_tracker.toml` and that nothing else is using port
8765.

**The service keeps restarting**
Run `journalctl -u doa-tracker -n 50` to see the error. `Bad settings` means a
typo in `doa_tracker.toml`; the message names the setting.

**`cloud  HTTP 401` or `HTTP 403`**
The key is wrong, or Row Level Security is blocking the anon key. Check the
key in `cloud.env` and add the insert policy from step 8.

**`cloud  HTTP 404`**
The table name in `doa_tracker.toml` does not match the table in Supabase.

**`cloud  HTTP 400` mentioning a column**
A column is missing or misspelled; create the table with the SQL in step 8.

**`cloud  cannot reach ...`**
The Pi has no internet, or `url` has a typo. Try `curl -I <url>` on the Pi.

## Uninstalling

```bash
sudo systemctl disable --now doa-tracker
sudo rm /etc/systemd/system/doa-tracker.service /etc/udev/rules.d/99-respeaker-xvf3800.rules
sudo systemctl daemon-reload
```

Then remove the `dtoverlay=pwm,pin=13,func=4` line from
`/boot/firmware/config.txt`, reboot, and delete `~/python_control`.
