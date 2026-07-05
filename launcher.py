"""ぼいぼサポーター 起動口（exe 化のエントリポイント・二刀流）

ひとつの exe が「人間の入口」と「AI・スクリプトの入口」を兼ねる:

  引数なし   人間のダブルクリック用。トレイ常駐を確保してから設定GUIを開く
  --tray     トレイ常駐だけ（スタートアップの .lnk がこれを指す）
  --gui      設定GUIだけ（トレイの「設定...」がこれで開く）
  それ以外   speak CLI として振る舞う（AI_GUIDE.md が案内する --bg / play / stop など）

ソース実行（python launcher.py）でも同じ分岐で動く。普段ソース版を使う場合は
従来どおり speak.py / gui.py / tray.py を直接呼んでよい（このファイルは入口の別名にすぎない）。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import speak  # Windows チェックは speak 側で行われる


def _spawn_tray():
    """トレイ常駐を切り離しプロセスで立ち上げる（既に居るなら何もしない）。"""
    import tray
    if tray.tray_running():
        return
    if speak.FROZEN:
        cmd = [sys.executable, "--tray"]
    else:
        cmd = [sys.executable, str(Path(__file__).resolve().parent / "tray.py")]
    subprocess.Popen(cmd, **speak._detached_popen_kwargs())


def main():
    args = sys.argv[1:]
    if args and args[0] == "--tray":
        import tray
        tray.main()
    elif args and args[0] == "--gui":
        import gui
        gui.main()
    elif args:
        speak.main()  # speak CLI へ委譲（speak.main は sys.argv を直接読む）
    else:
        # ダブルクリック: 常駐を確保してから設定画面。どちらが先の入口でも同じ姿になる
        _spawn_tray()
        import gui
        gui.main()


if __name__ == "__main__":
    main()
