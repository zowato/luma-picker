"""
Luma Picker - a floating, lightness-locked color palette for digital painting.

The big field shows every sRGB color that has the SAME perceptual lightness
(OKLab L); the bar below changes that lightness. Out-of-gamut area is drawn as
the neutral grey of the same lightness, so the whole field is one value.

Extras: shading ramps (cool shadows / warm lights), color harmonies, floor
shadow + bounce light suggestions, a screen eyedropper and hex copy/paste.

Windows: a global hotkey opens the panel. Picking a color sets it in
Clip Studio Paint directly, over CSP's companion connection (csp_link.py).

https://github.com/zowato/luma-picker  -  MIT License
"""
__version__ = "1.0.0"

import colorsys
import ctypes
import json
import os
import sys

import numpy as np
from csp_link import CspLink, decode_qr, find_qr_url
from PySide6.QtCore import QDateTime, QObject, QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QConicalGradient, QCursor, QFont,
                           QGuiApplication, QIcon, QImage, QPainter,
                           QPainterPath, QPen, QPixmap)
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon, QToolTip, QWidget

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
    "greyscale_hotkey": "ctrl+alt+shift+g",    # whole screen in greyscale on/off
    "grey_panel": False,                       # show the panel itself in greyscale
    "hue_shift_strength": 1.0,     # 0.5 = subtle, 2 = strong hue shifting
    "value_mode": "perceptual",    # or "luminance" (Clip Studio's grey check)
    "luma_weights": [0.30, 0.59, 0.11],   # R, G, B weights of "luminance"
    "field_shape": "square",       # "round": hue wheel, "focus": a/b plane slice
    "focus_zoom": True,            # focus view: zoom to the slice, or show the full plane
    "field_cells": 24,             # color blocks per side of the square field
    "mask": 0,                     # index into MASKS (0 = off)
    "mask_rotate": 0,              # degrees
    "mask_width": 60,              # degrees per allowed hue sector
    "wheel_step": 0.01,            # mouse wheel over field/bar: value per notch
    "wheel_send_ms": 350,          # send to CSP this long after the last notch
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
SHADE_N = 7  # swatches per row, base color in the middle


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


def harmony_colors(L, a, b, offsets, luma=None):
    """Partner colors at the same lightness, rotated around the hue wheel.
    luma: keep this luminance instead (the "luminance" value mode).
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
        Ck = 0.08 if neutral else C
        if luma is None:
            out.append((_lch_to_hex(L, Ck, hk), hk, False))
        else:
            r = np.radians(hk)
            Lk, ak, bk = luma_color(luma, Ck * np.cos(r), Ck * np.sin(r))
            out.append((oklab_to_hex(float(Lk), float(ak), float(bk)), hk, False))
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


def _to_qimage(rgb, opaque=None):  # rgb: H x W x 3 floats in sRGB 0..1
    h, w, _ = rgb.shape
    arr = np.empty((h, w, 4), dtype=np.uint8)
    arr[..., :3] = np.clip(np.round(rgb * 255), 0, 255).astype(np.uint8)
    arr[..., 3] = 255 if opaque is None else np.where(opaque, 255, 0)
    return QImage(arr.data, w, h, w * 4, QImage.Format_RGBA8888).copy()


def render_slider(a, b, w, h):
    Ls = (np.arange(w) + 0.5) / w
    aa, bb = clamp_chroma(Ls, np.full(w, a), np.full(w, b))
    rgb = linear_to_srgb(np.stack(oklab_to_linear(Ls, aa, bb), axis=-1))
    return _to_qimage(np.repeat(rgb[None, :, :], h, axis=0))


# ------------------------------------------------- "luminance" value mode
# Value the way Clip Studio shows it when you check values with a grey layer
# in Color blend mode: a weighted sum of the gamma-encoded sRGB channels
# (the usual Color-blend formula). Set from settings["luma_weights"].
LUMA_W = (0.30, 0.59, 0.11)


def apply_luma_weights(settings):
    global LUMA_W
    try:
        w = tuple(float(x) for x in settings.get("luma_weights", LUMA_W))
        if len(w) == 3 and sum(w) > 0:
            LUMA_W = tuple(x / sum(w) for x in w)
    except (TypeError, ValueError):
        pass


def luma_of_srgb(r, g, b):
    return LUMA_W[0] * r + LUMA_W[1] * g + LUMA_W[2] * b


def luma_of_hex(hx):
    return float(luma_of_srgb(*_hex_rgb(hx)))


def L_for_luma(Y, a, b, iters=18):
    """OKLab L that gives hue/chroma (a, b) the luminance Y (vectorized)."""
    a, b = np.asarray(a, dtype=float), np.asarray(b, dtype=float)
    lo = np.zeros(np.broadcast(Y, a, b).shape)
    hi = np.ones_like(lo)
    for _ in range(iters):
        mid = (lo + hi) / 2
        r, g, bl = linear_to_srgb(np.stack(oklab_to_linear(mid, a, b)))
        low = luma_of_srgb(r, g, bl) < Y
        lo = np.where(low, mid, lo)
        hi = np.where(low, hi, mid)
    return (lo + hi) / 2


def luma_color(Y, a, b):
    """(L, a, b) with luminance Y, chroma pulled in until it fits sRGB."""
    a, b = (np.asarray(x, dtype=float) for x in np.broadcast_arrays(a, b))
    ok = in_gamut(*oklab_to_linear(L_for_luma(Y, a, b), a, b))
    # largest chroma (same hue) whose color of luminance Y still fits sRGB
    lo, hi = np.zeros(np.broadcast(Y, a).shape), np.ones(np.broadcast(Y, a).shape)
    for _ in range(16):
        mid = (lo + hi) / 2
        m = in_gamut(*oklab_to_linear(L_for_luma(Y, a * mid, b * mid, 14),
                                      a * mid, b * mid))
        lo, hi = np.where(m, mid, lo), np.where(m, hi, mid)
    t = np.where(ok, 1.0, lo)
    a, b = a * t, b * t
    return L_for_luma(Y, a, b), a, b




_cmax_cache = {}


def max_chroma(value, luma=False):
    """Largest in-gamut OKLab chroma for each whole hue degree (0..359)
    at this lightness (or luminance, if luma)."""
    key = (round(float(value), 3), bool(luma), LUMA_W if luma else None)
    if key not in _cmax_cache:
        h = np.radians(np.arange(360.0))
        c, s = np.cos(h), np.sin(h)
        if luma:
            lo, hi = np.zeros(360), np.full(360, 0.5)
            for _ in range(16):
                mid = (lo + hi) / 2
                ok = in_gamut(*oklab_to_linear(L_for_luma(key[0], c * mid, s * mid, 14),
                                               c * mid, s * mid))
                lo, hi = np.where(ok, mid, lo), np.where(ok, hi, mid)
            _cmax_cache[key] = lo
        else:
            a, b = clamp_chroma(np.full(360, key[0]), c * 0.5, s * 0.5)
            _cmax_cache[key] = np.hypot(a, b)
    return _cmax_cache[key]


def cmax_at(table, h_deg):
    """Max chroma at any hue (degrees), interpolated from max_chroma()."""
    return np.interp(np.asarray(h_deg) % 360.0, np.arange(361.0), np.append(table, table[0]))


# --------------------------------------------------------------- gamut masks
# Hue sectors you may pick from (centers in degrees, before rotation).
# Masked hues stay visible but dimmed, and picking snaps to the nearest
# allowed hue. Near-greys are always allowed.
MASKS = [
    ("Mask off", ()),
    ("Analogous", (0,)),
    ("Complement", (0, 180)),
    ("Split compl.", (0, 150, 210)),
    ("Triad", (0, 120, 240)),
]
MASK_GREY_C = 0.015   # chroma below this counts as grey (never masked)


def _hue_dist(h, c):
    return np.abs((np.asarray(h) - c + 540.0) % 360.0 - 180.0)


def mask_allows(h_deg, centers, rotate, width):
    if not centers:
        return np.ones(np.shape(h_deg), dtype=bool)
    return np.min([_hue_dist(h_deg, c + rotate) for c in centers], axis=0) <= width / 2


def mask_snap(h_deg, centers, rotate, width):
    """The allowed hue nearest to h_deg (h_deg itself if it is allowed)."""
    if not centers or mask_allows(h_deg, centers, rotate, width):
        return h_deg % 360.0
    best = None
    for c in centers:
        for edge in (c + rotate - width / 2, c + rotate + width / 2):
            d = float(_hue_dist(h_deg, edge))
            if best is None or d < best[0]:
                best = (d, edge % 360.0)
    return best[1]


def mask_rotation_for(h_deg, centers, rotate):
    """Mask rotation that puts the nearest sector's middle on hue h_deg."""
    d = min(((h_deg - c - rotate + 540.0) % 360.0 - 180.0 for c in centers), key=abs)
    return (rotate + d) % 360.0


