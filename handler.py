"""Gateway hook that suppresses selected Hermes operational messages."""

from __future__ import annotations

import asyncio
import functools
import importlib.abc
import importlib.machinery
import inspect
import logging
import os
import sys
import threading
from contextvars import ContextVar
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Iterable


logger = logging.getLogger(__name__)

HOOK_NAME = "hermes-gateway-event-filter"
CONFIG_KEY = "gateway_event_filter"
LEGACY_CONFIG_KEY = "hermes_gateway_event_filter"
LEGACY_PLUGIN_KEY = "hermes-agent-gateway-event-filter"
_EMPTY_SENTINEL = "(empty)"
_PATCH_ATTR = "_gateway_event_filter_wrapped"
_ORIGINAL_ATTR = "_gateway_event_filter_original"
_CALLBACK_WRAPPED_ATTR = "_gateway_event_filter_callback_wrapped"
_SKIP_IMPORT_BOOTSTRAP_ENV = "HERMES_HOOK_SKIP_IMPORT_BOOTSTRAP"
_IMPORT_FINDER_ATTR = "_gateway_event_filter_import_finder"
_IMPORT_LOADER_ATTR = "_gateway_event_filter_import_loader"
_PATCH_LOCK = threading.Lock()
_IMPORT_HOOK_LOCK = threading.Lock()
_AGENT_PATCH_PENDING = False
_AIAGENT_IMPORT_MODULES = {
    "run_agent",
    "hermes.agent.run",
    "hermes.agent.run_agent",
}
_BUSY_ACK_SUPPRESS_PLATFORM: ContextVar[str | None] = ContextVar(
    "gateway_event_filter_busy_ack_suppress_platform",
    default=None,
)

_DEFAULT_PLATFORMS = {"all"}
_ALL_PLATFORM_EXCLUSIONS = {"", "cli", "local"}
_DEFAULT_SUPPRESS = {
    "empty_final_warning": True,
    "busy_ack": True,
    "background_review": True,
}
_CALLBACK_SUPPRESS_KEYS = {
    "background_review_callback": "background_review",
}
_EMPTY_STATUS_MARKERS = (
    "Model returned empty after tool calls",
    "Thinking-only response",
    "Empty response from model",
    "Model returning empty responses",
    "Model produced reasoning but no visible",
    "Model returned no content after all retries",
    "Empty response after tool calls",
)
_EMPTY_FINAL_WARNING_MARKER = "The model returned no response after processing tool results."
_EMPTY_FINAL_WARNING_DETAIL = "This can happen with some models"
_EMPTY_FINAL_WARNING_ACTION_MARKERS = (
    "try again or rephrase your question",
    "try again or",
    "rephrase your question",
)
_GATEWAY_RUN_MODULES = {
    "gateway.run",
}
_BASE_ADAPTER_MODULES = {
    "gateway.platforms.base",
}

_CONFIG: dict[str, Any] = {
    "platforms": set(_DEFAULT_PLATFORMS),
    "suppress": dict(_DEFAULT_SUPPRESS),
}


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home())
    except Exception:
        raw = os.environ.get("HERMES_HOME", "").strip()
        return Path(raw) if raw else Path.home() / ".hermes"


def _platform_name(value: Any) -> str:
    if value is None:
        return ""
    enum_value = getattr(value, "value", None)
    if enum_value is not None:
        value = enum_value
    text = str(value).strip().lower()
    if "." in text:
        text = text.rsplit(".", 1)[-1]
    return text


def _coerce_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
        return default
    return bool(value)


def _normalize_platforms(raw: Any) -> set[str]:
    if isinstance(raw, str):
        platforms = {_platform_name(raw)}
    elif isinstance(raw, Iterable):
        platforms = {_platform_name(item) for item in raw}
    else:
        platforms = set(_DEFAULT_PLATFORMS)
    platforms.discard("")
    return platforms or set(_DEFAULT_PLATFORMS)


