"""Surfaces: how the system perceives and acts on a UI (web implemented; desktop/pixel by design)."""

from .base import FrameSnapshot, NotReady, PageSnapshot, SessionLost, StaleRef, Surface
from .web import Resolution, WebSurface

__all__ = [
    "FrameSnapshot",
    "NotReady",
    "PageSnapshot",
    "Resolution",
    "SessionLost",
    "StaleRef",
    "Surface",
    "WebSurface",
]
