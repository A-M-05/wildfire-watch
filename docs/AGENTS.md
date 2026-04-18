# Agent Roster + Orchestration Rules

Each agent owns a domain. Use the right agent for the right work.

## Agents

| Agent | File | Owns | Issues |
|---|---|---|---|
| infra-agent | `.claude/agents/infra-agent.md` | CDK stacks, AWS provisioning | #1–5 |
| data-agent | `.claude/agents/data-agent.md` | NASA/NOAA/CAL FIRE pollers | #6, #7, #11 |
| pipeline-agent | `.claude/agents/pipeline-agent.md` | Lambda, Kinesis, EventBridge | #8, #9, #10 |
| ml-agent | `.claude/agents/ml-agent.md` | SageMaker, Bedrock | #12–15 |
| safety-agent | `.claude/agents/safety-agent.md` | QLDB, Guardrails, Clarify, Monitor | #16–21 |
| alert-agent | `.claude/agents/alert-agent.md` | SNS, Pinpoint, dispatch workflow | #22–24 |
| frontend-agent | `.claude/agents/frontend-agent.md` | React, Mapbox, live map UI | #25–30 |
| test-agent | `.claude/agents/test-agent.md` | Tests, CI validation | #31–32 |

## Orchestration rules

1. **One issue per person at a time.** Use `/claim N` to register.
2. **Never start an issue whose dependencies aren't merged.** Check `docs/ISSUES.md`.
3. **Schema changes require team sync.** If you change the normalized fire event schema in `CLAUDE.md`, ping the team before merging.
4. **Safety issues (#16–21) block alerting (#22).** The safety-agent's work is on the critical path — prioritize unblocking it.
5. **Commits must reference the issue number.** Format: `[#N] short description`
6. **Run `/status` after merging** to see what's newly unblocked for teammates.
