# Remote Provider Parity

`codex-provider-console` is the remote-server implementation of the provider,
protocol, model and session-management parts of Codex++. It does not reproduce
desktop injection or UI-enhancement features.

## Supported provider modes

| Mode | Meaning |
| --- | --- |
| `official` | The remote Codex runtime uses its saved ChatGPT/Codex login. |
| `pure_api` | The remote Codex runtime uses a configured upstream API through the local relay. |

Mixed API and aggregate/round-robin providers are intentionally out of scope.

## Compatibility contract

- A profile is versioned and has an explicit `mode` and `protocol`.
- Pure API profiles support Responses and Chat Completions upstreams; the relay,
  not Codex configuration alone, owns protocol translation.
- Provider activation is transactional: validate, back up, apply, refresh the
  runtime, and restore on failure.
- A session retains its provider association. Changing the global default only
  changes new sessions; migrations are explicit and reversible.
- Provider deletion must report affected sessions and offer a replacement or
  restore path.

## Delivery order

1. Versioned two-mode profile schema and legacy migration.
2. Separate provider/session/runtime/operations domains.
3. Docker-internal relay: Responses passthrough, then Chat Completions conversion.
4. Model catalog, context-window and compaction settings. Generation is opt-in:
   only models with a valid numeric context limit are included.
5. Session association, export, migration and recovery.
6. Contract fixtures, integration tests, release/versioning.
