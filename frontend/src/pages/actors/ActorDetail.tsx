import { type ReactNode, useState } from "react";
import { Pencil } from "lucide-react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  pagesApi,
  type ActorRecord,
  type ActorEdit,
  type ActorMonthRow,
  type ActorMonthArticle,
  ACTOR_KINDS,
} from "../../api/pages";
import { api } from "../../api/client";
import { actorHref, actorDictHref, situationHref } from "../../utils/intelNav";
import { vocabLabel } from "../../hooks/useVocab";
import { Sparkline } from "../../components/charts";

export function NationBadge({ nation }: { nation: string | null }) {
  if (!nation) return <span className="text-fg-subtle text-xs">—</span>;
  // ラベルは country vocab を SSoT に解決 (旧: cn/ru/kp/ir のみ持つ複製辞書だった)。
  const label = vocabLabel("country", nation.toUpperCase()) || nation;
  return (
    <span className="text-xs whitespace-nowrap" title={label}>
      <span className="uppercase text-fg-muted">{nation}</span>
    </span>
  );
}

export function mitreUrl(g: string | null): string | null {
  return g ? `https://attack.mitre.org/groups/${g}/` : null;
}

// 詳細ドロワー: 閲覧カード (reference) ⇄ 編集フォーム。
export function ActorDetail({
  actor,
  families,
  readOnly,
  onSaved,
}: {
  actor: ActorRecord;
  families: string[];
  readOnly: boolean;
  onSaved: () => void;
}) {
  const [editMode, setEditMode] = useState(false);
  if (editMode) {
    return (
      <ActorEditForm
        actor={actor}
        families={families}
        readOnly={readOnly}
        onSaved={onSaved}
        onCancel={() => setEditMode(false)}
      />
    );
  }
  return <ActorCard actor={actor} onEdit={readOnly ? undefined : () => setEditMode(true)} />;
}

function DetailRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div>
      <div className="text-[10px] uppercase tracking-wider text-fg-muted font-semibold mb-1">{label}</div>
      {children}
    </div>
  );
}

export function Chips({ items }: { items: string[] }) {
  if (!items.length) return <span className="text-fg-subtle text-sm">—</span>;
  return (
    <div className="flex flex-wrap gap-1.5">
      {items.map((x, i) => (
        <span key={i} className="text-[11px] px-1.5 py-0.5 rounded bg-surface-2 border border-border-subtle text-fg">
          {x}
        </span>
      ))}
    </div>
  );
}

function topKeys(counts: Record<string, number>, n: number): string[] {
  return Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, n)
    .map(([k]) => k);
}

