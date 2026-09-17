"""Resize only the probe's own SDL window and verify the fit width it resolves to.

"Fit" keeps the 144-pixel game height and derives the width from the window's
real client aspect, clamped to 160..4096.
"""
import ctypes, json
from ctypes import wintypes

from probe_adaptive import Probe, ROOT, PLAY_FRAME

user32 = ctypes.WinDLL("user32", use_last_error=True)
callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.EnumWindows.argtypes = [callback, wintypes.LPARAM]


def window_for(pid):
    found = []

    @callback
    def visit(hwnd, _):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == pid and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
        return True

    user32.EnumWindows(visit, 0)
    assert found, "SDL window not found"
    return found[0]


def main():
    p = Probe("adaptive-fit", aspect="fit", window=True)
    results = []
    try:
        p.run_to(PLAY_FRAME + 120)
        state = p.view()
        assert state["valid"] == 1, state
        hwnd = window_for(p.process.pid)
        for w, h in [(1280, 720), (1600, 450), (1800, 400), (800, 800), (1536, 432)]:
            outer, client = wintypes.RECT(), wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(outer))
            user32.GetClientRect(hwnd, ctypes.byref(client))
            dx = (outer.right - outer.left) - client.right
            dy = (outer.bottom - outer.top) - client.bottom
            assert user32.SetWindowPos(hwnd, 0, 0, 0, w + dx, h + dy, 0x0014)
            p.step(8)
            user32.GetClientRect(hwnd, ctypes.byref(client))
            state = p.capture(f"fit-{w}x{h}")
            expected = max(160, min(4096, (client.right * 144 + client.bottom // 2) // client.bottom))
            assert state["width"] == expected, (state, expected)
            if state["width"] > 160:
                assert state["valid"] == 1, state
                # 21 x 17: the 18th BG row is behind the status-bar window and
                # is not scored (sml2_adaptive.c validate_scene).
                assert state["paint_score"][0] == state["paint_score"][1] == 357, state
            results.append(dict(client=[client.right, client.bottom], **state))
    finally:
        p.close()
    (ROOT / "logs/adaptive-fit-results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
