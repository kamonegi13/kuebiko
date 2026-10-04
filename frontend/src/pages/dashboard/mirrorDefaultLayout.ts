// 写し (アドバンスド) の PC のダッシュボードの既定の並び (2026-10-04 利用者が写しで組んだ並びをそのまま採用)。
// 事象ニュースの旧設定 (importance=high,medium) は表示時に「注意以上・政策地政学を含む」へ読み替わる。
// スマートフォンは従来どおり registry から自動で組む並び (DashboardPage の buildMirrorLayout)。
// ⚠ 部品の id を変えたらここも直す。写しで出さない部品 (liveState / mirrorExcluded) は
//    DashboardPage 側の sanitizeMirrorStored が落とす。
import type { StoredDashboardLayout } from "../../api/dashboard";

export const MIRROR_PC_DEFAULT_LAYOUT: StoredDashboardLayout = {
  widgets: [
    { id: "kpi_row", uid: "kpi_row", x: 0, y: 0, w: 7, h: 17, config: {} },
    { id: "notable_actors", uid: "notable_actors", x: 7, y: 0, w: 3, h: 17, config: {} },
    { id: "holdings", uid: "holdings", x: 10, y: 0, w: 2, h: 17, config: {} },
    { id: "mini_map", uid: "mini_map", x: 0, y: 17, w: 7, h: 33, config: {} },
    { id: "geo_ranking", uid: "geo_ranking", x: 7, y: 17, w: 3, h: 22, config: {} },
    { id: "jp_ci_threat", uid: "jp_ci_threat", x: 10, y: 17, w: 2, h: 22, config: {"days": "auto"} },
    { id: "geo_trend", uid: "geo_trend", x: 7, y: 39, w: 5, h: 11, config: {} },
    { id: "standing_assessment", uid: "standing_assessment", x: 0, y: 50, w: 12, h: 8, config: {} },
    { id: "eventnews", uid: "eventnews", x: 0, y: 58, w: 6, h: 22, config: {"importance": "high,medium", "per": 6} },
    { id: "news_feed", uid: "news_feed", x: 6, y: 58, w: 6, h: 22, config: {"mode": "summary", "per": "12"} },
    { id: "separator", uid: "separator", x: 0, y: 80, w: 12, h: 3, config: {"label": ""} },
    { id: "pir_spotlight", uid: "pir_spotlight", x: 0, y: 83, w: 6, h: 22, config: {} },
    { id: "news_feed", uid: "news_feed_2", x: 6, y: 83, w: 6, h: 22, config: {"mode": "summary", "per": 5} },
  ],
};
