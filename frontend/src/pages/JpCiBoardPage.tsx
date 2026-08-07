import { Fragment, useState } from "react";
import { AlertTriangle, ChevronDown, ChevronRight, ExternalLink, Globe, ListChecks } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { pageContainer } from "../components/Page";
import { usePersistedState } from "../utils/usePersistedState";
import { vocabLabel } from "../hooks/useVocab";
import {
  MATRIX_COLUMNS,
  jpciApi,
  type ActiveStageId,
  type BoardChange,
  type BoardItem,
  type Campaign,
  type PostureCard,
  type SectorPosture,
  type ThreatBehavior,
} from "../api/jpci";
import {
  ConfidencePill,
  DeltaBadge,
  IntentBadge,
  NationTag,
  OperatorTierBadge,
  PrecisionBadge,
  STAGE_STYLE,
  StageBadge,
  StatusBadge,
  SystemDomainBadge,
  SystemDomainMix,
  SystemicBadge,
} from "../components/jpci/Badges";

const DAY_OPTS = [
  { v: 7, label: "7日" },
  { v: 30, label: "30日" },
  { v: 90, label: "90日" },
  { v: 0, label: "全期間" },
];

// NISC 分野の固定グルーピング (毎日同じ配置 = 変化が目に飛び込む空間記憶)
const SECTOR_GROUPS: { label: string; sectors: string[] }[] = [
  { label: "エネルギー・ライフライン", sectors: ["electricity", "gas", "oil", "water"] },
  { label: "情報通信", sectors: ["it_telecom"] },
  { label: "金融・決済", sectors: ["finance", "credit"] },
  { label: "交通・物流", sectors: ["railway", "aviation", "airport", "logistics", "port"] },
  { label: "医療・化学", sectors: ["medical", "chemical"] },
  { label: "政府・防衛", sectors: ["government", "defense"] },
];

