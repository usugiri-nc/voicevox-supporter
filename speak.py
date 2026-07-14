"""VOICEVOX TTS for Claude Code

Usage:
  python speak.py "読み上げたいテキスト"
  python speak.py --bg "テキスト"   # 切り離し再生（すぐ返る・stop で止められる。外部AI・スクリプト向き）
  echo "テキスト" | python speak.py
  python speak.py --hook          # 応答完了フック（stdin から JSON を受け取る）
  python speak.py -l              # キャラ一覧
  python speak.py -s 13 "テキスト"  # スピーカー指定
  python speak.py config          # 設定表示
  python speak.py config speaker 13
  python speak.py config speed 1.3
  python speak.py reading         # 読み替え辞書の表示
  python speak.py reading add 身体 からだ
  python speak.py cast            # キャスト一覧（添字対応表つき）
  python speak.py cast set A 2    # スロットA にキャラを登録（IDはそのキャラの声ならどれでも）
  python speak.py play 台本.txt    # 台本ファイルを順次再生（--out 出力.wav で書き出し、--auto で長文の速度自動調整）
  python speak.py paste           # クリップボードのテキストを読み上げ（ワンショット）
  python speak.py serve           # ローカルHTTPで待ち受け（他ソフトからの流し込み口）
  python speak.py serve stop      # 待ち受けを終了
  python speak.py config speedSteps 0.8 1.3 1.4 1.55 1.7  # 読み上げ速度設定1〜5（速度2=通常）
  python speak.py config stepChars 80 200 400       # 速度3〜5に切り替わる文字数（off で自動早口をやめる）
  python speak.py stop            # 再生中の音声を止める（合成中なら中断）
  python speak.py doctor          # 環境診断（何も変更しない）
  python speak.py install-hook    # Claude Code の Stop フックを設定（他ツールは各自の方法で登録）
  python speak.py on / off / status

Voice notations (in AI responses):
  『セリフ』                            # 括弧記法（デフォルト有効）
  『（ささやき）セリフ』                  # スタイル指定
  A『セリフ』                           # キャスト指定（=Aa。1番目のスタイルで発言）
  Ac『セリフ』                          # キャストの3番目のスタイルで（a=1番目, b=2番目…）
  A1『セリフ』                          # 読み上げ速度1〜5で読む（2=通常。そのセリフは固定速度）
  Ad1『セリフ』                         # スタイル添字と数字添字の併用（並びは スタイル→数字）
  ｜漢字《かな》                        # ルビ記法（｜必須。振り先をルビの読みで発音する）
  漢字《かな》                          # ｜なしは見た目の注記扱い（《かな》は読み飛ばすだけ）
  B『（怒り）セリフ』                    # スタイル名で指定してもよい
  <voice>セリフ</voice>                 # タグ記法
  <voice style="怒り">セリフ</voice>
  ♪ セリフ                             # 行頭マーカー記法（デフォルト無効）

記法は config voiceTag / lineMarker / brackets で個別に切り替えられる。
"""

from __future__ import annotations

import ctypes
import io
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

if sys.platform != "win32":
    sys.exit("voicevox-tts is Windows-only（再生に winsound を使用）")

import winsound

VOICEVOX_DEFAULT_HOST = "http://127.0.0.1:50021"
DEFAULT_SPEAKER = 3
DEFAULT_SPEED = 1.0
MAX_CHARS = 500
OVERFLOW_TEXT = "以下省略"  # maxChars 超過時に末尾で読む言葉（overflowText 設定。空文字なら黙って打ち切る）

CONFIG_PATH = Path.home() / ".voicevox-tts.json"
FLAG_PATH = Path.home() / ".voicevox-tts-enabled"
VOICEVOX_ENGINE_PATHS = [
    Path(r"C:\Program Files\VOICEVOX\vv-engine\run.exe"),
    Path(r"C:\Program Files\VOICEVOX\run.exe"),
    Path.home() / "AppData/Local/Programs/VOICEVOX/vv-engine/run.exe",
    Path.home() / "AppData/Local/Programs/VOICEVOX/run.exe",
]

SCRIPT_PATH = str(Path(__file__).resolve())
FROZEN = bool(getattr(sys, "frozen", False))  # PyInstaller の exe として動いているか


def self_cmd(*args: str, unbuffered: bool = False) -> list[str]:
    """自分（speak CLI）を子プロセスで呼ぶためのコマンド列。
    ソース実行では python speak.py、exe では exe 自身（launcher が引数つき起動を CLI に回す）。
    unbuffered は進捗行 [n/N] をパイプでリアルタイムに受けたい呼び出し向け
    （exe に -u は無いので、進捗表示の print には flush=True も付けてある）。"""
    if FROZEN:
        return [sys.executable, *args]
    head = [sys.executable, "-u"] if unbuffered else [sys.executable]
    return [*head, SCRIPT_PATH, *args]


def self_cmd_str() -> str:
    """人間・AI に見せるコマンド例の先頭部分（AI_GUIDE・install-hook・案内文が使う）。
    「引用符＋スラッシュ」で書く。バックスラッシュ裸書きだと bash 系シェルで \\s が食われる。"""
    if FROZEN:
        return f'"{Path(sys.executable).resolve().as_posix()}"'
    return f'python "{Path(SCRIPT_PATH).as_posix()}"'


def app_dir() -> Path:
    """配布物の顔になるフォルダ。exe では exe の隣（_internal の中に埋めない）、ソースではリポジトリ直下。
    AI_GUIDE.md のような「利用者に見せるファイル」の置き場に使う。"""
    return Path(sys.executable).resolve().parent if FROZEN else Path(SCRIPT_PATH).parent


VOICE_TAG_RE = re.compile(
    r'<voice(?:\s+style="(?P<stylename>[^"]*)")?\s*>(?P<content>.*?)</voice>', re.DOTALL
)


def load_config() -> dict:
    if CONFIG_PATH.exists():
        try:
            return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Warning: config load failed ({e}), using defaults", file=sys.stderr)
    return {"speaker": DEFAULT_SPEAKER, "speed": DEFAULT_SPEED, "host": VOICEVOX_DEFAULT_HOST}


def save_config(config: dict):
    CONFIG_PATH.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")


def vv_host() -> str:
    return load_config().get("host", VOICEVOX_DEFAULT_HOST)


def vv_request(path: str, method: str = "GET", query: dict | None = None, body: bytes | None = None) -> bytes:
    url = vv_host() + path
    if query:
        url += "?" + urllib.parse.urlencode(query)
    req = urllib.request.Request(url, method=method)
    if body is not None:
        req.data = body
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def pick_speaker(cfg: dict, explicit: int | None = None) -> int:
    if explicit is not None:
        return explicit
    favs = cfg.get("favorites")
    if favs:
        return random.choice(favs)
    return cfg.get("speaker", DEFAULT_SPEAKER)


# --- Reading fixes (VOICEVOXが読み間違える語を合成前に置換して直す) ---
# ここには誰にでも役立つ汎用の読み替えだけを置く。
# 個人的な読み替え（当て字・造語など）は ~/.voicevox-tts-readings.json に
# {"表記": "よみ"} 形式で保存され、内蔵分より優先される。
# VOICEVOX本体のユーザー辞書とは別物で、合成直前のクライアント側文字置換のみ。
# 長い語ほど先に処理されるので、部分一致の誤爆は起きにくい。
READING_FIXES = {
    "身体": "からだ",
}

READINGS_PATH = Path.home() / ".voicevox-tts-readings.json"


def load_user_readings() -> dict:
    if READINGS_PATH.exists():
        try:
            return json.loads(READINGS_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"Warning: readings load failed ({e})", file=sys.stderr)
    return {}


def save_user_readings(readings: dict):
    READINGS_PATH.write_text(json.dumps(readings, indent=2, ensure_ascii=False), encoding="utf-8")


def apply_reading_fixes(text: str) -> str:
    readings = dict(READING_FIXES)
    readings.update(load_user_readings())
    for surface in sorted(readings, key=len, reverse=True):
        text = text.replace(surface, readings[surface])
    return text


