"""Runtime catalog over the bundled ConnectWise OpenAPI spec.

The spec is data, not generated tools: the gateway tools search this catalog to
*find* an operation and describe its contract, then execute it. That keeps the
whole API surface reachable behind a handful of tools rather than 3,000 of them.

The catalog documents; it does not gate. ``cw_get``/``cw_call`` will execute a
path that is absent here, because the bundled spec trails the live ConnectWise
release and refusing unknown paths would block endpoints that exist.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any

_SPEC_RESOURCE = "openapi_filtered.json"

METHODS = ("get", "post", "patch", "put", "delete")

# Words a caller is likely to use for an intent, mapped to the verb that serves
# it. Lets search_endpoints("create a ticket") rank POST above GET.
_VERB_HINTS = {
    "create": "POST", "new": "POST", "add": "POST", "post": "POST",
    "update": "PATCH", "edit": "PATCH", "change": "PATCH", "modify": "PATCH",
    "set": "PATCH", "patch": "PATCH", "close": "PATCH", "assign": "PATCH",
    "replace": "PUT", "put": "PUT", "overwrite": "PUT",
    "delete": "DELETE", "remove": "DELETE", "destroy": "DELETE",
    "get": "GET", "list": "GET", "find": "GET", "search": "GET",
    "read": "GET", "show": "GET", "fetch": "GET",
}

_WORD = re.compile(r"[a-z0-9]+")

# Filler words carry no signal but do break strict (AND) matching, forcing the
# fall back to loose OR ranking and burying the right answer.
_STOPWORDS = frozenset(
    {
        "a", "an", "the", "to", "for", "of", "in", "on", "at", "by", "with",
        "from", "and", "or", "my", "our", "all", "any", "me", "is", "are",
        "that", "this", "it", "its", "into", "endpoint", "endpoints", "api",
        "connectwise", "cw", "what", "which", "who", "when", "where", "did",
        "do", "does", "we", "i", "us", "have", "has", "been", "was", "were",
    }
)


def _words(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def _resource_words(path: str) -> set[str]:
    """Words of the last non-parameter path segment: the resource being named.

    Matching the resource ("companies" in /company/companies/{id}) is a much
    stronger signal than matching the module prefix, which every path under
    that module shares.
    """
    for seg in reversed(path.strip("/").split("/")):
        if not seg.startswith("{"):
            return _WORD.findall(seg.lower()) and set(_WORD.findall(seg.lower()))
    return set()


def _depluralize(term: str) -> str:
    """Crude singulariser so "note" matches the "notes" path segment.

    Only strip "es" after a sibilant (boxes, batches); a plain trailing "es" is
    usually just "e" + plural "s" (notes, types, entries), and stripping two
    characters there turns "notes" into "not" and kills the match.
    """
    if term.endswith("ies") and len(term) > 4:
        return term[:-3] + "y"
    if term.endswith(("ses", "xes", "zes", "ches", "shes")) and len(term) > 4:
        return term[:-2]
    if term.endswith("s") and not term.endswith("ss") and len(term) > 2:
        return term[:-1]
    return term


@dataclass
class Endpoint:
    operation_id: str
    method: str
    path: str
    module: str
    tags: list[str]
    summary: str
    parameters: list[dict[str, Any]] = field(default_factory=list)
    has_body: bool = False

    @property
    def path_params(self) -> list[str]:
        return [p["name"] for p in self.parameters if p.get("in") == "path"]

    @property
    def query_params(self) -> list[str]:
        return [p["name"] for p in self.parameters if p.get("in") == "query"]

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)


class Catalog:
    def __init__(self, spec: dict[str, Any]):
        self._spec = spec
        self._schemas = spec.get("components", {}).get("schemas", {})
        self.spec_version = spec.get("info", {}).get("version", "unknown")
        self.endpoints: dict[str, Endpoint] = {}
        self._by_key: dict[tuple[str, str], Endpoint] = {}
        self._search_blobs: dict[str, tuple[set[str], ...]] = {}

        for path, item in spec.get("paths", {}).items():
            for method in METHODS:
                op = item.get(method)
                if not isinstance(op, dict):
                    continue
                opid = op.get("operationId") or f"{method} {path}"
                ep = Endpoint(
                    operation_id=opid,
                    method=method.upper(),
                    path=path,
                    module=path.strip("/").split("/")[0],
                    tags=op.get("tags", []),
                    summary=op.get("summary", "") or "",
                    parameters=op.get("parameters", []),
                    has_body=bool(op.get("requestBody")),
                )
                self.endpoints[opid] = ep
                self._by_key[ep.key] = ep
                self._search_blobs[opid] = (
                    _words(path),
                    _resource_words(path),
                    _words(opid),
                    _words(" ".join(ep.tags)),
                    _words(ep.summary),
                )

    # ------- lookups -------
    def modules(self) -> dict[str, dict[str, int]]:
        """Per-module operation counts, broken down by verb."""
        out: dict[str, dict[str, int]] = {}
        for ep in self.endpoints.values():
            bucket = out.setdefault(ep.module, {"total": 0})
            bucket["total"] += 1
            bucket[ep.method] = bucket.get(ep.method, 0) + 1
        return dict(sorted(out.items()))

    def get(self, operation_id: str) -> Endpoint | None:
        return self.endpoints.get(operation_id)

    def by_path(self, path: str, method: str = "GET") -> Endpoint | None:
        return self._by_key.get((method.upper(), path))

    def methods_for(self, path: str) -> list[str]:
        """Which verbs the spec documents for a path."""
        return [m.upper() for m in METHODS if (m.upper(), path) in self._by_key]

    # ------- search -------
    def search(
        self,
        query: str,
        module: str | None = None,
        method: str | None = None,
        limit: int = 20,
    ) -> list[Endpoint]:
        """Rank operations against a keyword query.

        With 3,000+ operations in the catalog, loose OR-matching buries the
        right answer. Terms are ANDed first and only fall back to OR when that
        returns nothing. Path and operationId matches outweigh summary matches,
        and deeply nested sub-resources are penalised so ``/service/tickets``
        beats ``/service/tickets/{id}/configurations/{configurationId}``.
        """
        terms = [
            t for t in _WORD.findall(query.lower()) if t and t not in _STOPWORDS
        ]

        # An intent word ("create", "delete") both filters and stops polluting
        # the term list, since "create" appears in no path.
        inferred_method = None
        content_terms = []
        for t in terms:
            if t in _VERB_HINTS and inferred_method is None:
                inferred_method = _VERB_HINTS[t]
            else:
                content_terms.append(t)
        want_method = (method or inferred_method or "").upper() or None
        want_count = "count" in content_terms

        def score_one(ep: Endpoint, strict: bool) -> float | None:
            path_w, res_w, opid_w, tag_w, summ_w = self._search_blobs[ep.operation_id]
            total = 0.0
            matched = 0
            for t in content_terms:
                stem = _depluralize(t)
                hit = 0.0
                if t in res_w or stem in {_depluralize(w) for w in res_w}:
                    hit = max(hit, 5.0)
                elif t in path_w or stem in {_depluralize(w) for w in path_w}:
                    hit = max(hit, 2.5)
                if t in opid_w or stem in {_depluralize(w) for w in opid_w}:
                    hit = max(hit, 3.0)
                if t in tag_w or stem in {_depluralize(w) for w in tag_w}:
                    hit = max(hit, 3.0)
                if t in summ_w:
                    hit = max(hit, 1.0)
                if hit:
                    matched += 1
                    total += hit
            if content_terms:
                if strict and matched < len(content_terms):
                    return None
                if not strict and matched == 0:
                    return None
            else:
                total = 1.0

            # Shallow, collection-style paths are the usual intent.
            depth = ep.path.strip("/").count("/")
            total -= 0.5 * max(0, depth - 1)
            total -= 0.75 * len(ep.path_params)
            # /count endpoints are noise unless asked for.
            if ep.path.endswith("/count") and not want_count:
                total -= 3.0
            return total

        for strict in (True, False):
            scored: list[tuple[float, Endpoint]] = []
            for ep in self.endpoints.values():
                if module and ep.module != module:
                    continue
                if want_method and ep.method != want_method:
                    continue
                s = score_one(ep, strict)
                if s is None:
                    continue
                scored.append((s, ep))
            if scored:
                scored.sort(key=lambda s: (-s[0], len(s[1].path), s[1].path))
                return [ep for _, ep in scored[:limit]]
        return []

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
            return {
                "type": "array",
                "items": self.resolve_schema(node.get("items"), _depth + 1, _seen),
            }
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

    @staticmethod
    def _body_schema(op: dict[str, Any]) -> Any:
        content = (op.get("requestBody") or {}).get("content", {})
        for media in content.values():
            if isinstance(media, dict) and media.get("schema"):
                return media["schema"]
        return None

    @staticmethod
    def _response_schema(op: dict[str, Any]) -> Any:
        responses = op.get("responses", {})
        for code in ("200", "201", "202", "204"):
            content = responses.get(code, {}).get("content", {})
            for media in content.values():
                if isinstance(media, dict) and media.get("schema"):
                    return media["schema"]
        return None

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
        op = self._spec["paths"][ep.path][ep.method.lower()]
        # ConnectWise uses a vendor media type
        # (application/vnd.connectwise.com+json), so take whichever content
        # entry is present rather than assuming application/json.
        out: dict[str, Any] = {
            "operationId": ep.operation_id,
            "method": ep.method,
            "path": ep.path,
            "module": ep.module,
            "tags": ep.tags,
            "summary": ep.summary,
            "path_params": ep.path_params,
            "query_params": ep.query_params,
            "parameters": params,
            "other_methods_on_path": [
                m for m in self.methods_for(ep.path) if m != ep.method
            ],
        }
        body = self._body_schema(op)
        if body is not None:
            out["request_body"] = self.resolve_schema(body)
            if ep.method == "PATCH":
                out["request_body_note"] = (
                    "ConnectWise PATCH takes a JSON Patch array: "
                    '[{"op": "replace", "path": "/status/id", "value": 5}]. '
                    'op is one of add/replace/remove.'
                )
        resp = self._response_schema(op)
        out["response_schema"] = self.resolve_schema(resp) if resp else None
        return out


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    with resources.files("connectwise_mcp.data").joinpath(_SPEC_RESOURCE).open(
        "r", encoding="utf-8"
    ) as f:
        spec = json.load(f)
    return Catalog(spec)
