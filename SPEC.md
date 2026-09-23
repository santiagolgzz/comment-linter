# comment-lint — spec

A small tool that flags low-value or stale-prone code comments, using TypeSafe's Jev decision model (via OpenRouter) as a cheap screener. First target: `rust-sap-agent`.

Status: v1 prototype built as `comment_lint.py` (see README). Thresholds not yet calibrated. The tool is being reworked around "What a good comment is" below; the sections after it describe v1.

## What a good comment is

Comments in these projects are read by coding agents, not people. An agent reads code about as fast as prose, so a comment is only worth its place if it tells the agent something the code can't.

**A good comment states a non-local fact:** a cause and its effect that the nearby code doesn't show, because the link lives somewhere else (another file, a caller, an external API, runtime behavior). It is informative, not normative: it describes what happens and leaves the decision to the reader.

> This reads the totals that `X` writes. Running it before `X` double-counts them.

An agent asked to reorder this code now knows what it would break, and can decide what to do about it. Naming the other thing (`X`) lets it check the fact still holds.

**The test for any comment:** would an agent reading only the nearby code miss this, and would missing it lead to a wrong change?

Everything else costs something. Comments influence edits: an agent tends to bend a change to agree with what a comment says. So, from most to least harmful:

1. **Stale:** says something the code no longer matches.
2. **Memorializing:** records how the code got here (stories of past versions, pointers to design docs, plans, issues, phases or sessions) instead of facts about it now. That history belongs in commit messages and PRs.
3. **Commands without facts:** "must", "never", "keep in sync" with nothing saying what happens otherwise.
4. **What-only:** describes what the code visibly does. Little value, and mild pull toward the current shape.

The best comments are also the likeliest to go stale, because they describe something elsewhere that can change without anyone touching this code. That is why checking for staleness matters most.

## Goal

Find comments that should be cut or rewritten, especially the kind AI coding agents leave behind:

- **Redundant** — restates what the code plainly says (`// create a vector to hold the results`).
- **History** — narrates a change instead of describing the code (`// now uses the new parser`, `// fixed: was off by one`).
- **Stale** — makes a claim the code no longer matches.
- **Fragile** — spells out implementation details that will go stale the next time the code changes.

Non-goals: editing code, judging style/grammar, enforcing doc coverage.

## Pipeline

```
source files
  → extract comments + the code they describe      (local, tree-sitter)
  → cheap local checks (commented-out code, TODOs)  (local, no API)
  → Jev answers 5 yes/no questions per comment      (OpenRouter)
  → fixed rules turn probabilities into flags       (local, plain code)
  → report: file:line  CATEGORY  score
  → optional: hand only flagged comments to Claude to propose fixes
```

Jev is the screen, not the editor. It sees every comment; Claude sees only the few percent that get flagged.

## 1. Extraction

- Use **tree-sitter** with the Rust grammar. Not `syn`: `syn` throws away ordinary `//` comments and keeps only doc comments.
- **v1 covers ordinary comments only: `//` and `/* */`.** Doc comments (`///`, `//!`) are out of scope. They serve readers of generated docs and IDE hovers, who don't see the body, so restating the obvious can be correct there. Mixing them in would skew calibration. They need their own policy later.
- Merge consecutive line comments into one comment block.
- For each block, record: file, start/end line, kind (`line` / `block`), text, and whether it trails code on the same line.
- **Context** — send only what the questions need; OpenRouter's guidance is that irrelevant detail lowers Jev's accuracy:
  - the immediately **preceding** statement (ordering comments like "must run after `foo`" depend on it),
  - the **following** statement or block the comment sits above (for a trailing comment, its own line),
  - the enclosing function or type **signature/name**.
  - Truncate to a token budget (~1,000 tokens), trimming the following block first. Jev's limit is 32,000 tokens, so the budget is about signal, not the limit.
- **Skip or handle locally, no API call:**
  - License/header boilerplate.
  - Tool directives (e.g. `// rustfmt::skip`-style markers, `// SAFETY:` blocks — keep, never flag).
  - `TODO` / `FIXME` → listed separately in the report, not judged.
  - Commented-out code → detect locally and flag `COMMENTED_CODE`. Tree-sitter is error-tolerant and returns a tree even for prose, so "it parsed" means nothing. Require both:
    - **Clean parse**: wrap the text in a container (e.g. `fn _x() { … }` for statements; try item-level too) and accept only if the tree has no `ERROR` or `MISSING` nodes.
    - **Strong code evidence**: a clean parse alone isn't enough, since short English is often valid Rust (`// safety` is a bare identifier; `// return result` is a return statement). Also require at least one of: operators or code punctuation (`;`, `=`, `::`, `->`, braces), literals, calls, paths, declarations (`let`, `fn`, `struct`…), or control-flow nodes. A bare identifier or keyword-plus-word never counts.

## 2. Jev request

