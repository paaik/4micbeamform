#!/usr/bin/env bash
# Install the reSpeaker direction tracker as a systemd service on a Raspberry Pi 5
# (Raspberry Pi OS Bookworm or later). Run from the account that should own it:
#
#   sudo ./deploy/install_pi.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Run with sudo: sudo $0" >&2
    exit 1
fi
SERVICE_USER="${SUDO_USER:-}"
if [[ -z "$SERVICE_USER" || "$SERVICE_USER" == "root" ]]; then
    echo "Run through sudo from the normal user account that should run the service." >&2
    exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BOOT_CONFIG=/boot/firmware/config.txt
[[ -f "$BOOT_CONFIG" ]] || BOOT_CONFIG=/boot/config.txt

echo "==> Installing system packages"
apt-get update
apt-get install -y python3-venv python3-dev build-essential python3-lgpio python3-spidev \
    python3-numpy libusb-1.0-0 fonts-dejavu-core i2c-tools libportaudio2 alsa-utils
apt-get install -y --no-install-recommends python3-picamera2

echo "==> Enabling SPI (LCD) and I2C (reSpeaker I2S firmware control)"
raspi-config nonint do_spi 0
raspi-config nonint do_i2c 0
if grep -q "^camera_auto_detect=0" "$BOOT_CONFIG"; then
    echo "!! camera_auto_detect=0 in $BOOT_CONFIG; set it to 1 so the Camera v2 is found" >&2
fi

# One hardware PWM channel per servo: GPIO12/13 use function 4, GPIO18/19 function 2.
OVERLAY="$(python3 - "$APP_DIR/doa_tracker.toml" <<'PY'
import sys, tomllib
cfg = tomllib.load(open(sys.argv[1], "rb"))
func = {12: 4, 13: 4, 18: 2, 19: 2}
pins = set()
for section, default in (("servo", 13), ("tilt", 12)):
    servo = cfg.get(section, {})
    if servo.get("enabled", True) and servo.get("backend", "auto") != "software":
        pins.add(int(servo.get("gpio", default)))
pins = sorted(p for p in pins if p in func)
if len(pins) == 1:
    print("dtoverlay=pwm,pin={},func={}".format(pins[0], func[pins[0]]))
elif len(pins) == 2:
    print("dtoverlay=pwm-2chan,pin={},func={},pin2={},func2={}".format(pins[0], func[pins[0]], pins[1], func[pins[1]]))
PY
)"
MARK="# reSpeaker tracker servo"
if [[ -z "$OVERLAY" ]]; then
    echo "==> No servo on a hardware PWM pin; skipping the pwm overlay"
elif grep -qx "$OVERLAY" "$BOOT_CONFIG"; then
    echo "==> $BOOT_CONFIG already has $OVERLAY"
elif grep -qx "$MARK" "$BOOT_CONFIG"; then
    echo "==> Updating the servo PWM overlay in $BOOT_CONFIG to $OVERLAY"
    sed -i "/^${MARK}\$/{n;s/^dtoverlay=pwm.*\$/${OVERLAY}/;}" "$BOOT_CONFIG"
elif grep -q "^dtoverlay=pwm" "$BOOT_CONFIG"; then
    echo "!! $BOOT_CONFIG already has a different pwm overlay; replace it with: $OVERLAY" >&2
else
    echo "==> Routing the servo pins to hardware PWM in $BOOT_CONFIG"
    printf '\n[all]\n%s\n%s\n' "$MARK" "$OVERLAY" >> "$BOOT_CONFIG"
fi

echo "==> Creating the Python environment in $APP_DIR/.venv"
sudo -u "$SERVICE_USER" python3 -m venv --system-site-packages "$APP_DIR/.venv"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements-pi.txt"

echo "==> USB permissions and groups"
install -m 0644 "$APP_DIR/deploy/99-respeaker-xvf3800.rules" /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb
usermod -aG gpio,spi,i2c,plugdev,video,audio "$SERVICE_USER"

echo "==> Installing doa-tracker.service"
sed -e "s|@USER@|$SERVICE_USER|g" -e "s|@APP_DIR@|$APP_DIR|g" \
    "$APP_DIR/deploy/doa-tracker.service" > /etc/systemd/system/doa-tracker.service
systemctl daemon-reload
systemctl enable doa-tracker.service

cat <<EOF

Done. Reboot once so SPI, I2C, the PWM overlay and the new groups take effect:
  sudo reboot
Then check:
  cd $APP_DIR && .venv/bin/python -m doa_tracker --check
  systemctl status doa-tracker
  journalctl -u doa-tracker -f
Dashboard: http://$(hostname).local:8765/
Camera stream for the PC detector: http://$(hostname).local:8765/camera.mjpg
EOF
