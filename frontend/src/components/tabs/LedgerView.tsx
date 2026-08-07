// 情勢台帳 (Situation Ledger) ボード — 「推論の可視化」の時間軸拡張 (段D)。
// 追跡中の情勢 (salience 順) と、各情勢の判定推移 (revision)・証拠台帳・関係を開示する。
import { useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  situationsApi,
  type SituationDetail,
  type SituationRevision,
  type SituationSummary,
} from "../../api/situations";
import { vocabLabel } from "../../hooks/useVocab";

// 確度ラベルは backend 配信 vocab "confidence" (vocabLabel) を SSoT に。ここは色 (tone) のみ保持。
const CONF_TONE: Record<string, string> = {
  high: "text-ok",
  moderate: "text-warn",
  low: "text-fg-subtle",
};

function fmtDate(iso: string): string {
  return iso ? iso.slice(0, 16).replace("T", " ") : "";
}

// deep link (#situation=<id>) — 実体ページ (アクター辞書等) からの着地点 (C-lite)。
// mount 時に 1 回だけ消費し、該当カードを展開してスクロールする。
function situationFromHash(): string {
  if (typeof window === "undefined") return "";
  const h = window.location.hash.startsWith("#") ? window.location.hash.slice(1) : "";
  return new URLSearchParams(h).get("situation") ?? "";
}

export function LedgerView() {
  const [initialOpenId] = useState(situationFromHash);
  // deep link 先が closed の可能性があるため、着地時は「終結も表示」を初期 ON にする
  const [showDormant, setShowDormant] = useState(() => initialOpenId !== "");
  const status = showDormant ? "active,dormant,closed" : "active,dormant";
  const { data, isLoading } = useQuery({
    queryKey: ["situations", status],
    queryFn: () => situationsApi.list(status),
  });

  if (isLoading) return <div className="text-fg-subtle text-sm p-4">読込中…</div>;
  const items = data?.situations ?? [];
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <div className="text-[11px] text-fg-subtle">
          追跡中の情勢 {items.length} 件 — 重要度 (使命序列 × 変化 × PIR × 日本関連 × 確度) 順。
          判定・確度・変化は状況総括処理が台帳更新で刻む (ここは読み取りのみ)。
        </div>
        <label className="text-[11px] text-fg-subtle flex items-center gap-1.5 cursor-pointer">
          <input
            type="checkbox"
            checked={showDormant}
            onChange={(e) => setShowDormant(e.target.checked)}
          />
          終結も表示
        </label>
      </div>
      <div className="space-y-2">
        {items.map((s) => (
          <SituationCard
            key={s.situation_id}
            s={s}
            initialOpen={s.situation_id === initialOpenId}
          />
        ))}
        {items.length === 0 && (
          <div className="text-fg-subtle text-sm p-4">追跡中の情勢はまだありません。</div>
        )}
      </div>
    </div>
  );
}

function SituationCard({
  s,
  initialOpen = false,
}: {
  s: SituationSummary;
  initialOpen?: boolean;
}) {
  const [open, setOpen] = useState(initialOpen);
  const cardRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (initialOpen) cardRef.current?.scrollIntoView({ block: "start" });
  }, [initialOpen]);
  const confTone = CONF_TONE[s.latest?.confidence ?? ""] ?? "text-fg-subtle";
  // 未知の delta_type は原値 fallback (空欄化しない、判定推移の :146 と同じ扱いに統一)。
  const delta = vocabLabel("delta_type", s.latest?.delta_type);
  return (
    <div
      ref={cardRef}
      className="bg-surface-1 border border-border-subtle rounded-lg transition-colors hover:border-border-default"
    >
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="w-full text-left p-3.5 flex items-start gap-3"
      >
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            {s.kind === "standing" && (
              <span className="text-[10px] px-1.5 py-0.5 rounded border border-accent/30 bg-accent-subtle text-accent shrink-0">
                常設
              </span>
            )}
            <span className="text-[13px] font-medium">{s.title}</span>
            {delta && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-accent-subtle text-accent-hover">
                {delta}
              </span>
            )}
          </div>
          <div className="mt-1.5 text-[11px] text-fg-subtle flex flex-wrap gap-3">
            <span>{s.domain}</span>
            <span>{vocabLabel("situation_status", s.status)}</span>
            <span className={confTone}>{vocabLabel("confidence", s.latest?.confidence)}</span>
            <span>
              証拠 {s.assessed_count ?? 0} 件
              {s.evidence_count - (s.assessed_count ?? 0) > 0 &&
                ` (+未評価 ${s.evidence_count - (s.assessed_count ?? 0)})`}
            </span>
            <span>改訂 {s.latest?.rev ?? 0}</span>
            <span>最終 {fmtDate(s.last_evidence_at)}</span>
            {s.pir_ids.length > 0 && <span>PIR {s.pir_ids.length}</span>}
          </div>
          {s.latest?.implication && (
            <div className="mt-1.5 text-[11px] text-fg-default">含意: {s.latest.implication}</div>
          )}
        </div>
        <span className="text-[10px] text-fg-subtle tnum shrink-0">{s.salience}</span>
      </button>
      {open && <SituationDetailPane id={s.situation_id} />}
    </div>
  );
}

