"""Screen time + kompyuterlarni boshqarish moduli (hodim bot uchun).

Hodim kompyuteridagi agent (agent/gigant_agent.py) har 30 soniyada /api/activity ga
qaysi dastur qancha ishlatilganini yuboradi; javobda navbatdagi buyruqlarni oladi.
bot.py: `import screentime as st` va `st.init(globals())`.
"""
import base64
import json
import os
import re
import threading
import time
import traceback
import urllib.request
import uuid
from datetime import datetime, timedelta

_NAMES = ("q", "db", "LOCK", "send", "send_kb", "tg", "now", "esc", "log", "OWNER", "CFG", "TOKEN", "TZ",
          "emp", "emp_by_key", "emp_name", "active_employees", "is_workday", "at", "WORK_START", "WORK_END",
          "PENDING", "CSRF", "current_url", "parse_day", "once", "BASE", "DISC_URL", "DISC_SECRET",
          "BRAND_HEAD", "BRAND_CSS", "brand_nav", "ic", "iconize", "IC_JS", "responsible", "sees_all", "role_of", "ROLES", "team_ids")

ONLINE_SEC = 120          # shuncha soniya signal bo'lmasa — oflayn
CMD_TTL = 600             # oflayn kompyuterga buyruq 10 daqiqa kutadi, keyin bekor bo'ladi
SHOTS = {}                # pc_id -> (unix vaqt, png bytes) — oxirgi skrinshot (xotirada)
EVENTS = {}               # pc_id -> threading.Event: agentning kutish so'rovini darhol uyg'otish uchun
WAIT_SEC = 25             # agentning kutish so'rovi shuncha soniya ushlab turiladi
DEFAULT_BLOCK_MSG = "Rahbar tomonidan bloklandi."
CMD_LABEL = {"lock": "🔑 Windows qulfi","shutdown": "⏻ O'chirish", "restart": "🔄 Qayta yoqish",
             "logoff": "🚪 Tizimdan chiqarish", "msg": "💬 Xabar", "shot": "📸 Skrinshot",
             "cancel": "✋ O'chirishni bekor qilish"}
CMD_DONE = {"lock": "bloklandi", "shutdown": "60 soniyada o'chadi", "restart": "60 soniyada qayta yoqiladi",
            "logoff": "tizimdan chiqarildi", "msg": "xabar ko'rsatildi", "cancel": "o'chirish bekor qilindi"}

APP_NAMES = {"chrome.exe": "Google Chrome", "msedge.exe": "Microsoft Edge", "firefox.exe": "Firefox",
             "opera.exe": "Opera", "browser.exe": "Yandex Browser", "brave.exe": "Brave", "vivaldi.exe": "Vivaldi",
             "excel.exe": "Excel", "winword.exe": "Word", "powerpnt.exe": "PowerPoint", "outlook.exe": "Outlook",
             "1cv8.exe": "1C:Предприятие", "1cv8c.exe": "1C:Предприятие", "1cv8s.exe": "1C:Предприятие",
             "1cestart.exe": "1C (zapusk)", "telegram.exe": "Telegram", "whatsapp.exe": "WhatsApp",
             "explorer.exe": "Provodnik (fayllar)", "acrord32.exe": "Adobe Reader", "acrobat.exe": "Adobe Acrobat",
             "notepad.exe": "Bloknot", "calculatorapp.exe": "Kalkulyator", "anydesk.exe": "AnyDesk",
             "teamviewer.exe": "TeamViewer", "javaw.exe": "Java (E-IMZO va b.)", "locked": "Ekran qulflangan",
             "blocked": "Rahbar bloklagan", "desktop": "Ish stoli"}
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "browser.exe", "brave.exe", "vivaldi.exe"}
TITLE_SUFFIX = re.compile(r"\s*(?:[-—–|]\s*)?(?:Google Chrome|Microsoft​? Edge|Mozilla Firefox|Opera|"
                          r"Yandex(?:\.Browser| Browser)?|Яндекс(?: Браузер)?|Brave|Vivaldi)\s*$", re.I)
EDGE_PROFILE = re.compile(r"\s+(?:and \d+ more pages?|и ещё \d+ стр\w*)?\s*[-—–]\s*(?:Profile|Профиль|Personal|Work)\b.*$", re.I)

DEFAULT_PRODUCTIVE = [
    "excel", "winword", "1cv8", "1cestart", "1с:", "1c:", "powerpnt", "outlook", "acrord32", "acrobat",
    "soliq.uz", "my.soliq", "soliq", "didox", "faktura", "my.gov.uz", "lex.uz", "stat.uz", "e-imzo", "eimzo",
    "ibank", "bank", "buxgalter", "бухгалт", "hisobot", "отчет", "отчёт", "декларац", "calculator", "kalkulyator",
    "kapital", "anor", "e-invoice", "1c-", "mehnat.uz", "my.mehnat", "fiskal", "ofd", "pdf"]
DEFAULT_UNPRODUCTIVE = [
    "youtube", "instagram", "tiktok", "facebook", "netflix", "kinopoisk", "kino", "фильм", "сериал", "film ",
    "serial", "steam", "epicgames", "game", "игра", "o'yin", "spotify", "music", "музык", "vk.com", "вконтакте",
    "ok.ru", "одноклассники", "twitch", "pinterest", "aliexpress", "uzum market", "olx", "wildberries",
    "ozon", "solitaire", "minesweeper", "pubg", "dota", "counter-strike", "roblox", "minecraft", "футбол", "match"]


def init(ns):
    g = globals()
    for k in _NAMES:
        g[k] = ns[k]
    global PROD, UNPROD
    PROD = [s.lower() for s in CFG.get("ST_PRODUCTIVE", DEFAULT_PRODUCTIVE)]
    UNPROD = [s.lower() for s in CFG.get("ST_UNPRODUCTIVE", DEFAULT_UNPRODUCTIVE)]
    with LOCK:
        db.executescript("""
CREATE TABLE IF NOT EXISTS st_usage(
  tg_id INTEGER, day TEXT, app TEXT, title TEXT, secs INTEGER DEFAULT 0, active INTEGER DEFAULT 0,
  PRIMARY KEY(tg_id, day, app, title));
CREATE TABLE IF NOT EXISTS st_minutes(
  tg_id INTEGER, day TEXT, hm TEXT, secs INTEGER DEFAULT 0, active INTEGER DEFAULT 0,
  app TEXT, cat TEXT, app_secs INTEGER DEFAULT 0, PRIMARY KEY(tg_id, day, hm));
CREATE TABLE IF NOT EXISTS st_pcs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, pc TEXT, winuser TEXT, tg_id INTEGER, last_seen REAL,
  now TEXT, ver TEXT, ip TEXT, first_seen REAL, UNIQUE(pc, winuser));
CREATE TABLE IF NOT EXISTS st_cmds(
  id INTEGER PRIMARY KEY AUTOINCREMENT, pc_id INTEGER, cmd TEXT, arg TEXT, created REAL,
  sent REAL, done REAL, notify INTEGER DEFAULT 0);
CREATE INDEX IF NOT EXISTS ix_st_cmds_pc ON st_cmds(pc_id, sent);
-- Rahbar dashboardda belgilagan turlar: k — dastur (excel.exe) yoki sayt nomidagi so'z; cat p/n/u
CREATE TABLE IF NOT EXISTS st_rules(k TEXT PRIMARY KEY, cat TEXT, created REAL);
-- soliq.uz kabinetida qaysi korxona (INN), qaysi sahifada qancha vaqt (hodimlar skriptidan)
CREATE TABLE IF NOT EXISTS st_company(tg_id INTEGER, day TEXT, inn TEXT, company TEXT, page TEXT,
  secs INTEGER DEFAULT 0, PRIMARY KEY(tg_id, day, inn, page));
-- Brauzerda har bir sayt (domen) va sahifa uchun aniq vaqt (hodimlar skriptidan, faqat faol tab)
-- Faoliyat jurnali (daqiqalik): qaysi dastur/oyna, soat nechada — "nechadan nechagacha" uchun (90 kun saqlanadi)
CREATE TABLE IF NOT EXISTS st_log(tg_id INTEGER, day TEXT, hm TEXT, app TEXT, title TEXT, secs INTEGER DEFAULT 0,
  active INTEGER DEFAULT 0, PRIMARY KEY(tg_id, day, hm, app, title));
-- Brauzer: soat nechada qaysi sahifa (havola — so'rov parametrlarisiz) va soliq kabinetida qaysi korxona
CREATE TABLE IF NOT EXISTS st_weblog(tg_id INTEGER, day TEXT, hm TEXT, domain TEXT, title TEXT, url TEXT, inn TEXT,
  secs INTEGER DEFAULT 0, PRIMARY KEY(tg_id, day, hm, domain, title));
CREATE TABLE IF NOT EXISTS st_web(tg_id INTEGER, day TEXT, domain TEXT, title TEXT, secs INTEGER DEFAULT 0,
  PRIMARY KEY(tg_id, day, domain, title));
""")
        if "url" not in {r[1] for r in db.execute("PRAGMA table_info(st_log)")}:
            db.execute("ALTER TABLE st_log ADD COLUMN url TEXT")    # agent 1.8+: brauzer manzil satridan havola
        cols = {r[1] for r in db.execute("PRAGMA table_info(st_pcs)")}
        for col, typ in (("blocked", "INTEGER DEFAULT 0"), ("block_msg", "TEXT"), ("block_notify", "INTEGER DEFAULT 0")):
            if col not in cols:
                db.execute(f"ALTER TABLE st_pcs ADD COLUMN {col} {typ}")
        db.commit()
    load_rules()


RULES = {"app": {}, "title": []}   # rahbar belgilagan turlar (xotirada)


def load_rules():
    apps, titles = {}, []
    for r in q("SELECT k, cat FROM st_rules"):
        if r["k"].endswith(".exe"):
            apps[r["k"]] = r["cat"]
        else:
            titles.append((r["k"], r["cat"]))
    titles.sort(key=lambda kv: -len(kv[0]))          # aniqrog'i (uzunrog'i) birinchi
    RULES["app"], RULES["title"] = apps, titles


def set_rule(key, cat):
    key = " ".join((key or "").lower().split())[:120]
    if len(key) < 2:
        return False
    if cat in ("p", "n", "u"):
        q("INSERT OR REPLACE INTO st_rules(k,cat,created) VALUES(?,?,?)", (key, cat, time.time()))
    else:
        q("DELETE FROM st_rules WHERE k=?", (key,))
    load_rules()
    return True


def rule_key(app, title=""):
    """Belgilash uchun kalit: brauzerda — sayt nomi (sarlavhaning oxirgi qismi), boshqa dasturda — exe nomi."""
    a = (app or "").lower()
    if a in BROWSERS and title:
        parts = [p.strip() for p in re.split(r"\s+[-—–|]\s+", site_of(title)) if p.strip()]
        return (parts[-1] if parts else site_of(title)).lower()[:120]
    return a


# ---------------------------------------------------------------- yordamchilar
def app_label(app):
    a = (app or "").lower()
    return APP_NAMES.get(a, re.sub(r"\.exe$", "", app or "?", flags=re.I))


def site_of(title):
    """Brauzer sarlavhasidan sahifa nomi ('YouTube - Google Chrome' -> 'YouTube')."""
    t = TITLE_SUFFIX.sub("", EDGE_PROFILE.sub("", title or "")).strip(" -—–|")
    return t or "(sarlavhasiz)"


def classify(app, title=""):
    """'p' — ish, 'u' — chalg'ish, 'n' — neytral, 'x' — ekran qulflangan."""
    if (app or "").upper() in ("LOCKED", "BLOCKED"):
        return "x"
    t = (title or "").lower()
    for k, c in RULES["title"]:          # rahbar belgilagani — birinchi (sayt, fayl nomi, so'z)
        if k in t:
            return c
    c = RULES["app"].get((app or "").lower())
    if c:
        return c
    s = f"{app} {title}".lower()
    if any(k in s for k in UNPROD):
        return "u"
    if any(k in s for k in PROD):
        return "p"
    return "n"


def dur(s):
    s = int(s or 0)
    h, m = s // 3600, (s % 3600) // 60
    if h:
        return f"{h} soat {m} daq"
    return f"{m} daq" if m else (f"{s} son" if s else "0")


def short(s):
    s = int(s or 0)
    return f"{s // 3600}:{(s % 3600) // 60:02d}"


def clean_url(u):
    """Faqat sxema+domen+yo'l (so'rov parametrlari va # — olib tashlanadi: tokenlar/shaxsiy ma'lumot bo'lishi mumkin)."""
    m = re.match(r"^(https?://[^/?#\s]+(?:/[^?#\s]*)?)", str(u or "").strip(), re.I)
    return m.group(1)[:300] if m else ""


def url_domain(u):
    m = re.match(r"^https?://(?:[^@/]*@)?([^/:?#]+)", u or "", re.I)
    return re.sub(r"^www\.", "", m.group(1).lower()) if m else ""


def pc_online(r):
    return r["last_seen"] and time.time() - r["last_seen"] < ONLINE_SEC


def pc_now(r):
    try:
        return json.loads(r["now"] or "{}")
    except Exception:
        return {}


def pc_state(r):
    """(emoji, matn) — kompyuter hozir qanday holatda."""
    if not pc_online(r):
        if not r["last_seen"]:
            return "⚫", "oflayn"
        ago = datetime.fromtimestamp(r["last_seen"], TZ)
        return "⚫", f"oflayn ({ago:%d.%m %H:%M} dan)"
    n = pc_now(r)
    want = bool(r["blocked"])
    if n.get("blocked") and want:
        return "⛔", "bloklangan"
    if want != bool(n.get("blocked")):
        return "⏳", "bloklanmoqda…" if want else "ochilmoqda…"
    if n.get("locked"):
        return "🔒", "ekran qulflangan"
    if n.get("idle", 0) >= 180:
        return "🟡", f"harakatsiz {dur(n.get('idle'))}"
    where = app_label(n["app"]) if n.get("app") else "onlayn"
    if n.get("url") and url_domain(n["url"]):
        where = f"{where} · {url_domain(n['url'])}"
    co = now_company(r["tg_id"], n)
    if co:
        where += f" · 🏢 {co[0]}" + ("" if co[1] else " (boshqa hodimniki)")
    return "🟢", where


def now_company(tg_id, n):
    """Hozir ochiq oyna (Telegram chat, 1C, Excel, sayt) qaysi korxonaga tegishli: (nomi, o'ziniki) yoki None."""
    if not tg_id or not (n.get("title") or n.get("url")):
        return None
    comps, match = company_matcher()
    inn = match(n.get("title", ""), n.get("url", ""))
    if not inn:
        return None
    return comps.get(inn, inn), mine_checker(tg_id, now().date().isoformat())(inn)


def pcs():
    return q("SELECT p.*, e.name ename FROM st_pcs p LEFT JOIN employees e ON e.tg_id=p.tg_id "
             "ORDER BY e.name, p.pc")


def pc_title(r):
    return f"{r['ename'] or '?'} · {r['pc']}"


# ---------------------------------------------------------------- API (agentdan)
def _cur(body):
    cur = body.get("now") if isinstance(body.get("now"), dict) else {}
    try:
        idle = int(cur.get("idle") or 0)
    except (TypeError, ValueError):
        idle = 0
    out = {"app": str(cur.get("app", ""))[:80], "title": str(cur.get("title", ""))[:150], "url": clean_url(cur.get("url")),
           "idle": idle, "locked": bool(cur.get("locked")), "blocked": bool(cur.get("blocked"))}
    for k in ("boot", "astart"):        # Windows yoqilgan vaqt va agent ishga tushgan vaqt (yopib qo'yishni aniqlash)
        try:
            out[k] = int(cur.get(k) or 0)
        except (TypeError, ValueError):
            out[k] = 0
    out["ver"] = str(body.get("ver") or "")[:20]
    return out


TAMPER_SENT = {}   # pc_id -> oxirgi ogohlantirish vaqti
LIVE_URL = {}      # tg_id -> {"ts", "domain", "since"} — agent manzil satridan


def _touch_pc(e, body, ip, cur, t_now):
    """Kompyuter yozuvini yangilaydi (LOCK ichida chaqiriladi). -> (pc_id, oldingi 'now')."""
    pc = str(body.get("pc") or "PC")[:60]
    user = str(body.get("user") or "")[:60]
    old = db.execute("SELECT id, now, ver FROM st_pcs WHERE pc=? AND winuser=?", (pc, user)).fetchone()
    db.execute("INSERT INTO st_pcs(pc,winuser,tg_id,last_seen,now,ver,ip,first_seen) VALUES(?,?,?,?,?,?,?,?) "
               "ON CONFLICT(pc,winuser) DO UPDATE SET tg_id=excluded.tg_id, last_seen=excluded.last_seen, "
               "now=excluded.now, ver=excluded.ver, ip=excluded.ip",
               (pc, user, e["tg_id"], t_now, json.dumps(cur, ensure_ascii=False),
                str(body.get("ver") or "")[:20], ip, t_now))
    d = url_domain(cur.get("url"))                    # hozir qaysi saytda va qachondan beri (agent 1.8+)
    lu = LIVE_URL.get(e["tg_id"])
    if d:
        since = lu["since"] if lu and lu["domain"] == d and t_now - lu["ts"] < 75 else t_now
        LIVE_URL[e["tg_id"]] = {"ts": t_now, "domain": d, "since": since}
    elif lu:
        LIVE_URL.pop(e["tg_id"], None)
    pc_id = old["id"] if old else db.execute("SELECT id FROM st_pcs WHERE pc=? AND winuser=?",
                                             (pc, user)).fetchone()["id"]
    try:
        prev = json.loads(old["now"] or "{}") if old else {}
    except ValueError:
        prev = {}
    if old:
        prev["_ver"] = old["ver"]
    return pc_id, prev


def _exchange(e, pc_id, body, t_now):
    """Bajarilgan buyruqlarni belgilaydi, navbatdagilarni oladi (LOCK ichida). -> (cmds, finished)."""
    done = [int(i) for i in (body.get("done") or []) if str(i).isdigit()][:50]
    finished = []
    for i in done:
        r = db.execute("SELECT * FROM st_cmds WHERE id=? AND pc_id=? AND done IS NULL", (i, pc_id)).fetchone()
        if r:
            db.execute("UPDATE st_cmds SET done=? WHERE id=?", (t_now, i))
            finished.append(r)
    cmds = db.execute("SELECT id,cmd,arg FROM st_cmds WHERE pc_id=? AND sent IS NULL AND created>? ORDER BY id",
                      (pc_id, t_now - CMD_TTL)).fetchall()
    for c in cmds:
        db.execute("UPDATE st_cmds SET sent=? WHERE id=?", (t_now, c["id"]))
    return cmds, finished


# ---------------------------------------------------------------- saytlar bo'yicha aniq vaqt
LIVE_WEB = {}   # tg_id -> {"ts", "domain", "title", "since"} — hozir qaysi saytda va qachondan beri


def api_web(body):
    """Hodimlar skripti (hamma saytlarda): har 30 soniyada faol tabdagi domen va sahifa nomi."""
    e = emp_by_key(body.get("key"))
    if not e:
        return 401, {"error": "bad key"}
    domain = re.sub(r"^www\.", "", str(body.get("domain") or "").strip().lower())[:80]
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*\.[a-z0-9-]+", domain or ""):
        return 200, {"ok": True}
    title = " ".join(str(body.get("title") or "").split())[:120] or domain
    try:
        secs = max(0, min(int(body.get("secs") or 0), 90))
    except (TypeError, ValueError):
        secs = 0
    if not secs:
        return 200, {"ok": True}
    q("INSERT INTO st_web(tg_id,day,domain,title,secs) VALUES(?,?,?,?,?) "
      "ON CONFLICT(tg_id,day,domain,title) DO UPDATE SET secs=secs+excluded.secs",
      (e["tg_id"], now().date().isoformat(), domain, title, secs))
    # Havola: faqat sxema+domen+yo'l (so'rov parametrlari va # — olib tashlangan: tokenlar/shaxsiy ma'lumot bo'lishi mumkin)
    url = str(body.get("url") or "")
    m = re.match(r"^(https?://[^/?#\s]+(?:/[^?#\s]*)?)", url)
    url = m.group(1)[:300] if m and domain in m.group(1).lower() else ""
    n = now()
    q("INSERT INTO st_weblog(tg_id,day,hm,domain,title,url,inn,secs) VALUES(?,?,?,?,?,?,NULL,?) "
      "ON CONFLICT(tg_id,day,hm,domain,title) DO UPDATE SET secs=secs+excluded.secs, url=COALESCE(NULLIF(excluded.url,''),url)",
      (e["tg_id"], n.date().isoformat(), n.strftime("%H:%M"), domain, title, url, secs))
    t = time.time()
    lw = LIVE_WEB.get(e["tg_id"])
    since = lw["since"] if lw and lw["domain"] == domain and t - lw["ts"] < 75 else t - secs
    LIVE_WEB[e["tg_id"]] = {"ts": t, "domain": domain, "title": title, "since": since}
    return 200, {"ok": True}


