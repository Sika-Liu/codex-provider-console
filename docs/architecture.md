# Architecture boundaries

The project remains a small single-user deployment, but its runtime boundaries
are intentionally explicit:

- `app.py`: HTTP routes and orchestration for the control panel.
- `host_ops.py`: verified host SSH command construction and container NSS setup;
  it does not own business routes.
- `storage_ops.py`: atomic private-file writes and validated backup path
  resolution.
- `application/backup_service.py`: panel backup creation, retention, listing,
  and restoration; it has no FastAPI dependency.
- `provider_domain.py`: provider schema normalization, compatibility migration,
  and session command construction. It has no FastAPI or Docker dependency.
- `relay.py`: Docker-internal HTTP relay entrypoint.
- `relay_domain.py`: pure Responses/Chat Completions conversion primitives and
  capability declarations.
- `compose.yml`: the console, relay, and optional reverse-proxy runtime.
- `tests/`: pure contract tests; Docker smoke tests are skipped when Docker is
  unavailable.

The backup/storage workflow has been extracted behind `BackupService` and
covered by filesystem-behavior tests. The next extraction should be the
provider application workflow, while retaining the current HTTP paths and
response shapes.
