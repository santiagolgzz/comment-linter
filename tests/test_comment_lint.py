import csv
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import comment_lint as cl  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "sample.rs"


def extract_src(tmp_path: Path, src: str) -> list[cl.Comment]:
    f = tmp_path / "x.rs"
    f.write_text(src)
    return cl.extract(f, "x.rs")


def probs(**overrides: float) -> dict[str, float]:
    p = {"restates": 0.1, "rationale": 0.1, "inconsistent": 0.1, "history": 0.1, "fragile": 0.1}
    p.update(overrides)
    return p


# --- extraction --------------------------------------------------------------


def test_doc_comments_are_ignored(tmp_path):
    src = "//! crate doc\n/// item doc\n/** block doc */\n/*! inner */\n//// four slashes\n/* plain */\nfn f() {}\n"
    got = [c.text for c in extract_src(tmp_path, src)]
    assert got == ["//// four slashes", "/* plain */"]


def test_consecutive_line_comments_merge(tmp_path):
    src = "fn f() {\n    // one\n    // two\n\n    // three\n    let x = 1; // trailing\n    // four\n}\n"
    cs = extract_src(tmp_path, src)
    assert [(c.start_line, c.end_line, c.trailing) for c in cs] == [
        (2, 3, False),
        (5, 5, False),
        (6, 6, True),
        (7, 7, False),
    ]
    assert cs[0].text == "// one\n// two"


def test_context_for_standalone_comment(tmp_path):
    src = (
        "impl S {\n"
        "    fn attach(&self, id: &str) -> Result<()> {\n"
        "        let mut ids = Vec::new();\n"
        "        // collect ids\n"
        "        for c in self.kids() {\n"
        "            ids.push(c);\n"
        "        }\n"
        "    }\n"
        "}\n"
    )
    [c] = extract_src(tmp_path, src)
    assert c.item == "fn attach(&self, id: &str) -> Result<()>"
    assert c.code_before == "let mut ids = Vec::new();"
    assert c.code_after.startswith("for c in self.kids() {")
    assert c.kind == "line"


def test_context_for_trailing_comment(tmp_path):
    src = "fn f() {\n    let a = 1;\n    let n = a + 1; // add one\n    g();\n}\n"
    [c] = extract_src(tmp_path, src)
    assert c.trailing
    assert c.code_after == "let n = a + 1;"
    assert c.code_before == "let a = 1;"


def test_type_signature_used_outside_functions(tmp_path):
    src = "struct Registry {\n    // the map\n    map: u32,\n}\n"
    [c] = extract_src(tmp_path, src)
    assert c.item == "struct Registry"
    assert c.code_after == "map: u32"


def test_context_is_trimmed_following_code_first(tmp_path):
    long_body = "\n".join(f"    let v{i} = {i};" for i in range(600))
    src = f"fn f() {{\n    let a = 1;\n    // note\n    {{\n{long_body}\n    }}\n}}\n"
    [c] = extract_src(tmp_path, src)
    assert c.code_before == "let a = 1;"
    assert c.code_after.endswith("…")
    total = len(c.file) + len(c.item) + len(c.text) + len(c.code_before) + len(c.code_after)
    assert total <= cl.CONTEXT_BUDGET_CHARS + 2


def test_block_comment_body():
    assert cl.comment_body("/* a\n * b\n */", "block") == " a\nb"
    assert cl.comment_body("// a\n//   b\n//", "line") == "a\n  b\n"


# --- local checks ------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "let x = foo(1);",
        "self.write_trim(line)?;",
        "use std::io::Write;",
        "x += 1",
        "fn f() -> u32 { 1 }",
        "println!(\"{}\", x);",
        "Some(v) => v,",
    ],
)
def test_commented_code_detected(text):
    assert cl.looks_like_code(text)


@pytest.mark.parametrize(
    "text",
    [
        "safety",  # bare identifier
        "return result",  # keyword plus word
        "Loop over the children and collect their ids",
        "well-known",  # parses as subtraction
        "and/or",
        "why?",  # parses as a try expression
        "Note: important",  # parses as a struct field
        "len <= cap",
        "20",  # grid label
        "\"abc\"",
        "config.toml",
        "",
    ],
)
def test_prose_not_detected_as_code(text):
    assert not cl.looks_like_code(text)


