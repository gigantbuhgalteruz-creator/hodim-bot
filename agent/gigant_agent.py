"""Gigant Buxgalter — hodim kompyuteri agenti (screen time + masofadan boshqaruv).

Nima qiladi:
  * har 5 soniyada oldindagi oyna qaysi dasturniki ekanini va hodim klaviatura/sichqonchani
    ishlatayotganini tekshiradi (klaviaturada nima yozilgani O'QILMAYDI);
  * brauzerda manzil satridan joriy sayt havolasini o'qiydi (UI Automation);
  * har 10 soniyada natijani hodim bot serveriga yuboradi (internet bo'lmasa — keyin yuboradi);
  * serverdan buyruq olsa bajaradi: bloklash, o'chirish, qayta yoqish, chiqish, xabar, skrinshot;
  * ekran yozuvi: har 10 soniyada kichik kadr (kuzatuv kamerasi kabi; server 2 kundan keyin o'chiradi);
  * "kompyuter klub" bloki: butun ekranni yopadigan oyna (rahbar ochmaguncha ishlab bo'lmaydi);
  * buyruqlarni darhol olish uchun serverda doimiy "kutish" so'rovi (long-poll) ushlab turadi.

Faqat Python standart kutubxonasi (ctypes) ishlatiladi; python.org embeddable Python bilan ishlaydi.
Sozlama: %ProgramData%\\GigantAgent\\config.json  {"key": "...", "server": "https://...", "disc": "http://IP:8099/u", "dkey": "..."}
"""
import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import struct
import subprocess
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import zlib

VERSION = "1.9"
STABLE_URL = "https://gigantbuxgalter.com"   # doimiy server manzili (Cloudflare tunnel, o'zgarmaydi)
SAMPLE_SEC = 5          # necha soniyada bir tekshiriladi
FLUSH_SEC = 10          # necha soniyada serverga yuboriladi (jonli jurnal uchun tez)
IDLE_LIMIT = 180        # shuncha soniya klaviatura/sichqoncha tegilmasa — "harakatsiz"
REC_SEC = 10            # ekran yozuvi: necha soniyada bir kadr (server o'zgartirishi mumkin)
MAX_PENDING = 6000      # internet yo'q paytda saqlanadigan yozuvlar chegarasi (~2-3 kun)

PROGRAMDATA = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
CONF_PATH = os.path.join(PROGRAMDATA, "GigantAgent", "config.json")
STATE_DIR = os.path.join(os.environ.get("LOCALAPPDATA", PROGRAMDATA), "GigantAgent")
STATE_PATH = os.path.join(STATE_DIR, "state.json")
LOG_PATH = os.path.join(STATE_DIR, "agent.log")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)

user32.GetForegroundWindow.restype = wt.HWND
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.OpenInputDesktop.restype = wt.HANDLE
user32.CloseDesktop.argtypes = [wt.HANDLE]
user32.MessageBoxW.argtypes = [wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.UINT]
user32.GetDC.restype = wt.HDC
user32.GetDC.argtypes = [wt.HWND]
user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.CreateMutexW.restype = wt.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleBitmap.restype = wt.HBITMAP
gdi32.CreateCompatibleBitmap.argtypes = [wt.HDC, ctypes.c_int, ctypes.c_int]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.StretchBlt.argtypes = [wt.HDC] + [ctypes.c_int] * 4 + [wt.HDC] + [ctypes.c_int] * 4 + [wt.DWORD]
gdi32.SetStretchBltMode.argtypes = [wt.HDC, ctypes.c_int]
gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
gdi32.DeleteDC.argtypes = [wt.HDC]


class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG), ("biPlanes", wt.WORD),
                ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                ("biXPelsPerMeter", wt.LONG), ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD),
                ("biClrImportant", wt.DWORD)]


def log(*parts):
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > 1_000_000:
            os.replace(LOG_PATH, LOG_PATH + ".1")
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + " ".join(str(p) for p in parts) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------- sozlama va holat
def load_json(path, default):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


_SAVE_LOCK = threading.Lock()


def save_state(state):
    with _SAVE_LOCK:
        try:
            os.makedirs(STATE_DIR, exist_ok=True)
            tmp = STATE_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(dict(state), f, ensure_ascii=False)
            os.replace(tmp, STATE_PATH)
        except Exception:
            log("STATE SAVE ERROR", traceback.format_exc())


