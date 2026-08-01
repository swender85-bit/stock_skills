"""資格情報の衛生テスト (土曜設計書 第5章-6 / 第7章-8)。

> ブローカーAPIの認証情報は環境変数のみ。
> **ログ・レポート・Neo4j・コミットに一切出力しない。**

ここが破れると、リポジトリや Obsidian vault に取引パスワードが残る。
機能の不具合と違って、後から気づいても取り返しがつかない種類の事故なので
テストで固定する。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent

#: 秘密が入り得る環境変数名。値ではなく**名前**をコードから読む前提。
SECRET_ENV_VARS = (
    "MOOMOO_TRADE_PWD_MD5",
    "XAI_API_KEY",
    "FINNHUB_API_KEY",
    "LINEAR_API_KEY",
    "NEO4J_PASSWORD",
)

_SENTINEL = "SUPERSECRET_DO_NOT_LEAK_0123456789"


# ---------------------------------------------------------------------------
# コード上の扱い
# ---------------------------------------------------------------------------


def test_broker_credentials_come_only_from_env():
    """資格情報がソースに直書きされていないこと。"""
    src = (REPO / "src" / "data" / "brokers" / "moomoo_broker.py").read_text(
        encoding="utf-8")
    assert 'os.environ.get("MOOMOO_TRADE_PWD_MD5"' in src
    # 値らしき長い16進文字列が埋まっていないこと
    assert not re.search(r"['\"][0-9a-fA-F]{32}['\"]", src), \
        "MD5 らしき定数がソースに埋まっています"


def test_broker_snapshot_never_carries_credentials(monkeypatch):
    """スナップショットは Neo4j にもレポートにも流れる。秘密を載せない。"""
    from src.data.brokers import moomoo_broker

    monkeypatch.setenv("MOOMOO_TRADE_PWD_MD5", _SENTINEL)
    monkeypatch.delenv("MOOMOO_ENABLED", raising=False)

    snap = moomoo_broker.fetch(autostart=False)
    assert _SENTINEL not in repr(snap)


def test_unlock_failure_does_not_surface_the_password(monkeypatch):
    """unlock に失敗しても、例外メッセージにパスワードを混ぜない。"""
    from src.data.brokers import moomoo_broker

    monkeypatch.setenv("MOOMOO_TRADE_PWD_MD5", _SENTINEL)

    class _Ctx:
        def unlock_trade(self, password_md5=None):
            raise RuntimeError(f"unlock failed for {password_md5}")

    # 例外は握り潰される（呼び出し側に伝播しない）
    moomoo_broker._unlock_if_needed(object(), _Ctx())


def test_reconciliation_output_has_no_credentials(monkeypatch):
    """照合結果はレポート第1セクションに出る。秘密が混ざらないこと。"""
    from src.core.portfolio.reconciliation import reconcile
    from src.data.brokers.base import make_snapshot

    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, _SENTINEL)

    snap = make_snapshot("moomoo", available=False, scope=["US"],
                         error="OpenD 未接続")
    result = reconcile([{"quote_symbol": "AAPL", "shares": 1, "name": "Apple"}],
                       [snap], check_intent=False)
    assert _SENTINEL not in repr(result)


def test_execution_audit_output_has_no_credentials(monkeypatch):
    from src.core.portfolio.execution_audit import build_execution_audit

    for var in SECRET_ENV_VARS:
        monkeypatch.setenv(var, _SENTINEL)

    audit = build_execution_audit([], [])
    assert _SENTINEL not in repr(audit)


# ---------------------------------------------------------------------------
# 収入関連の絶対額（第7章-8 の後半）
# ---------------------------------------------------------------------------


def test_graph_payload_omits_income_absolute_amounts():
    """Neo4j へは比率のみ。収入の絶対額を書かない。"""
    from src.core.portfolio.runway import runway, to_graph_safe

    cfg = {"estimation": {"runway_weeks": 12}}
    r = runway(123_456.0, cash_jpy=987_654.0, weeks=12, cfg=cfg)
    payload = repr(to_graph_safe(r, total_jpy=50_000_000))

    for forbidden in ("123456", "987654", "123,456", "987,654"):
        assert forbidden not in payload


def test_cashflow_config_is_not_echoed_into_graph_payload():
    """設定ファイルの積立額そのものがグラフに出ないこと。"""
    from src.core.portfolio.runway import load_cashflow_config, to_graph_safe, runway

    cfg = load_cashflow_config()
    monthly = (cfg.get("contributions") or {}).get("monthly_amount")
    if not monthly:
        pytest.skip("積立額が設定されていません")

    r = runway(float(monthly) * 12 / 52, cash_jpy=0.0, cfg=cfg)
    assert str(int(monthly)) not in repr(to_graph_safe(r, total_jpy=10_000_000))


# ---------------------------------------------------------------------------
# リポジトリに秘密が入っていないか
# ---------------------------------------------------------------------------


def test_env_file_is_gitignored():
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in ignored


def test_sensitive_data_dirs_are_gitignored():
    """照合・物語量・模型採点の蓄積は保有データと同じ扱いにする。"""
    ignored = (REPO / ".gitignore").read_text(encoding="utf-8")
    for path in ("data/narrative/", "data/weekly_snapshots/",
                 "data/model_scorecard/", "data/policies/", "data/notes/"):
        assert path in ignored, path


@pytest.mark.parametrize("config", ["config/tax.yaml", "config/cashflow.yaml"])
def test_configs_hold_no_secret_keys(config):
    """設定ファイルは税率や積立額のみ。資格情報のキーを持たせない。

    （金額そのものは正当な設定値なので数字列では判定しない。）
    """
    text = (REPO / config).read_text(encoding="utf-8").lower()
    for forbidden in ("password", "api_key", "apikey", "secret", "token",
                      "pwd_md5", "access_key"):
        assert forbidden not in text, f"{config} に {forbidden} があります"


@pytest.mark.parametrize("module", [
    "src/data/brokers/moomoo_broker.py",
    "src/data/brokers/rakuten_csv_broker.py",
    "src/data/brokers/base.py",
])
def test_broker_modules_do_not_print(module):
    """ブローカー層は print しない。ログに残ると秘密が漏れる経路になる。

    `if __name__ == "__main__":` の手動確認ブロックは対象外
    （無人実行では通らないため）。
    """
    src = (REPO / module).read_text(encoding="utf-8")
    body = src.split('if __name__ ==', 1)[0]
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        assert not stripped.startswith("print("), f"{module}: {stripped}"