def synthesize(text: str, speaker: int | None = None, speed: float | None = None) -> bytes:
    text = apply_reading_fixes(text)
    cfg = load_config()
    sp = pick_speaker(cfg, speaker)
    spd = speed if speed is not None else cfg.get("speed", DEFAULT_SPEED)

    query_data = vv_request("/audio_query", "POST", query={"text": text, "speaker": sp})

    q = json.loads(query_data)
    q["prePhonemeLength"] = cfg.get("prePhonemeLength", 0.8)
    q["speedScale"] = spd
    pause = cfg.get("pauseLengthScale")
    if pause is not None:
        q["pauseLengthScale"] = pause
    intonation = cfg.get("intonationScale")
    if intonation is not None:
        q["intonationScale"] = intonation
    query_data = json.dumps(q).encode("utf-8")

    return vv_request("/synthesis", "POST", query={"speaker": sp}, body=query_data)


LEAD_SILENCE_MS = 150


def prepend_silence(wav_data: bytes, ms: int = LEAD_SILENCE_MS) -> bytes:
    """WAVの先頭に無音を挿入してオーディオデバイスの起動遅延を吸収する。"""
    src = wave.open(io.BytesIO(wav_data), "rb")
    params = src.getparams()
    frames = src.readframes(params.nframes)
    src.close()

    silent_samples = int(params.framerate * ms / 1000)
    silent_bytes = b"\x00" * (silent_samples * params.sampwidth * params.nchannels)

    out = io.BytesIO()
    dst = wave.open(out, "wb")
    dst.setparams(params._replace(nframes=params.nframes + silent_samples))
    dst.writeframes(silent_bytes + frames)
    dst.close()
    return out.getvalue()


# --- Playback serialization (音の重なり防止) ---

_kernel32 = ctypes.windll.kernel32
_kernel32.CreateMutexW.restype = ctypes.c_void_p
_kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
_kernel32.WaitForSingleObject.restype = ctypes.c_ulong
_kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
_kernel32.ReleaseMutex.argtypes = [ctypes.c_void_p]
_kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
_kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]

PLAY_MUTEX_NAME = "vvtts_play"
PLAY_MUTEX_TIMEOUT_MS = 5 * 60 * 1000  # 待たされすぎるくらいなら重なって鳴る方を選ぶ


def acquire_play_lock() -> int | None:
    """再生用のグローバルロック（名前付きミューテックス）を取る。
    別プロセスが再生中なら終わるまで待つことで、音の重なりを防ぐ。
    保持したままプロセスが死んでも OS が解放するので、残骸ロックは残らない。
    タイムアウト時は None を返し、呼び出し側は重なりを許容して再生を続ける。"""
    handle = _kernel32.CreateMutexW(None, False, PLAY_MUTEX_NAME)
    if not handle:
        return None
    WAIT_OBJECT_0, WAIT_ABANDONED = 0x00, 0x80
    result = _kernel32.WaitForSingleObject(handle, PLAY_MUTEX_TIMEOUT_MS)
    if result in (WAIT_OBJECT_0, WAIT_ABANDONED):
        return handle
    _kernel32.CloseHandle(handle)
    return None


def release_play_lock(handle: int | None):
    if handle:
        _kernel32.ReleaseMutex(handle)
        _kernel32.CloseHandle(handle)


# --- Clipboard (paste コマンド用・ワンショット読み取り) ---

_user32 = ctypes.windll.user32
_user32.OpenClipboard.argtypes = [ctypes.c_void_p]
_user32.GetClipboardData.restype = ctypes.c_void_p
_user32.GetClipboardData.argtypes = [ctypes.c_uint]

CF_UNICODETEXT = 13


def read_clipboard_text() -> str | None:
    """クリップボードのテキストを1回だけ読む。常時監視はしない設計（DESIGN.md 行動原則3）。"""
    for _ in range(5):  # 他プロセスが開いている瞬間に当たったら少し待って再挑戦
        if _user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return None
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return None
        ptr = _kernel32.GlobalLock(handle)
        if not ptr:
            return None
        try:
            return ctypes.wstring_at(ptr)
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def play_wav_file(wav_path: str):
    winsound.PlaySound(wav_path, winsound.SND_FILENAME)


def concat_wav(chunks: list[bytes]) -> bytes:
    if len(chunks) == 1:
        return chunks[0]
    out = io.BytesIO()
    total_frames = 0
    all_frames = []
    params = None
    for chunk in chunks:
        src = wave.open(io.BytesIO(chunk), "rb")
        if params is None:
            params = src.getparams()
        frames = src.readframes(src.getnframes())
        all_frames.append(frames)
        total_frames += src.getnframes()
        src.close()
    dst = wave.open(out, "wb")
    dst.setparams(params._replace(nframes=total_frames))
    for f in all_frames:
        dst.writeframes(f)
    dst.close()
    return out.getvalue()


def _detached_popen_kwargs() -> dict:
    """コンソール窓を出さずに切り離して起動するためのPopen引数。"""
    return {
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "creationflags": subprocess.CREATE_NO_WINDOW,
    }


def _play_wav(wav_data: bytes):
    wav_data = prepend_silence(wav_data)
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False, prefix="vvtts_", dir=tempfile.gettempdir())
    tmp.write(wav_data)
    tmp.close()

    lock = acquire_play_lock()
    try:
        play_wav_file(tmp.name)
    finally:
        release_play_lock(lock)
        Path(tmp.name).unlink(missing_ok=True)


def speak(text: str, speaker: int | None = None, speed: float | None = None, background: bool = False):
    """CLI 直指定・stdin 用の入り口。play と同じ抽出を通すので、A『…』などの記法がそのまま効く
    （VOICEVOX_SUPPORTER_AI_GUIDE が案内する形。かつては素通しで、ラベルまで読み上げていた）。
    記法が1件も無いプレーンテキストは全行を既定の声で読む。-s の明示があればその声を既定にする。
    background=True（--bg）はフックと同じ切り離しワーカーに投げてすぐ返る
    （呼び出し元が先に終了しても再生は続き、stop で止められる）。"""
    cfg = load_config()
    default_speaker = speaker if speaker is not None else cfg.get("speaker", DEFAULT_SPEAKER)
    segments = extract_script_segments(
        text, cfg.get("styles", {}), default_speaker, get_markers(cfg), cfg.get("cast", {})
    )
    if not segments:
        return
    if background:
        _spawn_speak_worker(segments, speed)
    else:
        speak_segments(segments, speed=speed)


def clamp_speed(value: float) -> float:
    return max(0.5, min(2.0, value))


# 読み上げ速度設定1〜5。速度2が「通常」（普段の通知・台本再生の速さ）。速度1はゆっくり枠
# （自動では使われず、A1『…』のような数字記法から指名する）。速度3〜5は速い側で、
# 長文の自動早口（stepChars のしきい値）と数字記法の両方から使われる
DEFAULT_SPEED_STEPS = [0.8, 1.3, 1.4, 1.55, 1.7]
DEFAULT_STEP_CHARS = [80, 200, 400]  # 速度3〜5に対応するしきい値文字数（空リストで自動早口なし）


def get_speed_steps(cfg: dict) -> list[float]:
    """読み上げ速度設定1〜5を設定から取る。旧設定（speed だけの構成）は速度2に引き継ぐ。"""
    steps = cfg.get("speedSteps")
    if not isinstance(steps, list) or len(steps) != 5:
        steps = list(DEFAULT_SPEED_STEPS)
        steps[1] = cfg.get("speed", DEFAULT_SPEED)
    return [clamp_speed(float(s)) for s in steps]


def calc_auto_speed(total_chars: int, steps: list, step_chars: list | None = None) -> float:
    """通知の合計文字数から話速を決める（速度は通知1件につき1回だけ決まる）。
    steps は速度1〜5の話速。step_chars は速度3〜5のしきい値文字数で、
    しきい値以上ならその速度に切り替える。届かなければ通常（速度2）。
    None は初期値のしきい値、空リストは自動加速オフ。台本再生（play/paste）には効かない。"""
    if step_chars is None:
        step_chars = DEFAULT_STEP_CHARS
    speed = steps[1]
    for i, chars in enumerate(sorted(step_chars)[:3]):
        if total_chars >= int(chars):
            speed = steps[min(2 + i, 4)]
    return clamp_speed(speed)


