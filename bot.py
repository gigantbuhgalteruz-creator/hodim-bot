#!/usr/bin/env python3
"""
Hodimlar davomati + soliq.uz hisobotlari uchun Telegram bot va dashboard.
Faqat Python 3.9+ standart kutubxonasi ishlatiladi (pip install shart emas).

Ishga tushirish:  python3 bot.py      (config.json shu papkada bo'lishi kerak)
"""
import base64
import html
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import sys
for _s in (sys.stdout, sys.stderr):  # Windows konsolida emoji xatosiz chiqsin
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

BASE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
    CFG = json.load(f)

TOKEN = CFG["BOT_TOKEN"]
OWNER = int(CFG["OWNER_ID"])
PORT = int(CFG.get("PORT", 8080))
PUBLIC_URL = CFG.get("PUBLIC_URL", f"http://localhost:{PORT}").rstrip("/")
DASH_PASSWORD = CFG.get("DASHBOARD_PASSWORD", "")
DASH_USER = CFG.get("DASHBOARD_USER", "")          # bo'sh bo'lsa istalgan login qabul qilinadi
LOGIN_FAILS = {}                                    # ip -> [vaqtlar], parol tanlashdan himoya
MAX_FAILS, FAIL_WINDOW = 10, 900                    # 15 daqiqada 10 ta xato -> vaqtincha blok
WORK_START = CFG.get("WORK_START", "09:00")
WORK_END = CFG.get("WORK_END", "18:30")
WORK_DAYS = set(CFG.get("WORK_DAYS", [0, 1, 2, 3, 4]))          # 0=Dushanba ... 6=Yakshanba
HOLIDAYS = set(CFG.get("HOLIDAYS", []))                          # ["2026-12-08", ...]
PERIOD = CFG.get("VIOLATION_PERIOD", "month")                    # "month" yoki "all"
ABSENT_IS_VIOLATION = CFG.get("ABSENT_IS_VIOLATION", True)
FORWARD_VIDEOS = CFG.get("FORWARD_VIDEOS_TO_OWNER", False)
DB_PATH = CFG.get("DB_PATH", os.path.join(BASE, "data.db"))

TZ = timezone(timedelta(hours=5))  # Toshkent (yozgi vaqt yo'q)

# Kech qolish qoidalari: (jarima %, qoida matni). 3-va undan keyingisi oxirgi qoida.
PENALTIES = [
    (10, "maoshdan 10% jarima va ogohlantirish"),
    (30, "maoshdan 30% jarima va mehnat shartnomasini bekor qilish haqida ogohlantirish"),
    (None, "qoidaga ko'ra mehnat shartnomasi bekor qilinadi"),
]

esc = html.escape


def now():
    return datetime.now(TZ)


def hm(s):
    h, m = s.split(":")
    return int(h), int(m)


def money(n):
    return f"{int(n):,}".replace(",", " ") + " so'm"


def is_workday(d):
    return d.weekday() in WORK_DAYS and d.isoformat() not in HOLIDAYS


