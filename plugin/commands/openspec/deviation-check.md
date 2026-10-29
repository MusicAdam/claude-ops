---
name: OpenSpec: Deviation Check
description: Compare OpenSpec plan against actual implementation to identify deviations.
category: OpenSpec
tags: [openspec, review, deviation, audit]
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

**Purpose**
Analyze an OpenSpec change proposal and compare it against the actual implementation to identify deviations that need documentation.

**Guardrails**
- Do NOT make any code changes. This is analysis only.
- Focus on structural deviations, not minor wording differences.
- Categorize deviations by type and severity.

**Steps**
1. Resolve and read the OpenSpec change artifacts:
   - `openspec status --change "$ARGUMENTS" --json`
   - Read each from `artifactPaths.<artifact>.existingOutputPaths`

2. Identify implementation locations from the spec:
   - Extract file paths mentioned in tasks.md
   - Extract module/class names from design.md code examples

3. Read the actual implementation files and compare against spec.

4. Identify deviations in these categories:
   - **Model/Schema**: Fields added, removed, or changed type
   - **Directory Structure**: Files in different locations than specified
   - **API/Interface**: Method signatures, return types, parameters
   - **Removed Features**: Planned features that weren't implemented
   - **Added Features**: Implementation details not in original spec

5. Generate a deviation report:

```
## Deviation Report: <change-id>

### Summary
- X deviations found (Y need documentation, Z are documented)

### Deviations Requiring Documentation

| Category | Spec Says | Implementation Has | Severity |
|----------|-----------|-------------------|----------|
| Model | `entity_type` field | Removed (computed from key) | Low |
| Structure | `test/batch/` | `test/unit/batch/` | Medium |

### Already Documented Deviations
(List any deviations already noted in the spec files)

### Recommendations
1. Update tasks.md section X.Y to reflect...
2. Update design.md Decision N to show...
```

6. Ask user which deviations to document, then update the spec files accordingly.

**Usage**
```
/claude-ops:openspec:deviation-check add-fetch-job-batch-processing
```

**Reference**
- Check `openspec/config.yaml` (`context:` / `rules:`) and CLAUDE.md for project conventions
- Related: `/claude-ops:openspec:review` for pre-implementation review
<!-- OPENSPEC:END -->
