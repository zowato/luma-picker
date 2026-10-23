"""
Luma Picker - a floating, lightness-locked color palette for digital painting.

The big field shows every sRGB color that has the SAME perceptual lightness
(OKLab L); the bar below changes that lightness. Out-of-gamut area is drawn as
the neutral grey of the same lightness, so the whole field is one value.

Extras: shading ramps (cool shadows / warm lights), color harmonies, floor
shadow + bounce light suggestions, a screen eyedropper and hex copy/paste.

Windows: a global hotkey opens the panel. Picking a color sends it to
Clip Studio Paint through CSP's own "Pick screen color" command (give that
command a shortcut in CSP). Elsewhere / apply_mode "clipboard": the hex code
is copied instead.

https://github.com/zowato/luma-picker  -  MIT License
"""
__version__ = "1.0.0"

import colorsys
import ctypes
import json
import os
import sys

import numpy as np
from PySide6.QtCore import QDateTime, QObject, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QConicalGradient, QCursor, QFont,
                           QGuiApplication, QIcon, QImage, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QWidget

IS_WIN = sys.platform == "win32"

# ----------------------------------------------------------------- settings
if getattr(sys, "frozen", False):
    # the installed app keeps its settings in %APPDATA%\LumaPicker
    APP_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                           "LumaPicker")
    os.makedirs(APP_DIR, exist_ok=True)
else:
    APP_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(APP_DIR, "settings.json")

DEFAULTS = {
    "hotkey": "ctrl+alt+shift+c",
    "eyedropper_hotkey": "ctrl+alt+shift+e",   # pick a color from the screen
    "apply_mode": "screen_pick",          # "screen_pick" or "clipboard"
    "pick_shortcut": "ctrl+alt+shift+o",  # CSP: Edit > Pick screen color
    "pick_delay_ms": 250,          # longest wait for CSP's picker window
    "exit_delay_ms": 90,           # click -> closing right-click
    "flash_ms": 35,                # time for the color dot to appear
    "picker_ready_ms": 150,        # earliest click after CSP's picker opens
    "hue_shift_strength": 1.0,     # 0.5 = subtle, 2 = strong hue shifting
    "right_click_to_exit_pick": True,
    "after_pick_shortcut": "",            # e.g. "b" to go back to the brush
    "also_copy_hex": False,
    "auto_hide": True,
    "auto_hide_distance": 60,
    "always_on_top": True,
    "pinned": False,
    "window_pos": None,
    "last_color": "#c0607a",
    "history": [],
}


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            s.update(json.load(f))
    except FileNotFoundError:
        save_settings(s)
    except Exception as exc:  # broken file: keep defaults, don't crash
        print("settings.json could not be read:", exc)
    return s


def save_settings(s):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except Exception as exc:
        print("settings.json could not be written:", exc)


# --------------------------------------------------------------- color math
EPS = 1e-4


def srgb_to_linear(c):
    c = np.asarray(c, dtype=float)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def linear_to_srgb(c):
    c = np.clip(np.asarray(c, dtype=float), 0.0, 1.0)
    return np.where(c <= 0.0031308, 12.92 * c,
                    1.055 * np.power(c, 1 / 2.4) - 0.055)


def oklab_to_linear(L, a, b):
    l_ = L + 0.3963377774 * a + 0.2158037573 * b
    m_ = L - 0.1055613458 * a - 0.0638541728 * b
    s_ = L - 0.0894841775 * a - 1.2914855480 * b
    l, m, s = l_ ** 3, m_ ** 3, s_ ** 3
    return (4.0767416621 * l - 3.3077115913 * m + 0.2309699292 * s,
            -1.2684380046 * l + 2.6097574011 * m - 0.3413193965 * s,
            -0.0041960863 * l - 0.7034186147 * m + 1.7076147010 * s)


def linear_to_oklab(r, g, b):
    l = np.cbrt(0.4122214708 * r + 0.5363325363 * g + 0.0514459929 * b)
    m = np.cbrt(0.2119034982 * r + 0.6806995451 * g + 0.1073969566 * b)
    s = np.cbrt(0.0883024619 * r + 0.2817188376 * g + 0.6299787005 * b)
    return (0.2104542553 * l + 0.7936177850 * m - 0.0040720468 * s,
            1.9779984951 * l - 2.4285922050 * m + 0.4505937099 * s,
            0.0259040371 * l + 0.7827717662 * m - 0.8086757660 * s)


def in_gamut(r, g, b):
    return ((r >= -EPS) & (r <= 1 + EPS) & (g >= -EPS) & (g <= 1 + EPS)
            & (b >= -EPS) & (b <= 1 + EPS))


def clamp_chroma(L, a, b, iters=22):
    """Pull (a, b) toward neutral until the color fits in sRGB."""
    L, a, b = (np.asarray(x, dtype=float) for x in np.broadcast_arrays(L, a, b))
    ok = in_gamut(*oklab_to_linear(L, a, b))
    lo, hi = np.zeros_like(a), np.ones_like(a)
    for _ in range(iters):
        mid = (lo + hi) / 2
        m = in_gamut(*oklab_to_linear(L, a * mid, b * mid))
        lo = np.where(m, mid, lo)
        hi = np.where(m, hi, mid)
    t = np.where(ok, 1.0, lo)
    return a * t, b * t


def oklab_to_hex(L, a, b):
    a, b = clamp_chroma(L, a, b)
    rgb = linear_to_srgb(np.array(oklab_to_linear(L, float(a), float(b))))
    r, g, bb = (int(round(float(v) * 255)) for v in rgb)
    return "#{:02x}{:02x}{:02x}".format(r, g, bb)


def hex_to_oklab(hx):
    hx = hx.lstrip("#")
    rgb = np.array([int(hx[i:i + 2], 16) / 255 for i in (0, 2, 4)])
    L, a, b = linear_to_oklab(*srgb_to_linear(rgb))
    return float(L), float(a), float(b)


# ---------------------------------------------------------- shading ideas
# Hue anchors in OKLCH degrees: shadows lean toward blue-violet, lights
# toward warm yellow (the "hue contrast" way of shading).
SHADOW_HUE = 275.0
LIGHT_HUE = 95.0
SHADE_N = 7  # swatches per row, base color in the middle


def _toward(h, target, amount):
    """Rotate hue h toward target by up to `amount` degrees (shortest way)."""
    d = (target - h + 540.0) % 360.0 - 180.0
    step = max(-abs(amount), min(abs(amount), d))
    return (h + step) % 360.0


def _lch_to_hex(L, C, h):
    r = np.radians(h)
    return oklab_to_hex(min(max(L, 0.0), 1.0), C * np.cos(r), C * np.sin(r))


# HSV hue anchors (degrees) for the video's rule of thumb:
#   shadows -> cooler (toward blue), MORE saturated, darker
#   lights  -> warmer (toward yellow), LESS saturated, lighter
COOL_HUE = 235.0
WARM_HUE = 55.0


def _hsv_hex(h, s, v):
    r, g, b = colorsys.hsv_to_rgb((h % 360.0) / 360.0,
                                  min(max(s, 0.0), 1.0), min(max(v, 0.0), 1.0))
    return "#{:02x}{:02x}{:02x}".format(*(int(round(c * 255)) for c in (r, g, b)))


def _rotate(h, target, amount, direction=0):
    """Turn hue h toward target by up to `amount` degrees.
    direction: 0 = shortest way, +1 / -1 = force that way round."""
    if direction == 0:
        d = (target - h + 540.0) % 360.0 - 180.0
    elif direction > 0:
        d = (target - h) % 360.0
    else:
        d = -((h - target) % 360.0)
    step = max(-amount, min(amount, d))
    return (h + step) % 360.0


