"""File-based capability registry: capabilities/<product>/<id>/<version>.yaml.

Artifacts are reviewed like code (YAML diffs in a PR). Approval records the
reviewer and signs the content hash; an edited file is no longer approved.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from packaging.version import Version

from .configio import load_model, model_to_yaml, repo_root
from .models import Approval, Capability


class NotFound(LookupError):
    pass


class Registry:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or repo_root() / "capabilities"

    def path_for(self, cap: Capability) -> Path:
        return self.root / cap.implementation.app.product / cap.id / f"{cap.version}.yaml"

    def all(self) -> list[Capability]:
        if not self.root.exists():
            return []
        return [load_model(p, Capability) for p in sorted(self.root.rglob("*.yaml"))]

    def versions(self, cap_id: str) -> list[Capability]:
        caps = [c for c in self.all() if c.id == cap_id]
        return sorted(caps, key=lambda c: Version(c.version))

    def get(self, ref: str) -> Capability:
        cap_id, _, version = ref.partition("@")
        caps = self.versions(cap_id)
        if not caps:
            raise NotFound(f"no capability {cap_id!r} in {self.root}")
        if version:
            for c in caps:
                if c.version == version:
                    return c
            raise NotFound(f"{cap_id} has no version {version} (have {[c.version for c in caps]})")
        return caps[-1]

    def save(self, cap: Capability, *, overwrite: bool = False) -> Path:
        path = self.path_for(cap)
        if path.exists() and not overwrite:
            raise FileExistsError(
                f"{path} exists; bump the version instead of rewriting a published artifact"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(model_to_yaml(cap), encoding="utf-8")
        return path

    def approve(self, ref: str, reviewer: str) -> Capability:
        cap = self.get(ref)
        approved = cap.model_copy(
            update={
                "status": "approved",
                "approval": Approval(
                    approved_by=reviewer,
                    approved_at=datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
                    content_sha256=cap.content_sha256(),
                ),
            }
        )
        self.save(approved, overwrite=True)
        return approved
