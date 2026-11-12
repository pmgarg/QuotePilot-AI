"""Stage 1 fix round 11 - implementer regression tests (SEC7-1/SEC6-2 worker matching, SEC7-2/3/5/6 parser, SEC7-4 seed,
SEC7-7 limiter)."""

from __future__ import annotations

import os
import random
import subprocess
import sys
import threading
import time
import uuid
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core.config import get_settings
from app.core.errors import RateLimited
from app.fixtures import seed as seed_module
from app.services import matching
from app.services.matching import CatalogIndex
from app.services.money import parse_decimal
from app.services.ratelimit import ActionRateLimiter

WORDS = ["hex", "bolt", "steel", "zinc", "washer", "m8", "m10", "plated", "size", "nut", "screw", "flange", "brass"]


def _products(n: int, desc_len: int = 60, seed: int = 3) -> list[Any]:
    rnd = random.Random(seed)
    return [
        SimpleNamespace(
            id=uuid.uuid4(),
            sku=f"P-{i}",
            sku_normalized=f"P-{i}",
            description=(" ".join(rnd.choice(WORDS) + str(rnd.randint(0, 99)) for _ in range(40)))[:desc_len],
        )
        for i in range(n)
    ]


def _queries(n: int, seed: int = 5) -> list[str]:
    rnd = random.Random(seed)
    return [" ".join(rnd.choice(WORDS) + str(rnd.randint(0, 99)) for _ in range(rnd.randint(2, 12))) for _ in range(n)]


def _key(o: Any) -> Any:
    return (
        o.status,
        o.fuzzy_skipped,
        [(c.product.id, str(c.score), c.category, c.reason, c.matched_on) for c in o.candidates],
    )


def _my_children() -> list[int]:
    """PIDs of live child processes of this test process that run the match worker (Linux /proc)."""
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            cmd = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if stat[0] != "Z" and int(stat[1]) == os.getpid() and b"app.services.match_worker" in cmd:
            out.append(int(entry.name))
    return out


# ------------------------------------------------------------------------------------------ matching: worker path
def test_worker_and_in_process_paths_return_identical_hits_and_outcomes(monkeypatch: pytest.MonkeyPatch) -> None:
    products = _products(400)
    queries = _queries(40) + ["hex bolt size 77 steel", "zzzz qqqq"]
    lines = [(None, q, q) for q in queries]
    in_proc = CatalogIndex(products)
    in_proc.prefetch(lines)
    expected = [_key(in_proc.match(sku_hint=None, description=q, raw_text=q)) for q in queries]
    direct = matching.fuzzy_batch(in_proc.descriptions, [matching.CatalogIndex._query(q, q) for q in queries])

    monkeypatch.setattr(matching, "IN_PROCESS_MAX_COMPARISONS", 0)  # force the worker process
    worker = CatalogIndex(products)
    worker.prefetch(lines)
    got = [_key(worker.match(sku_hint=None, description=q, raw_text=q)) for q in queries]
    assert got == expected
    assert any(k[0] != "unmatched" for k in got)  # the comparison is not vacuous
    assert matching.fuzzy_batch(worker.descriptions, [matching.CatalogIndex._query(q, q) for q in queries]) == direct
    # the single-line path (no prefetch: add_item, re-match) goes through the worker as well and agrees
    single = CatalogIndex(products)
    assert [_key(single.match(sku_hint=None, description=q, raw_text=q)) for q in queries[:5]] == expected[:5]
    assert not _my_children()


