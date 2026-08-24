// 「Diamond / 判定」カードの **共有 SSoT**。記事画面 (ArticleReadView) と
// 事象ニュース (EventNewsDetail) の両方がここを使う。
//
// 2026-08-24: 事象ニュース側で同じカードを書き直したところ、行の並び・ラベル・
// 被害の 1 行表記・PMESII の有無がすべてズレた。ArticleReadView の冒頭に
// 「表示ロジックを二重化するとドリフト発生器になる」と書いてある通りだったので、
// 描画をこの 1 箇所へ寄せる。呼び手は自分のデータを ``Judgement`` へ写すだけ。
//
// 事象は複数記事の集計なので、各値は任意で「何記事がその値か」を持てる (``articles``)。
// 記事 1 件の呼び出しでは undefined にして件数を出さない。

import type { ReactNode } from "react";

/** 1 つの判定値。``articles`` があれば「(N)」を添える (集計時のみ)。 */
export interface JudgementValue {
  value: string;
  label: string;
  articles?: number;
  tone?: string;
}

/** 自由記述の判定欄。事象では記事ごとに並べ、出典番号を付ける。 */
export interface JudgementText {
  label: string;
  items: { text: string; sourceIndex?: number }[];
}

export interface Judgement {
  intent?: JudgementValue[] | null;
  /** 意図の確度。仮説扱いなら ``hypothesis`` を立てる。 */
  intentConfidence?: { label: string; hypothesis: boolean; articles?: number }[] | null;
  texts?: JudgementText[] | null;
  stance?: JudgementValue[] | null;
  /** 被害は「セクター / 国」を **1 行** にまとめる (記事画面の書式)。 */
  victim?: JudgementValue[][] | null;
  delivery?: ReactNode | null;
  pmesii?: { label: string; articles?: number }[] | null;
  /** 呼び手固有の追加行 (事象の「裏取り」等)。末尾に置く。 */
  extraRows?: { label: string; node: ReactNode }[] | null;
}

const ROW = "flex gap-2";
const DT = "text-fg-subtle w-24 shrink-0";

function Values({ values }: { values: JudgementValue[] }) {
  return (
    <>
      {values.map((v, i) => (
        <span key={`${v.value}-${i}`} className={v.tone}>
          {i > 0 && <span className="text-fg-subtle"> / </span>}
          {v.label}
          {v.articles != null && (
            <span className="text-fg-subtle text-xs ml-0.5 tnum">({v.articles})</span>
          )}
        </span>
      ))}
    </>
  );
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className={ROW}>
      <dt className={DT}>{label}</dt>
      <dd className="text-fg-muted m-0">{children}</dd>
    </div>
  );
}

export function JudgementCard({ j, title = "Diamond / 判定" }: { j: Judgement; title?: string }) {
  const victimRow = (j.victim ?? []).flat().filter((v) => v.label);
  const hasAny =
    (j.intent?.length ?? 0) > 0 ||
    (j.texts?.length ?? 0) > 0 ||
    (j.stance?.length ?? 0) > 0 ||
    victimRow.length > 0 ||
    j.delivery != null ||
    (j.pmesii?.length ?? 0) > 0 ||
    (j.extraRows?.length ?? 0) > 0;
  if (!hasAny) return null;

  // 常時表示は「意図 / 被害 / 呼び手固有の行 (裏取り等)」だけ。読み手が最初に要る
  // のはこの 3 つで、根拠・技術面・対処・所見・論調・配信判定・PMESII は
  // **必要なときに開く**。カードが縦に長いと本文へ辿り着く前に画面を使い切る。
  const detailCount =
    (j.texts?.length ?? 0) +
    (j.stance?.length ? 1 : 0) +
    (j.delivery != null ? 1 : 0) +
    (j.pmesii?.length ? 1 : 0);

  return (
    <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-2">
      <div className="text-fg-muted text-xs uppercase">{title}</div>
      <dl className="text-sm space-y-1.5 m-0">
        {j.intent && j.intent.length > 0 && (
          <Row label="意図">
            <span className="font-medium">
              <Values values={j.intent} />
            </span>
            {(j.intentConfidence ?? []).map((c) => (
              <span
                key={c.label}
                className={`ml-2 text-xs font-normal ${c.hypothesis ? "text-warning" : "text-fg-subtle"}`}
              >
                {c.label}
                {c.hypothesis && " (仮説)"}
                {c.articles != null && <span className="tnum ml-0.5">({c.articles})</span>}
              </span>
            ))}
          </Row>
        )}
        {victimRow.length > 0 && (
          <Row label="被害">
            <Values values={victimRow} />
          </Row>
        )}
        {(j.extraRows ?? []).map((r) => (
          <Row key={r.label} label={r.label}>
            {r.node}
          </Row>
        ))}
      </dl>
      {detailCount > 0 && (
        <details className="group">
          <summary className="text-fg-subtle text-xs cursor-pointer select-none hover:text-accent">
            <span className="inline-block transition-transform group-open:rotate-90">▸</span>{" "}
            <span className="group-open:hidden">詳細を表示 ({detailCount})</span>
            <span className="hidden group-open:inline">詳細を閉じる</span>
          </summary>
          <dl className="text-sm space-y-1.5 m-0 mt-2">
            {(j.texts ?? []).map((t) => (
              <Row key={t.label} label={t.label}>
                {t.items.map((it, i) => (
                  <span key={i} className="block">
                    {it.text}
                    {it.sourceIndex != null && (
                      <span className="align-super text-[10px] font-mono text-fg-subtle ml-0.5">
                        [{it.sourceIndex}]
                      </span>
                    )}
                  </span>
                ))}
              </Row>
            ))}
            {j.stance && j.stance.length > 0 && (
              <Row label="論調">
                <Values values={j.stance} />
              </Row>
            )}
            {j.delivery != null && <Row label="配信判定">{j.delivery}</Row>}
          </dl>
          {j.pmesii && j.pmesii.length > 0 && (
            <div className="pt-2">
              <div className="text-fg-subtle text-xs mb-1">PMESII-PT</div>
              <div className="flex flex-wrap gap-1.5">
                {j.pmesii.map((p) => (
                  <span
                    key={p.label}
                    className="bg-surface-2 border border-border-default rounded px-2 py-0.5 text-xs text-fg-muted"
                  >
                    {p.label}
                    {p.articles != null && <span className="tnum ml-1 text-fg-subtle">{p.articles}</span>}
                  </span>
                ))}
              </div>
            </div>
          )}
        </details>
      )}
    </div>
  );
}