# Square field: hue runs left -> right from this OKLCH hue (red), the share
# of each hue's max chroma runs top (pale) -> bottom (strongest).
SQUARE_HUE0 = 30.0


def square_hue_share(fx, fy):
    """Field fractions (0..1, top-left origin) -> (hue degrees, chroma share)."""
    return (SQUARE_HUE0 + np.asarray(fx) * 360.0) % 360.0, np.asarray(fy)


FULL_HALF = 0.34      # "full" plane view: a, b from -0.34 to 0.34 (all of sRGB)


def plane_view(value, luma, shape):
    """Center and half-size of the a/b plane view. "focus" zooms so the
    slice of this value fills the field, "full" shows the whole plane."""
    if shape != "focus":
        return 0.0, 0.0, FULL_HALF
    h = np.arange(0, 360, 2.5)
    c = cmax_at(max_chroma(value, luma), h)
    a, b = c * np.cos(np.radians(h)), c * np.sin(np.radians(h))
    half = max(a.max() - a.min(), b.max() - b.min()) / 2 * 1.14
    return float(a.max() + a.min()) / 2, float(b.max() + b.min()) / 2, float(max(half, 0.02))


def field_ab(n, value, luma, shape, cells=24):
    """OKLab a, b of every pixel of an n x n field, plus the drawable area.
    The square field is made of cells x cells flat color blocks; "focus" and
    "full" are the plain a/b plane (out-of-gamut parts drawn grey)."""
    t = (np.arange(n) + 0.5) / n
    if shape in ("focus", "full"):
        cx, cy, half = plane_view(value, luma, shape)
        A, B = np.meshgrid(cx + (t * 2 - 1) * half, cy - (t * 2 - 1) * half)
        return A, B, np.ones_like(A, dtype=bool)
    if shape == "round":
        # angle = hue, radius = share of the most chroma this hue can have
        x, y = np.meshgrid(t * 2 - 1, 1 - t * 2)
        r, h = np.hypot(x, y), np.degrees(np.arctan2(y, x)) % 360.0
        C = np.minimum(r, 1.0) * cmax_at(max_chroma(value, luma), h)
    else:
        q = (np.floor(t * cells) + 0.5) / cells           # cell centers
        h, s = square_hue_share(*np.meshgrid(q, q))
        C, r = s * cmax_at(max_chroma(value, luma), h), np.zeros_like(s)
    hr = np.radians(h)
    return C * np.cos(hr), C * np.sin(hr), r <= 1.0


