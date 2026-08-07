// 1 ルールの編集 UI (ID + 投稿先 + 再帰条件)。FlowPage の Rule Drawer から使う。
// 語彙統一 (2026-06-14): 条件は「型付きプロパティ」の再帰ツリー (葉 | グループ)。
// flag/記事属性 の区別は無く、プロパティを選ぶと型に応じた入力が出る。
// 否定は「でない」トグル (葉/グループ)、結合は「すべて/いずれか」、ネストは任意深さ。
// 詳細: docs/routing_rule_authoring_design.md

import { useChannelMeta } from "../../components/channel";
import { routingLabel } from "../../api/routingLabels";
import type { PropertySpec, RoutingVocab } from "../../api/routingRules";
import {
  type Condition,
  type CondValue,
  type EditRule,
  opIsMulti,
  opTakesValue,
} from "./clauseModel";

function defaultValueFor(prop: PropertySpec, op: string): CondValue {
  if (prop.kind === "boolean") return null;
  if (prop.kind === "number") return 9;
  if (prop.kind === "set" || opIsMulti(op)) return [];
  if (op === "in_config") return prop.id === "category" ? "" : "";
  return prop.values[0] ?? "";
}

function coerceValueForOp(value: CondValue, op: string): CondValue {
  if (!opTakesValue(op)) return null;
  if (opIsMulti(op)) {
    if (Array.isArray(value)) return value;
    return value === null || value === "" ? [] : [String(value)];
  }
  // 単一値へ
  if (Array.isArray(value)) return value[0] ?? "";
  return value;
}

