"""写しの資格情報の関門。

写しは限定公開とはいえエッジに置くので、鍵は載せない。
弾きすぎると関門を外したくなり結局守られなくなるので、**通すもの**も固定する。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from export_mirror import _assert_no_credentials  # noqa: E402


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        # 鍵の置き場を指す値 (環境変数名)。鍵そのものではない。
        ("環境変数名", {"webhook_env_key": "DISCORD_WEBHOOK_ALERT"}),
        # アクター名に secret を含むものが実在する。キー名の部分一致では誤検知する。
        ("アクター名に secret", {"actor_lookup": {"global_secret_group": {"n": 1}}}),
        ("件数", {"cookie_count": 36}),
        ("記事本文", {"body": "The United States is considering " * 20}),
        ("空", {"api_key": ""}),
        # ⚠ 記事 URL の "task-host-flaw" が "sk-host-flaw…" に見えて止まった実例。
        # 語の途中に当てないことを固定する。
        (
            "記事 URL",
            {
                "article_id": "rss:https://www.bleepingcomputer.com/news/security/"
                "cisa-windows-task-host-flaw-now-exploited-by-ransomware"
            },
        ),
    ],
)
def test_通す(name: str, payload: dict[str, object]) -> None:
    _assert_no_credentials(payload, "t")


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        # キー名に頼らない — 名前を変えた経路が素通りしないように値の形でも見る。
        ("Discord webhook", {"url": "https://discord.com/api/webhooks/123/AbCdEfGh_XyZ"}),
        ("API キー (キー名なし)", {"note": "sk-ant-api03-AbCdEfGhIjKlMnOp"}),
        ("GitHub token (入れ子)", {"a": [{"b": "ghp_" + "A" * 24}]}),
        ("access_token", {"access_token": "eyJhbGciOiJIUzI1NiJ9abcdef"}),
        ("password", {"password": "correct-horse-battery"}),
    ],
)
def test_止める(name: str, payload: dict[str, object]) -> None:
    with pytest.raises(SystemExit):
        _assert_no_credentials(payload, "t")
