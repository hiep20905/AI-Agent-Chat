"""Run the AI Agent benchmark against the running Superset and score each answer against its reference SQL.

Usage: uv run scripts/benchmark.py [--only B01,B12] [--workers 3] [--taxonomy]

Reads extensions/ai-agents/benchmark/questions.jsonl. For each case it computes the gold answer by running the case's
reference SQL on the warehouse, with its dates filled in from today in Vietnam's time (Airflow reloads the data every
hour, so a stored figure would go stale). It then asks the question through the chat API as the admin, in a new
conversation that first asks the case's `history`, and keeps every streamed event. It scores the classification, the
sub-questions' intents, the metrics used, where the figure came from (an approved metric or the base tables) and the
figure itself, then writes trajectories.jsonl, results.json and report.md under
extensions/ai-agents/benchmark/runs/<time>/ (docs/specs/benchmark.md).

With --taxonomy it measures the classifier alone instead: each labelled question of
extensions/ai-agents/backend/tests/fixtures/question_taxonomy.jsonl, with its history, goes to the admin-only
/chat/classify (one model call, nothing logged), and the labels are scored against the thresholds of
docs/specs/taxonomy-cau-hoi.md §11.
"""

import argparse
import http.cookiejar
import json
import re
import subprocess
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from typing import Any

from _common import ROOT, SUPERSET_URL, fail, resolve_command, step, wait_for_health

BENCHMARK_DIR = ROOT / "extensions" / "ai-agents" / "benchmark"
CHAT_STREAM = "/extensions/text-to-bi/superset-ai-agent/chat/stream"
CLASSIFY = "/extensions/text-to-bi/superset-ai-agent/chat/classify"
TAXONOMY_SAMPLE = ROOT / "extensions" / "ai-agents" / "backend" / "tests" / "fixtures" / "question_taxonomy.jsonl"
# The acceptance thresholds of docs/specs/taxonomy-cau-hoi.md §11: (share of questions, at least or at most).
TAXONOMY_THRESHOLDS = {
    "blocked_recall": (1.0, "min"), "answerable_blocked": (0.0, "max"), "triage": (0.9, "min"),
    "execution": (0.85, "min"), "intents": (0.8, "min"), "follow_up": (0.85, "min"),
}
DASHBOARD_ID = 1
BUSINESS_TIMEZONE = timezone(timedelta(hours=7))
# A figure matches the gold one within this share of it, or this much absolutely for figures near zero.
RELATIVE_TOLERANCE = 0.005
ABSOLUTE_TOLERANCE = 0.01
CHECKS = ("error", "triage", "intents", "metrics", "source", "value")


def _dates(today: date) -> dict[str, str]:
    week_start = today - timedelta(days=today.weekday())
    last_week_start = week_start - timedelta(days=7)
    month_start = today.replace(day=1)
    next_month = (month_start + timedelta(days=32)).replace(day=1)
    named = {
        "today": today, "yesterday": today - timedelta(days=1), "tomorrow": today + timedelta(days=1),
        "week_start": week_start, "week_end": week_start + timedelta(days=6),
        "last_week_start": last_week_start, "last_week_end": week_start - timedelta(days=1),
        "month_start": month_start, "month_end": next_month - timedelta(days=1),
    }
    return {name: value.isoformat() for name, value in named.items()}


def gold_rows(sql: str) -> list[list[Any]]:
    """The reference SQL's rows on the warehouse, each as its values in column order."""
    wrapped = f"SELECT coalesce(json_agg(row_to_json(gold)), '[]') FROM ({sql}) AS gold;"
    command = [
        "docker", "compose", "exec", "-T", "warehouse-db",
        "sh", "-c", 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -v ON_ERROR_STOP=1',
    ]
    finished = subprocess.run(
        resolve_command(command), cwd=ROOT, input=wrapped, capture_output=True, text=True, encoding="utf-8",
        check=False,
    )
    if finished.returncode != 0:
        raise RuntimeError(finished.stderr.strip() or "psql failed")
    return [list(row.values()) for row in json.loads(finished.stdout)]


def _login() -> urllib.request.OpenerDirector:
    """Log in through the /login/ form, because the extensions read the admin role from the browser session."""
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    with opener.open(f"{SUPERSET_URL}/login/", timeout=60) as response:
        page = response.read().decode()
    token = re.search(r'name="csrf_token"[^>]*value="([^"]+)"', page)
    if not token:
        fail("could not read the CSRF token from /login/")
    form = urllib.parse.urlencode({"csrf_token": token.group(1), "username": "admin", "password": "admin"}).encode()
    opener.open(f"{SUPERSET_URL}/login/", data=form, timeout=60).read()
    return opener