def shading_rows(L, a, b, shift=1.0):
    """Shades for a base color, dark -> light, following the usual painting
    rule: shadows cooler, more saturated and darker; lights warmer, less
    saturated and lighter. Hue, saturation AND value all change."""
    base = oklab_to_hex(L, a, b)
    r, g, bl = (int(base[i:i + 2], 16) / 255.0 for i in (1, 3, 5))
    h, s, v = colorsys.rgb_to_hsv(r, g, bl)
    h *= 360.0
    neutral = s < 0.06
    half = SHADE_N // 2

    # which way round the wheel each side travels
    if 30.0 <= h <= 72.0:
        shadow_dir = -1     # yellows/oranges darken through orange-red, not green
    else:
        shadow_dir = 0
    if 170.0 <= h <= 255.0:
        light_dir = -1      # blues light up through cyan/teal, not purple
    else:
        light_dir = 0

    row = []
    for k in range(-half, half + 1):
        if k == 0:
            row.append(base)
            continue
        n = abs(k)
        if k < 0:   # ---- shadow: cooler, more saturated, darker
            hk = _rotate(h, COOL_HUE, 10.0 * n * shift, shadow_dir)
            if neutral:
                hk, sk = COOL_HUE, 0.06 * n
            else:
                sk = min(s + (1.0 - s) * 0.18 * n + 0.04 * n, 0.92)
            vk = v * (1.0 - 0.19 * n)
        else:       # ---- light: warmer, less saturated, lighter
            hk = _rotate(h, WARM_HUE, 9.0 * n * shift, light_dir)
            if neutral:
                hk, sk = WARM_HUE, 0.05 * n
            else:
                sk = s * (1.0 - 0.2 * n)
            vk = v + (1.0 - v) * 0.3 * n + 0.03 * n
        row.append(_hsv_hex(hk, sk, max(vk, 0.06)))
    return row


# --------------------------------------------------------- color harmonies
# hue offsets in degrees around the OKLCH wheel; the first entry is the base
HARMONIES = [
    ("Complementary", (0, 180)),
    ("Split compl.", (0, 150, 210)),
    ("Triad", (0, 120, 240)),
    ("Square", (0, 90, 180, 270)),
    ("Tetradic", (0, 60, 180, 240)),
    ("Analogous", (-30, 0, 30)),
]


def harmony_colors(L, a, b, offsets):
    """Partner colors at the same lightness, rotated around the hue wheel.
    Returns [(hex, hue_degrees, is_base), ...] in the order given."""
    a, b = (float(v) for v in clamp_chroma(L, a, b))
    C = float(np.hypot(a, b))
    h = float(np.degrees(np.arctan2(b, a))) % 360.0
    neutral = C < 0.02
    out = []
    for off in offsets:
        if off == 0:
            out.append((oklab_to_hex(L, a, b), h, True))
            continue
        hk = (h + off) % 360.0
        # a grey has no hue of its own: give the partners a gentle tint
        out.append((_lch_to_hex(L, 0.08 if neutral else C, hk), hk, False))
    return out


# ------------------------------------------------------ surroundings / bounce
def _hex_rgb(hx):
    return np.array([int(hx[i:i + 2], 16) / 255.0 for i in (1, 3, 5)])


def _mix(hx1, hx2, t):
    """Mix two colors in linear light (like paint glazing / real light)."""
    a, b = srgb_to_linear(_hex_rgb(hx1)), srgb_to_linear(_hex_rgb(hx2))
    c = linear_to_srgb(a * (1.0 - t) + b * t)
    return "#{:02x}{:02x}{:02x}".format(*(int(round(float(x) * 255)) for x in c))


def _hsv_of(hx):
    h, s, v = colorsys.rgb_to_hsv(*_hex_rgb(hx))
    return h * 360.0, s, v


def surroundings(surface_hex, object_hex, shadow_hexes, shift=1.0):
    """Suggestions for the area around an object.

    floor:  the object's cast shadow on the surface, from the dark contact
            shadow under the object out to the soft edge. It's the SURFACE
            color made darker, cooler and a bit more saturated, with a hint
            of the object's color only right at the contact point.
    bounce: light reflected from the surface into the object's shadow side
            (the "reflection of the surroundings"): the object's shadow
            mixed with the surface color, strongest near the bottom edge.
    """
    h, s, v = _hsv_of(surface_hex)
    neutral = s < 0.06

    def shadow_of_surface(dark, cool, sat):
        hk = COOL_HUE if neutral else _rotate(h, COOL_HUE, cool * shift)
        sk = (0.04 + sat * 0.5) if neutral else min(s + (1 - s) * sat, 0.9)
        return _hsv_hex(hk, sk, v * dark)

    edge = shadow_of_surface(0.86, 6.0, 0.04)
    mid = shadow_of_surface(0.68, 12.0, 0.10)
    core = shadow_of_surface(0.52, 16.0, 0.16)
    # contact / occlusion shadow: darkest, picks up a little of the object
    contact = _mix(shadow_of_surface(0.36, 18.0, 0.22), shadow_hexes[0], 0.15)
    floor = [contact, core, mid, edge]

    # object's shadow side lit by the surface: shadow -> more surface color
    obj_shadow = shadow_hexes[1]
    bounce = [_mix(obj_shadow, surface_hex, t) for t in (0.12, 0.25, 0.38)]
    # strongest bounce right at the bottom rim, a touch lighter
    rim = _mix(_mix(obj_shadow, surface_hex, 0.5), "#ffffff", 0.06)
    bounce.append(rim)
    return floor, bounce


_fit_cache = {}


def fit_for_L(L):
    """Center and half-size of the view so the gamut slice fills the field."""
    key = round(float(L), 3)
    if key not in _fit_cache:
        h = np.linspace(0, 2 * np.pi, 144, endpoint=False)
        a, b = clamp_chroma(np.full_like(h, key), np.cos(h) * 0.5, np.sin(h) * 0.5)
        cx, cy = (a.min() + a.max()) / 2, (b.min() + b.max()) / 2
        half = max(a.max() - a.min(), b.max() - b.min()) / 2 * 1.14
        _fit_cache[key] = (float(cx), float(cy), float(max(half, 0.02)))
    return _fit_cache[key]


def _to_qimage(rgb):  # rgb: H x W x 3 floats in sRGB 0..1
    h, w, _ = rgb.shape
    arr = np.empty((h, w, 4), dtype=np.uint8)
    arr[..., :3] = np.clip(np.round(rgb * 255), 0, 255).astype(np.uint8)
    arr[..., 3] = 255
    return QImage(arr.data, w, h, w * 4, QImage.Format_RGBA8888).copy()


def render_field(L, n):
    cx, cy, half = fit_for_L(L)
    t = (np.arange(n) + 0.5) / n * 2 * half - half
    A, B = np.meshgrid(cx + t, cy - t)
    r, g, b = oklab_to_linear(L, A, B)
    ok = in_gamut(r, g, b)
    gr, gg, gb = oklab_to_linear(L, 0.0, 0.0)
    rgb = np.stack([np.where(ok, r, gr), np.where(ok, g, gg),
                    np.where(ok, b, gb)], axis=-1)
    return _to_qimage(linear_to_srgb(rgb))


def render_slider(a, b, w, h):
    Ls = (np.arange(w) + 0.5) / w
    aa, bb = clamp_chroma(Ls, np.full(w, a), np.full(w, b))
    rgb = linear_to_srgb(np.stack(oklab_to_linear(Ls, aa, bb), axis=-1))
    return _to_qimage(np.repeat(rgb[None, :, :], h, axis=0))


