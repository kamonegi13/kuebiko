// W1 (通知再設計): 日次ブリーフ (朝刊/夕刊) の Web 通読サーフェス。
// 2026-07-12: Discord は要点射影のみ、Web が全文の正。payload (構造化 JSON) があれば
// 構造描画 (節ラベル / 変化マーカー / tradecraft / PIR 記事リンク)、旧行は markdown fallback。
// 2026-07-25: 旧 /app/retrospect (週次振り返り) を粒度切替で統合 — 日次 (配信物) と
// 週次 (過去参照の再構成) を同一サーフェスの時間軸ファミリーとして扱う。

import { useState } from "react";
import { Moon, Sun } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api/client";
import { pageContainer, PageHeader } from "../components/Page";
import { MarkdownText } from "../components/MarkdownText";
import { formatJst } from "../utils/date";
import { vocabLabel } from "../hooks/useVocab";
import { WeeklyRetrospectView } from "./brief/WeeklyRetrospectView";
import type {
  BriefContextResponse,
  BriefPirSection,
  BriefTradecraft,
  DailyBrief,
  DailyBriefPayload,
  DailyBriefDetailResponse,
  DailyBriefMeta,
  DailyBriefMetasResponse,
} from "../api/types";

type SlotMeta = { label: string; Icon: typeof Sun };

function slotMeta(slot: string): SlotMeta {
  if (slot === "evening") return { label: "夕刊", Icon: Moon };
  if (slot === "morning") return { label: "朝刊", Icon: Sun };
  return { label: "—", Icon: Sun };
}

// 節テキスト内の変化マーカー (【強化】【拡大】【新規】…) を bullet に分解する (表示のみ)。
// マーカーが無い節はそのまま段落 1 つで描画する。
function MarkedText({ text }: { text: string }) {
  const parts = text
    .split(/(?=【)/)
    .map((s) => s.trim())
    .filter(Boolean);
  if (parts.length <= 1) {
    return <p className="text-sm text-fg leading-relaxed m-0">{text}</p>;
  }
  return (
    <ul className="space-y-1.5 m-0 p-0 list-none">
      {parts.map((p, i) => {
        const m = p.match(/^【([^】]+)】\s*([\s\S]*)$/);
        return (
          <li key={i} className="text-sm text-fg leading-relaxed flex items-start gap-2">
            {m ? (
              <>
                <span className="shrink-0 mt-0.5 text-[10px] px-1.5 py-0.5 rounded border border-border-default text-fg-muted whitespace-nowrap">
                  {m[1]}
                </span>
                <span className="min-w-0">{m[2]}</span>
              </>
            ) : (
              <span className="min-w-0">{p}</span>
            )}
          </li>
        );
      })}
    </ul>
  );
}

function ImportanceChip({ importance }: { importance: string }) {
  const tone =
    importance === "high"
      ? "bg-critical-soft text-critical border-critical/30"
      : importance === "medium"
        ? "bg-warning-soft text-warning border-warning/30"
        : "border-border-default text-fg-subtle";
  return (
    <span
      className={`shrink-0 mt-0.5 text-[10px] px-1.5 py-0.5 rounded border whitespace-nowrap ${tone}`}
    >
      {vocabLabel("importance", importance)}
    </span>
  );
}

function TierChip({ tier }: { tier: string }) {
  if (tier === "official") {
    return (
      <span className="shrink-0 mt-0.5 text-[10px] px-1.5 py-0.5 rounded border bg-accent-subtle text-accent border-accent/30 whitespace-nowrap">
        一次
      </span>
    );
  }
  if (tier === "social") {
    return (
      <span className="shrink-0 mt-0.5 text-[10px] px-1.5 py-0.5 rounded border bg-warning-soft text-warning border-warning/30 whitespace-nowrap">
        SNS 要裏取り
      </span>
    );
  }
  return null;
}