# ---------------------------------------------------------------- oyna / faollik
def idle_seconds():
    lii = LASTINPUTINFO()
    lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
    if not user32.GetLastInputInfo(ctypes.byref(lii)):
        return 0
    return max(0, (kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000.0


def screen_locked():
    """Ekran qulflangan (Win+L) yoki boshqa xavfsiz ish stolida bo'lsa — True."""
    h = user32.OpenInputDesktop(0, False, 0x0100)  # DESKTOP_SWITCHDESKTOP
    if not h:
        return True
    user32.CloseDesktop(h)
    return False


def process_name(pid):
    h = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value)
        return ""
    finally:
        kernel32.CloseHandle(h)


def foreground():
    """(dastur.exe, oyna sarlavhasi, oyna)."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return "", "", None
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return process_name(pid.value), buf.value, hwnd


# ---------------------------------------------------------------- brauzer manzili (UI Automation, faqat stdlib)
BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "opera.exe", "browser.exe", "brave.exe", "vivaldi.exe"}


class _VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort), ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("val", ctypes.c_void_p), ("val2", ctypes.c_void_p)]


def norm_url(v):
    """Manzil satridagi matn -> 'https://domen/yo'l' (so'rov parametrlari va # olib tashlanadi). URL bo'lmasa ''."""
    v = (v or "").strip()
    if not v or " " in v or len(v) > 4000:
        return ""
    if not re.match(r"^[a-z][a-z0-9+.-]*://", v, re.I):
        if not re.match(r"^[\w-]+(\.[\w-]+)+(:\d+)?(/|$)", v):
            return ""
        v = "https://" + v
    m = re.match(r"^(https?://[^/?#\s]+(?:/[^?#\s]*)?)", v, re.I)
    return m.group(1)[:300] if m else ""


class UrlReader:
    """Oldindagi brauzer oynasining manzil satridan (Chrome, Edge, Firefox, Yandex...) joriy sayt havolasini o'qiydi.
    Manzil satri elementi har oyna uchun bir marta topiladi va eslab qolinadi — keyingi o'qishlar juda tez."""

    def __init__(self):
        self.uia = None          # None — hali ochilmagan, False — ishlamaydi
        self.el = {}             # hwnd -> [element (c_void_p yoki None), oxirgi qidirish vaqti]
        self.last = {}           # hwnd -> oxirgi to'g'ri URL

    def _open(self):
        try:
            ole32.CoInitializeEx(None, 0)                    # MTA
            clsid = _guid("{ff48dba4-60ef-4201-aa87-54103eef594e}")   # CUIAutomation
            iid = _guid("{30cbe57d-d9d0-452a-ab13-7ac5ac4825ee}")     # IUIAutomation
            p = ctypes.c_void_p()
            hr = ole32.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(p))
            self.uia = p if hr == 0 and p.value else False
        except Exception:
            log("UIA ochilmadi", traceback.format_exc())
            self.uia = False

    @staticmethod
    def _release(p):
        if p and p.value:
            try:
                _com(p, 2, ctypes.c_ulong)(p)
            except Exception:
                pass

    def _find(self, hwnd):
        u = self.uia
        root, cond, el = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
        try:
            if _com(u, 6, ctypes.c_long, wt.HWND, ctypes.POINTER(ctypes.c_void_p))(u, hwnd, ctypes.byref(root)) != 0 \
                    or not root.value:
                return None
            var = _VARIANT(vt=3, val=50004)                  # VT_I4, UIA_EditControlTypeId
            if _com(u, 23, ctypes.c_long, ctypes.c_int, _VARIANT, ctypes.POINTER(ctypes.c_void_p))(
                    u, 30003, var, ctypes.byref(cond)) != 0:  # UIA_ControlTypePropertyId
                return None
            _com(root, 5, ctypes.c_long, ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))(
                root, 4, cond, ctypes.byref(el))             # FindFirst(TreeScope_Descendants) — 1-matn maydoni = manzil satri
            return el if el.value else None
        finally:
            self._release(root)
            self._release(cond)

    def _prop(self, el, pid):
        var = _VARIANT()
        hr = _com(el, 10, ctypes.c_long, ctypes.c_int, ctypes.POINTER(_VARIANT))(el, pid, ctypes.byref(var))
        if hr != 0:
            raise OSError(hr)
        try:
            if var.vt == 8:                                  # VT_BSTR
                return ctypes.wstring_at(var.val) if var.val else ""
            if var.vt == 11:                                 # VT_BOOL
                return bool(var.val and (var.val & 0xFFFF))
            return None
        finally:
            oleaut32.VariantClear(ctypes.byref(var))

    def read(self, hwnd, app):
        if not hwnd or (app or "").lower() not in BROWSERS:
            return ""
        if self.uia is None:
            self._open()
        if not self.uia:
            return ""
        key = int(hwnd)
        rec = self.el.get(key)
        if rec is None or (rec[0] is None and time.time() - rec[1] > 20):
            if len(self.el) > 30:                            # yopilgan oynalar to'planib qolmasin
                for r in self.el.values():
                    self._release(r[0])
                self.el.clear()
                self.last.clear()
            try:
                rec = self.el[key] = [self._find(hwnd), time.time()]
            except Exception:
                rec = self.el[key] = [None, time.time()]
        el = rec[0]
        if el is None:
            return ""
        try:
            if not self._prop(el, 30008):                    # UIA_HasKeyboardFocus: hodim manzil yozayotgan bo'lsa — eski URL
                u = norm_url(self._prop(el, 30045))          # UIA_ValueValuePropertyId
                if u:
                    self.last[key] = u
        except Exception:
            self._release(el)                                # oyna yopilgan / element eskirgan — keyingi safar qayta topamiz
            self.el.pop(key, None)
        return self.last.get(key, "")


def clean_title(t):
    t = re.sub(r"\s+", " ", t or "").strip()
    return t[:150]


# ---------------------------------------------------------------- skrinshot (PNG, faqat stdlib)
def screenshot_png(max_w=1600):
    sx, sy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)    # virtual ekran (bir nechta monitor)
    w, h = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    if w <= 0 or h <= 0:
        w, h, sx, sy = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1), 0, 0
    scale = min(1.0, max_w / float(w))
    ow, oh = max(1, int(w * scale)), max(1, int(h * scale))
    sdc = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, ow, oh)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        gdi32.SetStretchBltMode(mdc, 4)  # HALFTONE — kichraytirganda sifatli
        gdi32.StretchBlt(mdc, 0, 0, ow, oh, sdc, sx, sy, w, h, 0x00CC0020 | 0x40000000)  # SRCCOPY|CAPTUREBLT
        bih = BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bih.biWidth, bih.biHeight, bih.biPlanes, bih.biBitCount = ow, -oh, 1, 24
        stride = (ow * 3 + 3) & ~3
        raw = ctypes.create_string_buffer(stride * oh)
        gdi32.GetDIBits(mdc, bmp, 0, oh, raw, ctypes.byref(bih), 0)
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(None, sdc)
    data = raw.raw
    rows = bytearray()
    for y in range(oh):
        row = data[y * stride: y * stride + ow * 3]
        rgb = bytearray(row)
        rgb[0::3], rgb[2::3] = row[2::3], row[0::3]   # BGR -> RGB
        rows += b"\x00" + rgb

    def chunk(tag, payload):
        c = tag + payload
        return struct.pack(">I", len(payload)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", ow, oh, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(rows), 6)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------- "kompyuter klub" bloki
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC), ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int), ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR), ("lpszClassName", wt.LPCWSTR),
                ("hIconSm", wt.HICON)]


class PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wt.HDC), ("fErase", wt.BOOL), ("rcPaint", wt.RECT), ("fRestore", wt.BOOL),
                ("fIncUpdate", wt.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.RegisterClassExW.restype = wt.ATOM
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.CreateWindowExW.restype = wt.HWND
user32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID]
user32.DestroyWindow.argtypes = [wt.HWND]
user32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SetWindowPos.argtypes = [wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT]
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.BeginPaint.restype = wt.HDC
user32.BeginPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.EndPaint.argtypes = [wt.HWND, ctypes.POINTER(PAINTSTRUCT)]
user32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.FillRect.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH]
user32.DrawTextW.argtypes = [wt.HDC, wt.LPCWSTR, ctypes.c_int, ctypes.POINTER(wt.RECT), wt.UINT]
user32.InvalidateRect.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
user32.LoadCursorW.restype = wt.HANDLE
user32.LoadCursorW.argtypes = [wt.HINSTANCE, ctypes.c_void_p]
kernel32.GetModuleHandleW.restype = wt.HMODULE
kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
gdi32.CreateSolidBrush.restype = wt.HBRUSH
gdi32.CreateSolidBrush.argtypes = [wt.COLORREF]
gdi32.CreateFontW.restype = wt.HFONT
gdi32.CreateFontW.argtypes = [ctypes.c_int] * 5 + [wt.DWORD] * 8 + [wt.LPCWSTR]
gdi32.SetTextColor.argtypes = [wt.HDC, wt.COLORREF]
gdi32.SetBkMode.argtypes = [wt.HDC, ctypes.c_int]