def test_small_jobs_stay_in_process(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("a worker process was started for a small job")

    monkeypatch.setattr(matching, "_fuzzy_in_worker", boom)
    index = CatalogIndex(_products(200))
    index.prefetch([(None, "hex bolt size 12", "")] * 5)
    assert index.match(sku_hint=None, description="hex bolt size 12", raw_text="").status in (
        "likely",
        "ambiguous",
        "unmatched",
    )
    assert matching.IN_PROCESS_MAX_COMPARISONS == 50_000 and matching.MAX_CONCURRENT_MATCHERS == 2


def test_batch_that_cannot_finish_returns_at_the_deadline_with_the_answered_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings()
    monkeypatch.setattr(settings, "match_timeout_seconds", 4.0)
    monkeypatch.setattr(matching, "IN_PROCESS_MAX_COMPARISONS", 0)
    products = _products(20_000, desc_len=200)
    index = CatalogIndex(products)
    queries = [f"hex bolt size {i} steel zinc plated washer m8 flange {i * 7}" for i in range(500)]
    started = time.perf_counter()
    index.prefetch([(None, q, "") for q in queries])
    elapsed = time.perf_counter() - started
    assert elapsed < 4.0 + 3.0, elapsed  # deadline plus start-up and teardown; measured about 4.3 s
    answered = len(index._hits)
    assert 1 <= answered < len(queries), answered
    outcomes = [index.match(sku_hint=None, description=q, raw_text="") for q in queries]
    assert [o.fuzzy_skipped for o in outcomes] == [False] * answered + [True] * (len(queries) - answered)
    assert all(o.status == "unmatched" and not o.candidates for o in outcomes[answered:])
    assert not any(o.fuzzy_skipped for o in outcomes[:answered])
    time.sleep(0.2)
    assert not _my_children(), "a matcher process was left running"


def test_all_matcher_slots_busy_means_not_matched_not_waiting_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "match_timeout_seconds", 0.5)
    monkeypatch.setattr(matching, "IN_PROCESS_MAX_COMPARISONS", 0)
    for _ in range(matching.MAX_CONCURRENT_MATCHERS):
        assert matching._matcher_slots.acquire(timeout=1)
    try:
        started = time.perf_counter()
        index = CatalogIndex(_products(50))
        index.prefetch([(None, "hex bolt size 12", "")])
        assert time.perf_counter() - started < 5.0  # measured about 0.5 s
        o = index.match(sku_hint=None, description="hex bolt size 12", raw_text="")
        assert o.fuzzy_skipped and o.status == "unmatched" and not o.candidates
    finally:
        for _ in range(matching.MAX_CONCURRENT_MATCHERS):
            matching._matcher_slots.release()
    # the slots are free again afterwards
    assert matching._matcher_slots.acquire(timeout=1)
    matching._matcher_slots.release()


def test_at_most_two_matcher_processes_run_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(get_settings(), "match_timeout_seconds", 3.0)
    monkeypatch.setattr(matching, "IN_PROCESS_MAX_COMPARISONS", 0)
    descriptions = [p.description for p in _products(20_000, desc_len=200)]
    queries = [f"hex bolt size {i} steel zinc plated" for i in range(300)]
    peak = 0
    stop = threading.Event()

    def watch() -> None:
        nonlocal peak
        while not stop.is_set():
            peak = max(peak, len(_my_children()))
            time.sleep(0.02)

    results: list[dict[int, Any]] = []
    threads = [
        threading.Thread(target=lambda: results.append(matching.fuzzy_batch(descriptions, queries))) for _ in range(4)
    ]
    watcher = threading.Thread(target=watch)
    watcher.start()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stop.set()
    watcher.join()
    assert 1 <= peak <= matching.MAX_CONCURRENT_MATCHERS, peak
    assert len(results) == 4
    assert not _my_children()


def test_a_worker_that_cannot_start_or_returns_garbage_leaves_lines_unmatched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(matching, "IN_PROCESS_MAX_COMPARISONS", 0)

    def cannot_start(*a: object, **k: object) -> None:
        raise OSError("no fork")

    monkeypatch.setattr(matching.subprocess, "run", cannot_start)
    index = CatalogIndex(_products(30))
    index.prefetch([(None, "hex bolt size 12", "")])
    assert index.match(sku_hint=None, description="hex bolt size 12", raw_text="").fuzzy_skipped

    def garbage(cmd: Any, **k: Any) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(cmd, 1, stdout=b'{"i": 0, "hits": [[91.0, 3]]}\n{"i": 1, "hi', stderr=b"")

    monkeypatch.setattr(matching.subprocess, "run", garbage)
    index = CatalogIndex(_products(30))
    qs = ["hex bolt size 12", "nut m8", "washer 5"]
    index.prefetch([(None, q, "") for q in qs])
    o = [index.match(sku_hint=None, description=q, raw_text="") for q in qs]
    assert o[0].candidates and not o[0].fuzzy_skipped  # the complete line is used
    assert o[1].fuzzy_skipped and o[2].fuzzy_skipped  # the torn line and what follows are not


