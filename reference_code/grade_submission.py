"""Score one blind submission against the private rubric and frozen inputs."""

import argparse
from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
import hashlib
from itertools import combinations
import json
from pathlib import Path
import re

import read_evidence as reader
import solve


SCENARIOS = ("base", "fallback")
DEVICES = solve.DEVICES
SCHEDULE_KEYS = [(s, d) for s in SCENARIOS for d in DEVICES]
METRICS = ("protected_jobs", "affected_jobs", "gpu_minutes", "changed_devices")
MINUTE = timedelta(minutes=1)
ABSENT = object()
EXAMPLE_LIMIT = 12


def text(value):
    return "" if value is None else str(value)


def number(value):
    try:
        return reader.count_value(value)
    except ValueError:
        return None


def signed_number(value):
    raw = text(value).strip()
    magnitude = number(raw[1:] if raw.startswith("-") else raw)
    return -magnitude if magnitude is not None and raw.startswith("-") else magnitude


def constraint_threshold(key, value):
    """Accept numeric limits and equivalent inclusive comparison expressions."""
    parsed = signed_number(value)
    if parsed is not None:
        return parsed
    match = re.fullmatch(r"(<=|>=|==|≤|≥|=)\s*(\d+)", text(value).strip())
    if match is None:
        return None
    operator, bound = match.groups()
    if key.endswith("_PEAK"):
        allowed = ("<=", "≤")
    elif key.endswith("_GAP"):
        allowed = (">=", "≥")
    else:
        allowed = ("=", "==", "<=", "≤")
    return int(bound) if operator in allowed else None


def minute(value):
    try:
        return datetime.strptime(reader.excel_time(value), reader.STAMP)
    except ValueError:
        return None


def scenario(value):
    return reader.SCENARIOS.get(text(value), text(value))


def window(value):
    raw = text(value)
    return int(raw[1:]) if len(raw) == 3 and raw[0] == "W" and raw[1:].isascii() and raw[1:].isdigit() else None


def rows(tables, name):
    return tables.get(name, {}).get("records", [])


def csv_flag(value):
    return int(value) if value in ("0", "1") else None


def by_key(records, key, value):
    result = defaultdict(list)
    for row in records:
        result[key(row)].append(value(row))
    return result


def only(index, key):
    values = index.get(key, [])
    return values[0] if len(values) == 1 else None


def shown(value):
    return None if value is ABSENT else value


