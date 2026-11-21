"""Deprecated ``effectfence`` command line; forwards to :mod:`safesink.cli`."""

from __future__ import annotations

import argparse
import warnings

from safesink.cli import build_parser as _build_parser
from safesink.cli import main as _main


def build_parser(prog: str = "effectfence") -> argparse.ArgumentParser:
    return _build_parser(prog)


def main(argv: list[str] | None = None, prog: str = "effectfence") -> int:
    warnings.warn(
        "effectfence is deprecated, use safesink (pip install safesink)",
        DeprecationWarning,
        stacklevel=2,
    )
    return _main(argv, prog)


if __name__ == "__main__":
    raise SystemExit(main())