def _format_steps(cfg: dict) -> str:
    """status/config 表示用: 読み上げ速度設定を「速度1:×0.8 速度2:×1.3=通常 速度3:×1.4(80字〜)…」の1行にする。"""
    steps = get_speed_steps(cfg)
    chars = cfg.get("stepChars")
    chars = DEFAULT_STEP_CHARS if chars is None else sorted(int(c) for c in chars)[:3]

    def tag(idx: int) -> str:
        if idx == 1:
            return "=通常"
        if idx >= 2 and idx - 2 < len(chars):
            return f"({chars[idx - 2]}字〜)"
        return ""

    body = "  ".join(f"速度{i + 1}:×{s}{tag(i)}" for i, s in enumerate(steps))
    return body if chars else body + "  [文字数による自動早口なし]"


def _synth_segment(text: str, speaker: int | None, speed: float) -> bytes:
    text = strip_ruby(text)
    cfg = load_config()
    limit = cfg.get("maxChars", MAX_CHARS)
    if len(text) > limit:
        suffix = cfg.get("overflowText", OVERFLOW_TEXT)
        text = text[:limit] + (f"。{suffix}。" if suffix else "")
    return synthesize(text, speaker, speed)


def _segment_parts(seg) -> tuple[str, int | None, int | None]:
    """セグメントを (テキスト, スピーカーID, 速度番号 or None) にほどく。
    速度番号は数字添字（A1『…』）があるときだけ付く第3要素（フックの JSON 経由だと list になる）。"""
    return seg[0], seg[1], (seg[2] if len(seg) > 2 else None)


def speak_segments(segments: list, speed: float | None = None):
    """セグメントを順次再生する（play と同じ「先読み」方式）。カラオケの予約のように、
    いま鳴っているセリフの裏で次のセリフを合成しておく。最初のセリフの合成が済んだ時点で
    音が出るので、かつての「全部合成してから連結して一括再生」より初音がずっと早い。"""
    if not segments:
        return
    cfg = load_config()
    steps = get_speed_steps(cfg)
    if speed is not None:
        # 話速が明示されたら固定（play/paste と同じ「指定＝固定」。自動早口は未指定時だけ）
        effective_speed = speed
    else:
        total_chars = sum(len(seg[0]) for seg in segments)
        effective_speed = calc_auto_speed(total_chars, steps, cfg.get("stepChars"))

    def synth(seg) -> bytes:
        # 数字添字つきのセリフは指名された速度で固定（自動早口や --speed より具体的な指定として勝つ）
        text, speaker, step = _segment_parts(seg)
        return _synth_segment(text, speaker, steps[step - 1] if step else effective_speed)

    lock = acquire_play_lock()  # ひとまとまりの途中に他の再生が割り込まないよう、全体でひとつ取る
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(synth, segments[0])
            for n in range(len(segments)):
                wav = future.result()
                if n + 1 < len(segments):
                    future = pool.submit(synth, segments[n + 1])
                _play_wav(wav)
    finally:
        release_play_lock(lock)


# --- Voice notation extraction ---

CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RE = re.compile(r"``[^`]+``|`[^`]+`")

# ルビ記法（見た目は青空文庫と同じ）。発音を変える指定は ｜振り先《よみ》 の明示だけ——
# 機械が実際に発音してしまう以上、どこからどこまでが指定なのかを書き手に見える形で
# 書かせる（黙読で流せる紙のルビとの違い）。｜は全角/半角どちらも可
RUBY_RE = re.compile(r"[｜|]([^《》｜|\n]*)《([^《》\n]+)》")
# ｜のない 漢字《かな》 は「見た目の注記」扱い: 《かな》を読み飛ばすだけで発音は変えない。
# 青空文庫からの貼り付けを二重読みで壊さないための受け皿
RUBY_NOTE_RE = re.compile(r"(?<=[一-鿿々〆ヶ〇])《[^《》\n]+》")


def strip_ruby(text: str) -> str:
    """ルビ記法を合成用テキストにほどく。｜振り先《よみ》 は振り先ごと読みに置換
    （理を「ことわり」と読むか「り」と読むかはここで効く）。｜のない 漢字《かな》 は
    《かな》を取り除くだけ（発音は変えない）。合成の直前と外部ツール向けの書き出しで
    使う——抽出より後の層なので、括弧記法を《》に変えている構成とも衝突しない。"""
    text = RUBY_RE.sub(lambda m: m.group(2), text)
    return RUBY_NOTE_RE.sub("", text)

# スタイル指定: マーカー直後の （怒り） / (怒り)
# stylefull は括弧ごとの原文。登録済みスタイル名と一致しないときは本文として読み戻す
STYLE_PAREN = r"(?P<stylefull>[（(](?P<stylename>[^（）()]{1,20})[）)])?"

DEFAULT_MARKERS = {
    "voiceTag": True,           # <voice>タグ記法
    "linePrefix": None,         # 行頭マーカー記法 例: "♪"（None で無効）
    "brackets": ["『", "』"],   # 括弧記法 例: ["《", "》"] や ["「", "」."]（None で無効）
    "requireLabel": False,      # True なら A『…』のようにキャストラベル付きの括弧だけ読む
}


def get_markers(cfg: dict) -> dict:
    saved = cfg.get("markers", {})
    return {key: saved.get(key, default) for key, default in DEFAULT_MARKERS.items()}


def strip_code(text: str) -> str:
    text = CODE_BLOCK_RE.sub("", text)
    text = INLINE_CODE_RE.sub("", text)
    return text


def _blank_span(text: str) -> str:
    """マッチ済み領域を、文字位置と行構造を保ったまま無効化する。"""
    return "".join("\n" if ch == "\n" else " " for ch in text)


def extract_voice_segments(
    text: str,
    styles: dict,
    default_speaker: int,
    markers: dict | None = None,
    cast: dict | None = None,
) -> list[tuple]:
    """有効な記法すべてから (テキスト, スピーカーID) を出現順に抽出する。コードブロック内は無視。
    キャスト記法に数字添字（A1『…』=読み上げ速度1〜5）があるセグメントだけは
    (テキスト, スピーカーID, 速度番号) の3要素になる（速度の解決は再生側の仕事）。"""
    if markers is None:
        markers = DEFAULT_MARKERS
    cast = cast or {}
    cleaned = strip_code(text)
    found = []  # (位置, テキスト, スピーカーID, 速度番号 or None)

    def resolve_style(gd: dict, member_styles: dict, base_speaker: int) -> tuple[int, str]:
        """スタイル括弧を解決する。登録済みスタイル名と一致したときだけスタイル扱いにして、
        それ以外（『（笑）そうだね』等）は括弧ごと本文として読み戻す。"""
        name = gd.get("stylename")
        if name and name in member_styles:
            return member_styles[name], ""
        return base_speaker, gd.get("stylefull") or ""

    def consume(pattern: re.Pattern, resolve):
        nonlocal cleaned
        for match in pattern.finditer(cleaned):
            gd = match.groupdict()
            speaker, prefix = resolve(gd)
            step = int(gd["speedstep"]) if gd.get("speedstep") else None
            for i, line in enumerate((prefix + gd["content"]).splitlines()):
                line = line.strip()
                if line:
                    found.append((match.start() + i, line, speaker, step))
        # 抽出済み領域を消して、他の記法との二重マッチを防ぐ
        cleaned = pattern.sub(lambda m: _blank_span(m.group(0)), cleaned)

    if markers.get("voiceTag"):
        # タグのstyle属性は本文に現れないので、未知の名前は黙ってデフォルト声に落とす
        def resolve_tag(gd):
            name = gd.get("stylename")
            return (styles.get(name, default_speaker) if name else default_speaker), ""

        consume(VOICE_TAG_RE, resolve_tag)

    brackets = markers.get("brackets")
    require_label = markers.get("requireLabel", False)
    if brackets and len(brackets) == 2 and not (require_label and not cast):
        open_s, close_s = brackets
        label_part = ""
        if cast:
            # ラベル（大文字1文字）＋任意の小文字スタイル添字＋任意の数字添字（読み上げ速度1〜5）。
            # 直前が英数字なら不成立（英単語の末尾誤爆防止）。並びは「スタイル→数字」固定（例: Ad1）
            alts = "|".join(re.escape(label) for label in sorted(cast, key=len, reverse=True))
            label_part = (r"(?:(?<![A-Za-z0-9])(?P<label>" + alts
                          + r")(?P<stylesuffix>[a-z])?(?P<speedstep>[1-5])?)")
            if not require_label:
                label_part += "?"
        # 内容に括弧文字そのものは含めない（入れ子や複数ペアまたぎの誤結合を防ぐ）
        content = r"(?P<content>[^" + re.escape(open_s[0]) + re.escape(close_s[0]) + r"]+?)"
        pattern = re.compile(label_part + re.escape(open_s) + STYLE_PAREN + content + re.escape(close_s))

        def resolve_bracket(gd):
            member = cast.get(gd.get("label") or "")
            if member is None and cast:
                # 素の『』はラベル省略の書き方とみなし、先頭のキャスト（通常A）の声で読む。
                # スタイル括弧『（ささやき）…』もそのキャストのスタイル表で解決される
                member = cast[min(cast)]
            if member:
                member_styles = member.get("styles", {})
                ids = list(member_styles.values())
                # 添字なし = a = 1番目のスタイル。表記と声が必ず一致する（隠れたデフォルトなし）
                base = ids[0] if ids else member.get("speaker", default_speaker)
                suffix = gd.get("stylesuffix")
                if suffix and ids:
                    idx = ord(suffix) - ord("a")
                    if 0 <= idx < len(ids):
                        base = ids[idx]
                    # 範囲外の添字は a と同じ扱い
                return resolve_style(gd, member_styles, base)
            return resolve_style(gd, styles, default_speaker)

        consume(pattern, resolve_bracket)

    prefix = markers.get("linePrefix")
    if prefix:
        pattern = re.compile(
            r"^[ \t]*" + re.escape(prefix) + STYLE_PAREN + r"[ \t]*(?P<content>\S.*)$", re.MULTILINE
        )
        consume(pattern, lambda gd: resolve_style(gd, styles, default_speaker))

    found.sort(key=lambda item: item[0])
    return [(line, speaker) if step is None else (line, speaker, step)
            for _, line, speaker, step in found]


