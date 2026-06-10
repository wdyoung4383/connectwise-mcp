# Hosting hardening for connectwise-mcp (DO App Platform)

**Date:** 2026-06-10
**Status:** Approved (items 1–4 of the hosting-readiness assessment; runtime: DigitalOcean App Platform)

## Goal

Make the server safe to host publicly: fail-closed credential resolution
under HTTP, an audit trail for writes, and a deployable App Platform
configuration with an encrypted tenant store. Per-tenant `writes_enabled`
gating was explicitly NOT approved — out of scope.

## Decisions

| Decision | Choice |
|----------|--------|
| Runtime | DO App Platform (Dockerfile build, `.do/app.yaml` spec) — not droplet/Compose/systemd |
| Tenant store | `CW_TENANTS` inline-JSON env var, stored as an encrypted App Platform secret (code already supports it) |
| Audit log destination | stdout via the `logging` module → App Platform runtime logs |
| Health check | new `/health` HTTP route on the MCP app |

## 1. Fail-closed credential resolution (`auth.py`)

Today `get_credentials()` falls through to `CW_*` env vars even for HTTP
requests, so a hosted instance with env credentials set is an open,
unauthenticated proxy to the PSA. Change the resolution rule:

- **HTTP request context** (an active request exists —
  `get_http_request()` returns a request rather than raising; true under the
  HTTP transport, never under stdio): credentials must come from a bearer
  token (tenant store) or `X-CW-*` headers. The `CW_*` env fallback is
  **skipped**. The MissingCredentials message must say exactly why ("env
  credentials are ignored for HTTP requests").
- **stdio context** (no active HTTP request): env fallback unchanged.

The discriminator is **request-context existence**, NOT "the header dict is
non-empty". fastmcp's `get_http_headers()` strips a fixed set of headers
(`host`, `content-length`, `content-type`, `connection`, `accept`,
`authorization`, `mcp-session-id`, ...) from its default view, so a minimal
HTTP request can yield an empty dict; gating on non-emptiness would let an
unauthenticated caller reopen the env fallback. Gate on `_in_http_request()`
(wraps `get_http_request()`) instead.

Bearer-path fix: because `authorization` is in that strip set, the default
filtered view never exposes the bearer token, so the tenant-store lookup
would never fire over real HTTP traffic. `get_credentials()` therefore reads
headers with `get_http_headers(include_all=True)` (keys stay lowercased) so
both the bearer token and any minimal header set are visible.

No escape hatch (YAGNI). Existing local stdio behavior is untouched.

## 2. Write audit logging

One log line per attempted write, success or failure, emitted from
`writer.cw_post` via `logging.getLogger("connectwise_mcp.audit")`:

- Success: `WRITE ok company=<cw company_id> path=<allowlist path> id=<created id>`
- Failure: `WRITE fail company=<cw company_id> path=<path> status=<http status or 'error'> detail=<first 120 chars>`

`cw_post` needs the acting tenant's identity: pass it explicitly — change
signature to `cw_post(client, path, body, *, path_params=None, actor: str)`
where `actor` is the ConnectWise `company_id` from the resolved credentials.
`curated_writes` threads it through (`_run_write` resolves creds and gives
`fn` access; tools pass `creds.company_id`). No tokens, keys, names, or
body contents are ever logged. Server startup configures `logging` to
stdout at INFO (only if no handlers configured — don't fight embedders).

## 3. `/health` endpoint

`server.py` adds a FastMCP custom HTTP route `GET /health` returning 200
`{"status": "ok"}` with no auth (it reveals nothing tenant-specific). Used by App Platform health checks. Only meaningful under
the HTTP transport; harmless under stdio (not registered there is fine if
the API requires transport, otherwise registered always).

## 4. Deployment artifacts

- **`Dockerfile`** (repo root): `python:3.12-slim`, copy project, `pip
  install .` (no dev extras), non-root user, `ENV CW_MCP_HOST=0.0.0.0
  CW_MCP_PORT=8080`, `EXPOSE 8080`, `CMD ["connectwise-mcp"]`.
- **`.do/app.yaml`**: one `services:` entry building from the Dockerfile on
  the GitHub repo (`wdyoung4383/connectwise-mcp`, branch `master`,
  `deploy_on_push: true`), `http_port: 8080`, `instance_size_slug:
  basic-xxs`, health check on `/health`, env vars:
  `CW_TENANTS` (`type: SECRET`, placeholder value to be set in the DO
  console), and nothing else credential-like.
- **`docs/DEPLOY.md`**: step-by-step — generate a tenant token
  (`python -c "import secrets; print(secrets.token_urlsafe(32))"`), build
  the `CW_TENANTS` JSON, create the app (`doctl apps create --spec
  .do/app.yaml` or console), set the secret, connect Claude Desktop via
  `mcp-remote https://<app-url>/mcp --header "Authorization: Bearer ..."`,
  how to rotate a token (edit secret, redeploy), where audit logs live
  (App Platform → Runtime Logs).
- **README**: short "Deploy (DigitalOcean App Platform)" pointer to
  DEPLOY.md replacing/augmenting the generic "Run" hosted notes.

## Error handling

- Fail-closed path returns the same `MissingCredentials`-derived
  `{"error": ...}` shape tools already produce.
- `/health` never raises; static payload.
- Audit logging must never break a write: wrap formatting defensively
  (log call itself is not allowed to throw — keep it to %-style args).

## Testing

Offline (extend existing suites):

- `tests/test_auth.py`: HTTP context (mock `get_http_headers` non-empty) +
  env vars set + no bearer/X-CW headers → MissingCredentials mentioning
  HTTP; stdio context (empty headers) + env vars → resolves as today;
  HTTP context + X-CW-* headers → resolves; HTTP context + bearer + tenant
  store → resolves.
- `tests/test_writer.py`: successful cw_post emits one audit record with
  actor/path/id (caplog); failed cw_post emits fail record; nothing
  resembling a key in the message. Existing cw_post call sites updated for
  the `actor` kwarg.
- `tests/test_tools.py` (or new): `/health` route returns 200 + JSON via
  fastmcp in-memory HTTP test client if available; otherwise assert route
  registration.
- Dockerfile/app.yaml: no automated test; verified by a local
  `docker build` if Docker is available, else by review + DO build.

Live verification: none required pre-deploy (no CW contract changes). The
deploy itself (DO build, health check green, mcp-remote connection, one
read + one labeled write through the hosted URL, audit line visible in
runtime logs) is the acceptance test, performed with the user.

## Out of scope

Per-tenant `writes_enabled`, rate limiting, droplet/Compose/systemd
variants, token rotation automation, log shipping.
