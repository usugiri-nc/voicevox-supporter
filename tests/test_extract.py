# -*- coding: utf-8 -*-
"""extract_voice_segments の記法別テスト

実行: python tests/test_extract.py
依存なし（標準ライブラリのみ）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from speak import (
    MIN_ENGINE_VERSION,
    calc_auto_speed,
    decode_stdin_bytes,
    extract_script_segments,
    extract_voice_segments,
    parse_script_args,
    parse_version,
    strip_ruby,
)

STYLES = {"ノーマル": 122, "怒り": 123, "哀しみ": 124, "ささやき": 125}
DEFAULT = 122
ALL = {"voiceTag": True, "linePrefix": "♪", "brackets": ["『", "』"]}
CAST = {
    "A": {"speaker": 2, "styles": {"ノーマル": 2, "あまあま": 0, "ツンツン": 6}},
    "B": {"speaker": 3, "styles": {"ノーマル": 3, "ささやき": 22}},
    # speaker が1番目のスタイルとズレている登録でも、添字なし=1番目 が守られることの確認用
    "M": {"speaker": 125, "styles": {"ノーマル": 122, "怒り": 123, "哀しみ": 124, "ささやき": 125}},
}

failures = []


def check(name, text, expected, markers=ALL, cast=None):
    actual = extract_voice_segments(text, STYLES, DEFAULT, markers, cast)
    if actual == expected:
        print(f"[OK] {name}")
    else:
        print(f"[NG] {name}: {actual!r} != {expected!r}")
        failures.append(name)


# --- voice タグ ---
check("tag basic", "説明\n<voice>こんにちは</voice>\n続き", [("こんにちは", 122)])
check("tag style", '<voice style="怒り">むー</voice>', [("むー", 123)])
check("tag multiline", "<voice>あ\nい</voice>", [("あ", 122), ("い", 122)])
check("tag unknown style", '<voice style="謎">x</voice>', [("x", 122)])

# --- 行頭マーカー ---
check("line basic", "説明文\n♪ 読むよ\n次の説明", [("読むよ", 122)])
check("line style", "♪(ささやき) ないしょだよ", [("ないしょだよ", 125)])
check("line midline not match", "文中の♪は読まない", [])

# --- 括弧ペア（デフォルト『』） ---
check("brackets basic", "地の文『呼んだ？』続き", [("呼んだ？", 122)])
check("brackets style zenkaku", "『（哀しみ）しくしく』", [("しくしく", 124)])
check("brackets style hankaku", "『(怒り)もう！』", [("もう！", 123)])
check("brackets two pairs", "『いち』と『に』", [("いち", 122), ("に", 122)])

# --- スタイル括弧の厳格化 ---
check("strict style keeps text", "『（笑）そうだね』", [("（笑）そうだね", 122)])
check("strict style line", "♪(謎) こんにちは", [("(謎)こんにちは", 122)])

# --- キャスト ---
check("cast basic", "A『こんにちは！』", [("こんにちは！", 2)], ALL, CAST)
check("cast suffix a (1番目)", "Aa『ノーマルだよ』", [("ノーマルだよ", 2)], ALL, CAST)
check("cast suffix b (2番目)", "Ab『あまあまだよ』", [("あまあまだよ", 0)], ALL, CAST)
check("cast suffix c (3番目)", "Ac『ツンツンだよ』", [("ツンツンだよ", 6)], ALL, CAST)
check("cast suffix out of range", "Az『範囲外はaと同じ』", [("範囲外はaと同じ", 2)], ALL, CAST)
check("cast bare = first style", "M『添字なしは1番目』", [("添字なしは1番目", 122)], ALL, CAST)
check("cast explicit d", "Md『ささやきは明示する』", [("ささやきは明示する", 125)], ALL, CAST)
check("cast paren style", "B『（ささやき）ぼくなのだ』", [("ぼくなのだ", 22)], ALL, CAST)
check("cast paren beats suffix", "Ba『（ささやき）ひそひそ』", [("ひそひそ", 22)], ALL, CAST)
check("cast unknown style spoken", "A『（謎）ん？』", [("（謎）ん？", 2)], ALL, CAST)
check("cast unregistered label", "X『Aの声になる』", [("Aの声になる", 2)], ALL, CAST)
check("cast word boundary", "MANGA『タイトル』", [("タイトル", 2)], ALL, CAST)
check("cast conversation", "A『やあ』\nBb『どうも』\n『地の文』",
      [("やあ", 2), ("どうも", 22), ("地の文", 2)], ALL, CAST)
check("cast no cast arg", "A『こんにちは』", [("こんにちは", 122)])  # cast未指定ならAは無視

# --- 素の『』はラベル省略＝先頭キャスト（通常A）の声 ---
check("bare bracket = cast A", "『ラベル省略はAの声』", [("ラベル省略はAの声", 2)], ALL, CAST)
check("bare bracket A paren style", "『（あまあま）Aのスタイル表で解決』",
      [("Aのスタイル表で解決", 0)], ALL, CAST)
check("bare bracket unknown paren", "『（笑）本文として読む』", [("（笑）本文として読む", 2)], ALL, CAST)
check("bare bracket first label", "『Aが空ならBの声』", [("Aが空ならBの声", 3)], ALL,
      {"B": {"speaker": 3, "styles": {"ノーマル": 3, "ささやき": 22}}})
check("bare bracket no cast fallback", "『キャスト無しは既定の声』", [("キャスト無しは既定の声", 122)], ALL, None)

# --- コード除外 ---
check("codeblock excluded", "```\n♪ 読まない\n『これも読まない』\n```\n♪ 読む", [("読む", 122)])
check("inline code excluded", "これは `『読まない』` の説明", [])

# --- 二重マッチ防止・順序 ---
check("no double match", "<voice>♪ うた</voice>", [("♪ うた", 122)])
check("order preserved", "♪ いち\n<voice>に</voice>\n『さん』", [("いち", 122), ("に", 122), ("さん", 122)])

# --- デフォルト設定（markers=None）: 『』オン・♪オフ ---
check("default markers", "『デフォルト』\n♪ 読まない", [("デフォルト", 122)], None)

# --- requireLabel（ラベル必須モード） ---
REQ = {"voiceTag": True, "linePrefix": None, "brackets": ["『", "』"], "requireLabel": True}
check("requireLabel bare ignored", "『作品名っぽい表記』は読まない", [], REQ, CAST)
check("requireLabel labeled ok", "A『喋るよ』", [("喋るよ", 2)], REQ, CAST)
check("requireLabel suffix ok", "Ab『あまあま』", [("あまあま", 0)], REQ, CAST)
check("requireLabel labeled style", "B『（ささやき）ひそひそ』", [("ひそひそ", 22)], REQ, CAST)
check("requireLabel unregistered", "X『読まない』", [], REQ, CAST)
check("requireLabel no cast", "『読まない』A『これも読まない』", [], REQ, None)
check("requireLabel tag alive", "<voice>タグは生きてる</voice>", [("タグは生きてる", 122)], REQ, CAST)

# --- A案括弧も引き続き設定可能 ---
KAGI = {"voiceTag": False, "linePrefix": None, "brackets": ["「", "」."]}
check("kagi+period only", "彼は「ふつうの会話」と言った。\n「これは読む」.", [("これは読む", 122)], KAGI)


# --- 台本ファイル用抽出（play コマンド） ---
def check_script(name, text, expected, markers=ALL, cast=None):
    actual = extract_script_segments(text, STYLES, DEFAULT, markers, cast)
    if actual == expected:
        print(f"[OK] {name}")
    else:
        print(f"[NG] {name}: {actual!r} != {expected!r}")
        failures.append(name)


check_script("script notation as usual", "A『やあ』\n地の文は読まない\nB『どうも』",
             [("やあ", 2), ("どうも", 3)], ALL, CAST)
check_script("script plain fallback", "一行目\n二行目\n\n三行目",
             [("一行目", 122), ("二行目", 122), ("三行目", 122)])
check_script("script fallback strips code", "読む行\n```\ncode\n```",
             [("読む行", 122)])


# --- play/paste 共通のオプション解析 ---
def check_args(name, args, expected):
    rest, out_path, speed, auto = parse_script_args(args)
    actual = (rest, str(out_path) if out_path else None, speed, auto)
    if actual == expected:
        print(f"[OK] {name}")
    else:
        print(f"[NG] {name}: {actual!r} != {expected!r}")
        failures.append(name)


check_args("args file only", ["台本.txt"], (["台本.txt"], None, None, False))
check_args("args out and speed", ["台本.txt", "--out", "o.wav", "--speed", "1.2"],
           (["台本.txt"], "o.wav", 1.2, False))
check_args("args empty (paste)", [], ([], None, None, False))
check_args("args speed clamped high", ["--speed", "9"], ([], None, 2.0, False))
check_args("args speed clamped low", ["--speed", "0.1"], ([], None, 0.5, False))
check_args("args auto", ["台本.txt", "--auto"], (["台本.txt"], None, None, True))
check_args("args auto with out", ["--auto", "台本.txt", "--out", "o.wav"],
           (["台本.txt"], "o.wav", None, True))


# --- ルビ記法（青空文庫互換）: 振り先ごと読みのかなに置換 ---
def check_ruby(name, text, expected):
    actual = strip_ruby(text)
    if actual == expected:
        print(f"[OK] {name}")
    else:
        print(f"[NG] {name}: {actual!r} != {expected!r}")
        failures.append(name)


check_ruby("ruby pipe applies", "｜理《ことわり》を説く", "ことわりを説く")
check_ruby("ruby half-width pipe", "|強敵《とも》よ", "ともよ")
check_ruby("ruby pipe multiple", "｜打《う》ち｜矧《は》ぐ", "うちはぐ")
check_ruby("ruby pipe bounds kana base", "お｜天道様《てんとさま》", "おてんとさま")
# ｜なしは見た目の注記扱い: 《よみ》を読み飛ばすだけで、漢字は普通に読ませる
check_ruby("ruby note stripped", "盛者必衰《じょうしゃひっすい》の理", "盛者必衰の理")
check_ruby("ruby note multiple", "打《う》ち矧《は》ぐ", "打ち矧ぐ")
check_ruby("ruby note repeat mark", "日々《ひび》の糧", "日々の糧")
check_ruby("ruby kana base untouched", "ひらがな《かっこ》はそのまま", "ひらがな《かっこ》はそのまま")
check_ruby("ruby empty reading untouched", "漢字《》はそのまま", "漢字《》はそのまま")
check_ruby("ruby none untouched", "ルビのない文はそのまま", "ルビのない文はそのまま")


# --- 読み上げ速度設定1〜5と長文の自動早口（speedSteps / stepChars） ---
def check_speed(name, actual, expected):
    if abs(actual - expected) < 1e-9:
        print(f"[OK] {name}")
    else:
        print(f"[NG] {name}: {actual!r} != {expected!r}")
        failures.append(name)


STEPS = [0.8, 1.3, 1.4, 1.55, 1.7]
CHARS = [80, 200, 400]
check_speed("steps below first = normal", calc_auto_speed(79, STEPS, CHARS), 1.3)
check_speed("steps 80 -> 速度3", calc_auto_speed(80, STEPS, CHARS), 1.4)
check_speed("steps holds between", calc_auto_speed(199, STEPS, CHARS), 1.4)
check_speed("steps 200 -> 速度4", calc_auto_speed(200, STEPS, CHARS), 1.55)
check_speed("steps 400 -> 速度5", calc_auto_speed(1000, STEPS, CHARS), 1.7)
check_speed("steps empty = off (always 速度2)", calc_auto_speed(1000, STEPS, []), 1.3)
check_speed("steps None = default chars", calc_auto_speed(400, STEPS, None), 1.7)
check_speed("steps unsorted chars ok", calc_auto_speed(250, STEPS, [400, 80, 200]), 1.55)
check_speed("steps clamped at 2.0", calc_auto_speed(500, [0.8, 1.3, 1.4, 1.55, 9.9], None), 2.0)

# --- 数字添字（読み上げ速度の指名） ---
check("cast speed step", "A1『ゆっくり』", [("ゆっくり", 2, 1)], ALL, CAST)
check("cast style+step", "Ab3『あまあまで速度3』", [("あまあまで速度3", 0, 3)], ALL, CAST)
check("cast step with paren style", "B1『（ささやき）ひっそり』", [("ひっそり", 22, 1)], ALL, CAST)
check("cast step out of range not suffix", "A6『速度6はないよ』", [("速度6はないよ", 2)], ALL, CAST)  # 記法不成立→素の『』=Aの声
check("cast step conversation mixed", "A『ふつう』B5『はやくち』",
      [("ふつう", 2), ("はやくち", 3, 5)], ALL, CAST)
check("requireLabel step ok", "A1『ゆっくり』", [("ゆっくり", 2, 1)],
      {"voiceTag": True, "linePrefix": None, "brackets": ["『", "』"], "requireLabel": True}, CAST)


# --- エンジンバージョン判定 ---
def check_eq(name, actual, expected):
    if actual == expected:
        print(f"[OK] {name}")
    else:
        print(f"[NG] {name}: {actual!r} != {expected!r}")
        failures.append(name)


check_eq("version parse", parse_version("0.25.2"), (0, 25, 2))
check_eq("version parse preview", parse_version("0.25.2-preview.3"), (0, 25, 2))
check_eq("version old fails minimum", parse_version("0.9.0") < MIN_ENGINE_VERSION, True)
check_eq("version short ok", parse_version("1.0") >= MIN_ENGINE_VERSION, True)

# --- Windows PowerShell パイプ入力（cp932）も保持する ---
check_eq("stdin decode utf-8", decode_stdin_bytes("こんにちは".encode("utf-8")), "こんにちは")
check_eq("stdin decode utf-8 BOM", decode_stdin_bytes(b"\xef\xbb\xbf" + "こんにちは".encode("utf-8")), "こんにちは")
check_eq("stdin decode utf-16", decode_stdin_bytes("こんにちは".encode("utf-16")), "こんにちは")
check_eq("stdin decode cp932", decode_stdin_bytes("こんにちは".encode("cp932")), "こんにちは")

print()
if failures:
    print(f"FAILED: {len(failures)} case(s): {failures}")
    sys.exit(1)
print("ALL PASSED")
