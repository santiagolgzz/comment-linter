#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "httpx==0.28.1",
#     "tree-sitter==0.25.2",
#     "tree-sitter-rust==0.24.0",
# ]
# ///
"""comment-lint: flag low-value or stale-prone Rust comments.

Pipeline: tree-sitter extracts comment blocks and the code around them, cheap
local checks handle TODOs, directives and commented-out code, Jev (via
OpenRouter) answers five yes/no questions per remaining block, and fixed rules
turn the probabilities into flags. See SPEC.md.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import tree_sitter_rust
from tree_sitter import Language, Node, Parser

# --- Jev configuration -------------------------------------------------------

API_URL = "https://openrouter.ai/api/alpha/decisions"
# Pinned on purpose: thresholds are tuned against one version. Re-calibrate
# before changing it.
MODEL = "typesafe/jev-1.13"
PROVIDER = {"zdr": True, "data_collection": "deny"}

QUESTIONS = {
    "restates": {
        "type": "noul",
        "instructions": "Does the comment only restate what the code already makes obvious?",
        "criteria": {
            "true": "A competent reader learns nothing from the comment that the code does not already show.",
            "false": "The comment adds information not evident from the code itself.",
        },
    },
    "rationale": {
        "type": "noul",
        "instructions": "Does the comment explain a non-obvious reason, constraint, invariant, or tradeoff?",
        "criteria": {
            "true": "It explains why, or a rule the code must respect, that the code alone does not reveal.",
            "false": "It describes what or how, or nothing beyond the code.",
        },
    },
    "inconsistent": {
        "type": "noul",
        "instructions": "Does the comment make a claim that the code contradicts?",
        "criteria": {
            "true": "Something the comment states about behavior, names, values, or order does not match the code.",
            "false": "Everything the comment claims is consistent with the code.",
        },
    },
    "history": {
        "type": "noul",
        "instructions": "Does the comment narrate a past change instead of describing the code as it is?",
        "criteria": {
            "true": "It refers to what the code used to do, what was changed, fixed, or replaced, or to a past session or refactor.",
            "false": "It describes the code in the present tense with no reference to its history.",
        },
    },
    "fragile": {
        "type": "noul",
        "instructions": "Does the comment describe implementation details that are likely to change whenever the code changes?",
        "criteria": {
            "true": "It restates specific steps, counts, variable names, or mechanics that a routine edit would invalidate.",
            "false": "It describes intent, contract, or reasons that survive routine edits.",
        },
    },
}
QUESTION_NAMES = list(QUESTIONS)

# --- Rules (starting points; calibrate before trusting) ----------------------

THRESHOLDS = {
    "stale_inconsistent": 0.75,
    "history": 0.80,
    "redundant_restates": 0.90,
    "redundant_max_rationale": 0.20,
    "fragile": 0.85,
    "fragile_max_rationale": 0.30,
    # Above this, only STALE may fire: a wrong "why" is still wrong.
    "rationale_veto": 0.70,
}

# --- Runtime limits ----------------------------------------------------------

MAX_IN_FLIGHT = 4
MAX_TRIES = 3
REQUEST_TIMEOUT = 30.0
RETRY_STATUSES = {429, 502, 503, 504}
# About 1,000 tokens at ~4 characters per token.
CONTEXT_BUDGET_CHARS = 4000
# The preceding statement is there for ordering context; a whole preceding
# `impl` block would crowd out the code the comment describes.
PRECEDING_MAX_CHARS = 800
MACRO_CONTEXT_LINES = 15

DEFAULT_CACHE = ".comment-lint-cache.json"
SKIP_DIRS = {"target", "node_modules"}

RUST = Language(tree_sitter_rust.language())

# --- Extraction --------------------------------------------------------------


@dataclass
class Comment:
    file: str
    start_line: int  # 1-based
    end_line: int
    kind: str  # "line" or "block"
    text: str
    trailing: bool
    item: str = ""
    code_before: str = ""
    code_after: str = ""
    # Filled in later.
    status: str = ""  # todo | skipped | commented_code | judged | cached | error
    probs: dict[str, float] = field(default_factory=dict)
    flags: list[tuple[str, float | None]] = field(default_factory=list)
    cost: float = 0.0
    error: str = ""

    def location(self) -> str:
        lines = f"{self.start_line}" if self.start_line == self.end_line else f"{self.start_line}-{self.end_line}"
        return f"{self.file}:{lines}"

    def state(self) -> dict:
        return {
            "file": self.file,
            "item": self.item,
            "comment_kind": self.kind,
            "comment": truncate(self.text, CONTEXT_BUDGET_CHARS - len(self.file)),
            "code_before": self.code_before,
            "code_after": self.code_after,
        }


def is_doc_comment(node: Node) -> bool:
    return any(c.type in ("outer_doc_comment_marker", "inner_doc_comment_marker") for c in node.children)


def is_comment(node: Node) -> bool:
    return node.type in ("line_comment", "block_comment")


def node_text(node: Node) -> str:
    return node.text.decode("utf-8", errors="replace")


def line_prefix(src: bytes, node: Node) -> str:
    line_start = src.rfind(b"\n", 0, node.start_byte) + 1
    return src[line_start : node.start_byte].decode("utf-8", errors="replace")


def collect_comments(root: Node) -> list[Node]:
    found: list[Node] = []
    stack = [root]
    while stack:
        node = stack.pop()
        if is_comment(node):
            found.append(node)
            continue
        stack.extend(reversed(node.children))
    return sorted(found, key=lambda n: n.start_byte)


def group_line_comments(nodes: list[Node], src: bytes) -> list[list[Node]]:
    """Merge consecutive standalone `//` comments into blocks."""
    groups: list[list[Node]] = []
    for node in nodes:
        standalone = line_prefix(src, node).strip() == ""
        prev = groups[-1][-1] if groups else None
        if (
            prev is not None
            and node.type == "line_comment"
            and prev.type == "line_comment"
            and standalone
            and line_prefix(src, prev).strip() == ""
            and node.start_point.row == prev.end_point.row + (0 if node_text(prev).endswith("\n") else 1)
            and node.parent == prev.parent
        ):
            groups[-1].append(node)
        else:
            groups.append([node])
    return groups


def signature(node: Node) -> str:
    body = node.child_by_field_name("body")
    end = body.start_byte if body is not None else node.end_byte
    text = node.text[: end - node.start_byte].decode("utf-8", errors="replace")
    return " ".join(text.split())


TYPE_TYPES = {"impl_item", "trait_item", "struct_item", "enum_item", "union_item", "mod_item"}


def enclosing_item(node: Node) -> str:
    fallback = ""
    cur = node.parent
    while cur is not None:
        if cur.type in ("function_item", "function_signature_item"):
            return signature(cur)
        if cur.type in TYPE_TYPES and not fallback:
            fallback = signature(cur)
        cur = cur.parent
    return fallback


def prev_code_sibling(node: Node) -> Node | None:
    sib = node.prev_named_sibling
    while sib is not None and is_comment(sib):
        sib = sib.prev_named_sibling
    return sib


def next_code_sibling(node: Node) -> Node | None:
    sib = node.next_named_sibling
    while sib is not None and is_comment(sib):
        sib = sib.next_named_sibling
    return sib


def comment_body(raw: str, kind: str) -> str:
    """Comment text with the `//` / `/* */` markers removed."""
    if kind == "block":
        inner = raw[2:-2] if raw.endswith("*/") else raw[2:]
        lines = [re.sub(r"^\s*\*(?!/)\s?", "", ln).rstrip() for ln in inner.split("\n")]
        return "\n".join(lines).strip("\n")
    out = []
    for ln in raw.split("\n"):
        ln = ln.strip()
        if ln.startswith("//"):
            ln = ln[2:]
            if ln.startswith(" "):
                ln = ln[1:]
        out.append(ln)
    return "\n".join(out)


def code_text(src: bytes, node: Node | None, end: Node | None = None) -> str:
    """Source from `node` through `end`, dedented as if it started at column 0."""
    if node is None:
        return ""
    text = src[node.start_byte : (end or node).end_byte].decode("utf-8", errors="replace")
    return textwrap.dedent(" " * node.start_point.column + text).strip()


def following_code(src: bytes, node: Node) -> str:
    """The next statement or item; an item's attributes come with it."""
    first = last = next_code_sibling(node)
    while last is not None and last.type == "attribute_item":
        nxt = next_code_sibling(last)
        if nxt is None:
            break
        last = nxt
    return code_text(src, first, last)


