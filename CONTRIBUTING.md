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

## Fault-injection tests

`tests/faults` runs pynntp against a fake server that splits, delays, drops
and garbles its replies. These tests are opt-in and need Go; see
[tests/faults/README.md](tests/faults/README.md).

## Docs

```sh
uv run --only-group docs sphinx-build -c docs . /tmp/pynntp-docs && rm -rf autoapi
```

The build should have no warnings. Docstrings use the Google style.

## Releases

First merge a pull request that sets the version in `pyproject.toml` (run
`uv version 3.1.0`, which also updates `uv.lock`) and dates the
`CHANGELOG.md` section. Then publish a GitHub release on `main` tagged with
that version, for example `3.1.0`; it is uploaded to PyPI, and the release
fails if the tag and version differ. A pre-release (for example `3.2.0rc1`)
goes to TestPyPI instead, built with the tag as its version. Only admins can
create tags.
