#!/usr/bin/env python3
"""
Hodimlar davomati + soliq.uz hisobotlari uchun Telegram bot va dashboard.
Faqat Python 3.9+ standart kutubxonasi ishlatiladi (pip install shart emas).

Ishga tushirish:  python3 bot.py      (config.json shu papkada bo'lishi kerak)
"""
import base64
import html
import http.cookies
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

import screentime as st  # hodim kompyuterlari: screen time + boshqaruv (screentime.py)

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
WORK_START = CFG.get("WORK_START", "09:30")
WORK_END = CFG.get("WORK_END", "18:30")
WORK_DAYS = set(CFG.get("WORK_DAYS", [0, 1, 2, 3, 4]))          # 0=Dushanba ... 6=Yakshanba
HOLIDAYS = set(CFG.get("HOLIDAYS", []))                          # ["2026-12-08", ...]
PERIOD = CFG.get("VIOLATION_PERIOD", "month")                    # "month" yoki "all"
ABSENT_IS_VIOLATION = CFG.get("ABSENT_IS_VIOLATION", True)
FORWARD_VIDEOS = CFG.get("FORWARD_VIDEOS_TO_OWNER", False)
DB_PATH = CFG.get("DB_PATH", os.path.join(BASE, "data.db"))
# Tunnel manzili o'zgarsa agent va skript yangisini shu yerdan topadi (bot profilida link ko'rinmasin).
# Serverning doimiy IP manzilida faqat /u ni beradigan kichik HTTP; javob imzolangan — soxtasini qabul qilmaydi.
DISC_PORT = int(CFG.get("DISCOVERY_PORT", 0))
DISC_URL = CFG.get("DISCOVERY_URL", "")             # masalan "http://165.227.155.177:8099/u"
DISC_SECRET = __import__("hashlib").sha256(("disc:" + CFG["BOT_TOKEN"]).encode()).hexdigest()[:32]

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
# Hisobotlar jadvali (Excel o'rniga): korxona × soliq turi × davr (YYYY-MM).
# filings.source: 'soliq' (skriptdan), 'excel' (eski Excel'dan import), 'manual' (dashboardda qo'lda).
db.executescript("""
CREATE TABLE IF NOT EXISTS filings(
  inn TEXT, period TEXT, tax TEXT, status TEXT, source TEXT, report_id TEXT, detail TEXT, ts TEXT,
  PRIMARY KEY(inn, period, tax));
CREATE INDEX IF NOT EXISTS ix_fil_period ON filings(period);
-- Korxona qaysi soliqni topshiradi: freq 'm' har oy, 'q' chorakda (3,6,9,12-oy); required=0 — topshirmaydi.
CREATE TABLE IF NOT EXISTS obligations(
  inn TEXT, tax TEXT, required INTEGER, freq TEXT DEFAULT 'm', source TEXT DEFAULT 'manual', PRIMARY KEY(inn, tax));
""")
for _col, _typ in (("activity", "TEXT"), ("staff", "TEXT"), ("c1", "TEXT"), ("note", "TEXT"),
                   ("regime", "TEXT"), ("perk", "TEXT"), ("rating", "TEXT"), ("rating_ts", "TEXT"),
                   ("sort", "INTEGER"), ("active", "INTEGER DEFAULT 1")):
    if _col not in {r[1] for r in db.execute("PRAGMA table_info(companies)")}:
        db.execute(f"ALTER TABLE companies ADD COLUMN {_col} {_typ}")
db.commit()
# Korxonalar ro'yxati faqat qo'lda to'ldiriladi (dashboard "+ Mijoz" yoki botda /firma).
# active: 1 — bizning mijoz; 0 — karzinada (bildirishnoma kelmaydi, jadvalda yo'q; qaytarish yoki butunlay o'chirish mumkin).
# Bir martalik: hisobot kelgani uchun o'zi qo'shilib qolgan (hech kim qo'shmagan/biriktirmagan) korxonalar karzinaga.
if not db.execute("SELECT 1 FROM meta WHERE k='manual_list_v1'").fetchone():
    db.execute("UPDATE companies SET active=0 WHERE tg_id IS NULL AND sort IS NULL AND COALESCE(staff,'')='' "
               "AND inn NOT IN (SELECT inn FROM assignments) AND inn NOT IN (SELECT inn FROM obligations) "
               "AND inn NOT IN (SELECT inn FROM filings WHERE source!='soliq')")
    db.execute("INSERT INTO meta(k,v) VALUES('manual_list_v1', datetime('now'))")
    db.commit()
LISTED = "COALESCE(active,1)=1"   # SQL sharti: korxona bizning ro'yxatda


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
        comps = q(f"SELECT inn,name FROM companies WHERE tg_id=? AND {LISTED} ORDER BY name", (e["tg_id"],))
        reps = q("SELECT COUNT(DISTINCT report_id) c FROM reports WHERE tg_id=? AND ts>=? AND ts<? "
                 f"AND inn IN (SELECT inn FROM companies WHERE {LISTED})",
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
        if "(sinov)" not in status:     # sinov xabari (Tampermonkey menyusidan) har safar yuborilaversin
            return 200, {"ok": True, "duplicate": True}

    c = q("SELECT * FROM companies WHERE inn=?", (inn,), one=True)
    if not c or c["active"] == 0:
        # Bizning ro'yxatda yo'q (yoki karzinadagi) korxona — hodim kabinetidagi boshqa firma.
        # Hisobot saqlanadi (keyin ro'yxatga qo'shilsa jadvalga tushadi), lekin xabar yuborilmaydi.
        return 200, {"ok": True, "ignored": True}
    if company and not c["name"]:
        q("UPDATE companies SET name=? WHERE inn=?", (company, inn))
    try:
        record_filing(inn, rec, e["name"], now().isoformat())
    except Exception:
        log("FILING ERROR", traceback.format_exc())
    if body.get("silent"):   # skript eski hisobotlarni jadval uchun yubordi — Telegram'ga xabar shart emas
        return 200, {"ok": True, "silent": True}
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
    if c and c["rating"]:
        lines.append(f"⭐ <b>Reyting:</b> {esc(c['rating'])}")
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


# ---------------------------------------------------------------- hisobotlar jadvali (Excel o'rniga)
# (kod, jadvaldagi nom, Excel'dagi nom) — jadval ustunlari shu tartibda
TAXES = [("jshds", "JShDS / IJS", "Подоход И ЕСП"), ("inps", "INPS", "ИНПС"), ("nds", "QQS", "НДС"),
         ("div", "Dividend", "ДИВИДЕНД"), ("enp", "Aylanma (ENP)", "ЕНП"), ("prib", "Foyda", "ПРИБЫЛЬ"),
         ("imush", "Mol-mulk", "ИМУЩ."), ("zem", "Yer", "ЗЕМЛЯ"), ("voda", "Suv", "ВОДА"),
         ("akciz", "Aksiz", "АКЦИЗ"), ("balans", "Balans", "Баланс"), ("finres", "Moliyaviy natija", "Фин. Резуль"),
         ("stat", "Statistika", "СТАТИСТИКА")]
TAX_NAME = {k: n for k, n, _ in TAXES}
# soliq.uz hisobot nomi (kichik harf) -> kod. Tartib muhim: "дивиденд" "доходы" dan oldin tekshiriladi.
TAX_MATCH = [("div", ("дивиденд",)), ("jshds", ("доходов физических", "доходы физических", "социальн", "ндфл")),
             ("inps", ("накопительн",)), ("nds", ("добавленн",)),
             ("enp", ("с оборота", "единого налогового", "единый налог")), ("prib", ("на прибыль",)),
             ("imush", ("имуществ",)), ("zem", ("земельн", "за землю")), ("voda", ("водн",)), ("akciz", ("акциз",)),
             ("balans", ("баланс",)), ("finres", ("финансовых результат",)), ("stat", ("статист",))]
RU_MONTHS = [("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("май", 5), ("мая", 5), ("июн", 6),
             ("июл", 7), ("август", 8), ("сентябр", 9), ("октябр", 10), ("ноябр", 11), ("декабр", 12)]
UZ_MONTHS = ["", "Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun", "Iyul", "Avgust", "Sentyabr", "Oktyabr",
             "Noyabr", "Dekabr"]
DUE_DAY = int(CFG.get("REPORT_DUE_DAY", 25))   # davrdan keyingi oyning shu sanasigacha topshirilmasa — "qarz"
FIL_RANK = {"ok": 3, "late": 3, "wait": 2, "error": 1}


def tax_of(form):
    s = (form or "").lower()
    for code, words in TAX_MATCH:
        if any(w in s for w in words):
            return code
    return ("x:" + " ".join(str(form).split())[:80]) if form else None


def report_period(rec, ts):
    """soliq.uz 'Год' + 'Период' -> 'YYYY-MM' (chorak/yil — oxirgi oyi). Aniqlanmasa — yuborilgan oydan oldingi oy."""
    y = re.search(r"(20\d\d)", str(rec.get("Год") or ""))
    p = str(rec.get("Период") or "").lower()
    m = next((n for k, n in RU_MONTHS if k in p), None)
    if m is None:
        qm = re.search(r"\b(iv|iii|ii|i|[1-4])\s*-?\s*(?:й\s*)?квартал", p)
        if qm:
            m = 3 * ({"i": 1, "ii": 2, "iii": 3, "iv": 4}.get(qm.group(1)) or int(qm.group(1)))
        elif "полугод" in p:
            m = 6
        elif "9" in p and "мес" in p:
            m = 9
        elif "год" in p:
            m = 12
    if y and m:
        return f"{y.group(1)}-{m:02d}"
    d = datetime.fromisoformat(report_day(rec, ts)[:10]).date().replace(day=1) - timedelta(days=1)
    return f"{d:%Y-%m}"


def filing_status(status, error_reason=None):
    s = (status or "").lower()
    if error_reason or any(w in s for w in ("ошиб", "отклон", "отказ", "аннулир")):
        return "error"
    if "опоздан" in s:
        return "late"
    return "ok" if status_level(status)[0] == "good" else "wait"


def record_filing(inn, rec, sender, ts):
    """soliq.uz hisobotini jadval katagiga yozadi. Bir katakka bir nechta hisobot bo'lsa (tuzatish),
    eng yangisi qoladi — faqat yangisi xato bo'lib, avvalgisi qabul qilingan bo'lsa, qabul qilingani qoladi."""
    rid = str(rec.get("№ Отчета", "")).strip()
    tax = tax_of(rec.get("Наименование"))
    if not inn or not tax or not rid.isdigit():
        return
    per = report_period(rec, ts)
    st = filing_status(rec.get("Состояние"), rec.get("Причина ошибки"))
    detail = json.dumps({"by": sender, "rec": rec}, ensure_ascii=False)   # soliq.uz dan kelgan hamma maydon
    with LOCK:
        old = db.execute("SELECT * FROM filings WHERE inn=? AND period=? AND tax=?", (inn, per, tax)).fetchone()
        if old and old["source"] == "soliq" and old["report_id"] != rid:
            newer = int(rid) > int(old["report_id"] or 0)
            if (newer and st == "error" and old["status"] in ("ok", "late")) or \
               (not newer and FIL_RANK.get(st, 0) <= FIL_RANK.get(old["status"], 0)):
                return
        db.execute("INSERT OR REPLACE INTO filings(inn,period,tax,status,source,report_id,detail,ts) "
                   "VALUES(?,?,?,?,'soliq',?,?,?)", (inn, per, tax, st, rid, detail, ts))
        db.commit()


def backfill_filings(inn=None):
    """Jadval paydo bo'lishidan oldin kelgan hisobotlarni ham jadvalga yozadi (takror ishlatsa zarari yo'q).
    inn berilsa — faqat shu korxona (qo'lda belgi olib tashlanganda avtomatik holatni tiklash uchun)."""
    for r in q("SELECT r.*, e.name ename FROM reports r LEFT JOIN employees e ON e.tg_id=r.tg_id "
               + ("WHERE r.inn=? " if inn else "") + "ORDER BY r.ts", (inn,) if inn else ()):
        try:
            record_filing(r["inn"], json.loads(r["data"] or "{}"), r["ename"] or "", r["ts"])
        except Exception:
            log("BACKFILL ERROR", r["report_id"], traceback.format_exc())


def period_label(per):
    return f"{UZ_MONTHS[int(per[5:7])]} {per[:4]}"


def period_shift(per, n):
    y, m = int(per[:4]), int(per[5:7]) - 1 + n
    return f"{y + m // 12}-{m % 12 + 1:02d}"


def default_period():
    """Hozir topshirilayotgan davr — o'tgan oy (noyabrda oktyabr hisobotlari topshiriladi)."""
    return period_shift(f"{now():%Y-%m}", -1)


def period_due(per):
    return datetime.fromisoformat(period_shift(per, 1) + f"-{DUE_DAY:02d}").date()


_CYR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ",
                ["a", "b", "v", "g", "d", "e", "yo", "j", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                 "u", "f", "x", "ts", "ch", "sh", "sh", "", "i", "", "e", "yu", "ya", "o", "q", "g", "h"]))


def name_key(s):
    """'Нилуфар' va 'Nilufar Karimova' -> 'nilufar' (birinchi so'z, lotincha) — Excel ismini bot hodimiga moslash."""
    w = (str(s or "").split() or [""])[0].lower()
    return re.sub(r"[^a-z]", "", "".join(_CYR.get(ch, ch) for ch in w).replace("'", ""))


def make_cell(f, o, month, hist, overdue):
    """filings qatori (f) va majburiyat (o) -> jadval katagi."""
    expected = not hist and bool(o and o["required"] and (o["freq"] != "q" or month % 3 == 0))
    if f:
        return {"st": f["status"], "src": f["source"], "rid": f["report_id"], "ts": f["ts"],
                "detail": f["detail"], "expected": expected}
    if expected:
        return {"st": "missing" if overdue else "pending", "expected": True}
    return {"st": None, "expected": False, "never": bool(o and not o["required"])}


def is_hist(per):
    xl_last = (q("SELECT v FROM meta WHERE k='excel_last_period'", one=True) or {"v": ""})["v"]
    return bool(xl_last) and per <= xl_last


def one_cell(inn, per, tax):
    return make_cell(q("SELECT * FROM filings WHERE inn=? AND period=? AND tax=?", (inn, per, tax), one=True),
                     q("SELECT * FROM obligations WHERE inn=? AND tax=?", (inn, tax), one=True),
                     int(per[5:7]), is_hist(per), now().date() > period_due(per))


