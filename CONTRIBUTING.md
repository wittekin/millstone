# Contributing

Thanks for contributing to millstone.

## Before You Start

- Search existing issues and pull requests to avoid duplicates.
- Open an issue first for substantial changes to align on scope.
- Be respectful and follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Development Setup

1. Fork and clone the repository.
2. Create and activate a virtual environment.
3. Install in editable mode with development dependencies:

```bash
pip install -e .[dev]
```

## Running Tests

Run the full test suite:

```bash
pytest
```

Run a single test:

```bash
pytest tests/test_orchestrator.py::test_function_name -v
```

Run with coverage:

```bash
pytest --cov=. --cov-report=term-missing
```

Run dependency security checks:

```bash
pip install -e .[security]
pip check
pip-audit
```

## Pull Request Guidelines

- Keep PRs focused and reviewable.
- Add or update tests for behavior changes.
- Update docs for user-facing changes.
- Call out breaking changes explicitly.
- Use clear, imperative commit messages.

## Review and Merge Standards

- Pull requests require maintainer review before merge.
- Required CI checks must pass.
- Use draft PRs while work is in progress.
- Resolve all review conversations before merge.
- Code ownership is defined in `.github/CODEOWNERS`.
- Maintainer responsibilities are documented in `docs/maintainer/maintainers.md`.

## Commit Message Examples

- `Add eval baseline regression guard`
- `Fix tasklist parser for empty context blocks`

## Reporting Bugs and Requesting Features

Use the issue templates:
- Bug report
- Feature request

Include expected behavior, actual behavior, reproduction details, and environment information.