def period_range(kind, day):
    """'week' — dushanba..yakshanba, 'month' — oyning 1-kuni..oxiri (bugundan keyingi kunlar kirmaydi)."""
    d = datetime.strptime(day, "%Y-%m-%d").date()
    if kind == "month":
        a = d.replace(day=1)
        b = (a.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
        label = f"{['', 'Yanvar', 'Fevral', 'Mart', 'Aprel', 'May', 'Iyun', 'Iyul', 'Avgust', 'Sentyabr', 'Oktyabr', 'Noyabr', 'Dekabr'][a.month]} {a.year}"
    else:
        a = d - timedelta(days=d.weekday())
        b = a + timedelta(days=6)
        label = f"{a:%d.%m} – {b:%d.%m.%Y}"
    return a, min(b, now().date()), label


def _hm_avg(vals):
    if not vals:
        return ""
    m = sum(int(v[:2]) * 60 + int(v[3:5]) for v in vals) // len(vals)
    return f"{m // 60:02d}:{m % 60:02d}"


def period_report(kind, day):
    """Haftalik/oylik statistika: har hodim bo'yicha jami va o'rtacha + kunlik batafsil."""
    a, b, label = period_range(kind, day)
    emps = {e["tg_id"]: e["name"] for e in active_employees()}
    agg = {t: {"name": n, "days": 0, "on": 0, "active": 0, "prod": 0, "unprod": 0, "mine": 0, "other": 0,
               "starts": [], "ends": []} for t, n in emps.items()}
    daily = []
    d = a
    while d <= b:
        ds = d.isoformat()
        for t, s in day_stats(ds).items():
            if t not in agg or not s.get("on"):
                continue
            g = agg[t]
            cd = company_data(t, ds)
            mine = sum(r["secs"] for r in cd["rows"] if r["mine"])
            other = sum(r["secs"] for r in cd["rows"] if not r["mine"])
            g["days"] += 1
            for k in ("on", "active", "prod", "unprod"):
                g[k] += s.get(k, 0)
            g["mine"] += mine
            g["other"] += other
            if s.get("span"):
                g["starts"].append(s["span"][0])
                g["ends"].append(s["span"][1])
            daily.append({"day": ds, "name": g["name"], "on": s.get("on", 0), "active": s.get("active", 0),
                          "prod": s.get("prod", 0), "unprod": s.get("unprod", 0), "mine": mine, "other": other,
                          "eff": s.get("eff"), "start": (s.get("span") or ["", ""])[0], "end": (s.get("span") or ["", ""])[1]})
        d += timedelta(days=1)
    rows = []
    for t, g in sorted(agg.items(), key=lambda kv: -kv[1]["active"]):
        rows.append({"tg": t, "name": g["name"], "days": g["days"], "on": g["on"], "active": g["active"],
                     "avg": g["active"] // g["days"] if g["days"] else 0, "prod": g["prod"], "unprod": g["unprod"],
                     "eff": round(100 * g["prod"] / g["active"]) if g["active"] >= 600 else None,
                     "mine": g["mine"], "other": g["other"], "start": _hm_avg(g["starts"]), "end": _hm_avg(g["ends"])})
    return {"kind": kind, "label": label, "from": a.isoformat(), "to": b.isoformat(), "rows": rows, "daily": daily}


def report_csv(kind, day, detail=False):
    """Excel ochadigan CSV (UTF-8 BOM, ';'). Vaqtlar soat:daqiqa ko'rinishida."""
    r = period_report(kind, day)

    def line(vals):
        out = []
        for v in vals:
            v = "" if v is None else str(v)
            out.append('"' + v.replace('"', '""') + '"' if any(c in v for c in ';"\n') else v)
        return ";".join(out)
    if detail:
        lines = [line(["Sana", "Hodim", "Kompyuterda", "Faol", "Ish dasturlari", "Ish ulushi %", "Chalg'ish",
                       "O'z korxonalari", "Boshqa korxonalar", "Boshladi", "Tugatdi"])]
        for x in sorted(r["daily"], key=lambda v: (v["day"], v["name"])):
            lines.append(line([fmt_day(x["day"]), x["name"], short(x["on"]), short(x["active"]), short(x["prod"]),
                               x["eff"] if x["eff"] is not None else "", short(x["unprod"]), short(x["mine"]),
                               short(x["other"]), x["start"], x["end"]]))
    else:
        lines = [line([f"Screen time — {r['label']}"]),
                 line(["Hodim", "Ishlagan kunlar", "Kompyuterda jami", "Faol jami", "Kunlik o'rtacha faol",
                       "Ish dasturlari", "Ish ulushi %", "Chalg'ish", "O'z korxonalari", "Boshqa korxonalar",
                       "O'rtacha boshlash", "O'rtacha tugatish"])]
        for x in r["rows"]:
            lines.append(line([x["name"], x["days"], short(x["on"]), short(x["active"]), short(x["avg"]), short(x["prod"]),
                               x["eff"] if x["eff"] is not None else "", short(x["unprod"]), short(x["mine"]),
                               short(x["other"]), x["start"], x["end"]]))
    return "﻿" + "\r\n".join(lines) + "\r\n", f"screentime-{kind}-{r['from']}{'-kunlik' if detail else ''}.csv"


def report_emp_csv(kind, day, tg_id):
    """Bitta hodim samaradorligi (hafta/oy): umumiy ko'rsatkichlar, kunlik jadval, eng ko'p ishlatilgan
    dasturlar, saytlar va korxonalar. Excel ochadigan CSV."""
    a, b, label = period_range(kind, day)
    name = emp_name(tg_id)

    def line(vals):
        out = []
        for v in vals:
            v = "" if v is None else str(v)
            out.append('"' + v.replace('"', '""') + '"' if any(c in v for c in ';"\n') else v)
        return ";".join(out)
    tot = {"on": 0, "active": 0, "prod": 0, "unprod": 0, "idle": 0, "mine": 0, "other": 0}
    apps, sites, comps, days, starts, ends = {}, {}, {}, [], [], []
    d = a
    while d <= b:
        ds = d.isoformat()
        s = day_stats(ds, tg_id).get(tg_id)
        if s and s.get("on"):
            cd = company_data(tg_id, ds)
            mine = sum(r["secs"] for r in cd["rows"] if r["mine"])
            other = sum(r["secs"] for r in cd["rows"] if not r["mine"])
            for k in ("on", "active", "prod", "unprod", "idle"):
                tot[k] += s.get(k, 0)
            tot["mine"] += mine
            tot["other"] += other
            for k, v in s["apps"].items():
                apps[k] = apps.get(k, 0) + v[0]
            for r in cd["rows"]:
                if r["secs"]:
                    c = comps.setdefault(r["name"], [0, r["mine"]])
                    c[0] += r["secs"]
            if s.get("span"):
                starts.append(s["span"][0])
                ends.append(s["span"][1])
            days.append([fmt_day(ds), short(s["on"]), short(s["active"]), short(s["idle"]), short(s["prod"]),
                         s["eff"] if s["eff"] is not None else "", short(s["unprod"]), short(mine), short(other),
                         (s.get("span") or ["", ""])[0], (s.get("span") or ["", ""])[1]])
        for r in q("SELECT domain, SUM(secs) s FROM st_web WHERE tg_id=? AND day=? GROUP BY domain", (tg_id, ds)):
            sites[r["domain"]] = sites.get(r["domain"], 0) + r["s"]
        d += timedelta(days=1)
    eff = round(100 * tot["prod"] / tot["active"]) if tot["active"] >= 600 else ""
    nd = len(days)
    L = [line([f"{name} — samaradorlik hisoboti"]), line(["Davr", label]), "",
         line(["Ko'rsatkich", "Qiymat"]),
         line(["Ishlagan kunlar", nd]),
         line(["Kompyuterda jami", short(tot["on"])]),
         line(["Faol jami", short(tot["active"])]),
         line(["Kunlik o'rtacha faol", short(tot["active"] // nd) if nd else "0:00"]),
         line(["Harakatsiz jami", short(tot["idle"])]),
         line(["Ish dasturlari", short(tot["prod"])]),
         line(["Ish ulushi %", eff]),
         line(["Chalg'ish", short(tot["unprod"])]),
         line(["O'z korxonalariga", short(tot["mine"])]),
         line(["Boshqa korxonalarga", short(tot["other"])]),
         line(["O'rtacha boshlash", _hm_avg(starts)]),
         line(["O'rtacha tugatish", _hm_avg(ends)]), "",
         line(["KUNLAR BO'YICHA"]),
         line(["Sana", "Kompyuterda", "Faol", "Harakatsiz", "Ish dasturlari", "Ish ulushi %", "Chalg'ish",
               "O'z korxonalari", "Boshqa korxonalar", "Boshladi", "Tugatdi"])]
    L += [line(r) for r in days]
    L += ["", line(["KORXONALAR", "Vaqt", "Kimniki"])]
    L += [line([k, short(v[0]), "o'zining" if v[1] else "boshqa hodimniki"])
          for k, v in sorted(comps.items(), key=lambda kv: -kv[1][0])]
    L += ["", line(["DASTURLAR", "Vaqt"])]
    L += [line([k, short(v)]) for k, v in sorted(apps.items(), key=lambda kv: -kv[1])[:30]]
    if sites:
        L += ["", line(["SAYTLAR", "Vaqt"])]
        L += [line([k, short(v)]) for k, v in sorted(sites.items(), key=lambda kv: -kv[1])[:40]]
    safe = re.sub(r"[^\w-]+", "_", name, flags=re.U).strip("_")[:40] or str(tg_id)
    return "﻿" + "\r\n".join(L) + "\r\n", f"samaradorlik-{safe}-{kind}-{a.isoformat()}.csv"


def mine_checker(tg_id, day):
    """inn -> korxona shu hodimniki hisoblanadimi (`day` kuni). Hodim — o'ziga biriktirilgan;
    Bosh buxgalter — o'zi va jamoasidagi hodimlarga biriktirilgan; ZAM/Boshliq — ro'yxatdagi hammasi."""
    role = role_of(tg_id)
    team = set(team_ids(tg_id)) if role == "bosh" else {tg_id}
    listed = {r["inn"] for r in q("SELECT inn FROM companies WHERE COALESCE(active,1)=1")} \
        if role in ("zam", "boshliq") else set()
    cache = {}

    def assigned(inn):
        if inn not in cache:
            cache[inn] = responsible(inn, day) in team
        return cache[inn]

    def is_mine(inn):
        return inn in listed or assigned(inn)
    is_mine.assigned = assigned
    return is_mine


def company_matcher():
    """Ro'yxatdagi korxonalar + oyna/sahifa/chat nomidan korxonani topuvchi funksiya.
    Kalitlar: korxona nomi (shakl so'zlarisiz), INN, va jadvaldagi "Chat / kalit so'zlar" (Telegram guruh nomi,
    qisqa nom, 1C baza nomi...) — vergul bilan."""
    rows = q("SELECT inn, name, aliases FROM companies WHERE COALESCE(active,1)=1")
    comps = {r["inn"]: r["name"] or r["inn"] for r in rows}
    keys = [(norm_name(r["name"]), r["inn"]) for r in rows]
    for r in rows:
        keys += [(norm_name(a), r["inn"]) for a in re.split(r"[,;\n]+", r["aliases"] or "") if a.strip()]
    keys = sorted({(k, inn) for k, inn in keys if len(k) >= 3}, key=lambda kv: -len(kv[0]))

    def match(*texts):
        for text in texts:
            if not text:
                continue
            hit = next((d for d in re.findall(r"(?<!\d)\d{9}(?!\d)", text) if d in comps), None)
            if hit:
                return hit
            t = " " + norm_name(text) + " "
            hit = next((inn for k, inn in keys if f" {k} " in t), None)
            if hit:
                return hit
        return None
    return comps, match


CTX_MIN = 10      # ish sayti/dasturida korxona nomi ko'rinmasa — oxirgi 10 daqiqadagi korxona davom etyapti deb olinadi


def _hm_add(hm, n=1):
    m = int(hm[:2]) * 60 + int(hm[3:5]) + n
    return f"{m // 60 % 24:02d}:{m % 60:02d}"


def activity_log(tg_id, day):
    """Faoliyat jurnali: kun davomida "nechadan nechagacha — qaysi dastur/sayt — qaysi korxona" bo'laklari.
    Agentning daqiqalik yozuvi + brauzer skripti (sahifa, havola) + soliq kabineti (INN) birlashtiriladi."""
    comps, match = company_matcher()
    is_mine = mine_checker(tg_id, day)
    mins, act = {}, {}
    for r in q("SELECT hm, app, title, secs, active, url FROM st_log WHERE tg_id=? AND day=?", (tg_id, day)):
        act[r["hm"]] = act.get(r["hm"], 0) + r["active"]
        m = mins.get(r["hm"])
        if not m or r["secs"] > m["secs"]:
            mins[r["hm"]] = dict(r)
    web, kab = {}, {}
    for r in q("SELECT hm, domain, title, url, inn, secs FROM st_weblog WHERE tg_id=? AND day=?", (tg_id, day)):
        if r["domain"] == "__kabinet__":
            kab[r["hm"]] = r["inn"]
        elif r["hm"] not in web or r["secs"] > web[r["hm"]]["secs"]:
            web[r["hm"]] = dict(r)
    rows = []
    last_inn, last_at = None, -999
    for hm in sorted(mins):
        m = mins[hm]
        app = m["app"] or "?"
        if app.upper() in ("LOCKED", "BLOCKED"):
            rows.append({"hm": hm, "label": app_label(app), "sub": "", "url": "", "inn": None, "cat": "x", "via": "",
                         "a": 0, "cat0": "x", "mine": False})
            last_inn = None
            continue
        browser = app.lower() in BROWSERS
        w = web.get(hm) or web.get(_hm_add(hm, 1)) if browser else None
        mu = (m.get("url") or "") if browser else ""          # agent 1.8+: manzil satridan aniq havola
        if mu and url_domain(mu):
            label, sub, url = url_domain(mu), site_of(m["title"]), mu
        elif w:
            label, sub, url = w["domain"], w["title"], w["url"] or ""
        else:
            label, sub, url = app_label(app), (site_of(m["title"]) if browser else m["title"]), ""
        site = browser and label != app_label(app)
        inn = kab.get(hm) or kab.get(_hm_add(hm, 1)) if (site and "soliq.uz" in label) else None
        # Korxona: soliq kabineti (INN) / oyna sarlavhasi (Telegram chat, 1C baza, Excel fayl) / sahifa nomi / havola
        inn = inn or match(m["title"], w["title"] if w else "", url)
        cat0 = classify(app, f"{label} {sub}" if site else m["title"])
        t_min = int(hm[:2]) * 60 + int(hm[3:5])
        ctx = False
        if not inn and cat0 == "p" and last_inn and t_min - last_at <= CTX_MIN:
            inn, ctx = last_inn, True                        # bank / statistika / 1C — o'sha korxona ishi davom etyapti
        if inn and not ctx:
            last_inn, last_at = inn, t_min
        elif inn:
            last_at = t_min
        mine = bool(inn) and is_mine(inn)
        a = act.get(hm, 0)
        # O'ziga biriktirilgan korxona ustidagi ish — qaysi dasturda bo'lsa ham (Telegram, bank, statistika...) "ish"
        cat = "i" if a < 10 else ("p" if mine else cat0)
        rows.append({"hm": hm, "label": label, "sub": sub or "", "url": url, "inn": inn, "cat": cat,
                     "via": app_label(app) if site else "", "a": min(a, 60), "cat0": cat0, "mine": mine})
    # Ketma-ket daqiqalarni bo'laklarga birlashtirish (2 daqiqagacha uzilish — bitta bo'lak)
    segs = []
    for r in rows:
        s = segs[-1] if segs else None
        if s and s["label"] == r["label"] and s["inn"] == r["inn"] and (s["cat"] == "i") == (r["cat"] == "i") \
                and int(r["hm"][:2]) * 60 + int(r["hm"][3:]) - (int(s["last"][:2]) * 60 + int(s["last"][3:])) <= 3:
            s["last"] = r["hm"]
            s["mins"] += 1
            s["subs"][r["sub"]] = s["subs"].get(r["sub"], 0) + 1
            if r["url"] and r["url"] not in s["urls"] and len(s["urls"]) < 8:
                s["urls"].append(r["url"])
        else:
            segs.append({"from": r["hm"], "last": r["hm"], "mins": 1, "label": r["label"], "inn": r["inn"],
                         "cat": r["cat"], "subs": {r["sub"]: 1}, "urls": [r["url"]] if r["url"] else [], "via": r["via"]})
    out, per_co = [], {}
    for s in segs:
        subs = [k for k, _v in sorted(s["subs"].items(), key=lambda kv: -kv[1]) if k][:3]
        mine = bool(s["inn"]) and is_mine(s["inn"])
        out.append({"from": s["from"], "to": _hm_add(s["last"]), "mins": s["mins"], "t": dur(s["mins"] * 60),
                    "label": s["label"], "subs": subs, "urls": s["urls"], "via": s["via"],
                    "company": comps.get(s["inn"], s["inn"]) if s["inn"] else "", "mine": mine,
                    "cat": "mine" if mine and s["cat"] == "p" else
                    {"p": "work", "u": "distract", "i": "idle", "x": "idle"}.get(s["cat"], "neutral")})
        if s["inn"] and s["cat"] != "i":
            c = per_co.setdefault(s["inn"], {"name": comps.get(s["inn"], s["inn"]), "mins": 0, "first": s["from"],
                                             "last": _hm_add(s["last"]), "times": 0, "mine": mine})
            c["mins"] += s["mins"]
            c["last"] = _hm_add(s["last"])
            c["times"] += 1
    active_mins = sum(1 for r in rows if r["cat"] not in ("i", "x"))
    co_mins = sum(c["mins"] for c in per_co.values())
    # Statistikaga tuzatish: o'z korxonasi ishi bo'lgan, lekin dastur/sayt bo'yicha "ish" deb sanalmagan faol soniyalar
    fix = {"prod": sum(r["a"] for r in rows if r["mine"] and r["cat"] == "p" and r["cat0"] != "p"),
           "unprod": sum(r["a"] for r in rows if r["mine"] and r["cat"] == "p" and r["cat0"] == "u"),
           "mine": sum(r["a"] for r in rows if r["mine"] and r["cat"] == "p")}
    return {"segments": out, "fix": fix, "mine_hm": [r["hm"] for r in rows if r["mine"] and r["cat"] == "p"],
            "companies": [{"inn": k, **v, "t": dur(v["mins"] * 60)} for k, v in sorted(per_co.items(), key=lambda kv: -kv[1]["mins"])],
            "co_mins": co_mins, "co_t": dur(co_mins * 60),
            "co_share": round(100 * co_mins / active_mins) if active_mins >= 10 else None}


def log_csv(tg_id, day):
    """Bir kunlik faoliyat jurnali — Excel uchun."""
    lg = activity_log(tg_id, day)
    cat = {"mine": "o'z korxonasi ishi", "work": "ish", "distract": "chalg'ish", "idle": "harakatsiz", "neutral": "boshqa"}

    def line(vals):
        return ";".join('"' + str(v).replace('"', '""') + '"' if any(c in str(v) for c in ';"\n') else str(v) for v in vals)
    L = [line([f"{emp_name(tg_id)} — faoliyat jurnali", fmt_day(day)]),
         line(["Korxonalar ishiga", lg["co_t"], f"{lg['co_share']}% faol vaqtdan" if lg["co_share"] is not None else ""]), "",
         line(["Boshlandi", "Tugadi", "Davomiyligi", "Dastur / sayt", "Oyna / sahifa", "Korxona", "Kimniki", "Tur", "Havolalar"])]
    for s in lg["segments"]:
        L.append(line([s["from"], s["to"], dur(s["mins"] * 60), s["label"], " | ".join(s["subs"]), s["company"],
                       ("o'zining" if s["mine"] else "boshqa hodimniki") if s["company"] else "", cat[s["cat"]],
                       " ".join(s["urls"])]))
    safe = re.sub(r"[^\w-]+", "_", emp_name(tg_id), flags=re.U).strip("_")[:40] or str(tg_id)
    return "﻿" + "\r\n".join(L) + "\r\n", f"jurnal-{safe}-{day}.csv"


WB_COL = {"mine": "#F28322", "work": "#22c55e", "neutral": "#60a5fa", "distract": "#ef4444", "idle": "#d6d3d1"}
WB_NAME = {"mine": "Bizning korxonalar", "work": "Boshqa ish", "neutral": "Boshqa", "distract": "Chalg‘ish",
           "idle": "Harakatsiz"}


def wb_data(tg_id, day):
    """Hodimning bir kunlik "raqamli muvozanati" (Pixel Digital Wellbeing kabi): umumiy vaqt va toifalar,
    soatlar bo'yicha, hafta, saytlar (aniq — skriptdan), dasturlar, korxonalar, hozir nima ochiq."""
    d = datetime.strptime(day, "%Y-%m-%d").date()
    s = day_stats(day, tg_id).get(tg_id) or {}
    cd = company_data(tg_id, day)
    lg = activity_log(tg_id, day)
    mine = sum(r["secs"] for r in cd["rows"] if r["mine"])
    other_co = sum(r["secs"] for r in cd["rows"] if not r["mine"])
    if lg["companies"]:          # daqiqalik jurnal bor — aniqroq hisob shundan
        mine = max(mine, sum(c["mins"] for c in lg["companies"] if c["mine"]) * 60)
        other_co = max(other_co, sum(c["mins"] for c in lg["companies"] if not c["mine"]) * 60)
    act, prod, unprod, idle = s.get("active", 0), s.get("prod", 0), s.get("unprod", 0), s.get("idle", 0)
    mine = min(max(mine, s.get("mine", 0)), act)
    work = max(0, prod - mine)
    neutral = max(0, act - mine - work - unprod)
    seg = {"mine": mine, "work": work, "neutral": neutral, "distract": unprod, "idle": idle}
    # Soatlar bo'yicha (agentning daqiqalik yozuvlaridan)
    # O'z korxonasi ishi bo'lgan daqiqalar (Telegram, bank, 1C... — qayerda bo'lsa ham) alohida rangda
    hours = {h: {"mine": 0, "work": 0, "neutral": 0, "distract": 0, "idle": 0} for h in range(24)}
    mine_hm = set(lg.get("mine_hm", []))
    for r in q("SELECT hm, secs, active, cat, app FROM st_minutes WHERE tg_id=? AND day=?", (tg_id, day)):
        if r["cat"] == "x":
            continue
        cat = RULES["app"].get((r["app"] or "").lower(), r["cat"])   # keyin belgilangan tur ham hisobga olinadi
        k = "idle" if r["active"] < 10 else "mine" if r["hm"] in mine_hm else \
            {"p": "work", "u": "distract"}.get(cat, "neutral")
        hours[int(r["hm"][:2])][k] += r["secs"]
    # Hafta: oxirgi 7 kun faol vaqti
    week = []
    for i in range(6, -1, -1):
        dd = (d - timedelta(days=i)).isoformat()
        ws = day_stats(dd, tg_id).get(tg_id) or {}
        week.append({"day": dd, "label": ["Du", "Se", "Ch", "Pa", "Ju", "Sh", "Ya"][(d - timedelta(days=i)).weekday()],
                     "active": ws.get("active", 0), "prod": ws.get("prod", 0), "unprod": ws.get("unprod", 0),
                     "mine": ws.get("mine", 0)})
    prev = week[-2]["active"] if len(week) > 1 else 0
    # Saytlar: skriptdan aniq (domen bo'yicha), bo'lmasa — oyna sarlavhalaridan taxminiy
    sites, exact = {}, False
    for r in q("SELECT domain, title, secs FROM st_web WHERE tg_id=? AND day=?", (tg_id, day)):
        exact = True
        x = sites.setdefault(r["domain"], {"name": r["domain"], "secs": 0, "pages": {}, "cats": {}})
        x["secs"] += r["secs"]
        x["pages"][r["title"]] = x["pages"].get(r["title"], 0) + r["secs"]
        c = classify("web", f"{r['domain']} {r['title']}")
        x["cats"][c] = x["cats"].get(c, 0) + r["secs"]
    if not exact:
        for r in q("SELECT app, title, secs FROM st_usage WHERE tg_id=? AND day=?", (tg_id, day)):
            if (r["app"] or "").lower() not in BROWSERS:
                continue
            name = rule_key(r["app"], r["title"]) or site_of(r["title"])
            x = sites.setdefault(name, {"name": name, "secs": 0, "pages": {}, "cats": {}})
            x["secs"] += r["secs"]
            x["pages"][site_of(r["title"])] = x["pages"].get(site_of(r["title"]), 0) + r["secs"]
            c = classify(r["app"], r["title"])
            x["cats"][c] = x["cats"].get(c, 0) + r["secs"]
    site_list = [{"name": x["name"], "secs": x["secs"], "t": dur(x["secs"]),
                  "cat": {"p": "work", "u": "distract"}.get(max(x["cats"], key=x["cats"].get), "neutral"),
                  "pages": [{"name": k[:120], "t": dur(v), "secs": v}
                            for k, v in sorted(x["pages"].items(), key=lambda kv: -kv[1])[:6]]}
                 for x in sorted(sites.values(), key=lambda v: -v["secs"])[:40]]
    apps = [{"name": k, "secs": v[0], "t": dur(v[0]), "cat": {"p": "work", "u": "distract"}.get(v[1], "neutral")}
            for k, v in top(s.get("apps", {}), 15)]
    companies = [{"name": r["name"], "inn": r["inn"], "mine": r["mine"], "secs": r["secs"], "t": dur(r["secs"]) if r["secs"] else "—"}
                 for r in cd["rows"]]
    # Hozir
    live, today = None, day == now().date().isoformat()
    if today:
        pr = q("SELECT * FROM st_pcs WHERE tg_id=? ORDER BY last_seen DESC LIMIT 1", (tg_id,), one=True)
        lw = LIVE_WEB.get(tg_id)
        if pr and pc_online(pr):
            n = pc_now(pr)
            live = {"app": app_label(n.get("app")) if n.get("app") else "", "title": site_of(n.get("title", "")),
                    "idle": n.get("idle", 0), "blocked": bool(n.get("blocked")), "locked": bool(n.get("locked")),
                    "site": None, "corp": corp_now(tg_id), "co": None}
            co = None if live["corp"] else now_company(tg_id, n)
            if co:
                live["co"] = {"name": co[0], "mine": co[1]}
            lu = LIVE_URL.get(tg_id)
            if n.get("url") and lu and lu["domain"] == url_domain(n["url"]):
                live["site"] = {"domain": lu["domain"], "title": site_of(n.get("title", "")), "url": n["url"],
                                "via": app_label(n.get("app")), "secs": int(time.time() - lu["since"])}
            elif lw and time.time() - lw["ts"] < 75:
                live["site"] = {"domain": lw["domain"], "title": lw["title"], "secs": int(time.time() - lw["since"])}
    co_time = {c["inn"]: c for c in lg["companies"]}
    have = {c["inn"] for c in companies}
    for x in lg["companies"]:     # jurnalda bor, lekin umumiy hisobda yo'q korxonalar ham ko'rinsin
        if x["inn"] not in have:
            companies.append({"name": x["name"], "inn": x["inn"], "mine": x["mine"], "secs": x["mins"] * 60, "t": x["t"]})
        else:
            c = next(c for c in companies if c["inn"] == x["inn"])
            if x["mins"] * 60 > c["secs"]:
                c["secs"], c["t"] = x["mins"] * 60, x["t"]
    companies.sort(key=lambda c: -c["secs"])
    for c in companies:          # korxonalar ro'yxatiga "nechadan nechagacha" qo'shiladi
        x = co_time.get(c["inn"])
        c["span"] = f"{x['first']}–{x['last']} · {x['times']} marta" if x else ""
    return {"name": emp_name(tg_id), "day": day, "dayf": fmt_day(day), "today": today, "exact": exact,
            "log": lg["segments"], "co_t": lg["co_t"], "co_share": lg["co_share"], "role": role_of(tg_id),
            "total": s.get("on", 0), "active": act, "prev": prev, "span": s.get("span"),
            "seg": [{"k": k, "name": WB_NAME[k], "col": WB_COL[k], "secs": v, "t": dur(v)} for k, v in seg.items()],
            "other_co": dur(other_co), "mine_t": dur(mine),
            "hours": [{"h": h, **hours[h]} for h in range(24)], "week": week,
            "sites": site_list, "apps": apps, "companies": companies, "live": live}


WB_CSS = """
.wbh{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:6px}
.wbh .back{color:var(--mut);text-decoration:none;font-size:14px;display:inline-flex;align-items:center;gap:4px}
.wbh .back:hover{color:var(--acc)}
.hero{display:grid;grid-template-columns:minmax(240px,320px) 1fr;gap:28px;align-items:center;padding:28px 30px;margin-top:18px}
.donut{position:relative;width:100%;max-width:290px;aspect-ratio:1;margin:auto}
.donut svg{width:100%;height:100%;transform:rotate(-90deg)}
.donut circle{fill:none;stroke-width:26;transition:stroke-dasharray .9s cubic-bezier(.2,.8,.2,1),stroke-dashoffset .9s cubic-bezier(.2,.8,.2,1)}
.donut .ctr{position:absolute;inset:0;display:grid;place-content:center;text-align:center}
.donut .ctr b{font-size:34px;font-weight:700;letter-spacing:-.03em;line-height:1.1}
.donut .ctr span{font-size:13px;color:var(--mut)}
.delta{display:inline-block;margin-top:6px;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;background:var(--line)}
.delta.up{background:var(--y-bg);color:var(--y)}.delta.down{background:var(--g-bg);color:var(--g)}
.legend{display:grid;gap:10px}
.lgr{display:grid;grid-template-columns:14px 1fr auto 52px;gap:12px;align-items:center;font-size:15px}
.lgr i{width:14px;height:14px;border-radius:5px}
.lgr b{font-weight:600}.lgr .pc{color:var(--mut);font-size:13px;text-align:right}
.live{display:flex;align-items:center;gap:14px;padding:16px 22px;margin-top:14px;flex-wrap:wrap}
.live .pulse{width:12px;height:12px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 0 rgba(34,197,94,.6);animation:pl 1.8s infinite}
.live.off .pulse{background:#a8a29e;animation:none}
@keyframes pl{70%{box-shadow:0 0 0 12px rgba(34,197,94,0)}100%{box-shadow:0 0 0 0 rgba(34,197,94,0)}}
.live .what{font-size:15px;min-width:0;flex:1}.live .what b{font-size:16px}
.live .ttl{max-width:100%}
.live .cnt{font-size:28px;font-weight:700;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.g2{display:grid;grid-template-columns:3fr 2fr;gap:14px;margin-top:14px}
.pane{padding:20px 22px}.pane h3{font-size:16px;margin:0 0 14px}
.chart{width:100%;height:190px;display:block}
.chart text{font-size:10px;fill:var(--mut);font-family:inherit}
.chart rect{transition:height .6s,y .6s}
.pane .tabs2{margin-bottom:12px}
.rows{display:grid;gap:2px}
.rw{border-radius:14px;padding:10px 12px;cursor:pointer}.rw:hover,.rw.open{background:var(--acc-soft)}
.rwh{display:grid;grid-template-columns:38px minmax(0,1fr) auto;gap:12px;align-items:center}
.av{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;font-weight:700;font-size:16px;color:#fff}
.rwh .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600;font-size:15px}
.rwh .tm{font-weight:600;font-variant-numeric:tabular-nums;font-size:15px}
.rw .bar{margin:8px 0 0 50px;height:6px}
.rw .pg{display:none;margin:8px 0 2px 50px}.rw.open .pg{display:block}
.pgr{display:flex;justify-content:space-between;gap:12px;font-size:13px;padding:3px 0}.pgr span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.note2{font-size:13px;color:var(--mut);margin:10px 2px 0}
.lhead{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:12px}.lhead .tabs2{margin:0}
.lhead .btn{margin-left:auto}
.lg-row{display:grid;grid-template-columns:112px 4px minmax(0,1fr);gap:14px;align-items:start;padding:11px 10px;border-radius:12px}
.lg-row:hover{background:var(--acc-soft)}
.lgt b{display:block;font-size:14px;font-weight:500;font-variant-numeric:tabular-nums}.lgt span{font-size:12.5px;color:var(--mut)}
.lgc{width:4px;align-self:stretch;border-radius:4px}
.lgl{display:flex;align-items:center;gap:8px;flex-wrap:wrap}.lgl b{font-weight:500;font-size:15px}
.lgm .ttl{max-width:100%}
.lgu{display:flex;flex-direction:column;gap:2px;margin-top:4px}
.lgu a{font-size:12.5px;color:var(--acc);text-decoration:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lgu a:hover{text-decoration:underline}
.via{font-size:12px;color:var(--mut);font-weight:400}.fav{font-size:13px;line-height:1}
.tg{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:500;background:var(--line);color:var(--mut)}
.tg.g{background:rgba(242,131,34,.14);color:#c2410c}.tg.y{background:var(--line);color:var(--mut)}
@media (max-width:820px){.hero{grid-template-columns:1fr}.g2{grid-template-columns:1fr}}
"""

WB_JS = r"""
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const E=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const COL={mine:'#F28322',work:'#22c55e',neutral:'#60a5fa',distract:'#ef4444',idle:'#d6d3d1'};
const fmt=s=>{s=Math.max(0,Math.round(s||0));const h=Math.floor(s/3600),m=Math.floor(s%3600/60);return h?`${h} soat ${m} daq`:(m?`${m} daq`:`${s} son`)};
const clock=s=>{s=Math.max(0,Math.floor(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return (h?h+':':'')+String(m).padStart(h?2:1,'0')+':'+String(x).padStart(2,'0')};
let D=null,TAB='log',OPEN={},liveBase=0,liveAt=0;
function donut(){const C=2*Math.PI*90,tot=D.seg.reduce((a,b)=>a+b.secs,0)||1;let off=0;
 $$('#dn circle.s').forEach((c,i)=>{const v=D.seg[i].secs/tot*C;c.setAttribute('stroke',D.seg[i].col);
  c.style.strokeDasharray=`${Math.max(0,v-2)} ${C}`;c.style.strokeDashoffset=-off;off+=v});
 $('#tot').textContent=fmt(D.active);
 const dl=$('#dl');if(D.prev>300){const p=Math.round((D.active-D.prev)/D.prev*100);dl.hidden=false;
  dl.className='delta '+(p>0?'up':'down');dl.textContent=(p>0?'+':'')+p+'% kechagiga nisbatan'}else dl.hidden=true;
 $('#lg').innerHTML=D.seg.map(s=>`<div class="lgr"><i style="background:${s.col}"></i><span>${E(s.name)}</span><b>${s.t}</b>
  <span class="pc">${Math.round(s.secs/tot*100)}%</span></div>`).join('')+
  `<div class="note2">Korxonalar ishiga <b>${D.co_t}</b>${D.co_share!=null?` (<b>${D.co_share}%</b> faol vaqtdan)`:''}`+
  ` · o‘z korxonalariga <b>${D.mine_t}</b> · boshqalarniki <b>${D.other_co}</b>`+(D.span?` · ${D.span[0]}–${D.span[1]}`:'')+`</div>`}
function logList(){if(!D.log.length)return '<div class="note2">Bu kunda faoliyat yozilmagan</div>';
 return D.log.slice().reverse().map(s=>{const k='l:'+s.from;
  const links=s.urls.map(u=>`<a href="${E(u)}" target="_blank" rel="noopener noreferrer">${E(u.replace(/^https?:\/\//,'').slice(0,80))}</a>`).join('');
  return `<div class="lg-row${OPEN[k]?' open':''}" data-k="${E(k)}"><div class="lgt"><b>${s.from}–${s.to}</b><span>${s.t}</span></div>
  <i class="lgc" style="background:${COL[s.cat]}"></i><div class="lgm"><div class="lgl">${s.via?'<span class="fav">🌐</span>':''}<b>${E(s.label)}</b>
  ${s.via?`<span class="via">${E(s.via)}</span>`:''}
  ${s.company?`<span class="tg ${s.mine?'g':'y'}">${E(s.company)}</span>`:''}</div>
  <div class="sub ttl">${E(s.subs.join(' · '))}</div>${links?`<div class="lgu">${links}</div>`:''}</div></div>`}).join('')}
function bars(el,items,keys,labels,maxv,hl){const W=el.clientWidth||600,H=190,b=24,n=items.length,gw=(W-30)/n,bw=Math.min(28,gw*.62);
 let s=`<line x1="30" y1="${H-b}" x2="${W}" y2="${H-b}" stroke="var(--line)"/>`;
 [.5,1].forEach(f=>{const y=(H-b)-(H-b-10)*f;s+=`<line x1="30" y1="${y}" x2="${W}" y2="${y}" stroke="var(--line)" stroke-dasharray="3 4"/><text x="0" y="${y+3}">${Math.round(maxv*f/60)}d</text>`});
 items.forEach((it,i)=>{let y=H-b;const x=30+i*gw+(gw-bw)/2;
  keys.forEach(k=>{const h=(it[k]||0)/maxv*(H-b-10);if(h>.5){y-=h;s+=`<rect x="${x}" y="${y}" width="${bw}" height="${h}" rx="4" fill="${COL[k]}" opacity="${hl&&!hl(it)?.45:1}"><title>${E(labels(it))}: ${fmt(it[k])}</title></rect>`}});
  s+=`<text x="${x+bw/2}" y="${H-6}" text-anchor="middle">${E(labels(it))}</text>`});
 el.innerHTML=s}
function charts(){const hs=D.hours.filter(h=>h.h>=7&&h.h<=21);
 bars($('#hc'),hs,['mine','work','neutral','distract','idle'],h=>String(h.h).padStart(2,'0'),3600);
 const wk=D.week.map(w=>({...w,mine:w.mine||0,work:Math.max(0,w.prod-(w.mine||0)),distract:w.unprod,
  neutral:Math.max(0,w.active-w.prod-w.unprod)}));
 const mx=Math.max(3600,...wk.map(w=>w.active));
 bars($('#wc'),wk,['mine','work','neutral','distract'],w=>w.label,Math.ceil(mx/3600)*3600,w=>w.day===D.day)}
function lists(){let rows;
 if(TAB==='log'){$('#ls').innerHTML=logList();$('#exact').textContent='Eng yangisi tepada. Havolalar — faqat sahifa manzili (parametrlarsiz).';return}
 if(TAB==='sites')rows=D.sites.map(x=>({k:'s:'+x.name,name:x.name,t:x.t,secs:x.secs,col:COL[x.cat],sub:x.pages}));
 else if(TAB==='corp')rows=D.companies.map(x=>({k:'c:'+x.inn,name:x.name,t:x.t,secs:x.secs,col:x.mine?COL.mine:COL.neutral,
  tag:x.mine?'o‘zining':'boshqa hodimniki',sub:x.span?[{name:'Ishlagan vaqti: '+x.span,t:''}]:[]}));
 else rows=D.apps.map(x=>({k:'a:'+x.name,name:x.name,t:x.t,secs:x.secs,col:COL[x.cat],sub:[]}));
 const mx=Math.max(1,...rows.map(r=>r.secs));
 $('#ls').innerHTML=rows.length?rows.map(r=>`<div class="rw${OPEN[r.k]?' open':''}" data-k="${E(r.k)}"><div class="rwh">
  <div class="av" style="background:${r.col}">${E((r.name||'?').replace(/^[^\p{L}\p{N}]+/u,'').charAt(0).toUpperCase())}</div>
  <div class="nm" title="${E(r.name)}">${E(r.name)}${r.tag?` <span class="tg${r.tag==='o‘zining'?' g':' y'}">${r.tag}</span>`:''}</div><div class="tm">${r.t}</div></div>
  <div class="bar"><i style="width:${r.secs?Math.max(2,Math.round(100*r.secs/mx)):0}%;background:${r.col}"></i></div>
  ${r.sub&&r.sub.length?`<div class="pg">${r.sub.map(p=>`<div class="pgr"><span>${E(p.name)}</span><b>${p.t}</b></div>`).join('')}</div>`:''}</div>`).join('')
  :'<div class="note2">Bu kunda ma’lumot yo‘q</div>';
 $('#exact').textContent=TAB==='sites'?(D.exact?'Aniq: brauzer skriptidan, faqat faol tab hisoblanadi':'Taxminiy: oyna sarlavhalaridan (skript 5.0 o‘rnatilgach aniq bo‘ladi)'):'';
 $$('#ls .rw').forEach(el=>el.onclick=()=>{OPEN[el.dataset.k]=!OPEN[el.dataset.k];el.classList.toggle('open')})}
function live(){const L=D.live,el=$('#live');if(!D.today){el.hidden=true;return}el.hidden=false;
 if(!L){el.classList.add('off');$('#lvw').innerHTML='<b>Kompyuter oflayn</b>';$('#lvc').textContent='';return}
 el.classList.remove('off');
 let what=L.blocked?'<b>Kompyuter bloklangan</b>':L.locked?'<b>Ekran qulflangan</b>':
  L.site?`<b>${E(L.site.domain)}</b>${L.site.via?` <span class="via">${E(L.site.via)}</span>`:''}<div class="sub ttl">${E(L.site.title)}</div>`+
   (L.site.url?`<div class="lgu"><a href="${E(L.site.url)}" target="_blank" rel="noopener noreferrer">${E(L.site.url.replace(/^https?:\/\//,'').slice(0,90))}</a></div>`:''):`<b>${E(L.app||'—')}</b><div class="sub ttl">${E(L.title)}</div>`;
 if(L.corp)what+=`<div class="sub">${E(L.corp.name)} kabinetida · ${E(L.corp.page)}</div>`;
 if(L.co)what+=`<div class="sub">🏢 <b>${E(L.co.name)}</b> ustida ishlayapti${L.co.mine?' <span class="tg g">o‘z korxonasi</span>':' <span class="tg y">boshqa hodimniki</span>'}</div>`;
 if(L.idle>=180&&!L.blocked)what+=`<div class="sub">harakatsiz ${fmt(L.idle)}</div>`;
 $('#lvw').innerHTML=what;liveBase=L.site?L.site.secs:0;liveAt=Date.now();tick()}
function tick(){if(D&&D.live&&D.live.site)$('#lvc').textContent=clock(liveBase+(Date.now()-liveAt)/1000);else $('#lvc').textContent=''}
async function load(){try{const r=await fetch('/screentime/wb.json?auto=1&u='+U+'&d='+DAY,{credentials:'same-origin',cache:'no-store'});
 D=await r.json();donut();charts();lists();live()}catch(e){}}
$$('.tabs2 button').forEach(b=>b.onclick=()=>{TAB=b.dataset.t;$$('.tabs2 button').forEach(x=>x.classList.toggle('on',x===b));lists()});
addEventListener('resize',()=>D&&charts());
load();setInterval(tick,1000);setInterval(()=>{if(document.visibilityState==='visible'&&D&&D.today)load()},5000);
"""


def render_wb(tg_id, day):
    """Hodimning "Raqamli muvozanat" sahifasi (Pixel Digital Wellbeing kabi, jonli)."""
    d = datetime.strptime(day, "%Y-%m-%d").date()
    today = now().date()
    prev_d, next_d = (d - timedelta(days=1)).isoformat(), min(d + timedelta(days=1), today).isoformat()
    C = 2 * 3.14159265 * 90
    circles = "".join(f'<circle class="s" cx="110" cy="110" r="90" stroke-dasharray="0 {C:.1f}"/>' for _ in range(5))
    pr = q("SELECT id FROM st_pcs WHERE tg_id=? ORDER BY last_seen DESC LIMIT 1", (tg_id,), one=True)
    rec_link = (f'<a class="btn sm ghost" href="/screentime/rec?id={pr["id"]}&d={day}" style="margin-left:auto" '
                f'title="Shu kungi ekran yozuvi">⏪ Ekran yozuvi</a>') if pr else ""
    return f"""<!DOCTYPE html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(emp_name(tg_id))} · Raqamli muvozanat</title>{BRAND_HEAD}
<style>{CSS}{BRAND_CSS}{WB_CSS}</style></head><body><div class="wrap">{brand_nav("screentime")}
<div class="wbh"><div><a class="back" href="/screentime?d={day}">‹ Screen time</a><h1 style="margin-top:6px">{esc(emp_name(tg_id))}
<span class="role r-{role_of(tg_id)}" style="font-size:13px;vertical-align:middle">{ROLES[role_of(tg_id)]}</span></h1>
<div class="sub">Raqamli muvozanat — vaqt nimaga sarflanayotgani</div></div>
<form class="pnav glass" method="get" action="/screentime/hodim"><input type="hidden" name="u" value="{tg_id}">
<a href="/screentime/hodim?u={tg_id}&d={prev_d}" title="Oldingi kun">‹</a>
<input type="date" name="d" value="{day}" max="{today.isoformat()}" onchange="this.form.submit()">
<a href="/screentime/hodim?u={tg_id}&d={next_d}" title="Keyingi kun">›</a></form></div>
<div class="hero glass"><div class="donut"><svg id="dn" viewBox="0 0 220 220">
<circle cx="110" cy="110" r="90" stroke="var(--line)"/>{circles}</svg>
<div class="ctr"><span>Faol vaqt</span><b id="tot">—</b><span class="delta" id="dl" hidden></span></div></div>
<div class="legend" id="lg"></div></div>
<div class="live glass" id="live" hidden><span class="pulse"></span><div class="what" id="lvw"></div><div class="cnt" id="lvc"></div></div>
<div class="g2"><div class="pane glass"><h3>Soatlar bo‘yicha</h3><svg class="chart" id="hc"></svg></div>
<div class="pane glass"><h3>Oxirgi 7 kun</h3><svg class="chart" id="wc"></svg></div></div>
<div class="pane glass" style="margin-top:14px"><div class="lhead"><div class="tabs2"><button class="on" data-t="log">Jurnal</button>
<button data-t="corp">Korxonalar</button><button data-t="sites">Saytlar</button><button data-t="apps">Dasturlar</button></div>
{rec_link}<a class="btn sm ghost" href="/screentime/log.csv?u={tg_id}&d={day}"{' style="margin-left:8px"' if pr else ''}>{ic("download")}Jurnal (Excel)</a></div>
<div class="rows" id="ls"></div><div class="note2" id="exact"></div></div>
</div><script>const U={int(tg_id)},DAY="{day}";{WB_JS}</script></body></html>"""


# ---------------------------------------------------------------- ekran yozuvi pleyeri
REC_CSS = """
.rech{display:flex;align-items:flex-end;justify-content:space-between;gap:14px;flex-wrap:wrap;margin-bottom:14px}
.rech .back{color:var(--mut);text-decoration:none;font-size:14px}.rech .back:hover{color:var(--acc)}
.days{display:flex;gap:6px;flex-wrap:wrap}.days a{padding:7px 14px;border-radius:999px;text-decoration:none;color:var(--ink);
font-size:13px;font-weight:600;border:1px solid var(--line);background:var(--glass)}.days a.on{background:var(--acc);color:#fff;border-color:var(--acc)}
.stage{position:relative;padding:10px;border-radius:22px;overflow:hidden}
.stage img{display:block;width:100%;max-height:72vh;object-fit:contain;border-radius:14px;background:#111;min-height:240px}
.stage .clk{position:absolute;left:24px;top:22px;background:rgba(0,0,0,.62);color:#fff;font:600 22px/1 inherit;
padding:8px 14px;border-radius:12px;font-variant-numeric:tabular-nums;letter-spacing:.02em}
.stage .clk small{font-size:12px;opacity:.75;margin-left:8px;font-weight:500}
.stage .empty{position:absolute;inset:10px;display:grid;place-content:center;text-align:center;color:#ddd;border-radius:14px;
background:#1b1410;font-size:15px;padding:30px}
.stage .empty[hidden]{display:none}
.ctl{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:14px}
.ctl button,.ctl select,.ctl input[type=time]{font:inherit;font-size:14px;border:1px solid var(--line);background:var(--glass);
color:var(--ink);border-radius:12px;height:40px;min-width:44px;padding:0 12px;cursor:pointer}
.ctl button:hover{border-color:var(--acc);color:var(--acc)}.ctl .play{background:var(--acc);color:#fff;border-color:var(--acc);min-width:64px;font-size:16px}
.ctl .play:hover{color:#fff;filter:brightness(1.06)}.ctl .sp{margin-left:auto}
.ctl .live{background:#16a34a;color:#fff;border-color:#16a34a}.ctl .live.off{background:var(--glass);color:var(--ink);border-color:var(--line)}
.tl{position:relative;height:54px;margin-top:16px;border-radius:12px;background:var(--line);cursor:pointer;overflow:hidden;user-select:none}
.tl .sg{position:absolute;top:0;height:30px;opacity:.85}
.tl .fr{position:absolute;top:30px;height:24px;background:rgba(0,0,0,.06)}
.tl .hr{position:absolute;bottom:2px;font-size:10px;color:var(--mut);transform:translateX(-50%);pointer-events:none}
.tl .hl{position:absolute;top:0;bottom:16px;width:1px;background:rgba(0,0,0,.08);pointer-events:none}
.tl .pos{position:absolute;top:0;bottom:0;width:3px;background:var(--acc);box-shadow:0 0 0 3px rgba(249,115,22,.25);pointer-events:none;transition:left .12s}
.tl .hov{position:absolute;top:-2px;transform:translateX(-50%);background:#111;color:#fff;font-size:11px;padding:2px 6px;border-radius:6px;pointer-events:none}
input.scrub{width:100%;margin:10px 0 0;accent-color:#f97316}
.nowact{display:flex;gap:12px;align-items:center;padding:14px 18px;margin-top:14px;flex-wrap:wrap}
.nowact i{width:12px;height:12px;border-radius:4px;flex:none}.nowact b{font-size:15px}.nowact .sub{font-size:13px}
.lgd{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin-top:8px}.lgd span{display:inline-flex;gap:6px;align-items:center}
.lgd i{width:10px;height:10px;border-radius:3px;display:inline-block}
.keys{font-size:12px;color:var(--mut);margin-top:12px}
"""

REC_JS = r"""
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const E=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const COL={mine:'#F28322',work:'#22c55e',neutral:'#60a5fa',distract:'#ef4444',idle:'#d6d3d1'};
const sec=h=>+h.slice(0,2)*3600+ +h.slice(2,4)*60+ +h.slice(4,6), hm=m=>m.slice(0,2)*60+ +m.slice(3,5);
const fmt=h=>h.slice(0,2)+':'+h.slice(2,4)+':'+h.slice(4,6);
let D=null,I=-1,PLAY=null,FOLLOW=false,T0=8*3600,T1=19*3600;
const SPEED={10:1000,30:333,60:167,120:83};
function range(){const t=D.times.map(sec),s=D.segs.map(x=>[hm(x.from)*60,hm(x.to)*60]).flat();
 const all=t.concat(s);T0=Math.min(8*3600,...all);T1=Math.max(19*3600,...all);
 T0=Math.floor(T0/3600)*3600;T1=Math.ceil(T1/3600)*3600}
const X=s=>(s-T0)/(T1-T0)*100;
function drawTL(){const tl=$('#tl');let h='';
 D.segs.forEach(g=>{const a=hm(g.from)*60,b=Math.max(hm(g.to)*60,a+60);
  h+=`<div class="sg" style="left:${X(a)}%;width:${Math.max(.15,X(b)-X(a))}%;background:${COL[g.company&&g.cat!=='idle'?'mine':g.cat]||COL.neutral}" title="${E(g.from+'–'+g.to+' · '+g.label)}"></div>`});
 // kadrlar bor joylar (bo'shliq — kompyuter o'chiq / qulflangan)
 let a=null,p=null;D.times.map(sec).forEach(s=>{if(a===null){a=p=s}else if(s-p>90){h+=`<div class="fr" style="left:${X(a)}%;width:${X(p+10)-X(a)}%;background:rgba(249,115,22,.28)"></div>`;a=p=s}else p=s});
 if(a!==null)h+=`<div class="fr" style="left:${X(a)}%;width:${X(p+10)-X(a)}%;background:rgba(249,115,22,.28)"></div>`;
 for(let t=T0;t<=T1;t+=3600){h+=`<div class="hl" style="left:${X(t)}%"></div><div class="hr" style="left:${X(t)}%">${String(t/3600).padStart(2,'0')}</div>`}
 tl.innerHTML=h+'<div class="pos" id="pos"></div><div class="hov" id="hov" hidden></div>'}
function nearest(s){let lo=0,hi=D.times.length-1;if(hi<0)return -1;const ts=D.times;
 while(lo<hi){const m=(lo+hi)>>1;if(sec(ts[m])<s)lo=m+1;else hi=m}
 return lo>0&&Math.abs(sec(ts[lo-1])-s)<Math.abs(sec(ts[lo])-s)?lo-1:lo}
function segAt(s){return D.segs.find(g=>hm(g.from)*60<=s&&s<Math.max(hm(g.to)*60,hm(g.from)*60+60))}
const src=i=>`/screentime/rec.jpg?id=${PC}&d=${D.day}&t=${D.times[i]}`;
function show(i,keepFollow){if(!D.times.length)return;I=Math.max(0,Math.min(D.times.length-1,i));
 if(!keepFollow&&FOLLOW&&I<D.times.length-1)setFollow(false);
 const h=D.times[I],s=sec(h);$('#fr').src=src(I);$('#clk').innerHTML=fmt(h)+`<small>${D.dayf}</small>`;
 $('#pos').style.left=X(s)+'%';$('#scrub').value=I;$('#tm').value=fmt(h).slice(0,5);
 for(let k=1;k<=3;k++)if(D.times[I+k])(new Image()).src=src(I+k);       // oldindan yuklab qo'yish
 const g=segAt(s),na=$('#na');
 na.innerHTML=g?`<i style="background:${COL[g.company&&g.cat!=='idle'?'mine':g.cat]||COL.neutral}"></i><div><b>${E(g.label)}</b>`+
  (g.via?` <span class="sub">${E(g.via)}</span>`:'')+(g.company?` <span class="tg g">${E(g.company)}</span>`:'')+
  `<div class="sub">${E(g.from)}–${E(g.to)}${g.sub?' · '+E(g.sub):''}</div></div>`:'<div class="sub">Bu vaqtda faoliyat yozuvi yo‘q</div>';
 history.replaceState(null,'','#t='+h)}
function stop(){if(PLAY){clearInterval(PLAY);PLAY=null}$('#pp').textContent='▶'}
function play(){stop();if(I>=D.times.length-1)I=-1;$('#pp').textContent='⏸';
 PLAY=setInterval(()=>{if(I>=D.times.length-1){stop();return}show(I+1)},SPEED[$('#spd').value]||1000)}
function setFollow(on){FOLLOW=on;$('#lv').classList.toggle('off',!on)}
async function load(first){try{const r=await fetch(`/screentime/rec.json?id=${PC}&d=${DAY}`,{credentials:'same-origin',cache:'no-store'});
 const j=await r.json(),grew=D&&j.times.length>D.times.length;D=j;range();drawTL();
 $('#scrub').max=Math.max(0,D.times.length-1);$('#cnt').textContent=D.times.length?`${D.times.length} kadr`:'';
 $('#empty').hidden=!!D.times.length;
 if(first){const m=location.hash.match(/t=(\d{6})/);
  if(m)show(nearest(sec(m[1])));else if(D.times.length){if(D.today)setFollow(true);show(D.times.length-1,true)}}
 else if(grew&&FOLLOW)show(D.times.length-1,true)}catch(e){}}
const tl=$('#tl');
tl.onclick=e=>{const r=tl.getBoundingClientRect(),s=T0+(e.clientX-r.left)/r.width*(T1-T0);stop();show(nearest(s))};
tl.onmousemove=e=>{const r=tl.getBoundingClientRect(),s=T0+(e.clientX-r.left)/r.width*(T1-T0),hv=$('#hov');
 hv.hidden=false;hv.style.left=(e.clientX-r.left)+'px';hv.textContent=String(Math.floor(s/3600)).padStart(2,'0')+':'+String(Math.floor(s%3600/60)).padStart(2,'0')};
tl.onmouseleave=()=>$('#hov').hidden=true;
$('#scrub').oninput=e=>{stop();show(+e.target.value)};
$('#pp').onclick=()=>PLAY?stop():play();
$('#spd').onchange=()=>{if(PLAY)play()};
$('#pv').onclick=()=>{stop();show(I-1)};$('#nx').onclick=()=>{stop();show(I+1)};
$('#bk').onclick=()=>{stop();show(nearest(sec(D.times[I])-60))};$('#fw').onclick=()=>{stop();show(nearest(sec(D.times[I])+60))};
$('#tm').onchange=e=>{const v=e.target.value;if(v){stop();show(nearest(+v.slice(0,2)*3600+ +v.slice(3,5)*60))}};
$('#lv').onclick=()=>{stop();if(!D.today){location.href=`/screentime/rec?id=${PC}`;return}setFollow(true);show(D.times.length-1,true)};
addEventListener('keydown',e=>{if(e.target.tagName==='INPUT'||e.target.tagName==='SELECT')return;
 if(e.key===' '){e.preventDefault();PLAY?stop():play()}
 else if(e.key==='ArrowLeft'){e.preventDefault();stop();e.shiftKey?$('#bk').click():show(I-1)}
 else if(e.key==='ArrowRight'){e.preventDefault();stop();e.shiftKey?$('#fw').click():show(I+1)}});
load(true);setInterval(()=>{if(D&&D.today&&document.visibilityState==='visible')load(false)},15000);
"""


def render_rec(pc_id, idx):
    """Ekran yozuvi sahifasi: kun bo'yicha kadrlar, vaqt chizig'i (faoliyat ranglari bilan), pleyer."""
    day = idx["day"]
    names = {now().date().isoformat(): "Bugun", (now().date() - timedelta(days=1)).isoformat(): "Kecha"}
    days = idx["days"] if day in idx["days"] else [day] + idx["days"]
    tabs = "".join(f'<a href="/screentime/rec?id={pc_id}&d={d}" class="{"on" if d == day else ""}">'
                   f'{names.get(d, fmt_day(d))}</a>' for d in days)
    lgd = "".join(f'<span><i style="background:{WB_COL[k]}"></i>{WB_NAME[k]}</span>' for k in WB_COL)
    back = f"/screentime/hodim?u={idx['tg']}&d={day}" if idx["tg"] else "/screentime"
    return f"""<!DOCTYPE html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{esc(idx['name'])} · Ekran yozuvi</title>{BRAND_HEAD}
<style>{CSS}{BRAND_CSS}{WB_CSS}{REC_CSS}</style></head><body><div class="wrap">{brand_nav("screentime")}
<div class="rech"><div><a class="back" href="{back}">‹ {esc(idx['name'])}</a>
<h1 style="margin-top:6px">Ekran yozuvi</h1>
<div class="sub">{esc(idx['name'])} · {esc(idx['pc'])} · <span id="cnt"></span> · faqat siz ko‘rasiz, {idx['keep']} kundan eskisi o‘zi o‘chadi</div></div>
<div class="days">{tabs}</div></div>
<div class="stage glass"><img id="fr" alt=""><div class="clk" id="clk">—</div>
<div class="empty" id="empty" hidden>Bu kunda yozuv yo‘q.<br><span style="font-size:13px;opacity:.7">Agent 1.9 o‘rnatilgach, kompyuter yoqilib
ekran qulflanmagan paytda har 10 soniyada kadr yoziladi.</span></div></div>
<div class="tl" id="tl" title="Bosing — shu vaqtga o‘tish"></div>
<div class="lgd">{lgd}<span><i style="background:rgba(249,115,22,.28)"></i>Yozuv bor</span></div>
<input type="range" class="scrub" id="scrub" min="0" max="0" value="0">
<div class="ctl">
<button id="bk" title="1 daqiqa orqaga (Shift+←)">−1 daq</button><button id="pv" title="Oldingi kadr (←)">‹</button>
<button class="play" id="pp" title="O‘ynatish / to‘xtatish (Probel)">▶</button>
<button id="nx" title="Keyingi kadr (→)">›</button><button id="fw" title="1 daqiqa oldinga (Shift+→)">+1 daq</button>
<input type="time" id="tm" step="60" title="Vaqtga o‘tish">
<select id="spd" class="sp" title="Tezlik"><option value="10">10×</option><option value="30" selected>30×</option>
<option value="60">60×</option><option value="120">120×</option></select>
<button class="live off" id="lv" title="Eng oxirgi kadr va yangilarini kuzatib borish">● Hozir</button></div>
<div class="nowact glass" id="na"></div>
<div class="keys">Klaviatura: Probel — o‘ynatish/to‘xtatish · ← → — kadr · Shift + ← → — 1 daqiqa</div>
</div><script>const PC={int(pc_id)},DAY="{day}";{REC_JS}</script></body></html>"""


# ---------------------------------------------------------------- korxonalar bo'yicha vaqt
PRESENCE = {}   # tg_id -> (vaqt, inn, korxona, sahifa) — hozir qaysi kabinetda


def api_presence(body):
    """Hodimlar skripti: har 30 soniyada "shu korxona kabinetida, shu sahifada" (faqat faol tab)."""
    e = emp_by_key(body.get("key"))
    if not e:
        return 401, {"error": "bad key"}
    inn = "".join(ch for ch in str(body.get("inn") or "") if ch.isdigit())[:14]
    try:
        secs = max(0, min(int(body.get("secs") or 0), 90))
    except (TypeError, ValueError):
        secs = 0
    if len(inn) not in (9, 14) or not secs:
        return 200, {"ok": True}
    company = " ".join(str(body.get("company") or "").split())[:120]
    page = " ".join(str(body.get("page") or "").split())[:120] or "(sahifa)"
    day = now().date().isoformat()
    q("INSERT INTO st_company(tg_id,day,inn,company,page,secs) VALUES(?,?,?,?,?,?) "
      "ON CONFLICT(tg_id,day,inn,page) DO UPDATE SET secs=secs+excluded.secs, company=excluded.company",
      (e["tg_id"], day, inn, company, page, secs))
    PRESENCE[e["tg_id"]] = (time.time(), inn, company, page)
    n = now()       # jurnal uchun: shu daqiqada qaysi korxona kabinetida
    q("INSERT INTO st_weblog(tg_id,day,hm,domain,title,url,inn,secs) VALUES(?,?,?,'__kabinet__',?,'',?,?) "
      "ON CONFLICT(tg_id,day,hm,domain,title) DO UPDATE SET secs=secs+excluded.secs, inn=excluded.inn",
      (e["tg_id"], n.date().isoformat(), n.strftime("%H:%M"), page, inn, secs))
    return 200, {"ok": True}


_TR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюяўқғҳ",
               ["a", "b", "v", "g", "d", "e", "yo", "j", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s", "t",
                "u", "f", "x", "ts", "ch", "sh", "sh", "", "i", "", "e", "yu", "ya", "o", "q", "g", "h"]))
_STOP = {"mchj", "mchj", "ooo", "oao", "zao", "ao", "xk", "llc", "ltd", "ok", "chp", "xususiy", "korxonasi", "korxona",
         "firma", "firmasi", "qk", "ab", "dk", "tif", "ooo", "mas", "masuliyati", "cheklangan", "jamiyati"}


def norm_name(s):
    """'"RAESTIC" MCHJ' / 'ООО «Раестик»' -> 'raestic' (lotin, kichik harf, shakl so'zlarisiz)."""
    s = "".join(_TR.get(ch, ch) for ch in re.sub(r"[ʻʼ'`‘’´]", "", str(s or "").lower()))   # "ZO‘R" = "ZOR"
    words = [w for w in re.split(r"[^a-z0-9]+", s) if w and w not in _STOP]
    return " ".join(words)


def company_data(tg_id, day):
    """Hodim bu kunda qaysi korxona uchun qancha vaqt sarfladi:
    1) soliq.uz kabineti (skriptdan, aniq INN bo'yicha, sahifalari bilan);
    2) boshqa dasturlar (1C, Excel, Word, Telegram, brauzer) — oyna sarlavhasida korxona nomi yoki INN bo'lsa."""
    comps = {r["inn"]: r["name"] or r["inn"] for r in q("SELECT inn, name FROM companies WHERE COALESCE(active,1)=1")}
    out = {}
    is_mine = mine_checker(tg_id, day)  # o'ziniki / jamoasiniki (Bosh buxgalter) / hammasi (ZAM, Boshliq)

    def slot(inn, name=""):
        return out.setdefault(inn, {"inn": inn, "name": comps.get(inn) or name or inn, "listed": inn in comps,
                                    "mine": is_mine(inn),
                                    "secs": 0, "parts": {}})

    for r in q("SELECT inn, company, page, secs FROM st_company WHERE tg_id=? AND day=?", (tg_id, day)):
        s = slot(r["inn"], r["company"])
        s["secs"] += r["secs"]
        k = ("soliq.uz kabineti", r["page"])
        s["parts"][k] = s["parts"].get(k, 0) + r["secs"]
    keys = sorted(((norm_name(n), inn) for inn, n in comps.items()), key=lambda kv: -len(kv[0]))
    keys = [(k, inn) for k, inn in keys if len(k) >= 4]
    for r in q("SELECT app, title, secs FROM st_usage WHERE tg_id=? AND day=?", (tg_id, day)):
        if classify(r["app"], r["title"]) == "x" or not r["title"]:
            continue
        t = " " + norm_name(r["title"]) + " "
        digits = re.findall(r"\d{9}", r["title"])
        hit = next((inn for inn in digits if inn in comps), None) or \
            next((inn for k, inn in keys if f" {k} " in t), None)
        if not hit:
            continue
        s = slot(hit)
        s["secs"] += r["secs"]
        label = site_of(r["title"]) if (r["app"] or "").lower() in BROWSERS else r["title"]
        k = (app_label(r["app"]), label[:120])
        s["parts"][k] = s["parts"].get(k, 0) + r["secs"]
    for inn, name in comps.items():                 # biriktirilgan, lekin bugun ochilmagan korxonalar ham ko'rinsin
        if inn not in out and is_mine.assigned(inn):
            slot(inn)
    rows = []
    for s in sorted(out.values(), key=lambda v: (-v["secs"], v["name"])):
        rows.append({"inn": s["inn"], "name": s["name"], "mine": s["mine"], "listed": s["listed"], "secs": s["secs"],
                     "t": short(s["secs"]),
                     "parts": [{"src": k[0], "label": k[1], "t": short(v), "secs": v}
                               for k, v in sorted(s["parts"].items(), key=lambda kv: -kv[1])[:10]]})
    mine = sum(r["secs"] for r in rows if r["mine"])
    other = sum(r["secs"] for r in rows if not r["mine"])
    pr = PRESENCE.get(tg_id)
    now_c = None
    if pr and time.time() - pr[0] < 75:
        now_c = {"inn": pr[1], "name": comps.get(pr[1]) or pr[2] or pr[1], "page": pr[3],
                 "mine": is_mine(pr[1])}
    return {"rows": rows, "mine": short(mine), "other": short(other), "now": now_c}


# ---------------------------------------------------------------- jonli ekran (AnyDesk kabi, faqat ko'rish)
LIVE = {}                         # pc_id -> qachongacha ko'rilmoqda (dashboard so'rab turadi)
FRAMES = {}                       # pc_id -> (vaqt, seq, jpeg)
FRAME_COND = threading.Condition()
_SEQ = [0]


def live_wanted(pc_id):
    return LIVE.get(pc_id, 0) > time.time()


def live_needs_start(pc_id):
    return live_wanted(pc_id) and time.time() - FRAMES.get(pc_id, (0,))[0] > 3


def api_frame(headers, raw):
    """Agent POST /api/frame (tanasi — JPEG). -> (kod, javob)."""
    from urllib.parse import unquote
    e = emp_by_key(headers.get("X-Key"))
    if not e:
        return 401, {"error": "bad key"}
    if not raw.startswith(b"\xff\xd8") or len(raw) > 3_000_000:
        return 400, {"error": "bad image"}
    r = q("SELECT id FROM st_pcs WHERE pc=? AND winuser=? AND tg_id=?",
          (unquote(headers.get("X-Pc", ""))[:60], unquote(headers.get("X-User", ""))[:60], e["tg_id"]), one=True)
    if not r:
        return 404, {"error": "pc not found"}
    if not live_wanted(r["id"]):          # hech kim ko'rmayapti — kadr saqlanmaydi, agent to'xtaydi
        FRAMES.pop(r["id"], None)
        return 200, {"live": False}
    with FRAME_COND:
        _SEQ[0] += 1
        FRAMES[r["id"]] = (time.time(), _SEQ[0], raw)   # faqat xotirada, faqat oxirgi kadr; diskka yozilmaydi
        FRAME_COND.notify_all()
    hq = LIVE_HQ.get(r["id"], 0) > time.time()
    # Butun ekranda / boshqaruv oynasida — sifatli va tez; ekranlar devorida — kichik va 2 soniyada bir
    return 200, {"live": True, "interval": 0.35 if hq else 2.0, "w": 1280 if hq else 640, "q": 55 if hq else 45}


LIVE_HQ = {}                      # pc_id -> qachongacha sifatli rejim (butun ekran / boshqaruv oynasi)


# ---------------------------------------------------------------- ekran yozuvi (kuzatuv kamerasi kabi)
# Agent 1.9+ har 10 soniyada kichik JPEG yuboradi -> rec/<pc_id>/<YYYY-MM-DD>/<HHMMSS>.jpg
# Faqat rahbar (owner) ko'radi. REC_DAYS kundan eskisi o'chiriladi (2 = bugun + kecha). Disk to'lsa yozilmaydi.
def rec_dir():
    return os.path.join(BASE, "rec")


def rec_conf():
    return {"on": bool(CFG.get("REC_ENABLED", True)), "days": max(1, int(CFG.get("REC_DAYS", 2))),
            "interval": int(CFG.get("REC_INTERVAL", 10)), "min_free_gb": float(CFG.get("REC_MIN_FREE_GB", 3))}


_REC_FREE = {"t": 0.0, "ok": True}


def _rec_disk_ok():
    if time.time() - _REC_FREE["t"] > 60:
        try:
            st_ = os.statvfs(BASE)
            _REC_FREE["ok"] = st_.f_bavail * st_.f_frsize / 1e9 >= rec_conf()["min_free_gb"]
        except (AttributeError, OSError):
            _REC_FREE["ok"] = True
        _REC_FREE["t"] = time.time()
    return _REC_FREE["ok"]


def api_rec(headers, raw):
    """Agent POST /api/rec (tanasi — JPEG kadr). -> (kod, javob)."""
    from urllib.parse import unquote
    e = emp_by_key(headers.get("X-Key"))
    if not e:
        return 401, {"error": "bad key"}
    c = rec_conf()
    if not c["on"] or not _rec_disk_ok():
        return 200, {"rec": False}
    if not raw.startswith(b"\xff\xd8") or len(raw) > 1_500_000:
        return 400, {"error": "bad image"}
    r = q("SELECT id FROM st_pcs WHERE pc=? AND winuser=? AND tg_id=?",
          (unquote(headers.get("X-Pc", ""))[:60], unquote(headers.get("X-User", ""))[:60], e["tg_id"]), one=True)
    if not r:
        return 404, {"error": "pc not found"}
    t = datetime.now(TZ)                       # server vaqti (hodim soatini o'zgartirsa ham to'g'ri)
    d = os.path.join(rec_dir(), str(r["id"]), t.date().isoformat())
    os.makedirs(d, exist_ok=True)
    tmp = os.path.join(d, f".{t:%H%M%S}.tmp")
    with open(tmp, "wb") as f:
        f.write(raw)
    os.replace(tmp, os.path.join(d, f"{t:%H%M%S}.jpg"))
    return 200, {"rec": True, "interval": c["interval"], "idle_interval": 60, "w": 960, "q": 45}


def rec_cleanup():
    """REC_DAYS kundan eski yozuvlarni o'chiradi (rejalashtiruvchi soatda bir chaqiradi)."""
    import shutil
    keep = {(now().date() - timedelta(days=i)).isoformat() for i in range(rec_conf()["days"])}
    base = rec_dir()
    removed = 0
    if not os.path.isdir(base):
        return 0
    for pid in os.listdir(base):
        pdir = os.path.join(base, pid)
        if not os.path.isdir(pdir):
            continue
        for day in os.listdir(pdir):
            if day not in keep:
                shutil.rmtree(os.path.join(pdir, day), ignore_errors=True)
                removed += 1
    return removed


def rec_days(pc_id):
    p = os.path.join(rec_dir(), str(int(pc_id)))
    return sorted((d for d in os.listdir(p) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d)), reverse=True) \
        if os.path.isdir(p) else []


def rec_index(pc_id, day):
    """Kun yozuvi: kadrlar vaqtlari + o'sha kundagi faoliyat bo'laklari (kadr ostida ko'rsatish uchun)."""
    pr = q("SELECT p.*, e.name ename FROM st_pcs p LEFT JOIN employees e ON e.tg_id=p.tg_id WHERE p.id=?",
           (int(pc_id),), one=True)
    if not pr:
        return None
    p = os.path.join(rec_dir(), str(int(pc_id)), day)
    times = sorted(f[:6] for f in os.listdir(p) if re.fullmatch(r"\d{6}\.jpg", f)) if os.path.isdir(p) else []
    segs = []
    if pr["tg_id"]:
        try:
            segs = [{"from": s["from"], "to": s["to"], "label": s["label"], "via": s.get("via", ""),
                     "sub": " · ".join(s["subs"][:2]), "company": s["company"], "cat": s["cat"]}
                    for s in activity_log(pr["tg_id"], day)["segments"]]
        except Exception:
            log("rec_index segs", traceback.format_exc())
    return {"pc": pr["pc"], "name": pr["ename"] or "?", "tg": pr["tg_id"], "day": day, "dayf": fmt_day(day),
            "days": rec_days(pc_id), "times": times, "segs": segs, "keep": rec_conf()["days"],
            "today": day == now().date().isoformat()}


def rec_frame(pc_id, day, hms):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or "") or not re.fullmatch(r"\d{6}", hms or ""):
        return None
    try:
        with open(os.path.join(rec_dir(), str(int(pc_id)), day, hms + ".jpg"), "rb") as f:
            return f.read()
    except OSError:
        return None