def workdays(a, b):
    out, d = [], a
    while d <= b:
        if is_workday(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def at(d, s):
    h, m = hm(s)
    return datetime(d.year, d.month, d.day, h, m, tzinfo=TZ)


# ---------------------------------------------------------------- database
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
LOCK = threading.RLock()
db.executescript("""
CREATE TABLE IF NOT EXISTS employees(
  tg_id INTEGER PRIMARY KEY, name TEXT, salary INTEGER DEFAULT 0,
  api_key TEXT UNIQUE, active INTEGER DEFAULT 1, created TEXT);
CREATE TABLE IF NOT EXISTS companies(inn TEXT PRIMARY KEY, name TEXT, tg_id INTEGER);
CREATE TABLE IF NOT EXISTS checkins(
  tg_id INTEGER, day TEXT, ts TEXT, status TEXT, minutes_late INTEGER DEFAULT 0, file_id TEXT,
  PRIMARY KEY(tg_id, day));
CREATE TABLE IF NOT EXISTS violations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, day TEXT, kind TEXT,
  level INTEGER, fine_pct INTEGER, created TEXT, UNIQUE(tg_id, day));
CREATE TABLE IF NOT EXISTS reports(
  report_id TEXT, status TEXT, inn TEXT, company TEXT, tg_id INTEGER, data TEXT, ts TEXT,
  PRIMARY KEY(report_id, status));
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
-- Korxona qaysi hodimga qaysi sanadan qaysi sanagacha biriktirilgan (start kiradi, end kirmaydi; end NULL = hozirgacha)
CREATE TABLE IF NOT EXISTS assignments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, inn TEXT, tg_id INTEGER, start TEXT, end TEXT, created TEXT);
CREATE INDEX IF NOT EXISTS ix_asg_inn ON assignments(inn, start);
""")
# Birinchi ishga tushishda: mavjud biriktirishlarni tarixga "boshidan beri" deb yozamiz.
if not db.execute("SELECT 1 FROM assignments LIMIT 1").fetchone():
    db.execute("INSERT INTO assignments(inn,tg_id,start,end,created) "
               "SELECT inn, tg_id, '2000-01-01', NULL, datetime('now') FROM companies WHERE tg_id IS NOT NULL")
    db.commit()


def q(sql, args=(), one=False):
    with LOCK:
        rows = db.execute(sql, args).fetchall()
        db.commit()
    if one:
        return rows[0] if rows else None
    return rows


def emp(tg_id, active_only=True):
    r = q("SELECT * FROM employees WHERE tg_id=?", (tg_id,), one=True)
    if r and active_only and not r["active"]:
        return None
    return r


def emp_by_key(key):
    if not key:
        return None
    return q("SELECT * FROM employees WHERE api_key=? AND active=1", (str(key).strip(),), one=True)


def active_employees():
    return q("SELECT * FROM employees WHERE active=1 ORDER BY name")


def emp_name(tg_id):
    if tg_id is None:
        return "—"
    e = emp(tg_id, active_only=False)
    return (e["name"] + ("" if e["active"] else " (ishdan ketgan)")) if e else str(tg_id)


def set_owner(inn, uid, day, name=""):
    """Korxonani `day` (YYYY-MM-DD) sanasidan boshlab `uid` ga o'tkazadi (uid=None — hech kimga).
    Oldingi hodimning davri shu sanada yopiladi, tarix saqlanadi. True — o'zgarish bo'ldi."""
    with LOCK:
        cur = db.execute("SELECT * FROM assignments WHERE inn=? AND end IS NULL ORDER BY start DESC LIMIT 1",
                         (inn,)).fetchone()
        db.execute("INSERT INTO companies(inn,name,tg_id) VALUES(?,?,?) ON CONFLICT(inn) DO UPDATE SET "
                   "tg_id=excluded.tg_id, name=COALESCE(NULLIF(excluded.name,''), companies.name)", (inn, name, uid))
        if (cur and cur["tg_id"] == uid and cur["start"] <= day) or (not cur and uid is None):
            db.commit()
            return False
        # `day` dan keyingi hamma narsa yangi qaror bilan almashadi:
        db.execute("DELETE FROM assignments WHERE inn=? AND start>=?", (inn, day))
        db.execute("UPDATE assignments SET end=? WHERE inn=? AND start<? AND (end IS NULL OR end>?)",
                   (day, inn, day, day))
        if uid is not None:
            prev = db.execute("SELECT id FROM assignments WHERE inn=? AND end=? AND tg_id=?", (inn, day, uid)).fetchone()
            if prev:   # oldingi davr ham shu hodimniki — davom ettiramiz
                db.execute("UPDATE assignments SET end=NULL WHERE id=?", (prev["id"],))
            else:
                db.execute("INSERT INTO assignments(inn,tg_id,start,end,created) VALUES(?,?,?,NULL,?)",
                           (inn, uid, day, now().isoformat()))
        db.commit()
    return True


def responsible(inn, day):
    """`day` (YYYY-MM-DD) kuni korxona uchun mas'ul hodim tg_id (yoki None)."""
    r = q("SELECT tg_id FROM assignments WHERE inn=? AND start<=? AND (end IS NULL OR end>?) "
          "ORDER BY start DESC LIMIT 1", (inn, day, day), one=True)
    return r["tg_id"] if r else None


def report_day(rec, ts):
    """Hisobot sanasi: 'Дата отправки' (DD.MM.YYYY ...) bo'lsa o'sha, bo'lmasa qabul qilingan vaqt."""
    m = re.match(r"\s*(\d{2})\.(\d{2})\.(\d{4})", str((rec or {}).get("Дата отправки") or ""))
    return f"{m.group(3)}-{m.group(2)}-{m.group(1)}" if m else (ts or "")[:10]


def parse_day(s):
    """'bugun', 'DD.MM.YYYY' yoki 'YYYY-MM-DD' -> 'YYYY-MM-DD' (kelajak sana bo'lmaydi). Noto'g'ri bo'lsa None."""
    s = (s or "").strip().lower()
    today = now().date()
    if s in ("", "bugun", "today"):
        return today.isoformat()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d.%m.%y"):
        try:
            d = datetime.strptime(s, fmt).date()
            return min(d, today).isoformat()
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- telegram
_tls = threading.local()  # har oqim uchun doimiy (keep-alive) HTTPS ulanish — har so'rovda TLS qayta ochilmaydi


def tg(method, **params):
    import http.client
    body = json.dumps(params).encode()
    for attempt in (1, 2):  # ulanish uzilgan bo'lsa bir marta qayta ochamiz
        conn = getattr(_tls, "conn", None)
        if conn is None:
            conn = _tls.conn = http.client.HTTPSConnection("api.telegram.org", timeout=70)
        try:
            conn.request("POST", f"/bot{TOKEN}/{method}", body=body,
                         headers={"Content-Type": "application/json", "Connection": "keep-alive"})
            r = conn.getresponse()
            data = r.read()
            if r.status != 200:
                print("TG error", method, r.status, data[:300])
                return None
            return json.loads(data).get("result")
        except Exception as e:  # network hiccup / server ulanishni yopgan
            try:
                conn.close()
            except Exception:
                pass
            _tls.conn = None
            if attempt == 2:
                print("TG fail", method, e)
    return None


def send(chat, text):
    """Send HTML text, splitting messages longer than Telegram's limit."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    ok = True
    for c in chunks:
        if c.strip():
            ok = tg("sendMessage", chat_id=chat, text=c, parse_mode="HTML",
                    disable_web_page_preview=True) is not None and ok
    return ok


# ---------------------------------------------------------------- violations
def period_start(d):
    return d.replace(day=1) if PERIOD == "month" else date(2000, 1, 1)


def add_violation(e, d, kind, detail):
    if q("SELECT 1 FROM violations WHERE tg_id=? AND day=?", (e["tg_id"], d.isoformat()), one=True):
        return
    n = q("SELECT COUNT(*) c FROM violations WHERE tg_id=? AND day>=? AND day<?",
          (e["tg_id"], period_start(d).isoformat(), d.isoformat()), one=True)["c"] + 1
    level = min(n, len(PENALTIES))
    pct, rule = PENALTIES[level - 1]
    q("INSERT INTO violations(tg_id,day,kind,level,fine_pct,created) VALUES(?,?,?,?,?,?)",
      (e["tg_id"], d.isoformat(), kind, level, pct or 0, now().isoformat()))

    what = "Kech qoldi" if kind == "late" else "Ishga kelmadi (video yubormadi)"
    period_txt = "bu oyda" if PERIOD == "month" else "jami"
    fine_txt = ""
    if pct:
        fine_txt = f"\n💸 Jarima: {pct}%" + (f" = <b>{money(e['salary'] * pct // 100)}</b>" if e["salary"]
                                            else " (maosh kiritilmagan: /salary)")
    send(OWNER,
         f"⏰ <b>{what}</b>: {esc(e['name'])}\n{esc(detail)}\n"
         f"📊 {period_txt.capitalize()} {n}-marta → {rule}{fine_txt}"
         + ("\n\n❗️ Qaror sizda. Bekor qilish uchun: /forgive " + str(e["tg_id"]) if level >= 2 else ""))

    if level < len(PENALTIES):
        send(e["tg_id"],
             f"⚠️ <b>{what}</b>\n{esc(detail)}\n{period_txt.capitalize()} {n}-marta.\n"
             f"Qoida: {rule}.")
    else:
        send(e["tg_id"], f"⚠️ <b>{what}</b>\n{esc(detail)}\n{period_txt.capitalize()} {n}-marta. "
                         f"Rahbariyat bilan bog'laning.")


# ---------------------------------------------------------------- attendance
def handle_video(msg, e):
    uid = e["tg_id"]
    if msg.get("forward_origin") or msg.get("forward_date") or msg.get("forward_from"):
        send(uid, "❌ Qayta yuborilgan (forward) video qabul qilinmaydi. Hozir yangi yumaloq video yozing.")
        return
    t = datetime.fromtimestamp(msg["date"], TZ)  # Telegram server vaqti — o'zgartirib bo'lmaydi
    d = t.date()
    if not is_workday(d):
        send(uid, "Bugun dam olish kuni 🙂 Video qayd etilmadi.")
        return
    if t >= at(d, WORK_END):
        send(uid, "Bugungi ish vaqti tugagan, video qayd etilmadi.")
        return
    if q("SELECT 1 FROM checkins WHERE tg_id=? AND day=?", (uid, d.isoformat()), one=True):
        send(uid, "✅ Bugun allaqachon qayd etilgansiz.")
        return

    late_sec = (t - at(d, WORK_START)).total_seconds()
    minutes_late = int(late_sec // 60) if late_sec >= 60 else 0
    status = "late" if minutes_late else "on_time"
    q("INSERT INTO checkins(tg_id,day,ts,status,minutes_late,file_id) VALUES(?,?,?,?,?,?)",
      (uid, d.isoformat(), t.isoformat(), status, minutes_late, msg["video_note"]["file_id"]))

    if FORWARD_VIDEOS:
        tg("forwardMessage", chat_id=OWNER, from_chat_id=uid, message_id=msg["message_id"])
    if status == "on_time":
        send(uid, f"✅ Qayd etildi: {t:%H:%M}. Xayrli ish kuni!")
    else:
        add_violation(e, d, "late", f"Keldi: {t:%H:%M} ({minutes_late} daqiqa kech)")


def mark_absent(d):
    for e in active_employees():
        if q("SELECT 1 FROM checkins WHERE tg_id=? AND day=?", (e["tg_id"], d.isoformat()), one=True):
            continue
        q("INSERT INTO checkins(tg_id,day,ts,status) VALUES(?,?,?,?)",
          (e["tg_id"], d.isoformat(), now().isoformat(), "absent"))
        if ABSENT_IS_VIOLATION:
            add_violation(e, d, "absent", f"{d:%d.%m.%Y} kuni video yuborilmadi")


# ---------------------------------------------------------------- statistics
def stats(a, b):
    """Per-employee stats for days a..b (inclusive), counting only days that have started."""
    n = now()
    today = n.date()
    all_counted = [d for d in workdays(a, min(b, today))
                   if d < today or n >= at(d, WORK_START) + timedelta(minutes=1)]
    out = []
    for e in active_employees():
        rows = q("SELECT * FROM checkins WHERE tg_id=? AND day>=? AND day<=?",
                 (e["tg_id"], a.isoformat(), b.isoformat()))
        by = {r["day"]: r for r in rows}
        # Hodim qo'shilgan kundan oldingi kunlar hisobga olinmaydi.
        first = q("SELECT MIN(day) d FROM checkins WHERE tg_id=?", (e["tg_id"],), one=True)["d"]
        joined = min(filter(None, [(e["created"] or "")[:10], first]), default=a.isoformat())
        counted = [d for d in all_counted if d.isoformat() >= joined]
        on_time = sum(1 for d in counted if by.get(d.isoformat()) and by[d.isoformat()]["status"] == "on_time")
        late = sum(1 for d in counted if by.get(d.isoformat()) and by[d.isoformat()]["status"] == "late")
        absent = sum(1 for d in counted if by.get(d.isoformat()) and by[d.isoformat()]["status"] == "absent")
        pending = len(counted) - on_time - late - absent          # bugun hali kelmagan
        late_minutes = sum(r["minutes_late"] or 0 for r in rows)
        ms = period_start(today)
        viol = q("SELECT * FROM violations WHERE tg_id=? AND day>=? ORDER BY day", (e["tg_id"], ms.isoformat()))
        fine_pct = sum(v["fine_pct"] for v in viol)
        level = max([v["level"] for v in viol], default=0)
        comps = q("SELECT inn,name FROM companies WHERE tg_id=? ORDER BY name", (e["tg_id"],))
        reps = q("SELECT COUNT(DISTINCT report_id) c FROM reports WHERE tg_id=? AND ts>=? AND ts<?",
                 (e["tg_id"], a.isoformat(), (b + timedelta(days=1)).isoformat()), one=True)["c"]
        eff = round(100 * on_time / len(counted)) if counted else None
        today_row = by.get(today.isoformat())
        out.append(dict(e=e, days=len(counted), on_time=on_time, late=late, absent=absent, pending=pending,
                        late_minutes=late_minutes, eff=eff, viol=len(viol), fine_pct=fine_pct,
                        fine_sum=e["salary"] * fine_pct // 100, level=level, comps=comps, reports=reps,
                        today=today_row))
    return out


def week_range(d=None):
    d = d or now().date()
    a = d - timedelta(days=d.weekday())
    return a, a + timedelta(days=6)


def summary_text(title, a, b):
    lines = [f"📊 <b>{title}</b> ({a:%d.%m}–{b:%d.%m})", ""]
    for s in stats(a, b):
        eff = "—" if s["eff"] is None else f"{s['eff']}%"
        lines.append(f"👷 <b>{esc(s['e']['name'])}</b> — samaradorlik <b>{eff}</b>")
        lines.append(f"   ✅ {s['on_time']}  ⏰ {s['late']} ({s['late_minutes']} daq)  ❌ {s['absent']}"
                     f"  📄 hisobot: {s['reports']}")
        if s["viol"]:
            lines.append(f"   ⚠️ Oy bo'yicha qoidabuzarlik: {s['viol']} | jarima {s['fine_pct']}%"
                         + (f" = {money(s['fine_sum'])}" if s["e"]["salary"] else ""))
    if len(lines) == 2:
        lines.append("Hodimlar yo'q. /add bilan qo'shing.")
    return "\n".join(lines)


def morning_text():
    d = now().date()
    came, late, missing = [], [], []
    for e in active_employees():
        r = q("SELECT * FROM checkins WHERE tg_id=? AND day=?", (e["tg_id"], d.isoformat()), one=True)
        if not r:
            missing.append(e["name"])
        elif r["status"] == "on_time":
            came.append(f"{e['name']} ({r['ts'][11:16]})")
        else:
            late.append(f"{e['name']} ({r['ts'][11:16]})")
    return (f"🌅 <b>{d:%d.%m.%Y} davomat ({WORK_START})</b>\n"
            f"✅ Vaqtida: {esc(', '.join(came)) or '—'}\n"
            f"⏰ Kech: {esc(', '.join(late)) or '—'}\n"
            f"❓ Hali kelmagan: {esc(', '.join(missing)) or '—'}")


# ---------------------------------------------------------------- reports from soliq.uz
ICONS = {"№ Отчета": "🆔", "№ Пакет": "📦", "Наименование": "📄", "Год": "📅", "Период": "🗓",
         "Дата отправки": "📤", "Сертификат": "🔐", "Состояние": "✅", "Дата проверки": "🔎",
         "Инспектор": "👤", "Регион": "📍", "Дата создания": "🕓", "IP адрес": "🌐", "Причина ошибки": "⚠️"}


def status_level(status, error_reason=None):
    """Hisobot holati -> ("good" | "bad" | "wait", o'zbekcha nomi)."""
    s = (status or "").lower()
    if error_reason or any(w in s for w in ("ошиб", "отклон", "отказ", "аннулир")):
        return "bad", "Xato bilan qaytarildi"
    if "опоздан" in s:
        return "bad", "Kechikib topshirildi"
    if any(w in s for w in ("вовремя", "принят", "проведен")):
        return "good", "Vaqtida topshirildi" if "вовремя" in s else "Qabul qilindi"
    return "wait", "Tekshiruvda"


def handle_report(e, body):
    rec = body.get("rec") or {}
    rid = str(rec.get("№ Отчета", "")).strip()
    status = str(rec.get("Состояние", "")).strip()
    if not rid.isdigit():
        return 400, {"error": "bad report"}
    inn = "".join(ch for ch in str(body.get("inn") or "") if ch.isdigit())
    company = str(body.get("company") or "").strip()
    cert = str(body.get("cert") or "").strip()

    is_update = q("SELECT 1 FROM reports WHERE report_id=?", (rid,), one=True) is not None
    try:
        with LOCK:
            db.execute("INSERT INTO reports(report_id,status,inn,company,tg_id,data,ts) VALUES(?,?,?,?,?,?,?)",
                       (rid, status, inn, company, e["tg_id"], json.dumps(rec, ensure_ascii=False),
                        now().isoformat()))
            db.commit()
    except sqlite3.IntegrityError:
        return 200, {"ok": True, "duplicate": True}

    c = q("SELECT * FROM companies WHERE inn=?", (inn,), one=True)
    if c and company and not c["name"]:
        q("UPDATE companies SET name=? WHERE inn=?", (company, inn))
    resp = responsible(inn, report_day(rec, now().isoformat()))   # hisobot sanasidagi mas'ul hodim
    if resp is None:
        note = "⚠️ Bu firma hech bir hodimga biriktirilmagan"
    elif resp != e["tg_id"]:
        note = f"⚠️ Bu firma boshqa hodimga biriktirilgan: {esc(emp_name(resp))}"
    else:
        note = ""

    level, label = status_level(status, rec.get("Причина ошибки"))
    title = "🔄 Hisobot holati o'zgardi" if is_update else "🧾 Yangi hisobot"
    if level == "bad":
        lines = ["🚨🚨🚨 <b>DIQQAT! " + esc(label.upper()) + "</b> 🚨🚨🚨",
                 f"🔴 <b>{title}</b>", f"<b>{esc(company)}</b>", ""]
    elif level == "good":
        lines = [f"🟢 <b>{title} — {esc(label)}</b>", f"<b>{esc(company)}</b>", ""]
    else:
        lines = [f"🟡 <b>{title} — {esc(label)}</b>", f"<b>{esc(company)}</b>", ""]
    for k, v in rec.items():
        if v:
            if k == "Состояние":
                dot = {"good": "🟢", "bad": "🔴"}.get(level, "🟡")
                lines.append(f"{dot} <b>{esc(str(k))}:</b> <b>{esc(str(v))}</b>")
            else:
                lines.append(f"{ICONS.get(k, '•')} <b>{esc(str(k))}:</b> {esc(str(v))}")
    if level == "bad":
        lines += ["", "❗️ <b>Zudlik bilan tekshiring!</b>"]
    lines += ["", "━━━━━━━━━━━━━━━",
              f"🏢 <b>Kabinet:</b> {esc(company or '—')}",
              f"🔢 <b>ИНН:</b> <code>{esc(inn)}</code>"]
    if cert:
        lines.append(f"🔑 <b>Kabinet sertifikati:</b> {esc(cert)}")
    lines.append(f"👷 <b>Yuborgan hodim:</b> {esc(e['name'])}")
    if resp is not None and resp != e["tg_id"]:
        lines.append(f"🧑‍💼 <b>Mas'ul hodim:</b> {esc(emp_name(resp))}")
    if note:
        lines.append(note)
    lines.append(f"#INN{inn}")
    send(OWNER, "\n".join(lines))
    return 200, {"ok": True}


# ---------------------------------------------------------------- employees & companies (bot + dashboard)
COMPANY_LINE = re.compile(r"^\s*(\d{9}|\d{14})(?!\d)[\s,;:\-–—]*(.*?)\s*$")
PENDING = {}  # owner keyingi matnda nima yuborishini kutyapmiz: {"assign": tg_id}


def current_url():
    """Joriy tashqi manzil (tunnel qayta ishga tushsa o'zgaradi — shuning uchun fayldan o'qiymiz)."""
    try:
        with open(os.path.join(BASE, "config.json"), encoding="utf-8") as f:
            return json.load(f).get("PUBLIC_URL", PUBLIC_URL).rstrip("/")
    except Exception:
        return PUBLIC_URL


def script_help(key):
    return (f"🧩 <b>soliq.uz skriptini o'rnatish</b> (bir marta):\n"
            f"1) Chrome'ga <b>Tampermonkey</b> kengaytmasini o'rnating (Chrome Web Store)\n"
            f"2) Shu havolani oching va <b>Install / Установить</b> ni bosing:\n{current_url()}/skript.user.js\n"
            f"3) oldmy.soliq.uz kabinetiga kiring — skript kalit so'raydi, shuni kiriting:\n<code>{key}</code>")


def add_employee(uid, name):
    """Hodimni qo'shadi (yoki ismini yangilaydi). Hodimga xabar yetgan-yetmaganini qaytaradi."""
    old = emp(uid, active_only=False)
    key = old["api_key"] if old else secrets.token_urlsafe(9)
    q("INSERT INTO employees(tg_id,name,api_key,active,created) VALUES(?,?,?,1,?) "
      "ON CONFLICT(tg_id) DO UPDATE SET name=excluded.name, active=1", (uid, name, key, now().isoformat()))
    return send(uid, f"👋 Assalomu alaykum, {esc(name)}!\nSiz tizimga qo'shildingiz.\n\n"
                     f"📹 Har ish kuni soat <b>{WORK_START}</b> gacha shu botga <b>yumaloq video</b> yuboring.\n\n"
                     + script_help(key) + "\n\n/key — shu ko'rsatmani qayta ko'rish")


def parse_companies(text):
    """'INN Firma nomi' qatorlari -> ([(inn, nom)], [noto'g'ri qatorlar])."""
    items, bad = [], []
    for line in str(text or "").splitlines():
        if not line.strip():
            continue
        m = COMPANY_LINE.match(line)
        if m:
            items.append((m.group(1), m.group(2)))
        else:
            bad.append(line.strip())
    return items, bad


def assign_companies(uid, items, day=None):
    """[(inn, nom)] ni `day` dan (standart: bugun) hodimga biriktiradi. Boshqa hodimdan olinganlar ro'yxati."""
    day = day or now().date().isoformat()
    moved = []
    for inn, name in items:
        old = q("SELECT tg_id FROM companies WHERE inn=?", (inn,), one=True)
        if old and old["tg_id"] not in (None, uid):
            moved.append(f"{inn} ({emp_name(old['tg_id'])} dan)")
        set_owner(inn, uid, day, name)
    return moved


def company_label(inn):
    c = q("SELECT name FROM companies WHERE inn=?", (inn,), one=True)
    return f"{c['name'] or '(nomsiz)'} — {inn}" if c else inn


def transfer_companies(from_uid, to_uid, day, inns=None):
    """`from_uid` ning korxonalarini (yoki faqat `inns` ni) `day` sanasidan `to_uid` ga o'tkazadi.
    to_uid=None — biriktirilmagan holatga. Ko'chirilgan INN lar ro'yxatini qaytaradi."""
    own = [r["inn"] for r in q("SELECT inn FROM companies WHERE tg_id=?", (from_uid,))]
    inns = [i for i in (inns if inns is not None else own) if i in own]
    for inn in inns:
        set_owner(inn, to_uid, day)
    return inns


def fire_employee(uid, to_uid, day):
    """Hodimni ishdan bo'shatadi: korxonalari `day` dan `to_uid` ga (yoki hech kimga) o'tadi.
    Hodimning ismi va tarixi saqlanadi."""
    moved = transfer_companies(uid, to_uid, day)
    q("UPDATE employees SET active=0 WHERE tg_id=?", (uid,))
    return moved


def assignment_history(inn=None, uid=None, limit=200):
    where, args = [], []
    if inn:
        where.append("a.inn=?"); args.append(inn)
    if uid:
        where.append("a.tg_id=?"); args.append(uid)
    sql = ("SELECT a.*, c.name cname FROM assignments a LEFT JOIN companies c ON c.inn=a.inn "
           + ("WHERE " + " AND ".join(where) if where else "") + " ORDER BY a.start DESC, a.id DESC LIMIT ?")
    return q(sql, (*args, limit))


def fmt_day(iso):
    if not iso or iso <= "2000-01-01":
        return "boshidan"
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}"


def kb(rows):
    return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in row] for row in rows]}