**v1 uses one comment block per request**, all five questions in it (they're answered independently and in parallel). Jev does support batching: put several records in `state` and name the target record in each question's text; OpenRouter's own examples do this. v1 avoids it on purpose because Jev 1.13 loses accuracy as unrelated state grows, and here each record carries code context, so a batch of 20 means most of the state is irrelevant to any one question. Cost is negligible and concurrency keeps request overhead acceptable (~200 requests at ~0.75 s, four in flight, finish in about a minute). Batch size is tested during calibration (§4).

- Endpoint: `POST https://openrouter.ai/api/alpha/decisions`
- Auth: `Authorization: Bearer $OPENROUTER_API_KEY`
- Model: **pin `typesafe/jev-1.13`**, not `~typesafe/jev-latest`. Thresholds are tuned against one version; the alias would silently shift them. Re-calibrate on purpose when upgrading.
  - *Found in live testing:* `typesafe/jev-1.13` is itself an alias. Responses name a dated build (`typesafe/jev-1.13-20260917`), and the API accepts that name directly. The tool pins the dated build.
- `state` accepts a string or a JSON object. Send an object:

```json
{
  "model": "typesafe/jev-1.13",
  "provider": { "zdr": true, "data_collection": "deny" },
  "state": {
    "file": "crates/agent-sap/src/session.rs",
    "item": "fn attach_session(&self, id: &str) -> Result<Session>",
    "comment_kind": "line",
    "comment": "// Loop over the children and collect their ids",
    "code_before": "let mut ids = Vec::new();",
    "code_after": "for child in node.children() {\n    ids.push(child.id());\n}"
  },
  "questions": {
    "restates": {
      "type": "noul",
      "instructions": "Does the comment only restate what the code already makes obvious?",
      "criteria": {
        "true": "A competent reader learns nothing from the comment that the code does not already show.",
        "false": "The comment adds information not evident from the code itself."
      }
    },
    "rationale": {
      "type": "noul",
      "instructions": "Does the comment explain a non-obvious reason, constraint, invariant, or tradeoff?",
      "criteria": {
        "true": "It explains why, or a rule the code must respect, that the code alone does not reveal.",
        "false": "It describes what or how, or nothing beyond the code."
      }
    },
    "inconsistent": {
      "type": "noul",
      "instructions": "Does the comment make a claim that the code contradicts?",
      "criteria": {
        "true": "Something the comment states about behavior, names, values, or order does not match the code.",
        "false": "Everything the comment claims is consistent with the code."
      }
    },
    "history": {
      "type": "noul",
      "instructions": "Does the comment narrate a past change instead of describing the code as it is?",
      "criteria": {
        "true": "It refers to what the code used to do, what was changed, fixed, or replaced, or to a past session or refactor.",
        "false": "It describes the code in the present tense with no reference to its history."
      }
    },
    "fragile": {
      "type": "noul",
      "instructions": "Does the comment describe implementation details that are likely to change whenever the code changes?",
      "criteria": {
        "true": "It restates specific steps, counts, variable names, or mechanics that a routine edit would invalidate.",
        "false": "It describes intent, contract, or reasons that survive routine edits."
      }
    }
  }
}
```

- Response: `answers.<name>.noul` is the probability of "yes" (0–1). `usage.cost` is the USD cost of the call — sum it into the report footer.
- **Privacy is enforced per request**, not assumed: `provider.zdr: true` routes only to endpoints that don't retain prompts, and `data_collection: "deny"` excludes providers that store or train on data. If no provider qualifies, the request errors instead of going through. (Jev's model page currently lists TypeSafe as no-training, no-retention; the flags keep that true if it ever changes.)
- The five questions overlap on purpose — e.g. `restates` and `fragile` are different reasons to cut. At this price, trimming one isn't worth it.

## 3. Rules (starting points — calibrate before trusting)

The policy lives in plain code, not in the model. Combine probabilities like this:

| Flag | Rule |
|---|---|
| `STALE` | `inconsistent > 0.75` |
| `HISTORY` | `history > 0.80` |
| `REDUNDANT` | `restates > 0.90` and `rationale < 0.20` |
| `FRAGILE` | `fragile > 0.85` and `rationale < 0.30` |
| `COMMENTED_CODE` | local parse check (no API) |

Never flag when `rationale > 0.70` except for `STALE` — a wrong "why" is still wrong.

Report score = the probability that triggered the flag.

## 4. Calibration

1. Run the tool once over all of `rust-sap-agent` (~94 `//` lines, so fewer blocks — likely too few alone; add a public Rust repo for a sample of ~200) with no thresholds — just dump every probability to a CSV.
2. A human marks each row `keep` / `cut` / `rewrite` (one column).
3. Pick thresholds that catch most `cut`/`rewrite` rows while flagging few `keep` rows. False flags are the cost to minimize — a noisy lint gets ignored.
4. **Batch size**: re-run the labeled set at batch sizes 1, 5, 10, and 20. Enable batching only if the false-positive rate on `keep` comments doesn't materially increase **and** recall on `cut`/`rewrite` comments doesn't regress.
5. Save the labeled CSV as a regression set. Re-run it whenever the questions, thresholds, or pinned model version change.

## 5. Output

