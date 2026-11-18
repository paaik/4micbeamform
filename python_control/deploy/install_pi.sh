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
    python3-numpy libusb-1.0-0 fonts-dejavu-core i2c-tools

echo "==> Enabling SPI (LCD) and I2C (reSpeaker I2S firmware control)"
raspi-config nonint do_spi 0
raspi-config nonint do_i2c 0

SERVO_GPIO="$(python3 -c 'import sys, tomllib; print(tomllib.load(open(sys.argv[1], "rb")).get("servo", {}).get("gpio", 13))' "$APP_DIR/doa_tracker.toml")"
OVERLAY="dtoverlay=pwm,pin=${SERVO_GPIO},func=4"
if grep -q "^dtoverlay=pwm," "$BOOT_CONFIG"; then
    if ! grep -qx "$OVERLAY" "$BOOT_CONFIG"; then
        echo "!! $BOOT_CONFIG already has a different pwm overlay; set it to: $OVERLAY" >&2
    fi
else
    echo "==> Routing GPIO${SERVO_GPIO} to hardware PWM in $BOOT_CONFIG"
    printf '\n[all]\n# reSpeaker tracker servo\n%s\n' "$OVERLAY" >> "$BOOT_CONFIG"
fi

echo "==> Creating the Python environment in $APP_DIR/.venv"
sudo -u "$SERVICE_USER" python3 -m venv --system-site-packages "$APP_DIR/.venv"
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements-pi.txt"

# Cloud code
if [[ ! -f "$APP_DIR/cloud.env" ]]; then
    echo "==> Creating $APP_DIR/cloud.env for the Supabase API key"
    printf '# Supabase API key for cloud upload. Keep this file private; git ignores it.\nSUPABASE_KEY=\n' \
        > "$APP_DIR/cloud.env"
fi
chown "$SERVICE_USER": "$APP_DIR/cloud.env"
chmod 600 "$APP_DIR/cloud.env"
# end of the cloud codes

echo "==> USB permissions and groups"
install -m 0644 "$APP_DIR/deploy/99-respeaker-xvf3800.rules" /etc/udev/rules.d/
udevadm control --reload-rules
udevadm trigger --subsystem-match=usb
usermod -aG gpio,spi,i2c,plugdev "$SERVICE_USER"

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
EOF