WM_DESTROY, WM_PAINT, WM_CLOSE, WM_TIMER, WM_ERASEBKGND = 0x0002, 0x000F, 0x0010, 0x0113, 0x0014
WM_SYSCOMMAND, WM_QUIT, WM_APP_STOP, WM_APP_TEXT = 0x0112, 0x0012, 0x8001, 0x8002
TASKMGR_KEY = r"Software\Microsoft\Windows\CurrentVersion\Policies\System"


def rgb(r, g, b):
    return r | (g << 8) | (b << 16)


# Oyna klassi jarayonda BIR MARTA ro'yxatga olinadi va doimiy WNDPROC ishlatadi.
# (Har blokda yangi callback bilan qayta ro'yxatdan o'tkazish ikkinchi blokda agentni yiqitardi:
# klass eski, o'chirilgan callback'ga ishora qilib qolardi.)
_BLOCK_FN = {"fn": None}


def _static_wndproc(hwnd, msg, wparam, lparam):
    fn = _BLOCK_FN["fn"]
    if fn is not None:
        try:
            return fn(hwnd, msg, wparam, lparam)
        except Exception:
            log("BLOK oyna xatosi", traceback.format_exc())
    return user32.DefWindowProcW(hwnd, msg, wparam, lparam)


_STATIC_PROC = WNDPROC(_static_wndproc)
_CLASS = {"ok": False, "brush": None}

# ---------------------------------------------------------------- blok ekrani dizayni (GDI+)
LOGO_PATH = os.path.join(STATE_DIR, "logo-w.png")
user32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.COLORREF, ctypes.c_ubyte, wt.DWORD]
user32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
gdi32.BitBlt.argtypes = [wt.HDC] + [ctypes.c_int] * 4 + [wt.HDC, ctypes.c_int, ctypes.c_int, wt.DWORD]
VP = ctypes.c_void_p
F = ctypes.c_float
try:
    gdip = ctypes.WinDLL("gdiplus")
    for _n, _a in {"GdipCreateFromHDC": [wt.HDC, ctypes.POINTER(VP)], "GdipSetSmoothingMode": [VP, ctypes.c_int],
                   "GdipSetInterpolationMode": [VP, ctypes.c_int], "GdipGraphicsClear": [VP, ctypes.c_uint32],
                   "GdipCreatePath": [ctypes.c_int, ctypes.POINTER(VP)], "GdipAddPathEllipse": [VP, F, F, F, F],
                   "GdipAddPathArc": [VP, F, F, F, F, F, F], "GdipClosePathFigure": [VP],
                   "GdipCreatePathGradientFromPath": [VP, ctypes.POINTER(VP)],
                   "GdipSetPathGradientCenterColor": [VP, ctypes.c_uint32],
                   "GdipSetPathGradientSurroundColorsWithCount": [VP, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_int)],
                   "GdipFillPath": [VP, VP, VP], "GdipDrawPath": [VP, VP, VP], "GdipCreateSolidFill": [ctypes.c_uint32, ctypes.POINTER(VP)],
                   "GdipCreatePen1": [ctypes.c_uint32, F, ctypes.c_int, ctypes.POINTER(VP)],
                   "GdipSetPenStartCap": [VP, ctypes.c_int], "GdipSetPenEndCap": [VP, ctypes.c_int],
                   "GdipDrawEllipse": [VP, VP, F, F, F, F], "GdipDrawLine": [VP, VP, F, F, F, F],
                   "GdipCreateBitmapFromFile": [ctypes.c_wchar_p, ctypes.POINTER(VP)],
                   "GdipGetImageWidth": [VP, ctypes.POINTER(ctypes.c_uint)], "GdipGetImageHeight": [VP, ctypes.POINTER(ctypes.c_uint)],
                   "GdipDrawImageRectI": [VP, VP, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int],
                   "GdipDeleteBrush": [VP], "GdipDeletePen": [VP], "GdipDeletePath": [VP], "GdipDisposeImage": [VP],
                   "GdipDeleteGraphics": [VP]}.items():
        getattr(gdip, _n).argtypes = _a

    class _GSI(ctypes.Structure):
        _fields_ = [("v", ctypes.c_uint32), ("cb", VP), ("s1", wt.BOOL), ("s2", wt.BOOL)]
    _gtok = ctypes.c_size_t()
    gdip.GdiplusStartup(ctypes.byref(_gtok), ctypes.byref(_GSI(1, None, 0, 0)), None)
except Exception:
    gdip = None


def argb(a, r, g, b):
    return (a << 24) | (r << 16) | (g << 8) | b


def _glow(g, cx, cy, rx, ry, color):
    """Markazdan chetga qarab yo'qolib boradigan yumshoq rangli dog' (brend fonidagi kabi)."""
    path, br = VP(), VP()
    gdip.GdipCreatePath(0, ctypes.byref(path))
    gdip.GdipAddPathEllipse(path, cx - rx, cy - ry, rx * 2, ry * 2)
    gdip.GdipCreatePathGradientFromPath(path, ctypes.byref(br))
    gdip.GdipSetPathGradientCenterColor(br, color)
    sur, n = (ctypes.c_uint32 * 1)(color & 0x00FFFFFF), ctypes.c_int(1)
    gdip.GdipSetPathGradientSurroundColorsWithCount(br, sur, ctypes.byref(n))
    gdip.GdipFillPath(g, br, path)
    gdip.GdipDeleteBrush(br)
    gdip.GdipDeletePath(path)


def _round_rect(x, y, w, h, r):
    path = VP()
    gdip.GdipCreatePath(0, ctypes.byref(path))
    d = r * 2
    gdip.GdipAddPathArc(path, x, y, d, d, 180, 90)
    gdip.GdipAddPathArc(path, x + w - d, y, d, d, 270, 90)
    gdip.GdipAddPathArc(path, x + w - d, y + h - d, d, d, 0, 90)
    gdip.GdipAddPathArc(path, x, y + h - d, d, d, 90, 90)
    gdip.GdipClosePathFigure(path)
    return path