def is_code_line(line: str) -> bool:
    s = line.strip()
    return bool(s) and not s.startswith(("//", "/*", "*"))


def lines_after(lines: list[str], row: int) -> str:
    """Following lines up to the first blank line (a paragraph of code)."""
    out: list[str] = []
    for line in lines[row + 1 : row + 1 + MACRO_CONTEXT_LINES]:
        if not line.strip():
            break
        out.append(line)
    return textwrap.dedent("\n".join(out)).strip()


def line_before(lines: list[str], row: int) -> str:
    for line in reversed(lines[:row]):
        if is_code_line(line):
            return line.strip()
    return ""


def build_comment(path: str, group: list[Node], src: bytes) -> Comment:
    first, last = group[0], group[-1]
    trailing = line_prefix(src, first).strip() != ""
    text = "\n".join(node_text(n).rstrip("\n") for n in group)
    end_row = last.end_point.row - (1 if node_text(last).endswith("\n") else 0)
    c = Comment(
        file=path,
        start_line=first.start_point.row + 1,
        end_line=end_row + 1,
        kind="line" if first.type == "line_comment" else "block",
        text=text,
        trailing=trailing,
        item=enclosing_item(first),
    )
    if first.parent is not None and first.parent.type == "token_tree":
        # Inside a macro call tree-sitter only sees a flat run of tokens, so
        # siblings are single tokens. Use source lines instead.
        lines = src.decode("utf-8", errors="replace").split("\n")
        if trailing:
            c.code_after = line_prefix(src, first).strip()
        else:
            c.code_after = lines_after(lines, end_row)
        c.code_before = line_before(lines, first.start_point.row)
    elif trailing:
        # The comment describes the code it trails: the statement ending on
        # this line if there is one, otherwise the line itself.
        target = prev_code_sibling(first)
        if target is not None and target.end_point.row == first.start_point.row:
            c.code_after = code_text(src, target)
            c.code_before = code_text(src, prev_code_sibling(target))
        else:
            c.code_after = line_prefix(src, first).strip()
            c.code_before = code_text(src, prev_code_sibling(first))
    else:
        c.code_before = code_text(src, prev_code_sibling(first))
        c.code_after = following_code(src, last)
    c.code_before = truncate(c.code_before, PRECEDING_MAX_CHARS)
    fit_budget(c)
    return c


def truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    if max_chars <= 0:
        return ""
    cut = text[:max_chars]
    if "\n" in cut:
        cut = cut[: cut.rfind("\n")]
    return cut + "\n…"


def fit_budget(c: Comment, budget: int = CONTEXT_BUDGET_CHARS) -> None:
    """Trim context to the budget: following code first, then preceding, then the item.

    The comment text itself is kept whole here so the local checks see all of
    it; `state()` trims it for the request.
    """
    fixed = len(c.file) + len(c.item) + len(c.text)
    room = budget - fixed - len(c.code_before)
    c.code_after = truncate(c.code_after, max(room, 0))
    room = budget - fixed - len(c.code_after)
    c.code_before = truncate(c.code_before, max(room, 0))
    room = budget - len(c.file) - len(c.text) - len(c.code_before) - len(c.code_after)
    c.item = truncate(c.item, max(room, 0))


def extract(path: Path, display: str) -> list[Comment]:
    src = path.read_bytes()
    tree = Parser(RUST).parse(src)
    nodes = [n for n in collect_comments(tree.root_node) if not is_doc_comment(n)]
    return [build_comment(display, g, src) for g in group_line_comments(nodes, src)]


def find_rust_files(paths: list[str]) -> list[Path]:
    files: dict[Path, None] = {}
    for p in map(Path, paths):
        if p.is_file():
            files.setdefault(p.resolve())
            continue
        for f in sorted(p.rglob("*.rs")):
            rel = f.relative_to(p).parts[:-1]
            if any(part in SKIP_DIRS or part.startswith(".") for part in rel):
                continue
            files.setdefault(f.resolve())
    return list(files)