def extract_script_segments(
    text: str,
    styles: dict,
    default_speaker: int,
    markers: dict | None = None,
    cast: dict | None = None,
) -> list[tuple[str, int]]:
    """台本ファイル用の抽出。記法が1件でも見つかれば通常抽出と同じ。
    記法なしのプレーンテキストは全行をデフォルト声で読む（コード除外は維持）。"""
    segments = extract_voice_segments(text, styles, default_speaker, markers, cast)
    if segments:
        return segments
    lines = [l.strip() for l in strip_code(text).splitlines() if l.strip()]
    return [(line, default_speaker) for line in lines]


def find_style_info(speaker_id: int) -> tuple[str, str, dict[str, int]] | None:
    """speaker_idから (キャラ名, スタイル名, そのキャラの全スタイル) を引く。"""
    try:
        data = json.loads(vv_request("/speakers"))
    except Exception:
        return None
    for sp in data:
        for style in sp.get("styles", []):
            if style.get("id") == speaker_id:
                return (
                    sp.get("name", "?"),
                    style.get("name", "?"),
                    {s["name"]: s["id"] for s in sp["styles"]},
                )
    return None


def detect_styles(speaker_id: int) -> dict[str, int]:
    """現在のspeaker_idが属するキャラクターの全スタイルを返す。"""
    info = find_style_info(speaker_id)
    return info[2] if info else {}


# --- Engine management ---

MIN_ENGINE_VERSION = (0, 25, 2)  # 動作検証済みの下限（暁記ミタマ・夜語トバリ収録版）


def parse_version(ver: str) -> tuple[int, ...]:
    """'0.25.2' や '0.25.2-preview.1' を比較用タプルにする（先頭3要素）。"""
    return tuple(int(n) for n in re.findall(r"\d+", ver)[:3])


def engine_version() -> str | None:
    try:
        return vv_request("/version").decode().strip('"')
    except Exception:
        return None


def is_engine_running() -> bool:
    try:
        vv_request("/version")
        return True
    except Exception:
        return False


def find_engine_path() -> Path | None:
    cfg = load_config()
    custom = cfg.get("enginePath")
    if custom:
        p = Path(custom)
        if p.exists():
            return p
    for p in VOICEVOX_ENGINE_PATHS:
        if p.exists():
            return p
    found = shutil.which("run", path=None)
    if found and "voicevox" in found.lower():
        return Path(found)
    return None


def ensure_engine():
    """VOICEVOXエンジンが起動していなければ起動する。"""
    if is_engine_running():
        return True

    engine = find_engine_path()
    if engine is None:
        print("VOICEVOX engine not found", file=sys.stderr)
        return False

    host = vv_host()
    try:
        port = host.rsplit(":", 1)[1].rstrip("/")
    except (IndexError, ValueError):
        port = "50021"
    print(f"Starting VOICEVOX engine: {engine}")
    subprocess.Popen(
        [str(engine), "--host", "127.0.0.1", "--port", port],
        **_detached_popen_kwargs(),
    )

    for i in range(30):
        time.sleep(1)
        if is_engine_running():
            print("VOICEVOX engine ready")
            return True
        if i % 5 == 4:
            print(f"  Waiting... ({i + 1}s)")

    print("VOICEVOX engine failed to start", file=sys.stderr)
    return False


# --- Hook ---

def _pid_file_path(pid: int) -> Path:
    return Path(tempfile.gettempdir()) / f"vvtts_{pid}.pid"


def _spawn_speak_worker(segments: list, speed: float | None = None):
    """セグメントを切り離しワーカーに渡して合成・再生させる（フック・serve 共通の裏方）。"""
    payload = tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, prefix="vvtts_seg_",
        dir=tempfile.gettempdir(), encoding="utf-8",
    )
    json.dump({"segments": segments, "speed": speed}, payload, ensure_ascii=False)
    payload.close()
    subprocess.Popen(
        self_cmd("--hook-worker", payload.name),
        **_detached_popen_kwargs(),
    )


def handle_hook():
    """応答完了フック本体。テキストの検出だけして即終了し、
    合成・再生（時間がかかる）は切り離したワーカープロセスに任せる。
    こうすることで呼び出し元の応答完了処理を一切待たせない。
    Claude Code / Codex (last_assistant_message) と Gemini CLI (prompt_response) に対応。"""
    if not FLAG_PATH.exists():
        return

    try:
        raw = sys.stdin.buffer.read()
        data = json.loads(raw)
    except Exception:
        return

    full_text = data.get("last_assistant_message") or data.get("prompt_response", "")
    if not full_text:
        return

    cfg = load_config()
    styles = cfg.get("styles", {})
    default_speaker = cfg.get("speaker", DEFAULT_SPEAKER)
    segments = extract_voice_segments(
        full_text, styles, default_speaker, get_markers(cfg), cfg.get("cast", {})
    )
    if not segments:
        return

    try:
        _spawn_speak_worker(segments)
    except Exception:
        pass


def handle_hook_worker(payload_path: str):
    """フックから切り離されたワーカー。エンジン確認〜合成〜再生をここでやる。
    stop コマンドから殺せるように自分のPIDをファイルに残す。"""
    pid_file = _pid_file_path(os.getpid())
    try:
        pid_file.write_text(str(os.getpid()))
        p = Path(payload_path)
        data = json.loads(p.read_text(encoding="utf-8"))
        p.unlink(missing_ok=True)
        segments = data.get("segments", [])
        if not segments:
            return

        if not is_engine_running():
            cfg = load_config()
            if not cfg.get("autoStart", True):
                return
            if not ensure_engine():
                return

        speak_segments(segments, speed=data.get("speed"))
    except Exception:
        pass
    finally:
        pid_file.unlink(missing_ok=True)


# --- CLI commands ---

def list_speakers():
    data = vv_request("/speakers")
    speakers = json.loads(data)
    for sp in speakers:
        name = sp.get("name", "?")
        styles = sp.get("styles", [])
        style_strs = [f'{s.get("name", "?")}(id={s.get("id", 0)})' for s in styles]
        print(f"  {name}: {', '.join(style_strs)}")


