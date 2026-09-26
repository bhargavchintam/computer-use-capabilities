"""Resolve the capability that actually runs for a tenant: base + version overlays.

Capabilities belong to a vendor product and version range, not to a tenant.
Overlays keyed by vendor version are shared by every tenant on that release,
so cost grows as N (capabilities) + M (overlays) rather than N x tenants.
The effective plan is hashed together with everything that shapes behaviour
(overlays, app profile, policy) and that hash is reported with every result.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from packaging.specifiers import SpecifierSet
from packaging.version import InvalidVersion, Version

from .models import AppProfile, Capability, Overlay, Policy, TenantConfig, sha256_of


def version_in(spec: str, version: str) -> bool:
    try:
        return Version(version) in SpecifierSet(spec)
    except InvalidVersion:
        return False


@dataclass
class EffectivePlan:
    capability: Capability  # base with overlays applied (re-validated)
    base: Capability
    overlays_applied: list[str] = field(default_factory=list)
    labels: dict[str, dict[str, str]] = field(default_factory=dict)
    plan_sha256: str = ""
    overlays: list[Overlay] = field(default_factory=list)  # the ones that changed something


def resolve_plan(
    cap: Capability,
    tenant: TenantConfig,
    app: AppProfile,
    policy: Policy,
    overlays: list[Overlay],
    *,
    use_overlays: bool = True,
    product_version: str | None = None,
) -> EffectivePlan:
    """The plan that runs on this tenant: base capability + the version overlays that apply.

    `product_version` defaults to the tenant config; replay re-resolves with the version the
    application actually reports, and refuses to run if that would change the plan.
    """
    version = product_version or tenant.product_version
    data = cap.model_dump(mode="json", exclude_none=True)
    used: list[Overlay] = []
    labels: dict[str, dict[str, str]] = {}
    product = cap.implementation.app.product
    for ov in overlays if use_overlays else []:
        if ov.applies_to.product != product or not version_in(ov.applies_to.versions, version):
            continue
        changed = False
        patch = ov.capabilities.get(cap.id)
        if patch:
            steps = {s["id"]: s for s in data["implementation"]["steps"]}
            for step_id, sp in patch.steps.items():
                if step_id not in steps:
                    raise ValueError(f"overlay {ov.id} patches unknown step {step_id!r} of {cap.ref}")
                if steps[step_id]["effect"] == "commit" and sp.expect is not None:
                    raise ValueError(
                        f"overlay {ov.id} may re-target commit step {step_id!r} but not change its checkpoints"
                    )
                steps[step_id].update(sp.model_dump(mode="json", exclude_none=True))
                changed = True
            for code, det in patch.outcome_detectors.items():
                data["implementation"]["outcome_detectors"][code] = det.model_dump(
                    mode="json", exclude_none=True
                )
                changed = True
        for name, mapping in ov.labels.items():
            if name in cap.contract.inputs:
                labels.setdefault(name, {}).update(mapping)
                changed = True
        if changed:
            used.append(ov)
    effective = Capability.model_validate(data)
    plan_hash = sha256_of(
        {
            "capability": effective.model_dump(
                mode="json", exclude_none=True, exclude={"status", "approval"}
            ),
            "labels": labels,
            "overlays": [(o.id, o.content_sha256()) for o in used],
            "app_profile": {
                "product": app.product,
                "profile_version": app.profile_version,
                "sha256": sha256_of(app.model_dump(mode="json")),
            },
            "policy": sha256_of(policy.model_dump(mode="json")),
        }
    )
    return EffectivePlan(effective, cap, [o.id for o in used], labels, plan_hash, used)
