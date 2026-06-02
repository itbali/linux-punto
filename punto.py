#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
linux-punto — Punto Switcher для Linux (X11 / KDE Plasma).

По глобальному хоткею:
  * если есть выделенный текст — транслитерирует его по раскладке
    (ghbdtn -> привет) и заменяет выделение;
  * если ничего не выделено — переключает системную раскладку
    на следующую в списке (KDE D-Bus).

Зависимости: только то, что уже есть в системе — libX11/libXtst (ctypes),
xclip, python3-dbus. Ничего ставить не нужно.
"""

import ctypes
import ctypes.util
import json
import os
import subprocess
import sys
import threading
import time

# ---------------------------------------------------------------------------
# Конфиг
# ---------------------------------------------------------------------------

CONFIG_DIR = os.path.expanduser("~/.config/punto-switcher")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DEFAULT_CONFIG = {
    # Хоткей, варианты:
    #   одиночная клавиша        — "Pause", "F12", "Menu"
    #   модификатор(ы)+клавиша   — "Ctrl+Space", "Ctrl+Shift+Z", "Alt+grave"
    #   два+ модификатора        — "Ctrl+Shift", "Alt+Shift" (чистый аккорд,
    #                              срабатывает на отпускание, как в Punto)
    "hotkey": "Pause",
    # Показывать всплывающее уведомление при действии.
    "notify": False,
    # После исправления выделенного текста ещё и переключить раскладку,
    # чтобы продолжать печатать в правильной.
    "switch_layout_after_fix": False,
    # Показывать всплывающий бейдж с раскладкой при переключении.
    "badge": True,
    # Показывать бейдж у каретки (AT-SPI); если позиция недоступна — у курсора.
    "badge_at_caret": True,
    # Слова, которые не трогать при исправлении (нижний регистр).
    "exceptions": [],
    # Приложения (подстроки WM_CLASS), где свитчер не работает.
    "disabled_apps": [],
}


# Состояние времени выполнения (меняется из меню трея).
STATE = {"enabled": True}

# Трекер каретки/выделения (AT-SPI); ставится в run_with_tray.
CARET = {"tracker": None}


def log(*a):
    print("[punto]", *a, file=sys.stderr, flush=True)


# Подробные логи каждого срабатывания — только при PUNTO_DEBUG=1.
DEBUG = os.environ.get("PUNTO_DEBUG", "") not in ("", "0", "false")


def dbg(*a):
    if DEBUG:
        log(*a)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg.update(json.load(f))
    except FileNotFoundError:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print("config error, using defaults:", e, file=sys.stderr)
    return cfg


def save_config(cfg):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    data = {k: cfg.get(k, DEFAULT_CONFIG[k]) for k in DEFAULT_CONFIG}
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


AUTOSTART_PATH = os.path.expanduser("~/.config/autostart/linux-punto.desktop")


def autostart_enabled():
    return os.path.exists(AUTOSTART_PATH)


def set_autostart(enabled):
    try:
        if enabled:
            os.makedirs(os.path.dirname(AUTOSTART_PATH), exist_ok=True)
            content = (
                "[Desktop Entry]\nType=Application\n"
                "Name=linux-punto (Punto Switcher)\n"
                "Exec=%s %s\nX-KDE-autostart-phase=2\nOnlyShowIn=KDE;\n"
                "Terminal=false\nNoDisplay=true\n"
                % (sys.executable, os.path.abspath(__file__)))
            with open(AUTOSTART_PATH, "w", encoding="utf-8") as f:
                f.write(content)
        elif os.path.exists(AUTOSTART_PATH):
            os.remove(AUTOSTART_PATH)
    except Exception as e:
        log("autostart error: %r" % e)


# ---------------------------------------------------------------------------
# История исправлений (с пинами, удалением, очисткой; хранится на диске)
# ---------------------------------------------------------------------------

HISTORY_PATH = os.path.join(CONFIG_DIR, "history.json")
HISTORY_MAX = 100  # сколько незакреплённых записей хранить


class History:
    def __init__(self, path=HISTORY_PATH, maxn=HISTORY_MAX):
        self.path = path
        self.maxn = maxn
        self.items = []   # [{original, fixed, ts, pinned}], новые в начале
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                self.items = [d for d in data
                              if isinstance(d, dict) and "fixed" in d]
        except Exception:
            self.items = []

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            log("history save error: %r" % e)

    def add(self, original, fixed, app=""):
        self.items.insert(0, {"original": original, "fixed": fixed, "app": app,
                              "ts": time.time(), "pinned": False})
        self._trim()
        self.save()

    def _trim(self):
        kept, n = [], 0
        for it in self.items:
            if it.get("pinned"):
                kept.append(it)
            else:
                n += 1
                if n <= self.maxn:
                    kept.append(it)
        self.items = kept

    def _index(self, item):
        for i, it in enumerate(self.items):
            if it is item:
                return i
        return -1

    def delete(self, item):
        i = self._index(item)
        if i >= 0:
            del self.items[i]
            self.save()

    def toggle_pin(self, item):
        i = self._index(item)
        if i >= 0:
            self.items[i]["pinned"] = not self.items[i].get("pinned")
            self.save()

    def clear(self):
        """Удаляет всё, кроме закреплённого."""
        self.items = [it for it in self.items if it.get("pinned")]
        self.save()

    def ordered(self):
        """Закреплённые сверху, остальные — по свежести (как добавлены)."""
        pinned = [it for it in self.items if it.get("pinned")]
        rest = [it for it in self.items if not it.get("pinned")]
        return pinned + rest


HISTORY = History()  # загружается с диска при старте


# ---------------------------------------------------------------------------
# Статистика (счётчики переключений/исправлений, топ слов)
# ---------------------------------------------------------------------------

STATS_PATH = os.path.join(CONFIG_DIR, "stats.json")


def _today():
    return time.strftime("%Y-%m-%d", time.localtime())


class Stats:
    def __init__(self, path=STATS_PATH):
        self.path = path
        self.data = {"switches": 0, "fixes": 0, "days": {}, "words": {}}
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self.data.update({k: d.get(k, self.data[k]) for k in self.data})
        except Exception:
            pass

    def save(self):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            log("stats save error: %r" % e)

    def _day(self):
        return self.data["days"].setdefault(_today(), {"switches": 0, "fixes": 0})

    def record_switch(self):
        self.data["switches"] += 1
        self._day()["switches"] += 1
        self.save()

    def record_fix(self, fixed):
        self.data["fixes"] += 1
        self._day()["fixes"] += 1
        w = (fixed or "").strip().lower()
        if w:
            self.data["words"][w] = self.data["words"].get(w, 0) + 1
        self.save()

    def top_words(self, n=10):
        return sorted(self.data["words"].items(), key=lambda kv: -kv[1])[:n]

    def reset(self):
        self.data = {"switches": 0, "fixes": 0, "days": {}, "words": {}}
        self.save()


STATS = Stats()


# ---------------------------------------------------------------------------
# Таблица транслитерации по физическим клавишам (ЙЦУКЕН <-> QWERTY)
# ---------------------------------------------------------------------------

_PAIRS = [
    ("qwertyuiop[]asdfghjkl;'zxcvbnm,./`",
     "йцукенгшщзхъфывапролджэячсмитьбю.ё"),
    ('QWERTYUIOP{}ASDFGHJKL:"ZXCVBNM<>?~',
     "ЙЦУКЕНГШЩЗХЪФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,Ё"),
    # символы под цифрами, отличающиеся в ru-раскладке
    ('@#$^&', '"№;:?'),
]

EN2RU = {}
for _en, _ru in _PAIRS:
    for _a, _b in zip(_en, _ru):
        EN2RU[_a] = _b
RU2EN = {v: k for k, v in EN2RU.items()}


def transliterate(text):
    """Перекодирует текст между раскладками по физическому положению клавиш."""
    has_cyr = any("Ѐ" <= ch <= "ӿ" for ch in text)
    table = RU2EN if has_cyr else EN2RU
    return "".join(table.get(ch, ch) for ch in text)


# ---------------------------------------------------------------------------
# X11 / XTEST через ctypes
# ---------------------------------------------------------------------------

x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
xtst = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")

x11.XOpenDisplay.restype = ctypes.c_void_p
x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
x11.XDefaultRootWindow.restype = ctypes.c_ulong
x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
x11.XStringToKeysym.restype = ctypes.c_ulong
x11.XStringToKeysym.argtypes = [ctypes.c_char_p]
x11.XKeysymToKeycode.restype = ctypes.c_ubyte
x11.XKeysymToKeycode.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x11.XGrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint,
                         ctypes.c_ulong, ctypes.c_int, ctypes.c_int, ctypes.c_int]
x11.XNextEvent.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
x11.XPending.restype = ctypes.c_int
x11.XPending.argtypes = [ctypes.c_void_p]
x11.XConnectionNumber.restype = ctypes.c_int
x11.XConnectionNumber.argtypes = [ctypes.c_void_p]
x11.XUngrabKey.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint,
                           ctypes.c_ulong]
x11.XCloseDisplay.argtypes = [ctypes.c_void_p]

# --- Работа с окнами (для «вставить в последнее активное поле») ---
ClientMessage = 33
SubstructureRedirectMask = 1 << 20
SubstructureNotifyMask = 1 << 19
RevertToParent = 2

x11.XInternAtom.restype = ctypes.c_ulong
x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
x11.XGetWindowProperty.restype = ctypes.c_int
x11.XGetWindowProperty.argtypes = [
    ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long,
    ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
    ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
    ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p)]
x11.XFree.argtypes = [ctypes.c_void_p]
x11.XSendEvent.restype = ctypes.c_int
x11.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                           ctypes.c_long, ctypes.c_void_p]
x11.XSetInputFocus.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                               ctypes.c_ulong]
x11.XRaiseWindow.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x11.XGetSelectionOwner.restype = ctypes.c_ulong
x11.XGetSelectionOwner.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x11.XQueryTree.restype = ctypes.c_int
x11.XQueryTree.argtypes = [
    ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
    ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
    ctypes.POINTER(ctypes.c_uint)]
XA_PRIMARY = 1


class XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("message_type", ctypes.c_ulong),
        ("format", ctypes.c_int),
        ("data_l", ctypes.c_long * 5),
        ("_pad", ctypes.c_char * 96),
    ]

# --- XRecord: пассивный мониторинг всех клавиш (для хоткея из модификаторов) ---
XRecordAllClients = 3


class XRecordRange8(ctypes.Structure):
    _fields_ = [("first", ctypes.c_ubyte), ("last", ctypes.c_ubyte)]


class XRecordRange16(ctypes.Structure):
    _fields_ = [("first", ctypes.c_ushort), ("last", ctypes.c_ushort)]


class XRecordExtRange(ctypes.Structure):
    _fields_ = [("ext_major", XRecordRange8), ("ext_minor", XRecordRange16)]


class XRecordRange(ctypes.Structure):
    _fields_ = [
        ("core_requests", XRecordRange8),
        ("core_replies", XRecordRange8),
        ("ext_requests", XRecordExtRange),
        ("ext_replies", XRecordExtRange),
        ("delivered_events", XRecordRange8),
        ("device_events", XRecordRange8),
        ("errors", XRecordRange8),
        ("client_started", ctypes.c_int),
        ("client_died", ctypes.c_int),
    ]


class XRecordInterceptData(ctypes.Structure):
    _fields_ = [
        ("id_base", ctypes.c_ulong),
        ("server_time", ctypes.c_ulong),
        ("client_seq", ctypes.c_ulong),
        ("category", ctypes.c_int),
        ("client_swapped", ctypes.c_int),
        ("data", ctypes.POINTER(ctypes.c_ubyte)),
        ("data_len", ctypes.c_ulong),
    ]


XRECORD_CB = ctypes.CFUNCTYPE(None, ctypes.c_void_p,
                              ctypes.POINTER(XRecordInterceptData))
xtst.XRecordAllocRange.restype = ctypes.POINTER(XRecordRange)
xtst.XRecordCreateContext.restype = ctypes.c_ulong
xtst.XRecordCreateContext.argtypes = [
    ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_ulong), ctypes.c_int,
    ctypes.POINTER(ctypes.POINTER(XRecordRange)), ctypes.c_int]
xtst.XRecordEnableContextAsync.restype = ctypes.c_int
xtst.XRecordEnableContextAsync.argtypes = [
    ctypes.c_void_p, ctypes.c_ulong, XRECORD_CB, ctypes.c_void_p]
xtst.XRecordProcessReplies.argtypes = [ctypes.c_void_p]
xtst.XRecordFreeData.argtypes = [ctypes.POINTER(XRecordInterceptData)]
xtst.XRecordDisableContext.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
xtst.XRecordFreeContext.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
x11.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
x11.XFlush.argtypes = [ctypes.c_void_p]
xtst.XTestFakeKeyEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint,
                                   ctypes.c_int, ctypes.c_ulong]

# Константы X11
KeyPress = 2
GrabModeAsync = 1
ShiftMask = 1 << 0
LockMask = 1 << 1      # CapsLock
ControlMask = 1 << 2
Mod1Mask = 1 << 3      # Alt
Mod2Mask = 1 << 4      # NumLock
Mod4Mask = 1 << 6      # Super / Meta
XK_Control_L = 0xFFE3
XK_c = 0x0063
XK_v = 0x0076

# Модификаторы хоткея: имя -> (X-маска, [keysym левого, правого]).
MOD_DEFS = {
    "Ctrl":  (ControlMask, [0xFFE3, 0xFFE4]),
    "Alt":   (Mod1Mask, [0xFFE9, 0xFFEA]),
    "Shift": (ShiftMask, [0xFFE1, 0xFFE2]),
    "Meta":  (Mod4Mask, [0xFFEB, 0xFFEC]),
}
MOD_ORDER = ["Ctrl", "Alt", "Shift", "Meta"]  # каноничный порядок в строке

# Все keysym модификаторов — чтобы «погасить» их перед синтетическим вводом.
ALL_MODIFIER_KEYSYMS = [0xFFE1, 0xFFE2, 0xFFE3, 0xFFE4, 0xFFE7, 0xFFE8,
                        0xFFE9, 0xFFEA, 0xFFEB, 0xFFEC, 0xFE03]

# Комбинации залипающих модификаторов, чтобы хоткей ловился при любом
# состоянии Caps/Num Lock.
MOD_COMBOS = [0, LockMask, Mod2Mask, LockMask | Mod2Mask]


class XKeyEvent(ctypes.Structure):
    # Достаточно большой буфер под XEvent (union, sizeof == 192 на x86-64).
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", ctypes.c_ulong),
        ("root", ctypes.c_ulong),
        ("subwindow", ctypes.c_ulong),
        ("time", ctypes.c_ulong),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("x_root", ctypes.c_int),
        ("y_root", ctypes.c_int),
        ("state", ctypes.c_uint),
        ("keycode", ctypes.c_uint),
        ("same_screen", ctypes.c_int),
        ("_pad", ctypes.c_char * 96),
    ]


BAD_ACCESS = 10  # X-код ошибки: ресурс (клавиша) уже занят другим клиентом


class XErrorEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("serial", ctypes.c_ulong),
        ("error_code", ctypes.c_ubyte),
        ("request_code", ctypes.c_ubyte),
        ("minor_code", ctypes.c_ubyte),
        ("resourceid", ctypes.c_ulong),
    ]


# Перехватываем X-ошибки: запоминаем код (чтобы поймать BadAccess при grab)
# и не даём демону упасть.
_last_x_error = {"code": 0}
_ERR_HANDLER = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)


def _error_handler(_dpy, ev):
    try:
        err = ctypes.cast(ev, ctypes.POINTER(XErrorEvent)).contents
        _last_x_error["code"] = err.error_code
    except Exception:
        pass
    return 0


_err_cb = _ERR_HANDLER(_error_handler)
x11.XSetErrorHandler(_err_cb)


class HotkeyConflict(RuntimeError):
    """Клавиша уже захвачена системой или другим приложением."""


class PureModifierHotkey(RuntimeError):
    """Сочетание только из модификаторов (напр. Ctrl+Shift) — без обычной
    клавиши такой хоткей перехватывал бы любые Ctrl+Shift+X, поэтому не
    поддерживается."""


_MOD_ALIASES = {"control": "Ctrl", "ctrl": "Ctrl", "alt": "Alt",
                "shift": "Shift", "meta": "Meta", "super": "Meta", "win": "Meta"}


def parse_hotkey(spec):
    """'Ctrl+Shift+Space' -> (mask, 'Space', ['Ctrl','Shift'])."""
    mask, base, mods = 0, None, []
    for tok in spec.split("+"):
        tok = tok.strip()
        if not tok:
            continue
        canon = _MOD_ALIASES.get(tok.lower())
        if canon:
            mask |= MOD_DEFS[canon][0]
            mods.append(canon)
        else:
            base = tok
    return mask, base, mods


class ModTapDetector:
    """Срабатывает, когда все целевые модификаторы зажаты и отпущены без
    единой «посторонней» клавиши между (чистый аккорд, как в Punto)."""

    def __init__(self, groups):
        self.groups = groups            # список множеств keycode (L/R) по модификатору
        self.all_target = set().union(*groups) if groups else set()
        self.held = set()
        self.polluted = False
        self.armed = False

    def reset(self):
        self.held.clear()
        self.polluted = False
        self.armed = False

    def _all_held(self):
        return all(self.held & g for g in self.groups)

    def feed(self, is_press, kc):
        fire = False
        if is_press:
            self.held.add(kc)
            if kc in self.all_target:
                if self._all_held() and not self.polluted:
                    self.armed = True
            else:
                self.polluted = True   # посторонняя клавиша — аккорд «грязный»
                self.armed = False
        else:
            if self.armed and kc in self.all_target:
                fire = True
                self.armed = False
                self.polluted = True   # до полного отпускания не повторяем
            self.held.discard(kc)
            if not self.held:
                self.polluted = False
                self.armed = False
        return fire


class XRecorder:
    """Мониторит все нажатия через XRecord и зовёт on_fire на чистый аккорд."""

    def __init__(self, groups):
        self.detector = ModTapDetector(groups)
        self.on_fire = None
        self._busy = False
        self.ctrl_dpy = x11.XOpenDisplay(None)
        self.data_dpy = x11.XOpenDisplay(None)
        if not self.ctrl_dpy or not self.data_dpy:
            raise RuntimeError("XRecord: не удалось открыть дисплей")
        rng = xtst.XRecordAllocRange()
        rng.contents.device_events.first = KeyPress      # 2
        rng.contents.device_events.last = KeyPress + 1   # KeyRelease = 3
        spec = ctypes.c_ulong(XRecordAllClients)
        ranges = (ctypes.POINTER(XRecordRange) * 1)(rng)
        self.ctx = xtst.XRecordCreateContext(
            self.ctrl_dpy, 0, ctypes.byref(spec), 1, ranges, 1)
        x11.XSync(self.ctrl_dpy, False)
        self._cb = XRECORD_CB(self._intercept)  # держим ссылку от GC
        ok = xtst.XRecordEnableContextAsync(self.data_dpy, self.ctx,
                                            self._cb, None)
        x11.XFlush(self.data_dpy)
        log("XRecord активирован: ctx=%s ok=%s fd=%d" % (self.ctx, ok, self.fd()))

    def fd(self):
        return x11.XConnectionNumber(self.data_dpy)

    def process(self):
        try:
            xtst.XRecordProcessReplies(self.data_dpy)
        except Exception as e:
            print("XRecord process error:", e, file=sys.stderr)

    def _intercept(self, _closure, data_ptr):
        try:
            d = data_ptr.contents
            if d.category == 0 and d.data_len > 0 and not self._busy:  # FromServer
                etype = d.data[0] & 0x7F
                kc = d.data[1]
                if etype in (KeyPress, KeyPress + 1):
                    if self.detector.feed(etype == KeyPress, kc) and self.on_fire:
                        dbg("чистый аккорд пойман -> срабатывание")
                        self._busy = True
                        try:
                            self.on_fire()
                        finally:
                            self.detector.reset()
                            self._busy = False
        except Exception as e:
            print("XRecord intercept error:", e, file=sys.stderr)
        finally:
            xtst.XRecordFreeData(data_ptr)

    def stop(self):
        try:
            xtst.XRecordDisableContext(self.ctrl_dpy, self.ctx)
            xtst.XRecordFreeContext(self.ctrl_dpy, self.ctx)
            x11.XCloseDisplay(self.data_dpy)
            x11.XCloseDisplay(self.ctrl_dpy)
        except Exception:
            pass


class X11Backend:
    def __init__(self):
        self.dpy = x11.XOpenDisplay(None)
        if not self.dpy:
            raise RuntimeError("Не удалось открыть X-дисплей (DISPLAY)")
        self.root = x11.XDefaultRootWindow(self.dpy)
        self.kc_ctrl = x11.XKeysymToKeycode(self.dpy, XK_Control_L)
        self.kc_c = x11.XKeysymToKeycode(self.dpy, XK_c)
        self.kc_v = x11.XKeysymToKeycode(self.dpy, XK_v)

    def _mod_groups(self, mods):
        groups = []
        for mname in mods:
            kcs = set()
            for ks in MOD_DEFS[mname][1]:
                kc = x11.XKeysymToKeycode(self.dpy, ks)
                if kc:
                    kcs.add(kc)
            if kcs:
                groups.append(kcs)
        return groups

    def grab(self, spec):
        mask, base, mods = parse_hotkey(spec)
        if base is None:
            # Чистый аккорд из модификаторов (напр. Ctrl+Shift) — без grab,
            # отслеживаем через XRecord, чтобы не ломать Ctrl+Shift+X.
            if len(mods) < 2:
                raise PureModifierHotkey(spec)
            rec = XRecorder(self._mod_groups(mods))
            return {"mode": "mod", "spec": spec, "recorder": rec}
        keysym = x11.XStringToKeysym(base.encode())
        if keysym == 0:
            raise RuntimeError("Неизвестная клавиша: %r" % base)
        keycode = x11.XKeysymToKeycode(self.dpy, keysym)
        if keycode == 0:
            raise RuntimeError("Клавиши %r нет на этой клавиатуре" % base)
        _last_x_error["code"] = 0
        for extra in MOD_COMBOS:  # каждая комбинация Caps/Num Lock
            x11.XGrabKey(self.dpy, keycode, mask | extra, self.root,
                         False, GrabModeAsync, GrabModeAsync)
        x11.XSync(self.dpy, False)  # дожидаемся обработки возможной ошибки
        g = {"mode": "key", "keycode": keycode, "mask": mask, "spec": spec}
        if _last_x_error["code"] == BAD_ACCESS:
            self.ungrab(g)
            raise HotkeyConflict(spec)
        return g

    def ungrab(self, g):
        if not g:
            return
        if g.get("mode") == "mod":
            rec = g.get("recorder")
            if rec:
                rec.stop()
            return
        for extra in MOD_COMBOS:
            x11.XUngrabKey(self.dpy, g["keycode"], g["mask"] | extra, self.root)
        x11.XSync(self.dpy, False)

    def clear_modifiers(self):
        """Гасим зажатые модификаторы, чтобы синтетические Ctrl+C/Ctrl+V
        не превратились в Ctrl+Shift+C и т.п. (когда хоткей с модификаторами)."""
        for ks in ALL_MODIFIER_KEYSYMS:
            kc = x11.XKeysymToKeycode(self.dpy, ks)
            if kc:
                xtst.XTestFakeKeyEvent(self.dpy, kc, False, 0)
        x11.XFlush(self.dpy)

    def _tap(self, mod_kc, key_kc):
        xtst.XTestFakeKeyEvent(self.dpy, mod_kc, True, 0)
        xtst.XTestFakeKeyEvent(self.dpy, key_kc, True, 0)
        xtst.XTestFakeKeyEvent(self.dpy, key_kc, False, 0)
        xtst.XTestFakeKeyEvent(self.dpy, mod_kc, False, 0)
        x11.XFlush(self.dpy)

    def ctrl_c(self):
        self._tap(self.kc_ctrl, self.kc_c)

    def ctrl_v(self):
        self._tap(self.kc_ctrl, self.kc_v)

    def wait_key(self, ev):
        x11.XNextEvent(self.dpy, ctypes.byref(ev))

    def active_window(self):
        """Текущее активное окно через _NET_ACTIVE_WINDOW (EWMH)."""
        try:
            atom = x11.XInternAtom(self.dpy, b"_NET_ACTIVE_WINDOW", True)
            if not atom:
                return 0
            a_type = ctypes.c_ulong()
            a_fmt = ctypes.c_int()
            nitems = ctypes.c_ulong()
            after = ctypes.c_ulong()
            prop = ctypes.c_void_p()
            st = x11.XGetWindowProperty(
                self.dpy, self.root, atom, 0, 1, False, 0,
                ctypes.byref(a_type), ctypes.byref(a_fmt),
                ctypes.byref(nitems), ctypes.byref(after), ctypes.byref(prop))
            win = 0
            if st == 0 and prop and nitems.value > 0:
                win = ctypes.cast(prop, ctypes.POINTER(ctypes.c_ulong))[0]
            if prop:
                x11.XFree(prop)
            return int(win)
        except Exception as e:
            log("active_window error: %r" % e)
            return 0

    def window_class(self, win):
        """WM_CLASS активного окна (нижним регистром), напр. 'kate'."""
        if not win:
            return ""
        try:
            atom = x11.XInternAtom(self.dpy, b"WM_CLASS", True)
            if not atom:
                return ""
            a_type = ctypes.c_ulong()
            a_fmt = ctypes.c_int()
            nitems = ctypes.c_ulong()
            after = ctypes.c_ulong()
            prop = ctypes.c_void_p()
            st = x11.XGetWindowProperty(
                self.dpy, win, atom, 0, 1024, False, 31,  # XA_STRING
                ctypes.byref(a_type), ctypes.byref(a_fmt),
                ctypes.byref(nitems), ctypes.byref(after), ctypes.byref(prop))
            cls = ""
            if st == 0 and prop and nitems.value > 0:
                raw = ctypes.string_at(prop, nitems.value)
                parts = raw.split(b"\x00")
                pick = parts[1] if len(parts) >= 2 and parts[1] else \
                    (parts[0] if parts else b"")
                cls = pick.decode("latin-1", "replace")
            if prop:
                x11.XFree(prop)
            return cls.lower()
        except Exception as e:
            log("window_class error: %r" % e)
            return ""

    def active_window_class(self):
        return self.window_class(self.active_window())

    def selection_in_active(self):
        """Текст PRIMARY-выделения, если оно принадлежит активному окну,
        иначе ''. Неразрушающе (без синтетических нажатий). Проверка владельца
        отсекает «старое» выделение из другого приложения."""
        try:
            owner = x11.XGetSelectionOwner(self.dpy, XA_PRIMARY)
            active = self.active_window()
            if not owner or not active:
                dbg("PRIMARY owner=%s active=%s" % (owner, active))
                return ""
            w = owner
            found = False
            for _ in range(40):  # активное окно должно быть предком владельца
                if w == active:
                    found = True
                    break
                root_r = ctypes.c_ulong()
                parent_r = ctypes.c_ulong()
                ch = ctypes.POINTER(ctypes.c_ulong)()
                nch = ctypes.c_uint()
                ok = x11.XQueryTree(self.dpy, w, ctypes.byref(root_r),
                                    ctypes.byref(parent_r), ctypes.byref(ch),
                                    ctypes.byref(nch))
                if ch:
                    x11.XFree(ch)
                if not ok:
                    break
                parent = parent_r.value
                if not parent or parent == self.root or parent == w:
                    break
                w = parent
            dbg("PRIMARY owner=%#x active=%#x found=%s" % (owner, active, found))
            if not found:
                return ""
            return clip_get("primary")
        except Exception as e:
            log("selection_in_active error: %r" % e)
            return ""

    def activate(self, win):
        """Делаем окно активным (EWMH) + ставим фокус ввода."""
        if not win:
            return
        try:
            ev = XClientMessageEvent()
            ev.type = ClientMessage
            ev.window = win
            ev.message_type = x11.XInternAtom(self.dpy, b"_NET_ACTIVE_WINDOW",
                                              False)
            ev.format = 32
            ev.data_l[0] = 2  # источник: прямое действие пользователя
            ev.data_l[1] = 0  # CurrentTime
            mask = SubstructureRedirectMask | SubstructureNotifyMask
            x11.XSendEvent(self.dpy, self.root, False, mask, ctypes.byref(ev))
            x11.XRaiseWindow(self.dpy, win)
            x11.XSetInputFocus(self.dpy, win, RevertToParent, 0)
            x11.XFlush(self.dpy)
        except Exception as e:
            log("activate error: %r" % e)


# ---------------------------------------------------------------------------
# Буфер обмена (xclip) и раскладка (KDE D-Bus)
# ---------------------------------------------------------------------------

def clip_get(sel="clipboard"):
    try:
        r = subprocess.run(["xclip", "-selection", sel, "-o"],
                           capture_output=True, timeout=2)
        return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else ""
    except Exception:
        return ""


def clip_set(text, sel="clipboard"):
    try:
        subprocess.run(["xclip", "-selection", sel],
                       input=text.encode("utf-8"), timeout=2)
    except Exception:
        pass


_dbus_iface = None


def switch_layout():
    try:
        _ensure_iface().switchToNextLayout()
        dbg("раскладка переключена -> %s" % current_layout_code())
    except Exception as e:
        log("switch_layout ОШИБКА: %r" % e)


def _ensure_iface():
    global _dbus_iface
    if _dbus_iface is None:
        import dbus
        bus = dbus.SessionBus()
        obj = bus.get_object("org.kde.keyboard", "/Layouts")
        _dbus_iface = dbus.Interface(obj, "org.kde.KeyboardLayouts")
    return _dbus_iface


def current_layout_code():
    """Возвращает короткий код текущей раскладки, напр. 'us' / 'ru'."""
    try:
        i = _ensure_iface()
        idx = int(i.getLayout())
        layouts = i.getLayoutsList()
        return str(layouts[idx][0])
    except Exception:
        return "?"


def current_layout_name():
    """Полное имя текущей раскладки, напр. 'Russian' / 'English (US)'."""
    try:
        i = _ensure_iface()
        idx = int(i.getLayout())
        row = i.getLayoutsList()[idx]
        return str(row[2]) or str(row[0]).upper()
    except Exception:
        return current_layout_code().upper()


def _norm_key(s):
    s = s.strip().lower().replace(" ", "").replace("_", "")
    return _MOD_ALIASES.get(s, s).lower()


def _token_set(spec):
    """Множество нормализованных токенов хоткея для сравнения сочетаний."""
    return frozenset(_norm_key(t) for t in spec.split("+") if t.strip())


def find_kde_conflict(spec):
    """Ищет в kglobalshortcutsrc хоткей spec (одиночный или сочетание).
    Возвращает 'компонент → действие' или None. Чисто для понятного
    сообщения — основная проверка конфликта делается через BadAccess."""
    path = os.path.expanduser("~/.config/kglobalshortcutsrc")
    target = _token_set(spec)
    if not target:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    section = ""
    for ln in lines:
        ln = ln.strip()
        if ln.startswith("[") and ln.endswith("]"):
            section = ln[1:-1]
            continue
        if "=" not in ln:
            continue
        name, _, val = ln.partition("=")
        active = val.split(",")[0].strip()
        if not active or active.lower() == "none":
            continue
        if _token_set(active) == target:
            return "%s → %s" % (section, name.strip())
    return None


def notify(msg):
    try:
        subprocess.Popen(["notify-send", "-a", "Punto", "-t", "1200",
                          "Punto Switcher", msg])
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Позиция каретки через AT-SPI (для бейджа у поля ввода, #10)
# ---------------------------------------------------------------------------

class CaretTracker:
    """Фоновый поток слушает события каретки/фокуса через AT-SPI и кэширует
    экранную позицию каретки. Работает для a11y-приложений (Qt, LibreOffice,
    Firefox…); где недоступно — caret_xy() вернёт None (откат на курсор)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pos = {"x": 0, "y": 0, "h": 0, "ts": 0.0}
        self._atspi = None
        self._thread = None
        self._ctx = None      # отдельный GLib-контекст (чтобы не конфликтовать с Qt)
        self._loop = None
        self._focused = None  # последний активный текстовый объект (только AT-SPI-поток)
        self._focused_ts = 0.0

    def start(self):
        try:
            import gi
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi
        except Exception as e:
            log("AT-SPI недоступен, бейдж будет у курсора: %r" % e)
            return False
        self._atspi = Atspi
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return True

    def _run(self):
        Atspi = self._atspi
        try:
            from gi.repository import GLib
            # Свой контекст в этом потоке — иначе конфликт с GLib-циклом Qt.
            self._ctx = GLib.MainContext.new()
            self._ctx.push_thread_default()
            Atspi.init()
            self._listeners = []
            for name in ("object:text-caret-moved",
                         "object:state-changed:focused",
                         "object:text-selection-changed"):
                l = Atspi.EventListener.new(self._on_event)
                l.register(name)
                self._listeners.append(l)
            self._loop = GLib.MainLoop.new(self._ctx, False)
            log("AT-SPI: слушатели активны")
            self._loop.run()
        except Exception as e:
            log("AT-SPI цикл завершился: %r" % e)

    def _on_event(self, event):
        Atspi = self._atspi
        try:
            src = event.source
            if src is None:
                return
            off = src.get_caret_offset()  # бросит, если объект без интерфейса Text
            self._focused = src           # запоминаем активное текстовое поле
            self._focused_ts = time.time()
            r = src.get_character_extents(off, Atspi.CoordType.SCREEN)
            x, y, w, h = r.x, r.y, r.width, r.height
            if w == 0 and h == 0 and off > 0:  # каретка в конце строки
                r = src.get_character_extents(off - 1, Atspi.CoordType.SCREEN)
                x, y, w, h = r.x, r.y, r.width, r.height
            if h <= 0 or (x <= 0 and y <= 0):
                return
            with self.lock:
                self.pos = {"x": int(x), "y": int(y), "h": int(h),
                            "ts": time.time()}
        except Exception:
            pass  # объект без интерфейса Text и т.п.

    def caret_xy(self, max_age=15.0):
        """(x, y) под кареткой, если позиция свежая; иначе None."""
        with self.lock:
            p = dict(self.pos)
        if p["ts"] and (time.time() - p["ts"]) < max_age and p["h"] > 0:
            return (p["x"], p["y"] + p["h"] + 2)
        return None

    def query_selection(self, timeout=0.3):
        """Спрашивает у сфокусированного поля текущее выделение через AT-SPI.
        Возвращает ('sel', text) / ('nosel', why) / ('unknown', why) / None.
        Запрос выполняется в AT-SPI-потоке (invoke_full в его контексте)."""
        if not self._atspi or self._ctx is None:
            return None
        Atspi = self._atspi
        try:
            from gi.repository import GLib
            import queue
            q = queue.Queue(1)

            def job(*_a):
                res = ("unknown", "init")
                try:
                    acc = self._focused
                    if acc is None:
                        res = ("unknown", "no _focused")
                    else:
                        age = time.time() - (self._focused_ts or 0)
                        try:
                            focused = acc.get_state_set().contains(
                                Atspi.StateType.FOCUSED)
                        except Exception:
                            focused = None
                        try:
                            nsel = Atspi.Text.get_n_selections(acc)
                        except Exception:
                            nsel = None
                        # доверяем объекту, если он в фокусе ИЛИ был активен только
                        # что (свежесть отсекает «залипший» объект из др. приложения)
                        fresh = bool(focused) or age < 4.0
                        if nsel is None:
                            res = ("unknown", "noText")
                        elif not fresh:
                            res = ("unknown", "stale age=%.1f" % age)
                        elif nsel > 0:
                            rng = Atspi.Text.get_selection(acc, 0)
                            txt = Atspi.Text.get_text(
                                acc, rng.start_offset, rng.end_offset)
                            res = ("sel", txt) if txt else ("nosel", "empty")
                        else:
                            res = ("nosel", "focused=%s age=%.1f" % (focused, age))
                except Exception as ex:
                    res = ("unknown", "exc:%s" % ex)
                try:
                    q.put_nowait(res)
                except Exception:
                    pass
                return False  # не повторять

            # выполняем job в потоке AT-SPI (его GLib-контекст)
            self._ctx.invoke_full(GLib.PRIORITY_DEFAULT, job)
            return q.get(timeout=timeout)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Основная логика по нажатию хоткея
