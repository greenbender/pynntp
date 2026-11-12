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

`tests/integration` runs against a real INN server on localhost, ports 119
and 563. CI runs it against the
[greenbender/inn](https://github.com/greenbender/inn-docker) image:

```sh
docker run -d --rm --name inn -p 119:119 -p 563:563 greenbender/inn
uv run pytest
docker stop inn
```

and against Ubuntu's inn2 package, set up by `tests/integration/inn2/setup.sh`
(Debian's works too). The script changes INN's configuration, so run it only
on a CI runner or a disposable machine:

```sh
sudo tests/integration/inn2/setup.sh
PYNNTP_TLS_CAFILE=/etc/news/pynntp-cert.pem uv run pytest
```

`PYNNTP_TLS_CAFILE` is the certificate to verify the TLS tests against;
without it they don't check it. The tests post articles but don't rely on the
groups being empty, and those for optional features skip when the server
doesn't list them in CAPABILITIES.

## Docs

```sh
uv run --only-group docs sphinx-build -c docs . /tmp/pynntp-docs && rm -rf autoapi
```

The build should have no warnings. Docstrings use the Google style.

## Pull requests

Every pull request needs a label, which CI checks: `breaking`, `security`,
`feature`, `bug`, `documentation` or `dependencies` (`ignore-for-release`
leaves it out). Only maintainers can add labels, so one adds it if you can't.
The release's "Generate release notes" button groups pull requests by these
labels (`.github/release.yml`).

Don't edit `CHANGELOG.md`; describe the change in the pull request instead.

## Releases

First merge a pull request that sets the version in `pyproject.toml` (run
`uv version 3.1.0`, which also updates `uv.lock`) and adds a dated
`CHANGELOG.md` section written from the pull requests merged since the last
release. Then publish a GitHub release on `main` tagged with that version, for
example `3.1.0`; it is uploaded to PyPI, and the release fails if the tag and
version differ. A pre-release (for example `3.2.0rc1`) goes to TestPyPI
instead, built with the tag as its version. Only admins can create tags.