def _csrf(opener: urllib.request.OpenerDirector) -> str:
    with opener.open(f"{SUPERSET_URL}/api/v1/security/csrf_token/", timeout=60) as response:
        return json.loads(response.read())["result"]


def _ask(opener: urllib.request.OpenerDirector, question: str, session_id: int | None) -> list[list[Any]]:
    """One question through the chat stream, as the list of its [event, data] pairs."""
    csrf = _csrf(opener)
    body = {"session_id": session_id, "question": question} if session_id else {
        "dashboard_id": DASHBOARD_ID, "question": question,
    }
    request = urllib.request.Request(
        f"{SUPERSET_URL}{CHAT_STREAM}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-CSRFToken": csrf, "Referer": SUPERSET_URL},
    )
    try:
        with opener.open(request, timeout=600) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        return [["http_error", {"status": error.code, "body": error.read().decode(errors="replace")[:500]}]]
    events = []
    for frame in raw.split("\n\n"):
        lines = frame.splitlines()
        name = next((line[6:].strip() for line in lines if line.startswith("event:")), None)
        data = "\n".join(line[5:].strip() for line in lines if line.startswith("data:"))
        if name and data:
            events.append([name, json.loads(data)])
    return events


def _plain(text: Any) -> str:
    decomposed = unicodedata.normalize("NFD", str(text).replace("đ", "d").replace("Đ", "D"))
    return " ".join("".join(char for char in decomposed if unicodedata.category(char) != "Mn").lower().split())


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _close(got: float | None, gold: float | None, tolerance: float) -> bool:
    if got is None or gold is None:
        return got is None and gold is None
    return abs(got - gold) <= max(tolerance * abs(gold), ABSOLUTE_TOLERANCE)


def _results(done: dict[str, Any]) -> list[dict[str, Any]]:
    if done.get("result"):
        return [done["result"]]
    return [part["result"] for part in done.get("parts") or [] if part.get("result")]


def _labelled(row: dict[str, Any], dimension: dict[str, Any]) -> str:
    value = row.get(dimension["name"])
    labels = {str(item["value"]): item["label"] for item in dimension.get("value_labels") or []}
    return str(labels.get(str(value), value))


def _metric_values(result: dict[str, Any]) -> list[float]:
    names = [metric["name"] for metric in result.get("metrics", [])]
    names += [metric["compared_name"] for metric in result.get("metrics", []) if metric.get("compared_name")]
    return [
        number for row in result.get("rows", []) for name in names
        if (number := _number(row.get(name))) is not None
    ]


def _first_value(result: dict[str, Any] | None) -> float | None:
    if not result or not result.get("rows") or not result.get("metrics"):
        return None
    return _number(result["rows"][0].get(result["metrics"][0]["name"]))


def _series(result: dict[str, Any] | None) -> dict[str, float | None]:
    if not result or not result.get("dimensions") or not result.get("metrics"):
        return {}
    dimension, metric = result["dimensions"][0], result["metrics"][0]["name"]
    return {_plain(_labelled(row, dimension)): _number(row.get(metric)) for row in result.get("rows", [])}


