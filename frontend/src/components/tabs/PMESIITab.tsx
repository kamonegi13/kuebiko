// 国家中心 情勢ボード (National Situation Board)。旧 PMESII-PT 8軸カード(飽和で低価値)を
// 作り変え。サイバー↔地政学を **国家** で相関する: 背骨=国家 / ドリル=アクター。
//   - サイバー攻撃者面 = その国が主体のサイバー攻勢 (国家機関・APT 群を問わず、主題帰属)。敵対国で厚い。
//   - サイバー標的面   = その国が受けている脅威 (victim_country)。自国/同盟で厚い。攻撃元+分野付き。
//   - 地政学面         = その国が当事者の地政学事象 (involved_country ブリッジ)。
// 「攻撃者」と「標的」の両レンズで国際力学の両面を見る。intent(動機)で色付け、PMESII を足場に。

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../../api/client";
import { useFilters } from "../../state/filters";
import type { NationOption, SituationFace } from "../../api/types";
import type { TrendResponse } from "../../api/geo";
import { TrendChart } from "../geo/TrendChart";
import { intentHex, intentLabel } from "../../utils/diamond";
import { formatJstDate } from "../../utils/date";
import { goToActor } from "../../utils/intelNav";
import { vocabLabel } from "../../hooks/useVocab";

// セレクタの役割グループ (敵対 → 同盟 → その他 の順で前面化)。ラベルは backend vocab
// "pmesii_role" (vocabLabel) を SSoT に解決する (静的 value→label マップは持たない)。
const ROLE_GROUPS: { role: NationOption["role"] }[] = [
  { role: "home" },
  { role: "adversary" },
  { role: "allied" },
  { role: "other" },
];
const OTHER_CAP = 14; // その他は長尾になるので既定で打ち切り、「+N」で展開

