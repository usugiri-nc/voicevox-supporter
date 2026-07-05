"""ぼいぼサポーター 設定GUI

Usage:
  python gui.py      # 単体起動（トレイの「設定...」からも開く）

依存は Python 標準ライブラリのみ（tkinter）。

キャラクターの画像・名前・規約文は、ユーザー自身がインストールした VOICEVOX エンジンの
公式 API（/speakers, /speaker_info）から実行時に取得して表示するだけで、
このツールには一切同梱しない（再配布しない）。本家のインストールが大前提。
"""

from __future__ import annotations

import base64
import ctypes
import json
import random
import re
import struct
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
import urllib.request
import webbrowser
import zlib
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from tkinter import filedialog
from tkinter import font as tkfont
from tkinter import ttk

sys.path.insert(0, str(Path(__file__).resolve().parent))
import speak

APP_TITLE = "ぼいぼサポーター 設定"
ICON_PATH = Path(__file__).resolve().parent / "icon.ico"  # tray.py と同じ流儀（exe では _internal 内）
OFFICIAL_SITE = "https://voicevox.hiroshiba.jp/"
GUI_MUTEX_NAME = "vvtts_gui"
ERROR_ALREADY_EXISTS = 183

CAST_SLOTS = list("ABCDEFGH")  # 構造上の上限は A〜Z の26。GUI は実用の節度として8枠
UNSET = "（未設定）"

# キャストカードの立ち絵ストリップ表示サイズ（上端固定で顔基準の拡大クロップ。crop_strip 参照）
CARD_W, CARD_H = 100, 230
THUMB = 64  # 選択オーバーレイの正方形サムネイル（公式アイコン 256px を縮小）
PICKER_COLS = 7
PREVIEW_W, PREVIEW_H = 360, 505  # 立ち絵プレビューの枠。原寸がこれを超えるキャラは縮小して収める
DARK = "#161616"     # 選択モーダルの地（いちばん深い黒）

# ダーク基調＋金アクセントの配色（2026-07-03 ご主人様承認の方向性）
BG = "#1f1f1f"        # 全体の基調
BG_RAISED = "#2c2c2c" # 浮いている部品（選択中タブ・ボタン）
FIELD = "#2a2a2a"     # 入力欄・溝の地
FG = "#e6e6e6"
FG_DIM = "#9a9a9a"
ACCENT = "#ffd966"    # 金。ドロップ先ハイライト・選択中タブなどの差し色

# 台本タブ: キャスト枠ごとの行色（ダーク地で読める明度のパステル8色。
# 左端サムネイルの枠文字も同じ色にして、色と枠の対応を凡例なしで見せる）
CAST_COLORS = {
    "A": "#e8b3b3", "B": "#a8c8ea", "C": "#b7e0a5", "D": "#eacfa0",
    "E": "#d4b3ea", "F": "#a5e0d4", "G": "#eab3d6", "H": "#cde08e",
}

# エンジンから取った素材のプロセス内キャッシュ。
# 起動直後にバックグラウンドスレッドが先読みして、選択画面をガタつかせない。
# スレッドから触るのは文字列/辞書だけ（PhotoImage の生成は必ずメインスレッドで行う）。
INFO_CACHE: dict[str, dict] = {}      # speaker_uuid -> /speaker_info 応答
ICON_DATA: dict[str, str] = {}        # speaker_uuid -> アイコンの base64 文字列
PORTRAIT_DATA: dict[str, str] = {}    # speaker_uuid -> 立ち絵の base64 文字列
BBOX_CACHE: dict[str, tuple[int, int, int, int]] = {}  # speaker_uuid -> 立ち絵の不透明部分の外接矩形
ICONS_READY = threading.Event()       # 先読みスレッドがアイコンを配り終えたら立つ

# 擬似縦書き（tkinter に縦書き機能はないため文字を縦に積む）。
# 長音・ダッシュ・括弧類は横書きの字形のまま積むと寝てしまうので、縦書き用の字形に差し替える。
VERTICAL_SUBS = str.maketrans({
    "ー": "｜", "−": "｜", "－": "｜", "―": "｜", "…": "︙",
    "（": "︵", "）": "︶", "「": "﹁", "」": "﹂",
})


def vertical_text(text: str) -> str:
    return "\n".join(text.translate(VERTICAL_SUBS))


def draw_vertical_name(canvas: tk.Canvas, x: int, y: int, name: str, size: int):
    """キャラ名をキャンバス右上 (x=右端, y=上端) に縦書きで描く（影＋白の二度打ち）。
    和文は1文字ずつ縦積み（約物は縦書き字形に置換）。
    英字だけの名前は積むと不自然なので、横書きのまま90度回転する（洋書の背表紙方式）。"""
    font = ("Yu Mincho", size, "bold")
    ascii_only = all(0x20 <= ord(ch) < 0x7F for ch in name)
    for ox, oy, fill in ((2, 2, "#101010"), (0, 0, "#ffffff")):
        if ascii_only:
            canvas.create_text(x + ox, y + oy, text=name, anchor="nw", angle=270,
                               font=font, fill=fill)
        else:
            canvas.create_text(x + ox, y + oy, text=vertical_text(name), anchor="ne",
                               font=font, fill=fill, justify="center")


def already_open() -> bool:
    global _gui_mutex  # プロセス生存中ミューテックスを保持し続ける
    _gui_mutex = ctypes.windll.kernel32.CreateMutexW(None, False, GUI_MUTEX_NAME)
    return ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS


def fetch_json(path: str, query: dict | None = None):
    return json.loads(speak.vv_request(path, query=query))


def fetch_image(field: str) -> str:
    """/speaker_info の画像欄（アイコン・立ち絵）を PhotoImage 用の base64 文字列にする。
    新しめのエンジン(resource_format=url)は URL、古いものは base64 をそのまま返してくる。"""
    if field.startswith("http"):
        raw = urllib.request.urlopen(field, timeout=10).read()
        return base64.b64encode(raw).decode("ascii")
    return field


def get_speaker_info(uuid: str) -> dict:
    if uuid not in INFO_CACHE:
        INFO_CACHE[uuid] = fetch_json(
            "/speaker_info", query={"speaker_uuid": uuid, "resource_format": "url"}
        )
    return INFO_CACHE[uuid]


def preload_assets(speakers: list[dict]):
    """全キャラのアイコンと立ち絵を裏で先読みする（GUI起動直後に開始）。
    先に格子で見えるアイコンを全員分、その後にプレビュー用の立ち絵を集める。"""

    def worker():
        for sp in speakers:
            uuid = sp["speaker_uuid"]
            try:
                info = get_speaker_info(uuid)
                if uuid not in ICON_DATA:
                    ICON_DATA[uuid] = fetch_image(info["style_infos"][0]["icon"])
            except Exception:
                pass
        ICONS_READY.set()
        for sp in speakers:
            uuid = sp["speaker_uuid"]
            try:
                info = get_speaker_info(uuid)
                if uuid not in PORTRAIT_DATA and info.get("portrait"):
                    PORTRAIT_DATA[uuid] = fetch_image(info["portrait"])
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()


def _png(w: int, h: int, scanlines: bytes, color_type: int) -> bytes:
    """フィルタ済み走査線（各行頭に種別バイト0）から PNG を組み立てる（標準ライブラリのみ）。"""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + tag + payload
                + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(scanlines)) + chunk(b"IEND", b""))


def shade_png(w: int, h: int, alpha: int = 140) -> bytes:
    """半透明の黒一色 PNG を生成する（標準ライブラリのみ）。
    Canvas に透過矩形はないが透過 PNG のアルファ合成はできるので、沈み効果はこれを重ねる。"""
    row = b"\x00" + bytes((0, 0, 0, alpha)) * w
    return _png(w, h, row * h, 6)  # 6 = RGBA


def capture_window(widget: tk.Misc) -> tk.PhotoImage | None:
    """ウィンドウの今の見た目を写し取って PhotoImage にする（モーダルの下敷き用）。
    PrintWindow が使えなければ画面からの BitBlt に切り替え、それでもだめなら None
    （呼び元はベタ塗りの暗幕で代替する）。"""
    u32, g32 = ctypes.windll.user32, ctypes.windll.gdi32
    # 64bit でハンドル既定 restype(c_int) に切り詰められる事故防止（tray.py と同じ注意）
    u32.GetDC.restype = ctypes.c_void_p
    u32.GetDC.argtypes = [ctypes.c_void_p]
    u32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    u32.PrintWindow.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint]
    g32.CreateCompatibleDC.restype = ctypes.c_void_p
    g32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    g32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    g32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    g32.SelectObject.restype = ctypes.c_void_p
    g32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    g32.BitBlt.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                           wintypes.DWORD]
    g32.GetDIBits.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
                              ctypes.c_uint, ctypes.c_char_p, ctypes.c_void_p, ctypes.c_uint]
    g32.DeleteObject.argtypes = [ctypes.c_void_p]
    g32.DeleteDC.argtypes = [ctypes.c_void_p]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    hwnd = widget.winfo_id()
    rect = wintypes.RECT()
    u32.GetClientRect(hwnd, ctypes.byref(rect))
    w, h = rect.right, rect.bottom
    if w <= 0 or h <= 0:
        return None

    wdc = u32.GetDC(hwnd)
    mdc = g32.CreateCompatibleDC(wdc)
    bmp = g32.CreateCompatibleBitmap(wdc, w, h)
    old = g32.SelectObject(mdc, bmp)
    try:
        # 3 = PW_CLIENTONLY | PW_RENDERFULLCONTENT（DWM 合成済みの見た目ごと描かせる）
        if not u32.PrintWindow(hwnd, mdc, 3):
            g32.BitBlt(mdc, 0, 0, w, h, wdc, 0, 0, 0x00CC0020)  # SRCCOPY
        g32.SelectObject(mdc, old)
        bi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=w,
                              biHeight=-h, biPlanes=1, biBitCount=32)  # 負の高さ=上から下
        buf = ctypes.create_string_buffer(w * h * 4)
        if not g32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0):
            return None
    finally:
        g32.DeleteObject(bmp)
        g32.DeleteDC(mdc)
        u32.ReleaseDC(hwnd, wdc)

    raw = buf.raw  # BGRA の並び
    if not any(raw):
        return None  # 真っ黒＝描画に失敗している
    stride = w * 3
    rgb = bytearray(w * h * 3)
    rgb[0::3] = raw[2::4]
    rgb[1::3] = raw[1::4]
    rgb[2::3] = raw[0::4]
    scanlines = b"".join(
        b"\x00" + bytes(rgb[y * stride:(y + 1) * stride]) for y in range(h)
    )
    try:
        return tk.PhotoImage(data=base64.b64encode(_png(w, h, scanlines, 2)).decode("ascii"))
    except tk.TclError:
        return None