function PropertySelect({
  properties,
  value,
  readOnly,
  onChange,
}: {
  properties: PropertySpec[];
  value: string;
  readOnly: boolean;
  onChange: (id: string) => void;
}) {
  // topic (group) ごとに optgroup 化。
  const byGroup = new Map<string, PropertySpec[]>();
  for (const p of properties) {
    const arr = byGroup.get(p.group) ?? [];
    arr.push(p);
    byGroup.set(p.group, arr);
  }
  return (
    <select
      value={value}
      disabled={readOnly}
      onChange={(e) => onChange(e.target.value)}
      className="rounded border border-border-subtle bg-surface-2 px-1.5 py-0.5 text-xs text-fg"
    >
      {[...byGroup.entries()].map(([g, ps]) => (
        <optgroup key={g} label={g}>
          {ps.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
        </optgroup>
      ))}
    </select>
  );
}

function MultiSelect({
  options,
  selected,
  readOnly,
  label,
  onChange,
}: {
  options: string[];
  selected: string[];
  readOnly: boolean;
  label: (id: string) => string;
  onChange: (v: string[]) => void;
}) {
  const toggle = (o: string) =>
    onChange(selected.includes(o) ? selected.filter((x) => x !== o) : [...selected, o]);
  if (options.length === 0) {
    // 値域が空 (例: source) は自由入力。
    return (
      <input
        value={selected.join(", ")}
        disabled={readOnly}
        onChange={(e) => onChange(e.target.value.split(",").map((s) => s.trim()).filter(Boolean))}
        placeholder="値 (カンマ区切り)"
        className="min-w-[140px] rounded border border-border-subtle bg-surface-2 px-1.5 py-0.5 text-xs text-fg"
      />
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-1">
      {options.map((o) => {
        const on = selected.includes(o);
        return (
          <button
            key={o}
            type="button"
            disabled={readOnly}
            onClick={() => toggle(o)}
            title={o}
            className={`rounded border px-1.5 py-0.5 text-[11px] ${on ? "border-accent bg-accent-soft text-accent-hover" : "border-border-subtle bg-surface-2 text-fg-subtle hover:bg-surface-3"}`}
          >
            {label(o)}
          </button>
        );
      })}
    </div>
  );
}

function ValueInput({
  prop,
  op,
  value,
  vocab,
  readOnly,
  onChange,
}: {
  prop: PropertySpec;
  op: string;
  value: CondValue;
  vocab: RoutingVocab;
  readOnly: boolean;
  onChange: (v: CondValue) => void;
}) {
  if (!opTakesValue(op)) return null; // boolean is_true/is_false
  if (prop.kind === "number") {
    return (
      <input
        type="number"
        step="0.1"
        min="0"
        max="10"
        value={typeof value === "number" ? value : (value as string) ?? ""}
        disabled={readOnly}
        onChange={(e) => onChange(e.target.value === "" ? 0 : Number(e.target.value))}
        placeholder="0-10"
        className="w-20 rounded border border-border-subtle bg-surface-2 px-1.5 py-0.5 text-xs text-fg"
      />
    );
  }
  if (op === "in_config") {
    return (
      <select
        value={typeof value === "string" ? value : ""}
        disabled={readOnly}
        onChange={(e) => onChange(e.target.value)}
        className="rounded border border-border-subtle bg-surface-2 px-1.5 py-0.5 text-xs text-fg"
      >
        <option value="">(設定リストを選択)</option>
        {vocab.config_lists.map((c) => (
          <option key={c} value={c}>
            {routingLabel.value("category", c)}
          </option>
        ))}
      </select>
    );
  }
  const lbl = (id: string) => routingLabel.value(prop.id, id);
  if (opIsMulti(op)) {
    return (
      <MultiSelect
        options={prop.values}
        selected={Array.isArray(value) ? value : []}
        readOnly={readOnly}
        label={lbl}
        onChange={(v) => onChange(v)}
      />
    );
  }
  // 単一値 (eq/ne): 値域があれば select、なければ自由入力。
  const single = Array.isArray(value) ? (value[0] ?? "") : (value ?? "");
  if (prop.values.length > 0) {
    return (
      <select
        value={String(single)}
        disabled={readOnly}
        onChange={(e) => onChange(e.target.value)}
        className="rounded border border-border-subtle bg-surface-2 px-1.5 py-0.5 text-xs text-fg"
      >
        {prop.values.map((v) => (
          <option key={v} value={v}>
            {lbl(v)}
          </option>
        ))}
      </select>
    );
  }
  return (
    <input
      value={String(single)}
      disabled={readOnly}
      onChange={(e) => onChange(e.target.value)}
      placeholder="値"
      className="min-w-[120px] rounded border border-border-subtle bg-surface-2 px-1.5 py-0.5 text-xs text-fg"
    />
  );
}

function NegToggle({
  negated,
  readOnly,
  onChange,
}: {
  negated: boolean;
  readOnly: boolean;
  onChange: (n: boolean) => void;
}) {
  return (
    <label className="inline-flex items-center gap-1 text-[11px] text-fg-subtle">
      <input
        type="checkbox"
        checked={negated}
        disabled={readOnly}
        onChange={(e) => onChange(e.target.checked)}
      />
      でない
    </label>
  );
}

function RemoveBtn({ readOnly, onRemove }: { readOnly: boolean; onRemove?: () => void }) {
  if (readOnly || !onRemove) return null;
  return (
    <button
      type="button"
      onClick={onRemove}
      title="この条件を削除"
      className="shrink-0 rounded px-1 text-xs text-fg-subtle hover:text-critical"
    >
      ✕
    </button>
  );
}

function LeafEditor({
  leaf,
  vocab,
  readOnly,
  onChange,
  onRemove,
}: {
  leaf: Extract<Condition, { kind: "leaf" }>;
  vocab: RoutingVocab;
  readOnly: boolean;
  onChange: (c: Condition) => void;
  onRemove?: () => void;
}) {
  const prop = vocab.properties.find((p) => p.id === leaf.property) ?? vocab.properties[0];
  if (!prop) return null;
  const changeProperty = (pid: string) => {
    const np = vocab.properties.find((p) => p.id === pid);
    if (!np) return;
    const op = np.ops[0] ?? "eq";
    onChange({ ...leaf, property: pid, op, value: defaultValueFor(np, op) });
  };
  const changeOp = (op: string) => onChange({ ...leaf, op, value: coerceValueForOp(leaf.value, op) });
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      <PropertySelect
        properties={vocab.properties}
        value={prop.id}
        readOnly={readOnly}
        onChange={changeProperty}
      />
      <select
        value={leaf.op}
        disabled={readOnly}
        onChange={(e) => changeOp(e.target.value)}
        className="rounded border border-border-subtle bg-surface-2 px-1.5 py-0.5 text-xs text-fg"
      >
        {prop.ops.map((o) => (
          <option key={o} value={o}>
            {routingLabel.op(o)}
          </option>
        ))}
      </select>
      <ValueInput
        prop={prop}
        op={leaf.op}
        value={leaf.value}
        vocab={vocab}
        readOnly={readOnly}
        onChange={(v) => onChange({ ...leaf, value: v })}
      />
      <NegToggle negated={leaf.negated} readOnly={readOnly} onChange={(n) => onChange({ ...leaf, negated: n })} />
      <RemoveBtn readOnly={readOnly} onRemove={onRemove} />
    </div>
  );
}

function GroupEditor({
  group,
  vocab,
  readOnly,
  depth,
  onChange,
  onRemove,
}: {
  group: Extract<Condition, { kind: "group" }>;
  vocab: RoutingVocab;
  readOnly: boolean;
  depth: number;
  onChange: (c: Condition) => void;
  onRemove?: () => void;
}) {
  const setChild = (i: number, c: Condition) =>
    onChange({ ...group, children: group.children.map((x, j) => (j === i ? c : x)) });
  const removeChild = (i: number) =>
    onChange({ ...group, children: group.children.filter((_, j) => j !== i) });
  const firstProp = vocab.properties[0];
  const addLeaf = () => {
    if (!firstProp) return;
    const op = firstProp.ops[0] ?? "eq";
    const leaf: Condition = {
      kind: "leaf",
      property: firstProp.id,
      op,
      value: defaultValueFor(firstProp, op),
      negated: false,
    };
    onChange({ ...group, children: [...group.children, leaf] });
  };
  const addGroup = () =>
    onChange({
      ...group,
      children: [...group.children, { kind: "group", mode: "any", children: [], negated: false }],
    });
  return (
    <div className={depth > 0 ? "rounded border border-border-subtle/60 pl-2" : ""}>
      <div className="flex items-center gap-2 py-1">
        <select
          value={group.mode}
          disabled={readOnly}
          onChange={(e) => onChange({ ...group, mode: e.target.value as "all" | "any" })}
          className="rounded border border-border-subtle bg-surface-2 px-1.5 py-0.5 text-xs font-medium text-fg"
        >
          <option value="all">すべて満たす (AND)</option>
          <option value="any">いずれか満たす (OR)</option>
        </select>
        {depth > 0 && (
          <NegToggle negated={group.negated} readOnly={readOnly} onChange={(n) => onChange({ ...group, negated: n })} />
        )}
        <RemoveBtn readOnly={readOnly} onRemove={onRemove} />
      </div>
      <div className="space-y-1.5 border-l border-border-subtle pl-2">
        {group.children.length === 0 && (
          <div className="text-[11px] text-fg-subtle">条件なし = すべての記事が該当</div>
        )}
        {group.children.map((c, i) => (
          <ConditionEditor
            key={i}
            cond={c}
            vocab={vocab}
            readOnly={readOnly}
            depth={depth + 1}
            onChange={(x) => setChild(i, x)}
            onRemove={() => removeChild(i)}
          />
        ))}
        {!readOnly && (
          <div className="flex gap-2 pt-0.5">
            <button
              type="button"
              onClick={addLeaf}
              className="rounded border border-border-default px-2 py-0.5 text-[11px] text-fg-muted hover:text-accent hover:border-accent-soft"
            >
              + 条件
            </button>
            <button
              type="button"
              onClick={addGroup}
              className="rounded border border-border-default px-2 py-0.5 text-[11px] text-fg-muted hover:text-accent hover:border-accent-soft"
            >
              + グループ
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

function ConditionEditor({
  cond,
  vocab,
  readOnly,
  depth,
  onChange,
  onRemove,
}: {
  cond: Condition;
  vocab: RoutingVocab;
  readOnly: boolean;
  depth: number;
  onChange: (c: Condition) => void;
  onRemove?: () => void;
}) {
  if (cond.kind === "leaf") {
    return <LeafEditor leaf={cond} vocab={vocab} readOnly={readOnly} onChange={onChange} onRemove={onRemove} />;
  }
  if (cond.kind === "group") {
    return (
      <GroupEditor
        group={cond}
        vocab={vocab}
        readOnly={readOnly}
        depth={depth}
        onChange={onChange}
        onRemove={onRemove}
      />
    );
  }
  if (cond.kind === "raw") {
    return (
      <div className="flex items-center gap-1.5">
        <span className="text-[11px] text-warning">JSON を直接入力:</span>
        <input
          value={cond.json}
          disabled={readOnly}
          onChange={(e) => onChange({ kind: "raw", json: e.target.value })}
          className="min-w-[200px] flex-1 rounded border border-border-subtle bg-surface-2 px-1.5 py-0.5 font-mono text-[11px] text-fg"
        />
        <RemoveBtn readOnly={readOnly} onRemove={onRemove} />
      </div>
    );
  }
  // always
  return <div className="text-[11px] text-fg-subtle">すべての記事が該当</div>;
}

// root を編集用に group へ寄せる (leaf/always/raw を group(all) で包む)。
function coerceRootGroup(root: Condition): Extract<Condition, { kind: "group" }> {
  if (root.kind === "group") return root;
  if (root.kind === "always") return { kind: "group", mode: "all", children: [], negated: false };
  return { kind: "group", mode: "all", children: [root], negated: false };
}

export function RuleEditorFields({
  rule,
  vocab,
  readOnly,
  onChange,
}: {
  rule: EditRule;
  vocab: RoutingVocab;
  readOnly: boolean;
  onChange: (patch: Partial<EditRule>) => void;
}) {
  const channelMeta = useChannelMeta();
  const rootGroup = coerceRootGroup(rule.root);
  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <input
          value={rule.id}
          disabled={readOnly}
          onChange={(e) => onChange({ id: e.target.value })}
          className="min-w-0 flex-1 rounded border border-border-subtle bg-surface-1 px-2 py-1 text-sm text-fg"
          placeholder="ルール ID"
        />
        <span className="text-xs text-fg-subtle">→</span>
        <select
          value={rule.channel}
          disabled={readOnly}
          onChange={(e) => onChange({ channel: e.target.value })}
          className="rounded border border-border-subtle bg-surface-1 px-2 py-1 text-sm text-fg"
        >
          {vocab.channels.map((c) => (
            <option key={c} value={c}>
              {channelMeta(c).label}
            </option>
          ))}
        </select>
      </div>
      <div>
        <div className="mb-1 text-xs text-fg-subtle">
          条件 (項目を選ぶと内容に合った入力欄が出ます。否定は「でない」)
        </div>
        <ConditionEditor
          cond={rootGroup}
          vocab={vocab}
          readOnly={readOnly}
          depth={0}
          onChange={(c) => onChange({ root: c })}
        />
      </div>
    </div>
  );
}