def _load_runtime_config() -> dict[str, Any] | None:
    config_path = _hermes_home() / "config.yaml"
    raw_hook_config: dict[str, Any] = {}

    if config_path.exists():
        try:
            import yaml

            loaded = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        except Exception as exc:
            logger.warning("%s: failed to read config.yaml; event filter disabled: %s", HOOK_NAME, exc)
            return None

        if not isinstance(loaded, dict):
            logger.warning("%s: config.yaml is not a mapping; event filter disabled", HOOK_NAME)
            return None

        native_config_found = False
        for key in (CONFIG_KEY, LEGACY_CONFIG_KEY):
            if key not in loaded:
                continue
            native_config_found = True
            value = loaded.get(key)
            if isinstance(value, dict):
                raw_hook_config = value
            break

        plugin_config = loaded.get("plugins", {})
        if not native_config_found and isinstance(plugin_config, dict):
            value = plugin_config.get(LEGACY_PLUGIN_KEY)
            if isinstance(value, dict):
                raw_hook_config = value

    suppress = dict(_DEFAULT_SUPPRESS)
    configured_suppress = raw_hook_config.get("suppress")
    if isinstance(configured_suppress, dict):
        for key in _DEFAULT_SUPPRESS:
            if key in configured_suppress:
                suppress[key] = _coerce_bool(configured_suppress[key], default=suppress[key])

    return {
        "platforms": _normalize_platforms(raw_hook_config.get("platforms", _DEFAULT_PLATFORMS)),
        "suppress": suppress,
    }


def _reload_config() -> bool:
    global _CONFIG
    loaded = _load_runtime_config()
    if loaded is None:
        return False
    _CONFIG = loaded
    return True


def _source_platform(source: Any) -> str:
    return _platform_name(getattr(source, "platform", source))


def _is_target_platform(platform: Any) -> bool:
    platform_name = _source_platform(platform)
    platforms = _CONFIG["platforms"]
    if "all" in platforms or "*" in platforms:
        return platform_name not in _ALL_PLATFORM_EXCLUSIONS
    return platform_name in platforms


def _should_suppress(platform: Any, key: str) -> bool:
    return bool(_CONFIG["suppress"].get(key)) and _is_target_platform(platform)


def _should_suppress_for_agent(agent: Any, key: str) -> bool:
    return _should_suppress(getattr(agent, "platform", None), key)


def _is_empty_status(message: Any) -> bool:
    text = str(message or "")
    return any(marker in text for marker in _EMPTY_STATUS_MARKERS)


def _is_busy_ack_message(content: Any) -> bool:
    return "Interrupting current task" in str(content or "")


def _is_empty_final_warning_message(content: Any) -> bool:
    text = str(content or "").strip()
    marker_index = text.find(_EMPTY_FINAL_WARNING_MARKER)
    if marker_index < 0 or marker_index > 4:
        return False
    leading = text[:marker_index].strip()
    if leading and leading != "\u26a0\ufe0f":
        return False
    return (
        _EMPTY_FINAL_WARNING_DETAIL in text
        and any(marker in text for marker in _EMPTY_FINAL_WARNING_ACTION_MARKERS)
        and len(text) <= 240
    )


def _should_suppress_send(platform: Any, content: Any, *, busy_ack_context: bool = False) -> bool:
    if busy_ack_context and _should_suppress(platform, "busy_ack") and _is_busy_ack_message(content):
        return True
    if _should_suppress(platform, "empty_final_warning") and _is_empty_final_warning_message(content):
        return True
    return False


def _successful_send_result() -> Any:
    try:
        from gateway.platforms.base import SendResult

        return SendResult(success=True)
    except Exception:
        return SimpleNamespace(success=True, message_id=None, error=None, retryable=False)


def _normalize_empty_result(result: Any, platform: Any) -> Any:
    if not _should_suppress(platform, "empty_final_warning"):
        return result
    if not isinstance(result, dict) or result.get("final_response") != _EMPTY_SENTINEL:
        return result

    updated = dict(result)
    updated["final_response"] = ""
    suppressed = list(updated.get("gateway_event_filter_suppressed", []))
    suppressed.append("empty_final_warning")
    updated["gateway_event_filter_suppressed"] = suppressed
    return updated


def _make_suppressed_callback(callback: Callable[..., Any], key: str, platform: Any) -> Callable[..., Any]:
    @functools.wraps(callback)
    def wrapped(*_args: Any, **_kwargs: Any) -> None:
        logger.debug(
            "%s: suppressed %s callback for %s",
            HOOK_NAME,
            key,
            _source_platform(platform),
        )
        return None

    setattr(wrapped, _CALLBACK_WRAPPED_ATTR, True)
    setattr(wrapped, _ORIGINAL_ATTR, callback)
    return wrapped


