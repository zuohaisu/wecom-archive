## GitHub issues and commits

One ticket must map to exactly one final commit. Add one row per ticket.

| Issue | Commit | Goal |
|---|---|---|
| RND-<N> / GH-<N> | `<sha>` | <one-sentence outcome> |

## Scope

- What changed:
  - <focused change>
- Intentionally not changed:
  - <explicit boundary>
- Grouping rationale when this PR contains multiple tickets:
  - <same Epic / coherent delivery reason, or N/A>

## QA evidence

- [ ] Independent QA completed before the approved commit.
- [ ] `make verify` passed, or every narrower command and remaining gap is listed below.
- [ ] `backend/tests/test_architecture_boundary.py` passed when application code changed.
- [ ] No secrets, production data, or unrelated changes are included.

Commands and results:

```text
<command>: EXIT=<code> — <summary>
```

## Delivery gates

- [ ] Source is an assigned delivery worktree/branch; the source branch is not `main`.
- [ ] Every GitHub issue maps to exactly one final commit, and no commit mixes issues.
- [ ] All tickets in this PR form one coherent delivery, normally within the same Epic.
- [ ] If this PR contains multiple tickets, the selected merge strategy preserves their
      individual commits; the PR will not be squash-merged into one commit.
- [ ] The PR CI workflow is green before merge.
- [ ] Haisu performs or explicitly approves the merge to `main`.

CD impact:

- [ ] Deployable paths changed — merging triggers CD.
- [ ] Only ignored documentation/task paths changed — merging does not trigger CD.

## Risks and rollback

- Risk: <risk or none>
- Rollback: <how to revert safely>
