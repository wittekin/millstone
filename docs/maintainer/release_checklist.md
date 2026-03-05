# Release Checklist

Use this checklist before publishing a new version.

## One-Time Setup

- Enable GitHub Pages with source set to **GitHub Actions**.
- Add code ownership in `.github/CODEOWNERS`.
- Configure branch protection for `main` (PR review + required checks + no force push).
- Apply branch protection via CLI:

```bash
gh auth login
.millstone/setup_branch_protection.sh
```

- Configure PyPI trusted publishing for:
  - Owner: `wittekin`
  - Repository: `millstone`
  - Workflow: `release.yml`
  - Environment: `pypi`

## Version and Metadata

- Update version in `pyproject.toml`.
- Update `CHANGELOG.md` with release notes and date.
- Confirm project URLs in `pyproject.toml` are correct.

## Quality Gates

- Run full test suite: `pytest`.
- Run coverage report if needed: `pytest --cov=. --cov-report=term-missing`.
- Validate CLI help output: `millstone --help`.

## Packaging

- Build artifacts: `python -m build`.
- Verify wheel/sdist metadata locally.
- Smoke install in a clean environment.

## Repository Hygiene

- Ensure `LICENSE`, `README.md`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md` are up to date.
- Ensure CI is passing on `main`.
- Tag release commit with `vX.Y.Z`.

## Publish

- Push release tag `vX.Y.Z` to trigger automated release workflow.
- Confirm GitHub Release was created with generated notes and attached artifacts.
- Confirm PyPI publish completed via trusted publishing.

### Tag Command Reference

```bash
git tag -a vX.Y.Z -m "Release vX.Y.Z"
git push origin vX.Y.Z
```