def send_kb(chat, text, rows):
    return tg("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
              disable_web_page_preview=True, reply_markup=kb(rows)) is not None


def ask_company_employee():
    emps = active_employees()
    if not emps:
        return send(OWNER, "Hodimlar yo'q. Avval /add TG_ID Ism bilan qo'shing.")
    rows = [[(f"👷 {e['name']}", f"asg:{e['tg_id']}")] for e in emps] + [[("❌ Bekor qilish", "cancel")]]
    send_kb(OWNER, "🏢 <b>Qaysi hodimga korxona qo'shamiz?</b>", rows)


def ask_company_lines(uid):
    e = emp(uid)
    if not e:
        return send(OWNER, "Bunday hodim yo'q. Avval /add qiling.")
    PENDING["assign"] = uid
    send_kb(OWNER, f"🏢 <b>{esc(e['name'])}</b> uchun korxonalarni yozing.\n"
                   f"Har qatorga bittadan: <b>INN Firma nomi</b>\n\nMasalan:\n"
                   f"<code>309497371 UNI-TAX CONSULT MCHJ\n305123456 BEKOBOD ASFALT BETON MCHJ</code>",
            [[("❌ Bekor qilish", "cancel")]])


def finish_company_lines(text):
    uid = PENDING.pop("assign")
    e = emp(uid)
    items, bad = parse_companies(text)
    if not e:
        return send(OWNER, "Hodim topilmadi.")
    if not items:
        PENDING["assign"] = uid
        return send_kb(OWNER, "INN topilmadi. Har qatorga: <code>INN Firma nomi</code> (INN 9 yoki 14 raqam).",
                       [[("❌ Bekor qilish", "cancel")]])
    moved = assign_companies(uid, items)
    lines = [f"✅ <b>{esc(e['name'])}</b>ga {len(items)} ta korxona biriktirildi:"]
    lines += [f"   • {esc(n or '(nomsiz)')} — {i}" for i, n in items]
    if moved:
        lines.append("\n↪️ Boshqa hodimdan o'tkazildi: " + esc(", ".join(moved)))
    if bad:
        lines.append("\n⚠️ Tushunilmagan qatorlar: " + esc("; ".join(bad)))
    send_kb(OWNER, "\n".join(lines), [[("➕ Yana qo'shish", f"asg:{uid}"), ("📋 Ro'yxat", "list")]])


