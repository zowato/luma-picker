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
    A, B, _ = lp.field_ab(60, L, False, "square")
    r, g, b = lp.oklab_to_linear(L, A, B)
    ok = lp.in_gamut(r, g, b)
    assert ok.mean() > 0.99                        # every cell is a real color
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


# ---- "luminance" value mode (Clip Studio's grey-layer check)
def _luma(hx):
    return lp.luma_of_hex(hx)


@pytest.mark.parametrize("Y", [0.15, 0.4, 0.64, 0.9])
def test_luma_field_is_one_luminance(Y):
    A, B, _ = lp.field_ab(40, Y, True, "square")
    r, g, b = lp.oklab_to_linear(lp.L_for_luma(Y, A, B), A, B)
    ok = lp.in_gamut(r, g, b)
    assert ok.mean() > 0.99
    s = lp.linear_to_srgb(np.stack([r[ok], g[ok], b[ok]]))
    assert np.allclose(lp.luma_of_srgb(*s), Y, atol=2e-3)


def test_square_field_layout():
    A, B, _ = lp.field_ab(48, 0.7, False, "square", cells=12)
    C = np.hypot(A, B)
    assert C[0].max() < C[-1].min()                # pale top, strong bottom
    assert np.allclose(A[:4, :4], A[0, 0]) and np.allclose(B[:4, :4], B[0, 0])  # flat 4x4 cells
    h = np.degrees(np.arctan2(B[-1], A[-1])) % 360
    assert abs(h[0] - (lp.SQUARE_HUE0 + 15) % 360) < 1   # first cell center hue


def test_mask_turns_to_color():
    rot = lp.mask_rotation_for(100, (0, 120, 240), 0)
    assert lp.mask_allows(100, (0, 120, 240), rot, 20)
    assert rot == 340                               # nearest sector (120) moved -20°


def test_luma_color_hits_target_and_fits_gamut():
    for hue in range(0, 360, 30):
        h = np.radians(hue)
        L, a, b = lp.luma_color(0.6, 0.3 * np.cos(h), 0.3 * np.sin(h))
        assert abs(_luma(lp.oklab_to_hex(float(L), float(a), float(b))) - 0.6) < 0.01


def test_luma_differs_from_perceptual():
    # same OKLab L, very different grey value: the reason the mode exists
    L = 0.6
    red = lp._lch_to_hex(L, 0.4, 20)
    green = lp._lch_to_hex(L, 0.4, 120)
    assert abs(_luma(red) - _luma(green)) > 0.1


def test_harmony_keeps_luminance():
    L, a, b = lp.hex_to_oklab("#c0607a")
    Y = _luma("#c0607a")
    for hx, _h, _base in lp.harmony_colors(L, a, b, (0, 120, 240), luma=Y):
        assert abs(_luma(hx) - Y) < 0.01


# ---- round wheel + gamut masks
@pytest.mark.parametrize("luma", [False, True])
def test_round_wheel_rim_is_in_gamut_and_one_value(luma):
    v = 0.6
    A, B, inside = lp.field_ab(60, v, luma, "round")
    A, B = A[inside], B[inside]
    L = lp.L_for_luma(v, A, B) if luma else np.full_like(A, v)
    r, g, b = lp.oklab_to_linear(L, A, B)
    assert lp.in_gamut(r, g, b).mean() > 0.99      # the whole disc is usable
    if luma:
        s = lp.linear_to_srgb(np.stack([r, g, b]))
        assert np.allclose(lp.luma_of_srgb(*s), v, atol=2e-3)


def test_mask_allows_and_snaps():
    tri = (0, 120, 240)
    assert lp.mask_allows(10, tri, 0, 40)
    assert not lp.mask_allows(60, tri, 0, 40)
    assert lp.mask_allows(70, tri, 60, 40)          # rotated
    assert lp.mask_snap(30, tri, 0, 40) == 20       # nearest sector edge
    assert lp.mask_snap(355, (0,), 0, 40) == 355    # allowed: unchanged
    assert lp.mask_snap(160, (0,), 0, 40) == 20     # 140° to 20, 180° to 340
    assert lp.mask_snap(200, (0,), 0, 40) == 340
    assert lp.mask_allows(123, (), 0, 40)            # mask off
