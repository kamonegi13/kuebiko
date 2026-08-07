// 接続セットアップ (設定 → 接続タブ、既定タブ)。
// 初期設定・復旧時のワンストップ: 接続を持つ設定 (Discord webhook / Grok メール /
// LLM 接続 / tunnel) をこの 1 画面で埋められる。SSoT は API + .env (保存層) で、
// 各対象画面 (情報フロー/購読ソース/モデルタブ/ジョブ管理) と同一コンポーネント・
// 同一 API を再掲しているだけ — 二重管理は発生しない。運用時の文脈編集は各画面に残す。

import { useQuery } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { channelsApi } from "../../api/channels";
import { grokMailApi } from "../../api/grokMail";
import { pagesApi } from "../../api/pages";
import { channelColor } from "../../components/channel";
import { GrokMailCard } from "../../components/GrokMailCard";
import { MobileTunnelCard } from "../../components/MobileTunnelCard";
import { AccessStatusLine } from "../../components/AccessAuditCard";
import { ChannelWebhookField } from "../channels/ChannelWebhookField";

function SetupChip({ label, ok, detail }: { label: string; ok: boolean | null; detail: string }) {
  const tone =
    ok === null ? "text-fg-subtle" : ok ? "text-success" : "text-warning";
  const dot = ok === null ? "bg-surface-3" : ok ? "bg-success" : "bg-warning";
  return (
    <div className="flex items-center gap-1.5 rounded bg-surface-2 px-2.5 py-1.5">
      <span className={`inline-block h-2 w-2 rounded-full ${dot}`} />
      <span className="text-xs font-semibold text-fg">{label}</span>
      <span className={`text-[11px] ${tone}`}>{detail}</span>
    </div>
  );
}

export function ConnectionsPanel({ onOpenModels }: { onOpenModels: () => void }) {
  const { data: reg } = useQuery({ queryKey: ["channels"], queryFn: channelsApi.get });
  const { data: chHealth } = useQuery({
    queryKey: ["channels-health"],
    queryFn: channelsApi.health,
    refetchInterval: 120_000,
  });
  const { data: grokMail } = useQuery({ queryKey: ["grok-mail"], queryFn: grokMailApi.get });
  // ollama 疎通/外部 LLM の状況はモデルタブと同じ API から (編集はモデルタブで)
  const { data: tiers } = useQuery({ queryKey: ["model-tiers"], queryFn: pagesApi.getModelTiers });

  const channels = reg?.channels ?? [];
  const enabledChannels = channels.filter((c) => c.enabled);
  const unsetWebhooks = enabledChannels.filter((c) => reg?.webhook_set?.[c.id] === false);

  return (
    <div className="space-y-4">
      {/* セットアップ状況サマリ */}
      <div className="flex flex-wrap items-center gap-2">
        <SetupChip
          label="Discord webhook"
          ok={reg ? unsetWebhooks.length === 0 : null}
          detail={
            reg
              ? unsetWebhooks.length === 0
                ? `${enabledChannels.length} ch 設定済`
                : `未設定 ${unsetWebhooks.length} / ${enabledChannels.length} ch`
              : "確認中…"
          }
        />
        <SetupChip
          label="Grok メール (IMAP)"
          ok={grokMail ? grokMail.configured : null}
          detail={grokMail ? (grokMail.configured ? "設定済" : "未設定") : "確認中…"}
        />
        <SetupChip
          label="Ollama"
          ok={tiers ? !tiers.ollama_error : null}
          detail={tiers ? (tiers.ollama_error ? "接続不可" : "稼働中") : "確認中…"}
        />
        <SetupChip
          label="外部 LLM"
          ok={tiers ? true : null}
          detail={tiers ? (tiers.external_enabled ? "キー設定済" : "未設定 (任意)") : "確認中…"}
        />
      </div>
      <p className="m-0 text-[11px] text-fg-subtle">
        「外部と繋ぐ」設定だけをここに集約しています。値の保存先は .env (即時反映・常にマスク表示)。
        同じ設定は各対象画面 (情報フロー / 購読ソース / モデルタブ / ジョブ管理) にもあり、
        どちらで編集しても同じです。
      </p>

      {/* 1. Discord 配信先 (チャンネル別 webhook) */}
      <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-3">
        <div className="flex items-baseline justify-between flex-wrap gap-2">
          <h3 className="m-0 text-md font-semibold text-fg">Discord 配信先 (チャンネル別 webhook)</h3>
          <a href="/app/flow" className="inline-flex items-center gap-1 text-[11px] text-accent no-underline hover:underline">
            チャンネルの追加・配信設定は情報フローで <ExternalLink className="h-3 w-3" />
          </a>
        </div>
        {!reg && <div className="text-sm text-fg-muted">読み込み中...</div>}
        <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
          {channels.map((ch) => (
            <div key={ch.id} className={ch.enabled ? "" : "opacity-60"}>
              <div className="mb-1 flex items-center gap-1.5 text-xs font-semibold" style={{ color: channelColor(ch.id) }}>
                {ch.label || ch.id}
                {!ch.enabled && <span className="text-[10px] font-normal text-fg-subtle">(無効)</span>}
              </div>
              <ChannelWebhookField
                channelId={ch.id}
                masked={reg?.webhook_masked?.[ch.id] ?? ""}
                health={chHealth?.checks?.[ch.id]}
                readOnly={false}
              />
            </div>
          ))}
        </div>
      </div>

      {/* 2. Grok メール受信 (IMAP) — 購読ソース画面と同一カード */}
      <GrokMailCard readOnly={false} />

      {/* 3. LLM 接続 (Ollama / 外部) — 編集はモデルタブ (ティア割当と一体のため) */}
      <div className="bg-surface-1 border border-border-subtle rounded-lg p-4 space-y-2">
        <div className="flex items-baseline justify-between flex-wrap gap-2">
          <h3 className="m-0 text-md font-semibold text-fg">LLM 接続 (Ollama / 外部)</h3>
          <button
            onClick={onOpenModels}
            className="inline-flex items-center gap-1 text-[11px] text-accent hover:underline"
          >
            モデルタブで管理 (URL 変更・キー設定・ティア割当) <ExternalLink className="h-3 w-3" />
          </button>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs text-fg-muted">
          <span
            className={`inline-block h-2 w-2 rounded-full ${tiers ? (tiers.ollama_error ? "bg-critical" : "bg-success") : "bg-surface-3"}`}
            title={tiers?.ollama_error ?? "稼働中"}
          />
          <span>
            ollama: <code className="text-fg">{tiers?.ollama_base_url ?? "…"}</code>
            {tiers && !tiers.ollama_error && ` — モデル ${tiers.available.length} 件`}
            {tiers?.ollama_error && " — 接続できません"}
          </span>
        </div>
        <p className="m-0 text-[11px] text-fg-subtle">
          既定はローカル Ollama で完結。外部 LLM (Anthropic / OpenAI 互換 / Claude Code) は
          任意 — キーを設定しティアに明示割当した処理だけが外部に出ます。
        </p>
      </div>

      {/* 4. モバイル公開 tunnel */}
      <MobileTunnelCard readOnly={false} />

      {/* 5. 公開面の認証 — 「何を公開するか」の直後に「誰が入れるか」。状態のみで、
          アクセス履歴は「履歴・監査」タブが持つ (設定と記録は別種のものとして分離)。 */}
      <AccessStatusLine />
    </div>
  );
}
