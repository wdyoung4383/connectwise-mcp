# Hosting Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the connectwise-mcp server safe to host on DigitalOcean App Platform: fail-closed credentials under HTTP, write audit logging, /health endpoint, and deployment artifacts.

**Architecture:** Three small code changes (auth.py fail-closed env gating; writer.py audit logger with an `actor` threaded from curated_writes; server.py /health route + logging bootstrap) plus deployment files (Dockerfile, .do/app.yaml, docs/DEPLOY.md).

**Tech Stack:** Python 3.10+, FastMCP 3.4.2 (custom_route, http_app), httpx ASGITransport for route tests, pytest caplog.

**Spec:** `docs/superpowers/specs/2026-06-10-hosting-hardening-design.md`

**Working directory:** `C:\Automation\Mann IT\connectwise-mcp\connectwise-mcp`. Run tests from the project root (pytest config lives in pyproject.toml): `.venv\Scripts\python.exe -m pytest`.

---

### Task 1: Fail-closed credential resolution

**Files:**
- Modify: `src/connectwise_mcp/auth.py`
- Test: `tests/test_auth.py`

- [ ] **Step 1: Write the failing tests** — append to `tests/test_auth.py`:

```python
def test_env_fallback_ignored_for_http_requests(monkeypatch):
    monkeypatch.setenv("CW_COMPANY_ID", "acme")
    monkeypatch.setenv("CW_PUBLIC_KEY", "pub")
    monkeypatch.setenv("CW_PRIVATE_KEY", "priv")
    monkeypatch.setenv("CW_CLIENT_ID", "guid-123")
    # any HTTP request carries headers (e.g. host) -> hosted context
    monkeypatch.setattr(auth, "get_http_headers", lambda: {"host": "x"})
    with pytest.raises(MissingCredentials, match="ignored for HTTP"):
        get_credentials()


def test_x_cw_headers_resolve_even_with_env_set(monkeypatch):
    monkeypatch.setenv("CW_COMPANY_ID", "operator")
    monkeypatch.setattr(
        auth,
        "get_http_headers",
        lambda: {
            "x-cw-company-id": "acme",
            "x-cw-public-key": "pub",
            "x-cw-private-key": "priv",
            "x-cw-client-id": "guid-123",
        },
    )
    assert get_credentials().company_id == "acme"


def test_partial_x_cw_headers_do_not_borrow_env(monkeypatch):
    # missing private key must NOT be silently filled from the operator env
    monkeypatch.setenv("CW_PRIVATE_KEY", "operator-secret")
    monkeypatch.setattr(
        auth,
        "get_http_headers",
        lambda: {
            "x-cw-company-id": "acme",
            "x-cw-public-key": "pub",
            "x-cw-client-id": "guid-123",
        },
    )
    with pytest.raises(MissingCredentials, match="private_key"):
        get_credentials()


def test_bearer_without_tenant_store_does_not_fall_to_env(monkeypatch):
    monkeypatch.setenv("CW_COMPANY_ID", "acme")
    monkeypatch.setenv("CW_PUBLIC_KEY", "pub")
    monkeypatch.setenv("CW_PRIVATE_KEY", "priv")
    monkeypatch.setenv("CW_CLIENT_ID", "guid-123")
    monkeypatch.setattr(
        auth, "get_http_headers", lambda: {"authorization": "Bearer tok"}
    )
    with pytest.raises(MissingCredentials, match="ignored for HTTP"):
        get_credentials()
```

- [ ] **Step 2: Run** `tests/test_auth.py` — the 4 new tests FAIL (env fallback currently serves them / message mismatch); the 8 existing tests pass.

- [ ] **Step 3: Implement** in `src/connectwise_mcp/auth.py`:

Replace `_pick` with:

```python
def _pick(
    headers: dict[str, str],
    header_name: str,
    env_name: str,
    *,
    allow_env: bool,
) -> str | None:
    # get_http_headers() lowercases keys; env is the local-stdio fallback.
    val = headers.get(header_name.lower())
    if val:
        return val
    return os.getenv(env_name) if allow_env else None
```

In `get_credentials()`, after the bearer-token block, insert:

```python
    # Env credentials exist for local stdio only. An HTTP request always
    # carries headers, so a non-empty dict means we are hosted: fail closed
    # instead of serving an unauthenticated caller with the operator's keys.
    allow_env = not h
```

and pass `allow_env=allow_env` to all six `_pick` calls (company, public, private, client_id, region, host).

Replace the `raise MissingCredentials(...)` block with:

