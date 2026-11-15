"""翻訳の前処理: 「そのまま残すべき行・URL」を LLM に渡さず、訳の後に原文どおり戻す。

背景 (2026-11-16、Opus 判定 120 窓): プロンプトで「URL・IoC・コードは維持」と指示しても、
URL だけの窓で本文を創作する・URL 行を落とす・URL の文字を書き換える重い誤りが出る。
指示ではなく構造で止める (CLAUDE.md §7 の 11-16 の行)。

- 保持行 (URL だけの行・コードの囲み・ハッシュ/IP/識別子だけの行・画像の行) の連なりは
  ``⟦K{n}⟧`` の 1 行に置き換えて LLM に渡し、訳の後で原文のまま戻す
- 散文の中の URL は ``⟦U{n}⟧`` に置き換え、訳の後で戻す
- 保持行・URL を除いた散文が少なければ LLM を呼ばず原文をそのまま訳とする
本文・訳文はログに出さない (件数のみ)。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

PASSTHROUGH_ENV = "TRANSLATE_RETAIN_PASSTHROUGH"

#: 保持行・URL を除いた散文の文字数がこれ未満なら LLM を呼ばない (URL だけの窓での創作を防ぐ)
PROSE_MIN_LETTERS = 40

#: トークンを LLM に残させるためのシステムプロンプトの追記 (トークンがある窓だけ)
PASSTHROUGH_SYSTEM_SUFFIX = (
    "\n- ⟦K1⟧ や ⟦U1⟧ のような ⟦…⟧ で囲まれたトークンは URL・コード等の置き場所の印。"
    "一字も変えず、削除・並べ替えもせずに出力の同じ位置へ残す"
)

_TOKEN_RE = re.compile(r"⟦[KU]\d+⟧")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]\s+|\d+[.)]\s+)")
_URL_LINE_RE = re.compile(r"^(?:(?:https?|hxxps?)://|www\.)\S+$", re.IGNORECASE)
_IMAGE_LINE_RE = re.compile(r"^!\[[^\]]*\]\([^)]*\)$")
_HASH_LINE_RE = re.compile(r"^[0-9a-fA-F]{32,}$")
_IP_LINE_RE = re.compile(r"^\d{1,3}(?:\[?\.\]?\d{1,3}){3}(?::\d+)?$")
_IDENT_MARK_RE = re.compile(r"[0-9./_:]")
_IDENT_MIN_CHARS = 6
_INLINE_URL_RE = re.compile(r"(?:https?|hxxps?)://[^\s<>\"'「」）)\]]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?、。"
_FENCE = "```"


def passthrough_mode() -> str:
    """``TRANSLATE_RETAIN_PASSTHROUGH`` (0 / shadow / 1、既定 0)。未知の値は 0。"""
    raw = os.environ.get(PASSTHROUGH_ENV, "0").strip().lower()
    if raw in ("1", "true", "on"):
        return "1"
    if raw == "shadow":
        return "shadow"
    return "0"


def is_retainable_line(line: str) -> bool:
    """1 行が、正しく訳しても原文のまま残る種類 (URL だけ・ハッシュ・IP・識別子・画像) か。"""
    stripped = _BULLET_RE.sub("", line).strip()
    if not stripped:
        return False
    if _URL_LINE_RE.match(stripped) or _IMAGE_LINE_RE.match(stripped):
        return True
    if _HASH_LINE_RE.match(stripped) or _IP_LINE_RE.match(stripped):
        return True
    single = len(stripped.split()) == 1 and stripped.isascii()
    return single and len(stripped) >= _IDENT_MIN_CHARS and bool(_IDENT_MARK_RE.search(stripped))


@dataclass(frozen=True)
class ProtectedChunk:
    """LLM へ渡す形 (``text``) と、戻すための保持行・URL。"""

    text: str
    runs: tuple[str, ...]  # K1.. の原文 (複数行は改行つき)
    inline_urls: tuple[tuple[int, str], ...]  # (段落の番号, URL)。U1.. の順
    prose_letters: int
    kept_lines: int

    @property
    def has_tokens(self) -> bool:
        return bool(self.runs or self.inline_urls)

    @property
    def should_skip_llm(self) -> bool:
        return self.has_tokens and self.prose_letters < PROSE_MIN_LETTERS


def _fence_end(lines: list[str], start: int) -> int:
    """``start`` の囲み開始行に対応する終了行の次の添字 (閉じが無ければ末尾)。"""
    for j in range(start + 1, len(lines)):
        if lines[j].strip().startswith(_FENCE):
            return j + 1
    return len(lines)


def _group_runs(lines: list[str]) -> list[tuple[bool, list[str]]]:
    """行を (保持か, 行の連なり) に分ける。囲みは 1 つの保持の連なり、空行は連なりを切る。"""
    groups: list[tuple[bool, list[str]]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith(_FENCE):
            end = _fence_end(lines, i)
            groups.append((True, lines[i:end]))
            i = end
            continue
        keep = is_retainable_line(line)
        if groups and groups[-1][0] == keep and keep:
            groups[-1][1].append(line)
        else:
            groups.append((keep, [line]))
        i += 1
    return groups


def _replace_inline_urls(line: str, para: int, urls: list[tuple[int, str]]) -> str:
    def repl(m: re.Match[str]) -> str:
        url = m.group(0)
        tail = ""
        while url and url[-1] in _TRAILING_PUNCT:
            tail = url[-1] + tail
            url = url[:-1]
        urls.append((para, url))
        return f"⟦U{len(urls)}⟧{tail}"

    return _INLINE_URL_RE.sub(repl, line)


def protect_chunk(chunk: str) -> ProtectedChunk:
    """チャンクの保持行の連なりと散文中の URL をトークンへ置き換える (純粋関数)。"""
    runs: list[str] = []
    urls: list[tuple[int, str]] = []
    out_lines: list[str] = []
    para = 0
    kept = 0
    prev_blank = False
    for keep, group in _group_runs(chunk.split("\n")):
        if keep:
            runs.append("\n".join(group))
            kept += len(group)
            out_lines.append(f"⟦K{len(runs)}⟧")
            prev_blank = False
            continue
        for line in group:
            blank = not line.strip()
            if blank and not prev_blank:
                para += 1
            prev_blank = blank
            out_lines.append(line if blank else _replace_inline_urls(line, para, urls))
    text = "\n".join(out_lines)
    letters = sum(c.isalpha() for c in _TOKEN_RE.sub("", text))
    return ProtectedChunk(text, tuple(runs), tuple(urls), letters, kept)


@dataclass(frozen=True)
class RestoreResult:
    text: str
    restored_runs: int  # 落ちていたため位置を推定して戻した保持行の連なりの数
    missing_urls: int  # 落ちていた散文中の URL の数


def _insert_missing_run_tokens(lines: list[str], total: int) -> tuple[list[str], int]:
    """出力に無い ``⟦K{n}⟧`` を、直前の番号のトークンの行の後 (無ければ先頭) へ足す。"""
    restored = 0
    for n in range(1, total + 1):
        token = f"⟦K{n}⟧"
        if any(token in ln for ln in lines):
            continue
        at = 0
        if n > 1:
            prev = f"⟦K{n - 1}⟧"
            idx = next((k for k, ln in enumerate(lines) if prev in ln), len(lines) - 1)
            at = idx + 1
        lines = [*lines[:at], token, *lines[at:]]
        restored += 1
    return lines, restored


def _swap_token(text: str, token: str, original: str) -> str:
    """最初の 1 つだけを原文へ戻し、重複して出たトークンは消す。"""
    head, sep, tail = text.partition(token)
    if not sep:
        return text
    return head + original + tail.replace(token, "")


def _append_missing_urls(text: str, protected: ProtectedChunk) -> tuple[str, int]:
    """戻らなかった散文中の URL を、元の段落に当たる出力の段落の末尾へ付け足す。"""
    paras = re.split(r"(\n\s*\n)", text)
    bodies = list(range(0, len(paras), 2))
    missing = 0
    for n, (para, url) in enumerate(protected.inline_urls, start=1):
        token = f"⟦U{n}⟧"
        if token in text:
            continue
        missing += 1
        if url in text:
            continue
        pos = bodies[min(para, len(bodies) - 1)]
        paras[pos] = f"{paras[pos]} {url}"
    return "".join(paras), missing


# モデルが崩したトークン ([K1]・【K1】・⟦ K1 ⟧・全角の Ｋ１ 等) を正しい形へ戻すための照合
_MANGLED_TOKEN_RE = re.compile(r"[\[【〚⟦<{（(]\s*([KUＫＵ])\s*([0-9０-９]+)\s*[\]】〛⟧>}）)]")


def normalize_mangled_tokens(text: str, runs: int, urls: int) -> str:
    """崩れたトークンを ``⟦K{n}⟧`` / ``⟦U{n}⟧`` に直す。

    番号が実在の範囲のものだけ直す (原文の [K1] 等の誤爆を避ける)。
    """

    def repl(m: re.Match[str]) -> str:
        kind = "K" if m.group(1) in "KＫ" else "U"
        n = int(m.group(2).translate(str.maketrans("０１２３４５６７８９", "0123456789")))
        limit = runs if kind == "K" else urls
        if m.group(0) == f"⟦{kind}{n}⟧" or not 1 <= n <= limit:
            return m.group(0)
        return f"⟦{kind}{n}⟧"

    return _MANGLED_TOKEN_RE.sub(repl, text)


def restore_chunk(protected: ProtectedChunk, translated: str) -> RestoreResult:
    """訳文のトークンを原文へ戻す。落ちた保持行は必ず戻し、落ちた URL は段落末尾へ足す。"""
    translated = normalize_mangled_tokens(
        translated, len(protected.runs), len(protected.inline_urls)
    )
    lines, restored = _insert_missing_run_tokens(translated.split("\n"), len(protected.runs))
    text = "\n".join(lines)
    for n, run in enumerate(protected.runs, start=1):
        text = _swap_token(text, f"⟦K{n}⟧", run)
    appended, missing = _append_missing_urls(text, protected)
    for n, (_, url) in enumerate(protected.inline_urls, start=1):
        appended = _swap_token(appended, f"⟦U{n}⟧", url)
    return RestoreResult(appended, restored, missing)


def count_lost_in_output(protected: ProtectedChunk, output: str) -> tuple[int, int]:
    """(出力に原文のまま無い保持行の連なり数, 無い URL 数)。shadow が旧経路の出力で数える。"""
    lost_runs = sum(1 for run in protected.runs if run not in output)
    lost_urls = sum(1 for _, url in protected.inline_urls if url not in output)
    return lost_runs, lost_urls