// 観測ブロック (表示要領 2026-07-26): 言及 (30日) / 主題行動史 (月次・永年) / 関連情勢を
// **基底ラベルを明記して 1 箱**に統合する。旧構成は「観測 8 件」と「行動史 2 件」が
// 別箱で並び、言及≠主題の基底差が読めず矛盾に見えた (S1)。記事リストは脅威ページの
// 領分のためここには置かない (S7)。辞書=知識レンズの従属要素として概要より下に置く (S2)。
function ObservationBlock({
  actorId,
  kind,
  knownMalware = [],
}: {
  actorId: string;
  kind: string;
  // P3-S5: 公知の使用マルウェア (MITRE/公表情報)。観測装備とのギャップ検出に使う
  knownMalware?: string[];
}) {
  const { data: detail } = useQuery({
    queryKey: ["actorDetail", actorId, "30"],
    queryFn: () => api.actorDetail(actorId, 30),
  });
  const { data: history, isLoading } = useQuery({
    queryKey: ["actorHistory", actorId],
    queryFn: () => pagesApi.actorHistory(actorId),
  });
  const { data: sits } = useQuery({
    queryKey: ["actorSituations", actorId],
    queryFn: () => pagesApi.actorSituations(actorId),
  });
  if (isLoading) return <div className="text-fg-subtle text-xs">観測状況を読み込み中…</div>;

  const a = detail?.found ? detail.activity : undefined;
  const isOrg = kind === "organization" || kind === "contractor";
  const childGroups = detail?.child_groups ?? [];
  const months = [...(history?.months ?? [])].reverse(); // 新しい月から表示
  const spark = (history?.series ?? []).map((p) => p.subject_articles);
  const subjectTotal = spark.reduce((sum, v) => sum + v, 0);
  const situations = sits?.situations ?? [];
  // P3-S5: 公知リストにない観測装備 = 行動変化シグナル候補 (公知と観測のギャップ自体が情報)
  const knownKit = new Set(knownMalware.map((m) => m.toLowerCase()));
  const observedKit = new Set<string>();
  for (const m of history?.months ?? []) {
    for (const k of Object.keys(m.malware)) observedKit.add(k);
  }
  const novelKit = [...observedKit].filter((k) => !knownKit.has(k.toLowerCase())).sort();

  return (
    <div className="rounded border border-border-subtle bg-surface-2 px-3 py-2.5 space-y-2">
      {/* 言及 (30日): 本文照合ベース。主題 (下の行動史) とは基底が異なることをラベルで明示 */}
      <div className="flex items-center gap-2.5 text-xs">
        <span
          className="text-[10px] uppercase tracking-wider text-fg-muted font-semibold"
          title="本文照合で名前がヒットした記事数 (主題でない言及も含む)"
        >
          言及 (直近 30 日)
        </span>
        {a && a.total_articles > 0 ? (
          <>
            <span className="sparkline-svg" dangerouslySetInnerHTML={{ __html: a.sparkline }} />
            <span className="text-fg font-semibold tnum">{a.total_articles} 件</span>
          </>
        ) : (
          <span className="text-fg-subtle">{isOrg ? "単独言及なし" : "言及なし"}</span>
        )}
        <a href={actorHref(actorId)} className="ml-auto text-accent-hover whitespace-nowrap">
          脅威アクターで詳細 ↗
        </a>
      </div>
      {/* organization: 配下グループの活動 rollup (機関単位で全体動向を俯瞰) */}
      {isOrg && childGroups.length > 0 && (
        <div className="pt-1.5 border-t border-border-subtle">
          <div className="text-[10px] uppercase tracking-wider text-fg-muted font-semibold mb-1">
            配下グループの活動 (30 日)
          </div>
          <div className="space-y-1">
            {childGroups.map(([cid, canonical, count, spark2]) => (
              <a
                key={cid}
                href={actorDictHref(cid)}
                className="flex items-center gap-2 text-xs text-fg-muted hover:text-accent-hover"
              >
                <span className="flex-1 truncate">{canonical}</span>
                <span className="sparkline-svg" dangerouslySetInnerHTML={{ __html: spark2 }} />
                <span className="tnum">{count}</span>
              </a>
            ))}
          </div>
        </div>
      )}

      {/* 主題行動史 (月次・永年): subject 記事の決定論蒸留 (F7)。恒久史のホーム */}
      <div className="pt-1.5 border-t border-border-subtle">
        <div className="flex items-center gap-2.5 text-xs mb-1">
          <span
            className="text-[10px] uppercase tracking-wider text-fg-muted font-semibold"
            title="当該アクターを主題として報じた記事のみを数える永久記録 (言及だけの記事は含まない)"
          >
            主題記事の行動史 (月次・永年)
          </span>
          {spark.length > 1 && (
            <span className="text-accent">
              <Sparkline data={spark} width={90} height={20} highlightLast />
            </span>
          )}
          <span className="text-fg font-semibold tnum">{subjectTotal} 件</span>
          {(history?.merged_from ?? []).length > 0 && (
            <span className="text-[10px] text-fg-subtle">統合前 id の観測を含む</span>
          )}
        </div>
        {months.length > 0 ? (
          <div className="space-y-1.5">
            {months.slice(0, 12).map((m) => (
              <MonthRow key={m.month} actorId={actorId} m={m} />
            ))}
          </div>
        ) : (
          <div className="text-xs text-fg-subtle">主題記事の観測はまだありません</div>
        )}
        {novelKit.length > 0 && (
          <div
            className="mt-1.5 text-[11px] text-warning"
            title="自網の主題記事で観測されたが、公表情報 (使用マルウェア・ツール) に未記載の装備 — 行動変化または誤抽出の可能性"
          >
            ⚠ 公知リスト外の観測装備: {novelKit.join(" / ")}
          </div>
        )}
      </div>

      {situations.length > 0 && (
        <div className="pt-1.5 border-t border-border-subtle">
          <div className="text-[10px] uppercase tracking-wider text-fg-muted font-semibold mb-1">
            関連する情勢台帳
          </div>
          <div className="space-y-1">
            {situations.slice(0, 5).map((s) => (
              <a
                key={s.situation_id}
                href={situationHref(s.situation_id)}
                className="flex items-center gap-2 text-xs text-fg-muted hover:text-accent-hover"
              >
                <span className="flex-1 truncate">{s.title}</span>
                <span className="text-[10px] text-fg-subtle shrink-0">
                  {vocabLabel("situation_status", s.status)}
                </span>
              </a>
            ))}
          </div>
        </div>
      )}
      {history?.note && <p className="m-0 text-[10px] text-fg-subtle">{history.note}</p>}
    </div>
  );
}

