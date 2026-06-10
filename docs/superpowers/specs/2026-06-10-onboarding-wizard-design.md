# Self-service onboarding wizard, company blocklist, onboarding notifications

**Date:** 2026-06-10
**Status:** Approved

## Goal

A brand-new client connects Claude Desktop to the hosted gateway with no
credentials and gets walked through connecting: generate a personal
ConnectWise API key, validate it live, receive a ready-to-paste config.
Will & Way keeps a kill switch (company-id blocklist) and gets a heads-up
ticket in their own ConnectWise whenever someone onboards.

## Decisions

| Decision | Choice |
|----------|--------|
| Connection model | Self-service: client credentials ride as `X-CW-*` headers (already supported); no tenant-store entry needed |
| API key guidance | **Personal API keys** (My Account → API Keys in ConnectWise) — inherit the member's own permissions, so the connector can never exceed what the user themselves can do. NOT the admin "API Members" flow. |
| clientId | `CW_DEFAULT_CLIENT_ID` server env var (Will & Way's integration id) used whenever a request/validation lacks one. Clients never deal with clientIds. |
| Kill switch | `CW_BLOCKED_COMPANY_IDS` env var (comma-separated, case-insensitive), enforced centrally in credential resolution — blocks every auth path |
| Onboarding heads-up | On successful validation, the server creates a ticket in Will & Way's own ConnectWise via a designated tenant entry. Fire-and-forget: notification failure never fails onboarding. |
| Token tenants | Unchanged; both auth methods remain valid |

## Components

### 1. `auth.py`

- **Default clientId:** in `get_credentials()`, when no client id arrived via
  tenant entry, header, or (stdio-only) env, fall back to
  `os.getenv("CW_DEFAULT_CLIENT_ID")`. This env read is ALWAYS allowed —
  deliberately exempt from the HTTP fail-closed rule because a clientId is
  integration identity, not an access credential (useless without valid
  keys). Comment must state this rationale. `client_id` then only appears in
  the missing-fields error when neither source provided it.
- **Blocklist:** `_blocked_company_ids() -> frozenset[str]` parses
  `CW_BLOCKED_COMPANY_IDS` (comma-separated, trimmed, lowercased; empty/unset
  → empty set). At the END of `get_credentials()` (after successful
  resolution, covering tenant-store, header, and env paths), if
  `company_id.lower()` is in the set, raise `MissingCredentials` with:
  "Access for company '<id>' is disabled. Contact Will & Way Solutions
  (book.willandway.solutions)." Read env fresh per call (no caching) so a
  redeploy-with-new-env always takes effect.
- **Onboarding pointer:** the generic missing-credentials message gains a
  final sentence: "New here? Call the get_started tool to set up a
  connection."

### 2. `onboarding.py` (new module, credential-free tools)

- **`get_started()`** → static structured walkthrough (dict). Content:
  1. Sign in to ConnectWise Manage.
  2. Click your avatar/initials (top right) → **My Account** → **API Keys**
     tab → **+** (new key) → description "Will & Way Claude Connector" →
     Save. Copy the **public key** and the **private key** (the private key
     is shown only once — copy it now).
  3. Note: the key inherits YOUR permissions — Claude will only see and do
     what your ConnectWise account allows.
  4. Collect: your login company id (the one typed on the CW login screen),
     public key, private key, and region (na/eu/au/aus/za — na if unsure).
  5. "Now call validate_connection with those values."
- **`validate_connection(company_id, public_key, private_key, region="na")`**:
  1. Blocklist check first (same set as auth) → refuse with the same
     "access disabled" message; no probe performed.
  2. Build `CWCredentials` with clientId from `CW_DEFAULT_CLIENT_ID`
     (error with a clear server-misconfiguration message if that env is
     unset).
  3. Live probes (read-only, page_size=1): GET `/service/boards` and GET
     `/company/companies`. Classify outcomes: 401 → "keys rejected — check
     for copy/paste errors, regenerate if needed"; 403 → "your account's
     security role does not allow API access / this module"; network → named.
  4. On success returns: `connected: true`, a complete
     `claude_desktop_config.json` snippet (mcp-remote with FOUR `--header`
     args: X-CW-Company-Id, X-CW-Public-Key, X-CW-Private-Key, X-CW-Region),
     and next steps ("replace your current connectwise entry with this,
     fully restart Claude Desktop, then ask: What service boards do we
     have?").
  5. Fire the heads-up notification (below), then audit-log
     `ONBOARD validated company=<id> region=<r>` (never any key material; on
     failure log `ONBOARD failed company=<id> reason=<class>`).

### 3. Onboarding notification

- Config via env: `CW_NOTIFY_TENANT` = a token that must exist in the tenant
  store (reuses the existing willandway entry — no new secret material);
  `CW_NOTIFY_COMPANY` = company identifier in Will & Way's CW to file the
  ticket against (e.g. their internal company record);
  `CW_NOTIFY_BOARD` = service board name (e.g. "Service Desk").
- On successful validation: using the notify tenant's credentials, create a
  ticket via the existing write path (`cw_post`, `/service/tickets` is
  allowlisted; actor string "onboarding"): summary
  "New Claude connector onboarding: <company_id>", initialDescription with
  company id and region ONLY (never keys).
- Fire-and-forget: any failure (env unset, tenant missing, CW error) is
  caught, logged at WARN ("onboarding notification failed: ..."), and the
  client's onboarding still succeeds. If the notify envs are unset the step
  is silently skipped (log at INFO once per call).

### 4. `server.py`

- Register the two onboarding tools.
- `instructions` gains: "Not connected yet? The get_started tool walks new
  users through creating a ConnectWise API key and connecting."

### 5. Deployment artifacts

- `.do/app.yaml`: add envs — `CW_DEFAULT_CLIENT_ID` (SECRET),
  `CW_BLOCKED_COMPANY_IDS` (plain, default empty), `CW_NOTIFY_TENANT`
  (SECRET), `CW_NOTIFY_COMPANY` (plain), `CW_NOTIFY_BOARD` (plain).
- `docs/DEPLOY.md`: document all five (what they do, that blocklist edits
  trigger redeploy ≈ the revocation latency, notify-tenant must be a token
  present in CW_TENANTS).
- README: one paragraph on self-service onboarding + blocklist.

## Error handling

- Wizard tools never raise to the protocol: all failures return
  `{"error"/"connected": false, guidance}` shapes the model can relay.
- Blocklist message is identical across auth paths and the wizard.
- validate_connection probes use the existing read executor; its
  `ExecutionError` classifications are mapped to wizard-friendly guidance.

## Testing

Offline (mock transport, monkeypatched env):

- Blocklist: blocks header/token/stdio-env paths; case-insensitivity;
  whitespace tolerance; empty/unset → no blocking; message content.
- Default clientId: header-supplied wins; env default fills the gap under
  HTTP and stdio; absent both → client_id in missing-fields error.
- get_started: mentions "My Account", "API Keys", private-key-shown-once
  warning, permissions-inherit note, validate_connection pointer.
- validate_connection: success returns snippet containing all four X-CW
  headers and the user's values; 401/403 mapped to friendly guidance;
  blocked id refused before any HTTP; missing CW_DEFAULT_CLIENT_ID handled.
- Notification: success creates ticket via mocked POST (assert path+summary,
  no keys in body); notify failure does NOT fail onboarding; unset envs skip.
- Secrets hygiene: caplog scan — private_key value never in any log record.

Live verification (after deploy): run get_started + validate_connection
against the hosted server with a real personal API key; confirm the config
snippet works (one read through new headers); confirm the heads-up ticket
appears in Will & Way's ConnectWise; blocklist round-trip (add a test id,
confirm refusal, remove).

## Out of scope

Persistent tenant provisioning, client guide doc rewrite (follow-up — the
wizard supersedes parts of it), rate-limiting the wizard, CAPTCHA-style
abuse protection (the wizard only validates credentials a caller already
possesses).
