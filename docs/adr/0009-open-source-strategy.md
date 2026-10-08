# ADR-0009: Open-Source Strategy — single-repo AGPL-3.0 release

**Status**: Accepted (supersedes ADR-0003)
**Date**: 2026-10-09
**Author**: Crowntime WeCom Archive dev (Haisu-approved product direction)
**Related**: GH-166 (#166), GH-167 (#167), GH-168 (#168), GH-203 (#203),
[Discussion #178](https://github.com/zuohaisu/wecom-archive/discussions/178)

## 1. Context

ADR-0003 decided the product would stay closed-source and hosted-only,
positioned as fully automated fly-order deterrence for micro teams. Two
subsequent shifts reversed its premises:

1. **Product**: the buyer is the owner of a 1–5 person sales/support team,
   and the commercial model is a low-price, fully self-serve cloud. Any
   human involvement in onboarding breaks unit economics, which makes
   zero-touch self-service — and credible fork/self-host distribution —
   part of the go-to-market rather than a threat to it.
2. **Operations**: the repository's security posture was audited
   end-to-end (2026-10-09 three-way sweep: code + full git history,
   issues, PRs, discussions, Actions logs). No credentials were ever
   committed; the remaining production-fingerprint exposure was explicitly
   accepted with infrastructure hardening (documented risk acceptance,
   GH-173), and operational originals now live outside the tree (GH-203).

## 2. Decision

1. **License**: AGPL-3.0 for the entire single repository — archive core
   and cloud operations code alike. No proprietary directory, no source
   exclusion, no additional license restrictions beyond AGPL §7's
   allowable attribution notices.
2. **Release mode**: make **this repository public**. The earlier
   two-repo sanitized-snapshot model is retired; the snapshot tooling
   survives only as an optional pre-publish self-check. Git history ships
   as-is (see GH-173 / GH-203 for the boundary decisions).
3. **Edition semantics**: `APP_EDITION` distinguishes `selfhost` vs
   `cloud` **runtime behavior** only — never a licensing or
   confidentiality boundary (details in ADR-0008). Self-hosted must not
   depend on any cloud purchase, trial, or subscription-expiry state.
4. **Contributions**: DCO (`Signed-off-by`), no CLA. Future contribution
   terms may change, but existing DCO contributions never automatically
   gain closed-source/relicensing rights.
5. **Trademark**: 康冠时代 (Shenzhen Crowntime Technology Co., Ltd.).
   Official self-host packages default to a `Powered by 康冠时代`
   attribution notice, enforced declaratively (NOTICE/trademark policy)
   — no license locks, phone-home, or kill switches. Forks must not
   present themselves as official products, hosting, or endorsements,
   but are free to brand their own distributions.
6. **Operational boundary**: production fingerprints, customer data, and
   live operational configuration never enter the tree; per-host
   operational originals live in gitignored local storage (GH-203).

## 3. Consequences

- Anyone may fork, self-host, or offer competing hosted services under
  AGPL-3.0 obligations (including §13's network-source offer). The
  trademark policy prevents impersonation only; it does not — and is not
  intended to — prohibit lawful competition.
- `docs/operations/` carries generalized, host-agnostic operational
  references; published defect history stays published (transparency is
  part of the model).
- ADR-0003 (hosted-only strategy) is removed from the tree; this ADR is
  its successor of record, and the original remains recoverable from git
  history.
- Governance docs (CONTRIBUTING, Code of Conduct, security policy)
  describe the DCO/community flow and supersede any older
  "no external contribution process" statements elsewhere.

## 4. References

- [Discussion #178 — decision convergence](https://github.com/zuohaisu/wecom-archive/discussions/178)
- [ADR-0008 — runtime edition and self-hosted policy](0008-runtime-edition-policies.md)
- [ADR-0005 — cloud billing lifecycle and service gates](0005-saas-billing-lifecycle-refunds-and-service-gates.md)
- [AGPL-3.0](https://www.gnu.org/licenses/agpl-3.0.html), [DCO 1.1](https://developercertificate.org/)