function TradecraftCard({ tc }: { tc: BriefTradecraft }) {
  const lists: Array<{ key: keyof BriefTradecraft; label: string }> = [
    { key: "alternatives", label: "対立仮説" },
    { key: "key_assumptions", label: "前提" },
    { key: "indicators", label: "監視指標" },
  ];
  return (
    <div className="border border-border-subtle rounded-md p-3 space-y-2 bg-surface-2/50">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-fg-muted m-0">
        分析トレードクラフト (別解・前提・監視指標)
      </h4>
      {tc.leading_assessment && (
        <p className="text-sm text-fg leading-relaxed m-0">
          <span className="font-semibold">主見立て: </span>
          {tc.leading_assessment}
        </p>
      )}
      {lists.map(({ key, label }) => {
        const items = tc[key];
        if (!Array.isArray(items) || items.length === 0) return null;
        return (
          <div key={key} className="space-y-1">
            <div className="text-xs font-semibold text-fg-muted">{label}</div>
            <ul className="space-y-1 m-0 p-0 list-none">
              {items.map((it, i) => (
                <li key={i} className="text-sm text-fg-muted leading-relaxed pl-3 border-l-2 border-border-subtle">
                  {it}
                </li>
              ))}
            </ul>
          </div>
        );
      })}
    </div>
  );
}

