// synthesis 系の長文 narrative を「構造」として描画する共通コンポーネント (2026-07-19 v2)。
//
// DB 原文は不変 (表示専用の写像)。CTI 総括の本文は実質「判定文の列挙」なので、
// 段落プローズではなく **文単位の判定行** に分解して表示する:
//   1. 文 (。区切り) = 1 行。行頭マーカー付きで scan しやすく
//   2. 文末の確度括弧 「(高確度)」「(中確度、〜の可能性も残る)」を **確度チップ** に抽出
//      (補足文言はチップ横の muted 注記として保持 — 情報は落とさない)
//   3. 「・」「-」箇条書き行はインデント行、(a)〜(e) マーカーは小見出し行
//   4. 元の段落境界はグループ間隔として保持
//   5. 既定高さで折りたたみ (+fade+全文表示)。短文はそのまま

import { useLayoutEffect, useRef, useState } from "react";
import { MarkdownText } from "./MarkdownText";
import { vocabLabel } from "../hooks/useVocab";

// 折りたたみ時の既定高さ (px)。~12 行分
const DEFAULT_COLLAPSED_PX = 300;

type ConfLevel = "high" | "moderate" | "low";

// 確度ラベルは backend 配信 vocab "confidence" (vocabLabel) を SSoT に。ここは色 (cls) のみ保持。
const CONF_CLS: Record<ConfLevel, string> = {
  high: "bg-critical-soft text-critical",
  moderate: "bg-warning-soft text-warning",
  low: "bg-surface-3 text-fg-subtle",
};

interface ProseRow {
  kind: "sentence" | "bullet" | "marker";
  text: string;
  conf?: ConfLevel;
  confNote?: string; // 確度括弧内の補足 (例: 報道アーティファクトの可能性も残る)
}

// 文末の確度括弧: (高確度) / （中確度、〜） / (組織的国家作戦、高確度) 等
const CONF_PAREN_RE = /[(（]([^()（）]{0,60}?(高確度|中確度|低確度)[^()（）]{0,60}?)[)）]\s*(。?)\s*$/;
const CONF_LEVEL: Record<string, ConfLevel> = {
  高確度: "high",
  中確度: "moderate",
  低確度: "low",
};

function extractConfidence(sentence: string): Pick<ProseRow, "text" | "conf" | "confNote"> {
  const m = sentence.match(CONF_PAREN_RE);
  if (!m) return { text: sentence };
  const inner = m[1];
  const conf = CONF_LEVEL[m[2]];
  // 括弧内から確度語と区切り記号を除いた残りが補足注記
  const note = inner
    .replace(m[2], "")
    .replace(/^[、,・\s]+|[、,・\s]+$/g, "")
    .trim();
  const text = sentence.slice(0, m.index).trimEnd() + (m[3] || "。");
  return { text, conf, confNote: note || undefined };
}

function sentenceRows(block: string): ProseRow[] {
  return block
    .split(/(?<=。)/)
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => ({ kind: "sentence" as const, ...extractConfidence(s) }));
}

// 原文 → 行グループ (グループ = 元の段落/行のまとまり)。表示専用で原文は不変。
export function parseSynthesisRows(text: string): ProseRow[][] {
  const groups: ProseRow[][] = [];
  let current: ProseRow[] = [];
  const flush = () => {
    if (current.length) {
      groups.push(current);
      current = [];
    }
  };
  for (const rawLine of text.split("\n")) {
    let line = rawLine.trim();
    if (!line) {
      flush();
      continue;
    }
    // 箇条書き行
    if (/^[・•\-]\s*/.test(line)) {
      current.push({
        kind: "bullet",
        ...extractConfidence(line.replace(/^[・•\-]\s*/, "")),
      });
      continue;
    }
    // (a)〜(e) マーカー: 行頭なら小見出し、文中なら分割してから処理
    const parts = line
      .replace(/(。)\s*([(（][a-eア-オ][)）])\s*/g, "$1\n$2 ")
      .split("\n");
    for (const part of parts) {
      const mm = part.match(/^[(（]([a-eア-オ])[)）]\s*(.*)$/);
      if (mm) {
        flush();
        current.push({ kind: "marker", text: `(${mm[1]})` });
        if (mm[2]) current.push(...sentenceRows(mm[2]));
        continue;
      }
      current.push(...sentenceRows(part));
    }
  }
  flush();
  return groups;
}

