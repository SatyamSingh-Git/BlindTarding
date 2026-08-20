#!/usr/bin/env bash
# Lock the box down. Run once, after setup.sh, and read it before you do --
# it will disconnect you if your key is not working.
#
# What is on this machine: ANGEL_MPIN and ANGEL_TOTP_SECRET, which together
# are full trading access to a real account. Treat it accordingly.
set -euo pipefail

echo "==> confirming key-based login works before disabling passwords"
if [ ! -s ~/.ssh/authorized_keys ]; then
    echo "REFUSING: ~/.ssh/authorized_keys is empty. You would lock yourself out."
    exit 1
fi

echo "==> ssh: keys only, no root"
sudo tee /etc/ssh/sshd_config.d/99-orbot.conf >/dev/null <<'CONF'
PasswordAuthentication no
PermitRootLogin no
KbdInteractiveAuthentication no
X11Forwarding no
MaxAuthTries 3
# AllowTcpForwarding stays ON: the dashboard is reached only through a tunnel.
AllowTcpForwarding yes
CONF
sudo systemctl restart ssh

echo "==> firewall"
# Oracle images ship iptables rules plus a cloud-side security list. Keep the
# host firewall to SSH only; the dashboard binds to 127.0.0.1 and must never
# be reachable from outside, kill switch and positions and all.
sudo apt-get install -y -qq ufw
sudo ufw --force reset >/dev/null
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp comment 'ssh'
sudo ufw --force enable

echo "==> fail2ban"
sudo apt-get install -y -qq fail2ban
sudo tee /etc/fail2ban/jail.d/sshd.local >/dev/null <<'CONF'
[sshd]
enabled = true
maxretry = 4
bantime = 1h
CONF
sudo systemctl enable --now fail2ban

echo "==> unattended security updates"
sudo apt-get install -y -qq unattended-upgrades
sudo dpkg-reconfigure -f noninteractive unattended-upgrades

echo "==> permissions on secrets"
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
[ -f "$APP_DIR/.env" ] && chmod 600 "$APP_DIR/.env"
chmod 700 "$APP_DIR/data" 2>/dev/null || true

cat <<'DONE'

Hardened.

  Open ports:        22 only
  Password login:    disabled
  Dashboard:         127.0.0.1 only, reachable via SSH tunnel

  Still to do in the OCI console, if you have not:
    VCN -> Security Lists -> restrict the SSH ingress rule from 0.0.0.0/0
    to your own IP. It is the single biggest remaining exposure.

DONE