def period_matrix(per):
    """Davr jadvali: {"rows": [...], "taxes": [kod...], "other": bool}. Har katak:
    {"st": ok|late|wait|error|na|note|missing|None, "src", "detail", "expected"}."""
    last_day = (datetime.fromisoformat(period_shift(per, 1) + "-01").date() - timedelta(days=1)).isoformat()
    month = int(per[5:7])
    fil = {}
    for f in q("SELECT * FROM filings WHERE period=?", (per,)):
        fil.setdefault(f["inn"], {})[f["tax"]] = f
    obl = {}
    for o in q("SELECT * FROM obligations"):
        obl.setdefault(o["inn"], {})[o["tax"]] = o
    # Excel davrlari (import qilingan oxirgi oygacha) — tarix: faqat o'sha oyda bor bo'lgan ma'lumot ko'rsatiladi.
    hist = is_hist(per)
    # Karzinadagi korxonalar hech qachon ko'rinmaydi; ro'yxatdagilar — joriy davrlarda hammasi, tarixda — ma'lumoti borlari.
    comps = q(f"SELECT * FROM companies WHERE {LISTED} AND (?=0 OR inn IN (SELECT inn FROM filings WHERE period=?)) "
              "ORDER BY sort IS NULL, sort, name", (int(hist), per))
    allinn = {r["inn"] for r in q("SELECT inn FROM companies")}
    extra = [{"inn": i, "name": "", "activity": "", "staff": "", "c1": "", "note": "", "active": 0, "tg_id": None}
             for i in fil if hist and i not in allinn]
    overdue = now().date() > period_due(per)
    by_key = {}
    for e_ in active_employees():
        by_key.setdefault(name_key(e_["name"]), []).append(e_["name"])
    used, other, rows = set(), False, []
    for c in list(comps) + extra:
        c = dict(c)
        inn = c["inn"]
        resp = responsible(inn, last_day)
        cells = {}
        for code, _n, _x in TAXES:
            f = fil.get(inn, {}).get(code)
            cells[code] = make_cell(f, obl.get(inn, {}).get(code), month, hist, overdue)
            if f or cells[code]["expected"]:
                used.add(code)
        others = [f for t, f in fil.get(inn, {}).items() if t.startswith("x:")]
        other = other or bool(others)
        same = by_key.get(name_key(c.get("staff"))) if c.get("staff") else None
        staff = emp_name(resp) if resp else (same[0] if same and len(same) == 1 else (c.get("staff") or ""))
        rows.append({"c": c, "resp": resp, "staff": staff, "staff_is_bot": resp is not None,
                     "cells": cells, "others": others})
    return {"rows": rows, "taxes": [k for k, _n, _x in TAXES if k in used], "other": other, "overdue": overdue}


def matrix_counts(m):
    """Jami va hodim bo'yicha: kerak / topshirilgan / qarz / xato."""
    tot = {"need": 0, "done": 0, "missing": 0, "bad": 0}
    by = {}
    for r in m["rows"]:
        s = by.setdefault(r["staff"] or "—", {"need": 0, "done": 0, "missing": 0, "bad": 0})
        for cell in r["cells"].values():
            st = cell["st"]
            if st not in ("ok", "late", "wait", "error", "missing", "pending"):
                continue
            for d in (tot, s):
                d["need"] += 1
                d["done"] += st in ("ok", "late")
                d["missing"] += st in ("missing", "pending")
                d["bad"] += st in ("error", "late")
    return tot, by


def debt_text(per=None):
    per = per or default_period()
    m = period_matrix(per)
    tot, _by = matrix_counts(m)
    lines = [f"📋 <b>{period_label(per)} hisobotlari</b>",
             f"Kerak: {tot['need']} · ✅ topshirildi: {tot['done']} · ⏳ qolgan: {tot['missing']} · ⚠️ xato/kech: {tot['bad']}",
             f"Muddat (taxminan): {period_due(per):%d.%m.%Y}", ""]
    groups = {}
    for r in m["rows"]:
        miss = [TAX_NAME[k] for k, cell in r["cells"].items() if cell["st"] in ("missing", "pending", "error")]
        if miss:
            groups.setdefault(r["staff"] or "biriktirilmagan", []).append(f"   • {esc(r['c']['name'] or r['c']['inn'])}: "
                                                                      f"{esc(', '.join(miss))}")
    for who in sorted(groups):
        lines.append(f"👷 <b>{esc(who)}</b> — {len(groups[who])} ta korxona")
        lines += groups[who]
    if not groups:
        lines.append("🎉 Hamma hisobotlar topshirilgan.")
    lines.append(f"\n📊 Jadval: {current_url()}/hisobotlar?p={period_shift(per, 1)}")
    return "\n".join(lines)


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


def join_request(uid, frm):
    """Ro'yxatda yo'q odam botga yozdi: rahbarga "Qo'shish" tugmasi bilan so'rov (12 soatda bir marta)."""
    name = " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x).strip() or str(uid)
    user = frm.get("username") or ""
    old = q("SELECT v FROM meta WHERE k=?", (f"join:{uid}",), one=True)
    fresh = False
    if old:
        try:
            fresh = (now() - datetime.fromisoformat(json.loads(old["v"])["ts"])).total_seconds() < 12 * 3600
        except (ValueError, KeyError):
            pass
    send(uid, "👋 Assalomu alaykum!\nSo'rovingiz rahbarga yuborildi. Tasdiqlangach shu yerga "
              "soliq.uz skripti uchun <b>kalit</b> va yo'riqnoma keladi.\n"
              f"Sizning Telegram ID: <code>{uid}</code>")
    if fresh:
        return
    q("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)",
      (f"join:{uid}", json.dumps({"name": name, "user": user, "ts": now().isoformat()})))
    send_kb(OWNER, f"🆕 Botga yangi odam yozdi: <b>{esc(name)}</b>"
                   + (f" (@{esc(user)})" if user else "") + f"\nID: <code>{uid}</code>\nHodim sifatida qo'shilsinmi?",
            [[("➕ Qo'shish", f"join:{uid}"), ("❌ Yo'q", f"joinno:{uid}")]])


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
        activate_company(inn)
    return moved


def activate_company(inn):
    """Korxonani ro'yxatga qo'shadi (karzinadan qaytaradi) va avval kelgan hisobotlarini jadvalga yozadi."""
    q("UPDATE companies SET active=1, sort=COALESCE(sort,(SELECT COALESCE(MAX(sort),0)+1 FROM companies)) "
      "WHERE inn=?", (inn,))
    backfill_filings(inn)
    apply_saved_rating(inn)


def apply_saved_rating(inn):
    """Korxona ro'yxatga qo'shilishidan oldin kelgan reytingni jadvalga yozadi."""
    r = q("SELECT v FROM meta WHERE k=?", (f"rating:{inn}",), one=True)
    if r:
        q("UPDATE companies SET rating=COALESCE(NULLIF(rating,''),?), rating_ts=COALESCE(rating_ts,?) WHERE inn=?",
          (r["v"], now().isoformat(), inn))


def purge_company(inn):
    """Karzinadagi korxonani butunlay o'chiradi (jadval, majburiyatlar, biriktirish tarixi, hisobotlar)."""
    with LOCK:
        if not db.execute("SELECT 1 FROM companies WHERE inn=? AND active=0", (inn,)).fetchone():
            return False
        for t in ("filings", "obligations", "assignments", "reports", "companies"):
            db.execute(f"DELETE FROM {t} WHERE inn=?", (inn,))
        db.commit()
    return True


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
    if login_callback(cq, data) or st.handle_callback(data):
        return
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
    elif p[0] in ("join", "joinno") and len(p) == 2 and p[1].isdigit():
        uid = int(p[1])
        row = q("SELECT v FROM meta WHERE k=?", (f"join:{uid}",), one=True)
        q("DELETE FROM meta WHERE k=?", (f"join:{uid}",))
        if emp(uid):
            return send(OWNER, f"{esc(emp_name(uid))} allaqachon ro'yxatda.")
        if not row:
            return send(OWNER, "Bu so'rov eskirgan yoki allaqachon ko'rib chiqilgan.")
        name = (json.loads(row["v"]).get("name") or str(uid))[:60]
        if p[0] == "joinno":
            send(uid, "Rahbar so'rovingizni tasdiqlamadi.")
            return send(OWNER, f"❌ {esc(name)} qo'shilmadi.")
        delivered = add_employee(uid, name)
        send_kb(OWNER, f"✅ <b>{esc(name)}</b> hodim sifatida qo'shildi (ID {uid}).\n"
                       + ("Unga skript kaliti va yo'riqnoma yuborildi." if delivered else "⚠️ Unga xabar yetmadi.")
                       + f"\nIsmini o'zgartirish: <code>/add {uid} Ism Familiya</code>",
                [[("➕ Korxona biriktirish", f"asg:{uid}")]])
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

<b>Dashboard xavfsizligi</b>
/chiqar — dashboarddagi hamma sessiyalarni yopish
/qurilmalar — ishonchli qurilmalar (ulardan kirganda xabar kelmaydi)
/ipblok — bloklangan IP lar (blokdan chiqarish)
(sana: 01.11.2026; yozilmasa — bugundan)