def display_path(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(Path.cwd().resolve()))
    except ValueError:
        return str(p)


# --- Local checks ------------------------------------------------------------

# Upper case anywhere; any case only at the start of a line ("the todo list" is prose).
TODO_RE = re.compile(r"\b(TODO|FIXME)\b|^\s*(?i:todo|fixme)\b", re.MULTILINE)
LICENSE_RE = re.compile(r"SPDX-License-Identifier|\bcopyright\b|licensed under|\blicense\b", re.IGNORECASE)
# Matched against every line: one directive line exempts the whole block.
DIRECTIVE_RE = re.compile(
    r"^\s*("
    r"(?i:safety):"  # unsafe justification; keep, never flag
    r"|rustfmt::|rustfmt-"
    r"|clippy::"
    r"|@"  # `@generated`, rustc `//@` test directives
    r"|~"  # rustc UI-test annotations (`//~ ERROR`)
    r"|ignore-tidy"
    r"|(compile-flags|edition|revisions):"
    r"|(check|build|run)-pass\b"
    r"|#?region:|#region\b|#?endregion\b"
    r"|(cspell|spell-checker|codespell):"
    r")",
    re.MULTILINE,
)

# Evidence that a clean parse is really code and not a short English phrase.
# Comparison, logic and arithmetic operators are left out: invariant notes such
# as `// len <= cap` or `// well-known` parse cleanly but are prose.
CODE_TOKENS = {";", "=", "::", "->", "=>", "{", "}", "+=", "-=", "*=", "/="}
# Stricter evidence for one paragraph of a mixed prose/code block, so that an
# example value such as `// [Ok(1), Err(Elapsed)]` doesn't count.
STATEMENT_TOKENS = {";", "{", "}"}
STATEMENT_NODES = {
    "let_declaration", "function_item", "struct_item", "enum_item", "impl_item", "trait_item",
    "use_declaration", "mod_item", "const_item", "static_item", "type_item",
}
CODE_NODES = STATEMENT_NODES | {
    "string_literal", "raw_string_literal", "char_literal", "integer_literal", "float_literal",
    "call_expression", "macro_invocation", "scoped_identifier", "scoped_type_identifier",
    "if_expression", "match_expression", "for_expression", "while_expression", "loop_expression",
    "assignment_expression", "compound_assignment_expr", "reference_expression",
    "generic_type", "reference_type", "visibility_modifier",
}
CODE_WRAPPERS = [
    ("fn _x() {\n", "\n}"),  # statements
    ("", ""),  # items
    ("impl _X {\n", "\n}"),  # methods
    ("struct _X {\n", "\n}"),  # fields
    ("fn _x() { match _x {\n", "\n} }"),  # match arms
]


def has_bad_node(node: Node) -> bool:
    if node.is_error or node.is_missing:
        return True
    return any(has_bad_node(c) for c in node.children)


def has_code_evidence(node: Node, lo: int, hi: int, nodes: set[str], tokens: set[str]) -> bool:
    if node.end_byte <= lo or node.start_byte >= hi:
        return False
    if node.start_byte >= lo and node.end_byte <= hi:
        if node.type in nodes or (not node.is_named and node.type in tokens):
            return True
    return any(has_code_evidence(c, lo, hi, nodes, tokens) for c in node.children)


# A lone number or string (`// 20`, `// "abc"`) is a label, not code.
LONE_LITERAL_RE = re.compile(r"""^\s*(-?[\d.][\w.]*|"[^"\n]*"|'.')\s*$""")


def looks_like_code(text: str, strict: bool = False) -> bool:
    if not text.strip() or LONE_LITERAL_RE.match(text):
        return False
    parser = Parser(RUST)
    for head, tail in CODE_WRAPPERS:
        src = (head + text + tail).encode()
        root = parser.parse(src).root_node
        if has_bad_node(root):
            continue
        lo, hi = len(head.encode()), len(head.encode()) + len(text.encode())
        nodes, tokens = (STATEMENT_NODES, STATEMENT_TOKENS) if strict else (CODE_NODES, CODE_TOKENS)
        if has_code_evidence(root, lo, hi, nodes, tokens):
            return True
    return False