def test_mixed_block_needs_statement_paragraph():
    assert cl.contains_code("Disabled until Sink exists.\n\npub fn consume(self) -> Self {\n    self\n}")
    assert not cl.contains_code("Items time out, so we get:\n\n[Ok(1), Err(Elapsed), Ok(2)]")


def make(text: str, kind: str = "line", **kw) -> cl.Comment:
    c = cl.Comment(file="x.rs", start_line=1, end_line=1, kind=kind, text=text, trailing=False, **kw)
    cl.local_check(c)
    return c


def test_local_check_statuses():
    assert make("// TODO: evict").status == "todo"
    assert make("// fixme later").status == "todo"
    assert make("// keeps the todo list sorted").status == ""
    assert make("// SAFETY: ptr is valid").status == "skipped"
    assert make("// Safety: ptr is valid").status == "skipped"
    assert make("// Explains stuff\n// SAFETY: ptr is valid").status == "skipped"
    assert make("// rustfmt::skip").status == "skipped"
    assert make("// ---------").status == "skipped"
    assert make("//").status == "skipped"
    assert make("// Copyright 2026 Foo. Licensed under MIT.").status == "skipped"
    # A license mention inside code is judged normally.
    assert make("// the license file is read lazily", item="fn f()").status == ""
    c = make("// let x = 1;")
    assert c.status == "commented_code"
    assert c.flags == [("COMMENTED_CODE", None)]
    assert make("// region of memory we own").status == ""
    assert make("/* now uses the new parser */", kind="block").status == ""


# --- rules -------------------------------------------------------------------


def test_rules_from_spec_table():
    assert cl.apply_rules(probs()) == []
    assert cl.apply_rules(probs(inconsistent=0.8)) == [("STALE", 0.8)]
    assert cl.apply_rules(probs(inconsistent=0.75)) == []
    assert cl.apply_rules(probs(history=0.81)) == [("HISTORY", 0.81)]
    assert cl.apply_rules(probs(restates=0.95, rationale=0.1)) == [("REDUNDANT", 0.95)]
    assert cl.apply_rules(probs(restates=0.95, rationale=0.25)) == []
    assert cl.apply_rules(probs(fragile=0.9, rationale=0.25)) == [("FRAGILE", 0.9)]
    assert cl.apply_rules(probs(fragile=0.9, rationale=0.35)) == []


def test_rationale_veto_spares_all_but_stale():
    p = probs(rationale=0.75, history=0.95, inconsistent=0.9)
    assert cl.apply_rules(p) == [("STALE", 0.9)]


def test_multiple_flags():
    p = probs(history=0.9, restates=0.95, fragile=0.9, rationale=0.05)
    assert [f for f, _ in cl.apply_rules(p)] == ["HISTORY", "REDUNDANT", "FRAGILE"]


# --- Jev client and CLI ------------------------------------------------------


def jev_response(p: dict[str, float], cost: float = 0.0001) -> httpx.Response:
    return httpx.Response(200, json={"answers": {k: {"noul": v} for k, v in p.items()}, "usage": {"cost": cost}})


class FakeJev:
    """Answers by comment text; records each request body."""

    def __init__(self, answers: dict[str, dict[str, float]], fail_first: dict[str, list[int]] | None = None):
        self.answers = answers
        self.fail_first = fail_first or {}
        self.bodies: list[dict] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        self.bodies.append(body)
        assert request.headers["authorization"] == "Bearer test-key"
        comment = body["state"]["comment"]
        pending = self.fail_first.get(comment)
        if pending:
            return httpx.Response(pending.pop(0), text="busy")
        for needle, p in self.answers.items():
            if needle in comment:
                return jev_response(probs(**p))
        return jev_response(probs())


