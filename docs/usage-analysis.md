# Claude usage analysis: where the queue's tokens go

Task #110. Data from 2026-10-19 12:17 to 2026-10-20 17:44 (29.4 hours). All figures are Claude API
list prices as the logs report them. The account runs on usage windows, not a bill, so dollars here
stand for "share of the usage window": **$1.08 of Opus usage fills 1% of the five-hour window**
(see Q8).

## Summary

- **Total measured: $254.24** over 242 Claude processes and 4,636 API calls. All of it was Opus 5.5.
- **The cost column in ops.db overcounts by ×1.80** ($458 against $254). `total_cost_usd` is a running
  total for the whole session, and a resumed session carries the earlier total forward. `run_claude`
  adds it to the task's cost on every turn (`opsd.py:1033`), and `stream_claude` does the same for chats
  (`opsd.py:3245`). Tasks with one process are about right (×0.95). Tasks with two or more are ×2.27.
  Example: task #8 shows $45.33 but cost $5.76.
- **Resuming within an hour always hits the cache. After an hour it always misses.** All 99 resumes within
  59 minutes hit, and all 14 resumes after 64 minutes or more missed. The misses cost $11.48 extra.
- **About a quarter of a task's cost is start-up and orientation**: a median $0.53 and 8 API calls
  before the first edit. A fresh session starts at 32k tokens of context and reaches 62k by its first edit.
- **Cache writes are the biggest cost (39%)**, ahead of rereading context (33%) and output (27%). Every
  main-agent write uses the one-hour cache, which costs $8/M against $5/M for the five-minute cache.

## Method

The analysis script is stdlib Python, run from `/tmp/t110/analyse.py`, outside the repo. It is not
committed. It only reads its sources; it opens `ops.db` read-only.

1. **Processes.** Each `~/.claude-ops/runs/<id>/claude.jsonl` is split at its `{"type": "ops"}` lines.
   Each piece is one worker or release-checker process. The first is a fresh session; later ones
   `--resume` it. Design and chat transcripts (`runs/design-<id>/`, `runs/<id>/chat/`) have no ops
   lines, so they are split at each `SessionStart` hook. A process's phase comes from the opening
   words of its prompt (for example, "The queue rebased your branch…" is a gate-fix round).
2. **API calls.** The stream logs are not enough for per-call cost. Their `output_tokens` is the value
   at the start of streaming, about 2% of the real output (#97: 813 against 40,547). They also hold
   only some subagent calls. So each call's usage comes from Claude Code's own session files,
   `~/.claude/projects/*/<session>.jsonl` and `<session>/subagents/*.jsonl`. Those have the final
   usage, a timestamp, and the tool calls. They are joined to processes by message id, or by session
   and time for subagent calls. One session had no session file; its 5 calls use stream values.
3. **Prices**, taken from the logs alone. Fitting per-segment `result.usage` against the change in
   `total_cost_usd` over 165 clean result segments gives an exact fit (error under 1e-14):

   | Opus 5.5, $ per million tokens | Input | Cache write, 1 hour | Cache write, 5 minutes | Cache read | Output |
   |---|---|---|---|---|---|
   | From the logs | 4.00 | 8.00 | 5.00 ¹ | 0.20 | 20.00 |

   ¹ Only subagents write to the five-minute cache. Design-7, the one subagent session with complete
   logs, fits exactly at $5.00. Sessions #64 and #72 leave about $1.2 unexplained, which suggests some
   nested subagent calls are missing from the session files.
4. **Check.** Priced calls per session, divided by the session's final `total_cost_usd`: median 1.000,
   p10 1.000, p90 1.000 (119 sessions). Total priced $254.24 against $249.56 logged. The $4.68
   difference is calls made after a session's last result: tasks still running, and processes killed
   before they wrote one.
5. **Context size** of a call = input + cache write + cache read tokens. **Tool output size**: result
   characters ÷ 2.34, a ratio calibrated on 50 calls whose only tool returned more than 20k characters.
   At that ratio `opsd.py` (249 KB) is about 106k tokens.

### Coverage

| Included | Count |
|---|---|
| Worker tasks with a transcript | 82 |
| Release checks | 21 |
| Design chats | 9 |
| Project chats | 7 |
| Task chats (`runs/<id>/chat`) | 3 |
| Deploy runs | 0 (ops.db has none) |

**Skipped**, and why: task #110 (this analysis); tasks #25, #66–#70, #109 and #111 (cancelled or
queued, never ran); `runs/main-aimage` (only a build.log, no Claude transcript); `design-8` (empty
transcript). Tasks #65, #97, #98 and #99 were still running at the snapshot. They are included with
their data so far.

