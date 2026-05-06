import asyncio
import importlib
import importlib.util
import logging
import sys
import textwrap
import types
from pathlib import Path

import pytest


HOOK_ROOT = Path(__file__).resolve().parents[1]
IMPORT_FINDER_ATTR = "_gateway_event_filter_import_finder"


def _remove_import_finders():
    sys.meta_path[:] = [
        finder for finder in sys.meta_path
        if not getattr(finder, IMPORT_FINDER_ATTR, False)
    ]


@pytest.fixture(autouse=True)
def cleanup_import_finders():
    _remove_import_finders()
    yield
    _remove_import_finders()
    sys.modules.pop("run_agent", None)
    sys.modules.pop("hermes.agent.run", None)
    sys.modules.pop("hermes.agent.run_agent", None)


def load_hook(monkeypatch, hermes_home=None, *, skip_import_bootstrap=True):
    _remove_import_finders()
    sys.modules.pop("run_agent", None)
    if hermes_home is None:
        monkeypatch.delenv("HERMES_HOME", raising=False)
    else:
        monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    if skip_import_bootstrap:
        monkeypatch.setenv("HERMES_HOOK_SKIP_IMPORT_BOOTSTRAP", "true")
    else:
        monkeypatch.delenv("HERMES_HOOK_SKIP_IMPORT_BOOTSTRAP", raising=False)
    module_name = "gateway_event_filter_hook_under_test"
    sys.modules.pop(module_name, None)
    spec = importlib.util.spec_from_file_location(module_name, HOOK_ROOT / "handler.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module._reload_config()
    return module


def test_agent_callbacks_and_empty_lifecycle_are_source_aware(monkeypatch):
    hook = load_hook(monkeypatch)
    seen = []

    class AIAgent:
        def __init__(self, platform="discord"):
            self.platform = platform
            self.background_review_callback = lambda *args: seen.append(("background", platform, args))
            self.tool_progress_callback = lambda *args: seen.append(("tool", platform, args))
            self.interim_assistant_callback = lambda *args: seen.append(("interim", platform, args))
            self.status_callback = lambda event_type, message: seen.append(("status", platform, event_type, message))

        def _emit_status(self, message):
            self.status_callback("lifecycle", message)

        def run_conversation(self):
            return {"final_response": "(empty)", "kept": True}

    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=AIAgent))
    assert hook._patch_aiagent() is True

    discord_agent = AIAgent("discord")
    discord_agent.background_review_callback("User profile updated")
    discord_agent.tool_progress_callback("tool.started")
    discord_agent.interim_assistant_callback("preview")
    discord_agent._emit_status("Model returned empty after tool calls - nudging to continue")
    discord_agent._emit_status("ordinary lifecycle")

    slack_agent = AIAgent("slack")
    slack_agent.background_review_callback("User profile updated")
    slack_result = slack_agent.run_conversation()
    cli_agent = AIAgent("cli")
    cli_agent.background_review_callback("User profile updated")
    cli_result = cli_agent.run_conversation()
    local_agent = AIAgent("local")
    local_agent.background_review_callback("User profile updated")
    local_result = local_agent.run_conversation()

    assert ("background", "discord", ("User profile updated",)) not in seen
    assert ("tool", "discord", ("tool.started",)) in seen
    assert ("interim", "discord", ("preview",)) in seen
    assert ("status", "discord", "lifecycle", "ordinary lifecycle") in seen
    assert all("Model returned empty after tool calls" not in str(item) for item in seen)
    assert discord_agent.run_conversation() == {
        "final_response": "",
        "kept": True,
        "gateway_event_filter_suppressed": ["empty_final_warning"],
    }
    assert ("background", "slack", ("User profile updated",)) not in seen
    assert slack_result == {
        "final_response": "",
        "kept": True,
        "gateway_event_filter_suppressed": ["empty_final_warning"],
    }
    assert ("background", "cli", ("User profile updated",)) in seen
    assert cli_result == {"final_response": "(empty)", "kept": True}
    assert ("background", "local", ("User profile updated",)) in seen
    assert local_result == {"final_response": "(empty)", "kept": True}


