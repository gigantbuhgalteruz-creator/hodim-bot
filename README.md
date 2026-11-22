# Hodim bot — davomat, jarimalar, soliq.uz hisobotlari va dashboard

Telegram bot (`@tekshiruvbuxgalterbot`) + veb-dashboard + hodimlar Chrome'i uchun Tampermonkey skripti.
Faqat Python standart kutubxonasi (pip shart emas). Ma'lumotlar SQLite (`data.db`) da.

## Nima qiladi
- **Davomat:** hodim har ish kuni 09:00 gacha botga yumaloq video yuboradi (vaqt Telegram serveridan). 09:05 da davomat, 18:30 da "kelmaganlar", juma 18:35 da haftalik hisobot owner'ga keladi.
- **Jarimalar (oy bo'yicha):** 1-kechikish 10%, 2-si 30% + ogohlantirish, 3-si — faqat owner'ga xabar (qarorni owner qiladi). `/forgive` bekor qiladi.
- **soliq.uz hisobotlari:** skript hodim kirgan kabinet jurnalini kuzatadi; "Отправить" dan keyin bir necha soniyada owner'ga xabar: 🟢 vaqtida, 🟡 tekshiruvda, 🚨🔴 kechikkan/xato.
- **Biriktirish tarixi:** korxona qaysi hodimga qaysi sanadan qaysi sanagacha biriktirilgani saqlanadi; hisobot o'sha sanadagi mas'ul hodim nomida qoladi.
- **Dashboard:** samaradorlik, davomat, jarimalar, rangli hisobotlar, hodim/korxona qo'shish, ko'chirish, ishdan bo'shatish, tarix. Login sahifasi (`DASHBOARD_USER` / `DASHBOARD_PASSWORD`), sessiya 60 daqiqa
  (`SESSION_MINUTES`), HttpOnly+Secure+SameSite=Strict cookie, 15 daqiqada 10 xato → blok, CSRF, xavfsizlik headerlari.

## Fayllar
| Fayl | Vazifasi |
|---|---|
| `bot.py` | Telegram bot (long polling), scheduler, HTTP API + dashboard (port 8080) |
| `tunnel.py` | Cloudflare quick tunnel; yangi manzilni `config.json`, bot profili (t.me) ga yozadi va owner'ga yuboradi |
| `soliq-hisobot.user.js` | Tampermonkey skripti (server uni `/skript.user.js` da joriy manzil bilan tarqatadi) |
| `config.example.json` | `config.json` namunasi (haqiqiy `config.json` repoga qo'shilmaydi — token bor) |
| `deploy/` | Ubuntu serverni tayyorlash va systemd xizmatlari |
| `docs/` | Owner (kirill) va hodimlar (lotin) uchun yo'riqnomalar |
| `SERVER.md` | Ishlab turgan server haqida (IP, buyruqlar, yangilash) |
| `windows/` | Eski: Windows kompyuterda ishga tushirish fayllari |

## O'rnatish (Ubuntu 24.04)
```bash
bash deploy/server_setup.sh          # yangilash, firewall (faqat SSH), swap, cloudflared, hodimbot foydalanuvchisi
cp bot.py tunnel.py soliq-hisobot.user.js /opt/hodim-bot/
cp config.example.json /opt/hodim-bot/config.json && nano /opt/hodim-bot/config.json   # BOT_TOKEN, OWNER_ID, parol
chown -R hodimbot: /opt/hodim-bot && chmod 600 /opt/hodim-bot/config.json
bash deploy/services.sh              # systemd: hodim-bot, hodim-tunnel + kunlik zaxira 03:00
systemctl start hodim-bot hodim-tunnel
```

## Owner buyruqlari (to'liq ro'yxat — botda `/help`)
`/add`, `/salary`, `/remove`, `/list`, `/firma`, `/assign`, `/unassign`, `/kochir`, `/move`, `/tarix`,
`/today`, `/week`, `/month`, `/forgive`, `/dashboard`, `/cancel`.
Hodim: `/start`, `/key`, `/me`, yumaloq video.

## Sozlamalar (`config.json`)
- `WORK_START`, `WORK_END`, `WORK_DAYS` (0=Du), `HOLIDAYS` (`"2026-12-08"`)
- `VIOLATION_PERIOD`: `"month"` — har oy qaytadan; `"all"` — umuman
- `ABSENT_IS_VIOLATION`, `FORWARD_VIDEOS_TO_OWNER`
- `DASHBOARD_USER`, `DASHBOARD_PASSWORD`, `SESSION_MINUTES` (standart 60), `PORT`, `PUBLIC_URL` (tunnel o'zi yozadi)
- `BIND`: serverda `"127.0.0.1"` — dashboardga faqat tunnel orqali kiriladi