def paint_block(hdc, vx, vy, vw, vh, msg, font_big, font):
    """Blok ekrani: to'q fon + brend rangidagi yorug'lik, shisha karta, logo, belgi, matn, soat.
    Ikki bosqichli chizish (xotirada, keyin bir martada ekranga) — miltillamaydi."""
    pw, ph = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)   # asosiy monitor
    sc = max(0.6, ph / 1080.0)
    ox, oy = -vx, -vy                                                   # asosiy monitorning oyna ichidagi joyi
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, vw, vh)
    old_bmp = gdi32.SelectObject(mdc, bmp)
    cw, chh = 820 * sc, 500 * sc
    cx0, cy0 = ox + (pw - cw) / 2, oy + (ph - chh) / 2
    try:
        if gdip:
            g = VP()
            gdip.GdipCreateFromHDC(mdc, ctypes.byref(g))
            gdip.GdipSetSmoothingMode(g, 4)
            gdip.GdipSetInterpolationMode(g, 7)
            gdip.GdipGraphicsClear(g, argb(255, 14, 12, 16))
            for mx_, my_ in ((ox, oy), ):   # asosiy monitor; qolganlari ham to'q fon bilan yopiladi
                _glow(g, mx_ + pw * 0.12, my_ + ph * 0.05, pw * 0.55, ph * 0.6, argb(150, 237, 106, 35))
                _glow(g, mx_ + pw * 0.95, my_ + ph * 0.15, pw * 0.45, ph * 0.55, argb(110, 243, 150, 20))
                _glow(g, mx_ + pw * 0.65, my_ + ph * 1.05, pw * 0.5, ph * 0.5, argb(80, 122, 90, 240))
            # shisha karta
            card = _round_rect(cx0, cy0, cw, chh, 34 * sc)
            fill, pen = VP(), VP()
            gdip.GdipCreateSolidFill(argb(34, 255, 255, 255), ctypes.byref(fill))
            gdip.GdipFillPath(g, fill, card)
            gdip.GdipCreatePen1(argb(70, 255, 255, 255), 1.5 * sc, 2, ctypes.byref(pen))
            gdip.GdipDrawPath(g, pen, card)
            gdip.GdipDeleteBrush(fill)
            gdip.GdipDeletePen(pen)
            gdip.GdipDeletePath(card)
            # logo (oq)
            ly = cy0 + 46 * sc
            if os.path.exists(LOGO_PATH):
                img, iw, ih = VP(), ctypes.c_uint(), ctypes.c_uint()
                if gdip.GdipCreateBitmapFromFile(LOGO_PATH, ctypes.byref(img)) == 0:
                    gdip.GdipGetImageWidth(img, ctypes.byref(iw))
                    gdip.GdipGetImageHeight(img, ctypes.byref(ih))
                    lh = int(64 * sc)
                    lw = int(iw.value * lh / max(1, ih.value))
                    gdip.GdipDrawImageRectI(g, img, int(cx0 + (cw - lw) / 2), int(ly), lw, lh)
                    gdip.GdipDisposeImage(img)
            # "taqiq" belgisi — brend to'q sarig'ida
            r_, icx, icy = 34 * sc, cx0 + cw / 2, cy0 + 196 * sc
            ring = VP()
            gdip.GdipCreatePen1(argb(255, 242, 131, 34), 7 * sc, 2, ctypes.byref(ring))
            gdip.GdipSetPenStartCap(ring, 2)
            gdip.GdipSetPenEndCap(ring, 2)
            gdip.GdipDrawEllipse(g, ring, icx - r_, icy - r_, r_ * 2, r_ * 2)
            k = r_ * 0.7071
            gdip.GdipDrawLine(g, ring, icx - k, icy - k, icx + k, icy + k)
            gdip.GdipDeletePen(ring)
            gdip.GdipDeleteGraphics(g)
        else:
            user32.FillRect(mdc, ctypes.byref(wt.RECT(0, 0, vw, vh)), _CLASS["brush"])
        # matnlar (GDI — ClearType bilan aniq)
        gdi32.SetBkMode(mdc, 1)
        old_f = gdi32.SelectObject(mdc, font_big)
        gdi32.SetTextColor(mdc, rgb(255, 255, 255))
        box = wt.RECT(int(cx0), int(cy0 + 248 * sc), int(cx0 + cw), int(cy0 + 320 * sc))
        user32.DrawTextW(mdc, "Kompyuter bloklangan", -1, ctypes.byref(box), 0x1 | 0x4 | 0x20)
        gdi32.SelectObject(mdc, font)
        gdi32.SetTextColor(mdc, rgb(236, 226, 218))
        box = wt.RECT(int(cx0 + 60 * sc), int(cy0 + 336 * sc), int(cx0 + cw - 60 * sc), int(cy0 + 420 * sc))
        user32.DrawTextW(mdc, msg or "Rahbar tomonidan bloklandi.", -1, ctypes.byref(box), 0x1 | 0x10 | 0x40000)
        gdi32.SetTextColor(mdc, rgb(170, 158, 150))
        box = wt.RECT(int(cx0), int(cy0 + chh - 64 * sc), int(cx0 + cw), int(cy0 + chh - 26 * sc))
        user32.DrawTextW(mdc, time.strftime("%H:%M") + "   ·   Ochish uchun rahbarga murojaat qiling", -1,
                         ctypes.byref(box), 0x1 | 0x4 | 0x20)
        gdi32.SelectObject(mdc, old_f)
        gdi32.BitBlt(hdc, 0, 0, vw, vh, mdc, 0, 0, 0x00CC0020)
    finally:
        gdi32.SelectObject(mdc, old_bmp)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)


def fetch_logo(base):
    """Blok ekrani uchun oq logo (bir marta yuklab olinadi)."""
    if os.path.exists(LOGO_PATH) and os.path.getsize(LOGO_PATH) > 1000:
        return
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/brand/logo-w.png", timeout=30) as r:
            data = r.read()
        if data.startswith(b"\x89PNG"):
            os.makedirs(STATE_DIR, exist_ok=True)
            with open(LOGO_PATH, "wb") as f:
                f.write(data)
    except Exception as e:
        log("Logo yuklanmadi:", e)


