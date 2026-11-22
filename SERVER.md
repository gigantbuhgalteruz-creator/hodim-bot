# Hodim bot — server (DigitalOcean)

- **IP:** 165.227.155.177 (Ubuntu 24.04, 1 vCPU / 1 GB, Frankfurt/…)
- **Kirish:** `ssh -i %USERPROFILE%\.ssh\hodim_bot_hetzner root@165.227.155.177` (faqat kalit bilan, parol o'chirilgan)
- **Papka:** `/opt/hodim-bot` (bot.py, tunnel.py, config.json, data.db, soliq-hisobot.user.js)
- **Xizmatlar (systemd, server yonganda o'zi ishga tushadi):**
  - `hodim-bot` — Telegram bot + dashboard (port 8080, faqat ichkarida)
  - `hodim-tunnel` — Cloudflare tunnel; yangi manzilni config.json ga, bot profiliga (t.me) yozadi va rahbarga yuboradi
- **Zaxira:** har kuni 03:00 da `/opt/hodim-bot/backup/data-YYYYMMDD.db`, 30 kun saqlanadi
- **Firewall:** faqat SSH (22) ochiq

## Foydali buyruqlar (serverda)
```
systemctl status hodim-bot hodim-tunnel     # holat
systemctl restart hodim-bot                 # botni qayta yuklash
journalctl -u hodim-bot -f                  # jonli log
tail -f /opt/hodim-bot/bot.log              # xabarlar logi
grep PUBLIC_URL /opt/hodim-bot/config.json  # joriy manzil
```

## Kodni yangilash (shu kompyuterdan)
```
scp -i %USERPROFILE%\.ssh\hodim_bot_hetzner C:\hodim-bot\bot.py root@165.227.155.177:/opt/hodim-bot/
ssh -i %USERPROFILE%\.ssh\hodim_bot_hetzner root@165.227.155.177 "chown hodimbot: /opt/hodim-bot/bot.py && systemctl restart hodim-bot"
```

## Eslatma
- Shu kompyuterdagi lokal bot O'CHIRILGAN. Avtostart yorliqlari `C:\hodim-bot\backup\startup-yorliqlar` ga olindi.
  Ikkala bot bir vaqtda ishlamasin (xabarlar ikki marta keladi).
- Server ko'chirilishidan oldingi baza: `C:\hodim-bot\backup\data-oldin-server-*.db`
- C:\hodim-bot dagi config.json/data.db endi ESKI — haqiqiy ma'lumot serverda.