def contains_code(body: str) -> bool:
    """True if the block, or any blank-line-separated paragraph of it, is code."""
    paragraphs = re.split(r"\n\s*\n", body)
    return looks_like_code(body) or (len(paragraphs) > 1 and any(looks_like_code(p, strict=True) for p in paragraphs))


def local_check(c: Comment) -> None:
    """Set status for comments that need no API call."""
    body = comment_body(c.text, c.kind)
    if not re.search(r"\w", body):
        c.status = "skipped"  # empty or decorative (`// -----`)
    elif DIRECTIVE_RE.search(body):
        c.status = "skipped"
    elif LICENSE_RE.search(body) and not c.item and not c.code_before:
        c.status = "skipped"  # header boilerplate: before any code in the file
    elif TODO_RE.search(body):
        c.status = "todo"
    elif contains_code(body):
        c.status = "commented_code"
        c.flags = [("COMMENTED_CODE", None)]


# --- Rules -------------------------------------------------------------------


def apply_rules(p: dict[str, float], t: dict[str, float] = THRESHOLDS) -> list[tuple[str, float]]:
    flags: list[tuple[str, float]] = []
    if p["inconsistent"] > t["stale_inconsistent"]:
        flags.append(("STALE", p["inconsistent"]))
    if p["rationale"] > t["rationale_veto"]:
        return flags
    if p["history"] > t["history"]:
        flags.append(("HISTORY", p["history"]))
    if p["restates"] > t["redundant_restates"] and p["rationale"] < t["redundant_max_rationale"]:
        flags.append(("REDUNDANT", p["restates"]))
    if p["fragile"] > t["fragile"] and p["rationale"] < t["fragile_max_rationale"]:
        flags.append(("FRAGILE", p["fragile"]))
    return flags


# --- Jev client --------------------------------------------------------------


class FatalApiError(Exception):
    pass


def request_body(c: Comment) -> dict:
    return {"model": MODEL, "provider": PROVIDER, "state": c.state(), "questions": QUESTIONS}