def cmd_config(args: list[str]):
    if not args:
        cfg = load_config()
        print(f"  speaker:        {cfg.get('speaker', DEFAULT_SPEAKER)}")
        print(f"  favorites:      {cfg.get('favorites', '(not set)')}")
        print(f"  speed:          {cfg.get('speed', DEFAULT_SPEED)}")
        print(f"  speedSteps:     {_format_steps(cfg)}")
        print(f"  maxChars:       {cfg.get('maxChars', MAX_CHARS)}")
        print(f"  overflowText:   {cfg.get('overflowText', OVERFLOW_TEXT) or '(なし=黙って打ち切り)'}")
        print(f"  prePhoneme:     {cfg.get('prePhonemeLength', 0.8)}")
        print(f"  intonation:     {cfg.get('intonationScale', 1.0)}")
        print(f"  pause:          {cfg.get('pauseLengthScale', 1.0)}")
        print(f"  host:           {cfg.get('host', VOICEVOX_DEFAULT_HOST)}")
        print(f"  enginePath:     {cfg.get('enginePath', '(auto-detect)')}")
        print(f"  autoStart:      {'on' if cfg.get('autoStart', True) else 'off'}")
        print(f"  servePort:      {cfg.get('servePort', DEFAULT_SERVE_PORT)}")
        markers = get_markers(cfg)
        br = markers["brackets"]
        print(f"  voiceTag:       {'on' if markers['voiceTag'] else 'off'}")
        print(f"  lineMarker:     {markers['linePrefix'] or '(off)'}")
        print(f"  brackets:       {(br[0] + '…' + br[1]) if br else '(off)'}")
        print(f"  requireLabel:   {'on' if markers['requireLabel'] else 'off'}")
        styles = cfg.get("styles", {})
        if styles:
            pairs = [f"{name}={sid}" for name, sid in styles.items()]
            print(f"  styles:         {', '.join(pairs)}")
        else:
            print(f"  styles:         (not set, run 'start' to auto-detect)")
        return

    if len(args) < 2:
        print("Usage: speak.py config <key> <value>")
        return

    key = args[0]
    cfg = load_config()
    try:
        if key == "speaker":
            cfg["speaker"] = int(args[1])
        elif key == "speed":
            val = clamp_speed(float(args[1]))
            cfg["speed"] = val
            if isinstance(cfg.get("speedSteps"), list) and len(cfg["speedSteps"]) == 5:
                cfg["speedSteps"][1] = val
        elif key == "speedSteps":
            vals = args[1:]
            if len(vals) != 5:
                print("Usage: speak.py config speedSteps <速度1 速度2 速度3 速度4 速度5>（速度2=通常。例: 0.8 1.3 1.4 1.55 1.7）")
                return
            cfg["speedSteps"] = [clamp_speed(float(v)) for v in vals]
            cfg["speed"] = cfg["speedSteps"][1]
        elif key == "stepChars":
            if args[1].lower() in ("off", "none"):
                cfg["stepChars"] = []
            else:
                cfg["stepChars"] = sorted(max(1, int(v)) for v in args[1:4])
        elif key == "maxChars":
            cfg["maxChars"] = max(100, min(2000, int(args[1])))
        elif key == "host":
            cfg["host"] = args[1]
        elif key == "overflowText":
            cfg["overflowText"] = " ".join(args[1:])
        elif key == "enginePath":
            cfg["enginePath"] = args[1]
        elif key == "prePhonemeLength":
            cfg["prePhonemeLength"] = max(0.0, min(2.0, float(args[1])))
        elif key == "intonationScale":
            cfg["intonationScale"] = max(0.0, min(2.0, float(args[1])))
        elif key == "pauseLengthScale":
            cfg["pauseLengthScale"] = max(0.0, min(2.0, float(args[1])))
        elif key == "favorites":
            cfg["favorites"] = [int(v) for v in args[1:]]
        elif key == "autoStart":
            cfg["autoStart"] = args[1].lower() in ("on", "true", "1")
        elif key == "servePort":
            port = int(args[1])
            if not 1024 <= port <= 65535:
                print("servePort は 1024〜65535 の範囲で指定してください")
                return
            cfg["servePort"] = port
        elif key == "voiceTag":
            cfg.setdefault("markers", {})["voiceTag"] = args[1].lower() in ("on", "true", "1")
        elif key == "lineMarker":
            value = None if args[1].lower() in ("off", "none") else args[1]
            cfg.setdefault("markers", {})["linePrefix"] = value
        elif key == "brackets":
            if args[1].lower() in ("off", "none"):
                cfg.setdefault("markers", {})["brackets"] = None
            elif len(args) >= 3:
                cfg.setdefault("markers", {})["brackets"] = [args[1], args[2]]
            elif len(args[1]) >= 2:
                cfg.setdefault("markers", {})["brackets"] = [args[1][0], args[1][1:]]
            else:
                print("Usage: speak.py config brackets <開き> <閉じ> | off")
                return
        elif key == "requireLabel":
            cfg.setdefault("markers", {})["requireLabel"] = args[1].lower() in ("on", "true", "1")
        else:
            print(f"Unknown key: {key} (speaker, speed, speedSteps, stepChars, maxChars, overflowText, host, enginePath, favorites, autoStart, servePort, voiceTag, lineMarker, brackets, requireLabel)")
            return
    except ValueError:
        print(f"config {key}: 数値を指定してください")
        return
    save_config(cfg)
    if key == "favorites":
        print(f"Set favorites = {cfg['favorites']}")
    elif key in ("voiceTag", "lineMarker", "brackets", "requireLabel"):
        markers = get_markers(cfg)
        display = {
            "voiceTag": "on" if markers["voiceTag"] else "off",
            "lineMarker": markers["linePrefix"] or "off",
            "brackets": "".join(markers["brackets"]) if markers["brackets"] else "off",
            "requireLabel": "on" if markers["requireLabel"] else "off",
        }
        print(f"Set {key} = {display[key]}")
    else:
        print(f"Set {key} = {args[1]}")


def cmd_cast(args: list[str]):
    cfg = load_config()
    cast = cfg.get("cast", {})
    if not args or args[0] == "list":
        if not cast:
            print("  (no cast members)")
            print("  Usage: speak.py cast set <ラベル> <スピーカーID>  (IDは -l で確認)")
            return
        for label, member in cast.items():
            names = list(member.get("styles", {}))
            mapped = " ".join(f"{label}{chr(ord('a') + i)}={name}" for i, name in enumerate(names))
            extra = f"  {mapped}" if mapped else f"  id={member.get('speaker')}"
            print(f"  {label}: {member.get('name', '?')}{extra}")
        return
    if args[0] == "set" and len(args) >= 3:
        label = args[1]
        if not re.fullmatch(r"[A-Z]", label):
            print(f"Invalid label: {label}（ラベルは大文字アルファベット1文字。スタイルは Ab のように小文字添字で切り替える）")
            return
        member = {"speaker": int(args[2])}
        info = find_style_info(member["speaker"])
        if info:
            member["name"], _, member["styles"] = info
            names = list(member["styles"])
            if names:
                # 添字なし表記(=a)と一致するよう、基準は常に1番目のスタイル
                member["style"] = names[0]
                member["speaker"] = member["styles"][names[0]]
            mapped = " ".join(f"{label}{chr(ord('a') + i)}={n}" for i, n in enumerate(names))
            print(f"Cast {label} = {member['name']}  {mapped}")
            print(f"  {label}『…』は {label}a と同じ（1番目のスタイル）。他は小文字添字で指定")
        else:
            print(f"Cast {label} = id={member['speaker']} (エンジン未起動のためスタイル未取得。start 時に補完される)")
        cast[label] = member
        cfg["cast"] = cast
        save_config(cfg)
        return
    if args[0] in ("remove", "rm") and len(args) >= 2:
        if cast.pop(args[1], None) is not None:
            cfg["cast"] = cast
            save_config(cfg)
            print(f"Removed cast: {args[1]}")
        else:
            print(f"Not found: {args[1]}")
        return
    if args[0] == "refresh":
        # エンジン更新でスタイルが増減したときに、表を明示的に取り直す
        for label, member in cast.items():
            info = find_style_info(member.get("speaker"))
            if info:
                member["name"], _, member["styles"] = info
                names = list(member["styles"])
                if names:
                    member["style"] = names[0]
                    member["speaker"] = member["styles"][names[0]]
                print(f"  {label}: {member['name']} styles: {', '.join(member['styles'])}")
            else:
                print(f"  {label}: id={member.get('speaker')} が見つからない（エンジン未起動 or 廃止ID）")
        cfg["cast"] = cast
        save_config(cfg)
        return
    print("Usage: speak.py cast [list] | set <ラベル> <スピーカーID> | remove <ラベル> | refresh")