class Blocker:
    """Butun ekranni (hamma monitorlar) yopadigan, eng ustida turadigan oyna.
    Klaviatura bosilishlari (Win, Alt+Tab, Alt+F4, Ctrl+Shift+Esc...) yutiladi, Диспетчер задач o'chiriladi.
    Faqat rahbar serverdan "ochish" qilganda yopiladi."""

    def __init__(self, state):
        self.state = state
        self.hwnd = None
        self.thread = None
        self.msg = ""
        self.lock = threading.Lock()
        self._keep = []    # ctypes callback'lar yig'ib olinmasin

    @property
    def on(self):
        return self.thread is not None and self.thread.is_alive()

    def set(self, on, msg=""):
        msg = (msg or "")[:300]
        with self.lock:
            if on and not self.on:
                self.msg = msg
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()
                log("BLOK yoqildi")
            elif on and msg != self.msg:
                self.msg = msg
                if self.hwnd:
                    user32.PostMessageW(self.hwnd, WM_APP_TEXT, 0, 0)
            elif not on and self.on:
                if self.hwnd:
                    user32.PostMessageW(self.hwnd, WM_APP_STOP, 0, 0)
                self.thread.join(5)
                self.thread = None
                log("BLOK ochildi")
        if self.state.get("blocked") != bool(on) or self.state.get("block_msg") != msg:
            self.state["blocked"], self.state["block_msg"] = bool(on), msg
            save_state(self.state)
        self._taskmgr(bool(on))

    def _taskmgr(self, disable):
        """Blok paytida Диспетчер задач ochilmasin (HKCU siyosati; faqat o'zimiz qo'ygan bo'lsak olib tashlaymiz)."""
        try:
            import winreg
            if disable and not self.state.get("tm_set"):
                k = winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, TASKMGR_KEY, 0,
                                       winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE)
                try:
                    self.state["tm_prev"] = winreg.QueryValueEx(k, "DisableTaskMgr")[0]   # avvalgi qiymat qaytariladi
                except OSError:
                    self.state["tm_prev"] = None
                winreg.SetValueEx(k, "DisableTaskMgr", 0, winreg.REG_DWORD, 1)
                winreg.CloseKey(k)
                self.state["tm_set"] = True
                save_state(self.state)
            elif not disable and self.state.get("tm_set"):
                k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, TASKMGR_KEY, 0, winreg.KEY_SET_VALUE)
                prev = self.state.get("tm_prev")
                try:
                    if prev is None:
                        winreg.DeleteValue(k, "DisableTaskMgr")
                    else:
                        winreg.SetValueEx(k, "DisableTaskMgr", 0, winreg.REG_DWORD, int(prev))
                except OSError:
                    pass
                winreg.CloseKey(k)
                self.state["tm_set"] = False
                save_state(self.state)
        except Exception as e:
            log("TaskMgr siyosati:", e)

    def _run(self):
        hinst = kernel32.GetModuleHandleW(None)
        if _CLASS["brush"] is None:
            _CLASS["brush"] = gdi32.CreateSolidBrush(rgb(17, 12, 9))
        bg = _CLASS["brush"]
        x, y = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        w, h = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        if w <= 0 or h <= 0:
            x, y, w, h = 0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
        sc = max(0.6, user32.GetSystemMetrics(1) / 1080.0)          # asosiy monitor balandligiga moslash
        font_big = gdi32.CreateFontW(-int(54 * sc), 0, 0, 0, 600, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI Semibold")
        font = gdi32.CreateFontW(-int(24 * sc), 0, 0, 0, 400, 0, 0, 0, 1, 0, 0, 5, 0, "Segoe UI")
        fade = [0, 32]                                                # [alfa, qadam]

        def kb_hook(code, wparam, lparam):
            if code == 0:
                return 1          # blok paytida hamma tugmalar yutiladi (Ctrl+Alt+Del dan tashqari — uni Windows o'zi boshqaradi)
            return user32.CallNextHookEx(None, code, wparam, lparam)

        def wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_PAINT:
                ps = PAINTSTRUCT()
                hdc = user32.BeginPaint(hwnd, ctypes.byref(ps))
                try:
                    paint_block(hdc, x, y, w, h, self.msg, font_big, font)
                except Exception:
                    log("BLOK chizish xatosi", traceback.format_exc())
                user32.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            if msg == WM_ERASEBKGND:
                return 1
            if msg == WM_CLOSE or (msg == WM_SYSCOMMAND and (wparam & 0xFFF0) == 0xF060):
                return 0          # Alt+F4 / yopish — e'tiborsiz
            if msg == WM_TIMER:
                if wparam == 1:
                    user32.SetWindowPos(hwnd, wt.HWND(-1), x, y, w, h, 0x0040 | 0x0010)  # TOPMOST, SHOWWINDOW|NOACTIVATE
                elif wparam == 2:
                    user32.InvalidateRect(hwnd, None, False)        # soat yangilansin
                elif wparam == 3:                                   # silliq paydo bo'lish / yo'qolish
                    fade[0] = max(0, min(255, fade[0] + fade[1]))
                    user32.SetLayeredWindowAttributes(hwnd, 0, fade[0], 2)
                    if fade[0] in (0, 255):
                        user32.KillTimer(hwnd, 3)
                        if fade[0] == 0:
                            user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_APP_TEXT:
                user32.InvalidateRect(hwnd, None, False)
                return 0
            if msg == WM_APP_STOP:
                fade[1] = -40
                user32.SetTimer(hwnd, 3, 16, None)
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        hook_cb = HOOKPROC(kb_hook)
        self._keep = [hook_cb]
        _BLOCK_FN["fn"] = wndproc
        if not _CLASS["ok"]:
            wc = WNDCLASSEXW()
            wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
            wc.lpfnWndProc = _STATIC_PROC
            wc.hInstance = hinst
            wc.hCursor = user32.LoadCursorW(None, ctypes.c_void_p(32512))
            wc.hbrBackground = bg
            wc.lpszClassName = "GigantAgentBlock"
            if not user32.RegisterClassExW(ctypes.byref(wc)):
                log("BLOK klassi ro'yxatga olinmadi", ctypes.get_last_error())
                return
            _CLASS["ok"] = True
        # WS_EX_TOPMOST | WS_EX_TOOLWINDOW (Alt+Tab ro'yxatida ko'rinmaydi) | WS_EX_LAYERED (silliq paydo bo'lish)
        hwnd = user32.CreateWindowExW(0x8 | 0x80 | 0x80000, "GigantAgentBlock", "Gigant blok", 0x80000000,
                                      x, y, w, h, None, None, hinst, None)
        if not hwnd:
            log("BLOK oynasi yaratilmadi", ctypes.get_last_error())
            return
        self.hwnd = hwnd
        user32.SetLayeredWindowAttributes(hwnd, 0, 0, 2)
        user32.ShowWindow(hwnd, 5)
        hook = user32.SetWindowsHookExW(13, hook_cb, hinst, 0)   # WH_KEYBOARD_LL
        user32.SetTimer(hwnd, 1, 400, None)
        user32.SetTimer(hwnd, 2, 20000, None)
        user32.SetTimer(hwnd, 3, 16, None)
        user32.SetForegroundWindow(hwnd)
        m = wt.MSG()
        while user32.GetMessageW(ctypes.byref(m), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(m))
            user32.DispatchMessageW(ctypes.byref(m))
        if hook:
            user32.UnhookWindowsHookEx(hook)
        _BLOCK_FN["fn"] = None
        gdi32.DeleteObject(font_big)
        gdi32.DeleteObject(font)
        self.hwnd = None


# ---------------------------------------------------------------- jonli ekran (JPEG, GDI+)
class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort), ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]


