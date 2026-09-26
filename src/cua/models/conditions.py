"""A deliberately small checkpoint language.

Each condition is a single-key mapping so the YAML reads like a sentence:
``{text_visible: {container: ["frame:work"], text: "Member Summary"}}``.
It is used for step postconditions (``expect``), commit preconditions
(``pre``), final success, outcome detection and runtime detectors.
"""

from __future__ import annotations

from typing import Annotated, Any

from pydantic import Discriminator, Field, Tag

from .common import Container, ParamRef, Strict, ValueRef
from .targets import Target


class TextArgs(Strict):
    container: Container = Field(default_factory=list)
    text: str | ParamRef


class PatternArgs(Strict):
    container: Container = Field(default_factory=list)
    pattern: str


class ContainerArgs(Strict):
    container: Container = Field(default_factory=list)


class UrlArgs(Strict):
    container: Container = Field(default_factory=list)
    path: str  # route pattern; ":name" segments bind to params, "*" matches one segment


class FieldValueArgs(Strict):
    target: Target | None = None  # None: the step's own target
    equals: ValueRef


class TextVisible(Strict):
    text_visible: TextArgs


class TextMatches(Strict):
    text_matches: PatternArgs


class DocumentChanged(Strict):
    """The container's document was replaced since the action started.

    Legacy postbacks re-render at the same URL, so URL checks alone can pass on
    the *old* page; the init script stamps each document with a nonce instead.
    """

    document_changed: ContainerArgs


class UrlMatches(Strict):
    url_matches: UrlArgs


class ElementPresent(Strict):
    element_present: Target


class FieldValue(Strict):
    field_value: FieldValueArgs


class OutputsPresent(Strict):
    outputs_present: list[str]


class AllOf(Strict):
    all_of: list[Condition]


class AnyOf(Strict):
    any_of: list[Condition]


CONDITION_KINDS = (
    "text_visible",
    "text_matches",
    "document_changed",
    "url_matches",
    "element_present",
    "field_value",
    "outputs_present",
    "all_of",
    "any_of",
)


def _condition_tag(v: Any) -> str | None:
    if isinstance(v, dict):
        return next(iter(v)) if len(v) == 1 else None
    return next((k for k in CONDITION_KINDS if hasattr(v, k)), None)


Condition = Annotated[
    Annotated[TextVisible, Tag("text_visible")]
    | Annotated[TextMatches, Tag("text_matches")]
    | Annotated[DocumentChanged, Tag("document_changed")]
    | Annotated[UrlMatches, Tag("url_matches")]
    | Annotated[ElementPresent, Tag("element_present")]
    | Annotated[FieldValue, Tag("field_value")]
    | Annotated[OutputsPresent, Tag("outputs_present")]
    | Annotated[AllOf, Tag("all_of")]
    | Annotated[AnyOf, Tag("any_of")],
    Discriminator(_condition_tag),
]

AllOf.model_rebuild()
AnyOf.model_rebuild()


def condition_kind(c: Any) -> str:
    kind = _condition_tag(c)
    if kind is None:
        raise ValueError(f"not a condition: {c!r}")
    return kind


def describe(c: Any) -> str:
    """One-line human description used in failure reports."""
    kind = condition_kind(c)
    args = getattr(c, kind)
    where = ""
    container = getattr(args, "container", None)
    if container:
        where = f" in {'/'.join(container)}"
    if kind == "text_visible":
        text = args.text if isinstance(args.text, str) else "{{" + args.text.param + "}}"
        return f'text "{text}" visible{where}'
    if kind == "text_matches":
        return f"text matching /{args.pattern}/{where}"
    if kind == "document_changed":
        return f"document changed{where}"
    if kind == "url_matches":
        return f"url path matches {args.path}{where}"
    if kind == "element_present":
        s = args.strategies[0]
        return f"element present: {s.kind} {s.model_dump(exclude={'kind'})}"
    if kind == "field_value":
        return "field value equals expected input"
    if kind == "outputs_present":
        return f"outputs present: {', '.join(args)}"
    if kind in ("all_of", "any_of"):
        joiner = " AND " if kind == "all_of" else " OR "
        return "(" + joiner.join(describe(x) for x in args) + ")"
    return kind