def frame_get(pc_id, after, hq=True):
    """Dashboard: yangi kadrni kutadi (1.5 s gacha). -> (seq, jpeg) yoki None.
    hq=False — ekranlar devori (kichik kadr, 2 soniyada bir)."""
    if hq:
        LIVE_HQ[pc_id] = time.time() + 8
    if not live_wanted(pc_id) or live_needs_start(pc_id):
        LIVE[pc_id] = time.time() + 8
        wake(pc_id)
    LIVE[pc_id] = time.time() + 8          # oyna yopilsa 8 soniyada to'xtaydi
    end = time.time() + 1.5
    with FRAME_COND:
        while True:
            f = FRAMES.get(pc_id)
            if f and f[1] > after and time.time() - f[0] < 10:
                return f[1], f[2]
            left = end - time.time()
            if left <= 0:
                return None
            FRAME_COND.wait(left)


_AGENT_VER = {"mtime": None, "v": ""}


def agent_version():
    """Serverdagi gigant_agent.py versiyasi — agent o'zini yangilash kerakligini shundan biladi."""
    p = os.path.join(BASE, "agent", "gigant_agent.py")
    try:
        mt = os.path.getmtime(p)
        if mt != _AGENT_VER["mtime"]:
            with open(p, encoding="utf-8") as f:
                m = re.search(r'VERSION = "([\d.]+)"', f.read())
            _AGENT_VER.update(mtime=mt, v=m.group(1) if m else "")
    except OSError:
        pass
    return _AGENT_VER["v"]