def cmd_reading(args: list[str]):
    user = load_user_readings()
    if not args or args[0] == "list":
        merged = dict(READING_FIXES)
        merged.update(user)
        if not merged:
            print("  (no readings)")
            return
        for surface in sorted(merged, key=len, reverse=True):
            origin = "user    " if surface in user else "built-in"
            print(f"  [{origin}] {surface} -> {merged[surface]}")
        print(f"  (user dictionary: {READINGS_PATH})")
        return
    if args[0] == "add" and len(args) >= 3:
        user[args[1]] = args[2]
        save_user_readings(user)
        print(f"Added reading: {args[1]} -> {args[2]}")
        return
    if args[0] in ("remove", "rm") and len(args) >= 2:
        if user.pop(args[1], None) is not None:
            save_user_readings(user)
            print(f"Removed reading: {args[1]}")
        elif args[1] in READING_FIXES:
            print(f"'{args[1]}' is built-in. Override it: speak.py reading add {args[1]} <よみ>")
        else:
            print(f"Not found: {args[1]}")
        return
    print("Usage: speak.py reading [list] | add <表記> <よみ> | remove <表記>")


def _read_script_file(path: Path) -> str:
    """台本ファイルを読む。UTF-8（BOM可）優先、古いメモ帳の ANSI 保存にも対応。"""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "cp932"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def parse_script_args(args: list[str]) -> tuple[list[str], Path | None, float | None, bool]:
    """play/paste 共通のオプション解析。(残りの引数, --out, --speed, --auto) を返す。"""
    out_path = None
    speed = None
    auto = False
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "--out" and i + 1 < len(args):
            out_path = Path(args[i + 1])
            i += 2
        elif args[i] == "--speed" and i + 1 < len(args):
            speed = clamp_speed(float(args[i + 1]))
            i += 2
        elif args[i] == "--auto":
            auto = True
            i += 1
        else:
            rest.append(args[i])
            i += 1
    return rest, out_path, speed, auto


def _run_script(text: str, out_path: Path | None, speed: float | None, auto: bool = False):
    """台本テキストを抽出して、WAV書き出しまたはプリフェッチ順次再生する。play/paste 共通。"""
    cfg = load_config()
    segments = extract_script_segments(
        text,
        cfg.get("styles", {}),
        cfg.get("speaker", DEFAULT_SPEAKER),
        get_markers(cfg),
        cfg.get("cast", {}),
    )
    if not segments:
        print("読み上げるテキストがありません")
        return

    if not ensure_engine():
        sys.exit(1)

    # 台本は鑑賞用途なので、既定では文字数による自動加速をかけず等速（速度2）。
    # --auto 指定時は通知と同じ「読み上げ速度の自動調整」を【セリフ1件ごとの文字数】で判定する
    # （当初は台本の合計文字数で1つの速度を決めていたが、短い掛け合いまで最速で読まれてしまう。
    # 2026-07-05 修正）。--speed の明示は自動調整より勝ち、数字添字（A1『…』）のセリフはさらに勝つ
    steps = get_speed_steps(cfg)
    step_chars = cfg.get("stepChars")

    def line_speed(line: str) -> float:
        if speed is not None:
            return speed
        if auto:
            return calc_auto_speed(len(line), steps, step_chars)
        return steps[1]

    def synth_line(seg) -> bytes:
        line, speaker, step = _segment_parts(seg)
        return _synth_segment(line, speaker, steps[step - 1] if step else line_speed(line))

    if out_path:
        chunks = []
        for n, seg in enumerate(segments, 1):
            # flush: GUI がパイプ越しに進捗を拾う（exe には -u が無いので明示フラッシュが頼り）
            print(f"[{n}/{len(segments)}] {seg[0][:40]}", flush=True)
            chunks.append(synth_line(seg))
        out_path.write_bytes(concat_wav(chunks))
        print(f"Saved: {out_path}")
        print("※書き出した音声の利用は、各キャラクターの利用規約（クレジット表記等）に従ってください")
        return

    # 順次再生。次のセリフを裏で合成しながら今のセリフを鳴らして、行間の待ちを短くする
    pid_file = _pid_file_path(os.getpid())
    pid_file.write_text(str(os.getpid()))
    lock = acquire_play_lock()  # 台本の途中に他の再生が割り込まないよう、全体でひとつ取る
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(synth_line, segments[0])
            for n, seg in enumerate(segments):
                wav = future.result()
                if n + 1 < len(segments):
                    future = pool.submit(synth_line, segments[n + 1])
                print(f"[{n + 1}/{len(segments)}] {seg[0][:40]}", flush=True)
                _play_wav(wav)
    except KeyboardInterrupt:
        print("\n(interrupted)")
    finally:
        release_play_lock(lock)
        pid_file.unlink(missing_ok=True)


def cmd_play(args: list[str]):
    files, out_path, speed, auto = parse_script_args(args)
    if len(files) != 1:
        print("Usage: speak.py play <台本ファイル> [--out 出力.wav] [--speed 0.5-2.0] [--auto]")
        return
    path = Path(files[0])
    if not path.exists():
        print(f"File not found: {path}", file=sys.stderr)
        sys.exit(1)
    _run_script(_read_script_file(path), out_path, speed, auto)


def cmd_paste(args: list[str]):
    rest, out_path, speed, auto = parse_script_args(args)
    if rest:
        print("Usage: speak.py paste [--out 出力.wav] [--speed 0.5-2.0] [--auto]")
        return
    text = read_clipboard_text()
    if not text or not text.strip():
        print("クリップボードにテキストがありません")
        return
    _run_script(text, out_path, speed, auto)


