# Hodimlar kompyuterini nazorat qilish — qo'llanma

Tizim ikki qismdan iborat:

| Qism | Nima qiladi | Qayerdan boshqariladi |
|---|---|---|
| **Gigant agent** (o'zimiz yozdik) | Qaysi dasturda/saytda qancha vaqt o'tdi, faol/harakatsiz vaqt, ish ulushi %. Bloklash, o'chirish, qayta yoqish, xabar, skrinshot | Telegram bot (`/screen`, `/onlayn`, `/pc`) va dashboard → **🖥 Screen time** |
| **Veyon** (bepul, tayyor dastur) | Hamma ekranlarni **jonli** ko'rish (kompyuter klubdagi kabi), sichqoncha/klaviaturani masofadan olish, bir tugma bilan hammasini bloklash/o'chirish | Sizning (admin) kompyuteringizdagi **Veyon Master** oynasi (faqat ofis tarmog'ida) |

> ⚖️ **Muhim (qonun):** hodimlarni kuzatuv haqida **yozma ogohlantiring** — ichki tartib qoidalari yoki
> buyruqqa "ish kompyuterlarida ish vaqti va dasturlar hisobi yuritiladi, ekran tasviri olinishi mumkin"
> degan band qo'shib, imzo qildiring. Agent klaviaturada nima yozilganini, parollarni **o'qimaydi**.

---

## 1-qism. Gigant agent (screen time)

### Har bir hodim kompyuterida (bir marta, ~3 daqiqa)

1. Hodim Telegram botga `/key` yozadi va kalitini sizga beradi (yoki o'zi kiritadi).
2. Hodim kompyuterida **Пуск** → `PowerShell` yozing → o'ng tugma → **Запуск от имени администратора**.
3. Telegram botda **`/agent`** yozing — u yerda joriy manzil bilan tayyor buyruq bor, masalan:
   ```
   irm https://XXXX.trycloudflare.com/agent/install.ps1 | iex
   ```
4. Kalitni so'raganda kiriting → "Tayyor!" chiqadi.
5. 1 daqiqadan keyin botda `/onlayn` — kompyuter 🟢 bo'lib ko'rinishi kerak.

Agent har safar kompyuter yoqilganda o'zi ishga tushadi; hodim uni Диспетчер задач dan yopsa ham 1 daqiqada qayta yonadi.

**Tavsiya:** hodimlarning Windows hisobi **oddiy foydalanuvchi** (administrator emas) bo'lsin —
shunda ular agentni o'chira olmaydi va dastur o'rnata olmaydi.

### Botdagi buyruqlar (faqat siz uchun)

| Buyruq | Nima |
|---|---|
| `/screen` | Bugun har hodim: faol vaqt, harakatsiz, ish ulushi %, eng ko'p ishlatilgan dasturlar |
| `/screen 25.11.2026` | O'sha kun |
| `/screen TG_ID` | Bitta hodim batafsil: barcha dasturlar, brauzerda ochilgan sahifalar, chalg'ituvchilar |
| `/onlayn` | Hozir kim kompyuterda va qaysi oynada |
| `/pc` | Tugmalar: 🔒 bloklash, 📸 skrinshot, 💬 xabar, ⏻ o'chirish, 🔄 qayta yoqish, 🚪 chiqarish; "hammasini bloklash/o'chirish" |
| `/agent` | O'rnatish yo'riqnomasi |

Har ish kuni ish tugagach (+20 daqiqa) kunlik screen time hisoboti o'zi keladi.

### ⛔ "Kompyuter klub" bloki
Dashboard → **🖥 Screen time** → hodim plitkasini bosing (yoki asosiy dashboardda hodim ismi ostidagi **🖥 Kompyuter**).
Ochilgan oynada jonli holat (hozir qaysi dastur, bugungi vaqt, skrinshot) va katta **⛔ Kompyuterni bloklash** tugmasi.
- Bloklanganda hodim ekranini to'liq qora oyna yopadi ("KOMPYUTER BLOKLANGAN" + siz yozgan matn), klaviatura ishlamaydi,
  Диспетчер задач o'chiriladi. Faqat siz **🔓 Blokni ochish** ni bosganingizda ochiladi.
- Buyruq 1–2 soniyada yetib boradi. Kompyuter o'chirib-yoqilsa ham blok qaytadi.
- Telegram: `/pc` → kompyuter → ⛔ Bloklash / 🔓 Blokni ochish; "Hammasini bloklash/ochish" ham bor.
- Ctrl+Alt+Del ni Windows o'zi boshqaradi — undan hodim faqat "Выйти/Сменить пользователя" qila oladi (ishi yopiladi).

### Qaysi dastur "ish", qaysi "chalg'ish"

Standart ro'yxat `screentime.py` da (`DEFAULT_PRODUCTIVE`, `DEFAULT_UNPRODUCTIVE`): Excel, Word, 1C, soliq.uz,
didox, my.gov.uz, bank kabinetlari — **ish**; YouTube, Instagram, TikTok, kino, o'yinlar, marketplace'lar — **chalg'ish**;
qolganlari — **neytral**. O'zgartirish uchun serverdagi `config.json` ga qo'shing:
```json
"ST_PRODUCTIVE": ["excel", "1cv8", "soliq.uz", "didox"],
"ST_UNPRODUCTIVE": ["youtube", "instagram", "tiktok"]
```
(kalit so'z dastur nomi yoki oyna sarlavhasida qidiriladi; o'zgartirish eski kunlarga ham ta'sir qiladi.)

### Xavfsizlik va yangilanish
- Agent 1.3+ o'zini **avtomatik yangilaydi** (SYSTEM nomidan har soatda `GigantAgent-Update` vazifasi). Keyingi o'zgarishlarda kompyuterlarga qayta o'rnatish shart emas.
- Kompyuter o'chmagan holda agent qayta ishga tushsa (Диспетчер задач dan yopilgan, tizimdan chiqib-kirilgan) — botga ⚠️ xabar keladi.
- Hodim hisobi **oddiy foydalanuvchi** bo'lishi shart. Administrator bo'lsa, agentni o'chirib tashlay oladi.
- Dashboard paroli uzun bo'lsin (12+ belgi). Ketma-ket noto'g'ri parol kiritilsa — botga xabar keladi, 30 ta xatodan keyin kirish 1 soatga yopiladi.

### Agentni o'chirish
PowerShell (admin): `irm https://XXXX.trycloudflare.com/agent/uninstall.ps1 | iex`

### Muammo bo'lsa
- Agent logi: `%LOCALAPPDATA%\GigantAgent\agent.log`
- Antivirus `GigantAgent.exe` ni bloklasa — `C:\Program Files\GigantAgent` papkasini istisnoga qo'shing.
- Internet uzilsa agent ma'lumotni saqlab turadi va keyin yuboradi (2–3 kungacha).
- Bloklash/o'chirish buyrug'i 30 soniya ichida bajariladi; kompyuter oflayn bo'lsa 10 daqiqa kutadi, keyin bekor bo'ladi.

---

## 2-qism. Veyon — jonli ekranlar va "kompyuter klub" boshqaruvi

Veyon — bepul, ochiq kodli dastur (veyon.io). Ofis tarmog'ida (bitta Wi-Fi/router) ishlaydi.

### O'rnatish
1. https://veyon.io/en/download/ dan **Windows 64-bit** o'rnatuvchini yuklang (hamma kompyuterlarga bir xil fayl).
2. **Hodim kompyuterlarida:** o'rnatishda **"Veyon Master"** belgisini **olib tashlang** (faqat Service qoladi).
3. **Sizning kompyuteringizda:** to'liq o'rnating (Master + Service).

### Kalitlar (faqat siz boshqara olishingiz uchun)
1. O'z kompyuteringizda **Veyon Configurator** → **Authentication keys** → **Create key pair** → nomi: `gigant`.
2. **Export public key** → faylni fleshkaga saqlang (`gigant_public_key.pem`).
3. Har hodim kompyuterida Veyon Configurator → Authentication keys → **Import key** → shu public key → nomi `gigant`.
4. Hamma kompyuterlarda: **General → Authentication method: Key file authentication** → **Apply**.

### Kompyuterlarni qo'shish
1. Har hodim kompyuterining IP manzilini bilib oling (`ipconfig` → IPv4). Routerda bu IP'larni **doimiy (DHCP reservation)** qilib qo'ying.
2. Sizning kompyuteringizda Veyon Configurator → **Locations & computers** → **+** joy (`Ofis`) → har kompyuter uchun nom va IP.
3. **Veyon Master** ni oching — hamma ekranlar plitka bo'lib ko'rinadi.

### Asosiy tugmalar (Veyon Master)
- **Monitor** — hamma ekranlar jonli; ikki marta bossangiz — kattalashadi
- **Lock** — tanlangan/hamma kompyuterlarni qora ekran bilan bloklash (kompyuter klubdagi kabi), qayta bosish — ochish
- **Remote control / Remote view** — masofadan boshqarish yoki faqat ko'rish
- **Power down / Reboot / Power on** — o'chirish, qayta yoqish, yoqish (Wake-on-LAN BIOS'da yoqilgan bo'lsa)
- **Text message** — ekranga xabar
- **Screenshot** — skrinshot (Veyon papkasiga saqlanadi)

Veyon Configurator → **Service → "Hide tray icon"** ni o'chirmang: hodim nazorat borligini bilishi kerak (qonun talabi).

---

## Server tomoni (texnik)

- Kod: `bot.py` + `screentime.py`; agent: `agent/gigant_agent.py`, o'rnatuvchi `agent/install.ps1`, `agent/uninstall.ps1`
  (server ularni `/agent/...` manzilidan tarqatadi).
- Hodim kompyuterida: `C:\Program Files\GigantAgent\` — python.org'ning rasmiy portativ Python 3.11 nusxasi,
  `GigantAgent.exe` (= pythonw.exe nusxasi) va `gigant_agent.py`. Alohida Python o'rnatilmaydi.
- Agentni yangilash: `gigant_agent.py` ni serverda almashtiring, hodim kompyuterida o'rnatish buyrug'ini qayta ishga tushiring.
- Bazadagi jadvallar: `st_usage` (kun × dastur × sarlavha), `st_minutes` (daqiqalik vaqt chizig'i, 90 kun saqlanadi), `st_pcs` (kompyuterlar), `st_cmds` (buyruqlar).
- Serverga yangilash: `bot.py`, `screentime.py` → `/opt/hodim-bot/`; `agent/*.ps1`, `agent/gigant_agent.py` → `/opt/hodim-bot/agent/`;
  keyin `systemctl restart hodim-bot`.
