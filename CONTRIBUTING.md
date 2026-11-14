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

`tests/integration` runs against a real server, by default an INN server on
localhost, ports 119 and 563. CI runs it against the
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
without it they don't check it. The tests don't rely on the groups being
empty.

The tests post permanent articles: each has a unique Message-ID and
`From: GitHub Actions <actions@github.com>`, and none is cancelled or removed.
Run them only against a server where that is acceptable.

Skips on any server:

- Tests of STARTTLS, COMPRESS DEFLATE, IHAVE, NEWNEWS and XPAT, when
  CAPABILITIES doesn't list them. The compressed article test also needs
  COMPRESS DEFLATE and POST or IHAVE, and INN before 2.7.2 skips it.
- The implicit TLS tests, with `PYNNTP_TLS_PORT=0`.

Skips only with `PYNNTP_HOST` set, that is against another server. Without
it (default INN runs) these fail instead, so a client bug can't become a skip:

- Tests of HDR, OVER and LIST ACTIVE.TIMES, when CAPABILITIES doesn't list
  them.
- The XHDR test, when the server answers 500 to XHDR.
- The overview test, without an Xref overview field.
- The posting tests, without an Xref header in HEAD (Xref is optional in
  RFC 5536). An Xref that doesn't list the group fails them on any server,
  after `PYNNTP_POST_WAIT` seconds.
- The TLS test for default verification, if the default trust store accepts
  the server's certificate.
- The INN-only tests (the `junk` group, and posting to it being refused) and
  the empty-group test without `PYNNTP_EMPTY_GROUP`. Without `PYNNTP_HOST`
  they run, and fail if the server isn't INN.

A server that isn't INN needs:

- Each group in `PYNNTP_GROUPS` listed by LIST ACTIVE with status `y` (posting
  allowed). Otherwise the tests fail rather than skip. Its check of INN's
  default groups becomes a check that these groups are listed.
- An Xref header in HEAD for posted articles, with the group and number.
- CAPABILITIES, which the optional-feature tests read. A server that answers
  500 to CAPABILITIES is accepted only with `PYNNTP_HOST` set; then every
  optional-feature test skips. Without `PYNNTP_HOST`, the error fails the test.

To run them against another server, set `PYNNTP_HOST`, `PYNNTP_PORT`,
`PYNNTP_TLS_PORT` (0 skips implicit TLS), `PYNNTP_USER`/`PYNNTP_PASS` and
`PYNNTP_GROUPS` (groups the tests may post to); the docstring of
`tests/integration/inn2/test_inn2.py` lists them all.

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
