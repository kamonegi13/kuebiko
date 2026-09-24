"""単体テスト共通の設定。

⚠⚠ **単体テストは本物の Ollama (ホストの 11434) に繋いではいけない** (2026-09-24)。
分析チャットのテストが本物の LLM を呼び、gemma4:26b (20GB) を載せた。GPU で学習中の
MLX (peak 78GB) と重なって Metal の OOM を起こし、n18 の学習が 25 反復で落ちた。
テストは「LLM 不在」を前提に書かれていたが、実際には LLM に頼って通っていた。
接続を構造で塞ぐ — 新しいテストが同じ穴を踏んでも、LLM 不在として振る舞う。
"""

from __future__ import annotations

import socket
from collections.abc import Iterator

import pytest

_OLLAMA_PORT = 11434


@pytest.fixture(autouse=True)
def _no_real_ollama(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    original = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> None:
        if isinstance(address, tuple) and len(address) >= 2 and address[1] == _OLLAMA_PORT:
            raise ConnectionRefusedError("単体テストから本物の Ollama へは繋がない")
        return original(self, address)  # type: ignore[arg-type]

    monkeypatch.setattr(socket.socket, "connect", guarded)
    yield
