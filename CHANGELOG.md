# Changelog

## Unreleased

- **Clip Studio luminance value mode**: click `L` next to the hex to switch to `Y`, which keeps the grey value Clip Studio shows with a grey Color-blend layer (field, bar and harmonies all follow it)
- **Round wheel**: switch the field between Square and Round. The wheel shows the same single value, with hue as the angle
- **Gamut masks**: analogous, complementary, split complementary and triad, with Rotate and Width. Masked hues are dimmed and can't be picked. Loading a color outside the mask turns the mask onto it
- Warm orange accent instead of purple, new eyedropper/copy/pin icons, a clearer "stay in front" icon, and hover tooltips on every control
- Masked hues are pushed darker when the picked value is light and lighter when it is dark, so the allowed hues stand out
- **New square field**: blocks of color with hue left to right and pale (top) to strongest (bottom), all at one value. It replaces the old a/b plane view
- Mouse wheel over the field or bar nudges the value 1% per notch and sends the color when you stop
- Fix: the pick shortcut is only sent to Clip Studio (`pick_apps`). Before, picking while another app was in front sent it Ctrl+Alt+Shift+O, which could open a file dialog

## 1.0.0 – first public release

- Lightness-locked color field (every color in it has the same perceptual value) with a lightness bar
- Floating panel: drag by the top bar, pin it, keep it on top of other windows; opens with a global hotkey or the tray icon
- Sends colors straight to Clip Studio Paint through its "Pick screen color" command
- **Shading** ramp: shadows cooler, more saturated and darker; lights warmer, less saturated and lighter
- **Harmony**: complementary, split complementary, triad, square, tetradic and analogous, with a mini color wheel
- **Surroundings**: floor/cast shadow and bounce light suggestions based on the surface color
- **Eyedropper** for any color on screen (button or hotkey), click the hex to copy it, right-click to paste one
- Recent colors, settings file that reloads while the app runs
