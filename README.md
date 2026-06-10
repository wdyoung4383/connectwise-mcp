# ConnectWise PSA MCP Server

A [FastMCP](https://gofastmcp.com) server that exposes **ConnectWise Manage
(PSA)** as a gateway for AI agents: catalog-wide reads plus five curated
create actions.

## Why a gateway, not 300 tools

The ConnectWise API has thousands of operations; the in-scope read subset alone
is **324 GET endpoints** across 71 categories. Exposing one tool per endpoint
would overwhelm any LLM client. Instead the OpenAPI spec is loaded as a runtime
**catalog**, with gateway tools in front of it, plus a few curated shortcuts:

| Tool | Purpose |
|------|---------|
| `list_modules` | Orientation: modules + endpoint counts |
| `search_endpoints` | Find a GET endpoint by keyword |
| `describe_endpoint` | See an endpoint's params + response shape |
| `cw_get` | Execute any in-scope GET (paging, `conditions` filtering, `_info` stripping) |
| `search_tickets` / `get_ticket` | Service tickets without conditions syntax |
| `find_company` | Companies by name/identifier |
| `list_agreements` | Finance agreements, optionally per company |
| `recent_time_entries` | Time entries from the last N days |
| `create_ticket` / `create_ticket_note` | Create a service ticket / add a note to one |
| `create_time_entry` | Log time against a ticket or company |
| `create_company` / `create_contact` | Create a company / contact |

**Write scope by construction:** reads cover the whole GET catalog; writes
are limited to five allowlisted POST paths in `writer.py` (ticket, time
entry, ticket note, company, contact). There is no update/delete code path,
and no generic write gateway.

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

Only `GET` operations under the categories in
[`scope.py`](src/connectwise_mcp/scope.py) are exposed. To change scope, edit
that set, download the full spec from https://developer.connectwise.com, and
regenerate:

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