@pytest.fixture
def api_key(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")


def run_cli(args, fake, capsys) -> tuple[int, str]:
    code = cl.run(args, transport=httpx.MockTransport(fake), backoff=0)
    return code, capsys.readouterr().out


def test_request_shape(tmp_path, api_key, capsys):
    fake = FakeJev({})
    code, _ = run_cli(["--no-cache", str(FIXTURE)], fake, capsys)
    body = fake.bodies[0]
    assert body["model"] == cl.MODEL == "typesafe/jev-1.13-20260917"
    assert body["provider"] == {"zdr": True, "data_collection": "deny"}
    assert set(body["questions"]) == {"restates", "rationale", "inconsistent", "history", "fragile"}
    assert all(q["type"] == "noul" for q in body["questions"].values())
    assert set(body["state"]) == {"file", "item", "comment_kind", "comment", "code_before", "code_after"}
    # Only comments that survive the local checks are sent.
    sent = {b["state"]["comment"] for b in fake.bodies}
    assert not any("TODO" in s or "let old" in s or "Copyright" in s for s in sent)
    assert code == 1  # the commented-out code is a flag


def test_report_and_exit_code(tmp_path, api_key, capsys):
    fake = FakeJev({"Loop over": {"restates": 0.97}, "new parser": {"history": 0.91}})
    code, out = run_cli(["--no-cache", str(FIXTURE)], fake, capsys)
    assert code == 1
    lines = out.splitlines()
    assert lines[0].split() == ["tests/fixtures/sample.rs:14-15", "REDUNDANT", "0.97"]
    assert lines[1].split() == ["tests/fixtures/sample.rs:19", "COMMENTED_CODE", "-"]
    assert lines[2].split() == ["tests/fixtures/sample.rs:25", "HISTORY", "0.91"]
    assert "TODO: evict old entries" in out
    assert lines[-1] == "3 flagged / 6 checked · 1 TODOs · cost $0.0005"


def test_clean_run_exits_zero(tmp_path, api_key, capsys):
    f = tmp_path / "ok.rs"
    f.write_text("fn f() {\n    // Must run after init() or the pool is empty.\n    g();\n}\n")
    code, out = run_cli(["--no-cache", str(f)], FakeJev({}), capsys)
    assert code == 0
    assert out.startswith("--\n0 flagged / 1 checked")


def test_retry_then_success(tmp_path, api_key, capsys):
    f = tmp_path / "a.rs"
    f.write_text("fn f() {\n    // note one\n    g();\n}\n")
    fake = FakeJev({}, fail_first={"// note one": [429, 502]})
    code, out = run_cli(["--no-cache", str(f)], fake, capsys)
    assert code == 0
    assert len(fake.bodies) == 3


def test_gives_up_after_three_tries(tmp_path, api_key, capsys):
    f = tmp_path / "a.rs"
    f.write_text("fn f() {\n    // note one\n    g();\n}\n")
    fake = FakeJev({}, fail_first={"// note one": [429, 429, 429, 429]})
    code, out = run_cli(["--no-cache", str(f)], fake, capsys)
    assert len(fake.bodies) == 3
    assert "ERROR" in out and "gave up after 3 tries (HTTP 429)" in out
    assert code == 2


def test_timeout_is_retried(tmp_path, api_key, capsys):
    f = tmp_path / "a.rs"
    f.write_text("fn f() {\n    // note one\n    g();\n}\n")
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return jev_response(probs())

    code, _ = run_cli(["--no-cache", str(f)], handler, capsys)
    assert code == 0 and len(calls) == 2


def or_error(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": status, "message": message}})


@pytest.mark.parametrize(
    "status, message, hint",
    [
        (401, "No auth credentials found", "API key was rejected"),
        (402, "Insufficient credits", "out of credits"),
        (404, "No allowed providers are available for the selected model.", "privacy settings"),
        (503, "There is no available model provider that meets your routing requirements", "privacy settings"),
    ],
)
def test_fatal_errors_stop_the_run(tmp_path, api_key, capsys, status, message, hint):
    f = tmp_path / "a.rs"
    f.write_text("fn f() {\n    // one\n    g();\n    // two\n    h();\n}\n")
    calls = []

    def handler(request):
        calls.append(1)
        return or_error(status, message)

    code = cl.run(["--no-cache", str(f)], transport=httpx.MockTransport(handler), backoff=0)
    err = capsys.readouterr().err
    assert code == 2
    assert f"HTTP {status}: {message}" in err and hint in err
    assert len(calls) <= cl.MAX_IN_FLIGHT  # stopped, not retried per comment


def test_forbidden_fails_only_that_comment(tmp_path, api_key, capsys):
    f = tmp_path / "a.rs"
    f.write_text("fn f() {\n    // one\n    g();\n    // two\n    h();\n}\n")

    def handler(request):
        if "one" in json.loads(request.content)["state"]["comment"]:
            return or_error(403, "Flagged by moderation")
        return jev_response(probs())

    code, out = run_cli(["--no-cache", str(f)], handler, capsys)
    assert code == 2
    assert "ERROR" in out and "HTTP 403: Flagged by moderation" in out
    assert "0 flagged / 2 checked" in out


def test_retry_after_is_read_and_capped():
    assert cl.retry_after(httpx.Response(429, headers={"Retry-After": "2"})) == 2.0
    assert cl.retry_after(httpx.Response(429, headers={"Retry-After": "9999"})) == cl.MAX_RETRY_AFTER
    assert cl.retry_after(httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})) == 0.0
    assert cl.retry_after(httpx.Response(429)) == 0.0


