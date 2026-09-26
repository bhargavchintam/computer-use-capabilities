"""Tenant variants of the fictional vendor product "AcmeCore".

Two institutions run the same product at different versions. 7.3 renamed a
menu item (and moved its route), relabelled the search button, and reordered
the shares table. This is the kind of per-version drift the automation layer
has to absorb without re-recording the flow per tenant.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class MockTenant:
    id: str
    display_name: str
    version: str
    inquiry_label: str
    inquiry_path: str
    search_label: str
    share_columns: tuple[str, ...]
    share_types: dict[str, str] = field(default_factory=dict)  # code -> UI label
    env_prefix: str = ""


TENANTS: dict[str, MockTenant] = {
    "pinecrest": MockTenant(
        id="pinecrest",
        display_name="Pinecrest Community CU (demo)",
        version="7.2.4",
        inquiry_label="Member Inquiry",
        inquiry_path="/core/inquiry",
        search_label="Search",
        share_columns=("Share ID", "Description", "Balance", "Available", "Status"),
        share_types={"HC": "Holiday Club", "VC": "Vacation Club", "C12": "12 Month Certificate"},
        env_prefix="PINECREST",
    ),
    "lakeside": MockTenant(
        id="lakeside",
        display_name="Lakeside Savings Bank (demo)",
        version="7.3.1",
        inquiry_label="Member Lookup",
        inquiry_path="/cu/lookup",
        search_label="Find",
        share_columns=("Description", "Share ID", "Rate", "Available", "Balance", "Status"),
        share_types={"HC": "Christmas Club", "VC": "Vacation Club", "C12": "12 Month Certificate"},
        env_prefix="LAKESIDE",
    ),
}
