"""Exercise the grader with the reference files and intentionally changed copies."""

import argparse
from copy import deepcopy
from io import BytesIO
from pathlib import Path

from pypdf import PdfWriter

import grade_submission as grader
import read_evidence as reader
import solve


def parts(report):
    return {p["id"]: p for criterion in report["criteria"] for p in criterion["parts"]}


def expect_loss(items, *keys):
    for key in keys:
        assert items[key]["earned"] < items[key]["max_points"], key


def expect_full(items, *keys):
    for key in keys:
        assert items[key]["earned"] == items[key]["max_points"], key


def records(data, table):
    return data["workbook"][table]["records"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference_brief_pdf", type=Path, help="用标准参考简报渲染的 PDF")
    args = parser.parse_args()
    ref = reader.read_submission(solve.ROOT / "reference_answer")
    pdf = args.reference_brief_pdf

    def grade(data, review=None):
        return grader.grade_submission(data, pdf, review)

    baseline = grade(ref)
    pending = {"S09.2", "S09.3", "O05.1", "O05.2"}
    assert baseline["criteria_count"] == 30
    assert baseline["known_earned"] == baseline["scored_max_points"] == 96
    assert baseline["total_score"] is None and set(baseline["pending_review"]) == pending
    assert all(p["location"] and p["rule"] and p["evidence"] for p in parts(baseline).values())
    print("30 criteria have evidence; verified 96 objective points and 4 review slots")

    data = deepcopy(ref)
    for row in records(data, "checks"):
        if row[1].endswith("_PEAK"):
            row[4] = f"≤{row[4]}"
        elif row[1].endswith("_GAP"):
            row[4] = f">= {row[4]}"
        else:
            row[4] = f"={row[4]}"
    expect_full(parts(grade(data)), "S09.1")
    records(data, "checks")[0][4] = ">=2"
    expect_loss(parts(grade(data)), "S09.1")
    assert grader.constraint_threshold("D02_D03_GAP", "<=30") is None
    assert grader.constraint_threshold("D02_D03_GAP", ">30") is None
    print("equivalent inclusive thresholds accepted; reversed and strict comparisons rejected")

    data = deepcopy(ref)
    audit = records(data, "audit")
    valid = next(r for r in audit if r[-1] == 1)
    valid[1], valid[3], valid[5], valid[6], valid[9] = (
        "wrong-job", "2023-07-27 20:00", 1 - valid[5], valid[6] + 1, "wrong-host")
    next(r for r in audit if r[-1] == 0)[7] = "OK"
    audit.append(audit[-1].copy())
    records(data, "reasons")[0][2] += 1
    candidates = records(data, "candidates")
    next(r for r in candidates if r[0] == "D05" and r[2] == "W08")[9] += 30
    candidates[0][5], candidates[0][7], candidates[0][8], candidates[0][9] = 9, 999, 999, 999
    hit = records(data, "evidence")[0]
    hit[6], hit[7], hit[-1] = 1 - hit[6], "2023-07-29 13:12", hit[-1] + 5
    records(data, "evidence").pop(1)
    summaries = records(data, "summaries")
    summaries[0][1] += 1
    summaries[0][3] += 1
    expect_loss(parts(grade(data)), "D01.1", "D02.1", "D02.2", "D02.3", "D03.1", "D04.1", "D05.1",
                "D06.1", "D06.2", "D07.1", "I01.1", "I02.1", "I02.2", "I03.1", "I03.2", "I04.1",
                "I05.1", "I05.2", "I06.1", "I07.1")
    print("source, candidate, row-level impact and global-impact errors detected")

    data = deepcopy(ref)
    csv = data["schedule"]["records"]
    csv[0], csv[1] = csv[1], csv[0]
    csv[1][10] = str(int(csv[1][10]) + 1)
    records(data, "schedule")[0][10] += 1
    doc = data["brief"]["tables"]
    doc["brief_schedule"]["records"][0][4] = "1"
    doc["brief_metrics"]["records"][1][4] = "2"
    records(data, "checks")[0][3] += 1
    items = parts(grade(data))
    expect_loss(items, "O02.3", "O03.1", "O06.3", "O07.1", "S09.1")
    expect_full(items, "O07.3", "S05.1")
    print("CSV row order, wrong shared values, constraint report and Word flags distinguished")

    data = deepcopy(ref)
    data["schedule"]["records"][0][6] = "2023-07-29 19:59"
    reconciled = grade(data)
    assert reconciled["timestamp_reconciliations"][0]["key"] == ("base", "D01")
    items = parts(reconciled)
    expect_loss(items, "O07.2")
    expect_full(items, "O02.4", "I06.1", "I07.1", "S01.1", "S02.1", "S05.1", "O03.1", "O06.1")
    print("isolated CSV time transcription error corroborated from other artifacts")

    data = deepcopy(ref)
    for row in data["schedule"]["records"][1:6]:
        row[6:8] = ["2023-07-28 20:00", "2023-07-28 21:00"]
    expect_loss(parts(grade(data)), "S01.1", "S02.1", "S03.1", "S04.1", "S05.1", "S06.1", "S07.1", "S08.1")
    print("conflicting intervals invalidate the relevant constraint and optimization points")

    data = deepcopy(ref)
    del data["schedule"]
    del data["brief"]
    missing = grader.grade_submission(data)
    assert missing["schedule_evidence"].startswith("impact_audit")
    items = parts(missing)
    expect_full(items, "S05.1", "I07.1")
    assert all(items[key]["earned"] == 0 for key in ("O01.2", "O01.3", "O04.1", "O05.1", "O05.2"))
    assert grader.grade_submission({})["total_score"] == 0
    assert "O04.1" in grader.grade_submission(ref)["pending_review"]
    oversized = BytesIO()
    writer = PdfWriter()
    for _ in range(3):
        writer.add_blank_page(width=595, height=842)
    writer.write(oversized)
    expect_loss(parts(grader.grade_submission(ref, oversized)), "O04.1")
    print("missing files, workbook fallback, and unavailable pagination are handled")

    _, workers = solve.audit_workers(solve.read_inputs())
    local = solve.impacts(workers)
    best, target, _ = solve.best_plan(solve.feasible_plans(False), local)
    alternate = next(p for p in solve.feasible_plans(False) if p != best and solve.impact_score(p, local) == target)
    fallback, _, _ = solve.best_plan(solve.feasible_plans(True), local, alternate)
    data = deepcopy(ref)
    for name, plan in (("base", alternate), ("fallback", fallback)):
        for device, w, old in zip(solve.DEVICES, plan, alternate):
            row = next(q for q in data["schedule"]["records"] if q[:2] == [name, device])
            protected, jobs, gpu, _ = local[device, w]
            row[5:12] = [solve.wid(w), *map(solve.stamp, solve.WINDOWS[w]), str(len(protected)),
                         str(len(jobs)), str(gpu), str(int(w != old))]
        summary = next(r for r in records(data, "summaries") if grader.scenario(r[0]) == name)
        summary[1:4] = solve.impact_score(plan, local)
    items = parts(grade(data))
    expect_loss(items, "S06.1")
    expect_full(items, "S05.1", "S07.1", "S08.1", "I06.1", "I07.1")
    print("backup optimization and impact remain scorable against a noncanonical submitted main plan")

    fixtures = {k: {"reviewer": "test", "evidence": parts(baseline)[k]["location"],
                    "reason": "test input for the manual-review merge", "score": 1} for k in pending}
    complete = grade(ref, fixtures)
    assert not complete["pending_review"] and complete["total_score"] == 100
    print("manual-review merge produces a final score when all review slots are resolved")


if __name__ == "__main__":
    main()