# ---------------------------------------------------------------------------

SENTINEL = "​⁣__punto_no_selection__⁣​"


def detect_selection(x):
    """Текст активного выделения или '' (нет выделения). Неразрушающе:
    сначала AT-SPI (Qt/GTK/браузеры), иначе PRIMARY-выделение активного окна.
    Никаких синтетических Ctrl+C — чтобы не слать SIGINT в терминалы и не
    копировать строку в редакторах вроде Kate."""
    tracker = CARET["tracker"]
    if tracker is not None:
        info = tracker.query_selection()
        dbg("AT-SPI query_selection -> %r" % (info,))
        if info and info[0] == "sel" and info[1]:
            return info[1]
        if info and info[0] == "nosel":
            return ""
        # ('unknown', ...)/None -> PRIMARY ниже
    sel = x.selection_in_active()
    dbg("PRIMARY активного окна -> %r" % (sel[:40] if sel else sel))
    return sel


def handle_hotkey(x, cfg):
    """Возвращает ('switch',) / ('fix', orig, fixed) / None — для бейджа и истории."""
    dbg("hotkey fired (enabled=%s)" % STATE["enabled"])
    if not STATE["enabled"]:
        return None
    app = x.active_window_class()
    if app and any(d and d.lower() in app for d in cfg.get("disabled_apps", [])):
        dbg("приложение %r отключено -> пропуск" % app)
        return None

    selection = detect_selection(x)
    if not selection:
        # Ничего не выделено -> переключаем раскладку.
        dbg("нет выделения -> переключаю раскладку")
        switch_layout()
        STATS.record_switch()
        if cfg["notify"]:
            notify("Раскладка → %s" % current_layout_name())
        return ("switch",)

    if selection.strip().lower() in {e.lower() for e in cfg.get("exceptions", [])}:
        dbg("исключение: %r — не трогаю" % selection)
        return None

    fixed = transliterate(selection)
    dbg("исправляю: %r -> %r" % (selection[:30], fixed[:30]))
    if fixed == selection:
        return None

    # Выделение всё ещё подсвечено -> Ctrl+V заменяет его.
    saved = clip_get("clipboard")
    try:
        clip_set(fixed, "clipboard")
        time.sleep(0.05)
        x.clear_modifiers()
        x.ctrl_v()
        time.sleep(0.15)
        if cfg["switch_layout_after_fix"]:
            switch_layout()
        HISTORY.add(selection, fixed, app)
        STATS.record_fix(fixed)
        if cfg["notify"]:
            notify("%s → %s" % (selection.strip()[:30], fixed.strip()[:30]))
        return ("fix", selection, fixed)
    except Exception:
        import traceback
        log("ОШИБКА в handle_hotkey:\n" + traceback.format_exc())
        return None
    finally:
        if saved:
            time.sleep(0.02)
            clip_set(saved, "clipboard")