def _block_state(pc_id):
    r = db.execute("SELECT blocked, block_msg FROM st_pcs WHERE id=?", (pc_id,)).fetchone()
    return {"on": bool(r and r["blocked"]), "msg": (r["block_msg"] if r else "") or DEFAULT_BLOCK_MSG}


def _after(e, pc_id, pc, prev, cur, finished):
    """Rahbarga xabarlar: bajarilgan buyruqlar va blok holati o'zgargani (faqat Telegram'dan berilganlar)."""
    for r in finished:
        if r["notify"] and r["cmd"] in CMD_DONE:
            send(OWNER, f"✅ {esc(e['name'])} ({esc(pc)}): {CMD_DONE[r['cmd']]}")
    # Agent qayta ishga tushdi, lekin Windows o'chib-yonmagan va agent yangilanmagan -> uni kimdir yopgan bo'lishi mumkin
    pb, pa, cb, ca = prev.get("boot") or 0, prev.get("astart") or 0, cur.get("boot") or 0, cur.get("astart") or 0
    if pa and ca and ca != pa and abs(cb - pb) < 180 and prev.get("_ver") == cur.get("ver"):
        if time.time() - TAMPER_SENT.get(pc_id, 0) > 1800:
            TAMPER_SENT[pc_id] = time.time()
            send(OWNER, f"⚠️ <b>{esc(e['name'])}</b> ({esc(pc)}): kompyuter o'chmagan holda agent qayta ishga tushdi — "
                        f"uni Диспетчер задач dan yopishgan yoki tizimdan chiqib-kirishgan bo'lishi mumkin. "
                        f"Agent o'zi qayta yondi, kuzatuv davom etmoqda.")
    if bool(prev.get("blocked")) != cur["blocked"]:
        r = q("SELECT blocked, block_notify FROM st_pcs WHERE id=?", (pc_id,), one=True)
        if r and r["block_notify"] and bool(r["blocked"]) == cur["blocked"]:
            q("UPDATE st_pcs SET block_notify=0 WHERE id=?", (pc_id,))
            send(OWNER, f"{'⛔' if cur['blocked'] else '🔓'} {esc(e['name'])} ({esc(pc)}): "
                        f"{'kompyuter bloklandi' if cur['blocked'] else 'blok ochildi'}")