def _has_parameters(callable_obj: Any, required: set[str]) -> bool:
    try:
        params = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return False
    return required.issubset(params)


def _patch_aiagent_module(module: Any, module_name: str = "run_agent") -> bool:
    global _AGENT_PATCH_PENDING
    agent_cls = getattr(module, "AIAgent", None) if module is not None else None
    if agent_cls is None:
        logger.warning("%s: %s.AIAgent is not available; skipping patch", HOOK_NAME, module_name)
        return False

    patched_setattr = False
    patched_emit_status = False
    patched_run_conversation = False
    failed = False

    original_setattr = getattr(agent_cls, "__setattr__", None)
    if original_setattr is None:
        logger.warning("%s: %s.AIAgent.__setattr__ is not available; skipping patch", HOOK_NAME, module_name)
        failed = True
    elif getattr(original_setattr, _PATCH_ATTR, False):
        patched_setattr = True
    else:
        @functools.wraps(original_setattr)
        def wrapped_setattr(self: Any, name: str, value: Any) -> Any:
            key = _CALLBACK_SUPPRESS_KEYS.get(name)
            if (
                key
                and callable(value)
                and not getattr(value, _CALLBACK_WRAPPED_ATTR, False)
                and _should_suppress_for_agent(self, key)
            ):
                value = _make_suppressed_callback(value, key, getattr(self, "platform", None))
            return original_setattr(self, name, value)

        setattr(wrapped_setattr, _PATCH_ATTR, True)
        setattr(wrapped_setattr, _ORIGINAL_ATTR, original_setattr)
        agent_cls.__setattr__ = wrapped_setattr
        patched_setattr = True
        logger.info("%s: patched AIAgent.__setattr__", HOOK_NAME)

    original_emit_status = getattr(agent_cls, "_emit_status", None)
    if original_emit_status is None:
        logger.warning("%s: %s.AIAgent._emit_status is not available; skipping patch", HOOK_NAME, module_name)
        failed = True
    elif getattr(original_emit_status, _PATCH_ATTR, False):
        patched_emit_status = True
    elif not _has_parameters(original_emit_status, {"message"}):
        logger.warning("%s: %s.AIAgent._emit_status signature is not supported; skipping patch", HOOK_NAME, module_name)
        failed = True
    else:
        @functools.wraps(original_emit_status)
        def wrapped_emit_status(self: Any, message: str) -> Any:
            if _should_suppress_for_agent(self, "empty_final_warning") and _is_empty_status(message):
                logger.debug(
                    "%s: suppressed empty lifecycle status for %s",
                    HOOK_NAME,
                    _source_platform(getattr(self, "platform", None)),
                )
                return None
            return original_emit_status(self, message)

        setattr(wrapped_emit_status, _PATCH_ATTR, True)
        setattr(wrapped_emit_status, _ORIGINAL_ATTR, original_emit_status)
        agent_cls._emit_status = wrapped_emit_status
        patched_emit_status = True
        logger.info("%s: patched AIAgent._emit_status", HOOK_NAME)

    original_run_conversation = getattr(agent_cls, "run_conversation", None)
    if original_run_conversation is None:
        logger.warning("%s: %s.AIAgent.run_conversation is not available; skipping patch", HOOK_NAME, module_name)
        failed = True
    elif getattr(original_run_conversation, _PATCH_ATTR, False):
        patched_run_conversation = True
    else:
        @functools.wraps(original_run_conversation)
        def wrapped_run_conversation(self: Any, *args: Any, **kwargs: Any) -> Any:
            result = original_run_conversation(self, *args, **kwargs)
            return _normalize_empty_result(result, getattr(self, "platform", None))

        setattr(wrapped_run_conversation, _PATCH_ATTR, True)
        setattr(wrapped_run_conversation, _ORIGINAL_ATTR, original_run_conversation)
        agent_cls.run_conversation = wrapped_run_conversation
        patched_run_conversation = True
        logger.info("%s: patched AIAgent.run_conversation", HOOK_NAME)

    patched = patched_setattr and patched_emit_status and patched_run_conversation and not failed
    if patched:
        _AGENT_PATCH_PENDING = False
    return patched


