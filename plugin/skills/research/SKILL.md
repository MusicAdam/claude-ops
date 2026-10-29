---
name: research
description: Use for a research question asked for this repo — market or competitor analysis, a technology evaluation, "look into X", "what's the state of Y", or `/claude-ops:research <question> [T0|T1|T2|T3]`. Runs a token-frugal loop (brief → tiered search or parallel workers → dated, sourced report) and writes exactly one report into the current repo's docs/research/<slug>.md, then ends with a plain-language chat summary in a fixed format.
version: 1.0.0
---

# Research

Turns a question into a dated, sourced report in the current repo, without
spending tokens the question doesn't need.

## Where files go

The run must leave exactly **one** new file in the repo: the report. Its branch
gets merged, so anything else would be merged too.

| Path in the repo | Holds |
|---|---|
| `docs/research/<slug>.md` | The report. No date in the file name; the date goes in the header. Create `docs/research/` if missing. If `<slug>.md` already exists, pick a new slug (reports are immutable). |
| `docs/research/raw/` | Scratch while the run lasts: the brief, worker drafts, API pulls, saved page text. **Delete the whole folder before finishing.** |

Templates live next to this file in `templates/`; `access.md` is the access
ladder and free-source catalogue.

Rules:

- **Write nothing outside the repo**, and nothing in the repo but the report.
  The brief lives in `docs/research/raw/brief.md`; its question, tier and
  done-criteria are repeated in the report header.
- **Read earlier reports** in `docs/research/` for prior work, and re-search only
  what's stale or missing.
- **Don't ask questions when `OPS_TASK_ID` is set.** Nobody is there to answer.
  State your assumptions in the report instead.
- Before the final summary, run
  `git status --porcelain --untracked-files=all` and check that the report is the
  only new or changed path. Plain `--porcelain` folds a new folder into one line
  and hides a leftover `raw/`.

## Tiers — pick one before searching, and say which

| Tier | When | Caps |
|---|---|---|
| **T0 Lookup** | One fact, number, or URL | 2–10 searches, inline |
| **T1 Scan** (default) | State of X, short comparison | 10–30 searches, 0–2 workers, ~1-page report |
| **T2 Deep dive** | Business screens, technology evaluations | 3–5 parallel workers, full report |
| **T3 Watch** | Something to recheck on a cadence | One T0/T1 pass now; the report's "Next actions" says what to recheck and when |

Multi-agent runs cost ~15× chat tokens. Use workers only when the question splits
into independent strands.

**What the limits protect.** On a Claude subscription nothing is billed per run;
what a run uses up is the plan's usage allowance. The search caps above are the
main control. Prefer Sonnet for workers: Opus uses the allowance faster.

## Steps

1. **Check what we know.** Read earlier reports in `docs/research/`. Re-search
   only what's stale or missing.
2. **Ask the profile questions first** if the answer depends on the user's
   constraints (time, money, skills, risk) and someone is there to answer (no
   `OPS_TASK_ID`). Otherwise state your assumptions.
3. **Write the brief** from `templates/brief.md` to `docs/research/raw/brief.md`:
   question, tier, caps, done-criteria, preferred sources, out of scope.
4. **Research.**
   - Start with short broad queries, then narrow. Prefer primary sources.
   - T2: launch all workers in one message, `model: sonnet`, background. Each gets a
     brief from `templates/worker-brief.md`: objective, output file
     `docs/research/raw/<slug>-w<n>-<strand>.md`, required sections, a 30-search
     cap, boundaries. Workers return a 5-line summary and the path — never the
     full findings. API pulls go to `docs/research/raw/` too.
   - **Access ladder** (details and free APIs: `access.md` next to this file).
     Read it before planning around a blocked site or writing a worker brief. In
     short:
     1. An API or feed first.
     2. WebFetch for a quick look. Its output is a model's summary, so treat it
        as a lead.
     3. For anything you quote, count or take a number from, get the page text
        on disk: an API's JSON, the Internet Archive's copy, or a Chrome extract
        saved to `docs/research/raw/`.
     4. Chrome for review sites and JS pages, if it is connected.
     5. Otherwise mark it blocked in the report's "Blocked or unreachable" line.