class _EncParam(ctypes.Structure):
    _fields_ = [("guid", _GUID), ("n", ctypes.c_ulong), ("type", ctypes.c_ulong), ("value", ctypes.c_void_p)]


class _EncParams(ctypes.Structure):
    _fields_ = [("count", ctypes.c_uint), ("p", _EncParam * 1)]


ole32 = ctypes.WinDLL("ole32")
ole32.CreateStreamOnHGlobal.argtypes = [ctypes.c_void_p, wt.BOOL, ctypes.POINTER(ctypes.c_void_p)]
ole32.CLSIDFromString.argtypes = [wt.LPCWSTR, ctypes.POINTER(_GUID)]
ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, wt.DWORD]
ole32.CoCreateInstance.argtypes = [ctypes.POINTER(_GUID), ctypes.c_void_p, wt.DWORD, ctypes.POINTER(_GUID),
                                   ctypes.POINTER(ctypes.c_void_p)]
ole32.CoCreateInstance.restype = ctypes.c_long
oleaut32 = ctypes.WinDLL("oleaut32")
oleaut32.VariantClear.argtypes = [ctypes.c_void_p]


def _guid(s):
    g = _GUID()
    ole32.CLSIDFromString(s, ctypes.byref(g))
    return g


_JPEG_CLSID = _guid("{557CF401-1A04-11D3-9A73-0000F81EF32E}")
_QUALITY = _guid("{1D5BE4B5-FA4A-452D-9CDD-5DB35105E7EB}")
if gdip:
    gdip.GdipCreateBitmapFromScan0.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_void_p)]
    gdip.GdipSaveImageToStream.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_GUID), ctypes.c_void_p]


def _com(obj, idx, restype, *argtypes):
    vt = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)(vt[idx])


def capture_bgr(max_w):
    """Ekran (hamma monitorlar) -> (bgr baytlar, eni, bo'yi, qator uzunligi)."""
    sx, sy = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    w, h = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
    if w <= 0 or h <= 0:
        w, h, sx, sy = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1), 0, 0
    scale = min(1.0, max_w / float(w))
    ow, oh = max(1, int(w * scale)), max(1, int(h * scale))
    sdc = user32.GetDC(None)
    mdc = gdi32.CreateCompatibleDC(sdc)
    bmp = gdi32.CreateCompatibleBitmap(sdc, ow, oh)
    old = gdi32.SelectObject(mdc, bmp)
    try:
        gdi32.SetStretchBltMode(mdc, 4)
        gdi32.StretchBlt(mdc, 0, 0, ow, oh, sdc, sx, sy, w, h, 0x00CC0020 | 0x40000000)
        bih = BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bih.biWidth, bih.biHeight, bih.biPlanes, bih.biBitCount = ow, -oh, 1, 24
        stride = (ow * 3 + 3) & ~3
        raw = ctypes.create_string_buffer(stride * oh)
        gdi32.GetDIBits(mdc, bmp, 0, oh, raw, ctypes.byref(bih), 0)
    finally:
        gdi32.SelectObject(mdc, old)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mdc)
        user32.ReleaseDC(None, sdc)
    return raw, ow, oh, stride


def screenshot_jpeg(max_w=1280, quality=55):
    """Jonli ko'rish uchun tez va kichik JPEG (~60–150 KB)."""
    raw, ow, oh, stride = capture_bgr(max_w)
    img, stream = ctypes.c_void_p(), ctypes.c_void_p()
    if gdip.GdipCreateBitmapFromScan0(ow, oh, stride, 0x00021808, raw, ctypes.byref(img)) != 0:  # 24bppRGB (BGR)
        raise OSError("GDI+ bitmap")
    try:
        ole32.CreateStreamOnHGlobal(None, True, ctypes.byref(stream))
        q = ctypes.c_ulong(quality)
        params = _EncParams(1, (_EncParam * 1)(_EncParam(_QUALITY, 1, 4, ctypes.cast(ctypes.byref(q), ctypes.c_void_p))))
        if gdip.GdipSaveImageToStream(img, stream, ctypes.byref(_JPEG_CLSID), ctypes.byref(params)) != 0:
            raise OSError("JPEG saqlanmadi")
        size = ctypes.c_ulonglong()
        _com(stream, 5, ctypes.c_long, ctypes.c_longlong, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulonglong))(
            stream, 0, 1, ctypes.byref(size))                           # Seek(0, CUR) -> hajm
        _com(stream, 5, ctypes.c_long, ctypes.c_longlong, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulonglong))(
            stream, 0, 0, None)                                          # boshiga
        buf = ctypes.create_string_buffer(size.value)
        got = ctypes.c_ulong()
        _com(stream, 3, ctypes.c_long, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong))(
            stream, buf, size.value, ctypes.byref(got))                  # Read
        return buf.raw[:got.value]
    finally:
        gdip.GdipDisposeImage(img)
        if stream:
            _com(stream, 2, ctypes.c_ulong)(stream)                      # Release


# ---------------------------------------------------------------- server bilan aloqa
class Client:
    def __init__(self, conf, state):
        self.conf, self.state = conf, state
        self.base = conf.get("server", "").rstrip("/")
        found = state.get("server_url")
        if found and state.get("server_base") == self.base:
            self.base = found
        if "trycloudflare.com" in self.base or self.base.endswith("gigantbuxgalter.com") and self.base != STABLE_URL:
            self.base = STABLE_URL          # vaqtinchalik / eski manzil o'rniga doimiy domen

    def _post(self, path, data, timeout=25):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=body, method="POST",
                                     headers={"Content-Type": "application/json",
                                              "User-Agent": f"GigantAgent/{VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    def discover(self):
        """Tunnel manzili o'zgarsa — yangisini serverning doimiy IP manzilidan olamiz (javob imzosi tekshiriladi)."""
        disc, dkey = self.conf.get("disc", ""), self.conf.get("dkey", "")
        if not disc or not dkey:
            return None
        try:
            import hmac
            with urllib.request.urlopen(f"{disc}?_={int(time.time())}", timeout=20) as r:
                d = json.loads(r.read() or b"{}")
            url = str(d.get("url") or "")
            good = hmac.new(dkey.encode(), url.encode(), "sha256").hexdigest()
            if re.match(r"^https://([a-z0-9-]+\.trycloudflare\.com|((panel|www)\.)?gigantbuxgalter\.com)$", url) and \
                    hmac.compare_digest(good, str(d.get("sig"))):
                return url
            log("Manzil imzosi noto'g'ri — e'tiborsiz")
        except Exception:
            pass
        return None

    def post(self, path, data, timeout=25):
        try:
            return self._post(path, data, timeout)
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 413):
                raise
            err = e
        except Exception as e:
            err = e
        fresh = self.discover() or (STABLE_URL if self.base != STABLE_URL else None)
        if not fresh or fresh == self.base:
            raise err
        log("Yangi server manzili:", fresh)
        self.base = fresh
        self.state["server_url"], self.state["server_base"] = fresh, self.conf.get("server", "").rstrip("/")
        return self._post(path, data, timeout)


