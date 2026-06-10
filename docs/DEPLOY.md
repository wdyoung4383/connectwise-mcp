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
WRITE ok  | company_id=acme path=/service/tickets
WRITE fail | company_id=acme path=/service/tickets/999/notes
```

`WRITE ok` = the POST was accepted by ConnectWise.
`WRITE fail` = the POST was rejected (4xx/5xx from ConnectWise, or path not
allowlisted). Each line includes the acting `company_id` and the target path
so you can audit which tenant triggered which write.

---

## 9. Security model

| Layer | Detail |
|-------|--------|
| **TLS** | Terminated by App Platform; all traffic to `/mcp` and `/health` is HTTPS. |
| **Auth** | Bearer token looked up in `CW_TENANTS`. `CW_*` env-var credential fallback is **disabled** for HTTP requests by design — only tenant tokens or `X-CW-*` headers are accepted. |
| **Write scope** | Writes are limited to five allowlisted POST paths in `writer.py` (ticket, time entry, ticket note, company, contact). There is no update/delete path and no generic write gateway. |
| **Secret hygiene** | Real credentials live only in the DO secret store, never in the repo or build artifacts. |