<b>Hisobotlar</b>
/today — bugungi davomat
/week — shu hafta
/month — shu oy
/forgive TG_ID — oxirgi qoidabuzarlikni bekor qilish
/qarz [10.2026] — qaysi korxonaning qaysi hisoboti hali topshirilmagan (standart: o'tgan oy)
/dashboard — dashboard havolasi
{st.HELP}

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

    if st.handle_command(cmd, args):
        return
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
    elif cmd == "/chiqar":
        n = len(SESSIONS)
        SESSIONS.clear()
        send(OWNER, f"🚪 Dashboarddagi hamma sessiyalar yopildi ({n} ta).")
    elif cmd == "/qurilmalar":
        t = trusted_devices()
        if not t:
            send(OWNER, "Ishonchli qurilma yo'q. Dashboardga kirganda kelgan xabarda \"✅ Bu men\" ni bosing.")
        else:
            send_kb(OWNER, "📱 <b>Ishonchli qurilmalar</b> (ulardan kirganda xabar kelmaydi)\n"
                    + "\n".join(f"• {esc(v.get('label', ''))} — {v.get('since', '')} dan, oxirgi: {v.get('last', '')}"
                                for v in t.values())
                    + "\n\nBosing — ro'yxatdan olib tashlanadi:",
                    [[(f"🗑 {v.get('label', '')} ({v.get('since', '')})"[:60], f"ldel:{k[:16]}")] for k, v in t.items()])
    elif cmd == "/ipblok":
        ips = sorted(blocked_ips())
        if not ips:
            send(OWNER, "Bloklangan IP yo'q.")
        else:
            send_kb(OWNER, "🚫 <b>Bloklangan IP manzillar</b>\nBosing — blokdan chiqariladi.",
                    [[(f"↩ {ip}", f"lunb:{ip}")] for ip in ips[:30]])
    elif cmd == "/list":
        lines = ["👥 <b>Hodimlar</b>"]
        for e in active_employees():
            comps = q(f"SELECT inn,name FROM companies WHERE tg_id=? AND {LISTED} ORDER BY name", (e["tg_id"],))
            lines.append(f"\n👷 <b>{esc(e['name'])}</b> — ID <code>{e['tg_id']}</code>, "
                         f"maosh {money(e['salary']) if e['salary'] else '—'}, firmalar: {len(comps)}")
            for c in comps:
                lines.append(f"   • {esc(c['name'] or '(nomi hali yo‘q)')} — {c['inn']}")
        free = q(f"SELECT inn,name FROM companies WHERE tg_id IS NULL AND {LISTED}")
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
    elif cmd in ("/qarz", "/jadval"):
        per = None
        if args:
            m = re.match(r"^(\d{1,2})[./-](\d{4})$", args[0]) or re.match(r"^(\d{4})-(\d{1,2})$", args[0])
            if not m:
                return send(OWNER, "Foydalanish: /qarz 10.2026")
            a1, a2 = m.groups()
            mm, yy = (int(a1), a2) if len(a2) == 4 else (int(a2), a1)
            if not 1 <= mm <= 12:
                return send(OWNER, "Oy 1–12 bo'lishi kerak. Masalan: /qarz 10.2026")
            per = f"{yy}-{mm:02d}"
        send(OWNER, debt_text(per))
    elif cmd == "/dashboard":
        url = current_url()
        send(OWNER, f"📈 Dashboard: {url}/dashboard\n📋 Hisobotlar jadvali: {url}/hisobotlar\n"
                    f"(login va parol bilan kiriladi, 1 soat harakatsiz qolsa o'zi chiqaradi)"
                    f"\n🧩 Hodimlar skripti: {url}/skript.user.js")
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
    if uid == OWNER and text and "pcmsg" in PENDING:
        return st.finish_msg(PENDING.pop("pcmsg"), text)
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
            join_request(uid, msg.get("from", {}))
        return
    if "video_note" in msg:
        return handle_video(msg, e)
    if "video" in msg:
        return send(uid, "❌ Oddiy video emas — <b>yumaloq video</b> (video xabar) yuboring.")
    if text.startswith("/key"):
        return send(uid, script_help(e["api_key"]))
    if text.startswith("/agent"):
        # Kompyuterga agent o'rnatish yo'riqnomasi (server manzili bor) — 10 daqiqadan keyin chatdan o'chiriladi.
        r = tg("sendMessage", chat_id=uid, text=st.agent_text(temporary=True), parse_mode="HTML",
               disable_web_page_preview=True)
        ids = [i for i in (msg.get("message_id"), (r or {}).get("message_id")) if i]
        threading.Timer(600, lambda: [tg("deleteMessage", chat_id=uid, message_id=i) for i in ids]).start()
        return send(OWNER, f"🧩 {esc(e['name'])} /agent yozdi — o'rnatish yo'riqnomasi unga yuborildi "
                           f"(10 daqiqadan keyin chatdan o'chadi).")
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
            st.tick(n)
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


# ---- login sessiyalari: har kirish 1 soat amal qiladi, keyin qayta login so'raladi
SESSION_COOKIE = "hb_session"
SESSION_TTL = int(CFG.get("SESSION_MINUTES", 60)) * 60
SESSIONS = {}   # token -> tugash vaqti (unix). Bot qayta yonsa hamma qayta kiradi.
SECURITY_HEADERS = {"X-Frame-Options": "DENY", "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}


def session_new():
    t = secrets.token_urlsafe(32)
    with LOCK:
        now_ts = time.time()
        for k in [k for k, v in SESSIONS.items() if v < now_ts]:
            SESSIONS.pop(k, None)
        SESSIONS[t] = now_ts + SESSION_TTL
    return t


def session_valid(token, touch=False):
    """Sessiya tirikmi. touch=True — foydalanuvchi faol: muddat yana SESSION_TTL ga uzayadi
    (ya'ni 1 soat HARAKATSIZ qolsa chiqariladi)."""
    exp = SESSIONS.get(token or "")
    if exp and exp > time.time():
        if touch:
            exp = SESSIONS[token] = time.time() + SESSION_TTL
        return exp
    SESSIONS.pop(token or "", None)
    return None


def session_end(token):
    SESSIONS.pop(token or "", None)


# Telegram'dagi kirish ogohlantirishidan rahbar begona kirishni chiqarib yuborishi va IP ni bloklashi mumkin
LOGINS = {}     # qisqa id -> {"token", "ip", "dev", "label"} (oxirgi kirishlar)
DEVICE_COOKIE = "hb_dev"


def trusted_devices():
    """Rahbar "✅ Bu men" degan qurilmalar: {qurilma_id: {"label", "since", "last"}}."""
    r = q("SELECT v FROM meta WHERE k='trusted_devices'", one=True)
    try:
        return json.loads(r["v"]) if r else {}
    except ValueError:
        return {}


def save_trusted(d):
    q("INSERT OR REPLACE INTO meta(k,v) VALUES('trusted_devices',?)", (json.dumps(d, ensure_ascii=False),))


def blocked_ips():
    r = q("SELECT v FROM meta WHERE k='blocked_ips'", one=True)
    try:
        return set(json.loads(r["v"])) if r else set()
    except ValueError:
        return set()


def set_blocked_ips(ips):
    q("INSERT OR REPLACE INTO meta(k,v) VALUES('blocked_ips',?)", (json.dumps(sorted(ips)),))


def login_callback(cq, data):
    """lkick:ID — shu kirishni chiqarish + IP blok; lall — hamma sessiyalarni yopish; lok:ID — bu men;
    lunb:IP — IP ni blokdan chiqarish. Tegishli bo'lsa True."""
    p = data.split(":", 1)
    if p[0] == "ldel" and len(p) == 2:          # ishonchli qurilmani ro'yxatdan olib tashlash
        t = trusted_devices()
        key = next((k for k in t if k.startswith(p[1])), None)
        if key:
            lab = t.pop(key).get("label", "")
            save_trusted(t)
            send(OWNER, f"🗑 {esc(lab)} ishonchli ro'yxatdan olib tashlandi — keyingi kirishda yana xabar keladi.")
        return True
    if p[0] not in ("lkick", "lall", "lok", "lunb"):
        return False
    mid = (cq.get("message") or {}).get("message_id")
    if mid and p[0] != "lunb":
        tg("editMessageReplyMarkup", chat_id=OWNER, message_id=mid, reply_markup={"inline_keyboard": []})
    if p[0] == "lok":
        info = LOGINS.pop(p[1], None) if len(p) == 2 else None
        if info and info.get("dev"):
            t = trusted_devices()
            t[info["dev"]] = {"label": info.get("label", ""), "since": now().strftime("%d.%m.%Y"),
                              "last": now().strftime("%d.%m.%Y %H:%M")}
            save_trusted(t)
            send(OWNER, f"👍 {esc(info.get('label', ''))} — ishonchli qurilma. Undan kirganda endi xabar kelmaydi.\n"
                        f"Ro'yxat: /qurilmalar")
        else:
            send(OWNER, "👍 Yaxshi, bu kirish sizniki deb belgilandi.")
    elif p[0] == "lall":
        n = len(SESSIONS)
        SESSIONS.clear()
        send(OWNER, f"🚪 Dashboarddagi hamma sessiyalar yopildi ({n} ta). Endi qaytadan parol bilan kirish kerak.")
    elif p[0] == "lkick" and len(p) == 2:
        info = LOGINS.pop(p[1], None)
        if not info:
            return send(OWNER, "Bu kirish topilmadi (bot qayta ishga tushgan bo'lishi mumkin). "
                               "Hamma sessiyalarni yopish: /chiqar") or True
        session_end(info["token"])
        ips = blocked_ips() | {info["ip"]}
        set_blocked_ips(ips)
        send_kb(OWNER, f"🚫 Chiqarib yuborildi va IP <code>{esc(info['ip'])}</code> bloklandi — bu manzildan dashboardga "
                       f"kirib bo'lmaydi.\n\n⚠️ Parolni ham almashtiring: begona odam uni biladi.",
                [[("↩ IP ni blokdan chiqarish", f"lunb:{info['ip']}")]])
    elif p[0] == "lunb" and len(p) == 2:
        set_blocked_ips(blocked_ips() - {p[1]})
        send(OWNER, f"↩ IP <code>{esc(p[1])}</code> blokdan chiqarildi.")
    return True


LOGIN_MSG = {"bad": "Login yoki parol noto‘g‘ri", "expired": "1 soat harakatsiz qolgani uchun tizimdan chiqarildingiz",
             "out": "Tizimdan chiqdingiz", "blocked": "Juda ko‘p xato urinish. 15 daqiqadan keyin urining",
             "denied": "Bu manzildan kirish rahbar tomonidan bloklangan"}


# ---------------------------------------------------------------- brend (Gigant Buxgalter brendbuki)
# Ranglar logotipdan: ko'mir #2C2B29, to'q sariq #F28322, gradient #ED6A23 → #F39614. Shrift: Roboto.
BRAND_HEAD = ('<link rel="preconnect" href="https://fonts.googleapis.com">'
              '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
              '<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap">'
              '<link rel="icon" href="/brand/logo.png">')
BRAND_CSS = """
:root{--ink:#2C2B29;--mut:#857a70;--acc:#F28322;--acc-deep:#ED6A23;--acc-soft:rgba(242,131,34,.12);
--grad:linear-gradient(135deg,#F39614 0%,#ED6A23 100%);
--glass:rgba(255,255,255,.62);--edge:rgba(255,255,255,.78);--line:rgba(44,43,41,.08);--shadow:rgba(120,60,20,.10);
--field:rgba(255,255,255,.8);--dlg:rgba(255,251,247,.96);--solid:#fffaf6;
--bg1:#fbf4ee;--bg2:#f6e9df;--b1:rgba(243,150,20,.38);--b2:rgba(237,106,35,.30);--b3:rgba(150,110,200,.16)}
@media (prefers-color-scheme:dark){:root{--ink:#f4eee8;--mut:#a89d93;--acc:#F59A3C;--acc-soft:rgba(242,131,34,.16);
--glass:rgba(28,26,30,.62);--edge:rgba(255,255,255,.08);--line:rgba(255,255,255,.07);--shadow:rgba(0,0,0,.45);
--field:rgba(0,0,0,.25);--dlg:rgba(24,22,26,.97);--solid:#17161a;
--bg1:#0A0B12;--bg2:#141218;--b1:rgba(237,106,35,.28);--b2:rgba(243,150,20,.16);--b3:rgba(120,90,190,.14)}}
body{font-family:Roboto,"Segoe UI",system-ui,sans-serif!important;font-size:14px;letter-spacing:0;
background:radial-gradient(1100px 620px at 0% -10%,var(--b1),transparent 60%),
radial-gradient(900px 560px at 100% 0%,var(--b2),transparent 58%),
radial-gradient(900px 600px at 70% 110%,var(--b3),transparent 60%),
linear-gradient(170deg,var(--bg1),var(--bg2))!important;background-attachment:fixed!important;
font-variant-numeric:tabular-nums;-webkit-font-smoothing:antialiased}
button,input,select,textarea{font-family:inherit}
h1:before{display:none!important}
h1{font-weight:600!important;letter-spacing:-.025em!important}
h2{color:var(--mut)!important;font-weight:600!important;letter-spacing:.06em!important}
.glass{background:var(--glass)!important;border-color:var(--edge)!important;
box-shadow:0 1px 0 var(--edge) inset,0 18px 50px -24px var(--shadow)!important}
.btn{background:var(--grad)!important;box-shadow:0 8px 20px -10px var(--acc-deep)!important;font-weight:600}
.btn.ghost{background:var(--glass)!important;color:var(--ink)!important;border:1px solid var(--edge)!important;
box-shadow:0 4px 14px -10px var(--shadow)!important;backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px)}
.btn.ghost:hover{background:#fff!important}
@media (prefers-color-scheme:dark){.btn.ghost:hover{background:rgba(255,255,255,.1)!important}}
.kpi b{color:var(--ink)!important;font-weight:600!important}
.kpi:first-child b{background:var(--grad);-webkit-background-clip:text;background-clip:text;color:transparent!important}
.topbar{display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:0 0 26px}
.brand{display:flex;align-items:center;text-decoration:none}
.brand img{height:40px;width:auto;display:block}.brand .logo-d{display:none}
@media (prefers-color-scheme:dark){.brand .logo-l{display:none}.brand .logo-d{display:block}}
.navpill{display:flex;gap:4px;padding:5px;border-radius:999px}
.navpill a{padding:8px 18px;border-radius:999px;text-decoration:none;color:var(--mut);font-weight:500;font-size:13.5px;transition:color .15s,background .15s}
.navpill a:hover{color:var(--ink)}
.navpill a.on{background:#fff;color:var(--ink);font-weight:600;box-shadow:0 4px 14px -6px rgba(44,43,41,.25)}
@media (prefers-color-scheme:dark){.navpill a.on{background:rgba(255,255,255,.14)}}
.navout{color:var(--mut);text-decoration:none;font-weight:500;font-size:13px;padding:8px 14px;border-radius:999px}
.navout:hover{color:var(--ink);background:var(--glass)}
@media (max-width:700px){.topbar{gap:10px}.brand img{height:32px}.navpill a{padding:7px 12px;font-size:12.5px}}
/* --- tipografiya: o'qiladigan o'lchamlar, mayda KATTA HARFLI yozuvlarsiz --- */
body{font-size:15px!important;line-height:1.5}
.sub,.mut{font-size:13px}
h1{font-size:30px!important}
h2{font-size:18px!important;text-transform:none!important;letter-spacing:-.01em!important;color:var(--ink)!important;margin-top:40px!important}
th{font-size:12.5px!important;text-transform:none!important;letter-spacing:0!important;font-weight:500!important}
.kpi span{font-size:14px!important}.kpi b{font-size:38px!important;letter-spacing:-.035em!important}
.pill{font-size:12.5px!important;padding:4px 11px!important;display:inline-flex!important;align-items:center;gap:6px}
.btn{font-size:14px!important;padding:10px 18px!important}
.btn.sm{font-size:13px!important;padding:8px 14px!important}
.btn.big{font-size:17px!important;padding:17px 20px!important;border-radius:18px!important}
.state{display:flex;align-items:center;gap:10px;font-size:16px!important}
/* --- ikonkalar (SF Symbols uslubi) --- */
.ic{width:1.15em;height:1.15em;flex:none;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round;
vertical-align:-.2em;margin-right:.4em}
.btn,.btn.sm,a.btn{display:inline-flex!important;align-items:center;justify-content:center;gap:0}
.flash{display:flex;align-items:center;gap:4px}.flash .ic{color:var(--acc);width:20px;height:20px}
.sd{width:8px;height:8px;border-radius:50%;background:currentColor;display:inline-block;flex:none}
/* --- minimalistik tugmalar: tekis rang, ingichka chegara, soyasiz; bosilganda yengil siqiladi --- */
.btn{position:relative;transition:transform .2s ease,background-color .2s,color .2s,border-color .2s!important;
font-weight:500!important;border-radius:12px!important;box-shadow:none!important;background:var(--acc)!important}
.btn:hover{transform:none!important;filter:brightness(.96)}
.btn:active{transform:scale(.97)!important}
.btn.ghost{background:transparent!important;color:var(--ink)!important;border:1px solid var(--line)!important;
box-shadow:none!important;backdrop-filter:none!important;-webkit-backdrop-filter:none!important}
.btn.ghost:hover{background:var(--glass)!important;border-color:var(--mut)!important;filter:none}
.btn.red,.btn.stop{background:#d4321c!important}.btn.ok{background:#16a34a!important}
.ibtn{display:inline-grid;place-items:center;width:34px;height:34px;border-radius:10px;border:1px solid var(--line);
background:var(--glass);color:var(--ink);cursor:pointer;text-decoration:none;transition:background .2s,border-color .2s}
.ibtn:hover{border-color:var(--mut);background:var(--solid)}.ibtn .ic{margin:0;width:17px;height:17px}
.morph{animation:morph .55s cubic-bezier(.2,.9,.3,1.2)}
@keyframes morph{0%{transform:scale(1);filter:blur(0)}35%{transform:scale(.94,.9);filter:blur(3px)}
70%{transform:scale(1.02,1.03);filter:blur(0)}100%{transform:scale(1)}}
dialog[open]{animation:pop .42s cubic-bezier(.2,.9,.3,1.15)}
dialog[open]::backdrop{animation:fade .3s ease}
@keyframes pop{from{opacity:0;transform:translateY(14px) scale(.94)}to{opacity:1;transform:none}}
@keyframes fade{from{opacity:0}to{opacity:1}}
/* bo'limlar orasida o'tganda menyudagi oq "tabletka" siljib o'tadi (Chrome) */
@view-transition{navigation:auto}
.navpill a.on{view-transition-name:navon}
.navpill a{transition:color .25s,background-color .35s}
@media (prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}
"""


# iPhone (SF Symbols) uslubidagi chiziqli ikonkalar — emoji o'rniga (har qurilmada bir xil ko'rinadi)
_IC = {
    "camera": '<path d="M3 8.5A2.5 2.5 0 0 1 5.5 6H7l1.6-2.2h6.8L17 6h1.5A2.5 2.5 0 0 1 21 8.5v9a2.5 2.5 0 0 1-2.5 2.5h-13A2.5 2.5 0 0 1 3 17.5z"/><circle cx="12" cy="12.8" r="3.6"/>',
    "message": '<path d="M20.5 11.6c0 4.2-3.8 7.4-8.5 7.4-1.1 0-2.2-.2-3.2-.5L4 20l1.3-3.7c-1.1-1.3-1.8-2.9-1.8-4.7C3.5 7.4 7.3 4.2 12 4.2s8.5 3.2 8.5 7.4z"/>',
    "lock": '<rect x="5" y="10.5" width="14" height="10" rx="2.6"/><path d="M8 10.5V7.8a4 4 0 0 1 8 0v2.7"/>',
    "unlock": '<rect x="5" y="10.5" width="14" height="10" rx="2.6"/><path d="M8 10.5V7.8a4 4 0 0 1 7.6-1.8"/>',
    "key": '<circle cx="8" cy="15.5" r="4"/><path d="M10.9 12.6 20 3.5M16.6 6.9l2.5 2.5M14.4 9.1l1.8 1.8"/>',
    "restart": '<path d="M19.8 12.5a7.8 7.8 0 1 1-2.3-6"/><path d="M19.8 4v4.6h-4.6"/>',
    "power": '<path d="M12 3.5v8"/><path d="M6.5 7a8 8 0 1 0 11 0"/>',
    "xcircle": '<circle cx="12" cy="12" r="8.8"/><path d="m9.2 9.2 5.6 5.6m0-5.6-5.6 5.6"/>',
    "nosign": '<circle cx="12" cy="12" r="8.8"/><path d="m5.8 5.8 12.4 12.4"/>',
    "trash": '<path d="M4.5 6.8h15M9.5 6.8V4.6h5v2.2M6.4 6.8l.9 12.4a1.6 1.6 0 0 0 1.6 1.5h6.2a1.6 1.6 0 0 0 1.6-1.5l.9-12.4M10 10.5v6.5M14 10.5v6.5"/>',
    "download": '<path d="M12 3.8v11M7.6 10.6 12 15l4.4-4.4M5 19.6h14"/>',
    "plus": '<path d="M12 5v14M5 12h14"/>',
    "clock": '<circle cx="12" cy="12" r="8.8"/><path d="M12 7.2V12l3.2 2"/>',
    "desktop": '<rect x="3" y="4" width="18" height="12.5" rx="2.2"/><path d="M9 20.2h6M12 16.5v3.7"/>',
    "check": '<circle cx="12" cy="12" r="8.8"/><path d="m8.3 12.4 2.6 2.6 4.9-5.4"/>',
    "warn": '<path d="M10.3 4.6 3.4 16.7A2 2 0 0 0 5.1 19.7h13.8a2 2 0 0 0 1.7-3L13.7 4.6a2 2 0 0 0-3.4 0z"/><path d="M12 9.5v4.2M12 16.6v.1"/>',
    "undo": '<path d="M9 14.5 4 9.5l5-5"/><path d="M4 9.5h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    "late": '<circle cx="12" cy="12" r="8.8"/><path d="M12 7.2V12l3.2 2"/>',
    "question": '<circle cx="12" cy="12" r="8.8"/><path d="M9.6 9.4a2.5 2.5 0 0 1 4.8.9c0 1.7-2.4 2.2-2.4 3.7M12 16.8v.1"/>',
    "chart": '<path d="M4 20h16M7 16.5V11M12 16.5V6.5M17 16.5v-4"/>',
    "building": '<path d="M4.5 20.5V5.2A1.2 1.2 0 0 1 5.7 4h7.6a1.2 1.2 0 0 1 1.2 1.2v15.3M14.5 9.5h3.8a1.2 1.2 0 0 1 1.2 1.2v9.8M3 20.5h18M8 8h3M8 11.5h3M8 15h3"/>',
}
EMOJI_IC = {"✅": "check", "⚠️": "warn", "⚠": "warn", "🗑": "trash", "↩": "undo", "📸": "camera", "⛔": "nosign",
            "🔓": "unlock", "🔒": "lock", "🔑": "key", "💬": "message", "🔄": "restart", "⏻": "power", "✋": "xcircle",
            "⬇": "download", "⏱": "clock", "🖥": "desktop", "⏰": "late", "❌": "xcircle", "❓": "question"}


def ic(name, cls=""):
    return (f'<svg class="ic{" " + cls if cls else ""}" viewBox="0 0 24 24" aria-hidden="true">{_IC.get(name, "")}</svg>')


def iconize(text):
    """'✅ Matn' -> <svg> + 'Matn' (veb uchun; Telegram xabarlarida emoji qoladi). HTML qaytaradi."""
    s = str(text or "")
    for e, n in EMOJI_IC.items():
        if s.startswith(e):
            return ic(n) + esc(s[len(e):].lstrip(" ️"))
    return esc(s)


IC_JS = json.dumps({k: ic(k) for k in _IC}).replace("<", "\\u003c")


def brand_nav(active):
    """Hamma sahifalar tepasidagi panel: logo + bo'limlar + chiqish."""
    items = [("dashboard", "/dashboard", "Davomat"), ("hisobotlar", "/hisobotlar", "Hisobotlar"),
             ("screentime", "/screentime", "Screen time")]
    links = "".join(f'<a href="{u}"{" class=on" if k == active else ""}>{t}</a>' for k, u, t in items)
    return (f'<nav class="topbar"><a class="brand" href="/dashboard" aria-label="Gigant Buxgalter">'
            f'<img class="logo-l" src="/brand/logo.png" alt="Gigant Buxgalter"><img class="logo-d" src="/brand/logo-w.png" alt="">'
            f'</a><div class="navpill glass">{links}</div><a class="navout" href="/logout">Chiqish</a></nav>')


def render_login(err=""):
    msg = LOGIN_MSG.get(err, "")
    cls = "ok" if err == "out" else "err"
    return f"""<!DOCTYPE html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Kirish · Gigant Buxgalter</title>{BRAND_HEAD}<style>
:root{{--ink:#2b1a0f;--mut:#97755e;--acc:#f97316;--acc-soft:rgba(249,115,22,.14);--glass:rgba(255,255,255,.55);
--edge:rgba(255,255,255,.85);--line:rgba(194,65,12,.14);--field:rgba(255,255,255,.75);--shadow:rgba(194,65,12,.16);
--bg1:#fff8f1;--bg2:#ffe9d6;--b1:rgba(251,146,60,.55);--b2:rgba(253,186,116,.6);--r:#b91c1c;--r-bg:rgba(239,68,68,.12);--g:#15803d;--g-bg:rgba(34,197,94,.14)}}
@media (prefers-color-scheme:dark){{:root{{--ink:#fbeee4;--mut:#c19c84;--acc:#fb923c;--glass:rgba(48,28,16,.55);
--edge:rgba(255,255,255,.1);--field:rgba(0,0,0,.25);--shadow:rgba(0,0,0,.4);--bg1:#160c06;--bg2:#24130a;
--b1:rgba(234,88,12,.4);--b2:rgba(251,146,60,.22);--r:#fca5a5;--g:#4ade80}}}}
*{{box-sizing:border-box}}
body{{margin:0;min-height:100vh;display:grid;place-items:center;padding:16px;font:14px/1.5 "Segoe UI",system-ui,sans-serif;color:var(--ink);
background:radial-gradient(800px 500px at 10% -10%,var(--b1),transparent 60%),radial-gradient(700px 480px at 110% 110%,var(--b2),transparent 60%),
linear-gradient(170deg,var(--bg1),var(--bg2));background-attachment:fixed}}
.card{{width:min(380px,100%);padding:32px 28px;border-radius:26px;background:var(--glass);border:1px solid var(--edge);
backdrop-filter:blur(24px) saturate(170%);-webkit-backdrop-filter:blur(24px) saturate(170%);box-shadow:0 24px 70px -24px var(--shadow)}}
h1{{font-size:22px;font-weight:600;margin:0 0 4px;display:flex;align-items:center;gap:12px}}
h1:before{{content:"";width:12px;height:12px;border-radius:50%;background:var(--acc);box-shadow:0 0 0 6px var(--acc-soft)}}
p{{margin:0 0 22px;color:var(--mut)}}
label{{display:block;font-size:12px;font-weight:600;color:var(--mut);margin-bottom:14px}}
input{{display:block;width:100%;margin-top:6px;font:inherit;font-size:15px;color:var(--ink);padding:11px 14px;border-radius:12px;
border:1px solid var(--line);background:var(--field);outline:none}}
input:focus{{border-color:var(--acc);box-shadow:0 0 0 3px var(--acc-soft)}}
button{{width:100%;margin-top:8px;font:inherit;font-weight:600;font-size:15px;border:0;border-radius:999px;padding:12px;cursor:pointer;
background:var(--acc);color:#fff;box-shadow:0 8px 22px -8px var(--acc)}}
.msg{{padding:10px 14px;border-radius:12px;font-weight:600;font-size:13px;margin-bottom:16px}}
.msg.err{{background:var(--r-bg);color:var(--r)}}.msg.ok{{background:var(--g-bg);color:var(--g)}}
.foot{{text-align:center;font-size:12px;color:var(--mut);margin-top:18px}}
{BRAND_CSS}
button{{background:var(--grad);box-shadow:0 10px 24px -12px var(--acc-deep)}}
.card{{padding:36px 32px}}.brand{{justify-content:center;margin-bottom:26px}}.brand img{{height:52px}}
h1{{justify-content:center;font-size:19px}}p{{text-align:center}}
</style></head><body><form class="card" method="post" action="/login" autocomplete="on">
<div class="brand"><img class="logo-l" src="/brand/logo.png" alt="Gigant Buxgalter"><img class="logo-d" src="/brand/logo-w.png" alt=""></div>
<h1>Boshqaruv paneli</h1><p>Davom etish uchun tizimga kiring</p>
{f'<div class="msg {cls}">{esc(msg)}</div>' if msg else ''}
<label>Login<input name="username" autocomplete="username" required autofocus></label>
<label>Parol<input name="password" type="password" autocomplete="current-password" required></label>
<button>Kirish</button>
<div class="foot">Xavfsizlik uchun {SESSION_TTL // 60} daqiqa harakatsiz qolinsa, tizim avtomatik chiqaradi</div>
</form></body></html>"""


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
        today = (f'<span class="pill g">{ic("check")}{t["ts"][11:16]}</span>' if t and t["status"] == "on_time" else
                 f'<span class="pill y">{ic("late")}{t["ts"][11:16]}</span>' if t and t["status"] == "late" else
                 f'<span class="pill r">{ic("xcircle")}kelmadi</span>' if t else
                 ("—" if not is_workday(d) else f'<span class="pill">{ic("clock")}hali yo‘q</span>'))
        lvl = lvl_txt.get(m["level"], "")
        rows.append(f"""<tr>
<td><b>{esc(s['e']['name'])}</b><div class="sub">ID {s['e']['tg_id']} · <a href="/screentime?u={s['e']['tg_id']}" style="color:var(--acc);font-weight:600;text-decoration:none">{ic("desktop")}Kompyuter</a></div></td>
<td>{today}</td>
<td>{pill(s['eff'])}</td>
<td>{s['on_time']} / {s['late']} / {s['absent']}</td>
<td>{pill(m['eff'])}</td>
<td>{m['viol']}{' <span class="pill r">' + lvl + '</span>' if lvl else ''}</td>
<td>{m['fine_pct']}%{'<div class="sub">' + money(m['fine_sum']) + '</div>' if s['e']['salary'] and m['fine_pct'] else ''}</td>
<td>{len(s['comps'])}</td>
<td>{m['reports']}</td></tr>""")

    reps = q("SELECT r.*, e.name ename FROM reports r LEFT JOIN employees e ON e.tg_id=r.tg_id "
             f"WHERE r.inn IN (SELECT inn FROM companies WHERE {LISTED}) ORDER BY r.ts DESC LIMIT 40")
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
    flash_html = f'<div class="flash glass">{iconize(FLASH[flash])}</div>' if flash in FLASH else ""

    total = len(week)
    on_today = sum(1 for s in week if s["today"] and s["today"]["status"] == "on_time")
    late_today = sum(1 for s in week if s["today"] and s["today"]["status"] == "late")
    effs = [s["eff"] for s in week if s["eff"] is not None]
    avg = round(sum(effs) / len(effs)) if effs else None

    return f"""<!DOCTYPE html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Davomat · Gigant Buxgalter</title>{BRAND_HEAD}<style>
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
a.btn{{text-decoration:none;display:inline-block}}
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
{BRAND_CSS}
</style></head><body><div class="wrap">
{brand_nav("dashboard")}
<header><h1>Hodimlar samaradorligi</h1>
<div class="hright"><div class="mut meta glass">Hafta {a:%d.%m} – {b:%d.%m.%Y} · {WORK_START}–{WORK_END} · {now():%H:%M}</div>
<button class="btn" id="addEmp">{ic("plus")}Hodim qo'shish</button></div></header>
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
// Har daqiqa yangilanadi. Oxirgi daqiqada sichqoncha/klaviatura/aylantirish bo'lsa — faol (sessiya uzayadi).
let lastAct=/[?&]auto=1/.test(location.search)?0:Date.now();
['mousemove','mousedown','keydown','scroll','touchstart'].forEach(ev=>addEventListener(ev,()=>{{lastAct=Date.now()}},{{passive:true}}));
setInterval(()=>{{if(document.querySelector('dialog[open]'))return;
location.replace('/dashboard?auto=1&active='+(Date.now()-lastAct<60000?1:0))}},60000);
</script></body></html>"""


MATRIX_CSS = """
:root{--ink:#2b1a0f;--mut:#97755e;--acc:#f97316;--acc-soft:rgba(249,115,22,.12);
--glass:rgba(255,255,255,.58);--edge:rgba(255,255,255,.85);--line:rgba(194,65,12,.12);--shadow:rgba(194,65,12,.10);
--bg1:#fff8f1;--bg2:#ffe9d6;--b1:rgba(251,146,60,.45);--b2:rgba(253,186,116,.5);--solid:#fffaf5;
--g:#15803d;--g-bg:rgba(34,197,94,.16);--y:#b45309;--y-bg:rgba(251,191,36,.22);--o:#c2410c;--o-bg:rgba(251,146,60,.22);
--r:#b91c1c;--r-bg:rgba(239,68,68,.14);--n:#8a7565;--n-bg:rgba(120,90,70,.07);--dlg:rgba(255,250,245,.94);--field:rgba(255,255,255,.75)}
@media (prefers-color-scheme:dark){:root{--ink:#fbeee4;--mut:#c19c84;--acc:#fb923c;--acc-soft:rgba(251,146,60,.16);
--glass:rgba(48,28,16,.55);--edge:rgba(255,255,255,.10);--line:rgba(251,146,60,.14);--shadow:rgba(0,0,0,.35);
--bg1:#160c06;--bg2:#24130a;--b1:rgba(234,88,12,.35);--b2:rgba(251,146,60,.2);--solid:#21130b;
--g:#4ade80;--g-bg:rgba(74,222,128,.15);--y:#fcd34d;--y-bg:rgba(251,191,36,.16);--o:#fdba74;--o-bg:rgba(251,146,60,.18);
--r:#fca5a5;--r-bg:rgba(248,113,113,.16);--n:#a8917f;--n-bg:rgba(255,255,255,.05);--dlg:rgba(38,22,12,.95);--field:rgba(0,0,0,.25)}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font:14px/1.45 "Segoe UI",system-ui,-apple-system,Roboto,sans-serif;color:var(--ink);
background:radial-gradient(900px 520px at 8% -8%,var(--b1),transparent 62%),radial-gradient(760px 480px at 105% 6%,var(--b2),transparent 60%),
linear-gradient(170deg,var(--bg1),var(--bg2));background-attachment:fixed}
.wrap{max-width:1500px;margin:0 auto;padding:32px 24px 56px}
.glass{background:var(--glass);border:1px solid var(--edge);border-radius:20px;backdrop-filter:blur(22px) saturate(170%);
-webkit-backdrop-filter:blur(22px) saturate(170%);box-shadow:0 10px 40px -12px var(--shadow),inset 0 1px 0 var(--edge)}
header{display:flex;align-items:center;justify-content:space-between;gap:14px;flex-wrap:wrap}
h1{font-size:25px;font-weight:600;letter-spacing:-.02em;margin:0;display:flex;align-items:center;gap:12px}
h1:before{content:"";width:12px;height:12px;border-radius:50%;background:var(--acc);box-shadow:0 0 0 6px var(--acc-soft)}
h2{font-size:12px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin:28px 4px 10px}
.sub,.mut{color:var(--mut)}.sub{font-size:12px}
.hright{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.btn{font:inherit;font-weight:600;font-size:13px;border:0;border-radius:999px;padding:9px 15px;cursor:pointer;background:var(--acc);
color:#fff;text-decoration:none;display:inline-block;box-shadow:0 6px 18px -6px var(--acc)}
.btn.ghost{background:var(--acc-soft);color:var(--acc);box-shadow:none}.btn.red{background:var(--r);box-shadow:none}
.pnav{display:flex;align-items:center;gap:4px;padding:4px;border-radius:999px}
.pnav a{width:34px;height:34px;display:grid;place-items:center;border-radius:50%;text-decoration:none;color:var(--acc);font-size:18px;font-weight:600}
.pnav a:hover{background:var(--acc-soft)}
.pnav select{font:inherit;font-weight:600;border:0;background:transparent;color:var(--ink);padding:6px 4px;cursor:pointer}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:12px;margin-top:24px}
.kpi{padding:16px 20px}.kpi span{font-size:13px;color:var(--mut)}
.kpi b{display:block;font-size:30px;font-weight:600;letter-spacing:-.03em;margin-top:4px}
.kpi.g b{color:var(--g)}.kpi.r b{color:var(--r)}.kpi.o b{color:var(--o)}.kpi.a b{color:var(--acc)}
.staff{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(230px,100%),1fr));gap:10px}
.st{padding:12px 16px;cursor:pointer;border:1px solid var(--edge)}.st.on{outline:2px solid var(--acc)}
.st .top{display:flex;justify-content:space-between;gap:8px;font-weight:600}
.bar{height:6px;border-radius:99px;background:var(--n-bg);margin-top:8px;overflow:hidden}.bar i{display:block;height:100%;background:var(--g)}
.tools{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin:0 0 12px}
.tools input[type=search],.tools select{font:inherit;padding:9px 14px;border-radius:999px;border:1px solid var(--line);background:var(--field);color:var(--ink);min-width:0}
.tools input[type=search]{flex:1 1 220px}
.tools label{display:flex;gap:6px;align-items:center;font-size:13px;cursor:pointer}
.legend{display:flex;gap:6px 14px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin:0 4px 12px}
.legend span{display:inline-flex;align-items:center;gap:6px}
.card{overflow:auto;max-height:calc(100vh - 120px)}
table{border-collapse:separate;border-spacing:0;width:100%}
th,td{padding:7px 8px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle;white-space:nowrap}
thead th{position:sticky;top:0;z-index:2;background:var(--solid);font-size:11px;color:var(--mut);font-weight:600;
text-transform:uppercase;letter-spacing:.04em;text-align:center}
thead th.l{text-align:left}
td.co,th.co{position:sticky;left:0;z-index:1;background:var(--solid);min-width:240px;max-width:300px;white-space:normal}
thead th.co{z-index:3}
td.co b{font-weight:600;cursor:pointer}td.co b:hover{color:var(--acc)}
tbody tr:hover td{background:var(--acc-soft)}tbody tr:hover td.co{background:var(--solid)}
td.n{color:var(--mut);font-size:12px;text-align:right}
.sheet th,.sheet td{border-right:1px solid var(--line)}
.sheet thead th{white-space:normal;min-width:64px;line-height:1.2;vertical-align:bottom}
td.ed{cursor:text;min-width:90px;max-width:220px;white-space:normal;font-size:13px}
td.co.ed{font-weight:600}
td.ed:hover{box-shadow:inset 0 0 0 1px var(--acc)}
td.ed[contenteditable=true]{box-shadow:inset 0 0 0 2px var(--acc);background:var(--field);outline:none}
td.ro{color:var(--mut);font-size:12.5px}
.saved{animation:sv 1.2s}@keyframes sv{from{background:var(--g-bg)}}
td.x{padding:0;min-width:104px;text-align:center;cursor:pointer;font-size:12px;font-weight:600;line-height:1.15;color:var(--n)}
td.x>div{padding:6px;min-height:42px;display:flex;flex-direction:column;align-items:center;justify-content:center}
td.x small{font-size:10.5px;font-weight:500;opacity:.85;margin-top:2px}
td.x:hover,td.x.sel{box-shadow:inset 0 0 0 2px var(--acc)}
td.x.ok{background:var(--g-bg);color:var(--g)}td.x.late{background:var(--o-bg);color:var(--o)}
td.x.wait{background:var(--y-bg);color:var(--y)}td.x.error{background:var(--r-bg);color:var(--r)}
td.x.missing>div{box-shadow:inset 0 0 0 1.5px var(--r);border-radius:6px;margin:3px;min-height:36px;color:var(--r)}
td.x.pending>div{box-shadow:inset 0 0 0 1.5px var(--y);border-radius:6px;margin:3px;min-height:36px;color:var(--y)}
td.x.na,td.x.never{color:var(--n);font-weight:500}td.x.never{opacity:.55}
td.x.note{background:var(--n-bg);color:var(--ink);font-weight:500;white-space:normal}
tr.gone td{opacity:.6}
td.rt{font-weight:700;font-size:13px;text-align:center;min-width:76px;letter-spacing:.02em}
td.rt.gA{color:#15803d;background:rgba(34,197,94,.14)}td.rt.gB{color:#4d7c0f;background:rgba(132,204,22,.16)}
td.rt.gC{color:#c2410c;background:rgba(251,146,60,.18)}td.rt.gD,td.rt.gE{color:#b91c1c;background:rgba(239,68,68,.14)}
td.per{font-weight:600;color:var(--ink);white-space:nowrap}
td.del{padding:0 4px}td.del button{border:0;background:none;color:var(--mut);cursor:pointer;font-size:16px;width:28px;height:28px;border-radius:50%}
td.del button:hover{background:var(--r-bg);color:var(--r)}
#menu,#pick{position:fixed;z-index:50;width:290px;max-width:calc(100vw - 16px);padding:12px;border-radius:16px;background:var(--dlg);
border:1px solid var(--edge);box-shadow:0 20px 60px -15px var(--shadow)}
#pick{width:240px}#pick .opts{grid-template-columns:1fr}
#menu .mh,#pick .mh{font-weight:600;margin-bottom:6px}
#menu .info{margin:0 0 8px;padding:8px 10px;font-size:12px;max-height:190px;overflow:auto}
#menu .opts,#pick .opts{display:grid;grid-template-columns:1fr 1fr;gap:5px;max-height:300px;overflow:auto}
#pick .opts{grid-template-columns:1fr}
#menu button,#pick button{font:inherit;font-size:12.5px;font-weight:600;border:0;border-radius:10px;padding:7px 8px;cursor:pointer;
background:var(--n-bg);color:var(--ink);text-align:left}
#menu button:hover,#pick button:hover{background:var(--acc-soft);color:var(--acc)}
#pick button.on{background:var(--acc);color:#fff}
#menu .ok{color:var(--g)}#menu .late{color:var(--o)}#menu .error{color:var(--r)}#menu .wait{color:var(--y)}
td.pk{cursor:pointer;min-width:96px;font-size:13px;white-space:normal}
td.pk::after{content:" ▾";color:var(--mut);font-size:10px}
td.pk:hover,td.pk.sel{box-shadow:inset 0 0 0 2px var(--acc)}
dialog select{display:block;width:100%;margin-top:5px;font:inherit;color:var(--ink);padding:9px 12px;
border-radius:12px;border:1px solid var(--line);background:var(--field)}
#menu form,#pick form{display:flex;gap:5px;margin:6px 0}#menu input,#pick input{flex:1;min-width:0;font:inherit;font-size:13px;padding:7px 10px;
border-radius:10px;border:1px solid var(--line);background:var(--field);color:var(--ink)}
#menu .sep{font-size:11px;color:var(--mut);margin:8px 2px 4px;text-transform:uppercase;letter-spacing:.05em}
.tabs{display:inline-flex;gap:4px;padding:4px;border-radius:999px;margin:28px 0 12px}
.tab{font:inherit;font-weight:600;font-size:13px;border:0;border-radius:999px;padding:8px 16px;cursor:pointer;background:none;color:var(--mut)}
.tab.on{background:var(--acc);color:#fff}
.log td{font-size:12.5px}.log td.w{white-space:normal;min-width:220px}
.dot{width:12px;height:12px;border-radius:4px;display:inline-block}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:600;background:var(--n-bg);color:var(--mut)}
.flash{margin-top:18px;padding:12px 18px;font-weight:600}
dialog{border:1px solid var(--edge);border-radius:22px;padding:24px;width:min(460px,calc(100vw - 32px));color:var(--ink);background:var(--dlg);
box-shadow:0 30px 80px -20px var(--shadow)}
dialog::backdrop{background:rgba(70,30,5,.25);backdrop-filter:blur(3px)}
dialog h3{margin:0 0 4px;font-size:18px;font-weight:600}
.info{margin:14px 0;padding:12px 14px;border-radius:14px;background:var(--n-bg);font-size:13px;white-space:pre-wrap}
.acts{display:grid;grid-template-columns:1fr 1fr;gap:8px}.acts .btn{width:100%;text-align:center}
.acts .full{grid-column:1/-1}
dialog label{display:block;font-size:12px;color:var(--mut);font-weight:600;margin-bottom:10px}
dialog input[type=text],dialog textarea{display:block;width:100%;margin-top:5px;font:inherit;color:var(--ink);padding:9px 12px;
border-radius:12px;border:1px solid var(--line);background:var(--field)}
dialog textarea{min-height:70px;resize:vertical}
.taxpick{display:grid;grid-template-columns:1fr 1fr;gap:4px 12px;margin:6px 0 12px}
.taxpick label{display:flex;gap:8px;align-items:center;margin:0;font-weight:500;color:var(--ink);font-size:13px}
.close{display:flex;justify-content:flex-end;margin-top:14px}
dialog.wide{width:min(640px,calc(100vw - 32px))}
.trash{max-height:55vh;overflow:auto;margin-bottom:14px}.trash td{white-space:normal}
.trash td.ta{text-align:right;white-space:nowrap}.trash td.ta form{display:inline-block;margin-left:6px}
.btn.sm{padding:6px 12px;font-size:12px}.btn.danger{background:var(--r-bg);color:var(--r);box-shadow:none}
@media (max-width:640px){.wrap{padding:20px 16px 40px}h1{font-size:21px}.kpi b{font-size:24px}
td.co,th.co{min-width:150px}td.x{min-width:92px}}
"""

MATRIX_JS = """
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const ss={get:k=>{try{return sessionStorage.getItem(k)}catch(e){return null}},set:(k,v)=>{try{sessionStorage.setItem(k,v)}catch(e){}}};
const q=$('#q'),fs=$('#fstaff'),fm=$('#fmiss');
function apply(){const t=(q.value||'').toLowerCase().trim(),s=fs.value,m=fm.checked;let n=0;
 $$('#pane-sheet tbody tr').forEach(tr=>{const ok=(!t||tr.dataset.k.includes(t))&&(!s||tr.dataset.s===s)&&(!m||tr.dataset.m==='1');
 tr.hidden=!ok;if(ok)n++});$('#shown').textContent=n;
 $$('#pane-log tbody tr').forEach(tr=>{tr.hidden=!!t&&!tr.dataset.k.includes(t)});
 $$('.st').forEach(c=>c.classList.toggle('on',!!s&&c.dataset.s===s));
 ss.set('mx_q',q.value);ss.set('mx_s',s);ss.set('mx_m',m?'1':'');}
q.value=ss.get('mx_q')||'';fs.value=ss.get('mx_s')||'';fm.checked=ss.get('mx_m')==='1';
[q,fs,fm].forEach(el=>el.addEventListener('input',apply));apply();
$$('.st').forEach(c=>c.onclick=()=>{fs.value=fs.value===c.dataset.s?'':c.dataset.s;apply()});
$('#per').onchange=e=>location.href='/hisobotlar?p='+e.target.value;
function showTab(t){$$('.tab').forEach(b=>b.classList.toggle('on',b.dataset.t===t));
 $$('.pane').forEach(p=>p.hidden=p.id!=='pane-'+t);ss.set('mx_tab',t)}
$$('.tab').forEach(b=>b.onclick=()=>showTab(b.dataset.t));showTab(ss.get('mx_tab')==='log'?'log':'sheet');
async function api(data){
 const r=await fetch('/dashboard/api/edit',{method:'POST',credentials:'same-origin',
  headers:{'Content-Type':'application/x-www-form-urlencoded'},body:new URLSearchParams(Object.assign({csrf:CSRF,p:PERIOD},data))});
 let j=null;try{j=await r.json()}catch(e){}
 if(!j||!j.ok){alert((j&&j.error)||'Saqlanmadi — sessiya tugagan bo‘lishi mumkin. Sahifani yangilang.');throw new Error('save')}
 return j}
// Matnli kataklar: bosing — yozing — Enter (Esc — bekor)
$$('td.ed').forEach(td=>{
 td.onclick=()=>{if(td.isContentEditable)return;closeMenu();td.dataset.old=td.textContent;td.contentEditable='true';td.focus();
  const r=document.createRange();r.selectNodeContents(td);const s=getSelection();s.removeAllRanges();s.addRange(r)};
 td.onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();td.blur()}
  if(e.key==='Escape'){td.textContent=td.dataset.old;td.blur()}};
 td.onblur=async()=>{if(!td.isContentEditable)return;td.contentEditable='false';const v=td.textContent.trim();
  if(v===(td.dataset.old||'').trim()){td.textContent=td.dataset.old;return}
  try{const j=await api({kind:'co',inn:td.closest('tr').dataset.inn,field:td.dataset.f,v});td.textContent=j.text;
   if(td.dataset.f==='rating')td.className='ed rt g'+(j.grade||'');
   td.classList.add('saved');setTimeout(()=>td.classList.remove('saved'),1300)}catch(e){td.textContent=td.dataset.old}}});
// Soliq kataklari: bosing — tafsilot va tanlov
const menu=$('#menu');let cur=null;
function closeMenu(){menu.hidden=true;if(cur)cur.classList.remove('sel');cur=null}
$$('td.x').forEach(td=>td.onclick=e=>{e.stopPropagation();if(cur===td)return closeMenu();closeMenu();cur=td;td.classList.add('sel');
 menu.querySelector('.mh').textContent=td.closest('tr').dataset.name+' · '+td.dataset.tn+' · '+PERIOD_LABEL;
 const inf=menu.querySelector('.info');inf.textContent=td.dataset.info||'';inf.hidden=!td.dataset.info;
 menu.querySelector('input').value='';menu.hidden=false;
 const r=td.getBoundingClientRect(),w=menu.offsetWidth,h=menu.offsetHeight;
 let x=Math.min(r.left,innerWidth-w-8),y=r.bottom+4;if(y+h>innerHeight-8)y=Math.max(8,r.top-h-4);
 menu.style.left=Math.max(8,x)+'px';menu.style.top=y+'px'});
document.addEventListener('click',e=>{if(!menu.hidden&&!menu.contains(e.target))closeMenu()});
addEventListener('keydown',e=>{if(e.key==='Escape')closeMenu()});
$$('.card').forEach(c=>c.addEventListener('scroll',closeMenu,{passive:true}));
async function setCell(v){const td=cur;if(!td)return;const obl=v.startsWith('obl:');
 try{const j=await api({kind:obl?'obl':'cell',inn:td.closest('tr').dataset.inn,tax:td.dataset.tax,v:obl?v.slice(4):v});
  if(j.reload){location.reload();return}
  td.className='x '+j.cls;td.innerHTML=j.html;td.dataset.info=j.info;td.title=j.info;closeMenu();
  td.classList.add('saved');setTimeout(()=>td.classList.remove('saved'),1300)}catch(e){}}
menu.querySelectorAll('[data-v]').forEach(b=>b.onclick=()=>setCell(b.dataset.v));
menu.querySelector('form').onsubmit=e=>{e.preventDefault();const t=menu.querySelector('input').value.trim();if(t)setCell('t:'+t)};
// Ro'yxatdan tanlash: Ходимлар (bot hodimlari), Солиқ режими, Период
const pick=$('#pick');let pcur=null;
const PT={staff:'Ходимлар',regime:'Солиқ режими',perk:'Период'};
function closePick(){pick.hidden=true;if(pcur)pcur.classList.remove('sel');pcur=null}
async function setPick(v){const td=pcur;if(!td)return;const f=td.dataset.f;
 try{const j=await api(f==='staff'?{kind:'staff',inn:td.closest('tr').dataset.inn,v}:{kind:'co',inn:td.closest('tr').dataset.inn,field:f,v});
  if(j.reload){location.reload();return}
  td.textContent=j.text;td.dataset.v=j.text;closePick();td.classList.add('saved');setTimeout(()=>td.classList.remove('saved'),1300)}catch(e){}}
$$('td.pk').forEach(td=>td.onclick=e=>{e.stopPropagation();if(pcur===td)return closePick();closePick();closeMenu();pcur=td;td.classList.add('sel');
 const f=td.dataset.f;pick.querySelector('.mh').textContent=td.closest('tr').dataset.name+' · '+PT[f];
 const box=pick.querySelector('.opts');box.innerHTML='';
 (PICKS[f]||[]).forEach(([v,t])=>{const b=document.createElement('button');b.type='button';b.textContent=t;
  if(v===td.dataset.v&&v!=='')b.className='on';b.onclick=()=>setPick(v);box.appendChild(b)});
 if(f==='staff'&&PICKS.staff.length<2){const s=document.createElement('div');s.className='sub';
  s.textContent='Botda hali hodim yo‘q — avval hodim qo‘shing.';box.prepend(s)}
 const fm=pick.querySelector('form');fm.hidden=f==='staff';fm.querySelector('input').value='';pick.hidden=false;
 const r=td.getBoundingClientRect(),w=pick.offsetWidth,h=pick.offsetHeight;
 let x=Math.min(r.left,innerWidth-w-8),y=r.bottom+4;if(y+h>innerHeight-8)y=Math.max(8,r.top-h-4);
 pick.style.left=Math.max(8,x)+'px';pick.style.top=y+'px'});
pick.querySelector('form').onsubmit=e=>{e.preventDefault();const t=pick.querySelector('input').value.trim();if(t)setPick(t)};
document.addEventListener('click',e=>{if(!pick.hidden&&!pick.contains(e.target))closePick()});
addEventListener('keydown',e=>{if(e.key==='Escape')closePick()});
$$('.card').forEach(c=>c.addEventListener('scroll',closePick,{passive:true}));
$('#addCo').onclick=()=>$('#dAdd').showModal();
$$('[data-close]').forEach(b=>b.onclick=()=>b.closest('dialog').close());
$$('form.hide').forEach(f=>f.onsubmit=()=>confirm('Korxona karzinaga o‘tkazilsinmi?\\nU haqida botga xabar kelmaydi. Karzinadan qaytarish mumkin.'));
$$('form.purge').forEach(f=>f.onsubmit=()=>confirm('Korxona BUTUNLAY o‘chirilsinmi?\\nJadvaldagi belgilari, hisobotlari va tarixi o‘chadi. Qaytarib bo‘lmaydi.'));
$('#trashBtn')&&($('#trashBtn').onclick=()=>$('#dTrash').showModal());
let lastAct=/[?&]auto=1/.test(location.search)?0:Date.now();
['mousemove','mousedown','keydown','scroll','touchstart'].forEach(ev=>addEventListener(ev,()=>{lastAct=Date.now()},{passive:true}));
setInterval(()=>{if(document.querySelector('dialog[open],[contenteditable=true]')||!menu.hidden||!pick.hidden||Date.now()-lastAct<110000)return;
 location.replace('/hisobotlar?p='+NAV+'&auto=1&active=0')},120000);
"""

MX_FLASH = {"co_hidden": "🗑 Korxona karzinaga o'tkazildi — endi u haqida xabar kelmaydi",
            "co_restored": "↩ Korxona karzinadan qaytarildi", "co_purged": "✅ Korxona butunlay o'chirildi",
            "co_add": "✅ Yangi mijoz qo'shildi",
            "bad_inn": "⚠️ INN 9 yoki 14 raqam bo'lishi kerak", "stale": "⚠️ Sahifa eskirgan edi — yangilandi. Qayta urining."}
WORDS = {"ok": "топширилган", "late": "кечикиб топширилган", "wait": "текширувда", "error": "хато", "na": "йук",
         "missing": "топширилмаган", "pending": "кутилмоқда"}
LOG_FIELDS = ["№ Отчета", "№ Пакет", "Наименование", "Год", "Период", "Дата отправки", "Состояние", "Причина ошибки",
              "Дата проверки", "Инспектор", "Регион", "Сертификат", "Дата создания", "IP адрес"]
CO_FIELDS = {"name": 120, "activity": 80, "staff": 80, "c1": 40, "note": 500, "regime": 40, "perk": 40, "rating": 20}
# Ro'yxatdan tanlanadigan ustunlar (Excel'dagi "Солик режими" va "Период"); "boshqa" matn ham yozish mumkin.
REGIMES = ["Айланма", "НДС", "ЯТТ", "Фикс"]
PERKS = ["Ойлик", "Чораклик", "Йиллик"]


def cell_view(cell):
    """Katak -> (css klass, ichki html, tafsilot matni). Matn Excel'dagi so'zlar bilan."""
    st, src = cell["st"], cell.get("src")
    det = {}
    if src == "soliq":
        try:
            det = json.loads(cell.get("detail") or "{}")
        except ValueError:
            pass
    rec = det.get("rec") or {k2: det.get(k1) for k1, k2 in (("form", "Наименование"), ("state", "Состояние"),
                                                              ("sent", "Дата отправки"), ("err", "Причина ошибки"))}
    small = ""
    if src == "soliq":
        small = (rec.get("Дата отправки") or "")[:5]
        info = "\n".join(["Manba: soliq.uz (avtomatik)", f"Yuborgan hodim: {det.get('by') or '—'}"]
                         + [f"{k}: {v}" for k, v in rec.items() if v])
    elif src == "excel":
        info = "Manba: eski Excel fayl" + (f"\nExcel'da: {cell.get('detail')}" if cell.get("detail") else "")
    elif src == "manual":
        small = "qo'lda"
        info = f"Manba: dashboardda qo'lda belgilangan ({(cell.get('ts') or '')[:16].replace('T', ' ')})"
    else:
        info = ""
    if st == "missing":
        info = "Topshirilmagan — muddat o'tgan.\nBu korxona odatda shu hisobotni topshiradi."
    elif st == "pending":
        info = "Hali topshirilmagan, muddat o'tmagan."
    if st == "note":
        text = cell.get("detail") or ""
    elif st is None:
        text = "йук" if cell.get("never") else ""
        info = "Belgilangan: bu korxona bu soliqni topshirmaydi" if cell.get("never") else ""
    else:
        text = WORDS.get(st, st)
    cls = st or ("never" if cell.get("never") else "none")
    html_ = f"<div>{esc(text)}{f'<small>{esc(small)}</small>' if small else ''}</div>"
    return cls, html_, info


def text_status(t):
    s = t.lower().replace("ў", "у").replace("ё", "е")
    if "кечик" in s:
        return "late"
    if "топширилмаган" in s:
        return "note"
    if "топшир" in s or "сдан" in s:
        return "ok"
    if s in ("йук", "йок", "нет", "-", "—", "х", "x", "yo'q", "yoq") or "шарт эмас" in s:
        return "na"
    if "хато" in s or "ошиб" in s:
        return "error"
    if "текшир" in s:
        return "wait"
    return "note"


def all_periods():
    ps = {r["period"] for r in q("SELECT DISTINCT period FROM filings")}
    cur = default_period()
    ps |= {period_shift(cur, i) for i in range(-3, 2)}
    return sorted(ps, reverse=True)


def period_reports(per):
    """Shu davrga tegishli, bot qabul qilgan hamma hisobotlar (har biri oxirgi holati bilan)."""
    latest = {}
    for r in q("SELECT r.*, e.name ename FROM reports r LEFT JOIN employees e ON e.tg_id=r.tg_id "
               f"WHERE r.inn IN (SELECT inn FROM companies WHERE {LISTED}) ORDER BY r.ts"):
        try:
            d = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        if report_period(d, r["ts"]) == per:
            latest[r["report_id"]] = (r, d)
    return sorted(latest.values(), key=lambda x: x[0]["ts"], reverse=True)


def rating_grade(r):
    """'С - 34' / 'A-80' -> ('C', 'C · 34'). Kirill harflar lotinga."""
    s = str(r or "").strip().upper().translate(str.maketrans("АБВСДЕ", "ABBCDE"))
    m = re.match(r"^([A-E])\s*[-–:·]?\s*(\d{1,3})?", s)
    if not m:
        return "", esc(r or "")
    return m.group(1), m.group(1) + (f" · {m.group(2)}" if m.group(2) else "")


def render_matrix(nav, flash=""):
    """nav — tepada tanlangan oy (topshirish oyi). Jadval esa bir oy oldingi davr hisobotlari:
    masalan, tepada Oktyabr bo'lsa — Sentyabr hisobotlari (oktyabrda sentyabr uchun topshiriladi)."""
    per = period_shift(nav, -1)
    m = period_matrix(per)
    tot, by = matrix_counts(m)
    pct = round(100 * tot["done"] / tot["need"]) if tot["need"] else None
    opts = "".join(f'<option value="{p}"{" selected" if p == nav else ""}>{period_label(p)}</option>'
                   for p in sorted({period_shift(x, 1) for x in all_periods()}, reverse=True))
    staff_cards = "".join(
        f'<div class="st glass" data-s="{esc(s)}"><div class="top"><span>{esc(s)}</span>'
        f'<span class="mut">{v["done"]}/{v["need"]}</span></div>'
        f'<div class="bar"><i style="width:{round(100 * v["done"] / v["need"]) if v["need"] else 0}%"></i></div>'
        f'<div class="sub">{"qolgan " + str(v["missing"]) if v["missing"] else "hammasi topshirilgan ✓"}'
        f'{" · xato/kech " + str(v["bad"]) if v["bad"] else ""}</div></div>' for s, v in sorted(by.items()) if v["need"])
    head = "".join(f'<th title="{esc(n)}">{esc(x)}</th>' for _k, n, x in TAXES)
    body = []
    for i, r in enumerate(m["rows"], 1):
        c = r["c"]
        tds, miss = [], False
        for k, n, _x in TAXES:
            cell = r["cells"][k]
            cls, inner, info = cell_view(cell)
            miss = miss or cell["st"] in ("missing", "pending", "error")
            tds.append(f'<td class="x {cls}" data-tax="{k}" data-tn="{esc(n)}" data-info="{esc(info)}" '
                       f'title="{esc(info)}">{inner}</td>')
        if m["other"]:
            oth = "; ".join(f'{f["tax"][2:]} — {WORDS.get(f["status"], f["status"])}' for f in r["others"])
            tds.append(f'<td class="ro" style="white-space:normal;min-width:180px">{esc(oth)}</td>')
        staff_td = (f'<td class="pk" data-f="staff" data-v="{r["resp"] or ""}" title="Bosing — hodimni tanlang">'
                    f'{esc(r["staff"])}</td>')
        rp_td = (f'<td class="pk" data-f="regime" data-v="{esc(c.get("regime") or "")}">{esc(c.get("regime") or "")}</td>'
                 f'<td class="ro per">{period_label(per)}</td>')
        g, gtxt = rating_grade(c.get("rating"))
        rt_td = (f'<td class="ed rt g{g}" data-f="rating" title="soliq.uz reytingi — hodim kabinetga kirganda o‘zi yangilanadi, '
                 f'yoki bosib yozing (masalan: C - 34)">{esc(c.get("rating") or "")}</td>')
        key = f'{c["name"] or ""} {c["inn"]} {c.get("activity") or ""} {r["staff"]} {c.get("note") or ""}'.lower()
        gone = c.get("active", 1) not in (1, None)
        body.append(
            f'<tr{" class=gone" if gone else ""} data-k="{esc(key)}" data-s="{esc(r["staff"] or "—")}" '
            f'data-m="{1 if miss else 0}" data-inn="{esc(c["inn"])}" data-name="{esc(c["name"] or c["inn"])}">'
            f'<td class="n">{i}</td><td class="co ed" data-f="name">{esc(c["name"] or "")}</td>'
            f'<td class="ed" data-f="activity">{esc(c.get("activity") or "")}</td><td class="ro">{esc(c["inn"])}</td>{rt_td}'
            f'{staff_td}<td class="ed" data-f="c1">{esc(c.get("c1") or "")}</td>{rp_td}{"".join(tds)}'
            f'<td class="ed" data-f="note" style="min-width:160px">{esc(c.get("note") or "")}</td>'
            f'<td class="del"><form class="hide" method="post" action="/dashboard/company">'
            f'<input type="hidden" name="inn" value="{esc(c["inn"])}"><input type="hidden" name="act" value="hide">'
            f'<input type="hidden" name="p" value="{nav}"><input type="hidden" name="csrf" value="{CSRF}">'
            f'<button title="Karzinaga o‘tkazish" aria-label="Karzinaga">×</button></form></td></tr>')
    # Karzina: olib tashlangan korxonalar — qaytarish yoki butunlay o'chirish
    trash = q("SELECT c.*, (SELECT COUNT(*) FROM reports r WHERE r.inn=c.inn) nrep FROM companies c "
              "WHERE active=0 ORDER BY name")
    trash_rows = "".join(
        f'<tr><td><b>{esc(c["name"] or "(nomsiz)")}</b><div class="sub">{esc(c["inn"])} · hisobotlar: {c["nrep"]}</div></td>'
        f'<td class="ta"><form method="post" action="/dashboard/company"><input type="hidden" name="inn" value="{esc(c["inn"])}">'
        f'<input type="hidden" name="act" value="restore"><input type="hidden" name="p" value="{nav}">'
        f'<input type="hidden" name="csrf" value="{CSRF}"><button class="btn ghost sm">{ic("undo")}Qaytarish</button></form>'
        f'<form class="purge" method="post" action="/dashboard/company"><input type="hidden" name="inn" value="{esc(c["inn"])}">'
        f'<input type="hidden" name="act" value="purge"><input type="hidden" name="p" value="{nav}">'
        f'<input type="hidden" name="csrf" value="{CSRF}"><button class="btn sm danger">{ic("trash")}Butunlay o‘chirish</button></form></td></tr>'
        for c in trash)
    # Bot olgan barcha ma'lumotlar (shu davr)
    reps = period_reports(per)
    log_rows = []
    for r, d in reps:
        tax = tax_of(d.get("Наименование")) or ""
        col = next((x for k, _n, x in TAXES if k == tax), "boshqa")
        lvl = {"ok": "g", "late": "y", "error": "r"}.get(filing_status(d.get("Состояние"), d.get("Причина ошибки")), "y")
        key = f'{r["company"] or ""} {r["inn"]} {r["ename"] or ""} {d.get("Наименование") or ""}'.lower()
        vals = "".join(
            f'<td{" class=w" if f in ("Наименование", "Причина ошибки") else ""}>'
            + (f'<span class="pill {lvl}">{esc(str(d.get(f) or ""))}</span>' if f == "Состояние" else esc(str(d.get(f) or "")))
            + "</td>" for f in LOG_FIELDS)
        log_rows.append(f'<tr data-k="{esc(key)}"><td>{esc(r["ts"][:16].replace("T", " "))}</td>'
                        f'<td class="w"><b>{esc(r["company"] or "")}</b></td><td>{esc(r["inn"])}</td>'
                        f'<td>{esc(r["ename"] or "")}</td><td>{esc(col)}</td>{vals}</tr>')
    log_head = "".join(f"<th class=l>{esc(f)}</th>" for f in LOG_FIELDS)
    fl = f'<div class="flash glass">{iconize(MX_FLASH[flash])}</div>' if flash in MX_FLASH else ""
    staff_opts = "".join(f'<option value="{esc(s)}">{esc(s)}</option>' for s in sorted(by))
    tax_checks = "".join(f'<label><input type="checkbox" name="tax" value="{k}"'
                         f'{" checked" if k == "jshds" else ""}> {esc(x)}</label>' for k, _n, x in TAXES)
    hidden = f'<input type="hidden" name="p" value="{nav}"><input type="hidden" name="csrf" value="{CSRF}">'
    due = period_due(per)
    emps = [(str(e_["tg_id"]), e_["name"]) for e_ in active_employees()]
    picks = {"staff": emps + [("", "— biriktirilmagan")],
             "regime": [(x, x) for x in REGIMES] + [("", "— bo‘sh")],
             "perk": [(x, x) for x in PERKS] + [("", "— bo‘sh")]}
    picks_js = json.dumps(picks).replace("<", "\\u003c")
    emp_opts = "".join(f'<option value="{u}">{esc(n)}</option>' for u, n in emps)
    reg_opts = "".join(f'<option>{esc(x)}</option>' for x in REGIMES)
    perk_opts = "".join(f'<option>{esc(x)}</option>' for x in PERKS)
    return f"""<!DOCTYPE html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Hisobotlar · Gigant Buxgalter</title>{BRAND_HEAD}
<style>{MATRIX_CSS}{BRAND_CSS}thead th,td.co,th.co{{background:var(--solid)!important}}</style></head><body><div class="wrap">
{brand_nav("hisobotlar")}
<header><h1>Hisobotlar jadvali</h1>
<div class="hright">
<div class="pnav glass"><a href="/hisobotlar?p={period_shift(nav, -1)}" title="Oldingi oy">‹</a>
<select id="per" aria-label="Oy">{opts}</select>
<a href="/hisobotlar?p={period_shift(nav, 1)}" title="Keyingi oy">›</a></div>
<a class="btn ghost" href="/hisobotlar.csv?p={nav}">{ic("download")}Excel</a>
<button class="btn" id="addCo">{ic("plus")}Mijoz</button>
<button class="btn ghost" id="trashBtn" title="Olib tashlangan korxonalar">{ic("trash")}Karzina · {len(trash)}</button></div></header>
{fl}
<div class="kpis">
<div class="kpi glass a"><span>Topshirilishi kerak</span><b>{tot['need']}</b></div>
<div class="kpi glass g"><span>Topshirildi</span><b>{tot['done']}{f' <span class="sub">· {pct}%</span>' if pct is not None else ''}</b></div>
<div class="kpi glass {'r' if m['overdue'] else 'o'}"><span>{'Qarz (muddat o‘tgan)' if m['overdue'] else 'Qolgan'}</span><b>{tot['missing']}</b></div>
<div class="kpi glass o"><span>Xato yoki kech</span><b>{tot['bad']}</b></div>
</div>
<p class="sub" style="margin:10px 4px 0">{period_label(nav)} oyida topshiriladigan <b>{period_label(per)}</b> davri hisobotlari ·
muddat taxminan {due:%d.%m.%Y} gacha ·
hodimlar soliq.uz da “Отправить” bosgach katak o‘zi to‘ladi</p>
<h2>Hodimlar bo‘yicha</h2>
<div class="staff">{staff_cards or '<div class="sub">Ma’lumot yo‘q</div>'}</div>
<div class="tabs glass"><button class="tab on" data-t="sheet">Jadval (Excel) · <span id="shown">{len(m['rows'])}</span></button>
<button class="tab" data-t="log">Bot olgan ma’lumotlar · {len(reps)}</button></div>
<div class="tools"><input type="search" id="q" placeholder="Qidirish: nom, INN, faoliyat, hodim, izoh…">
<select id="fstaff"><option value="">Hamma hodimlar</option>{staff_opts}</select>
<label><input type="checkbox" id="fmiss"> Faqat topshirilmaganlar</label></div>
<div id="pane-sheet" class="pane">
<p class="sub" style="margin:0 4px 10px">Matnli katakni bosing va yozing (Enter — saqlash, Esc — bekor).
Soliq katagini bosing — soliq.uz dan kelgan hamma ma’lumot va o‘zgartirish. Yashil — topshirilgan, sariq — tekshiruvda,
to‘q sariq — kech, qizil — xato, qizil ramka — qarz.</p>
<div class="card glass"><table class="sheet"><thead><tr><th class="l">№</th><th class="co l">Наименование организации</th>
<th class="l">Вид деятельности</th><th class="l">ИНН</th><th class="l">Рейтинг</th><th class="l">Ходимлар</th><th class="l">1C</th>
<th class="l">Солиқ режими</th><th class="l">Период</th>{head}
{'<th class="l">Бошқа ҳисоботлар</th>' if m['other'] else ''}<th class="l">Изоҳ</th><th></th></tr></thead>
<tbody>{''.join(body) or '<tr><td colspan="24" class="mut">Korxonalar yo‘q — “+ Mijoz” tugmasini bosing</td></tr>'}</tbody></table></div>
</div>
<div id="pane-log" class="pane" hidden>
<p class="sub" style="margin:0 4px 10px">{period_label(per)} davri uchun hodimlarning soliq.uz kabinetidan bot qabul qilgan
hamma hisobotlar — har bir maydon bilan (oxirgi holati).</p>
<div class="card glass"><table class="log"><thead><tr><th class="l">Bot qabul qildi</th><th class="l">Korxona</th><th class="l">ИНН</th>
<th class="l">Yuborgan hodim</th><th class="l">Jadval ustuni</th>{log_head}</tr></thead>
<tbody>{''.join(log_rows) or '<tr><td colspan="19" class="mut">Bu davr uchun hali hisobot kelmagan</td></tr>'}</tbody></table></div>
<p style="margin-top:10px"><a class="btn ghost" href="/hisobotlar.csv?p={nav}&amp;t=log">{ic("download")}Bu ro‘yxatni Excel’ga</a></p>
</div>
</div>
<div id="menu" hidden><div class="mh"></div><div class="info"></div>
<div class="opts"><button data-v="ok" class="ok">топширилган</button><button data-v="na">йук</button>
<button data-v="late" class="late">кечикиб топширилган</button><button data-v="wait" class="wait">текширувда</button>
<button data-v="error" class="error">хато</button><button data-v="clear">↺ avtomatik / bo‘sh</button></div>
<form><input placeholder="Boshqa matn yozing…" maxlength="120"><button>OK</button></form>
<div class="sep">Bu korxona bu soliqni</div>
<div class="opts"><button data-v="obl:req_m">har oy topshiradi</button><button data-v="obl:req_q">har chorakda</button>
<button data-v="obl:never">umuman topshirmaydi</button></div></div>
<div id="pick" hidden><div class="mh"></div><div class="opts"></div>
<form><input placeholder="Boshqa matn yozing…" maxlength="40"><button>OK</button></form></div>
<dialog id="dTrash" class="wide"><h3>Karzina</h3>
<p class="sub" style="margin:0 0 12px">Bu korxonalar jadvalda ko‘rinmaydi va ular haqida botga xabar kelmaydi.
“Qaytarish” — ro‘yxatga qaytadi; “Butunlay o‘chirish” — hamma ma’lumoti bilan o‘chadi.</p>
<div class="trash"><table>{trash_rows or '<tr><td class="mut">Karzina bo‘sh</td></tr>'}</table></div>
<div class="acts"><button type="button" class="btn ghost full" data-close>Yopish</button></div></dialog>
<dialog id="dAdd"><h3>Yangi mijoz</h3>
<form method="post" action="/dashboard/company" style="margin-top:14px">{hidden}<input type="hidden" name="act" value="add">
<label>INN<input type="text" name="inn" required pattern="[0-9]{{9}}|[0-9]{{14}}" inputmode="numeric" placeholder="309497371"></label>
<label>Korxona nomi<input type="text" name="name" required maxlength="120" placeholder="UNI-TAX CONSULT"></label>
<label>Faoliyat turi<input type="text" name="activity" maxlength="80" placeholder="Улгурчи савдо"></label>
<label>Hodim<select name="uid"><option value="">— biriktirilmagan</option>{emp_opts}</select></label>
<label>Солиқ режими<input type="text" name="regime" maxlength="40" list="dlReg" placeholder="Айланма"></label>
<datalist id="dlReg">{reg_opts}</datalist>
<div class="sub">Har oy topshiradigan hisobotlari:</div><div class="taxpick">{tax_checks}</div>
<div class="acts"><button type="button" class="btn ghost" data-close>Bekor</button><button class="btn">Qo‘shish</button></div>
</form></dialog>
<script>const PERIOD="{per}",NAV="{nav}",PERIOD_LABEL="{period_label(per)}",CSRF="{CSRF}",PICKS={picks_js};{MATRIX_JS}</script></body></html>"""


def _csv_line(vals):
    out = []
    for v in vals:
        v = str(v if v is not None else "")
        out.append('"' + v.replace('"', '""') + '"' if any(ch in v for ch in ';"\n\r') else v)
    return ";".join(out)


def matrix_csv(nav, kind=""):
    """Jadval (yoki bot olgan hisobotlar ro'yxati) — Excel ochadigan CSV (UTF-8 BOM, ';'). nav — topshirish oyi."""
    per = period_shift(nav, -1)
    if kind == "log":
        lines = [_csv_line(["Bot qabul qildi", "Korxona", "ИНН", "Yuborgan hodim", "Jadval ustuni"] + LOG_FIELDS)]
        for r, d in period_reports(per):
            tax = tax_of(d.get("Наименование")) or ""
            col = next((x for k, _n, x in TAXES if k == tax), "boshqa")
            lines.append(_csv_line([r["ts"][:16].replace("T", " "), r["company"], r["inn"], r["ename"], col]
                                   + [d.get(f) for f in LOG_FIELDS]))
        return "﻿" + "\r\n".join(lines) + "\r\n"
    m = period_matrix(per)
    lines = [_csv_line(["№", "Наименование организации", "Вид деятельности", "ИНН", "Рейтинг", "Ходимлар", "1C",
                        "Солиқ режими", "Период"]
                       + [x for _k, _n, x in TAXES] + ["Изоҳ"])]
    for i, r in enumerate(m["rows"], 1):
        c = r["c"]
        vals = []
        for k, _n, _x in TAXES:
            ce = r["cells"][k]
            vals.append(ce.get("detail") if ce["st"] == "note" else ("йук" if ce.get("never") else WORDS.get(ce["st"], "")))
        lines.append(_csv_line([i, c["name"], c.get("activity"), c["inn"], c.get("rating"), r["staff"], c.get("c1"),
                                c.get("regime"), period_label(per)] + vals + [c.get("note")]))
    return "﻿" + "\r\n".join(lines) + "\r\n"


def matrix_api(f):
    """Jadvalda joyida tahrirlash (fetch). -> JSON javob."""
    inn = "".join(ch for ch in f.get("inn", "") if ch.isdigit())
    per = f.get("p", "")
    if len(inn) not in (9, 14) or not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", per):
        return {"ok": False, "error": "Noto'g'ri so'rov — sahifani yangilang"}
    kind, v = f.get("kind", ""), f.get("v", "")
    if kind == "staff":
        # Hodim tanlandi: shu davr boshidan (oyning 1-kunidan) korxona unga biriktiriladi, tarix saqlanadi.
        uid = int(v) if v.isdigit() else None
        if uid is not None and not emp(uid):
            return {"ok": False, "error": "Bunday faol hodim yo'q — sahifani yangilang"}
        set_owner(inn, uid, per + "-01")
        q("UPDATE companies SET staff=? WHERE inn=?", (emp(uid)["name"] if uid else "", inn))
        return {"ok": True, "reload": True}
    if kind == "co":
        field = f.get("field", "")
        if field not in CO_FIELDS:
            return {"ok": False, "error": "Bu ustunni o'zgartirib bo'lmaydi"}
        val = (v.strip() if field == "note" else " ".join(v.split()))[:CO_FIELDS[field]]
        if field == "name" and not val:
            return {"ok": False, "error": "Korxona nomi bo'sh bo'lmasin"}
        grade = ""
        if field == "rating" and val:      # "c-34" / "С 34" -> "C - 34" (rangi harfdan)
            grade, _t = rating_grade(val)
            num = re.search(r"\d{1,3}", val)
            if not grade:
                return {"ok": False, "error": "Reyting A–E harfi bilan boshlansin, masalan: C - 34"}
            val = f"{grade} - {num.group(0)}" if num else grade
        if not q("SELECT 1 FROM companies WHERE inn=?", (inn,), one=True):
            q("INSERT INTO companies(inn,name,tg_id) VALUES(?,'',NULL)", (inn,))
        q(f"UPDATE companies SET {field}=? WHERE inn=?", (val, inn))
        return {"ok": True, "text": val, "grade": grade}
    tax = f.get("tax", "")
    if tax not in TAX_NAME:
        return {"ok": False, "error": "Soliq turi noto'g'ri"}
    if kind == "obl":
        if v not in ("req_m", "req_q", "never"):
            return {"ok": False, "error": "Noto'g'ri tanlov"}
        q("INSERT OR REPLACE INTO obligations(inn,tax,required,freq,source) VALUES(?,?,?,?,'manual')",
          (inn, tax, 0 if v == "never" else 1, "q" if v == "req_q" else "m"))
        return {"ok": True, "reload": True}
    if kind != "cell":
        return {"ok": False, "error": "Noto'g'ri so'rov"}
    ts = now().isoformat()
    text = " ".join(v[2:].split())[:120] if v.startswith("t:") else ""
    if v in WORDS and v not in ("missing", "pending"):
        q("INSERT OR REPLACE INTO filings(inn,period,tax,status,source,report_id,detail,ts) "
          "VALUES(?,?,?,?,'manual',NULL,NULL,?)", (inn, per, tax, v, ts))
    elif v == "clear" or (v.startswith("t:") and not text):
        q("DELETE FROM filings WHERE inn=? AND period=? AND tax=? AND source!='soliq'", (inn, per, tax))
        backfill_filings(inn)   # qo'lda belgi olib tashlansa — soliq.uz dagi holat qaytadi
    elif text:
        st = text_status(text)
        q("INSERT OR REPLACE INTO filings(inn,period,tax,status,source,report_id,detail,ts) "
          "VALUES(?,?,?,?,'manual',NULL,?,?)", (inn, per, tax, st, text if st == "note" else None, ts))
    else:
        return {"ok": False, "error": "Noto'g'ri tanlov"}
    cls, inner, info = cell_view(one_cell(inn, per, tax))
    return {"ok": True, "cls": cls, "html": inner, "info": info}


def matrix_post(path, f, fl):
    """Hisobotlar jadvali formalari (yangi mijoz, olib tashlash). -> flash kodi."""
    inn = "".join(ch for ch in f.get("inn", "") if ch.isdigit())
    if len(inn) not in (9, 14):
        return "bad_inn"
    act = f.get("act", "")
    clip = lambda k, n: " ".join(f.get(k, "").split())[:n]
    if act == "hide":
        q("UPDATE companies SET active=0 WHERE inn=?", (inn,))
        return "co_hidden"
    if act == "restore":
        activate_company(inn)
        return "co_restored"
    if act == "purge":
        return "co_purged" if purge_company(inn) else "stale"
    if act == "add":
        uid = int(f["uid"]) if f.get("uid", "").isdigit() else None
        e_ = emp(uid) if uid else None
        q("INSERT INTO companies(inn,name,tg_id,activity,staff,regime,perk,active,sort) VALUES(?,?,NULL,?,?,?,?,1,"
          "(SELECT COALESCE(MAX(sort),0)+1 FROM companies)) ON CONFLICT(inn) DO UPDATE SET active=1, "
          "name=COALESCE(NULLIF(excluded.name,''),companies.name), activity=excluded.activity, staff=excluded.staff, "
          "regime=excluded.regime, perk=excluded.perk",
          (inn, clip("name", 120), clip("activity", 80), e_["name"] if e_ else "", clip("regime", 40), clip("perk", 40)))
        if e_:
            per = f.get("p", "")
            # p — tepadagi (topshirish) oyi; biriktirish jadvaldagi davr boshidan (bir oy oldin)
            day = (period_shift(per, -1) + "-01" if re.match(r"^\d{4}-(0[1-9]|1[0-2])$", per)
                   else now().date().isoformat())
            set_owner(inn, uid, day)
        for t in fl.get("tax", []):
            if t in TAX_NAME:
                q("INSERT OR REPLACE INTO obligations(inn,tax,required,freq,source) VALUES(?,?,1,'m','manual')", (inn, t))
        backfill_filings(inn)   # avval kelgan hisobotlari bo'lsa — jadvalga tushadi
        apply_saved_rating(inn)
        return "co_add"
    return "stale"


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _out(self, code, body, ctype="application/json; charset=utf-8", extra=None):
        data = body if isinstance(body, bytes) else body.encode() if isinstance(body, str) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        if ctype.startswith("text/html"):
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _ip(self):
        return self.headers.get("CF-Connecting-IP") or self.client_address[0]

    def _cookie(self, name=SESSION_COOKIE):
        c = http.cookies.SimpleCookie()
        try:
            c.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        m = c.get(name)
        return m.value if m else ""

    def _session(self, touch=False):
        """Joriy sessiya tugash vaqti (unix) yoki None. Bloklangan IP dan — hech qachon."""
        if self._ip() in blocked_ips():
            session_end(self._cookie())
            return None
        return session_valid(self._cookie(), touch)

    def _go(self, location, cookie=None):
        self.send_response(303)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        for ck in ([cookie] if isinstance(cookie, str) else (cookie or [])):
            self.send_header("Set-Cookie", ck)
        self.end_headers()

    def _redirect(self, flash):
        self._go("/dashboard?m=" + flash)

    def _login_post(self, raw):
        ip, t = self._ip(), time.time()
        if ip in blocked_ips():
            log("LOGIN BLOKLANGAN IP", ip)
            return self._go("/login?e=denied")
        fails = [x for x in LOGIN_FAILS.get(ip, []) if x > t - FAIL_WINDOW]
        # Umumiy chegara ham: har xil IP lardan parol tanlansa (IP almashtirib) — 1 soatda 30 ta xatodan keyin hamma uchun yopiq
        glob = [x for x in LOGIN_FAILS.get("*", []) if x > t - 3600]
        LOGIN_FAILS["*"] = glob
        if len(fails) >= MAX_FAILS or len(glob) >= 30:
            LOGIN_FAILS[ip] = fails
            return self._go("/login?e=blocked")
        f = {k: v[0] for k, v in urllib.parse.parse_qs(raw.decode("utf-8", "replace")).items()}
        ok = bool(DASH_PASSWORD) and secrets.compare_digest(f.get("password", ""), DASH_PASSWORD) and (
            not DASH_USER or secrets.compare_digest(f.get("username", "").strip(), DASH_USER))
        if not ok:
            fails.append(t)
            LOGIN_FAILS[ip] = fails
            glob.append(t)
            log("LOGIN XATO", ip)
            if len(glob) in (5, 15, 30):
                send(OWNER, f"⚠️ Dashboardga {len(glob)} marta noto'g'ri parol bilan kirishga urinildi (oxirgi 1 soat, IP {esc(ip)})."
                            + (" Kirish 1 soatga yopildi." if len(glob) >= 30 else ""))
            return self._go("/login?e=bad")
        LOGIN_FAILS.pop(ip, None)
        token = session_new()
        log("LOGIN", ip)
        # Har muvaffaqiyatli kirish haqida rahbarga xabar — begona odam kirsa darhol bilinadi
        ua = self.headers.get("User-Agent", "")
        dev = next((n for k, n in (("iPhone", "iPhone"), ("iPad", "iPad"), ("Android", "Android"), ("Windows", "Windows"),
                                   ("Mac OS", "Mac"), ("Linux", "Linux")) if k in ua), "noma'lum qurilma")
        brw = next((n for k, n in (("Edg/", "Edge"), ("YaBrowser", "Yandex"), ("OPR/", "Opera"), ("Chrome/", "Chrome"),
                                   ("Firefox/", "Firefox"), ("Safari/", "Safari")) if k in ua), "")
        # Qurilma belgisi (2 yillik cookie): "✅ Bu men" bosilgan qurilmadan kirilganda xabar kelmaydi
        devid = self._cookie(DEVICE_COOKIE)
        if not re.fullmatch(r"[A-Za-z0-9_-]{20,64}", devid or ""):
            devid = secrets.token_urlsafe(24)
        label = f"{dev}{(' · ' + brw) if brw else ''}"
        trusted = trusted_devices()
        if devid in trusted:
            trusted[devid]["last"] = now().strftime("%d.%m.%Y %H:%M")
            save_trusted(trusted)
        else:
            lid = secrets.token_hex(4)
            LOGINS[lid] = {"token": token, "ip": ip, "dev": devid, "label": label}
            for k in list(LOGINS)[:-50]:          # faqat oxirgi 50 ta kirish eslab qolinadi
                LOGINS.pop(k, None)
            send_kb(OWNER, f"🔐 <b>Yangi qurilmadan kirildi</b>: {now():%d.%m %H:%M} · {esc(label)} · IP {esc(ip)}\n"
                           f"Bu siz bo'lsangiz — <b>✅ Bu men</b> ni bosing, shu qurilmadan boshqa xabar kelmaydi.",
                    [[("🚫 Chiqarib yuborish va bloklash", f"lkick:{lid}")],
                     [("✅ Bu men", f"lok:{lid}"), ("🚪 Hammani chiqarish", "lall")]])
        # Sessiya cookie: Max-Age yo'q — brauzer yopilsa o'chadi; muddatni server nazorat qiladi
        self._go("/dashboard", [f"{SESSION_COOKIE}={token}; Path=/; HttpOnly; Secure; SameSite=Strict",
                                f"{DEVICE_COOKIE}={devid}; Path=/; Max-Age=63072000; HttpOnly; Secure; SameSite=Strict"])

    def _dashboard_post(self, raw):
        path = self.path.split("?")[0]
        is_api = path == "/dashboard/api/edit" or (path == "/dashboard/pc" and b"ajax=1" in raw)
        if not self._session(touch=True):
            if is_api:
                return self._out(401, {"ok": False, "error": "Sessiya tugagan — sahifani yangilab, qayta kiring"})
            return self._go("/login?e=expired")
        fl = urllib.parse.parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
        f = {k: v[0] for k, v in fl.items()}
        log("DASHBOARD", path, {k: v for k, v in f.items() if k != "csrf"})
        if not secrets.compare_digest(f.get("csrf", ""), CSRF):
            if is_api:
                return self._out(403, {"ok": False, "error": "Sahifa eskirgan — yangilang (F5)"})
            return self._redirect("stale")
        if path == "/dashboard/pc":
            if f.get("ajax") == "1":
                return self._out(200, st.dashboard_json(f))
            return self._go(st.dashboard_post(f))
        if is_api:
            try:
                return self._out(200, matrix_api(f))
            except Exception:
                log("API EDIT ERROR", traceback.format_exc())
                return self._out(500, {"ok": False, "error": "Server xatosi — qayta urining"})
        if path == "/dashboard/company":
            per = f.get("p", "")
            per = per if re.match(r"^\d{4}-(0[1-9]|1[0-2])$", per) else f"{now():%Y-%m}"
            return self._go(f"/hisobotlar?p={per}&m={matrix_post(path, f, fl)}")
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
            limit = (13_000_000 if self.path == "/api/shot" else 3_000_000 if self.path == "/api/frame"
                     else 2_000_000 if self.path == "/api/activity" else 200_000)
            if n > limit:
                return self._out(413, {"error": "too large"})
            raw = self.rfile.read(n)
        except Exception:
            return self._out(400, {"error": "bad request"})
        if self.path == "/api/frame":   # jonli ekran kadri (agentdan, JPEG) — logga yozilmaydi
            try:
                code, res = st.api_frame(self.headers, raw)
            except Exception:
                log("FRAME ERROR", traceback.format_exc())
                code, res = 400, {"error": "bad request"}
            return self._out(code, res)
        if self.path in ("/api/activity", "/api/shot", "/api/wait", "/api/presence", "/api/web"):
            # Hodim kompyuteridagi agent (har 30 soniyada + doimiy kutish so'rovi) — logga yozilmaydi
            try:
                body = json.loads(raw or b"{}")
                fn = {"/api/activity": lambda: st.api_activity(body, self._ip()), "/api/shot": lambda: st.api_shot(body),
                      "/api/wait": lambda: st.api_wait(body, self._ip()),
                      "/api/presence": lambda: st.api_presence(body),
                      "/api/web": lambda: st.api_web(body)}[self.path]
                code, res = fn()
            except Exception:
                log("AGENT API ERROR", traceback.format_exc())
                code, res = 400, {"error": "bad request"}
            return self._out(code, res)
        if self.path.split("?")[0] == "/login":
            return self._login_post(raw)
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
        if self.path == "/api/rating":
            # soliq.uz kabinetidagi "Рейтинг: C - 34" (hodimlar skripti o'zi topib yuboradi)
            if not e:
                return self._out(401, {"error": "bad key"})
            inn = "".join(ch for ch in str(body.get("inn") or "") if ch.isdigit())
            g, _t = rating_grade(body.get("rating"))
            num = re.search(r"\d{1,3}", str(body.get("rating") or ""))
            if len(inn) in (9, 14) and g:
                rv = f"{g} - {num.group(0)}" if num else g
                q("UPDATE companies SET rating=?, rating_ts=? WHERE inn=?", (rv, now().isoformat(), inn))
                # Korxona hali ro'yxatda bo'lmasa ham eslab qolamiz — qo'shilganda jadvalga o'zi tushadi
                q("INSERT OR REPLACE INTO meta(k,v) VALUES(?,?)", (f"rating:{inn}", rv))
            pr = body.get("probe")
            if isinstance(pr, dict):               # reyting API sinovi natijasi (skriptdan)
                log("RATING PROBE", json.dumps({k: str(v)[:1500] for k, v in pr.items()}, ensure_ascii=False))
            eps = body.get("endpoints")
            if isinstance(eps, list) and eps:      # yangi kabinet API manzillari (reytingni avtomatlashtirish uchun)
                log("RATING ENDPOINTS", json.dumps([str(x)[:150] for x in eps[:60]], ensure_ascii=False))
            return self._out(200, {"ok": True})
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
            js = re.sub(r"const DISCOVERY_URL\s*=\s*'[^']*'", f"const DISCOVERY_URL = '{DISC_URL}'", js, count=1)
            js = re.sub(r"const DISCOVERY_KEY\s*=\s*'[^']*'", f"const DISCOVERY_KEY = '{DISC_SECRET}'", js, count=1)
            # Tampermonkey shu manzildan yangilanishlarni o'zi tekshirsin
            js = js.replace("// ==/UserScript==", f"// @updateURL    {url}/skript.user.js\n"
                                                   f"// @downloadURL  {url}/skript.user.js\n// ==/UserScript==", 1)
            # no-store: Cloudflare va brauzer keshlamasin — yangi versiya darhol tarqalsin
            return self._out(200, js, "text/javascript; charset=utf-8", {"Cache-Control": "no-store, max-age=0"})
        path = self.path.split("?")[0]
        qs = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if path == "/login":
            if self._session():
                return self._go("/dashboard")
            return self._out(200, render_login(qs.get("e", [""])[0]), "text/html; charset=utf-8")
        if path == "/logout":
            session_end(self._cookie())
            return self._go("/login?e=out", f"{SESSION_COOKIE}=; Path=/; Max-Age=0; HttpOnly; Secure; SameSite=Strict")
        if path in ("/brand/logo.png", "/brand/logo-w.png"):
            try:
                with open(os.path.join(BASE, "static", path.rsplit("/", 1)[1]), "rb") as f:
                    return self._out(200, f.read(), "image/png", {"Cache-Control": "public, max-age=604800"})
            except OSError:
                return self._out(404, "not found", "text/plain")
        if path.startswith("/agent/"):
            # Agent o'rnatish fayllari — maxfiy narsa yo'q (hodim kaliti o'rnatishda so'raladi)
            got = st.agent_file(path[len("/agent/"):])
            if not got:
                return self._out(404, "not found", "text/plain")
            return self._out(200, got[0], got[1], {"Cache-Control": "no-store"})
        if path not in ("/", "/dashboard", "/hisobotlar", "/hisobotlar.csv", "/screentime", "/screentime/shot",
                        "/screentime/live.json", "/screentime/detail.json", "/screentime/frame",
                        "/screentime/hodim", "/screentime/wb.json", "/screentime/report.json", "/screentime/report.csv"):
            return self._out(404, "not found", "text/plain")
        # Avto-yangilanish (har daqiqa) faollik hisoblanmaydi — faqat foydalanuvchi sahifada nimadir qilgan bo'lsa.
        auto = qs.get("auto", [""])[0] == "1"
        active = qs.get("active", [""])[0] == "1"
        exp = self._session(touch=(not auto) or active)
        if not exp:
            return self._go("/login?e=expired" if self._cookie() else "/login")
        if path == "/screentime/live.json":
            return self._out(200, st.live_data(), extra={"Cache-Control": "no-store"})
        if path in ("/screentime/hodim", "/screentime/wb.json"):
            u = qs.get("u", [""])[0]
            day = parse_day(qs.get("d", [""])[0] or "") or now().date().isoformat()
            if not u.isdigit() or not emp(int(u), active_only=False):
                return self._out(404, "hodim topilmadi", "text/plain")
            if path.endswith(".json"):
                return self._out(200, st.wb_data(int(u), day), extra={"Cache-Control": "no-store"})
            return self._out(200, st.render_wb(int(u), day), "text/html; charset=utf-8")
        if path in ("/screentime/report.json", "/screentime/report.csv"):
            kind = "month" if qs.get("p", [""])[0] == "month" else "week"
            day = parse_day(qs.get("d", [""])[0] or "") or now().date().isoformat()
            if path.endswith(".json"):
                r = st.period_report(kind, day)
                r.pop("daily", None)
                return self._out(200, r, extra={"Cache-Control": "no-store"})
            u = qs.get("u", [""])[0]
            if u.isdigit() and emp(int(u), active_only=False):
                body, fname = st.report_emp_csv(kind, day, int(u))     # bitta hodim samaradorligi
            else:
                body, fname = st.report_csv(kind, day, detail=qs.get("t", [""])[0] == "daily")
            fname = urllib.parse.quote(fname)
            return self._out(200, body, "text/csv; charset=utf-8",
                             {"Content-Disposition": f"attachment; filename*=UTF-8''{fname}", "Cache-Control": "no-store"})
        if path == "/screentime/frame":
            pid, after = qs.get("id", [""])[0], qs.get("after", ["0"])[0]
            if not pid.isdigit():
                return self._out(400, "bad id", "text/plain")
            got = st.frame_get(int(pid), int(after) if after.isdigit() else 0, hq=qs.get("wall", [""])[0] != "1")
            if not got:
                self.send_response(204)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            return self._out(200, got[1], "image/jpeg", {"Cache-Control": "no-store", "X-Seq": str(got[0])})
        if path == "/screentime/detail.json":
            tg_s, day = qs.get("tg", [""])[0], parse_day(qs.get("d", [""])[0] or "") or now().date().isoformat()
            if not tg_s.isdigit():
                return self._out(400, {"error": "bad tg"})
            return self._out(200, st.detail_data(int(tg_s), day), extra={"Cache-Control": "no-store"})
        if path == "/screentime/shot":
            pid = qs.get("id", [""])[0]
            png = st.shot_png(int(pid)) if pid.isdigit() else None
            if not png:
                return self._out(404, "not found", "text/plain")
            return self._out(200, png, "image/png", {"Cache-Control": "no-store"})
        if path == "/screentime":
            day = qs.get("d", [""])[0]
            day = parse_day(day) if day else None
            return self._out(200, st.render_page(day, qs.get("m", [""])[0]), "text/html; charset=utf-8")
        if path in ("/hisobotlar", "/hisobotlar.csv"):
            per = qs.get("p", [""])[0]   # tepadagi oy (topshirish oyi); standart — joriy oy
            per = per if re.match(r"^\d{4}-(0[1-9]|1[0-2])$", per) else f"{now():%Y-%m}"
            if path.endswith(".csv"):
                kind = "log" if qs.get("t", [""])[0] == "log" else ""
                return self._out(200, matrix_csv(per, kind), "text/csv; charset=utf-8",
                                 {"Content-Disposition": f'attachment; filename="hisobotlar-{kind or "jadval"}-{per}.csv"',
                                  "Cache-Control": "no-store"})
            return self._out(200, render_matrix(per, qs.get("m", [""])[0]), "text/html; charset=utf-8")
        self._out(200, render_dashboard(qs.get("m", [""])[0]), "text/html; charset=utf-8")


class DiscoveryHandler(BaseHTTPRequestHandler):
    """Doimiy IP dagi yagona sahifa: GET /u -> {"url": joriy tunnel manzili, "sig": HMAC}. Boshqa hech narsa yo'q."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path.split("?")[0] != "/u":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        url = current_url()
        sig = __import__("hmac").new(DISC_SECRET.encode(), url.encode(), "sha256").hexdigest()
        data = json.dumps({"url": url, "sig": sig}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main():
    if DISC_PORT:
        dsrv = ThreadingHTTPServer(("0.0.0.0", DISC_PORT), DiscoveryHandler)
        threading.Thread(target=dsrv.serve_forever, daemon=True).start()
    if not DASH_PASSWORD:
        print("⚠️  config.json: DASHBOARD_PASSWORD bo'sh — dashboard ochilmaydi.")
    st.init(globals())
    backfill_filings()
    # Serverda "BIND": "127.0.0.1" — dashboardga faqat tunnel orqali kiriladi
    server = ThreadingHTTPServer((CFG.get("BIND", "0.0.0.0"), PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=scheduler, daemon=True).start()
    print(f"Bot ishga tushdi. Dashboard: {PUBLIC_URL}/dashboard")
    send(OWNER, "🤖 Bot ishga tushdi. /help — buyruqlar")
    polling()


if __name__ == "__main__":
    main()
