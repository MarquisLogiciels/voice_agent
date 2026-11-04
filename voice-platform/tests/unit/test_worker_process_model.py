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


def test_turn_detector_runners_are_registered_in_the_main_process() -> None:
    """LiveKit's inference process only runs models whose plugin was imported in the main
    process before the server starts (audit F6)."""
    from livekit.agents.inference_runner import _InferenceRunner

    from voice_platform.apps.worker.main import register_main_process_plugins

    assert not register_main_process_plugins(False)
    assert register_main_process_plugins(True)
    names = set(_InferenceRunner.registered_runners)
    assert {"lk_end_of_utterance_en", "lk_end_of_utterance_multilingual"} <= names, names


def test_model_turn_detection_falls_back_when_disabled_on_the_worker() -> None:
    from tests.conftest import sample_config
    from voice_platform.config.schema import AgentConfig
    from voice_platform.providers.builder import PipelineBuilder
    from voice_platform.providers.registry import default_registry
    from voice_platform.providers.types import BuildContext

    cfg = AgentConfig.model_validate(sample_config())  # turn_detection mode=model
    builder = PipelineBuilder(default_registry())
    out_td = builder._turn_detection(
        cfg.pipeline, BuildContext(env={"VP_WORKER_TURN_DETECTOR": "false"}), out=None
    )
    assert out_td == "vad"  # sample config has Silero VAD
