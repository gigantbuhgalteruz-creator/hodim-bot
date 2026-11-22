#!/bin/bash
# systemd xizmatlari + kunlik zaxira (hali ISHGA TUSHIRILMAYDI — faqat o'rnatiladi)
set -e
cat > /etc/systemd/system/hodim-bot.service <<'EOF'
[Unit]
Description=Hodim bot (Telegram + dashboard)
After=network-online.target
Wants=network-online.target

[Service]
User=hodimbot
WorkingDirectory=/opt/hodim-bot
Environment=PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /opt/hodim-bot/bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/hodim-tunnel.service <<'EOF'
[Unit]
Description=Hodim bot Cloudflare tunnel
After=network-online.target hodim-bot.service
Wants=network-online.target

[Service]
User=hodimbot
WorkingDirectory=/opt/hodim-bot
Environment=PYTHONIOENCODING=utf-8 PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 /opt/hodim-bot/tunnel.py
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
EOF

# Kunlik zaxira 03:00 da, 30 kun saqlanadi
cat > /etc/cron.d/hodim-backup <<'EOF'
0 3 * * * hodimbot sqlite3 /opt/hodim-bot/data.db ".backup /opt/hodim-bot/backup/data-$(date +\%Y\%m\%d).db" && find /opt/hodim-bot/backup -name 'data-*.db' -mtime +30 -delete
EOF
chmod 644 /etc/cron.d/hodim-backup
systemctl daemon-reload
systemctl enable hodim-bot hodim-tunnel
echo "xizmatlar o'rnatildi (hali ishga tushmagan)"