5. **Synthesize** into `docs/research/<slug>.md` from `templates/report.md`.
6. **Clean up.** Put recheck ideas under the report's "Next actions", delete
   `docs/research/raw/`, and confirm the report is the only new file.
7. **Finish in chat with the final summary** — see below. Always.

## Business screens: ceiling first, then three profiles

Don't use the user's limits as a filter before judging an idea. Two passes:

1. **Ceiling.** What a realistic winner in this space reaches, from evidence:
   market size, what the top players earn, how many winners there are, and who
   usually wins (incumbents, funded teams, solo makers). This is the best case
   with evidence behind it, not "if everything goes right". Demand and
   competition are scored once, here, because they're facts about the market.
2. **Profiles.** Assess the idea against three profiles: A side project
   (~10 hrs/week, under $5k), B committed (~20–25 hrs/week, $25–50k), C all-in
   (full-time, $100k+). Per profile: can it reach the ceiling (yes / partly /
   no), realistic revenue range, time to first dollar, and where it breaks.

End with the **smallest profile** at which the idea makes sense. "Only at C"
is a finding, not a rejection: it tells the user what it would take. Keep the
profile pass to one table per idea, so reports don't triple in length.

Worker strands that feed this: the ceiling (market size, top-earner revenue),
the field (who wins, incumbent features, team sizes and money raised by
winners), and the path (time and cost from start to first revenue for comparable
products, built by teams of each size).

## Quality rules

- Date every number. Source every claim. Give confidence and why.
- Tier A = primary (filings, official docs, APIs, papers, first-hand posts).
  B = reputable secondary. C = blogs, SEO guides, vendor and competitor posts.
  A C-tier source never carries a number alone.
- Label inferences as inferences. Vendor claims are reported as claims.
- Absence in N searches is not proof of absence — say "found none in N searches".
- Quotes and numbers come from text on disk (saved API JSON, an archive copy, a
  saved Chrome extract), never from a WebFetch summary alone. Each source line
  says how it was read (`via Wayback snapshot 2026-08-28`, `via Chrome`, `via API`).
- Prefer counts to anecdotes: pull up to 100 items from an API, count them, and
  report the count with the query.
- Every report names its blocked or unreachable sources and what they would
  have added.
- Never financial advice. Stock work is a dated thesis snapshot.
- Write absolute dates (2026-10-15), never "last week".

## Final summary — required format for every research answer

Every research request ends with this chat message, written in plain language
(answer first, everyday words, short sentences, "you"/"I"). Use
`templates/final-summary.md`. Sections, in order:

1. **Answer** — the recommendation or finding in 1–3 sentences.
2. **What <key term> is** — a plain-language primer for any jargon the answer
   depends on. Skip only if there is none.
3. **A worked example** — one concrete, realistic scenario with real-looking
   numbers. For a product: how it goes today vs. with it. For a stock: what a
   specific event would do to the thesis. For a technology: one task before and after.
4. **Why, with the evidence** — Reason, Evidence, Strength. Evidence names the
   specific source, quote or number, and date. Strength is Strong / Medium / Weak
   plus a few words on why (primary? how many items? off-target?).
5. **The ceiling, and what each profile gets** — business screens only: the
   ceiling in a sentence or two, then one line each for profiles A, B and C,
   and the smallest profile that makes sense.
6. **What I'm less sure about** — inferences, gaps, and the single cheapest test
   that would settle the biggest doubt.
7. **What I added** — the report's path.

T0 lookups may compress this to Answer + evidence with source, but keep the order.

**When the answer is "no"** — don't do it, drop it, not viable — use the short
form instead. A worked example of how it *would* work contradicts the verdict.

1. **Answer** — the no, plus the one-line reason.
2. **Why not** — the 2–4 deciding facts, each with source and strength on one line.
3. **What would change this** — only if something realistic would; otherwise skip.
4. **Next** — what to do instead, in one or two lines.
5. **Files** — the report's path.

No primer, no worked example, no "less sure" section, unless the user needs one to
follow the reason. The full report file keeps everything.

**Chat formatting:** the terminal breaks wide tables. In chat, write the evidence
section and comparisons as numbered lists (claim, evidence, then *Strength:* on its
own line). Tables are fine in report files.