# ------------------------------------------------------------ windows glue
MOD_FLAG = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
MOD_VK = {"ctrl": 0x11, "alt": 0x12, "shift": 0x10, "win": 0x5B}
NAMED_VK = {"space": 0x20, "tab": 0x09, "enter": 0x0D, "esc": 0x1B,
            "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
            "pageup": 0x21, "pagedown": 0x22, "pause": 0x13,
            "scrolllock": 0x91}


def parse_combo(text):
    mods, vk = [], None
    for part in text.lower().replace(" ", "").split("+"):
        if part in ("ctrl", "control"):
            mods.append("ctrl")
        elif part in ("alt", "shift"):
            mods.append(part)
        elif part in ("win", "meta"):
            mods.append("win")
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part[:1] == "f" and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            vk = 0x6F + int(part[1:])
        elif part.startswith("numpad") and part[6:].isdigit():
            vk = 0x60 + int(part[6:])
        elif part in NAMED_VK:
            vk = NAMED_VK[part]
        else:
            raise ValueError("unknown key '%s' in '%s'" % (part, text))
    if vk is None:
        raise ValueError("no main key in '%s'" % text)
    return mods, vk


if IS_WIN:
    from ctypes import wintypes
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class _INPUTUNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
    user32.IsWindow.argtypes = (wintypes.HWND,)

    def _send(inputs):
        arr = (INPUT * len(inputs))(*inputs)
        user32.SendInput(len(inputs), arr, ctypes.sizeof(INPUT))

    def _key(vk, up=False):
        scan = user32.MapVirtualKeyW(vk, 0)
        return INPUT(type=1, u=_INPUTUNION(ki=KEYBDINPUT(
            wVk=vk, wScan=scan, dwFlags=0x2 if up else 0, time=0, dwExtraInfo=0)))

    def send_combo(text):
        mods, vk = parse_combo(text)
        down = [_key(MOD_VK[m]) for m in mods] + [_key(vk)]
        up = [_key(vk, True)] + [_key(MOD_VK[m], True) for m in reversed(mods)]
        _send(down + up)

    def click(button="left"):
        d, u = (0x2, 0x4) if button == "left" else (0x8, 0x10)
        mk = lambda f: INPUT(type=0, u=_INPUTUNION(mi=MOUSEINPUT(
            dx=0, dy=0, mouseData=0, dwFlags=f, time=0, dwExtraInfo=0)))
        _send([mk(d), mk(u)])

    def set_noactivate(hwnd):
        GWL_EXSTYLE = -20
        style = user32.GetWindowLongPtrW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongPtrW(hwnd, GWL_EXSTYLE,
                                 style | 0x08000000 | 0x00000080)  # NOACTIVATE|TOOLWINDOW

    user32.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                    ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                    wintypes.UINT)

    def set_topmost(hwnd, on):
        # HWND_TOPMOST / HWND_NOTOPMOST; SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE
        user32.SetWindowPos(hwnd, wintypes.HWND(-1 if on else -2), 0, 0, 0, 0,
                            0x0001 | 0x0002 | 0x0010)

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = (WNDENUMPROC, wintypes.LPARAM)
    user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD))
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)

    def window_pid(hwnd):
        pid = wintypes.DWORD(0)
        if hwnd:
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def visible_windows_of(pid):
        """Visible top-level windows belonging to a process."""
        found = set()

        def cb(h, _):
            if user32.IsWindowVisible(h) and window_pid(h) == pid:
                found.add(int(h) if h else 0)
            return True
        user32.EnumWindows(WNDENUMPROC(cb), 0)
        return found

    def foreground():
        return user32.GetForegroundWindow()

    def focus(hwnd):
        if hwnd and user32.IsWindow(hwnd) and user32.GetForegroundWindow() != hwnd:
            user32.SetForegroundWindow(hwnd)


class HotkeyWindow(QWidget):
    """Hidden window that receives WM_HOTKEY from RegisterHotKey."""
    triggered = Signal(int)

    def __init__(self):
        super().__init__()
        self.hwnd = int(self.winId())

    def register(self, text, hk_id=1):
        mods, vk = parse_combo(text)
        flags = 0x4000  # MOD_NOREPEAT
        for m in mods:
            flags |= MOD_FLAG[m]
        user32.UnregisterHotKey(wintypes.HWND(self.hwnd), hk_id)
        return bool(user32.RegisterHotKey(wintypes.HWND(self.hwnd), hk_id, flags, vk))

    def nativeEvent(self, event_type, message):
        msg = wintypes.MSG.from_address(int(message))
        if msg.message == 0x0312:  # WM_HOTKEY
            self.triggered.emit(int(msg.wParam))
            return True, 0
        return False, 0


# ------------------------------------------------------------------- panel
W = 288
PAD = 12
HEADER_H = 44                      # drag the window by this top bar
FIELD = QRect(PAD, 46, W - 2 * PAD, W - 2 * PAD)
SLIDER = QRect(PAD, FIELD.bottom() + 11, W - 2 * PAD, 24)
HIST_Y = SLIDER.bottom() + 11
HIST_N = 8
HIST_GAP = 6
HIST_SIZE = (FIELD.width() - HIST_GAP * (HIST_N - 1)) // HIST_N
SHADE_LABEL_Y = HIST_Y + HIST_SIZE + 12          # "Shading" caption line
SHADE_GAP = 5
SHADE_SIZE = (FIELD.width() - SHADE_GAP * (SHADE_N - 1)) // SHADE_N
SHADE_ROW_H = 26
SHADE_Y = SHADE_LABEL_Y + 18
JUMP_BTN = QRect(W - PAD - 58, SHADE_LABEL_Y - 2, 58, 18)   # "Jump" toggle

HARM_LABEL_Y = SHADE_Y + SHADE_ROW_H + 10                  # "Harmony" caption
HARM_PREV = QRect(W - PAD - 132, HARM_LABEL_Y - 2, 18, 18)  # ‹
HARM_NEXT = QRect(W - PAD - 18, HARM_LABEL_Y - 2, 18, 18)   # ›
HARM_NAME = QRect(HARM_PREV.right() + 1, HARM_LABEL_Y - 2,
                  HARM_NEXT.left() - HARM_PREV.right() - 2, 18)
HARM_Y = HARM_LABEL_Y + 20
HARM_ROW_H = 34
HARM_WHEEL = QRect(PAD, HARM_Y, HARM_ROW_H, HARM_ROW_H)    # mini hue wheel
HARM_X0 = HARM_WHEEL.right() + 10
SURR_LABEL_Y = HARM_Y + HARM_ROW_H + 12            # "Surroundings" caption
SURF_CUR = QRect(W - PAD - 54, SURR_LABEL_Y - 2, 54, 18)    # "Current"
SURF_PICK = QRect(SURF_CUR.left() - 48, SURR_LABEL_Y - 2, 44, 18)  # "Pick"
SURF_SW = QRect(SURF_PICK.left() - 30, SURR_LABEL_Y - 2, 26, 18)   # surface color
SURR_CAP_W = 48
SURR_N = 4
SURR_ROW_H = 24
SURR_Y = (SURR_LABEL_Y + 20, SURR_LABEL_Y + 20 + SURR_ROW_H + 6)
H = SURR_Y[1] + SURR_ROW_H + PAD + 1


def surr_rect(row, i):
    avail = FIELD.width() - SURR_CAP_W
    w = (avail - SHADE_GAP * (SURR_N - 1)) // SURR_N
    return QRect(PAD + SURR_CAP_W + i * (w + SHADE_GAP), SURR_Y[row], w, SURR_ROW_H)


def shade_rect(i):
    x = PAD + i * (SHADE_SIZE + SHADE_GAP)
    return QRect(x, SHADE_Y, SHADE_SIZE, SHADE_ROW_H)