```python
    if missing:
        hint = (
            " (CW_* env credentials are ignored for HTTP requests; send an "
            "Authorization: Bearer token or X-CW-* headers)"
            if not allow_env
            else ""
        )
        raise MissingCredentials(
            "Missing ConnectWise credentials: "
            + ", ".join(missing)
            + ". Supply an Authorization: Bearer token (hosted), X-CW-* request "
            "headers (custom agents), or CW_* env vars (local stdio)."
            + hint
        )
```

Update the module docstring's item 3 to read: `3. **CW_* environment variables** (local stdio ONLY — ignored whenever the request arrives over HTTP, so a hosted server never falls back to the operator's credentials): ...` (keep the variable list).

- [ ] **Step 4: Run the full suite** — expect 61 passed (57 + 4).

- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: fail-closed credentials — env fallback is stdio-only"`

---

### Task 2: Write audit logging

**Files:**
- Modify: `src/connectwise_mcp/writer.py`, `src/connectwise_mcp/curated_writes.py`
- Test: `tests/test_writer.py` (update call sites + 2 new tests), `tests/test_write_tools.py` (no changes expected — tools thread actor internally)

- [ ] **Step 1: Write the failing tests** — in `tests/test_writer.py`, add `import logging` to the imports, then add `actor="acme"` as a keyword argument to EVERY existing `cw_post(...)` call in the file, and append:

```python
async def test_audit_log_on_success(caplog):
    def handler(request):
        return httpx.Response(201, json={"id": 42})

    with caplog.at_level(logging.INFO, logger="connectwise_mcp.audit"):
        async with make_client(handler) as client:
            await cw_post(client, "/service/tickets", {"summary": "s"}, actor="acme")
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        m == "WRITE ok company=acme path=/service/tickets id=42" for m in messages
    )


async def test_audit_log_on_failure(caplog):
    def handler(request):
        return httpx.Response(400, text="identifier taken")

    with caplog.at_level(logging.INFO, logger="connectwise_mcp.audit"):
        async with make_client(handler) as client:
            with pytest.raises(ExecutionError):
                await cw_post(client, "/company/companies", {"name": "X"}, actor="acme")
    messages = [r.getMessage() for r in caplog.records]
    assert any(
        m.startswith("WRITE fail company=acme path=/company/companies status=400")
        for m in messages
    )
```

- [ ] **Step 2: Run** `tests/test_writer.py` — fails (`cw_post() got an unexpected keyword argument 'actor'`).

- [ ] **Step 3: Implement.** In `src/connectwise_mcp/writer.py`:

Add to imports: `import logging`, and below the imports:

```python
_audit = logging.getLogger("connectwise_mcp.audit")
```

Change `cw_post`'s signature to:

```python
async def cw_post(
    client: httpx.AsyncClient,
    path: str,
    body: dict[str, Any],
    *,
    path_params: dict[str, Any] | None = None,
    actor: str,
) -> Any:
```

and rework its body to log every attempt (no tokens/keys/body contents ever logged — only actor company id, path, status/id):

```python
    if path not in ALLOWED_POSTS:
        _audit.info(
            "WRITE fail company=%s path=%s status=error detail=%.120s",
            actor, path, "path not in allowlist",
        )
        raise ExecutionError(
            f"POST {path!r} is not allowed. This server only writes to: "
            + ", ".join(sorted(ALLOWED_POSTS))
        )
    url = _fill_path(path, path_params)
    try:
        resp = await _post_with_retries(client, url, body)
    except ExecutionError as e:
        _audit.info(
            "WRITE fail company=%s path=%s status=error detail=%.120s",
            actor, path, str(e),
        )
        raise
    if resp.status_code >= 400:
        _audit.info(
            "WRITE fail company=%s path=%s status=%s detail=%.120s",
            actor, path, resp.status_code, resp.text,
        )
        raise ExecutionError(_classify_post_error(resp.status_code, resp.text))
    try:
        data = resp.json()
    except ValueError:
        data = {"raw": resp.text}
    data = strip_info(data)
    _audit.info(
        "WRITE ok company=%s path=%s id=%s",
        actor, path, data.get("id") if isinstance(data, dict) else None,
    )
    return data
```

In `src/connectwise_mcp/curated_writes.py`, change `_run_write` to hand the
acting tenant's company id to the workload:

```python
async def _run_write(fn) -> Any:
    """Resolve credentials, open a client, run ``fn(client, actor)``,
    map errors. ``actor`` is the tenant's ConnectWise company id, used for
    write audit logging."""
    try:
        creds = get_credentials()
    except MissingCredentials as e:
        return {"error": str(e)}
    try:
        async with make_client(creds) as client:
            return await fn(client, creds.company_id)
    except ExecutionError as e:
        return {"error": str(e)}
```

and update all five tools: each inner `async def go(client):` becomes
`async def go(client, actor):` and each `cw_post(client, <path>, body, ...)`
call gains `actor=actor`.