def api_wait(body, ip):
    """Agentning kutish so'rovi: buyruq yoki blok o'zgarishi bo'lsa darhol, bo'lmasa WAIT_SEC dan keyin javob."""
    e = emp_by_key(body.get("key"))
    if not e:
        return 401, {"error": "bad key"}
    cur = _cur(body)
    pc = str(body.get("pc") or "PC")[:60]
    t_now = time.time()
    with LOCK:
        pc_id, prev = _touch_pc(e, body, ip, cur, t_now)
        cmds, finished = _exchange(e, pc_id, body, t_now)
        block = _block_state(pc_id)
        db.commit()
    _after(e, pc_id, pc, prev, cur, finished)
    if not cmds and block["on"] == cur["blocked"] and not live_needs_start(pc_id):
        ev = EVENTS.setdefault(pc_id, threading.Event())
        ev.clear()
        with LOCK:   # clear() dan keyin qayta tekshiramiz — oraliqda kelgan buyruq yo'qolmasin
            cmds, _ = _exchange(e, pc_id, {}, time.time())
            block = _block_state(pc_id)
            db.commit()
        if not cmds and block["on"] == cur["blocked"] and not live_needs_start(pc_id):
            ev.wait(WAIT_SEC)
            with LOCK:
                cmds, _ = _exchange(e, pc_id, {}, time.time())
                block = _block_state(pc_id)
                db.execute("UPDATE st_pcs SET last_seen=? WHERE id=?", (time.time(), pc_id))
                db.commit()
    return 200, {"ok": True, "block": block, "agent_ver": agent_version(), "live": live_needs_start(pc_id),
                 "cmds": [{"id": c["id"], "cmd": c["cmd"], "arg": c["arg"] or ""} for c in cmds]}


def api_activity(body, ip):
    e = emp_by_key(body.get("key"))
    if not e:
        return 401, {"error": "bad key"}
    pc = str(body.get("pc") or "PC")[:60]
    cur = _cur(body)
    t_now = time.time()
    usage, minutes, urls = [], [], []
    for it in (body.get("items") or [])[:2000]:
        try:
            ts = int(it["t"])
            s = max(0, min(int(it.get("s") or 0), 90))
            a = max(0, min(int(it.get("a") or 0), s))
        except (KeyError, TypeError, ValueError):
            continue
        if not s or ts > t_now + 300 or ts < t_now - 14 * 86400:
            continue
        app = str(it.get("app") or "?")[:80]
        title = str(it.get("title") or "")[:150]
        dt = datetime.fromtimestamp(ts, TZ)
        day, hm = dt.date().isoformat(), dt.strftime("%H:%M")
        usage.append((e["tg_id"], day, app, title, s, a))
        minutes.append((e["tg_id"], day, hm, s, a, app, classify(app, title), s))
        urls.append(clean_url(it.get("u")))
    with LOCK:
        pc_id, prev = _touch_pc(e, body, ip, cur, t_now)
        # Faoliyat jurnali: har daqiqa qaysi dastur/oyna (soat nechadan nechagacha ishlaganini ko'rsatish uchun)
        db.executemany("INSERT INTO st_log(tg_id,day,hm,app,title,secs,active,url) VALUES(?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(tg_id,day,hm,app,title) DO UPDATE SET secs=secs+excluded.secs, "
                       "active=active+excluded.active, url=COALESCE(NULLIF(excluded.url,''),url)",
                       [(u[0], u[1], m[2], u[2], u[3], u[4], u[5], url) for u, m, url in zip(usage, minutes, urls)])
        db.executemany("INSERT INTO st_usage(tg_id,day,app,title,secs,active) VALUES(?,?,?,?,?,?) "
                       "ON CONFLICT(tg_id,day,app,title) DO UPDATE SET secs=secs+excluded.secs, "
                       "active=active+excluded.active", usage)
        db.executemany("INSERT INTO st_minutes(tg_id,day,hm,secs,active,app,cat,app_secs) VALUES(?,?,?,?,?,?,?,?) "
                       "ON CONFLICT(tg_id,day,hm) DO UPDATE SET secs=secs+excluded.secs, active=active+excluded.active, "
                       "app=CASE WHEN excluded.app_secs>st_minutes.app_secs THEN excluded.app ELSE st_minutes.app END, "
                       "cat=CASE WHEN excluded.app_secs>st_minutes.app_secs THEN excluded.cat ELSE st_minutes.cat END, "
                       "app_secs=MAX(st_minutes.app_secs, excluded.app_secs)", minutes)
        cmds, finished = _exchange(e, pc_id, body, t_now)
        block = _block_state(pc_id)
        db.commit()
    _after(e, pc_id, pc, prev, cur, finished)
    return 200, {"ok": True, "block": block, "agent_ver": agent_version(),
                 "cmds": [{"id": c["id"], "cmd": c["cmd"], "arg": c["arg"] or ""} for c in cmds]}


def api_shot(body):
    e = emp_by_key(body.get("key"))
    if not e:
        return 401, {"error": "bad key"}
    r = q("SELECT p.*, e.name ename FROM st_pcs p LEFT JOIN employees e ON e.tg_id=p.tg_id "
          "WHERE p.pc=? AND p.tg_id=? ORDER BY p.last_seen DESC LIMIT 1",
          (str(body.get("pc") or "")[:60], e["tg_id"]), one=True)
    if not r:
        return 404, {"error": "pc not found"}
    try:
        png = base64.b64decode(body.get("png") or "", validate=True)
    except Exception:
        return 400, {"error": "bad image"}
    if not png.startswith(b"\x89PNG") or len(png) > 9_000_000:
        return 400, {"error": "bad image"}
    SHOTS[r["id"]] = (time.time(), png)
    c = q("SELECT notify FROM st_cmds WHERE id=?", (int(body.get("id") or 0),), one=True)
    if not c or c["notify"]:
        send_photo(OWNER, png, f"📸 {pc_title(r)} · {datetime.now(TZ):%d.%m %H:%M}")
    return 200, {"ok": True}


def send_photo(chat, png, caption):
    """Telegram sendPhoto (multipart) — bot.tg() faqat JSON yuboradi."""
    bnd = uuid.uuid4().hex
    parts = []
    for k, v in (("chat_id", str(chat)), ("caption", caption[:1000])):
        parts.append(f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode())
    parts.append(f'--{bnd}\r\nContent-Disposition: form-data; name="photo"; filename="shot.png"\r\n'
                 f'Content-Type: image/png\r\n\r\n'.encode() + png + b"\r\n")
    parts.append(f"--{bnd}--\r\n".encode())
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/sendPhoto", data=b"".join(parts),
                                 headers={"Content-Type": f"multipart/form-data; boundary={bnd}"})
    try:
        urllib.request.urlopen(req, timeout=60).read()
        return True
    except Exception as ex:
        log("SENDPHOTO XATO", ex)
        return False


# ---------------------------------------------------------------- buyruqlar navbati
def wake(pc_id):
    ev = EVENTS.get(pc_id)
    if ev:
        ev.set()


def queue_cmd(pc_id, cmd, arg="", notify=1):
    q("INSERT INTO st_cmds(pc_id,cmd,arg,created,notify) VALUES(?,?,?,?,?)",
      (pc_id, cmd, (arg or "")[:500], time.time(), notify))
    wake(pc_id)


def set_block(pc_id, on, msg="", notify=1):
    """Kompyuter klub bloki: on=True — ekran to'liq yopiladi, rahbar ochmaguncha turadi (o'chirib-yoqilsa ham)."""
    q("UPDATE st_pcs SET blocked=?, block_msg=?, block_notify=? WHERE id=?",
      (1 if on else 0, (msg or "").strip()[:300] or None, notify, pc_id))
    wake(pc_id)


def block_all(on, msg="", notify=1):
    n = 0
    for r in pcs():
        if pc_online(r) or not on:
            set_block(r["id"], on, msg, notify)
            n += 1
    return n


def queue_all(cmd, arg="", notify=1, online_only=True):
    n = 0
    for r in pcs():
        if not online_only or pc_online(r):
            queue_cmd(r["id"], cmd, arg, notify)
            n += 1
    return n


# ---------------------------------------------------------------- statistika
_FIX = {}          # (tg_id, day) -> (vaqt, fix) — o'tgan kunlar o'zgarmaydi, bugungisi 60 soniya keshlanadi


def mine_fix(tg_id, day):
    """O'z korxonasi ishi (Telegram, bank, statistika...) — "ish" ga qo'shiladigan soniyalar."""
    k = (tg_id, day)
    c = _FIX.get(k)
    if c and (day != now().date().isoformat() or time.time() - c[0] < 60):
        return c[1]
    try:
        f = activity_log(tg_id, day)["fix"]
    except Exception:
        log("mine_fix", traceback.format_exc())
        f = {"prod": 0, "unprod": 0, "mine": 0}
    if len(_FIX) > 5000:
        _FIX.clear()
    _FIX[k] = (time.time(), f)
    return f


def day_stats(day, tg_id=None):
    """Har hodim uchun bir kunlik screen time.
    "Ish" (prod) = ish dasturlari/saytlari + o'ziga biriktirilgan korxona ustidagi har qanday ish (mine_fix)."""
    rows = q("SELECT * FROM st_usage WHERE day=?" + (" AND tg_id=?" if tg_id else ""),
             (day, tg_id) if tg_id else (day,))
    out = {}
    for r in rows:
        s = out.setdefault(r["tg_id"], {"tg_id": r["tg_id"], "on": 0, "active": 0, "prod": 0, "unprod": 0,
                                        "locked": 0, "apps": {}, "sites": {}, "bad": {}})
        cat = classify(r["app"], r["title"])
        if cat == "x":
            s["locked"] += r["secs"]
            continue
        s["on"] += r["secs"]
        s["active"] += r["active"]
        if cat == "p":
            s["prod"] += r["active"]
        elif cat == "u":
            s["unprod"] += r["active"]
            k = site_of(r["title"]) if r["app"].lower() in BROWSERS else app_label(r["app"])
            s["bad"][k] = s["bad"].get(k, 0) + r["secs"]
        lab = app_label(r["app"])
        a = s["apps"].setdefault(lab, [0, cat, {}])
        a[0] += r["secs"]
        a[2][cat] = a[2].get(cat, 0) + r["secs"]
        a[1] = max(a[2], key=a[2].get)   # dastur rangi — vaqtining ko'p qismi qaysi turga tushgan
        if r["app"].lower() in BROWSERS:
            site = site_of(r["title"])
            sv = s["sites"].setdefault(site, [0, cat])
            sv[0] += r["secs"]
    span = q("SELECT tg_id, MIN(hm) a, MAX(hm) b FROM st_minutes WHERE day=? AND active>0 GROUP BY tg_id", (day,))
    spans = {r["tg_id"]: (r["a"], r["b"]) for r in span}
    for s in out.values():
        s["idle"] = max(0, s["on"] - s["active"])
        f = mine_fix(s["tg_id"], day)
        s["prod"] = min(s["active"], s["prod"] + f["prod"])
        s["unprod"] = max(0, s["unprod"] - f["unprod"])
        s["mine"] = min(s["active"], f["mine"])          # o'z korxonalari ustida faol ishlagan vaqt
        s["eff"] = round(100 * s["prod"] / s["active"]) if s["active"] >= 300 else None
        s["span"] = spans.get(s["tg_id"])
        s["name"] = emp_name(s["tg_id"])
    return out


def top(d, n):
    return sorted(d.items(), key=lambda kv: -(kv[1][0] if isinstance(kv[1], list) else kv[1]))[:n]


# ---------------------------------------------------------------- Telegram matnlari
def fmt_day(day):
    return datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")


def summary_text(day):
    st = day_stats(day)
    online = {}
    for r in pcs():
        online.setdefault(r["tg_id"], []).append(r)
    lines = [f"🖥 <b>Screen time · {fmt_day(day)}</b>"]
    emps = active_employees()
    if not st and not any(online.values()):
        return lines[0] + "\n\nHali ma'lumot yo'q. Hodimlar kompyuteriga agent o'rnating: /agent"
    quiet = []
    for e in sorted(emps, key=lambda e: -(st.get(e["tg_id"]) or {}).get("active", 0)):
        s = st.get(e["tg_id"])
        prs = online.get(e["tg_id"], [])
        if not s:
            quiet.append(f"• {esc(e['name'])}" + ("" if prs else " <i>(agent yo'q)</i>"))
            continue
        state = " · ".join(f"{pc_state(r)[0]} {esc(pc_state(r)[1])}" for r in prs) or "⚪ agent yo'q"
        lines += ["", f"<b>{esc(e['name'])}</b>", f"      {state}",
                  f"      ⏱ Faol <b>{dur(s['active'])}</b> · 💤 {dur(s['idle'])}",
                  f"      ✅ Ish ulushi {'—' if s['eff'] is None else str(s['eff']) + '%'}"
                  + (f" · ❌ Chalg'ish {dur(s['unprod'])}" if s["unprod"] >= 60 else "")]
        if s.get("mine", 0) >= 60:
            lines.append(f"      🏢 O'z korxonalari ishi: <b>{dur(s['mine'])}</b>")
        if s["span"]:
            lines.append(f"      🕘 {s['span'][0]} – {s['span'][1]}")
        apps = " · ".join(f"{esc(k)} {short(v[0])}" for k, v in top(s["apps"], 3))
        if apps:
            lines.append(f"      📊 {apps}")
    if quiet:
        lines += ["", f"<b>⚪ Bugun faollik yo'q — {len(quiet)}</b>"] + quiet
    lines += ["", "Batafsil: /screen TG_ID", "Boshqaruv: /pc · Jonli: /onlayn"]
    return "\n".join(lines)


def detail_text(tg_id, day):
    s = day_stats(day, tg_id).get(tg_id)
    name = emp_name(tg_id)
    if not s:
        return f"🖥 <b>{esc(name)}</b> — {fmt_day(day)}\nBu kunda faollik yo'q."
    eff = "—" if s["eff"] is None else f"{s['eff']}%"
    icon = {"p": "✅", "u": "❌", "n": "▫️"}
    lines = [f"🖥 <b>{esc(name)}</b>", f"🗓 {fmt_day(day)}", "",
             f"⏱ Kompyuterda: {dur(s['on'])}",
             f"⚡ Faol: <b>{dur(s['active'])}</b>",
             f"💤 Harakatsiz: {dur(s['idle'])}",
             f"✅ Ish: {dur(s['prod'])} ({eff})",
             f"🏢 O'z korxonalari ishi: {dur(s.get('mine', 0))} <i>(Telegram, bank, soliq — qayerda bo'lsa ham)</i>",
             f"❌ Chalg'ish: {dur(s['unprod'])}"]
    if s["locked"] >= 60:
        lines.append(f"🔒 Qulflangan: {dur(s['locked'])}")
    if s["span"]:
        lines.append(f"🕘 {s['span'][0]} – {s['span'][1]}")
    cd = company_data(tg_id, day)
    if cd["rows"]:
        lines.append(f"\n<b>🏢 Korxonalar</b> (o'ziniki {cd['mine']}, boshqalarniki {cd['other']})")
        lines += [f"{'✅' if r['mine'] else '⚠️'} {esc(r['name'])} — {dur(r['secs']) if r['secs'] else 'ochilmagan'}"
                  for r in cd["rows"][:12]]
    lines.append("\n<b>Dasturlar</b>")
    lines += [f"{icon.get(v[1], '▫️')} {esc(k)} — {dur(v[0])}" for k, v in top(s["apps"], 12)]
    if s["sites"]:
        lines.append("\n<b>Brauzerda (sahifalar)</b>")
        lines += [f"{icon.get(v[1], '▫️')} {esc(k[:70])} — {dur(v[0])}" for k, v in top(s["sites"], 10)]
    if s["bad"]:
        lines.append("\n<b>❌ Chalg'ituvchi</b>")
        lines += [f"• {esc(k[:70])} — {dur(v)}" for k, v in top(s["bad"], 8)]
    return "\n".join(lines)


def _seen(ts):
    """Oxirgi ko'rinish: bugun bo'lsa '19:35', bo'lmasa '26.11 19:35'."""
    d = datetime.fromtimestamp(ts, TZ)
    return f"{d:%H:%M}" if d.date() == now().date() else f"{d:%d.%m %H:%M}"


def online_text():
    """Kompyuterlar hozir: holat bo'yicha guruhlar (ishlayapti / harakatsiz / qulflangan / bloklangan / oflayn)."""
    rows = pcs()
    if not rows:
        return "Hali birorta kompyuterga agent o'rnatilmagan. /agent — o'rnatish yo'riqnomasi"
    many = {}
    for r in rows:
        many[r["tg_id"]] = many.get(r["tg_id"], 0) + 1
    groups = {k: [] for k in ("work", "idle", "lock", "block", "wait", "off")}
    for r in rows:
        em, txt = pc_state(r)
        n = pc_now(r)
        name = f"<b>{esc(r['ename'] or '?')}</b>" + (f" <i>({esc(r['pc'])})</i>" if many.get(r["tg_id"], 0) > 1 else "")
        if em == "⚫":
            groups["off"].append((r["last_seen"] or 0, f"• {name} — {_seen(r['last_seen']) + ' dan' if r['last_seen'] else 'hech ulanmagan'}"))
        elif em == "⛔":
            groups["block"].append((0, f"• {name}"))
        elif em == "⏳":
            groups["wait"].append((0, f"• {name} — {esc(txt)}"))
        elif em == "🔒":
            groups["lock"].append((0, f"• {name}"))
        elif em == "🟡":
            groups["idle"].append((-n.get("idle", 0), f"• {name} — {dur(n.get('idle', 0))}"))
        else:
            line = f"• {name} — {esc(txt)}"
            t = site_of(n.get("title", ""))[:70] if n.get("title") else ""
            if t and t != "(sarlavhasiz)":
                line += f"\n      <i>{esc(t)}</i>"
            groups["work"].append((0, line))
    heads = {"work": "🟢 Ishlayapti", "idle": "🟡 Harakatsiz", "lock": "🔒 Ekran qulflangan", "block": "⛔ Bloklangan",
             "wait": "⏳ Buyruq bajarilmoqda", "off": "⚫ Oflayn"}
    lines = [f"📡 <b>Kompyuterlar hozir</b> · {now():%H:%M}",
             f"Onlayn: {len(rows) - len(groups['off'])} / {len(rows)}"]
    for k, items in groups.items():
        if items:
            items.sort(key=lambda x: x[0], reverse=(k == "off"))
            lines += ["", f"<b>{heads[k]} — {len(items)}</b>"] + [x[1] for x in items]
    return "\n".join(lines)