def harm_rect(i, n):
    avail = PAD + FIELD.width() - HARM_X0
    w = (avail - SHADE_GAP * (n - 1)) // n
    return QRect(HARM_X0 + i * (w + SHADE_GAP), HARM_Y, w, HARM_ROW_H)
CLOSE_BTN = QRect(W - PAD - 20, 12, 20, 20)
PIN_BTN = QRect(CLOSE_BTN.left() - 24, 12, 20, 20)
TOP_BTN = QRect(PIN_BTN.left() - 24, 12, 20, 20)
NEW_SW = QRect(TOP_BTN.left() - 8 - 28, 13, 28, 18)
OLD_SW = QRect(NEW_SW.left() - 28, 13, 28, 18)
EYE_BTN = QRect(OLD_SW.left() - 26, 12, 20, 20)            # eyedropper
HEX_RECT = QRect(PAD + 2, 12, EYE_BTN.left() - PAD - 6, 20)  # click = copy

BG, BORDER, TEXT, DIM = "#1c1c22", "#5b4bd1", "#d8d8e4", "#7d7d8c"


def hist_rect(i):
    return QRect(PAD + i * (HIST_SIZE + HIST_GAP), HIST_Y, HIST_SIZE, HIST_SIZE)


class _SamplerScreen(QWidget):
    """One monitor: shows a frozen screenshot; clicking samples from it."""

    def __init__(self, owner, scr):
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint)
        self.owner = owner
        self.pm = scr.grabWindow(0)               # snapshot of this monitor
        self.img = self.pm.toImage()
        self.setGeometry(scr.geometry())
        self.setCursor(Qt.CrossCursor)
        self.setMouseTracking(True)
        self.hover = None

    def paintEvent(self, _):
        p = QPainter(self)
        p.drawPixmap(self.rect(), self.pm)
        if self.hover is not None:            # little preview of the color
            pos, col = self.hover
            box = QRectF(pos.x() + 16, pos.y() + 16, 34, 34)
            p.setPen(QPen(QColor("#ffffff"), 2))
            p.setBrush(col)
            p.drawRoundedRect(box, 6, 6)

    def _color_at(self, local):
        sx = self.img.width() / max(self.width(), 1)
        sy = self.img.height() / max(self.height(), 1)
        x = min(max(int(local.x() * sx), 0), self.img.width() - 1)
        y = min(max(int(local.y() * sy), 0), self.img.height() - 1)
        return self.img.pixelColor(x, y)

    def mouseMoveEvent(self, e):
        local = e.position().toPoint()
        self.hover = (local, self._color_at(local))
        self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape:
            self.owner.finish(None)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            self.owner.finish(None)
            return
        self.owner.finish(self._color_at(e.position().toPoint()).name())


class ScreenSampler(QObject):
    """Pick any color on any screen: the screens freeze for a moment and the
    next click samples the color (the click does not reach other apps)."""
    picked = Signal(str)

    def __init__(self):
        super().__init__()
        self.views = []

    def start(self):
        self.finish(None)
        self.views = [_SamplerScreen(self, scr) for scr in QGuiApplication.screens()]
        for v in self.views:
            v.show()
            v.raise_()
            if IS_WIN:
                set_topmost(int(v.winId()), True)
        if self.views:
            self.views[0].activateWindow()

    def finish(self, hx):
        for v in self.views:
            v.hide()
            v.deleteLater()
        self.views = []
        if hx:
            self.picked.emit(hx)