def test_default_suppression_only_covers_gateway_noise(monkeypatch):
    hook = load_hook(monkeypatch)

    assert hook._CONFIG["suppress"] == {
        "suppress_empty_final_warning": True,
        "suppress_busy_ack_notice": True,
        "suppress_background_review_notice": True,
    }


def test_platforms_can_scope_suppression_when_configured(monkeypatch):
    hook = load_hook(monkeypatch)
    hook._CONFIG = {
        "platforms": {"discord"},
        "suppress": dict(hook._DEFAULT_SUPPRESS),
    }
    seen = []

    class AIAgent:
        def __init__(self, platform):
            self.platform = platform
            self.background_review_callback = lambda *args: seen.append(("background", platform, args))

        def _emit_status(self, message):
            seen.append(("status", self.platform, message))

        def run_conversation(self):
            return {"final_response": "(empty)"}

    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=AIAgent))
    assert hook._patch_aiagent() is True

    discord_agent = AIAgent("discord")
    slack_agent = AIAgent("slack")
    discord_agent.background_review_callback("User profile updated")
    slack_agent.background_review_callback("User profile updated")

    assert ("background", "discord", ("User profile updated",)) not in seen
    assert ("background", "slack", ("User profile updated",)) in seen
    assert discord_agent.run_conversation()["final_response"] == ""
    assert slack_agent.run_conversation()["final_response"] == "(empty)"


