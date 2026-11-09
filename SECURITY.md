# Security Policy

## Supported Versions

pynntp is developed on the `main` branch of
[greenbender/pynntp](https://github.com/greenbender/pynntp) and released on
[PyPI](https://pypi.org/project/pynntp/). Security fixes go into `main` and the
next release.

| Version | Security support |
| ------- | ---------------- |
| `main` and the latest 3.x release | :white_check_mark: |
| Older 3.x releases | :x: (upgrade to the latest first) |
| 2.x and earlier | :x: |

If you use an older release, please check whether the problem is still present
in the latest one before reporting it; it may already have been fixed.

## Reporting a Vulnerability

Please do **not** report security vulnerabilities through GitHub issues, pull
requests, discussions, Usenet newsgroups or mailing lists: they are all
**public**.

Report them **privately** through
[GitHub Security Advisories](https://github.com/greenbender/pynntp/security/advisories/new)
for this repository. This opens a private report that only you and the
maintainers (and the collaborators they add) can see until it is resolved and
disclosed.

Please include as much of the following as you can:

- what the vulnerability is and its impact (for instance credentials sent in
  clear text or to the wrong place, a TLS downgrade, command or header
  injection, a desync that returns another reply's data, unbounded memory or
  CPU use, a crash);
- the affected function or command (`login()`, STARTTLS, `post()`, `ihave()`,
  `compress()`, `body()` with yEnc, XZVER, ...) and the version or commit you
  tested;
- the server replies that trigger it, ideally as the exact bytes, so it can be
  reproduced with a fake server; a test or patch, if you have one;
- your assessment of the severity and of what an attacker needs, for instance:
  - a malicious or compromised news server the client connects to;
  - an attacker on the network path (plain-text connections, or before
    STARTTLS completes);
  - an article or header that reaches the client through any server;
  - control over the arguments an application passes to pynntp.

Please do not disclose the issue publicly before a fix is available. If you
have several unrelated issues, please submit one report for each.

### Reporting from a script

Once private vulnerability reporting is enabled for this repository, a report
can also be submitted through the GitHub API instead of the web form. Anyone
with a GitHub account can submit one; repository access is not required. `gh`
must be logged in:

```sh
gh api -X POST repos/greenbender/pynntp/security-advisories/reports \
    -f summary='pynntp: one-line title' \
    -f description="$(cat report.md)"
```

Contributors who work with AI agents: have the agent submit verified findings
this way, one report per issue, and never post them in public places.

## What to Expect

- The maintainers will acknowledge your report as soon as they can.
- The fix is prepared in the private advisory (with a private fork where
  needed) and released with a new version.
- The advisory is then published, with a CVE where appropriate.
- You will be credited in the advisory and the changelog, unless you prefer to
  remain anonymous.

## Scope

This policy covers the `nntp` package in this repository, its tests and its
release workflows.

Problems in news servers (INN, Diablo, ...), in Python itself or in OpenSSL
should be reported to those projects. If pynntp uses such software in an
unsafe way, that is in scope here.
