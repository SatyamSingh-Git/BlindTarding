#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu 24.04 Always Free instance.
#
#   ssh -i orbot.key ubuntu@<ip>
#   git clone https://github.com/SatyamSingh-Git/BlindTarding.git orbot
#   cd orbot && bash deploy/setup.sh
#
# Then copy .env across separately -- it never goes in git:
#   scp -i orbot.key .env ubuntu@<ip>:~/orbot/.env
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$APP_DIR"

echo "==> timezone"
# Three separate timezone bugs have already been found in this codebase. The
# host matching the exchange removes a whole class of them.
sudo timedatectl set-timezone Asia/Kolkata

echo "==> packages"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip git

echo "==> swap"
# 1 GB of RAM with no swap means the OOM reaper kills the bot rather than
# slowing it down. 2 GB of file swap is cheap insurance on a 47 GB disk.
if ! swapon --show | grep -q /swapfile; then
    sudo fallocate -l 2G /swapfile
    sudo chmod 600 /swapfile
    sudo mkswap /swapfile >/dev/null
    sudo swapon /swapfile
    grep -q '^/swapfile' /etc/fstab || \
        echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
    # Prefer keeping the bot resident; swap is a backstop, not a working store.
    sudo sysctl -qw vm.swappiness=10
    grep -q '^vm.swappiness' /etc/sysctl.conf || \
        echo 'vm.swappiness=10' | sudo tee -a /etc/sysctl.conf >/dev/null
fi

echo "==> python environment"
python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -e ".[webhook]"

echo "==> warming the instrument cache"
# Do this once here rather than inside the service: the cold parse peaks at
# 180 MB, and it is better to pay that now than during the first bar close.
mkdir -p data
./.venv/bin/orbot instruments || \
    echo "    (skipped -- needs .env; run it again after copying credentials)"

echo "==> systemd"
sudo cp deploy/orbot.service /etc/systemd/system/
sudo sed -i "s|@APP_DIR@|$APP_DIR|g; s|@USER@|$(whoami)|g" \
    /etc/systemd/system/orbot.service
sudo systemctl daemon-reload
sudo systemctl enable orbot.service

cat <<'DONE'

Setup complete.

  1. Copy your .env across if you have not already:
       scp -i orbot.key .env ubuntu@<ip>:~/orbot/.env

  2. Check the configuration before starting:
       ./.venv/bin/orbot doctor
       ./.venv/bin/orbot validate

  3. Start it:
       sudo systemctl start orbot
       journalctl -u orbot -f

  4. The dashboard runs as its own service, bound to localhost. Tunnel in:
       ssh -i orbot.key -L 8777:localhost:8777 ubuntu@<ip>
       # then open http://localhost:8777 on your own machine

DONE