# ---------------------------------------------------------------- buyruqlar
def show_message(text):
    def run():
        # MB_OK | MB_ICONINFORMATION | MB_SYSTEMMODAL | MB_SETFOREGROUND | MB_TOPMOST
        user32.MessageBoxW(None, text, "Rahbardan xabar", 0x0 | 0x40 | 0x1000 | 0x10000 | 0x40000)
    threading.Thread(target=run, daemon=True).start()


def run_cmd(c, client, pc):
    cmd, arg = c.get("cmd"), (c.get("arg") or "")
    log("BUYRUQ", cmd, arg[:80])
    no_window = 0x08000000  # CREATE_NO_WINDOW
    if cmd == "lock":
        if arg:
            show_message(arg)
            time.sleep(3)
        user32.LockWorkStation()
    elif cmd in ("shutdown", "restart"):
        flag = "/s" if cmd == "shutdown" else "/r"
        note = arg or ("Kompyuter rahbar buyrug'i bilan o'chiriladi" if cmd == "shutdown"
                       else "Kompyuter rahbar buyrug'i bilan qayta yoqiladi")
        subprocess.Popen(["shutdown", flag, "/t", "60", "/c", note[:500]], creationflags=no_window)
    elif cmd == "cancel":
        subprocess.Popen(["shutdown", "/a"], creationflags=no_window)
    elif cmd == "logoff":
        subprocess.Popen(["shutdown", "/l"], creationflags=no_window)
    elif cmd == "msg":
        show_message(arg)
    elif cmd == "shot":
        import base64
        png = screenshot_png()
        client.post("/api/shot", {"key": client.conf.get("key"), "pc": pc, "id": c.get("id"),
                                  "png": base64.b64encode(png).decode()}, timeout=90)


