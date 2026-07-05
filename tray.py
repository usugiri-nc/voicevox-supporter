"""ぼいぼサポーター トレイ常駐

タスクトレイ（時計横）にアイコンを出し、右クリックメニューから操作する。

Usage:
  pythonw tray.py    # コンソールなしで常駐（普段使い）
  python tray.py     # コンソール付き（デバッグ用）

依存は Python 標準ライブラリのみ（トレイは ctypes で Win32 API を直接叩く）。
アイコンはこのファイルと同じフォルダに icon.ico を置けば差し替わる。
"""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import speak  # Windows チェックは speak 側で行われる

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
shell32 = ctypes.windll.shell32

APP_NAME = "ぼいぼサポーター"
WINDOW_CLASS = "vvtts_tray_cls"
ICON_PATH = Path(__file__).resolve().parent / "icon.ico"
TRAY_MUTEX_NAME = "vvtts_tray"

# --- Win32 定数 ---
WM_CLOSE = 0x0010
WM_DESTROY = 0x0002
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_APP = 0x8000
TRAY_MSG = WM_APP + 1

NIM_ADD, NIM_MODIFY, NIM_DELETE = 0, 1, 2
NIF_MESSAGE, NIF_ICON, NIF_TIP = 1, 2, 4

MF_STRING = 0x0000
MF_GRAYED = 0x0001
MF_CHECKED = 0x0008
MF_SEPARATOR = 0x0800
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100

IDI_APPLICATION = 32512
IDC_ARROW = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
LR_DEFAULTSIZE = 0x0040
ERROR_ALREADY_EXISTS = 183
MB_ICONINFORMATION = 0x0040
SYNCHRONIZE = 0x00100000

# メニュー項目ID
ID_TOGGLE_SPEAK = 1
ID_STOP = 2
ID_TOGGLE_SERVE = 3
ID_SETTINGS = 4
ID_EXIT = 5
ID_TOGGLE_AUTOSTART = 6

# --- Win32 型宣言（64bit でハンドルが切り詰められないよう明示する） ---
LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

user32.DefWindowProcW.restype = LRESULT
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.CreateWindowExW.restype = wintypes.HWND
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, ctypes.c_void_p, wintypes.HINSTANCE, ctypes.c_void_p,
]
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.CreatePopupMenu.restype = ctypes.c_void_p
user32.AppendMenuW.argtypes = [ctypes.c_void_p, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
user32.TrackPopupMenu.restype = ctypes.c_int
user32.TrackPopupMenu.argtypes = [
    ctypes.c_void_p, wintypes.UINT, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p,
]
user32.DestroyMenu.argtypes = [ctypes.c_void_p]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.LoadIconW.restype = ctypes.c_void_p
user32.LoadIconW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.LoadCursorW.restype = ctypes.c_void_p
user32.LoadCursorW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.LoadImageW.restype = ctypes.c_void_p
user32.LoadImageW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
user32.RegisterWindowMessageW.argtypes = [wintypes.LPCWSTR]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = LRESULT
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.c_void_p]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", ctypes.c_void_p),
        ("hCursor", ctypes.c_void_p),
        ("hbrBackground", ctypes.c_void_p),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class NOTIFYICONDATAW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT),
        ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT),
        ("hIcon", ctypes.c_void_p),
        ("szTip", wintypes.WCHAR * 128),
        ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD),
        ("szInfo", wintypes.WCHAR * 256),
        ("uVersion", wintypes.UINT),
        ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD),
    ]


# --- 状態の参照と切り替え（speak.py の仕組みをそのまま使う） ---

def speak_enabled() -> bool:
    return speak.FLAG_PATH.exists()


def toggle_speak():
    if speak_enabled():
        speak.FLAG_PATH.unlink(missing_ok=True)
    else:
        speak.FLAG_PATH.touch()


def serve_running() -> bool:
    state = speak._serve_state()
    if not state:
        return False
    try:
        speak._serve_request(state["port"], "/status", timeout=1.0)
        return True
    except Exception:
        return False


