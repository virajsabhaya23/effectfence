# Releasing

The release workflow refuses to publish an incoherent version, so a mistake is
caught before PyPI sees it.

## 1. Prepare

Bump the version in all three places (they must agree, and the workflow checks
that they do):

- `pyproject.toml` (`version = "..."`)
- `safesink/__init__.py` (`__version__`)
- `build_backend.py` (`VERSION`)

Add a `## <version> - YYYY-MM-DD` section to `CHANGELOG.md`. The workflow fails
without it: every published version has release notes.

## 2. Create the release

Two supported paths, both running the same checks:

- **GitHub UI:** Draft a release, create tag `v<version>`, publish it. The
  `release: published` trigger starts the workflow.
- **Manual:** Actions -> `Publish to PyPI` -> `Run workflow` with `version`
  filled in (no `v` prefix needed, both forms are accepted).

## 3. What the workflow does

1. **Version gate.** Reads `pyproject.toml`, `safesink/__init__.py` and
   `build_backend.py`, compares them to the requested version or tag, and stops
   with the exact mismatch otherwise.
2. **Changelog gate.** Requires a `## <version>` heading.
3. **Gates before publishing.** Unit tests, format, lint and type-interface
   checks, then `python -m build`.
4. **Publish** through PyPI trusted publishing (OIDC). No API token is stored in
   the repository or in secrets.
5. **Post-publish verification.** In a clean job it installs
   `safesink==<version>` from PyPI with retries for index propagation, runs
   `safesink --help` and `safesink mcp-verify --help`, and asserts
   `safesink.__version__` matches. A publish that installs nothing usable fails
   the workflow instead of looking green.

## One-time manual steps

These need repository-owner access and cannot be automated from the repository:

- **PyPI trusted publisher.** On PyPI, add a publisher for project `safesink`:
  owner `virajsabhaya23`, repository `effectfence`, workflow `release.yml`,
  environment `pypi`. Without it the publish step fails with a permission error.
- **`pypi` environment.** Create it in the repository settings. Adding required
  reviewers there is the approval gate for publishing.
- **Marketplace listing.** Optional: after a release exists, use "Publish this
  Action to the Marketplace" on the release page. The action metadata is ready;
  only the listing click is manual.

## After a release

The `container` workflow builds and pushes
`ghcr.io/virajsabhaya23/safesink` for the new `v*` tag. The post-publish check
is the source of truth for "the release works", not the publish step's success.