def render_view(value, n, luma=False, shape="square", mask=None, cells=24):
    """The big field. value = OKLab L, or luminance if luma.
    mask = (centers, rotate, width) or None."""
    A, B, inside = field_ab(n, value, luma, shape, cells)
    if luma and shape == "square":
        # flat cells: solve L once per cell and spread it over the cell
        Lc = L_for_luma(value, *field_ab(cells, value, luma, shape, cells)[:2])
        idx = np.minimum((np.arange(n) + 0.5) / n * cells, cells - 1).astype(int)
        L = Lc[idx][:, idx]
        grey = np.full(3, min(max(value, 0.0), 1.0))     # same luminance
    elif luma:
        # solving L per pixel is slow; L changes smoothly across the field,
        # so solve it on a coarse grid and interpolate (picks stay exact)
        m = max(n // 4, 24)
        Lc = L_for_luma(value, *field_ab(m, value, luma, shape)[:2])
        pos = np.clip((np.arange(n) + 0.5) / n * m - 0.5, 0, m - 1)
        i0 = np.minimum(pos.astype(int), m - 2)
        f = pos - i0
        Lr = Lc[i0] * (1 - f)[:, None] + Lc[i0 + 1] * f[:, None]          # rows
        L = Lr[:, i0] * (1 - f)[None, :] + Lr[:, i0 + 1] * f[None, :]     # cols
        grey = np.full(3, min(max(value, 0.0), 1.0))     # same luminance
    else:
        L = value
        grey = linear_to_srgb(np.array(oklab_to_linear(value, 0.0, 0.0)))
    r, g, b = oklab_to_linear(L, A, B)
    ok = in_gamut(r, g, b)
    rgb = linear_to_srgb(np.stack([r, g, b], axis=-1))
    rgb[~ok] = grey
    if mask and mask[0]:
        hue = np.degrees(np.arctan2(B, A)) % 360.0
        off = ok & ~mask_allows(hue, *mask)
        dull = grey + (rgb[off] - grey) * 0.22            # visible, but dull
        # and pushed away from the picked value so the allowed hues stand out:
        # darker when the value is light, lighter when it is dark
        rgb[off] = dull * 0.62 if value >= 0.5 else dull + (1 - dull) * 0.38
    return _to_qimage(rgb, inside)


def to_grey(rgb, luma):
    """sRGB 0..1 (... x 3) -> grey of the same value: luminance, or OKLab L."""
    if luma:
        return luma_of_srgb(rgb[..., 0], rgb[..., 1], rgb[..., 2])
    L = linear_to_oklab(*np.moveaxis(srgb_to_linear(rgb), -1, 0))[0]
    return linear_to_srgb(np.clip(L, 0, 1) ** 3)       # OKLab grey: lin = L^3


def grey_matrix(weights):
    """5x5 color matrix (row = input channel) that turns the screen grey."""
    m = np.eye(5, dtype=np.float32)
    for i, w in enumerate(weights):
        m[i, :3] = w
    return m


_mag = {}


def set_screen_grey(on, weights=None):
    """Whole desktop in greyscale (Windows Magnification API full-screen
    color effect), or back to normal. False if unsupported. Screen captures
    see the grey too, so the eyedropper lifts it while it grabs."""
    if not IS_WIN:
        return False
    try:
        if "dll" not in _mag:
            dll = ctypes.WinDLL("Magnification")
            dll.MagInitialize.restype = ctypes.c_bool
            dll.MagSetFullscreenColorEffect.restype = ctypes.c_bool
            dll.MagSetFullscreenColorEffect.argtypes = (ctypes.c_void_p,)
            if not dll.MagInitialize():
                return False
            _mag["dll"] = dll
        m = grey_matrix(weights or LUMA_W) if on else np.eye(5, dtype=np.float32)
        buf = (ctypes.c_float * 25)(*m.flatten().tolist())
        return bool(_mag["dll"].MagSetFullscreenColorEffect(ctypes.byref(buf)))
    except (OSError, AttributeError):
        return False


def render_slider_luma(a, b, w, h):
    Ys = (np.arange(w) + 0.5) / w
    L, aa, bb = luma_color(Ys, np.full(w, a), np.full(w, b))
    rgb = linear_to_srgb(np.stack(oklab_to_linear(L, aa, bb), axis=-1))
    return _to_qimage(np.repeat(rgb[None, :, :], h, axis=0))


# ------------------------------------------------------------ windows glue
MOD_FLAG = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "win": 0x8}
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
    user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
    user32.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)

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
LINK_RECT = QRect(PAD, 44, W - 2 * PAD, 32)     # Clip Studio connection status
LINK_BTN = QRect(W - PAD - 64, 45, 64, 16)
FIELD = QRect(PAD, LINK_RECT.bottom() + 5, W - 2 * PAD, W - 2 * PAD)
ZOOM_BTN = QRect(FIELD.right() - 50, FIELD.top() + 6, 44, 18)   # Focus/Full (focus view)
SLIDER = QRect(PAD, FIELD.bottom() + 11, W - 2 * PAD, 24)
VIEW_Y = SLIDER.bottom() + 10                     # Square|Round  ·  mask
SHAPE_SQ = QRect(PAD, VIEW_Y, 37, 18)
SHAPE_RD = QRect(SHAPE_SQ.right() + 1, VIEW_Y, 37, 18)
SHAPE_FO = QRect(SHAPE_RD.right() + 1, VIEW_Y, 37, 18)
GREY_BTN = QRect(SHAPE_FO.right() + 5, VIEW_Y, 18, 18)      # panel in greyscale
SCREEN_BTN = QRect(GREY_BTN.right() + 3, VIEW_Y, 18, 18)    # screen in greyscale
MASK_PREV = QRect(SCREEN_BTN.right() + 4, VIEW_Y, 18, 18)
MASK_NEXT = QRect(W - PAD - 18, VIEW_Y, 18, 18)
MASK_NAME = QRect(MASK_PREV.right() + 1, VIEW_Y,
                  MASK_NEXT.left() - MASK_PREV.right() - 2, 18)
MASK_SL_Y = VIEW_Y + 26                           # Rotate / Width sliders
ROT_SL = QRect(PAD + 42, MASK_SL_Y, 82, 14)
WID_SL = QRect(W - PAD - 82, MASK_SL_Y, 82, 14)
HIST_Y = MASK_SL_Y + 14 + 12
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
NEW_SW = QRect(TOP_BTN.left() - 8 - 22, 13, 22, 18)
OLD_SW = QRect(NEW_SW.left() - 22, 13, 22, 18)
EYE_BTN = QRect(OLD_SW.left() - 24, 12, 20, 20)            # eyedropper
TOAST_RECT = QRect(PAD + 2, 12, EYE_BTN.left() - PAD - 6, 20)
HEX_RECT = QRect(PAD + 2, 12, 58, 20)                       # click = copy
COPY_BTN = QRect(HEX_RECT.right() + 1, 12, 18, 20)
MODE_BTN = QRect(COPY_BTN.right() + 3, 13, EYE_BTN.left() - COPY_BTN.right() - 7, 18)

# warm orange accent; ACTIVE = background of a switched-on button
BG, BORDER, TEXT, DIM = "#1c1c22", "#e8963a", "#d8d8e4", "#7d7d8c"
ACTIVE = "#3d2e1c"

if getattr(sys, "frozen", False):
    ICON_DIR = os.path.join(getattr(sys, "_MEIPASS", ""), "assets", "icons")