def round_score(value):
    return float(Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


class Report:
    def __init__(self):
        rubric_source = (solve.ROOT / "rubric.json").read_bytes()
        self.rubric = json.loads(rubric_source)
        self.rubric_sha256 = hashlib.sha256(rubric_source).hexdigest()
        self.parts = {
            f"{criterion['id']}.{i}": dict(item)
            for criterion in self.rubric["criteria"]
            for i, item in enumerate(criterion["scoring"], 1)
        }
        definitions = self.rubric["criteria"]
        if (len({c["id"] for c in definitions}) != self.rubric["criteria_count"]
                or sum(c["points"] for c in definitions) != self.rubric["total_points"]
                or any(sum(p["points"] for p in c["scoring"]) != c["points"] for c in definitions)):
            raise ValueError("评分表的考点数量或分值合计不一致")
        self.results = {}

    def add(self, key, fraction, location, evidence):
        if key in self.results or (fraction is not None and not 0 <= fraction <= 1):
            raise ValueError(f"评分程序给出无效分项：{key}")
        part = self.parts[key]
        self.results[key] = {"id": key, "max_points": part["points"],
                             "earned": None if fraction is None else fraction * part["points"],
                             "rule": part["rule"], "location": location, "evidence": evidence}

    def binary(self, key, ok, location, evidence):
        self.add(key, int(bool(ok)), location, evidence)

    def ratio(self, key, expected, actual, location, *, missing=0, zero_proven=True):
        correct = 0
        differences = []
        for unit, value in expected.items():
            found = actual.get(unit, [])
            if value is not ABSENT and found == [value]:
                correct += 1
            elif len(differences) < EXAMPLE_LIMIT:
                differences.append({"key": unit, "expected": shown(value), "submitted": found[:3],
                                    "submitted_count": len(found),
                                    "reason": "缺少可核验的前置证据" if value is ABSENT else "值、数量或键不符"})
        for unit in sorted(actual.keys() - expected.keys(), key=str):
            if len(differences) >= EXAMPLE_LIMIT:
                break
            differences.append({"key": unit, "reason": "多报", "submitted": actual[unit][:3]})
        sent = sum(map(len, actual.values()))
        denominator = max(len(expected), sent) + missing
        self.add(key, correct / denominator if denominator else int(zero_proven), location,
                 {"correct_units": correct, "expected_units": len(expected), "submitted_units": sent,
                  "unverified_schedule_units": missing, "denominator": denominator,
                  "differences_sample": differences})

    def finish(self, review):
        if self.results.keys() != self.parts.keys():
            raise ValueError(f"评分分项遗漏：{self.parts.keys() - self.results.keys()}")
        if not isinstance(review, dict):
            raise ValueError("复核记录 JSON 的顶层须为评分分项 ID 到复核记录的对象")
        for key, record in review.items():
            original = self.results.get(key)
            if original is None or not isinstance(record, dict):
                raise ValueError(f"无效复核分项：{key}")
            score = record.get("score")
            notes = ("reviewer", "evidence", "reason")
            if (type(score) not in (int, float) or not 0 <= score <= original["max_points"]
                    or any(not isinstance(record.get(k), str) or not record[k].strip() for k in notes)
                    or (self.parts[key]["mode"] == "binary" and score not in (0, original["max_points"]))):
                raise ValueError(f"复核须填写 reviewer、evidence、reason 和符合档位的 score：{key}")
            if original["earned"] is not None:
                if record.get("basis") != "读取或取证限制" or original["earned"] == original["max_points"]:
                    raise ValueError(f"{key} 已自动计分。仅已有实际证据且受读取或取证限制时可复核。")
                original["automatic_earned"] = original["earned"]
            original["review"] = record
            original["earned"] = score
        criteria = []
        for definition in self.rubric["criteria"]:
            ident = definition["id"]
            parts = [self.results[k] for k in self.parts if k.startswith(ident + ".")]
            pending = [p["id"] for p in parts if p["earned"] is None]
            earned = round_score(sum(p["earned"] or 0 for p in parts))
            for part in parts:
                part["status"] = ("待复核" if part["earned"] is None else
                                  "满分" if part["earned"] == part["max_points"] else
                                  "零分" if part["earned"] == 0 else "部分得分")
            all_full = all(p["earned"] == p["max_points"] for p in parts)
            criteria.append({"id": ident, "name": definition["name"], "max_points": definition["points"],
                             "earned": None if pending else earned, "known_earned": earned,
                             "pending_points": sum(p["max_points"] for p in parts if p["id"] in pending),
                             "status": "待复核" if pending else "满分" if all_full
                             else "零分" if earned == 0 else "部分得分", "parts": parts})
        earned = round_score(sum(c["known_earned"] for c in criteria))
        pending = [key for key, item in self.results.items() if item["earned"] is None]
        total = self.rubric["total_points"]
        return {"rubric_sha256": self.rubric_sha256, "criteria_count": len(criteria),
                "max_points": total, "known_earned": earned,
                "scored_max_points": total - sum(self.parts[key]["points"] for key in pending),
                "total_score": None if pending else earned, "pending_review": pending, "criteria": criteria}


def grade_data(report, audit, tables):
    place = "impact_audit.xlsx / 逐条核验 / worker ID"
    raw = rows(tables, "audit")
    entry = by_key(raw, lambda r: text(r[0]), tuple)
    worker_ids = {r[0] for r in audit}
    matches = {r[0]: r for r in audit if r[5] is not None}
    included = {r[0]: r for r in audit if r[-1] == 1}

    def compare(key, gold, values, allowed=None):
        selected = {k: [values(r) for r in items] for k, items in entry.items()
                    if allowed is None or k in gold or k not in worker_ids}
        report.ratio(key, gold, selected, place)

    compare("D01.1", dict.fromkeys((r[0] for r in audit), True), lambda r: True)
    compare("D02.1", {k: r[1] for k, r in matches.items()}, lambda r: text(r[1]), matches)
    compare("D02.2", {k: r[5] for k, r in matches.items()}, lambda r: number(r[5]), matches)
    topology = {k: (r[2], r[9]) for k, r in matches.items() if r[2]}
    compare("D03.1", topology, lambda r: (text(r[2]), text(r[9])), topology)
    compare("D04.1", {k: (minute(r[3]), minute(r[4])) for k, r in included.items()},
            lambda r: (minute(r[3]), minute(r[4])), included)
    hits = rows(tables, "evidence")
    hit_gpu = defaultdict(set)
    for hit in hits:
        hit_gpu[text(hit[4])].add(number(hit[12]))
    def reported_gpu(row):
        if text(row[6]):
            return number(row[6])
        inferred = hit_gpu.get(text(row[0]), ())
        return next(iter(inferred)) if len(inferred) == 1 else None

    compare("D05.1", {k: r[6] for k, r in included.items()}, reported_gpu, included)
    compare("D06.1", {r[0]: (r[7], r[10]) for r in audit}, lambda r: (text(r[7]), number(r[10])))
    reasons = Counter(r[7] for r in audit)
    excluded = {k: v for k, v in reasons.items() if k != "OK"}
    recall = Counter(r[7] for r in audit if r[7] != "OK" and len(entry.get(r[0], [])) == 1
                     and text(entry[r[0]][0][7]) == r[7])
    report.add("D06.2", sum(recall[k] / v for k, v in excluded.items()) / len(excluded), place,
               {"categories": {k: {"expected": v, "correct": recall[k]} for k, v in excluded.items()}})
    submitted_counts = Counter(text(r[7]) for r in raw)
    listed = by_key(rows(tables, "reasons"), lambda r: text(r[0]), lambda r: number(r[2]))
    gold = {k: v if submitted_counts[k] == v else ABSENT for k, v in reasons.items()}
    for k in listed.keys() - gold.keys():
        if k in solve.REASONS and not submitted_counts[k]:
            gold[k] = 0
        elif k in ("TOTAL", "总计", "合计"):
            gold[k] = len(audit) if len(raw) == len(audit) else ABSENT
    report.ratio("D07.1", gold, listed, "impact_audit.xlsx / 核验汇总、逐条核验 / 原因码")


def candidate_rows(tables):
    grouped = by_key(rows(tables, "candidates"), lambda r: (text(r[1]), window(r[2])), tuple)
    return {
        key: [tuple(group[0][:5]) + (1, 1) + tuple(group[0][7:])]
        if len(group) == 2 and {(number(r[5]), number(r[6])) for r in group} == {(1, 0), (0, 1)}
        and all((r[0], minute(r[3]), minute(r[4]), *(number(v) for v in r[7:10])) ==
                (group[0][0], minute(group[0][3]), minute(group[0][4]), *(number(v) for v in group[0][7:10]))
                for r in group) else group
        for key, group in grouped.items()
    }


def grade_candidates(report, tables, local):
    place = "impact_audit.xlsx / 候选影响 / 设备全路径、窗口"
    submitted = candidate_rows(tables)
    keys = {(solve.TABLE[d][0], w): (d, w) for d, w in local}
    gold = {k: (d, *solve.WINDOWS[w], int(w in solve.window_choices(d, False)), 1)
            for k, (d, w) in keys.items()}
    values = {k: [(r[0], minute(r[3]), minute(r[4]), number(r[5]), number(r[6])) for r in group]
              for k, group in submitted.items()}
    report.ratio("I01.1", gold, values, place)
    edge = {("D03", 3), ("D05", 7), ("D05", 8), ("D07", 8)}
    for code, index in (("I02.1", 0), ("I02.2", 1), ("I03.1", 2), ("I03.2", 2)):
        cohort = {k: pair for k, pair in keys.items()
                  if index != 2 or (pair in edge) == (code == "I03.2")}
        gold = {k: len(local[pair][index]) if index < 2 else local[pair][index] for k, pair in cohort.items()}
        actual = {k: [number(r[7 + index]) for r in group] for k, group in submitted.items()
                  if k in cohort or (k not in keys and code != "I03.2")}
        report.ratio(code, gold, actual, place)


@dataclass(frozen=True)
class Slot:
    path: str
    window: int | None
    start: datetime | None
    end: datetime | None
    metrics: tuple
    changed: int | None
    row: int
    source_interval: tuple

    @property
    def interval(self):
        return self.start, self.end

    @property
    def has_interval(self):
        return self.start is not None and self.end is not None and self.start < self.end


def schedule_index(records, is_csv=False, first_row=2):
    result = defaultdict(list)
    for line, r in enumerate(records, first_row):
        r = (list(r) + [None] * 12)[:12]
        key = (text(r[0]) if is_csv else scenario(r[0]), text(r[1]))
        result[key].append(Slot("/".join(map(text, r[2:5])), window(r[5]), minute(r[6]), minute(r[7]),
                                tuple(map(number, r[8:11])), (csv_flag if is_csv else number)(r[11]), line,
                                tuple(r[6:8])))
    return result


def word_slot(value, selected):
    try:
        w, lo, hi = reader.parse_word_window(value, solve.STARTS[0].year)
    except ValueError:
        w = window(value.strip())
        if w is not None:
            return (w, None, None)
        for separator in ("–", "—", " 至 "):
            parts = value.split(separator)
            if len(parts) == 2:
                lo, hi = map(minute, parts)
                if lo is not None and hi is not None:
                    return selected.window, lo, hi
        return None
    return window(w), minute(lo), minute(hi)


def reconcile(csv, excel, word):
    actual = {k: only(csv, k) for k in SCHEDULE_KEYS if len(csv.get(k, [])) == 1}
    notes = []
    for name in SCENARIOS:
        slots = {d: actual.get((name, d)) for d in DEVICES}
        if not all(slots.values()) or sum(len(v) for k, v in csv.items() if k[0] == name) != len(DEVICES):
            continue
        if any(slot.path != solve.TABLE[d][0] or slot.window not in solve.window_choices(d, name == "fallback")
               for d, slot in slots.items()):
            continue
        errors = [(d, slot) for d, slot in slots.items() if slot.interval != solve.WINDOWS[slot.window]]
        if len(errors) != 1:
            continue
        d, slot = errors[0]
        lo, hi = solve.WINDOWS[slot.window]
        if (slot.start != lo) + (slot.end != hi) != 1:
            continue
        others = {}
        workbook_slot = only(excel, (name, d))
        if workbook_slot:
            others["impact_audit.xlsx / 排程明细"] = (workbook_slot.path, workbook_slot.window,
                                                         *workbook_slot.interval)
        word_row = only(word, d)
        if word_row:
            w = word_slot(word_row[2 if name == "base" else 3], slot)
            if w is not None:
                others["change_brief.docx / 维护窗口明细"] = (word_row[1], *w)
        expected = (slot.path, slot.window, lo, hi)
        corroborated = [source for source, value in others.items() if value == expected]
        conflicts = [value for value in others.values()
                     if any(a is not None and a != b for a, b in zip(value, expected))]
        if corroborated and not conflicts:
            actual[name, d] = replace(slot, start=lo, end=hi)
            notes.append({"key": (name, d), "csv_row": slot.row, "csv_interval": slot.source_interval,
                          "confirmed_interval": (lo, hi), "confirmed_by": corroborated})
    return actual, notes


def local_impact(slot, workers):
    if not slot or not slot.has_interval or not slot.path or slot.window is None:
        return None
    hits = []
    for worker in workers.get(slot.path, ()):
        left, right = max(slot.start, worker.start), min(slot.end, worker.end)
        if right > left:
            hits.append((worker, left, right, (right - left) // MINUTE))
    return ({r.job for r, *_ in hits if r.protected}, {r.job for r, *_ in hits},
            sum(r.gpu * minutes for r, _, _, minutes in hits), hits)


def impacts_by_plan(actual, workers, local):
    results = {}
    for name, device in SCHEDULE_KEYS:
        slot = actual.get((name, device))
        canonical = local.get((device, slot.window)) if slot else None
        if canonical is not None and slot.path == solve.TABLE[device][0] and slot.interval == solve.WINDOWS[slot.window]:
            results[name, device] = canonical
        else:
            results[name, device] = local_impact(slot, workers)
    return results


def total_impact(impacts):
    if not all(r is not None for r in impacts):
        return None
    return (len(set().union(*(r[0] for r in impacts))), len(set().union(*(r[1] for r in impacts))),
            sum(r[2] for r in impacts))


def grade_impacts(report, audit, tables, actual, impacts):
    evidence_place = "impact_audit.xlsx / 命中证据 / 方案、设备全路径、窗口、worker ID"
    evidence = by_key(rows(tables, "evidence"), lambda r: (scenario(r[0]), text(r[2]), window(r[3]), text(r[4])), tuple)
    expected = {}
    for (name, d), result in impacts.items():
        if result is not None:
            slot = actual[name, d]
            for worker, left, right, minutes in result[3]:
                expected[name, slot.path, slot.window, worker.id] = (
                    d, worker.job, int(worker.protected), worker.start, worker.end,
                    left, right, minutes, worker.gpu, worker.gpu * minutes)
    projects = {
        "I04.1": ((0, 1), lambda r: (text(r[1]), text(r[5]))),
        "I05.1": ((3, 4, 5, 6, 7), lambda r: (*map(minute, r[7:11]), number(r[11]))),
        "I05.2": ((8, 9), lambda r: (number(r[12]), number(r[13]))),
    }
    missing = sum(result is None for result in impacts.values())
    for code, (columns, extract) in projects.items():
        gold = {k: tuple(r[i] for i in columns) for k, r in expected.items()}
        reported = {k: [extract(r) for r in group] for k, group in evidence.items()}
        report.ratio(code, gold, reported, evidence_place, missing=missing, zero_proven="evidence" in tables)

    by_worker = {r[0]: r for r in audit if r[5] is not None}
    identified = {k for k in evidence if k[3] in by_worker}
    protection_expected = {k: by_worker[k[3]][5] for k in identified}
    # An explicitly reported protection list in the audit can back up a blank hit flag.
    audit_flags = by_key(rows(tables, "audit"), lambda r: text(r[0]), lambda r: number(r[5]))
    protection_reported = {k: [number(r[6]) if text(r[6]) else only(audit_flags, k[3]) for r in evidence[k]]
                           for k in identified}
    report.ratio("D02.3", protection_expected, protection_reported, evidence_place,
                 zero_proven="evidence" in tables and not expected and not missing)

    totals = {name: total_impact([impacts[name, d] for d in DEVICES]) for name in SCENARIOS}
    summary = by_key(rows(tables, "summaries"), lambda r: scenario(r[0]), lambda r: tuple(map(number, r[1:5])))
    place = "impact_audit.xlsx / 方案汇总 / 方案"
    for code, columns in (("I06.1", (0, 1)), ("I07.1", (2,))):
        gold = {(name, METRICS[i]): totals[name][i] if totals[name] is not None else ABSENT
                for name in SCENARIOS for i in columns}
        reported = {(name, METRICS[i]): [r[i] for r in records] for name, records in summary.items() for i in columns}
        report.ratio(code, gold, reported, place)
    return summary


def time_checks(chosen, count_rows, name):
    fallback = name == "fallback"
    complete = len(chosen) == count_rows == len(DEVICES)
    timed = [slot for slot in chosen.values() if slot.has_interval]
    limits = (2, 2, 1 if fallback else 2)
    boundaries = [solve.WINDOWS[w][0].replace(hour=0, minute=0) for w in (1, 5, 9)]
    events = {slot.start for slot in timed} | set(boundaries)
    peaks = [0, 0, 0]
    for instant in events:
        night = max((i for i, point in enumerate(boundaries) if point <= instant), default=0)
        peaks[night] = max(peaks[night], sum(r.start <= instant < r.end for r in timed))
    paths = {d: slot.path.rsplit("/", 1)[0] for d, slot in chosen.items()}
    clashes = [(a, b) for a, b in combinations(chosen, 2)
               if paths[a] == paths[b] and all(chosen[d].has_interval for d in (a, b))
               and solve.intersects(chosen[a].interval, chosen[b].interval)]
    gaps = {}
    for device, previous in solve.PRECEDENCE.items():
        a, b = chosen.get(previous), chosen.get(device)
        gaps[f"{previous}_{device}_GAP"] = ((b.start - a.end) // MINUTE
                                              if a and b and a.has_interval and b.has_interval else None)
    off = [d for d, slot in chosen.items()
           if slot.path != solve.TABLE[d][0] or slot.window not in solve.window_choices(d, fallback)
           or slot.interval != solve.WINDOWS[slot.window]]
    coverage_errors = max(len(DEVICES), count_rows) - len(chosen)
    valid_windows = complete and not off
    full_time = complete and len(timed) == len(DEVICES)
    uplink_known = full_time and all(len(slot.path.split("/")) == 3 and all(slot.path.split("/")) for slot in timed)
    parallel = full_time and all(p <= cap for p, cap in zip(peaks, limits))
    exclusive = uplink_known and not clashes
    checks = {f"NIGHT_{i}_PEAK": (p if full_time else None, cap, p <= cap)
              for i, (p, cap) in enumerate(zip(peaks, limits), 1)}
    checks.update({key: (v, 30, v is not None and v >= 30) for key, v in gaps.items()})
    checks.update({"WINDOW_VIOLATIONS": (len(off), 0, not off),
                   "UPLINK_OVERLAPS": (len(clashes) if uplink_known else None, 0, not clashes),
                   "DEVICE_COVERAGE_ERRORS": (coverage_errors, 0, coverage_errors == 0)})
    return valid_windows, parallel, exclusive, gaps, checks, off, clashes


def prefix(value, target):
    for i, (actual, expected) in enumerate(zip(value, target)):
        if actual != expected:
            return i
    return len(target)


def grade_constraints(report, actual, selected, local, tables, place):
    plans = {}
    uplinks, collisions, submitted_gaps = {}, {}, {}
    worksheet = by_key(rows(tables, "checks"), lambda r: (scenario(r[0]), text(r[1])),
                       lambda r: (signed_number(r[3]), constraint_threshold(text(r[1]), r[4]), text(r[5])))
    summary_actual = defaultdict(list)
    for i, name in enumerate(SCENARIOS, 1):
        chosen = {d: actual[name, d] for d in DEVICES if (name, d) in actual}
        count = sum(len(v) for key, v in selected.items() if key[0] == name)
        windows, parallel, uplink, gaps, checks, off, clashes = time_checks(chosen, count, name)
        plans[name] = (tuple(chosen[d].window for d in DEVICES)
                       if windows and parallel and uplink and all(g is not None and g >= 30 for g in gaps.values())
                       else None)
        report.binary(f"S01.{i}", windows, place,
                      {"scenario": name, "rows": count, "recognized_devices": list(chosen),
                       "slot_violations": [{"device": d, "submitted_window": chosen[d].window,
                                            "submitted_path": chosen[d].path, "submitted_interval": chosen[d].interval,
                                            "expected_path": solve.TABLE[d][0],
                                            "eligible_windows": solve.window_choices(d, name == "fallback"),
                                            "official_window_interval": solve.WINDOWS.get(chosen[d].window)}
                                           for d in off]})
        report.binary(f"S02.{i}", parallel, place,
                      {"scenario": name, "nightly_peaks_and_limits": {k: v[:2] for k, v in checks.items() if "PEAK" in k}})
        uplinks[name] = [uplink]
        collisions[name] = clashes
        for dependency, gap in gaps.items():
            submitted_gaps[name, dependency] = gap
        expected = {k: (v, cap, "通过" if good else "失败") for k, (v, cap, good) in checks.items()}
        differences = {k: {"expected": v, "submitted": worksheet.get((name, k), [])}
                       for k, v in expected.items() if v[0] is None or worksheet.get((name, k), []) != [v]}
        extra = [k for s, k in worksheet if s == name and k not in checks]
        if differences or extra or not all((name, d) in actual for d in DEVICES):
            summary_actual[name] = [{"differences": differences, "extra_keys": extra}]
        else:
            summary_actual[name] = [True]
    for name, check in worksheet:
        if name not in SCENARIOS:
            summary_actual[name].append({"extra_key": check})
    report.ratio("S03.1", dict.fromkeys(SCENARIOS, True), uplinks, place)
    report.results["S03.1"]["evidence"]["overlapping_devices"] = collisions
    gaps_ok = {k: [v is not None and v >= 30] for k, v in submitted_gaps.items()}
    report.ratio("S04.1", dict.fromkeys(gaps_ok, True), gaps_ok, place)
    report.results["S04.1"]["evidence"]["actual_gaps_minutes"] = [
        {"key": k, "minutes": v} for k, v in submitted_gaps.items()]
    report.ratio("S09.1", dict.fromkeys(SCENARIOS, True), summary_actual, "impact_audit.xlsx / 约束复核 / 方案、检查键")

    best, objective, _ = solve.best_plan(solve.feasible_plans(False), local)
    base = plans["base"]
    base_cost = solve.impact_score(base, local) if base else None
    matched = prefix(base_cost, objective) if base else 0
    report.add("S05.1", (0, 3, 5, 7)[matched] / 7, place,
               {"submitted": base_cost, "expected": objective, "matched_prefix": matched, "feasible": base is not None})
    report.binary("S06.1", base == best, place, {"submitted_windows": base, "expected_windows": best})
    backup = plans["fallback"]
    if not base or not backup:
        for key in ("S07.1", "S08.1"):
            report.binary(key, False, place, {"base_feasible": base is not None, "fallback_feasible": backup is not None})
        return
    changes = sum(a != b for a, b in zip(base, backup))
    choices = list(solve.feasible_plans(True))
    min_changes = min(sum(a != b for a, b in zip(base, option)) for option in choices)
    report.add("S07.1", (1 if changes == min_changes else 2 / 5 if changes == min_changes + 1 else 0), place,
               {"base_windows": base, "fallback_windows": backup, "submitted_changes": changes,
                "minimum_for_submitted_base": min_changes})
    subgroup = (option for option in choices if sum(a != b for a, b in zip(base, option)) == changes)
    winner, target, _ = solve.best_plan(subgroup, local)
    cost = solve.impact_score(backup, local)
    matched = prefix(cost, target)
    report.add("S08.1", (5 if backup == winner else matched) / 5, place,
               {"submitted": cost, "expected_at_same_change_count": target, "submitted_windows": backup,
                "tie_break_windows": winner, "matched_prefix": matched})


def grade_csv(report, schedule, csv, actual, impacts, contract):
    path = "maintenance_schedule.csv"
    records = schedule.get("records", [])
    headers = schedule.get("columns", [])
    expected_headers = contract["columns"]
    report.binary("O02.1", headers == expected_headers, path + " / 表头",
                  {"expected": expected_headers, "submitted": headers})
    widths = [i for i, r in enumerate(records, 2) if len(r) != len(headers)]
    report.binary("O02.2", len(records) == contract["data_rows"] and not widths, path + " / 数据行",
                  {"expected_rows": contract["data_rows"], "submitted_rows": len(records), "irregular_rows": widths})
    if all(headers.count(c) == 1 for c in ("scenario", "device_id")):
        si, di = map(headers.index, ("scenario", "device_id"))
        ordered = [tuple(r[i] if i < len(r) else "" for i in (si, di)) for r in records]
    else:
        ordered = []
    order_issues = [{"row": i, "expected": k, "submitted": v}
                    for i, (k, v) in enumerate(zip(SCHEDULE_KEYS, ordered), 2) if k != v]
    report.binary("O02.3", ordered == SCHEDULE_KEYS, path + " / scenario、device_id",
                  {"submitted_key_count": len(ordered), "mismatches": order_issues[:EXAMPLE_LIMIT]})
    present = all(headers.count(c) == 1 for c in ("start_time", "end_time"))
    faults = []
    if present:
        start, end = map(headers.index, ("start_time", "end_time"))
        faults = [{"row": i, "submitted": (r[start:start + 1], r[end:end + 1])} for i, r in enumerate(records, 2)
                  if max(start, end) >= len(r) or not all(reader.valid_csv_time(r[j]) for j in (start, end))]
    report.binary("O02.4", present and records and not faults, path + " / start_time、end_time",
                  {"timestamp_columns_found": present, "invalid_rows": faults[:EXAMPLE_LIMIT]})
    expected = {}
    changed = {}
    for key in SCHEDULE_KEYS:
        s, d = key
        value = impacts[key]
        expected[key] = (len(value[0]), len(value[1]), value[2]) if value is not None and only(csv, key) else ABSENT
        base, selected = actual.get(("base", d)), actual.get(key)
        changed[key] = 0 if s == "base" and only(csv, key) else (
            int(base.window != selected.window) if only(csv, key) and base and selected
            and base.window is not None and selected.window is not None else ABSENT)
    report.ratio("O03.1", expected, {k: [r.metrics for r in v] for k, v in csv.items()},
                 path + " / 设备窗口影响指标")
    report.ratio("O03.2", changed, {k: [r.changed for r in v] for k, v in csv.items()},
                 path + " / changed_from_base")


def grade_document(report, doc, word, actual, pdf):
    path = "change_brief.docx"
    sections = (doc or {}).get("sections", {})
    if doc is None:
        report.binary("O04.1", False, path, {"reason": "缺少或无法打开 Word"})
    elif pdf is None:
        report.add("O04.1", None, path, {"reason": "须用统一环境渲染原始 DOCX 后确认页数"})
    else:
        try:
            from pypdf import PdfReader
            count = len(PdfReader(pdf).pages)
        except Exception as error:
            report.add("O04.1", None, path, {"pdf": str(pdf), "reason": str(error)})
        else:
            report.binary("O04.1", 1 <= count <= 2, path, {"rendered_pdf": str(pdf), "pages": count})
    for key, heading in (("O05.1", "审批建议"), ("O05.2", "数据覆盖与执行复核")):
        content = sections.get(heading, "")
        report.add(key, None if content else 0, f"{path} / {heading}", {"submitted_text": content})

    place = path + " / 维护窗口明细"
    matched = {}
    for i, s in enumerate(SCENARIOS, 1):
        gold = {d: True for d in DEVICES}
        actual_word = {}
        for d, records in word.items():
            chosen = actual.get((s, d))
            actual_word[d] = []
            for r in records:
                parsed = word_slot(r[i + 1], chosen) if chosen is not None else None
                compatible = (chosen is not None and parsed is not None and r[1] == chosen.path
                              and parsed[0] is not None and parsed[0] == chosen.window
                              and (parsed[1:] == chosen.interval or parsed[1:] == (None, None)))
                actual_word[d].append(True if compatible else {
                    "submitted_window": r[i + 1], "submitted_path": r[1],
                    "expected_window": chosen.window if chosen else None,
                    "expected_interval": chosen.interval if chosen else None,
                    "expected_path": chosen.path if chosen else None})
        matched[s] = actual_word
        report.ratio(f"O06.{i}", gold, actual_word, place + f" / {s}")
    diffs = []
    for d in DEVICES:
        a, b = actual.get(("base", d)), actual.get(("fallback", d))
        expected_flag = int(a.window != b.window) if a and b and a.window is not None and b.window is not None else None
        submitted_flags = [csv_flag(r[4]) for r in word.get(d, [])]
        if (expected_flag is None or submitted_flags != [expected_flag]
                or (expected_flag and any(matched[s].get(d) != [True] for s in SCENARIOS))):
            diffs.append({"device": d, "expected_flag": expected_flag, "submitted_flags": submitted_flags,
                          "base_window": a.window if a else None, "fallback_window": b.window if b else None})
    report.binary("O06.3", not diffs and set(word) == set(DEVICES), place + " / 变更、主排程、备用排程",
                  {"differences": diffs, "extra_devices": sorted(word.keys() - set(DEVICES))})


def grade_consistency(report, summaries, brief_metrics, csv, excel):
    gold, submitted = {}, {}
    for s in SCENARIOS:
        xlsx = only(summaries, s)
        word = brief_metrics.get(s, [])
        flags = [only(csv, (s, d)) for d in DEVICES]
        csv_total = sum(r.changed for r in flags) if all(r and r.changed is not None for r in flags) else None
        for i, column in enumerate(METRICS):
            raw = xlsx[i] if xlsx is not None else None
            gold[s, column] = raw if raw is not None else ABSENT
            if i != 3 or (csv_total is not None and raw == csv_total):
                submitted[s, column] = [r[i] for r in word]
            else:
                submitted[s, column] = [{"word": r[i], "csv_changed_total": csv_total} for r in word]
    report.ratio("O07.1", gold, submitted, "impact_audit.xlsx / 方案汇总 ↔ change_brief.docx / 主备方案影响 ↔ CSV 变更合计")

    def interval(r):
        return r.path, r.window, r.start, r.end, r.changed

    def metrics(r):
        return r.path, r.window, *r.metrics

    place = "maintenance_schedule.csv ↔ impact_audit.xlsx / 排程明细 / 方案、设备"
    for key, values in (("O07.2", interval), ("O07.3", metrics)):
        expected = {}
        for k in SCHEDULE_KEYS:
            slot = only(csv, k)
            units = values(slot) if slot is not None else ()
            expected[k] = units if units and all(v not in (None, "") for v in units) else ABSENT
        actual = {k: [values(r) for r in v] for k, v in excel.items()}
        report.ratio(key, expected, actual, place)


def grade_submission(data, pdf=None, review=None):
    task = reader.config()
    source = solve.read_inputs()
    audit, workers = solve.audit_workers(source)
    local = solve.impacts(workers)
    report = Report()
    xlsx = data.get("workbook", {})
    schedule = data.get("schedule", {})
    doc = data.get("brief")
    sections = (doc or {}).get("sections", {})
    doc_tables = (doc or {}).get("tables", {})
    word = by_key(rows(doc_tables, "brief_schedule"), lambda r: text(r[0]), tuple)
    word_metrics = by_key(rows(doc_tables, "brief_metrics"), lambda r: scenario(r[0]),
                          lambda r: tuple(map(number, r[1:5])))
    contract = task["deliverables"][1]
    headers = schedule.get("columns", [])
    csv_records = schedule.get("records", [])
    if all(headers.count(c) == 1 for c in solve.CSV_COLUMNS[:8]):
        indices = [headers.index(c) if headers.count(c) == 1 else None for c in solve.CSV_COLUMNS]
        canonical_csv = [[row[i] if i is not None and i < len(row) else None for i in indices] for row in csv_records]
        csv = schedule_index(canonical_csv, is_csv=True)
    else:
        csv = {}
    excel = schedule_index(rows(xlsx, "schedule"), first_row=xlsx.get("schedule", {}).get("header_row", 1) + 1)
    if any(key in csv for key in SCHEDULE_KEYS):
        selected, location = csv, "maintenance_schedule.csv"
        actual, notes = reconcile(csv, excel, word)
    else:
        selected, location = excel, "impact_audit.xlsx / 排程明细（CSV 排程不可辨识）"
        actual = {k: only(excel, k) for k in SCHEDULE_KEYS if len(excel.get(k, [])) == 1}
        notes = []
    impacts = impacts_by_plan(actual, workers, local)
    grade_data(report, audit, xlsx)
    grade_candidates(report, xlsx, local)
    summaries = grade_impacts(report, audit, xlsx, actual, impacts)
    grade_constraints(report, actual, selected, local, xlsx, location)
    methods = by_key(rows(xlsx, "method"), lambda r: scenario(r[0]), lambda r: [text(v) for v in r[1:4]])
    for code, columns in (("S09.2", (0, 1)), ("S09.3", (2,))):
        evidence = {s: [[record[i] for i in columns] for record in records] for s, records in methods.items()}
        complete = all(len(evidence.get(s, [])) == 1 and all(v.strip() for v in evidence[s][0]) for s in SCENARIOS)
        report.add(code, None if complete else 0, "impact_audit.xlsx / 核验方法", {"submitted_text": evidence})
    evidence_contract = task["evidence"]
    missing_tables = [t["sheet"] for t in evidence_contract["xlsx_tables"] if t["id"] not in xlsx]
    missing_doc = [t["id"] for t in evidence_contract["docx_tables"] if t["id"] not in doc_tables]
    missing_headings = [title for title in evidence_contract["brief_headings"] if title not in sections]
    bad_word_labels = [r[0] for r in rows(doc_tables, "brief_metrics")
                       if r[0] not in evidence_contract["word_scenario_labels"]]
    table_shapes = {k: v for k, v in data.get("issues", {}).items() if k.startswith("docx/") and k.endswith("/shape")}
    report.binary("O01.1", "workbook" in data and not missing_tables, "outputs/impact_audit.xlsx",
                  {"missing_tables_or_headers": missing_tables})
    report.binary("O01.2", "schedule" in data and bool(headers), "outputs/maintenance_schedule.csv",
                  {"readable": "schedule" in data, "header": headers})
    report.binary("O01.3", doc is not None and not missing_doc and not missing_headings
                  and not bad_word_labels and not table_shapes,
                  "outputs/change_brief.docx", {"missing_tables": missing_doc, "missing_headings": missing_headings,
                                              "invalid_scenario_labels": bad_word_labels, "malformed_rows": table_shapes})
    grade_csv(report, schedule, csv, actual, impacts, contract)
    grade_document(report, doc, word, actual, pdf)
    grade_consistency(report, summaries, word_metrics, csv, excel)
    return {**report.finish(review or {}), "read_issues": data.get("issues", {}),
            "schedule_evidence": location, "timestamp_reconciliations": notes}


def main():
    import docx  # Verify scoring-environment dependencies before reading any submission.
    import openpyxl

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path, help="被测 Agent 的 outputs 目录")
    parser.add_argument("--brief-pdf", type=Path, help="用统一环境从本次提交的 DOCX 渲染的 PDF")
    parser.add_argument("--manual-review", type=Path, help="待复核分项的评分记录 JSON")
    parser.add_argument("--report", type=Path, help="评分报告 JSON（不覆盖已有报告）")
    options = parser.parse_args()
    if not options.output_dir.is_dir():
        parser.error(f"找不到提交目录：{options.output_dir}")
    review = json.loads(options.manual_review.read_text(encoding="utf-8-sig")) if options.manual_review else {}
    try:
        result = grade_submission(reader.read_submission(options.output_dir, strict=False), options.brief_pdf, review)
    except (ValueError, PermissionError) as error:
        parser.error(str(error))
    result["submission_directory"] = str(options.output_dir.resolve())
    payload = json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n"
    if options.report:
        with options.report.open("x", encoding="utf-8") as target:
            target.write(payload)
        print(f"{result['criteria_count']} 项，已判 {result['known_earned']}/{result['scored_max_points']}；"
              f"待复核 {len(result['pending_review'])} 个分项。报告：{options.report}")
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