export function PMESIITab() {
  const f = useFilters();
  const { data: nationsData } = useQuery({
    queryKey: ["situation-nations", f.time],
    queryFn: () => api.situationNations(f.time),
  });
  const { data } = useQuery({
    queryKey: ["situation", f.nation, f.time],
    queryFn: () => api.situation(f.nation, f.time),
  });

  const nations = nationsData?.nations ?? [];
  const selected = f.nation || data?.nation || "";

  // 時間軸トグル: 報道時刻 (collection が今出している) ↔ 発生時刻 (実際に起きた、dated のみ)。
  const [tempoBasis, setTempoBasis] = useState<"report" | "event">("report");
  const isEvent = tempoBasis === "event";

  const tempo: TrendResponse | null = data
    ? {
        group_by: "country",
        buckets: data.tempo.buckets,
        series: [
          {
            key: "cyber",
            label: "サイバー(攻撃者)",
            counts: isEvent ? data.tempo.cyber_event : data.tempo.cyber,
          },
          {
            key: "geopol",
            label: "地政学(当事国)",
            counts: isEvent ? data.tempo.geopol_event : data.tempo.geopol,
          },
        ],
      }
    : null;

  // アクタードリル: 脅威アクタービュー (/app/intel/threats) へ遷移し当該 actor を選択。
  const drillToActor = (id: string) => goToActor(id);

  return (
    <div className="space-y-3.5">
      <div className="flex items-baseline justify-between gap-2 flex-wrap px-1">
        <h3 className="m-0 text-lg font-bold text-fg tracking-tight">国家情勢ボード</h3>
        <span className="text-fg-muted text-xs">
          攻撃者(帰属) / 標的(被害) / 地政学(当事国) を国家で相関 · 過去 {f.time}日
        </span>
      </div>

      {/* 国家セレクタ: 役割 (敵対/同盟/その他) でグループ化し折り返し表示。
          横スクロールを解消し、防衛 CTI で最も使う敵対・同盟を最前面に。 */}
      <NationSelector
        nations={nations}
        selected={selected}
        onSelect={(iso) => f.setFilter("nation", iso)}
      />

      {!data ? (
        <div className="text-fg-muted text-sm py-12 text-center">読み込み中...</div>
      ) : (
        <>
          {/* テンポ: 日次 cyber vs geopol。報道時刻↔発生時刻トグル + カバレッジ正直化。 */}
          {tempo && (
            <div className="bg-surface-1 border border-border-subtle rounded-lg px-3 py-2">
              <div className="flex items-center justify-between gap-2 mb-1 flex-wrap">
                <div className="text-[11px] font-medium text-fg-muted">
                  テンポ — {data.label} の日次活動（{isEvent ? "発生時刻" : "報道時刻"}）
                </div>
                <div className="inline-flex rounded border border-border-subtle overflow-hidden text-[10.5px]">
                  <button
                    onClick={() => setTempoBasis("report")}
                    className={`px-2 py-0.5 transition-colors ${!isEvent ? "bg-accent-soft text-accent-hover" : "text-fg-subtle hover:text-fg"}`}
                    title="記事が報道/取得された時刻 (現況認識の正、全件)"
                  >
                    報道時刻
                  </button>
                  <button
                    onClick={() => setTempoBasis("event")}
                    className={`px-2 py-0.5 transition-colors ${isEvent ? "bg-accent-soft text-accent-hover" : "text-fg-subtle hover:text-fg"}`}
                    title="事象が実際に発生した時刻 (発生日が判明した記事のみ・過去事象に偏る)"
                  >
                    発生時刻
                  </button>
                </div>
              </div>
              <TrendChart data={tempo} mode="line" />
              {/* 発生時刻は dated subset のみ=未抽出を「無し」と誤読させない (暗域=不明≠安全)。 */}
              {isEvent && data.tempo.coverage && (
                <div className="mt-1 text-[10px] leading-snug text-fg-subtle">
                  発生日付き {data.tempo.coverage.dated}/{data.tempo.coverage.total} 件（期間内{" "}
                  {data.tempo.coverage.event_in_window}）。未抽出（速報の大半）は非表示＝
                  <span className="text-warning">不明（≠無し）</span>。
                  {Number(f.time) <= 7 && " 短い期間では発生時刻はほぼ空（速報は日付未記載）。"}
                </div>
              )}
            </div>
          )}

          {/* 凡例: 攻撃者(accent) / 標的(cyan) / 地政学(warning) */}
          <div className="flex items-center gap-3 px-1 text-[10.5px] text-fg-subtle">
            <span className="inline-flex items-center gap-1">
              <span className="w-2 h-2 rounded-full bg-accent" />攻撃者=その国が主体のサイバー攻勢 (帰属済み・国家機関含む)
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="w-2 h-2 rounded-full bg-violet-400" />言及=帰属なしサイバー言及 (政策/態勢中心)
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="w-2 h-2 rounded-full bg-cyan-400" />標的=その国が受けている脅威
            </span>
            <span className="inline-flex items-center gap-1">
              <span className="w-2 h-2 rounded-full bg-warning" />地政学=当事者の事象
            </span>
          </div>

          {/* 四面: サイバー攻撃者(帰属) | サイバー言及(帰属なし) | サイバー標的 | 地政学 */}
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-3">
            <FacePanel
              title="サイバー攻撃者面"
              tone="accent"
              face={data.cyber}
              actorsLabel="攻撃主体"
              emptyHint={
                (data.cyber?.tracked_actor_count ?? 0) > 0
                  ? "この期間、この国のアクター (国家機関・APT 群) が主体と帰属された攻勢記事なし"
                  : "この国に帰属する追跡対象アクターが辞書に無い"
              }
              onActor={drillToActor}
            />
            <FacePanel
              title="サイバー言及面 (帰属なし)"
              tone="muted"
              face={data.cyber_mention}
              note="情報(サイバー)分野×当事国×攻撃主体の名指し無し。地政学/政策記事に埋もれた言及 (多くは政策・態勢、一部 実事象)"
              emptyHint="この期間に攻撃主体の名指し無しのサイバー言及なし"
            />
            <FacePanel
              title="サイバー標的面 (被害)"
              tone="info"
              face={data.cyber_target}
              actorsLabel="攻撃元"
              emptyHint="この期間に観測された被害事案なし"
              onActor={drillToActor}
            />
            <FacePanel
              title="地政学面 (当事国)"
              tone="warning"
              face={data.geopolitical}
              emptyHint="この期間の地政学事象なし"
            />
          </div>
        </>
      )}
    </div>
  );
}

const TONE_CLASS: Record<"accent" | "info" | "warning" | "muted", string> = {
  accent: "text-accent",
  info: "text-cyan-400",
  warning: "text-warning",
  muted: "text-violet-400",
};

