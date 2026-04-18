# /claim — Claim an issue and verify dependencies

Usage: `/claim N`

## What this command does

1. Reads `docs/ISSUES.md` to find issue #N and its dependencies
2. Checks whether all dependency issues are closed on GitHub
3. If clear: assigns the issue to you and posts a comment
4. If blocked: lists which dependency issues are still open

## Steps

1. Read `docs/ISSUES.md` and extract the dependency list for issue #$ARGUMENTS
2. For each dependency, check GitHub issue status:
   ```bash
   gh issue view <dep_number> --json state,assignees
   ```
3. If any dependency is open: output which ones are blocking and stop
4. If all deps are closed (or there are none):
   - Assign the issue: `gh issue edit $ARGUMENTS --add-assignee @me`
   - Add comment: `gh issue comment $ARGUMENTS --body "Claiming this — starting now. Dependencies verified clear."`
   - Output: "Issue #$ARGUMENTS claimed. Read .claude/agents/<domain>-agent.md before starting."
5. Tell the user which agent file to read for this issue's domain
6. Remind them to commit with `[#$ARGUMENTS]` in the message