# --- ko'chirish / ishdan bo'shatish (tugmalar bilan)
def emp_buttons(prefix, exclude=None, extra=None):
    rows = [[(f"👷 {e['name']}", f"{prefix}{e['tg_id']}")] for e in active_employees() if e["tg_id"] != exclude]
    return rows + (extra or []) + [[("❌ Bekor qilish", "cancel")]]


def ask_transfer_from():
    send_kb(OWNER, "⇄ <b>Kimning korxonalarini ko'chiramiz?</b>", emp_buttons("tf:"))


def ask_transfer_to(frm):
    n = len(q("SELECT 1 FROM companies WHERE tg_id=?", (frm,)))
    send_kb(OWNER, f"⇄ <b>{esc(emp_name(frm))}</b> ning {n} ta korxonasi <b>kimga</b> o'tsin?",
            emp_buttons(f"tt:{frm}:", exclude=frm, extra=[[("🚫 Hech kimga (biriktirilmagan)", f"tt:{frm}:0")]]))


def ask_fire_to(uid):
    n = len(q("SELECT 1 FROM companies WHERE tg_id=?", (uid,)))
    send_kb(OWNER, f"🚪 <b>{esc(emp_name(uid))}</b> ishdan bo'shatiladi.\nUning {n} ta korxonasi <b>kimga</b> o'tsin?",
            emp_buttons(f"rt:{uid}:", exclude=uid, extra=[[("🚫 Hech kimga (biriktirilmagan)", f"rt:{uid}:0")]]))


def ask_date(action, a, b):
    """action: 'td' (ko'chirish) yoki 'rd' (bo'shatish). Sanani tugma yoki matn bilan so'raydi."""
    t = now().date()
    m1 = t.replace(day=1)
    pm1 = (m1 - timedelta(days=1)).replace(day=1)
    PENDING["date"] = (action, a, b)
    send_kb(OWNER, "📅 <b>Qaysi sanadan boshlab?</b>\nShu sanadan oldingi hisobotlar eski hodim nomida qoladi.\n"
                   "Boshqa sana uchun yozing: <code>01.11.2026</code>",
            [[(f"Bugundan ({t:%d.%m})", f"{action}:{a}:{b}:{t.isoformat()}")],
             [(f"Shu oy boshidan ({m1:%d.%m})", f"{action}:{a}:{b}:{m1.isoformat()}")],
             [(f"O'tgan oy boshidan ({pm1:%d.%m})", f"{action}:{a}:{b}:{pm1.isoformat()}")],
             [("❌ Bekor qilish", "cancel")]])


def do_transfer(frm, to, day, inns=None):
    to_uid = to or None
    if to_uid and not emp(to_uid):
        return send(OWNER, "Qabul qiluvchi hodim topilmadi.")
    moved = transfer_companies(frm, to_uid, day, inns)
    if not moved:
        return send(OWNER, f"{esc(emp_name(frm))} da ko'chiriladigan korxona yo'q.")
    lines = [f"✅ {len(moved)} ta korxona <b>{fmt_day(day)}</b> dan boshlab "
             f"<b>{esc(emp_name(frm))}</b> → <b>{esc(emp_name(to_uid) if to_uid else 'hech kimga')}</b> o'tkazildi:"]
    lines += [f"   • {esc(company_label(i))}" for i in moved]
    lines.append(f"\n{fmt_day(day)} dan oldingi hisobotlar {esc(emp_name(frm))} nomida qoladi.")
    send_kb(OWNER, "\n".join(lines), [[("📜 Tarix", f"hist:{frm}"), ("📋 Ro'yxat", "list")]])


def do_fire(uid, to, day):
    e = emp(uid)
    if not e:
        return send(OWNER, "Bunday faol hodim yo'q.")
    to_uid = to or None
    if to_uid and not emp(to_uid):
        return send(OWNER, "Qabul qiluvchi hodim topilmadi.")
    moved = fire_employee(uid, to_uid, day)
    send(OWNER, f"🚪 <b>{esc(e['name'])}</b> ishdan bo'shatildi ({fmt_day(day)}).\n"
                f"Korxonalari ({len(moved)} ta) → <b>{esc(emp_name(to_uid) if to_uid else 'biriktirilmagan')}</b>.\n"
                f"Uning ismi va hisobotlar tarixi saqlanadi. Qaytarish: /add {uid} {esc(e['name'])}")


def history_text(inn=None, uid=None):
    rows = assignment_history(inn=inn, uid=uid, limit=40)
    if not rows:
        return "Tarix bo'sh."
    head = (f"📜 <b>Tarix: {esc(company_label(inn))}</b>" if inn else
            f"📜 <b>Tarix: {esc(emp_name(uid))}</b>" if uid else "📜 <b>Oxirgi o'zgarishlar</b>")
    lines = [head, ""]
    for r in rows:
        period = f"{fmt_day(r['start'])} → {fmt_day(r['end']) if r['end'] else 'hozirgacha'}"
        who = "" if uid else f"👷 {esc(emp_name(r['tg_id']))}  "
        what = "" if inn else f"🏢 {esc(r['cname'] or r['inn'])}  "
        lines.append(f"• {what}{who}<i>{period}</i>")
    return "\n".join(lines)


def handle_callback(cq):
    tg("answerCallbackQuery", callback_query_id=cq["id"])
    if cq.get("from", {}).get("id") != OWNER:
        return
    data = cq.get("data") or ""
    p = data.split(":")
    if data.startswith("asg:") and data[4:].isdigit():
        ask_company_lines(int(data[4:]))
    elif p[0] == "tf" and len(p) == 2:
        ask_transfer_to(int(p[1]))
    elif p[0] == "tt" and len(p) == 3:
        ask_date("td", int(p[1]), int(p[2]))
    elif p[0] == "td" and len(p) == 4:
        PENDING.clear()
        do_transfer(int(p[1]), int(p[2]), p[3])
    elif p[0] == "rm" and len(p) == 2:
        ask_fire_to(int(p[1]))
    elif p[0] == "rt" and len(p) == 3:
        ask_date("rd", int(p[1]), int(p[2]))
    elif p[0] == "rd" and len(p) == 4:
        PENDING.clear()
        do_fire(int(p[1]), int(p[2]), p[3])
    elif p[0] == "hist" and len(p) == 2:
        send(OWNER, history_text(uid=int(p[1])))
    elif data == "cancel":
        PENDING.clear()
        send(OWNER, "Bekor qilindi.")
    elif data == "list":
        handle_owner("/list")


# ---------------------------------------------------------------- bot commands
OWNER_HELP = f"""👑 <b>Owner buyruqlari</b>

<b>Hodimlar</b>
/add TG_ID Ism Familiya — hodim qo'shish
/salary TG_ID SUMMA — oylik maosh (jarima hisobi uchun)
/remove — hodimni ishdan bo'shatish (tugmalar bilan)
/remove TG_ID [YANGI_ID] [sana] — bo'shatish, korxonalari YANGI_ID ga o'tadi
/list — hodimlar va ularning firmalari

<b>Korxonalar</b>
/firma — tugmalar orqali hodimga korxona qo'shish
/assign TG_ID INN [INN ...] — firmalarni hodimga biriktirish
/assign TG_ID INN Firma nomi — bitta firmani nomi bilan
/unassign INN [INN ...] — biriktirishni olib tashlash
/kochir — bir hodimning hamma korxonalarini boshqasiga (tugmalar bilan)
/kochir ESKI_ID YANGI_ID [sana] — hammasini ko'chirish
/move YANGI_ID INN [INN ...] [sana] — tanlangan korxonalarni ko'chirish
/tarix [INN yoki TG_ID] — kim qaysi korxonaga qachondan qachongacha qaragan
(sana: 01.11.2026; yozilmasa — bugundan)

<b>Hisobotlar</b>
/today — bugungi davomat
/week — shu hafta
/month — shu oy
/forgive TG_ID — oxirgi qoidabuzarlikni bekor qilish
/dashboard — dashboard havolasi

Ish vaqti: {WORK_START}–{WORK_END}, hodim {WORK_START} gacha yumaloq video yuboradi."""


