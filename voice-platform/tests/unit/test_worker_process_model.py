"""LiveKit runs each job in a separate process in production and pickles the entrypoint and
setup function by qualified name. A closure here means no call can ever start (found by the
lab run of ``vp-worker start``)."""

from __future__ import annotations

import pickle

import pytest


def test_worker_callables_are_picklable_for_process_executor() -> None:
    from voice_platform.apps.worker.main import build_server

    server = build_server()
    for fn in (server._entrypoint_fnc, server._setup_fnc):
        assert fn is not None
        assert pickle.loads(pickle.dumps(fn)) is fn  # noqa: S301 - our own bytes


def test_worker_proxy_honours_no_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    from voice_platform.apps.worker.main import worker_http_proxy

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.corp:3128")
    monkeypatch.setenv("NO_PROXY", "localhost,127.0.0.1,.internal")
    monkeypatch.delenv("no_proxy", raising=False)
    assert worker_http_proxy("ws://127.0.0.1:7880") is None
    assert worker_http_proxy("wss://livekit.internal") is None
    assert worker_http_proxy("wss://example.livekit.cloud") == "http://proxy.corp:3128"