// 月行 (S3 表示要領): 1 行目=件数系、2 行目=種別ラベル付きの標的/装備。
// クリックでその月の主題記事リスト (証拠開示、D5) を展開する。
function MonthRow({ actorId, m }: { actorId: string; m: ActorMonthRow }) {
  const [open, setOpen] = useState(false);
  const targets = [
    ...topKeys(m.sectors, 2).map((k) => vocabLabel("sector", k) || k),
    ...topKeys(m.countries, 2).map((k) => vocabLabel("country", k) || k),
  ];
  const kit = topKeys(m.malware, 3);
  return (
    <div className="text-xs min-w-0">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full text-left cursor-pointer hover:bg-surface-3 rounded px-1 -mx-1"
        title="クリックでこの月の主題記事を表示"
      >
        <div className="flex items-center gap-2">
          <span className="text-fg-subtle shrink-0 w-2.5 inline-block">{open ? "▾" : "▸"}</span>
          <span className="text-fg-muted tnum w-[54px] shrink-0">{m.month}</span>
          <span className="text-fg font-semibold tnum shrink-0">主題 {m.subject_articles} 件</span>
          <span
            className="text-[10px] text-fg-subtle tnum shrink-0"
            title="この月に当該アクターを主題として報じた情報源 (feed) の数 — 1 ソース量産と多ソース裏取りを区別する"
          >
            {m.distinct_sources} 源
          </span>
          {m.japan_targeted > 0 && (
            <span
              className="text-[10px] px-1 py-0.5 rounded bg-critical-soft text-critical font-semibold shrink-0"
              title="日本標的 (victim=JP または japan_watch 配信) の主題記事数"
            >
              JP {m.japan_targeted}
            </span>
          )}
          {m.kev_hits > 0 && (
            <span
              className="text-[10px] px-1 py-0.5 rounded bg-warning-soft text-warning font-semibold shrink-0"
              title="KEV (実環境悪用) 掲載 CVE を含む主題記事数"
            >
              KEV {m.kev_hits}
            </span>
          )}
        </div>
        {(targets.length > 0 || kit.length > 0) && (
          <div className="pl-[72px] text-fg-subtle truncate">
            {targets.length > 0 && <>標的: {targets.join(" / ")}</>}
            {targets.length > 0 && kit.length > 0 && <span className="mx-1.5">·</span>}
            {kit.length > 0 && <>装備: {kit.join(" / ")}</>}
          </div>
        )}
      </button>
      {open && <MonthArticles actorId={actorId} month={m.month} expected={m.subject_articles} />}
    </div>
  );
}

