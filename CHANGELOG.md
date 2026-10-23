# Changelog

## Unreleased
- Added `api.compat` as a stable compatibility entrypoint for incremental route migration.
- Extracted password/session authentication and login rate limiting into `security/auth_service.py`; existing HTTP routes keep compatibility wrappers.

## 0.1.0 - 2026-09-26

Initial personal self-hosted optimization release.

- Local-only panel binding by default and login failure throttling.
- Strict SSH host-key verification for host-side operations.
- Backup metadata, restore workflow, and destructive-operation confirmations.
- Canonical provider storage using `mode` and `protocol`.
- Relay capability reporting and improved SSE compatibility handling.
- Added regression and Compose contract tests.
- Extracted host SSH and storage safety primitives; optional Compose smoke check passes.
- Added read-only deployment diagnostics and final operations acceptance checklist.
- Backup history now keeps only the latest complete restore point and latest before-restore snapshot.
