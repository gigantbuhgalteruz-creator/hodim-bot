#!/bin/bash
# Hodim bot serverini tayyorlash (Ubuntu 24.04). Takror ishga tushirsa ham xavfsiz.
set -e
export DEBIAN_FRONTEND=noninteractive
timedatectl set-timezone Asia/Tashkent || true
apt-get update -q
apt-get -yq -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade
apt-get -yq install ufw sqlite3 unattended-upgrades ca-certificates curl

# 1 GB RAM — ehtiyot uchun 1 GB swap
if [ ! -f /swapfile ]; then
  fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# Firewall: faqat SSH. Tunnel tashqariga ulanadi, kiruvchi port kerak emas.
ufw allow OpenSSH
ufw --force enable

# SSH: faqat kalit bilan kirish
sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl reload ssh || systemctl reload sshd || true

# cloudflared — Cloudflare rasmiy GitHub relizidan
if ! command -v cloudflared >/dev/null; then
  curl -fsSL -o /tmp/cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb
  dpkg -i /tmp/cloudflared.deb && rm /tmp/cloudflared.deb
fi

# Bot uchun alohida foydalanuvchi va papka
id hodimbot >/dev/null 2>&1 || useradd --system --home /opt/hodim-bot --shell /usr/sbin/nologin hodimbot
mkdir -p /opt/hodim-bot/backup
chown -R hodimbot:hodimbot /opt/hodim-bot

echo "=== TAYYOR ==="
python3 --version; cloudflared --version; ufw status | head -5; swapon --show
