import ctypes
from ctypes import wintypes

EnumWindows = ctypes.windll.user32.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
GetWindowText = ctypes.windll.user32.GetWindowTextW
GetWindowTextLength = ctypes.windll.user32.GetWindowTextLengthW
IsWindowVisible = ctypes.windll.user32.IsWindowVisible

titles = []
@EnumWindowsProc
def foreach(hwnd, lParam):
    try:
        if IsWindowVisible(hwnd):
            length = GetWindowTextLength(hwnd)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length+1)
                GetWindowText(hwnd, buf, length+1)
                title = buf.value
                if title:
                    titles.append(title)
    except Exception:
        pass
    return True

EnumWindows(foreach, 0)
for t in titles:
    print(t)