class _RunAgentPatchLoader(importlib.abc.Loader):
    def __init__(self, wrapped_loader: Any):
        self._wrapped_loader = wrapped_loader
        setattr(self, _IMPORT_LOADER_ATTR, True)

    def create_module(self, spec: Any) -> Any:
        create_module = getattr(self._wrapped_loader, "create_module", None)
        if create_module is None:
            return None
        return create_module(spec)

    def exec_module(self, module: Any) -> None:
        exec_module = getattr(self._wrapped_loader, "exec_module", None)
        if exec_module is None:
            load_module = getattr(self._wrapped_loader, "load_module", None)
            if load_module is None:
                raise ImportError(f"{self._wrapped_loader!r} cannot execute module {module.__name__}")
            module = load_module(module.__name__)
            _patch_aiagent_module(module, str(getattr(module, "__name__", None) or module.__name__))
            return
        exec_module(module)
        _patch_aiagent_module(module, str(getattr(module, "__name__", None) or "run_agent"))


class _RunAgentPatchFinder(importlib.abc.MetaPathFinder):
    def __init__(self) -> None:
        setattr(self, _IMPORT_FINDER_ATTR, True)

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        if fullname not in _AIAGENT_IMPORT_MODULES:
            return None

        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return spec
        if not getattr(spec.loader, _IMPORT_LOADER_ATTR, False):
            spec.loader = _RunAgentPatchLoader(spec.loader)
        return spec


def _install_aiagent_import_hook() -> bool:
    with _IMPORT_HOOK_LOCK:
        for finder in sys.meta_path:
            if getattr(finder, _IMPORT_FINDER_ATTR, False):
                return True
        try:
            sys.meta_path.insert(0, _RunAgentPatchFinder())
        except Exception as exc:
            logger.warning("%s: failed to install AIAgent import hook: %s", HOOK_NAME, exc)
            return False
    logger.info("%s: installed AIAgent import hook", HOOK_NAME)
    return True


def _iter_aiagent_patch_targets() -> list[tuple[str, Any]]:
    targets: list[tuple[str, Any]] = []
    seen_modules: set[int] = set()
    seen_classes: set[int] = set()

    def is_hermes_path(module_file: str) -> bool:
        parts = {part for part in module_file.split("/") if part}
        return "hermes" in parts or "hermes-agent" in parts

    def is_aiagent_module_candidate(module_name: str, module: Any) -> bool:
        if module_name in _AIAGENT_IMPORT_MODULES:
            return True
        if module_name.startswith("hermes.agent.") and (
            module_name.endswith(".run_agent") or module_name.endswith(".run")
        ):
            return True
        module_file = _module_file_path(module)
        if not module_file or not is_hermes_path(module_file):
            return False
        path_name = _module_file_name(module)
        return path_name == "run_agent.py" or (
            path_name == "run.py" and module_name.endswith(".agent.run")
        )

    def add_target(module_name: str, module: Any) -> None:
        if module is None:
            return
        module_id = id(module)
        if module_id in seen_modules:
            return
        if not is_aiagent_module_candidate(module_name, module):
            return
        agent_cls = getattr(module, "AIAgent", None)
        if agent_cls is None or getattr(agent_cls, "__name__", "") != "AIAgent":
            return
        class_id = id(agent_cls)
        if class_id in seen_classes:
            return
        seen_modules.add(module_id)
        seen_classes.add(class_id)
        targets.append((module_name, module))

    for module_name in _AIAGENT_IMPORT_MODULES:
        add_target(module_name, sys.modules.get(module_name))

    for module_name, module in list(sys.modules.items()):
        add_target(str(getattr(module, "__name__", None) or module_name), module)

    return targets


def _patch_aiagent() -> bool:
    global _AGENT_PATCH_PENDING
    patched_modules = 0
    failed_modules = 0
    targets = _iter_aiagent_patch_targets()
    for module_name, module in targets:
        if _patch_aiagent_module(module, module_name):
            patched_modules += 1
        else:
            failed_modules += 1

    import_hook_installed = _install_aiagent_import_hook()
    _AGENT_PATCH_PENDING = (
        import_hook_installed
        and patched_modules == 0
        and failed_modules == 0
        and not targets
    )
    if patched_modules == 0:
        return False
    return import_hook_installed and failed_modules == 0


