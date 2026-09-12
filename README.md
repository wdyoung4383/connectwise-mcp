# ConnectWise PSA MCP Server

A [FastMCP](https://gofastmcp.com) server that exposes **ConnectWise Manage
(PSA)** as a gateway for AI agents: the full documented API surface, every verb
except DELETE.

## Why a gateway, not 3,000 tools

The catalog covers **2,780 operations across 1,832 paths** (GET 1,725, POST 399,
PATCH 329, PUT 327). Exposing one tool per operation would overwhelm any LLM
client. Instead the OpenAPI spec is loaded as a runtime **catalog**, with a
handful of gateway tools in front of it, plus curated shortcuts for the common
asks:

| Tool | Purpose |
|------|---------|
| `list_modules` | Orientation: modules + endpoint counts |
| `search_endpoints` | Find a GET endpoint by keyword |
| `describe_endpoint` | See an endpoint's params + response shape |
| `cw_get` | Execute any GET (paging, `conditions` filtering, `_info` stripping) |
| `cw_call` | Execute any POST / PATCH / PUT |
| `search_tickets` / `get_ticket` | Service tickets without conditions syntax |
| `find_company` | Companies by name/identifier |
| `list_agreements` | Finance agreements, optionally per company |
| `recent_time_entries` | Time entries from the last N days |
| `create_ticket` / `create_ticket_note` | Create a service ticket / add a note to one |
| `create_time_entry` | Log time against a ticket or company |
| `create_company` / `create_contact` | Create a company / contact |

### Write scope

Reads and writes both cover the whole catalog. `cw_call` will POST, PATCH or
PUT to any path; there is no per-path allowlist. The curated create tools are
convenience wrappers over the same dispatcher, not the boundary.

**DELETE is the one exclusion, and it is excluded by construction.** ConnectWise
deletes are not recoverable through the API, so the verb is absent from both the
catalog (`scope.INCLUDED_METHODS`) and the dispatcher (`writer.WRITE_METHODS`).
There is no code path that issues one.

Three things to understand before pointing this at a customer's PSA:

- **PUT is destructive.** It replaces the whole record, so a PUT carrying a
  partial body clears every field omitted from it. Prefer PATCH, which takes a
  JSON Patch array and changes only the fields you name.
- **`/system/` is in scope.** That is 714 operations covering members, security
  roles, API keys and callbacks. A callback registered by an agent is an
  outbound data channel that outlives the session.
- **Writes are immediate.** The API member's own ConnectWise security role is
  the only other control. Scope that role to what the agent actually needs
  rather than relying on this server to be narrow, because it no longer is.

To narrow the surface again, edit `INCLUDED_METHODS` / `SELECTED_CATEGORIES` in
[`scope.py`](src/connectwise_mcp/scope.py) and `WRITE_METHODS` in
[`writer.py`](src/connectwise_mcp/writer.py), then regenerate the catalog.

**Write auditing.** Every write logs actor, verb, path and resulting id to the
`connectwise_mcp.audit` logger. Request bodies are omitted by default because
they carry customer data; set `CW_AUDIT_BODIES=1` to include them, which is
worth doing if the runtime log destination is an acceptable home for that data.

## Credentials (resolved per request, in this order)

1. **Bearer token → tenant store** (hosted, multi-tenant). Set
   `CW_TENANTS_FILE` to a JSON file mapping tokens to ConnectWise credentials
   (see `tenants.example.json`). Clients send
   `Authorization: Bearer <token>`.
2. **`X-CW-*` headers** — for custom agents that control request headers:
   `X-CW-Company-Id`, `X-CW-Public-Key`, `X-CW-Private-Key`,
   `X-CW-Client-Id`, optional `X-CW-Region` / `X-CW-Host`.
3. **`CW_*` env vars** — local stdio / single tenant (see `.env.example`).

ConnectWise auth = HTTP Basic `base64(companyId+publicKey : privateKey)` plus
the required `clientId` header — both are built per request and never cached.

## Self-service onboarding

New users can connect without a pre-provisioned token. Connect to the gateway
with no credentials and call `get_started` — it walks through creating a
personal ConnectWise API key under **My Account → API Keys**. Then call
`validate_connection` with the new keys; it probes ConnectWise live and
returns a ready-to-paste config snippet that uses `X-CW-*` headers instead of
a Bearer token. Successful validations automatically file a heads-up ticket in
Will & Way's ConnectWise.

Operators can cut off any company at any time by adding its company id to the
`CW_BLOCKED_COMPANY_IDS` environment variable (comma-separated) and
redeploying. A push to master triggers a redeploy in ~1–2 minutes, which is
the revocation latency. See [docs/DEPLOY.md](docs/DEPLOY.md) for the full
variable reference.

## Connect from Claude Desktop

**Local (stdio), per user — simplest:** in `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "connectwise": {
      "command": "/path/to/connectwise-mcp/.venv/bin/connectwise-mcp",
      "env": {
        "CW_MCP_TRANSPORT": "stdio",
        "CW_COMPANY_ID": "...", "CW_PUBLIC_KEY": "...",
        "CW_PRIVATE_KEY": "...", "CW_CLIENT_ID": "...",
        "CW_REGION": "na"
      }
    }
  }
}
```

**Hosted (HTTP) with a tenant token** — Claude Desktop can't send custom
headers itself, so route through `mcp-remote`:

```json
{
  "mcpServers": {
    "connectwise": {
      "command": "npx",
      "args": [
        "mcp-remote", "https://your-host/mcp",
        "--header", "Authorization: Bearer YOUR_TENANT_TOKEN"
      ]
    }
  }
}
```

Run the hosted server behind TLS (reverse proxy) — tokens and results travel in
requests. (App Platform terminates TLS for you when deployed per DEPLOY.md.)

## Run

```bash
pip install -e ".[dev]"

# Hosted HTTP (default; set CW_TENANTS_FILE for multi-tenant)
connectwise-mcp                       # binds 127.0.0.1:8000

# Local stdio (uses CW_* env vars)
CW_MCP_TRANSPORT=stdio connectwise-mcp
```

For production hosting on DigitalOcean App Platform (TLS, tenant tokens, audit logs, health checks) see [docs/DEPLOY.md](docs/DEPLOY.md).

## Test

```bash
pytest                          # offline tests (catalog, executor, auth, tools)
python scripts/live_smoke.py    # live read checks; needs CW_* env vars
```

## Changing scope

The catalog is **documentation, not access control**: `cw_get` and `cw_call`
execute whatever path they are given, whether or not it appears in the catalog.
That is deliberate, because the bundled spec trails the live ConnectWise release
and refusing unknown paths would block endpoints that exist. Scope therefore
decides what `search_endpoints` and `describe_endpoint` can *find*; the verb
allowlist in `writer.WRITE_METHODS` is what decides what can be *executed*.

To change what the catalog documents, edit `SELECTED_CATEGORIES` /
`INCLUDED_METHODS` in [`scope.py`](src/connectwise_mcp/scope.py), download the
full spec from https://developer.connectwise.com, and regenerate:

```bash
python scripts/generate_catalog.py path/to/All.json
```

## Filtering with `conditions`

`cw_get` accepts ConnectWise's `conditions` query language, e.g.

```
status/name = 'Open' and board/id = 1
lastUpdated > [2026-01-01T00:00:00Z]
company/identifier = 'ACME'
```

The full cheatsheet lives in `conditions.py` and is embedded in the `cw_get`
tool description so the agent always has it inline.
