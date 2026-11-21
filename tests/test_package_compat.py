from __future__ import annotations

import contextlib
import importlib
import io
import pkgutil
import re
import unittest
import warnings
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 has no tomllib
    tomllib = None

import effectfence
import safesink
from effectfence.cli import main as effectfence_main
from safesink.cli import main as safesink_main

ROOT = Path(__file__).parents[1]
# ``cli`` and ``__init__`` intentionally differ; ``schemas`` ships a data file.
ALIASED_MODULES = sorted(
    module.name
    for module in pkgutil.iter_modules(safesink.__path__)
    if module.name not in {"__main__", "cli", "schemas"}
)


def public_names(module: object) -> list[str]:
    return sorted(name for name in vars(module) if not name.startswith("_"))


def project_version() -> str:
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    if tomllib is not None:
        return str(tomllib.loads(text)["project"]["version"])
    section = text.split("[project]", 1)[1]
    match = re.search(r'^version = "([^"]+)"', section, re.MULTILINE)
    if match is None:
        raise AssertionError("pyproject.toml has no [project] version")
    return match.group(1)


class VersionTests(unittest.TestCase):
    def test_pyproject_version_matches_the_package(self) -> None:
        self.assertEqual(project_version(), safesink.__version__)


class DeprecatedAliasTests(unittest.TestCase):
    def test_alias_covers_every_primary_module(self) -> None:
        self.assertIn("mcp_verifier", ALIASED_MODULES)
        self.assertIn("simulator", ALIASED_MODULES)
        self.assertEqual(
            ALIASED_MODULES,
            sorted(
                module.name
                for module in pkgutil.iter_modules(effectfence.__path__)
                if module.name not in {"__main__", "cli", "schemas"}
            ),
        )

    def test_alias_reexports_the_identical_objects(self) -> None:
        for name in ALIASED_MODULES:
            with self.subTest(module=name):
                target = importlib.import_module(f"safesink.{name}")
                shim = importlib.import_module(f"effectfence.{name}")
                expected = public_names(target)
                self.assertGreater(len(expected), 0)
                self.assertEqual(set(expected) - set(public_names(shim)), set())
                for attribute in expected:
                    self.assertIs(getattr(shim, attribute), getattr(target, attribute))

    def test_versions_and_schema_copies_agree(self) -> None:
        self.assertEqual(effectfence.__version__, safesink.__version__)
        schema = "mcp-manifest-v1.schema.json"
        self.assertEqual(
            (ROOT / "safesink" / "schemas" / schema).read_bytes(),
            (ROOT / "effectfence" / "schemas" / schema).read_bytes(),
        )

    def test_version_flag_reports_the_invoked_program(self) -> None:
        for main, prog in ((safesink_main, "safesink"), (effectfence_main, "effectfence")):
            with self.subTest(prog=prog):
                out = io.StringIO()
                with contextlib.redirect_stdout(out), warnings.catch_warnings():
                    warnings.simplefilter("ignore", DeprecationWarning)
                    with self.assertRaises(SystemExit) as raised:
                        main(["--version"])
                self.assertEqual(raised.exception.code, 0)
                self.assertEqual(out.getvalue().strip(), f"{prog} {safesink.__version__}")

    def test_installed_console_scripts_use_their_own_entry_point(self) -> None:
        # A shared ``safesink.cli:main`` entry point would make the installed
        # ``effectfence`` command report "safesink" and skip the deprecation
        # warning, so each script must resolve through its own module.
        pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn('safesink = "safesink.cli:main"', pyproject)
        self.assertIn('effectfence = "effectfence.cli:main"', pyproject)

    def test_deprecated_cli_warns_and_matches_the_primary_verdict(self) -> None:
        fixture = str(ROOT / "examples" / "safe_fenced_recovery.json")
        with contextlib.redirect_stdout(io.StringIO()):
            primary = safesink_main(["verify", fixture])
            with self.assertWarns(DeprecationWarning):
                alias = effectfence_main(["verify", fixture])
        self.assertEqual(primary, 0)
        self.assertEqual(alias, primary)

    def test_deprecated_cli_keeps_the_failure_exit_code(self) -> None:
        fixture = str(ROOT / "examples" / "crash_after_effect.json")
        with contextlib.redirect_stdout(io.StringIO()):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                alias = effectfence_main(["verify", fixture])
        self.assertEqual(alias, 2)


if __name__ == "__main__":
    unittest.main()