Default, one line per flag, sorted by file then line:

```
crates/agent-sap/src/session.rs:81-82   REDUNDANT   0.97
crates/sap-com/src/grid.rs:142          STALE       0.88
crates/agent-sap/src/cli.rs:39-41       HISTORY     0.91
--
14 flagged / 138 checked · 6 TODOs · cost $0.0021
```

- `--json`: full records (comment text, target code, all five probabilities, flags) — this is what gets handed to Claude.
- `--all`: include unflagged comments with their probabilities (for calibration).
- Exit code 1 if anything is flagged, so it can run as a check.

## 6. Practical behavior

- **Cache** answers keyed on a hash of (comment + target code + question set + model ID). Re-runs only send comments that changed.
- **Concurrency**: at most ~4 requests in flight. Retry on HTTP 429 / 502 / timeout with backoff; give up on a comment after 3 tries and list it as `ERROR`.
- **Cost**: ~800 input tokens per request (context + five questions) × ~200 comments ≈ 160k tokens ≈ under a cent at $0.042 per million. Output is free.
- Read the API key from `OPENROUTER_API_KEY` only.

## 7. Build notes

- Prototype as a single Python script run with `uv` (inline script metadata: `httpx`, `tree-sitter`, `tree-sitter-rust`, exact pins). Port to Rust only if it becomes a permanent lint.
- The OpenRouter Python SDK has an `Alpha.Decisions` call, but plain HTTP is simpler and has fewer dependencies.
- Network: from the SLB network, `docs.typesafe.ai` gets a firewall warning page (category "AI-platform-service"); `openrouter.ai` loads normally. Another reason to build elsewhere.

## Python (added after v1)

Python is supported alongside Rust, with the same questions and rules:

- `#` comments only. Docstrings are strings, not comments, so they're left out, the same way Rust doc comments are.
- Directives skipped: `#!`, encoding lines, `noqa`, `nosec`, `type:`, `ruff:`, `mypy:`, `pyright:`, `pylint:`, `flake8:`, `isort:`, `pragma:`, `fmt:`.
- Commented-out code: the text is dedented and parsed as a module. `{`/`}` don't count as code evidence in Python, where braces are data.

Changes to the commented-out-code check found while adding Python, applied to both languages:

- **Literals no longer count as code evidence.** `# mcp < 2.0` and `// capacity >= 9` are labels, but a clean parse plus a number made them "code". Real commented-out code nearly always has stronger evidence (`=`, a call, `;`, a declaration).
- **A call counts only when `(` directly follows the name.** `Initialize (optional)` parses as a call in both languages.
- `vendor/` and `third_party/` folders are skipped: they hold other projects' code.

## Findings from the first live runs

- The request and response shapes match the docs exactly. Requests were served by provider `TypeSafe`. Cost is $0.042 per million input tokens; output tokens are free.
- About 850 input tokens per comment on average. 245 ripgrep comments took 23 s, cost $0.0088, with no errors.
- Identical requests vary slightly (for example `restates` 0.62 / 0.63 / 0.65).
- Scores run lower than the §3 starting thresholds assume. Across 245 ripgrep comments the highest `restates` was 0.85, and an obviously redundant demo comment scored 0.62–0.66, so `REDUNDANT` at 0.90 almost never fires. Calibration (§4) should settle this.
- On AI-written Python (the monarch MCP repos, 292 comments), scores differ clearly from hand-written Rust (ripgrep, 245): median `restates` 0.29 vs 0.21, 70 vs 18 comments above 0.5, and median `rationale` 0.28 vs 0.70. The highest `restates` was 0.82, on comments such as `# Configure logging`, so `REDUNDANT` at 0.90 still never fired.
- Other comments inside the context leaked into answers: with a neighbor's "why" comment in the code, a comment's `rationale` rose from 0.11 to 0.71. The tool now strips comments from the context code.

## Open questions

- Context for a comment at the top of a function body that describes the whole function (`// Returns true if …`). Sending only the next statement hides the code it describes: on the demo, `inconsistent` was 0.20 with the next statement and 0.73–0.75 with the whole body (comments stripped). Whether sending the whole body helps overall should be measured on the labeled set.

- Doc-comment policy for v2: which questions apply to `///`/`//!`, and with what thresholds.
- More languages? Python was a small addition (a grammar, directives, a code-evidence table), so others should be too.

Decided against: a sixth Jev question for "comment names something that doesn't exist." Jev can't tell "doesn't exist" from "not in the context we sent." If wanted, do it later as local symbol lookup.

## Sources

- [Jev documentation hub (OpenRouter)](https://openrouter.ai/docs/guides/community/jev)
- [Jev tutorial — request/response format](https://openrouter.ai/docs/guides/community/jev-tutorial)
- [Decisions API reference](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-questions-and-answers-request)
- [Jev model page — price, context, data policy](https://openrouter.ai/~typesafe/jev-latest)

Not verified: claims from the originating chat about third-party projects (`jev-code`, `jev-semantic-reviewer`) and their benchmark numbers.
