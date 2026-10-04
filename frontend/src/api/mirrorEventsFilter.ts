// 写し (ops-mirror) の事象ニュース一覧を、画面の絞り込みと同じ意味論で
// ブラウザ側に適用する。
//
// backend (src/ui/api/eventnews.py:list_event_news) は記事側の条件を
// 記事単位で評価してから事象へ持ち上げる (1 件でも該当メンバーを含む事象を返す)。
// 写しは 1 ファイルに事象を持つだけなので、必要な値は書き出し時に
// scripts/export_mirror.py の `_eventnews_filter_tags` が詳細 API から複製して
// 一覧アイテムへ足している (categories / channels / intents / feeds / entities /
// vendors)。
//
// ⚠ **近似であることの明記**: 記事単位の AND (同じ 1 本の記事が複数条件を同時に
// 満たす) までは再現できず、事象単位の OR (いずれかの構成記事が持つ値の和集合)
// で近似する。単一条件の絞り込みは忠実、複数条件の組み合わせ (例: category=vuln
// かつ actor=X) は近似になる。
import type { EventNewsListItem, EventNewsQuery } from "./eventnews";

/** 写し用に export_mirror.py が複製した絞り込み値。元値が無ければ省略されうる。 */
export interface MirrorEventTags {
  categories?: string[];
  channels?: string[];
  intents?: string[];
  feeds?: string[];
  entities?: Record<string, string[]>;
  vendors?: string[];
}

export type MirrorEventNewsItem = EventNewsListItem & MirrorEventTags;

/** category の合成グループ。SSoT は src/ui/api/articles_feed.py の `_CATEGORY_GROUPS`
 *  — 写しはブラウザ内で評価するため複製する (変えたら両方直す)。 */
const CATEGORY_GROUPS: Record<string, string[]> = {
  vuln: ["vulnerability", "advisory"],
  threat: ["malware", "apt", "apt_leak", "phishing"],
  incident_breach: ["incident", "breach"],
};

function includesCi(values: string[] | undefined, needle: string): boolean {
  if (!values || values.length === 0) return false;
  const n = needle.toLowerCase();
  return values.some((v) => v.toLowerCase() === n);
}

function matchesCategory(item: MirrorEventNewsItem, category: string): boolean {
  const wanted = CATEGORY_GROUPS[category] ?? [category];
  return wanted.some((c) => includesCi(item.categories, c));
}

function matchesSearch(item: MirrorEventNewsItem, term: string): boolean {
  const n = term.trim().toLowerCase();
  if (!n) return true;
  // 写しは本文を持たないため、見出し + 要点の冒頭 (preview) のみを対象にする。
  // ライブはこれに加え構成記事の本文も検索するので、写しの検索は部分集合になる。
  const haystack = `${item.headline} ${item.preview}`.toLowerCase();
  return haystack.includes(n);
}

/** 事象 1 件が絞り込み条件すべてに合致するか。 */
function matchesEvent(item: MirrorEventNewsItem, q: EventNewsQuery): boolean {
  if (q.importance) {
    const wanted = q.importance.split(",").map((s) => s.trim()).filter(Boolean);
    if (wanted.length > 0 && !wanted.includes(item.importance)) return false;
  }
  if (q.status) {
    const wanted = q.status.split(",").map((s) => s.trim()).filter(Boolean);
    if (wanted.length > 0 && !wanted.includes(item.status)) return false;
  }
  if (q.min_independent_sources && item.independent_sources < q.min_independent_sources) {
    return false;
  }
  if (q.has_news !== undefined && item.has_news !== q.has_news) return false;
  if (q.since_hours && q.since_hours > 0) {
    const cutoff = Date.now() - q.since_hours * 60 * 60 * 1000;
    if (new Date(item.last_reported_at).getTime() < cutoff) return false;
  }
  if (q.search && !matchesSearch(item, q.search)) return false;
  if (q.category && !matchesCategory(item, q.category)) return false;
  if (q.channel && !includesCi(item.channels, q.channel)) return false;
  if (q.intent && !includesCi(item.intents, q.intent)) return false;
  if (q.feed && !includesCi(item.feeds, q.feed)) return false;
  if (q.actor && !includesCi(item.entities?.actor, q.actor)) return false;
  if (q.cve && !includesCi(item.entities?.cve, q.cve)) return false;
  if (q.malware && !includesCi(item.entities?.malware_family, q.malware)) return false;
  if (q.pir && !includesCi(item.entities?.pir, q.pir)) return false;
  if (q.affected_vendor) {
    const n = q.affected_vendor.trim().toLowerCase();
    if (!item.vendors?.some((v) => v.toLowerCase().includes(n))) return false;
  }
  if (q.entity_type && q.entity_value) {
    if (!includesCi(item.entities?.[q.entity_type], q.entity_value)) return false;
  }
  if (q.jp === "targeted_affected" && item.jp !== "targeted" && item.jp !== "affected") {
    return false;
  }
  if (q.jp === "mentioned" && (!item.jp || item.jp === "none")) return false;
  if (!matchesSeverityFacet(item, q)) return false;
  return true;
}