function PirCard({ sec }: { sec: BriefPirSection }) {
  return (
    <div className="border border-border-subtle rounded-md p-3 space-y-1.5 min-w-0">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-sm font-semibold text-fg">{sec.title}</span>
        <span className="text-[10px] px-1.5 py-0.5 rounded border border-border-default text-fg-subtle whitespace-nowrap">
          {sec.total} 件
        </span>
      </div>
      {sec.summary && (
        <p className="text-sm text-fg-muted leading-relaxed m-0">{sec.summary}</p>
      )}
      {sec.matches.length > 0 && (
        <ul className="space-y-1 m-0 p-0 list-none">
          {sec.matches.map((m, i) => (
            <li key={`${i}-${m.url}`} className="flex items-start gap-2 min-w-0">
              <ImportanceChip importance={m.importance} />
              <a
                href={m.url}
                target="_blank"
                rel="noopener noreferrer"
                className="text-sm text-accent hover:underline min-w-0 break-words"
              >
                {m.title}
              </a>
              <TierChip tier={m.tier} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function StructuredBrief({ payload }: { payload: DailyBriefPayload }) {
  const syn = payload.synthesis;
  return (
    <div className="space-y-4">
      {syn && (
        <div className="space-y-3">
          {syn.headline && (
            <p className="text-base font-semibold text-fg leading-relaxed m-0">{syn.headline}</p>
          )}
          {syn.sections.map((s) => (
            <section key={s.key} className="space-y-1.5">
              <h4 className="text-xs font-semibold uppercase tracking-wide text-fg-muted m-0">
                {s.label}
              </h4>
              <MarkedText text={s.text} />
            </section>
          ))}
          {syn.tradecraft && <TradecraftCard tc={syn.tradecraft} />}
        </div>
      )}
      {payload.pir.length > 0 && (
        <div className="space-y-2">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-fg-muted m-0 border-t border-border-subtle pt-3">
            PIR Daily Focus — {payload.pir.length} 領域 (24h)
          </h4>
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-2">
            {payload.pir.map((p, i) => (
              <PirCard key={`${i}-${p.title}`} sec={p} />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

export function DailyBriefPage() {
  // 粒度切替 (旧 /app/retrospect の統合)。deep link = /app/daily-brief#weekly
  const initialView: "daily" | "weekly" =
    typeof window !== "undefined" && window.location.hash === "#weekly" ? "weekly" : "daily";
  const [view, setView] = useState<"daily" | "weekly">(initialView);
  const switchView = (next: "daily" | "weekly") => {
    setView(next);
    if (typeof window !== "undefined") {
      history.replaceState(null, "", next === "daily" ? "/app/daily-brief" : "/app/daily-brief#weekly");
    }
  };

  // 一覧はメタのみ (~40KB)。本文は選択時に 1 件オンデマンド取得 (60 件全文 ~2MB の
  // over-fetch がトンネル越し表示を数秒〜十数秒にしていた、2026-07-31 根治)。
  const { data, isFetching } = useQuery<DailyBriefMetasResponse>({
    queryKey: ["daily-brief-metas"],
    queryFn: () => api.dailyBriefMetas(60),
    enabled: view === "daily",
  });
  const briefs = data?.briefs ?? [];
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const selectedMeta: DailyBriefMeta | undefined =
    briefs.find((b) => b.id === selectedId) ?? briefs[0];
  const { data: detail, isFetching: isFetchingDetail } = useQuery<DailyBriefDetailResponse>({
    queryKey: ["daily-brief", selectedMeta?.id ?? 0],
    queryFn: () => api.dailyBrief(selectedMeta?.id ?? 0),
    enabled: view === "daily" && selectedMeta != null,
  });
  const selected: DailyBrief | undefined = detail?.brief;
  // P2/P3: 選択中ブリーフの補足コンテキスト (24h 活動アクター + 今週の予測)。
  // 配信物 (brief 本文) には焼き込まず閲覧時に計算する (週次振り返りと同方式)。
  const { data: ctx } = useQuery<BriefContextResponse>({
    queryKey: ["brief-context", selectedMeta?.generated_at ?? ""],
    queryFn: () => api.briefContext(selectedMeta?.generated_at ?? ""),
    enabled: view === "daily" && !!selectedMeta,
  });
  const hasPayload =
    !!selected?.payload &&
    (!!selected.payload.synthesis || selected.payload.pir.length > 0);

  return (
    <div className={`${pageContainer("wide")} space-y-5`}>
      <PageHeader
        title="ブリーフ・振り返り"
        subtitle="毎日 06:30 の朝刊 (状況総括 + PIR 24h focus) と 19:30 の夕刊 (状況更新)。Discord には要点のみを配信し、全文はここで読む。週次は「あの週、何が起きていたか」の過去参照。"
      />

      <div className="flex border-b border-border-subtle">
        <button onClick={() => switchView("daily")} className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${view === "daily" ? "text-accent-hover border-accent font-semibold" : "text-fg-muted hover:text-fg border-transparent"}`}>
          日次 (朝刊 / 夕刊)
        </button>
        <button onClick={() => switchView("weekly")} className={`px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors ${view === "weekly" ? "text-accent-hover border-accent font-semibold" : "text-fg-muted hover:text-fg border-transparent"}`}>
          週次振り返り
        </button>
      </div>

      {view === "weekly" && <WeeklyRetrospectView />}

      {view === "daily" && isFetching && briefs.length === 0 && (
        <div className="text-fg-subtle text-sm">読み込み中…</div>
      )}

      {view === "daily" && !isFetching && briefs.length === 0 && (
        <div className="text-fg-subtle text-sm p-10 text-center bg-surface-1 border border-border-subtle rounded-lg">
          まだ日次ブリーフがありません。次回の朝刊 (06:30) / 夕刊 (19:30) から表示されます。
        </div>
      )}

      {view === "daily" && briefs.length > 0 && (
        <div className="space-y-5">
          {/* 時間ナビ (週次と同じ作法: ← 前 / 期間ラベル / 次 →) */}
          <div className="flex flex-wrap items-center justify-end gap-2">
            <button
              onClick={() => {
                const i = briefs.findIndex((b) => b.id === selectedMeta?.id);
                if (i >= 0 && i + 1 < briefs.length) setSelectedId(briefs[i + 1].id);
              }}
              disabled={briefs.findIndex((b) => b.id === selectedMeta?.id) >= briefs.length - 1}
              className="px-2 py-1 rounded border border-border-default text-fg-muted hover:text-fg text-sm disabled:opacity-40"
            >
              ← 前
            </button>
            <span className="text-sm text-fg font-medium tnum min-w-[160px] text-center">
              {selectedMeta ? `${selectedMeta.period_label} ${slotMeta(selectedMeta.slot).label}` : "—"}
            </span>
            <button
              onClick={() => {
                const i = briefs.findIndex((b) => b.id === selectedMeta?.id);
                if (i > 0) setSelectedId(briefs[i - 1].id);
              }}
              disabled={briefs.findIndex((b) => b.id === selectedMeta?.id) <= 0}
              className="px-2 py-1 rounded border border-border-default text-fg-muted hover:text-fg text-sm disabled:opacity-40"
            >
              次 →
            </button>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-[minmax(0,1fr)_360px] gap-5 items-start">
          {/* 左: 選択中ブリーフ本文 (payload=構造描画 / 旧行=markdown fallback) */}
          {isFetchingDetail && !selected && (
            <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 text-fg-subtle text-sm">
              本文を読み込み中…
            </div>
          )}
          {selected && (
            <article className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-3 min-w-0">
              <div className="flex flex-wrap items-center gap-2 border-b border-border-subtle pb-2">
                <h3 className="text-base font-bold text-fg m-0">{selected.title}</h3>
                <span className="text-xs text-fg-subtle ml-auto tnum">
                  {formatJst(selected.generated_at)}
                </span>
              </div>
              {hasPayload && selected.payload ? (
                <StructuredBrief payload={selected.payload} />
              ) : (
                <>
                  {selected.bluf && (
                    <p className="text-sm text-fg-muted font-medium leading-relaxed">
                      {selected.bluf}
                    </p>
                  )}
                  <MarkdownText>{selected.summary}</MarkdownText>
                </>
              )}
              {selected.sources.length > 0 && !hasPayload && (
                <div className="border-t border-border-subtle pt-2">
                  <div className="text-fg-muted text-xs font-semibold mb-1">主要ソース</div>
                  <ul className="space-y-1">
                    {selected.sources.map((s, i) => (
                      <li key={`${i}-${s.url}`}>
                        <a
                          href={s.url}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="text-sm text-accent hover:underline break-all"
                        >
                          {s.title || s.url}
                        </a>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </article>
          )}

          {/* 右: コンテキスト (週次振り返りと同じ文法 — 一覧 + 活動アクター + 予測) */}
          <div className="space-y-4">
            <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
              <div className="px-3 py-2 bg-surface-2 text-fg-muted text-xs uppercase border-b border-border-subtle">
                最近のブリーフ {briefs.length} 件
              </div>
              <ul className="divide-y divide-border-subtle max-h-[40vh] overflow-y-auto">
                {briefs.map((b) => {
                  const m = slotMeta(b.slot);
                  const active = selectedMeta?.id === b.id;
                  return (
                    <li key={b.id}>
                      <button
                        onClick={() => setSelectedId(b.id)}
                        className={`w-full text-left px-3 py-2 flex items-center gap-2 hover:bg-surface-2 transition-colors ${active ? "bg-surface-2" : ""}`}
                      >
                        <m.Icon className="h-4 w-4 text-fg-muted shrink-0" />
                        <span className="text-sm text-fg flex-1 truncate tnum">{b.period_label}</span>
                        <span className="text-xs text-fg-subtle shrink-0">{m.label}</span>
                      </button>
                    </li>
                  );
                })}
              </ul>
            </div>

            {/* P2: この 24h の活動アクター (閲覧時計算、配信物には焼き込まない) */}
            <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
              <div className="px-3 py-2 bg-surface-2 text-fg-muted text-xs uppercase border-b border-border-subtle flex items-baseline justify-between gap-2">
                <span>活動したアクター (24h)</span>
                {ctx?.window_label && <span className="normal-case text-fg-subtle tnum">{ctx.window_label}</span>}
              </div>
              {!ctx || ctx.top_actors.length === 0 ? (
                <div className="px-3 py-4 text-fg-subtle text-sm text-center">{ctx ? "記録なし" : "読み込み中…"}</div>
              ) : (
                <div className="p-2 flex flex-wrap gap-1.5">
                  {ctx.top_actors.map((a) => (
                    <a key={a.value} href={`/app/news?pivot_type=actor&pivot_value=${encodeURIComponent(a.value)}`}
                      className="inline-flex items-center gap-1 bg-surface-2 border border-border-default rounded px-2 py-0.5 text-xs text-fg-muted hover:text-accent hover:border-accent-soft transition-colors">
                      <span className="font-mono">{a.value}</span>
                      <span className="text-fg-subtle tnum">{a.count}</span>
                    </a>
                  ))}
                </div>
              )}
            </div>

            {/* P3: 今週の予測 (的中判定は週次 — 未検証は観測中として表示) */}
            <div className="bg-surface-1 border border-border-subtle rounded-lg overflow-hidden">
              <div className="px-3 py-2 bg-surface-2 text-fg-muted text-xs uppercase border-b border-border-subtle">
                今週の予測
              </div>
              {!ctx || ctx.forecast_indicators.length === 0 ? (
                <div className="px-3 py-4 text-fg-subtle text-sm text-center">
                  {ctx ? "予測指標なし" : "読み込み中…"}
                </div>
              ) : (
                <ul className="divide-y divide-border-subtle">
                  {ctx.forecast_indicators.map((o) => (
                    <li key={`${o.scope}-${o.target_value}`} className="flex items-center gap-2 px-3 py-1.5 text-sm">
                      <span className="font-mono text-fg-muted truncate flex-1">{o.target_value}</span>
                      {!o.verified ? (
                        <span className="text-xs text-fg-subtle">観測中</span>
                      ) : o.hit ? (
                        <span className="text-xs text-accent font-semibold">的中</span>
                      ) : (
                        <span className="text-xs text-fg-subtle">外れ</span>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
          </div>
        </div>
      )}
    </div>
  );
}
