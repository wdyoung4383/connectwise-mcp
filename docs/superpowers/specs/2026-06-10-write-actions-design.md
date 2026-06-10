# Write actions for connectwise-mcp

**Date:** 2026-06-10
**Status:** Approved (option A: curated write tools over a hard-coded POST allowlist)

## Goal

Add five create actions to the currently read-only ConnectWise MCP server:
create a service ticket, create a time entry, create a note on a ticket,
create a company, and create a contact. No updates, no deletes, no generic
write gateway.

## Decisions made during brainstorming

| Decision | Choice |
|----------|--------|
| Write gating | Always enabled (no env flag, no per-tenant opt-in) |
| Input style | Friendly names (company name/identifier, board name, member identifier) resolved to IDs by the tools, with clear errors on ambiguous/missing matches |
| Extensibility | No `extra_fields` passthrough — strict minimal parameter sets (option A, not C) |
| Scope guarantee | Replaces "read-only by construction" with "writes only via five allowlisted POST paths, by construction" |

## Architecture

Two new modules, mirroring the existing read-side split
(`executor.py` / `curated.py`):

### 1. `src/connectwise_mcp/writer.py` — POST executor

- `ALLOWED_POSTS`: a frozen set of exactly five POST paths:
  - `/service/tickets`
  - `/time/entries`
  - `/service/tickets/{parentId}/notes`
  - `/company/companies`
  - `/company/contacts`
- `async cw_post(client, path, body, *, path_params=None) -> Any`
  - Raises `ExecutionError` immediately for any path not in `ALLOWED_POSTS`.
  - Fills `{...}` path segments via the same `_fill_path` logic as the GET
    executor (import or extract shared helper).
  - Sends `POST` with JSON body.
  - **Retry policy (differs from GET on purpose — POSTs are not
    idempotent):** retry only on 429 (honoring `Retry-After`, capped) and on
    `httpx.ConnectError`/connection-phase transport errors where the request
    never reached the server. Never retry on 5xx. Never retry on
    `ReadTimeout`-class errors (the server may have received the request).
  - On HTTP >= 400, raise `ExecutionError` using the existing
    `_classify_error` messages; for 400 include ConnectWise's per-field
    validation detail from the response body.
  - On success, return the created record with `_info` stripped
    (reuse `strip_info`).

### 2. `src/connectwise_mcp/curated_writes.py` — five MCP tools

Shared plumbing:

- `run_post(path, body, **kw)`: credential resolution + client lifecycle +
  error-to-`{"error": ...}` mapping, mirroring `curated.run_get`.
- Resolution helpers (each does a GET via the existing read path):
  - `_resolve_company(query)`: exact `identifier` match wins; else
    `name contains`; excludes `deletedFlag = true`. One hit → id. Multiple →
    error listing candidates (id, identifier, name). Zero → error.
  - `_resolve_board(name)`: exact name match against `/service/boards`
    (case-insensitive); same multiple/zero error shape.
  - `_resolve_member(identifier)`: exact identifier match against
    `/system/members`.
  - `_resolve_contact(name, company_id)`: `firstName`/`lastName` contains
    match scoped to the company.
- All tools return the created record as JSON (includes `id`) so the model
  can confirm and chain (e.g. note on the just-created ticket). Errors come
  back as `{"error": ...}` consistent with the read tools.

Tool signatures (required unless marked optional):

1. **`create_ticket`**
   - `summary: str`
   - `company: str` — name or identifier, resolved
   - `board: str` — board name, resolved
   - `initial_description: str | None` — becomes `initialDescription`
   - `priority: str | None` — priority name, e.g. "Priority 3 - Medium",
     sent as `priority: {"name": ...}` (CW resolves names on nested refs)
   - `status: str | None` — status name on the target board
   - `contact: str | None` — contact name, resolved within the company
2. **`create_time_entry`**
   - Exactly one of `ticket_id: int | None` (→ `chargeToId` +
     `chargeToType: "ServiceTicket"`) or `company: str | None`
     (→ `company: {"id": ...}`, `chargeToType: "CompanyManagement"`);
     supplying both or neither is an error.
   - `hours: float` — `actualHours`
   - `notes: str | None`
   - `time_start: str | None` — ISO 8601 UTC; default: now minus `hours`,
     formatted as CW expects (`YYYY-MM-DDTHH:MM:SSZ`)
   - `member: str | None` — member identifier, resolved; default: the API
     member (omit from body)
   - `billable: bool | None` — maps to `billableOption`
     ("Billable"/"DoNotBill"); omitted → CW default
3. **`create_ticket_note`**
   - `ticket_id: int`
   - `text: str`
   - `note_type: str = "discussion"` — enum `discussion` (sets
     `detailDescriptionFlag`), `internal` (`internalAnalysisFlag`),
     `resolution` (`resolutionFlag`)
4. **`create_company`**
   - `name: str`
   - `identifier: str` — validated ≤25 chars before sending
   - `phone, website, address_line, city, state, zip: str | None`
   - `company_type: str | None` — type name → `types: [{"name": ...}]`
   - `status: str | None` — status name → `status: {"name": ...}`
   - Auto-includes `site: {"name": "Main"}` (CW requires a site)
5. **`create_contact`**
   - `first_name: str`
   - `last_name: str | None`
   - `company: str | None` — name or identifier, resolved
   - `email: str | None`, `phone: str | None` — wrapped as
     `communicationItems` with `type {"name": "Email"|"Phone"}` and
     `defaultFlag: true`
   - `title: str | None`

### 3. `server.py` and docs

- `curated_writes.register(mcp)` after the existing `curated.register(mcp)`.
- Server `instructions` updated: read access as before, plus "five create
  actions: create_ticket, create_time_entry, create_ticket_note,
  create_company, create_contact. All other operations remain read-only."
- README: "Read-only by construction" section rewritten to state the actual
  guarantee — all GETs in catalog scope, writes only via the five
  allowlisted POSTs; tool table gains the five create tools.

## Error handling

- Allowlist violation → `ExecutionError` naming the allowed paths.
- Resolution failures → `{"error": ...}` listing candidates (ambiguous) or
  suggesting `find_company`/`search_endpoints` (no match).
- CW 400 validation errors → surfaced with CW's field-level messages so the
  model can correct and retry.
- Mutually-exclusive params (`ticket_id` vs `company` on time entries)
  validated before any network call.

## Testing

Offline (pytest, mocked httpx transport, following existing test style):

- `tests/test_writer.py`:
  - allowlist: the five paths pass, anything else raises before any request
  - retry: 429 retried with backoff; 500 NOT retried; connect-error retried;
    read-timeout NOT retried
  - 400 surfaces CW validation detail; success strips `_info`
- `tests/test_write_tools.py`:
  - per tool: body construction from friendly inputs (mock the resolution
    GETs), required-field validation, ticket/company exclusivity, note-type
    flag mapping, identifier length check
  - resolution helpers: single/multiple/zero match behavior

Live verification (manual, end of implementation): create one clearly
labeled test record per tool against the real instance — test company →
test contact in it → test ticket for it → note on that ticket → 0.25 h time
entry on it — report all five IDs for manual cleanup in ConnectWise
(deletes are out of scope by design).

## Out of scope

Updates/deletes, generic write gateway, catalog regeneration, tenant-store /
hosted-mode changes, live smoke script changes, automated cleanup of test
records.