def agent_text(temporary=False):
    url = current_url()
    tail = "\n\n⏳ Bu xabar 10 daqiqadan keyin o'chadi." if temporary else ""
    return (f"🧩 <b>Hodim kompyuteriga agent o'rnatish</b>\n\n"
            f"1. Hodim kompyuterida <b>PowerShell</b> ni <b>administrator</b> sifatida oching "
            f"(Пуск → PowerShell → o'ng tugma → «Запуск от имени администратора»).\n"
            f"2. Quyidagi buyruqni qo'ying va Enter bosing:\n"
            f"<code>irm {url}/agent/install.ps1 | iex</code>\n"
            f"3. So'ralganda hodim kalitini kiriting (hodim botga /key yozsa chiqadi).\n\n"
            f"Agent har yoqilganda o'zi ishga tushadi va yopib qo'yilsa 1 daqiqada qayta yonadi.\n"
            + (tail.strip() if temporary else
               "⚠️ Hodimlarni kuzatuv haqida yozma ogohlantiring (buyruq/shartnomaga band)."))


# ---------------------------------------------------------------- Telegram buyruqlari
def pc_menu_rows():
    rows = [[(f"{pc_state(r)[0]} {pc_title(r)}"[:60], f"pc:{r['id']}")] for r in pcs()]
    rows.append([("⛔ Hammasini bloklash", "pcb:1:all"), ("🔓 Hammasini ochish", "pcb:0:all")])
    rows.append([("📸 Hammasidan skrinshot", "pca:shot"), ("💬 Hammaga xabar", "pcm:all")])
    rows.append([("⏻ Hammasini o'chirish", "pcq:shutdown:all")])
    return rows


def handle_command(cmd, args):
    """Owner buyrug'i shu modulga tegishli bo'lsa bajaradi va True qaytaradi."""
    if cmd in ("/screen", "/screentime"):
        a = args[0] if args else ""
        if a.isdigit() and emp(int(a), active_only=False):
            day = parse_day(args[1] if len(args) > 1 else "")
            send(OWNER, detail_text(int(a), day or now().date().isoformat()))
        else:
            day = parse_day(a)
            if day is None:
                send(OWNER, "Sana noto'g'ri. Masalan: /screen 25.11.2026 yoki /screen TG_ID")
            else:
                send(OWNER, summary_text(day))
        return True
    if cmd in ("/onlayn", "/online"):
        send(OWNER, online_text())
        return True
    if cmd in ("/pc", "/kompyuter"):
        if not pcs():
            send(OWNER, agent_text())
        else:
            send_kb(OWNER, "🖥 <b>Qaysi kompyuterni boshqaramiz?</b>", pc_menu_rows())
        return True
    if cmd == "/agent":
        send(OWNER, agent_text())
        return True
    return False


def handle_callback(data):
    """Tugma bosilganda (faqat owner). Tegishli bo'lsa True."""
    p = data.split(":")
    if p[0] == "pc" and len(p) == 2 and p[1].isdigit():
        r = q("SELECT p.*, e.name ename FROM st_pcs p LEFT JOIN employees e ON e.tg_id=p.tg_id WHERE p.id=?",
              (int(p[1]),), one=True)
        if not r:
            return send(OWNER, "Kompyuter topilmadi.") or True
        em, txt = pc_state(r)
        i = r["id"]
        blk = ("🔓 Blokni ochish", f"pcb:0:{i}") if r["blocked"] else ("⛔ Bloklash", f"pcb:1:{i}")
        send_kb(OWNER, f"🖥 <b>{esc(pc_title(r))}</b>\nHolat: {em} {esc(txt)}",
                [[blk, ("📸 Skrinshot", f"pcc:shot:{i}")],
                 [("🔑 Windows qulfi", f"pcc:lock:{i}")],
                 [("💬 Xabar yuborish", f"pcm:{i}"), ("🚪 Chiqarish", f"pcq:logoff:{i}")],
                 [("⏻ O'chirish", f"pcq:shutdown:{i}"), ("🔄 Qayta yoqish", f"pcq:restart:{i}")],
                 [("✋ O'chirishni bekor qilish", f"pcc:cancel:{i}")],
                 [("📊 Bugungi screen time", f"pcs:{r['tg_id']}")]])
        return True
    if p[0] == "pcq" and len(p) == 3 and p[1] in CMD_LABEL:
        who = "HAMMA onlayn kompyuterlarni" if p[2] == "all" else "shu kompyuterni"
        send_kb(OWNER, f"⚠️ Rostdan ham {who}: <b>{CMD_LABEL[p[1]]}</b>?\n"
                       f"(Hodim ekranida 60 soniya ogohlantirish chiqadi, saqlanmagan ishlar yo'qolishi mumkin.)",
                [[("✅ Ha", f"pcc:{p[1]}:{p[2]}"), ("❌ Yo'q", "cancel")]])
        return True
    if p[0] == "pcc" and len(p) == 3 and p[1] in CMD_LABEL and p[1] != "msg":
        if p[2] == "all":
            n = queue_all(p[1])
            return send(OWNER, f"⏳ {CMD_LABEL[p[1]]} — {n} ta onlayn kompyuterga yuborildi (30 soniya ichida).") or True
        if not p[2].isdigit():
            return True
        r = q("SELECT * FROM st_pcs WHERE id=?", (int(p[2]),), one=True)
        if not r:
            return send(OWNER, "Kompyuter topilmadi.") or True
        queue_cmd(r["id"], p[1])
        send(OWNER, f"⏳ {CMD_LABEL[p[1]]}: buyruq yuborildi, bir necha soniyada bajariladi."
             + ("" if pc_online(r) else "\n⚠️ Kompyuter hozir oflayn — 10 daqiqa ichida yoqilsa bajariladi."))
        return True
    if p[0] == "pcb" and len(p) == 3 and p[1] in ("0", "1"):
        on = p[1] == "1"
        if p[2] == "all":
            n = block_all(on)
            return send(OWNER, f"{'⛔ Bloklash' if on else '🔓 Ochish'} — {n} ta kompyuterga yuborildi.") or True
        if not p[2].isdigit() or not q("SELECT 1 FROM st_pcs WHERE id=?", (int(p[2]),), one=True):
            return send(OWNER, "Kompyuter topilmadi.") or True
        set_block(int(p[2]), on)
        r = q("SELECT * FROM st_pcs WHERE id=?", (int(p[2]),), one=True)
        send(OWNER, f"⏳ {'Bloklash' if on else 'Ochish'} buyrug'i yuborildi (bir necha soniyada bajariladi)."
             + ("" if pc_online(r) else "\n⚠️ Kompyuter hozir oflayn — yoqilganda bajariladi."))
        return True
    if p[0] == "pca" and len(p) == 2 and p[1] in ("lock", "shot"):
        n = queue_all(p[1])
        send(OWNER, f"⏳ {CMD_LABEL[p[1]]} — {n} ta onlayn kompyuterga yuborildi.")
        return True
    if p[0] == "pcm" and len(p) == 2:
        PENDING.clear()
        PENDING["pcmsg"] = p[1]
        send_kb(OWNER, "💬 Hodim ekranida chiqadigan xabar matnini yozing:", [[("❌ Bekor qilish", "cancel")]])
        return True
    if p[0] == "pcs" and len(p) == 2 and p[1].isdigit():
        send(OWNER, detail_text(int(p[1]), now().date().isoformat()))
        return True
    return False


def finish_msg(target, text):
    text = text.strip()[:500]
    if target == "all":
        n = queue_all("msg", text)
        return send(OWNER, f"💬 Xabar {n} ta onlayn kompyuterga yuborildi.")
    if target.isdigit():
        queue_cmd(int(target), "msg", text)
        send(OWNER, "💬 Xabar yuborildi, 30 soniya ichida hodim ekranida chiqadi.")


HELP = """
<b>Kompyuterlar (screen time)</b>
/screen [sana] — kim qaysi dasturda qancha vaqt o'tkazdi
/screen TG_ID [sana] — bitta hodim batafsil (dasturlar, saytlar)
/onlayn — hozir kim kompyuterda, nima qilyapti
/pc — ⛔ bloklash (kompyuter klub kabi) / ochish, o'chirish, xabar, skrinshot
/agent — hodim kompyuteriga agent o'rnatish"""


# ---------------------------------------------------------------- rejalashtiruvchi
def tick(n):
    d = n.date()
    # Jonli ekran kadrlari va skrinshotlar xotirada uzoq qolmasin
    t = time.time()
    for pid in [k for k, v in FRAMES.items() if t - v[0] > 30]:
        FRAMES.pop(pid, None)
    for pid in [k for k, v in SHOTS.items() if t - v[0] > 3600]:
        SHOTS.pop(pid, None)
    if is_workday(d) and n >= at(d, WORK_END) + timedelta(minutes=20) and once(f"st_daily:{d}"):
        if q("SELECT 1 FROM st_usage WHERE day=? LIMIT 1", (d.isoformat(),), one=True):
            send(OWNER, summary_text(d.isoformat()))
    if n.hour == 4 and once(f"st_clean:{d}"):
        keep = (d - timedelta(days=int(CFG.get("ST_KEEP_MINUTES_DAYS", 90)))).isoformat()
        q("DELETE FROM st_minutes WHERE day<?", (keep,))
        q("DELETE FROM st_log WHERE day<?", (keep,))
        q("DELETE FROM st_weblog WHERE day<?", (keep,))
        q("DELETE FROM st_usage WHERE day<?", ((d - timedelta(days=int(CFG.get("ST_KEEP_DAYS", 400)))).isoformat(),))
        q("DELETE FROM st_cmds WHERE created<?", (time.time() - 30 * 86400,))


# ---------------------------------------------------------------- agent fayllari
def agent_file(name):
    """/agent/install.ps1, uninstall.ps1 (joriy manzil bilan) yoki gigant_agent.py. (bytes, content-type) yoki None."""
    adir = os.path.join(BASE, "agent")
    if name in ("install.ps1", "uninstall.ps1"):
        try:
            with open(os.path.join(adir, name), encoding="utf-8") as f:
                ps = f.read()
        except OSError:
            return None
        ps = (ps.replace("__SERVER_URL__", current_url()).replace("__DISC_URL__", DISC_URL)
              .replace("__DISC_KEY__", DISC_SECRET))
        return ps.encode("utf-8"), "text/plain; charset=utf-8"
    if name in ("gigant_agent.py", "update.ps1"):
        try:
            with open(os.path.join(adir, name), "rb") as f:
                return f.read(), "text/plain; charset=utf-8"
        except OSError:
            return None
    return None


_BOT_USER = {}


def bot_username():
    if "u" not in _BOT_USER:
        me = tg("getMe") or {}
        if me.get("username"):
            _BOT_USER["u"] = me["username"]
    return _BOT_USER.get("u") or CFG.get("BOT_USERNAME", "")


# ---------------------------------------------------------------- dashboard
CSS = """:root{--ink:#2b1a0f;--mut:#97755e;--acc:#f97316;--acc-soft:rgba(249,115,22,.12);
--glass:rgba(255,255,255,.52);--edge:rgba(255,255,255,.85);--line:rgba(194,65,12,.10);--shadow:rgba(194,65,12,.10);
--bg1:#fff8f1;--bg2:#ffe9d6;--b1:rgba(251,146,60,.55);--b2:rgba(253,186,116,.60);--field:rgba(255,255,255,.7);
--g:#15803d;--g-bg:rgba(34,197,94,.14);--y:#c2410c;--y-bg:rgba(251,146,60,.18);--r:#b91c1c;--r-bg:rgba(239,68,68,.13);
--c-p:#22c55e;--c-n:#fb923c;--c-u:#ef4444;--c-i:rgba(151,117,94,.35);--c-x:rgba(151,117,94,.15)}
@media (prefers-color-scheme:dark){:root{--ink:#fbeee4;--mut:#c19c84;--acc:#fb923c;--acc-soft:rgba(251,146,60,.16);
--glass:rgba(48,28,16,.50);--edge:rgba(255,255,255,.10);--line:rgba(251,146,60,.14);--shadow:rgba(0,0,0,.35);
--bg1:#160c06;--bg2:#24130a;--b1:rgba(234,88,12,.40);--b2:rgba(251,146,60,.22);--field:rgba(0,0,0,.25);
--g:#4ade80;--g-bg:rgba(74,222,128,.14);--y:#fdba74;--r:#fca5a5;--r-bg:rgba(248,113,113,.15);
--c-i:rgba(193,156,132,.35);--c-x:rgba(193,156,132,.14)}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;font:14px/1.5 "Segoe UI",system-ui,sans-serif;color:var(--ink);
background:radial-gradient(900px 520px at 8% -8%,var(--b1),transparent 62%),radial-gradient(760px 480px at 105% 6%,var(--b2),transparent 60%),
linear-gradient(170deg,var(--bg1),var(--bg2));background-attachment:fixed}
.wrap{max-width:1180px;margin:0 auto;padding:40px 24px 56px}
.glass{background:var(--glass);border:1px solid var(--edge);border-radius:22px;backdrop-filter:blur(22px) saturate(170%);
-webkit-backdrop-filter:blur(22px) saturate(170%);box-shadow:0 10px 40px -12px var(--shadow),inset 0 1px 0 var(--edge)}
header{display:flex;align-items:flex-end;justify-content:space-between;gap:16px;flex-wrap:wrap}
h1{font-size:26px;font-weight:600;letter-spacing:-.02em;margin:0;display:flex;align-items:center;gap:12px}
h1:before{content:"";width:12px;height:12px;border-radius:50%;background:var(--acc);box-shadow:0 0 0 6px var(--acc-soft)}
h2{font-size:13px;font-weight:600;text-transform:uppercase;letter-spacing:.08em;color:var(--mut);margin:36px 4px 12px}
h3{margin:0;font-size:17px;font-weight:600}
.mut,.sub{color:var(--mut)}.sub{font-size:12px}
.hright{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.btn{font:inherit;font-weight:600;font-size:13px;border:0;border-radius:999px;padding:9px 16px;cursor:pointer;
background:var(--acc);color:#fff;box-shadow:0 6px 18px -6px var(--acc);text-decoration:none;display:inline-block}
.btn.ghost{background:var(--acc-soft);color:var(--acc);box-shadow:none}
.btn.sm{padding:5px 11px;font-size:12px;box-shadow:none}.btn.red{background:var(--r)}
.pnav{display:flex;align-items:center;gap:4px;padding:4px;border-radius:999px}
.pnav a{text-decoration:none;color:var(--acc);font-weight:700;padding:4px 12px;border-radius:999px}
.pnav input{font:inherit;border:0;background:transparent;color:var(--ink)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:14px;margin-top:28px}
.kpi{padding:20px 22px}.kpi span{font-size:13px}
.kpi b{display:block;font-size:32px;font-weight:600;letter-spacing:-.03em;margin-top:6px;color:var(--acc)}
.card{overflow:auto}table{border-collapse:collapse;width:100%}
th,td{padding:12px 16px;border-bottom:1px solid var(--line);text-align:left;vertical-align:middle}
th{font-size:11px;color:var(--mut);font-weight:600;text-transform:uppercase;letter-spacing:.06em;white-space:nowrap}
tr:last-child td{border-bottom:0}
.pill{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:600;font-size:12px;background:var(--acc-soft);color:var(--mut);white-space:nowrap}
.pill.g{background:var(--g-bg);color:var(--g)}.pill.y{background:var(--y-bg);color:var(--y)}.pill.r{background:var(--r-bg);color:var(--r)}
.acts{display:flex;gap:6px;flex-wrap:wrap}.acts form{margin:0}
.ttl{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px}
.team{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(540px,100%),1fr));gap:14px}
.member{padding:20px 22px}
.mhead{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}
.stats{display:flex;gap:18px;flex-wrap:wrap;margin:14px 0 10px;font-size:13px}.stats b{font-size:16px}
.tl{width:100%;height:22px;display:block;border-radius:6px;background:var(--c-x)}
.tlx{display:flex;justify-content:space-between;font-size:10.5px;color:var(--mut);margin-top:3px}
.apps{margin-top:14px;display:grid;gap:7px}
.app{display:grid;grid-template-columns:minmax(0,1fr) 64px;gap:10px;align-items:center;font-size:13px}
.bar{height:7px;border-radius:99px;background:var(--line);margin-top:3px;overflow:hidden}.bar i{display:block;height:100%;border-radius:99px}
.lg{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--mut);margin-top:12px}
.lg i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:5px;vertical-align:-1px}
.flash{margin-top:20px;padding:12px 18px;font-weight:600}
.shot{display:block;max-width:100%;border-radius:12px;margin-top:10px;border:1px solid var(--line)}
details summary{cursor:pointer;color:var(--acc);font-weight:600;font-size:12.5px;margin-top:10px}
/* --- Ekranlar devori --- */
.wall{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(300px,100%),1fr));gap:14px}
.wt-card{padding:8px;border-radius:18px!important}.wt-card.blk{outline:2px solid var(--r)}
.scr{position:relative;aspect-ratio:16/9;border-radius:12px;overflow:hidden;background:#141218;cursor:zoom-in}
.scr img{width:100%;height:100%;object-fit:cover;display:block;opacity:0;transition:opacity .4s}
.wt-card.on .scr img{opacity:1}
.scr .off{position:absolute;inset:0;display:grid;place-items:center;color:#a8a29e;font-size:13px}
.wt-card.on .scr .off{display:none}.wt-card.offl .scr{cursor:default}
.scr .st{position:absolute;left:8px;bottom:8px;display:inline-flex;align-items:center;gap:6px;max-width:calc(100% - 16px);
padding:3px 9px;border-radius:999px;background:rgba(20,18,24,.72);color:#fff;font-size:11.5px;backdrop-filter:blur(6px)}
.scr .st em{font-style:normal;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.scr .st.g .sd{color:#4ade80}.scr .st.y .sd{color:#fbbf24}.scr .st.r .sd{color:#f87171}.scr .st .sd{color:#a8a29e}
.wt{display:flex;align-items:center;gap:8px;padding:10px 4px 2px}
.wn{flex:1;min-width:0}.wn b{font-size:15px;font-weight:500}.wn .ttl{max-width:100%}
/* --- Butun ekran --- */
#fs{position:fixed;inset:0;z-index:100;background:#0b0a0e;display:flex;flex-direction:column}
#fs[hidden]{display:none}
.fsbar{display:flex;align-items:center;gap:10px;padding:10px 14px;color:#f4eee8}
.fsbar b{font-size:16px;font-weight:500}.fsbar .sub{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#a8a29e}
.fsbar .ibtn{background:rgba(255,255,255,.08);border-color:rgba(255,255,255,.14);color:#f4eee8}
#fsImg{flex:1;min-height:0;width:100%;object-fit:contain;opacity:0;transition:opacity .3s}#fs.on #fsImg{opacity:1}
.fswait{position:absolute;inset:60px 0 0;display:grid;place-items:center;color:#a8a29e}#fs.on .fswait{display:none}
/* --- Statistika --- */
.stbox{padding:18px 20px}.sthead{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:12px}
.stexp{margin-left:auto;display:flex;gap:8px}
.sttab td,.sttab th{padding:11px 12px;white-space:nowrap}.sttab td .ibtn{width:30px;height:30px}
.sttab td.ra{display:flex;gap:6px;justify-content:flex-end}
.tabs2{display:inline-flex;gap:4px;padding:4px;border-radius:12px;background:var(--line)}
.tabs2 button{font:inherit;font-size:14px;font-weight:500;border:0;background:none;color:var(--mut);padding:6px 16px;border-radius:9px;cursor:pointer;transition:background .2s,color .2s}
.tabs2 button.on{background:var(--solid);color:var(--ink);box-shadow:0 1px 3px rgba(0,0,0,.08)}
.mlink{color:inherit;text-decoration:none}.mlink:hover{color:var(--acc)}.mlink span{color:var(--mut);font-weight:400}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(260px,100%),1fr));gap:12px}
.tile{font:inherit;color:inherit;text-align:left;cursor:pointer;padding:16px 18px;display:flex;flex-direction:column;gap:6px;
transition:transform .12s,box-shadow .12s}
.tile:hover{transform:translateY(-2px);box-shadow:0 14px 40px -12px var(--shadow),0 0 0 2px var(--acc)}
.tile.off{opacity:.62}.tile.blk{box-shadow:0 0 0 2px var(--r),0 10px 40px -12px var(--shadow)}
.th{display:flex;justify-content:space-between;gap:8px;align-items:flex-start}.th b{font-size:15px}
.tile .now{font-size:13px;font-weight:600;min-height:38px}.tile .ttl{max-width:100%;font-weight:400}
.tf{display:flex;gap:12px;font-size:12px;color:var(--mut);align-items:center}.tf b{color:var(--ink)}
.tf .go{margin-left:auto;color:var(--acc);font-weight:600}
dialog{border:1px solid var(--edge);border-radius:24px;padding:24px;width:min(620px,calc(100vw - 24px));color:var(--ink);
background:var(--bg1);box-shadow:0 30px 80px -20px var(--shadow)}
dialog::backdrop{background:rgba(40,20,5,.35);backdrop-filter:blur(3px)}
.mh{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
.x{border:0;background:var(--acc-soft);color:var(--acc);width:34px;height:34px;border-radius:50%;font-size:20px;cursor:pointer}
.state{margin:14px 0;padding:12px 16px;border-radius:14px;font-weight:600;font-size:15px;background:var(--acc-soft)}
.state.g{background:var(--g-bg);color:var(--g)}.state.y{background:var(--y-bg);color:var(--y)}.state.r{background:var(--r-bg);color:var(--r)}
.grid4{display:grid;grid-template-columns:repeat(4,1fr);gap:10px 14px}.grid4 b{display:block;font-size:16px}
.grid4 .wide2{grid-column:1/-1}.grid4 .ttl{max-width:100%}
.blockbox{display:flex;flex-direction:column;gap:8px;margin-top:18px}
.blockbox input{font:inherit;padding:10px 14px;border-radius:12px;border:1px solid var(--line);background:var(--field);color:var(--ink)}
.btn.big{padding:16px;font-size:17px;border-radius:16px;width:100%}
.btn.stop{background:#dc2626;box-shadow:0 8px 24px -8px #dc2626}.btn.ok{background:#16a34a;box-shadow:0 8px 24px -8px #16a34a}
.mt{margin-top:12px}.toast{margin-top:12px;padding:10px 14px;border-radius:12px;background:var(--acc-soft);font-weight:600;font-size:13px}
dialog#dPc{width:min(880px,calc(100vw - 24px));max-height:calc(100vh - 32px);overflow:auto}
.cby{white-space:normal;font-size:13px}.cby span{white-space:nowrap}.cby .oth{color:var(--y);font-weight:600}
.corp{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-top:10px;padding:9px 13px;border-radius:14px;
background:var(--acc-soft);font-size:13.5px;font-weight:400}.corp .ic{color:var(--acc);margin:0}
.tg{display:inline-block;padding:2px 9px;border-radius:999px;font-size:11.5px;font-weight:600;background:var(--line);color:var(--mut)}
.tg.g{background:var(--g-bg);color:var(--g)}.tg.y{background:var(--y-bg);color:var(--y)}
.csum{display:flex;gap:18px;flex-wrap:wrap;font-size:13.5px;color:var(--mut);margin:2px 2px 10px}.csum b{color:var(--ink)}
.tcorp{display:flex;align-items:center;gap:6px;margin-top:6px;font-size:12.5px;font-weight:600;color:var(--g)}
.tcorp.oth{color:var(--y)}.tcorp .ic{margin:0}.tcorp span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.lvh{display:flex;align-items:center;gap:10px;margin-top:22px}.lvh b{display:flex;align-items:center;font-size:16px}
.lvh .btn{margin-left:auto}
.lv{position:relative;margin-top:10px;border-radius:18px;overflow:hidden;background:#0e0c10;aspect-ratio:16/9;
border:1px solid var(--line);box-shadow:0 18px 40px -24px rgba(0,0,0,.5)}
.lv img{width:100%;height:100%;object-fit:contain;display:block;opacity:0;transition:opacity .35s}
.lv.on img{opacity:1}
.lvbadge{position:absolute;top:12px;left:12px;display:none;align-items:center;gap:6px;padding:4px 10px;border-radius:999px;
background:rgba(220,38,38,.92);color:#fff;font-size:11.5px;font-weight:600;letter-spacing:.06em}
.lv.on .lvbadge{display:inline-flex}.lvbadge .sd{animation:blink 1.4s infinite}
@keyframes blink{50%{opacity:.25}}
.lvwait{position:absolute;inset:0;display:grid;place-items:center;color:#cfc4bb;font-size:14px}
.lv.on .lvwait{display:none}
.lv:fullscreen{border-radius:0;border:0;aspect-ratio:auto}
.detail{margin-top:22px;padding-top:18px;border-top:1px solid var(--line)}
.dh{display:flex;gap:8px;align-items:baseline;flex-wrap:wrap;font-size:15px}
.dsum{display:flex;gap:16px;flex-wrap:wrap;font-size:13px;color:var(--mut);margin:10px 0 12px}.dsum b{color:var(--ink)}
.seg{display:inline-flex;gap:4px;padding:4px;border-radius:999px;background:var(--acc-soft);margin:16px 0 10px}
.seg button{font:inherit;font-size:13px;font-weight:600;border:0;background:none;color:var(--acc);padding:6px 14px;border-radius:999px;cursor:pointer}
.seg button.on{background:var(--acc);color:#fff}
.dlist{display:grid;gap:4px;margin-bottom:10px}
.row{border-radius:12px;padding:8px 10px}.row:hover,.row.open{background:var(--acc-soft)}
.rl{display:grid;grid-template-columns:10px minmax(0,1fr) auto 52px;gap:8px;align-items:center;cursor:pointer;font-size:13.5px}
.rl i,.ti i{width:10px;height:10px;border-radius:3px;display:inline-block}
.rl .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:600}
.rl .ct{font-size:11px;color:var(--mut)}.rl b{text-align:right}
.row .bar{margin:5px 0 0 18px}
.more{display:none;margin:8px 0 2px 18px}.row.open .more{display:block}
.ti{display:grid;grid-template-columns:10px minmax(0,1fr) 46px;gap:8px;align-items:center;font-size:12.5px;padding:2px 0}
.ti span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.ti b{text-align:right;font-weight:500;color:var(--mut)}
.pk{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-top:8px}
.pk button{font:inherit;font-size:12px;font-weight:600;border:1px solid var(--line);background:var(--field);color:var(--ink);
padding:5px 10px;border-radius:999px;cursor:pointer}.pk button.on{background:var(--acc);color:#fff;border-color:var(--acc)}
@media (max-width:600px){.grid4{grid-template-columns:1fr 1fr}}
@media (max-width:600px){.wrap{padding:24px 16px 40px}h1{font-size:21px}}"""