else:
    ICON_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "assets", "icons")
_icon_cache = {}


def icon(name, color, size, dpr=1.0):
    """assets/icons/<name>.png (black on transparent) tinted in color."""
    key = (name, color, size, dpr)
    if key not in _icon_cache:
        src = QPixmap(os.path.join(ICON_DIR, name + ".png"))
        pm = None
        if not src.isNull():
            px = int(round(size * dpr))
            pm = QPixmap(px, px)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawPixmap(pm.rect(), src)
            p.setCompositionMode(QPainter.CompositionMode_SourceIn)
            p.fillRect(pm.rect(), QColor(color))
            p.end()
            pm.setDevicePixelRatio(dpr)
        _icon_cache[key] = pm
    return _icon_cache[key]


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
        apply_luma_weights(settings)
        self.L, self.a, self.b = hex_to_oklab(settings.get("last_color", "#c0607a"))
        self.Y = luma_of_hex(oklab_to_hex(self.L, self.a, self.b))
        self.applied_hex = self.current_hex()
        self.wheel_timer = QTimer(self, singleShot=True,
                                  timeout=lambda: self.apply_hex(self.current_hex()))
        self.history = [h for h in settings.get("history", []) if isinstance(h, str)][:HIST_N]
        self.dragging = None
        self.pinned = bool(settings.get("pinned", False))
        self.move_offset = None
        self._field_key = self._slider_key = None
        self._shade_key = None
        self._shade_rows = []
        self.shade_base = None      # set while "Jump" is off and you pick shades
        self.pending = None         # swatch pressed, sent when the pen lifts
        self.sampler = ScreenSampler()
        self.sampler.picked.connect(self._on_sampled)
        # direct link to Clip Studio's companion connection (no screen reading)
        self.link = CspLink(settings.setdefault("csp_link", {}),
                            lambda: save_settings(self.settings))
        self.link.state.connect(self._on_link)
        self.pair_timer = QTimer(self, interval=700, timeout=self._look_for_qr)
        self.link.connect_saved()
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
        self.Y = luma_of_hex(hx)
        m = self.mask()
        if m and np.hypot(self.a, self.b) >= MASK_GREY_C:
            h = float(np.degrees(np.arctan2(self.b, self.a))) % 360.0
            if not mask_allows(h, *m):      # turn the mask onto the new color
                self.settings["mask_rotate"] = round(mask_rotation_for(h, m[0], m[1])) % 360
                save_settings(self.settings)
        self.update()

    def cells(self):
        try:
            return min(max(int(self.settings.get("field_cells", 24)), 4), 96)
        except (TypeError, ValueError):
            return 24

    def luma_mode(self):
        return self.settings.get("value_mode") == "luminance"

    def value(self):
        """What the bar controls: OKLab L, or luminance in "luminance" mode."""
        return self.Y if self.luma_mode() else self.L

    def set_value(self, v):
        self.shade_base = None
        v = min(max(v, 0.0), 1.0)
        if self.luma_mode():
            self.Y = v
            self.L = float(luma_color(v, self.a, self.b)[0])
        else:
            self.L = v
            self.Y = luma_of_hex(self.current_hex())
        self.update()

    def toggle_value_mode(self):
        on = not self.luma_mode()
        self.settings["value_mode"] = "luminance" if on else "perceptual"
        save_settings(self.settings)
        self.Y = luma_of_hex(self.current_hex())   # same color, new ruler
        self.show_toast("Value: CSP luminance" if on else "Value: perceptual")

    def round_field(self):
        return self.settings.get("field_shape") == "round"

    def view_shape(self):
        """square, round, or the a/b plane: focus (zoomed) / full."""
        sh = self.settings.get("field_shape", "square")
        if sh == "focus":
            return "focus" if self.settings.get("focus_zoom", True) else "full"
        return "round" if sh == "round" else "square"

    def toggle_zoom(self):
        self.settings["focus_zoom"] = not self.settings.get("focus_zoom", True)
        save_settings(self.settings)
        self.update()

    def toggle_grey_panel(self):
        on = not self.settings.get("grey_panel", False)
        self.settings["grey_panel"] = on
        save_settings(self.settings)
        self.show_toast("Panel in greyscale" if on else "Panel in color")

    def toggle_screen_grey(self):
        on = not getattr(self, "screen_grey", False)
        if not set_screen_grey(on):
            self.screen_grey = False
            self.show_toast("Screen greyscale not available")
            return
        self.screen_grey = on
        self.show_toast("Screen in greyscale" if on else "Screen in color")

    def set_shape(self, shape):
        self.settings["field_shape"] = shape
        save_settings(self.settings)
        self.update()

    def mask_index(self):
        try:
            return int(self.settings.get("mask", 0)) % len(MASKS)
        except (TypeError, ValueError):
            return 0

    def mask(self):
        """(centers, rotate, width) of the gamut mask, or None when off."""
        centers = MASKS[self.mask_index()][1]
        if not centers:
            return None
        try:
            rot = float(self.settings.get("mask_rotate", 0)) % 360.0
            wid = min(max(float(self.settings.get("mask_width", 60)), 10.0), 180.0)
        except (TypeError, ValueError):
            rot, wid = 0.0, 60.0
        return centers, rot, wid

    def cycle_mask(self, step):
        self.settings["mask"] = (self.mask_index() + step) % len(MASKS)
        save_settings(self.settings)
        self.update()

    def _drag_mask(self, p):
        if self.dragging == "rot":
            f = (p.x() - ROT_SL.left()) / ROT_SL.width()
            v = self.settings["mask_rotate"] = round(min(max(f, 0.0), 1.0) * 360) % 360
            self.show_toast("Rotate %d°" % v, 700)
        else:
            f = (p.x() - WID_SL.left()) / WID_SL.width()
            v = self.settings["mask_width"] = round(10 + min(max(f, 0.0), 1.0) * 170)
            self.show_toast("Width %d°" % v, 700)

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
        luma = luma_of_hex(oklab_to_hex(L, a, b)) if self.luma_mode() else None
        return harmony_colors(L, a, b, HARMONIES[self.harmony_index()][1], luma)

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

    def start_pairing(self):
        """Connect button: wait for Clip Studio's pairing QR code on screen
        (File > Connect to smartphone), then connect. Click again to cancel."""
        if self.pair_timer.isActive():
            self.pair_timer.stop()
        else:
            self.pair_left = 90                   # about one minute
            self.pair_timer.start()
        self.update()

    def _look_for_qr(self):
        self.pair_left -= 1
        if self.pair_left <= 0:
            self.pair_timer.stop()
            self.show_toast("No QR code found", 2500)
            return
        for scr in QGuiApplication.screens():
            qr = decode_qr(find_qr_url(scr.grabWindow(0).toImage()) or "")
            if qr:
                self.pair_timer.stop()
                self.link.pair(qr)
                return

    def _on_link(self, state):
        if state == "paired":
            self.show_toast("Connected! Close the QR window", 3000)
        elif state.startswith("failed:"):
            self.show_toast("Clip Studio refused (%s)" % state[7:], 4000)
        self.update()

    def _paint_link(self, p):
        """Status row under the header: connected, or a warning with how to fix it."""
        r = LINK_RECT
        busy = self.pair_timer.isActive() or bool(self.link.pairing)
        if self.link.ready:
            head, sub, col, btn = "Connected to Clip Studio", "Colors go straight in", "#9ee6a8", None
        elif busy:
            head, sub, col, btn = ("Looking for the QR code...",
                                   "Clip Studio: File > Connect to smartphone", BORDER, "Cancel")
        elif self.link.paired():
            head, sub, col, btn = ("Not connected to Clip Studio",
                                   "Start Clip Studio, or click Connect to pair again",
                                   "#ff8a5c", "Connect")
        else:
            head, sub, col, btn = ("Not connected to Clip Studio",
                                   "Click Connect, then File > Connect to smartphone in CSP",
                                   "#ff8a5c", "Connect")
        f = QFont("Segoe UI", 8)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(col))
        p.setBrush(QColor(col))
        p.drawEllipse(QRectF(r.left() + 1, r.top() + 6, 6, 6))
        p.drawText(QRect(r.left() + 11, r.top(), r.width() - 80, 18),
                   Qt.AlignVCenter | Qt.AlignLeft, head)
        f.setBold(False)
        f.setPointSizeF(7.5)
        p.setFont(f)
        p.setPen(QColor(DIM))
        p.drawText(QRect(r.left() + 11, r.top() + 16, r.width() - 11, 16),
                   Qt.AlignVCenter | Qt.AlignLeft, sub)
        if btn:
            p.setPen(QPen(QColor(col), 1))
            p.setBrush(QColor(ACTIVE))
            p.drawRoundedRect(QRectF(LINK_BTN), 8, 8)
            p.setPen(QColor(TEXT))
            p.drawText(LINK_BTN, Qt.AlignCenter, btn)

    def start_eyedropper(self, target="main"):
        self.sample_target = target
        if getattr(self, "screen_grey", False):
            # the grab would be grey too: lift the effect for the snapshot only
            set_screen_grey(False)
            QTimer.singleShot(60, lambda: (self.sampler.start(), set_screen_grey(True)))
        else:
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
        shape = self.view_shape()
        if shape in ("focus", "full"):
            cx, cy, half = plane_view(self.value(), self.luma_mode(), shape)
            return QPointF(FIELD.left() + (a - cx + half) / (2 * half) * FIELD.width(),
                           FIELD.top() + (cy + half - b) / (2 * half) * FIELD.height())
        if self.round_field():
            h = float(np.degrees(np.arctan2(b, a))) % 360.0
            cm = float(cmax_at(max_chroma(self.value(), self.luma_mode()), h))
            r = min(float(np.hypot(a, b)) / max(cm, 1e-6), 1.0) * FIELD.width() / 2
            hr = np.radians(h)
            return QPointF(FIELD.center().x() + 0.5 + r * np.cos(hr),
                           FIELD.center().y() + 0.5 - r * np.sin(hr))
        h = float(np.degrees(np.arctan2(b, a))) % 360.0
        cm = float(cmax_at(max_chroma(self.value(), self.luma_mode()), h))
        fx = ((h - SQUARE_HUE0) % 360.0) / 360.0
        fy = min(float(np.hypot(a, b)) / max(cm, 1e-6), 1.0)
        return QPointF(FIELD.left() + fx * FIELD.width(), FIELD.top() + fy * FIELD.height())

    def point_to_ab(self, p):
        shape = self.view_shape()
        if shape in ("focus", "full"):
            cx, cy, half = plane_view(self.value(), self.luma_mode(), shape)
            fx = min(max((p.x() - FIELD.left()) / FIELD.width(), 0.0), 1.0)
            fy = min(max((p.y() - FIELD.top()) / FIELD.height(), 0.0), 1.0)
            return cx - half + fx * 2 * half, cy + half - fy * 2 * half
        if self.round_field():
            rad = FIELD.width() / 2
            x = (p.x() - FIELD.center().x() - 0.5) / rad
            y = (FIELD.center().y() + 0.5 - p.y()) / rad
            h = float(np.degrees(np.arctan2(y, x))) % 360.0
            C = min(float(np.hypot(x, y)), 1.0) * float(
                cmax_at(max_chroma(self.value(), self.luma_mode()), h))
            return C * np.cos(np.radians(h)), C * np.sin(np.radians(h))
        # the color of the cell under the pen, exactly as drawn
        k = self.cells()
        fx = min(max((p.x() - FIELD.left()) / FIELD.width(), 0.0), 0.9999)
        fy = min(max((p.y() - FIELD.top()) / FIELD.height(), 0.0), 0.9999)
        h, s = square_hue_share((np.floor(fx * k) + 0.5) / k, (np.floor(fy * k) + 0.5) / k)
        C = float(s) * float(cmax_at(max_chroma(self.value(), self.luma_mode()), h))
        return C * np.cos(np.radians(float(h))), C * np.sin(np.radians(float(h)))

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
        if (not self.isVisible() or self.pinned or self.dragging
                or self.move_offset is not None
                or not self.settings.get("auto_hide", True)):
            return
        d = int(self.settings.get("auto_hide_distance", 60))
        if not self.frameGeometry().adjusted(-d, -d, d, d).contains(QCursor.pos()):
            self.hide_popup()

    # ---------------------------------------------------------------- applying
    def apply_hex(self, hx):
        self.wheel_timer.stop()
        if not self.link.set_color(hx):
            self.show_toast("Not connected to Clip Studio", 2500)
        self._finish_apply(hx)

    def _finish_apply(self, hx):
        self.applied_hex = hx
        if self.settings.get("also_copy_hex"):
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
        if e.button() == Qt.RightButton and HEX_RECT.contains(e.position().toPoint()):
            self.paste_hex()
            return
        if e.button() != Qt.LeftButton:
            return
        p = e.position()
        pt = p.toPoint()
        if ZOOM_BTN.contains(pt) and self.settings.get("field_shape") == "focus":
            self.toggle_zoom()
        elif FIELD.contains(pt):
            self.dragging = "field"
            self._drag_field(p)
        elif SLIDER.adjusted(0, -4, 0, 4).contains(pt):
            self.dragging = "slider"
            self._drag_slider(p)
        elif LINK_BTN.contains(pt) and not self.link.ready:
            self.start_pairing()
        elif GREY_BTN.contains(pt):
            self.toggle_grey_panel()
        elif SCREEN_BTN.contains(pt):
            self.toggle_screen_grey()
        elif SHAPE_SQ.contains(pt) or SHAPE_RD.contains(pt) or SHAPE_FO.contains(pt):
            self.set_shape("round" if SHAPE_RD.contains(pt) else
                           "focus" if SHAPE_FO.contains(pt) else "square")
        elif MASK_PREV.contains(pt):
            self.cycle_mask(-1)
        elif MASK_NEXT.contains(pt) or MASK_NAME.contains(pt):
            self.cycle_mask(1)
        elif self.mask() and (ROT_SL.adjusted(-6, -4, 6, 4).contains(pt)
                              or WID_SL.adjusted(-6, -4, 6, 4).contains(pt)):
            self.dragging = "rot" if ROT_SL.adjusted(-6, -4, 6, 4).contains(pt) else "wid"
            self._drag_mask(p)
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
        elif MODE_BTN.contains(pt):
            self.toggle_value_mode()
        elif COPY_BTN.contains(pt):
            self.copy_hex()
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
        if self.move_offset is not None:
            self.move(e.globalPosition().toPoint() - self.move_offset)
            return
        pt = e.position().toPoint()
        over_btn = any(r.contains(pt) for r in (CLOSE_BTN, PIN_BTN, TOP_BTN, EYE_BTN, HEX_RECT,
                                               MODE_BTN, COPY_BTN, OLD_SW, NEW_SW, SHAPE_SQ,
                                               SHAPE_RD, SHAPE_FO, MASK_PREV, MASK_NAME, MASK_NEXT,
                                               GREY_BTN, SCREEN_BTN, LINK_BTN))
        if not (self.dragging or e.buttons()):
            self._show_tip(pt, e.globalPosition().toPoint())
        want = (Qt.SizeAllCursor if pt.y() < HEADER_H and not over_btn
                else Qt.PointingHandCursor if over_btn else Qt.ArrowCursor)
        if self.cursor().shape() != want:
            self.setCursor(want)
        if self.dragging == "field":
            self._drag_field(e.position())
        elif self.dragging == "slider":
            self._drag_slider(e.position())
        elif self.dragging in ("rot", "wid"):
            self._drag_mask(e.position())

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self.move_offset is not None:
            self.move_offset = None
            # a window you placed yourself stays there and stays open
            self.settings["window_pos"] = [self.x(), self.y()]
            self.set_pinned(True)
            return
        if self.dragging in ("rot", "wid"):
            self.dragging = None
            save_settings(self.settings)
            self.update()
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
        m = self.mask()
        C = float(np.hypot(a, b))
        if m and C >= MASK_GREY_C:      # masked hue: slide to the nearest allowed one
            h = np.radians(mask_snap(float(np.degrees(np.arctan2(b, a))), *m))
            a, b = C * np.cos(h), C * np.sin(h)
        if self.luma_mode():
            L, a, b = luma_color(self.Y, a, b)
            self.L = float(L)
        else:
            a, b = clamp_chroma(self.L, a, b)
        self.a, self.b = float(a), float(b)
        if not self.luma_mode():
            self.Y = luma_of_hex(self.current_hex())   # ready if the mode flips
        self.update()

    def _tips(self):
        on = lambda v: "on" if v else "off"    # noqa: E731
        return (
            (HEX_RECT, "Hex code. Click to copy, right-click to paste one"),
            (COPY_BTN, "Copy the hex code"),
            (LINK_BTN, "Connect to Clip Studio: click, then open File > Connect to "
                       "smartphone in Clip Studio. Luma reads the QR code and pairs"),
            (MODE_BTN, "How value is measured. L: perceptual. Y: Clip Studio "
                       "luminance (grey Color-layer check). Click to switch"),
            (EYE_BTN, "Eyedropper: pick any color on screen (%s)"
             % self.settings.get("eyedropper_hotkey", "")),
            (OLD_SW, "Last color sent. Click to go back to it"),
            (NEW_SW, "Current color"),
            (TOP_BTN, "Stay in front of other windows (%s)" % on(self.on_top)),
            (PIN_BTN, "Pin: keep the panel open at this spot (%s). Unpinned, it "
                      "hides when the pen moves away" % on(self.pinned)),
            (CLOSE_BTN, "Hide (%s shows it again)" % self.settings.get("hotkey", "")),
            (SLIDER, "Value. Drag, or use the mouse wheel for 1% steps"),
            (SHAPE_SQ, "Square field: hue left to right, pale to strong top to bottom"),
            (SHAPE_RD, "Round wheel: hue around the circle, strongest at the edge"),
            (SHAPE_FO, "Focus: the plain color plane of this value, grey where no color exists"),
            (GREY_BTN, "Show this panel in greyscale: the field turns one flat grey "
                       "because every color in it has the same value"),
            (SCREEN_BTN, "Whole screen in greyscale (%s): paint in values only. "
                         "Picked colors stay real colors"
             % (self.settings.get("greyscale_hotkey", "") or "no hotkey")),
            (MASK_NAME.united(MASK_PREV).united(MASK_NEXT),
             "Gamut mask: only some hues can be picked. Click to change"),
            (ROT_SL, "Turn the mask"),
            (WID_SL, "How wide each allowed hue sector is"),
            (JUMP_BTN, "On: picking a shade moves the field to it. Off: the rows stay put"),
            (SURF_PICK, "Pick the floor or background color from the screen"),
            (SURF_CUR, "Use the current color as the floor or background"),
        )

    def _show_tip(self, pt, gpos):
        tip = next((t for r, t in self._tips() if r.contains(pt)), "")
        if tip != getattr(self, "_tip", ""):
            self._tip = tip
            if tip:
                QToolTip.showText(gpos, tip, self)
            else:
                QToolTip.hideText()

    def leaveEvent(self, _):
        self._tip = ""
        QToolTip.hideText()

    def _drag_slider(self, p):
        self.set_value((p.x() - SLIDER.left()) / SLIDER.width())

    def wheelEvent(self, e):
        """Wheel over the field or bar: nudge the value; the color goes to
        Clip Studio once the wheel has been still for a moment."""
        pt = e.position().toPoint()
        if not (FIELD.contains(pt) or SLIDER.adjusted(0, -4, 0, 4).contains(pt)):
            return
        notches = e.angleDelta().y() / 120.0
        if not notches:
            return
        self.set_value(self.value() + notches * float(self.settings.get("wheel_step", 0.01)))
        self.wheel_timer.start(int(self.settings.get("wheel_send_ms", 350)))

    # -------------------------------------------------------------- painting
    def _images(self):
        dpr = self.devicePixelRatioF()
        # half resolution while the lightness bar is being dragged: much
        # cheaper, and the full-quality field comes back on release
        fast = self.dragging in ("slider", "rot", "wid") or self.wheel_timer.isActive()
        luma = self.luma_mode()
        v = self.value()
        shape = self.view_shape()
        mask = self.mask()
        fk = (round(v, 3 if fast else 4), dpr, fast, luma, LUMA_W, shape, mask, self.cells())
        if fk != self._field_key:
            n = int(FIELD.width() * dpr)
            n = n // 2 if fast else n
            img = render_view(v, n, luma, shape, mask, self.cells())
            if fast:
                img = img.scaled(n, n, Qt.IgnoreAspectRatio, Qt.FastTransformation)
            img.setDevicePixelRatio(dpr)
            self._field_img, self._field_key = img, fk
        sk = (round(self.a, 4), round(self.b, 4), dpr, luma, LUMA_W)
        if sk != self._slider_key:
            img = (render_slider_luma if luma else render_slider)(
                self.a, self.b, int(SLIDER.width() * dpr), int(SLIDER.height() * dpr))
            img.setDevicePixelRatio(dpr)
            self._slider_img, self._slider_key = img, sk
        return self._field_img, self._slider_img

    def paintEvent(self, _):
        if not self.settings.get("grey_panel", False):
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            self._paint(p)
            return
        # greyscale: paint off-screen, then turn every pixel into the grey of
        # its value (the field becomes one flat grey: proof it's one value)
        dpr = self.devicePixelRatioF()
        img = QImage(int(W * dpr), int(H * dpr), QImage.Format_RGBA8888)
        img.fill(Qt.transparent)
        img.setDevicePixelRatio(dpr)
        q = QPainter(img)
        q.setRenderHint(QPainter.Antialiasing)
        self._paint(q)
        q.end()
        arr = np.frombuffer(img.bits(), np.uint8).reshape(
            img.height(), img.bytesPerLine() // 4, 4)[:, :img.width()]
        g = np.round(to_grey(arr[..., :3] / 255.0, self.luma_mode()) * 255)
        arr[..., :3] = np.clip(g, 0, 255).astype(np.uint8)[..., None]
        QPainter(self).drawImage(0, 0, img)

    def _paint(self, p):
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
            p.drawText(TOAST_RECT, Qt.AlignVCenter | Qt.AlignLeft, toast[0])
        else:
            # hex (click to copy, right-click to paste) + value mode button
            p.drawText(HEX_RECT, Qt.AlignVCenter | Qt.AlignLeft, cur.upper())
            luma = self.luma_mode()
            mr = QRectF(MODE_BTN)
            p.setPen(QPen(QColor(BORDER if luma else "#3a3a46"), 1))
            p.setBrush(QColor(ACTIVE) if luma else Qt.NoBrush)
            p.drawRoundedRect(mr, 9, 9)
            p.setBrush(Qt.NoBrush)
            p.setFont(QFont("Segoe UI", 8))
            p.setPen(QColor(TEXT if luma else DIM))
            p.drawText(mr, Qt.AlignCenter, ("Y %d" if luma else "L %d")
                       % round(self.value() * 100))
        font.setBold(False)
        p.setFont(font)
        dpr = self.devicePixelRatioF()

        def draw_icon(rect, name, color, size=14):
            pm = icon(name, color, size, dpr)
            if pm:
                p.drawPixmap(QPointF(rect.center().x() + 0.5 - size / 2,
                                     rect.center().y() + 0.5 - size / 2), pm)
        if not toast:
            draw_icon(COPY_BTN, "copy", DIM, 12)
        draw_icon(EYE_BTN, "eyedropper", TEXT)
        self._paint_link(p)

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
                p.setBrush(QColor(ACTIVE))
                p.drawRoundedRect(QRectF(btn), 5, 5)
        # stay on top: an arrow pushing up to a bar ("always in front")
        t = QRectF(TOP_BTN)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor(BORDER if self.on_top else DIM), 1.6, Qt.SolidLine, Qt.RoundCap))
        cx_ = t.center().x() + 0.5
        p.drawLine(QPointF(t.left() + 5, t.top() + 5), QPointF(t.right() - 4, t.top() + 5))
        p.drawLine(QPointF(cx_, t.top() + 8), QPointF(cx_, t.bottom() - 4))
        p.drawLine(QPointF(cx_ - 3.5, t.top() + 11.5), QPointF(cx_, t.top() + 8))
        p.drawLine(QPointF(cx_ + 3.5, t.top() + 11.5), QPointF(cx_, t.top() + 8))
        draw_icon(PIN_BTN, "pin", BORDER if self.pinned else DIM, 17)
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
        if self.round_field():          # smooth the wheel's pixel edge
            p.setBrush(Qt.NoBrush)
            p.setPen(QPen(QColor(BG), 2.2))
            p.drawEllipse(QRectF(FIELD).adjusted(-0.5, -0.5, 0.5, 0.5))

        if self.settings.get("field_shape") == "focus":      # Focus / Full switch
            zoom = self.settings.get("focus_zoom", True)
            p.setPen(QPen(QColor(BORDER if zoom else "#3a3a46"), 1))
            p.setBrush(QColor(BG))
            p.drawRoundedRect(QRectF(ZOOM_BTN), 9, 9)
            p.setFont(QFont("Segoe UI", 8))
            p.setPen(QColor(TEXT))
            p.drawText(QRectF(ZOOM_BTN), Qt.AlignCenter, "Focus" if zoom else "Full")
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
        hx = SLIDER.left() + self.value() * SLIDER.width()
        handle = QRectF(hx - 6, SLIDER.top() - 3, 12, SLIDER.height() + 6)
        p.setPen(QPen(QColor("#000000a0"), 3.2))
        p.drawRoundedRect(handle, 4, 4)
        p.setPen(QPen(QColor("#ffffff"), 1.6))
        p.setBrush(QColor(cur))
        p.drawRoundedRect(handle, 4, 4)

        # ---- field shape switch + gamut mask
        small8 = QFont("Segoe UI", 8)
        p.setFont(small8)
        sh = self.settings.get("field_shape", "square")
        seg = QPainterPath()
        seg.addRoundedRect(QRectF(SHAPE_SQ.united(SHAPE_FO)), 9, 9)
        p.setPen(QPen(QColor("#3a3a46"), 1))
        p.setBrush(Qt.NoBrush)
        p.drawPath(seg)
        p.save()
        p.setClipPath(seg)
        p.fillRect({"round": SHAPE_RD, "focus": SHAPE_FO}.get(sh, SHAPE_SQ), QColor(ACTIVE))
        p.restore()
        for rect, label, on in ((SHAPE_SQ, "Square", sh not in ("round", "focus")),
                                (SHAPE_RD, "Round", sh == "round"),
                                (SHAPE_FO, "Focus", sh == "focus")):
            p.setPen(QColor(TEXT if on else DIM))
            p.drawText(QRectF(rect), Qt.AlignCenter, label)

        # greyscale buttons: half-filled circle (panel), half-filled monitor (screen)
        for rect, on in ((GREY_BTN, self.settings.get("grey_panel", False)),
                         (SCREEN_BTN, getattr(self, "screen_grey", False))):
            r = QRectF(rect)
            if on:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(ACTIVE))
                p.drawRoundedRect(r, 5, 5)
            col = QColor(BORDER if on else DIM)
            if rect is GREY_BTN:
                shape = QRectF(r.center().x() - 5.5, r.center().y() - 5.5, 12, 12)
            else:
                shape = QRectF(r.left() + 3, r.top() + 3, 13, 9)
                p.setPen(QPen(col, 1.4))
                p.drawLine(QPointF(r.center().x() + 0.5, shape.bottom()),
                           QPointF(r.center().x() + 0.5, r.bottom() - 3))
                p.drawLine(QPointF(r.center().x() - 3, r.bottom() - 3),
                           QPointF(r.center().x() + 4, r.bottom() - 3))
            half = QPainterPath()
            if rect is GREY_BTN:
                half.addEllipse(shape)
            else:
                half.addRoundedRect(shape, 1.5, 1.5)
            p.save()
            p.setClipRect(QRectF(shape.left(), shape.top(), shape.width() / 2, shape.height()))
            p.fillPath(half, col)
            p.restore()
            p.setPen(QPen(col, 1.4))
            p.setBrush(Qt.NoBrush)
            p.drawPath(half)

        mask = self.mask()
        p.setPen(QColor(DIM))
        p.drawText(QRectF(MASK_PREV), Qt.AlignCenter, "‹")
        p.drawText(QRectF(MASK_NEXT), Qt.AlignCenter, "›")
        p.setPen(QColor(TEXT if mask else DIM))
        p.drawText(QRectF(MASK_NAME), Qt.AlignCenter, MASKS[self.mask_index()][0])

        rot = float(self.settings.get("mask_rotate", 0) or 0) % 360.0
        wid = min(max(float(self.settings.get("mask_width", 60) or 60), 10.0), 180.0)
        for rect, label, f in ((ROT_SL, "Rotate", rot / 360.0),
                               (WID_SL, "Width", (wid - 10) / 170.0)):
            p.setPen(QColor(DIM if not mask else TEXT))
            p.drawText(QRect(rect.left() - 42, rect.top() - 2, 40, 18),
                       Qt.AlignVCenter | Qt.AlignLeft, label)
            y = rect.center().y() + 0.5
            p.setPen(QPen(QColor("#3a3a46"), 3, Qt.SolidLine, Qt.RoundCap))
            p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
            x = rect.left() + f * rect.width()
            if mask:
                p.setPen(QPen(QColor(BORDER), 3, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(QPointF(rect.left(), y), QPointF(x, y))
            p.setPen(QPen(QColor("#000000a0"), 1))
            p.setBrush(QColor(TEXT if mask else DIM))
            p.drawEllipse(QPointF(x, y), 5, 5)

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
        p.setBrush(QColor(ACTIVE) if jump else Qt.NoBrush)
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
                if k not in ("history", "last_color", "window_pos", "csp_link"):
                    settings[k] = v
            apply_luma_weights(settings)
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
    menu.addAction("Screen in greyscale on/off", picker.toggle_screen_grey)
    menu.addSeparator()
    menu.addAction("Quit", app.quit)
    app.aboutToQuit.connect(lambda: set_screen_grey(False)
                            if getattr(picker, "screen_grey", False) else None)
    tray.setContextMenu(menu)
    tray.activated.connect(lambda r: picker.show_centered()
                           if r == QSystemTrayIcon.Trigger else None)
    tray.show()

    if IS_WIN:
        hk = HotkeyWindow()
        actions = {1: picker.toggle, 2: lambda: picker.start_eyedropper("main"),
                   3: picker.toggle_screen_grey}
        hk.triggered.connect(lambda i: actions.get(i, lambda: None)())
        for key, hk_id, what in (("eyedropper_hotkey", 2, "eyedropper"),
                                 ("greyscale_hotkey", 3, "greyscale")):
            try:
                eh = settings.get(key, "")
                if eh and not hk.register(eh, hk_id):
                    notify("Could not register %s key %s." % (what, eh))
            except ValueError as exc:
                notify("%s is not valid: %s" % (key, exc))
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