def toggle_serve():
    if serve_running():
        speak._serve_shutdown()
    else:
        subprocess.Popen(
            speak.self_cmd("serve"),
            **speak._detached_popen_kwargs(),
        )


# --- PC起動時の自動起動（スタートアップフォルダのショートカットで管理） ---
# 状態はショートカットの実在そのもの（config に二重管理しない）。手で消しても矛盾しない。

STARTUP_LINK = (
    Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows"
    / "Start Menu" / "Programs" / "Startup" / f"{APP_NAME}.lnk"
)


def autostart_enabled() -> bool:
    return STARTUP_LINK.exists()


def _pythonw_path() -> str:
    # コンソール窓を出さない pythonw を優先（無い環境は python のまま）
    candidate = Path(sys.executable).with_name("pythonw.exe")
    return str(candidate if candidate.exists() else sys.executable)


def set_autostart(enable: bool) -> bool:
    if not enable:
        STARTUP_LINK.unlink(missing_ok=True)
        return not STARTUP_LINK.exists()

    def esc(p) -> str:  # PowerShell の単引用符文字列用
        return str(p).replace("'", "''")

    if speak.FROZEN:
        # exe 配布: ショートカットは exe 自身の --tray モードへ。アイコンも exe 埋め込みのものを使う
        exe = Path(sys.executable).resolve()
        target, arguments, workdir, icon = exe, "--tray", exe.parent, exe
    else:
        tray_path = Path(__file__).resolve()
        target, arguments, workdir = _pythonw_path(), f'"{tray_path}"', tray_path.parent
        icon = ICON_PATH if ICON_PATH.exists() else None
    lines = [
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{esc(STARTUP_LINK)}')",
        f"$s.TargetPath = '{esc(target)}'",
        f"$s.Arguments = '{esc(arguments)}'",
        f"$s.WorkingDirectory = '{esc(workdir)}'",
        f"$s.Description = '{APP_NAME}（トレイ常駐）'",
    ]
    if icon:
        lines.append(f"$s.IconLocation = '{esc(icon)}'")
    lines.append("$s.Save()")
    STARTUP_LINK.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", "; ".join(lines)],
        creationflags=0x08000000,  # CREATE_NO_WINDOW（pythonw から呼んでも窓を出さない）
        capture_output=True,
    )
    return STARTUP_LINK.exists()


_kernel32_le = ctypes.WinDLL("kernel32", use_last_error=True)
_kernel32_le.CreateMutexW.restype = wintypes.HANDLE
_kernel32_le.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]


def already_running() -> bool:
    # ハンドルは意図的に閉じない（プロセス生存中ミューテックスを保持し続ける）
    global _tray_mutex
    _tray_mutex = _kernel32_le.CreateMutexW(None, False, TRAY_MUTEX_NAME)
    return ctypes.get_last_error() == ERROR_ALREADY_EXISTS


def tray_running() -> bool:
    """常駐トレイが居るかを、ミューテックスを掴まずに覗くだけの確認（launcher が使う）。
    already_running() と違い自分では作らないので、後から起動するトレイ本体の判定を壊さない。"""
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, TRAY_MUTEX_NAME)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def load_tray_icon() -> int:
    if ICON_PATH.exists():
        handle = user32.LoadImageW(None, str(ICON_PATH), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE)
        if handle:
            return handle
    return user32.LoadIconW(None, IDI_APPLICATION)  # 仮アイコン（Windows標準）


# --- トレイ本体 ---

def _make_nid(hwnd) -> NOTIFYICONDATAW:
    nid = NOTIFYICONDATAW()
    nid.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
    nid.hWnd = hwnd
    nid.uID = 1
    nid.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP
    nid.uCallbackMessage = TRAY_MSG
    nid.hIcon = load_tray_icon()
    nid.szTip = APP_NAME
    return nid


def _open_settings():
    if speak.FROZEN:
        gui_cmd = [sys.executable, "--gui"]
    else:
        gui_cmd = [sys.executable, str(Path(__file__).resolve().parent / "gui.py")]
    subprocess.Popen(gui_cmd, **speak._detached_popen_kwargs())


