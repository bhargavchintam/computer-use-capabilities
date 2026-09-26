"""One place that decides what may leave the process: to the model, to logs, to disk.

Order matters: exact secret values first, then caller inputs (by declared
sensitivity), then PII patterns, then money. Replacement tokens are chosen so
they cannot be re-matched by a later pass.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
MASKED_SSN = re.compile(r"\*{3}-\*{2}-\d{4}")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
PHONE = re.compile(r"\(?\b\d{3}\)?[ .-]?\d{3}-\d{4}\b")
CARD = re.compile(r"\b(?:\d[ -]?){13,19}\b")
MONEY = re.compile(r"-?\$\s?\d{1,3}(?:,\d{3})+(?:\.\d{2})?|-?\$\s?\d+(?:\.\d{2})?")
API_KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}")


def _luhn(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = int(ch)
        if alt:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
        alt = not alt
    return total % 10 == 0


def _norm_label(s: str) -> str:
    return re.sub(r"[\s:*#]+$", "", re.sub(r"\s+", " ", s)).strip().lower()


def money_shape(text: str) -> str:
    """$12,450.31 -> $##,###.## : the model can reason about a value's type without seeing it."""
    return re.sub(r"\d", "#", text)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:12]


class Redactor:
    def __init__(self, sensitive_labels: list[str] | None = None) -> None:
        self._secrets: dict[str, str] = {}  # value -> name
        self._params: dict[str, tuple[str, str]] = {}  # value -> (name, sensitivity)
        self.sensitive_labels = {_norm_label(x) for x in (sensitive_labels or [])}

    # ------------------------------------------------------------------ registration
    def add_secret(self, name: str, value: str) -> None:
        if value:
            self._secrets[value] = name

    def add_param(self, name: str, value: str, sensitivity: str) -> None:
        if value and len(value) >= 3:
            self._params[value] = (name, sensitivity)

    @property
    def secret_values(self) -> list[str]:
        return list(self._secrets)

    def identifying_values(self) -> list[str]:
        """Values to paint over in screenshots: secrets and non-public caller inputs."""
        vals = list(self._secrets)
        vals += [v for v, (_, sens) in self._params.items() if sens != "public"]
        return vals

    def label_is_sensitive(self, label: str) -> bool:
        return _norm_label(label) in self.sensitive_labels

    # ------------------------------------------------------------------ text
    def scrub_text(self, text: str, *, for_model: bool = False) -> str:
        if not text:
            return text
        for value in sorted(self._secrets, key=len, reverse=True):
            text = re.sub(re.escape(value), f"<secret:{self._secrets[value]}>", text, flags=re.I)
        text = API_KEY.sub("<api-key>", text)
        for value in sorted(self._params, key=len, reverse=True):
            name, sens = self._params[value]
            pattern = rf"(?<![\w]){re.escape(value)}(?![\w])"
            if for_model:
                repl = "{{" + name + "}}"
            elif sens in ("public", "internal"):
                continue
            elif sens == "pii_identifier":
                repl = f"<{name}:…{value[-2:]}>"
            else:
                repl = f"<{name}>"
            text = re.sub(pattern, repl, text, flags=re.I)
        text = SSN.sub("<ssn>", text)
        text = MASKED_SSN.sub("<ssn>", text)
        text = EMAIL.sub("<email>", text)
        text = PHONE.sub("<phone>", text)
        text = CARD.sub(lambda m: "<card>" if _luhn(re.sub(r"\D", "", m.group())) else m.group(), text)
        if for_model:
            text = MONEY.sub(lambda m: money_shape(m.group()), text)
        else:
            text = MONEY.sub("<money>", text)
        return text

    def scrub_value_for_label(self, label: str, value: str, *, for_model: bool) -> str:
        """Values next to a sensitive label ("Name:", "SSN:") are masked outright."""
        if self.label_is_sensitive(label):
            return f"<pii:{_norm_label(label).replace(' ', '_')}>"
        return self.scrub_text(value, for_model=for_model)

    def scrub(self, obj: Any) -> Any:
        """Deep scrub for anything written to logs or disk."""
        if isinstance(obj, str):
            return self.scrub_text(obj)
        if isinstance(obj, dict):
            return {k: self.scrub(v) for k, v in obj.items()}
        if isinstance(obj, list | tuple):
            return [self.scrub(v) for v in obj]
        return obj

    def output_for_log(self, value: Any, sensitivity: str) -> Any:
        if sensitivity in ("public", "internal"):
            return value
        return {"redacted": sensitivity, "digest": digest(value)}
