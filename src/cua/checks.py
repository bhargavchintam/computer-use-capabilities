"""Evaluate the checkpoint language against a live surface."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

from .models import Target, condition_kind
from .models.capability import InputSpec
from .models.common import LiteralValue, ParamRef, SecretRef
from .models.targets import TableCell
from .surface.web import WebSurface


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def money_variants(value: str) -> list[str]:
    try:
        d = Decimal(value.replace("$", "").replace(",", "").strip())
    except InvalidOperation:
        return [value]
    return list(dict.fromkeys([value, f"{d:.2f}", f"{d:,.2f}", f"${d:,.2f}"]))


def strategy_dicts(target: Target, params: dict[str, str]) -> list[dict[str, Any]]:
    """Strategies as JSON for the in-page resolver, with {param} row keys filled in."""
    out = []
    for s in target.strategies:
        d = s.model_dump(mode="json")
        if isinstance(s, TableCell) and isinstance(s.row.equals, ParamRef):
            d["row"]["equals"] = params[s.row.equals.param]
        out.append(d)
    return out


class Checks:
    def __init__(
        self,
        surface: WebSurface,
        *,
        params: dict[str, str] | None = None,
        input_specs: dict[str, InputSpec] | None = None,
        labels: dict[str, dict[str, str]] | None = None,
        secrets: dict[str, str] | None = None,
    ) -> None:
        self.surface = surface
        self.params = params or {}
        self.input_specs = input_specs or {}
        self.labels = labels or {}
        self.secrets = secrets or {}
        self.outputs: dict[str, Any] = {}

    # ------------------------------------------------------------------ values
    def resolve_value(self, ref: Any) -> str:
        if isinstance(ref, ParamRef):
            value = self.params[ref.param]
            return self.labels.get(ref.param, {}).get(value, value)
        if isinstance(ref, SecretRef):
            return self.secrets[ref.secret]
        if isinstance(ref, LiteralValue):
            return ref.literal
        raise TypeError(f"not a value ref: {ref!r}")

    def text_variants(self, text: str | ParamRef) -> list[str]:
        if isinstance(text, str):
            return [text]
        value = self.resolve_value(text)
        spec = self.input_specs.get(text.param)
        return money_variants(value) if spec is not None and spec.type == "money" else [value]

    # ------------------------------------------------------------------ evaluation
    async def holds(
        self,
        cond: Any,
        *,
        step_target: Target | None = None,
        before_docs: dict[tuple[str, ...], str] | None = None,
    ) -> bool:
        kind = condition_kind(cond)
        args = getattr(cond, kind)
        if kind == "all_of":
            for c in args:
                if not await self.holds(c, step_target=step_target, before_docs=before_docs):
                    return False
            return True
        if kind == "any_of":
            for c in args:
                if await self.holds(c, step_target=step_target, before_docs=before_docs):
                    return True
            return False
        if kind == "outputs_present":
            return all(self.outputs.get(name) is not None for name in args)
        if kind == "text_visible":
            text = await self.surface.page_text(args.container)
            if text is None:
                return False
            hay = _norm(text)
            return any(_norm(v) in hay for v in self.text_variants(args.text))
        if kind == "text_matches":
            text = await self.surface.page_text(args.container)
            return text is not None and re.search(args.pattern, text) is not None
        if kind == "document_changed":
            now = await self.surface.doc_id(args.container)
            before = (before_docs or {}).get(tuple(args.container))
            return now is not None and now != before
        if kind == "url_matches":
            path = self.surface.url_path(args.container)
            return path is not None and self._path_regex(args.path).match(path) is not None
        if kind == "element_present":
            res, _, _ = await self.surface.resolve(args.container, strategy_dicts(args, self.params))
            return res is not None
        if kind == "field_value":
            target = args.target or step_target
            if target is None:
                return False
            res, _, _ = await self.surface.resolve(target.container, strategy_dicts(target, self.params))
            if res is None:
                return False
            actual = _norm(await self.surface.read_text(res.element))
            return actual in {_norm(v) for v in money_variants(self.resolve_value(args.equals))}
        raise ValueError(f"unknown condition kind {kind}")

    async def snippet(self, pattern: str, container: list[str]) -> str | None:
        """The sentence around a regex hit, for outcome/failure messages."""
        text = await self.surface.page_text(container)
        if not text:
            return None
        for red in await self.surface.red_texts(container):
            if re.search(pattern, red):
                return red
        m = re.search(pattern, text)
        if not m:
            return None
        start = max(0, text.rfind(".", 0, m.start()) + 1)
        end = text.find(".", m.end())
        return text[start : end + 1 if end >= 0 else m.end() + 80].strip()

    def _path_regex(self, pattern: str) -> re.Pattern[str]:
        parts = []
        for seg in pattern.strip("/").split("/"):
            if seg.startswith(":"):
                parts.append(re.escape(self.params.get(seg[1:], "")))
            elif seg == "*":
                parts.append("[^/]+")
            else:
                parts.append(re.escape(seg))
        return re.compile("^/" + "/".join(parts) + "/?$")