def _show_menu(hwnd):
    menu = user32.CreatePopupMenu()
    user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if speak_enabled() else 0), ID_TOGGLE_SPEAK, "読み上げ ON/OFF")
    user32.AppendMenuW(menu, MF_STRING, ID_STOP, "再生を停止")
    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
    user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if serve_running() else 0), ID_TOGGLE_SERVE, "serve 待ち受け ON/OFF")
    user32.AppendMenuW(menu, MF_SEPARATOR, 0, None)
    user32.AppendMenuW(menu, MF_STRING | (MF_CHECKED if autostart_enabled() else 0), ID_TOGGLE_AUTOSTART, "PC起動時に自動で立ち上げる")
    user32.AppendMenuW(menu, MF_STRING, ID_SETTINGS, "設定...")
    user32.AppendMenuW(menu, MF_STRING, ID_EXIT, "終了")

    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    # 前面化しておかないと、メニュー外クリックで閉じなくなる（Win32の作法）
    user32.SetForegroundWindow(hwnd)
    cmd = user32.TrackPopupMenu(
        menu, TPM_RIGHTBUTTON | TPM_RETURNCMD | TPM_NONOTIFY, pt.x, pt.y, 0, hwnd, None
    )
    user32.PostMessageW(hwnd, 0, 0, 0)  # WM_NULL: メニュー外クリック後の再表示が効かなくなる問題の回避
    user32.DestroyMenu(menu)

    if cmd == ID_TOGGLE_SPEAK:
        toggle_speak()
    elif cmd == ID_STOP:
        speak.stop_playback()
    elif cmd == ID_TOGGLE_SERVE:
        toggle_serve()
    elif cmd == ID_TOGGLE_AUTOSTART:
        if not set_autostart(not autostart_enabled()):
            user32.MessageBoxW(None, "スタートアップへの登録に失敗しました", APP_NAME, MB_ICONINFORMATION)
    elif cmd == ID_SETTINGS:
        _open_settings()
    elif cmd == ID_EXIT:
        user32.DestroyWindow(hwnd)


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    if already_running():
        user32.MessageBoxW(None, f"{APP_NAME}のトレイは既に起動しています", APP_NAME, MB_ICONINFORMATION)
        return

    hinst = kernel32.GetModuleHandleW(None)
    taskbar_created = user32.RegisterWindowMessageW("TaskbarCreated")
    nid_ref: list[NOTIFYICONDATAW] = []

    def wnd_proc(hwnd, msg, wparam, lparam):
        if msg == TRAY_MSG:
            if lparam == WM_RBUTTONUP:
                _show_menu(hwnd)
            elif lparam == WM_LBUTTONDBLCLK:
                _open_settings()
            return 0
        if msg == WM_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            if nid_ref:
                shell32.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(nid_ref[0]))
            user32.PostQuitMessage(0)
            return 0
        if msg == taskbar_created:
            # Explorer が再起動するとトレイが消えるので、載せ直す
            if nid_ref:
                shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid_ref[0]))
            return 0
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    wndproc = WNDPROC(wnd_proc)  # GC されないようローカルに保持（main は常駐中ずっと生きる）
    wc = WNDCLASSW()
    wc.lpfnWndProc = wndproc
    wc.hInstance = hinst
    wc.hCursor = user32.LoadCursorW(None, IDC_ARROW)
    wc.lpszClassName = WINDOW_CLASS
    if not user32.RegisterClassW(ctypes.byref(wc)):
        sys.exit("RegisterClassW failed")

    hwnd = user32.CreateWindowExW(0, WINDOW_CLASS, APP_NAME, 0, 0, 0, 0, 0, None, None, hinst, None)
    if not hwnd:
        sys.exit("CreateWindowExW failed")

    nid = _make_nid(hwnd)
    nid_ref.append(nid)
    if not shell32.Shell_NotifyIconW(NIM_ADD, ctypes.byref(nid)):
        sys.exit("Shell_NotifyIconW failed（トレイに載せられませんでした）")

    print(f"{APP_NAME}: トレイに常駐中（右クリックでメニュー、終了もそこから）", flush=True)
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


if __name__ == "__main__":
    main()