# ---------------------------------------------------------------- asosiy sikl
def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # skrinshot to'liq o'lchamda bo'lsin
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass
    kernel32.CreateMutexW(None, False, "Local\\GigantAgentMutex")
    if ctypes.get_last_error() == 183:   # ERROR_ALREADY_EXISTS — agent allaqachon ishlayapti
        return
    conf = load_json(CONF_PATH, {})
    if not conf.get("key") or not conf.get("server"):
        log("Sozlama topilmadi yoki to'liq emas:", CONF_PATH)
        return
    state = load_json(STATE_PATH, {})
    pending = state.get("pending") or []
    client = Client(conf, state)
    pc = os.environ.get("COMPUTERNAME", "PC")
    user = os.environ.get("USERNAME", "")
    log("Agent ishga tushdi", VERSION, pc, user, client.base)
    threading.Thread(target=fetch_logo, args=(client.base,), daemon=True).start()
    blocker = Blocker(state)
    if state.get("blocked"):          # o'chirib-yoqilgan bo'lsa ham blok darhol qaytadi
        blocker.set(True, state.get("block_msg", ""))
    elif state.get("tm_set"):
        blocker.set(False)

    shared = {"now": {}, "done": [], "lock": threading.Lock()}

    def handle(res):
        """Server javobi: blok holati + buyruqlar."""
        av = res.get("agent_ver")
        if av and av != VERSION and not shared.get("restart"):
            try:
                with open(os.path.abspath(__file__), encoding="utf-8") as f:
                    on_disk = (re.search(r'VERSION = "([\d.]+)"', f.read()) or [None, None])[1]
            except OSError:
                on_disk = None
            if on_disk == av:          # yangilovchi faylni allaqachon almashtirgan — qayta ishga tushamiz
                shared["restart"] = True
        if res.get("live") and gdip and not shared.get("live"):
            shared["live"] = True
            threading.Thread(target=live_loop, daemon=True).start()
        b = res.get("block")
        changed = bool(res.get("cmds"))
        if isinstance(b, dict):
            changed = changed or bool(b.get("on")) != blocker.on
            blocker.set(bool(b.get("on")), str(b.get("msg") or ""))
        for c in res.get("cmds") or []:
            try:
                run_cmd(c, client, pc)
            except Exception:
                log("BUYRUQ XATO", traceback.format_exc())
            with shared["lock"]:
                shared["done"].append(c.get("id"))
        return changed

    def live_loop():
        """Rahbar dashboardda hodim oynasini ochib turganda ekranni soniyasiga ~2–3 kadr yuboradi.
        Server "live": false desa (oyna yopildi) — to'xtaydi."""
        from urllib.parse import quote
        log("Jonli ekran boshlandi")
        fails = 0
        iv, wmax, qual = 0.35, 1280, 55      # server javobiga qarab o'zgaradi (devor — kichik/siyrak, butun ekran — sifatli)
        try:
            while True:
                t0 = time.time()
                try:
                    jpg = screenshot_jpeg(wmax, qual)
                    req = urllib.request.Request(client.base + "/api/frame", data=jpg, method="POST", headers={
                        "Content-Type": "image/jpeg", "X-Key": conf["key"], "X-Pc": quote(pc), "X-User": quote(user),
                        "User-Agent": f"GigantAgent/{VERSION}"})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        res = json.loads(r.read() or b"{}")
                    fails = 0
                    if not res.get("live"):
                        break
                    try:
                        iv = max(0.2, min(float(res.get("interval", iv)), 10.0))
                        wmax = max(320, min(int(res.get("w", wmax)), 1920))
                        qual = max(20, min(int(res.get("q", qual)), 90))
                    except (TypeError, ValueError):
                        pass
                except Exception as e:
                    fails += 1
                    if fails >= 5:
                        log("Jonli ekran xatosi:", e)
                        break
                    time.sleep(1)
                time.sleep(max(0.05, iv - (time.time() - t0)))
        finally:
            shared["live"] = False
            log("Jonli ekran to'xtadi")

    def rec_loop():
        """Ekran yozuvi (kuzatuv kamerasi kabi): har REC_SEC soniyada kichik JPEG kadr serverga.
        Ekran qulflangan / bloklangan paytda yozilmaydi; hodim harakatsiz bo'lsa — siyrakroq.
        Server javobi: {"rec": bool, "interval", "idle_interval", "w", "q"}; o'chirilgan bo'lsa 5 daqiqada qayta so'raladi."""
        from urllib.parse import quote
        iv, wmax, qual, idle_iv = REC_SEC, 960, 45, 60
        nxt = 0.0
        while True:
            time.sleep(1)
            try:
                if time.time() < nxt or screen_locked() or blocker.on:
                    continue
                idle = idle_seconds()
                t = time.time()
                nxt = t + (idle_iv if idle >= IDLE_LIMIT else iv)
                jpg = screenshot_jpeg(wmax, qual)
                req = urllib.request.Request(client.base + "/api/rec", data=jpg, method="POST", headers={
                    "Content-Type": "image/jpeg", "X-Key": conf["key"], "X-Pc": quote(pc), "X-User": quote(user),
                    "X-Ts": str(int(t)), "User-Agent": f"GigantAgent/{VERSION}"})
                with urllib.request.urlopen(req, timeout=20) as r:
                    res = json.loads(r.read() or b"{}")
                if not res.get("rec"):
                    nxt = time.time() + 300           # yozuv o'chirilgan — 5 daqiqadan keyin qayta so'raymiz
                    continue
                iv = max(3, min(int(res.get("interval", iv)), 120))
                idle_iv = max(iv, min(int(res.get("idle_interval", idle_iv)), 600))
                wmax = max(480, min(int(res.get("w", wmax)), 1600))
                qual = max(20, min(int(res.get("q", qual)), 80))
            except urllib.error.HTTPError as e:
                nxt = time.time() + (300 if e.code == 404 else 20)
            except Exception:
                nxt = time.time() + 20                # internet yo'q — kadr o'tkazib yuboriladi

    if gdip:
        threading.Thread(target=rec_loop, daemon=True).start()

    def take_done():
        with shared["lock"]:
            d, shared["done"] = shared["done"], []
        return d

    kernel32.GetTickCount64.restype = ctypes.c_ulonglong
    astart = int(time.time())

    def now_info():
        n = dict(shared["now"])
        n["blocked"] = blocker.on
        n["boot"] = int(time.time() - kernel32.GetTickCount64() / 1000)   # Windows yoqilgan vaqt
        n["astart"] = astart                                                # agent ishga tushgan vaqt
        return n

    def wait_loop():
        """Doimiy kutish so'rovi: rahbar tugmani bosishi bilan buyruq shu yerga keladi (1–2 soniyada)."""
        while True:
            t0 = time.time()
            done = take_done()
            try:
                res = client.post("/api/wait", {"key": conf["key"], "pc": pc, "user": user, "ver": VERSION,
                                                "now": now_info(), "done": done}, timeout=45)
                if not handle(res) and time.time() - t0 < 2:   # o'zgarish bo'lmay tez qaytdi — serverni qiynamaylik
                    time.sleep(3)
            except urllib.error.HTTPError as e:
                with shared["lock"]:
                    shared["done"] = done + shared["done"]
                time.sleep(60 if e.code == 404 else 15)
            except Exception:
                with shared["lock"]:
                    shared["done"] = done + shared["done"]
                time.sleep(15)

    threading.Thread(target=wait_loop, daemon=True).start()

    buckets = {}     # (minute_ts, app, title) -> [secs, active_secs, url]
    rest = {}        # yaxlitlashda qolgan soniya ulushlari (10 soniyada yuborilganda yo'qolmasin)
    urls = UrlReader()
    last_flush = 0.0
    last_tick = time.time()
    while True:
        try:
            t = time.time()
            dt = min(max(t - last_tick, 0), SAMPLE_SEC * 3)   # uyqu rejimidan keyin katta oraliq hisoblanmasin
            last_tick = t
            idle = idle_seconds()
            url = ""
            if screen_locked():
                app, title, active = "LOCKED", "", False
            elif blocker.on:
                app, title, active = "BLOCKED", "", False
            else:
                app, title, hwnd = foreground()
                app, title = (app or "Desktop"), clean_title(title)
                active = idle < IDLE_LIMIT
                try:
                    url = urls.read(hwnd, app)
                except Exception:
                    log("URL o'qilmadi", traceback.format_exc())
            shared["now"] = {"app": app, "title": title, "url": url, "idle": int(idle), "locked": app == "LOCKED"}
            if dt > 0:
                k = (int(t // 60 * 60), app, title)
                b = buckets.setdefault(k, [0.0, 0.0, ""])
                b[0] += dt
                if active:
                    b[1] += dt
                if url:
                    b[2] = url

            if t - last_flush >= FLUSH_SEC:
                last_flush = t
                for k, v in buckets.items():
                    r = rest.pop(k, [0.0, 0.0])
                    s_, a_ = v[0] + r[0], v[1] + r[1]
                    if int(s_) > 0:
                        pending.append({"t": k[0], "app": k[1], "title": k[2], "s": int(s_), "a": min(int(a_), int(s_)),
                                        "u": v[2]})
                    if k[0] >= int(t // 60 * 60):            # joriy daqiqa — qoldiq keyingi yuborishga o'tadi
                        rest[k] = [s_ - int(s_), a_ - int(min(int(a_), int(s_)))]
                rest = {k: v for k, v in rest.items() if k[0] >= int(t // 60 * 60)}
                buckets.clear()
                pending = pending[-MAX_PENDING:]
                done = take_done()
                try:
                    res = client.post("/api/activity", {"key": conf["key"], "pc": pc, "user": user, "ver": VERSION,
                                                        "now": now_info(), "items": pending[:1500], "done": done})
                    pending = pending[1500:]
                    handle(res)
                except urllib.error.HTTPError as e:
                    log("Server javobi:", e.code)
                    with shared["lock"]:
                        shared["done"] = done + shared["done"]
                except Exception as e:
                    log("Aloqa yo'q:", e)
                    with shared["lock"]:
                        shared["done"] = done + shared["done"]
                state["pending"] = pending
                save_state(state)
                if shared.get("restart") and not blocker.on:
                    # Yangi versiya: ma'lumot saqlandi, chiqamiz — vazifa rejalashtiruvchi 1 daqiqada yangisini yoqadi
                    log("Yangi versiya o'rnatilgan — agent qayta ishga tushirilmoqda")
                    os._exit(0)
        except Exception:
            log("XATO", traceback.format_exc())
        time.sleep(SAMPLE_SEC)


if __name__ == "__main__":
    main()
