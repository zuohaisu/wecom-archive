# Contributing to wecom-archive

Thanks for helping improve the project. Contributions are welcome through
GitHub issues and pull requests. By participating, you agree to follow the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- For a bug, check existing issues and provide a minimal, reproducible report.
- For a substantial feature or architecture change, open an issue or discussion
  first so its scope and design can be agreed before implementation.
- Never put credentials, private keys, customer data, real WeCom archive
  messages, or production media in issues, pull requests, tests, or fixtures.
  Use synthetic examples. Report security vulnerabilities privately as
  described in [SECURITY.md](SECURITY.md), not in a public issue.
- The project is licensed under AGPL-3.0; see [LICENSE](LICENSE) and
  [NOTICE](NOTICE). Tencent's proprietary WeCom SDK is not included and must
  not be committed.

## Development setup and validation

The backend requires Python 3.11 or later. From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r backend/requirements.txt -r backend/requirements-dev.txt
make verify
```

`make verify` runs lint checks on the current diff, import/syntax checks, the
build checks, and the test suite. It is the normal local acceptance command;
report the exact commands and results in your pull request. If you configure
`DATABASE_URL` for PostgreSQL-only tests, it must point to a disposable test
database, never a production database. Most tests use synthetic data and do
not require the Tencent SDK or live WeCom credentials.

If your change affects the public snapshot, its allowlist, or files intended
for the public repository, also run:

```bash
make public-verify
```

This builds and validates a local snapshot; it does not publish or synchronize
anything. Do not weaken, skip, or delete an assertion merely to make a check
pass. If a check cannot run in your environment, explain the limitation and
what remains unverified.

## Codebase boundaries

Read the
[Wiki architecture overview](https://github.com/zuohaisu/wecom-archive/wiki/Architecture-Overview)
and
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
before changing application boundaries. The intended dependency direction is composition root → routers →
services/domain → database. Keep business routes, direct SQL, and inline
HTML/CSS/JavaScript out of the composition root. Simple cohesive CRUD may live
in a router; put multi-step domain workflows in services. Services must not
import routers, and routers must not import the composition root. Preserve
tenant isolation, authorization, auditability, and fail-closed behavior.

A change to module ownership, dependency direction, trust boundaries, or
persistence strategy needs an Architecture Decision Record under
[`docs/adr/`](docs/adr/), using the Wiki's
[ADR template](https://github.com/zuohaisu/wecom-archive/wiki/ADR-0000-Template)
and a synchronized repository copy. Agree on the decision before implementing
the architectural change.

## Issues, commits, and pull requests

Keep a pull request focused on one issue or one coherent change. Do not mix
unrelated issues in a commit. Use an imperative Conventional Commit subject
with a type and optional scope, and include the issue number when applicable:

```text
fix(auth): reject invalid sessions (#123)
```

Common types are `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, and `ci`.
Include a clear summary, the validation commands and their actual results,
changed test files and why, relevant risks or data boundaries, and known
limitations. The [pull request template](.github/PULL_REQUEST_TEMPLATE.md)
provides a checklist.

## Developer Certificate of Origin (DCO)

This project uses the Developer Certificate of Origin, version 1.1 (DCO),
not a Contributor License Agreement (CLA). Sign off **every commit** by using
`git commit -s`; for example:

```bash
git commit -s -m "fix(auth): reject invalid sessions (#123)"
```

Git adds a `Signed-off-by: Name <email>` line to the commit message. Use an
email address you are comfortable having recorded publicly: commit metadata
and sign-offs are public and may be redistributed with the contribution.

A DCO sign-off certifies your right to submit the contribution under the
project license. It does not transfer copyright or grant the project a
separate right to relicense your contribution under a different license.
Contributions are accepted under the license identified in the project files,
currently AGPL-3.0. A future licensing change would require the appropriate
rights or replacement of the affected contribution; signing the DCO does not
provide those rights.

The DCO 1.1 text is reproduced verbatim from
[developercertificate.org](https://developercertificate.org/):

> Developer Certificate of Origin
> Version 1.1
>
> Copyright (C) 2004, 2006 The Linux Foundation and its contributors.
>
> Everyone is permitted to copy and distribute verbatim copies of this
> license document, but changing it is not allowed.
>
> Developer's Certificate of Origin 1.1
>
> By making a contribution to this project, I certify that:
>
> (a) The contribution was created in whole or in part by me and I have the
> right to submit it under the open source license indicated in the file; or
>
> (b) The contribution is based upon previous work that, to the best of my
> knowledge, is covered under an appropriate open source license and I have the
> right under that license to submit that work with modifications, whether
> created in whole or in part by me, under the same open source license (unless
> I am permitted to submit under a different license), as indicated in the
> file; or
>
> (c) The contribution was provided directly to me by some other person who
> certified (a), (b) or (c) and I have not modified it.
>
> (d) I understand and agree that this project and the contribution are public
> and that a record of the contribution (including all personal information I
> submit with it, including my sign-off) is maintained indefinitely and may be
> redistributed consistent with this project or the open source license(s)
> involved.