def stop_playback() -> int:
    """再生中（合成中含む）のワーカー/プレイヤーを止めて、残骸ファイルを掃除する。"""
    tmpdir = Path(tempfile.gettempdir())
    killed = 0
    for pid_file in tmpdir.glob("vvtts_*.pid"):
        try:
            pid = int(pid_file.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except (OSError, ValueError):
            pass  # すでに終了している等
        pid_file.unlink(missing_ok=True)
    # 強制終了だと各プロセスの後始末が走らないので、こちらで掃除する
    for leftover in list(tmpdir.glob("vvtts_*.wav")) + list(tmpdir.glob("vvtts_seg_*.json")):
        try:
            leftover.unlink()
        except OSError:
            pass
    return killed


def cmd_stop():
    killed = stop_playback()
    print(f"Stopped {killed} process(es)" if killed else "Nothing playing")


# --- serve (ローカルHTTP待ち受け・他ソフトからの流し込み口) ---

DEFAULT_SERVE_PORT = 51021  # 棒読みちゃん(50001/50080)や各社エンジンの500xx帯を避けた独自ポート
SERVE_STATE_PATH = Path(tempfile.gettempdir()) / "vvtts_serve.json"
SERVE_MAX_BODY = 1_000_000  # 台本1本には十分。桁違いの巨大ボディだけ弾く


def _serve_state() -> dict | None:
    """serve の稼働メモ（PIDとポート）。serve 起動中だけ存在する。"""
    if SERVE_STATE_PATH.exists():
        try:
            return json.loads(SERVE_STATE_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return None


def _serve_request(port: int, path: str, method: str = "GET", timeout: float = 3.0) -> bytes:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _serve_shutdown():
    state = _serve_state()
    port = state["port"] if state else load_config().get("servePort", DEFAULT_SERVE_PORT)
    try:
        _serve_request(port, "/shutdown", method="POST")
        print(f"serve を停止しました (port {port})")
    except Exception:
        print(f"serve は動いていません (port {port})")
        SERVE_STATE_PATH.unlink(missing_ok=True)


def cmd_serve(args: list[str]):
    if args and args[0] == "stop":
        _serve_shutdown()
        return

    port = None
    if args and args[0] == "--port":
        try:
            port = int(args[1])
        except (IndexError, ValueError):
            port = 0
        args = args[2:]
        if not 1024 <= port <= 65535:
            print("Usage: speak.py serve [--port 1024-65535] | serve stop")
            return
    if args:
        print("Usage: speak.py serve [--port 1024-65535] | serve stop")
        return
    if port is None:
        port = load_config().get("servePort", DEFAULT_SERVE_PORT)

    state = _serve_state()
    if state:
        try:
            _serve_request(state["port"], "/status", timeout=1.0)
            print(f"serve はすでに動いています (port {state['port']})", file=sys.stderr)
            sys.exit(1)
        except Exception:
            SERVE_STATE_PATH.unlink(missing_ok=True)  # 前回異常終了の残骸なので消して進む

    # http.server はフック経路では使わないので、起動を重くしないようここで import する
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class TalkHandler(BaseHTTPRequestHandler):
        def _respond(self, code: int, obj: dict):
            data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt, *args):
            sys.stderr.write(f"  {time.strftime('%H:%M:%S')} {fmt % args}\n")

        def _talk(self, text: str, speed_raw: str | None):
            speed = None
            if speed_raw is not None:
                try:
                    speed = clamp_speed(float(speed_raw))
                except ValueError:
                    self._respond(400, {"ok": False, "error": "invalid speed"})
                    return
            if not text.strip():
                self._respond(400, {"ok": False, "error": "text is empty"})
                return
            cfg = load_config()
            segments = extract_script_segments(
                text,
                cfg.get("styles", {}),
                cfg.get("speaker", DEFAULT_SPEAKER),
                get_markers(cfg),
                cfg.get("cast", {}),
            )
            if not segments:
                self._respond(200, {"ok": True, "segments": 0})
                return
            try:
                # 合成・再生は切り離しワーカーへ（受理したら即応答。stop で止められるのも同じ）
                _spawn_speak_worker(segments, speed)
            except Exception as e:
                self._respond(500, {"ok": False, "error": str(e)})
                return
            self._respond(202, {"ok": True, "segments": len(segments)})

        def do_GET(self):
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path == "/talk":
                self._talk((query.get("text") or [""])[0], (query.get("speed") or [None])[0])
            elif url.path == "/status":
                try:
                    ver = vv_request("/version").decode().strip('"')
                except Exception:
                    ver = None
                self._respond(200, {"ok": True, "engine": "running" if ver else "stopped", "engineVersion": ver})
            else:
                self._respond(404, {"ok": False, "error": "not found"})

        def do_POST(self):
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path == "/talk":
                try:
                    length = int(self.headers.get("Content-Length", ""))
                except ValueError:
                    self._respond(411, {"ok": False, "error": "Content-Length required"})
                    return
                if length > SERVE_MAX_BODY:
                    self._respond(413, {"ok": False, "error": "body too large"})
                    return
                try:
                    text = self.rfile.read(length).decode("utf-8")
                except UnicodeDecodeError:
                    self._respond(400, {"ok": False, "error": "body must be UTF-8 text"})
                    return
                self._talk(text, (query.get("speed") or [None])[0])
            elif url.path == "/stop":
                self._respond(200, {"ok": True, "stopped": stop_playback()})
            elif url.path == "/shutdown":
                self._respond(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._respond(404, {"ok": False, "error": "not found"})

    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), TalkHandler)  # 127.0.0.1 固定（外部公開しない）
    except OSError as e:
        print(f"ポート {port} で待ち受けできません ({e})", file=sys.stderr)
        sys.exit(1)

    SERVE_STATE_PATH.write_text(json.dumps({"pid": os.getpid(), "port": port}), encoding="utf-8")
    print(f"serve: http://127.0.0.1:{port} で待ち受け中")
    print("  POST /talk           本文のテキストを読み上げ（台本記法もそのまま通る）")
    print("  GET  /talk?text=...  短文用（ブラウザや curl から手軽に）")
    print("  GET  /status         動作確認 / POST /stop 再生停止")
    print("  終了: Ctrl+C か  python speak.py serve stop", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n(serve stopped)")
    finally:
        server.server_close()
        SERVE_STATE_PATH.unlink(missing_ok=True)


def install_hook(settings_path: Path | None = None) -> dict:
    """Claude Code のグローバル設定に Stop フックを追記する（CLI と GUI の共通本体）。
    既存の設定は保持し、書き換える前にバックアップを作る。二重登録はしない（何度実行しても安全）。
    設定ファイルが JSON として壊れているときは RuntimeError（壊れたまま上書きしない）。
    返り値: {"status": "already"|"updated"|"added", "path", "command", "backup", "old"}"""
    settings_path = settings_path or Path.home() / ".claude" / "settings.json"
    command = f"{self_cmd_str()} --hook"

    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception as e:
            raise RuntimeError(f"設定ファイルが読めません: {settings_path} ({e})") from e

    stop_hooks = settings.setdefault("hooks", {}).setdefault("Stop", [])
    result = {"status": "added", "path": settings_path, "command": command, "backup": None, "old": None}
    for entry in stop_hooks:
        for hook in entry.get("hooks", []):
            cmd_str = hook.get("command", "")
            # ソース版（…speak.py --hook）と exe 版（…exe --hook）のどちらの登録も自分のものとして扱う
            if "--hook" in cmd_str and ("speak.py" in cmd_str or "voibo" in cmd_str.lower()):
                if cmd_str == command:
                    result["status"] = "already"
                    return result
                # 登録済みだがパスが違う＝フォルダの移動・改名後。現在の場所に付け替える
                hook["command"] = command
                result.update(status="updated", old=cmd_str)
                break
        if result["old"]:
            break
    if result["status"] == "added":
        stop_hooks.append({"matcher": "", "hooks": [{"type": "command", "command": command}]})

    if settings_path.exists():
        backup = settings_path.with_name(settings_path.name + ".bak")
        shutil.copy2(settings_path, backup)
        result["backup"] = backup
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return result


def cmd_install_hook(args: list[str]):
    """install-hook サブコマンド。本体は install_hook()、ここは結果の表示だけ。"""
    try:
        result = install_hook(Path(args[0]) if args else None)
    except RuntimeError as e:
        print(e, file=sys.stderr)
        print("壊れたまま上書きすると危険なので、手動で直してから再実行してください", file=sys.stderr)
        sys.exit(1)
    if result["status"] == "already":
        print(f"すでに設定済みです: {result['command']}")
        return
    if result["backup"]:
        print(f"バックアップ: {result['backup']}")
    if result["status"] == "updated":
        print(f"Stop フックのパスを現在の場所に更新しました: {result['path']}")
        print(f"  旧: {result['old']}")
    else:
        print(f"Stop フックを追加しました: {result['path']}")
    print(f"  command: {result['command']}")
    print(f"次の Claude Code セッションから有効。読み上げを有効にするには: {self_cmd_str()} start")


def cmd_doctor():
    """環境診断。「動かない」の原因を自己解決できるように、確認だけして何も変更しない。"""
    problems = 0

    def report(ok: bool | None, label: str, detail: str = ""):
        nonlocal problems
        mark = "[--]" if ok is None else ("[OK]" if ok else "[NG]")
        if ok is False:
            problems += 1
        print(f"  {mark} {label}" + (f": {detail}" if detail else ""))

    report(sys.version_info >= (3, 10), "Python バージョン",
           f"{sys.version_info.major}.{sys.version_info.minor} (3.10 以上を推奨)")

    if CONFIG_PATH.exists():
        try:
            json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            report(True, "設定ファイル", str(CONFIG_PATH))
        except Exception as e:
            report(False, "設定ファイル", f"JSON が壊れている ({e})")
    else:
        report(None, "設定ファイル", "未作成（デフォルト設定で動作。config コマンドで作られる）")

    if READINGS_PATH.exists():
        try:
            json.loads(READINGS_PATH.read_text(encoding="utf-8"))
            report(True, "読み替え辞書", str(READINGS_PATH))
        except Exception as e:
            report(False, "読み替え辞書", f"JSON が壊れている ({e})")
    else:
        report(None, "読み替え辞書", "未作成（reading add で作られる）")

    engine = find_engine_path()
    report(engine is not None, "VOICEVOX エンジン検出",
           str(engine) if engine else "見つからない（VOICEVOX をインストールするか config enginePath で指定）")

    running = is_engine_running()
    if running:
        try:
            ver = vv_request("/version").decode().strip('"')
        except Exception:
            ver = "?"
        report(True, "エンジン起動", f"v{ver} ({vv_host()})")
        if ver != "?":
            ok = parse_version(ver) >= MIN_ENGINE_VERSION
            minimum = ".".join(map(str, MIN_ENGINE_VERSION))
            report(ok, "エンジンバージョン",
                   f"v{ver}" if ok else f"v{ver}（{minimum} 以上を推奨。公式サイトから更新できます）")
    else:
        report(None if engine else False, "エンジン起動",
               "停止中（start で自動起動できる）" if engine else "停止中")

    cfg = load_config()
    if running:
        sp = cfg.get("speaker", DEFAULT_SPEAKER)
        info = find_style_info(sp)
        report(info is not None, f"スピーカー ID {sp}",
               f"{info[0]}（{info[1]}）" if info else "エンジンに存在しない ID（-l で確認して config speaker で設定）")
        for label, member in cfg.get("cast", {}).items():
            m_info = find_style_info(member.get("speaker"))
            report(m_info is not None, f"キャスト {label}",
                   m_info[0] if m_info else f"ID {member.get('speaker')} が見つからない（cast refresh か cast set で直す）")
    else:
        report(None, "スピーカー/キャスト確認", "エンジン停止中のためスキップ")

    hook_installed = False
    settings = Path.home() / ".claude" / "settings.json"
    if settings.exists():
        try:
            hook_installed = "--hook" in settings.read_text(encoding="utf-8")
        except Exception:
            pass
    report(True if hook_installed else None, "Claude Code フック",
           "設定済み" if hook_installed else "未設定（音声通知に使う場合は README のセットアップ参照）")

    report(True if FLAG_PATH.exists() else None, "自動読み上げ",
           "ON" if FLAG_PATH.exists() else "OFF（on または start で有効化）")

    print()
    print("問題は見つかりませんでした" if problems == 0 else f"{problems} 件の問題があります")
    if problems:
        sys.exit(1)


def cmd_status():
    enabled = FLAG_PATH.exists()
    try:
        ver = vv_request("/version").decode().strip('"')
        engine = f"Running (v{ver})"
    except Exception:
        engine = "Not running"
    cfg = load_config()
    print(f"  Auto-speak: {'ON' if enabled else 'OFF'}")
    print(f"  VOICEVOX:   {engine}")
    serve = "Not running"
    state = _serve_state()
    if state:
        try:
            _serve_request(state["port"], "/status", timeout=1.0)
            serve = f"Running (port {state['port']})"
        except Exception:
            pass
    print(f"  Serve:      {serve}")
    print(f"  Speaker:    {cfg.get('speaker', DEFAULT_SPEAKER)}")
    print(f"  Speed:      {_format_steps(cfg)}")
    markers = get_markers(cfg)
    notations = []
    if markers["voiceTag"]:
        notations.append("<voice>タグ")
    if markers["linePrefix"]:
        notations.append(f"行頭 {markers['linePrefix']}")
    if markers["brackets"]:
        br = f"{markers['brackets'][0]}…{markers['brackets'][1]}"
        if markers["requireLabel"]:
            br += "（ラベル必須）"
        notations.append(br)
    print(f"  Notation:   {', '.join(notations) or '(none)'}")
    user_readings = load_user_readings()
    print(f"  Readings:   built-in {len(READING_FIXES)} + user {len(user_readings)}")
    cast = cfg.get("cast", {})
    if cast:
        pairs = [f"{label}={m.get('name', '?')}({m.get('style', '?')})" for label, m in cast.items()]
        print(f"  Cast:       {', '.join(pairs)}")
    styles = cfg.get("styles", {})
    if styles:
        pairs = [f"{name}={sid}" for name, sid in styles.items()]
        print(f"  Styles:     {', '.join(pairs)}")


def cmd_start():
    if not is_engine_running():
        FLAG_PATH.unlink(missing_ok=True)
        print("voicevox-tts: OFF (VOICEVOX engine not running)")
        return

    FLAG_PATH.touch()
    cfg = load_config()

    try:
        ver = vv_request("/version").decode().strip('"')
    except Exception:
        ver = "?"

    sp = cfg.get("speaker", DEFAULT_SPEAKER)
    styles = detect_styles(sp)
    if styles:
        cfg["styles"] = styles

    cast = cfg.get("cast", {})
    for member in cast.values():
        # スタイル表は登録時のスナップショットが正。ここでは欠けているときだけ補完する
        # （エンジン更新に追従したいときは cast refresh を明示的に実行）
        if "styles" not in member:
            info = find_style_info(member.get("speaker"))
            if info:
                member["name"], member["style"], member["styles"] = info
        # キャストのモデルを予熱して初回発話の遅延をなくす
        try:
            vv_request(
                "/initialize_speaker", "POST",
                query={"speaker": member.get("speaker"), "skip_reinit": "true"},
            )
        except Exception:
            pass

    if styles or cast:
        save_config(cfg)

    print(f"voicevox-tts: ON  |  VOICEVOX v{ver}  |  speaker={sp}  speed={cfg.get('speed', DEFAULT_SPEED)}")
    if styles:
        pairs = [f"{name}={sid}" for name, sid in styles.items()]
        print(f"  styles: {', '.join(pairs)}")
    if cast:
        pairs = [f"{label}={m.get('name', '?')}({m.get('style', '?')})" for label, m in cast.items()]
        print(f"  cast:   {', '.join(pairs)}")


def decode_stdin_bytes(raw: bytes) -> str:
    """Read piped text from UTF-8 tools and Windows PowerShell alike.

    PowerShell 5 may send native-command pipeline text in the active console
    code page (CP932 on Japanese Windows), while Python can expose stdin as
    UTF-8.  Decode bytes explicitly so ``speak.py --bg`` keeps Japanese text
    intact in either case.
    """
    # BOMつき入力は先に確定させる。Windows PowerShellのファイル／パイプ
    # 経路ではUTF-16が現れる場合もある。
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16").strip()
    for encoding in ("utf-8-sig", "cp932"):
        try:
            return raw.decode(encoding).strip()
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", errors="replace").strip()


def read_stdin_text() -> str:
    return decode_stdin_bytes(sys.stdin.buffer.read())


def main():
    if sys.stdout and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.stderr and hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")

    args = sys.argv[1:]

    if not args:
        text = read_stdin_text()
        if text:
            speak(text)
        return

    cmd = args[0]

    if cmd == "--hook":
        handle_hook()
        return

    if cmd == "--hook-worker":
        if len(args) > 1:
            handle_hook_worker(args[1])
        return

    if cmd in ("-l", "--list"):
        list_speakers()
        return

    if cmd == "config":
        cmd_config(args[1:])
        return

    if cmd == "reading":
        cmd_reading(args[1:])
        return

    if cmd == "cast":
        cmd_cast(args[1:])
        return

    if cmd == "play":
        cmd_play(args[1:])
        return

    if cmd == "paste":
        cmd_paste(args[1:])
        return

    if cmd == "serve":
        cmd_serve(args[1:])
        return

    if cmd == "on":
        FLAG_PATH.touch()
        print("voicevox-tts: ON")
        return

    if cmd == "off":
        FLAG_PATH.unlink(missing_ok=True)
        print("voicevox-tts: OFF")
        return

    if cmd == "stop":
        cmd_stop()
        return

    if cmd == "status":
        cmd_status()
        return

    if cmd == "doctor":
        cmd_doctor()
        return

    if cmd == "install-hook":
        cmd_install_hook(args[1:])
        return

    if cmd == "ensure-engine":
        if ensure_engine():
            print("VOICEVOX engine: OK")
        return

    if cmd == "start":
        cmd_start()
        return

    speaker = None
    speed = None
    background = False
    text_parts = []
    i = 0
    while i < len(args):
        if args[i] in ("-s", "--speaker") and i + 1 < len(args):
            speaker = int(args[i + 1])
            i += 2
        elif args[i] == "--speed" and i + 1 < len(args):
            speed = float(args[i + 1])
            i += 2
        elif args[i] == "--bg":
            background = True
            i += 1
        else:
            text_parts.append(args[i])
            i += 1

    text = " ".join(text_parts)
    if not text and (background or speaker is not None or speed is not None):
        text = read_stdin_text()  # フラグだけの起動は無引数と同じく stdin から読む
    if text:
        speak(text, speaker=speaker, speed=speed, background=background)


if __name__ == "__main__":
    main()
