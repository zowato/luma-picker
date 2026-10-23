"""Checks for the color math (no window is opened)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import numpy as np  # noqa: E402
import pytest  # noqa: E402

import luma_picker as lp  # noqa: E402


@pytest.mark.parametrize("hx", ["#ff0000", "#00ff00", "#0000ff", "#808080",
                                "#123456", "#ffffff", "#000000", "#c0607a"])
def test_hex_round_trip(hx):
    assert lp.oklab_to_hex(*lp.hex_to_oklab(hx)) == hx


def test_field_is_one_lightness():
    L = 0.65
    cx, cy, half = lp.fit_for_L(L)
    t = np.linspace(-half, half, 60)
    A, B = np.meshgrid(cx + t, cy + t)
    r, g, b = lp.oklab_to_linear(L, A, B)
    ok = lp.in_gamut(r, g, b)
    assert ok.mean() > 0.2
    Lr, _, _ = lp.linear_to_oklab(r[ok], g[ok], b[ok])
    assert np.allclose(Lr, L, atol=1e-3)


def test_clamp_keeps_colors_in_gamut():
    a, b = lp.clamp_chroma(0.7, 0.4, -0.4)
    assert lp.in_gamut(*lp.oklab_to_linear(0.7, float(a), float(b)))


def _hsv(hx):
    import colorsys
    r, g, b = (int(hx[i:i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hsv(r, g, b)


@pytest.mark.parametrize("base", ["#5b8fd6", "#d8314a", "#2bb04a", "#e8b080"])
def test_shading_rules(base):
    row = lp.shading_rows(*lp.hex_to_oklab(base))
    assert len(row) == lp.SHADE_N and row[lp.SHADE_N // 2] == base
    _, s0, v0 = _hsv(base)
    _, s_dark, v_dark = _hsv(row[0])
    _, s_light, v_light = _hsv(row[-1])
    assert v_dark < v0 < v_light or v0 >= 0.99      # darker / lighter
    assert s_dark >= s0 - 1e-6                      # shadows more saturated
    assert s_light <= s0 + 1e-6                     # lights less saturated


def test_yellow_shadows_avoid_green():
    row = lp.shading_rows(*lp.hex_to_oklab("#e6b422"))
    h, _, _ = _hsv(row[0])
    assert h * 360 < 50                             # orange/brown, not green


@pytest.mark.parametrize("name,offsets", lp.HARMONIES)
def test_harmonies(name, offsets):
    out = lp.harmony_colors(*lp.hex_to_oklab("#c0607a"), offsets)
    assert len(out) == len(offsets)
    assert sum(1 for _, _, base in out if base) == 1


def test_surroundings():
    shades = lp.shading_rows(*lp.hex_to_oklab("#a8783a"))
    floor, bounce = lp.surroundings("#d6d6d6", "#a8783a", shades)
    assert len(floor) == 4 and len(bounce) == 4
    # floor shadow gets lighter toward the soft edge
    vs = [_hsv(c)[2] for c in floor]
    assert vs == sorted(vs)


def test_parse_combo():
    assert lp.parse_combo("ctrl+alt+shift+o") == (["ctrl", "alt", "shift"], ord("O"))
    assert lp.parse_combo("F13")[1] == 0x7C
    with pytest.raises(ValueError):
        lp.parse_combo("ctrl+nonsense")
