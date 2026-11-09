"""Direct color hand-off to Clip Studio Paint through its companion
("Connect to smartphone") connection, so picks never read the screen.

Built from the public protocol notes of ok-picker-csp (docs/PROTOCOL.md) and
clipremote (both MIT). The connection is unofficial and may change with any
Clip Studio update; Luma falls back to the screen pick when it is not up.
"""
import base64
import json
import os
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket

QR_KEY = bytes.fromhex("74B2925B4A21DA")
AUTH_KEY = bytes.fromhex("B6D592C4A783E1")
RECONNECT = "{{(([[reconnection request marker]]))}}\r\n"
RS = b"\x1e"


def _xor(data, key):
    return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))


def decode_qr(url):
    """Pairing QR url -> dict(ips, port, password, generation), or None."""
    try:
        u = urlparse(url)
        if not u.netloc.endswith("clip-studio.com"):
            return None
        s = parse_qs(u.query)["s"][0]
        f = _xor(bytes.fromhex(s), QR_KEY).decode("utf-8").split("\t")
        return {"ips": [i for i in f[0].split(",") if i], "port": int(f[1]),
                "password": f[2], "generation": f[3]}
    except (KeyError, IndexError, ValueError, UnicodeDecodeError):
        return None


def frame(kind, command, serial, detail):
    body = json.dumps(detail, separators=(",", ":")) if detail != "" else ""
    return (bytes([kind]) + b"$tcp_remote_command_protocol_version=1.0" + RS
            + ("$command=%s" % command).encode() + RS
            + ("$serial=%d" % serial).encode() + RS
            + ("$detail=%s" % body).encode("utf-8") + RS + b"\x00")


def parse_frame(raw):
    """One frame (without the trailing 0x00) -> (kind, command, serial, detail)."""
    kind, fields = raw[0], {}
    for part in raw[1:].split(b"\x0b")[0].split(RS):
        if part.startswith(b"$") and b"=" in part:
            k, v = part[1:].split(b"=", 1)
            fields[k.decode()] = v.decode("utf-8", "replace")
    try:
        detail = json.loads(fields.get("detail") or "null")
    except ValueError:
        detail = None
    return kind, fields.get("command", ""), int(fields.get("serial", "0") or 0), detail


def auth_detail(generation, first_secret, password):
    h = lambda x: _xor(x.encode("utf-8"), AUTH_KEY).hex()
    return [generation, h(first_secret), h(password)]


def find_qr_url(qimage):
    """Look for Clip Studio's pairing QR in a screenshot."""
    try:
        import numpy as np
        import zxingcpp
    except ImportError:
        return None
    img = qimage.convertToFormat(qimage.Format.Format_Grayscale8)
    arr = np.frombuffer(img.constBits(), np.uint8).reshape(
        img.height(), img.bytesPerLine())[:, :img.width()]
    for r in zxingcpp.read_barcodes(arr):
        if "clip-studio.com" in r.text:
            return r.text
    return None


class CspLink(QObject):
    """Keeps one companion connection to Clip Studio and sets its color."""
    state = Signal(str)          # "ready", "paired", "failed:<why>", "down"

    def __init__(self, cfg, save):
        super().__init__()
        self.cfg, self.save = cfg, save   # cfg: dict stored in settings
        self.sock = QTcpSocket(self)
        self.sock.connected.connect(self._on_connected)
        self.sock.readyRead.connect(self._on_read)
        self.sock.disconnected.connect(self._on_down)
        self.sock.errorOccurred.connect(lambda _e: self._on_down())
        self.buf, self.serial, self.ready, self.pairing = b"", 0, False, None
        self.beat = QTimer(self, interval=1000, timeout=lambda: self._send("TellHeartbeat", ""))
        self.retry = QTimer(self, interval=3000, singleShot=True, timeout=self.connect_saved)

    # ------------------------------------------------------------- public
    def paired(self):
        return bool(self.cfg.get("password"))

    def connect_saved(self):
        if self.paired() and self.sock.state() == QAbstractSocket.UnconnectedState:
            self.pairing = None
            self.sock.connectToHost("127.0.0.1", int(self.cfg.get("port", 32035)))

    def pair(self, qr):
        """Pair with the info from a fresh QR code."""
        self.sock.abort()
        self.pairing = dict(qr, new=base64.b64encode(os.urandom(6)).decode().rstrip("="))
        self.sock.connectToHost("127.0.0.1", qr["port"])

    def set_color(self, hx):
        """Send a color. False if the link is not up (use the fallback)."""
        if not self.ready:
            return False
        r, g, b = (int(hx[i:i + 2], 16) for i in (1, 3, 5))
        self._send("SetCurrentColor", {"IsColorTransparent": False,
                                       "RGBColorR": r, "RGBColorG": g, "RGBColorB": b})
        return True

    # ------------------------------------------------------------ private
    def _send(self, command, detail, kind=1, serial=None):
        if self.sock.state() != QAbstractSocket.ConnectedState:
            return
        if serial is None:
            serial, self.serial = self.serial, self.serial + 1
        self.sock.write(frame(kind, command, serial, detail))

    def _on_connected(self):
        self.buf, self.serial = b"", 0
        if self.pairing:
            p = self.pairing
            d = auth_detail(p["generation"], p["password"], p["new"])
        else:
            d = auth_detail(self.cfg.get("generation", ""), RECONNECT, self.cfg["password"])
        self._send("Authenticate", d)

    def _on_read(self):
        self.buf += bytes(self.sock.readAll())
        while b"\x00" in self.buf:
            raw, self.buf = self.buf.split(b"\x00", 1)
            if raw:
                self._handle(*parse_frame(raw))

    def _handle(self, kind, command, serial, detail):
        if kind == 1:                       # Clip Studio asks: just acknowledge
            self._send(command, "", kind=6, serial=serial)
            return
        if command != "Authenticate":
            return
        why = (detail or {}).get("AuthErrorReason", "") if isinstance(detail, dict) else ""
        if why in ("", "Unknown"):
            if self.pairing:
                p = self.pairing
                self.cfg.update(port=p["port"], generation=p["generation"], password=p["new"])
                self.save()
                self.pairing = None
                self.state.emit("paired")
            self.ready = True
            self.beat.start()
            self.state.emit("ready")
        else:
            if why == "PasswordMismatch" and not self.pairing:
                self.cfg.pop("password", None)   # saved login is void: pair again
                self.save()
            self.pairing = None
            self.state.emit("failed:" + why)
            self.sock.abort()

    def _on_down(self):
        was = self.ready
        self.ready = False
        self.beat.stop()
        if was:
            self.state.emit("down")
        if self.paired() and not self.retry.isActive():
            self.retry.start()