def test_worker_output_parser_keeps_only_a_clean_prefix() -> None:
    parse = matching._parse_worker_lines
    good = b'{"i": 0, "hits": [[90.5, 1]]}\n{"i": 1, "hits": []}\n'
    assert parse(good, 3, 5) == {0: [(90.5, 1)], 1: []}
    assert parse(good + b'{"i": 2, "hits"', 3, 5) == {0: [(90.5, 1)], 1: []}  # torn line
    assert parse(b'{"i": 1, "hits": []}\n', 3, 5) == {}  # out of order
    assert parse(b'{"i": 0, "hits": [[90.5, 9]]}\n', 3, 5) == {}  # index outside the catalog
    assert parse(b"not json\n" + good, 3, 5) == {}
    assert parse(b"\xff\xfe\n", 3, 5) == {} and parse(None, 3, 5) == {} and parse(b"", 3, 5) == {}
    assert parse(good + b'{"i": 5, "hits": []}\n', 3, 5) == {0: [(90.5, 1)], 1: []}


def test_worker_module_rejects_bad_input_without_output() -> None:
    from app.services import match_worker

    r = subprocess.run(
        [sys.executable, "-I", "-m", "app.services.match_worker"],
        input=b"{not json",
        capture_output=True,
        cwd=str(Path(match_worker.__file__).resolve().parents[2]),
        timeout=30,
        check=False,
    )
    assert r.returncode == 2 and r.stdout == b""


def test_catalog_descriptions_are_capped_before_matching() -> None:
    huge = SimpleNamespace(id=uuid.uuid4(), sku="HUGE-1", sku_normalized="HUGE-1", description="hex bolt " * 200_000)
    started = time.perf_counter()
    index = CatalogIndex([huge])  # type: ignore[list-item]
    assert time.perf_counter() - started < 0.5
    assert len(index.descriptions[0]) <= matching.MAX_DESCRIPTION_CHARS + 1 + len("huge-1")
    assert index.descriptions[0].endswith("huge-1")  # the SKU is still part of the text
    short = CatalogIndex([SimpleNamespace(id=uuid.uuid4(), sku="S-1", sku_normalized="S-1", description="Hex  Bolt")])  # type: ignore[list-item]
    assert short.descriptions == ["hex bolt s-1"]


def test_prefetch_applies_the_budget_in_line_order_and_counts_duplicates(monkeypatch: pytest.MonkeyPatch) -> None:
    products = _products(100)
    monkeypatch.setattr(matching, "MAX_FUZZY_COMPARISONS", 1_000)  # ten fuzzy lines
    calls: list[list[str]] = []
    real = matching.fuzzy_batch

    def spy(descriptions: list[str], queries: list[str]) -> Any:
        calls.append(list(queries))
        return real(descriptions, queries)

    monkeypatch.setattr(matching, "fuzzy_batch", spy)
    index = CatalogIndex(products)
    lines: list[tuple[str | None, str, str]] = [(None, f"hex bolt size {i} steel", "") for i in range(8)]
    lines.insert(2, ("P-7", "whatever", ""))  # exact: free
    lines += [(None, "hex bolt size 0 steel", "")] * 3  # duplicate query text still costs budget per line
    lines += [(None, f"nut m10 {i}", "") for i in range(5)]
    index.prefetch(lines)
    assert len(calls) == 1 and len(calls[0]) == 8  # one batch; duplicates sent once; lines past the budget not sent
    outcomes = [index.match(sku_hint=s, description=d, raw_text=r) for s, d, r in lines]
    fuzzy = [o for o, ln in zip(outcomes, lines, strict=True) if ln[0] is None]
    assert [o.fuzzy_skipped for o in fuzzy] == [False] * 10 + [True] * 6
    assert outcomes[2].status == "exact" and not outcomes[2].fuzzy_skipped
    assert len(calls) == 1  # match() did not start further batches


