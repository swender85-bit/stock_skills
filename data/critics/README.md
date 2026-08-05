# 批評家較正台帳 (改善5)

外部言説（ニュース・アナリスト・X上の論者）の**過去の的中率をドメイン別に**持つ台帳。

## なぜ要るか

系譜会計（`src/core/provenance.py`）は主張を四系譜に分けるが、
**「外部言説」に、その情報源が過去どれだけ当たったかという重みがない。**
需給読みで9回当てた情報源と、初出の情報源が同じ扱いになる。

## いまの状態（重要）

**判定への使用は保留しています。** 採点済みのテーゼが足りず、ドメイン別の重みを
出せる情報源がまだありません。`domain_weight()` は `available=False` を返し、
これは **「当たらない」ではなく「まだ測れていない」** として扱われます。

台帳の形式を先に作るのは、蓄積が始まらないと永遠に使えるようにならないためです。
効果が出るのは蓄積後です。

## ファイル形式

`data/critics/<source_id>.json`（`_` 始まりは雛形として無視されます）

```json
{
  "source_id": "critic_a",
  "name": "批評家A",
  "theses": [
    {
      "date": "2026-07-16",
      "claim": "規制の事前織り込みでSKHY急落",
      "domain": "supply_demand",
      "verified_on": "2026-07-16",
      "score": "hit_exact",
      "note": ""
    }
  ]
}
```

## 採点

| score | 重み | 意味 |
|:---|---:|:---|
| `hit_exact` | 1.0 | 方向も水準も当たった |
| `hit_direction` | 0.7 | 方向は当たった |
| `partial` | 0.4 | 部分的に当たった |
| `refuted` | 0.0 | 反証された |
| `pending` | — | **未検証。分母に入れない**（未検証と誤りは別物） |

**採点には `verified_on` が必須です。** 検証日の無い採点は、結果を見てから
付けた後知恵と区別できません。

## ドメイン

`supply_demand`（需給・資金フロー）/ `price_level`（価格水準の断言）/
`timing` / `fundamentals` / `macro` / `regulation` / `technology` / `sentiment`

**採点は主張ごと、重みはドメインごと。** 一人の批評家が需給には強く価格水準には
弱い、という実態は珍しくありません。情報源そのものを「信頼できる／できない」で
二値化すると、この構造が潰れて使い物になりません。

## 使う側の規約

`.claude/rules/provenance.md` に記載:

> 外部言説をレポート本文の根拠に使えるのは、その情報源のそのドメインの重みが
> **0.6 以上**のときのみ。それ未満は「◯◯氏の見解（当該ドメインでの過去的中率: n/m）」
> として引用形式で書く。

重みを出すには同一ドメインで**5件以上**の採点が必要です。少数の的中で
「この人は当たる」と決めると、偶然を実力と誤認します。

## 使い方

```python
from src.core.critic_calibration import add_thesis, build_thesis, domain_weight, profile

# 主張を記録する（結果が出る前）
add_thesis("critic_a", build_thesis("規制の事前織り込みで急落", "supply_demand"))

# 後日、採点する
critic = load_critic("critic_a")
critic["theses"][-1].update({"score": "hit_exact", "verified_on": "2026-07-16"})
save_critic(critic)

# 重みを見る
domain_weight("critic_a", "supply_demand")
profile("critic_a")            # ドメイン別の得手不得手
```
