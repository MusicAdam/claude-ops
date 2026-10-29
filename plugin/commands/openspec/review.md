---
name: OpenSpec: Review
description: Review an OpenSpec change proposal with multiple expert agents.
category: OpenSpec
tags: [openspec, review, agents]
---
<!-- OPENSPEC:START -->

## CLI contract

Requires the `openspec` CLI (v1.10+). Resolve artifact paths from the CLI rather
than assuming them: the artifact set is defined by the change's **schema**, and
`spec-driven` (proposal / specs / design / tasks) is only the default.

Run `openspec status --change "<id>" --json` and read artifacts from
`artifactPaths.<artifact>.existingOutputPaths`. An empty array means that
artifact was never written — report that rather than assuming a filename.

Project conventions live in `openspec/config.yaml` (`context:` and `rules:`),
**not** `openspec/AGENTS.md`; the CLI deletes that file as legacy on `init`.
Per-artifact guidance: `openspec instructions <artifact> --change "<id>" --json`.

**Stores**: if the change lives in a registered standalone OpenSpec repo, run
`openspec store list --json` and append `--store <id>` to every OpenSpec command
below. Treat the flag as sticky once selected.

**Guardrails**
- Do NOT implement any code. This is a review-only stage.
- Dispatch at least 3 expert agents based on proposal content.
- Synthesize agent feedback into actionable recommendations.
- Prioritize issues by severity (HIGH/MEDIUM/LOW).

**Steps**
1. Resolve and read the change's artifacts via `openspec status --change "<id>" --json` → `artifactPaths.<artifact>.existingOutputPaths`.
2. Analyze proposal content to pick the expert roles the change needs, for example:
   - **Code architecture** (patterns, structure, implementation) — always
   - **Testing strategy** (unit tests, integration tests, what the spec scenarios need)
   - **Database changes** (schema, migrations, indexes)
   - **API or GraphQL changes** (contracts, resolvers, compatibility)
   - **Infrastructure** (containers, cloud, CI/CD)
   - **Security** (auth, secrets, input handling)
   - **UI and UX** (screens, flows, wording)
3. Launch one agent per role, in parallel, with the Agent tool and `run_in_background: true`.
   Use `subagent_type: "general-purpose"` and put the role in the prompt ("You are a senior database
   reviewer…"), with the change's artifact paths and what to report. Use a named specialist agent instead
   only if it appears in the Agent tool's list of available agent types; never assume one exists.
   Every agent is read-only: it reports findings and edits nothing.
4. Wait for all agents to complete.
5. Synthesize feedback into a consolidated report with:
   - Summary table of issues by priority
   - Cross-cutting issues (flagged by multiple experts)
   - Recommended next steps
6. Present findings to user using **iterative confirmation flow**:
   - Show one recommendation at a time
   - For each recommendation, provide:
     - **Summary**: 2-3 sentence overview
     - **Recommendation**: Detailed explanation with code examples if applicable
   - Use AskUserQuestion to get confirmation before proceeding to next
   - Allow user to ask clarifying questions before deciding
   - Track accepted/rejected recommendations
7. After all recommendations reviewed, summarize decisions and offer to invoke `/opsx:update`.

**Recommendation Presentation Format**
```
### Recommendation N of M: <Title>

**Summary**
<2-3 sentences explaining the issue>

**Recommendation**
<Detailed explanation with rationale and code examples>
```
Then prompt: "Accept this recommendation? (Yes/No/Need clarification)"

**Agent Selection Heuristics**
- Always include a code-architecture reviewer
- Include a database reviewer if the proposal mentions: schema, migration, column, index, upsert, bulk
- Include a testing reviewer if tasks.md has test items or the proposal mentions testing strategy
- Include an API reviewer if the proposal mentions GraphQL, REST, resolvers, or public interfaces
- Minimum 3 agents required; select additional based on content

**Output Format**
```
## Expert Review Summary: <change-id>

### 1. <Role> - <Focus Area>
**Strengths**: ...
**Key Suggestions**:
| Priority | Issue | Recommendation |
|----------|-------|----------------|
| HIGH | ... | ... |

### Cross-Cutting Issues
Issues flagged by multiple experts...

### Recommended Next Steps
1. ...
2. ...

### Summary of Decisions
| # | Recommendation | Decision |
|---|----------------|----------|
| 1 | ... | Accepted/Rejected/Modified |
```

**Automatic Update Handoff**
After completing all recommendations, ask:
"Would you like me to apply the accepted changes now? (This will invoke `/opsx:update <id>`)"

If user accepts, immediately invoke the Skill tool with `skill: "opsx:update"` and `args: "<change-id>"`.

**Reference**
- Use `openspec show <id> --json` for proposal metadata (`deltaCount`, per-requirement deltas) if needed.
- Review `openspec/config.yaml` (`context:` / `rules:`) and CLAUDE.md for project-specific conventions.
<!-- OPENSPEC:END -->