const MONTH_ARTICLES_SHOWN = 20;

// 月次カウントの中身 (証拠開示、D5)。ライブ照会のため、蒸留 (週次) との間で
// 件数が一時的にズレることがある — ズレは注記で正直に示す。
function MonthArticles({
  actorId,
  month,
  expected,
}: {
  actorId: string;
  month: string;
  expected: number;
}) {
  const { data, isLoading } = useQuery({
    queryKey: ["actorMonthArticles", actorId, month],
    queryFn: () => pagesApi.actorMonthArticles(actorId, month),
  });
  if (isLoading) return <div className="mt-1 pl-[72px] text-[11px] text-fg-subtle">読み込み中…</div>;
  const articles = data?.articles ?? [];
  const total = data?.total ?? 0;
  return (
    <div className="mt-1 ml-[62px] pl-2 border-l border-border-subtle space-y-1">
      {articles.slice(0, MONTH_ARTICLES_SHOWN).map((a) => (
        <MonthArticleRow key={a.article_id} a={a} />
      ))}
      {total > MONTH_ARTICLES_SHOWN && (
        <div className="text-[10px] text-fg-subtle tnum">他 {total - MONTH_ARTICLES_SHOWN} 件</div>
      )}
      {total === 0 && <div className="text-[11px] text-fg-subtle">記事が見つかりません</div>}
      {data && total !== expected && (
        <div className="text-[10px] text-fg-subtle">
          集計 ({expected} 件) と件数が異なります — 蒸留は週次のため次回蒸留で一致します
        </div>
      )}
    </div>
  );
}

function MonthArticleRow({ a }: { a: ActorMonthArticle }) {
  return (
    <div className="flex items-center gap-2 text-[11px] min-w-0">
      <span className="text-fg-subtle tnum shrink-0">{a.created_at.slice(5, 10)}</span>
      <a
        href={`/app/article/${encodeURIComponent(a.article_id)}`}
        className="flex-1 truncate text-fg-muted hover:text-accent-hover"
      >
        {a.title || a.article_id}
      </a>
      {a.japan_targeted && (
        <span className="text-[10px] px-1 rounded bg-critical-soft text-critical font-semibold shrink-0">JP</span>
      )}
      {a.kev_hit && (
        <span className="text-[10px] px-1 rounded bg-warning-soft text-warning font-semibold shrink-0">KEV</span>
      )}
      {a.url && (
        <a
          href={a.url}
          target="_blank"
          rel="noreferrer"
          className="text-fg-subtle hover:text-accent-hover shrink-0"
          title="元記事を開く"
        >
          ↗
        </a>
      )}
    </div>
  );
}

// 別名 (P2-S4): 名前の家を 1 箇所に統合 — 各名前に照合実績 (F5 累計記事数) を併記。
// 実績データが貯まるまではプレーン表示 (全ゼロを「死に alias」と誤読させない)。
function NameChips({ actor }: { actor: ActorRecord }) {
  const { data } = useQuery({
    queryKey: ["actorHistory", actor.id],
    queryFn: () => pagesApi.actorHistory(actor.id),
  });
  const usage = data?.alias_usage ?? {};
  const hasUsage = Object.keys(usage).length > 0;
  if (actor.aliases.length === 0 && !hasUsage) return null;
  const names = [actor.canonical, ...actor.aliases];
  return (
    <DetailRow label={hasUsage ? "別名と照合実績 (累計記事数)" : "別名"}>
      <div className="flex flex-wrap gap-1.5">
        {names.map((n) => {
          const count = usage[n] ?? 0;
          return (
            <span
              key={n}
              className={`text-[11px] px-1.5 py-0.5 rounded border border-border-subtle ${
                !hasUsage || count > 0 ? "bg-surface-2 text-fg" : "bg-surface-1 text-fg-subtle"
              }`}
              title={
                hasUsage && count === 0
                  ? "照合実績なし — 取込での発火が記録されていない名前 (整理候補)"
                  : undefined
              }
            >
              {n}
              {count > 0 && <span className="tnum text-fg-muted ml-1">{count}</span>}
            </span>
          );
        })}
      </div>
    </DetailRow>
  );
}

