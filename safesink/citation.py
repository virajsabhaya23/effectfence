from __future__ import annotations

import json
from typing import Literal


REPOSITORY_URL = "https://github.com/virajsabhaya23/effectfence"
TITLE = "SafeSink: MCP Tool Side-Effect Conformance Verifier"


def _version() -> str:
    # Read lazily so this module can be imported while ``safesink`` initializes.
    from . import __version__

    return __version__


def citation(format: Literal["bibtex", "cff", "json"] = "bibtex") -> str:
    """Return a copy-ready citation without inventing an archival DOI."""

    version = _version()
    if format == "bibtex":
        return f"""@software{{sabhaya_safesink_2026,
  author  = {{Sabhaya, Viraj}},
  title   = {{{TITLE}}},
  year    = {{2026}},
  version = {{{version}}},
  url     = {{{REPOSITORY_URL}}}
}}"""
    if format == "cff":
        return f"""cff-version: 1.2.0
message: "If you use SafeSink, please cite it using this metadata."
title: "{TITLE}"
type: software
authors:
  - family-names: Sabhaya
    given-names: Viraj
version: {version}
repository-code: "{REPOSITORY_URL}"
"""
    if format == "json":
        return json.dumps(
            {
                "type": "software",
                "author": [{"family": "Sabhaya", "given": "Viraj"}],
                "title": TITLE,
                "version": version,
                "URL": REPOSITORY_URL,
            },
            indent=2,
        )
    raise ValueError(f"unsupported citation format: {format}")