// 深刻さ facet の許容値 → severity の許容集合 (常に上位からの prefix)。
// SSoT: src/storage/importance_level_sql.py:SEVERITY_ALLOWED。
const SEVERITY_ALLOWED: Record<string, ("S3" | "S2" | "S1")[]> = {
  S3: ["S3"],
  S2: ["S3", "S2"],
  S1: ["S3", "S2", "S1"],
};

/** 深刻さ・関連性・戦略上の重みの 3 独立 facet (2026-10-04)。ライブ
 *  (src/storage/importance_level_sql.py:severity_relevance_exists_for_event) と同じ
 *  意味論を、事象の代表値 (severity=最良メンバー / relevant=いずれかのメンバー /
 *  strategic_weight_heavy_no_level=いずれかのメンバー) から再現する。旧 1 本化
 *  ``level_filter`` (top/notable/relevant) も後方互換で受け付け、新 3 facet が
 *  無指定のときだけ写像する。 */
function matchesSeverityFacet(item: MirrorEventNewsItem, q: EventNewsQuery): boolean {
  let minSeverity = q.min_severity ?? "";
  let relevantOnly = Boolean(q.relevant_only);
  let includeStrategic = Boolean(q.include_strategic);
  if (!minSeverity && !relevantOnly && !includeStrategic && q.level_filter) {
    if (q.level_filter === "top") {
      minSeverity = "S3";
    } else if (q.level_filter === "notable") {
      minSeverity = "S2";
      includeStrategic = true;
    } else if (q.level_filter === "relevant") {
      minSeverity = "S1";
      relevantOnly = true;
    }
  }
  if (!minSeverity && !relevantOnly) return true;
  if (!minSeverity) return item.relevant === true;
  const allowed = SEVERITY_ALLOWED[minSeverity] ?? [];
  let core = item.severity != null && allowed.includes(item.severity);
  if (includeStrategic) {
    core = core || (item.severity == null && item.strategic_weight_heavy_no_level === true);
  }
  if (relevantOnly) core = core && item.relevant === true;
  return core;
}

// 重要度 6 段階 (1 が最上位・未記録は null)。null は常に最後に回す。
function levelSortValue(item: MirrorEventNewsItem): number {
  return item.level == null ? Number.POSITIVE_INFINITY : item.level;
}

/** 写しの事象一覧へ、画面の絞り込み・並び順・ページングを適用する。
 *  ライブ API と同じ応答の形 ({items, note, scan_capped}) を返す。 */
export function filterMirrorEvents(
  all: MirrorEventNewsItem[],
  q: EventNewsQuery,
): { items: MirrorEventNewsItem[]; note: string; scan_capped?: boolean } {
  const matched = all.filter((it) => matchesEvent(it, q));
  if (q.sort === "level") {
    // 重要度 6 段階の高い順 (未記録は最後) → 同値は新着順 (ライブと同じ意味論)。
    matched.sort((a, b) => {
      const diff = levelSortValue(a) - levelSortValue(b);
      if (diff !== 0) return diff;
      return new Date(b.last_reported_at).getTime() - new Date(a.last_reported_at).getTime();
    });
  } else {
    // 新着順 (last_reported_at DESC) — ライブの既定順と同じ。書き出し時点で既に
    // この順だが、フィルタ後の安定性のため明示的にも揃える。
    matched.sort(
      (a, b) => new Date(b.last_reported_at).getTime() - new Date(a.last_reported_at).getTime(),
    );
  }
  const offset = Math.max(0, q.offset ?? 0);
  const limit = q.limit ?? 50;
  return {
    items: matched.slice(offset, offset + limit),
    note: "",
  };
}