class FlashDot(QWidget):
    """A few pixels of the picked color right under the pen tip. Clip Studio's
    screen picker samples this instead of the whole panel changing color."""
    SIZE = 9

    def __init__(self):
        # click-through: the pick click must land on the panel underneath,
        # Clip Studio ignores clicks that hit this little window itself
        super().__init__(None, Qt.Tool | Qt.FramelessWindowHint
                         | Qt.WindowStaysOnTopHint | Qt.WindowDoesNotAcceptFocus
                         | Qt.WindowTransparentForInput)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.color = QColor("#000000")
        if IS_WIN:
            set_noactivate(int(self.winId()))

    def show_at_cursor(self, hx):
        self.color = QColor(hx)
        pos = QCursor.pos()
        self.move(pos.x() - self.SIZE // 2, pos.y() - self.SIZE // 2)
        if not self.isVisible():
            self.show()
        if IS_WIN:
            set_topmost(int(self.winId()), True)
        self.repaint()

    def paintEvent(self, _):
        QPainter(self).fillRect(self.rect(), self.color)


class LumaPicker(QWidget):
    def __init__(self, settings, tray_notify=None):
        self.on_top = bool(settings.get("always_on_top", True))
        flags = Qt.Tool | Qt.FramelessWindowHint | Qt.WindowDoesNotAcceptFocus
        if self.on_top:
            flags |= Qt.WindowStaysOnTopHint
        super().__init__(None, flags)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setFixedSize(W, H)
        self.setMouseTracking(True)
        self.setWindowTitle("Luma Picker")
        self.settings = settings
        self.notify = tray_notify or (lambda *a: None)
        self.L, self.a, self.b = hex_to_oklab(settings.get("last_color", "#c0607a"))
        self.applied_hex = self.current_hex()
        self.history = [h for h in settings.get("history", []) if isinstance(h, str)][:HIST_N]
        self.dragging = None
        self.applying = False
        self.flash = None
        self.dot = FlashDot()
        self.pinned = bool(settings.get("pinned", False))
        self.target_hwnd = None
        self.move_offset = None
        self._field_key = self._slider_key = None
        self._shade_key = None
        self._shade_rows = []
        self.shade_base = None      # set while "Jump" is off and you pick shades
        self.pending = None         # swatch pressed, sent when the pen lifts
        self.sampler = ScreenSampler()
        self.sampler.picked.connect(self._on_sampled)
        self.sample_target = "surface"
        self.toast = None           # (text, until_ms) short message in the header
        self._field_img = self._slider_img = None
        if IS_WIN:
            set_noactivate(int(self.winId()))
        self.hide_timer = QTimer(self, interval=120, timeout=self._check_auto_hide)
        # some apps (fullscreen canvases) push other windows down; re-assert
        self.top_timer = QTimer(self, interval=1000, timeout=self._apply_topmost)

    # ---------------------------------------------------------- window modes
    def _apply_topmost(self):
        if not self.isVisible():
            return
        if IS_WIN:
            set_topmost(int(self.winId()), self.on_top)
        elif self.on_top:
            self.raise_()

    def set_on_top(self, on):
        self.on_top = on
        self.settings["always_on_top"] = on
        save_settings(self.settings)
        if IS_WIN:
            set_topmost(int(self.winId()), on)
        else:
            vis = self.isVisible()
            self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
            if vis:
                self.show()
        if on:
            self.top_timer.start()
        else:
            self.top_timer.stop()
        self.update()

    def set_pinned(self, on):
        self.pinned = on
        self.settings["pinned"] = on
        save_settings(self.settings)
        self.update()

    def _saved_pos(self):
        p = self.settings.get("window_pos")
        if isinstance(p, list) and len(p) == 2:
            from PySide6.QtCore import QPoint
            pt = QPoint(int(p[0]), int(p[1]))
            # only if that spot is still on a connected screen
            for scr in QGuiApplication.screens():
                if scr.availableGeometry().contains(pt + QPointF(W / 2, 20).toPoint()):
                    return pt
        return None

    # ------------------------------------------------------------ color state
    def effective_ab(self):
        a, b = clamp_chroma(self.L, self.a, self.b)
        return float(a), float(b)

    def current_hex(self):
        return oklab_to_hex(self.L, self.a, self.b)

    def set_hex(self, hx):
        self.L, self.a, self.b = hex_to_oklab(hx)
        self.update()

    def _base(self):
        """Color the shading/harmony rows are built from."""
        return self.shade_base if self.shade_base else (self.L, self.a, self.b)

    def shades(self):
        L, a, b = self._base()
        key = (round(L, 4), round(a, 4), round(b, 4),
               float(self.settings.get("hue_shift_strength", 1.0)))
        if key != self._shade_key:
            self._shade_rows = shading_rows(L, a, b, key[3])
            self._shade_key = key
        return self._shade_rows

    def harmony_index(self):
        i = int(self.settings.get("harmony", 0))
        return i % len(HARMONIES)

    def harmonies(self):
        L, a, b = self._base()
        return harmony_colors(L, a, b, HARMONIES[self.harmony_index()][1])

    def cycle_harmony(self, step):
        self.settings["harmony"] = (self.harmony_index() + step) % len(HARMONIES)
        save_settings(self.settings)
        self.update()

    def surface(self):
        hx = self.settings.get("surface", "#d6d6d6")
        return hx if isinstance(hx, str) and len(hx) == 7 else "#d6d6d6"

    def set_surface(self, hx):
        self.settings["surface"] = hx
        save_settings(self.settings)
        self.update()

    def surroundings(self):
        L, a, b = self._base()
        return surroundings(self.surface(), oklab_to_hex(L, a, b), self.shades(),
                            float(self.settings.get("hue_shift_strength", 1.0)))

    # --------------------------------------------- copy / paste / eyedropper
    def show_toast(self, text, ms=1200):
        self.toast = (text, QDateTime.currentMSecsSinceEpoch() + ms)
        self.update()
        QTimer.singleShot(ms + 30, self.update)

    def copy_hex(self):
        hx = self.current_hex().upper()
        QGuiApplication.clipboard().setText(hx)
        self.show_toast("Copied " + hx)

    def paste_hex(self):
        import re
        txt = (QGuiApplication.clipboard().text() or "").strip()
        m = re.search(r"#?([0-9a-fA-F]{6})\b", txt)
        if not m:
            self.show_toast("No hex in clipboard")
            return
        self.take_color("#" + m.group(1).lower())
        self.show_toast("Pasted #" + m.group(1).upper())

    def take_color(self, hx):
        """Load a color into Luma without sending it anywhere (it came from
        Clip Studio / the screen already)."""
        self.shade_base = None
        self.set_hex(hx)
        self.applied_hex = hx
        if hx in self.history:
            self.history.remove(hx)
        self.history.insert(0, hx)
        del self.history[HIST_N:]
        self.settings["history"] = self.history
        self.settings["last_color"] = hx
        save_settings(self.settings)
        self.update()

    def start_eyedropper(self, target="main"):
        self.sample_target = target
        self.sampler.start()

    def _on_sampled(self, hx):
        if self.sample_target == "main":
            self.take_color(hx)
            if not self.isVisible():
                self.show_at_cursor()
            self.show_toast("Picked " + hx.upper())
        else:
            self.set_surface(hx)

    def set_jump(self, on):
        self.settings["jump_to_picked"] = on
        save_settings(self.settings)
        self.shade_base = None
        self.update()

    def pick_suggestion(self, hx):
        """A shade or harmony swatch was clicked."""
        if self.settings.get("jump_to_picked", True):
            # move the main wheel to that color; the rows follow it
            self.shade_base = None
            self.set_hex(hx)
        else:
            # keep the rows where they are so you can grab several in a row
            if self.shade_base is None:
                self.shade_base = (self.L, self.a, self.b)
        self.apply_hex(hx)

    # --------------------------------------------------------------- geometry
    def ab_to_point(self, a, b):
        cx, cy, half = fit_for_L(self.L)
        x = FIELD.left() + (a - (cx - half)) / (2 * half) * FIELD.width()
        y = FIELD.top() + ((cy + half) - b) / (2 * half) * FIELD.height()
        return QPointF(x, y)

    def point_to_ab(self, p):
        cx, cy, half = fit_for_L(self.L)
        fx = min(max((p.x() - FIELD.left()) / FIELD.width(), 0.0), 1.0)
        fy = min(max((p.y() - FIELD.top()) / FIELD.height(), 0.0), 1.0)
        return cx - half + fx * 2 * half, cy + half - fy * 2 * half

    # ---------------------------------------------------------------- showing
    @staticmethod
    def _area_for(pos):
        for scr in QGuiApplication.screens():
            if scr.geometry().contains(pos):
                return scr.availableGeometry()
        return QGuiApplication.primaryScreen().availableGeometry()

    def _place(self, pos):
        ag = self._area_for(pos)
        x = pos.x() - FIELD.center().x()
        y = pos.y() - FIELD.center().y()
        x = max(ag.left(), min(x, ag.left() + ag.width() - W))
        y = max(ag.top(), min(y, ag.top() + ag.height() - H))
        self.move(x, y)

    def show_at_cursor(self):
        if IS_WIN:
            self.target_hwnd = foreground()
        pos = QCursor.pos()
        saved = self._saved_pos() if self.pinned else None
        if saved is not None:
            self.move(saved)
        else:
            self._place(pos)
        if self.isMinimized():
            self.setWindowState(Qt.WindowNoState)
        self.show()
        self.raise_()
        if IS_WIN:
            set_noactivate(int(self.winId()))
        if saved is not None:
            self.move(saved)
        else:
            self._place(pos)  # again, now that the window exists on its screen
        self._apply_topmost()
        self.hide_timer.start()
        if self.on_top:
            self.top_timer.start()

    def show_centered(self):
        """Used from the tray icon, where the cursor sits on the taskbar."""
        if not self.pinned:
            self.set_pinned(True)
        self.show_at_cursor()
        if self._saved_pos() is None:
            ag = QGuiApplication.primaryScreen().availableGeometry()
            self.move(ag.center().x() - W // 2, ag.center().y() - H // 2)
        self.update()

    def hide_popup(self):
        self.dragging = None
        self.move_offset = None
        self.hide_timer.stop()
        self.top_timer.stop()
        self.hide()

    def toggle(self):
        if self.isVisible():
            self.hide_popup()
        else:
            self.show_at_cursor()

    def _check_auto_hide(self):
        if (not self.isVisible() or self.pinned or self.applying or self.dragging
                or self.move_offset is not None
                or not self.settings.get("auto_hide", True)):
            return
        d = int(self.settings.get("auto_hide_distance", 60))
        if not self.frameGeometry().adjusted(-d, -d, d, d).contains(QCursor.pos()):
            self.hide_popup()

    # ---------------------------------------------------------------- applying
    def apply_hex(self, hx):
        mode = self.settings.get("apply_mode", "screen_pick")
        if mode != "screen_pick" or not IS_WIN:
            QGuiApplication.clipboard().setText(hx)
            self._finish_apply(hx)
            return
        try:
            parse_combo(self.settings.get("pick_shortcut", ""))
        except ValueError as exc:
            QGuiApplication.clipboard().setText(hx)
            self.notify("pick_shortcut is not valid (%s). Copied %s instead." % (exc, hx))
            self._finish_apply(hx)
            return
        self.applying = True
        self._flash_on(hx)
        QApplication.processEvents()
        focus(self.target_hwnd)
        # give Windows a frame or two to actually put the dot on screen
        QTimer.singleShot(int(self.settings.get("flash_ms", 35)),
                          lambda: self._step_shortcut(hx))

    def _step_shortcut(self, hx):
        self._flash_on(hx)
        # remember CSP's windows, so we can spot its picker window opening
        self._pid = window_pid(self.target_hwnd)
        self._before = visible_windows_of(self._pid) if self._pid else set()
        send_combo(self.settings["pick_shortcut"])
        self._t0 = QDateTime.currentMSecsSinceEpoch()
        QTimer.singleShot(15, lambda: self._wait_for_picker(hx))

    def _wait_for_picker(self, hx):
        """Click as soon as CSP's picker is up, instead of a fixed wait."""
        waited = QDateTime.currentMSecsSinceEpoch() - self._t0
        limit = int(self.settings.get("pick_delay_ms", 250))
        opened = bool(self._pid) and bool(visible_windows_of(self._pid) - self._before)
        # CSP's picker window shows up after ~65 ms but only takes clicks
        # a little later, so never click before "picker_ready_ms"
        ready = int(self.settings.get("picker_ready_ms", 150))
        if (opened and waited >= ready) or waited >= max(limit, ready + 60):
            self._log("picker %s, clicked after %d ms" % (
                "seen" if opened else "NOT seen", waited))
            self._step_click(hx)
        else:
            QTimer.singleShot(10, lambda: self._wait_for_picker(hx))

    def _flash_on(self, hx):
        """Put the color under the pen for CSP's screen picker to sample:
        a tiny dot ("dot") or the whole panel ("panel")."""
        if self.settings.get("flash_mode", "dot") == "panel":
            self.flash = hx
            set_topmost(int(self.winId()), True)
            self.repaint()
        else:
            self.dot.show_at_cursor(hx)

    def _log(self, msg):
        if not self.settings.get("debug_log", False):
            return
        try:
            with open(os.path.join(APP_DIR, "pick_log.txt"), "a", encoding="utf-8") as f:
                f.write("%s  %s  target=%s\n" % (
                    QDateTime.currentDateTime().toString("HH:mm:ss.zzz"), msg,
                    self.target_hwnd))
        except Exception:
            pass

    def _step_click(self, hx):
        self._flash_on(hx)            # follow the pen if it moved meanwhile
        QApplication.processEvents()
        click("left")
        self._log("left-click sent")
        # CSP needs a moment to take the click before the closing right-click
        QTimer.singleShot(int(self.settings.get("exit_delay_ms", 90)),
                          lambda: self._step_exit(hx))

    def _step_exit(self, hx):
        self._log("right-click at %d ms" % (QDateTime.currentMSecsSinceEpoch() - self._t0))
        if self.settings.get("right_click_to_exit_pick", True):
            click("right")
        after = self.settings.get("after_pick_shortcut", "")
        if after:
            QTimer.singleShot(30, lambda: send_combo(after))
        QTimer.singleShot(40, lambda: self._finish_apply(hx))

    def _finish_apply(self, hx):
        self.flash = None
        self.applying = False
        self.dot.hide()
        if IS_WIN and not self.on_top:
            set_topmost(int(self.winId()), False)
        self.applied_hex = hx
        if self.settings.get("also_copy_hex") and self.settings.get("apply_mode") == "screen_pick":
            QGuiApplication.clipboard().setText(hx)
        if hx in self.history:
            self.history.remove(hx)
        self.history.insert(0, hx)
        del self.history[HIST_N:]
        self.settings["history"] = self.history
        self.settings["last_color"] = hx
        save_settings(self.settings)
        self.update()

    # ------------------------------------------------------------------ input
    def mousePressEvent(self, e):
        if self.applying:
            return
        if e.button() == Qt.RightButton and HEX_RECT.contains(e.position().toPoint()):
            self.paste_hex()
            return
        if e.button() != Qt.LeftButton:
            return
        if IS_WIN:
            # the panel never takes focus, so the app in front is the one to paint in
            fg = foreground()
            if fg and int(fg) != int(self.winId()):
                self.target_hwnd = fg
        p = e.position()
        pt = p.toPoint()
        if FIELD.contains(pt):
            self.dragging = "field"
            self._drag_field(p)
        elif SLIDER.adjusted(0, -4, 0, 4).contains(pt):
            self.dragging = "slider"
            self._drag_slider(p)
        elif CLOSE_BTN.contains(pt):
            self.hide_popup()
        elif PIN_BTN.contains(pt):
            self.set_pinned(not self.pinned)
        elif TOP_BTN.contains(pt):
            self.set_on_top(not self.on_top)
        elif OLD_SW.contains(pt):
            self.set_hex(self.applied_hex)
        elif EYE_BTN.contains(pt):
            self.start_eyedropper("main")
        elif HEX_RECT.contains(pt):
            self.copy_hex()
        elif pt.y() < HEADER_H:
            g = e.globalPosition().toPoint()
            self.move_offset = g - self.pos()
        else:
            for i, hx in enumerate(self.history):
                if hist_rect(i).contains(pt):
                    self.pending = ("history", hx)   # sent on pen-up
                    return
            if JUMP_BTN.contains(pt):
                self.set_jump(not self.settings.get("jump_to_picked", True))
                return
            if HARM_PREV.contains(pt):
                self.cycle_harmony(-1)
                return
            if HARM_NEXT.contains(pt) or HARM_NAME.contains(pt):
                self.cycle_harmony(1)
                return
            for i, hx in enumerate(self.shades()):
                if shade_rect(i).contains(pt):
                    self.pending = ("suggest", hx)   # sent on pen-up
                    return
            hs = self.harmonies()
            for i, (hx, _h, _base) in enumerate(hs):
                if harm_rect(i, len(hs)).contains(pt):
                    self.pending = ("suggest", hx)
                    return
            if SURF_PICK.contains(pt):
                self.start_eyedropper("surface")   # next click on screen
                return
            if SURF_CUR.contains(pt) or SURF_SW.contains(pt):
                self.set_surface(self.current_hex())
                return
            rows = self.surroundings()
            for r in (0, 1):
                for i, hx in enumerate(rows[r]):
                    if surr_rect(r, i).contains(pt):
                        # send without moving the wheel: these depend on it
                        self.pending = ("send", hx)
                        return

    def mouseMoveEvent(self, e):
        if self.applying:
            return
        if self.move_offset is not None:
            self.move(e.globalPosition().toPoint() - self.move_offset)
            return
        pt = e.position().toPoint()
        over_btn = any(r.contains(pt) for r in (CLOSE_BTN, PIN_BTN, TOP_BTN, EYE_BTN, HEX_RECT,
                                               OLD_SW, NEW_SW))
        want = (Qt.SizeAllCursor if pt.y() < HEADER_H and not over_btn
                else Qt.PointingHandCursor if over_btn else Qt.ArrowCursor)
        if self.cursor().shape() != want:
            self.setCursor(want)
        if self.dragging == "field":
            self._drag_field(e.position())
        elif self.dragging == "slider":
            self._drag_slider(e.position())

    def mouseReleaseEvent(self, e):
        if self.applying or e.button() != Qt.LeftButton:
            return
        if self.move_offset is not None:
            self.move_offset = None
            # a window you placed yourself stays there and stays open
            self.settings["window_pos"] = [self.x(), self.y()]
            self.set_pinned(True)
            return
        if self.dragging in ("field", "slider"):
            self.dragging = None
            self.apply_hex(self.current_hex())
            return
        if self.pending:
            kind, hx = self.pending
            self.pending = None
            if kind == "history":
                self.shade_base = None
                self.set_hex(hx)
                self.apply_hex(hx)
            elif kind == "send":
                self.apply_hex(hx)
            else:
                self.pick_suggestion(hx)

    def _drag_field(self, p):
        self.shade_base = None
        a, b = self.point_to_ab(p)
        a, b = clamp_chroma(self.L, a, b)
        self.a, self.b = float(a), float(b)
        self.update()

    def _drag_slider(self, p):
        self.shade_base = None
        self.L = min(max((p.x() - SLIDER.left()) / SLIDER.width(), 0.0), 1.0)
        self.update()

    # -------------------------------------------------------------- painting
    def _images(self):
        dpr = self.devicePixelRatioF()
        # half resolution while the lightness bar is being dragged: much
        # cheaper, and the full-quality field comes back on release
        fast = self.dragging == "slider"
        fk = (round(self.L, 3 if fast else 4), dpr, fast)
        if fk != self._field_key:
            n = int(FIELD.width() * dpr)
            img = render_field(self.L, n // 2 if fast else n)
            if fast:
                img = img.scaled(n, n, Qt.IgnoreAspectRatio, Qt.FastTransformation)
            img.setDevicePixelRatio(dpr)
            self._field_img, self._field_key = img, fk
        sk = (round(self.a, 4), round(self.b, 4), dpr)
        if sk != self._slider_key:
            img = render_slider(self.a, self.b, int(SLIDER.width() * dpr),
                                int(SLIDER.height() * dpr))
            img.setDevicePixelRatio(dpr)
            self._slider_img, self._slider_key = img, sk
        return self._field_img, self._slider_img

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        if self.flash:  # solid color the CSP screen picker samples
            p.fillRect(self.rect(), QColor(self.flash))
            return

        panel = QPainterPath()
        panel.addRoundedRect(QRectF(self.rect()).adjusted(1, 1, -1, -1), 14, 14)
        p.fillPath(panel, QColor(BG))
        p.setPen(QPen(QColor(BORDER), 1.5))
        p.drawPath(panel)

        font = QFont("Segoe UI", 9)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor(TEXT))
        cur = self.current_hex()
        toast = self.toast if (self.toast and
                                QDateTime.currentMSecsSinceEpoch() < self.toast[1]) else None
        if toast:
            p.setPen(QColor("#9ee6a8"))
            p.drawText(HEX_RECT, Qt.AlignVCenter | Qt.AlignLeft, toast[0])
        else:
            # hex (click to copy, right-click to paste) + lightness
            p.drawText(HEX_RECT, Qt.AlignVCenter | Qt.AlignLeft, cur.upper())
            font.setBold(False)
            p.setFont(font)
            p.setPen(QColor(DIM))
            p.drawText(HEX_RECT.adjusted(66, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft,
                       "L %d" % round(self.L * 100))
        font.setBold(False)
        p.setFont(font)
        # eyedropper icon
        e_ = QRectF(EYE_BTN)
        p.setPen(QPen(QColor(TEXT), 1.6, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(e_.left() + 5, e_.bottom() - 5), QPointF(e_.right() - 8, e_.top() + 8))
        p.setBrush(QColor(TEXT))
        p.drawEllipse(QPointF(e_.right() - 6.5, e_.top() + 6.5), 3.2, 3.2)
        p.setBrush(Qt.NoBrush)

        sw = QPainterPath()
        sw.addRoundedRect(QRectF(OLD_SW.united(NEW_SW)), 5, 5)
        p.save()
        p.setClipPath(sw)
        p.fillRect(OLD_SW, QColor(self.applied_hex))
        p.fillRect(NEW_SW, QColor(cur))
        p.restore()
        p.setPen(QPen(QColor("#00000080"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(sw)

        # on-top, pin, close
        for btn, active in ((TOP_BTN, self.on_top), (PIN_BTN, self.pinned)):
            if active:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor("#2e2a4a"))
                p.drawRoundedRect(QRectF(btn), 5, 5)
        t = QRectF(TOP_BTN)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(TEXT if self.on_top else DIM), 1.4))
        p.drawRoundedRect(QRectF(t.left() + 4, t.top() + 4, 9, 9), 2, 2)
        p.setBrush(QColor(BORDER) if self.on_top else QColor(BG))
        p.drawRoundedRect(QRectF(t.left() + 8, t.top() + 8, 9, 9), 2, 2)
        p.setPen(QPen(QColor(TEXT if self.pinned else DIM), 1.6))
        p.setBrush(QColor(BORDER) if self.pinned else Qt.NoBrush)
        p.drawEllipse(QRectF(PIN_BTN).adjusted(5, 5, -5, -5))
        p.setPen(QPen(QColor(DIM), 1.6))
        c = QRectF(CLOSE_BTN).adjusted(6, 6, -6, -6)
        p.drawLine(c.topLeft(), c.bottomRight())
        p.drawLine(c.topRight(), c.bottomLeft())

        field_img, slider_img = self._images()

        fpath = QPainterPath()
        fpath.addRoundedRect(QRectF(FIELD), 9, 9)
        p.save()
        p.setClipPath(fpath)
        p.drawImage(FIELD, field_img)
        p.restore()

        ea, eb = self.effective_ab()
        pt = self.ab_to_point(ea, eb)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor("#000000a0"), 3.2))
        p.drawEllipse(pt, 7, 7)
        p.setPen(QPen(QColor("#ffffff"), 1.6))
        p.drawEllipse(pt, 7, 7)

        spath = QPainterPath()
        spath.addRoundedRect(QRectF(SLIDER), 7, 7)
        p.save()
        p.setClipPath(spath)
        p.drawImage(SLIDER, slider_img)
        p.restore()
        hx = SLIDER.left() + self.L * SLIDER.width()
        handle = QRectF(hx - 6, SLIDER.top() - 3, 12, SLIDER.height() + 6)
        p.setPen(QPen(QColor("#000000a0"), 3.2))
        p.drawRoundedRect(handle, 4, 4)
        p.setPen(QPen(QColor("#ffffff"), 1.6))
        p.setBrush(QColor(cur))
        p.drawRoundedRect(handle, 4, 4)

        for i in range(HIST_N):
            r = QRectF(hist_rect(i))
            if i < len(self.history):
                p.setPen(QPen(QColor("#00000080"), 1))
                p.setBrush(QColor(self.history[i]))
            else:
                p.setPen(QPen(QColor("#34343e"), 1))
                p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), 5, 5)

        # ---- shading suggestions
        def caption(y, text):
            p.setPen(QPen(QColor("#2c2c36"), 1))
            p.drawLine(PAD, y - 5, W - PAD, y - 5)
            fb = QFont("Segoe UI", 8)
            fb.setBold(True)
            p.setFont(fb)
            p.setPen(QColor(TEXT))
            p.drawText(QRect(PAD + 2, y, 120, 14), Qt.AlignVCenter | Qt.AlignLeft, text)

        def swatch(rect, hx, marked):
            rr = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
            p.setPen(QPen(QColor("#00000080"), 1))
            p.setBrush(QColor(hx))
            p.drawRoundedRect(rr, 5, 5)
            if marked:
                p.setBrush(Qt.NoBrush)
                p.setPen(QPen(QColor("#ffffff"), 1.6))
                p.drawRoundedRect(rr.adjusted(-1.5, -1.5, 1.5, 1.5), 6, 6)

        small = QFont("Segoe UI", 8)
        caption(SHADE_LABEL_Y, "Shading")
        jump = bool(self.settings.get("jump_to_picked", True))
        jr = QRectF(JUMP_BTN)
        p.setPen(QPen(QColor(BORDER if jump else "#3a3a46"), 1))
        p.setBrush(QColor("#2e2a4a") if jump else Qt.NoBrush)
        p.drawRoundedRect(jr, 9, 9)
        p.setFont(small)
        p.setPen(QColor(TEXT if jump else DIM))
        p.drawText(jr, Qt.AlignCenter, "Jump " + ("on" if jump else "off"))

        shades = self.shades()
        mark = shades.index(self.applied_hex) if (
            self.shade_base and self.applied_hex in shades) else (
            SHADE_N // 2 if not self.shade_base else -1)
        for i, hx in enumerate(shades):
            swatch(shade_rect(i), hx, i == mark)

        # ---- harmony
        caption(HARM_LABEL_Y, "Harmony")
        p.setFont(small)
        p.setPen(QColor(DIM))
        p.drawText(QRectF(HARM_PREV), Qt.AlignCenter, "\u2039")
        p.drawText(QRectF(HARM_NEXT), Qt.AlignCenter, "\u203a")
        p.setPen(QColor(TEXT))
        p.drawText(QRectF(HARM_NAME), Qt.AlignCenter, HARMONIES[self.harmony_index()][0])

        hs = self.harmonies()
        # mini hue wheel with the scheme's points, like a color-wheel diagram
        wr = QRectF(HARM_WHEEL).adjusted(1, 1, -1, -1)
        cx, cy, rad = wr.center().x(), wr.center().y(), wr.width() / 2
        grad = QConicalGradient(cx, cy, 0)
        for k in range(13):
            grad.setColorAt(k / 12, QColor(_lch_to_hex(0.75, 0.13, k * 30.0)))
        p.setPen(Qt.NoPen)
        p.setBrush(grad)
        p.drawEllipse(wr)
        p.setBrush(QColor(BG))
        p.drawEllipse(wr.adjusted(6, 6, -6, -6))
        pts = []
        for hx, hdeg, is_base in hs:
            r_ = np.radians(hdeg)
            pts.append((QPointF(cx + (rad - 3) * np.cos(r_), cy - (rad - 3) * np.sin(r_)),
                        hx, is_base))
        if len(pts) > 1:
            poly = QPainterPath()
            order = sorted(pts, key=lambda t: -np.arctan2(t[0].y() - cy, t[0].x() - cx))
            poly.moveTo(order[0][0])
            for pt_, _, _ in order[1:]:
                poly.lineTo(pt_)
            if len(order) > 2:
                poly.closeSubpath()
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor("#d8d8e4"), 1.2))
            p.drawPath(poly)
        for pt_, hx, is_base in pts:
            p.setPen(QPen(QColor("#ffffff" if is_base else "#000000"), 1.4))
            p.setBrush(QColor(hx))
            p.drawEllipse(pt_, 4.5 if is_base else 3.5, 4.5 if is_base else 3.5)

        for i, (hx, _hdeg, is_base) in enumerate(hs):
            swatch(harm_rect(i, len(hs)), hx, is_base)

        # ---- surroundings: floor shadow + bounce light from the surface
        caption(SURR_LABEL_Y, "Surroundings")
        p.setFont(small)
        swatch(SURF_SW, self.surface(), False)
        for rect, label in ((SURF_PICK, "Pick"), (SURF_CUR, "Current")):
            p.setPen(QPen(QColor("#3a3a46"), 1))
            p.setBrush(Qt.NoBrush)
            p.drawRoundedRect(QRectF(rect), 9, 9)
            p.setPen(QColor(TEXT))
            p.drawText(QRectF(rect), Qt.AlignCenter, label)
        floor, bounce = self.surroundings()
        for r_i, (label, row) in enumerate((("Floor", floor), ("Bounce", bounce))):
            p.setPen(QColor(DIM))
            p.drawText(QRect(PAD + 2, SURR_Y[r_i], SURR_CAP_W - 4, SURR_ROW_H),
                       Qt.AlignVCenter | Qt.AlignLeft, label)
            for i, hx in enumerate(row):
                swatch(surr_rect(r_i, i), hx, False)


