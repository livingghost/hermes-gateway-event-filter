# Hermes Gateway Event Filter

Gateway hook for Hermes Agent that suppresses selected operational event
messages before they are delivered to chat platforms.

This is intended for shared rooms where gateway lifecycle notices are useful in
logs but noisy in the chat timeline. Examples include empty model-output
nudges, active-task interrupt acknowledgments, and background memory/profile
review notices.

## Compatibility

This hook is currently maintained against:

- Hermes Agent `v0.11.0`
- upstream `main` commit `05d8f11085fec55106a0d2e0ed2051baeb4b108c`
- commit date `2026-04-24`

The current compatibility target is the upstream snapshot whose HEAD commit is:

```text
05d8f11085fec55106a0d2e0ed2051baeb4b108c
fix(/model): show provider-enforced context length, not raw models.dev (#15438)
```

The hook is source-aware. It does not globally block arbitrary substrings from
normal assistant replies. Instead it patches the dedicated runtime paths that
emit these gateway events, with a narrow send-boundary guard for two known
gateway notices that are materialized immediately before delivery.

## Installation

Copy this directory to the Hermes home hooks directory:

```text
$HERMES_HOME/hooks/hermes-gateway-event-filter
```

The directory must contain:

```text
HOOK.yaml
handler.py
README.md
```

Restart the gateway after installing or changing the hook.

## Configuration

No configuration is required. By default, the hook applies to all non-local
gateway/chat platforms. The `cli` and `local` interfaces are excluded.

To override the defaults, add this optional block to
`$HERMES_HOME/config.yaml`:

```yaml
gateway_event_filter:
  platforms: all
  suppress:
    empty_final_warning: true
    busy_ack: true
    background_review: true
```

`platforms` may be `all` or a list:

```yaml
gateway_event_filter:
  platforms:
    - discord
    - telegram
```

For migration from the old plugin, the hook also reads the legacy config block:

```yaml
plugins:
  hermes-agent-gateway-event-filter:
    suppress:
      background_review: true
```

The native `gateway_event_filter` block takes precedence when both are present.

| Key | Default | Behavior |
|-----|---------|----------|
| `empty_final_warning` | `true` | Suppresses empty-output lifecycle statuses and normalizes the internal `(empty)` final-response sentinel to `""`. |
| `busy_ack` | `true` | Suppresses the active-session interrupt acknowledgment. |
| `background_review` | `true` | Suppresses memory/profile background-review delivery callbacks. |

Tool-progress and interim assistant commentary are intentionally not handled by
this hook in the current Hermes snapshot. Use Hermes core display settings
instead:

```yaml
display:
  tool_progress: "off"
  interim_assistant_messages: true
```

## Behavior

The hook patches these runtime targets:

| Target | Purpose |
|--------|---------|
| `AIAgent.__setattr__` | Wraps selected gateway callbacks when they are assigned. |
| `AIAgent._emit_status` | Suppresses only known empty-response lifecycle statuses. |
| `AIAgent.run_conversation` | Normalizes `(empty)` before gateway handling sees it. |
| `GatewayRunner._run_agent` | Fallback normalization for gateway turns. |
| `GatewayRunner._handle_active_session_busy_message` | Suppresses only the busy acknowledgment send inside the busy-handler path. |
| `BasePlatformAdapter._send_with_retry` | Drops known busy-ack and empty-final warning notices immediately before platform delivery. |

When the model returns:

```python
{"final_response": "(empty)", ...}
```

the hook returns:

```python
{
    "final_response": "",
    "gateway_event_filter_suppressed": ["empty_final_warning"],
    ...
}
```

All other result fields are preserved.

## What It Does Not Suppress

This hook does not globally scan platform messages for arbitrary blocked
strings. The send-boundary guard only matches known Hermes gateway notices. It
does not suppress:

- user messages
- provider errors
- tool exceptions
- gateway drain/restart notices
- context-window or compression warnings unrelated to empty model output

Those should remain visible because they may require action.

## Operational Notes

- Hook discovery is per-process. File changes require a gateway restart.
- The hook does not modify Hermes core files.
- The handler attempts to patch during hook discovery, when `handler.py` is
  imported. It also retries on the `gateway:startup` event as a safety check.
- `run_agent` is imported lazily by the gateway, so the hook installs a small
  import hook that patches `AIAgent` immediately after the agent module loads.
  Until that lazy import happens, startup may report the AIAgent patch as
  pending rather than already applied.
- For dry-run tooling or tests that need import without side effects, set
  `HERMES_HOOK_SKIP_IMPORT_BOOTSTRAP=true`; the `gateway:startup` retry remains
  available.

## Limitations

This hook intentionally avoids Hermes core changes, so it patches internal
gateway functions at runtime. If Hermes renames or changes the signatures of
`AIAgent`, `GatewayRunner._run_agent`,
`GatewayRunner._handle_active_session_busy_message`, or
`BasePlatformAdapter._send_with_retry`, the hook logs a warning and skips the
unsupported patch. First-class Hermes hooks for status events and pre-send
message cancellation would be more stable if upstream adds them later.

The hook also auto-discovers loaded Hermes agent and gateway modules that expose
`AIAgent`, `GatewayRunner`, or `BasePlatformAdapter` classes. Discovery is
intentionally constrained to known Hermes module names and Hermes-like file
paths, so `python -m gateway.run` style entrypoints and modest module-path
refactors can be patched without touching unrelated classes that happen to use
the same names.

## Validation

Check local syntax:

```bash
python -m py_compile handler.py
```

Run tests from this hook directory:

```bash
python -m pytest tests
```

## License

MIT. See [LICENSE](LICENSE).

## Support

If this hook is useful, you can support development here:

- [Sponsor on GitHub](https://github.com/sponsors/livingghost)
