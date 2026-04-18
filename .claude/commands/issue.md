# /issue — Work a GitHub issue end to end

Usage: `/issue N`

## What this command does

Fully implements issue #N: reads the spec, checks dependencies, writes the code, runs verification, commits, and closes the issue.

## Steps

1. **Read the issue:**
   ```bash
   gh issue view $ARGUMENTS --json title,body,labels,assignees
   ```

2. **Check dependencies** — same as `/claim N`. If any dep is open, stop and report.

3. **Identify the agent** — look up issue #N in `docs/ISSUES.md` to find its label (infra/data/pipeline/ml/safety/alert/frontend/testing). Read the corresponding `.claude/agents/<label>-agent.md`.

4. **Read the relevant SKILL.md** — find the matching skill in `.claude/skills/` and read it before writing any code.

5. **Implement** — write the code as specified in the agent file's issue checklist.

6. **Verify** — run the verification step from the agent file.

7. **Commit:**
   ```bash
   git add <relevant files>
   git commit -m "[#$ARGUMENTS] <short description>"
   ```

8. **Close the issue:**
   ```bash
   gh issue close $ARGUMENTS --comment "Implemented and verified. Commit: $(git rev-parse --short HEAD)"
   ```

9. **Report** what was built, what files were changed, and which issues are now unblocked.
