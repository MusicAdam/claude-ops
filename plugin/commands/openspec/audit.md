---
description: Audit an OpenSpec change for internal consistency, drift, and completeness
argument-hint: [change-id]
---

# OpenSpec Audit

Audit an OpenSpec change for internal consistency between its documents (proposal, design, tasks, specs) and detect drift from actual code.

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


## Step 1: Identify Change

Parse `$ARGUMENTS` to get the change-id. If not provided:
1. Run `openspec list --json` to find active changes
2. If one active change, use it automatically
3. If multiple, ask which to audit
4. If none, inform the user

## Step 2: Load All Change Documents

Resolve the change's artifacts first:

```bash
openspec status --change "<change-id>" --json
```

Read each artifact from its `artifactPaths.<artifact>.existingOutputPaths`. For
the default `spec-driven` schema that is the proposal, spec deltas, design (may
be absent — it is conditional), and tasks. Also read any main specs the change
touches, listed by `openspec list --specs --json`.

Roles: the proposal is the source of truth for intent, design carries technical
decisions, tasks is the implementation checklist, and spec deltas hold
requirements and scenarios.

## Step 3: Run Audit Checks

### 3.1 Proposal <-> Specs Alignment
- Every capability listed in proposal Impact section has a corresponding delta spec in `specs/`
- No delta specs exist for capabilities NOT mentioned in the proposal
- The "What Changes" bullets are covered by delta requirements (no orphaned promises)

### 3.2 Proposal <-> Tasks Alignment
- Every change described in proposal has corresponding task(s) in tasks.md
- No major task sections exist for work not described in the proposal
- Task descriptions match proposal intent (not drifted in scope)

### 3.3 Tasks <-> Specs Alignment
- Every ADDED/MODIFIED requirement has task(s) that implement it
- Every REMOVED requirement has task(s) that handle the removal
- No tasks exist that would create behavior not captured in specs

### 3.4 Design <-> Implementation Alignment (if design.md exists)
- Decisions in design.md are reflected in task approach
- No contradictions between design decisions and spec requirements
- Open questions in design.md are resolved or tracked

### 3.5 Spec Quality
- All delta requirements use proper `## ADDED|MODIFIED|REMOVED Requirements` headers
- Every requirement has at least one `#### Scenario:`
- MODIFIED requirements include full replacement text (not partial diffs)
- Scenarios use WHEN/THEN format
- Requirements use SHALL/MUST language

### 3.6 Task Progress Consistency
- Check for tasks marked complete `[x]` that reference files/code — verify those files actually exist and reflect the changes
- Check for tasks marked incomplete `[ ]` where the code already implements them (drift)
- Identify blocked tasks (depend on incomplete prerequisites)

### 3.7 Code Drift Detection
- For files listed in proposal Impact section, read a sample (up to 5 key files) and check:
  - Do they already contain changes described by incomplete tasks? (tasks behind reality)
  - Are they missing changes described by complete tasks? (tasks ahead of reality)
  - Are there changes not described by any task? (undocumented drift)

### 3.8 Cross-Change Conflicts
- Run `openspec list --json` and check if other active changes affect the same specs
- Flag overlapping requirements that could conflict

## Step 4: Report Findings

Present a structured audit report:

```
# Audit: <change-id>

## Summary
- Documents: [list which exist]
- Specs affected: [list]
- Task progress: [N/M complete]
- Issues found: [N]

## Findings

### 1. Critical (blocks correctness)
[Numbered list — inconsistencies that would cause wrong implementation]

### 2. Drift (spec != reality)
[Numbered list — places where docs and code have diverged]

### 3. Gaps (missing coverage)
[Numbered list — promised features without specs/tasks, or specs without tasks]

### 4. Warnings (should fix)
[Numbered list — quality issues, formatting, weak scenarios]
```

## Step 5: Offer Fixes

After presenting findings, ask the user which issues to address. Group by:

- **Auto-fixable**: Missing scenario format, task checkbox drift, missing delta headers
- **Needs discussion**: Scope drift, conflicting requirements, undocumented changes
- **Code changes needed**: Task/code misalignment that requires implementation work

## Guidelines

- **Read before judging**: Load all documents and referenced code before flagging issues
- **Be specific**: Quote the conflicting text from each document, with file:line references
- **Prioritize impact**: Lead with issues that would cause incorrect implementation
- **Don't nitpick**: Skip cosmetic issues unless they cause parsing failures (e.g., wrong scenario header format DOES matter)
- **Proposal owns scope, design owns mechanism**: when the two disagree about
  *what* is being built, the proposal wins and the design has drifted; when they
  disagree about *how*, the design wins. A design decision that widens scope is
  not a design decision — flag it as a proposal amendment rather than letting the
  proposal be quietly rewritten to match.