- [ ] **Step 4: Run the full suite** — expect 63 passed.

- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: audit log every write attempt (actor, path, outcome)"`

---

### Task 3: /health route + logging bootstrap

**Files:**
- Modify: `src/connectwise_mcp/server.py`
- Test: `tests/test_tools.py` (append)

- [ ] **Step 1: Write the failing test** — append to `tests/test_tools.py`:

```python
async def test_health_route():
    import httpx

    from connectwise_mcp.server import mcp

    app = mcp.http_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

(If `mcp.http_app()` requires a running lifespan for the custom route, wrap the client usage in `async with app.router.lifespan_context(app):` — adapt the test, not the route.)

- [ ] **Step 2: Run it** — fails (404 or AttributeError).

- [ ] **Step 3: Implement** in `src/connectwise_mcp/server.py`:

Add imports:

```python
from starlette.requests import Request
from starlette.responses import JSONResponse
```

After the `cw_get` tool definition (before `curated.register(mcp)`):

```python
@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Unauthenticated liveness probe for hosted deployments."""
    return JSONResponse({"status": "ok"})
```

In `main()`, before transport selection, bootstrap stdout logging without clobbering embedders:

```python
    import logging

    if not logging.getLogger().handlers:
        logging.basicConfig(
            level=logging.INFO, format="%(asctime)s %(name)s %(message)s"
        )
```

- [ ] **Step 4: Run the full suite** — expect 64 passed.

- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: /health route and stdout logging bootstrap"`

---

### Task 4: Deployment artifacts

**Files:**
- Create: `Dockerfile`, `.do/app.yaml`, `docs/DEPLOY.md`
- Modify: `README.md` (Run section pointer)

- [ ] **Step 1: Create `Dockerfile`** (repo root):

```dockerfile
FROM python:3.12-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home appuser
USER appuser

# App Platform routes to the container port; bind beyond loopback here only.
ENV CW_MCP_HOST=0.0.0.0 \
    CW_MCP_PORT=8080
EXPOSE 8080

CMD ["connectwise-mcp"]
```

- [ ] **Step 2: Create `.do/app.yaml`:**

```yaml
name: connectwise-mcp
services:
  - name: server
    dockerfile_path: Dockerfile
    source_dir: /
    github:
      repo: wdyoung4383/connectwise-mcp
      branch: master
      deploy_on_push: true
    http_port: 8080
    instance_count: 1
    instance_size_slug: basic-xxs
    health_check:
      http_path: /health
    envs:
      - key: CW_TENANTS
        scope: RUN_TIME
        type: SECRET
        value: "REPLACE_IN_DO_CONSOLE"
```

- [ ] **Step 3: Create `docs/DEPLOY.md`** covering, in order: (1) generate a tenant token with `python -c "import secrets; print(secrets.token_urlsafe(32))"`; (2) build the `CW_TENANTS` JSON value `{"<token>": {"company_id": "...", "public_key": "...", "private_key": "...", "client_id": "...", "region": "na"}}`; (3) create the app via `doctl apps create --spec .do/app.yaml` or the DO console (App Platform → Create App → GitHub repo), authorizing DO's GitHub access to the private repo; (4) set the `CW_TENANTS` secret in the app's settings (never commit it); (5) confirm the deploy is healthy (`/health` returns `{"status": "ok"}`); (6) connect Claude Desktop through `npx mcp-remote https://<app-url>/mcp --header "Authorization: Bearer <token>"` (full JSON snippet); (7) token rotation = edit the secret and redeploy; (8) audit logs live in App Platform → Runtime Logs, lines starting `WRITE ok|fail`; (9) note that env-credential fallback is disabled for HTTP requests by design — the only way in is a tenant token or X-CW-* headers. Write it as numbered steps with exact commands/JSON, ~60-90 lines.

- [ ] **Step 4: README pointer** — in the Run section, after the hosted-HTTP lines, add: `See docs/DEPLOY.md for a complete DigitalOcean App Platform deployment guide (TLS, tenant tokens, audit logs).` And update the "Run the hosted server behind TLS" sentence to mention App Platform terminates TLS for you when deployed per DEPLOY.md.

- [ ] **Step 5: Verify + commit** — run the full suite (64 passed, nothing code-side changed), `docker build .` only if Docker is available (skip silently if not), then `git add -A && git commit -m "feat: DigitalOcean App Platform deployment artifacts"`

---

### Task 5: Ship

- [ ] Full suite green (`.venv\Scripts\python.exe -m pytest` → 64 passed)
- [ ] Final holistic review (subagent) of the whole range
- [ ] Push master to GitHub (existing origin remote — established destination)
- [ ] Report next manual steps to the user (create the app in DO, set the secret)
