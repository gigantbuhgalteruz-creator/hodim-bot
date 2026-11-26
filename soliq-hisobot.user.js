// ==UserScript==
// @name         Soliq.uz → Hodim bot: hisobotlar
// @namespace    soliq-telegram
// @version      3.8
// @description  Hodim soliq.uz kabinetida ishlayotganda hisobotlar jurnalini fonda tekshiradi va yangi/o'zgargan hisobotlarni bot serveriga yuboradi (bot ularni faqat ownerga jo'natadi).
// @match        https://oldmy.soliq.uz/*
// @match        https://my.soliq.uz/*
// @grant        GM_xmlhttpRequest
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @connect      trycloudflare.com
// @connect      t.me
// @connect      165.227.155.177
// @connect      *
// @run-at       document-idle
// @noframes
// ==/UserScript==

(function () {
  'use strict';

  // ====== SOZLAMA (hamma kompyuterda bir xil) ======
  const SERVER_URL    = 'https://neo-calling-exam-points.trycloudflare.com';   // bot server manzili (config.json dagi PUBLIC_URL)
  const BOT_USERNAME  = 'tekshiruvbuxgalterbot';  // eski usul (zaxira): bot profilidan
  const DISCOVERY_URL = '';                       // server o'zi qo'yadi: tunnel o'zgarsa yangi manzil shu yerdan olinadi
  const DISCOVERY_KEY = '';                       // server o'zi qo'yadi: javob imzosini tekshirish uchun
  const CHECK_MINUTES = 2;                        // jurnalni necha daqiqada tekshirish
  const SKIP_DRAFTS   = true;                      // "Черновик"larni yubormaslik
  const SYNC_DAYS     = 60;                        // birinchi kirishda shu kunlardagi eski hisobotlar jadvalga jim yuboriladi
  // Hodim kaliti har bir kompyuterda bir marta so'raladi (botda /key buyrug'i).
  // =================================================

  const JOURNAL_URL = '/wefo4-clientui/form/ru/reports/0';
  const clean = s => (s || '').replace(/\s+/g, ' ').trim();
  const txt = el => clean(el.textContent);

  const myInn = (document.body.textContent.match(/ИНН:\s*(\d+)/) || [])[1];
  const onNew = location.hostname === 'my.soliq.uz';   // yangi kabinet — faqat korxona reytingini o'qiymiz
  if (!myInn && !onNew) return;                     // kabinetga kirilmagan
  const STORE_KEY = 'soliq_seen_reports_v3_' + myInn;

  let KEY = GM_getValue('employee_key', '');
  function askKey() {
    const k = prompt('Hodim kaliti (Telegram botda /key yozing va shu kalitni kiriting):', KEY || '');
    if (k !== null && k.trim()) { KEY = k.trim(); GM_setValue('employee_key', KEY); verifyKey(true); }
  }
  GM_registerMenuCommand('🔑 Hodim kalitini kiritish', askKey);

  const loadSeen = () => { try { return JSON.parse(GM_getValue(STORE_KEY, '{}')); } catch { return {}; } };
  const saveSeen = o => GM_setValue(STORE_KEY, JSON.stringify(o));

  // Server manzili o'zgarsa (vaqtinchalik tunnel), yangisini bot profilidan (t.me/<bot>) o'qib olamiz.
  // Topilgan manzil faqat shu o'rnatilgan versiya uchun eslab qolinadi (qayta o'rnatilsa — skriptdagi manzil ustun).
  let serverUrl = SERVER_URL;
  const savedUrl = GM_getValue('server_url_v2', null);
  if (savedUrl && savedUrl.base === SERVER_URL && savedUrl.url) serverUrl = savedUrl.url;
  async function hmacHex(key, msg) {
    const enc = new TextEncoder();
    const k = await crypto.subtle.importKey('raw', enc.encode(key), { name: 'HMAC', hash: 'SHA-256' }, false, ['sign']);
    const s = await crypto.subtle.sign('HMAC', k, enc.encode(msg));
    return [...new Uint8Array(s)].map(b => b.toString(16).padStart(2, '0')).join('');
  }
  function discoverSigned() {
    if (!DISCOVERY_URL || !DISCOVERY_KEY) return Promise.resolve(null);
    return new Promise(resolve => {
      GM_xmlhttpRequest({
        method: 'GET', url: DISCOVERY_URL + '?_=' + Date.now(), timeout: 20000,
        onload: async r => {
          try {
            const d = JSON.parse(r.responseText || '{}');
            const ok = /^https:\/\/[a-z0-9-]+\.trycloudflare\.com$/.test(d.url || '') &&
                       (await hmacHex(DISCOVERY_KEY, d.url)) === d.sig;
            resolve(ok ? d.url : null);
          } catch { resolve(null); }
        },
        onerror: () => resolve(null), ontimeout: () => resolve(null)
      });
    });
  }
  async function discoverServer() {
    return (await discoverSigned()) || (await discoverProfile());
  }
  function discoverProfile() {
    return new Promise(resolve => {
      GM_xmlhttpRequest({
        method: 'GET', url: 'https://t.me/' + BOT_USERNAME + '?_=' + Date.now(), timeout: 20000,
        onload: r => {
          const m = (r.responseText || '').match(/https:\/\/[a-z0-9-]+\.trycloudflare\.com/);
          resolve(m ? m[0] : null);
        },
        onerror: () => resolve(null), ontimeout: () => resolve(null)
      });
    });
  }

  async function post(path, data) {
    try {
      return await postOnce(path, data);
    } catch (e) {
      // 401 — kalit xato, manzil emas. Boshqa xatolarda serverning yangi manzilini qidiramiz.
      if (e.status === 401 || (e.status && e.status < 500 && e.status !== 404)) throw e;
      const fresh = await discoverServer();
      if (!fresh || fresh === serverUrl) throw e;
      serverUrl = fresh; GM_setValue('server_url_v2', { base: SERVER_URL, url: fresh });
      console.info('[soliq→bot] yangi server manzili:', fresh);
      return await postOnce(path, data);
    }
  }

  function postOnce(path, data) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: 'POST', url: serverUrl.replace(/\/$/, '') + path,
        headers: { 'Content-Type': 'application/json' },
        data: JSON.stringify(Object.assign({ key: KEY }, data)),
        timeout: 20000,
        onload: r => {
          let body = {}; try { body = JSON.parse(r.responseText); } catch {}
          if (r.status === 200) resolve(body);
          else { const e = new Error('HTTP ' + r.status); e.status = r.status; reject(e); }
        },
        onerror: () => reject(new Error('server bilan aloqa yo\'q')),
        ontimeout: () => reject(new Error('server javob bermadi'))
      });
    });
  }

  function verifyKey(show) {
    post('/api/ping', {}).then(r => { if (show) alert('✅ Kalit to\'g\'ri: ' + r.name); })
      .catch(e => alert(e.status === 401 ? '❌ Kalit noto\'g\'ri. Botda /key yozib tekshiring.' : '⚠️ ' + e.message));
  }
  // ====== Korxona reytingi ("Рейтинг: C - 34") — topilsa serverga yuboriladi, jadvaldagi "Рейтинг" ustuniga tushadi ======
  const sentRating = {};
  function ratingTick() {
    if (!KEY) return;
    const found = {};
    document.querySelectorAll('body *').forEach(el => {
      if (el.children.length > 3) return;
      const m = (el.textContent || '').match(/(?:Рейтинг|Reyting)\s*:?\s*([A-EАВСЕ])\s*[-–]\s*(\d{1,3})/i);
      if (!m || el.textContent.length > 80) return;
      let up = el;                                   // yaqin ota elementdan korxona STIR/INN ini topamiz
      for (let i = 0; i < 7 && up; i++, up = up.parentElement) {
        const inn = ((up.textContent || '').match(/(?:СТИР|STIR|ИНН|INN)\s*:?\s*(\d{9})/i) || [])[1]
                    || (up !== el && onNew ? ((up.textContent || '').match(/\b(\d{9})\b/) || [])[1] : null);
        if (inn) { found[inn] = m[1] + ' - ' + m[2]; break; }
      }
    });
    for (const [inn, rating] of Object.entries(found)) {
      if (sentRating[inn] === rating) continue;
      sentRating[inn] = rating;
      post('/api/rating', { inn, rating }).catch(() => { delete sentRating[inn]; });
    }
  }
  setTimeout(ratingTick, 3000);
  setInterval(ratingTick, 15000);
  if (onNew) return;    // yangi kabinetda boshqa ish yo'q

  if (!KEY) askKey();   // serverUrl/post e'lon qilingandan keyin

  function parseReports(doc) {
    const body = doc.body.textContent;
    const company = clean((body.match(/НАИМЕНОВАНИЕ:\s*([^\n]+?)(?:\s{2,}|\n|ИНН:)/) || [])[1]);
    const inn = (body.match(/ИНН:\s*(\d+)/) || [])[1];
    const cert = (body.match(/Сертификат:\s*"?(\d+)/) || [])[1];
    const out = new Map();
    doc.querySelectorAll('table').forEach(tb => {
      if (!tb.rows.length) return;
      const headers = [...tb.rows[0].cells].map(txt);
      if (!headers.includes('№ Отчета') || !headers.includes('Состояние')) return;
      for (let r = 1; r < tb.rows.length; r++) {
        const cells = tb.rows[r].cells;
        if (cells.length !== headers.length) continue;
        const rec = {}; let errorReason = '';
        headers.forEach((h, i) => {
          if (!h || h === 'Действие') return;
          const cell = cells[i];
          if (h === 'Состояние') {
            const err = cell.querySelector('input[id^="hidden_error_reason"]');
            if (err && err.value) errorReason = clean(err.value);
            const copy = cell.cloneNode(true);
            copy.querySelectorAll('input,button,a.btn').forEach(e => e.remove());
            rec[h] = txt(copy);
          } else rec[h] = txt(cell);
        });
        const id = rec['№ Отчета'];
        if (!id || !/^\d+$/.test(id)) continue;
        if (SKIP_DRAFTS && /Черновик/i.test(rec['Состояние'] || '')) continue;
        if (errorReason) rec['Причина ошибки'] = errorReason;
        const prev = out.get(id);
        if (!prev || Object.keys(rec).length > Object.keys(prev.rec).length) out.set(id, { rec, company, inn, cert });
      }
    });
    return out;
  }

  async function getJournalDoc() {
    if (location.pathname.includes('/reports')) return document;
    const res = await fetch(JOURNAL_URL, { credentials: 'include' });
    if (!res.ok) throw new Error('journal HTTP ' + res.status);
    return new DOMParser().parseFromString(await res.text(), 'text/html');
  }

  let busy = false, keyWarned = false;
  const diag = { last: '', result: 'hali tekshirilmadi', found: 0, sent: 0, error: '' };
  const pad = n => String(n).padStart(2, '0');
  const d0 = new Date();
  const TODAY = `${pad(d0.getDate())}.${pad(d0.getMonth() + 1)}.${d0.getFullYear()}`;
  const sentAt = rec => {   // "DD.MM.YYYY HH:MM:SS" -> Date (yoki null)
    const m = (rec['Дата отправки'] || '').match(/(\d{2})\.(\d{2})\.(\d{4})/);
    return m ? new Date(+m[3], +m[2] - 1, +m[1]) : null;
  };
  const isRecent = rec => { const d = sentAt(rec); return !!d && (Date.now() - d) < SYNC_DAYS * 864e5; };
  async function check() {
    if (!KEY) { diag.result = 'kalit kiritilmagan'; return; }
    if (busy) return;
    busy = true;
    diag.last = new Date().toLocaleTimeString();
    try {
      const reports = parseReports(await getJournalDoc());
      diag.found = reports.size;
      if (!reports.size) { diag.result = 'jurnalda hisobot topilmadi'; return; }
      if ([...reports.values()][0].inn !== myInn) { diag.result = 'jurnal boshqa firmaga tegishli'; return; }
      const seen = loadSeen();
      const firstRun = Object.keys(seen).length === 0;
      const ids = [...reports.keys()].sort((a, b) => Number(a) - Number(b));
      let sentNow = 0, syncFail = false;
      const SYNC_KEY = 'soliq_table_synced_v35_' + myInn;   // v3.5: eski hisobotlar jadvalga bir marta jim yuboriladi
      const needSync = !GM_getValue(SYNC_KEY, false);
      diag.result = firstRun ? 'birinchi kirish: eski hisobotlar jadvalga yozildi' : 'yangi hisobot yo\'q';
      for (const id of ids) {
        const item = reports.get(id);
        const status = item.rec['Состояние'] || '';
        if (seen[id] === status) {
          if (needSync && isRecent(item.rec)) {
            try { await post('/api/report', Object.assign({ silent: true }, item)); } catch (e) { syncFail = true; }
          }
          continue;
        }
        // Birinchi kirish: eskilari rahbarga xabar bo'lib bormaydi — faqat jadvalga jim yoziladi (SYNC_DAYS ichidagilari).
        // BUGUN yuborilganlari esa odatdagidek xabar bo'lib boradi.
        const silent = firstRun && !(item.rec['Дата отправки'] || '').includes(TODAY);
        if (silent && !isRecent(item.rec)) { seen[id] = status; continue; }
        try {
          await post('/api/report', silent ? Object.assign({ silent: true }, item) : item);
          seen[id] = status; saveSeen(seen);
          sentNow++; diag.sent++;
          diag.result = `yuborildi: ${sentNow} ta`;
        } catch (e) {
          if (e.status === 401 && !keyWarned) { keyWarned = true; alert('❌ Hodim kaliti noto\'g\'ri. Tampermonkey menyusi → "🔑 Hodim kalitini kiritish".'); }
          console.warn('[soliq→bot] yuborilmadi', e);
          diag.result = 'yuborishda xato'; diag.error = e.message;
          syncFail = true;
          break;                                            // keyingi tekshiruvda qayta urinadi
        }
      }
      saveSeen(seen);
      if (needSync && !syncFail) GM_setValue(SYNC_KEY, true);
    } catch (e) {
      console.warn('[soliq→bot] tekshiruv xatosi', e);
      diag.result = 'tekshiruv xatosi'; diag.error = e.message;
    } finally { busy = false; }
  }

  GM_registerMenuCommand('🔍 Holatni ko\'rish', async () => {
    await check();
    const seen = loadSeen();
    alert(`Skript: v${GM_info.script.version}\nKabinet ИНН: ${myInn}\nKalit: ${KEY ? 'bor' : 'YO\'Q'}\n` +
          `Server: ${serverUrl}\n\nOxirgi tekshiruv: ${diag.last || '—'}\nNatija: ${diag.result}\n` +
          `Jurnalda hisobot: ${diag.found}\nEslab qolingan: ${Object.keys(seen).length}\n` +
          `Shu sahifada yuborilgan: ${diag.sent}` + (diag.error ? `\nXato: ${diag.error}` : ''));
  });

  GM_registerMenuCommand('📨 Oxirgi hisobotni yuborish (sinov)', async () => {
    try {
      const reports = parseReports(await getJournalDoc());
      const id = [...reports.keys()].sort((a, b) => Number(b) - Number(a))[0];
      if (!id) return alert('Hisobot topilmadi.');
      const item = reports.get(id);
      // Sinov uchun holatga belgi qo'shamiz, server takror deb hisoblamasligi uchun.
      const test = Object.assign({}, item, { rec: Object.assign({}, item.rec, { 'Состояние': item.rec['Состояние'] + ' (sinov)' }) });
      await post('/api/report', test);
      alert('Yuborildi ✔');
    } catch (e) { alert('Xato: ' + e.message); }
  });
  GM_registerMenuCommand('📋 Hamma hisobotlarni jadvalga yuborish (xabarsiz)', async () => {
    try {
      const reports = parseReports(await getJournalDoc());
      if (![...reports.values()].length || [...reports.values()][0].inn !== myInn) return alert('Hisobot topilmadi.');
      let n = 0;
      for (const item of reports.values()) { await post('/api/report', Object.assign({ silent: true }, item)); n++; }
      alert(`Jadvalga yuborildi: ${n} ta hisobot ✔`);
    } catch (e) { alert('Xato: ' + e.message); }
  });
  GM_registerMenuCommand('♻️ Shu firma xotirasini tozalash', async () => {
    saveSeen({});
    await check();   // bugungi hisobotlar darhol yuboriladi, qolganlari eslab qolinadi
    alert('Tozalandi: ИНН ' + myInn + '\n' + diag.result);
  });

  setTimeout(check, 2000);
  setInterval(check, CHECK_MINUTES * 60 * 1000);

  // ====== Korxona uchun sarflangan vaqt ======
  // Kabinet sahifasi ko'rinib turgan, brauzer oynasi faol va hodim oxirgi 3 daqiqada sichqoncha/klaviaturani
  // ishlatgan bo'lsa — har 30 soniyada "shu korxona kabinetida, shu sahifada" deb yuboriladi.
  // (Bir nechta tab ochiq bo'lsa, faqat ko'rinib turgani hisoblanadi.)
  const myCompany = clean((document.body.textContent.match(/НАИМЕНОВАНИЕ:\s*([^\n]+?)(?:\s{2,}|\n|ИНН:)/) || [])[1]);
  let lastInput = Date.now(), lastBeat = Date.now();
  ['mousemove', 'mousedown', 'keydown', 'wheel', 'touchstart'].forEach(ev =>
    addEventListener(ev, () => { lastInput = Date.now(); }, { passive: true, capture: true }));
  function pageName() {
    const h = document.querySelector('h1, h2, .page-title, .breadcrumb li:last-child, .breadcrumb .active');
    return clean((h && h.textContent) || document.title).slice(0, 120);
  }
  let focused = document.hasFocus();
  addEventListener('focus', () => { lastBeat = Date.now(); focused = true; });
  addEventListener('blur', () => { beat(true); focused = false; });
  function beat(leaving) {
    const now = Date.now(), secs = Math.round((now - lastBeat) / 1000);
    lastBeat = now;
    const active = (leaving === true ? focused : document.visibilityState === 'visible' && document.hasFocus())
                   && now - lastInput < 180000;
    if (!KEY || !active || secs < 5) return;
    post('/api/presence', { inn: myInn, company: myCompany, page: pageName(), path: location.pathname.slice(0, 120),
                            secs: Math.min(secs, 90) }).catch(() => {});
  }
  setInterval(beat, 30000);
  document.addEventListener('visibilitychange', () => { if (document.visibilityState !== 'visible') beat(true); else lastBeat = Date.now(); });
  addEventListener('pagehide', () => beat(true));

  // "Отправить" bosilganda — 5 daqiqa davomida har 20 soniyada tekshiramiz (sahifa almashsa ham davom etadi),
  // shunda hisobot va uning holati (В обработке → Принят/Ошибка) deyarli darhol rahbarga boradi.
  const BURST_KEY = 'soliq_burst_until';
  let burstTimer = null;
  function runBurst() {
    if (burstTimer) return;
    setTimeout(check, 5000);
    burstTimer = setInterval(() => {
      if (Date.now() > Number(sessionStorage.getItem(BURST_KEY) || 0)) { clearInterval(burstTimer); burstTimer = null; return; }
      check();
    }, 20000);
  }
  document.addEventListener('click', e => {
    const b = e.target.closest && e.target.closest('button, input[type=submit], input[type=button], a');
    if (!b) return;
    if (/^\s*Отправить/i.test(b.value || b.textContent || '')) {
      sessionStorage.setItem(BURST_KEY, String(Date.now() + 5 * 60 * 1000));
      runBurst();
    }
  }, true);
  if (Number(sessionStorage.getItem(BURST_KEY) || 0) > Date.now()) runBurst();
  if (location.pathname.includes('/reports')) {
    let t;
    new MutationObserver(() => { clearTimeout(t); t = setTimeout(check, 1500); })
      .observe(document.body, { childList: true, subtree: true });
  }
})();