def test_a_line_that_was_not_prefetched_is_matched_on_its_own() -> None:
    index = CatalogIndex(_products(100))
    index.prefetch([(None, "hex bolt size 12", "")])
    o = index.match(sku_hint=None, description="nut m10 steel", raw_text="")  # not part of the prefetch
    assert not o.fuzzy_skipped


# ----------------------------------------------------------------------------------------------- parser
@pytest.mark.parametrize("raw", ["1234,567", "12345,678", "5550,000", "1234,567,890", "5005,505", "1234,567,890,123"])
def test_sec7_2_four_digit_first_group_is_never_thousands(raw: str) -> None:
    d = parse_decimal(raw)
    assert d is None or d < 100_000, (raw, d)
    if "," in raw[raw.index(",") + 1 :]:
        assert d is None  # multi-group forms with a 4+-digit first group are rejected
    else:
        assert d == D(raw.replace(",", "."))  # a two-part cell reads as a decimal number


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("1,234", D(1234)), ("12,345", D(12345)), ("123,456", D(123456)), ("1,234,567", D(1234567)), ("12,5", D("12.5")),
     ("1234,5", D("1234.5")), ("1234,56", D("1234.56")), ("0,500", D("0.500")), ("1,234.56", D("1234.56"))],
)  # fmt: skip
def test_sec7_2_well_formed_comma_forms_are_unchanged(raw: str, expected: D) -> None:
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", ["0 500", "0 005", "00 500", "05 050", "0'500", "0 500", "0 500,5"])
def test_sec7_3_zero_led_space_groups_are_rejected(raw: str) -> None:
    assert parse_decimal(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1 000", D(1000)),
        ("12 345", D(12345)),
        ("1'234'567", D(1234567)),
        ("1 234,56", D("1234.56")),
        ("10 500", D(10500)),
    ],
)
def test_sec7_3_ordinary_space_groups_still_work(raw: str, expected: D) -> None:
    assert parse_decimal(raw) == expected


@pytest.mark.parametrize("raw", [". 5", "..-5", "-. 5,500", ". . 5", ".  5", "- . 5"])
def test_sec7_6_dots_and_blanks_only_head_is_rejected(raw: str) -> None:
    assert parse_decimal(raw) is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (".5", D("0.5")),
        ("$.50", D("0.50")),
        ("Rs. 500", D(500)),
        ("-.5", D("-0.5")),
        ("AED 5", D(5)),
        ("12.", D(12)),
        ("-5", D(-5)),
    ],
)
def test_sec7_6_legitimate_heads_keep_working(raw: str, expected: D) -> None:
    assert parse_decimal(raw) == expected


def test_sec7_5_cost_does_not_depend_on_how_many_expanding_characters_a_cell_has() -> None:
    cell = "ﷺ" * 500
    started = time.perf_counter()
    for _ in range(20):
        assert parse_decimal(cell) is None
    assert (time.perf_counter() - started) / 20 < 0.0005
    # a megabyte cell is refused before any per-character work; a cell just over the cap likewise
    started = time.perf_counter()
    for _ in range(5):
        assert parse_decimal("ﷺ" * 1_000_000) is None
        assert parse_decimal(" " * 200 + "ﷺ" * 129) is None
    assert time.perf_counter() - started < 0.5
    # surrounding whitespace is not counted against the cap
    assert parse_decimal("  12.50  ") == D("12.50")