function SituationDetailPane({ id }: { id: string }) {
  const { data, isLoading } = useQuery({
    queryKey: ["situation", id],
    queryFn: () => situationsApi.detail(id),
  });
  if (isLoading) return <div className="text-fg-subtle text-[11px] px-3.5 pb-3">読込中…</div>;
  if (!data) return null;
  return (
    <div className="px-3.5 pb-3.5 space-y-3 border-t border-border-subtle pt-3">
      <RevisionTimeline revisions={data.revisions} />
      <RelationsList detail={data} />
      <EvidenceList detail={data} />
    </div>
  );
}

function RevisionTimeline({ revisions }: { revisions: SituationRevision[] }) {
  return (
    <div>
      <h5 className="text-[10px] text-fg-subtle uppercase tracking-wider font-semibold mb-1.5">
        判定の推移
      </h5>
      <div className="space-y-1.5">
        {[...revisions].reverse().map((r) => {
          const confTone = CONF_TONE[r.confidence] ?? "";
          return (
            <div key={r.rev} className="text-[11px] flex gap-2 items-baseline">
              <span className="text-fg-subtle tnum shrink-0">#{r.rev}</span>
              <span className="text-fg-subtle shrink-0">{fmtDate(r.created_at)}</span>
              <span className="px-1 rounded bg-surface-2 shrink-0">
                {vocabLabel("delta_type", r.delta_type)}
              </span>
              <span className={`shrink-0 ${confTone}`}>{vocabLabel("confidence", r.confidence)}</span>
              <span className="min-w-0">
                {r.claim}
                {r.delta_note && <span className="text-fg-subtle"> — {r.delta_note}</span>}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function RelationsList({ detail }: { detail: SituationDetail }) {
  if (detail.relations.length === 0) return null;
  return (
    <div>
      <h5 className="text-[10px] text-fg-subtle uppercase tracking-wider font-semibold mb-1.5">
        関係する情勢 (機械的に導出 — 因果は主張しない)
      </h5>
      <ul className="text-[11px] space-y-1">
        {detail.relations.map((r, i) => (
          <li key={i}>
            {vocabLabel("situation_rel_type", r.rel_type)} ({r.basis}): {r.other_title || "(不明)"}
          </li>
        ))}
      </ul>
    </div>
  );
}

// 観測と判断は別状態: ACH が引用した「接地証拠」(polarity/抜粋が有意) と、
// 割当だけで未評価の記事 (title のみ・読了状態を明示) を分けて開示する。
function EvidenceList({ detail }: { detail: SituationDetail }) {
  const assessed = detail.evidence.filter((e) => e.assessed_at || e.excerpt);
  const unassessed = detail.evidence.filter((e) => !e.assessed_at && !e.excerpt);
  return (
    <div className="space-y-2">
      <div>
        <h5 className="text-[10px] text-fg-subtle uppercase tracking-wider font-semibold mb-1.5">
          根拠となる証拠 — ACH 評価済み ({assessed.length} 件)
        </h5>
        <ul className="text-[11px] space-y-1.5">
          {assessed.slice(0, 15).map((e, i) => (
            <li key={i} className="flex gap-2 items-baseline">
              <span
                className={`shrink-0 px-1 rounded bg-surface-2 ${
                  e.polarity === "supports"
                    ? "text-ok"
                    : e.polarity === "contradicts"
                      ? "text-warn"
                      : "text-fg-subtle"
                }`}
              >
                {vocabLabel("polarity", e.polarity)}
              </span>
              <span className="min-w-0">
                「{e.excerpt}」
                {e.article_title && (
                  <span className="text-fg-subtle"> — {e.article_title}</span>
                )}
                <a
                  href={`/app/article/${encodeURIComponent(e.article_id)}`}
                  className="text-accent-hover ml-1"
                >
                  →本文
                </a>
              </span>
            </li>
          ))}
          {assessed.length === 0 && (
            <li className="text-fg-subtle">評価済みの証拠はまだありません。</li>
          )}
        </ul>
      </div>
      {unassessed.length > 0 && (
        <div>
          <h5 className="text-[10px] text-fg-subtle uppercase tracking-wider font-semibold mb-1.5">
            未評価の割当記事 ({unassessed.length} 件) — ACH は未引用 (中立の証拠ではない)
          </h5>
          <ul className="text-[11px] space-y-1">
            {unassessed.slice(0, 10).map((e, i) => (
              <li key={i} className="flex gap-2 items-baseline text-fg-muted">
                <span className="shrink-0 px-1 rounded bg-surface-2 text-fg-subtle">
                  {e.read_at ? "読了" : "未読"}
                </span>
                <span className="min-w-0">
                  {e.article_title || e.article_id}
                  <span className="text-fg-subtle"> ({vocabLabel("assigned_by", e.assigned_by)}割当 {fmtDate(e.added_at)})</span>
                  <a
                    href={`/app/article/${encodeURIComponent(e.article_id)}`}
                    className="text-accent-hover ml-1"
                  >
                    →本文
                  </a>
                </span>
              </li>
            ))}
            {unassessed.length > 10 && (
              <li className="text-fg-subtle">…ほか {unassessed.length - 10} 件</li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
}