def handle_owner(text):
    parts = text.split()
    cmd = parts[0].split("@")[0].lower()
    args = parts[1:]

    def need_emp(i=0):
        if len(args) <= i or not args[i].lstrip("-").isdigit():
            send(OWNER, "TG_ID raqam bo'lishi kerak.")
            return None
        return int(args[i])

    if cmd in ("/start", "/help"):
        send(OWNER, OWNER_HELP)
    elif cmd == "/add" and not args:
        PENDING["add"] = True
        send_kb(OWNER, "👷 Yangi hodimning <b>Telegram ID</b> va <b>ismini</b> yozing:\n"
                       "<code>1822803285 Nilufar Karimova</code>", [[("❌ Bekor qilish", "cancel")]])
    elif cmd == "/add":
        uid = need_emp()
        if uid is None:
            return
        name = " ".join(args[1:]).strip()
        if not name:
            return send(OWNER, "Foydalanish: /add TG_ID Ism Familiya")
        delivered = add_employee(uid, name)
        send_kb(OWNER, f"✅ {esc(name)} qo'shildi (ID {uid})."
                       + ("" if delivered else "\n⚠️ Unga xabar yetmadi — hodim avval botga /start yozishi kerak."),
                [[("➕ Korxona biriktirish", f"asg:{uid}")]])
    elif cmd == "/salary":
        uid = need_emp()
        if uid is None:
            return
        if len(args) < 2 or not args[1].replace(" ", "").isdigit():
            return send(OWNER, "Foydalanish: /salary TG_ID 5000000")
        q("UPDATE employees SET salary=? WHERE tg_id=?", (int(args[1]), uid))
        send(OWNER, f"✅ Maosh saqlandi: {money(int(args[1]))}")
    elif cmd == "/remove" and not args:
        send_kb(OWNER, "🚪 <b>Kimni ishdan bo'shatamiz?</b>", emp_buttons("rm:"))
    elif cmd == "/remove":
        uid = need_emp()
        if uid is None:
            return
        to = int(args[1]) if len(args) > 1 and args[1].isdigit() else 0
        day = parse_day(args[2] if len(args) > 2 else (args[1] if len(args) > 1 and not args[1].isdigit() else ""))
        if day is None:
            return send(OWNER, "Sana noto'g'ri. Masalan: /remove 7758605480 1822803285 01.11.2026")
        do_fire(uid, to, day)
    elif cmd in ("/kochir", "/transfer") and not args:
        ask_transfer_from()
    elif cmd in ("/kochir", "/transfer"):
        if len(args) < 2 or not args[0].isdigit() or not args[1].isdigit():
            return send(OWNER, "Foydalanish: /kochir ESKI_ID YANGI_ID [01.11.2026]\n(YANGI_ID=0 — hech kimga)")
        day = parse_day(args[2] if len(args) > 2 else "")
        if day is None:
            return send(OWNER, "Sana noto'g'ri. Masalan: 01.11.2026")
        do_transfer(int(args[0]), int(args[1]), day)
    elif cmd == "/move":
        uid = need_emp()
        if uid is None:
            return
        inns = [a for a in args[1:] if a.isdigit() and len(a) in (9, 14)]
        rest = [a for a in args[1:] if a not in inns]
        day = parse_day(rest[0] if rest else "")
        if not inns or day is None:
            return send(OWNER, "Foydalanish: /move YANGI_ID INN [INN ...] [01.11.2026]")
        if uid and not emp(uid):
            return send(OWNER, "Bunday hodim yo'q.")
        for inn in inns:
            set_owner(inn, uid or None, day)
        send_kb(OWNER, f"✅ {len(inns)} ta korxona {fmt_day(day)} dan <b>{esc(emp_name(uid or None))}</b> ga o'tkazildi.",
                [[("📋 Ro'yxat", "list")]])
    elif cmd == "/tarix":
        a = args[0] if args else ""
        if a.isdigit() and len(a) in (9, 14) and q("SELECT 1 FROM companies WHERE inn=?", (a,), one=True):
            send(OWNER, history_text(inn=a))
        elif a.isdigit():
            send(OWNER, history_text(uid=int(a)))
        else:
            send(OWNER, history_text())
    elif cmd == "/firma" or (cmd == "/assign" and not args):
        ask_company_employee()
    elif cmd == "/assign":
        uid = need_emp()
        if uid is None:
            return
        if not emp(uid):
            return send(OWNER, "Bunday hodim yo'q. Avval /add qiling.")
        rest = args[1:]
        if not rest:
            return ask_company_lines(uid)
        inns = []
        while rest and rest[0].isdigit():
            inns.append(rest.pop(0))
        name = " ".join(rest).strip() if len(inns) == 1 else ""
        if not inns:
            return send(OWNER, "Foydalanish: /assign TG_ID INN [INN ...]")
        moved = assign_companies(uid, [(inn, name) for inn in inns])
        send_kb(OWNER, f"✅ {len(inns)} ta firma {esc(emp(uid)['name'])}ga biriktirildi."
                       + ("\n↪️ Boshqa hodimdan o'tkazildi: " + esc(", ".join(moved)) if moved else ""),
                [[("➕ Yana qo'shish", f"asg:{uid}"), ("📋 Ro'yxat", "list")]])
    elif cmd == "/cancel":
        PENDING.clear()
        send(OWNER, "Bekor qilindi.")
    elif cmd == "/unassign":
        for inn in args:
            if inn.isdigit():
                set_owner(inn, None, now().date().isoformat())
        send(OWNER, "✅ Bajarildi (tarix saqlandi).")
    elif cmd == "/list":
        lines = ["👥 <b>Hodimlar</b>"]
        for e in active_employees():
            comps = q("SELECT inn,name FROM companies WHERE tg_id=? ORDER BY name", (e["tg_id"],))
            lines.append(f"\n👷 <b>{esc(e['name'])}</b> — ID <code>{e['tg_id']}</code>, "
                         f"maosh {money(e['salary']) if e['salary'] else '—'}, firmalar: {len(comps)}")
            for c in comps:
                lines.append(f"   • {esc(c['name'] or '(nomi hali yo‘q)')} — {c['inn']}")
        free = q("SELECT inn,name FROM companies WHERE tg_id IS NULL")
        if free:
            lines.append("\n❔ <b>Biriktirilmagan firmalar</b>")
            lines += [f"   • {esc(c['name'] or '')} — {c['inn']}" for c in free]
        send(OWNER, "\n".join(lines))
    elif cmd == "/today":
        send(OWNER, morning_text())
    elif cmd == "/week":
        a, b = week_range()
        send(OWNER, summary_text("Haftalik hisobot", a, b))
    elif cmd == "/month":
        d = now().date()
        a = d.replace(day=1)
        b = (a + timedelta(days=32)).replace(day=1) - timedelta(days=1)
        send(OWNER, summary_text("Oylik hisobot", a, b))
    elif cmd == "/forgive":
        uid = need_emp()
        if uid is None:
            return
        v = q("SELECT * FROM violations WHERE tg_id=? ORDER BY day DESC LIMIT 1", (uid,), one=True)
        if not v:
            return send(OWNER, "Qoidabuzarlik yo'q.")
        q("DELETE FROM violations WHERE id=?", (v["id"],))
        send(OWNER, f"✅ {v['day']} dagi qoidabuzarlik bekor qilindi.")
    elif cmd == "/dashboard":
        url = current_url()
        send(OWNER, f"📈 Dashboard: {url}/dashboard\n🧩 Hodimlar skripti: {url}/skript.user.js")
    else:
        send(OWNER, "Noma'lum buyruq. /help")


