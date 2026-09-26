"""Render a page snapshot as compact text, for the model or for evidence.

Everything goes through the redactor: values beside sensitive labels (or
under sensitive column headers) are masked outright, money keeps only its shape for the model, and caller inputs
appear as their {{placeholder}} so the model never sees concrete values.
"""

from __future__ import annotations

from typing import Any

from .redaction import Redactor
from .surface.base import PageSnapshot


def _q(s: str) -> str:
    return '"' + s.replace('"', "'") + '"'


def render(snapshot: PageSnapshot, redactor: Redactor, *, for_model: bool, max_rows: int = 25) -> str:
    lines: list[str] = []
    scrub = lambda s: redactor.scrub_text(s, for_model=for_model)  # noqa: E731
    for fr in snapshot.frames:
        name = "/".join(fr.container) or "top"
        if fr.frameset:
            lines.append(f"== {name} (frameset) {fr.url_path}")
            continue
        lines.append(f"== {name} {fr.url_path}")
        for it in fr.items:
            lines.extend("  " + ln for ln in _item(it, redactor, scrub, for_model, max_rows))
    return "\n".join(lines)


def _item(it: dict[str, Any], redactor: Redactor, scrub: Any, for_model: bool, max_rows: int) -> list[str]:
    t = it["t"]
    if t == "text":
        text = scrub(it["text"])
        ref = f"[{it['ref']}] " if it.get("ref") else ""
        if it.get("red"):
            return [f"{ref}!! {text}"]
        return [f"{ref}**{text}**" if (it.get("b") or it.get("big")) else text]
    if t == "el":
        parts = [f"[{it['ref']}] {it['role']}"]
        if it.get("name"):
            parts.append(_q(scrub(it["name"])))
        if it.get("label"):
            parts.append(f"label={_q(scrub(it['label']))}")
        if "value" in it:
            value = it.get("value") or ""
            if it.get("secret"):
                value = "••••" if value else ""
            else:
                value = scrub(value)
            parts.append(f"value={_q(value)}")
        if it.get("options"):
            parts.append("options=[" + ", ".join(scrub(o) for o in it["options"]) + "]")
        if it.get("disabled"):
            parts.append("(disabled)")
        if "checked" in it:
            parts.append("(checked)" if it["checked"] else "(unchecked)")
        return [" ".join(parts)]
    if t == "kv":
        out = [f"[{it['ref']}] fields:"]
        for p in it["pairs"]:
            value = redactor.scrub_value_for_label(p["label"], p["text"], for_model=for_model)
            out.append(f"    {scrub(p['label'])} [{p['ref']}] {value}")
        return out
    if t == "table":
        hdrs = list(it["headers"])
        headers = " | ".join(scrub(h) for h in hdrs)
        out = [f"[{it['ref']}] table ({it.get('total_rows', len(it['rows']))} rows): {headers}"]
        for row in it["rows"][:max_rows]:
            cells = []
            for i, c in enumerate(row):
                # a column headed "Name" holds PII just like a "Name:" field does
                header = hdrs[i] if i < len(hdrs) else ""
                cells.append(
                    f"[{c['ref']}] {redactor.scrub_value_for_label(header, c['text'], for_model=for_model)}"
                )
            out.append("    " + " | ".join(cells))
        if len(it["rows"]) > max_rows:
            out.append(f"    … {len(it['rows']) - max_rows} more rows")
        return out
    return []


def marks(snapshot: PageSnapshot) -> list[tuple[str, list[float]]]:
    """Page-coordinate boxes for actionable elements (set-of-marks on screenshots)."""
    out: list[tuple[str, list[float]]] = []
    for fr in snapshot.frames:
        ox, oy = fr.offset
        for it in fr.items:
            if it["t"] == "el" and it.get("rect"):
                x, y, w, h = it["rect"]
                out.append((it["ref"], [x + ox, y + oy, w, h]))
    return out