def alpha_bbox(img: tk.PhotoImage) -> tuple[int, int, int, int]:
    """立ち絵の「絵が描かれている範囲」（不透明ピクセルの外接矩形）を測る。
    透過の余白がキャラごとに大きく違い、そのまま縮小すると豆粒になる子がいるため。
    全画素の走査は Tcl 呼び出しが多すぎるので、粗い縮小版の四辺から端寄せで走査する。"""
    w, h = img.width(), img.height()
    k = max(1, min(w, h) // 48)
    small = img.subsample(k, k) if k > 1 else img
    sw, sh = small.width(), small.height()
    tkc = img.tk

    def opaque(x: int, y: int) -> bool:
        return not tkc.getboolean(tkc.call(small, "transparency", "get", x, y))

    y1 = next((y for y in range(sh) if any(opaque(x, y) for x in range(sw))), -1)
    if y1 < 0:
        return (0, 0, w, h)  # 全面透過（保険）
    y2 = next(y for y in range(sh - 1, -1, -1) if any(opaque(x, y) for x in range(sw)))
    x1 = next(x for x in range(sw) if any(opaque(x, y) for y in range(y1, y2 + 1)))
    x2 = next(x for x in range(sw - 1, -1, -1) if any(opaque(x, y) for y in range(y1, y2 + 1)))
    # 粗走査の取りこぼし対策に、走査解像度1目盛りぶんの余白を戻す
    return (max(0, (x1 - 1) * k), max(0, (y1 - 1) * k),
            min(w, (x2 + 2) * k), min(h, (y2 + 2) * k))


def trim_transparent(img: tk.PhotoImage, uuid: str) -> tk.PhotoImage:
    """立ち絵から透過の余白を取り除いた部分だけのコピーを返す（外接矩形はキャッシュ）。
    元画像は無加工——表示のたびに切り出すだけ。"""
    if uuid not in BBOX_CACHE:
        BBOX_CACHE[uuid] = alpha_bbox(img)
    x1, y1, x2, y2 = BBOX_CACHE[uuid]
    if (x1, y1, x2, y2) == (0, 0, img.width(), img.height()):
        return img
    dst = tk.PhotoImage()
    dst.tk.call(dst, "copy", img, "-from", x1, y1, x2, y2, "-to", 0, 0)
    return dst


# crop_strip の倍率候補（zoom, subsample）。PhotoImage は整数比の拡縮しかできないので、
# この中から「元絵の幅の約45%をカード幅に写す」に一番近いものを選ぶ
STRIP_SCALES = ((1, 1), (3, 4), (2, 3), (1, 2), (2, 5), (1, 3), (1, 4), (1, 5), (1, 6))

# プレビューの倍率候補。透過余白を切った後、枠に収まる最大の倍率を選ぶ（拡大は2倍まで）
PORTRAIT_SCALES = ((2, 1), (3, 2), (4, 3), (5, 4), (1, 1), (4, 5), (3, 4), (2, 3),
                   (1, 2), (2, 5), (1, 3), (1, 4), (1, 5), (1, 6))


def crop_strip(img: tk.PhotoImage) -> tk.PhotoImage:
    """立ち絵から顔が大きく写る縦長ストリップを切り出す。
    上端固定（顔を見切らせない）で、幅の約45%を使う倍率を STRIP_SCALES から選んで拡大クロップ。
    立ち絵の原寸はキャラごとにまちまちなので、固定サイズでなく元絵の幅を基準にする。"""
    w = img.width()
    z, m = min(STRIP_SCALES, key=lambda zm: abs(CARD_W * zm[1] / zm[0] - w * 0.45))
    src_w, src_h = CARD_W * m // z, CARD_H * m // z
    x1 = max(0, (w - src_w) // 2)
    dst = tk.PhotoImage()
    dst.tk.call(dst, "copy", img, "-from", x1, 0,
                min(w, x1 + src_w), min(img.height(), src_h), "-to", 0, 0)
    if z > 1:
        dst = dst.zoom(z, z)
    if m > 1:
        dst = dst.subsample(m, m)
    return dst


class CharacterPicker(tk.Frame):
    """格闘ゲームのキャラクターセレクト風のモーダル（メインウィンドウ内に覆い被せる）。
    下敷きは「開いた瞬間のウィンドウのスナップショット＋半透明黒のベール」——モーダル中は
    後ろを操作できないので、静止画でも本物の透けと見分けが付かない（Toplevel を重ねない方針）。
    ベールは3段階でフェードイン。中身は中央寄せのパネルで、
    左＝正方形サムネイルの格子、右＝ホバー中のキャラの立ち絵プレビュー（名前は縦書きで重ねる）。
    キャラが増えても格子が伸びるだけの拡張性優先。"""

    VEIL_ALPHAS = (60, 115, 170)        # フェードインの黒の濃さ（最後が「濃いめの半透明黒」）
    SETTLE_RELY = (0.53, 0.515, 0.5)    # パネルが下からすっと収まる演出

    thumb_cache: dict[str, tk.PhotoImage] = {}     # speaker_uuid -> サムネ PhotoImage
    portrait_cache: dict[str, tk.PhotoImage] = {}  # speaker_uuid -> 立ち絵（プレビュー枠サイズ）
    _blank_thumb: tk.PhotoImage | None = None

    def __init__(self, tab: "CastTab", slot: str):
        top = tab.winfo_toplevel()
        self._snapshot = capture_window(top)  # 覆い被せる前の姿を写し取っておく
        super().__init__(top, background=DARK)
        self.tab = tab
        self.slot = slot  # 割り当て先の枠。プレビューの「台本での書き方」もこの枠の表記で出す
        # 注意: tkinter は self._w をウィジェットのパス名に使うので、その名前は絶対に使わない
        self._win_w, self._win_h = top.winfo_width(), top.winfo_height()
        self.place(x=0, y=0, relwidth=1.0, relheight=1.0)
        self.lift()

        self.veil_canvas = tk.Canvas(self, background=DARK, highlightthickness=0)
        self.veil_canvas.pack(fill="both", expand=True)
        if self._snapshot is not None:
            self.veil_canvas.create_image(0, 0, image=self._snapshot, anchor="nw")
        self._veil_img: tk.PhotoImage | None = None

        # 中央のモーダルパネル（フェード完了後に _settle が置く）
        panel = tk.Frame(self, background=DARK, highlightthickness=1,
                         highlightbackground="#555555")
        self._panel = panel

        header = tk.Frame(panel, background=DARK)
        header.pack(fill="x", pady=(10, 4))
        # 注意: Segoe UI は日本語グリフを持たず、Tk の代替で縦書き用フォントが選ばれて
        # グリフが90度寝る事故が起きる。日本語を含むラベルは必ず日本語フォントを明示する
        title = tk.Label(header, text=f"キャスト選択（{slot} 枠）", foreground="#eeeeee",
                         background=DARK, font=("Yu Gothic UI", 11, "bold"))
        title.pack(side="left", padx=14)

        body = tk.Frame(panel, background=DARK)
        body.pack(padx=14)

        grid = tk.Frame(body, background=DARK)
        grid.pack(side="left", anchor="n")

        # 現在の配役（キャスト名 -> 枠文字）。サムネイルに枠文字を添えて「もう居る子」を見せる
        assigned = {}
        for s in CAST_SLOTS:
            n = tab.selection.get(s, UNSET)
            if n != UNSET and n not in assigned:
                assigned[n] = s

        self._cell_shade = tk.PhotoImage(
            data=base64.b64encode(shade_png(THUMB, THUMB)).decode("ascii"))
        self._letter_font = tkfont.Font(family="Yu Mincho", size=12, weight="bold")
        cx = THUMB // 2 + 2  # highlightthickness=2 のぶん内側にずれる

        self._cells: list[tuple[tk.Canvas, dict | None]] = []
        cells = [(UNSET, None)] + [(sp["name"], sp) for sp in tab.speakers]
        for i, (name, sp) in enumerate(cells):
            img = self._thumb(sp) if sp else self._blank()
            # 枠線で配役の状態を見せる: 金=current（いま開いている枠の配役。枠が空きなら
            # 先頭の空きセル）、銀=他の枠に配役済み、それ以外は沈んだ細枠
            current = name == tab.selection.get(slot, UNSET)
            letter = assigned.get(name)
            border = ACCENT if current else (FG_DIM if letter else "#3a3a3a")
            cell = tk.Canvas(grid, width=THUMB, height=THUMB, background=DARK,
                             highlightthickness=2, cursor="hand2",
                             highlightbackground=border)
            cell.create_image(cx, cx, image=img)
            cell.image = img
            if letter:
                # カード左下の大文字と同じ流儀（明朝・影付き）。小文字の添字はここでは出さない
                for ox, oy, fill in ((1, 1, "#101010"),
                                     (0, 0, ACCENT if current else "#f2f2f2")):
                    cell.create_text(6 + ox, THUMB + oy, text=letter, anchor="sw",
                                     font=self._letter_font, fill=fill)
            cell.grid(row=i // PICKER_COLS, column=i % PICKER_COLS, padx=3, pady=3)
            cell.bind("<Button-1>", lambda _e, n=name: self._choose(n))
            cell.bind("<Enter>", lambda _e, s=sp: self._hover_cell(s))
            self._cells.append((cell, sp))
        grid.bind("<Leave>", self._unhover)

        # 右側プレビュー: ホバー中のキャラの立ち絵（枠に収まるよう縮小）、名前は縦書きで右上に重ねる
        # （格子のすぐ隣に置く。ウィンドウ幅で左右に泣き別れないよう、広がるのはパネルの外側だけ）
        preview = tk.Frame(body, background=DARK)
        preview.pack(side="left", anchor="n", padx=(16, 0))
        self.preview_canvas = tk.Canvas(preview, width=PREVIEW_W, height=PREVIEW_H,
                                        background=DARK, highlightthickness=0)
        self.preview_canvas.pack()

        # 添字対応とクレジットは横幅いっぱいの1行書き。行数固定（height）でパネルの高さを
        # キャラのスタイル数で伸び縮みさせない（ガタつき対策）
        self.preview_label = tk.Label(panel, text="サムネイルにカーソルを乗せると、立ち絵が出ます",
                                      foreground="#aaaaaa", background=DARK,
                                      font=("Yu Gothic UI", 9), justify="left",
                                      wraplength=860, height=3, anchor="nw")
        self.preview_label.pack(fill="x", padx=14, pady=(6, 10))

        # キャストを選ぶ以外のクリック/タップは、どこであっても「選ばず閉じる」（モーダルの流儀。
        # だから✕ボタンは置かない。サムネイルだけが別扱い＝Button が自分でクリックを受ける）
        for w in (self.veil_canvas, panel, header, title, body, grid,
                  preview, self.preview_canvas, self.preview_label):
            w.bind("<Button-1>", lambda _e: self.close())

        self._esc_bind = top.bind("<Escape>", lambda _e: self.close())
        self._fade()

    def _fade(self, step: int = 0):
        """半透明黒のベールを3段階で濃くしていく（ぱっと出ないための遷移演出）。"""
        if not self.winfo_exists():
            return
        if step < len(self.VEIL_ALPHAS):
            png = shade_png(self._win_w, self._win_h, self.VEIL_ALPHAS[step])
            self._veil_img = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
            self.veil_canvas.delete("veil")
            self.veil_canvas.create_image(0, 0, image=self._veil_img, anchor="nw", tags="veil")
            self.after(35, self._fade, step + 1)
        else:
            self._settle()

    def _settle(self, step: int = 0):
        """ベールが張れたら、パネルをわずかに下から滑り込ませて据える。"""
        if not self.winfo_exists():
            return
        self._panel.place(relx=0.5, rely=self.SETTLE_RELY[step], anchor="center")
        if step + 1 < len(self.SETTLE_RELY):
            self.after(30, self._settle, step + 1)

    @classmethod
    def _blank(cls) -> tk.PhotoImage:
        if cls._blank_thumb is None:
            cls._blank_thumb = tk.PhotoImage(width=THUMB, height=THUMB)
        return cls._blank_thumb

    @classmethod
    def _thumb(cls, sp: dict) -> tk.PhotoImage:
        uuid = sp["speaker_uuid"]
        if uuid not in cls.thumb_cache:
            data = ICON_DATA.get(uuid)  # 先読み済みならネットワークに触らず即生成
            if data is None:
                try:
                    data = fetch_image(get_speaker_info(uuid)["style_infos"][0]["icon"])
                    ICON_DATA[uuid] = data
                except Exception:
                    return cls._blank()
            icon = tk.PhotoImage(data=data)
            cls.thumb_cache[uuid] = icon.subsample(max(1, icon.width() // THUMB))
        return cls.thumb_cache[uuid]

    def _portrait(self, sp: dict) -> tk.PhotoImage | None:
        uuid = sp["speaker_uuid"]
        if uuid not in self.portrait_cache:
            data = PORTRAIT_DATA.get(uuid)
            if data is None:
                try:
                    info = get_speaker_info(uuid)
                    if not info.get("portrait"):
                        return None
                    data = fetch_image(info["portrait"])
                    PORTRAIT_DATA[uuid] = data
                except Exception:
                    return None
            img = trim_transparent(tk.PhotoImage(data=data), uuid)
            # 透過余白を切った上で、枠に収まる最大の倍率で揃える（豆粒キャラ対策。
            # 過度なドット絵化を避けるため拡大は2倍まで）
            cw, ch = img.width(), img.height()
            z, m = max(
                (zm for zm in PORTRAIT_SCALES
                 if cw * zm[0] <= PREVIEW_W * zm[1] and ch * zm[0] <= PREVIEW_H * zm[1]),
                key=lambda zm: zm[0] / zm[1], default=(1, 6),
            )
            if z > 1:
                img = img.zoom(z, z)
            if m > 1:
                img = img.subsample(m, m)
            self.portrait_cache[uuid] = img
        return self.portrait_cache[uuid]

    def _hover_cell(self, sp: dict | None):
        """カード側と同じ流儀: 指しているサムネイル以外に半透明黒を重ねて沈ませる。"""
        cx = THUMB // 2 + 2
        for cell, other in self._cells:
            cell.delete("dim")
            if other is not sp:
                cell.create_image(cx, cx, image=self._cell_shade, tags="dim")
        self._preview(sp)

    def _unhover(self, _e=None):
        """格子から出たら沈みだけ戻す。プレビューは最後のキャラを残す（立ち絵を見に行けるように）。"""
        for cell, _sp in self._cells:
            cell.delete("dim")

    def _preview(self, sp: dict | None):
        canvas = self.preview_canvas
        canvas.delete("all")
        if sp is None:
            self.preview_label.configure(text="（この枠を空きに戻します）")
            return
        img = self._portrait(sp)
        if img is not None:
            # 足元を枠の下端に揃える（背の低い子が宙に浮いて見えないように）
            canvas.create_image(PREVIEW_W // 2, PREVIEW_H, image=img, anchor="s")
        draw_vertical_name(canvas, PREVIEW_W - 10, 8, sp["name"], 16)
        suffixes = "  ".join(
            f"{self.slot}{chr(ord('a') + i)}={st['name']}" for i, st in enumerate(sp["styles"])
        )
        self.preview_label.configure(
            text=f"台本での書き方: {suffixes}　／　クレジット表記: VOICEVOX:{sp['name']}"
        )

    def _choose(self, name: str):
        slot = self.slot
        self.close()
        self.tab.set_slot(slot, name)

    def close(self):
        top = self.winfo_toplevel()
        top.unbind("<Escape>", self._esc_bind)
        self.tab._picker = None
        self.destroy()


class CastTab(ttk.Frame):
    """キャスト8枠を横並びカードで見せる、キャスト選択画面ふうのタブ。
    カードのクリックで選択オーバーレイ、ドラッグ＆ドロップで枠の入れ替え。
    下部は「選択中キャスト全員の利用規約まとめ」（規約以外の話は書かない）。"""

    DRAG_THRESHOLD = 6  # これ未満の移動はクリック扱い

    # テスト再生の掛け合いセリフ（汎用・名前に触れない）。1人なら独白、2人以上は
    # 「開始→中間（人数ぶん）→締め」の構成で、何人でも自然に始まって自然に終わる。
    # 中間の句はどれも自己完結（疑問形で締めに繋がない）にして、人数分岐に耐えるようにする
    TEST_SOLO = "マイクテスト、いち、にー。うん、ちゃんと聞こえてるね"
    TEST_OPENER = "マイクテスト、いち、にー"
    TEST_CLOSER = "それじゃ、テストはここまで"
    TEST_MIDDLE = [
        "はい、こちらも聞こえてます",
        "じゃあ続けて、私も発声テスト",
        "うん、いい感じに聞こえてるよ",
        "こちらの調子もばっちりです",
        "発声練習、あーあー、っと",
        "みんな揃ってるみたいだね",
    ]

    def _test_script(self, n: int) -> list[str]:
        """人数 n（1〜8）に応じたテスト台本のセリフ列を返す。"""
        if n == 1:
            return [self.TEST_SOLO]
        return [self.TEST_OPENER] + self.TEST_MIDDLE[: n - 2] + [self.TEST_CLOSER]

    SPIN_FRAMES = "◐◓◑◒"  # 回転インジケーターのコマ

    def __init__(self, master):
        super().__init__(master, padding=12)
        self.speakers = fetch_json("/speakers")
        self.by_name = {sp["name"]: sp for sp in self.speakers}
        self.images: dict[str, tk.PhotoImage] = {}  # GC されないよう参照を保持
        self.selection: dict[str, str] = {}         # slot -> キャスト名 or UNSET
        self.canvases: dict[str, tk.Canvas] = {}
        self._canvas_slot: dict[tk.Canvas, str] = {}
        self._picker: CharacterPicker | None = None
        self._drag: dict | None = None
        self._test_proc: subprocess.Popen | None = None
        self._test_file: Path | None = None
        self._test_order: list[str] = []  # テスト台本の行順に対応する枠文字
        self._test_now = -1               # いま再生中の行番号（進捗パイプ読取りスレッドが書く）
        self._slot_font = tkfont.Font(family="Yu Mincho", size=26, weight="bold")
        self._sub_font = tkfont.Font(family="Yu Mincho", size=10, weight="bold")
        self._shade = tk.PhotoImage(
            data=base64.b64encode(shade_png(CARD_W + 2, CARD_H + 2)).decode("ascii")
        )

        preload_assets(self.speakers)  # 選択画面をガタつかせないための先読み
        self.after(400, self._warm_thumbs)  # サムネの先作り（初回オープンのもたつき対策）

        cast = speak.load_config().get("cast", {})
        cards = ttk.Frame(self)
        cards.grid(row=0, column=0, sticky="w")
        for col, slot in enumerate(CAST_SLOTS):
            self.selection[slot] = cast.get(slot, {}).get("name", UNSET)
            card = ttk.Frame(cards, padding=3)
            card.grid(row=0, column=col, sticky="n")
            canvas = tk.Canvas(
                card, width=CARD_W, height=CARD_H, highlightthickness=1,
                highlightbackground="#555555", background="#2a2a2a", cursor="hand2",
            )
            canvas.pack()
            canvas.bind("<ButtonPress-1>", lambda e, s=slot: self._press(e, s))
            canvas.bind("<B1-Motion>", self._motion)
            canvas.bind("<ButtonRelease-1>", self._release)
            # 操作できることはテキストで説明せず、カーソルの反応で分からせる（ホバーで他カードが沈む）
            canvas.bind("<Enter>", lambda _e, s=slot: self._hover(s))
            canvas.bind("<Leave>", lambda _e: self._hover(None))
            self.canvases[slot] = canvas
            self._canvas_slot[canvas] = slot
            self._render_card(slot)

        # 下段の並びは「テスト再生 → 連携案内 → 添え物（利用規約＋クレジット）」。
        # 本題はテスト再生と連携共有のふたつ。コピーボタンを持つのは連携案内だけ。
        # 本家のバージョンはウィンドウ最下部の「接続中」フッターが正で、この画面では扱わない
        # （古いエンジンへの更新案内は起動時バナーの仕事。外部通信は一切しない）

        tools = ttk.Frame(self)
        tools.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self._test_btn = ttk.Button(tools, text="テスト再生", command=self.test_play)
        self._test_btn.pack(side="left")
        # 再生中だけ回るインジケーター（tkinter にスピナーは無いので after でグリフを回す）
        self._spin_label = ttk.Label(tools, text="", foreground=ACCENT, width=2,
                                     font=("Yu Gothic UI", 11))
        self._spin_label.pack(side="left", padx=(6, 0))

        # 連携案内: 見出しは置かず、説明ラベル＋横に途切れる1行エリア＋コピーで完結させる
        # （文言のフルテキストはコピーで取れるので、見た目は1行に収める）
        share = ttk.Frame(self)
        share.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        # 連携の入口はこの共有文言ひとつ。フック導入などの配線は、ガイドファイルを
        # 読んだ AI が自分で行う（GUI 側にボタンは置かない。二択の入口を作らない）
        ttk.Label(share, foreground=FG_DIM, font=("Yu Gothic UI", 8), text=(
            "以下のテキストを、ローカル環境で動作する AI（Claude Code や Codex）に伝えてください。"
            "常時参照されるメモへの記載でも構いません。"
        )).pack(anchor="w")
        share_row = ttk.Frame(share)
        share_row.pack(fill="x", pady=(2, 0))
        self.share_text = tk.Text(share_row, height=1, wrap="none", state="disabled",
                                  font=("Yu Gothic UI", 9))
        self.share_text.pack(side="left", fill="x", expand=True)
        self._share_btn = ttk.Button(share_row, text="コピー", command=self.copy_share)
        self._share_btn.pack(side="left", padx=(6, 0))

        # 添え物ふたつ: 左=クレジット表記（幅狭。1行書きの文字列を折り返して収める）、
        # 右=利用規約（読ませる文書ではないので極小フォント＋スクロール）
        info = ttk.Frame(self)
        info.grid(row=3, column=0, sticky="nsew", pady=(12, 0))
        info.columnconfigure(0, weight=1)
        info.columnconfigure(1, weight=4)
        info.rowconfigure(0, weight=1)

        credit_frame = ttk.LabelFrame(info, text="クレジット表記")
        credit_frame.grid(row=0, column=0, sticky="nsew")
        self.credit_text = tk.Text(credit_frame, width=22, wrap="char", state="disabled",
                                   font=("Yu Gothic UI", 8))
        self.credit_text.pack(fill="both", expand=True, padx=4, pady=4)

        policy_frame = ttk.LabelFrame(info, text="利用規約（選択中キャスト全員分）")
        policy_frame.grid(row=0, column=1, sticky="nsew", padx=(10, 0))
        self.policy_text = tk.Text(policy_frame, height=8, wrap="char", state="disabled",
                                   font=("Yu Gothic UI", 7))
        scroll = ttk.Scrollbar(policy_frame, command=self.policy_text.yview)
        self.policy_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.policy_text.pack(fill="both", expand=True, padx=4, pady=4)

        self.columnconfigure(0, weight=1)
        self.rowconfigure(3, weight=1)
        for slot in CAST_SLOTS:
            if self.selection[slot] != UNSET:
                self._load_slot(slot)
        self.refresh_policies()

    def open_picker(self, slot: str):
        if self._picker is None or not self._picker.winfo_exists():
            self._picker = CharacterPicker(self, slot)

    def _warm_thumbs(self):
        """選択画面のサムネイルをメインループの隙間で少しずつ先に作っておく。
        PhotoImage の生成はメインスレッド限定なので、after で数枚ずつ刻む。"""
        if not self.winfo_exists():
            return
        ready = [sp for sp in self.speakers
                 if sp["speaker_uuid"] in ICON_DATA
                 and sp["speaker_uuid"] not in CharacterPicker.thumb_cache][:4]
        for sp in ready:
            CharacterPicker._thumb(sp)
        if ready or not ICONS_READY.is_set():
            self.after(150, self._warm_thumbs)

    # --- カードのクリック/ドラッグ判定（同じ左ボタンで「選択画面」と「入れ替え」を両立する） ---

    def _press(self, event, slot: str):
        self._drag = {"slot": slot, "x": event.x_root, "y": event.y_root, "moved": False, "ghost": None}

    def _motion(self, event):
        if not self._drag:
            return
        if not self._drag["moved"]:
            if (abs(event.x_root - self._drag["x"]) < self.DRAG_THRESHOLD
                    and abs(event.y_root - self._drag["y"]) < self.DRAG_THRESHOLD):
                return
            self._drag["moved"] = True
            self._drag["ghost"] = self._make_ghost(self._drag["slot"])
        if self._drag["ghost"] is not None:
            # カーソルから少しずらす（真下に置くとドロップ先の判定を遮ってしまう）
            self._drag["ghost"].geometry(f"+{event.x_root + 14}+{event.y_root + 10}")
        target = self._slot_at_pointer(event)
        for canvas, slot in self._canvas_slot.items():
            if slot == self._drag["slot"]:
                continue
            gold = slot == target
            canvas.configure(highlightbackground=ACCENT if gold else "#555555")

    def _release(self, event):
        drag, self._drag = self._drag, None
        for canvas in self._canvas_slot:
            canvas.configure(highlightbackground="#555555")
        if drag is None:
            return
        if drag["ghost"] is not None:
            drag["ghost"].destroy()
        if not drag["moved"]:
            self.open_picker(drag["slot"])
            return
        target = self._slot_at_pointer(event)
        if target and target != drag["slot"]:
            self._swap(drag["slot"], target)

    def _make_ghost(self, slot: str) -> tk.Toplevel | None:
        """ドラッグ中カードの半透明コピー。カーソルに追従して「持てている」ことを見せる。"""
        strip = self.images.get(slot)
        if strip is None:
            return None
        ghost = tk.Toplevel(self)
        ghost.overrideredirect(True)
        ghost.attributes("-alpha", 0.7)
        ghost.attributes("-topmost", True)
        tk.Label(ghost, image=strip, borderwidth=0).pack()
        return ghost

    def _hover(self, slot: str | None):
        """ホバー中のカード以外を沈ませて、触れるものだと分からせる（半透明黒PNGを重ねる）。"""
        for canvas, s in self._canvas_slot.items():
            canvas.delete("dim")
            if slot is not None and s != slot:
                canvas.create_image((CARD_W + 2) // 2, (CARD_H + 2) // 2,
                                    image=self._shade, tags="dim")

    def _slot_at_pointer(self, event) -> str | None:
        widget = self.winfo_containing(event.x_root, event.y_root)
        return self._canvas_slot.get(widget)

    def _swap(self, a: str, b: str):
        self.selection[a], self.selection[b] = self.selection[b], self.selection[a]
        self.images[a], self.images[b] = self.images.get(b), self.images.get(a)
        for slot in (a, b):
            if self.images.get(slot) is None:
                self.images.pop(slot, None)
            self._render_card(slot)
        self.refresh_policies()
        self._persist()

    def _render_card(self, slot: str):
        canvas = self.canvases[slot]
        canvas.delete("all")
        name = self.selection.get(slot, UNSET)
        strip = self.images.get(slot)
        if strip is not None:
            canvas.create_image(CARD_W // 2 + 1, CARD_H // 2 + 1, image=strip)
        elif name == UNSET:
            canvas.create_text(CARD_W // 2, CARD_H // 2, text="空き", fill="#777777")
        # キャスト名は右上に縦書きで（左下のスロット表記と対角の構図）
        if name != UNSET:
            draw_vertical_name(canvas, CARD_W - 8, 6, name, 10)
        # 左下コーナーにベタ付けの「A/abc」（スロット文字＋持ち添字の早見。対応表は選択画面が正）。
        # スラッシュと添字は大文字に食い込むくらい字間を詰めて一体に見せる
        parts = [(slot, self._slot_font, -4)]
        sp = self.by_name.get(name)
        if sp:
            subs = "".join(chr(ord("a") + i) for i in range(len(sp["styles"])))
            parts += [("/", self._sub_font, -1), (subs, self._sub_font, 0)]
        for ox, oy, fill in ((2, 2, "#1a1a1a"), (0, 0, "#f2f2f2")):
            x = 4 + ox
            for text, fnt, kern in parts:
                canvas.create_text(x, CARD_H - 2 + oy, text=text, anchor="sw", font=fnt, fill=fill)
                x += fnt.measure(text) + kern

    def _load_slot(self, slot: str):
        """選択中キャラの立ち絵をエンジンから取ってカードを描き直す。"""
        sp = self.by_name.get(self.selection[slot])
        if not sp:
            return
        try:
            info = get_speaker_info(sp["speaker_uuid"])
            if info.get("portrait"):
                full = tk.PhotoImage(data=fetch_image(info["portrait"]))
                # カードも透過余白を切ってから顔基準クロップ（余白の多い立ち絵対策）
                self.images[slot] = crop_strip(trim_transparent(full, sp["speaker_uuid"]))
        except Exception:
            self.images.pop(slot, None)
        self._render_card(slot)

    def set_slot(self, slot: str, name: str):
        """選択画面で選ばれたキャストを枠に割り当てて、即保存する。"""
        name = name if name in self.by_name else UNSET
        if name != UNSET:
            for other, current in self.selection.items():
                if other != slot and current == name:
                    # 移籍方式: 同じキャストは1枠だけ。元の枠は空きに戻す
                    self.selection[other] = UNSET
                    self.images.pop(other, None)
                    self._render_card(other)
        self.selection[slot] = name
        self.images.pop(slot, None)
        if name != UNSET:
            self._load_slot(slot)
        else:
            self._render_card(slot)
        self.refresh_policies()
        self._persist()

    def refresh_policies(self):
        """下段の「利用規約」「クレジット表記」を選択中キャストに合わせて更新する。
        規約欄に規約以外の話（添字対応など）は書かない——それは選択画面のプレビュー側の仕事。"""
        groups: dict[str, list[str]] = {}  # 同じキャストが複数枠にいたら1つにまとめる
        for slot in CAST_SLOTS:
            name = self.selection[slot]
            if name != UNSET and name in self.by_name:
                groups.setdefault(name, []).append(slot)
        parts = []
        for name, slots in groups.items():
            try:
                policy = get_speaker_info(self.by_name[name]["speaker_uuid"]).get("policy", "").strip()
            except Exception:
                policy = "（規約を取得できませんでした）"
            parts.append(f"◆ {'・'.join(slots)}: {name}\n{policy}")
        body = "\n\n".join(parts) if parts else "キャストが選ばれていません。カードをクリックして選んでください。"
        # クレジットは1行書き（動画説明欄などにそのまま貼れる形）: VOICEVOX:名A/名B/名C
        credits = "VOICEVOX:" + "/".join(groups) if groups else "（キャスト未選択）"
        if self._test_proc is None:  # 再生中は「停止」ボタンとして生かしておく
            self._test_btn.configure(state="normal" if groups else "disabled")
        for widget, text in ((self.policy_text, body), (self.credit_text, credits)):
            widget.configure(state="normal")
            widget.delete("1.0", "end")
            widget.insert("1.0", text)
            widget.configure(state="disabled")
        self.refresh_share()  # 共有文言にも配役が入るので一緒に作り直す

    def _write_guide(self) -> Path:
        """連携ガイド（VOICEVOX_SUPPORTER_AI_GUIDE.md）を現在の配役と設定から書き出す。
        共有文言の「詳しくはこのファイル」の飛び先。1行に収まらない話（スタイル対応表・
        記法の詳細・コマンド一覧）はすべてこちらに書く。キャスト変更のたびに作り直す。
        ファイル名は他のメモ類と並んで読み込まれても素性が分かるようフルネームにしてある。"""
        # コマンド例の先頭部分（ソース版=python speak.py／exe版=exe 自身。引用符＋スラッシュ流儀も speak 側で担保）
        cmd = speak.self_cmd_str()
        cfg = speak.load_config()
        markers = speak.get_markers(cfg)
        port = cfg.get("servePort", speak.DEFAULT_SERVE_PORT)
        max_chars = cfg.get("maxChars", speak.MAX_CHARS)
        overflow = cfg.get("overflowText", speak.OVERFLOW_TEXT)
        overflow_note = (f"（超過分は「{overflow}」と読まれます）" if overflow
                         else "（超過分は読まれません）")

        cast_lines = []
        for slot in CAST_SLOTS:
            name = self.selection[slot]
            sp = self.by_name.get(name)
            if sp is None:
                continue
            subs = ", ".join(f"{slot}{chr(ord('a') + i)}={st['name']}"
                             for i, st in enumerate(sp["styles"]))
            cast_lines.append(f"- {slot} = {name}（{subs}）")
        label_note = (
            "- ラベル必須設定が ON のため、キャスト文字のない素の『…』は読み上げられません\n"
            if markers["requireLabel"] else
            "- キャスト文字のない素の『…』も先頭キャスト（A）の声で読み上げられます\n"
        )
        tag_note = ("- `<voice>テキスト</voice>` タグ記法も使えます（`<voice style=\"怒り\">` でスタイル指定）\n"
                    if markers["voiceTag"] else "")
        # exe（窓なし）はコンソールに文字を出せないので、結果を読む status は exe 版では案内しない
        stop_line = (f"- `{cmd} stop` … 再生停止\n" if speak.FROZEN else
                     f"- `{cmd} stop` … 再生停止 ／ `{cmd} status` … 状態確認\n")
        body = (
            "# ぼいぼサポーター 連携ガイド（自動生成）\n\n"
            "このファイルは設定GUIが現在の設定から自動生成します（手で編集しても上書きされます）。\n\n"
            "## これは何\n\n"
            "このPCには「ぼいぼサポーター」（VOICEVOX 連携のCLI音声通知＆台本プレイヤー）と\n"
            "VOICEVOX がインストールされています。専用記法を含むテキストを渡すと VOICEVOX の声で\n"
            "読み上げられます。エンジンは必要時に自動起動するため事前準備は不要です。\n\n"
            # 経路分岐は環境の状態に依存させず常に全部書く（読み手が誰でも・いつ読んでも
            # 同じ内容→AI の挙動が安定する）。「返答と別に原稿を作らない」の念押しは、
            # 実際に AI が言っていないことを声で言い出した経緯があるので削らないこと
            "## 読み上げさせ方（自分に当てはまる経路を使ってください）\n\n"
            "**A. Claude Code のセッションで、Stop フック導入済み** — CLI を呼ぶ必要はありません。\n"
            "返答文の中に記法（下記）で書いたセリフが、応答の完了時に自動で読み上げられます。\n"
            "読み上げは「返答の一部を声にする」仕組みです。返答とは別に読み上げ用の原稿を\n"
            "作らないでください（書いた文とちがうことを声が言うのは望まれていません）。\n"
            "導入済みかは `~/.claude/settings.json` の hooks に `--hook` の登録があるかで分かります。\n\n"
            f"**B. Claude Code のセッションで、フック未導入** — `{cmd} install-hook` を一度実行すると\n"
            "A の自動読み上げが有効になります（既存設定はバックアップされ、二重登録もされないため\n"
            "何度実行しても安全です。有効になるのは次のセッションから）。\n\n"
            "**C. それ以外の AI・スクリプト・他ソフト** — 下記の CLI か HTTP を使ってください。\n"
            "AI が自分で呼ぶときは、読み上げさせる文を自分の返答に書いた文と同じにしてください。\n\n"
            f'- `{cmd} --bg "A『こんにちは』"` … 読み上げ（CLI から呼ぶ場合はこれを推奨。\n'
            "  合成と再生は切り離したプロセスで行われ、コマンドはすぐ返ります）\n"
            f'- `{cmd} "A『こんにちは』"` … `--bg` なしは再生が終わるまで返りません\n'
            "  （呼び出し元プロセスが途中で終了すると再生も止まります）\n"
            f"- `{cmd} play 台本.txt` … 台本ファイルの連続再生（`--out 出力.wav` で書き出し）\n"
            f"- `{cmd} paste` … クリップボードを読み上げ\n"
            f"{stop_line}"
            f"- serve 稼働時は `POST http://127.0.0.1:{port}/talk`（本文=UTF-8テキスト）でも読み上げ可能\n\n"
            "## 台本記法\n\n"
            "- `キャスト文字『セリフ』` … 例 `A『やあ』B『どうも』`（並べると会話劇になる）\n"
            "- 小文字の添字でスタイル指定 … 例 `Ad『ささやき声で』`（対応表は下）\n"
            "- 数字の添字で読み上げ速度（1〜5、2=通常）を指定 … 例 `A1『ゆっくり』`、`Ad1『ささやきでゆっくり』`\n"
            "  （そのセリフは固定速度になる。ゆっくり聞かせたい長文・強調したい一言などに）\n"
            "- ルビ記法 `｜漢字《かな》` … ｜で明示した振り先をルビの読みで発音します\n"
            "  （｜のない `漢字《かな》` は見た目の注記扱い＝《かな》を読み飛ばすだけで発音は変えません）\n"
            f"{label_note}{tag_note}"
            "- コードブロック・インラインコード内の記法は無視されます\n"
            f"- 1セグメント約{max_chars}字まで{overflow_note}\n\n"
            "## 現在のキャスト（枠 = キャスト名（添字=スタイル））\n\n"
            + ("\n".join(cast_lines) or "（未設定）") + "\n"
        )
        path = speak.app_dir() / "VOICEVOX_SUPPORTER_AI_GUIDE.md"
        path.write_text(body, encoding="utf-8")
        return path

    def refresh_share(self):
        """連携用の共有文言を作り直す。1行は案内に徹して、詳細はガイドファイルに書く。"""
        try:
            guide = self._write_guide()
            line = ("このPC環境には「ぼいぼサポーター」ならびに VOICEVOX がインストールされており、"
                    "A『こんにちは』のような記法を用いることで音声読み上げを行うことができます。"
                    f"詳しくは {guide} を参照してください。")
        except OSError:  # 書き込めない環境では従来のコマンド案内だけの1行に退化
            cmd = speak.self_cmd_str()
            line = (f'このPC環境では {cmd} --bg "A『こんにちは』" と実行すると VOICEVOX の声で'
                    f"読み上げることができます（キャスト一覧は {cmd} cast）")
        self.share_text.configure(state="normal")
        self.share_text.delete("1.0", "end")
        self.share_text.insert("1.0", line)
        self.share_text.configure(state="disabled")

    def copy_share(self):
        self.clipboard_clear()
        self.clipboard_append(self.share_text.get("1.0", "end").strip())
        self._share_btn.configure(text="コピーしました")
        self.after(1200, lambda: self._share_btn.configure(text="コピー"))

    def test_play(self):
        """選択中キャストの掛け合い台本で発話テストをする。再生中はボタンが「停止」になる。
        重い「全部合成してから再生」ではなく play のプリフェッチ順次再生に乗せるので、
        1人目の合成が済んだ時点で音が出る（人数が多くても待たされない）。
        配役は即保存なので、play が読む設定ファイルは常に画面と一致している。"""
        if self._test_proc is not None:
            speak.stop_playback()  # play は PID 登録済みなので stop で殺せる
            self._test_done()
            return
        filled = [s for s in CAST_SLOTS if self.selection[s] != UNSET]
        if not filled:
            return
        script = "\n".join(
            f"{slot}『{line}』" for slot, line in zip(filled, self._test_script(len(filled)))
        )
        self._test_file = Path(tempfile.gettempdir()) / "vvtts_gui_test.txt"
        self._test_file.write_text(script, encoding="utf-8")
        self._test_order = filled
        self._test_now = -1
        # -u で子プロセスの print を無バッファにして、順次再生の進捗表示 [n/N] を
        # パイプ越しにリアルタイムで受け取る（＝いま何行目かが分かる）
        self._test_proc = subprocess.Popen(
            speak.self_cmd("play", str(self._test_file), unbuffered=True),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        threading.Thread(target=self._pump_test_progress, args=(self._test_proc,),
                         daemon=True).start()
        self._test_btn.configure(text="停止", style="Playing.TButton")
        self._watch_test()

    def _pump_test_progress(self, proc: subprocess.Popen):
        """再生プロセスの進捗行 [n/N] を読み続ける裏方スレッド。
        Tk には触らず「いま何行目か」の整数を置くだけ（UI 反映は _watch_test の仕事）。"""
        try:
            for raw in proc.stdout:
                m = re.match(rb"\[(\d+)/", raw)
                if m:
                    self._test_now = int(m.group(1)) - 1
        except Exception:
            pass

    def _watch_test(self, tick: int = 0):
        """再生中のアニメーション兼終了検知。インジケーターを回し、発話中キャストを光らせる。"""
        if not self.winfo_exists():
            return
        if self._test_proc is not None and self._test_proc.poll() is None:
            self._spin_label.configure(text=self.SPIN_FRAMES[tick % len(self.SPIN_FRAMES)])
            if self._drag is None:  # ドラッグ中はドロップ先の金枠表示に譲る
                idx = self._test_now
                self._light_cast(self._test_order[idx]
                                 if 0 <= idx < len(self._test_order) else None)
            self.after(120, self._watch_test, tick + 1)
            return
        self._test_done()

    def _light_cast(self, slot: str | None):
        """発話中キャストのカード枠を金に光らせる（D&Dのドロップ先ハイライトと同じ言語）。"""
        for s, canvas in self.canvases.items():
            canvas.configure(highlightbackground=ACCENT if s == slot else "#555555")

    def _test_done(self):
        """テスト再生の後始末（自然終了・停止ボタン・多重呼び出しのどれでも安全）。"""
        self._test_proc = None
        if self._test_file is not None:
            self._test_file.unlink(missing_ok=True)
            self._test_file = None
        self._light_cast(None)
        self._spin_label.configure(text="")
        filled = any(self.selection[s] != UNSET for s in CAST_SLOTS)
        self._test_btn.configure(text="テスト再生", style="TButton",
                                 state="normal" if filled else "disabled")

    def _persist(self):
        """現在の8枠を設定ファイルに書く。キャスト操作は軽いので保存ボタンなしの即保存。"""
        cfg = speak.load_config()
        cast = cfg.get("cast", {})
        for slot in CAST_SLOTS:
            name = self.selection[slot]
            if name == UNSET or name not in self.by_name:
                cast.pop(slot, None)
                continue
            sp = self.by_name[name]
            styles = {st["name"]: st["id"] for st in sp["styles"]}
            first = next(iter(styles))
            # CLI の cast set と同じ形（speak.py が読む正式なスナップショット形式）
            cast[slot] = {"name": name, "styles": styles, "style": first, "speaker": styles[first]}
        cfg["cast"] = cast
        speak.save_config(cfg)


class DropMenu(tk.Frame):
    """ボタン直下に浮かべる自前のドロップダウン。Windows の tk.Menu はネイティブ描画で
    ダークテーマの指定色が効かない（ホバーが白地に白文字で消える）ため、色を自分で持つ。
    ホバー行は金地に濃色文字（Playing ボタンと同じ言語）。選択肢以外のクリックか
    Escape で閉じる（キャスト選択モーダルと同じ流儀）。"""

    def __init__(self, anchor: tk.Widget, items: list[tuple[str, object]]):
        top = anchor.winfo_toplevel()
        super().__init__(top, background=BG_RAISED, highlightthickness=1,
                         highlightbackground="#666666")
        self.anchor = anchor
        self._top = top
        for label, cmd in items:
            row = tk.Label(self, text=label, background=BG_RAISED, foreground=FG,
                           font=("Yu Gothic UI", 9), anchor="w", padx=12, pady=6,
                           cursor="hand2")
            row.pack(fill="x")
            row.bind("<Enter>", lambda _e, r=row: r.configure(
                background=ACCENT, foreground="#1f1f1f"))
            row.bind("<Leave>", lambda _e, r=row: r.configure(
                background=BG_RAISED, foreground=FG))
            row.bind("<ButtonRelease-1>", lambda _e, c=cmd: self._pick(c))
        self.place(x=anchor.winfo_rootx() - top.winfo_rootx(),
                   y=anchor.winfo_rooty() - top.winfo_rooty() + anchor.winfo_height() + 2)
        self.lift()
        self._press_bind = top.bind("<ButtonPress-1>", self._outside, add="+")
        self._esc_bind = top.bind("<Escape>", lambda _e: self.close(), add="+")

    def _pick(self, cmd):
        self.close()
        cmd()

    def _outside(self, event):
        w = event.widget
        if w is self.anchor:
            return  # ボタン自身のクリックはトグル（ボタン側の command が閉じる）
        if isinstance(w, tk.Misc) and str(w).startswith(str(self)):
            return  # メニューの中は各行のハンドラに任せる
        self.close()

    def close(self):
        if not self.winfo_exists():
            return
        self._top.unbind("<ButtonPress-1>", self._press_bind)
        self._top.unbind("<Escape>", self._esc_bind)
        self.destroy()


class StylePicker(tk.Frame):
    """台本タブ用: キャストのスタイルを選ぶモーダル。CharacterPicker と同じ
    「スナップショット＋ベール＋中央パネル」の流儀。左にスタイル一覧、右に立ち絵。"""

    VEIL_ALPHAS = (60, 115, 170)
    SETTLE_RELY = (0.53, 0.515, 0.5)
    STYLE_ICON_SIZE = 48

    def __init__(self, script_tab: "ScriptTab", slot: str, sp: dict):
        top = script_tab.winfo_toplevel()
        self._snapshot = capture_window(top)
        super().__init__(top, background=DARK)
        self.script_tab = script_tab
        self.slot = slot
        self.sp = sp
        self._win_w, self._win_h = top.winfo_width(), top.winfo_height()
        self.place(x=0, y=0, relwidth=1.0, relheight=1.0)
        self.lift()

        self.veil_canvas = tk.Canvas(self, background=DARK, highlightthickness=0)
        self.veil_canvas.pack(fill="both", expand=True)
        if self._snapshot is not None:
            self.veil_canvas.create_image(0, 0, image=self._snapshot, anchor="nw")
        self._veil_img: tk.PhotoImage | None = None

        panel = tk.Frame(self, background=DARK, highlightthickness=1,
                         highlightbackground="#555555")
        self._panel = panel

        header = tk.Frame(panel, background=DARK)
        header.pack(fill="x", pady=(10, 4))
        cast_cfg = speak.load_config().get("cast", {}).get(slot, {})
        title_text = f"スタイル選択（{slot} = {cast_cfg.get('name', sp['name'])}）"
        title = tk.Label(header, text=title_text, foreground="#eeeeee",
                         background=DARK, font=("Yu Gothic UI", 11, "bold"))
        title.pack(side="left", padx=14)

        body = tk.Frame(panel, background=DARK)
        body.pack(padx=14, pady=(4, 10))

        # 左: スタイル一覧
        style_list = tk.Frame(body, background=DARK)
        style_list.pack(side="left", anchor="n")

        uuid = sp["speaker_uuid"]
        info = get_speaker_info(uuid)
        style_infos = info.get("style_infos", [])
        styles = sp.get("styles", [])
        current_suffix = script_tab._active_style.get(slot, "")

        self._style_icons: list[tk.PhotoImage] = []
        self._rows: list[tk.Frame] = []

        for i, st in enumerate(styles):
            suffix = chr(ord("a") + i)
            is_current = suffix == current_suffix or (current_suffix == "" and i == 0)

            row = tk.Frame(style_list, background=DARK, cursor="hand2", padx=6, pady=4)
            row.pack(fill="x", pady=1)
            self._rows.append(row)

            icon_img = None
            if i < len(style_infos):
                try:
                    data = fetch_image(style_infos[i]["icon"])
                    raw = tk.PhotoImage(data=data)
                    icon_img = raw.subsample(max(1, raw.width() // self.STYLE_ICON_SIZE))
                    self._style_icons.append(icon_img)
                except Exception:
                    pass
            if icon_img is None:
                icon_img = tk.PhotoImage(width=self.STYLE_ICON_SIZE,
                                         height=self.STYLE_ICON_SIZE)
                self._style_icons.append(icon_img)

            icon_lbl = tk.Label(row, image=icon_img, background=DARK, borderwidth=0)
            icon_lbl.pack(side="left", padx=(0, 8))

            suffix_display = f"{slot}{suffix}" if i > 0 else f"{slot}"
            label_text = f"{st['name']}　（{suffix_display}『…』）"
            text_lbl = tk.Label(row, text=label_text, background=DARK, foreground=FG,
                                font=("Yu Gothic UI", 10), anchor="w")
            text_lbl.pack(side="left", fill="x")

            if is_current:
                row.configure(background=BG_RAISED)
                for child in row.winfo_children():
                    child.configure(background=BG_RAISED)

            for widget in (row, icon_lbl, text_lbl):
                widget.bind("<Enter>", lambda _e, r=row, idx=i: self._hover_row(r, idx))
                widget.bind("<Leave>", lambda _e, r=row, idx=i: self._unhover_row(r, idx))
                widget.bind("<Button-1>", lambda _e, idx=i: self._choose(idx))

        # 右: 立ち絵プレビュー
        preview = tk.Frame(body, background=DARK)
        preview.pack(side="left", anchor="n", padx=(20, 0))
        self.preview_canvas = tk.Canvas(preview, width=PREVIEW_W, height=PREVIEW_H,
                                        background=DARK, highlightthickness=0)
        self.preview_canvas.pack()
        self._show_portrait()

        for w in (self.veil_canvas, panel, header, title, body,
                  style_list, preview, self.preview_canvas):
            w.bind("<Button-1>", lambda _e: self.close())
        self._esc_bind = top.bind("<Escape>", lambda _e: self.close())
        self._fade()

    def _show_portrait(self):
        uuid = self.sp["speaker_uuid"]
        img = None
        if uuid in CharacterPicker.portrait_cache:
            img = CharacterPicker.portrait_cache[uuid]
        else:
            data = PORTRAIT_DATA.get(uuid)
            if data is None:
                try:
                    info = get_speaker_info(uuid)
                    if info.get("portrait"):
                        data = fetch_image(info["portrait"])
                        PORTRAIT_DATA[uuid] = data
                except Exception:
                    pass
            if data:
                raw = trim_transparent(tk.PhotoImage(data=data), uuid)
                cw, ch = raw.width(), raw.height()
                z, m = max(
                    (zm for zm in PORTRAIT_SCALES
                     if cw * zm[0] <= PREVIEW_W * zm[1] and ch * zm[0] <= PREVIEW_H * zm[1]),
                    key=lambda zm: zm[0] / zm[1], default=(1, 6),
                )
                if z > 1:
                    raw = raw.zoom(z, z)
                if m > 1:
                    raw = raw.subsample(m, m)
                CharacterPicker.portrait_cache[uuid] = raw
                img = raw
        if img is not None:
            self.preview_canvas.create_image(PREVIEW_W // 2, PREVIEW_H,
                                             image=img, anchor="s")
            self._portrait_ref = img
        draw_vertical_name(self.preview_canvas, PREVIEW_W - 10, 8, self.sp["name"], 16)

    def _hover_row(self, row: tk.Frame, idx: int):
        row.configure(background="#3a3a3a")
        for child in row.winfo_children():
            child.configure(background="#3a3a3a")

    def _unhover_row(self, row: tk.Frame, idx: int):
        current_suffix = self.script_tab._active_style.get(self.slot, "")
        is_current = (chr(ord("a") + idx) == current_suffix
                      or (current_suffix == "" and idx == 0))
        bg = BG_RAISED if is_current else DARK
        row.configure(background=bg)
        for child in row.winfo_children():
            child.configure(background=bg)

    def _choose(self, idx: int):
        suffix = chr(ord("a") + idx) if idx > 0 else ""
        self.script_tab._set_active_style(self.slot, suffix)
        self.close()

    def _fade(self, step: int = 0):
        if not self.winfo_exists():
            return
        if step < len(self.VEIL_ALPHAS):
            png = shade_png(self._win_w, self._win_h, self.VEIL_ALPHAS[step])
            self._veil_img = tk.PhotoImage(data=base64.b64encode(png).decode("ascii"))
            self.veil_canvas.delete("veil")
            self.veil_canvas.create_image(0, 0, image=self._veil_img, anchor="nw", tags="veil")
            self.after(35, self._fade, step + 1)
        else:
            self._settle()

    def _settle(self, step: int = 0):
        if not self.winfo_exists():
            return
        self._panel.place(relx=0.5, rely=self.SETTLE_RELY[step], anchor="center")
        if step + 1 < len(self.SETTLE_RELY):
            self.after(30, self._settle, step + 1)

    def close(self):
        top = self.winfo_toplevel()
        top.unbind("<Escape>", self._esc_bind)
        self.script_tab._style_picker = None
        self.destroy()


class ScriptTab(ttk.Frame):
    """台本タブ。左端=登録キャストの正方形サムネイル縦列（ドラッグ元）、右=台本の入力欄。
    行ごとに記法を解析してキャスト色で染め、サムネイルを行へドロップすると
    その行のテキストに記法を書き込む（割り当てはテキストそのもの＝画面の文がすべて。
    見えないメタ情報は持たない）。再生・WAV書き出しは play のプリフェッチ順次再生に乗せ、
    本家 VOICEVOX の「テキスト読み込み」形式への書き出し（移行口）も持つ。"""

    DRAG_THRESHOLD = 6
    SPIN_FRAMES = "◐◓◑◒"
    DRAFT_PATH = Path.home() / ".voicevox-tts.draft.txt"  # 入力欄の中身はアプリを閉じても残す

    # 行種別 -> Text タグ（cast はキャスト色なので個別、empty はタグなし）
    TAG_OF = {"other": "otherread", "default": "defaultread", "silent": "silent",
              "code": "silent", "warn": "warn"}

    # サンプル台本: 青空文庫収録の著作権保護期間満了作品（パブリックドメイン）の冒頭。
    # 記法なしの散文にしてあるのは、D&D での配役づけをそのまま練習できるようにするため。
    # 読みの危ない漢字には ｜振り先《よみ》 のルビを振ってある（｜蟋蟀《きりぎりす》・
    # ｜理《ことわり》など）——ルビ機能のデモを兼ねる。難読漢字だけでなく、エンジンが
    # 誤読する普通の語にも振る（｜四五人《しごにん》→よんじゅうごにん、｜円柱《まるばしら》→
    # えんちゅう 等。全行の読みは /audio_query の kana で検分済み・2026-07-05）
    TEST_SCRIPTS = {
        "吾輩は猫である（夏目漱石）": [
            "｜吾輩《わがはい》は猫である。名前はまだ無い。",
            "どこで生れたかとんと｜見当《けんとう》がつかぬ。",
            "何でも薄暗いじめじめした所でニャーニャー泣いていた事だけは記憶している。",
            "｜吾輩《わがはい》はここで始めて人間というものを見た。",
            "しかもあとで聞くとそれは書生という人間中で一番｜獰悪《どうあく》な種族であったそうだ。",
        ],
        "走れメロス（太宰治）": [
            "メロスは激怒した。",
            "必ず、かの｜邪智暴虐《じゃちぼうぎゃく》の王を除かなければならぬと決意した。",
            "メロスには政治がわからぬ。メロスは、村の｜牧人《ぼくじん》である。",
            "笛を吹き、羊と遊んで暮して来た。",
            "けれども邪悪に対しては、人一倍に敏感であった。",
        ],
        "羅生門（芥川龍之介）": [
            "ある日の｜暮方《くれがた》の事である。",
            "一人の｜下人《げにん》が、羅生門の下で雨やみを待っていた。",
            "広い門の下には、この男のほかに誰もいない。",
            "ただ、所々｜丹塗《にぬり》の｜剥《は》げた、大きな｜円柱《まるばしら》に、｜蟋蟀《きりぎりす》が一匹とまっている。",
        ],
        "銀河鉄道の夜（宮沢賢治）": [
            "「ではみなさんは、そういうふうに川だと｜云《い》われたり、乳の流れたあとだと｜云《い》われたりしていた、このぼんやりと白いものがほんとうは何かご承知ですか。」",
            "先生は、黒板に｜吊《つる》した大きな黒い星座の図の、上から下へ白くけぶった銀河帯のようなところを指しながら、みんなに｜問《とい》をかけました。",
            "カムパネルラが手をあげました。それから｜四五人《しごにん》｜手《て》をあげました。",
            "ジョバンニも手をあげようとして、急いでそのままやめました。",
        ],
        "平家物語（冒頭）": [
            "｜祇園精舎《ぎおんしょうじゃ》の鐘の声、｜諸行無常《しょぎょうむじょう》の響きあり。",
            "｜沙羅双樹《しゃらそうじゅ》の花の色、｜盛者必衰《じょうしゃひっすい》の｜理《ことわり》をあらはす。",
            "おごれる人も久しからず、ただ春の｜夜《よ》の夢のごとし。",
            "たけき｜者《もの》も｜遂《つい》にはほろびぬ、ひとへに風の前の｜塵《ちり》に同じ。",
        ],
    }

    def __init__(self, master):
        super().__init__(master, padding=12)
        self.speakers = fetch_json("/speakers")
        self.by_name = {sp["name"]: sp for sp in self.speakers}
        # 本家書き出し用: スピーカーID -> (キャラ名, スタイル名)
        self._id_names = {st["id"]: (sp["name"], st["name"])
                          for sp in self.speakers for st in sp.get("styles", [])}
        self._proc: subprocess.Popen | None = None
        self._proc_file: Path | None = None
        self._proc_kind = ""              # "play" か "wav"
        self._seg_lines: list[int] = []   # 進捗 [n/N] の n -> 入力欄の行番号
        self._slot_by_line: dict[int, str | None] = {}
        self._now = -1                    # いま再生中のセグメント番号（パイプ読取りスレッドが書く）
        self._drag: dict | None = None
        self._parse_timer: str | None = None
        self._line_info: list[tuple[str, str | None, int]] = []
        self._letter_font = tkfont.Font(family="Yu Mincho", size=11, weight="bold")
        self._active_style: dict[str, str] = {}  # slot -> スタイル添字（""=先頭スタイル）
        self._style_picker: StylePicker | None = None
        self._hover_slot: str | None = None

        # --- 上段ツールバー: 左=音が出る系（再生・WAV）、右=テキストの入出力系 ---
        bar = ttk.Frame(self)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        self._play_btn = ttk.Button(bar, text="最初から再生", command=self.play_all)
        self._play_btn.pack(side="left")
        self._here_btn = ttk.Button(bar, text="選択行から再生", command=self.play_from_cursor)
        self._here_btn.pack(side="left", padx=(6, 0))
        self._wav_btn = ttk.Button(bar, text="WAV書き出し", command=self.export_wav)
        self._wav_btn.pack(side="left", padx=(6, 0))
        self._spin = ttk.Label(bar, text="", foreground=ACCENT, width=2,
                               font=("Yu Gothic UI", 11))
        self._spin.pack(side="left", padx=(6, 0))
        # テスト台本と自動割り振りはドロップダウン（tk.Menu はダーク配色が効かないので自前の
        # DropMenu。▾ はボタン文字の一部なので文字色と一緒に描かれる）
        self._menu: DropMenu | None = None
        # 「サンプル台本」——キャストタブの「テスト再生」と紛れないよう「テスト」の語は使わない
        sample_btn = ttk.Button(bar, text="サンプル台本 ▾")
        sample_btn.configure(command=lambda: self._drop_menu(
            sample_btn, [(t, lambda t=t: self._insert_sample(t)) for t in self.TEST_SCRIPTS]))
        sample_btn.pack(side="left", padx=(6, 0))
        # キャスト割り振り: 台本がただのテキストだからできる一括の配役（本家のセル単位編集との差別化）
        self._assign_btn = ttk.Button(bar, text="キャスト割り振り ▾")
        self._assign_btn.configure(command=lambda: self._drop_menu(self._assign_btn, [
            ("未設定の行だけ割り振り", lambda: self.auto_assign(shuffle=False, unset_only=True)),
            ("全行を順に割り振り", lambda: self.auto_assign(shuffle=False)),
            ("全行をランダムに割り振り", lambda: self.auto_assign(shuffle=True)),
        ]))
        self._assign_btn.pack(side="left", padx=(6, 0))
        # テキストの入出力は右側に一族でまとめる（読み込み→保存→本家書き出し。どれも txt の話）
        self._vvox_btn = ttk.Button(bar, text="本家VOICEVOX用に書き出し",
                                    command=self.export_voicevox)
        self._vvox_btn.pack(side="right")
        self._save_btn = ttk.Button(bar, text="テキスト保存", command=self.save_file)
        self._save_btn.pack(side="right", padx=(0, 6))
        ttk.Button(bar, text="テキスト読み込み", command=self.open_file
                   ).pack(side="right", padx=(0, 6))

        # --- 左端: キャストの縦列。タブを開くたびに配役を取り直す ---
        self._strip = ttk.Frame(self)
        self._strip.grid(row=1, column=0, sticky="n", pady=(10, 0))
        self._strip_cells: dict[str, tk.Canvas] = {}

        body = ttk.Frame(self)
        body.grid(row=1, column=1, sticky="nsew", padx=(10, 0), pady=(10, 0))
        cfg = speak.load_config()
        self._text_font = tkfont.Font(family="Yu Gothic UI",
                                      size=int(cfg.get("scriptFontSize", 11)))
        # spacing1/3 で1行ごとに上下の余白を取り、行をブロックとして見せる
        # （ドロップの狙いを付けやすくする。折り返し行間 spacing2 は詰めたまま＝1ブロック感を保つ）
        self.text = tk.Text(body, wrap="word", undo=True, font=self._text_font,
                            spacing1=5, spacing3=5, padx=10, pady=8)
        scroll = ttk.Scrollbar(body, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        foot = ttk.Frame(self)
        foot.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        self._note = ttk.Label(foot, text="", foreground=FG_DIM, font=("Yu Gothic UI", 8))
        self._note.pack(side="left")
        # 文字サイズは右端に小さく（変更は即保存）
        self._font_var = tk.IntVar(value=self._text_font.cget("size"))
        tk.Spinbox(foot, textvariable=self._font_var, from_=8, to=24, increment=1,
                   width=4).pack(side="right")
        ttk.Label(foot, text="文字サイズ", foreground=FG_DIM,
                  font=("Yu Gothic UI", 8)).pack(side="right", padx=(0, 6))
        self._font_var.trace_add("write", self._apply_font_size)

        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        for slot, color in CAST_COLORS.items():
            self.text.tag_configure(f"cast_{slot}", foreground=color)
        self.text.tag_configure("otherread", foreground=FG)          # 読まれるがキャスト外の声
        self.text.tag_configure("defaultread", foreground=FG)        # 記法なし台本の全行読み
        self.text.tag_configure("silent", foreground=FG_DIM)         # 読まれない行（ト書き・地の文）
        self.text.tag_configure("warn", foreground="#e07a7a", underline=True)  # 記法ミスの疑い
        self.text.tag_configure("hoverhighlight", background="#2a2a3d")  # 左アイコン hover 中の対応行
        self.text.tag_configure("nowline", background="#54430f")     # 発話中の行
        self.text.tag_configure("droptarget", background="#3d3520")  # ドラッグ中のドロップ先
        # ルビの可視化（キャスト色より後に作る＝前景色で勝つ）: 下線=｜指定の振り先（発音に効く）、
        # 薄色=｜と《よみ》の記号部分と、｜なしの注記（発音に効かない）。効く/効かないが目で分かる
        self.text.tag_configure("rubytarget", underline=True)
        self.text.tag_configure("rubynote", foreground=FG_DIM)

        if self.DRAFT_PATH.exists():
            try:
                self.text.insert("1.0", self.DRAFT_PATH.read_text(encoding="utf-8"))
                self.text.edit_reset()  # 下書きの流し込みはアンドゥ履歴に積まない
            except OSError:
                pass
        self.text.edit_modified(False)
        self.text.bind("<<Modified>>", self._on_modified)
        # タブを開くたびに配役と色を取り直す（キャストタブでの変更を反映）
        self.bind("<Map>", lambda _e: (self._refresh_strip(), self._reparse()))

    def _drop_menu(self, anchor: tk.Widget, items: list):
        """ドロップダウンのトグル。同じボタンなら閉じる、別のボタンなら掛け替える。"""
        reopen = not (self._menu is not None and self._menu.winfo_exists()
                      and self._menu.anchor is anchor)
        if self._menu is not None and self._menu.winfo_exists():
            self._menu.close()
        self._menu = DropMenu(anchor, items) if reopen else None

    def _apply_font_size(self, *_):
        try:
            size = max(8, min(24, int(self._font_var.get())))
        except (tk.TclError, ValueError):
            return  # 入力途中（空欄など）は無視
        self._text_font.configure(size=size)
        cfg = speak.load_config()
        cfg["scriptFontSize"] = size
        speak.save_config(cfg)

    def _insert_sample(self, title: str):
        """テスト台本を差し込む。既に書きかけがあれば消さず、末尾に足す（Ctrl+Z で戻せる）。"""
        block = "\n".join(self.TEST_SCRIPTS[title]) + "\n"
        self.text.edit_separator()
        if self.text.get("1.0", "end-1c").strip():
            tail = self.text.get("end-2c", "end-1c")  # 既存文との間に空行をひとつ挟む
            self.text.insert("end", ("\n" if tail == "\n" else "\n\n") + block)
        else:
            self.text.delete("1.0", "end")
            self.text.insert("1.0", block)
        self.text.see("end")

    # --- 行の解析と色分け ---

    def _env(self) -> dict:
        """解析・再生に使う現在の設定ひとそろい。slot_of はスピーカーID -> キャスト枠。"""
        cfg = speak.load_config()
        cast = cfg.get("cast", {})
        return {
            "styles": cfg.get("styles", {}),
            "speaker": cfg.get("speaker", speak.DEFAULT_SPEAKER),
            "markers": speak.get_markers(cfg),
            "cast": cast,
            "slot_of": {sid: slot for slot, m in cast.items()
                        for sid in m.get("styles", {}).values()},
        }

    def _classify(self, lines: list[str], env: dict) -> list[tuple[str, str | None, int]]:
        """各行を (種別, キャスト枠 or None, セグメント数) にする。種別は cast / other /
        default / silent / warn / empty。play が読むのと同じ抽出関数に1行ずつ通すので、
        色分けは「実際に読まれるか」の答え合わせになる。"""
        ob, cb = env["markers"].get("brackets") or ["『", "』"]
        labelish = re.compile(r"(?<![A-Za-z0-9])[A-Z][a-z]?[1-5]?" + re.escape(ob))

        # コードブロックの中は読まれない（play と同じ CODE_BLOCK_RE で行を割り出す）
        text_all = "\n".join(lines)
        code_lines: set[int] = set()
        for m in speak.CODE_BLOCK_RE.finditer(text_all):
            start = text_all.count("\n", 0, m.start()) + 1
            end = text_all.count("\n", 0, m.end()) + 1
            code_lines.update(range(start, end + 1))

        info: list[tuple[str, str | None, int]] = []
        for i, line in enumerate(lines, 1):
            if not line.strip():
                info.append(("empty", None, 0))
                continue
            if i in code_lines:
                info.append(("code", None, 0))
                continue
            segs = speak.extract_voice_segments(
                line, env["styles"], env["speaker"], env["markers"], env["cast"])
            if segs:
                slot = env["slot_of"].get(segs[0][1])
                info.append(("cast" if slot else "other", slot, len(segs)))
            elif line.count(ob) != line.count(cb) or labelish.search(line):
                # 括弧が開きっぱなし・ラベル風なのに読まれない（枠未登録の文字など）
                info.append(("warn", None, 0))
            else:
                info.append(("silent", None, 0))

        # 記法が1件も無い台本は play が全行を既定の声で読む（extract_script_segments の挙動）
        if not any(kind in ("cast", "other") for kind, _s, _c in info):
            info = [("default", None, 1) if kind in ("silent", "warn") else (kind, slot, cnt)
                    for kind, slot, cnt in info]
        return info

    def _on_modified(self, _e=None):
        if not self.text.edit_modified():
            return
        self.text.edit_modified(False)
        if self._parse_timer is not None:
            self.after_cancel(self._parse_timer)
        self._parse_timer = self.after(300, self._reparse)

    def _reparse(self):
        """全行を解析し直して色を塗る。ついでに下書きを自動保存する。"""
        self._parse_timer = None
        env = self._env()
        content = self.text.get("1.0", "end-1c")
        lines = content.split("\n")
        self._line_info = self._classify(lines, env)
        for tag in ("otherread", "defaultread", "silent", "warn", "rubytarget", "rubynote",
                    *(f"cast_{s}" for s in CAST_COLORS)):
            self.text.tag_remove(tag, "1.0", "end")
        reads = warns = 0
        for i, (kind, slot, cnt) in enumerate(self._line_info, 1):
            tag = f"cast_{slot}" if kind == "cast" and slot in CAST_COLORS else self.TAG_OF.get(kind)
            if tag:
                self.text.tag_add(tag, f"{i}.0", f"{i}.end")
            reads += 1 if cnt else 0
            warns += kind == "warn"
            # ルビの可視化。｜指定は振り先に下線（発音に効く）、記号と読みは薄く。
            # ｜のない 漢字《よみ》 は全体を薄く（見た目の注記＝発音に効かない）
            line = lines[i - 1]
            for m in speak.RUBY_RE.finditer(line):
                self.text.tag_add("rubynote", f"{i}.{m.start()}", f"{i}.{m.start(1)}")
                self.text.tag_add("rubytarget", f"{i}.{m.start(1)}", f"{i}.{m.end(1)}")
                self.text.tag_add("rubynote", f"{i}.{m.end(1)}", f"{i}.{m.end()}")
            for m in speak.RUBY_NOTE_RE.finditer(line):
                self.text.tag_add("rubynote", f"{i}.{m.start()}", f"{i}.{m.end()}")
        if content.strip():
            note = f"読み上げ対象: {reads}行"
            if warns:
                note += f"　／　⚠ 読めない記法が {warns}行あります（赤い下線）"
        else:
            # 空っぽの画面だけは最初の一歩を言葉で示す（初見の人がここで止まらないように）
            note = ("台本を貼り付けるか、「サンプル台本」から始められます。"
                    "左のキャストを行へドラッグすると配役が決まります（クリックでスタイル選択）")
        self._note.configure(text=note)
        # 未割り振りの行があるときだけボタンを目立たせる
        has_cast = any(kind == "cast" for kind, _s, _c in self._line_info)
        unset = any(kind in ("silent", "default") for kind, _s, _c in self._line_info)
        if unset and has_cast and content.strip():
            self._assign_btn.configure(style="Nudge.TButton")
        else:
            self._assign_btn.configure(style="TButton")
        try:
            self.DRAFT_PATH.write_text(content, encoding="utf-8")
        except OSError:
            pass

    # --- 左端のキャスト縦列とドラッグ＆ドロップ ---

    def _refresh_strip(self):
        cast = speak.load_config().get("cast", {})
        for cell in self._strip.winfo_children():
            cell.destroy()
        self._strip_cells.clear()
        shown = [s for s in CAST_SLOTS if s in cast]
        if not shown:
            ttk.Label(self._strip, text="キャストタブで登録すると、ここに並びます",
                      foreground=FG_DIM, font=("Yu Gothic UI", 8),
                      wraplength=THUMB, justify="center").pack(pady=8)
            return
        for slot in shown:
            sp = self.by_name.get(cast[slot].get("name", ""))
            img = CharacterPicker._thumb(sp) if sp else CharacterPicker._blank()
            cell = tk.Canvas(self._strip, width=THUMB, height=THUMB, background=BG,
                             highlightthickness=2, highlightbackground="#3a3a3a",
                             cursor="hand2")
            # アクティブスタイルがあればそのスタイルのアイコンを使う
            style_idx = 0
            suffix = self._active_style.get(slot, "")
            if suffix and sp:
                style_idx = max(0, ord(suffix) - ord("a"))
                styled_img = self._style_thumb(sp, style_idx)
                if styled_img:
                    img = styled_img
            cell.create_image(THUMB // 2 + 2, THUMB // 2 + 2, image=img, tags="icon")
            cell.image = img
            for ox, oy, fill in ((1, 1, "#101010"), (0, 0, CAST_COLORS.get(slot, "#f2f2f2"))):
                cell.create_text(6 + ox, THUMB + oy, text=slot, anchor="sw",
                                 font=self._letter_font, fill=fill, tags="letter")
            self._draw_style_tag(cell, slot, cast)
            cell.pack(pady=(0, 6))
            cell.bind("<ButtonPress-1>", lambda e, s=slot: self._press(e, s))
            cell.bind("<B1-Motion>", self._motion)
            cell.bind("<ButtonRelease-1>", self._release)
            cell.bind("<Enter>", lambda _e, s=slot: self._hover_strip(s))
            cell.bind("<Leave>", lambda _e: self._unhover_strip())
            self._strip_cells[slot] = cell

    def _draw_style_tag(self, cell: tk.Canvas, slot: str, cast: dict):
        """アイコン右上にスタイル名の小タグを描く。アクティブスタイルがなければ何も出さない。"""
        cell.delete("styletag")
        suffix = self._active_style.get(slot, "")
        if not suffix:
            return
        entry = cast.get(slot, {})
        styles = entry.get("styles", {})
        idx = ord(suffix) - ord("a")
        names = list(styles.keys())
        if not (0 <= idx < len(names)):
            return
        name = names[idx]
        tag_font = ("Yu Gothic UI", 7)
        # 右上に白背景＋濃文字のタグ（highlightthickness=2 のぶん内側にずれる）
        tx, ty = THUMB - 1, 3
        tid = cell.create_text(tx, ty, text=name, anchor="ne", font=tag_font,
                               fill="#1f1f1f", tags="styletag")
        bbox = cell.bbox(tid)
        if bbox:
            pad = 2
            cell.create_rectangle(bbox[0] - pad, bbox[1] - 1, bbox[2] + pad, bbox[3],
                                  fill="#e6e6e6", outline="", tags="styletag")
            cell.tag_raise(tid)

    def _style_thumb(self, sp: dict, style_idx: int) -> tk.PhotoImage | None:
        """スタイル固有のアイコンを取得する。"""
        uuid = sp["speaker_uuid"]
        try:
            info = get_speaker_info(uuid)
            style_infos = info.get("style_infos", [])
            if style_idx < len(style_infos):
                data = fetch_image(style_infos[style_idx]["icon"])
                icon = tk.PhotoImage(data=data)
                return icon.subsample(max(1, icon.width() // THUMB))
        except Exception:
            pass
        return None

    def _set_active_style(self, slot: str, suffix: str):
        """スタイルのアクティブ状態を切り替え、アイコンとタグを再描画する。"""
        self._active_style[slot] = suffix
        cast = speak.load_config().get("cast", {})
        cell = self._strip_cells.get(slot)
        if not (cell and cell.winfo_exists()):
            return
        # アイコンをスタイル固有のものに差し替え
        entry = cast.get(slot, {})
        sp = self.by_name.get(entry.get("name", ""))
        if sp:
            style_idx = max(0, ord(suffix) - ord("a")) if suffix else 0
            img = self._style_thumb(sp, style_idx)
            if img is None:
                img = CharacterPicker._thumb(sp)
            cell.delete("icon")
            cell.create_image(THUMB // 2 + 2, THUMB // 2 + 2, image=img, tags="icon")
            cell.image = img
            # 枠文字を最前面に戻す
            cell.tag_raise("letter")
        self._draw_style_tag(cell, slot, cast)

    def _hover_strip(self, slot: str):
        """左アイコンの hover で対応行をハイライトする。"""
        if self._hover_slot == slot:
            return
        self._hover_slot = slot
        self.text.tag_remove("hoverhighlight", "1.0", "end")
        for i, (kind, line_slot, _cnt) in enumerate(self._line_info, 1):
            if kind == "cast" and line_slot == slot:
                self.text.tag_add("hoverhighlight", f"{i}.0", f"{i}.end")

    def _unhover_strip(self):
        """hover 解除。"""
        self._hover_slot = None
        self.text.tag_remove("hoverhighlight", "1.0", "end")

    def _open_style_picker(self, slot: str):
        """クリックでスタイル選択モーダルを開く。"""
        if self._style_picker is not None and self._style_picker.winfo_exists():
            self._style_picker.close()
        cast = speak.load_config().get("cast", {})
        entry = cast.get(slot, {})
        sp = self.by_name.get(entry.get("name", ""))
        if sp is None or len(sp.get("styles", [])) < 2:
            return  # スタイルが1つしかない場合は開かない
        self._style_picker = StylePicker(self, slot, sp)

    def _press(self, event, slot: str):
        self._drag = {"slot": slot, "x": event.x_root, "y": event.y_root,
                      "moved": False, "ghost": None}

    def _motion(self, event):
        if not self._drag:
            return
        if not self._drag["moved"]:
            if (abs(event.x_root - self._drag["x"]) < self.DRAG_THRESHOLD
                    and abs(event.y_root - self._drag["y"]) < self.DRAG_THRESHOLD):
                return
            self._drag["moved"] = True
            self._drag["ghost"] = self._make_ghost(self._drag["slot"])
        if self._drag["ghost"] is not None:
            self._drag["ghost"].geometry(f"+{event.x_root + 14}+{event.y_root + 10}")
        self.text.tag_remove("droptarget", "1.0", "end")
        line = self._line_at_pointer(event)
        if line is not None:
            self.text.tag_add("droptarget", f"{line}.0", f"{line}.end")

    def _release(self, event):
        drag, self._drag = self._drag, None
        self.text.tag_remove("droptarget", "1.0", "end")
        if drag is None:
            return
        if drag["ghost"] is not None:
            drag["ghost"].destroy()
        if not drag["moved"]:
            self._open_style_picker(drag["slot"])
            return
        line = self._line_at_pointer(event)
        if line is not None:
            self._assign(line, drag["slot"])

    def _make_ghost(self, slot: str) -> tk.Toplevel | None:
        cell = self._strip_cells.get(slot)
        img = getattr(cell, "image", None)
        if img is None:
            return None
        ghost = tk.Toplevel(self)
        ghost.overrideredirect(True)
        ghost.attributes("-alpha", 0.7)
        ghost.attributes("-topmost", True)
        tk.Label(ghost, image=img, borderwidth=0).pack()
        return ghost

    def _line_at_pointer(self, event) -> int | None:
        if self.winfo_containing(event.x_root, event.y_root) is not self.text:
            return None
        x = event.x_root - self.text.winfo_rootx()
        y = event.y_root - self.text.winfo_rooty()
        return int(self.text.index(f"@{x},{y}").split(".")[0])

    @staticmethod
    def _relabel(body: str, slot: str, ob: str, cb: str, suffix: str = "") -> str:
        """行の中身にキャスト記法を書き込む（D&D と自動割り振りの共通部）。
        suffix はスタイル添字（例: "d"）。既にラベルがある行は添字を付け替える。"""
        label = slot + suffix
        if re.match(r"[A-Z][a-z]?[1-5]?" + re.escape(ob), body):
            # 既存ラベルを丸ごと差し替え（スロット＋スタイル添字。速度数字は残す）
            m = re.match(r"[A-Z][a-z]?([1-5]?" + re.escape(ob) + r")", body)
            return label + m.group(1) + body[m.end():]
        if body.startswith(ob):
            return label + body
        return f"{label}{ob}{body}{cb}"

    def _rewrite_line(self, line_no: int, slot: str, ob: str, cb: str, suffix: str = ""):
        line = self.text.get(f"{line_no}.0", f"{line_no}.end")
        indent = line[: len(line) - len(line.lstrip())]
        self.text.delete(f"{line_no}.0", f"{line_no}.end")
        self.text.insert(f"{line_no}.0",
                         indent + self._relabel(line.strip(), slot, ob, cb, suffix))

    def _assign(self, line_no: int, slot: str):
        """行のテキストにキャスト記法を書き込む（ドロップ＝テキスト編集。Ctrl+Z で戻せる）。"""
        if not self.text.get(f"{line_no}.0", f"{line_no}.end").strip():
            return
        ob, cb = self._env()["markers"].get("brackets") or ["『", "』"]
        suffix = self._active_style.get(slot, "")
        self.text.configure(autoseparators=False)
        try:
            self.text.edit_separator()
            self._rewrite_line(line_no, slot, ob, cb, suffix)
            self.text.edit_separator()
        finally:
            self.text.configure(autoseparators=True)

    def auto_assign(self, shuffle: bool, unset_only: bool = False):
        """登録キャストを台本に一括で割り振る。unset_only=True なら未設定の行だけ。
        順=A→B→C…の輪番、ランダム=直前と同じキャストの連続だけ避ける。
        Ctrl+Z 一発で全部戻る。"""
        slots = [s for s in CAST_SLOTS if s in speak.load_config().get("cast", {})]
        if not slots:
            return
        env = self._env()
        ob, cb = env["markers"].get("brackets") or ["『", "』"]
        info = self._classify(self.text.get("1.0", "end-1c").split("\n"), env)
        if unset_only:
            targets = [i for i, (kind, _s, _c) in enumerate(info, 1)
                       if kind in ("default", "silent")]
        else:
            targets = [i for i, (kind, _s, _c) in enumerate(info, 1)
                       if kind in ("cast", "other", "default", "silent")]
        if not targets:
            return
        self.text.configure(autoseparators=False)
        try:
            self.text.edit_separator()
            prev = None
            for n, line_no in enumerate(targets):
                if shuffle:
                    slot = random.choice([s for s in slots if s != prev] or slots)
                else:
                    slot = slots[n % len(slots)]
                prev = slot
                self._rewrite_line(line_no, slot, ob, cb,
                                   self._active_style.get(slot, ""))
            self.text.edit_separator()
        finally:
            self.text.configure(autoseparators=True)

    # --- 再生と書き出し ---

    def play_all(self):
        if self._proc is not None:
            speak.stop_playback()  # play は PID 登録済みなので stop で殺せる
            self._done()
            return
        self._start(1)

    def play_from_cursor(self):
        if self._proc is None:
            self._start(int(self.text.index("insert").split(".")[0]))

    @staticmethod
    def _default_name(prefix: str, ext: str) -> str:
        """保存ダイアログの既定ファイル名（例: 台本_20260703_2215.txt）。"""
        return f"{prefix}_{datetime.now():%Y%m%d_%H%M}{ext}"

    def export_wav(self):
        if self._proc is not None:
            return
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".wav",
                                            filetypes=[("WAV 音声", "*.wav")],
                                            initialfile=self._default_name("台本音声", ".wav"))
        if path:
            self._start(1, out=path)

    def _start(self, from_line: int, out: str | None = None):
        content = self.text.get(f"{from_line}.0", "end-1c")
        if not content.strip():
            return
        info = self._classify(content.split("\n"), self._env())
        self._seg_lines = []
        self._slot_by_line = {}
        for i, (kind, slot, cnt) in enumerate(info):
            self._seg_lines += [from_line + i] * cnt
            self._slot_by_line[from_line + i] = slot
        if not self._seg_lines:
            return  # 読み上げ対象なし
        self._proc_file = Path(tempfile.gettempdir()) / "vvtts_gui_script.txt"
        self._proc_file.write_text(content, encoding="utf-8")
        # 進捗 [n/N] をリアルタイムで受け取る（unbuffered。exe では print 側の flush が担保）
        cmd = speak.self_cmd("play", str(self._proc_file), unbuffered=True)
        if speak.load_config().get("scriptAutoSpeed"):
            cmd.append("--auto")  # 設定タブ「台本再生でも読み上げ速度の自動調整を使う」
        if out:
            cmd += ["--out", out]
        self._proc_kind = "wav" if out else "play"
        self._now = -1
        self._proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                      creationflags=subprocess.CREATE_NO_WINDOW)
        threading.Thread(target=self._pump, args=(self._proc,), daemon=True).start()
        if out:
            # 書き出しは再生と違って途中停止の口がない（play の --out は PID 未登録）ので、
            # ボタンを沈めて完走を待つ
            self._wav_btn.configure(text="書き出し中…", state="disabled")
            self._play_btn.configure(state="disabled")
        else:
            self._play_btn.configure(text="停止", style="Playing.TButton")
            self._wav_btn.configure(state="disabled")
        self._here_btn.configure(state="disabled")
        self._vvox_btn.configure(state="disabled")
        self._watch()

    def _pump(self, proc: subprocess.Popen):
        """進捗行 [n/N] を読み続ける裏方スレッド。Tk には触らず整数を置くだけ。"""
        try:
            for raw in proc.stdout:
                m = re.match(rb"\[(\d+)/", raw)
                if m:
                    self._now = int(m.group(1)) - 1
        except Exception:
            pass

    def _watch(self, tick: int = 0):
        """再生中のアニメーション兼終了検知。発話中の行と左のキャストを光らせる。"""
        if not self.winfo_exists():
            return
        if self._proc is not None and self._proc.poll() is None:
            self._spin.configure(text=self.SPIN_FRAMES[tick % len(self.SPIN_FRAMES)])
            self.text.tag_remove("nowline", "1.0", "end")
            slot = None
            if 0 <= self._now < len(self._seg_lines):
                ln = self._seg_lines[self._now]
                self.text.tag_add("nowline", f"{ln}.0", f"{ln}.end")
                if self._proc_kind == "play":
                    self.text.see(f"{ln}.0")
                slot = self._slot_by_line.get(ln)
            self._light(slot)
            self.after(120, self._watch, tick + 1)
            return
        self._done()

    def _light(self, slot: str | None):
        """発話中キャストのサムネイル枠を金に（カードの発光と同じ言語）。"""
        for s, cell in self._strip_cells.items():
            cell.configure(highlightbackground=ACCENT if s == slot else "#3a3a3a")

    def _done(self):
        """再生・書き出しの後始末（自然終了・停止・多重呼び出しのどれでも安全）。"""
        finished = self._proc is not None and self._proc.poll() == 0
        was_wav = self._proc_kind == "wav"
        self._proc = None
        self._proc_kind = ""
        if self._proc_file is not None:
            self._proc_file.unlink(missing_ok=True)
            self._proc_file = None
        self.text.tag_remove("nowline", "1.0", "end")
        self._light(None)
        self._spin.configure(text="")
        self._play_btn.configure(text="最初から再生", style="TButton", state="normal")
        self._here_btn.configure(state="normal")
        self._vvox_btn.configure(state="normal")
        self._wav_btn.configure(state="normal")
        if was_wav and finished:
            self._flash(self._wav_btn, "書き出しました", "WAV書き出し")
        else:
            self._wav_btn.configure(text="WAV書き出し")

    def _flash(self, btn: ttk.Button, text: str, restore: str):
        """控えめな完了フィードバック（ポップアップは出さない流儀）。"""
        btn.configure(text=text)
        self.after(1600, lambda: btn.winfo_exists() and btn.configure(text=restore))

    # --- 入出力 ---

    def open_file(self):
        path = filedialog.askopenfilename(
            parent=self, filetypes=[("テキスト", ("*.txt", "*.md")),
                                    ("すべてのファイル", "*.*")])
        if not path:
            return
        try:
            content = Path(path).read_text(encoding="utf-8")
        except UnicodeDecodeError:
            try:
                content = Path(path).read_text(encoding="cp932")  # メモ帳育ちの txt 救済
            except (OSError, UnicodeDecodeError):
                return
        except OSError:
            return
        self.text.edit_separator()
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)

    def save_file(self):
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".txt", filetypes=[("テキスト", "*.txt")],
            initialfile=self._default_name("台本", ".txt"))
        if not path:
            return
        try:
            Path(path).write_text(self.text.get("1.0", "end-1c"), encoding="utf-8")
        except OSError:
            return
        self._flash(self._save_btn, "保存しました", "テキスト保存")

    def export_voicevox(self):
        """本家 VOICEVOX の「テキスト読み込み」形式（キャラ名（スタイル名）,セリフ）で書き出す。
        イントネーション等を細かく調整したくなった人が本家エディタへ持っていける移行口。
        本家はセリフ中の半角カンマでも欄を区切ってしまうので、読点に置き換えて渡す。"""
        env = self._env()
        segments = speak.extract_script_segments(
            self.text.get("1.0", "end-1c"), env["styles"], env["speaker"],
            env["markers"], env["cast"])
        if not segments:
            return
        out_lines = []
        for seg in segments:
            # ルビは読みのかなに展開して渡す（本家にルビ記法はないが、音はこれで正しく伝わる）
            text = speak.strip_ruby(seg[0]).replace(",", "、").replace("，", "、")
            name, style = self._id_names.get(seg[1], ("", ""))
            out_lines.append(f"{name}（{style}）,{text}" if name else text)
        path = filedialog.asksaveasfilename(
            parent=self, defaultextension=".txt", filetypes=[("テキスト", "*.txt")],
            initialfile=self._default_name("本家読み込み用台本", ".txt"))
        if not path:
            return
        try:
            Path(path).write_text("\n".join(out_lines) + "\n", encoding="utf-8")
        except OSError:
            return
        self._flash(self._vvox_btn, "書き出しました（本家の「ファイル→テキスト読み込み」へ）",
                    "本家VOICEVOX用に書き出し")


class SettingsTab(ttk.Frame):
    """基本設定。操作すると少し置いて自動保存される（保存ボタン・完了ポップアップなし。
    キャストタブの即保存と同じ流儀）。"""

    def __init__(self, master):
        super().__init__(master, padding=12)
        cfg = speak.load_config()
        markers = speak.get_markers(cfg)
        self._save_timer: str | None = None
        self._loading = True  # 初期値の流し込み中はトレース経由の保存を眠らせる

        # 読み上げ速度設定1〜5（速度2=通常）。旧設定（speed だけ）は get_speed_steps が速度2に引き継ぐ。
        # しきい値が3つに満たない（CLIで少なく設定した等）ときは初期値で埋めて見せる。
        # 文字数による自動早口はGUIでは常時オン（速くしたくなければ速度3〜5を通常と同じにすればよい。
        # CLI の stepChars off は上級技で、GUIで保存するとしきい値が書き戻る）
        steps = speak.get_speed_steps(cfg)
        chars = cfg.get("stepChars")
        shown_chars = sorted(int(c) for c in (chars or speak.DEFAULT_STEP_CHARS))[:3]
        shown_chars += speak.DEFAULT_STEP_CHARS[len(shown_chars):]

        self.step_vars = [tk.DoubleVar(value=s) for s in steps]
        self.char_vars = [tk.IntVar(value=c) for c in shown_chars]
        self.max_chars = tk.IntVar(value=cfg.get("maxChars", speak.MAX_CHARS))
        self.overflow_text = tk.StringVar(value=cfg.get("overflowText", speak.OVERFLOW_TEXT))
        self.serve_port = tk.IntVar(value=cfg.get("servePort", speak.DEFAULT_SERVE_PORT))
        self.bare_ok = tk.BooleanVar(value=not markers["requireLabel"])
        self.script_auto = tk.BooleanVar(value=cfg.get("scriptAutoSpeed", False))
        self.voice_tag = tk.BooleanVar(value=markers["voiceTag"])
        self.auto_start = tk.BooleanVar(value=cfg.get("autoStart", True))

        def note(parent, text, pady=(2, 6)):
            ttk.Label(parent, text=text, foreground=FG_DIM, wraplength=560, justify="left",
                      font=("Yu Gothic UI", 8)).pack(anchor="w", padx=8, pady=pady)

        # --- 読み上げ速度設定: 速度1〜5。速度2=通常、速度3〜5は文字数に応じた自動早口も担う ---
        # 仕組みの説明は冒頭の1文に集約し、各行の注釈は最小限にする（画面では内輪の用語
        # 「通常」「ゆっくり枠」を出さず、挙動をです・ます調で語る）
        speed_frame = ttk.LabelFrame(self, text="読み上げ速度設定")
        speed_frame.pack(fill="x")
        note(speed_frame, "読み上げの速さは5段階で設定できます。普段の読み上げは速度2で、"
                          "長い文は文字数に応じて速度3〜5に自動で切り替わります。", pady=(6, 2))
        ladder = ttk.Frame(speed_frame)
        ladder.pack(anchor="w", padx=8, pady=(4, 0))
        annotations = ("A1『…』のように数字で指名したときに使われます", "普段の読み上げはこの速さです")
        for i in range(5):
            ttk.Label(ladder, text=f"速度{i + 1}", width=6).grid(row=i, column=0, sticky="w", pady=1)
            tk.Spinbox(ladder, textvariable=self.step_vars[i], from_=0.5, to=2.0,
                       increment=0.05, width=6, format="%.2f").grid(row=i, column=1)
            ttk.Label(ladder, text="倍速").grid(row=i, column=2, sticky="w", padx=(4, 14))
            if i >= 2:
                tk.Spinbox(ladder, textvariable=self.char_vars[i - 2], from_=10,
                           to=2000, increment=10, width=6).grid(row=i, column=3)
                ttk.Label(ladder, text="字以上で切り替え").grid(row=i, column=4, sticky="w", padx=4)
            else:
                ttk.Label(ladder, text=annotations[i], foreground=FG_DIM
                          ).grid(row=i, column=3, columnspan=2, sticky="w", padx=4)
        ttk.Checkbutton(speed_frame, text="台本の再生でも文字数による自動調整を使う（台本タブ・play・paste）",
                        variable=self.script_auto, command=self._schedule_save
                        ).pack(anchor="w", padx=8, pady=(6, 0))
        note(speed_frame, "A1『こんにちは』のように数字を添えたセリフは、指名した速度で固定されます"
                          "（文字数による自動調整より優先されます）。")

        # --- 読み上げる記法: 実例をそのまま見せる（名前だけのチェックボックスにしない） ---
        notation = ttk.LabelFrame(self, text="読み上げる記法")
        notation.pack(fill="x", pady=(12, 0))
        ttk.Label(notation, text="A『こんにちは』 … キャスト記法は常に読み上げます"
                  ).pack(anchor="w", padx=8, pady=(8, 2))
        ttk.Checkbutton(notation, text="ラベルなしの 『こんにちは』 も読み上げる（キャストAの声で）",
                        variable=self.bare_ok, command=self._schedule_save).pack(anchor="w", padx=8)
        ttk.Checkbutton(notation, text="<voice>こんにちは</voice> のタグ記法も読み上げる",
                        variable=self.voice_tag, command=self._schedule_save).pack(anchor="w", padx=8)
        note(notation, '<voice style="怒り">…</voice> のようにスタイルも指定できます。'
                       "コードブロック内の記法は常に読み上げません。")

        # --- こまかい設定 ---
        adv = ttk.LabelFrame(self, text="こまかい設定")
        adv.pack(fill="x", pady=(12, 0))
        row1 = ttk.Frame(adv)
        row1.pack(anchor="w", padx=8, pady=(8, 0))
        ttk.Label(row1, text="1回に読む上限").pack(side="left")
        tk.Spinbox(row1, textvariable=self.max_chars, from_=100, to=2000,
                   increment=50, width=7).pack(side="left", padx=(8, 4))
        ttk.Label(row1, text="文字").pack(side="left")
        note(adv, "上限を長くすると、音が出はじめるまでの待ち時間が延びます（目安: 500字で約13秒）。")
        row_of = ttk.Frame(adv)
        row_of.pack(anchor="w", padx=8)
        ttk.Label(row_of, text="上限を超えたときに最後に読む言葉").pack(side="left")
        ttk.Entry(row_of, textvariable=self.overflow_text, width=14).pack(side="left", padx=(8, 0))
        note(adv, "超過したぶんは読まれず、代わりにこの言葉を読みます。空欄にすると、何も言わずに打ち切ります。")
        row2 = ttk.Frame(adv)
        row2.pack(anchor="w", padx=8)
        ttk.Label(row2, text="serve のポート番号").pack(side="left")
        tk.Spinbox(row2, textvariable=self.serve_port, from_=1024, to=65535,
                   width=7).pack(side="left", padx=(8, 0))
        note(adv, "HTTP 経由で読み上げを受け付けるとき（serve 機能）の待ち受けポートです。")
        ttk.Checkbutton(adv, text="VOICEVOX エンジンが止まっていたら自動で起動する",
                        variable=self.auto_start, command=self._schedule_save
                        ).pack(anchor="w", padx=8, pady=(2, 8))

        # 数値欄はキー入力・スピン操作のどちらでも変わるので、変数のトレースで拾って自動保存
        for var in (self.max_chars, self.overflow_text, self.serve_port,
                    *self.step_vars, *self.char_vars):
            var.trace_add("write", self._schedule_save)
        self._loading = False

    def _schedule_save(self, *_):
        """連打・入力途中で書きすぎないよう、少し置いてから保存する（デバウンス）。"""
        if self._loading:
            return
        if self._save_timer is not None:
            self.after_cancel(self._save_timer)
        self._save_timer = self.after(800, self._persist)

    def _persist(self):
        self._save_timer = None
        cfg = speak.load_config()

        def grab(var, lo, hi, cast=float):
            try:
                return max(lo, min(hi, cast(var.get())))
            except (tk.TclError, ValueError):
                return None  # 入力途中（空欄など）。その項目は保存せず前回値を守る

        current = speak.get_speed_steps(cfg)
        new_steps = []
        for i, var in enumerate(self.step_vars):
            v = grab(var, 0.5, 2.0)
            new_steps.append(round(v, 2) if v is not None else current[i])
        cfg["speedSteps"] = new_steps
        cfg["speed"] = new_steps[1]  # 旧キー（=通常）は CLI 互換のため速度2に同期し続ける
        chars = [c for c in (grab(v, 1, 2000, int) for v in self.char_vars)
                 if c is not None]
        cfg["stepChars"] = sorted(chars)
        max_chars = grab(self.max_chars, 100, 2000, int)
        if max_chars is not None:
            cfg["maxChars"] = max_chars
        cfg["overflowText"] = self.overflow_text.get().strip()
        port = grab(self.serve_port, 1024, 65535, int)
        if port is not None:
            cfg["servePort"] = port
        cfg["autoStart"] = self.auto_start.get()
        cfg["scriptAutoSpeed"] = self.script_auto.get()
        markers = cfg.setdefault("markers", {})
        markers["requireLabel"] = not self.bare_ok.get()
        markers["voiceTag"] = self.voice_tag.get()
        speak.save_config(cfg)


def apply_dark_theme(root: tk.Tk):
    """全体をダーク基調＋金アクセントに統一する。ttk は clam テーマを下敷きに一括配色、
    素の tk ウィジェット（Text・Scale・Spinbox）は option database で既定色を差す。
    ※ウィジェットを作る前に呼ぶこと（option_add は生成済みの部品には効かない）"""
    root.configure(background=BG)
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", background=BG, foreground=FG, bordercolor="#3a3a3a",
                    lightcolor=BG, darkcolor=BG, troughcolor=FIELD,
                    fieldbackground=FIELD, insertcolor=FG)
    style.configure("TNotebook", borderwidth=0)
    style.configure("TNotebook.Tab", background=BG, foreground=FG_DIM, padding=(16, 7))
    style.map("TNotebook.Tab", background=[("selected", BG_RAISED)],
              foreground=[("selected", ACCENT)])
    # ボタンは地に溶けないよう、明るめの面＋見える縁で「押せる物」の輪郭を立てる
    style.configure("TButton", background="#383838", foreground=FG,
                    bordercolor="#5f5f5f", lightcolor="#4a4a4a", darkcolor="#242424")
    style.map("TButton", background=[("active", "#474747")])
    # テスト再生中の「停止」ボタン用: 金地に濃色文字（再生中であることを色で見せる）
    style.configure("Playing.TButton", background=ACCENT, foreground="#1f1f1f")
    style.map("Playing.TButton", background=[("active", "#e6c34d")],
              foreground=[("active", "#1f1f1f")])
    # 未割り振り行があるときの「キャスト割り振り」: 枠を金にして穴埋め催促
    style.configure("Nudge.TButton", background="#383838", foreground=ACCENT,
                    bordercolor=ACCENT)
    style.map("Nudge.TButton", background=[("active", "#474747")],
              foreground=[("active", ACCENT)])
    style.configure("TCheckbutton", indicatorbackground=FIELD)
    style.map("TCheckbutton", background=[("active", BG)],
              indicatorforeground=[("selected", ACCENT)])
    style.configure("TLabelframe", bordercolor="#3a3a3a")
    style.configure("TLabelframe.Label", foreground=FG_DIM)
    style.configure("TSpinbox", arrowcolor=FG, background=BG_RAISED)
    style.configure("Vertical.TScrollbar", background=BG_RAISED, arrowcolor=FG,
                    troughcolor=BG)
    for pattern, value in (
        ("*Text.background", "#242424"), ("*Text.foreground", FG),
        ("*Text.insertBackground", FG), ("*Text.highlightThickness", 0),
        ("*Scale.background", BG), ("*Scale.foreground", FG),
        ("*Scale.troughColor", FIELD), ("*Scale.highlightThickness", 0),
        ("*Scale.activeBackground", "#3a3a3a"),
        ("*Spinbox.background", FIELD), ("*Spinbox.foreground", FG),
        ("*Spinbox.insertBackground", FG), ("*Spinbox.buttonBackground", BG_RAISED),
    ):
        root.option_add(pattern, value)


def build_gate(root: tk.Tk):
    """VOICEVOX が見つからない/起動できないときの案内画面。導線は常に公式サイトへ。"""
    frame = ttk.Frame(root, padding=24)
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text="VOICEVOX が見つかりません", font=("Yu Gothic UI", 14, "bold")).pack(anchor="w")
    ttk.Label(frame, wraplength=420, justify="left", text=(
        "このツールを使うには VOICEVOX（本家）が必要です。\n"
        "未インストールの場合は、公式サイトからインストールしてください（無料）。\n\n"
        "インストール済みなのにこの画面になる場合は、インストール先が標準と違う可能性があります。"
        "「インストール先を指定」から、VOICEVOX のエンジン（run.exe）の場所を教えてください。"
    )).pack(anchor="w", pady=12)
    buttons = ttk.Frame(frame)
    buttons.pack(anchor="w")
    ttk.Button(buttons, text="VOICEVOX 公式サイトを開く",
               command=lambda: webbrowser.open(OFFICIAL_SITE)).pack(side="left")

    def retry():
        if speak.is_engine_running() or speak.ensure_engine():
            frame.destroy()
            build_main(root)

    def choose_engine():
        path = filedialog.askopenfilename(
            parent=root, title="VOICEVOX のエンジン（run.exe）を選んでください",
            filetypes=[("VOICEVOX エンジン", "*.exe")])
        if not path:
            return
        cfg = speak.load_config()
        cfg["enginePath"] = path
        speak.save_config(cfg)
        retry()

    ttk.Button(buttons, text="インストール先を指定", command=choose_engine).pack(side="left", padx=8)
    ttk.Button(buttons, text="再チェック", command=retry).pack(side="left")


def build_main(root: tk.Tk):
    ver = speak.engine_version()
    if ver and speak.parse_version(ver) < speak.MIN_ENGINE_VERSION:
        minimum = ".".join(map(str, speak.MIN_ENGINE_VERSION))
        banner = tk.Frame(root, background="#fff3cd")
        banner.pack(fill="x")
        tk.Label(banner, background="#fff3cd", justify="left", text=(
            f"VOICEVOX が古いようです（v{ver}）。新しいキャラクターには v{minimum} 以上が必要です。"
        )).pack(side="left", padx=8, pady=4)
        tk.Button(banner, text="公式サイトで更新", command=lambda: webbrowser.open(OFFICIAL_SITE)).pack(
            side="right", padx=8, pady=2
        )

    notebook = ttk.Notebook(root)
    notebook.pack(fill="both", expand=True)
    notebook.add(CastTab(notebook), text=" キャスト ")
    notebook.add(ScriptTab(notebook), text=" 台本 ")
    notebook.add(SettingsTab(notebook), text=" 設定 ")

    status = ttk.Label(root, text=f"VOICEVOX ENGINE v{ver or '?'} に接続中（{speak.vv_host()}）",
                       foreground="#888888")
    status.pack(anchor="w", padx=8, pady=2)


def main():
    if already_open():
        ctypes.windll.user32.MessageBoxW(None, "設定画面はすでに開いています", APP_TITLE, 0x40)
        return

    engine_ok = speak.is_engine_running() or (speak.find_engine_path() is not None and speak.ensure_engine())

    root = tk.Tk()
    root.title(APP_TITLE)
    if ICON_PATH.exists():
        root.iconbitmap(default=str(ICON_PATH))  # default= で以後の Toplevel にも波及
    root.minsize(960, 640)
    apply_dark_theme(root)
    if engine_ok:
        build_main(root)
    else:
        build_gate(root)
    root.mainloop()


if __name__ == "__main__":
    main()