def cache_key(c: Comment) -> str:
    blob = json.dumps({"state": c.state(), "questions": QUESTIONS, "model": MODEL}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()


def parse_answers(data: dict) -> dict[str, float]:
    answers = data["answers"]
    return {name: float(answers[name]["noul"]) for name in QUESTION_NAMES}


async def ask_jev(client: httpx.AsyncClient, c: Comment, sem: asyncio.Semaphore, backoff: float) -> None:
    last_error = ""
    for attempt in range(MAX_TRIES):
        if attempt:
            await asyncio.sleep(backoff * 2 ** (attempt - 1))
        try:
            async with sem:
                resp = await client.post(API_URL, json=request_body(c))
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_error = f"{type(e).__name__}"
            continue
        if resp.status_code in (401, 403):
            raise FatalApiError(f"HTTP {resp.status_code}: {resp.text[:300]}")
        if resp.status_code in RETRY_STATUSES:
            last_error = f"HTTP {resp.status_code}"
            continue
        if resp.status_code != 200:
            c.status, c.error = "error", f"HTTP {resp.status_code}: {resp.text[:300]}"
            return
        try:
            data = resp.json()
            c.probs = parse_answers(data)
        except (ValueError, KeyError, TypeError) as e:
            c.status, c.error = "error", f"bad response: {e!r}"
            return
        c.cost = float((data.get("usage") or {}).get("cost") or 0.0)
        c.status = "judged"
        return
    c.status, c.error = "error", f"gave up after {MAX_TRIES} tries ({last_error})"


async def judge_all(
    comments: list[Comment],
    api_key: str,
    cache: dict,
    transport: httpx.AsyncBaseTransport | None = None,
    backoff: float = 1.0,
) -> None:
    todo = []
    for c in comments:
        hit = cache.get(cache_key(c))
        if hit is not None:
            c.probs, c.status = dict(hit), "cached"
        else:
            todo.append(c)
    if not todo:
        return
    sem = asyncio.Semaphore(MAX_IN_FLIGHT)
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(headers=headers, timeout=REQUEST_TIMEOUT, transport=transport) as client:
        await asyncio.gather(*(ask_jev(client, c, sem, backoff) for c in todo))
    for c in todo:
        if c.status == "judged":
            cache[cache_key(c)] = c.probs


def load_cache(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def save_cache(path: Path | None, cache: dict) -> None:
    if path is None:
        return
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(cache, sort_keys=True))
    tmp.replace(path)


# --- Report ------------------------------------------------------------------


def sort_key(c: Comment) -> tuple:
    return (c.file, c.start_line)


def fmt_score(s: float | None) -> str:
    return "-" if s is None else f"{s:.2f}"


def fmt_probs(p: dict[str, float]) -> str:
    return " ".join(f"{k}={p[k]:.2f}" for k in QUESTION_NAMES)


def summary(comments: list[Comment]) -> dict:
    checked = [c for c in comments if c.status in ("judged", "cached", "commented_code", "error")]
    return {
        "flagged": sum(1 for c in comments if c.flags),
        "checked": len(checked),
        "todos": sum(1 for c in comments if c.status == "todo"),
        "errors": sum(1 for c in comments if c.status == "error"),
        "cached": sum(1 for c in comments if c.status == "cached"),
        "skipped": sum(1 for c in comments if c.status == "skipped"),
        "cost": sum(c.cost for c in comments),
    }


def text_report(comments: list[Comment], show_all: bool) -> str:
    rows: list[tuple[str, str, str]] = []
    for c in sorted(comments, key=sort_key):
        for cat, score in c.flags:
            rows.append((c.location(), cat, fmt_score(score)))
        if c.status == "error":
            rows.append((c.location(), "ERROR", c.error))
        elif show_all and not c.flags and c.probs:
            rows.append((c.location(), "ok", fmt_probs(c.probs)))
        if show_all and c.flags and c.probs:
            rows.append((c.location(), "", fmt_probs(c.probs)))
    loc_w = max((len(r[0]) for r in rows), default=0) + 3
    cat_w = max((len(r[1]) for r in rows), default=0) + 3
    lines = [f"{loc:<{loc_w}}{cat:<{cat_w}}{detail}".rstrip() for loc, cat, detail in rows]

    todos = [c for c in sorted(comments, key=sort_key) if c.status == "todo"]
    if todos:
        if lines:
            lines.append("")
        tw = max(len(c.location()) for c in todos) + 3
        for c in todos:
            first = comment_body(c.text, c.kind).strip().split("\n")[0]
            lines.append(f"{c.location():<{tw}}TODO   {first[:80]}")

    s = summary(comments)
    lines.append("--")
    footer = f"{s['flagged']} flagged / {s['checked']} checked · {s['todos']} TODOs"
    if s["errors"]:
        footer += f" · {s['errors']} errors"
    footer += f" · cost ${s['cost']:.4f}"
    if s["cached"]:
        footer += f" ({s['cached']} cached)"
    lines.append(footer)
    return "\n".join(lines)


def json_record(c: Comment) -> dict:
    return {
        "file": c.file,
        "start_line": c.start_line,
        "end_line": c.end_line,
        "kind": c.kind,
        "trailing": c.trailing,
        "comment": c.text,
        "item": c.item,
        "code_before": c.code_before,
        "code_after": c.code_after,
        "status": c.status,
        "probabilities": c.probs or None,
        "flags": [{"category": cat, "score": score} for cat, score in c.flags],
        "error": c.error or None,
    }


def json_report(comments: list[Comment], show_all: bool) -> str:
    chosen = [c for c in sorted(comments, key=sort_key) if show_all or c.flags or c.status == "error"]
    return json.dumps({"records": [json_record(c) for c in chosen], "summary": summary(comments)}, indent=2)


CSV_FIELDS = ["file", "start_line", "end_line", "kind", "trailing", "comment", *QUESTION_NAMES, "flags", "label"]


def write_csv(path: Path, comments: list[Comment]) -> None:
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        w.writeheader()
        for c in sorted(comments, key=sort_key):
            if not c.probs:
                continue
            w.writerow({
                "file": c.file, "start_line": c.start_line, "end_line": c.end_line,
                "kind": c.kind, "trailing": c.trailing, "comment": c.text,
                **{k: f"{c.probs[k]:.4f}" for k in QUESTION_NAMES},
                "flags": " ".join(cat for cat, _ in c.flags), "label": "",
            })


def evaluate_csv(path: Path, t: dict[str, float] = THRESHOLDS) -> str:
    """Score the current rules against a hand-labeled CSV (no API calls)."""
    counts = {"keep": [0, 0], "cut": [0, 0], "rewrite": [0, 0]}  # [total, flagged]
    false_flags: list[str] = []
    with path.open(newline="") as f:
        for row in csv.DictReader(f):
            label = (row.get("label") or "").strip().lower()
            if label not in counts:
                continue
            flags = apply_rules({k: float(row[k]) for k in QUESTION_NAMES}, t)
            counts[label][0] += 1
            if flags:
                counts[label][1] += 1
                if label == "keep":
                    loc = f"{row['file']}:{row['start_line']}"
                    false_flags.append(f"  {loc}  {' '.join(c for c, _ in flags)}")
    bad_total = counts["cut"][0] + counts["rewrite"][0]
    bad_caught = counts["cut"][1] + counts["rewrite"][1]
    out = [f"{label:<8} {flagged:>4} flagged / {total:>4}" for label, (total, flagged) in counts.items()]
    if bad_total:
        out.append(f"recall on cut+rewrite: {bad_caught / bad_total:.0%}")
    if counts["keep"][0]:
        out.append(f"false-flag rate on keep: {counts['keep'][1] / counts['keep'][0]:.0%}")
    if false_flags:
        out.append("false flags:")
        out.extend(false_flags)
    return "\n".join(out)


# --- CLI ---------------------------------------------------------------------


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="comment-lint", description="Flag low-value or stale-prone Rust comments.")
    ap.add_argument("paths", nargs="*", default=["."], help="Rust files or directories (default: .)")
    ap.add_argument("--json", action="store_true", help="print full records for flagged comments")
    ap.add_argument("--all", action="store_true", help="include unflagged comments with their probabilities")
    ap.add_argument("--csv", metavar="PATH", help="write every judged comment with its probabilities (for labeling)")
    ap.add_argument("--evaluate", metavar="PATH", help="score the rules against a labeled CSV; no API calls")
    ap.add_argument("--dry-run", action="store_true", help="extract and run local checks only; no API calls")
    ap.add_argument("--cache", default=DEFAULT_CACHE, metavar="PATH", help=f"answer cache (default: {DEFAULT_CACHE})")
    ap.add_argument("--no-cache", action="store_true", help="neither read nor write the cache")
    return ap.parse_args(argv)


