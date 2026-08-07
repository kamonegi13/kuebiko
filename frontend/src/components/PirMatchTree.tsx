// PIR 照合条件ツリーの可読表示 (authoring 統一 2026-07-23)。
// 値ラベルは backend vocab SSoT (pir_match_property / category / intent / sector) を参照。
// ツリービルダー GUI は作らない (生成は AI、修正は JSON 直編集) — 表示だけを担う。

import { vocabLabel } from "../hooks/useVocab";
import type { MatchNode } from "../api/pir";

function valueLabels(property: string, value: string | string[] | undefined): string {
  const values = Array.isArray(value) ? value : value != null ? [value] : [];
  const mapper = (v: string): string => {
    if (property === "category") return vocabLabel("category", v);
    if (property === "intent") return vocabLabel("intent", v);
    if (property === "victim_sector") return vocabLabel("sector", v);
    if (property === "victim_country" || property === "actor_nation") return v.toUpperCase();
    return v;
  };
  return values.map(mapper).join(" / ");
}

function leafText(node: MatchNode): string {
  const prop = node.property ?? "";
  const propLabel = vocabLabel("pir_match_property", prop);
  const vals = valueLabels(prop, node.value);
  switch (node.op) {
    case "is_true":
      return `${propLabel} である`;
    case "contains_any":
      return `${propLabel}に「${vals}」のいずれかを含む`;
    case "keyword_any":
      return `${propLabel}に「${vals}」のいずれか (語一致)`;
    case "any_of":
      return `${propLabel}が「${vals}」のいずれか (辞書照合・主題判定つき)`;
    default: // eq / in
      return `${propLabel}が「${vals}」`;
  }
}

export function PirMatchTree({ node, depth = 0 }: { node: MatchNode | null; depth?: number }) {
  if (!node) return null;
  const indent = depth > 0 ? "ml-4" : "";
  if (node.all) {
    return (
      <div className={indent}>
        <div className="text-fg-muted text-xs">すべて満たす:</div>
        {node.all.map((c, i) => (
          <PirMatchTree key={i} node={c} depth={depth + 1} />
        ))}
      </div>
    );
  }
  if (node.any) {
    return (
      <div className={indent}>
        <div className="text-fg-muted text-xs">いずれかを満たす:</div>
        {node.any.map((c, i) => (
          <PirMatchTree key={i} node={c} depth={depth + 1} />
        ))}
      </div>
    );
  }
  if (node.not) {
    return (
      <div className={`${indent} flex items-baseline gap-1`}>
        <span className="text-critical text-xs font-medium shrink-0">除外:</span>
        <PirMatchTree node={node.not} depth={0} />
      </div>
    );
  }
  if (node.always !== undefined) {
    return <div className={`${indent} text-sm`}>{node.always ? "常に該当" : "常に非該当"}</div>;
  }
  return <div className={`${indent} text-sm text-fg`}>{leafText(node)}</div>;
}
