"""`.bat` は ASCII のみ -- 無人実行が静かに死ぬのを防ぐ.

## 実際に起きたこと

`scripts/run_weekly_deep.bat` の REM コメントは日本語（UTF-8）で書かれていた。
**cmd.exe はバッチファイルを UTF-8 ではなく OEM コードページ（この環境では cp932）
で解釈する。** ファイル冒頭の `chcp 65001` はコンソール出力の設定であって、
バッチ自身の解釈には間に合わない。

その結果、マルチバイトのコメント文字列が行構造を壊し、
**コメントのつもりだった文字列が本物のコマンドとして実行された。**
バッククォートで囲んだ claude CLI の記述がそのまま起動され、CLI が

    error: unknown option

を返してバッチは exit 1 で即死。**ログを1行も書かずに死ぬ**ので、
`output/weekly_deep.log` は存在すらしなかった。

タスクスケジューラ側の設定（WakeToRun / StartWhenAvailable）は正しかったのに、
`WeeklyDeep` の LastRunTime は `1999/11/30`（＝一度も成功していない）のままだった。
**週次レポートが自動で出ていなかった真の原因はこれ。**

## ここで縛っていること

- `.bat` に非ASCII文字を入れない
- `.bat` にバッククォートを入れない（コメントから漏れると即コマンド実行になる）
- ログ書き込みが python 呼び出しより**先**にある（死んでも痕跡が残る）
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
BAT_FILES = sorted(REPO.glob("scripts/*.bat"))


def test_bat_files_exist():
    assert BAT_FILES, "scripts/*.bat が1つも見つかりません"


@pytest.mark.parametrize("path", BAT_FILES, ids=lambda p: p.name)
def test_bat_is_ascii_only(path: Path):
    """cmd.exe は .bat を cp932 で読む。非ASCIIは行構造を壊す。"""
    raw = path.read_bytes()
    offenders = [(i, b) for i, b in enumerate(raw) if b > 0x7F]
    if offenders:
        first = offenders[0][0]
        context = raw[max(0, first - 40):first + 40].decode("utf-8", errors="replace")
        pytest.fail(
            f"{path.name} に非ASCIIバイトが {len(offenders)}個あります"
            f"（最初は offset {first}）。\n"
            f"cmd.exe は .bat を OEM コードページ（cp932）で解釈するため、"
            f"日本語コメントが行構造を壊し、**コメントが実行される**ことがあります。\n"
            f"該当箇所: ...{context}..."
        )


@pytest.mark.parametrize("path", BAT_FILES, ids=lambda p: p.name)
def test_bat_has_no_backticks(path: Path):
    """バッククォートはコメントから漏れた瞬間にコマンド実行になる。

    実際 `` `claude -p` `` がこの経路で起動され、週次が死んでいた。
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    assert "`" not in text, (
        f"{path.name} にバッククォートがあります。"
        "コメントが崩れた際にコマンドとして実行され得ます（実害が出た経路）。"
    )


@pytest.mark.parametrize("path", BAT_FILES, ids=lambda p: p.name)
def test_bat_has_no_trailing_caret_in_comments(path: Path):
    """行末 `^` は REM の中でも継続として働き、次行を飲み込む。

    飲み込まれた行が REM でなければ、それは実行される。
    """
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        stripped = line.rstrip()
        if stripped.upper().lstrip().startswith("REM") and stripped.endswith("^"):
            pytest.fail(
                f"{path.name}:{i} の REM 行が `^` で終わっています。"
                "バッチでは REM の中でも行継続として働き、次の行を飲み込みます。"
            )


@pytest.mark.parametrize("path", BAT_FILES, ids=lambda p: p.name)
def test_log_is_written_before_python_runs(path: Path):
    """python 呼び出しより前にログへ書く。

    これが無いと、バッチが早期に死んだときログが1行も残らず、
    「タスクは動いたのに何も起きていない」という最悪の状態になる
    （実際そうなっていた）。
    """
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    first_log = next((i for i, l in enumerate(lines) if ">> \"%LOG%\"" in l), None)
    first_python = next((i for i, l in enumerate(lines)
                         if l.strip().lower().startswith("python ")), None)
    if first_python is None:
        pytest.skip("python を呼ばないバッチ")
    assert first_log is not None, f"{path.name} はログを書いていません"
    assert first_log < first_python, (
        f"{path.name}: python 実行より前にログへ書いてください。"
        "早期死亡時に痕跡が残りません。"
    )