def log(*parts):
    line = f"{now():%Y-%m-%d %H:%M:%S} " + " ".join(str(p) for p in parts)
    print(line)
    try:
        with open(os.path.join(BASE, "bot.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def handle_update(u):
    if "callback_query" in u:
        cq = u["callback_query"]
        log("CALLBACK", u.get("update_id"), "from", cq.get("from", {}).get("id"), repr(cq.get("data")))
        return handle_callback(cq)
    msg = u.get("message")
    if msg:
        kind = next((k for k in ("text", "video_note", "video", "photo", "document") if k in msg), "other")
        log("UPDATE", u.get("update_id"), "from", msg.get("from", {}).get("id"),
            "chat", msg.get("chat", {}).get("type"), kind, repr((msg.get("text") or "")[:300]))
    if not msg or msg.get("chat", {}).get("type") != "private":
        return
    uid = msg["from"]["id"]
    text = (msg.get("text") or "").strip()

    if uid == OWNER and text.startswith("/"):
        PENDING.clear()  # yangi buyruq — kutilayotgan kiritish bekor
        # Bir xabarda bir nechta buyruq bo'lsa (har biri yangi qatorda) — bittadan bajariladi.
        for line in text.splitlines():
            if line.strip().startswith("/"):
                handle_owner(line.strip())
        return
    if uid == OWNER and text and PENDING.pop("add", None):
        return handle_owner("/add " + text)
    if uid == OWNER and text and "assign" in PENDING:
        return finish_company_lines(text)
    if uid == OWNER and text and "date" in PENDING:
        day = parse_day(text)
        if day is None:
            return send(OWNER, "Sana noto'g'ri. Masalan: <code>01.11.2026</code>")
        action, a, b = PENDING.pop("date")
        return do_transfer(a, b, day) if action == "td" else do_fire(a, b, day)

    e = emp(uid)
    if not e:
        if uid != OWNER:
            send(uid, f"Siz hali ro'yxatda yo'qsiz.\nSizning Telegram ID: <code>{uid}</code>\n"
                      f"Shu raqamni rahbaringizga yuboring.")
        return
    if "video_note" in msg:
        return handle_video(msg, e)
    if "video" in msg:
        return send(uid, "❌ Oddiy video emas — <b>yumaloq video</b> (video xabar) yuboring.")
    if text.startswith("/key"):
        return send(uid, script_help(e["api_key"]))
    if text.startswith("/me") or text.startswith("/start"):
        a, b = week_range()
        s = next((x for x in stats(a, b) if x["e"]["tg_id"] == uid), None)
        eff = "—" if not s or s["eff"] is None else f"{s['eff']}%"
        return send(uid, f"👷 {esc(e['name'])}\nShu hafta samaradorlik: <b>{eff}</b>\n"
                         f"📹 Har ish kuni {WORK_START} gacha yumaloq video yuboring.\n/key — skript kaliti")
    send(uid, f"📹 Davomat uchun yumaloq video yuboring ({WORK_START} gacha). /me — statistikangiz")


import queue
UPDATES = queue.Queue()


def worker():
    """Xabarlarni alohida oqimda ketma-ket bajaradi — polling javob kutib to'xtab qolmaydi."""
    while True:
        u = UPDATES.get()
        try:
            handle_update(u)
        except Exception:
            log("ERROR", traceback.format_exc())


def polling():
    threading.Thread(target=worker, daemon=True).start()
    offset = int((q("SELECT v FROM meta WHERE k='offset'", one=True) or {"v": 0})["v"])
    while True:
        upds = tg("getUpdates", offset=offset, timeout=50, allowed_updates=["message", "callback_query"])
        if upds is None:
            time.sleep(3)
            continue
        for u in upds:
            offset = u["update_id"] + 1
            q("INSERT OR REPLACE INTO meta(k,v) VALUES('offset',?)", (str(offset),))
            UPDATES.put(u)


# ---------------------------------------------------------------- scheduler
def once(key):
    if q("SELECT 1 FROM meta WHERE k=?", (key,), one=True):
        return False
    q("INSERT INTO meta(k,v) VALUES(?,?)", (key, now().isoformat()))
    return True


def scheduler():
    while True:
        try:
            n = now()
            d = n.date()
            if is_workday(d):
                if n >= at(d, WORK_START) + timedelta(minutes=5) and n < at(d, WORK_END) and once(f"morning:{d}"):
                    send(OWNER, morning_text())
                if n >= at(d, WORK_END) and once(f"absent:{d}"):
                    mark_absent(d)
                last_wd = max(workdays(*week_range(d)))
                if d == last_wd and n >= at(d, WORK_END) + timedelta(minutes=5) and once(f"weekly:{d}"):
                    a, b = week_range(d)
                    send(OWNER, summary_text("Haftalik hisobot", a, b))
        except Exception:
            traceback.print_exc()
        time.sleep(30)


# ---------------------------------------------------------------- web: API + dashboard
# Dashboard formalari uchun (boshqa saytdan yuborishdan himoya). Doimiy — bot qayta yonsa ham ochiq sahifa ishlaydi.
CSRF = base64.urlsafe_b64encode(__import__("hashlib").sha256(
    f"csrf|{TOKEN}|{DASH_PASSWORD}".encode()).digest()).decode()[:24]
FLASH = {"emp_ok": "✅ Hodim qo'shildi", "emp_nosend": "✅ Hodim qo'shildi, lekin unga xabar yetmadi — avval botga /start yozsin",
         "asg_ok": "✅ Korxona(lar) biriktirildi", "unasg_ok": "✅ Korxona olib tashlandi",
         "bad_id": "⚠️ Telegram ID faqat raqam bo'lishi kerak", "bad_name": "⚠️ Ism kiritilmagan",
         "bad_inn": "⚠️ INN topilmadi — har qatorga: INN Firma nomi (9 yoki 14 raqam)",
         "no_emp": "⚠️ Bunday hodim topilmadi",
         "tr_ok": "✅ Korxonalar ko'chirildi — eski hisobotlar avvalgi hodim nomida qoldi",
         "tr_none": "⚠️ Ko'chirish uchun korxona tanlanmadi", "bad_day": "⚠️ Sana noto'g'ri",
         "fire_ok": "✅ Hodim ishdan bo'shatildi — ismi va tarixi saqlandi", "same": "⚠️ Hodim o'ziga ko'chirilmaydi",
         "stale": "⚠️ Sahifa eskirgan edi — yangilandi. Iltimos, qayta kiriting."}


def status_pill(status, error_reason=None):
    level, label = status_level(status, error_reason)
    cls = {"good": "g", "bad": "r"}.get(level, "y")
    icon = {"good": "●", "bad": "▲"}.get(level, "◌")
    return (f'<span class="pill {cls}">{icon} {esc(label)}</span>'
            f'<div class="sub">{esc(status or "")}</div>')


def render_dashboard(flash=""):
    a, b = week_range()
    d = now().date()
    ma = d.replace(day=1)
    week = stats(a, b)
    month = {s["e"]["tg_id"]: s for s in stats(ma, d)}
    lvl_txt = {0: "", 1: "1-ogohlantirish", 2: "2-ogohlantirish", 3: "Shartnoma bekor"}

    def pill(v):
        if v is None:
            return '<span class="pill">—</span>'
        cls = "g" if v >= 90 else ("y" if v >= 70 else "r")
        return f'<span class="pill {cls}">{v}%</span>'

    rows = []
    for s in week:
        m = month.get(s["e"]["tg_id"], s)
        t = s["today"]
        today = ("✅ " + t["ts"][11:16] if t and t["status"] == "on_time" else
                 "⏰ " + t["ts"][11:16] if t and t["status"] == "late" else
                 "❌ kelmadi" if t else ("—" if not is_workday(d) else "❓ hali yo'q"))
        lvl = lvl_txt.get(m["level"], "")
        rows.append(f"""<tr>
<td><b>{esc(s['e']['name'])}</b><div class="sub">ID {s['e']['tg_id']}</div></td>
<td>{today}</td>
<td>{pill(s['eff'])}</td>
<td>{s['on_time']} / {s['late']} / {s['absent']}</td>
<td>{pill(m['eff'])}</td>
<td>{m['viol']}{' <span class="pill r">' + lvl + '</span>' if lvl else ''}</td>
<td>{m['fine_pct']}%{'<div class="sub">' + money(m['fine_sum']) + '</div>' if s['e']['salary'] and m['fine_pct'] else ''}</td>
<td>{len(s['comps'])}</td>
<td>{m['reports']}</td></tr>""")

    reps = q("SELECT r.*, e.name ename FROM reports r LEFT JOIN employees e ON e.tg_id=r.tg_id "
             "ORDER BY r.ts DESC LIMIT 40")
    rep_rows = []
    for r in reps:
        data = json.loads(r["data"] or "{}")
        resp = responsible(r["inn"], report_day(data, r["ts"]))   # o'sha sanadagi mas'ul hodim
        diff = resp is not None and resp != r["tg_id"]
        resp_html = esc(emp_name(resp)) if resp else '<span class="pill y">biriktirilmagan</span>'
        sender_html = esc(emp_name(r["tg_id"])) + (' <span class="pill y">boshqa</span>' if diff else "")
        rep_rows.append(f"<tr><td>{r['ts'][:16].replace('T', ' ')}</td><td>{esc(r['company'] or '')}"
                        f"<div class='sub'>{r['inn']}</div></td><td>{esc(data.get('Наименование', ''))}"
                        f"<div class='sub'>{esc(data.get('Период', ''))} {esc(data.get('Год', ''))}</div></td>"
                        f"<td>{status_pill(r['status'], data.get('Причина ошибки'))}</td>"
                        f"<td>{resp_html}</td><td>{sender_html}</td></tr>")

    emp_opts = "".join(f'<option value="{e["tg_id"]}">{esc(e["name"])}</option>' for e in active_employees())
    now_pill = '<span class="pill g">hozirgacha</span>'
    hist_rows = "".join(
        f"<tr><td>{esc(h['cname'] or '')}<div class='sub'>{h['inn']}</div></td><td>{esc(emp_name(h['tg_id']))}</td>"
        f"<td>{fmt_day(h['start'])}</td><td>{fmt_day(h['end']) if h['end'] else now_pill}</td></tr>"
        for h in assignment_history(limit=80))

    team = []
    for s in week:
        e = s["e"]
        chips = "".join(
            f"""<form method="post" action="/dashboard/unassign" class="chip" onsubmit="return confirm('Olib tashlansinmi?')">
<span>{esc(c['name'] or '(nomsiz)')}<span class="sub"> · {c['inn']}</span></span>
<input type="hidden" name="inn" value="{esc(c['inn'])}"><input type="hidden" name="csrf" value="{CSRF}">
<button title="Olib tashlash" aria-label="Olib tashlash">×</button></form>""" for c in s["comps"])
        team.append(f"""<div class="member glass"><div class="mhead"><div><b>{esc(e['name'])}</b>
<div class="sub">ID {e['tg_id']} · {len(s['comps'])} ta korxona</div></div>
<button class="btn ghost" data-asg data-id="{e['tg_id']}" data-name="{esc(e['name'])}">+ Korxona</button></div>
<div class="chips">{chips or '<span class="sub">Korxona biriktirilmagan</span>'}</div>
<div class="macts"><button class="lnk" data-tr data-id="{e['tg_id']}" data-name="{esc(e['name'])}"
 data-comps="{esc(json.dumps([[c['inn'], c['name'] or ''] for c in s['comps']], ensure_ascii=False))}">⇄ Korxonalarni ko‘chirish</button>
<button class="lnk danger" data-fire data-id="{e['tg_id']}" data-name="{esc(e['name'])}">Ishdan bo‘shatish</button></div></div>""")
    flash_html = f'<div class="flash glass">{esc(FLASH[flash])}</div>' if flash in FLASH else ""

    total = len(week)
    on_today = sum(1 for s in week if s["today"] and s["today"]["status"] == "on_time")
    late_today = sum(1 for s in week if s["today"] and s["today"]["status"] == "late")
    effs = [s["eff"] for s in week if s["eff"] is not None]
    avg = round(sum(effs) / len(effs)) if effs else None

    return f"""<!DOCTYPE html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Hodimlar samaradorligi</title><style>
:root{{--ink:#2b1a0f;--mut:#97755e;--acc:#f97316;--acc-soft:rgba(249,115,22,.12);
--glass:rgba(255,255,255,.52);--edge:rgba(255,255,255,.85);--line:rgba(194,65,12,.10);--shadow:rgba(194,65,12,.10);
--bg1:#fff8f1;--bg2:#ffe9d6;--b1:rgba(251,146,60,.55);--b2:rgba(253,186,116,.60);--b3:rgba(249,115,22,.30);
--g:#15803d;--g-bg:rgba(34,197,94,.14);--y:#c2410c;--y-bg:rgba(251,146,60,.18);--r:#b91c1c;--r-bg:rgba(239,68,68,.13)}}
@media (prefers-color-scheme:dark){{:root{{--ink:#fbeee4;--mut:#c19c84;--acc:#fb923c;--acc-soft:rgba(251,146,60,.16);
--glass:rgba(48,28,16,.50);--edge:rgba(255,255,255,.10);--line:rgba(251,146,60,.14);--shadow:rgba(0,0,0,.35);
--bg1:#160c06;--bg2:#24130a;--b1:rgba(234,88,12,.40);--b2:rgba(251,146,60,.22);--b3:rgba(194,65,12,.30);
--g:#4ade80;--g-bg:rgba(74,222,128,.14);--y:#fdba74;--y-bg:rgba(251,146,60,.18);--r:#fca5a5;--r-bg:rgba(248,113,113,.15)}}}}
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;font:14px/1.5 "Segoe UI",system-ui,-apple-system,Roboto,sans-serif;color:var(--ink);
background:radial-gradient(900px 520px at 8% -8%,var(--b1),transparent 62%),
radial-gradient(760px 480px at 105% 6%,var(--b2),transparent 60%),
radial-gradient(820px 560px at 50% 118%,var(--b3),transparent 62%),
linear-gradient(170deg,var(--bg1),var(--bg2));background-attachment:fixed}}
.wrap{{max-width:1180px;margin:0 auto;padding:40px 24px 56px}}
.glass{{background:var(--glass);border:1px solid var(--edge);border-radius:22px;
backdrop-filter:blur(22px) saturate(170%);-webkit-backdrop-filter:blur(22px) saturate(170%);
box-shadow:0 10px 40px -12px var(--shadow),inset 0 1px 0 var(--edge)}}
header{{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}}
h1{{font-size:26px;font-weight:600;letter-spacing:-.02em;margin:0;display:flex;align-items:center;gap:12px}}
h1:before{{content:"";width:12px;height:12px;border-radius:50%;background:var(--acc);box-shadow:0 0 0 6px var(--acc-soft)}}
h2{{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin:36px 4px 12px}}
.mut,.sub{{color:var(--mut)}}.sub{{font-size:12px}}
.meta{{font-size:13px;padding:8px 14px;border-radius:999px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-top:28px}}
.kpi{{padding:20px 22px}}.kpi span{{font-size:13px}}
.kpi b{{display:block;font-size:34px;font-weight:600;letter-spacing:-.03em;margin-top:6px;color:var(--acc)}}
.card{{overflow:auto}}
table{{border-collapse:collapse;width:100%}}
th,td{{padding:13px 18px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top;white-space:nowrap}}
th{{font-size:11px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.06em}}
tr:last-child td{{border-bottom:0}}tbody tr:hover td,tr:hover td{{background:var(--acc-soft)}}th:hover{{background:none}}
.pill{{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:600;font-size:12px;background:var(--acc-soft);color:var(--mut)}}
.pill.g{{background:var(--g-bg);color:var(--g)}}.pill.y{{background:var(--y-bg);color:var(--y)}}.pill.r{{background:var(--r-bg);color:var(--r)}}
.foot{{text-align:center;margin-top:28px}}
.hright{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
.btn{{font:inherit;font-weight:600;font-size:13px;border:0;border-radius:999px;padding:9px 16px;cursor:pointer;
background:var(--acc);color:#fff;box-shadow:0 6px 18px -6px var(--acc);transition:transform .12s,filter .12s}}
.btn:hover{{filter:brightness(1.06);transform:translateY(-1px)}}
.btn.ghost{{background:var(--acc-soft);color:var(--acc);box-shadow:none}}
.team{{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(330px,100%),1fr));gap:14px}}
.member{{padding:18px 20px}}.mhead{{display:flex;justify-content:space-between;align-items:flex-start;gap:12px}}
.chips{{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}}
.chip{{display:inline-flex;align-items:center;gap:4px;margin:0;padding:5px 5px 5px 12px;border-radius:999px;
background:var(--acc-soft);font-size:12.5px;max-width:100%}}
.chip>span{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}
.chip button{{border:0;background:transparent;color:var(--mut);cursor:pointer;font-size:16px;line-height:1;
width:24px;height:24px;border-radius:50%;flex:none}}.chip button:hover{{background:var(--r-bg);color:var(--r)}}
.flash{{margin-top:20px;padding:12px 18px;font-weight:600}}
dialog{{border:1px solid var(--edge);border-radius:24px;padding:26px;width:min(440px,calc(100vw - 32px));
color:var(--ink);background:var(--dlg);backdrop-filter:blur(26px) saturate(170%);-webkit-backdrop-filter:blur(26px) saturate(170%);
box-shadow:0 30px 80px -20px var(--shadow)}}
dialog::backdrop{{background:rgba(70,30,5,.22);backdrop-filter:blur(3px)}}
dialog h3{{margin:0 0 16px;font-size:18px;font-weight:600}}
label{{display:block;font-size:12px;color:var(--mut);font-weight:600;margin-bottom:12px}}
input[type=text],textarea{{display:block;width:100%;margin-top:6px;font:inherit;font-size:14px;color:var(--ink);
padding:10px 13px;border-radius:12px;border:1px solid var(--line);background:var(--field);outline:none}}
input[type=text]:focus,textarea:focus{{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-soft)}}
textarea{{min-height:120px;resize:vertical;font-family:Consolas,ui-monospace,monospace;font-size:13px}}
.actions{{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}}
.macts{{display:flex;gap:16px;margin-top:14px;padding-top:12px;border-top:1px solid var(--line)}}
.lnk{{font:inherit;font-size:12.5px;font-weight:600;border:0;background:none;padding:0;cursor:pointer;color:var(--acc)}}
.lnk:hover{{text-decoration:underline}}.lnk.danger{{color:var(--r);margin-left:auto}}
select,input[type=date]{{display:block;width:100%;margin-top:6px;font:inherit;font-size:14px;color:var(--ink);
padding:10px 13px;border-radius:12px;border:1px solid var(--line);background:var(--field)}}
.checks{{max-height:220px;overflow:auto;margin:6px 0 12px;padding:6px 4px;border-radius:12px;background:var(--field);border:1px solid var(--line)}}
.checks label{{display:flex;gap:10px;align-items:center;margin:0;padding:7px 10px;font-weight:500;color:var(--ink);font-size:13px}}
.note{{font-size:12px;color:var(--mut);background:var(--acc-soft);border-radius:12px;padding:10px 12px;margin:4px 0 0}}
.btn.red{{background:var(--r);box-shadow:none}}
:root{{--dlg:rgba(255,250,245,.88);--field:rgba(255,255,255,.7)}}
@media (prefers-color-scheme:dark){{:root{{--dlg:rgba(38,22,12,.92);--field:rgba(0,0,0,.25)}}}}
@media (max-width:600px){{.wrap{{padding:24px 16px 40px}}h1{{font-size:21px}}.kpi b{{font-size:28px}}}}
</style></head><body><div class="wrap">
<header><h1>Hodimlar samaradorligi</h1>
<div class="hright"><div class="mut meta glass">Hafta {a:%d.%m} – {b:%d.%m.%Y} · {WORK_START}–{WORK_END} · {now():%H:%M}</div>
<button class="btn" id="addEmp">+ Hodim qo'shish</button></div></header>
{flash_html}
<div class="kpis">
<div class="kpi glass"><span class="mut">Hodimlar</span><b>{total}</b></div>
<div class="kpi glass"><span class="mut">Bugun vaqtida</span><b>{on_today}</b></div>
<div class="kpi glass"><span class="mut">Bugun kech qolgan</span><b>{late_today}</b></div>
<div class="kpi glass"><span class="mut">Haftalik o'rtacha samaradorlik</span><b>{'—' if avg is None else str(avg) + '%'}</b></div>
</div>
<h2>Hodimlar</h2><div class="card glass"><table><tr>
<th>Hodim</th><th>Bugun</th><th>Hafta samaradorligi</th><th>Hafta: vaqtida / kech / kelmagan</th>
<th>Oy samaradorligi</th><th>Oy qoidabuzarlik</th><th>Oy jarima</th><th>Firmalar</th><th>Oy hisobotlari</th></tr>
{''.join(rows) or '<tr><td colspan=9 class="mut">Hodimlar yo‘q — “+ Hodim qo‘shish” tugmasini bosing</td></tr>'}
</table></div>
<h2>Hodimlar va korxonalar</h2>
<div class="team">{''.join(team) or '<div class="member glass mut">Hodimlar yo‘q</div>'}</div>
<h2>Oxirgi hisobotlar</h2><div class="card glass"><table><tr><th>Vaqt</th><th>Firma</th><th>Hisobot</th><th>Holat</th>
<th>Mas'ul (o‘sha sanada)</th><th>Yuborgan</th></tr>
{''.join(rep_rows) or '<tr><td colspan=6 class="mut">Hali hisobot kelmagan</td></tr>'}
</table></div>
<h2>Biriktirish tarixi</h2><div class="card glass"><table><tr><th>Korxona</th><th>Hodim</th><th>Qachondan</th><th>Qachongacha</th></tr>
{hist_rows or '<tr><td colspan=4 class="mut">Tarix bo‘sh</td></tr>'}
</table></div>
<p class="sub foot">Samaradorlik = vaqtida kelgan kunlar ÷ o‘tgan ish kunlari. Sahifa har daqiqada yangilanadi.</p>
</div>
<dialog id="dEmp"><form method="post" action="/dashboard/add_employee">
<h3>Yangi hodim</h3>
<label>Telegram ID<input type="text" name="tg_id" required pattern="[0-9]+" inputmode="numeric" placeholder="7758605480"></label>
<label>Ism Familiya<input type="text" name="name" required maxlength="80" placeholder="Shokh Karimov"></label>
<p class="sub">Hodim avval botga /start yozadi — bot unga Telegram ID'sini ko‘rsatadi.</p>
<input type="hidden" name="csrf" value="{CSRF}">
<div class="actions"><button type="button" class="btn ghost" data-close>Bekor</button><button class="btn">Qo‘shish</button></div>
</form></dialog>
<dialog id="dAsg"><form method="post" action="/dashboard/assign">
<h3>Korxona qo‘shish · <span class="who"></span></h3>
<label>Har qatorga bittadan: INN Firma nomi<textarea name="companies" required
placeholder="309497371 UNI-TAX CONSULT MCHJ&#10;305123456 BEKOBOD ASFALT BETON MCHJ"></textarea></label>
<input type="hidden" name="tg_id"><input type="hidden" name="csrf" value="{CSRF}">
<div class="actions"><button type="button" class="btn ghost" data-close>Bekor</button><button class="btn">Biriktirish</button></div>
</form></dialog>
<dialog id="dTr"><form method="post" action="/dashboard/transfer">
<h3>Korxonalarni ko‘chirish · <span class="who"></span></h3>
<div class="sub">Qaysi korxonalar:</div><div class="checks"></div>
<label>Kimga<select name="to" required><option value="">— tanlang —</option>{emp_opts}<option value="0">Hech kimga (biriktirilmagan)</option></select></label>
<label>Qaysi sanadan boshlab<input type="date" name="day" required max="{d.isoformat()}" value="{d.isoformat()}"></label>
<p class="note">Shu sanadan oldingi hisobotlar eski hodim nomida qoladi, keyingilari yangi hodimga yoziladi.</p>
<input type="hidden" name="from"><input type="hidden" name="csrf" value="{CSRF}">
<div class="actions"><button type="button" class="btn ghost" data-close>Bekor</button><button class="btn">Ko‘chirish</button></div>
</form></dialog>
<dialog id="dFire"><form method="post" action="/dashboard/fire">
<h3>Ishdan bo‘shatish · <span class="who"></span></h3>
<label>Korxonalari kimga o‘tsin<select name="to"><option value="0">Hech kimga (biriktirilmagan)</option>{emp_opts}</select></label>
<label>Qaysi sanadan<input type="date" name="day" required max="{d.isoformat()}" value="{d.isoformat()}"></label>
<p class="note">Hodimning ismi, hisobotlari va tarixi saqlanadi. Keyin qaytarish mumkin (/add).</p>
<input type="hidden" name="uid"><input type="hidden" name="csrf" value="{CSRF}">
<div class="actions"><button type="button" class="btn ghost" data-close>Bekor</button><button class="btn red">Bo‘shatish</button></div>
</form></dialog>
<script>
document.querySelectorAll('[data-tr]').forEach(b=>b.onclick=()=>{{const d=document.getElementById('dTr');
d.querySelector('[name=from]').value=b.dataset.id;d.querySelector('.who').textContent=b.dataset.name;
const box=d.querySelector('.checks');box.innerHTML='';const comps=JSON.parse(b.dataset.comps||'[]');
if(!comps.length)box.innerHTML='<div class="sub" style="padding:8px 10px">Korxona yo‘q</div>';
comps.forEach(([inn,nm])=>{{const l=document.createElement('label');const c=document.createElement('input');
c.type='checkbox';c.name='inn';c.value=inn;c.checked=true;l.append(c,document.createTextNode((nm||'(nomsiz)')+' · '+inn));box.append(l);}});
[...d.querySelectorAll('[name=to] option')].forEach(o=>o.disabled=(o.value===b.dataset.id));d.showModal();}});
document.querySelectorAll('[data-fire]').forEach(b=>b.onclick=()=>{{const d=document.getElementById('dFire');
d.querySelector('[name=uid]').value=b.dataset.id;d.querySelector('.who').textContent=b.dataset.name;
[...d.querySelectorAll('[name=to] option')].forEach(o=>o.disabled=(o.value===b.dataset.id));d.showModal();}});
document.querySelector('#dFire form').onsubmit=()=>confirm('Rostdan ham ishdan bo‘shatilsinmi?');
document.getElementById('addEmp').onclick=()=>document.getElementById('dEmp').showModal();
document.querySelectorAll('[data-asg]').forEach(b=>b.onclick=()=>{{const d=document.getElementById('dAsg');
d.querySelector('[name=tg_id]').value=b.dataset.id;d.querySelector('.who').textContent=b.dataset.name;d.showModal();
d.querySelector('textarea').focus();}});
document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>b.closest('dialog').close());
setInterval(()=>{{if(!document.querySelector('dialog[open]'))location.replace('/dashboard')}},60000);
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _out(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _auth(self):
        """Dashboard paroli. True — davom etamiz; False — javob allaqachon yuborilgan."""
        ip = self.headers.get("CF-Connecting-IP") or self.client_address[0]
        t = time.time()
        fails = [x for x in LOGIN_FAILS.get(ip, []) if x > t - FAIL_WINDOW]
        if len(fails) >= MAX_FAILS:
            self._out(429, "Juda ko'p noto'g'ri urinish. 15 daqiqadan keyin qayta urining.",
                      "text/plain; charset=utf-8")
            return False
        auth = self.headers.get("Authorization", "")
        ok = False
        if auth.startswith("Basic ") and DASH_PASSWORD:
            try:
                user, pw = base64.b64decode(auth[6:]).decode().split(":", 1)
                ok = secrets.compare_digest(pw, DASH_PASSWORD) and (
                    not DASH_USER or secrets.compare_digest(user, DASH_USER))
            except Exception:
                ok = False
            if not ok:
                fails.append(t)
        LOGIN_FAILS[ip] = fails
        if not ok:
            self._out(401, "Parol kerak", "text/plain; charset=utf-8",
                      {"WWW-Authenticate": 'Basic realm="dashboard"'})
        return ok

    def _redirect(self, flash):
        self.send_response(303)
        self.send_header("Location", "/dashboard?m=" + flash)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dashboard_post(self, raw):
        if not self._auth():
            return
        fl = urllib.parse.parse_qs(raw.decode("utf-8", "replace"))
        f = {k: v[0] for k, v in fl.items()}
        path = self.path.split("?")[0]
        log("DASHBOARD", path, {k: v for k, v in f.items() if k != "csrf"})
        if not secrets.compare_digest(f.get("csrf", ""), CSRF):
            return self._redirect("stale")
        if path == "/dashboard/add_employee":
            uid, name = f.get("tg_id", "").strip(), " ".join(f.get("name", "").split())[:80]
            if not uid.isdigit():
                return self._redirect("bad_id")
            if not name:
                return self._redirect("bad_name")
            delivered = add_employee(int(uid), name)
            send_kb(OWNER, f"✅ Dashboard orqali hodim qo'shildi: {esc(name)} (ID {uid})",
                    [[("➕ Korxona biriktirish", f"asg:{uid}")]])
            return self._redirect("emp_ok" if delivered else "emp_nosend")
        if path == "/dashboard/assign":
            uid = f.get("tg_id", "")
            if not uid.isdigit() or not emp(int(uid)):
                return self._redirect("no_emp")
            items, _bad = parse_companies(f.get("companies", ""))
            if not items:
                return self._redirect("bad_inn")
            assign_companies(int(uid), items)
            return self._redirect("asg_ok")
        if path == "/dashboard/unassign":
            inn = "".join(ch for ch in f.get("inn", "") if ch.isdigit())
            if inn:
                set_owner(inn, None, now().date().isoformat())
            return self._redirect("unasg_ok")
        if path in ("/dashboard/transfer", "/dashboard/fire"):
            frm = f.get("from") or f.get("uid") or ""
            to = f.get("to", "0") or "0"
            day = parse_day(f.get("day", ""))
            if not frm.isdigit() or not to.isdigit() or not emp(int(frm)):
                return self._redirect("no_emp")
            if day is None:
                return self._redirect("bad_day")
            if frm == to:
                return self._redirect("same")
            to_uid = int(to) or None
            if to_uid and not emp(to_uid):
                return self._redirect("no_emp")
            if path == "/dashboard/transfer":
                inns = [i for i in fl.get("inn", []) if i.isdigit()]
                if not inns:
                    return self._redirect("tr_none")
                moved = transfer_companies(int(frm), to_uid, day, inns)
                send(OWNER, f"⇄ Dashboard: {len(moved)} ta korxona {fmt_day(day)} dan "
                            f"{esc(emp_name(int(frm)))} → {esc(emp_name(to_uid) if to_uid else 'hech kimga')}")
                return self._redirect("tr_ok")
            name = emp_name(int(frm))
            moved = fire_employee(int(frm), to_uid, day)
            send(OWNER, f"🚪 Dashboard: {esc(name)} ishdan bo'shatildi ({fmt_day(day)}), "
                        f"{len(moved)} ta korxona → {esc(emp_name(to_uid) if to_uid else 'biriktirilmagan')}")
            return self._redirect("fire_ok")
        self._out(404, "not found", "text/plain")

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(min(n, 200_000))
        except Exception:
            return self._out(400, {"error": "bad request"})
        if self.path.startswith("/dashboard/"):
            return self._dashboard_post(raw)
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            return self._out(400, {"error": "bad json"})
        e = emp_by_key(body.get("key"))
        log("API", self.path, "hodim:", e["name"] if e else "KALIT NOTO'G'RI",
            "inn:", body.get("inn"), "hisobot:", (body.get("rec") or {}).get("№ Отчета"))
        if self.path == "/api/ping":
            return self._out(200, {"ok": True, "name": e["name"]}) if e else self._out(401, {"error": "bad key"})
        if self.path == "/api/report":
            if not e:
                return self._out(401, {"error": "bad key"})
            code, res = handle_report(e, body)
            return self._out(code, res)
        self._out(404, {"error": "not found"})

    def do_GET(self):
        if self.path.split("?")[0] == "/skript.user.js":
            # Hodimlar skripti — SERVER_URL joriy manzil bilan almashtiriladi (maxfiy narsa yo'q, kalit alohida).
            try:
                with open(os.path.join(BASE, "soliq-hisobot.user.js"), encoding="utf-8") as f:
                    js = f.read()
            except OSError:
                return self._out(404, "not found", "text/plain")
            url = current_url()
            js = re.sub(r"const SERVER_URL\s*=\s*'[^']*'", f"const SERVER_URL    = '{url}'", js, count=1)
            # Tampermonkey shu manzildan yangilanishlarni o'zi tekshirsin
            js = js.replace("// ==/UserScript==", f"// @updateURL    {url}/skript.user.js\n"
                                                   f"// @downloadURL  {url}/skript.user.js\n// ==/UserScript==", 1)
            return self._out(200, js, "text/javascript; charset=utf-8")
        if self.path.split("?")[0] not in ("/", "/dashboard"):
            return self._out(404, "not found", "text/plain")
        if not self._auth():
            return
        flash = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("m", [""])[0]
        self._out(200, render_dashboard(flash), "text/html; charset=utf-8")


def main():
    if not DASH_PASSWORD:
        print("⚠️  config.json: DASHBOARD_PASSWORD bo'sh — dashboard ochilmaydi.")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"Bot ishga tushdi. Dashboard: {PUBLIC_URL}/dashboard")
    send(OWNER, "🤖 Bot ishga tushdi. /help — buyruqlar")
    polling()


if __name__ == "__main__":
    main()
