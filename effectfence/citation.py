from __future__ import annotations

import json
from typing import Literal


REPOSITORY_URL = "https://github.com/virajsabhaya23/effectfence"
VERSION = "0.2.0"


def citation(format: Literal["bibtex", "cff", "json"] = "bibtex") -> str:
    """Return a copy-ready citation without inventing an archival DOI."""

    if format == "bibtex":
        return """@software{sabhaya_effectfence_2026,
  author  = {Sabhaya, Viraj},
  title   = {EffectFence: Crash/Retry and MCP Side-Effect Conformance Verifier},
  year    = {2026},
  version = {0.2.0},
  url     = {https://github.com/virajsabhaya23/effectfence}
}"""
    if format == "cff":
        return """cff-version: 1.2.0
message: "If you use EffectFence, please cite it using this metadata."
title: "EffectFence: Crash/Retry and MCP Side-Effect Conformance Verifier"
type: software
authors:
  - family-names: Sabhaya
    given-names: Viraj
version: 0.2.0
date-released: 2026-08-16
repository-code: "https://github.com/virajsabhaya23/effectfence"
"""
    if format == "json":
        return json.dumps(
            {
                "type": "software",
                "author": [{"family": "Sabhaya", "given": "Viraj"}],
                "title": "EffectFence: Crash/Retry and MCP Side-Effect Conformance Verifier",
                "issued": {"date-parts": [[2026, 8, 16]]},
                "version": VERSION,
                "URL": REPOSITORY_URL,
            },
            indent=2,
        )
    raise ValueError(f"unsupported citation format: {format}")