def _module_file_name(module: Any) -> str:
    return Path(str(getattr(module, "__file__", "") or "")).name.lower()


def _module_file_path(module: Any) -> str:
    return str(getattr(module, "__file__", "") or "").lower().replace("\\", "/")


def _is_gateway_run_file(module: Any) -> bool:
    module_file = _module_file_path(module)
    return module_file == "gateway/run.py" or module_file.endswith("/gateway/run.py")


def _is_gateway_runner_module_candidate(module_name: str, module: Any) -> bool:
    if module_name in _GATEWAY_RUN_MODULES or module_name.endswith(".gateway.run"):
        return True
    path_name = _module_file_name(module)
    return path_name == "run.py" and _is_gateway_run_file(module)


def _is_base_adapter_module_candidate(module_name: str, module: Any) -> bool:
    if module_name in _BASE_ADAPTER_MODULES or module_name.endswith(".gateway.platforms.base"):
        return True
    path_name = _module_file_name(module)
    module_file = _module_file_path(module)
    return path_name == "base.py" and "/gateway/platforms/" in module_file


def _get_gateway_platform_from_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:
    source = kwargs.get("source")
    if source is None:
        for candidate in args:
            if hasattr(candidate, "platform"):
                source = candidate
                break
    return getattr(source, "platform", source)


def _patch_gateway_runner() -> bool:
    patched_classes = 0
    failed_classes = 0
    seen: set[int] = set()
    for module_name, module in list(sys.modules.items()):
        if not _is_gateway_runner_module_candidate(str(module_name), module):
            continue
        runner_cls = getattr(module, "GatewayRunner", None)
        if runner_cls is None or getattr(runner_cls, "__name__", "") != "GatewayRunner":
            continue
        class_id = id(runner_cls)
        if class_id in seen:
            continue
        seen.add(class_id)

        patched_run_agent = False
        patched_busy = False
        failed = False

        original_run_agent = getattr(runner_cls, "_run_agent", None)
        if original_run_agent is None:
            logger.warning("%s: GatewayRunner._run_agent is not available; skipping patch", HOOK_NAME)
            failed = True
        elif getattr(original_run_agent, _PATCH_ATTR, False):
            patched_run_agent = True
        elif not _has_parameters(original_run_agent, {"source"}):
            logger.warning("%s: GatewayRunner._run_agent signature is not supported; skipping patch", HOOK_NAME)
            failed = True
        else:
            @functools.wraps(original_run_agent)
            async def wrapped_run_agent(self: Any, *args: Any, __original=original_run_agent, **kwargs: Any) -> Any:
                result = await __original(self, *args, **kwargs)
                platform = _get_gateway_platform_from_args(args, kwargs)
                return _normalize_empty_result(result, platform)

            setattr(wrapped_run_agent, _PATCH_ATTR, True)
            setattr(wrapped_run_agent, _ORIGINAL_ATTR, original_run_agent)
            runner_cls._run_agent = wrapped_run_agent
            patched_run_agent = True
            logger.info("%s: patched GatewayRunner._run_agent", HOOK_NAME)

        original_busy = getattr(runner_cls, "_handle_active_session_busy_message", None)
        if original_busy is None:
            logger.warning(
                "%s: GatewayRunner._handle_active_session_busy_message is not available; skipping patch",
                HOOK_NAME,
            )
            failed = True
        elif getattr(original_busy, _PATCH_ATTR, False):
            patched_busy = True
        elif not _has_parameters(original_busy, {"event", "session_key"}):
            logger.warning(
                "%s: GatewayRunner._handle_active_session_busy_message signature is not supported; skipping patch",
                HOOK_NAME,
            )
            failed = True
        else:
            @functools.wraps(original_busy)
            async def wrapped_busy(self: Any, event: Any, session_key: str, __original=original_busy) -> Any:
                platform = _source_platform(getattr(event, "source", None))
                if not _should_suppress(platform, "busy_ack"):
                    return await __original(self, event, session_key)

                token = _BUSY_ACK_SUPPRESS_PLATFORM.set(platform)
                try:
                    return await __original(self, event, session_key)
                finally:
                    _BUSY_ACK_SUPPRESS_PLATFORM.reset(token)

            setattr(wrapped_busy, _PATCH_ATTR, True)
            setattr(wrapped_busy, _ORIGINAL_ATTR, original_busy)
            runner_cls._handle_active_session_busy_message = wrapped_busy
            patched_busy = True
            logger.info("%s: patched GatewayRunner._handle_active_session_busy_message", HOOK_NAME)

        if patched_run_agent and patched_busy and not failed:
            patched_classes += 1
        else:
            failed_classes += 1

    if not seen:
        logger.warning("%s: GatewayRunner class is not available; skipping patch", HOOK_NAME)
    return patched_classes > 0 and failed_classes == 0