## Q1. What a fresh start costs

"Before the first edit" means calls in the first process before the first Edit or Write, or a Bash
command that writes a file (`sed -i`, `cat >`, a heredoc, …). Ten tasks made no edit in their first
turn and are left out.

| Project | Tasks | Start context | Context at first edit | Calls before first edit | $ before first edit (median) | $ before first edit (total) | Share of those tasks' cost |
|---|---|---|---|---|---|---|---|
| aimage | 14 | 34k | 70k | 5 | $0.62 | $8.72 | 27% |
| ballup-monorepo | 6 | 42k | 102k | 17 | $1.12 | $6.67 | 17% |
| claude-ops | 39 | 30k | 59k | 9 | $0.49 | $22.84 | 27% |
| diskgame | 10 | 34k | 51k | 7 | $0.39 | $5.35 | 29% |
| plugins | 3 | 32k | 58k | 12 | $0.53 | $2.07 | 37% |
| **all** | **72** | **32k** | **62k** | **8** | **$0.53** | **$45.66** | **25%** |

- The first API call of a fresh session **writes 20k tokens** (p10 17k, p90 24k): tools, CLAUDE.md and
  the brief. It also **reads 13k from a cache shared across sessions** (Claude Code's own system prompt).
  This confirms the 22k first seen in #97. That call costs a median $0.16.
- Orientation, from the first call to the first edit, adds a median 29k tokens. ballup-monorepo orients
  longest: 17 calls and 60k tokens.

## Q2. Resumes: cache hits and misses

A resume is the first API call of a process that continues an existing session. It is a hit when at
least half of its context comes from the cache. Every resume was either a near-full hit or a
near-full rewrite.

| Group | Resumes | Hits | Misses | Median gap at hit | Shortest gap at miss | Median gap at miss | Median context at miss | Extra $ from misses |
|---|---|---|---|---|---|---|---|---|
| Worker and release | 79 | 69 | 10 | 1 min | 64 min | 184 min | 137k | $10.10 |
| Design chat | 19 | 17 | 2 | 5 min | 99 min | 139 min | 61k | $0.76 |
| Project chat | 11 | 9 | 2 | 6 min | 91 min | 136 min | 51k | $0.62 |
| Task chat | 4 | 4 | 0 | 2 min | – | – | – | $0.00 |
| **All** | **113** | **99** | **14** | **2 min** | **64 min** | **183 min** | **82k** | **$11.48** |

Hits by gap: 72/72 under 5 min, 22/22 at 5–30 min, 5/5 at 30–60 min, 0/6 at 60–180 min, 0/8 over 3 h.
The longest gap that still hit was 59 minutes. **The cache lifetime is exactly one hour.** The
brief's sample is confirmed: a resume after 3 hours rewrote the whole transcript (#97: 67k, $0.52).

Where the misses came from:

| Owner | Phase | Gap | Context rewritten | Extra $ |
|---|---|---|---|---|
| #77 | rebase-conflict | 234 min | 265k | $2.07 |
| #72 | retry | 72 min | 190k | $1.48 |
| #84 | rebase-conflict | 244 min | 162k | $1.26 |
| #64 | retry | 64 min | 155k | $1.21 |
| #71 | retry | 64 min | 144k | $1.12 |
| #98 | retry | 183 min | 110k | $0.86 |
| #94 | retry | 187 min | 76k | $0.59 |
| #97 | retry | 184 min | 67k | $0.52 |
| #96 | retry | 184 min | 65k | $0.51 |
| #99 | retry | 184 min | 61k | $0.47 |
| 4 chats | – | 91–182 min | 32–65k | $1.38 |

Eight of the ten worker misses are retries after the usage limit stopped the queue for about 3 hours.
The other two are rebase-conflict turns that waited about 4 hours to merge. Within a turn, the cache
was rewritten only twice ($0.67). Subagents use only the five-minute cache: 520k tokens written.

## Q3. Context growth and when to start fresh

| Context at the call | Calls | Share of calls | Mean $ per call | Of which rereading context | Share of main-agent $ |
|---|---|---|---|---|---|
| 25k–50k | 916 | 20% | $0.05 | $0.01 | 19% |
| 50k–75k | 1,076 | 24% | $0.05 | $0.01 | 20% |
| 75k–100k | 856 | 19% | $0.05 | $0.02 | 18% |
| 100k–150k | 1,009 | 22% | $0.06 | $0.02 | 23% |
| 150k–200k | 441 | 10% | $0.07 | $0.03 | 12% |
| 200k–300k | 189 | 4% | $0.09 | $0.05 | 7% |
| 300k+ | 14 | 0.3% | $0.08 | $0.06 | 0.4% |

- The median main-agent context is 32k at call 1, 60k at call 10, 83k at call 25 and 126k at call 50.
  A task's final context has a median of 93k and a p90 of 178k. The largest was 318k (#64).
- Within a turn, context grows by a median 1,418 tokens per call (p90 3,090).
- Each call rereads its whole context at $0.20/M, so every 100k of context adds $0.02 to each call.
  Rereading is 33% of main-agent cost. Writing new context to the cache (tool output, replies) is 39%.
  Output is 27%.
- A resumed worker turn makes a median 11 calls (p90 30). From a resume to the end of the task there
  are a median 21 calls.

**Break-even.** Compare resuming at context C with starting fresh at context F. F is the fresh start
(32k) plus a handoff note plus whatever code the new session reads again. With n calls left:

- Warm cache: resume costs n·C·0.20. Fresh costs F·8 + (n−1)·F·0.20. Fresh wins when **C > F·(39+n)/n**.
- Cold cache: resume costs C·8 + (n−1)·C·0.20, the same shape as fresh. Fresh wins when **C > F**.

| Handoff note | Code read again | Calls left | Fresh context F | Break-even, warm | Break-even, cold |
|---|---|---|---|---|---|
| 5k | none | 21 | 37k | 106k | 37k |
| 5k | half the orientation (15k) | 21 | 52k | 148k | 52k |
| 5k | all of it (29k) | 21 | 66k | 189k | 66k |
| 10k | half the orientation (15k) | 11 | 57k | 257k | 57k |
| 10k | half the orientation (15k) | 21 | 57k | 162k | 57k |
| 10k | all of it (29k) | 21 | 71k | 204k | 71k |

Central case: a 7.5k note, half the orientation read again, F = 54k, and 21 calls left. **Fresh beats a
warm resume above about 155k, and beats a cold resume above about 54k.** 20 of 79 worker resumes were
above 155k, and all 10 cold resumes were above 54k. Starting fresh only where it pays would have saved
**$18.45 over 28 resumes**: $12.39 on cold ones and $6.06 on warm ones. Starting fresh every time
would save only $6.03, because most resumes are small and warm.

This model assumes a fresh session needs no more calls than the resumed one. It does not price the
risk of the new session losing track of the work.

## Q4. Where the cost goes

Total: $254.24.

| Kind | $ | Share |
|---|---|---|
| Worker task | $200.08 | 78.7% |
| Release checker | $37.47 | 14.7% |
| Design chat | $10.63 | 4.2% |
| Project chat | $5.45 | 2.1% |
| Task chat | $0.61 | 0.2% |
| Deploy | $0 | 0% (none ran) |

| Project | $ | Share |
|---|---|---|
| claude-ops | $122.30 | 48.1% |
| diskgame | $46.07 | 18.1% |
| ballup-monorepo | $40.53 | 15.9% |
| aimage | $36.43 | 14.3% |
| plugins | $8.91 | 3.5% |

| Phase (workers and release checkers; share of their $237.55) | Processes | $ | Share |
|---|---|---|---|
| Task, first turn | 82 | $128.00 | 53.9% |
| Release check, first turn | 21 | $29.49 | 12.4% |
| Retry after a stop (usage limit, no result) | 10 | $24.24 | 10.2% |
| Restart after the queue restarted | 33 | $21.21 | 8.9% |
| Rebase conflict (dev moved before merge) | 21 | $14.99 | 6.3% |
| Gate-fix round (merge check failed) | 7 | $8.46 | 3.6% |
| Resume after the user paused | 11 | $6.92 | 2.9% |
| Release carry/resolve | 3 | $3.57 | 1.5% |
| Answer after needs-you | 1 | $0.67 | 0.3% |

| Agent | $ | Share |
|---|---|---|
| Main agent | $249.16 | 98.0% |
| Subagents: python-test-engineer $2.12, general-purpose $1.94, Explore $1.02 | $5.08 | 2.0% |

Process counts for retry and restart include both tasks and releases. 42 processes ended without a
result event, mostly killed by queue restarts and the usage limit. They had spent $44.71. Most of that
work stays in the session and is used by the next turn, so it is not all waste.

## Q5. Batching small tasks

The median task cost is $1.70. The 41 tasks below it cost $37.65 in all.

- In the 35 small tasks with a first edit, **start-up and orientation are 40% of their cost**: a median
  $0.34 of a median $0.93.
- A small task starts at 31k, orients to 44k, ends at 61k, and makes 23 main calls.
- **Second task run in the first task's session, same files:** it saves its own start-up and
  orientation ($0.34). It pays $0.08 to reread the first task's extra context. **Net saving: $0.26 per
  batched task.**
- **Same, unrelated files:** it saves only the brief write ($0.24). It pays $0.14 to reread the first
  task's 30k of extra context on every call. **Net saving: $0.10 per batched task**, and less with
  each further task added.
- Overlap in practice: 28 of the 41 small tasks are claude-ops. Of 405 same-project pairs of small
  tasks, 164 share 30% or more of the files they read or edit, and 24 edited at least one file in
  common. 34 of the 41 started within 2 hours of another small task in the same project.

Upper bound: if the 41 small tasks ran in same-file pairs, about 20 start-ups would be saved, worth
**about $5 (2% of total)**.

## Q6. Model choice

- Every process ran `claude-opus-5-5`: 241 `init` events and all 4,636 calls. `setting("model")` was
  never used. The only other model is Haiku, in internal helper calls, for $0.08 in total.
- The logs give no price for any other model in a usable form. The Haiku entries have too few data
  points to fit. So the saving is shown at two assumed price ratios. 0.6× is a Sonnet-class model;
  this is an **assumption**, not a logged price. 0.2× is roughly the Haiku rate in these logs. The
  estimate assumes the cheaper model uses the same number of tokens, which it may not.

| Candidate for a cheaper model | $ | Share | Saving at 0.6× | Saving at 0.2× |
|---|---|---|---|---|
| Release checker (runs checks, writes a summary) | $37.47 | 14.7% | $14.99 | $29.98 |
| Rebase-conflict and carry turns | $14.99 | 5.9% | $6.00 | $11.99 |
| Small tasks (under $1.70) | $37.42 | 14.7% | $14.97 | $29.93 |

## Q7. The ten most expensive runs

"Lifetime" is the cost of one tool output: written to the cache once, then reread on every later call
in the task.

| Task | Project | $ (ops.db) | Turns | Main calls | Max context | Reread / write / output $ | Subagents | Cache-miss $ | Main cause |
|---|---|---|---|---|---|---|---|---|---|
| #64 Achievements | ballup-monorepo | $13.57 ($28.00) | 3: first, retry, gate-fix | 138 | 318k | 5.67 / 3.68 / 2.10 | $2.12, 1 agent | $1.55 | Very long first turn; reread design.md (23k, $0.81 lifetime) and CLAUDE.md (14k) whole |
| #72 dunk ship | ballup-monorepo | $10.33 ($19.85) | 5: first, restart, gate-fix, conflict, retry | 99 | 260k | 3.41 / 3.51 / 1.47 | $1.94, 3 agents | $1.48 | 190k context resumed cold after the usage limit; `cat CLAUDE.md` 13k |
| #77 Send to dev | claude-ops | $9.12 ($21.22) | 3: first, 2× conflict | 70 | 285k | 3.04 / 4.29 / 1.79 | – | $2.07 | `Read opsd.py` 6 times, 20–21k each ($2.02 lifetime); 265k rewritten after a 4 h merge wait |
| #98 Game log | diskgame | $6.56 ($19.07) | 6: first, retry, 3× restart, gate-fix | 74 | 218k | 2.38 / 2.46 / 1.71 | – | $0.86 | Three queue restarts; whole MatchController.cs (15k) |
| #84 Reader pane | claude-ops | $6.04 ($13.25) | 3: first, 2× conflict | 75 | 195k | 1.86 / 2.74 / 1.45 | – | $1.26 | `sed -n 900,1304p dashboard.html` (12k); 162k rewritten after a 4 h merge wait |
| #8 Thrust bumps | diskgame | $5.76 ($45.33) | 5: first, 3× restart, pause | 88 | 215k | 2.73 / 1.62 / 1.40 | – | – | Long debugging session; `cat` of whole C# files (8–10k each) |
| #71 swish --version | ballup-monorepo | $5.47 ($18.31) | 6 | 80 | 174k | 2.01 / 2.45 / 1.01 | – | $1.12 | `bin/swish` read 5 times; CLAUDE.md whole (13k) |
| #45 OpenSpec support | claude-ops | $4.41 ($7.92) | 2 | 66 | 178k | 1.62 / 1.32 / 1.47 | – | – | README + prompts + dashboard ranges |
| #60 Cross-project tasks | claude-ops | $4.41 ($8.10) | 2 | 66 | 189k | 1.77 / 1.41 / 1.23 | – | – | Read a 14k saved tool output back in; 3 big opsd.py dumps |
| #44 Slow dashboard | claude-ops | $4.37 ($8.28) | 2 | 85 | 171k | 1.92 / 1.27 / 1.18 | – | – | 85 calls; dashboard.html 300-line ranges |

Patterns:

- **Long sessions drive cost, not single reads.** In the top ten, rereading context costs more than any
  one file. #64 spent $5.67 rereading over 138 calls.
- **Big file dumps across all worker runs**, as lifetime cost:

  | Tool output | Calls | Tokens added | Lifetime $ |
  |---|---|---|---|
  | Bash, all | 3,628 | 2,632k | $41.74 |
  | Bash printing over 3k tokens of opsd.py | 103 | 563k | $9.27 |
  | Read, other files | 396 | 344k | $6.72 |
  | Read opsd.py | 20 | 201k | $3.65 |
  | Read dashboard.html | 11 | 118k | $1.94 |

  `Read opsd.py` with no range returns about 20k tokens, the first 2,000 lines, so the whole 106k file
  is never read in one go. Workers instead page through it, or `sed` large ranges, and the pages add up.
- **Gate loops are short.** Only 7 gate-fix rounds ran, for $8.46 in total, and no task had more than one.
- **Subagent fan-out is small.** Subagents are 2% of cost. Only #64, #72 and one design chat used them.
- **Repeated reads** of the same file three or more times: `opsd.py` ×5 in #77, `bin/swish` ×5 in #71.

## Q8. How fast the five-hour window fills

1,080 `rate_limit_event` samples cover 6 windows. Each sample is timed by the event logged just
before it. Usage is matched to all Opus calls on this Mac in the same intervals: $101.54, of which
$75.41 came from the queue and the rest from interactive sessions. Usage from other machines or
claude.ai is not visible here.

- **$1.08 of Opus usage fills 1% of the five-hour window**, so about $108 fills it. Per window the
  figure ranged from $0.78 to $1.43. Counting the queue alone, it is $0.80 per 1%.
- Two windows reached 100%: the ones resetting 10-20 11:40 and 16:20. Both had 6 Claude processes
  running at their peak. The seven-day window went from 24% to 9% (a weekly reset). It is not the limit
  that binds.

| Claude processes running at once | Hours observed | Window used per hour | Hours to fill 0→100% | Opus $ per hour (all) |
|---|---|---|---|---|
| 0 | 4.1 | 2.0% | 51 h | $3.42 |
| 1 | 1.5 | 15.1% | 6.6 h | $14.37 |
| 2 | 2.1 | 11.3% | 8.9 h | $12.23 |
| 3 | 1.4 | 13.9% | 7.2 h | $17.08 |
| 4 | 0.7 | 24.5% | 4.1 h | $19.52 |
| 5+ | 0.1 | 41.1% | 2.4 h | $36.70 |

The fill rate grows less than linearly with process count, because workers spend much of their time
waiting on builds and tests. From four processes up, the window fills in about 4 hours or less.
Interactive use on top of that adds about $3.40 an hour, or 2% of the window.

## Recommendations

Ranked by expected saving on this sample of $254.24. Each one would be its own task.

1. **Run the release checker and the conflict and carry turns on a cheaper model.** Saving **$21 (8%)**
   at an assumed 0.6× price, or $42 (16%) at 0.2×. These runs cost $52.46 (20.6%), and their work is
   mechanical: run the checks, summarise, merge two sides. Pick the model per task kind through
   `--model`, which `run_claude` already passes from `setting("model")`. Hold small tasks (another $15
   at 0.6×) until a trial shows the cheaper model doesn't take more turns. The price ratio is an
   assumption: the logs price Opus only.
2. **Use the five-minute cache for worker turns, if the CLI allows it.** Saving about **$24 (9.6%)**.
   All 12.3M main-agent cache-write tokens used the one-hour tier at $8/M. At the five-minute price
   ($5/M) they would cost $36.86 less. The 27 resumes that came 5–60 minutes after their last call
   would then miss, costing $12.57 more. I did not find a TTL switch in this repo; this needs a check
   of what Claude Code exposes.
3. **Resume only when the last call was under 55 minutes ago and the context is under about 155k.
   Otherwise start fresh with a handoff note** of 5–10k: the diff, a summary, and the files that
   matter. Saving **$18.45 (7.3%)** over 28 resumes. The cache always hit at 59 minutes or less and
   always missed at 64 minutes or more. A cold resume costs about 40× a warm one per token (C·$8/M
   against C·$0.20/M), so after a cold gap a fresh start wins at any context above about 54k. Every
   cold worker resume here was above that: median 137k, $10.10 rewritten. This matters most after a
   usage-limit wait (8 of the 10 cold resumes; #108 will make these waits routine) and after merge-queue
   waits of 4 hours (#77, #84).
4. **Fix the cost accounting before using it for any brake or budget.** This saves nothing by itself,
   but the dashboard shows ×1.80 the real cost, ×2.27 on tasks with more than one turn. Record each
   process's increase in `total_cost_usd` rather than adding the session's running total: the value
   before the turn is in the session's last result. #109's 85% brake should use `rate_limit_event`
   utilization, not ops.db cost.
5. **Read code in small ranges.** Saving **about $6 (2–3%)** if the large dumps are halved. Bash and Read
   outputs that print over 3k tokens of `opsd.py`, plus reads of `dashboard.html`, carry $14.86 of
   lifetime cost. Rule for worker prompts: `grep -n` first, then `sed -n` ranges of 150 lines or
   fewer. Don't page through `opsd.py` with `Read` (about 20k tokens a page, out of 106k for the file).
   Don't reread a file already in context (#77 read `opsd.py` 5 times).
6. **Batch small tasks only when they touch the same files.** Saving **about $5 (2%)** at most. A second
   small task run in the same session saves $0.26 when it shares files with the first, and only $0.10
   when it doesn't. Even that shrinks as more tasks pile into one context. Rule: batch two or three
   small tasks (under $1.70) in the same project when they share 30% or more of their files. 164 of
   405 small-task pairs met that. Never batch unrelated tasks.
7. **Leave subagents and gate loops as they are.** Subagents are 2.0% of cost and gate-fix rounds 3.6%.
   Neither is worth changing.

On throughput: the queue fills the five-hour window in about 4 hours with four or more Claude processes
running. To get through a window without stopping, keep three or fewer running at once (about 7 hours
to fill).