export function JpCiBoardPage() {
  const [days, setDays] = usePersistedState<number>("jpci.days", 30);
  const [expanded, setExpanded] = useState<string | null>(null);
  const { data, error, isLoading } = useQuery({
    queryKey: ["jpci-board", days],
    queryFn: () => jpciApi.board(days),
    staleTime: 60_000, // backend の TTL cache (60s) と揃える
  });

  const byId = new Map((data?.sectors ?? []).map((s) => [s.nisc_sector, s]));

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      <div className="flex items-center justify-between flex-wrap gap-2">
        <div className="flex items-center gap-3 flex-wrap">
          <h2 className="m-0 text-xl font-bold text-fg tracking-tight">
            日本重要インフラ 脅威ボード
          </h2>
          <a
            href="/app/jpci/operators"
            className="inline-flex items-center gap-1 text-xs text-fg-muted hover:text-accent border border-border-subtle rounded px-2 py-1"
          >
            <ListChecks className="h-3.5 w-3.5" />
            指定事業者名簿
          </a>
        </div>
        <div
          role="group"
          aria-label="表示期間"
          className="inline-flex bg-surface-2 border border-border-subtle rounded-md"
        >
          {DAY_OPTS.map((o) => (
            <button
              key={o.v}
              aria-pressed={o.v === days}
              onClick={() => setDays(o.v)}
              className={`px-2.5 py-1.5 text-sm ${
                o.v === days
                  ? "bg-accent/20 text-accent font-semibold"
                  : "text-fg-muted hover:text-fg"
              }`}
            >
              {o.label}
            </button>
          ))}
        </div>
      </div>

      {data && (
        <div className="space-y-2">
          <div className="bg-warning-soft border border-warning/40 rounded-lg p-3 text-xs text-fg-muted flex items-start gap-2">
            <AlertTriangle className="h-4 w-4 shrink-0 mt-0.5 text-warning" />
            <span>{data.note}</span>
          </div>
          <div className="flex items-center gap-x-4 gap-y-1 flex-wrap text-[11px] text-fg-subtle px-1">
            <span className="font-semibold text-fg-muted">凡例:</span>
            <span>
              <span className="text-accent font-semibold">指定事業者</span> = 経済安保推進法 特定社会基盤事業者 + 補完 (防衛産業/衛星/主要ISP)
            </span>
            <span>
              <span className="text-fg-muted font-semibold">分野該当</span> = 事業者名のパターンで判定 (〜病院/〜市 等、名簿外の中小)
            </span>
            <span>
              <span className="text-critical font-semibold">基幹ノード</span> = 清算・決済・取引の集中点 (分野をまたいで連鎖波及)
            </span>
            <span>指定 / 観測 = 既知の指定事業者のうち、期間内に観測された数 (残りは無音=暗域)</span>
            <span>
              <span className="text-fg-muted font-semibold">世界の国家アクター行動</span> = 日本の被害を名指さない情報源全体から、この分野を狙う敵性国家の行動 (先行指標・日本への含意は示唆)
            </span>
          </div>
        </div>
      )}

      {error && (
        <div className="p-3 bg-critical/10 text-critical rounded text-sm">
          {error instanceof Error ? error.message : String(error)}
        </div>
      )}
      {isLoading && <div className="text-center py-8 text-fg-muted text-sm">読み込み中…</div>}

      {data && data.posture && data.posture.length > 0 && (
        <PostureSection cards={data.posture} />
      )}

      {data && data.campaigns.length > 0 && <CampaignBanner campaigns={data.campaigns} />}

      {data && data.changes.length > 0 && (
        <ChangesStrip changes={data.changes} stageLabels={data.stage_labels} />
      )}

      {data && (
        <div className="overflow-x-auto border border-border-subtle rounded-lg bg-surface-1">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="text-[11px] text-fg-subtle border-b border-border-subtle">
                <th className="text-left font-semibold px-3 py-2">分野</th>
                {MATRIX_COLUMNS.map((c) => (
                  <th
                    key={c}
                    className="text-center font-semibold px-2 py-2 hidden md:table-cell w-24"
                    style={{ color: STAGE_STYLE[c] }}
                  >
                    {vocabLabel("jpci_stage", c)}
                  </th>
                ))}
                <th className="text-left font-semibold px-2 py-2 w-32">到達段階</th>
                <th className="text-center font-semibold px-2 py-2 w-20">Δ 前期間</th>
                <th className="text-center font-semibold px-2 py-2 hidden lg:table-cell w-24">
                  指定 / 観測
                </th>
              </tr>
            </thead>
            <tbody>
              {SECTOR_GROUPS.map((g) => (
                <Fragment key={g.label}>
                  <tr className="bg-surface-2/60">
                    <td
                      colSpan={MATRIX_COLUMNS.length + 4}
                      className="px-3 py-1 text-[10px] font-semibold text-fg-subtle tracking-wide"
                    >
                      {g.label}
                    </td>
                  </tr>
                  {g.sectors.map((id) => {
                    const s = byId.get(id);
                    if (!s) return null;
                    return (
                      <SectorRow
                        key={id}
                        sector={s}
                        expanded={expanded === id}
                        onToggle={() => setExpanded(expanded === id ? null : id)}
                      />
                    );
                  })}
                </Fragment>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

// 横断キャンペーン: 集約が個別記事を超えて示す創発信号 (協調的インフラ作戦の兆候)。
// 確度ラベルは backend 配信 vocab "confidence" (vocabLabel) を SSoT に。ここは色 (tone) のみ保持。
const CONF_TONE: Record<string, string> = {
  high: "bg-accent-subtle text-accent border-accent/30",
  moderate: "bg-warning-soft text-warning border-warning/30",
  low: "border-border-default text-fg-subtle",
};

const CONF_BAR: Record<string, string> = { high: "█", moderate: "▄", low: "▁" };

// 常設情報要求 posture カード (段C): 台帳の現在推定 + 確度の軌跡。ラダー/世界行動とは
// 独立の第 3 レンズ (確度を分野行に折り込まない — 設計 §5.1 の確定原則)。
// クリックで推移 (revision タイムライン: いつ・何が・なぜ動いたか) を展開する。
function PostureSection({ cards }: { cards: PostureCard[] }) {
  const [openId, setOpenId] = useState<string | null>(null);
  return (
    <div className="border border-border-subtle rounded-lg p-3 space-y-2 bg-surface-1">
      <div className="text-[11px] font-bold text-fg-muted flex items-center gap-2 flex-wrap">
        <span>事前配置の見立て (常設情報要求 — 台帳の確度の推移。観測データとは別の視点)</span>
        <a href="/app/intel/synthesis" className="text-accent hover:underline font-normal">
          台帳で検証 →
        </a>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 xl:grid-cols-4 gap-2">
        {cards.map((c) => {
          const confTone = CONF_TONE[c.confidence] ?? null;
          const spark = c.trajectory.map((t) => CONF_BAR[t.confidence] ?? "▁").join("");
          const open = openId === c.situation_id;
          return (
            <button
              key={c.situation_id}
              onClick={() => setOpenId(open ? null : c.situation_id)}
              className="text-left border border-border-subtle rounded-md p-2.5 space-y-1.5 min-w-0 hover:bg-surface-2 transition-colors"
              title={c.question}
            >
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm font-bold text-fg">{c.nation_label}</span>
                {c.assessed && confTone ? (
                  <span className={`text-[10px] px-1.5 py-0.5 rounded border whitespace-nowrap ${confTone}`}>
                    {vocabLabel("confidence", c.confidence)}
                  </span>
                ) : (
                  <span className="text-[10px] px-1.5 py-0.5 rounded border border-border-default text-fg-subtle whitespace-nowrap">
                    未評価 (証拠なし)
                  </span>
                )}
              </div>
              <div className="text-[11px] text-fg leading-relaxed line-clamp-3">
                {c.assessed ? c.leading_label : "帰属済みの証拠が期間内に無い (観測の不在は不在の証明ではない)"}
              </div>
              {c.assessed && c.confidence_basis && (
                <div className="text-[10px] text-fg-subtle">確度の根拠: {c.confidence_basis}</div>
              )}
              <div className="text-[10px] text-fg-subtle flex flex-wrap gap-x-3 gap-y-0.5">
                <span>直接 (JP) {c.evidence_direct_30d} / 関連 {c.evidence_related_30d} 件 (30日)</span>
                {c.assessed && (
                  <span>
                    軌跡 <span className="tnum tracking-tighter">{spark}</span>
                  </span>
                )}
              </div>
              {open && c.trajectory.length > 0 && (
                <ul className="m-0 mt-1.5 p-0 pt-1.5 list-none space-y-1 border-t border-border-subtle">
                  {[...c.trajectory].reverse().map((t) => {
                    const tcTone = CONF_TONE[t.confidence] ?? null;
                    return (
                      <li key={t.rev} className="text-[10px] leading-relaxed flex items-start gap-1.5 flex-wrap">
                        <span className="text-fg-subtle tnum shrink-0">{t.at.slice(5, 10)}</span>
                        <span className="text-fg font-medium shrink-0">
                          {vocabLabel("delta_type", t.delta_type)}
                        </span>
                        {tcTone && <span className={`px-1 rounded border shrink-0 ${tcTone}`}>{vocabLabel("confidence", t.confidence)}</span>}
                        {t.note && <span className="text-fg-subtle min-w-0">{t.note}</span>}
                      </li>
                    );
                  })}
                </ul>
              )}
            </button>
          );
        })}
      </div>
    </div>
  );
}

function CampaignBanner({ campaigns }: { campaigns: Campaign[] }) {
  // 世界コーパス由来の示唆情報 — 警報級 (赤枠/赤⚠) にしない (認識論的視覚階層)。
  return (
    <div className="border border-border-default bg-surface-2 rounded-lg p-3 space-y-1.5">
      <div className="text-[11px] font-bold text-fg-muted inline-flex items-center gap-1.5">
        <Globe className="h-3.5 w-3.5 text-fg-subtle" />
        横断キャンペーンの兆候 (同一アクターが複数分野で行動 = 協調的インフラ作戦の可能性)
      </div>
      <ul className="m-0 p-0 list-none space-y-1">
        {campaigns.map((c) => (
          <li key={c.actor} className="text-xs text-fg flex items-start gap-2 flex-wrap">
            <span className="font-bold text-fg shrink-0">{c.actor}</span>
            <NationTag nation={c.nation} />
            <IntentBadge intent={c.intent} />
            <span className="text-fg-muted min-w-0">
              {c.sectors.join("・")} の {c.sector_ids.length} 分野で活動 ({c.articles}報道)
              {c.jp_targeted && <span className="text-critical font-semibold"> / 日本標的</span>}
            </span>
          </li>
        ))}
      </ul>
      <div className="text-[10px] text-fg-subtle">
        ※ 情報源全体の集約による相関。報道時刻は実発生と異なり、因果は主張しない。
      </div>
    </div>
  );
}

function ChangesStrip({
  changes,
  stageLabels,
}: {
  changes: BoardChange[];
  stageLabels: Record<string, string>;
}) {
  return (
    <div className="border border-border-subtle rounded-lg bg-surface-1 p-3 space-y-1.5">
      <div className="text-[11px] font-semibold text-fg-muted">前期間からの変化</div>
      <ul className="m-0 p-0 list-none space-y-1">
        {changes.map((c) => (
          <li key={c.nisc_sector} className="text-xs text-fg flex items-start gap-2 flex-wrap">
            <span
              className={`font-bold shrink-0 ${c.direction === "up" ? "text-critical" : "text-accent"}`}
            >
              {c.direction === "up" ? "▲" : "▼"} {c.label}
            </span>
            <span className="text-fg-subtle shrink-0">
              {stageLabels[c.from_stage]} → {stageLabels[c.to_stage]}
            </span>
            <span className="text-fg-muted min-w-0">{c.driver}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function SectorRow({
  sector: s,
  expanded,
  onToggle,
}: {
  sector: SectorPosture;
  expanded: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <tr
        className="border-b border-border-subtle/60 hover:bg-surface-2/40 cursor-pointer"
        onClick={onToggle}
        aria-expanded={expanded}
      >
        <td className="px-3 py-2 align-top">
          <div className="flex items-center gap-1.5 font-semibold text-fg">
            {expanded ? (
              <ChevronDown className="h-3.5 w-3.5 shrink-0 text-fg-subtle" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5 shrink-0 text-fg-subtle" />
            )}
            {s.label}
            {s.systemic_hit && <SystemicBadge />}
          </div>
          <div className="mt-0.5 text-[11px] text-fg-subtle leading-snug line-clamp-2 pl-5">
            {s.headline}
          </div>
          {s.threat_behavior.length > 0 && (
            <div className="mt-1 pl-5 flex items-center gap-1.5 flex-wrap">
              <span className="text-[10px] text-fg-subtle shrink-0">世界:</span>
              <IntentBadge intent={s.threat_behavior[0].intent} />
              <span className="text-[10px] text-fg-muted">
                {s.threat_behavior[0].actor}
                <NationTag nation={s.threat_behavior[0].nation} />
                {s.threat_behavior.length > 1 && (
                  <span className="text-fg-subtle"> +{s.threat_behavior.length - 1}</span>
                )}
              </span>
            </div>
          )}
        </td>
        {MATRIX_COLUMNS.map((c) => (
          <td key={c} className="px-2 py-2 text-center align-middle hidden md:table-cell">
            <StageCell count={s.counts[c]} stage={c} />
          </td>
        ))}
        <td className="px-2 py-2 align-middle">
          <StageBadge stage={s.stage} />
        </td>
        <td className="px-2 py-2 text-center align-middle">
          <DeltaBadge delta={s.delta} />
        </td>
        <td className="px-2 py-2 text-center align-middle hidden lg:table-cell">
          <CoverageCell total={s.designated_total} observed={s.designated_observed} />
        </td>
      </tr>
      {expanded && (
        <tr className="border-b border-border-subtle/60">
          <td colSpan={MATRIX_COLUMNS.length + 4} className="px-4 pb-3 pt-1 bg-surface-2/30">
            <SectorDetail sector={s} />
          </td>
        </tr>
      )}
    </>
  );
}

function StageCell({ count, stage }: { count: number; stage: ActiveStageId }) {
  if (count === 0) return <span className="text-fg-subtle/50">·</span>;
  const color = STAGE_STYLE[stage];
  return (
    <span
      className="inline-block min-w-7 px-1.5 py-0.5 rounded text-xs font-bold"
      style={{ color, backgroundColor: `${color}22` }}
    >
      {count}
    </span>
  );
}

// 暗域の定量化: 指定 M 事業者中、観測 K 者。観測ありは強調、無音 (0) は暗さを可視化。
function CoverageCell({ total, observed }: { total: number; observed: number }) {
  if (total === 0) return <span className="text-fg-subtle/50">—</span>;
  return (
    <span className="text-[11px] tabular-nums" title={`指定 ${total} 事業者中 ${observed} 者に観測`}>
      <span className={observed > 0 ? "text-fg font-semibold" : "text-fg-subtle/60"}>
        {observed}
      </span>
      <span className="text-fg-subtle"> / {total}</span>
    </span>
  );
}

// 詳細は近接度の高い順に表示 (最も近い脅威から読む)
const DETAIL_ORDER: ActiveStageId[] = [
  "ot_approach",
  "intrusion_it",
  "targeting",
  "indication",
];

function SectorDetail({ sector: s }: { sector: SectorPosture }) {
  const empty = DETAIL_ORDER.every((st) => s.stages[st].length === 0);
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3 flex-wrap text-[11px] text-fg-subtle">
        {s.designated_total > 0 && (
          <span>
            指定 {s.designated_total} 事業者中{" "}
            <span className={s.designated_observed > 0 ? "text-fg font-semibold" : "text-warning"}>
              {s.designated_observed} 者に観測
            </span>
            {s.designated_observed === 0 && " — 無音 (静か≠安全: 開示が少ない)"}
          </span>
        )}
        {s.counts.nonthreat > 0 && (
          <span>非敵対の事務事故 {s.counts.nonthreat}件 (脅威計上外)</span>
        )}
      </div>
      <SystemDomainMix mix={s.system_domain_mix} />

      {s.threat_behavior.length > 0 && <BehaviorSection sector={s} />}

      {empty && s.threat_behavior.length === 0 ? (
        <p className="m-0 text-xs text-fg-subtle">
          この分野の脅威シグナルは現在なし。開示の少なさによる可能性があり安全とは限らない。
        </p>
      ) : (
        DETAIL_ORDER.map((st) =>
          s.stages[st].length > 0 ? (
            <StageSection
              key={st}
              stage={st}
              items={s.stages[st]}
              total={s.counts[st]}
            />
          ) : null,
        )
      )}
      {s.counts.peripheral > 0 && <PeripheralSection sector={s} />}
    </div>
  );
}

function PeripheralSection({ sector: s }: { sector: SectorPosture }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-t border-border-subtle/60 pt-2">
      <button
        onClick={() => setOpen(!open)}
        className="inline-flex items-center gap-1 text-[11px] text-fg-subtle hover:text-fg"
        aria-expanded={open}
      >
        {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
        周辺観測 {s.counts.peripheral}件 — 名簿・事業者名のパターンに合致しない関連報道 (段階に影響しない)
      </button>
      {open && (
        <ul className="m-0 mt-1.5 p-0 list-none space-y-1.5 opacity-75">
          {s.peripheral.map((it) => (
            <ItemRow key={it.id} item={it} />
          ))}
        </ul>
      )}
    </div>
  );
}

// 世界の国家アクター行動 (victim 非依存の先行指標)。JP 観測とは別レンズ。
function BehaviorSection({ sector: s }: { sector: SectorPosture }) {
  // 示唆レンズ — 枠/見出しを警報色にしない (認識論的視覚階層)。
  return (
    <div className="border border-border-default bg-surface-2 rounded p-2 space-y-1.5">
      <div className="text-[11px] font-semibold text-fg-muted">
        世界の国家アクター行動 — この分野への先行指標 (日本の被害の名指しなし・日本への含意は示唆)
      </div>
      <ul className="m-0 p-0 list-none space-y-1">
        {s.threat_behavior.map((b) => (
          <BehaviorRow key={b.id} b={b} />
        ))}
      </ul>
    </div>
  );
}

function BehaviorRow({ b }: { b: ThreatBehavior }) {
  return (
    <li className="text-xs text-fg flex items-center gap-1.5 flex-wrap">
      <IntentBadge intent={b.intent} />
      <span className="font-semibold">{b.actor}</span>
      <NationTag nation={b.nation} />
      <span className="text-[10px] text-fg-subtle">{b.articles}報道</span>
      {b.ics && (
        <span
          className="text-[10px] px-1 rounded bg-warning-soft text-warning"
          title="制御システム(ICS)への攻撃手口"
        >
          ICS-TTP
        </span>
      )}
      {b.spike && <span className="text-[10px] text-critical font-semibold">急増</span>}
      {b.is_new && <span className="text-[10px] text-accent font-semibold">新規</span>}
      {b.jp_targeted && (
        <span className="text-[10px] px-1 rounded bg-critical/15 text-critical font-semibold">
          日本標的
        </span>
      )}
    </li>
  );
}

function StageSection({
  stage,
  items,
  total,
}: {
  stage: ActiveStageId;
  items: BoardItem[];
  total: number;
}) {
  const color = STAGE_STYLE[stage];
  return (
    <div>
      <div className="text-[11px] font-semibold mb-1" style={{ color }}>
        {vocabLabel("jpci_stage", stage)}
        {total > items.length && (
          <span className="text-fg-subtle font-normal">
            {" "}
            (上位{items.length}/{total})
          </span>
        )}
      </div>
      <ul className="m-0 p-0 list-none space-y-1.5">
        {items.map((it) => (
          <ItemRow key={it.id} item={it} />
        ))}
      </ul>
    </div>
  );
}

function ItemRow({ item }: { item: BoardItem }) {
  return (
    <li className="text-xs text-fg leading-snug">
      <div className="flex items-start gap-1.5 flex-wrap">
        <StatusBadge status={item.status} />
        {item.systemic && <SystemicBadge />}
        <OperatorTierBadge tier={item.operator_tier} />
        <SystemDomainBadge domain={item.system_domain} />
        <PrecisionBadge level={item.precision} />
        {item.kev && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-critical/15 text-critical font-semibold">
            KEV
          </span>
        )}
        <ConfidencePill confidence={item.confidence} />
        {(item.report_count ?? 0) > 1 && (
          <span className="text-[10px] px-1.5 py-0.5 rounded bg-surface-2 text-fg-subtle">
            {item.report_count}報道
          </span>
        )}
      </div>
      <div className="mt-0.5">
        {item.url ? (
          <a
            href={item.url}
            target="_blank"
            rel="noreferrer"
            className="text-fg hover:text-accent inline-flex items-center gap-1"
          >
            {item.title}
            <ExternalLink className="h-3 w-3 shrink-0" />
          </a>
        ) : (
          <span>{item.title}</span>
        )}
      </div>
      <div className="mt-0.5 text-[11px] text-fg-subtle flex flex-wrap gap-x-2">
        {item.operator && <span>事業者: {item.operator}</span>}
        {item.cves && item.cves.length > 0 && <span>{item.cves.join(", ")}</span>}
        {item.event_date && <span>発生: {item.event_date}</span>}
      </div>
    </li>
  );
}
