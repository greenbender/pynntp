# Contributing

## Setup

pynntp uses [uv](https://docs.astral.sh/uv/). `uv sync` installs the
development tools.

## Checks

Pull requests run all of these in CI:

```sh
uv run ruff check .        # Lint
uv run ruff format --check .
uv run mypy --strict .     # Types
uv run pytest --ignore=tests/integration  # Unit tests, no server needed
```

`uv run pre-commit install` runs the lint and format checks on each commit.

## Integration tests

`tests/integration` runs against a real INN server, the
[greenbender/inn](https://github.com/greenbender/inn-docker) image, on ports
119 and 563:

```sh
docker run -d --rm --name inn -p 119:119 -p 563:563 greenbender/inn
uv run pytest
docker stop inn
```

The tests post articles, so use a fresh container for each run.

## Docs

```sh
uv run --only-group docs sphinx-build -c docs . /tmp/pynntp-docs && rm -rf autoapi
```

The build should have no warnings. Docstrings use the Google style.

## Releases

Publishing a GitHub release tagged with the version (for example `3.0.0`)
uploads it to PyPI and opens a pull request that bumps the version in
`pyproject.toml`. A pre-release (for example `3.0.0rc1`) goes to TestPyPI
instead. Update `CHANGELOG.md` before releasing.
