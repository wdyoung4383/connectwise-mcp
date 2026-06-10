# Deploying to DigitalOcean App Platform

This guide walks through deploying connectwise-mcp as a hosted, multi-tenant
service on DigitalOcean App Platform. You get TLS termination, tenant-token
auth, audit logging, and a `/health` endpoint out of the box.

---

## 1. Generate a tenant token

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

This produces a URL-safe random token (e.g. `xK8mQ2...`). Clients present it
as a Bearer credential — treat it like a password. Anyone who holds it can
query ConnectWise on behalf of the associated tenant, so keep it out of source
control and rotate it if it is ever exposed.

---

## 2. Build the CW_TENANTS JSON

The server reads tenant credentials from the `CW_TENANTS` environment variable
as a single-line JSON object whose keys are the tokens from step 1:

```json
{"<token-from-step-1>": {"company_id": "acme", "public_key": "abc123", "private_key": "secret", "client_id": "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx", "region": "na"}}
```

For multiple tenants (multiple clients sharing the same hosted server), add
more top-level keys — one per token:

```json
{"<token-A>": {"company_id": "acme", ...}, "<token-B>": {"company_id": "globex", ...}}
```

Keep this JSON string handy; you will paste it into the DigitalOcean console
in step 4. **Never commit real tokens or credentials to source control.**

---

## 3. Create the App Platform app

### Option A — doctl CLI

```bash
doctl apps create --spec .do/app.yaml
```

### Option B — DigitalOcean console

1. Go to **App Platform → Create App**.
2. Choose **GitHub** as your source.
3. Authorize DigitalOcean's GitHub integration if prompted — this is required
   to pull from the repo (including private repos).
4. Select the repo `wdyoung4383/connectwise-mcp` and branch **master**.
5. DigitalOcean detects `.do/app.yaml` automatically and pre-fills the app
   spec; confirm and proceed.

`deploy_on_push: true` in the spec means every push to `master` triggers a
new deploy automatically — no manual redeploys needed after merges.

---

## 4. Set the CW_TENANTS secret

The spec declares `CW_TENANTS` as a `SECRET` placeholder with a dummy value.
You must set the real value before the app will authenticate requests:

1. Open your app in the DO console.
2. Go to **Settings → server component → Environment Variables**.
3. Find `CW_TENANTS`, click **Edit**, and paste the single-line JSON you
   built in step 2.
4. Save — App Platform redeploys with the secret injected at runtime.

The secret is never stored in `.do/app.yaml` or visible in the build log.

---

## 5. Verify the deployment

Once the deploy finishes (green "Deployed" status), test the health endpoint:

```bash
curl https://<app-url>/health
```

Expected response:

```json
{"status": "ok"}
```

Replace `<app-url>` with the URL shown on the app's overview page
(e.g. `connectwise-mcp-xxxxx.ondigitalocean.app`).

---

## 6. Connect Claude Desktop

Add the following to your `claude_desktop_config.json`
(`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS,
`%APPDATA%\Claude\claude_desktop_config.json` on Windows):

```json
{
  "mcpServers": {
    "connectwise": {
      "command": "npx",
      "args": [
        "mcp-remote", "https://<app-url>/mcp",
        "--header", "Authorization: Bearer <token-from-step-1>"
      ]
    }
  }
}
```

Restart Claude Desktop after saving. The `mcp-remote` bridge handles the HTTP
transport and injects the Authorization header on every request.

---

## 7. Rotate a token

1. Generate a new token (step 1).
2. Edit the `CW_TENANTS` secret in the DO console: replace the old token key
   with the new one (or add the new one first, then remove the old one after
   clients have switched).
3. Save — App Platform redeploys automatically.

The old token stops working as soon as the new deploy is live. There is no
revocation delay.

---

## 8. Reading audit logs

In the DO console: **App Platform → your app → Runtime Logs**.

Write attempts produce log lines on stdout in this format:

```
WRITE ok company=acme path=/service/tickets id=42
WRITE fail company=acme path=/company/companies status=400 detail=...
```

`WRITE ok` = the POST was accepted by ConnectWise (`id` is the created
record). `WRITE fail` = the POST was rejected (`status` is ConnectWise's
HTTP status, or `error` for network/allowlist failures; `detail` is the
first 120 chars of the error). Each line includes the acting tenant's
company id and the target path so you can audit which tenant triggered
which write.

---

## 9. Self-service onboarding & the kill switch

New users do not need a pre-provisioned Bearer token. They connect to the
gateway with no credentials and call two tools:

1. `get_started` — returns a step-by-step walkthrough for creating a personal
   ConnectWise API key under **My Account → API Keys**. No credentials
   required.
2. `validate_connection` — takes the new keys, probes ConnectWise live, and
   returns a ready-to-paste `claude_desktop_config.json` snippet using
   `X-CW-*` headers. On success, a heads-up ticket is filed automatically.

### Environment variables

| Variable | Type | Purpose |
|----------|------|---------|
| `CW_DEFAULT_CLIENT_ID` | SECRET | Your ConnectWise integration `clientId`. Used for every self-service validation — self-service clients do not register their own. |
| `CW_BLOCKED_COMPANY_IDS` | plain | Comma-separated list of company ids that are denied access. Editing this value and redeploying is the revocation mechanism — a push triggers a redeploy in approximately 1–2 minutes, which is the revocation latency. Leave blank to allow all companies. |
| `CW_NOTIFY_TENANT` | SECRET | A Bearer token that **must exist as a key in `CW_TENANTS`**. The credentials behind that token are used to file the onboarding heads-up ticket. Leave blank to disable notifications. |
| `CW_NOTIFY_COMPANY` | plain | The `identifier` of the company in your ConnectWise instance the heads-up ticket is filed against. |
| `CW_NOTIFY_BOARD` | plain | The service board name the heads-up ticket is filed on (default: `Service Desk`). |

Set `CW_DEFAULT_CLIENT_ID` and `CW_NOTIFY_TENANT` in the DO console the same
way you set `CW_TENANTS` (Settings → server component → Environment
Variables). `CW_BLOCKED_COMPANY_IDS`, `CW_NOTIFY_COMPANY`, and
`CW_NOTIFY_BOARD` can be plain values set in the console or updated in
`.do/app.yaml` — each edit triggers an automatic redeploy.

---

## 10. Security model

| Layer | Detail |
|-------|--------|
| **TLS** | Terminated by App Platform; all traffic to `/mcp` and `/health` is HTTPS. |
| **Auth** | Bearer token looked up in `CW_TENANTS`. `CW_*` env-var credential fallback is **disabled** for HTTP requests by design — only tenant tokens or `X-CW-*` headers are accepted. |
| **Write scope** | Writes are limited to five allowlisted POST paths in `writer.py` (ticket, time entry, ticket note, company, contact). There is no update/delete path and no generic write gateway. |
| **Secret hygiene** | Real credentials live only in the DO secret store, never in the repo or build artifacts. |