CAT_COLOR = {"p": "var(--c-p)", "n": "var(--c-n)", "u": "var(--c-u)", "i": "var(--c-i)", "x": "var(--c-x)"}
FLASH = {"cmd_ok": "✅ Buyruq yuborildi — kompyuter bir necha soniyada bajaradi",
         "shot_ok": "📸 Skrinshot so'raldi — hodimni bosib boshqaruv oynasida ko'ring",
         "cmd_bad": "⚠️ Buyruq noto'g'ri", "pc_none": "⚠️ Kompyuter topilmadi"}


def timeline_svg(tg_id, day, h0=7, h1=21):
    rows = q("SELECT hm, secs, active, app, cat FROM st_minutes WHERE tg_id=? AND day=?", (tg_id, day))
    span = (h1 - h0) * 60
    rects = []
    for r in rows:
        h, m = map(int, r["hm"].split(":"))
        x = (h - h0) * 60 + m
        if not 0 <= x < span:
            continue
        cat = r["cat"] or "n"
        if cat != "x":
            cat = RULES["app"].get((r["app"] or "").lower(), cat)   # keyin belgilangan tur eski kunlarga ham
        if cat != "x" and r["active"] < 10:
            cat = "i"
        tip = f"{r['hm']} · {app_label(r['app'])}" + (" (harakatsiz)" if cat == "i" else "")
        rects.append(f'<rect x="{x}" y="0" width="1.05" height="22" fill="{CAT_COLOR[cat]}"><title>{esc(tip)}</title></rect>')
    ticks = "".join(f"<span>{h:02d}</span>" for h in range(h0, h1 + 1, 2))
    return (f'<svg class="tl" viewBox="0 0 {span} 22" preserveAspectRatio="none" role="img" '
            f'aria-label="Kun davomida faollik">{"".join(rects)}</svg><div class="tlx">{ticks}</div>')


def render_page(day, flash=""):
    day = day or now().date().isoformat()
    d = datetime.strptime(day, "%Y-%m-%d").date()
    today = now().date()
    st = day_stats(day)
    allpcs = pcs()
    online = [r for r in allpcs if pc_online(r)]
    act = [s["active"] for s in st.values()]
    effs = [s["eff"] for s in st.values() if s["eff"] is not None]
    unprod = sum(s["unprod"] for s in st.values())

    def btn(pc_id, cmd, label, cls="ghost", confirm=""):
        js = f' onsubmit="return confirm(\'{confirm}\')"' if confirm else ""
        return (f'<form method="post" action="/dashboard/pc"{js}><input type="hidden" name="csrf" value="{CSRF}">'
                f'<input type="hidden" name="pc" value="{pc_id}"><input type="hidden" name="cmd" value="{cmd}">'
                f'<input type="hidden" name="d" value="{day}"><button class="btn sm {cls}">{iconize(label)}</button></form>')

    with_pc = {r["tg_id"] for r in allpcs}
    no_agent = [e["name"] for e in active_employees() if e["tg_id"] not in with_pc]
    pcs_html = (f'<div class="wall" id="tiles"></div>'
                f'<div class="acts" style="margin-top:14px">{btn("all", "block", "⛔ Hammasini bloklash", "ghost", "HAMMA onlayn kompyuterlar bloklansinmi?")}'
                f'{btn("all", "unblock", "🔓 Hammasini ochish")}</div>'
                if allpcs else '<div class="glass" style="padding:20px">Hali agent o‘rnatilmagan. '
                               'Telegram botga <b>/agent</b> yozing — o‘rnatish yo‘riqnomasi keladi.</div>')
    if allpcs and no_agent:
        pcs_html += (f'<p class="sub" style="margin:10px 4px 0">Agent o‘rnatilmagan: {esc(", ".join(no_agent))} '
                     f'(Telegram botda /agent)</p>')
    init = json.dumps(live_data(), ensure_ascii=False).replace("<", "\\u003c")

    cards = []
    order = sorted(active_employees(), key=lambda e: -(st.get(e["tg_id"], {}).get("active", 0)))
    for e in order:
        s = st.get(e["tg_id"])
        if not s:
            continue
        eff = s["eff"]
        pcls = "" if eff is None else ("g" if eff >= 70 else ("y" if eff >= 45 else "r"))
        mx = max([v[0] for v in s["apps"].values()] + [1])
        apps = "".join(
            f'<div class="app"><div>{esc(k)}<div class="bar"><i style="width:{max(2, round(100 * v[0] / mx))}%;'
            f'background:{CAT_COLOR[v[1]]}"></i></div></div><div class="sub" style="text-align:right">{short(v[0])}</div></div>'
            for k, v in top(s["apps"], 8))
        sites = "".join(f'<div class="app"><div class="ttl" title="{esc(k)}">'
                        f'<i style="display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px;'
                        f'background:{CAT_COLOR[v[1]]}"></i>{esc(k)}</div><div class="sub" style="text-align:right">{short(v[0])}</div></div>'
                        for k, v in top(s["sites"], 12))
        cards.append(f"""<div class="member glass"><div class="mhead"><div><h3><a class="mlink" href="/screentime/hodim?u={e['tg_id']}&d={day}">{esc(s['name'])} <span>→</span></a></h3>
<div class="sub">{f"{s['span'][0]} – {s['span'][1]}" if s['span'] else ''}</div></div>
<span class="pill {pcls}">{'—' if eff is None else f'Ish: {eff}%'}</span></div>
<div class="stats"><div><span class="sub">Faol</span><br><b>{short(s['active'])}</b></div>
<div><span class="sub">Harakatsiz</span><br><b>{short(s['idle'])}</b></div>
<div><span class="sub">Ish dasturlari</span><br><b style="color:var(--g)">{short(s['prod'])}</b></div>
<div><span class="sub">Chalg‘ish</span><br><b style="color:var(--r)">{short(s['unprod'])}</b></div></div>
{timeline_svg(e['tg_id'], day)}
<div class="apps">{apps}</div>
{f'<details><summary>Brauzerda ochilgan sahifalar</summary><div class="apps">{sites}</div></details>' if sites else ''}</div>""")
    # Korxonalar bo'yicha: har korxonaga bu kunda kim qancha vaqt sarfladi
    corp = {}
    for e in active_employees():
        for r in company_data(e["tg_id"], day)["rows"]:
            c = corp.setdefault(r["inn"], {"name": r["name"], "inn": r["inn"], "total": 0, "by": []})
            if r["secs"]:
                c["total"] += r["secs"]
                c["by"].append((e["name"], r["secs"], r["mine"]))
    for c in q("SELECT inn, name FROM companies WHERE COALESCE(active,1)=1"):
        corp.setdefault(c["inn"], {"name": c["name"] or c["inn"], "inn": c["inn"], "total": 0, "by": []})
    cmx = max([c["total"] for c in corp.values()] + [1])
    corp_rows = []
    for c in sorted(corp.values(), key=lambda v: (-v["total"], v["name"])):
        resp = responsible(c["inn"], day)
        by = " · ".join(f'<span class="{"" if mine else "oth"}">{esc(n)} {short(s)}</span>'
                        for n, s, mine in sorted(c["by"], key=lambda x: -x[1])) or '<span class="sub">bu kunda ochilmagan</span>'
        corp_rows.append(
            f'<tr><td><b>{esc(c["name"])}</b><div class="sub">{esc(c["inn"])}</div></td>'
            f'<td>{esc(emp_name(resp)) if resp else "<span class=sub>biriktirilmagan</span>"}</td>'
            f'<td style="min-width:180px"><b>{short(c["total"]) if c["total"] else "—"}</b>'
            f'<div class="bar"><i style="width:{round(100 * c["total"] / cmx) if c["total"] else 0}%;background:var(--c-p)"></i></div></td>'
            f'<td class="cby">{by}</td></tr>')
    corp_html = (f'<div class="card glass"><table><thead><tr><th>Korxona</th><th>Mas’ul hodim</th><th>Sarflangan vaqt</th>'
                 f'<th>Kim ishladi</th></tr></thead><tbody>{"".join(corp_rows)}</tbody></table></div>'
                 f'<p class="sub" style="margin:8px 4px 0">Vaqt soliq.uz kabinetidan (hodimlar skripti, INN bo‘yicha) va 1C, Excel, Word, '
                 f'Telegram oynalari nomidagi korxona nomidan hisoblanadi. To‘q sariq — boshqa hodimning korxonasi.</p>') if corp else ""
    legend = ('<div class="lg"><span><i style="background:var(--c-p)"></i>Ish dasturi</span>'
              '<span><i style="background:var(--c-n)"></i>Boshqa</span><span><i style="background:var(--c-u)"></i>Chalg‘ituvchi</span>'
              '<span><i style="background:var(--c-i)"></i>Harakatsiz</span><span><i style="background:var(--c-x)"></i>Kompyuter yo‘q / qulflangan</span></div>')
    prev_d, next_d = (d - timedelta(days=1)).isoformat(), min(d + timedelta(days=1), today).isoformat()
    flash_html = f'<div class="flash glass">{iconize(FLASH[flash])}</div>' if flash in FLASH else ""
    refresh = ('<script>setInterval(function(){if(!document.querySelector("dialog[open]")&&!document.fullscreenElement'
               '&&document.getElementById("fs").hidden)'
               'location.replace(location.pathname+"?d=' + day + '&auto=1")},120000)</script>') if d == today else ""
    avg_act = short(sum(act) / len(act)) if act else "—"
    return f"""<!DOCTYPE html><html lang="uz"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Screen time · Gigant Buxgalter</title>{BRAND_HEAD}
<style>{CSS}{BRAND_CSS}dialog{{background:var(--dlg)!important}}</style></head>
<body><div class="wrap">{brand_nav("screentime")}<header><h1>Screen time</h1><div class="hright">
<form class="pnav glass" method="get" action="/screentime"><a href="/screentime?d={prev_d}" title="Oldingi kun">‹</a>
<input type="date" name="d" value="{day}" max="{today.isoformat()}" onchange="this.form.submit()">
<a href="/screentime?d={next_d}" title="Keyingi kun">›</a></form></div></header>{flash_html}
<div class="kpis"><div class="kpi glass"><span class="mut">Hozir onlayn</span><b>{len(online)} / {len(allpcs)}</b></div>
<div class="kpi glass"><span class="mut">O‘rtacha faol vaqt</span><b>{avg_act}</b></div>
<div class="kpi glass"><span class="mut">O‘rtacha ish ulushi</span><b>{f"{round(sum(effs) / len(effs))}%" if effs else "—"}</b></div>
<div class="kpi glass"><span class="mut">Jami chalg‘ish</span><b>{short(unprod)}</b></div></div>
<h2>Ekranlar <span class="sub" style="font-weight:400;margin-left:6px">jonli · ekranni bosing — butun ekran</span></h2>{pcs_html}
<h2>Statistika</h2>
<div class="glass stbox"><div class="sthead"><div class="tabs2" id="stTabs"><button class="on" data-k="week">Hafta</button><button data-k="month">Oy</button></div>
<div class="pnav glass"><a href="#" id="stPrev" title="Oldingi">‹</a><span id="stLabel" style="padding:0 8px;font-weight:500"></span><a href="#" id="stNext" title="Keyingi">›</a></div>
<div class="stexp"><a class="btn sm ghost" id="stCsv">{ic("download")}Excel</a><a class="btn sm ghost" id="stCsvD">{ic("download")}Kunlik batafsil</a></div></div>
<div class="card"><table class="sttab"><thead><tr><th>Hodim</th><th>Kunlar</th><th>Faol jami</th><th>Kunlik o‘rtacha</th>
<th>Ish ulushi</th><th>Chalg‘ish</th><th>O‘z korxonalari</th><th>Boshqa korxonalar</th><th>Boshlash · tugatish</th><th></th></tr></thead>
<tbody id="stBody"><tr><td colspan="10" class="sub">Yuklanmoqda…</td></tr></tbody></table></div></div>
<h2>Korxonalar bo‘yicha vaqt — {d:%d.%m.%Y}</h2>{corp_html}
<h2>Hodimlar — {d:%d.%m.%Y}</h2>{legend}
<div class="team" style="margin-top:12px">{''.join(cards) or '<div class="glass" style="padding:20px">Bu kunda ma’lumot yo‘q.</div>'}</div>
</div>
<div id="fs" hidden><div class="fsbar"><b id="fsName"></b><span class="sub" id="fsSt"></span>
<a class="ibtn" id="fsStat" title="Statistika">{ic("chart")}</a><button class="ibtn" id="fsMng" title="Boshqarish">{ic("lock")}</button>
<button class="ibtn" id="fsX" title="Yopish (Esc)">{ic("xcircle")}</button></div><img id="fsImg" alt="Hodim ekrani"><div class="fswait" id="fsWait">Ekran ulanmoqda…</div></div>
<dialog id="dPc"><div class="mh"><div><h3 id="mName"></h3><div class="sub" id="mPc"></div></div>
<button type="button" class="x" data-close aria-label="Yopish">×</button></div>
<div id="mState" class="state"></div>
<div class="grid4"><div class="wide2"><span class="sub">Hozir ochiq</span><b id="mApp"></b><div class="sub ttl" id="mTitle"></div>
<div class="corp" id="mCorp" hidden></div></div>
<div><span class="sub">Bugun faol</span><b id="mAct"></b></div><div><span class="sub">Ish ulushi</span><b id="mEff"></b></div>
<div><span class="sub">Ish dasturlari</span><b id="mProd" style="color:var(--g)"></b></div><div><span class="sub">Chalg‘ish</span><b id="mUn" style="color:var(--r)"></b></div></div>
<div class="blockbox"><input id="mMsg" maxlength="300" placeholder="Ekranda chiqadigan matn (ixtiyoriy), masalan: Ish vaqti tugadi">
<button type="button" class="btn big" id="mBlock"></button></div>
<div class="acts mt"><button type="button" class="btn sm ghost" data-cmd="msg">{ic("message")}Hodimga xabar yuborish</button></div>
<div id="mToast" class="toast" hidden></div>
<div class="lvh"><b>{ic("desktop")}Jonli ekran</b><span class="sub" id="lvSt">Ulanmoqda…</span>
<a class="btn sm ghost" id="lvRec" href="/screentime/rec" title="Kun davomidagi ekran yozuvi — orqaga qaytarib ko‘rish">⏪ Yozuv</a>
<button type="button" class="btn sm ghost" id="lvFull">Butun ekranga</button></div>
<div class="lv" id="lv"><img id="lvImg" alt="Hodim ekrani"><span class="lvbadge"><i class="sd"></i>JONLI</span>
<div class="lvwait" id="lvWait">Ekran ulanmoqda…</div></div>
<div class="sub mt" id="mSeen"></div>
<div class="detail"><div class="dh"><b>{ic("clock")}Vaqt nimaga sarflandi</b><span class="sub" id="dDay"></span>
<a class="btn sm ghost" id="dFull" style="margin-left:auto">{ic("chart")}To‘liq statistika</a></div>
<div class="dsum" id="dSum"></div><div id="dTl"></div>{legend}
<div class="seg"><button type="button" class="on" data-tab="corp">Korxonalar</button><button type="button" data-tab="apps">Dasturlar</button><button type="button" data-tab="sites">Saytlar (brauzer)</button></div>
<div id="dList" class="dlist"></div>
<p class="sub">Qatorni bosing — ichida nima ochilgani ko‘rinadi va uni <b>ish</b>, <b>boshqa</b> yoki <b>chalg‘ish</b> deb belgilash mumkin
(belgi hamma hodimlarga va o‘tgan kunlarga ham qo‘llanadi).</p></div></dialog>
<script>const CSRF="{CSRF}",DAY="{day}",TODAY="{today.isoformat()}",IC={IC_JS};let DATA={init};{LIVE_JS}</script>{refresh}</body></html>"""


def corp_now(tg_id):
    """Hozir qaysi korxona kabinetida, qaysi sahifada (oxirgi 75 soniya ichida signal bo'lsa)."""
    pr = PRESENCE.get(tg_id)
    if not pr or time.time() - pr[0] > 75:
        return None
    c = q("SELECT name FROM companies WHERE inn=?", (pr[1],), one=True)
    return {"name": (c and c["name"]) or pr[2] or pr[1], "page": pr[3],
            "mine": mine_checker(tg_id, now().date().isoformat())(pr[1])}


def live_data():
    """Jonli plitkalar va boshqaruv oynasi uchun: har kompyuter holati + bugungi screen time."""
    st = day_stats(now().date().isoformat())
    out = []
    for r in pcs():
        em, txt = pc_state(r)
        n = pc_now(r)
        on = bool(pc_online(r))
        s = st.get(r["tg_id"]) or {}
        shot = SHOTS.get(r["id"])
        cls = {"🟢": "g", "🟡": "y", "🔒": "y", "⛔": "r", "⏳": "y"}.get(em, "")
        seen = datetime.fromtimestamp(r["last_seen"], TZ).strftime("%d.%m %H:%M:%S") if r["last_seen"] else "—"
        out.append({"id": r["id"], "tg": r["tg_id"], "name": r["ename"] or "?", "pc": r["pc"], "user": r["winuser"] or "",
                    "em": em, "txt": txt, "cls": cls, "online": on, "blocked": bool(r["blocked"]),
                    "msg": r["block_msg"] or "", "app": app_label(n.get("app")) if on and n.get("app") else "",
                    "title": site_of(n.get("title", "")) if on and n.get("title") else "",
                    "seen": seen, "ver": r["ver"] or "", "shot": int(shot[0]) if shot else 0,
                    "act": short(s.get("active", 0)), "prod": short(s.get("prod", 0)),
                    "unprod": short(s.get("unprod", 0)), "eff": s.get("eff"), "corp": corp_now(r["tg_id"])})
    return out


def detail_data(tg_id, day):
    """Bitta hodimning bir kunlik vaqt taqsimoti: dasturlar (ichida oyna/fayl nomlari), saytlar, vaqt chizig'i."""
    s = day_stats(day, tg_id).get(tg_id) or {}
    apps, sites = {}, {}
    for r in q("SELECT app, title, secs, active FROM st_usage WHERE tg_id=? AND day=?", (tg_id, day)):
        cat = classify(r["app"], r["title"])
        if cat == "x":
            continue
        a_low = (r["app"] or "").lower()
        lab = app_label(r["app"])
        a = apps.setdefault(lab, {"label": lab, "key": a_low, "secs": 0, "active": 0, "cats": {}, "titles": {}})
        a["secs"] += r["secs"]
        a["active"] += r["active"]
        a["cats"][cat] = a["cats"].get(cat, 0) + r["secs"]
        if a_low in BROWSERS:
            site = site_of(r["title"])
            sv = sites.setdefault(site, {"label": site, "key": rule_key(r["app"], r["title"]), "secs": 0, "cat": cat})
            sv["secs"] += r["secs"]
        else:
            t = r["title"] or "(sarlavhasiz)"
            tv = a["titles"].setdefault(t, [0, cat])
            tv[0] += r["secs"]
    out_apps = []
    for a in sorted(apps.values(), key=lambda v: -v["secs"])[:30]:
        out_apps.append({"label": a["label"], "key": a["key"], "secs": a["secs"], "t": short(a["secs"]),
                         "cat": max(a["cats"], key=a["cats"].get), "rule": RULES["app"].get(a["key"]),
                         "browser": a["key"] in BROWSERS,
                         "titles": [{"label": k[:120], "t": short(v[0]), "secs": v[0], "cat": v[1]}
                                    for k, v in sorted(a["titles"].items(), key=lambda kv: -kv[1][0])[:8]]})
    rules_t = dict(RULES["title"])
    out_sites = [{"label": v["label"][:140], "key": v["key"], "secs": v["secs"], "t": short(v["secs"]), "cat": v["cat"],
                  "rule": rules_t.get(v["key"])}
                 for v in sorted(sites.values(), key=lambda v: -v["secs"])[:40]]
    return {"name": emp_name(tg_id), "day": day, "dayf": fmt_day(day),
            "on": short(s.get("on", 0)), "active": short(s.get("active", 0)), "idle": short(s.get("idle", 0)),
            "prod": short(s.get("prod", 0)), "unprod": short(s.get("unprod", 0)), "eff": s.get("eff"),
            "span": s.get("span"), "timeline": timeline_svg(tg_id, day), "apps": out_apps, "sites": out_sites,
            "companies": company_data(tg_id, day)}


