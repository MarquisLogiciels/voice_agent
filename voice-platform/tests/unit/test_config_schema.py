from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from tests.conftest import sample_config
from voice_platform.config.diff import diff_configs
from voice_platform.config.schema import AgentConfig, config_hash
from voice_platform.config.service import canary_bucket


def _cfg(**kw: Any) -> AgentConfig:
    return AgentConfig.model_validate(sample_config(**kw))


def test_sample_config_is_valid() -> None:
    cfg = _cfg()
    assert cfg.pipeline.stt and cfg.pipeline.stt.provider == "deepgram"
    assert cfg.conversation.context.max_items >= cfg.conversation.context.summarize_when_items_exceed


def test_cascaded_requires_components() -> None:
    data = sample_config()
    del data["pipeline"]["tts"]
    with pytest.raises(ValidationError, match="requires tts"):
        AgentConfig.model_validate(data)


def test_realtime_mode_rejects_cascaded_components() -> None:
    data = sample_config()
    data["pipeline"] = {
        "mode": "realtime",
        "realtime": {"provider": "openai-realtime", "model": "gpt-realtime", "voice": "marin"},
        "llm": {"provider": "openai"},
        "turn_detection": {"mode": "realtime_llm"},
    }
    with pytest.raises(ValidationError, match="must not define stt/llm"):
        AgentConfig.model_validate(data)
    del data["pipeline"]["llm"]
    cfg = AgentConfig.model_validate(data)
    assert cfg.pipeline.realtime is not None


def test_tools_require_bindings() -> None:
    with pytest.raises(ValidationError, match="integrations.crm"):
        _cfg(tools={"enabled": ["lookup_customer"]})
    cfg = _cfg(tools={"enabled": ["lookup_customer"]}, integrations={"crm": "hubspot"})
    assert "lookup_customer" in cfg.tools.enabled
    with pytest.raises(ValidationError, match="unknown tool"):
        _cfg(tools={"enabled": ["rm_rf"]})


def test_custom_tool_must_reference_bound_integration() -> None:
    with pytest.raises(ValidationError, match="custom tool"):
        _cfg(tools={"enabled": ["custom.crm.lookup"]})
    cfg = _cfg(tools={"enabled": ["custom.crm.lookup"]}, integrations={"custom": ["crm"]})
    assert cfg.tools.enabled == ["custom.crm.lookup"]


def test_transfer_validation() -> None:
    base = {"enabled": True, "destinations": [
        {"name": "front", "phone_number": "+15145550100", "fallback": ["billing"]},
        {"name": "billing", "phone_number": "+15145550101", "fallback": ["front"]},
    ]}
    with pytest.raises(ValidationError, match="cycle"):
        _cfg(transfer=base)
    base["destinations"][1]["fallback"] = []
    cfg = _cfg(transfer=base, tools={"enabled": ["transfer_to_human"]})
    assert cfg.transfer.destinations[0].fallback == ["billing"]
    with pytest.raises(ValidationError, match="exactly one"):
        _cfg(transfer={"enabled": True, "destinations": [{"name": "x"}]})
    with pytest.raises(ValidationError, match="transfer.enabled"):
        _cfg(tools={"enabled": ["transfer_to_human"]})


def test_business_hours_validation() -> None:
    with pytest.raises(ValidationError):
        _cfg(business={"hours": {"schedule": {"mon": ["17:00-09:00"]}}})
    with pytest.raises(ValidationError):
        _cfg(business={"timezone": "Mars/Olympus"})


def test_environment_override_materializes_and_is_validated() -> None:
    cfg = _cfg(environment_overrides={"staging": {"pipeline": {"llm": {"model": "gpt-4.1-nano"}}}})
    staging = cfg.materialize("staging")
    assert staging.pipeline.llm and staging.pipeline.llm.model == "gpt-4.1-nano"
    assert cfg.materialize("prod").pipeline.llm.model == "gpt-4.1-mini"  # type: ignore[union-attr]
    with pytest.raises(ValidationError, match="override 'staging' is invalid"):
        _cfg(environment_overrides={"staging": {"pipeline": {"tts": None}}})


def test_config_hash_is_stable() -> None:
    a = _cfg().model_dump(mode="json")
    b = dict(reversed(list(a.items())))
    assert config_hash(a) == config_hash(b)


def test_diff() -> None:
    a = sample_config()
    b = sample_config(instructions="New instructions", pipeline={"llm": {"model": "x"}})
    paths = {c.path for c in diff_configs(a, b)}
    assert paths == {"instructions", "pipeline.llm.model"}


def test_canary_bucket_is_stable_and_uniformish() -> None:
    assert canary_bucket("call-1") == canary_bucket("call-1")
    buckets = [canary_bucket(f"call-{i}") for i in range(4000)]
    share = sum(b < 10 for b in buckets) / len(buckets)
    assert 0.08 < share < 0.12
    assert all(0 <= b < 100 for b in buckets)