// 役割グループ化された国家セレクタ (横スクロール解消)。
function NationSelector({
  nations,
  selected,
  onSelect,
}: {
  nations: NationOption[];
  selected: string;
  onSelect: (iso: string) => void;
}) {
  const [showAllOther, setShowAllOther] = useState(false);
  return (
    <div className="space-y-1.5">
      {ROLE_GROUPS.map((g) => {
        const items = nations.filter((n) => n.role === g.role);
        if (!items.length) return null;
        const capped = g.role === "other" && !showAllOther;
        const shown = capped ? items.slice(0, OTHER_CAP) : items;
        const hidden = items.length - shown.length;
        return (
          <div key={g.role} className="flex items-start gap-2">
            <span className="shrink-0 w-9 pt-1.5 text-[11px] font-medium text-fg-subtle">{vocabLabel("pmesii_role", g.role)}</span>
            <div className="flex flex-wrap gap-1.5">
              {shown.map((n) => (
                <NationChip key={n.iso} n={n} selected={selected === n.iso} onClick={() => onSelect(n.iso)} />
              ))}
              {g.role === "other" && hidden > 0 && (
                <button
                  onClick={() => setShowAllOther(true)}
                  className="rounded border border-dashed border-border-subtle px-2 py-1 text-xs text-fg-subtle hover:text-fg transition-colors"
                >
                  +{hidden} 全て
                </button>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

function NationChip({
  n,
  selected,
  onClick,
}: {
  n: NationOption;
  selected: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1 rounded border px-2 py-1 text-xs transition-colors ${
        selected ? "border-accent text-fg bg-accent/10" : "border-border-subtle text-fg-muted hover:text-fg"
      }`}
      title={`攻撃者 ${n.cyber} / 標的(被害) ${n.cyber_target} / 地政学 ${n.geopol}`}
    >
      <span className="font-medium text-fg">{n.label}</span>
      <span className="text-accent tnum">{n.cyber}</span>
      <span className="text-fg-subtle">/</span>
      <span className="text-cyan-400 tnum">{n.cyber_target}</span>
      <span className="text-fg-subtle">/</span>
      <span className="text-warning tnum">{n.geopol}</span>
    </button>
  );
}

function FacePanel({
  title,
  tone,
  face,
  emptyHint,
  note,
  actorsLabel = "攻撃主体",
  onActor,
}: {
  title: string;
  tone: "accent" | "info" | "warning" | "muted";
  face: SituationFace | null;
  emptyHint: string;
  note?: string;
  actorsLabel?: string;
  onActor?: (actorId: string) => void;
}) {
  const total = face?.total ?? 0;
  const accent = TONE_CLASS[tone];
  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-3.5 space-y-2.5">
      <div className="flex items-baseline justify-between">
        <span className="text-sm font-bold text-fg">{title}</span>
        <span className={`tnum text-base font-bold ${accent}`}>
          {total}
          <span className="text-[10px] text-fg-subtle ml-1 font-normal">件</span>
        </span>
      </div>
      {note && <div className="text-[10px] leading-snug text-fg-subtle -mt-1">{note}</div>}

      {total === 0 ? (
        <div className="text-xs text-fg-subtle italic py-3">{emptyHint}</div>
      ) : (
        <>
          {/* アクター (攻撃者面=その国の攻撃主体 / 標的面=攻撃元) — click でアクター詳細へドリル */}
          {face?.actors && face.actors.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] text-fg-subtle uppercase tracking-wider">{actorsLabel}</span>
              {face.actors.map((a) => (
                <button
                  key={a.actor_id}
                  onClick={() => onActor?.(a.actor_id)}
                  className="inline-flex items-center gap-1 rounded bg-accent-soft text-accent-hover border border-accent/30 px-1.5 py-0.5 text-xs hover:bg-accent/20"
                  title="アクター詳細へ"
                >
                  {a.label} <span className="text-fg-subtle tnum">{a.count}</span>
                </button>
              ))}
            </div>
          )}

          {/* 標的セクター (標的面のみ: どの分野が狙われたか) */}
          {face?.sectors && face.sectors.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] text-fg-subtle uppercase tracking-wider">分野</span>
              {face.sectors.map((s) => (
                <span
                  key={s.sector}
                  className="inline-flex items-center gap-1 rounded bg-surface-2 border border-cyan-400/30 px-1.5 py-0.5 text-[11px] text-fg-muted"
                >
                  {s.label} <span className="text-fg-subtle tnum">{s.count}</span>
                </span>
              ))}
            </div>
          )}

          {/* 動機 (intent) */}
          {face && face.intents.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] text-fg-subtle uppercase tracking-wider">動機</span>
              {face.intents.map((i) => (
                <span
                  key={i.intent}
                  className="inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px]"
                  style={{ color: intentHex(i.intent), border: `1px solid ${intentHex(i.intent)}55` }}
                >
                  {intentLabel(i.intent)} <span className="text-fg-subtle tnum">{i.count}</span>
                </span>
              ))}
            </div>
          )}

          {/* PMESII ドメイン (足場=内訳) */}
          {face && face.domains.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] text-fg-subtle uppercase tracking-wider">領域</span>
              {face.domains.map((d) => (
                <span
                  key={d.axis}
                  className="inline-flex items-center gap-1 rounded bg-surface-2 border border-border-subtle px-1.5 py-0.5 text-[11px] text-fg-muted"
                >
                  {d.label} <span className="text-fg-subtle tnum">{d.count}</span>
                </span>
              ))}
            </div>
          )}

          {/* 直近 */}
          {face && face.recent.length > 0 && (
            <ul className="space-y-1 pt-1 border-t border-border-subtle">
              {face.recent.map((r) => (
                <li key={r.article_id} className="flex items-start gap-1.5 text-xs">
                  <span
                    className={`mt-1.5 w-1 h-1 rounded-full shrink-0 ${
                      r.importance === "high"
                        ? "bg-critical"
                        : r.importance === "medium"
                          ? "bg-warning"
                          : "bg-fg-subtle"
                    }`}
                  />
                  <div className="min-w-0 flex-1">
                    <a
                      href={`/app/article/${encodeURIComponent(r.article_id)}`}
                      className="text-fg hover:text-accent line-clamp-2"
                    >
                      {r.title}
                    </a>
                    <span className="text-[10px] text-fg-subtle ml-0.5">
                      {r.intent && (
                        <span style={{ color: intentHex(r.intent) }}>{intentLabel(r.intent)} · </span>
                      )}
                      {r.date && formatJstDate(r.date)}
                    </span>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}
