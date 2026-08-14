"""閾値ファイルが本当に読めているかを縛る。

2026-08-11 まで、Windows では cp932 で開いていたため UTF-8 の日本語コメントで
UnicodeDecodeError になり、`except Exception` がそれを握り潰していた。
**設定ファイルは存在したが、一度も効いていなかった。**
"""
import src.core._thresholds as T


class TestThresholdLoading:
    def test_file_actually_loads(self):
        T._THRESHOLDS = None  # キャッシュを外して実ファイルを読む
        status = T.load_status()
        assert status["loaded"] is True, f"閾値ファイルを読めていません: {status['error']}"
        assert status["error"] is None

    def test_japanese_comments_do_not_break_it(self):
        T._THRESHOLDS = None
        data = T.get_thresholds()
        # 実ファイルには日本語コメントが入っている。読めていればセクションが並ぶ。
        assert "rates" in data
        assert "data_quality" in data

    def test_values_come_from_file_not_defaults(self):
        """既定値と違う値を引いて、ファイル側が効いていることを確かめる。"""
        T._THRESHOLDS = None
        # 呼び出し側の既定を意図的に外れた値にする
        assert T.th("rates", "ust30y_warning", -999) == 5.50
        assert T.th("data_quality", "min_price_coverage", -999) == 0.7

    def test_failure_is_recorded_not_swallowed(self, tmp_path, monkeypatch):
        T._THRESHOLDS = None
        monkeypatch.setattr(T, "_PATH", tmp_path / "missing.yaml")
        status = T.load_status()
        assert status["loaded"] is False
        assert status["error"]          # 理由が残る
        assert T.th("rates", "ust30y_warning", 5.5) == 5.5  # 既定値へは落ちる
        T._THRESHOLDS = None            # 後続テストのために戻す