def do_cmd(f):
    """Dashboard buyrug'i. -> (natija kodi, matn)."""
    cmd, pc, arg = f.get("cmd", ""), f.get("pc", ""), f.get("arg", "")
    if cmd not in CMD_LABEL and cmd not in ("block", "unblock"):
        return "cmd_bad", "Buyruq noto‘g‘ri"
    if cmd == "msg" and not arg.strip():
        return "cmd_bad", "Xabar matni bo‘sh"
    if pc == "all":
        if cmd in ("block", "unblock"):
            n = block_all(cmd == "block", arg, notify=0)
        else:
            n = queue_all(cmd, arg, notify=0)
        log("DASHBOARD PC all", cmd)
        return "cmd_ok", f"{n} ta kompyuterga yuborildi"
    if not (pc.isdigit() and q("SELECT 1 FROM st_pcs WHERE id=?", (int(pc),), one=True)):
        return "pc_none", "Kompyuter topilmadi"
    if cmd in ("block", "unblock"):
        set_block(int(pc), cmd == "block", arg, notify=0)
    else:
        queue_cmd(int(pc), cmd, arg, notify=0)
    log("DASHBOARD PC", pc, cmd)
    done = {"block": "⛔ Bloklash yuborildi", "unblock": "🔓 Ochish yuborildi",
            "shot": "📸 Skrinshot so‘raldi — bir necha soniyada shu yerda chiqadi"}
    return ("shot_ok" if cmd == "shot" else "cmd_ok"), done.get(cmd, f"{CMD_LABEL.get(cmd, cmd)} — yuborildi")


def dashboard_post(f):
    """POST /dashboard/pc — redirect manzilini qaytaradi."""
    day = f.get("d", "")
    day = day if re.match(r"^\d{4}-\d{2}-\d{2}$", day) else ""
    back = "/screentime?" + (f"d={day}&" if day else "")
    return back + "m=" + do_cmd(f)[0]


def dashboard_json(f):
    """POST /dashboard/pc (ajax=1) — boshqaruv oynasidan."""
    if f.get("cmd") == "rule":
        ok = set_rule(f.get("arg", ""), f.get("cat", ""))
        log("DASHBOARD ST RULE", f.get("arg", "")[:60], f.get("cat", ""))
        return {"ok": ok, "text": "Saqlandi — hisob qayta hisoblandi" if ok else "Noto‘g‘ri"}
    code, text = do_cmd(f)
    return {"ok": code in ("cmd_ok", "shot_ok"), "text": text}


LIVE_JS = r"""
const $=s=>document.querySelector(s),$$=s=>[...document.querySelectorAll(s)];
const E=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const dlg=$('#dPc');let cur=null,lastShot={},timer=null;
// ---- Ekranlar devori: har kompyuter — jonli kichik ekran (2 soniyada bir kadr) ----
const WALL={};   // id -> {el, seq, run}
function tileHTML(p){return `<div class="scr" data-act="fs"><img alt=""><div class="off">${p.online?'Ekran ulanmoqda…':'Oflayn'}</div>
<span class="st ${p.cls}"><i class="sd"></i><em></em></span></div>
<div class="wt"><div class="wn"><b></b><div class="sub ttl wa"></div></div>
<a class="ibtn" data-act="stat" title="Statistika">${IC.chart}</a><button type="button" class="ibtn" data-act="mng" title="Boshqarish">${IC.lock}</button></div>`}
function updTile(w,p){const el=w.el;el.classList.toggle('blk',p.blocked);el.classList.toggle('offl',!p.online);
 el.querySelector('.wn b').textContent=p.name;el.querySelector('.st').className='st '+p.cls;el.querySelector('.st em').textContent=p.txt;
 el.querySelector('.wa').textContent=p.online?((p.corp?p.corp.name+' · ':'')+(p.title||p.app||'')):'oflayn';
 el.querySelector('[data-act=stat]').href='/screentime/hodim?u='+p.tg+'&d='+DAY;
 if(!p.online){el.querySelector('.off').textContent='Oflayn';el.classList.remove('on')}
 if(p.online&&!w.run&&DAY===TODAY)wallLoop(p.id)}
async function wallLoop(id){const w=WALL[id];if(!w||w.run)return;w.run=true;const im=w.el.querySelector('img');
 while(WALL[id]===w){const p=DATA.find(x=>x.id===id);if(!p||!p.online)break;
  if(dlg.open||FS.id!=null||document.visibilityState!=='visible'){await sleep(1500);continue}
  if(p.ver&&!(parseFloat(p.ver)>=1.4)){w.el.querySelector('.off').textContent='Agent yangilanishi kerak';await sleep(8000);continue}
  try{const r=await fetch('/screentime/frame?auto=1&wall=1&id='+id+'&after='+w.seq,{credentials:'same-origin',cache:'no-store'});
   if(r.status===200){w.seq=+r.headers.get('X-Seq')||w.seq;const u=URL.createObjectURL(await r.blob());const old=im.src;
    im.onload=()=>{if(old.startsWith('blob:'))URL.revokeObjectURL(old)};im.src=u;w.el.classList.add('on');await sleep(1200)}
   else if(r.status!==204)await sleep(3000)}catch(e){await sleep(3000)}}
 w.run=false}
function draw(){const t=$('#tiles');if(t){const ids=new Set(DATA.map(p=>p.id));
  for(const id in WALL)if(!ids.has(+id)){WALL[id].el.remove();delete WALL[id]}
  DATA.forEach(p=>{let w=WALL[p.id];if(!w){const el=document.createElement('div');el.className='wt-card glass';el.innerHTML=tileHTML(p);
   el.addEventListener('click',ev=>{const a=ev.target.closest('[data-act]');if(!a)return;
    if(a.dataset.act==='fs')openFs(p.id);else if(a.dataset.act==='mng'){ev.preventDefault();openPc(p.id)}});
   t.appendChild(el);w=WALL[p.id]={el,seq:0,run:false}}updTile(w,p)})}
 if(cur!=null&&dlg.open)fill();if(FS.id!=null)fsInfo()}
// ---- Butun ekran: bitta hodim ekrani, sifatli va tez ----
const FS={id:null,run:0};
function fsInfo(){const p=DATA.find(x=>x.id===FS.id);if(!p)return;$('#fsName').textContent=p.name;
 $('#fsSt').textContent=p.online?((p.corp?p.corp.name+' · ':'')+(p.title||p.app||'')):'oflayn';$('#fsStat').href='/screentime/hodim?u='+p.tg+'&d='+DAY}
async function openFs(id){FS.id=id;const run=++FS.run,box=$('#fs'),im=$('#fsImg');box.hidden=false;box.classList.remove('on');fsInfo();
 try{const pr=box.requestFullscreen&&box.requestFullscreen();if(pr&&pr.catch)pr.catch(()=>{})}catch(e){}
 let seq=0;while(run===FS.run){try{const r=await fetch('/screentime/frame?auto=1&id='+id+'&after='+seq,{credentials:'same-origin',cache:'no-store'});
  if(run!==FS.run)break;if(r.status===200){seq=+r.headers.get('X-Seq')||seq;const u=URL.createObjectURL(await r.blob());const old=im.src;
   im.onload=()=>{if(old.startsWith('blob:'))URL.revokeObjectURL(old)};im.src=u;box.classList.add('on')}else if(r.status!==204)await sleep(2000)}catch(e){await sleep(2000)}}}
function closeFs(){FS.run++;FS.id=null;$('#fs').hidden=true;if(document.fullscreenElement)document.exitFullscreen().catch(()=>{})}
$('#fsX').onclick=closeFs;$('#fsMng').onclick=()=>{const id=FS.id;closeFs();openPc(id)};
document.addEventListener('fullscreenchange',()=>{if(!document.fullscreenElement&&FS.id!=null)closeFs()});
addEventListener('keydown',e=>{if(e.key==='Escape'&&FS.id!=null)closeFs()});
// ---- Statistika (hafta / oy) ----
const ST={k:'week',d:DAY};
const hm=s=>{s=Math.round(s||0);return Math.floor(s/3600)+':'+String(Math.floor(s%3600/60)).padStart(2,'0')};
function shiftD(n){const d=new Date(ST.d+'T00:00:00');if(ST.k==='week')d.setDate(d.getDate()+7*n);else d.setMonth(d.getMonth()+n,1);
 return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')+'-'+String(d.getDate()).padStart(2,'0')}
async function loadSt(){$('#stCsv').href='/screentime/report.csv?p='+ST.k+'&d='+ST.d;$('#stCsvD').href='/screentime/report.csv?p='+ST.k+'&d='+ST.d+'&t=daily';
 try{const r=await fetch('/screentime/report.json?auto=1&p='+ST.k+'&d='+ST.d,{credentials:'same-origin'});const j=await r.json();
  $('#stLabel').textContent=j.label;
  $('#stBody').innerHTML=j.rows.map(x=>`<tr><td><a class="mlink" href="/screentime/hodim?u=${x.tg}&d=${j.to}">${E(x.name)}</a></td>
  <td>${x.days}</td><td><b>${hm(x.active)}</b></td><td>${hm(x.avg)}</td>
  <td>${x.eff==null?'—':`<span class="pill ${x.eff>=70?'g':x.eff>=45?'y':'r'}">${x.eff}%</span>`}</td>
  <td style="color:var(--r)">${hm(x.unprod)}</td><td style="color:var(--g)">${hm(x.mine)}</td><td>${hm(x.other)}</td>
  <td class="sub">${x.start?x.start+' · '+x.end:'—'}</td>
  <td class="ra"><a class="ibtn" href="/screentime/report.csv?p=${ST.k}&d=${ST.d}&u=${x.tg}" title="Samaradorlik hisobotini yuklab olish (Excel)">${IC.download}</a>
  <a class="ibtn" href="/screentime/hodim?u=${x.tg}&d=${j.to}" title="Batafsil">${IC.chart}</a></td></tr>`).join('')
   ||'<tr><td colspan="10" class="sub">Bu davrda ma’lumot yo‘q</td></tr>'}catch(e){$('#stBody').innerHTML='<tr><td colspan="10" class="sub">Yuklab bo‘lmadi</td></tr>'}}
$$('#stTabs button').forEach(b=>b.onclick=()=>{ST.k=b.dataset.k;$$('#stTabs button').forEach(x=>x.classList.toggle('on',x===b));loadSt()});
$('#stPrev').onclick=e=>{e.preventDefault();ST.d=shiftD(-1);loadSt()};
$('#stNext').onclick=e=>{e.preventDefault();const n=shiftD(1);if(n<=TODAY){ST.d=n;loadSt()}};
loadSt();
function fill(){const p=DATA.find(x=>x.id===cur);if(!p)return;
 $('#mName').textContent=p.name;$('#mPc').textContent=p.pc+(p.user?' · '+p.user:'')+(p.ver?' · agent '+p.ver:'');
 $('#mState').className='state '+p.cls;$('#mState').innerHTML='<i class="sd"></i>'+E(p.txt);
 $('#mApp').textContent=p.online?(p.app||'—'):'oflayn';$('#mTitle').textContent=p.title||'';
 $('#mAct').textContent=p.act;$('#mEff').textContent=p.eff==null?'—':p.eff+'%';$('#mProd').textContent=p.prod;$('#mUn').textContent=p.unprod;
 const b=$('#mBlock'),st=p.blocked?'ok':'stop';
 if(b.dataset.st!==st){const first=!b.dataset.st;b.dataset.st=st;
  b.innerHTML=p.blocked?IC.unlock+'Blokni ochish':IC.nosign+'Kompyuterni bloklash';b.className='btn big '+st;
  if(!first){void b.offsetWidth;b.classList.add('morph')}}
 $('#mMsg').hidden=p.blocked;
 $('#mSeen').textContent='Oxirgi aloqa: '+p.seen;
 const mc=$('#mCorp');mc.hidden=!p.corp;mc.innerHTML=corpLine(p.corp)}
$('#mBlock').addEventListener('animationend',e=>e.currentTarget.classList.remove('morph'));
function openPc(id){cur=id;$('#lvRec').href='/screentime/rec?id='+id;lastShot={};$('#mBlock').dataset.st='';
 const pp=DATA.find(x=>x.id===id);if(pp)$('#dFull').href='/screentime/hodim?u='+pp.tg+'&d='+DAY;$('#mToast').hidden=true;$('#mMsg').value='';
 $('#dList').innerHTML='<div class="sub">Yuklanmoqda…</div>';$('#dTl').innerHTML='';$('#dSum').innerHTML='';
 fill();dlg.showModal();poll();loadDetail();liveLoop(id)}
// Jonli ekran: oyna ochiq turganda agent ekranni soniyasiga 2–3 kadr yuboradi (AnyDesk kabi, faqat ko'rish)
let LV=0;const sleep=ms=>new Promise(r=>setTimeout(r,ms));
async function liveLoop(id){const run=++LV;let seq=0,n=0,t0=Date.now(),last=0;const im=$('#lvImg');
 $('#lv').classList.remove('on');$('#lvWait').textContent='Ekran ulanmoqda…';$('#lvSt').textContent='Ulanmoqda…';
 while(run===LV&&dlg.open&&cur===id){const p=DATA.find(x=>x.id===id);
  if(p&&!p.online){$('#lv').classList.remove('on');$('#lvWait').textContent='Kompyuter oflayn';$('#lvSt').textContent='';await sleep(3000);continue}
  if(p&&p.ver&&!(parseFloat(p.ver)>=1.4)){$('#lvWait').textContent='Jonli ko‘rish uchun agent yangilanishi kerak (1.4+)';await sleep(5000);continue}
  try{const r=await fetch('/screentime/frame?auto=1&id='+id+'&after='+seq,{credentials:'same-origin',cache:'no-store'});
   if(run!==LV)break;
   if(r.status===200){seq=+r.headers.get('X-Seq')||seq;const u=URL.createObjectURL(await r.blob());const old=im.src;
    im.onload=()=>{if(old.startsWith('blob:'))URL.revokeObjectURL(old)};im.src=u;$('#lv').classList.add('on');n++;last=Date.now();
    const s=(Date.now()-t0)/1000;if(s>3){$('#lvSt').textContent=(n/s).toFixed(1)+' kadr/s';n=0;t0=Date.now()}}
   else if(r.status===204){if(Date.now()-last>8000){$('#lv').classList.remove('on');$('#lvWait').textContent='Ekran ulanmoqda…'}}
   else await sleep(2000)}catch(e){await sleep(2000)}}}
$('#lvFull').onclick=()=>{const el=$('#lv');(el.requestFullscreen||el.webkitRequestFullscreen||(()=>{})).call(el)};
let DET=null,TAB='corp',open_={},dTimer=null;
function corpLine(c){return c?IC.building+'<span><b>'+E(c.name)+'</b> kabinetida · '+E(c.page)+'</span>'
 +(c.mine?'<span class="tg g">o‘zining</span>':'<span class="tg y">boshqa hodimniki</span>'):''}
function drawCorp(){const c=DET.companies;if(!c)return '';
 const head=`<div class="csum"><span>O‘z korxonalariga <b style="color:var(--g)">${c.mine}</b></span><span>Boshqa korxonalarga <b>${c.other}</b></span></div>`;
 if(!c.rows.length)return head+'<div class="sub" style="padding:10px 0">Bu kunda korxona bo‘yicha ma’lumot yo‘q</div>';
 const mx=Math.max(1,...c.rows.map(x=>x.secs));
 return head+c.rows.map((x,i)=>{const k='corp:'+x.inn,o=open_[k];
  const tag=x.mine?'<span class="tg g">biriktirilgan</span>':x.listed?'<span class="tg y">boshqa hodimniki</span>':'<span class="tg">ro‘yxatda yo‘q</span>';
  const parts=x.parts.map(p=>`<div class="ti"><i style="background:var(--acc)"></i><span><b>${E(p.src)}</b> · ${E(p.label)}</span><b>${p.t}</b></div>`).join('')
   ||'<div class="sub">Bugun bu korxona bilan ishlanmagan</div>';
  return `<div class="row${o?' open':''}" data-k="${E(k)}"><div class="rl"><i style="background:${x.mine?'var(--c-p)':'var(--c-n)'}"></i>
  <span class="nm" title="${E(x.name)}">${E(x.name)} <span class="sub" style="font-weight:400">${x.inn}</span></span>
  <span class="ct">${tag}</span><b>${x.secs?x.t:'—'}</b></div>
  <div class="bar"><i style="width:${x.secs?Math.max(2,Math.round(100*x.secs/mx)):0}%;background:${x.mine?'var(--c-p)':'var(--c-n)'}"></i></div>
  <div class="more">${parts}</div></div>`}).join('')}
const CC={p:'var(--c-p)',n:'var(--c-n)',u:'var(--c-u)'},CN={p:'ish',n:'boshqa',u:'chalg‘ish'};
async function loadDetail(){clearTimeout(dTimer);const p=DATA.find(x=>x.id===cur);if(!p)return;
 try{const r=await fetch('/screentime/detail.json?auto=1&tg='+p.tg+'&d='+DAY,{credentials:'same-origin',cache:'no-store'});
  DET=await r.json();drawDetail()}catch(e){$('#dList').innerHTML='<div class="sub">Ma’lumot olinmadi</div>'}
 if(dlg.open)dTimer=setTimeout(loadDetail,30000)}
function drawDetail(){const d=DET;if(!d)return;
 $('#dDay').textContent=' · '+d.dayf+(d.span?' · '+d.span[0]+'–'+d.span[1]:'');
 $('#dSum').innerHTML=`<span>Kompyuterda <b>${d.on}</b></span><span>Faol <b>${d.active}</b></span><span>Harakatsiz <b>${d.idle}</b></span>
 <span>Ish <b style="color:var(--g)">${d.prod}</b></span><span>Chalg‘ish <b style="color:var(--r)">${d.unprod}</b></span>`;
 $('#dTl').innerHTML=d.timeline;
 if(TAB==='corp'){$('#dList').innerHTML=drawCorp();
  $$('#dList .rl').forEach(el=>el.onclick=()=>{const r=el.parentNode;open_[r.dataset.k]=!open_[r.dataset.k];r.classList.toggle('open')});return}
 const list=TAB==='apps'?d.apps:d.sites,mx=Math.max(1,...list.map(x=>x.secs));
 if(!list.length){$('#dList').innerHTML='<div class="sub" style="padding:10px 0">'+(TAB==='apps'?'Bu kunda ma’lumot yo‘q':'Brauzerda sahifa ochilmagan')+'</div>';return}
 $('#dList').innerHTML=list.map((x,i)=>{const k=TAB+':'+x.key+':'+i,o=open_[k];
  const tt=TAB==='apps'?(x.browser?'<div class="sub">Brauzer — ochilgan sahifalar “Saytlar” bo‘limida</div>':
   x.titles.map(t=>`<div class="ti"><i style="background:${CC[t.cat]||CC.n}"></i><span>${E(t.label)}</span><b>${t.t}</b></div>`).join('')):'';
  const pick=['p','n','u'].map(c=>`<button type="button" data-c="${c}" class="${x.rule===c?'on':''}"><i class="sd" style="color:${CC[c]}"></i>${c==='p'?'Ish':c==='n'?'Boshqa':'Chalg‘ish'}</button>`).join('')
   +(x.rule?'<button type="button" data-c="">'+IC.undo+'Avtomatik</button>':'');
  return `<div class="row${o?' open':''}" data-k="${E(k)}" data-key="${E(x.key)}">
  <div class="rl"><i style="background:${CC[x.cat]||CC.n}"></i><span class="nm" title="${E(x.label)}">${E(x.label)}</span>
  <span class="ct">${CN[x.cat]||''}${x.rule?' · belgilangan':''}</span><b>${x.t}</b></div>
  <div class="bar"><i style="width:${Math.max(2,Math.round(100*x.secs/mx))}%;background:${CC[x.cat]||CC.n}"></i></div>
  <div class="more">${tt}<div class="pk"><span class="sub">“${E(x.key)}” —</span>${pick}</div></div></div>`}).join('');
 $$('#dList .rl').forEach(el=>el.onclick=()=>{const r=el.parentNode;open_[r.dataset.k]=!open_[r.dataset.k];r.classList.toggle('open')});
 $$('#dList .pk button').forEach(b=>b.onclick=async()=>{const key=b.closest('.row').dataset.key;
  try{const r=await fetch('/dashboard/pc',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/x-www-form-urlencoded'},
   body:new URLSearchParams({csrf:CSRF,cmd:'rule',arg:key,cat:b.dataset.c,ajax:'1'})});const j=await r.json();toast(j.text)}catch(e){toast('Saqlanmadi — sahifani yangilang')}
  loadDetail()})}
$$('.seg button').forEach(b=>b.onclick=()=>{TAB=b.dataset.tab;$$('.seg button').forEach(x=>x.classList.toggle('on',x===b));drawDetail()});
async function poll(){clearTimeout(timer);
 try{const r=await fetch('/screentime/live.json?auto=1',{credentials:'same-origin',cache:'no-store'});
  DATA=await r.json();draw()}catch(e){toast('Aloqa yo‘q yoki sessiya tugagan — sahifani yangilang')}
 timer=setTimeout(poll,dlg.open?1500:4000)}
function toast(t){const el=$('#mToast');el.innerHTML=IC.check+E(String(t).replace(/^[^\wА-Яа-яЁёЎўҚқҒғҲҳ]+/,''));el.hidden=false;clearTimeout(el._t);el._t=setTimeout(()=>el.hidden=true,6000)}
async function send(cmd,arg){
 try{const r=await fetch('/dashboard/pc',{method:'POST',credentials:'same-origin',
  headers:{'Content-Type':'application/x-www-form-urlencoded'},
  body:new URLSearchParams({csrf:CSRF,pc:String(cur),cmd,arg:arg||'',ajax:'1'})});
  const j=await r.json();toast(j.text||(j.ok?'Yuborildi':'Xato'))}catch(e){toast('Yuborilmadi — sahifani yangilang (F5)')}
 setTimeout(poll,700)}
$('#mBlock').onclick=()=>{const p=DATA.find(x=>x.id===cur);if(!p)return;
 if(p.blocked)send('unblock');else send('block',$('#mMsg').value.trim())};
$$('#dPc [data-cmd]').forEach(b=>b.onclick=()=>{const c=b.dataset.cmd;
 if(b.dataset.ask&&!confirm(b.dataset.ask))return;
 if(c==='msg'){const t=prompt('Hodim ekranida chiqadigan xabar:');if(t&&t.trim())send('msg',t.trim());return}
 send(c)});
$$('[data-close]').forEach(b=>b.onclick=()=>b.closest('dialog').close());
dlg.addEventListener('close',()=>{cur=null;LV++;clearTimeout(dTimer);poll()});
draw();timer=setTimeout(poll,4000);
const U=new URLSearchParams(location.search).get('u');
if(U){const p=DATA.find(x=>String(x.tg)===U);if(p)openPc(p.id)}
"""


def shot_png(pc_id):
    s = SHOTS.get(pc_id)
    return s[1] if s else None