def value_check(check: dict[str, Any], gold: list[list[Any]] | None, done: dict[str, Any]) -> tuple[bool, str]:
    """Whether the answer's figures match the gold rows, and what was compared, in words."""
    kind = check["kind"]
    tolerance = check.get("tolerance", RELATIVE_TOLERANCE)
    results = _results(done)
    first = results[0] if results else None
    if kind == "none":
        return True, "không cần số liệu"
    if kind == "clarify":
        # A question worded by perception ("nắng nóng gay gắt") is asked back, never answered with a guessed threshold.
        values = [value for result in results for value in _metric_values(result)]
        asked_back = bool(done.get("missing") or done.get("clarifications"))
        return asked_back and not values, f"missing={done.get('missing')}, giá trị={values[:3]}"
    if kind == "no_answer":
        values = [value for result in results for value in _metric_values(result)]
        return not values, f"giá trị={values[:3]}"
    gold_values = [_number(value) for row in gold or [] for value in row]
    if kind == "number":
        got = _first_value(first)
        return _close(got, gold_values[0] if gold_values else None, tolerance), f"{got} / chuẩn {gold_values[:1]}"
    if kind == "count":
        got = _first_value(first)
        rows = first.get("row_count") if first else None
        expected = gold_values[0] if gold_values else None
        matched = _close(got, expected, tolerance) or (rows is not None and _close(float(rows), expected, 0))
        return matched, f"{got} ({rows} dòng) / chuẩn {expected}"
    if kind == "numbers":
        found = [value for result in results for value in _metric_values(result)]
        expected = [value for value in gold_values if value is not None]
        missed = [value for value in expected if not any(_close(item, value, tolerance) for item in found)]
        return not missed, f"thiếu {missed} trong {found[:8]}" if missed else f"đủ {expected}"
    if kind == "top":
        # The first group the answer ranks must be one of those tied for first in the gold rows.
        if not first or not first.get("dimensions") or not first.get("rows"):
            return False, "không có cột nhóm"
        got = _plain(_labelled(first["rows"][0], first["dimensions"][0]))
        expected = [_plain(row[0]) for row in gold or []]
        return got in expected, f"{got} / chuẩn một trong {expected}"
    if kind == "labels":
        if not first or not first.get("dimensions"):
            return False, "không có cột nhóm"
        got = [_plain(_labelled(row, first["dimensions"][0])) for row in first.get("rows", [])]
        expected = [_plain(row[0]) for row in gold or []]
        if check.get("ordered"):
            return got[:len(expected)] == expected, f"{got[:5]} / chuẩn {expected}"
        return set(got) == set(expected), f"{sorted(got)} / chuẩn {sorted(expected)}"
    if kind == "series":
        got = _series(first)
        expected = {_plain(row[0]): _number(row[1]) for row in gold or []}
        wrong = [label for label, value in expected.items() if not _close(got.get(label), value, tolerance)]
        return not wrong, f"lệch {wrong} / {len(expected)} điểm" if wrong else f"khớp {len(expected)} điểm"
    raise ValueError(f"unknown check kind {kind}")


def score(case: dict[str, Any], gold: list[list[Any]] | None, events: list[list[Any]]) -> dict[str, Any]:
    """Each check of the case: True, False, or None when the case does not ask for it."""
    expect = case["expect"]
    done = next((data for name, data in events if name == "done"), None)
    triage = next(
        (data.get("result") for name, data in events
         if name == "tool_end" and data.get("name") == "classify_question" and data.get("status") == "ok"),
        None,
    ) or {}
    checks: dict[str, bool | None] = dict.fromkeys(CHECKS)
    notes = {}
    checks["error"] = done is not None and done.get("status") != "failed"
    if not checks["error"]:
        notes["error"] = next((json.dumps(data, ensure_ascii=False)[:300] for name, data in events
                               if name in ("error", "http_error")), "không có sự kiện done")
        return {"checks": checks, "notes": notes, "triage": triage.get("triage"), "status": None}
    checks["triage"] = triage.get("triage") == expect["triage"]
    intents = sorted(item["intent"] for item in triage.get("sub_questions") or [])
    if "intents" in expect:
        checks["intents"] = intents == sorted(expect["intents"])
    results = _results(done)
    used = list(dict.fromkeys(metric["label"] for result in results for metric in result.get("metrics", [])))
    if "metrics" in expect:
        checks["metrics"] = set(expect["metrics"]) <= set(used)
    sources = {result.get("source") or "metric" for result in results}
    if "source" in expect:
        checks["source"] = expect["source"] in sources
    checks["value"], notes["value"] = value_check(case["check"], gold, done)
    return {
        "checks": checks, "notes": notes, "triage": triage.get("triage"), "intents": intents, "metrics": used,
        "sources": sorted(sources), "status": done.get("status"),
    }


def run_case(case: dict[str, Any], dates: dict[str, str]) -> dict[str, Any]:
    started = time.monotonic()
    gold, gold_error = None, None
    if case.get("gold_sql"):
        try:
            gold = gold_rows(case["gold_sql"].format(**dates))
        except RuntimeError as error:
            gold_error = str(error)
    opener = _login()
    turns, session_id = [], None
    for question in [*case.get("history", []), case["question"]]:
        events = _ask(opener, question, session_id)
        session_id = session_id or next((data["id"] for name, data in events if name == "session"), None)
        turns.append({"question": question, "events": events})
    scored = score(case, gold, turns[-1]["events"])
    if gold_error:
        scored["checks"]["value"], scored["notes"]["value"] = False, f"SQL chuẩn lỗi: {gold_error}"
    steps = [data["name"] for name, data in turns[-1]["events"] if name == "tool_start"]
    failed = [name for name in CHECKS if scored["checks"][name] is False]
    return {
        "id": case["id"], "category": case["category"], "question": case["question"], "gold": gold,
        "passed": not failed, "failed": failed, **scored, "steps": steps,
        "seconds": round(time.monotonic() - started, 1), "turns": turns,
    }