def run(argv: list[str] | None = None, transport: httpx.AsyncBaseTransport | None = None, backoff: float = 1.0) -> int:
    args = parse_args(argv)

    if args.evaluate:
        print(evaluate_csv(Path(args.evaluate)))
        return 0

    comments: list[Comment] = []
    for f in find_rust_files(args.paths):
        comments.extend(extract(f, display_path(f)))
    for c in comments:
        local_check(c)
    pending = [c for c in comments if not c.status]

    if args.dry_run:
        for c in pending:
            c.status = "pending"
        if args.json:
            print(json_report(comments, show_all=True))
        else:
            print(text_report(comments, args.all))
            print(f"{len(pending)} comments would be sent to Jev")
        return 0

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if pending and not api_key:
        print("comment-lint: OPENROUTER_API_KEY is not set", file=sys.stderr)
        return 2

    cache_path = None if args.no_cache else Path(args.cache)
    cache = load_cache(cache_path)
    try:
        asyncio.run(judge_all(pending, api_key, cache, transport=transport, backoff=backoff))
    except FatalApiError as e:
        print(f"comment-lint: {e}", file=sys.stderr)
        return 2
    finally:
        save_cache(cache_path, cache)

    for c in pending:
        if c.probs:
            c.flags = list(apply_rules(c.probs))

    if args.csv:
        write_csv(Path(args.csv), comments)
    print(json_report(comments, args.all) if args.json else text_report(comments, args.all))

    s = summary(comments)
    if s["flagged"]:
        return 1
    return 2 if s["errors"] else 0


if __name__ == "__main__":
    sys.exit(run())