function Row({ row }: { row: ProseRow }) {
  if (row.kind === "marker") {
    return (
      <div className="pt-1 text-[12px] font-bold text-accent-hover tracking-wide">{row.text}</div>
    );
  }
  const confCls = row.conf ? CONF_CLS[row.conf] : null;
  const confLabel = row.conf ? vocabLabel("confidence", row.conf) : "";
  return (
    // max-w: 全幅カード (トレードクラフト等) 内でも行長を可読域に保つ
    // (2 カラム grid / 内部段組内 ~700px では実質無効 = 幅いっぱい使う)。
    // break-inside-avoid: 内部 2 段組 (columns) 時に行の途中で段が割れないように。
    <div
      className={`flex items-baseline gap-2.5 max-w-[84ch] break-inside-avoid ${row.kind === "bullet" ? "pl-4" : ""}`}
    >
      <span className="w-1 h-1 rounded-full bg-fg-subtle shrink-0 translate-y-[-2px]" aria-hidden />
      <div className="flex-1 min-w-0 text-[13.5px] leading-[1.85] text-fg [&_p]:!m-0 [&_p]:!text-[13.5px] [&_p]:!leading-[1.85]">
        <MarkdownText>{row.text}</MarkdownText>
        {row.confNote && <span className="text-[11.5px] text-fg-subtle">（{row.confNote}）</span>}
      </div>
      {confCls && (
        <span
          title={row.confNote ? `${confLabel}: ${row.confNote}` : confLabel}
          className={`shrink-0 self-start mt-1 inline-flex px-1.5 py-px rounded-sm text-[10px] font-semibold ${confCls}`}
        >
          {confLabel}
        </span>
      )}
    </div>
  );
}

interface SynthesisProseProps {
  text: string | null | undefined;
  /** 折りたたみ時の高さ (px)。0 = 折りたたみ無効 (常に全文) */
  collapsedHeight?: number;
  /** 全幅カード用: lg 以上で判定行を 2 段組に流し込む (独立判定のリスト向け) */
  columns?: boolean;
}

export function SynthesisProse({
  text,
  collapsedHeight = DEFAULT_COLLAPSED_PX,
  columns = false,
}: SynthesisProseProps) {
  const [expanded, setExpanded] = useState(false);
  const [overflows, setOverflows] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useLayoutEffect(() => {
    const el = ref.current;
    if (el && collapsedHeight > 0) {
      // +60px の余裕: ギリギリ超過での「展開しても 1 行しか増えない」toggle を出さない
      setOverflows(el.scrollHeight > collapsedHeight + 60);
    }
  }, [text, collapsedHeight]);

  if (!text) return null;
  const groups = parseSynthesisRows(text);
  const collapsed = collapsedHeight > 0 && overflows && !expanded;

  return (
    <div>
      <div
        ref={ref}
        style={collapsed ? { maxHeight: collapsedHeight } : undefined}
        className="relative overflow-hidden"
      >
        <div className={columns ? "lg:columns-2 lg:gap-10 space-y-3" : "space-y-3"}>
          {groups.map((rows, gi) => (
            <div key={gi} className="space-y-1.5">
              {rows.map((row, ri) => (
                <Row key={ri} row={row} />
              ))}
            </div>
          ))}
        </div>
        {collapsed && (
          <div className="absolute inset-x-0 bottom-0 h-16 bg-gradient-to-t from-surface-1 to-transparent pointer-events-none" />
        )}
      </div>
      {collapsedHeight > 0 && overflows && (
        <button
          onClick={() => setExpanded((v) => !v)}
          className="mt-1.5 text-xs text-accent hover:text-accent-hover"
        >
          {expanded ? "▲ 折りたたむ" : "▼ 全文を表示"}
        </button>
      )}
    </div>
  );
}