# ------------------------------------------------------------------------------------------------- seed
def test_seed_env_reader_reads_only_demo_password_and_tolerates_bad_files(tmp_path: Path) -> None:
    read = seed_module._read_demo_password
    f = tmp_path / ".env"
    f.write_text("SECRET_KEY=short\nDEMO_PASSWORD=abc def ghi jkl\nDATABASE_URL=nope\n")
    assert read(f) == "abc def ghi jkl"
    for content, expected in [
        ('DEMO_PASSWORD="quoted value 123"\n', "quoted value 123"),
        ("DEMO_PASSWORD='single quoted 1'\n", "single quoted 1"),
        ("export DEMO_PASSWORD=exported-value-1\n", "exported-value-1"),
        ("DEMO_PASSWORD = spaced-value-123\n", "spaced-value-123"),
        ("DEMO_PASSWORD=plain-value-12 # comment\n", "plain-value-12"),
        ("DEMO_PASSWORD=first-value-123\nDEMO_PASSWORD=second-value-12\n", "second-value-12"),
        ("# DEMO_PASSWORD=commented-out-1\n", None),
        ("DEMO_PASSWORD=\n", None),
        ('DEMO_PASSWORD=""\n', None),
        ("NOT_A_LINE\n\x00\x01 junk\n", None),
        ("﻿DEMO_PASSWORD=bom-value-12345\n", "bom-value-12345"),
    ]:
        f.write_text(content, encoding="utf-8")
        assert read(f) == expected, content
    f.write_bytes(b"\xff\xfe\x00bad\n")
    assert read(f) is None  # undecodable: absent
    assert read(tmp_path / "missing.env") is None
    assert read(tmp_path) is None  # a directory
    dangling = tmp_path / "dangling.env"
    dangling.symlink_to(tmp_path / "nowhere")
    assert read(dangling) is None


def test_seed_password_precedence_environment_then_cwd_then_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    root = tmp_path / "root.env"
    monkeypatch.chdir(cwd)
    monkeypatch.setattr(seed_module, "_REPO_ROOT_ENV", root)
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    assert seed_module._password() is None
    root.write_text("DEMO_PASSWORD=root-password-12\n")
    assert seed_module._password() == "root-password-12"
    (cwd / ".env").write_text("DEMO_PASSWORD=cwd-password-123\n")
    assert seed_module._password() == "cwd-password-123"
    monkeypatch.setenv("DEMO_PASSWORD", "env-password-123")
    assert seed_module._password() == "env-password-123"
    monkeypatch.setenv("DEMO_PASSWORD", "")  # set but empty: does not mask the files
    assert seed_module._password() == "cwd-password-123"
    (cwd / ".env").write_bytes(b"\xff\xfe")  # an undecodable cwd file counts as absent
    assert seed_module._password() == "root-password-12"


# ----------------------------------------------------------------------------------------------- limiter
def test_action_limiter_sliding_window(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("app.services.ratelimit.time.monotonic", lambda: clock[0])
    lim = ActionRateLimiter()
    u1, u2 = uuid.uuid4(), uuid.uuid4()
    for _ in range(3):
        lim.acquire("a", u1, attempts=3, window_seconds=60)
        clock[0] += 1
    with pytest.raises(RateLimited) as exc:
        lim.acquire("a", u1, attempts=3, window_seconds=60)
    assert exc.value.status_code == 429 and exc.value.extra["retry_after"] == 57
    lim.acquire("b", u1, attempts=3, window_seconds=60)  # another action
    lim.acquire("a", u2, attempts=3, window_seconds=60)  # another user
    with pytest.raises(RateLimited):
        lim.acquire("a", u1, attempts=3, window_seconds=60)  # a refused call is not counted: still 57 s to go
    clock[0] += 58
    lim.acquire("a", u1, attempts=3, window_seconds=60)  # the first hit has left the window
    lim.reset()
    for _ in range(3):
        lim.acquire("a", u1, attempts=3, window_seconds=60)


def test_production_defaults_of_the_expensive_action_limits() -> None:
    from app.core.config import Settings

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    fresh = Settings.model_fields
    assert fresh["rfq_parse_rate_limit_attempts"].default == 30
    assert fresh["catalog_import_rate_limit_attempts"].default == 10
    assert fresh["expensive_rate_limit_window_seconds"].default == 300
    assert fresh["match_timeout_seconds"].default == 8.0
    assert s.match_timeout_seconds > 0