// 閲覧用 reference カード。表示要領 (2026-07-26): 知識 → 識別 → 観測 → 参考 → 保守 の順。
// 辞書=知識レンズのため「何者か」(概要) が先頭、観測は従属要素として中段の埋込 1 箱。
function ActorCard({ actor, onEdit }: { actor: ActorRecord; onEdit?: () => void }) {
  const url = mitreUrl(actor.mitre_group);
  const [summaryOpen, setSummaryOpen] = useState(false);
  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 flex-wrap text-xs">
        {actor.kind && actor.kind !== "group" && (
          <span className="px-1.5 py-0.5 rounded bg-warning-soft text-warning font-semibold">
            {vocabLabel("actor_kind", actor.kind)}
          </span>
        )}
        {actor.ambiguous && (
          <span
            className="px-1.5 py-0.5 rounded bg-surface-2 text-fg-muted"
            title="一般語と衝突する名前のため、文脈 cue 共起時のみ照合される"
          >
            文脈ゲート
          </span>
        )}
        {actor.nation && (
          <span
            className="px-1.5 py-0.5 rounded bg-accent-subtle text-accent uppercase font-semibold"
            title={vocabLabel("country", actor.nation.toUpperCase()) || actor.nation}
          >
            {actor.nation}
          </span>
        )}
        {actor.family && (
          <span className="px-1.5 py-0.5 rounded bg-surface-2 text-fg-muted">
            {vocabLabel("actor_family", actor.family)}
          </span>
        )}
        {actor.motivation && <span className="px-1.5 py-0.5 rounded bg-surface-2 text-fg-muted">{actor.motivation}</span>}
        {url && (
          <a href={url} target="_blank" rel="noreferrer" className="text-accent-hover">
            MITRE {actor.mitre_group} ↗
          </a>
        )}
        <span className="text-fg-subtle ml-auto">id: <code>{actor.id}</code></span>
      </div>

      {/* 1. 概要 (公知プロファイル) — 「何者か」を最初に答える */}
      {actor.summary && (
        <div>
          <p className={`m-0 text-sm text-fg leading-relaxed ${summaryOpen ? "" : "line-clamp-4"}`}>
            {actor.summary}
          </p>
          {actor.summary.length > 180 && (
            <button
              onClick={() => setSummaryOpen(!summaryOpen)}
              className="mt-1 text-xs text-accent-hover"
            >
              {summaryOpen ? "折りたたむ" : "続きを読む"}
            </button>
          )}
        </div>
      )}
      {/* 2. 分析官メモ (手動所有 — MITRE 由来の概要と出所を分離) */}
      {actor.description && (
        <DetailRow label="分析官メモ"><span className="text-sm text-fg">{actor.description}</span></DetailRow>
      )}

      {/* 3. 別名 (照合実績付き、名前の家はここ 1 箇所) */}
      <NameChips actor={actor} />

      {/* 4. 観測 (言及 30 日 / 主題行動史 / 関連情勢 — 基底ラベル付き 1 箱) */}
      <ObservationBlock
        actorId={actor.id}
        kind={actor.kind ?? "group"}
        knownMalware={actor.associated_malware ?? []}
      />

      {/* 5. 公知プロファイル詳細 (MITRE/公表情報由来) */}
      {actor.first_seen && (
        <DetailRow label="活動開始"><span className="text-sm text-fg">{actor.first_seen}</span></DetailRow>
      )}
      {actor.target_sectors.length > 0 && <DetailRow label="標的業種"><Chips items={actor.target_sectors} /></DetailRow>}
      {actor.target_regions.length > 0 && <DetailRow label="標的地域"><Chips items={actor.target_regions} /></DetailRow>}
      {actor.associated_malware.length > 0 && (
        <DetailRow label="使用マルウェア・ツール (公表情報)"><Chips items={actor.associated_malware} /></DetailRow>
      )}
      {(actor.mitre_ttps ?? []).length > 0 && (
        <details className="group">
          <summary className="cursor-pointer text-[10px] uppercase tracking-wider text-fg-muted font-semibold list-none select-none">
            <span className="inline-block transition-transform group-open:rotate-90">▸</span>{" "}
            既知 TTP (MITRE) · {actor.mitre_ttps.length} 件
          </summary>
          <div className="mt-2">
            <Chips items={actor.mitre_ttps} />
          </div>
        </details>
      )}
      {actor.notable_campaigns.length > 0 && (
        <DetailRow label="主要作戦"><Chips items={actor.notable_campaigns} /></DetailRow>
      )}
      {actor.sponsor && <DetailRow label="支援主体"><span className="text-sm text-fg">{actor.sponsor}</span></DetailRow>}

      {/* 6. 出典 (折畳 — 件数のみ常時表示) */}
      {actor.references.length > 0 && (
        <details className="group">
          <summary className="cursor-pointer text-[10px] uppercase tracking-wider text-fg-muted font-semibold list-none select-none">
            <span className="inline-block transition-transform group-open:rotate-90">▸</span>{" "}
            出典 · {actor.references.length} 件
          </summary>
          <ul className="m-0 mt-2 p-0 list-none space-y-1">
            {actor.references.map((r, i) => (
              <li key={i} className="truncate">
                <a href={r} target="_blank" rel="noreferrer" className="text-accent-hover text-xs">{r}</a>
              </li>
            ))}
          </ul>
        </details>
      )}

      {onEdit && (
        <div className="pt-2 border-t border-border-subtle">
          <button
            onClick={onEdit}
            className="inline-flex items-center gap-1.5 px-4 py-1.5 rounded text-sm font-medium bg-surface-2 border border-border-subtle text-fg hover:bg-surface-3"
          >
            <Pencil className="h-3.5 w-3.5" /> 編集
          </button>
        </div>
      )}
    </div>
  );
}

