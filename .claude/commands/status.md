# /status — Show what's in progress and what's newly unblocked

Usage: `/status`

## What this command does

Gives a live view of the 32-issue board: who's working on what, what's blocked, and what's safe to pick up right now.

## Steps

1. Fetch all 32 issues from GitHub with their state and assignees:
   ```bash
   gh issue list --limit 50 --json number,title,state,assignees,labels
   ```
2. Read `docs/ISSUES.md` to get the full dependency graph
3. For each open issue, check if all its dependencies are closed
4. Output three sections:

### IN PROGRESS
List issues that are open and assigned, grouped by assignee:
```
@alice  — #1 [infra] Provision Kinesis, DynamoDB, Timestream
@bob    — #12 [ml] Train SageMaker dispatch model
@carol  — #25 [frontend] Mapbox base map
```

### READY TO CLAIM (unblocked, unassigned)
Issues where all dependencies are closed and no one is assigned:
```
#2 [infra] Provision SageMaker, S3, Glue — no deps, open
#3 [infra] Provision QLDB, Step Functions — no deps, open
```

### BLOCKED
Issues still waiting on open dependencies:
```
#8 [pipeline] Kinesis consumer — waiting on: #6 (open), #7 (open)
#9 [pipeline] Enrichment Lambda — waiting on: #8 (open)
```

### DONE
Count of closed issues out of 32.

5. End with: "Run `/claim N` to pick up any READY issue."
