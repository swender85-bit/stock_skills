#!/usr/bin/env python3
"""moomoo OpenD の OpenD.xml にログイン資格情報を安全に書き込む。

パスワードは getpass で受け取り（画面に出さない）、その場で MD5 に変換して
`<login_pwd_md5>` に書く。平文の `<login_pwd>` はコメントアウトする。
パスワード原文はファイルにもチャットにも残さない。

    python scripts/set_moomoo_login.py
    python scripts/set_moomoo_login.py --xml "C:\\path\\to\\OpenD.xml"

注意:
- 使うのは**ログインパスワード**であって取引パスワードではない。
- login_account はユーザーID / メール / 電話番号(例: +81 9012345678)のいずれか。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path

DEFAULT_XML = Path(
    r"C:\Users\swend\Desktop\moomoo_OpenD_10.9.6918_Windows"
    r"\moomoo_OpenD_10.9.6918_Windows\OpenD.xml"
)


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def apply_credentials(text: str, account: str, pwd_md5: str) -> tuple[str, dict]:
    """XML テキストに account / md5 を反映し、平文 pwd をコメント化する。

    行単位で処理する。返り値は (新テキスト, 各項目を書けたかのフラグ)。
    純粋関数なのでテストしやすい。
    """
    lines = text.splitlines(keepends=True)
    done = {"account": False, "md5": False, "pwd_commented": False}

    for i, line in enumerate(lines):
        stripped = line.strip()
        is_comment = stripped.startswith("<!--")
        nl = "\n" if line.endswith("\n") else ""
        ind = _indent(line)

        if "<login_account>" in line and not is_comment and not done["account"]:
            lines[i] = f"{ind}<login_account>{account}</login_account>{nl}"
            done["account"] = True
        elif "<login_pwd_md5>" in line and not done["md5"]:
            # コメント中でも有効化して値を入れる
            lines[i] = f"{ind}<login_pwd_md5>{pwd_md5}</login_pwd_md5>{nl}"
            done["md5"] = True
        elif "<login_pwd>" in line and not is_comment and not done["pwd_commented"]:
            # 平文は使わせない: 中身を消してコメントアウト
            lines[i] = f"{ind}<!-- <login_pwd></login_pwd> -->{nl}"
            done["pwd_commented"] = True

    new_text = "".join(lines)

    # md5 行が1つも無かった場合は account 行の直後に挿入する
    if not done["md5"]:
        out = []
        for line in new_text.splitlines(keepends=True):
            out.append(line)
            if "<login_account>" in line and not line.strip().startswith("<!--"):
                nl = "\n" if line.endswith("\n") else "\n"
                out.append(f"{_indent(line)}<login_pwd_md5>{pwd_md5}</login_pwd_md5>{nl}")
                done["md5"] = True
        new_text = "".join(out)

    return new_text, done


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xml", type=Path, default=DEFAULT_XML,
                        help=f"OpenD.xml のパス (既定: {DEFAULT_XML})")
    args = parser.parse_args()

    xml_path: Path = args.xml
    if not xml_path.is_file():
        print(f"[error] OpenD.xml が見つかりません: {xml_path}")
        return 1

    print(f"対象: {xml_path}")
    print("moomoo の *ログイン* パスワードを使います（取引パスワードではありません）。\n")

    account = input("moomoo アカウント (ユーザーID / メール / 電話番号): ").strip()
    if not account:
        print("[error] アカウントが空です。中止しました。")
        return 1

    pwd = getpass("ログインパスワード (表示されません): ")
    if not pwd:
        print("[error] パスワードが空です。中止しました。")
        return 1
    pwd2 = getpass("もう一度パスワードを入力: ")
    if pwd != pwd2:
        print("[error] パスワードが一致しません。中止しました。")
        return 1

    pwd_md5 = hashlib.md5(pwd.encode("utf-8")).hexdigest()  # noqa: S324 - moomoo仕様
    del pwd, pwd2

    text = xml_path.read_text(encoding="utf-8")
    new_text, done = apply_credentials(text, account, pwd_md5)

    if not (done["account"] and done["md5"]):
        print("[error] XML の login_account / login_pwd_md5 を書けませんでした。"
              "ファイル構造が想定と違います。手動編集してください。")
        return 1

    # バックアップ（初回のみ .orig、以降はタイムスタンプ）
    orig = xml_path.with_suffix(xml_path.suffix + ".orig")
    if not orig.exists():
        shutil.copy2(xml_path, orig)
        print(f"バックアップ: {orig.name}（初回のオリジナル）")
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = xml_path.with_suffix(xml_path.suffix + f".bak_{stamp}")
        shutil.copy2(xml_path, bak)
        print(f"バックアップ: {bak.name}")

    xml_path.write_text(new_text, encoding="utf-8")

    print("\n✅ 書き込み完了:")
    print(f"   login_account   = {account}")
    print(f"   login_pwd_md5   = {pwd_md5[:6]}…{pwd_md5[-4:]}（32桁MD5・平文は保存していません）")
    print(f"   login_pwd(平文) = コメントアウト済み")
    print("\n次: OpenD.exe を起動 → ログイン成功とポート11111オープンを確認 → "
          "python scripts/probe_moomoo.py で実測。")
    print("初回はSMS等の端末認証をOpenDが求める場合があります（コンソールの指示に従ってください）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
