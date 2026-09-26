"""The discovery agent's tools. The model acts on refs from the latest observation;
it never writes selectors. The runtime grounds every action into validated,
stable targets, which is what makes the run compilable into a capability."""

from __future__ import annotations

from typing import Any

REF = {"type": "string", "description": "An element ref from the LATEST observation, e.g. e12."}
REASON = {"type": "string", "description": "One short sentence: why this action (logged for auditors)."}


def _tool(name: str, description: str, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": props,
            "required": list(props),
            "additionalProperties": False,
        },
    }


def tool_definitions(output_names: list[str]) -> list[dict[str, Any]]:
    return [
        _tool("click", "Click a link, button or other control.", {"ref": REF, "reason": REASON}),
        _tool(
            "type_text",
            "Replace the contents of a text box. To enter a goal input, text must be exactly its "
            "placeholder, e.g. {{member_number}}; the runtime substitutes the real value.",
            {"ref": REF, "text": {"type": "string"}, "reason": REASON},
        ),
        _tool(
            "select_option",
            "Choose an option in a drop-down by its visible label (or an input placeholder).",
            {"ref": REF, "option": {"type": "string"}, "reason": REASON},
        ),
        _tool(
            "press_key",
            "Press a key while a control has focus.",
            {"ref": REF, "key": {"type": "string", "enum": ["Enter", "Tab", "Escape"]}, "reason": REASON},
        ),
        _tool(
            "wait_for_change",
            "Wait (1-10 seconds) for a slow page, then observe again.",
            {"seconds": {"type": "integer"}, "reason": REASON},
        ),
        _tool(
            "record_output",
            "Bind one requested output to the element that holds it (a table cell, a labelled value, "
            "or a whole table for list outputs). You do not need to read the value. For a list output, map "
            "each of its columns to the table header that holds it; for a single value pass an empty list.",
            {
                "name": {"type": "string", "enum": output_names},
                "ref": REF,
                "columns": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {"output_column": {"type": "string"}, "header": {"type": "string"}},
                        "required": ["output_column", "header"],
                        "additionalProperties": False,
                    },
                },
                "reason": REASON,
            },
        ),
        _tool(
            "report_outcome",
            "Stop because the application shows a legitimate business result that prevents the goal "
            "(e.g. no such member). code is UPPER_SNAKE_CASE; ref points at the message.",
            {"code": {"type": "string"}, "ref": REF, "description": {"type": "string"}},
        ),
        _tool(
            "request_human",
            "Ask a human operator for help when you are stuck, unsure, or a decision needs a person.",
            {"kind": {"type": "string", "enum": ["stuck", "needs_decision", "other"]}, "reason": REASON},
        ),
        _tool(
            "finish",
            "Declare the goal complete. All outputs must be recorded. success_ref is the element "
            "(usually the page title) that proves the goal state was reached.",
            {"summary": {"type": "string"}, "success_ref": REF},
        ),
    ]