def _patch_base_adapter() -> bool:
    patched_classes = 0
    failed_classes = 0
    seen: set[int] = set()
    for module_name, module in list(sys.modules.items()):
        if not _is_base_adapter_module_candidate(str(module_name), module):
            continue
        adapter_cls = getattr(module, "BasePlatformAdapter", None)
        if adapter_cls is None or getattr(adapter_cls, "__name__", "") != "BasePlatformAdapter":
            continue
        class_id = id(adapter_cls)
        if class_id in seen:
            continue
        seen.add(class_id)

        original_send = getattr(adapter_cls, "_send_with_retry", None)
        if original_send is None:
            logger.warning("%s: BasePlatformAdapter._send_with_retry is not available; skipping patch", HOOK_NAME)
            failed_classes += 1
            continue
        if getattr(original_send, _PATCH_ATTR, False):
            patched_classes += 1
            continue
        if not _has_parameters(original_send, {"content"}):
            logger.warning(
                "%s: BasePlatformAdapter._send_with_retry signature is not supported; skipping patch",
                HOOK_NAME,
            )
            failed_classes += 1
            continue

        @functools.wraps(original_send)
        async def wrapped_send(self: Any, *args: Any, __original=original_send, **kwargs: Any) -> Any:
            platform = _BUSY_ACK_SUPPRESS_PLATFORM.get()
            content = kwargs.get("content")
            if content is None and len(args) >= 2:
                content = args[1]
            adapter_platform = _source_platform(getattr(self, "platform", None))
            effective_platform = platform or adapter_platform
            if _should_suppress_send(effective_platform, content, busy_ack_context=bool(platform)):
                logger.debug("%s: suppressed gateway send for %s", HOOK_NAME, effective_platform)
                return _successful_send_result()

            result = __original(self, *args, **kwargs)
            if asyncio.iscoroutine(result) or hasattr(result, "__await__"):
                return await result
            return result

        setattr(wrapped_send, _PATCH_ATTR, True)
        setattr(wrapped_send, _ORIGINAL_ATTR, original_send)
        adapter_cls._send_with_retry = wrapped_send
        patched_classes += 1
        logger.info("%s: patched BasePlatformAdapter._send_with_retry", HOOK_NAME)

    if not seen:
        logger.warning("%s: BasePlatformAdapter class is not available; skipping patch", HOOK_NAME)
    return patched_classes > 0 and failed_classes == 0


def bootstrap(*, warn_incomplete: bool = True) -> bool:
    if not _reload_config():
        return False
    with _PATCH_LOCK:
        patched_agent = _patch_aiagent()
        patched_gateway = _patch_gateway_runner()
        patched_base = _patch_base_adapter()
    agent_ready = patched_agent or _AGENT_PATCH_PENDING
    if warn_incomplete and _AGENT_PATCH_PENDING:
        logger.info("%s: AIAgent patch pending until the agent module is imported", HOOK_NAME)
    if warn_incomplete and (not agent_ready or not patched_gateway or not patched_base):
        logger.warning(
            "%s: patch targets incomplete: agent=%s agent_pending=%s gateway=%s base_adapter=%s",
            HOOK_NAME,
            patched_agent,
            _AGENT_PATCH_PENDING,
            patched_gateway,
            patched_base,
        )
    return agent_ready and patched_gateway and patched_base


async def handle(event_type: str, context: dict[str, Any] | None = None) -> None:
    if event_type != "gateway:startup":
        return
    bootstrap()


def _bootstrap_on_import() -> None:
    if _coerce_bool(os.environ.get(_SKIP_IMPORT_BOOTSTRAP_ENV), default=False):
        return
    try:
        bootstrap(warn_incomplete=False)
    except Exception:
        logger.warning("%s: import-time bootstrap failed", HOOK_NAME, exc_info=True)


_bootstrap_on_import()
