# /new-issue — Create a new scoped GitHub issue

Usage: `/new-issue`

## What this command does

Guides you through creating a well-scoped GitHub issue that fits the project structure.

## Steps

1. Ask: "What does this issue accomplish in one sentence?"
2. Ask: "Which label fits — infra / data / pipeline / ml / safety / alert / frontend / testing?"
3. Ask: "Which existing issues does this depend on? (check docs/ISSUES.md)"
4. Ask: "Which existing issues does this block?"
5. Ask: "What's the verification step — how do we know it's done?"

6. Create the issue:
```bash
gh issue create \
  --title "[label] <one sentence>" \
  --label "<label>" \
  --body "## What this does
<one sentence>

## Dependencies
Depends on: <issue numbers or 'none'>
Blocks: <issue numbers or 'none'>

## Implementation notes
<key details>

## Verification
<how to verify it's complete>"
```

7. Add the new issue to `docs/ISSUES.md` in the right section with its dependency graph entry.
8. Remind the team via `/status` to see the updated board.
