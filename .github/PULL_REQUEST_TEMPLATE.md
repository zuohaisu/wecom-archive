## Related issue

Closes #<number> (replace before submitting)

## Ticket → commit mapping

Use one row per ticket; each ticket maps to exactly one commit.

| Ticket | Commit (SHA and subject) |
|---|---|
| RND-<n> (#<n>) or GH-<n> (#<n>) | `<sha> <subject>` |

## Summary

Describe the user-visible behavior or problem this change addresses.

## Scope and design

- What changed:
- Intentionally not changed:
- Architectural or data-boundary impact:

## Validation

List the exact commands run and their actual results. Do not report skipped or unavailable checks as passing.

```text
make verify: <result>
make public-verify: <result, when applicable>
Other focused checks: <command and result>
```

## Tests

- Test files added or changed and why:
- [ ] No test was weakened, skipped, or removed to make the change pass.

## Safety and limitations

- [ ] No credentials, customer data, production archive content, or proprietary SDK files are included.
- Risk, migration/rollback considerations, and known limitations:

## Contributor checklist

- [ ] Every commit is signed off with the DCO (`Signed-off-by`); see [CONTRIBUTING.md](CONTRIBUTING.md#developer-certificate-of-origin-dco).
- [ ] The change is focused on the related issue and public documentation is updated where needed.
- [ ] Required CI passes before merge; merging remains a maintainer action.