def test_timeout_status_is_retried(tmp_path, api_key, capsys):
    f = tmp_path / "a.rs"
    f.write_text("fn f() {\n    // note one\n    g();\n}\n")
    fake = FakeJev({}, fail_first={"// note one": [408]})
    code, _ = run_cli(["--no-cache", str(f)], fake, capsys)
    assert code == 0 and len(fake.bodies) == 2


def test_missing_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert cl.run(["--no-cache", str(FIXTURE)]) == 2
    assert "OPENROUTER_API_KEY" in capsys.readouterr().err


def test_cache_skips_unchanged_comments(tmp_path, api_key, capsys):
    f = tmp_path / "a.rs"
    f.write_text("fn f() {\n    // note one\n    g();\n    // note two\n    h();\n}\n")
    cache = tmp_path / "cache.json"
    fake = FakeJev({"note two": {"history": 0.9}})
    code, _ = run_cli(["--cache", str(cache), str(f)], fake, capsys)
    assert len(fake.bodies) == 2 and code == 1

    # Change the code under one comment: only that comment is re-sent.
    f.write_text("fn f() {\n    // note one\n    g();\n    // note two\n    h2();\n}\n")
    fake2 = FakeJev({"note two": {"history": 0.9}})
    code, out = run_cli(["--cache", str(cache), str(f)], fake2, capsys)
    assert [b["state"]["comment"] for b in fake2.bodies] == ["// note two"]
    assert code == 1  # cached answers still produce flags
    assert "(1 cached)" in out


def test_json_output(tmp_path, api_key, capsys):
    fake = FakeJev({"Loop over": {"restates": 0.97}})
    _, out = run_cli(["--no-cache", "--json", str(FIXTURE)], fake, capsys)
    data = json.loads(out)
    assert [r["start_line"] for r in data["records"]] == [14, 19]
    r = data["records"][0]
    assert r["flags"] == [{"category": "REDUNDANT", "score": 0.97}]
    assert set(r["probabilities"]) == set(cl.QUESTION_NAMES)
    assert r["code_after"].startswith("for child in")
    assert data["summary"]["flagged"] == 2

    _, out = run_cli(["--no-cache", "--json", "--all", str(FIXTURE)], fake, capsys)
    assert len(json.loads(out)["records"]) > 2


def test_all_shows_unflagged(tmp_path, api_key, capsys):
    _, out = run_cli(["--no-cache", "--all", str(FIXTURE)], FakeJev({}), capsys)
    assert "ok" in out and "restates=0.10" in out


