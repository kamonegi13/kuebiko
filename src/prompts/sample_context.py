"""block 方式プロンプトの保存前 render 検証に使うサンプル context。

「検証は preview、保存は PUT」と分けると検証だけが preview に残る事故が起きる
(summarizer WP2 の HIGH 指摘) — 保存経路自身が render まで通すための最小データ。
実データではなくダミー (kuebiko.example 系) を使い、ログ・応答に本物の記事本文を
持ち込まない。**Jinja の全ループ・分岐を 1 回は通る**ように非空で作る。
"""

from __future__ import annotations

from typing import Any

_STATUS_SYNTHESIS: dict[str, Any] = {
    "period_type": "daily",
    "period_label": "2026-01-01 (サンプル)",
    "baseline_weeks": 4,
    "axes_data": [
        {
            "axis_id": "I-cyber",
            "display": "サイバー",
            "total_current": 3,
            "baseline_avg": 2.0,
            "spike_label": "±0",
            "top_sectors": [{"label": "政府", "count": 2}],
            "top_countries": [{"label": "日本", "count": 1}],
            "recent_incidents": [
                {
                    "importance": "high",
                    "title": "サンプル事案 (render 検証用)",
                    "feed_title": "sample-feed",
                    "article_id": "sample-001",
                }
            ],
        }
    ],
    "trend_clusters": [
        {
            "name": "sample-cluster",
            "total_count": 2,
            "sources": ["a", "b"],
            "is_new": False,
            "spike_ratio": "1.5",
        }
    ],
    "high_importance_articles": [
        {
            "feed_title": "sample-feed",
            "title": "サンプル記事 (render 検証用)",
            "axes": ["I-cyber"],
            "reliability": "news",
            "stance": "",
            "article_id": "sample-001",
        }
    ],
    "nation_correlation": [
        {
            "label": "日本",
            "iso": "JP",
            "role": "home",
            "attributed_cyber": 1,
            "top_actors": ["SampleActor"],
            "cyber_mention": 1,
            "cyber_target": 1,
            "geopol": 1,
            "cyber_intents": ["espionage"],
            "geopol_intents": [],
        }
    ],
    "nation_window_days": 7,
    "forecast_indicators": [
        {"scope": "actor", "target": "sample", "direction": "increase", "z": "2.1", "count": 3}
    ],
    "freshness": {"dated": 10, "retrospective": 3, "retrospective_pct": 30, "fresh": 5},
    "previous_synthesis": {
        "period_label": "前期 (サンプル)",
        "headline": "サンプル headline",
        "cog_section": "サンプル重心",
        "spillover_section": "サンプル波及",
        "forecasts": ["サンプル予測 1"],
    },
    "pir_context": [{"title": "サンプル PIR", "description": "render 検証用"}],
}

_WEEKLY_RECAP: dict[str, Any] = {
    "period_label": "2026-01-01 〜 2026-01-07 (サンプル)",
    "total": 1,
    "items": [
        {
            "title": "サンプル記事 (render 検証用)",
            "url": "https://kuebiko.example/articles/sample-001",
            "feed": "sample-feed",
            "importance": "medium",
            "category": "vuln",
            "summary": "サンプル要約 (render 検証用)。",
        }
    ],
}

_PIR_DAILY_FOCUS: dict[str, Any] = {
    "pir": {
        "id": "pir_sample",
        "title": "サンプル PIR (render 検証用)",
        "description": "render 検証用の説明文。",
    },
    "matches": [
        {
            "importance": "high",
            "title": "サンプル記事 (render 検証用)",
            "feed_title": "sample-feed",
            "summary": "サンプル要約 (render 検証用)。",
        }
    ],
}

_DEEP_DIVE_RUBRIC: dict[str, Any] = {
    "total": 1,
    "pir_context": [{"title": "サンプル PIR", "description": "render 検証用"}],
    "recent_briefs": ["サンプル速報タイトル (render 検証用)"],
    "past_selected_keys": ["sample-dedup-key-001"],
    "items": [
        {
            "id": "sample-001",
            "title": "サンプル記事 (render 検証用)",
            "feed": "sample-feed",
            "importance": "medium",
            "category": "vuln",
            "dedup_key": "sample-dedup-key-001",
            "summary": "サンプル要約 (render 検証用)。",
        }
    ],
}

_PIR_SPOTLIGHT: dict[str, Any] = {
    "period_label": "2026-01-01 〜 2026-01-07 (サンプル)",
    "pir": {
        "id": "pir_sample",
        "title": "サンプル PIR (render 検証用)",
        "description": "render 検証用の説明文。",
        "strong_signals": {
            "actors": ["SampleActor"],
            "sectors": ["政府"],
            "countries": ["日本"],
        },
    },
    "candidate_articles": [
        {
            "article_id": "sample-001",
            "title": "サンプル記事 (render 検証用)",
            "url": "https://kuebiko.example/articles/sample-001",
            "feed_title": "sample-feed",
            "importance": "high",
            "created_at_jst": "2026-01-01 09:00 JST",
            "summary": "サンプル要約 (render 検証用)。",
            "ttps": ["T1566"],
            "reliability": "news",
        }
    ],
    "previous_spotlight": {
        "period_label": "前期 (サンプル)",
        "headline": "サンプル headline",
        "key_event_titles": ["サンプル前期 key event"],
        "outlook": "サンプル前期 outlook",
    },
    "ledger_situations": [
        {
            "claim": "サンプル claim (render 検証用)",
            "leading_hypothesis": "サンプル見立て",
            "confidence": "medium",
            "delta_type": "no_change",
            "delta_note": "",
            "implication": "サンプル含意",
        }
    ],
    "nation_correlation": [
        {
            "label": "日本",
            "role": "home",
            "attributed_cyber": 1,
            "cyber_mention": 1,
            "cyber_target": 1,
            "geopol": 1,
            "top_actors": ["SampleActor"],
        }
    ],
    "forecast_indicators": [
        {"scope": "actor", "target": "sample", "direction": "increase", "z": "2.1", "count": 3}
    ],
    "freshness": {"dated": 10, "retrospective": 3, "retrospective_pct": 30, "fresh": 5},
}

SAMPLE_CONTEXTS: dict[str, dict[str, Any]] = {
    "status_synthesis": _STATUS_SYNTHESIS,
    "weekly_recap": _WEEKLY_RECAP,
    "pir_daily_focus": _PIR_DAILY_FOCUS,
    "deep_dive_rubric": _DEEP_DIVE_RUBRIC,
    "pir_spotlight": _PIR_SPOTLIGHT,
}


def sample_context_for(prompt_id: str) -> dict[str, Any] | None:
    return SAMPLE_CONTEXTS.get(prompt_id)
