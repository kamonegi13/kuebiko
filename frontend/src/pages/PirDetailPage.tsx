// PIR 詳細画面 (/app/pir/{id})。
// - 必須 5 KPI (7d / 30d / last_match / top_feeds / top_actors) + 詳細展開 5 KPI
// - description / strong_signals / routing / spotlight の readonly 表示
// - Edit / Delete / Approve actions

import { useState } from "react";
import { Check, Trash2 } from "lucide-react";
import { pageContainer } from "../components/Page";
import { PirMatchTree } from "../components/PirMatchTree";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { pirApi } from "../api/pir";
import { spotlightApi } from "../api/spotlight";
import { useRuntimeFlags } from "../hooks/useRuntimeFlags";
import { formatJst, formatJstShort } from "../utils/date";
import { useChannelMeta } from "../components/channel";
import { vocabLabel } from "../hooks/useVocab";

// PIR 固有の値 "auto" は共通 SSoT に無い (PIR 以外では使わない値のため)。
function importanceOrAuto(v: string): string {
  return v === "auto" ? "自動" : vocabLabel("importance", v);
}

export function PirDetailPage({ pirId }: { pirId: string }) {
  const qc = useQueryClient();
  const { read_only } = useRuntimeFlags();
  const chMeta = useChannelMeta();
  const [showDetails, setShowDetails] = useState(false);

  const { data: pir, isLoading } = useQuery({
    queryKey: ["pir-detail", pirId],
    queryFn: () => pirApi.get(pirId),
  });
  const { data: kpi } = useQuery({
    queryKey: ["pir-kpi", pirId],
    queryFn: () => pirApi.kpi(pirId),
    enabled: !!pir,
  });

  const approveMut = useMutation({
    mutationFn: () => pirApi.approve(pirId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["pir-detail", pirId] });
      qc.invalidateQueries({ queryKey: ["pir-list"] });
    },
  });
  const deleteMut = useMutation({
    mutationFn: () => pirApi.delete(pirId),
    onSuccess: () => {
      window.location.href = "/app/pir";
    },
  });

  const spotlightEnabled = !!pir?.spotlight.enabled;
  const { data: latestSpotlight } = useQuery({
    queryKey: ["spotlight-detail", pirId],
    queryFn: () => spotlightApi.get(pirId, "weekly"),
    enabled: spotlightEnabled,
    retry: false,
  });
  const regenSpotlight = useMutation({
    mutationFn: (model?: string) => spotlightApi.regenerate(pirId, "weekly", model),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["spotlight-detail", pirId] });
      qc.invalidateQueries({ queryKey: ["spotlight-list"] });
    },
  });

  if (isLoading) return <div className={`${pageContainer("wide")} text-fg-muted`}>読み込み中...</div>;
  if (!pir) return <div className={`${pageContainer("wide")} text-fg-muted`}>PIR が見つかりません: {pirId}</div>;

  return (
    <div className={`${pageContainer("wide")} space-y-4`}>
      {/* Header */}
      <div className="flex items-start justify-between flex-wrap gap-3">
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline gap-2 flex-wrap">
            <a href="/app/pir" className="text-fg-muted text-sm no-underline hover:text-fg">← PIR 一覧</a>
            <span className="text-fg-subtle">/</span>
            <code className="text-[12px] text-fg-subtle font-mono">{pir.id}</code>
          </div>
          <h2 className="m-0 mt-1 text-xl font-bold text-fg tracking-tight">{pir.title}</h2>
          <div className="mt-1.5 flex flex-wrap items-center gap-2 text-xs">
            {pir.enabled ? (
              <span className="bg-success-soft text-success px-2 py-0.5 rounded font-mono uppercase">有効</span>
            ) : (
              <span className="bg-surface-3 text-fg-subtle px-2 py-0.5 rounded font-mono uppercase">無効</span>
            )}
            <span className="text-fg-muted">重要度: <code className="text-fg">{importanceOrAuto(pir.target_importance)}</code></span>
            {pir.spotlight.enabled && (
              <span className="bg-accent-soft text-accent-hover px-2 py-0.5 rounded font-mono uppercase">spotlight: {pir.spotlight.window}</span>
            )}
            {!pir.metadata.approved_by_user && (
              <span className="bg-warning-soft text-warning px-2 py-0.5 rounded font-mono uppercase">下書き</span>
            )}
          </div>
        </div>

        {!read_only && (
          <div className="flex items-center gap-1.5 shrink-0">
            <a
              href={`/app/pir/edit/${encodeURIComponent(pir.id)}`}
              className="bg-accent hover:bg-accent-hover text-fg rounded px-3 py-1.5 text-xs font-semibold no-underline"
            >編集</a>
            {!pir.metadata.approved_by_user && (
              <button
                onClick={() => approveMut.mutate()}
                disabled={approveMut.isPending}
                className="bg-success hover:bg-success-soft text-fg rounded px-3 py-1.5 text-xs font-semibold disabled:opacity-50 inline-flex items-center gap-1"
              ><Check className="h-3.5 w-3.5" /> 承認</button>
            )}
            <button
              onClick={() => {
                if (confirm(`PIR "${pir.id}" を削除しますか？`)) deleteMut.mutate();
              }}
              className="text-critical bg-surface-2 border border-border-subtle hover:bg-critical-soft rounded px-2 py-1.5 text-xs"
            ><Trash2 className="h-3.5 w-3.5" /></button>
          </div>
        )}
      </div>

      {/* 必須 5 KPI */}
      {kpi && (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-2.5">
          <KpiCard label="直近 7 日の該当" value={String(kpi.match_count_7d)} />
          <KpiCard label="直近 30 日の該当" value={String(kpi.match_count_30d)} />
          <KpiCard label="最終該当" value={formatJstShort(kpi.last_match_at)} />
          <KpiCard
            label="主要な情報源"
            value={kpi.top_feeds[0] ? `${kpi.top_feeds[0][0]} (${kpi.top_feeds[0][1]})` : "—"}
            small
          />
          <KpiCard
            label="主要アクター"
            value={kpi.top_actors[0] ? `${kpi.top_actors[0][0]} (${kpi.top_actors[0][1]})` : "—"}
            small
          />
        </div>
      )}

      {/* description */}
      <Section title="説明">
        <p className="m-0 text-fg whitespace-pre-wrap leading-relaxed">{pir.description || "(説明なし)"}</p>
        {pir.metadata.rationale && (
          <div className="mt-3 pt-3 border-t border-border-subtle">
            <div className="text-[10px] uppercase tracking-wider text-fg-subtle mb-1">AI 判断根拠</div>
            <p className="m-0 text-fg-muted text-xs italic">{pir.metadata.rationale}</p>
          </div>
        )}
      </Section>

      {/* 照合条件 (実際の記事照合を駆動、authoring 統一 2026-07-23) */}
      {pir.match && (
        <Section title="照合条件 (この条件で記事が照合されます)">
          <div className="bg-surface-2 border border-border-subtle rounded p-2.5">
            <PirMatchTree node={pir.match} />
          </div>
          {pir.llm_judge.enabled && (
            <div className="mt-2 text-xs text-fg-muted">
              <span className="inline-block px-1.5 py-0.5 rounded bg-accent-soft text-accent-hover font-medium mr-1.5">
                AI 主題判定あり
              </span>
              上の条件は候補の絞り込みで、記事が本当にこの PIR を主題としているかを AI が確定します
              (夜間の自動処理後に反映)。
              {pir.llm_judge.question && (
                <div className="mt-1 text-fg-subtle">判定基準: {pir.llm_judge.question}</div>
              )}
            </div>
          )}
        </Section>
      )}

      {/* Strong signals (6 カテゴリ) */}
      <Section title={pir.match ? "重要シグナル (補助情報 — 脅威アクター連携・旧方式の予備)" : "重要シグナル (絞り込み条件)"}>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2 text-sm">
          <SignalCategory label="検索語彙" items={pir.strong_signals.keywords} />
          <SignalCategory label="攻撃主体" items={pir.strong_signals.actors} />
          <SignalCategory label="対象業界" items={pir.strong_signals.sectors} />
          <SignalCategory label="被害/標的国" items={pir.strong_signals.countries} />
          <SignalCategory label="アクター国籍" items={pir.strong_signals.actor_nations ?? []} />
          <SignalCategory label="信頼情報源" items={pir.strong_signals.feed_titles} className="md:col-span-2" />
        </div>
      </Section>

      {/* Routing + Spotlight */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
        <Section title="配信先の判定">
          <dl className="text-sm space-y-1.5">
            <div className="flex justify-between"><dt className="text-fg-muted">目標重要度</dt><dd className="text-fg font-mono">{importanceOrAuto(pir.target_importance)}</dd></div>
          </dl>
        </Section>
        <Section title="Spotlight">
          <dl className="text-sm space-y-1.5">
            <div className="flex justify-between"><dt className="text-fg-muted">有効</dt><dd className="text-fg font-mono">{pir.spotlight.enabled ? "はい" : "いいえ"}</dd></div>
            <div className="flex justify-between"><dt className="text-fg-muted">タイトル</dt><dd className="text-fg">{pir.spotlight.title || "—"}</dd></div>
            <div className="flex justify-between"><dt className="text-fg-muted">集計期間</dt><dd className="text-fg font-mono">{pir.spotlight.window}</dd></div>
          </dl>
        </Section>
      </div>

      {/* Spotlight 最新生成 + 手動 trigger (enabled な PIR のみ) */}
      {spotlightEnabled && (
        <Section title="Spotlight — 最新の生成結果 / 手動で生成">
          {latestSpotlight ? (
            <div className="space-y-3">
              <div className="bg-accent-subtle border-l-[3px] border-l-accent rounded p-3">
                <div className="text-[10px] text-accent-hover uppercase tracking-wider font-semibold mb-1">見出し</div>
                <div className="text-fg text-sm leading-relaxed">{latestSpotlight.headline}</div>
              </div>
              <div className="flex items-center gap-2 text-[11px] text-fg-subtle flex-wrap">
                <span>生成日時 {formatJst(latestSpotlight.generated_at)}</span>
                <span>·</span>
                <span>記事数 {latestSpotlight.article_count}</span>
                <span>·</span>
                <span>主要イベント {latestSpotlight.key_events.length}</span>
                <span>·</span>
                <code className="bg-surface-2 px-1.5 py-0.5 rounded text-[10px]">{latestSpotlight.llm_model}</code>
                <span>·</span>
                <a href="/app/synthesis" className="text-fg-muted hover:text-accent-hover no-underline">→ Synthesis タブで全文表示</a>
              </div>
            </div>
          ) : (
            <p className="m-0 text-fg-muted text-xs">まだ Spotlight が生成されていません。下のボタンで生成できます。</p>
          )}

          {!read_only && (
            <div className="mt-3 pt-3 border-t border-border-subtle flex items-center gap-2 text-xs flex-wrap">
              <span className="text-fg-muted">{latestSpotlight ? "再生成" : "生成"}:</span>
              <button
                onClick={() => regenSpotlight.mutate(undefined)}
                disabled={regenSpotlight.isPending}
                className="bg-surface-2 border border-border-subtle hover:bg-surface-3 rounded px-2 py-1 text-fg disabled:opacity-50"
              >既定のモデル</button>
              <button
                onClick={() => regenSpotlight.mutate("gemma4:26b")}
                disabled={regenSpotlight.isPending}
                className="bg-surface-2 border border-border-subtle hover:bg-surface-3 rounded px-2 py-1 text-fg disabled:opacity-50"
              >26B</button>
              <button
                onClick={() => regenSpotlight.mutate("gemma4:31b")}
                disabled={regenSpotlight.isPending}
                className="bg-surface-2 border border-border-subtle hover:bg-surface-3 rounded px-2 py-1 text-fg disabled:opacity-50"
              >31B</button>
              {regenSpotlight.isPending && <span className="text-fg-muted">処理中 (~5-10 分)...</span>}
              {regenSpotlight.isError && <span className="text-critical">エラー: {(regenSpotlight.error as Error).message}</span>}
              {regenSpotlight.isSuccess && !regenSpotlight.isPending && <span className="text-success inline-flex items-center gap-1"><Check className="h-3.5 w-3.5" /> 生成完了</span>}
            </div>
          )}
        </Section>
      )}

      {/* (観察モードの項目は 2026-07-23 撤去 — ~2ヶ月で利用 0 件、除外/弱補強は照合条件で表現) */}

      {/* 詳細展開 KPI */}
      {kpi && (
        <Section title={`KPI 詳細 ${showDetails ? "" : "(クリックで展開)"}`}>
          <button
            onClick={() => setShowDetails((s) => !s)}
            className="text-fg-muted hover:text-fg text-sm mb-2"
          >
            {showDetails ? "詳細を隠す" : "詳細を表示"}
          </button>
          {showDetails && (
            <div className="space-y-4">
              <div>
                <h4 className="m-0 mb-2 text-[11px] uppercase tracking-wider text-fg-subtle font-semibold">配信チャンネル別の分布</h4>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(kpi.channel_distribution).map(([ch, n]) => (
                    <span key={ch} className="bg-surface-2 border border-border-subtle rounded px-2 py-1 text-xs">
                      <span className="text-fg-muted">{chMeta(ch).label}</span>: <span className="text-fg tnum">{n}</span>
                    </span>
                  ))}
                  {Object.keys(kpi.channel_distribution).length === 0 && (
                    <span className="text-fg-subtle text-xs">—</span>
                  )}
                </div>
              </div>
              <div>
                <h4 className="m-0 mb-2 text-[11px] uppercase tracking-wider text-fg-subtle font-semibold">最新の該当記事 (上位 10 件)</h4>
                <ul className="m-0 p-0 list-none space-y-1.5">
                  {kpi.samples.map((s) => (
                    <li key={s.article_id} className="text-sm flex items-baseline gap-2">
                      <span className="text-[10px] text-fg-subtle font-mono">{formatJstShort(s.created_at)}</span>
                      <span className="bg-surface-3 text-fg-muted px-1.5 py-0.5 rounded text-[10px] font-mono">{s.posted_channel ? chMeta(s.posted_channel).label : "—"}</span>
                      <a href={s.url} target="_blank" rel="noreferrer" className="text-fg hover:text-accent-hover">{s.title || s.article_id}</a>
                    </li>
                  ))}
                  {kpi.samples.length === 0 && <li className="text-fg-subtle text-xs italic">該当なし</li>}
                </ul>
              </div>
            </div>
          )}
        </Section>
      )}
    </div>
  );
}

function KpiCard({ label, value, small = false }: { label: string; value: string; small?: boolean }) {
  return (
    <div className="bg-surface-1 border border-border-subtle rounded p-3">
      <div className="text-[10px] uppercase tracking-wider text-fg-subtle">{label}</div>
      <div className={`mt-1 text-fg font-semibold ${small ? "text-xs" : "text-lg"} tnum break-all`}>{value}</div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-4">
      <h3 className="m-0 mb-3 text-sm font-semibold text-fg">{title}</h3>
      {children}
    </div>
  );
}

function SignalCategory({ label, items, className = "" }: { label: string; items: string[]; className?: string }) {
  return (
    <div className={className}>
      <div className="text-[10px] uppercase tracking-wider text-fg-subtle mb-1">{label}</div>
      <div className="flex flex-wrap gap-1">
        {items.length > 0 ? items.map((it, i) => (
          <span key={i} className="bg-surface-2 text-fg px-2 py-0.5 rounded text-xs font-mono">{it}</span>
        )) : <span className="text-fg-subtle text-xs">—</span>}
      </div>
    </div>
  );
}
