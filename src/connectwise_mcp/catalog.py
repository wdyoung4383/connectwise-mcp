"""Runtime catalog over the filtered (GET-only) ConnectWise OpenAPI spec.

The spec is data, not generated tools: the gateway tools search this catalog to
*find* an endpoint, describe its contract, then execute it. This keeps the whole
read surface reachable behind a handful of tools.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

_SPEC_RESOURCE = "openapi_get_filtered.json"


@dataclass
class Endpoint:
    operation_id: str
    method: str
    path: str
    module: str
    tags: list[str]
    summary: str
    parameters: list[dict[str, Any]] = field(default_factory=list)

    @property
    def path_params(self) -> list[str]:
        return [p["name"] for p in self.parameters if p.get("in") == "path"]

    @property
    def query_params(self) -> list[str]:
        return [p["name"] for p in self.parameters if p.get("in") == "query"]


class Catalog:
    def __init__(self, spec: dict[str, Any]):
        self._spec = spec
        self._schemas = spec.get("components", {}).get("schemas", {})
        self.endpoints: dict[str, Endpoint] = {}
        self._by_path: dict[str, Endpoint] = {}
        for path, item in spec.get("paths", {}).items():
            op = item.get("get")
            if not op:
                continue
            opid = op.get("operationId") or f"get {path}"
            ep = Endpoint(
                operation_id=opid,
                method="GET",
                path=path,
                module=path.strip("/").split("/")[0],
                tags=op.get("tags", []),
                summary=op.get("summary", "") or "",
                parameters=op.get("parameters", []),
            )
            self.endpoints[opid] = ep
            self._by_path[path] = ep

    # ------- lookups -------
    def modules(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for ep in self.endpoints.values():
            out[ep.module] = out.get(ep.module, 0) + 1
        return dict(sorted(out.items()))

    def get(self, operation_id: str) -> Endpoint | None:
        return self.endpoints.get(operation_id)

    def by_path(self, path: str) -> Endpoint | None:
        return self._by_path.get(path)

    def search(
        self, query: str, module: str | None = None, limit: int = 20
    ) -> list[Endpoint]:
        terms = [t for t in query.lower().split() if t]
        scored: list[tuple[int, Endpoint]] = []
        for ep in self.endpoints.values():
            if module and ep.module != module:
                continue
            haystack = " ".join(
                [ep.operation_id, ep.path, ep.summary, " ".join(ep.tags)]
            ).lower()
            if not terms:
                score = 1
            else:
                score = 0
                for t in terms:
                    if t in haystack:
                        score += 2
                        # boost exact-ish matches in path/tag
                        if t in ep.path.lower() or t in " ".join(ep.tags).lower():
                            score += 1
                if score == 0:
                    continue
            scored.append((score, ep))
        scored.sort(key=lambda s: (-s[0], s[1].path))
        return [ep for _, ep in scored[:limit]]

    # ------- schema resolution -------
    # Caps keep describe_endpoint output model-friendly: CW objects can have
    # 120+ fields and deep nesting that would otherwise flood the context.
    MAX_PROPS = 50
    MAX_DEPTH = 4

    def resolve_schema(self, ref_or_schema: Any, _depth: int = 0, _seen=None) -> Any:
        """Resolve $refs into a compact, model-friendly schema summary."""
        if _seen is None:
            _seen = set()
        if _depth > self.MAX_DEPTH or ref_or_schema is None:
            return {"note": "...truncated..."}
        node = ref_or_schema
        if isinstance(node, dict) and "$ref" in node:
            name = node["$ref"].split("/")[-1]
            if name in _seen:
                return {"$ref": name, "note": "recursive"}
            _seen = _seen | {name}
            node = self._schemas.get(name, {})
        if not isinstance(node, dict):
            return node

        t = node.get("type")
        if t == "array" or "items" in node:
            return {"type": "array", "items": self.resolve_schema(node.get("items"), _depth + 1, _seen)}
        if "properties" in node or t == "object":
            props = {}
            all_props = list((node.get("properties") or {}).items())
            required = set(node.get("required") or [])
            # Required fields first so they survive the cap.
            all_props.sort(key=lambda kv: (kv[0] not in required, kv[0]))
            for pname, pschema in all_props[: self.MAX_PROPS]:
                ps = self.resolve_schema(pschema, _depth + 1, _seen)
                props[pname] = ps if isinstance(ps, (dict, str)) else str(ps)
            out: dict[str, Any] = {"type": "object", "properties": props}
            if len(all_props) > self.MAX_PROPS:
                out["note"] = (
                    f"{len(all_props) - self.MAX_PROPS} more fields omitted; "
                    "use the `fields` query param to project specific columns."
                )
            if required:
                out["required"] = sorted(required)
            return out
        # primitive
        prim = {"type": t or "any"}
        if "format" in node:
            prim["format"] = node["format"]
        if "enum" in node:
            prim["enum"] = node["enum"]
        return prim

    def describe(self, operation_id: str) -> dict[str, Any] | None:
        ep = self.endpoints.get(operation_id)
        if not ep:
            return None
        params = []
        for p in ep.parameters:
            params.append(
                {
                    "name": p["name"],
                    "in": p.get("in"),
                    "required": p.get("required", p.get("in") == "path"),
                    "type": (p.get("schema") or {}).get("type", "string"),
                    "description": p.get("description", ""),
                }
            )
        # GET responses: surface the 200 body schema so callers know the shape.
        # ConnectWise uses a vendor media type (application/vnd.connectwise.com+json),
        # so take whichever content entry is present rather than assuming json.
        op = self._spec["paths"][ep.path]["get"]
        content = op.get("responses", {}).get("200", {}).get("content", {})
        resp = None
        for media in content.values():
            if isinstance(media, dict) and media.get("schema"):
                resp = media["schema"]
                break
        return {
            "operationId": ep.operation_id,
            "method": ep.method,
            "path": ep.path,
            "module": ep.module,
            "tags": ep.tags,
            "summary": ep.summary,
            "path_params": ep.path_params,
            "query_params": ep.query_params,
            "parameters": params,
            "response_schema": self.resolve_schema(resp) if resp else None,
        }


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    with resources.files("connectwise_mcp.data").joinpath(_SPEC_RESOURCE).open(
        "r", encoding="utf-8"
    ) as f:
        spec = json.load(f)
    return Catalog(spec)