def acquire_single_instance():
    """Не даём запуститься второму экземпляру (важно для автозапуска)."""
    import fcntl
    runtime = os.environ.get("XDG_RUNTIME_DIR", "/tmp")
    lock_path = os.path.join(runtime, "punto-switcher.lock")
    fh = open(lock_path, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("linux-punto уже запущен — выходим.", file=sys.stderr)
        sys.exit(0)
    return fh  # держим ссылку, чтобы блокировка жила


def run_headless(x, cfg, grabbed):
    """Запасной режим без трея: блокирующий цикл по X-событиям."""
    print("linux-punto запущен (без трея). Хоткей: %s." % cfg["hotkey"])

    def fire():
        try:
            handle_hotkey(x, cfg)
        except Exception as e:
            print("handle error:", e, file=sys.stderr)

    if grabbed and grabbed.get("mode") == "mod":
        import select
        rec = grabbed["recorder"]
        rec.on_fire = fire
        fd = rec.fd()
        while True:
            select.select([fd], [], [])
            rec.process()
        return

    ev = XKeyEvent()
    while True:
        x.wait_key(ev)
        if (ev.type == KeyPress and grabbed and grabbed.get("mode") == "key"
                and ev.keycode == grabbed["keycode"]):
            fire()


def make_layout_icon(QtGui, QtCore, code, enabled):
    """Рисует иконку трея с двухбуквенным кодом раскладки."""
    size = 64
    pm = QtGui.QPixmap(size, size)
    pm.fill(QtCore.Qt.transparent)
    p = QtGui.QPainter(pm)
    p.setRenderHint(QtGui.QPainter.Antialiasing)
    bg = QtGui.QColor("#3daee9") if enabled else QtGui.QColor("#7f8c8d")
    p.setBrush(bg)
    p.setPen(QtCore.Qt.NoPen)
    p.drawRoundedRect(2, 2, size - 4, size - 4, 14, 14)
    font = QtGui.QFont("Sans", 26, QtGui.QFont.Bold)
    p.setFont(font)
    p.setPen(QtGui.QColor("#ffffff"))
    p.drawText(pm.rect(), QtCore.Qt.AlignCenter, code.upper()[:2])
    p.end()
    return QtGui.QIcon(pm)


def _qt_to_keysym_map(QtCore):
    """Соответствие Qt-клавиш именам X-keysym (для XStringToKeysym)."""
    K = QtCore.Qt
    m = {
        K.Key_Pause: "Pause", K.Key_Print: "Print",
        K.Key_ScrollLock: "Scroll_Lock", K.Key_CapsLock: "Caps_Lock",
        K.Key_NumLock: "Num_Lock", K.Key_Insert: "Insert",
        K.Key_Delete: "Delete", K.Key_Home: "Home", K.Key_End: "End",
        K.Key_PageUp: "Prior", K.Key_PageDown: "Next", K.Key_Menu: "Menu",
        K.Key_Space: "space", K.Key_Escape: "Escape", K.Key_Tab: "Tab",
        K.Key_Backspace: "BackSpace", K.Key_Return: "Return",
        K.Key_Enter: "Return",
    }
    for i in range(1, 13):
        m[getattr(K, "Key_F%d" % i)] = "F%d" % i
    return m


def run_with_tray(x, cfg, grabbed):
    from PyQt5 import QtWidgets, QtGui, QtCore

    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # Всплывающий бейдж с раскладкой у курсора (#10).
    badge = QtWidgets.QLabel()
    badge.setWindowFlags(QtCore.Qt.FramelessWindowHint
                         | QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.Tool
                         | QtCore.Qt.WindowDoesNotAcceptFocus)
    badge.setAttribute(QtCore.Qt.WA_ShowWithoutActivating)
    badge.setAlignment(QtCore.Qt.AlignCenter)
    badge.setStyleSheet("background:#3daee9; color:white; font:bold 15px;"
                        "border-radius:8px; padding:4px 10px;")
    _badge_timer = QtCore.QTimer()
    _badge_timer.setSingleShot(True)
    _badge_timer.timeout.connect(badge.hide)

    caret_tracker = CaretTracker()
    caret_tracker.start()
    CARET["tracker"] = caret_tracker  # для detect_selection в handle_hotkey

    def flash_badge(code):
        if not cfg.get("badge"):
            return
        badge.setText(code.upper())
        badge.adjustSize()
        pt = caret_tracker.caret_xy() if cfg.get("badge_at_caret", True) else None
        if pt:
            bx, by = pt[0] + 4, pt[1]
        else:
            pos = QtGui.QCursor.pos()
            bx, by = pos.x() + 16, pos.y() + 16
        badge.move(bx, by)
        badge.show()
        badge.raise_()
        _badge_timer.start(900)

    hk = {"g": grabbed}  # мутабельно: словарь grab (или None), меняется на лету
    qt2keysym = _qt_to_keysym_map(QtCore)
    MODIFIER_KEYS = {QtCore.Qt.Key_Control, QtCore.Qt.Key_Shift,
                     QtCore.Qt.Key_Alt, QtCore.Qt.Key_Meta,
                     QtCore.Qt.Key_AltGr, QtCore.Qt.Key_Super_L,
                     QtCore.Qt.Key_Super_R}

    tray = QtWidgets.QSystemTrayIcon()
    tray.setIcon(make_layout_icon(QtGui, QtCore, current_layout_code(),
                                  STATE["enabled"]))
    tray.setToolTip("linux-punto — хоткей %s" % cfg["hotkey"])

    menu = QtWidgets.QMenu()
    act_layout = menu.addAction("Текущая раскладка: …")
    act_layout.setEnabled(False)
    menu.addSeparator()
    act_enabled = menu.addAction("Включён")
    act_enabled.setCheckable(True)
    act_enabled.setChecked(STATE["enabled"])
    act_switch = menu.addAction("Переключить раскладку")
    act_history = menu.addAction("История исправлений…")
    act_stats = menu.addAction("Статистика…")
    act_config = menu.addAction("Настройки…")
    menu.addSeparator()
    act_quit = menu.addAction("Выход")
    tray.setContextMenu(menu)

    def refresh():
        code = current_layout_code()
        tray.setIcon(make_layout_icon(QtGui, QtCore, code, STATE["enabled"]))
        act_layout.setText("Текущая раскладка: %s" % code.upper())

    def on_enabled(checked):
        STATE["enabled"] = checked
        refresh()

    def on_switch():
        switch_layout()
        STATS.record_switch()
        refresh()
        if cfg["notify"]:
            notify("Раскладка → %s" % current_layout_name())
        flash_badge(current_layout_code())

    # ---- Кнопка захвата клавиши --------------------------------------
    def _mods_from(qmods):
        r = []
        if qmods & QtCore.Qt.ControlModifier:
            r.append("Ctrl")
        if qmods & QtCore.Qt.AltModifier:
            r.append("Alt")
        if qmods & QtCore.Qt.ShiftModifier:
            r.append("Shift")
        if qmods & QtCore.Qt.MetaModifier:
            r.append("Meta")
        return r

    KEY_TO_MOD = {QtCore.Qt.Key_Control: "Ctrl", QtCore.Qt.Key_Alt: "Alt",
                  QtCore.Qt.Key_Shift: "Shift", QtCore.Qt.Key_Meta: "Meta",
                  QtCore.Qt.Key_Super_L: "Meta", QtCore.Qt.Key_Super_R: "Meta"}

    class CaptureButton(QtWidgets.QPushButton):
        captured = QtCore.pyqtSignal(str)

        def __init__(self):
            super().__init__("Изменить…")
            self._cap = False
            self._held = set()   # сейчас зажатые модификаторы
            self._seen = set()   # все модификаторы за этот заход

        def mousePressEvent(self, e):
            self._start()

        def _start(self):
            self._cap = True
            self._held = set()
            self._seen = set()
            self.setText("Нажмите сочетание…")
            self.grabKeyboard()

        def _stop(self):
            self._cap = False
            self.releaseKeyboard()
            self.setText("Изменить…")

        def _preview(self):
            mods = [m for m in MOD_ORDER if m in self._seen]
            if len(mods) >= 2:
                self.setText("+".join(mods) + "  (отпусти)")
            elif mods:
                self.setText("+".join(mods) + "+…")
            else:
                self.setText("Нажмите сочетание…")

        def keyPressEvent(self, e):
            if not self._cap:
                return super().keyPressEvent(e)
            key = e.key()
            if key == QtCore.Qt.Key_Escape:
                self._stop()
                return
            if key in MODIFIER_KEYS:
                pm = KEY_TO_MOD.get(key)
                if pm:
                    self._held.add(pm)
                    self._seen.add(pm)
                    self._preview()
                return  # модификатор — ждём дальше
            mods = [m for m in MOD_ORDER if m in self._seen]
            # Голый Enter/Tab/Backspace — это навигация по окну, не хоткей:
            # просто выходим из режима захвата, ничего не назначая.
            if not mods and key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter,
                                    QtCore.Qt.Key_Tab, QtCore.Qt.Key_Backtab,
                                    QtCore.Qt.Key_Backspace):
                self._stop()
                return
            # обычная клавиша -> сочетание модификатор(ы)+клавиша
            name = qt2keysym.get(key)
            if name is None:
                if 0x41 <= key <= 0x5A:       # A..Z
                    name = chr(key).lower()
                elif 0x30 <= key <= 0x39:     # 0..9
                    name = chr(key)
                else:
                    t = e.text()
                    if t and t.strip() and t.isprintable():
                        name = t
            self._stop()
            if name:
                self.captured.emit("+".join(mods + [name]))

        def keyReleaseEvent(self, e):
            if not self._cap or e.isAutoRepeat():
                return super().keyReleaseEvent(e)
            pm = KEY_TO_MOD.get(e.key())
            if pm:
                self._held.discard(pm)
                if not self._held:
                    mods = [m for m in MOD_ORDER if m in self._seen]
                    if len(mods) >= 2:   # чистый аккорд из модификаторов
                        self._stop()
                        self.captured.emit("+".join(mods))
                    else:               # один модификатор — мало, начинаем заново
                        self._seen = set()
                        self.setText("Нажмите сочетание…")

    # ---- Окно истории исправлений ------------------------------------
    hist_holder = {}

    def _esc(s):
        s = (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return s if len(s) <= 60 else s[:57] + "…"

    def _fmt_ts(ts):
        try:
            lt = time.localtime(ts)
            now = time.localtime()
            fmt = "%H:%M" if (lt.tm_year, lt.tm_yday) == (now.tm_year, now.tm_yday) \
                else "%d.%m %H:%M"
            return time.strftime(fmt, lt)
        except Exception:
            return ""

    def do_copy(text):
        clip_set(text, "clipboard")
        if cfg["notify"]:
            notify("Скопировано: %s" % text[:40])

    def do_paste(text):
        # Вставляем в окно, активное на момент открытия истории.
        win = hist_holder.get("target", 0)
        saved = clip_get("clipboard")
        clip_set(text, "clipboard")
        time.sleep(0.05)
        if win:
            x.activate(win)
            time.sleep(0.15)
        x.clear_modifiers()
        x.ctrl_v()
        time.sleep(0.2)
        if saved:
            clip_set(saved, "clipboard")  # не портим буфер пользователя

    def open_history():
        # Запоминаем активное окно ДО показа диалога — туда будем вставлять.
        hist_holder["target"] = x.active_window()
        if hist_holder.get("dlg") is not None:
            d = hist_holder["dlg"]
            d.show()
            d.raise_()
            d.activateWindow()
            hist_holder["rebuild"]()
            return

        dlg = QtWidgets.QDialog()
        dlg.setWindowTitle("История исправлений")
        dlg.setWindowIcon(make_layout_icon(QtGui, QtCore,
                                           current_layout_code(), True))
        dlg.setMinimumSize(480, 400)
        v = QtWidgets.QVBoxLayout(dlg)

        top_bar = QtWidgets.QHBoxLayout()
        search = QtWidgets.QLineEdit()
        search.setPlaceholderText("поиск по тексту/приложению…")
        search.setClearButtonEnabled(True)
        group_chk = QtWidgets.QCheckBox("группировать по приложению")
        top_bar.addWidget(search, 1)
        top_bar.addWidget(group_chk)
        v.addLayout(top_bar)
        search.textChanged.connect(lambda *_: rebuild())
        group_chk.toggled.connect(lambda *_: rebuild())

        scroll = QtWidgets.QScrollArea()
        scroll.setWidgetResizable(True)
        container = QtWidgets.QWidget()
        list_lay = QtWidgets.QVBoxLayout(container)
        list_lay.setSpacing(4)
        scroll.setWidget(container)
        v.addWidget(scroll, 1)

        def clear_lay(lay):
            while lay.count():
                it = lay.takeAt(0)
                w = it.widget()
                if w:
                    w.deleteLater()

        def make_row(item):
            row = QtWidgets.QFrame()
            row.setFrameShape(QtWidgets.QFrame.StyledPanel)
            hl = QtWidgets.QHBoxLayout(row)
            hl.setContentsMargins(6, 3, 6, 3)
            pin = QtWidgets.QToolButton()
            pin.setCheckable(True)
            pin.setChecked(bool(item.get("pinned")))
            pin.setText("📌")
            pin.setToolTip("Закрепить (не удаляется при «Очистить»)")
            pin.toggled.connect(
                lambda _c, it=item: (HISTORY.toggle_pin(it), rebuild()))
            lbl = QtWidgets.QLabel("<b>%s</b> &rarr; %s"
                                   % (_esc(item.get("original")),
                                      _esc(item.get("fixed"))))
            lbl.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            lbl.setToolTip("%s → %s" % (item.get("original"), item.get("fixed")))
            ts = QtWidgets.QLabel(_fmt_ts(item.get("ts")))
            ts.setStyleSheet("color: gray;")
            appname = item.get("app") or ""
            app_lbl = QtWidgets.QLabel(
                appname if len(appname) <= 14 else appname[:13] + "…")
            app_lbl.setStyleSheet("color: #888; font-size: 11px;")
            if appname:
                app_lbl.setToolTip("Приложение: %s" % appname)
            copy = QtWidgets.QToolButton()
            copy.setText("📋")
            copy.setToolTip("Копировать исправленный текст в буфер")
            copy.clicked.connect(lambda _c=False, it=item: do_copy(it.get("fixed")))
            paste = QtWidgets.QToolButton()
            paste.setText("📥")
            paste.setToolTip("Вставить в поле, активное при открытии истории")
            paste.clicked.connect(lambda _c=False, it=item: do_paste(it.get("fixed")))
            dele = QtWidgets.QToolButton()
            dele.setText("✕")
            dele.setToolTip("Удалить запись")
            dele.clicked.connect(
                lambda _c=False, it=item: (HISTORY.delete(it), rebuild()))
            hl.addWidget(pin)
            hl.addWidget(lbl, 1)
            hl.addWidget(app_lbl)
            hl.addWidget(ts)
            hl.addWidget(copy)
            hl.addWidget(paste)
            hl.addWidget(dele)
            return row

        def rebuild():
            clear_lay(list_lay)
            q = search.text().strip().lower()
            items = HISTORY.ordered()
            if q:
                items = [it for it in items
                         if q in ("%s %s %s" % (it.get("original", ""),
                                                it.get("fixed", ""),
                                                it.get("app", ""))).lower()]
            if group_chk.isChecked():
                items = sorted(items, key=lambda it: (it.get("app") or "￿"))
            if not items:
                empty = QtWidgets.QLabel("Ничего нет" if q else "История пуста")
                empty.setStyleSheet("color: gray;")
                empty.setAlignment(QtCore.Qt.AlignCenter)
                list_lay.addWidget(empty)
            else:
                for it in items:
                    list_lay.addWidget(make_row(it))
            list_lay.addStretch(1)

        def do_export():
            path, _ = QtWidgets.QFileDialog.getSaveFileName(
                dlg, "Экспорт истории",
                os.path.expanduser("~/punto-history.txt"),
                "Текст (*.txt)")
            if not path:
                return
            try:
                with open(path, "w", encoding="utf-8") as f:
                    for it in HISTORY.ordered():
                        f.write("%s\t%s\t%s\t%s\n" % (
                            _fmt_ts(it.get("ts")), it.get("original", ""),
                            it.get("fixed", ""), it.get("app", "")))
            except Exception as e:
                QtWidgets.QMessageBox.warning(dlg, "Ошибка", str(e))

        btns = QtWidgets.QHBoxLayout()
        btn_clear = QtWidgets.QPushButton("Очистить")
        btn_clear.setToolTip("Удалить всё, кроме закреплённого")
        btn_clear.clicked.connect(lambda: (HISTORY.clear(), rebuild()))
        btn_export = QtWidgets.QPushButton("Экспорт…")
        btn_export.clicked.connect(do_export)
        btns.addWidget(btn_clear)
        btns.addWidget(btn_export)
        btns.addStretch(1)
        btn_close = QtWidgets.QPushButton("Закрыть")
        btn_close.clicked.connect(dlg.close)
        btns.addWidget(btn_close)
        v.addLayout(btns)

        rebuild()
        hist_holder["dlg"] = dlg
        hist_holder["rebuild"] = rebuild
        dlg.finished.connect(lambda *_: hist_holder.update(dlg=None, rebuild=None))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    # ---- Окно статистики (#12) ---------------------------------------
    stats_holder = {}

    def open_stats():
        if stats_holder.get("dlg") is not None:
            stats_holder["dlg"].show()
            stats_holder["dlg"].raise_()
            stats_holder["rebuild"]()
            return
        dlg = QtWidgets.QDialog()
        dlg.setWindowTitle("Статистика")
        dlg.setWindowIcon(make_layout_icon(QtGui, QtCore,
                                           current_layout_code(), True))
        dlg.setMinimumWidth(360)
        v = QtWidgets.QVBoxLayout(dlg)
        totals = QtWidgets.QLabel()
        v.addWidget(totals)
        v.addWidget(QtWidgets.QLabel("<b>Топ исправлений:</b>"))
        top = QtWidgets.QListWidget()
        v.addWidget(top, 1)

        def rebuild():
            d = STATS.data
            today = d["days"].get(_today(), {"switches": 0, "fixes": 0})
            totals.setText(
                "Переключений раскладки: <b>%d</b> (сегодня %d)<br>"
                "Исправлений текста: <b>%d</b> (сегодня %d)"
                % (d["switches"], today["switches"],
                   d["fixes"], today["fixes"]))
            top.clear()
            for w, c in STATS.top_words(15):
                top.addItem("%d×  %s" % (c, w))
            if top.count() == 0:
                top.addItem("пока пусто")

        btns = QtWidgets.QHBoxLayout()
        btn_reset = QtWidgets.QPushButton("Сбросить")
        btn_reset.clicked.connect(lambda: (STATS.reset(), rebuild()))
        btns.addWidget(btn_reset)
        btns.addStretch(1)
        btn_close = QtWidgets.QPushButton("Закрыть")
        btn_close.clicked.connect(dlg.close)
        btns.addWidget(btn_close)
        v.addLayout(btns)

        rebuild()
        stats_holder["dlg"] = dlg
        stats_holder["rebuild"] = rebuild
        dlg.finished.connect(lambda *_: stats_holder.update(dlg=None, rebuild=None))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    # ---- Окно настроек ------------------------------------------------
    dlg_holder = {}

    def open_settings():
        if dlg_holder.get("dlg") is not None:
            d = dlg_holder["dlg"]
            d.show()
            d.raise_()
            d.activateWindow()
            return

        dlg = QtWidgets.QDialog()
        dlg.setWindowTitle("linux-punto — настройки")
        dlg.setWindowIcon(make_layout_icon(QtGui, QtCore,
                                           current_layout_code(), True))
        dlg.setMinimumWidth(420)
        v = QtWidgets.QVBoxLayout(dlg)

        header = QtWidgets.QLabel(
            "<b>Punto Switcher для Linux</b><br>"
            "<span style='color:gray'>переключение раскладки и исправление "
            "текста по хоткею</span>")
        v.addWidget(header)

        pending = {"name": cfg["hotkey"]}
        form = QtWidgets.QFormLayout()
        key_label = QtWidgets.QLabel("<b>%s</b>" % cfg["hotkey"])
        cap_btn = CaptureButton()
        hk_row = QtWidgets.QHBoxLayout()
        hk_row.addWidget(key_label, 1)
        hk_row.addWidget(cap_btn)
        hk_w = QtWidgets.QWidget()
        hk_w.setLayout(hk_row)
        form.addRow("Горячая клавиша:", hk_w)
        v.addLayout(form)

        hint = QtWidgets.QLabel(
            "<span style='color:gray'>варианты: одиночная клавиша "
            "(<b>Pause</b>), модификатор+клавиша (<b>Ctrl+Space</b>) или два "
            "модификатора (<b>Ctrl+Shift</b>) — нажми их и отпусти.</span>")
        hint.setWordWrap(True)
        v.addWidget(hint)

        def on_captured(name):
            pending["name"] = name
            key_label.setText("<b>%s</b>" % name)

        cap_btn.captured.connect(on_captured)

        chk_enabled = QtWidgets.QCheckBox("Включён")
        chk_enabled.setChecked(STATE["enabled"])
        chk_notify = QtWidgets.QCheckBox("Показывать уведомления")
        chk_notify.setChecked(cfg["notify"])
        chk_switch = QtWidgets.QCheckBox(
            "Переключать раскладку после исправления текста")
        chk_switch.setChecked(cfg["switch_layout_after_fix"])
        chk_badge = QtWidgets.QCheckBox(
            "Бейдж с раскладкой при переключении")
        chk_badge.setChecked(cfg.get("badge", True))
        chk_caret = QtWidgets.QCheckBox(
            "    …показывать у каретки (иначе у курсора мыши)")
        chk_caret.setChecked(cfg.get("badge_at_caret", True))
        chk_autostart = QtWidgets.QCheckBox("Запускать при входе в систему")
        chk_autostart.setChecked(autostart_enabled())
        v.addWidget(chk_enabled)
        v.addWidget(chk_notify)
        v.addWidget(chk_switch)
        v.addWidget(chk_badge)
        v.addWidget(chk_caret)
        v.addWidget(chk_autostart)
        chk_badge.toggled.connect(chk_caret.setEnabled)
        chk_caret.setEnabled(chk_badge.isChecked())

        exc_box = QtWidgets.QGroupBox("Исключения — слова, которые не исправлять")
        el = QtWidgets.QVBoxLayout(exc_box)
        exc_edit = QtWidgets.QPlainTextEdit("\n".join(cfg.get("exceptions", [])))
        exc_edit.setPlaceholderText("по одному слову на строку")
        exc_edit.setMaximumHeight(56)
        el.addWidget(exc_edit)
        v.addWidget(exc_box)

        app_box = QtWidgets.QGroupBox("Не работать в приложениях")
        al = QtWidgets.QVBoxLayout(app_box)
        apps_edit = QtWidgets.QPlainTextEdit(
            "\n".join(cfg.get("disabled_apps", [])))
        apps_edit.setMaximumHeight(56)
        _known = sorted({it.get("app", "") for it in HISTORY.items
                         if it.get("app")})
        _ahint = "по одному на строку (подстрока WM_CLASS)."
        if _known:
            _ahint += " Известные: " + ", ".join(_known[:8])
        apps_hint = QtWidgets.QLabel("<span style='color:gray'>%s</span>" % _ahint)
        apps_hint.setWordWrap(True)
        al.addWidget(apps_edit)
        al.addWidget(apps_hint)
        v.addWidget(app_box)

        box = QtWidgets.QGroupBox("Проверка транслитерации")
        bl = QtWidgets.QVBoxLayout(box)
        test_in = QtWidgets.QLineEdit()
        test_in.setPlaceholderText("например: ghbdtn")
        test_out = QtWidgets.QLabel("…")
        test_out.setStyleSheet("color:#3daee9; font-weight:bold;")
        test_in.textChanged.connect(
            lambda t: test_out.setText(transliterate(t) if t else "…"))
        bl.addWidget(test_in)
        bl.addWidget(test_out)
        v.addWidget(box)

        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)
        btn_save = QtWidgets.QPushButton("Сохранить")
        btn_save.setDefault(True)
        btn_close = QtWidgets.QPushButton("Закрыть")
        btns.addWidget(btn_save)
        btns.addWidget(btn_close)
        v.addLayout(btns)

        def do_save():
            new_name = pending["name"]
            old_name = cfg["hotkey"]
            if new_name != old_name:
                try:
                    x.ungrab(hk["g"])
                    hk["g"] = x.grab(new_name)
                    wire_hotkey()
                except PureModifierHotkey:
                    try:
                        hk["g"] = x.grab(old_name)
                    except Exception:
                        hk["g"] = None
                    wire_hotkey()
                    QtWidgets.QMessageBox.warning(
                        dlg, "Так нельзя",
                        "Нужно минимум два модификатора («%s» — мало).\n"
                        "Например Ctrl+Shift или добавь обычную клавишу "
                        "(Ctrl+Space)." % new_name)
                    return
                except HotkeyConflict:
                    try:  # возвращаем прежний рабочий хоткей
                        hk["g"] = x.grab(old_name)
                    except Exception:
                        hk["g"] = None
                    wire_hotkey()
                    extra = find_kde_conflict(new_name)
                    msg = "Хоткей «%s» уже занят" % new_name
                    msg += " (%s)" % extra if extra else \
                        " системой или другим приложением"
                    msg += ".\nВыбери другой."
                    QtWidgets.QMessageBox.warning(dlg, "Конфликт хоткея", msg)
                    return
                except RuntimeError as e:
                    try:
                        hk["g"] = x.grab(old_name)
                    except Exception:
                        hk["g"] = None
                    wire_hotkey()
                    QtWidgets.QMessageBox.warning(
                        dlg, "Ошибка",
                        "Не удалось назначить «%s»:\n%s" % (new_name, e))
                    return
                cfg["hotkey"] = new_name
            cfg["notify"] = chk_notify.isChecked()
            cfg["switch_layout_after_fix"] = chk_switch.isChecked()
            cfg["badge"] = chk_badge.isChecked()
            cfg["badge_at_caret"] = chk_caret.isChecked()
            cfg["exceptions"] = [w.strip().lower()
                                 for w in exc_edit.toPlainText().splitlines()
                                 if w.strip()]
            cfg["disabled_apps"] = [w.strip().lower()
                                    for w in apps_edit.toPlainText().splitlines()
                                    if w.strip()]
            set_autostart(chk_autostart.isChecked())
            STATE["enabled"] = chk_enabled.isChecked()
            act_enabled.setChecked(STATE["enabled"])
            save_config(cfg)
            tray.setToolTip("linux-punto — хоткей %s" % cfg["hotkey"])
            refresh()
            dlg.close()

        btn_save.clicked.connect(do_save)
        btn_close.clicked.connect(dlg.close)

        dlg_holder["dlg"] = dlg
        dlg.finished.connect(lambda *_: dlg_holder.update(dlg=None))
        dlg.show()
        dlg.raise_()
        dlg.activateWindow()

    def on_activated(reason):
        # ЛКМ по иконке — быстро переключить раскладку.
        if reason == QtWidgets.QSystemTrayIcon.Trigger:
            on_switch()

    act_enabled.toggled.connect(on_enabled)
    act_switch.triggered.connect(on_switch)
    act_history.triggered.connect(open_history)
    act_stats.triggered.connect(open_stats)
    act_config.triggered.connect(open_settings)
    act_quit.triggered.connect(app.quit)
    tray.activated.connect(on_activated)

    # X-события (грабнутый хоткей) обрабатываем прямо в Qt-цикле:
    # читаем сокет дисплея через QSocketNotifier — без отдельных потоков.
    ev = XKeyEvent()

    def fire_hotkey():
        res = None
        try:
            res = handle_hotkey(x, cfg)
        except Exception as e:
            print("handle error:", e, file=sys.stderr)
        refresh()
        if res and res[0] == "switch":
            flash_badge(current_layout_code())
        if hist_holder.get("rebuild"):   # обновим окно истории, если открыто
            hist_holder["rebuild"]()

    def drain_x():
        while x11.XPending(x.dpy) > 0:
            x.wait_key(ev)
            g = hk["g"]
            if (ev.type == KeyPress and g and g.get("mode") == "key"
                    and ev.keycode == g["keycode"]):
                fire_hotkey()

    fd = x11.XConnectionNumber(x.dpy)
    notifier = QtCore.QSocketNotifier(fd, QtCore.QSocketNotifier.Read)
    notifier.activated.connect(lambda *_: drain_x())

    # Хоткей из модификаторов (XRecord) — динамически подключаемый нотифаер.
    rec_notifier = {"n": None}

    def wire_hotkey():
        if rec_notifier["n"] is not None:
            rec_notifier["n"].setEnabled(False)
            rec_notifier["n"].deleteLater()
            rec_notifier["n"] = None
        g = hk["g"]
        if g and g.get("mode") == "mod":
            rec = g["recorder"]
            rec.on_fire = fire_hotkey
            n = QtCore.QSocketNotifier(rec.fd(), QtCore.QSocketNotifier.Read)
            n.activated.connect(lambda *_: rec.process())
            rec_notifier["n"] = n
        log("хоткей активен: %s (режим %s)"
            % (g.get("spec") if g else "—", g.get("mode") if g else "нет"))

    wire_hotkey()

    # Периодически обновляем иконку (раскладку могли сменить и через Alt+Shift).
    timer = QtCore.QTimer()
    timer.timeout.connect(refresh)
    timer.start(700)

    refresh()
    tray.show()
    # Если стартовый хоткей оказался занят — сообщаем и открываем настройки.
    if hk["g"] is None:
        extra = find_kde_conflict(cfg["hotkey"])
        notify("Хоткей «%s» занят%s. Выбери другой в настройках."
               % (cfg["hotkey"], " (%s)" % extra if extra else ""))
        QtCore.QTimer.singleShot(800, open_settings)
    print("linux-punto запущен с иконкой в трее. Хоткей: %s." % cfg["hotkey"])
    sys.exit(app.exec_())


def main():
    _lock = acquire_single_instance()  # noqa: F841
    cfg = load_config()
    x = X11Backend()
    try:
        grabbed = x.grab(cfg["hotkey"])
    except HotkeyConflict:
        extra = find_kde_conflict(cfg["hotkey"])
        print("Хоткей %s занят%s. Выбери другой в настройках."
              % (cfg["hotkey"], " (%s)" % extra if extra else ""), file=sys.stderr)
        grabbed = None
    except RuntimeError as e:  # PureModifierHotkey тоже сюда
        print("Хоткей недоступен:", e, file=sys.stderr)
        grabbed = None
    try:
        import PyQt5  # noqa: F401
        run_with_tray(x, cfg, grabbed)
    except ImportError:
        run_headless(x, cfg, grabbed)


if __name__ == "__main__":
    main()