# --------------------------------------------------------------------- app
def make_icon():
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    g = QConicalGradient(32, 32, 0)
    for i, c in enumerate(["#ff5577", "#ffcc44", "#55dd88", "#44aaff", "#aa66ff", "#ff5577"]):
        g.setColorAt(i / 5, QColor(c))
    p.setBrush(g)
    p.setPen(Qt.NoPen)
    p.drawEllipse(4, 4, 56, 56)
    p.setBrush(QColor("#8a8a8a"))
    p.drawEllipse(20, 20, 24, 24)
    p.end()
    return QIcon(pm)


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    # the packaged app has no console: write hidden errors to error_log.txt
    import traceback

    def _hook(et, ev, tb):
        try:
            with open(os.path.join(APP_DIR, "error_log.txt"), "a", encoding="utf-8") as f:
                f.write("".join(traceback.format_exception(et, ev, tb)) + "\n")
        except Exception:
            pass
    sys.excepthook = _hook

    # One copy at a time. Starting it again while it runs just shows the
    # palette; "--replace" closes the running copy and takes over instead.
    from PySide6.QtNetwork import QLocalServer, QLocalSocket
    replace = "--replace" in sys.argv
    sock = QLocalSocket()
    sock.connectToServer("LumaPickerInstance")
    if sock.waitForConnected(300):
        sock.write(b"quit" if replace else b"show")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        if not replace:
            return
        import time
        time.sleep(0.8)
    QLocalServer.removeServer("LumaPickerInstance")
    server = QLocalServer()
    server.listen("LumaPickerInstance")
    holder = {}

    def _on_msg(c):
        data = bytes(c.readAll())
        if b"quit" in data:
            app.quit()
        elif b"show" in data and "picker" in holder:
            holder["picker"].show_centered()

    def _on_conn():
        c = server.nextPendingConnection()
        c.readyRead.connect(lambda: _on_msg(c))
    server.newConnection.connect(_on_conn)
    app._server = server

    settings = load_settings()

    tray = QSystemTrayIcon(make_icon())
    tray.setToolTip("Luma Picker %s" % __version__)

    def notify(msg):
        tray.showMessage("Luma Picker", msg, QSystemTrayIcon.Information, 5000)

    picker = LumaPicker(settings, notify)

    # pick up edits to settings.json while running (no restart needed)
    from PySide6.QtCore import QFileSystemWatcher
    watcher = QFileSystemWatcher([SETTINGS_PATH])

    def _reload(_path):
        try:
            with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
                fresh = json.load(f)
            for k, v in fresh.items():
                if k not in ("history", "last_color", "window_pos"):
                    settings[k] = v
            picker.update()
        except Exception:
            pass
        if SETTINGS_PATH not in watcher.files():   # editors replace the file
            watcher.addPath(SETTINGS_PATH)
    watcher.fileChanged.connect(_reload)
    app._watcher = watcher
    holder["picker"] = picker
    app.setWindowIcon(make_icon())
    QTimer.singleShot(300, picker.show_centered)  # show it right away on start

    menu = QMenu()
    menu.addAction("Show palette", picker.show_centered)
    menu.addAction("Open settings file", lambda: os.startfile(SETTINGS_PATH)
                   if IS_WIN else None)
    menu.addSeparator()
    menu.addAction("Quit", app.quit)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda r: picker.show_centered()
                           if r == QSystemTrayIcon.Trigger else None)
    tray.show()

    if IS_WIN:
        hk = HotkeyWindow()
        hk.triggered.connect(lambda i: picker.toggle() if i == 1
                             else picker.start_eyedropper("main"))
        try:
            eh = settings.get("eyedropper_hotkey", "")
            if eh and not hk.register(eh, 2):
                notify("Could not register eyedropper key %s." % eh)
        except ValueError as exc:
            notify("eyedropper_hotkey is not valid: %s" % exc)
        try:
            if hk.register(settings["hotkey"]):
                notify("Running. Press %s to open the palette." % settings["hotkey"])
            else:
                notify("Could not register %s (another app uses it). "
                       "Change \"hotkey\" in settings.json." % settings["hotkey"])
        except ValueError as exc:
            notify("Hotkey setting is not valid: %s" % exc)
        app._hk = hk  # keep alive
    else:
        notify("Global hotkey is Windows-only. Click the tray icon to open.")

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
