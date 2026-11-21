"""Deprecated ``python -m effectfence`` entry point; forwards to ``safesink``."""

from .cli import main

raise SystemExit(main())