def test_csv_and_evaluate(tmp_path, api_key, capsys):
    fake = FakeJev({"Loop over": {"restates": 0.97}, "count the ids": {"restates": 0.95}})
    out_csv = tmp_path / "dump.csv"
    run_cli(["--no-cache", "--csv", str(out_csv), str(FIXTURE)], fake, capsys)
    assert out_csv.read_text().splitlines()[0].endswith(",flags,label")
    recs = list(csv.DictReader(out_csv.open()))
    assert len(recs) == 5  # every Jev-judged comment, none of the local ones

    # Label: the loop comment is a cut, the rest keeps.
    for rec in recs:
        rec["label"] = "cut" if "Loop over" in rec["comment"] else "keep"
    with out_csv.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cl.CSV_FIELDS)
        w.writeheader()
        w.writerows(recs)
    code = cl.run(["--evaluate", str(out_csv)])
    report = capsys.readouterr().out
    assert code == 0
    assert "recall on cut+rewrite: 100%" in report
    assert "false-flag rate on keep: 25%" in report
    assert "sample.rs:20  REDUNDANT" in report


def test_dry_run_needs_no_key(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert cl.run(["--dry-run", str(FIXTURE)]) == 0
    assert "5 comments would be sent to Jev" in capsys.readouterr().out


def test_at_most_four_requests_in_flight(tmp_path, api_key, capsys):
    import asyncio

    body = "\n".join(f"    // note {i}\n    g{i}();" for i in range(12))
    f = tmp_path / "a.rs"
    f.write_text(f"fn f() {{\n{body}\n}}\n")
    live, peak = 0, 0

    async def handler(request):
        nonlocal live, peak
        live += 1
        peak = max(peak, live)
        await asyncio.sleep(0.01)
        live -= 1
        return jev_response(probs())

    code, _ = run_cli(["--no-cache", str(f)], handler, capsys)
    assert code == 0
    assert peak == cl.MAX_IN_FLIGHT


def test_multiline_context_is_dedented(tmp_path):
    src = "fn f() {\n    // loop\n    for x in y {\n        g(x);\n    }\n}\n"
    [c] = extract_src(tmp_path, src)
    assert c.code_after == "for x in y {\n    g(x);\n}"


def test_comment_inside_macro_uses_lines(tmp_path):
    src = (
        "rgtest!(name, |dir: Dir| {\n"
        "    dir.create(\"a\");\n"
        "    // Set up the ignore file.\n"
        "    dir.create(\".gitignore\", \"foo\");\n"
        "    dir.create(\"b\");\n"
        "\n"
        "    cmd.run();\n"
        "});\n"
    )
    [c] = extract_src(tmp_path, src)
    assert c.code_before == 'dir.create("a");'
    assert c.code_after == 'dir.create(".gitignore", "foo");\ndir.create("b");'


def test_long_preceding_item_is_capped(tmp_path):
    body = "\n".join(f"    fn m{i}(&self) {{}}" for i in range(100))
    src = f"impl A {{\n{body}\n}}\n\n// Defers to B.\nimpl B {{}}\n"
    [c] = extract_src(tmp_path, src)
    assert len(c.code_before) <= cl.PRECEDING_MAX_CHARS + 2
    assert c.code_before.startswith("impl A {")
    assert c.code_after == "impl B {}"


def test_attributes_come_with_following_item(tmp_path):
    src = "// Linux doubles the size.\n#[cfg(target_os = \"linux\")]\n#[test]\nfn size() {}\n"
    [c] = extract_src(tmp_path, src)
    assert c.code_after == '#[cfg(target_os = "linux")]\n#[test]\nfn size() {}'


def test_large_commented_out_function_is_detected(tmp_path):
    lines = "\n".join(f"    // let v{i} = compute({i});" for i in range(300))
    src = f"fn f() {{\n    // fn old() {{\n{lines}\n    // }}\n    g();\n}}\n"
    [c] = extract_src(tmp_path, src)
    cl.local_check(c)
    assert c.status == "commented_code"
    assert len(c.state()["comment"]) <= cl.CONTEXT_BUDGET_CHARS


def test_overlapping_paths_are_read_once(tmp_path):
    f = tmp_path / "a.rs"
    f.write_text("fn f() {}\n")
    assert cl.find_rust_files([str(tmp_path), str(f)]) == [f.resolve()]


def test_cost_falls_back_to_token_count(tmp_path, api_key, capsys):
    f = tmp_path / "a.rs"
    f.write_text("fn f() {\n    // note\n    g();\n}\n")
    answers = {k: {"type": "noul", "noul": 0.1} for k in cl.QUESTION_NAMES}

    def handler(request):
        return httpx.Response(200, json={"answers": answers, "usage": {"input_tokens": 1_000_000, "output_tokens": 0}})

    code, out = run_cli(["--no-cache", str(f)], handler, capsys)
    assert code == 0
    assert out.splitlines()[-1].endswith("cost ~$0.0420")


@pytest.mark.parametrize("bad", [1.5, -0.1, "0.5", None, True])
def test_out_of_range_probability_is_an_error(tmp_path, api_key, capsys, bad):
    f = tmp_path / "a.rs"
    f.write_text("fn f() {\n    // note\n    g();\n}\n")
    answers = {k: {"noul": 0.1} for k in cl.QUESTION_NAMES}
    answers["history"] = {"noul": bad}
    code, out = run_cli(["--no-cache", str(f)], lambda r: httpx.Response(200, json={"answers": answers}), capsys)
    assert code == 2
    assert "ERROR" in out and "history.noul" in out


def test_probe_prints_raw_and_decoded(api_key, capsys):
    code, out = run_cli(["--probe"], FakeJev({"Loop over": {"restates": 0.97}}), capsys)
    assert code == 0
    assert f'"model": "{cl.MODEL}"' in out
    assert "== response: HTTP 200 ==" in out
    assert "restates=0.97" in out
    assert "flags: REDUNDANT 0.97" in out
    assert "from usage.cost" in out


def test_probe_reports_shape_mismatch(api_key, capsys):
    wrong = {"answers": {k: {"probability": 0.5} for k in cl.QUESTION_NAMES}}
    code, out = run_cli(["--probe"], lambda r: httpx.Response(200, json=wrong), capsys)
    assert code == 2
    assert "could not decode answers" in out


def test_demo_matches_readme(monkeypatch, capsys):
    # README.md quotes this output; update both together.
    monkeypatch.chdir(Path(__file__).resolve().parent.parent)
    assert cl.run(["--dry-run", "examples/demo.rs"]) == 0
    assert capsys.readouterr().out == (
        "examples/demo.rs:28   COMMENTED_CODE   -\n"
        "\n"
        "examples/demo.rs:33   TODO   TODO: cache this if inventories get large\n"
        "--\n"
        "1 flagged / 1 checked · 1 TODOs · cost $0.0000\n"
        "5 comments would be sent to Jev\n"
    )


def test_other_comments_are_left_out_of_context(tmp_path):
    src = (
        "fn f() {\n"
        "    let a = 1; // why a is 1\n"
        "    // the comment under test\n"
        "    if a > 0 {\n"
        "        // Saturate: callers expect it.\n"
        "        g(a); /* inline */ h();\n"
        "        /// doc on a nested item\n"
        "        fn inner() {}\n"
        "    }\n"
        "}\n"
    )
    c = next(c for c in extract_src(tmp_path, src) if "under test" in c.text)
    assert c.code_before == "let a = 1;"
    assert c.code_after == "if a > 0 {\n    g(a);  h();\n    fn inner() {}\n}"


def test_inline_comment_in_arguments_gets_its_line(tmp_path):
    src = (
        "fn f() {\n"
        "    let parent = p();\n"
        "    match self.matched(parent, /* is_dir */ true) {\n"
        "        _ => (),\n"
        "    }\n"
        "}\n"
    )
    [c] = extract_src(tmp_path, src)
    assert c.trailing
    assert c.code_after == "match self.matched(parent, true) {"
    assert c.code_before == "let parent = p();"


def test_trailing_comment_on_match_arm(tmp_path):
    src = "fn f() {\n    match m {\n        None => (), // walk up\n        a => return a,\n    }\n}\n"
    [c] = extract_src(tmp_path, src)
    assert c.code_after == "None => (),"
