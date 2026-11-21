"""Deprecated EffectFence package; use :mod:`safesink` instead."""

from __future__ import annotations

import warnings

from safesink import ManifestError, __version__, load_manifest, verify_manifest

warnings.warn(
    "effectfence is deprecated, use safesink (pip install safesink)",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["ManifestError", "load_manifest", "verify_manifest", "__version__"]
