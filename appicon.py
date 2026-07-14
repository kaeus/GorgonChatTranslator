"""
appicon.py - give a Python app its own unique Windows taskbar icon.

    # console / web-server app:
    try:
        import sys; sys.path.insert(0, r"c:\\projects")
        import appicon; appicon.set_app_icon("My Tool")
    except Exception:
        pass

    # tkinter app (call right after you create the Tk root):
    try:
        import sys; sys.path.insert(0, r"c:\\projects")
        import appicon; appicon.set_app_icon("My Tool", tk_root=root)
    except Exception:
        pass

It builds a deterministic identicon .ico (unique colour + symmetric pattern
derived from the name), caches it in c:\\projects\\_appicons, sets a unique
AppUserModelID so Windows stops piling the app under the generic python.exe
taskbar group, and applies the icon to the window. Pure stdlib, Windows-only
effects, and every step is wrapped so it can never break the host app.
"""

import hashlib
import math
import os
import re
import struct

ICON_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_appicons")


# --------------------------------------------------------------------------
# icon drawing (identicon: dark rounded tile + symmetric coloured blocks)
# --------------------------------------------------------------------------
def _hsv_to_rgb(h, s, v):
    i = int(h * 6)
    f = h * 6 - i
    p, q, t = v * (1 - s), v * (1 - f * s), v * (1 - (1 - f) * s)
    r, g, b = [(v, t, p), (q, v, p), (p, v, t),
               (p, q, v), (t, p, v), (v, p, q)][i % 6]
    return (int(r * 255), int(g * 255), int(b * 255), 255)


def _draw(size, digest):
    bg = (24, 26, 32, 255)  # dark tile (#181a20)
    hue = ((digest[0] << 8) | digest[1]) % 360 / 360.0
    accent = _hsv_to_rgb(hue, 0.58, 1.0)

    grid = 5
    pad = max(2, round(size * 0.16))
    cell = (size - 2 * pad) / grid

    # 5 rows x 3 left columns from hash bits, mirrored to the right -> symmetric
    bits = int.from_bytes(digest[2:6], "big")
    on = [[False] * grid for _ in range(grid)]
    b = 0
    for col in range(3):
        for row in range(grid):
            v = bool((bits >> b) & 1)
            b += 1
            on[row][col] = v
            on[row][grid - 1 - col] = v

    cx = cy = (size - 1) / 2.0
    R = size / 2.0
    corner = R * 0.30
    buf = bytearray(size * size * 4)
    for y in range(size):
        for x in range(size):
            color = (0, 0, 0, 0)
            dx = abs(x - cx) - (R - corner)
            dy = abs(y - cy) - (R - corner)
            if dx <= 0 or dy <= 0 or math.hypot(max(dx, 0), max(dy, 0)) <= corner:
                color = bg
                if pad <= x < size - pad and pad <= y < size - pad:
                    gx = int((x - pad) // cell)
                    gy = int((y - pad) // cell)
                    if 0 <= gx < grid and 0 <= gy < grid and on[gy][gx]:
                        color = accent
            i = (y * size + x) * 4
            buf[i:i + 4] = bytes(color)
    return buf


def _ico_image(size, digest):
    rgba = _draw(size, digest)
    xor = bytearray()
    for y in range(size - 1, -1, -1):  # bottom-up rows
        for x in range(size):
            i = (y * size + x) * 4
            r, g, bl, a = rgba[i], rgba[i + 1], rgba[i + 2], rgba[i + 3]
            xor += bytes((bl, g, r, a))  # BGRA
    mask_row = ((size + 31) // 32) * 4
    andmask = bytes(mask_row * size)
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    return header + bytes(xor) + andmask


def _safe(name):
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_") or "app"


def make_icon(name):
    """Generate (and cache) an .ico for the given app name; return its path."""
    os.makedirs(ICON_DIR, exist_ok=True)
    digest = hashlib.sha256(name.encode("utf-8")).digest()
    path = os.path.join(ICON_DIR, _safe(name) + ".ico")
    if os.path.exists(path):
        return path
    images = [(s, _ico_image(s, digest)) for s in (16, 32, 48)]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, datas = b"", b""
    for s, img in images:
        w = s if s < 256 else 0
        entries += struct.pack("<BBBBHHII", w, w, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
        datas += img
    with open(path, "wb") as f:
        f.write(out + entries + datas)
    return path


# --------------------------------------------------------------------------
# apply to the running app
# --------------------------------------------------------------------------
def _set_console_icon(ico_path):
    import ctypes
    from ctypes import wintypes
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if not hwnd:
        return
    user32 = ctypes.windll.user32
    user32.LoadImageW.restype = wintypes.HANDLE
    IMAGE_ICON, LR_LOADFROMFILE = 1, 0x00000010
    WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
    big = user32.LoadImageW(None, ico_path, IMAGE_ICON, 32, 32, LR_LOADFROMFILE)
    small = user32.LoadImageW(None, ico_path, IMAGE_ICON, 16, 16, LR_LOADFROMFILE)
    if big:
        user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
    if small:
        user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)


def set_app_icon(name, tk_root=None, app_id=None):
    """Give this app a unique taskbar icon + grouping.

    name    : label used to derive the icon and AppUserModelID (stable per app)
    tk_root : pass your Tk()/Toplevel root for GUI apps; omit for console apps
    Returns the .ico path, or None if unavailable. Never raises.
    """
    if os.name != "nt":
        return None
    try:
        path = make_icon(name)
    except Exception:
        path = None
    try:
        import ctypes
        aid = app_id or ("PyApp." + _safe(name))
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(aid)
    except Exception:
        pass
    if not path:
        return None
    try:
        if tk_root is not None:
            # default=path also applies the icon to any Toplevel windows
            tk_root.iconbitmap(default=path)
        else:
            _set_console_icon(path)
    except Exception:
        pass
    return path


if __name__ == "__main__":
    # quick self-test: generate a few sample icons
    for n in ["Launcher", "Fight Monitor", "Cashfall Solver", "AnatomyDPS"]:
        print(n, "->", make_icon(n))