def _report(results: list[dict[str, Any]], dates: dict[str, str]) -> str:
    passed = sum(result["passed"] for result in results)
    lines = [
        f"# Benchmark AI Agent: {passed}/{len(results)} câu đạt",
        "",
        f"Ngày chạy (giờ Việt Nam): {dates['today']}. SQL chuẩn tính lại theo ngày này.",
        "",
        "| Kiểm tra | Đạt | Có kiểm |",
        "| --- | --- | --- |",
    ]
    for name in CHECKS:
        asked = [result for result in results if result["checks"].get(name) is not None]
        lines.append(f"| {name} | {sum(bool(result['checks'][name]) for result in asked)} | {len(asked)} |")
    lines += ["", "| Nhóm | Đạt | Tổng |", "| --- | --- | --- |"]
    for category in dict.fromkeys(result["category"] for result in results):
        group = [result for result in results if result["category"] == category]
        lines.append(f"| {category} | {sum(result['passed'] for result in group)} | {len(group)} |")
    lines += ["", "| Câu | Nhóm | Kết quả | Lỗi | Triage | Intent | Metric | Giá trị | Giây |",
              "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for result in results:
        cells = [
            result["id"], result["category"], "đạt" if result["passed"] else "**trượt**", ", ".join(result["failed"]),
            result.get("triage") or "", ", ".join(result.get("intents") or []), ", ".join(result.get("metrics") or []),
            (result["notes"].get("value") or result["notes"].get("error") or "").replace("|", "/"),
            str(result["seconds"]),
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def _classified(opener: urllib.request.OpenerDirector, item: dict[str, Any], dashboard: str | None) -> dict:
    body = {"question": item["question"], "history": item.get("history", []), "dashboard": dashboard}
    request = urllib.request.Request(
        f"{SUPERSET_URL}{CLASSIFY}", data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "X-CSRFToken": _csrf(opener), "Referer": SUPERSET_URL},
    )
    try:
        with opener.open(request, timeout=300) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        return {"error": f"HTTP {error.code}: {error.read().decode(errors='replace')[:300]}"}


def _intents(labels: dict[str, Any]) -> list[str]:
    return sorted(item["intent"] for item in labels.get("sub_questions") or [])


def _scored_labels(item: dict[str, Any], labels: dict[str, Any]) -> dict[str, Any]:
    failed = "error" in labels
    answerable = item["triage"] == "answerable"
    shape = (labels.get("execution"), len(labels.get("sub_questions") or []))
    return {
        "id": item["id"], "question": item["question"], "error": labels.get("error"),
        "expected": item["triage"], "got": labels.get("triage"),
        "triage": not failed and labels["triage"] == item["triage"],
        "execution": None if not answerable else not failed and shape == (
            item.get("execution"), len(item.get("sub_questions") or [])),
        "intents": None if not answerable else not failed and _intents(labels) == _intents(item),
        "follow_up": None if failed else labels.get("follow_up") == item.get("follow_up", "new"),
        "got_intents": _intents(labels), "expected_intents": _intents(item),
    }


def _taxonomy_measures(rows: list[dict[str, Any]]) -> dict[str, tuple[float, int]]:
    def share(name: str) -> tuple[float, int]:
        scored = [row for row in rows if row[name] is not None]
        return (sum(bool(row[name]) for row in scored) / len(scored) if scored else 1.0), len(scored)

    blocked = [row for row in rows if row["expected"] == "blocked"]
    answerable = [row for row in rows if row["expected"] == "answerable"]
    caught = sum(row["got"] == "blocked" for row in blocked) / len(blocked) if blocked else 1.0
    wrongly = sum(row["got"] == "blocked" for row in answerable) / len(answerable) if answerable else 0.0
    return {
        "blocked_recall": (caught, len(blocked)), "answerable_blocked": (wrongly, len(answerable)),
        "triage": share("triage"), "execution": share("execution"), "intents": share("intents"),
        "follow_up": share("follow_up"),
    }


def run_taxonomy(workers: int) -> None:
    """Score the classifier on the labelled sample questions against the thresholds of the taxonomy spec."""
    sample = [json.loads(line) for line in TAXONOMY_SAMPLE.read_text(encoding="utf-8").splitlines() if line.strip()]
    opener = _login()
    with opener.open(f"{SUPERSET_URL}/api/v1/dashboard/{DASHBOARD_ID}", timeout=60) as response:
        dashboard = json.loads(response.read())["result"].get("dashboard_title")
    step(f"Classifying {len(sample)} labelled questions on {SUPERSET_URL} ({workers} at a time)")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        got = list(pool.map(lambda item: _classified(_login(), item, dashboard), sample))
    rows = [_scored_labels(item, labels) for item, labels in zip(sample, got)]
    # A question the server could not classify (out of model credit, a crash) says nothing about the classifier.
    failed = [row for row in rows if row["error"]]
    rows = [row for row in rows if not row["error"]]

    lines = [f"# Phân loại câu hỏi: {len(rows)}/{len(sample)} câu mẫu đo được", ""]
    if failed:
        lines += [f"{len(failed)} câu lỗi phía máy chủ, không tính vào chỉ tiêu: "
                  + ", ".join(str(row["id"]) for row in failed) + f". Lỗi đầu: {failed[0]['error'][:200]}", ""]
        print(f"WARN {len(failed)} questions failed on the server and are left out: {failed[0]['error'][:120]}")
    lines += ["| Chỉ tiêu | Đo được | Ngưỡng | Đạt |", "| --- | --- | --- | --- |"]
    for name, (value, count) in _taxonomy_measures(rows).items():
        bound, kind = TAXONOMY_THRESHOLDS[name]
        passed = value >= bound if kind == "min" else value <= bound
        sign, verdict = ("≥" if kind == "min" else "≤"), ("đạt" if passed else "không đạt")
        lines.append(f"| {name} | {value:.0%} ({count} câu) | {sign} {bound:.0%} | {verdict} |")
        print(f"{'PASS' if passed else 'FAIL'} {name:<20} {value:.0%} of {count}")
    lines += ["", "| Câu | Mong đợi | Nhận | Sai ở |", "| --- | --- | --- | --- |"]
    for row in rows:
        wrong = [name for name in ("triage", "execution", "intents", "follow_up") if row[name] is False]
        if wrong:
            detail = f"{', '.join(wrong)}; ý định {row['got_intents']} / {row['expected_intents']}"
            lines.append(f"| {row['id']} {row['question']} | {row['expected']} | {row['got']} | {detail} |")
    run_dir = BENCHMARK_DIR / "runs" / (datetime.now(BUSINESS_TIMEZONE).strftime("%Y%m%d-%H%M%S") + "-taxonomy")
    run_dir.mkdir(parents=True)
    (run_dir / "taxonomy.json").write_text(json.dumps(rows + failed, ensure_ascii=False, indent=1), encoding="utf-8")
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    step(f"wrote {run_dir.relative_to(ROOT)}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--only", help="comma-separated case IDs to run, such as B01,B12")
    parser.add_argument("--workers", type=int, default=3, help="questions asked at the same time (default 3)")
    parser.add_argument("--taxonomy", action="store_true", help="measure the classifier alone on the labelled sample")
    args = parser.parse_args(argv)
    if args.taxonomy:
        wait_for_health(timeout=30)
        run_taxonomy(args.workers)
        return

    cases = [json.loads(line) for line in (BENCHMARK_DIR / "questions.jsonl").read_text(encoding="utf-8").splitlines()]
    if args.only:
        wanted = set(args.only.split(","))
        cases = [case for case in cases if case["id"] in wanted]
    dates = _dates(datetime.now(BUSINESS_TIMEZONE).date())
    step(f"Asking {len(cases)} questions on {SUPERSET_URL} ({args.workers} at a time)")
    wait_for_health(timeout=30)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda case: run_case(case, dates), cases))
    for result in results:
        mark = "PASS" if result["passed"] else "FAIL"
        print(f"{mark} {result['id']} {result['category']:<12} {', '.join(result['failed']):<20} {result['seconds']}s")

    run_dir = BENCHMARK_DIR / "runs" / datetime.now(BUSINESS_TIMEZONE).strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True)
    with (run_dir / "trajectories.jsonl").open("w", encoding="utf-8") as trajectories:
        for result in results:
            trajectories.write(json.dumps({"id": result["id"], "turns": result["turns"]}, ensure_ascii=False) + "\n")
    summary = [{key: value for key, value in result.items() if key != "turns"} for result in results]
    (run_dir / "results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    (run_dir / "report.md").write_text(_report(results, dates), encoding="utf-8")
    step(f"{sum(result['passed'] for result in results)}/{len(results)} passed; wrote {run_dir.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