def test_config_yaml_overrides_defaults(monkeypatch, tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
gateway_event_filter:
  platforms:
    - discord
  suppress:
    suppress_empty_final_warning: false
    suppress_busy_ack_notice: true
    suppress_background_review_notice: true
""".lstrip(),
        encoding="utf-8",
    )
    hook = load_hook(monkeypatch, hermes_home=tmp_path)

    assert hook._CONFIG == {
        "platforms": {"discord"},
        "suppress": {
            "suppress_empty_final_warning": False,
            "suppress_busy_ack_notice": True,
            "suppress_background_review_notice": True,
        },
    }

def test_gateway_run_agent_normalizes_positional_source(monkeypatch):
    hook = load_hook(monkeypatch)

    class Source:
        platform = "discord"

    class GatewayRunner:
        async def _run_agent(self, message, context_prompt, history, source, session_id):
            return {"final_response": "(empty)", "session_id": session_id}

        async def _handle_active_session_busy_message(self, event, session_key):
            return True

    monkeypatch.setitem(sys.modules, "gateway.run", types.SimpleNamespace(GatewayRunner=GatewayRunner))
    assert hook._patch_gateway_runner() is True

    result = asyncio.run(GatewayRunner()._run_agent("msg", "ctx", [], Source(), "s1"))

    assert result == {
        "final_response": "",
        "session_id": "s1",
        "gateway_event_filter_suppressed": ["empty_final_warning"],
    }


def test_busy_ack_suppresses_only_busy_ack_send(monkeypatch):
    hook = load_hook(monkeypatch)
    sent = []

    class Source:
        platform = "discord"
        chat_id = "chat"
        thread_id = None

    class Event:
        source = Source()
        message_id = "message"

    class SendResult:
        def __init__(self, success=True):
            self.success = success

    class BasePlatformAdapter:
        async def _send_with_retry(self, content, **kwargs):
            sent.append(content)
            return types.SimpleNamespace(success=True)

    class Adapter(BasePlatformAdapter):
        platform = "discord"

    class GatewayRunner:
        def __init__(self):
            self.adapters = {"discord": Adapter()}

        async def _run_agent(self, source=None):
            return {"final_response": "ok"}

        async def _handle_active_session_busy_message(self, event, session_key):
            adapter = self.adapters[event.source.platform]
            await adapter._send_with_retry(content="Interrupting current task. I will respond shortly.")
            await adapter._send_with_retry(content="ordinary busy-handler message")
            return True

    monkeypatch.setitem(
        sys.modules,
        "gateway.platforms.base",
        types.SimpleNamespace(BasePlatformAdapter=BasePlatformAdapter, SendResult=SendResult),
    )
    monkeypatch.setitem(sys.modules, "gateway.run", types.SimpleNamespace(GatewayRunner=GatewayRunner))
    assert hook._patch_base_adapter() is True
    assert hook._patch_gateway_runner() is True

    async def run_busy_events():
        runner = GatewayRunner()
        first, second = await asyncio.gather(
            runner._handle_active_session_busy_message(Event(), "session-1"),
            runner._handle_active_session_busy_message(Event(), "session-2"),
        )
        await runner.adapters["discord"]._send_with_retry(
            content="Interrupting current task outside the busy handler."
        )
        await runner.adapters["discord"]._send_with_retry(
            content=(
                "Ordinary reply quoting: The model returned no response after processing tool results. "
                "Please do not hide this."
            )
        )
        await runner.adapters["discord"]._send_with_retry(
            content=(
                "\u26a0\ufe0f The model returned no response after processing tool results. "
                "This can happen with some models \u2014 try again or rephrase your question."
            )
        )
        await runner.adapters["discord"]._send_with_retry(
            content="ordinary final response"
        )
        return first, second

    result = asyncio.run(run_busy_events())

    assert result == (True, True)
    assert sent == [
        "ordinary busy-handler message",
        "ordinary busy-handler message",
        "Interrupting current task outside the busy handler.",
        (
            "Ordinary reply quoting: The model returned no response after processing tool results. "
            "Please do not hide this."
        ),
        "ordinary final response",
    ]


def test_import_hook_patches_late_run_agent_import(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch)
    run_agent_path = tmp_path / "run_agent.py"
    run_agent_path.write_text(
        textwrap.dedent(
            """
            seen = []

            class AIAgent:
                def __init__(self, platform="discord"):
                    self.platform = platform
                    self.background_review_callback = lambda *args: seen.append(args)

                def _emit_status(self, message):
                    seen.append(("status", message))

                def run_conversation(self):
                    return {"final_response": "(empty)"}
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    assert hook._patch_aiagent() is False
    assert hook._AGENT_PATCH_PENDING is True
    imported = importlib.import_module("run_agent")

    agent = imported.AIAgent("discord")
    agent.background_review_callback("User profile updated")
    agent._emit_status("Thinking-only response - prefilling to continue")

    assert imported.seen == []
    assert agent.run_conversation() == {
        "final_response": "",
        "gateway_event_filter_suppressed": ["empty_final_warning"],
    }


def test_import_hook_uses_pathfinder_without_chaining_meta_finders(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch)
    run_agent_path = tmp_path / "run_agent.py"
    run_agent_path.write_text(
        textwrap.dedent(
            """
            class AIAgent:
                def __init__(self, platform="discord"):
                    self.platform = platform
                    self.background_review_callback = lambda *args: None

                def _emit_status(self, message):
                    return message

                def run_conversation(self):
                    return {"final_response": "(empty)"}
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    class SentinelFinder:
        def find_spec(self, fullname, path=None, target=None):
            if fullname != "run_agent":
                return None
            raise AssertionError("AIAgent import hook must not delegate to other meta path finders")

    assert hook._patch_aiagent() is False
    original_meta_path = list(sys.meta_path)
    try:
        sys.meta_path.insert(1, SentinelFinder())
        imported = importlib.import_module("run_agent")
    finally:
        sys.meta_path[:] = original_meta_path

    assert getattr(imported.AIAgent.run_conversation, hook._PATCH_ATTR, False) is True


def test_aiagent_auto_discovery_patches_non_run_agent_module(monkeypatch):
    hook = load_hook(monkeypatch)
    seen = []

    class AIAgent:
        def __init__(self, platform="discord"):
            self.platform = platform
            self.background_review_callback = lambda *args: seen.append(args)

        def _emit_status(self, message):
            seen.append(("status", message))

        def run_conversation(self):
            return {"final_response": "(empty)"}

    monkeypatch.setitem(sys.modules, "hermes.agent.run", types.SimpleNamespace(AIAgent=AIAgent))

    assert hook._patch_aiagent() is True
    assert hook._AGENT_PATCH_PENDING is False
    assert getattr(AIAgent.__setattr__, hook._PATCH_ATTR, False) is True
    assert getattr(AIAgent._emit_status, hook._PATCH_ATTR, False) is True
    assert getattr(AIAgent.run_conversation, hook._PATCH_ATTR, False) is True

    agent = AIAgent()
    agent.background_review_callback("User profile updated")
    assert seen == []
    assert agent.run_conversation()["final_response"] == ""


def test_aiagent_auto_discovery_deduplicates_same_class(monkeypatch):
    hook = load_hook(monkeypatch)

    class AIAgent:
        def __init__(self, platform="discord"):
            self.platform = platform
            self.background_review_callback = lambda *args: None

        def _emit_status(self, message):
            return message

        def run_conversation(self):
            return {"final_response": "(empty)"}

    module_a = types.SimpleNamespace(AIAgent=AIAgent)
    module_b = types.SimpleNamespace(AIAgent=AIAgent)
    monkeypatch.setitem(sys.modules, "run_agent", module_a)
    monkeypatch.setitem(sys.modules, "hermes.agent.run_agent", module_b)

    targets = hook._iter_aiagent_patch_targets()

    assert len(targets) == 1
    assert hook._patch_aiagent() is True
    assert hook._AGENT_PATCH_PENDING is False


def test_aiagent_auto_discovery_ignores_unrelated_module(monkeypatch):
    hook = load_hook(monkeypatch)
    seen = []

    class AIAgent:
        def __init__(self, platform="discord"):
            self.platform = platform
            self.background_review_callback = lambda *args: seen.append(args)

        def _emit_status(self, message):
            seen.append(("status", message))

        def run_conversation(self):
            return {"final_response": "(empty)"}

    monkeypatch.setitem(sys.modules, "unrelated.module", types.SimpleNamespace(AIAgent=AIAgent))

    assert hook._patch_aiagent() is False
    assert hook._AGENT_PATCH_PENDING is True

    agent = AIAgent()
    agent.background_review_callback("User profile updated")
    assert seen == [("User profile updated",)]
    assert agent.run_conversation()["final_response"] == "(empty)"


def test_aiagent_auto_discovery_ignores_non_hermes_run_agent_module(monkeypatch):
    hook = load_hook(monkeypatch)
    seen = []

    class AIAgent:
        def __init__(self, platform="discord"):
            self.platform = platform
            self.background_review_callback = lambda *args: seen.append(args)

        def _emit_status(self, message):
            seen.append(("status", message))

        def run_conversation(self):
            return {"final_response": "(empty)"}

    module = types.ModuleType("thirdparty.run_agent")
    module.__file__ = "C:/vendor/thirdparty/run_agent.py"
    module.AIAgent = AIAgent
    monkeypatch.setitem(sys.modules, "thirdparty.run_agent", module)

    assert hook._patch_aiagent() is False
    assert hook._AGENT_PATCH_PENDING is True

    agent = AIAgent()
    agent.background_review_callback("User profile updated")
    assert seen == [("User profile updated",)]
    assert agent.run_conversation()["final_response"] == "(empty)"


def test_aiagent_auto_discovery_accepts_hermes_run_agent_path(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch)
    seen = []

    class AIAgent:
        def __init__(self, platform="discord"):
            self.platform = platform
            self.background_review_callback = lambda *args: seen.append(args)

        def _emit_status(self, message):
            seen.append(("status", message))

        def run_conversation(self):
            return {"final_response": "(empty)"}

    module = types.ModuleType("renamed.agent.run_agent")
    module.__file__ = str(tmp_path / "hermes-agent" / "run_agent.py")
    module.AIAgent = AIAgent
    monkeypatch.setitem(sys.modules, "renamed.agent.run_agent", module)

    assert hook._patch_aiagent() is True
    assert hook._AGENT_PATCH_PENDING is False

    agent = AIAgent()
    agent.background_review_callback("User profile updated")
    assert seen == []
    assert agent.run_conversation()["final_response"] == ""


def test_startup_fallback_patches_gateway_and_adapter(monkeypatch):
    hook = load_hook(monkeypatch)

    class BasePlatformAdapter:
        async def _send_with_retry(self, content):
            return types.SimpleNamespace(success=True, content=content)

    class GatewayRunner:
        async def _run_agent(self, source=None):
            return {"final_response": "(empty)"}

        async def _handle_active_session_busy_message(self, event, session_key):
            return True

    monkeypatch.setitem(sys.modules, "gateway.platforms.base", types.SimpleNamespace(BasePlatformAdapter=BasePlatformAdapter))
    monkeypatch.setitem(sys.modules, "gateway.run", types.SimpleNamespace(GatewayRunner=GatewayRunner))

    asyncio.run(hook.handle("gateway:startup", {"platforms": ["discord"]}))

    assert getattr(BasePlatformAdapter._send_with_retry, hook._PATCH_ATTR, False) is True
    assert getattr(GatewayRunner._run_agent, hook._PATCH_ATTR, False) is True


def test_gateway_runner_auto_discovery_ignores_unrelated_class(monkeypatch):
    hook = load_hook(monkeypatch)

    class GatewayRunner:
        async def _run_agent(self, source=None):
            return {"final_response": "(empty)"}

        async def _handle_active_session_busy_message(self, event, session_key):
            return True

    monkeypatch.setitem(sys.modules, "unrelated.gateway_runner", types.SimpleNamespace(GatewayRunner=GatewayRunner))

    assert hook._patch_gateway_runner() is False
    assert getattr(GatewayRunner._run_agent, hook._PATCH_ATTR, False) is False


def test_gateway_runner_main_module_requires_gateway_run_file(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch)

    class GatewayRunner:
        async def _run_agent(self, source=None):
            return {"final_response": "(empty)"}

        async def _handle_active_session_busy_message(self, event, session_key):
            return True

    module = types.ModuleType("__main__")
    module.__file__ = str(tmp_path / "run.py")
    module.GatewayRunner = GatewayRunner
    monkeypatch.setitem(sys.modules, "__main__", module)

    assert hook._patch_gateway_runner() is False
    assert getattr(GatewayRunner._run_agent, hook._PATCH_ATTR, False) is False


def test_gateway_runner_main_gateway_run_file_is_candidate(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch)

    class GatewayRunner:
        async def _run_agent(self, source=None):
            return {"final_response": "(empty)"}

        async def _handle_active_session_busy_message(self, event, session_key):
            return True

    module = types.ModuleType("__main__")
    module.__file__ = str(tmp_path / "gateway" / "run.py")
    module.GatewayRunner = GatewayRunner
    monkeypatch.setitem(sys.modules, "__main__", module)

    assert hook._patch_gateway_runner() is True
    assert getattr(GatewayRunner._run_agent, hook._PATCH_ATTR, False) is True


def test_base_adapter_auto_discovery_ignores_unrelated_class(monkeypatch):
    hook = load_hook(monkeypatch)

    class BasePlatformAdapter:
        def _send_with_retry(self, content):
            return types.SimpleNamespace(success=True, content=content)

    monkeypatch.setitem(
        sys.modules,
        "unrelated.base_adapter",
        types.SimpleNamespace(BasePlatformAdapter=BasePlatformAdapter),
    )

    assert hook._patch_base_adapter() is False
    assert getattr(BasePlatformAdapter._send_with_retry, hook._PATCH_ATTR, False) is False


def test_base_adapter_auto_discovery_ignores_non_gateway_platforms_base(monkeypatch):
    hook = load_hook(monkeypatch)

    class BasePlatformAdapter:
        def _send_with_retry(self, content):
            return types.SimpleNamespace(success=True, content=content)

    module = types.ModuleType("thirdparty.platforms.base")
    module.__file__ = "C:/vendor/thirdparty/platforms/base.py"
    module.BasePlatformAdapter = BasePlatformAdapter
    monkeypatch.setitem(sys.modules, "thirdparty.platforms.base", module)

    assert hook._patch_base_adapter() is False
    assert getattr(BasePlatformAdapter._send_with_retry, hook._PATCH_ATTR, False) is False


def test_base_adapter_auto_discovery_accepts_gateway_platforms_path(monkeypatch, tmp_path):
    hook = load_hook(monkeypatch)

    class BasePlatformAdapter:
        def _send_with_retry(self, content):
            return types.SimpleNamespace(success=True, content=content)

    module = types.ModuleType("renamed.platforms.base")
    module.__file__ = str(tmp_path / "gateway" / "platforms" / "base.py")
    module.BasePlatformAdapter = BasePlatformAdapter
    monkeypatch.setitem(sys.modules, "renamed.platforms.base", module)

    assert hook._patch_base_adapter() is True
    assert getattr(BasePlatformAdapter._send_with_retry, hook._PATCH_ATTR, False) is True


def test_skip_import_bootstrap_env_prevents_import_side_effect(monkeypatch):
    load_hook(monkeypatch, skip_import_bootstrap=True)

    assert not any(getattr(finder, IMPORT_FINDER_ATTR, False) for finder in sys.meta_path)


def test_import_hook_install_is_idempotent(monkeypatch):
    hook = load_hook(monkeypatch)

    assert hook._install_aiagent_import_hook() is True
    first_count = sum(1 for finder in sys.meta_path if getattr(finder, IMPORT_FINDER_ATTR, False))
    assert hook._install_aiagent_import_hook() is True
    second_count = sum(1 for finder in sys.meta_path if getattr(finder, IMPORT_FINDER_ATTR, False))

    assert first_count == 1
    assert second_count == 1


def test_successful_send_result_falls_back_when_sendresult_import_fails(monkeypatch):
    hook = load_hook(monkeypatch)
    monkeypatch.setitem(sys.modules, "gateway.platforms.base", None)

    result = hook._successful_send_result()

    assert result.success is True
    assert result.message_id is None
    assert result.error is None
    assert result.retryable is False


def test_base_adapter_wrapper_supports_sync_send(monkeypatch):
    hook = load_hook(monkeypatch)

    class BasePlatformAdapter:
        platform = "discord"

        def _send_with_retry(self, content):
            return types.SimpleNamespace(success=True, content=content)

    monkeypatch.setitem(
        sys.modules,
        "gateway.platforms.base",
        types.SimpleNamespace(BasePlatformAdapter=BasePlatformAdapter),
    )

    assert hook._patch_base_adapter() is True
    result = asyncio.run(BasePlatformAdapter()._send_with_retry("ordinary final response"))

    assert result.success is True
    assert result.content == "ordinary final response"


def test_gateway_runner_patch_requires_both_targets(monkeypatch, caplog):
    hook = load_hook(monkeypatch)

    class GatewayRunner:
        async def _run_agent(self, source=None):
            return {"final_response": "ok"}

    monkeypatch.setitem(sys.modules, "gateway.run", types.SimpleNamespace(GatewayRunner=GatewayRunner))

    with caplog.at_level(logging.WARNING):
        assert hook._patch_gateway_runner() is False

    assert "_handle_active_session_busy_message is not available" in caplog.text


def test_aiagent_patch_requires_all_targets(monkeypatch, caplog):
    hook = load_hook(monkeypatch)

    class AIAgent:
        def run_conversation(self):
            return {"final_response": "ok"}

    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=AIAgent))

    with caplog.at_level(logging.WARNING):
        assert hook._patch_aiagent() is False

    assert "AIAgent._emit_status is not available" in caplog.text


def test_signature_warning_is_logged(monkeypatch, caplog):
    hook = load_hook(monkeypatch)

    class BasePlatformAdapter:
        def _send_with_retry(self, payload):
            return types.SimpleNamespace(success=True, payload=payload)

    monkeypatch.setitem(
        sys.modules,
        "gateway.platforms.base",
        types.SimpleNamespace(BasePlatformAdapter=BasePlatformAdapter),
    )

    with caplog.at_level(logging.WARNING):
        assert hook._patch_base_adapter() is False

    assert "BasePlatformAdapter._send_with_retry signature is not supported" in caplog.text
