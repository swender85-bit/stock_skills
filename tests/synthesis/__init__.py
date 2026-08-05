"""synthesis層（Claude が書く文章）の eval harness.

Python層はテストが縛っているが、`.claude/prompts/*.md` が生む**文章**は
これまで誰も測っていなかった。ここが §16 の8原則に対する唯一の穴だった。
"""
