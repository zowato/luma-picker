# Third-party software

Luma Picker's own code is MIT licensed (see `LICENSE`). The Windows download
also contains the following software, each under its own license.

| Component | Used for | License |
|---|---|---|
| [Qt for Python (PySide6)](https://www.qt.io/qt-for-python) and the Qt 6 libraries | Window, drawing, tray icon | [LGPL v3](https://www.gnu.org/licenses/lgpl-3.0.html) |
| [NumPy](https://numpy.org) | Color math | [BSD 3-Clause](https://github.com/numpy/numpy/blob/main/LICENSE.txt) |
| [zxing-cpp](https://github.com/zxing-cpp/zxing-cpp) | Reads Clip Studio's pairing QR code | [Apache 2.0](https://github.com/zxing-cpp/zxing-cpp/blob/master/LICENSE) |
| Protocol notes from [ok-picker-csp](https://github.com/luapwastaken/ok-picker-csp) and [clipremote](https://github.com/chocolatkey/clipremote) | Reference for `src/csp_link.py` (no code copied) | MIT |
| [Python](https://www.python.org) | Runtime | [PSF License](https://docs.python.org/3/license.html) |
| Icons in `assets/icons` (eyedropper, copy, pin) | Header buttons | Source and license still to be confirmed before release |
| [PyInstaller](https://pyinstaller.org) bootloader | Turns the script into an .exe | GPL v2 with the PyInstaller bootloader exception (allows distributing apps built with it under any license) |

## About the Qt / LGPL v3 libraries

- The download ships Qt as separate, unmodified DLL files in the
  `_internal` folder (it is not compiled into one .exe). You may replace them
  with other compatible builds of Qt 6 / PySide6.
- Qt's source code is available at <https://download.qt.io/official_releases/qt/>
  and PySide6's at <https://code.qt.io/cgit/pyside/pyside-setup.git/>.
- The full license texts are included with the installed packages and at the
  links above.

Luma Picker is not affiliated with or endorsed by The Qt Company, CELSYS
(Clip Studio Paint) or any hardware maker.
