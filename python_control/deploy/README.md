# Deploying the direction tracker on a Raspberry Pi 5

This guide takes a fresh Raspberry Pi 5 to a running tracker that starts at
boot. Once running, it:

- pans a mini pan-tilt kit toward the sound with one SG90 servo,
- tilts the Camera v2 on the kit with a second SG90, scanning for a drone
  between 0° and 90° elevation and following it once it's found,
- shows the azimuth and camera elevation on the Waveshare 1.69" LCD,
- serves the live 3D dashboard and the camera feed on your network,
- sends the camera feed to a PC, which runs the drone detection and sends the
  results back to steer the tilt,
- streams a live spectrogram from a USB measurement microphone to the
  dashboard, and flags a drone when it hears a propeller blade-pass frequency
  (step 9).

Expect about 45 minutes, most of it package installs.

## What you need

- Raspberry Pi 5 with the official 27 W USB-C power supply.
- microSD card (16 GB or more) and a computer with
  [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
- reSpeaker XVF3800 4-mic array and a USB cable.
- Waveshare 1.69" LCD (ST7789) with its 8-wire cable.
- Mini pan-tilt kit with two TowerPro SG90 servos (pan and tilt), and six
  female-to-female jumper wires.
- Raspberry Pi Camera Module v2 and a **Raspberry Pi 5 camera cable**. The
  Pi 5 has smaller 22-pin camera connectors, so the 15-pin cable that comes
  with the camera doesn't fit.
- A separate 5 V supply (2 A or more) for the two servos is strongly
  recommended, plus a 470 µF capacitor (see step 3).
- A PC on the same network to run the drone detection, ideally with an NVIDIA
  GPU, and YOLO weights trained to detect drones (step 8).
- Optional: a USB measurement microphone for the spectrogram and the acoustic
  drone flag (step 9).

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
| Pan servo (base) | orange (signal) | 33 | GPIO13, hardware PWM0 channel 1 |
| Pan servo | brown (ground) | 34 | GND |
| Pan servo | red (+5 V) | 4 | 5 V |
| Tilt servo (camera) | orange (signal) | 32 | GPIO12, hardware PWM0 channel 0 |
| Tilt servo | brown (ground) | 30 | GND |
| Tilt servo | red (+5 V) | 2 | 5 V |
| LCD | VCC | 17 | 3.3 V |
| LCD | GND | 20 | GND |
| LCD | DIN | 19 | GPIO10, SPI0 MOSI |
| LCD | CLK | 23 | GPIO11, SPI0 SCLK |
| LCD | CS | 24 | GPIO8, SPI0 CE0 |
| LCD | DC | 22 | GPIO25 |
| LCD | RST | 13 | GPIO27 |
| LCD | BL | 18 | GPIO24 |

If you wired the LCD for an earlier version of this guide, move its **BL**
wire from pin 32 to pin 18. Pin 32 now drives the tilt servo, because GPIO12
and GPIO13 are the only two hardware PWM pins not reserved for I2S audio.

**Camera.** Connect the Camera v2 to the Pi's **CAM/DISP 0** connector with
the Pi 5 camera cable. Lift the connector latch, insert the cable, and press
the latch closed. Raspberry Pi's
[camera installation guide](https://www.raspberrypi.com/documentation/accessories/camera.html#install-a-raspberry-pi-camera)
shows which way the contacts face. Mount the camera on the tilt plate of the
pan-tilt kit. This camera's cable leaves it mounted upside down, so the shipped
`doa_tracker.toml` sets `hflip` and `vflip` to `true`. That rotates the picture
180° before it is streamed, so the dashboard, the detector and the servo angles
all see an upright image. Set both back to `false` if a later mount is upright.

Connect the reSpeaker according to its firmware:

- **USB firmware**: plug it into one of the Pi's USB ports. No header pins
  are needed. This is the firmware the board shipped with.
- **I2S firmware** (32-bit, 48 kHz): connect the reSpeaker's SDA to pin 3,
  SCL to pin 5 and GND to pin 9. The tracker reads direction over I2C. If you
  also capture I2S audio, those lines go to pins 12 (BCLK), 35 (LRCLK),
  38 (reSpeaker data out) and 40 (reSpeaker data in).

**Servo power.** Pins 2 and 4 are fine for a quick test. Two SG90s moving at
once can draw over 1 A, though, so for normal use power both red wires from a
separate 5 V supply. Connect that supply's ground to the brown wires and to a
Pi ground pin, so the servos and the Pi share a ground. A 470 µF capacitor
across the servos' 5 V and ground also helps. If the Pi reboots, the LCD
flickers, or the reSpeaker or camera drops out when the servos move, the 5 V
rail is sagging.

## 4. Run the installer

Power the Pi back on, SSH in, and run:

```bash
cd ~/python_control
sudo bash deploy/install_pi.sh
```

Run it with `sudo` from your normal account, not from a root shell. It needs
to know which user will own the service. The installer:

1. installs system packages (Python tools, lgpio, spidev, numpy, Picamera2,
   libusb, PortAudio for the measurement microphone, fonts, i2c-tools,
   alsa-utils),
2. enables SPI for the LCD and I2C for the reSpeaker's I2S firmware,
3. adds `dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4` to
   `/boot/firmware/config.txt`, which routes GPIO12 (tilt) and GPIO13 (pan) to
   hardware PWM. If an earlier install added the single-servo
   `dtoverlay=pwm,pin=13,func=4` line, it's replaced,
4. creates a Python environment in `~/python_control/.venv` and installs the
   Python packages,
5. installs a udev rule so your user can talk to the reSpeaker over USB without
   `sudo`,
6. adds your user to the `gpio`, `spi`, `i2c`, `plugdev`, `video` and `audio`
   groups,
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
[ok  ] camera     imx219 on ...
[ok  ] pan servo  GPIO13 -> /sys/class/pwm/pwmchip0/pwm1 (writable)
[ok  ] pin mux    13: a0 pd | lo // GPIO13 = PWM0_CHAN1
[ok  ] tilt servo GPIO12 -> /sys/class/pwm/pwmchip0/pwm0 (writable)
[ok  ] pin mux    12: a0 pd | lo // GPIO12 = PWM0_CHAN0
[ok  ] mic        USB Microphone: Audio (hw:2,0) (device 1, 48000 Hz)
```

Without a measurement microphone plugged in, the `mic` line reads `--` and
lists the inputs it can see. That isn't a failure; the tracker starts using
the mic as soon as you plug it in.

You can also list the camera directly with `rpicam-hello --list-cameras`.

The `pwmchip` number depends on the kernel version. Any `FAIL` line is
explained in the troubleshooting section below.

## 6. Confirm the service is running

The service starts by itself after the reboot:

```bash
systemctl status doa-tracker
journalctl -u doa-tracker -f
```

The log should show lines like these:

- `Connected to the XVF3800 over USB`
- `Servo on GPIO13 via hardware PWM`
- `Tilt servo on GPIO12 via hardware PWM`
- `Camera imx219 streaming 1280x960 at 15 fps`
- `Measurement microphone 'USB Microphone: Audio (hw:2,0)' at 48000 Hz; ...`
  (once a USB microphone is plugged in)
- `LCD ready (240x280)`
- `Dashboard at http://respeaker.local:8765/`

Every 10 seconds there is a status line with the current direction and poll
rate. Press Ctrl+C to stop following the log; the service keeps running.

Open `http://respeaker.local:8765/` in a browser on any device on the same
network. The camera feed is in the top-right corner of the 3D view; click it
to enlarge it. The LCD should show the azimuth, the pan servo should follow
speech, and the tilt servo should step the camera through 15°, 45° and 75°.
Until the PC detector from step 8 is running, the tilt just keeps scanning.

## 7. Calibrate the servos

### Pan servo

An SG90 only turns 180°, so the camera covers half the circle: azimuths 270°
through 0° to 90°, with servo 90° facing 0°. Inside that half, the pan
follows the sound until the camera finds the drone, then follows the drone's
position in the camera image. When the target is outside that half, the
camera parks facing 0° and waits until it comes back into range. Calibration
sets which direction counts as 0°. Run these commands from `~/python_control`.

1. Stop the service so it doesn't fight the test:

   ```bash
   sudo systemctl stop doa-tracker
   ```

2. Sweep the servo:

   ```bash
   sudo .venv/bin/python -m doa_tracker --servo-test
   ```

   It turns the horn slowly (30°/s) through azimuths 0°, 90° and 180°
   counter-clockwise, then clockwise back through 90° and 0° to 270° and 180°.
   It aims exactly as the tracker does. Just past 90° and 270° it logs "out of
   range; parking at the centre" and the camera returns to servo 90°, waiting
   there until the sweep comes back into range. At each stop it prints the
   servo angle, pulse width and where the camera points. It needs
   `sudo` because the PWM channel is only handed to your user while the
   service is starting. If the servo buzzes or strains at either end, open
   `doa_tracker.toml` and raise `min_pulse_us` or lower `max_pulse_us` by
   about 100 µs, then run it again.

3. With the default `center_azimuth_deg = 0`, the test starts and ends with
   the servo at 90°. Fit the pan-tilt kit so the camera looks along the
   array's 0° mark (+X axis) at that position.

4. Check the direction. Start the service (`sudo systemctl start doa-tracker`),
   stand in front of the camera and talk. The LCD or dashboard should read
   close to 0°. If it doesn't, set `center_azimuth_deg` in
   `doa_tracker.toml` to the angle it shows; the camera then covers that angle
   ±90° and parks facing it.

5. Talk from one side. If the camera turns away from you, set
   `direction = -1`.

6. Apply the changes:

   ```bash
   sudo systemctl restart doa-tracker
   ```

The `out_of_range` setting in `doa_tracker.toml` controls what happens when
the target is outside the camera's half:

- `"park"` (the default): the camera returns to servo 90° (facing
  `center_azimuth_deg`) and waits. The LCD footer reads `servo parked`, and
  the dashboard shows "Sound is outside the camera's range; parked". To stop
  it jumping back and forth at the edge, it only parks once the target is 10°
  past 90° or 270°, and resumes as soon as the target is back inside.
- `"clamp"`: the camera stays at the nearer end stop.
- `"mirror"`: the camera points at the same angle reflected to the front.
- `"flip"`: treats the horn as double-ended, so the rear end points at
  sounds behind it. The camera faces away from those sounds, so this is only
  useful without the camera.

`camera_assist = true` (the default) hands the pan over from the sound
direction to the camera once the tilt locks onto a drone. The LCD footer then
shows `cam`, the dashboard adds "· camera" after the servo angle, and the log
says "Pan servo now following the drone in the camera image". If the camera
loses the drone for `lost_after_s`, the pan goes back to following the sound.
Set it to `false` to steer the pan by sound only.

The camera is not at the array centre: `camera_forward_cm` (7.5) is how far it
sits along 0°, and `camera_right_cm` (5) is how far toward 270°, which is to
the right when looking along 0°. While the pan follows the sound, it aims along
the line from the camera to a point `sound_range_m` (10 m) out on the array's
azimuth, so a sound straight ahead is aimed slightly left of 0°. Once the
camera locks on, it follows the drone in the image and this offset is not
applied again. The shift is under 1° at 10 m, so it only moves the horn once it
exceeds `deadband_deg`, which happens inside about 2 m. Set `sound_range_m` to
0 to ignore the offset, or to the range you expect.

### Tilt servo

The tilt servo's angle that holds the camera level depends on how the kit was
assembled. `level_servo_deg` is that angle. `direction` says which way the
servo turns to tilt the camera up.

1. With the service stopped, sweep the tilt servo:

   ```bash
   sudo .venv/bin/python -m doa_tracker --servo-test tilt
   ```

   It moves to elevation 0°, 45°, 90°, 45°, 0°, and prints the servo angle for
   each.

2. At elevation 0° the camera should look level at the horizon. If it doesn't,
   change `level_servo_deg` in the `[tilt]` section of `doa_tracker.toml` and
   sweep again.
3. If the camera tilts down instead of up, set `direction = -1`.
4. If the kit's frame stops the camera before it points straight up, lower
   `max_elevation_deg` to where it stops. The settings check refuses any range
   that would need a servo angle outside 0-180°. If it complains, adjust
   `level_servo_deg` or `direction`.

`scan_elevations`, `scan_dwell_s` and the speeds in `[tilt]` control the
search pattern. The Camera v2 sees about 49° vertically, so the default stops
at 15°, 45° and 75° cover 0° to 90° with some overlap.

## 8. Run the drone detector on the PC

Detection runs on a PC on the same network, not on the Pi. The PC reads the
camera feed from `http://raspberrypi.local:8765/camera.mjpg`. It runs the
YOLOv8 weights in `camera_detect/best.pt` on the newest frame and posts the
boxes back to the Pi. The Pi turns each box into an azimuth and elevation from
the pan and tilt pose when that frame was taken. The tilt servo follows the
elevation and, with `camera_assist`, the pan servo follows the azimuth, which
brings the drone to the centre of the frame. The boxes also appear on the
dashboard's camera view.

If a frame contains more than one box, the servos follow the one with the
higher confidence. Every accepted sample is appended to `logs/drone_track.csv`
(`time, frame, azimuth_deg, elevation_deg, confidence, label`), and the same
angles are printed by the detector and the tracker whenever they move by 0.5°.

The weights are a YOLOv8 model with two classes, `copter_drone` and
`motor_drone`. Both count as the drone. The servos follow whichever box has
the higher confidence.

Run it from Windows. The `drone-yolov8` conda env is the miniforge env inside
WSL (`~/miniforge3`), and `camera_detect\run_tracker.ps1` activates it there:

```powershell
powershell -File camera_detect\run_tracker.ps1
powershell -File camera_detect\run_tracker.ps1 http://192.168.1.50:8765
```

WSL does not resolve `raspberrypi.local`, so the script looks that name up on
Windows and passes the IP into WSL. From a WSL shell the same command is:

```bash
source ~/miniforge3/etc/profile.d/conda.sh
conda activate drone-yolov8
cd /mnt/c/Users/hanna/Desktop/reSpeakerFlex/reSpeaker_XVF3800_USB_4MIC_ARRAY/python_control
python -m drone_detector --pi http://<pi-ip>:8765 --show
```

For an NVIDIA GPU inside WSL, add `--device 0`.

Useful options:

| Option | Meaning |
|---|---|
| `--classes drone` | only report these class names, if the model has several |
| `--conf 0.25` | lowest confidence to report (the Pi only locks on above `min_confidence`) |
| `--imgsz 640` | model input size; larger finds smaller, more distant drones but runs slower |
| `--show` | also show the annotated feed in a window (press q to quit) |

Every 10 seconds the detector logs how many frames per second it processes,
the inference time, and how many frames contained a drone. While it's running,
the dashboard's **Drone (camera)** section shows the model, its frame rate and
the lock, including the drone's azimuth and elevation. The LCD's top-right
corner switches from `EL 45° SCAN` to a green `EL 52° LOCK`. If the drone isn't
detected for `lost_after_s` (2 s), the tilt goes back to scanning and the pan
goes back to following the sound. If the PC detector stops, the Pi keeps
scanning on its own. Copy the updated `python_control` folder to the Pi and
restart `doa-tracker` before relying on the track log or the pan follow.

## 9. Listen for drones with a measurement microphone

Plug a USB measurement microphone into any of the Pi's USB ports. You can do
this while the tracker runs: it looks for a microphone every 5 seconds and
starts within a few seconds of finding one. With `device = "auto"` in the
`[acoustic]` section of `doa_tracker.toml`, it uses the first USB input whose
name isn't the reSpeaker. If it picks the wrong one, run `arecord -l` and set
`device` to part of the microphone's name.

This guide uses an **Omnitronic MM-2USB**: flat from 20 Hz to 20 kHz,
134 dB SPL maximum, 26 dBA self-noise, and a built-in 16-bit ADC at 44.1 kHz.
The tracker opens it at its own 44.1 kHz (`sample_rate = 0`), because
resampling adds false tones. A 16-bit ADC covers about 96 dB, so with full
scale at 134 dB SPL its quietest step is about 38 dB SPL. That's louder than
the mic's own 26 dBA self-noise, so a quiet room reads near -96 dBFS, the
16-bit floor, and the spectrogram is mostly dark. This is normal. A drone
within a few tens of metres is far louder.

The dashboard then shows a **Spectrogram** strip under the 3D view: the last
30 seconds from 0 to 4 kHz, newest on the right. The shaded band is the
blade-pass search range. Use **Range** to change the contrast.

Propellers hum at the blade-pass frequency (BPF), RPM ÷ 60 × number of blades,
and at whole multiples of it. A Parrot Bebop 2 Power has 3-blade props at
about 7,500 RPM in hover, so its BPF is about 375 Hz, rising to about 600 Hz
at full throttle. The tracker looks for a tone between `bpf_min_hz` and
`bpf_max_hz` (100–800 Hz) together with at least 3 of its first 5 harmonics.
When the same BPF holds for 1.5 seconds, it flags a drone:

- the dashboard badge turns red: `Drone BPF detected · 375 Hz · 7,500 RPM`,
  dashed lines mark the harmonics, a red bar runs along the top of the
  spectrogram while the flag is on, and the browser tab title changes,
- the **Drone (acoustic)** section shows the BPF, motor RPM (using `blades`)
  and how many harmonics matched,
- the LCD's top-left label changes from `AZIMUTH` to a red `DRONE 375Hz`,
- the log says `Drone BPF detected: 375.2 Hz (7504 RPM with 3 blades), 5/5
  harmonics`.

An amber `Possible BPF` badge means a harmonic comb is showing but hasn't
held long enough yet. The flag clears 2 seconds (`hold_s`) after the comb
fades.

Steady sounds such as a cooling fan or mains hum fade into a learned
background over about a minute (`background_s`), so they stop counting. The
tracker learns the background in its first few seconds, so start it before
the drone is flying. Keep the microphone away from the Pi's fan and the
servos.

Other tuning in `[acoustic]`:

| Setting | Effect |
|---|---|
| `bpf_min_hz`, `bpf_max_hz` | narrow to your drone, for example 300–650 Hz for the Bebop 2 Power, to avoid other hums |
| `blades` | propeller blades, used only to turn BPF into RPM |
| `peak_db` | lower to hear fainter, more distant drones; raise if fans or voices trigger it |
| `confirm_s` | longer means fewer false alarms but a slower flag |
| `display_max_hz`, `history_s` | spectrogram height and length on the dashboard |
| `spl_offset_db` | show the mic level in dB SPL instead of dBFS (see below) |

### Showing the level in dB SPL

By default the dashboard's **Mic level** and the spectrogram bar show dBFS
(decibels below digital full scale). To show dB SPL instead, set
`spl_offset_db` in `[acoustic]`. dB SPL is then dBFS + `spl_offset_db`. For
the MM-2USB, start with `spl_offset_db = 134.0`, since full scale is reached at
its 134 dB SPL maximum. That figure is approximate: it depends on the capture
gain in `alsamixer` (leave the gain alone once calibrated). To calibrate:

1. Put the mic next to a reference SPL meter in a steady sound, or fit a
   94 dB SPL calibrator over the mic capsule.
2. Note the dashboard's **Mic level** with `spl_offset_db = 0` (dBFS).
3. Set `spl_offset_db` to the reference reading minus that dBFS value. For
   example, 94 dB with -40.5 dBFS gives `134.5`.
4. Run `sudo systemctl restart doa-tracker` and check that the two readings
   now match.

The level is the unweighted RMS of each 0.1 s block, so compare it with the
meter's Z (flat) weighting rather than A weighting.

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

Copying over the old folder keeps `.venv`. Back up your `doa_tracker.toml`
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
Check the BL wire is on pin 18. While the service runs, `pinctrl get 24`
should show `op dh`; `ip pd` means nothing is driving the backlight, usually
because the Pi still has an old `display_config/config.py` with `PIN_BL = 12`.
See the tuning table in `display_config/README.md` for `Y_OFFSET`, `ROTATION`
and `SPI_BAUDRATE`.

**The spectrogram says `No USB microphone found`**
Run `arecord -l`. If the microphone isn't listed, try another USB port or
cable. If it's listed under a name without "USB", set `device` in `[acoustic]`
to part of that name. `Device or resource busy` means another program has it
open; stop that program.

**The spectrogram is almost black and the mic level sits near -96 dBFS**
In a quiet room this is normal for a 16-bit mic like the MM-2USB (step 9).
Clap or talk near it: the level should jump by 30 dB or more. If it doesn't,
the capture gain is at or near zero. Run `alsamixer`, press F6 to pick the USB
microphone, press F4 for capture controls, and raise the level. Then run
`sudo alsactl store` so it stays set after a reboot. Recalibrate
`spl_offset_db` after changing the gain.

**A fan, voice or engine sets off the drone flag**
Narrow `bpf_min_hz`/`bpf_max_hz` to your drone, raise `peak_db` or
`confirm_s`, or move the microphone away from the noise.

**`--check` says `no hardware PWM for GPIO12` or `GPIO13`, or the dashboard
shows a servo using "lgpio software PWM"**
The PWM overlay isn't loaded, so the servo falls back to software pulses and
twitches. Check that `/boot/firmware/config.txt` ends with
`dtoverlay=pwm-2chan,pin=12,func=4,pin2=13,func2=4` under `[all]`, then
reboot. If the installer warned about a different `dtoverlay=pwm` line,
replace that line with this one.

**`/camera.mjpg` returns 404**
First check the address uses the Pi's real hostname: the part after the `@` in
the Pi's prompt (`user@raspberrypi` means `http://raspberrypi.local:8765/`).
Then, on the Pi, run `curl -s http://localhost:8765/camera.mjpg | head -c 300`:

- `Camera is disabled ...`: set `enabled = true` under `[camera]` in the
  `doa_tracker.toml` the service uses, then restart it.
- A plain `Not Found` page: the service is running older code. Run
  `systemctl cat doa-tracker | grep -E 'WorkingDirectory|ExecStart'` to see
  which folder it runs from. If that isn't where you copied the new files,
  rerun `sudo bash deploy/install_pi.sh` from the new folder. Either way,
  finish with `sudo systemctl restart doa-tracker`.
- Binary JPEG data: the stream works on the Pi, so the laptop is reaching a
  different address. Use the IP from `hostname -I`.

`--check` may print libcamera's `Unable to set controls: Device or resource
busy` while the service is running. That only means the service already has
the camera open.

**`camera  no camera found`**
Check the ribbon cable is the Pi 5 type, fully inserted, and facing the right
way at both ends. Then check that `rpicam-hello --list-cameras` lists `imx219`.
`/boot/firmware/config.txt` must have `camera_auto_detect=1` (the default).

**`camera  picamera2 missing`**
Run `sudo apt install --no-install-recommends python3-picamera2`, then
`sudo systemctl restart doa-tracker`.

**The camera feed stutters, or the detector's received fps is low**
Wi-Fi can't keep up with 1280×960 at 15 fps. Use Ethernet on the Pi, or lower
`width`/`height` (keep 4:3, for example 1024×768 or 640×480), `fps` or
`jpeg_quality` in `[camera]`.

**The detector logs `Could not send detections to the Pi: HTTP 400 ... too old`**
The PC is running more than about 8 seconds behind the camera. Use a smaller
`--imgsz`, use the GPU (`--device 0`), or lower the camera `fps`.

**The tilt locks onto the drone at the wrong height**
Recheck `level_servo_deg` (step 7): elevation 0° must really be level. If the
camera is mounted upside down, set both flips to `true` so up in the image is
up in the sky.

**The Pi reboots or USB drops out when the servos move**
The servos are pulling the 5 V rail down. Power them from a separate 5 V
supply that shares a ground with the Pi (see step 3).

**The dashboard doesn't load**
Use the IP address from `hostname -I` instead of the `.local` name. Check that
`host = "0.0.0.0"` in `doa_tracker.toml` and that nothing else is using port
8765.

**The service keeps restarting**
Run `journalctl -u doa-tracker -n 50` to see the error. `Bad settings` means a
typo in `doa_tracker.toml`; the message names the setting.

## Uninstalling

```bash
sudo systemctl disable --now doa-tracker
sudo rm /etc/systemd/system/doa-tracker.service /etc/udev/rules.d/99-respeaker-xvf3800.rules
sudo systemctl daemon-reload
```

Then remove the `# reSpeaker tracker servo` and `dtoverlay=pwm-2chan,...` lines
from `/boot/firmware/config.txt`, reboot, and delete `~/python_control`.
