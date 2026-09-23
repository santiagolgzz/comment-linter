# comment-lint

Flags Rust comments that should be cut or rewritten: redundant, history-narrating, stale, fragile, or commented-out code. Tree-sitter finds the comments. Cheap local checks handle what they can. TypeSafe's Jev model (via OpenRouter) answers five yes/no questions about each remaining comment, and fixed rules turn those answers into flags.

Design and rationale: [SPEC.md](SPEC.md).

> **Status:** v1 prototype. The thresholds are the spec's starting points and have **not** been calibrated yet (see [Calibration](#calibration)).

## Run

Needs [uv](https://docs.astral.sh/uv/). Dependencies are pinned inside the script.

```sh
export OPENROUTER_API_KEY=...
uv run comment_lint.py path/to/rust-sap-agent
```

```
crates/agent-sap/src/session.rs:81-82   REDUNDANT        0.97
crates/agent-sap/src/cli.rs:39-41       HISTORY          0.91
crates/sap-com/src/grid.rs:142          STALE            0.88
crates/sap-com/src/grid.rs:200          COMMENTED_CODE   -

crates/agent-sap/src/session.rs:12   TODO   TODO: evict old entries
--
4 flagged / 138 checked · 1 TODOs · cost $0.0021
```

| Option | What it does |
|---|---|
| `--json` | Full records for flagged comments: text, code context, all five probabilities, flags. This is the input to hand to Claude for fixes. |
| `--all` | Also show unflagged comments with their probabilities. With `--json`, include every record. |
| `--csv PATH` | Write every Jev-judged comment with its probabilities and an empty `label` column. |
| `--evaluate PATH` | Score the current rules against a labeled CSV. No API calls. |
| `--dry-run` | Extract and run the local checks only. No API key needed. |
| `--cache PATH` / `--no-cache` | Answer cache, default `.comment-lint-cache.json`. |

**Exit codes:** `0` clean · `1` something was flagged · `2` setup error (missing key, HTTP 401/403), or some comments ended as `ERROR` with nothing flagged.

## What happens to each comment

1. **Skipped, no API call:** doc comments (`///`, `//!`, `/** */`, `/*! */`), empty or decorative lines, tool directives (`// SAFETY:` in any case, `rustfmt::`, `clippy::`, `@generated`, …), and license headers above the first code in a file.
2. **Listed as TODO, not judged:** blocks containing `TODO`/`FIXME` (upper case anywhere, any case at the start of a line).
3. **Flagged `COMMENTED_CODE` locally:** the text parses cleanly as Rust in some container (function body, item, impl, struct, match) *and* shows strong code evidence (`;`, `=`, `::`, `->`, braces, calls, paths, declarations, control flow). A bare identifier, keyword-plus-word, lone number, or comparison doesn't count. In a block that mixes prose and code, one blank-line-separated paragraph that is clearly a statement or item is enough.
4. **Sent to Jev:** everything else. One comment per request, at most 4 in flight. HTTP 429/502/503/504 and timeouts are retried with backoff, up to 3 tries in total, after which the comment is reported as `ERROR`.

Each request carries the comment, the enclosing function or type signature, the preceding statement, and the following statement (for a trailing comment, the statement it trails). An item's attributes come with it. Inside macro bodies, where tree-sitter only sees a flat run of tokens, the context comes from the surrounding source lines instead. Context is capped at about 1,000 tokens, trimming the following code first.

Every request sets `provider: {"zdr": true, "data_collection": "deny"}`, so it fails rather than reach a provider that retains or trains on data. The model is pinned to `typesafe/jev-1.13`.

## Rules

In `THRESHOLDS` at the top of `comment_lint.py`:

| Flag | Rule |
|---|---|
| `STALE` | `inconsistent > 0.75` |
| `HISTORY` | `history > 0.80` |
| `REDUNDANT` | `restates > 0.90` and `rationale < 0.20` |
| `FRAGILE` | `fragile > 0.85` and `rationale < 0.30` |

When `rationale > 0.70`, only `STALE` can fire. The score shown is the probability that triggered the flag.

## Calibration

1. `uv run comment_lint.py --csv dump.csv <repos…>` over ~200 comments.
2. Fill the `label` column with `keep`, `cut`, or `rewrite`.
3. Edit `THRESHOLDS`, then run `uv run comment_lint.py --evaluate dump.csv`. It shows recall on `cut`/`rewrite`, the false-flag rate on `keep`, and which `keep` rows got flagged. Repeat until false flags are rare.
4. Commit the labeled CSV as the regression set. Re-run `--evaluate` whenever the thresholds change. If the questions or the model version change, dump a fresh CSV, copy the labels over, then evaluate.

Not built yet: the batch-size experiment (SPEC §4 step 4).

## Tests

```sh
uv run --with pytest==8.4.2 --with-requirements comment_lint.py pytest -q tests
```

The tests mock the Jev endpoint. They cover extraction, context, local checks, rules, retries, the concurrency limit, the cache, and the output formats.