function ActorEditForm({
  actor,
  families,
  readOnly,
  onSaved,
  onCancel,
}: {
  actor: ActorRecord;
  families: string[];
  readOnly: boolean;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [canonical, setCanonical] = useState(actor.canonical);
  const [aliasesText, setAliasesText] = useState((actor.aliases ?? []).join("\n"));
  const [mitre, setMitre] = useState(actor.mitre_group ?? "");
  const [nation, setNation] = useState(actor.nation ?? "");
  const [sponsor, setSponsor] = useState(actor.sponsor ?? "");
  const [family, setFamily] = useState(actor.family ?? "");
  const [kind, setKind] = useState(actor.kind ?? "group");
  const [sponsorOrg, setSponsorOrg] = useState(actor.sponsor_org ?? "");
  const [description, setDescription] = useState(actor.description ?? "");
  const [ambiguous, setAmbiguous] = useState(actor.ambiguous ?? false);
  const [cuesText, setCuesText] = useState((actor.context_cues ?? []).join("\n"));
  // reference 用詳細 (Stage 3)
  const [summary, setSummary] = useState(actor.summary ?? "");
  const [motivation, setMotivation] = useState(actor.motivation ?? "");
  const [firstSeen, setFirstSeen] = useState(actor.first_seen ?? "");
  const [sectors, setSectors] = useState((actor.target_sectors ?? []).join("\n"));
  const [regions, setRegions] = useState((actor.target_regions ?? []).join("\n"));
  const [malware, setMalware] = useState((actor.associated_malware ?? []).join("\n"));
  const [campaigns, setCampaigns] = useState((actor.notable_campaigns ?? []).join("\n"));
  const [refs, setRefs] = useState((actor.references ?? []).join("\n"));

  const lines = (s: string) => s.split("\n").map((x) => x.trim()).filter(Boolean);

  const save = useMutation({
    mutationFn: () => {
      const body: ActorEdit = {
        canonical: canonical.trim(),
        aliases: lines(aliasesText),
        mitre_group: mitre.trim() || null,
        nation: nation.trim() || null,
        sponsor: sponsor.trim() || null,
        family: family.trim() || null,
        kind: kind.trim() || "group",
        sponsor_org: sponsorOrg.trim() || null,
        description: description.trim() || null,
        ambiguous,
        context_cues: lines(cuesText),
        summary: summary.trim() || null,
        motivation: motivation.trim() || null,
        first_seen: firstSeen.trim() || null,
        target_sectors: lines(sectors),
        target_regions: lines(regions),
        associated_malware: lines(malware),
        notable_campaigns: lines(campaigns),
        references: lines(refs),
      };
      return pagesApi.updateActor(actor.id, body);
    },
    onSuccess: onSaved,
  });

  const field = "w-full bg-surface-2 border border-border-subtle rounded px-2.5 py-1.5 text-sm text-fg";
  const label = "block text-[11px] uppercase tracking-wider text-fg-muted font-semibold mb-1";

  return (
    <div className="space-y-4">
      <p className="m-0 text-xs text-fg-subtle">
        id: <code className="text-fg-muted">{actor.id}</code>（変更できません）
      </p>
      <div>
        <label className={label}>正式名 (必須)</label>
        <input className={field} value={canonical} onChange={(e) => setCanonical(e.target.value)} disabled={readOnly} />
      </div>
      <div>
        <label className={label}>別名 (1 行 1 件)</label>
        <textarea
          className={`${field} font-mono text-xs`}
          rows={5}
          value={aliasesText}
          onChange={(e) => setAliasesText(e.target.value)}
          disabled={readOnly}
        />
        <p className="m-0 mt-1 text-[11px] text-fg-subtle">他アクターと重複する別名は保存時に拒否されます。</p>
      </div>
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className={label}>nation (ISO 2文字コード)</label>
          <input className={field} value={nation} onChange={(e) => setNation(e.target.value)} disabled={readOnly} />
        </div>
        <div>
          <label className={label}>MITRE group</label>
          <input className={field} value={mitre} onChange={(e) => setMitre(e.target.value)} disabled={readOnly} placeholder="G1017" />
        </div>
        <div>
          <label className={label}>系統</label>
          <select className={field} value={family} onChange={(e) => setFamily(e.target.value)} disabled={readOnly}>
            <option value="">（なし）</option>
            {families.map((f) => (
              <option key={f} value={f}>{vocabLabel("actor_family", f)}</option>
            ))}
          </select>
        </div>
        <div>
          <label className={label}>文脈ゲート (ambiguous)</label>
          <label className="flex items-center gap-2 text-sm text-fg cursor-pointer">
            <input
              type="checkbox"
              checked={ambiguous}
              onChange={(e) => setAmbiguous(e.target.checked)}
              disabled={readOnly}
            />
            一般語と衝突する名前 (文脈 cue 共起時のみ照合)
          </label>
          <p className="m-0 mt-1 text-[11px] text-fg-subtle">
            一般語の名前を持つアクターはこれを有効にしないと保存できません。
          </p>
        </div>
      </div>
      {ambiguous && (
        <div>
          <label className={label}>文脈 cue (1 行 1 件、空なら既定セット)</label>
          <textarea
            className={`${field} text-xs`}
            rows={3}
            value={cuesText}
            onChange={(e) => setCuesText(e.target.value)}
            disabled={readOnly}
            placeholder="hacktivist&#10;ddos&#10;犯行声明"
          />
        </div>
      )}
      <div className="grid grid-cols-2 gap-3">
        <div>
          <label className={label}>種別</label>
          <select className={field} value={kind} onChange={(e) => setKind(e.target.value)} disabled={readOnly}>
            {ACTOR_KINDS.map((k) => (
              <option key={k} value={k}>{vocabLabel("actor_kind", k)}</option>
            ))}
          </select>
          <p className="m-0 mt-1 text-[11px] text-fg-subtle">国家機関・請負は「活動」でなく「言及」として集計されます。</p>
        </div>
        <div>
          <label className={label}>親機関</label>
          <input className={field} value={sponsorOrg} onChange={(e) => setSponsorOrg(e.target.value)} disabled={readOnly} placeholder="russia_gru" />
          <p className="m-0 mt-1 text-[11px] text-fg-subtle">グループの親機関のアクター ID。記事帰属の二重計上を防ぎます。</p>
        </div>
      </div>
      <div>
        <label className={label}>支援主体 (自由記述)</label>
        <input className={field} value={sponsor} onChange={(e) => setSponsor(e.target.value)} disabled={readOnly} />
      </div>
      <div>
        <label className={label}>メモ (短い説明)</label>
        <textarea className={field} rows={2} value={description} onChange={(e) => setDescription(e.target.value)} disabled={readOnly} />
      </div>

      <div className="pt-3 border-t border-border-subtle space-y-3">
        <p className="m-0 text-[11px] text-fg-subtle font-semibold uppercase tracking-wider">参照用の詳細情報</p>
        <div>
          <label className={label}>概要</label>
          <textarea className={field} rows={3} value={summary} onChange={(e) => setSummary(e.target.value)} disabled={readOnly} />
        </div>
        <div className="grid grid-cols-2 gap-3">
          <div>
            <label className={label}>動機</label>
            <input className={field} value={motivation} onChange={(e) => setMotivation(e.target.value)} disabled={readOnly} placeholder="espionage / financial …" />
          </div>
          <div>
            <label className={label}>活動開始</label>
            <input className={field} value={firstSeen} onChange={(e) => setFirstSeen(e.target.value)} disabled={readOnly} placeholder="2021" />
          </div>
          <div>
            <label className={label}>標的業種 (1 行 1 件)</label>
            <textarea className={`${field} text-xs`} rows={3} value={sectors} onChange={(e) => setSectors(e.target.value)} disabled={readOnly} />
          </div>
          <div>
            <label className={label}>標的地域 (1 行 1 件)</label>
            <textarea className={`${field} text-xs`} rows={3} value={regions} onChange={(e) => setRegions(e.target.value)} disabled={readOnly} />
          </div>
        </div>
        <div>
          <label className={label}>使用マルウェア・ツール (1 行 1 件)</label>
          <textarea className={`${field} text-xs`} rows={4} value={malware} onChange={(e) => setMalware(e.target.value)} disabled={readOnly} />
        </div>
        <div>
          <label className={label}>主要作戦 (1 行 1 件)</label>
          <textarea className={`${field} text-xs`} rows={2} value={campaigns} onChange={(e) => setCampaigns(e.target.value)} disabled={readOnly} />
        </div>
        <div>
          <label className={label}>出典 URL (1 行 1 件)</label>
          <textarea className={`${field} text-xs font-mono`} rows={3} value={refs} onChange={(e) => setRefs(e.target.value)} disabled={readOnly} />
        </div>
      </div>

      {!readOnly && (
        <div className="flex items-center gap-3 pt-2 border-t border-border-subtle">
          <button
            onClick={() => save.mutate()}
            disabled={save.isPending || !canonical.trim()}
            className="px-4 py-1.5 rounded text-sm font-medium bg-accent text-white hover:bg-accent-hover disabled:opacity-50"
          >
            {save.isPending ? "保存中…" : "保存"}
          </button>
          <button
            onClick={onCancel}
            className="px-4 py-1.5 rounded text-sm font-medium bg-surface-2 border border-border-subtle text-fg-muted hover:bg-surface-3"
          >
            キャンセル
          </button>
          {save.isError && (
            <span className="text-critical text-sm">{(save.error as Error).message}</span>
          )}
        </div>
      )}
    </div>
  );
}
