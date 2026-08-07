import { useEffect, useMemo, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { pageContainer } from "../components/Page";
import { Drawer } from "../components/Drawer";
import { pagesApi, type ActorRecord, type ActorObservedSummary } from "../api/pages";
import { useRuntimeFlags } from "../hooks/useRuntimeFlags";
import { vocabLabel } from "../hooks/useVocab";
import { ActorDetail, NationBadge } from "./actors/ActorDetail";
import { ActorSyncPanel } from "./actors/ActorSyncPanel";

type ViewMode = "table" | "cards";
type SortKey = "canonical" | "nation" | "family" | "last_seen";

const VIEW_STORAGE_KEY = "actors_view_mode";

// nation フィルタ chip 用: null nation は犯罪系 (motivation=financial) として集約
const NATION_NONE = "__none__";

// Actor 辞書: 一覧 (テーブル/カード切替 + nation/family フィルタ) + 詳細ドロワー +
// MITRE 同期レビュー。alias の重複 (= 誤帰属) は保存時に API が検証し 400 で拒否する。
export function ActorsPage() {
  const qc = useQueryClient();
  const { read_only } = useRuntimeFlags();
  const { data, isLoading } = useQuery({ queryKey: ["actors"], queryFn: () => pagesApi.getActors() });
  const { data: sync } = useQuery({ queryKey: ["actor-sync"], queryFn: () => pagesApi.getActorSync() });
  // P2-S8: 鮮度 (最終観測月) と照合実績のバッチ取得 (表の保守列 + 並び替え用)
  const { data: observed } = useQuery({
    queryKey: ["actors-observed-summary"],
    queryFn: () => pagesApi.actorsObservedSummary(),
  });
  const summaries = observed?.summaries ?? {};
  const [search, setSearch] = useState("");
  const [nationFilter, setNationFilter] = useState<string | null>(null);
  const [familyFilter, setFamilyFilter] = useState("");
  const [sortKey, setSortKey] = useState<SortKey>("canonical");
  const [view, setView] = useState<ViewMode>(
    () => (localStorage.getItem(VIEW_STORAGE_KEY) === "cards" ? "cards" : "table"),
  );
  const [editing, setEditing] = useState<ActorRecord | null>(null);
  const [syncOpen, setSyncOpen] = useState(false);

  // 脅威アクターページからの deep link (?actor=<id>) で詳細ドロワーを直接開く。
  // 一度消費したら再オープンしない (閉じる操作を妨げないため)。
  const deepLinkConsumed = useRef(false);
  useEffect(() => {
    if (deepLinkConsumed.current || !data) return;
    const id = new URLSearchParams(window.location.search).get("actor");
    if (id) {
      const found = data.actors.find((a) => a.id === id);
      if (found) setEditing(found);
    }
    deepLinkConsumed.current = true;
  }, [data]);

  const switchView = (v: ViewMode) => {
    setView(v);
    localStorage.setItem(VIEW_STORAGE_KEY, v);
  };

  // nation chip: 件数付き (cn/ru/kp/ir + nation なし=犯罪系)
  const nationCounts = useMemo(() => {
    const counts = new Map<string, number>();
    for (const a of data?.actors ?? []) {
      const key = a.nation ?? NATION_NONE;
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    return counts;
  }, [data]);

  const filtered = useMemo(() => {
    let list = data?.actors ?? [];
    if (nationFilter === NATION_NONE) list = list.filter((a) => !a.nation);
    else if (nationFilter) list = list.filter((a) => a.nation === nationFilter);
    if (familyFilter) list = list.filter((a) => a.family === familyFilter);
    if (search.trim()) {
      const q = search.toLowerCase();
      list = list.filter(
        (a) =>
          a.id.toLowerCase().includes(q) ||
          a.canonical.toLowerCase().includes(q) ||
          (a.aliases ?? []).some((x) => x.toLowerCase().includes(q)) ||
          (a.associated_malware ?? []).some((x) => x.toLowerCase().includes(q)) ||
          (a.mitre_group ?? "").toLowerCase().includes(q),
      );
    }
    const cmp = (x: string | null, y: string | null) => (x ?? "￿").localeCompare(y ?? "￿");
    return [...list].sort((a, b) => {
      if (sortKey === "nation") return cmp(a.nation, b.nation) || a.canonical.localeCompare(b.canonical);
      if (sortKey === "family") return cmp(a.family, b.family) || a.canonical.localeCompare(b.canonical);
      if (sortKey === "last_seen") {
        // 最終観測 (主題記事) の新しい順 — 未観測は末尾。休眠エントリの棚卸し用
        const la = summaries[a.id]?.last_month ?? "";
        const lb = summaries[b.id]?.last_month ?? "";
        return lb.localeCompare(la) || a.canonical.localeCompare(b.canonical);
      }
      return a.canonical.localeCompare(b.canonical);
    });
  }, [data, search, nationFilter, familyFilter, sortKey, summaries]);

  const chipCls = (active: boolean) =>
    `px-2 py-0.5 rounded-full text-xs border cursor-pointer whitespace-nowrap ${
      active
        ? "bg-accent text-white border-accent"
        : "bg-surface-2 text-fg-muted border-border-subtle hover:bg-surface-3"
    }`;

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h2 className="m-0 text-xl font-bold text-fg tracking-tight">アクター辞書</h2>
        <div className="flex items-center gap-3 flex-wrap">
          {sync && sync.pending_count > 0 && (
            <button
              onClick={() => setSyncOpen(true)}
              className="inline-flex items-center gap-1 px-2.5 py-1 rounded text-xs font-medium bg-accent-subtle text-accent border border-accent hover:bg-accent hover:text-white"
            >
              <RefreshCw className="h-3.5 w-3.5" /> レビュー待ち提案 {sync.pending_count} 件
            </button>
          )}
          {sync && sync.pending_count === 0 && (
            <button
              onClick={() => setSyncOpen(true)}
              className="inline-flex items-center gap-1 text-fg-subtle text-xs hover:text-fg-muted"
              title="MITRE 同期の状態を表示"
            >
              <RefreshCw className="h-3.5 w-3.5" /> MITRE 同期済 {sync.synced_actors}/{sync.total_actors}
            </button>
          )}
          <span className="text-fg-subtle text-xs tnum">{filtered.length}/{data?.actors.length ?? 0} 件</span>
        </div>
      </div>
      <p className="m-0 text-xs text-fg-subtle">
        脅威アクターの別名辞書。別名は記事のアクター帰属判定に使われ、
        <strong className="text-fg-muted">別アクターと重複すると誤帰属</strong>を生むため保存時に検証されます。
        MITRE ATT&CK との差分は週次で自動同期されます (概要は和訳)。
      </p>

      <div className="flex items-center gap-2 flex-wrap">
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="正式名 / 別名 / マルウェア / MITRE で検索…"
          className="bg-surface-2 border border-border-subtle rounded px-3 py-1.5 text-sm w-full max-w-xs"
        />
        <button className={chipCls(nationFilter === null)} onClick={() => setNationFilter(null)}>
          全て
        </button>
        {(["cn", "ru", "kp", "ir"] as const)
          .filter((n) => nationCounts.has(n))
          .map((n) => (
            <button key={n} className={chipCls(nationFilter === n)} onClick={() => setNationFilter(nationFilter === n ? null : n)}>
              {vocabLabel("country", n.toUpperCase())} {nationCounts.get(n)}
            </button>
          ))}
        {nationCounts.has(NATION_NONE) && (
          <button
            className={chipCls(nationFilter === NATION_NONE)}
            onClick={() => setNationFilter(nationFilter === NATION_NONE ? null : NATION_NONE)}
          >
            犯罪系 {nationCounts.get(NATION_NONE)}
          </button>
        )}
        <select
          value={familyFilter}
          onChange={(e) => setFamilyFilter(e.target.value)}
          className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs text-fg-muted"
        >
          <option value="">系統: 全て</option>
          {(data?.families ?? []).map((f) => (
            <option key={f} value={f}>{vocabLabel("actor_family", f)}</option>
          ))}
        </select>
        <select
          value={sortKey}
          onChange={(e) => setSortKey(e.target.value as SortKey)}
          className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs text-fg-muted"
        >
          <option value="canonical">並び: 名前</option>
          <option value="nation">並び: 国</option>
          <option value="family">並び: 系統</option>
          <option value="last_seen">並び: 最終観測</option>
        </select>
        <div className="ml-auto flex rounded border border-border-subtle overflow-hidden text-xs">
          <button
            onClick={() => switchView("table")}
            className={`px-2.5 py-1 ${view === "table" ? "bg-surface-3 text-fg" : "bg-surface-2 text-fg-subtle hover:text-fg-muted"}`}
            title="テーブル表示 (編集作業向け)"
          >
            表
          </button>
          <button
            onClick={() => switchView("cards")}
            className={`px-2.5 py-1 ${view === "cards" ? "bg-surface-3 text-fg" : "bg-surface-2 text-fg-subtle hover:text-fg-muted"}`}
            title="カード表示 (参照・閲覧向け)"
          >
            カード
          </button>
        </div>
      </div>

      {isLoading && <div className="text-fg-muted text-sm">読み込み中…</div>}

      {view === "table" ? (
        <ActorTable actors={filtered} summaries={summaries} onSelect={setEditing} />
      ) : (
        <ActorCardGrid actors={filtered} onSelect={setEditing} />
      )}

      <Drawer isOpen={editing !== null} onClose={() => setEditing(null)} title={editing?.canonical ?? ""}>
        {editing && (
          <ActorDetail
            key={editing.id}
            actor={editing}
            families={data?.families ?? []}
            readOnly={read_only}
            onSaved={() => {
              qc.invalidateQueries({ queryKey: ["actors"] });
              setEditing(null);
            }}
          />
        )}
      </Drawer>

      <Drawer isOpen={syncOpen} onClose={() => setSyncOpen(false)} title="MITRE 同期レビュー">
        {syncOpen && <ActorSyncPanel readOnly={read_only} />}
      </Drawer>
    </div>
  );
}

function ActorTable({
  actors,
  summaries,
  onSelect,
}: {
  actors: ActorRecord[];
  summaries: Record<string, ActorObservedSummary>;
  onSelect: (a: ActorRecord) => void;
}) {
  // 照合実績 (F5) はデータが貯まるまで全体非表示 (全ゼロを「死に辞書」と誤読させない)
  const hasAnyUsage = Object.values(summaries).some((s) => s.matched_names.length > 0);
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-sm">
        <thead className="bg-surface-2 text-fg-muted text-[10.5px] uppercase tracking-wider">
          <tr>
            <th className="text-left px-3 py-2 w-56">正式名</th>
            <th className="text-left px-3 py-2 hidden sm:table-cell">概要</th>
            <th className="text-left px-3 py-2 w-20">nation</th>
            <th className="text-left px-3 py-2 w-20 hidden sm:table-cell">MITRE</th>
            <th className="text-left px-3 py-2 w-28 hidden sm:table-cell">系統</th>
            <th
              className="text-left px-3 py-2 w-24 hidden md:table-cell"
              title="主題記事が最後に観測された月 (行動史ベース) — 休眠エントリの棚卸し用"
            >
              最終観測
            </th>
            {hasAnyUsage && (
              <th
                className="text-left px-3 py-2 w-24 hidden md:table-cell"
                title="照合実績のある名前 / 登録名の数 — 実績のない名前は整理候補"
              >
                照合実績
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {actors.map((a) => {
            const s = summaries[a.id];
            const nameCount = 1 + (a.aliases ?? []).length;
            const hitCount = s?.matched_names.length ?? 0;
            return (
              <tr
                key={a.id}
                className="border-t border-border-subtle hover:bg-surface-2 cursor-pointer"
                onClick={() => onSelect(a)}
              >
                <td className="px-3 py-2">
                  <div className="text-fg font-medium">{a.canonical}</div>
                  <div className="text-fg-subtle text-[11px] tnum">
                    別名 {(a.aliases ?? []).length}
                    {(a.associated_malware ?? []).length > 0 && <> ・ マルウェア {(a.associated_malware ?? []).length}</>}
                  </div>
                </td>
                <td className="px-3 py-2 text-fg-muted text-xs hidden sm:table-cell">
                  <span className="line-clamp-2">{a.summary || a.description || "—"}</span>
                </td>
                <td className="px-3 py-2"><NationBadge nation={a.nation} /></td>
                <td className="px-3 py-2 text-fg-muted text-xs font-mono hidden sm:table-cell">{a.mitre_group ?? "—"}</td>
                <td className="px-3 py-2 text-fg-muted text-xs hidden sm:table-cell">
                  {a.family ? vocabLabel("actor_family", a.family) : "—"}
                </td>
                <td className="px-3 py-2 text-xs tnum hidden md:table-cell">
                  {s?.last_month ? (
                    <span className="text-fg">{s.last_month}</span>
                  ) : (
                    <span className="text-fg-subtle">—</span>
                  )}
                </td>
                {hasAnyUsage && (
                  <td className="px-3 py-2 text-xs tnum hidden md:table-cell">
                    <span className={hitCount > 0 ? "text-fg" : "text-fg-subtle"}>
                      {hitCount}/{nameCount} 名
                    </span>
                  </td>
                )}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

const CARD_MALWARE_MAX = 4;

function ActorCardGrid({ actors, onSelect }: { actors: ActorRecord[]; onSelect: (a: ActorRecord) => void }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3">
      {actors.map((a) => {
        const malware = a.associated_malware ?? [];
        return (
          <button
            key={a.id}
            onClick={() => onSelect(a)}
            className="text-left rounded-lg border border-border-subtle bg-surface-2 p-3 space-y-2 hover:border-accent cursor-pointer"
          >
            <div className="flex items-center gap-2">
              <span className="text-sm font-semibold text-fg flex-1 truncate">{a.canonical}</span>
              <NationBadge nation={a.nation} />
            </div>
            <div className="flex items-center gap-2 text-[11px] text-fg-subtle">
              {a.mitre_group && <span className="font-mono">{a.mitre_group}</span>}
              {a.family && <span>{vocabLabel("actor_family", a.family)}</span>}
              <span className="tnum">別名 {(a.aliases ?? []).length}</span>
            </div>
            <p className="m-0 text-xs text-fg-muted leading-relaxed line-clamp-3 min-h-[3.6em]">
              {a.summary || a.description || "—"}
            </p>
            {malware.length > 0 && (
              <div className="flex flex-wrap gap-1">
                {malware.slice(0, CARD_MALWARE_MAX).map((m, i) => (
                  <span key={i} className="text-[10.5px] px-1.5 py-0.5 rounded bg-surface-3 text-fg-muted">
                    {m}
                  </span>
                ))}
                {malware.length > CARD_MALWARE_MAX && (
                  <span className="text-[10.5px] text-fg-subtle tnum">+{malware.length - CARD_MALWARE_MAX}</span>
                )}
              </div>
            )}
          </button>
        );
      })}
    </div>
  );
}
