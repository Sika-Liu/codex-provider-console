# Architecture boundaries

The project remains a small single-user deployment, but its runtime boundaries
are intentionally explicit:

- `app.py`: HTTP routes and orchestration for the control panel.
- web/pages.py: server-rendered login and console templates; contains no API or service logic.
- application/task_store.py: durable single-instance job snapshots in SQLite; completed history is retained for up to seven days and capped at 200 records.
- `host_ops.py`: verified host SSH command construction and container NSS setup;
  it does not own business routes.
- `storage_ops.py`: atomic private-file writes and validated backup path
  resolution.
- `application/backup_service.py`: panel backup creation, retention, listing,
  and restoration; it has no FastAPI dependency.
- `application/provider_service.py`: provider save/delete persistence,
  credential-preserving updates, and public-profile redaction; it has no
  FastAPI dependency.
- `application/provider_activation.py`: validates and builds the Codex config
  and auth-file activation plan without starting services or changing state.
- `provider_domain.py`: provider schema normalization, compatibility migration,
  and session command construction. It has no FastAPI or Docker dependency.
- `relay.py`: Docker-internal bounded Responses relay with request IDs, upstream status, a 1 MiB body limit, 120-second upstream timeout, and four concurrent requests by default.
- `relay_domain.py`: pure Responses/Chat Completions conversion primitives and
  capability declarations.
- `compose.yml`: the console, relay, and optional reverse-proxy runtime.
- `tests/`: pure contract tests; Docker smoke tests are skipped when Docker is
  unavailable.

Backup and provider-persistence workflows are extracted behind dedicated
application services and covered by filesystem-behavior tests. Provider
activation-plan generation is also isolated; the remaining switch coordinator
still owns diagnostics, session migration, rollback, and host-side App Server
lifecycle control, and should be extracted only behind explicit side-effect
adapters.
