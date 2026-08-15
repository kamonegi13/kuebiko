// ルーティング条件 (when) ⟷ 再帰 Condition モデルの相互変換 + 要約。
// 語彙統一 (2026-06-14): flag/記事属性 の分離を撤廃し「型付きプロパティ」に一元化。
// 旧形 ({flag:x} / {field:{op:val}}) も parse できる (後方互換)。emit は新形のみ。
// 詳細: docs/routing_rule_authoring_design.md

import type { WhenCondition } from "../../api/routingRules";
import { routingLabel } from "../../api/routingLabels";

export type CondValue = string | string[] | number | boolean | null;

export type Condition =
  | { kind: "leaf"; property: string; op: string; value: CondValue; negated: boolean }
  | { kind: "group"; mode: "all" | "any"; children: Condition[]; negated: boolean }
  | { kind: "always" }
  | { kind: "raw"; json: string };

export interface EditRule {
  id: string;
  // 人向け表示名 (ルール一覧・記事の配信判定はこれを主表示にする)。空なら id を出す。
  label: string;
  channel: string;
  root: Condition;
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** op が値入力を取るか (boolean の is_true/is_false は取らない)。 */
export function opTakesValue(op: string): boolean {
  return op !== "is_true" && op !== "is_false";
}

/** op が複数値 (multiselect) か。 */
export function opIsMulti(op: string): boolean {
  return op === "in" || op === "not_in";
}

function leafFromOld(key: string, spec: unknown): Condition {
  if (key === "flag") {
    return { kind: "leaf", property: String(spec), op: "is_true", value: null, negated: false };
  }
  if (isObj(spec)) {
    const op = Object.keys(spec)[0] ?? "eq";
    return { kind: "leaf", property: key, op, value: (spec[op] as CondValue) ?? null, negated: false };
  }
  return { kind: "leaf", property: key, op: "eq", value: spec as CondValue, negated: false };
}

export function parseWhen(when: unknown): Condition {
  if (!isObj(when)) return { kind: "always" };
  if ("always" in when) return { kind: "always" };
  if (Array.isArray(when.all)) {
    return { kind: "group", mode: "all", children: when.all.map(parseWhen), negated: false };
  }
  if (Array.isArray(when.any)) {
    return { kind: "group", mode: "any", children: when.any.map(parseWhen), negated: false };
  }
  if ("not" in when) {
    const child = parseWhen(when.not);
    if (child.kind === "leaf" || child.kind === "group") {
      return { ...child, negated: !child.negated };
    }
    return { kind: "raw", json: JSON.stringify(when) };
  }
  if ("property" in when) {
    return {
      kind: "leaf",
      property: String(when.property),
      op: String(when.op ?? ""),
      value: (when.value as CondValue) ?? null,
      negated: false,
    };
  }
  // 旧形の葉 (複数キー = AND)
  const keys = Object.keys(when);
  if (keys.length === 0) return { kind: "always" };
  if (keys.length === 1) return leafFromOld(keys[0], when[keys[0]]);
  return {
    kind: "group",
    mode: "all",
    children: keys.map((k) => leafFromOld(k, when[k])),
    negated: false,
  };
}

function wrapNot(node: WhenCondition, negated: boolean): WhenCondition {
  return negated ? ({ not: node } as WhenCondition) : node;
}

export function emitWhen(c: Condition): WhenCondition {
  switch (c.kind) {
    case "always":
      return { always: true } as WhenCondition;
    case "raw":
      try {
        return JSON.parse(c.json) as WhenCondition;
      } catch {
        return { always: true } as WhenCondition;
      }
    case "leaf": {
      const leaf: Record<string, unknown> = { property: c.property, op: c.op };
      if (opTakesValue(c.op)) leaf.value = c.value;
      return wrapNot(leaf as WhenCondition, c.negated);
    }
    case "group":
      return wrapNot(
        { [c.mode]: c.children.map(emitWhen) } as WhenCondition,
        c.negated,
      );
  }
}

// ---- 要約 (ladder 表示用、vocab なしの静的ラベル) ----
function valueText(property: string, value: CondValue): string {
  if (Array.isArray(value)) return value.map((v) => routingLabel.value(property, String(v))).join(", ");
  if (value === null || value === undefined || value === "") return "";
  return String(value);
}

function summarizeCondition(c: Condition): string {
  if (c.kind === "always") return "全件該当";
  if (c.kind === "raw") return "raw(JSON)";
  if (c.kind === "leaf") {
    const p = routingLabel.property(c.property);
    const op = routingLabel.op(c.op);
    const body = opTakesValue(c.op) ? `${p} ${op} [${valueText(c.property, c.value)}]` : `${p}=${op}`;
    return c.negated ? `〔でない〕${body}` : body;
  }
  const sep = c.mode === "all" ? " かつ " : " または ";
  const inner = c.children.map(summarizeCondition).join(sep) || "(条件なし)";
  const body = `(${inner})`;
  return c.negated ? `〔でない〕${body}` : body;
}

export function summarizeWhen(when: WhenCondition): string {
  return summarizeCondition(parseWhen(when));
}
