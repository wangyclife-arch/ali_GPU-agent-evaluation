"""Rebuild the private reference answer from the three pinned CSV files."""

import argparse
import csv
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLE = {
    "D01": ("G6/P8/S17", (9, 10, 11, 12)),
    "D02": ("G6/P8/S18", (2, 5, 6, 8)),
    "D03": ("G6/P8/S23", (1, 3, 6, 7)),
    "D04": ("G6/P10/S13", (1, 2, 5, 6)),
    "D05": ("G6/P10/S14", (3, 4, 7, 8)),
    "D06": ("G6/P10/S17", (9, 10, 11, 12)),
    "D07": ("G6/P10/S47", (2, 4, 5, 8)),
    "D08": ("G6/P12/S8", (1, 3, 6, 7)),
    "D09": ("G6/P12/S14", (2, 4, 5, 7)),
    "D10": ("G6/P12/S23", (9, 10, 11, 12)),
}
DEVICES = tuple(TABLE)
PARENT = {d: path.rsplit("/", 1)[0] for d, (path, _) in TABLE.items()}
PRECEDENCE = {"D03": "D02", "D05": "D04"}
RESERVE_DEVICES = {"D01", "D06", "D10"}
GAP = timedelta(minutes=30)
HOUR = timedelta(hours=1)
STARTS = [datetime(2023, 7, d, h, m) for d in (27, 28, 29)
          for h, m in ((20, 0), (20, 30), (21, 30), (22, 0))]
STARTS.append(datetime(2023, 7, 29, 23))
WINDOWS = {i: (t, t + HOUR) for i, t in enumerate(STARTS, 1)}
CSV_COLUMNS = ("scenario", "device_id", "dsw", "psw", "asw", "window_id",
               "start_time", "end_time", "protected_jobs", "affected_jobs",
               "gpu_minutes", "changed_from_base")
REASONS = {
    "NO_JOB": "作业无法关联", "NO_HOST": "主机为空", "NO_TOPO": "主机无法关联拓扑",
    "BAD_TIME": "起止缺失或不可解析", "NONPOSITIVE_TIME": "时长非正",
    "BAD_RES": "RES 缺失或不是有效 JSON 对象", "NO_GPU_KEY": "缺少 GPU 键",
    "BAD_GPU": "GPU 数不是正整数", "OK": "进入可核算范围",
}


def check(condition, message):
    if not condition:
        raise ValueError(message)


def read_inputs():
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    entries = {f["path"]: f for f in manifest["files"]}
    data = {}
    for name in ("job", "worker", "topo"):
        relative = f"inputs/{name}.csv"
        source = ROOT / relative
        check(hashlib.sha256(source.read_bytes()).hexdigest() == entries[relative]["sha256"],
              f"原始文件哈希不符：{relative}")
        with source.open(encoding="utf-8", newline="") as stream:
            data[name] = list(csv.DictReader(stream, strict=True))
    return data


def index(rows, key):
    result = {row[key]: row for row in rows}
    check(len(result) == len(rows) and "" not in result, f"键 {key} 为空或重复")
    return result


def parse_time(value):
    try:
        return datetime.strptime(value, "%Y/%m/%d %H:%M")
    except ValueError:
        return None


def stamp(value):
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def wid(w):
    return f"W{w:02d}"


def parse_gpu(raw):
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError:
        return None, "BAD_RES"
    if not isinstance(obj, dict):
        return None, "BAD_RES"
    if "nvidia.com/gpu" not in obj:
        return None, "NO_GPU_KEY"
    value = obj["nvidia.com/gpu"]
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = int(value)
    if type(value) is not int or value <= 0:
        return None, "BAD_GPU"
    return value, "OK"


@dataclass(frozen=True)
class Worker:
    id: str
    job: str
    protected: bool
    start: datetime
    end: datetime
    gpu: int


def audit_workers(data):
    jobs = index(data["job"], "job_name")
    host_paths = {k: "/".join(r[c] for c in ("DSW", "PSW", "ASW"))
                  for k, r in index(data["topo"], "ip").items()}
    index(data["worker"], "id")
    by_path = defaultdict(list)
    audit = []
    for path, _ in TABLE.values():
        check(path in host_paths.values(), f"设备在拓扑表中不存在：{path}")
    for r in data["worker"]:
        j, host = r["job_name"], r["host_ip"]
        path = host_paths.get(host, "")
        start = parse_time(r["gmt_pod_running"])
        end = parse_time(r["gmt_pod_finished"])
        protected = int(jobs[j]["priority"] == "9") if j in jobs else None
        gpu = None
        if j not in jobs:
            reason = "NO_JOB"
        elif not host:
            reason = "NO_HOST"
        elif not path:
            reason = "NO_TOPO"
        elif start is None or end is None:
            reason = "BAD_TIME"
        elif end <= start:
            reason = "NONPOSITIVE_TIME"
        else:
            gpu, reason = parse_gpu(r["RES"])
        if reason == "OK":
            by_path[path].append(Worker(r["id"], j, bool(protected), start, end, gpu))
        audit.append([r["id"], j, path, r["gmt_pod_running"], r["gmt_pod_finished"],
                      protected, gpu, reason, REASONS[reason], host, int(reason == "OK")])
    return audit, by_path


def window_choices(device, fallback):
    options = TABLE[device][1]
    return options + (13,) if fallback and device in RESERVE_DEVICES else options


def intersects(a, b):
    return a[0] < b[1] and b[0] < a[1]


def cap(w, fallback):
    return 1 if fallback and w >= 9 else 2


def impacts(by_path):
    result = {}
    for device, (path, _) in TABLE.items():
        for w in window_choices(device, True):
            lo, hi = WINDOWS[w]
            evidence = []
            for worker in by_path[path]:
                left, right = max(lo, worker.start), min(hi, worker.end)
                if right > left:
                    minutes = (right - left) // timedelta(minutes=1)
                    evidence.append((worker, left, right, minutes))
            result[device, w] = (
                frozenset(r[0].job for r in evidence if r[0].protected),
                frozenset(r[0].job for r in evidence),
                sum(r[0].gpu * r[3] for r in evidence), evidence,
            )
    return result


def feasible_plans(fallback):
    # Event slices split at every candidate endpoint, with half-open occupancy.
    points = sorted({p for pair in WINDOWS.values() for p in pair})
    slices = {w: [i for i, p in enumerate(points[:-1]) if lo <= p < hi]
              for w, (lo, hi) in WINDOWS.items()}
    active = [0] * (len(points) - 1)
    selected = {}

    def extend(i):
        if i == len(DEVICES):
            yield tuple(selected[d] for d in DEVICES)
            return
        device = DEVICES[i]
        for w in window_choices(device, fallback):
            previous = PRECEDENCE.get(device)
            if previous and WINDOWS[w][0] < WINDOWS[selected[previous]][1] + GAP:
                continue
            if any(active[s] >= cap(w, fallback) for s in slices[w]):
                continue
            if any(PARENT[other] == PARENT[device] and intersects(WINDOWS[w], WINDOWS[v])
                   for other, v in selected.items()):
                continue
            selected[device] = w
            for s in slices[w]:
                active[s] += 1
            yield from extend(i + 1)
            for s in slices[w]:
                active[s] -= 1
            del selected[device]

    return extend(0)


def impact_score(plan, local):
    records = [local[d, w] for d, w in zip(DEVICES, plan)]
    return (len(set().union(*(r[0] for r in records))),
            len(set().union(*(r[1] for r in records))), sum(r[2] for r in records))


def best_plan(plans, local, base=None):
    best = None
    count = 0
    for plan in plans:
        count += 1
        impact = impact_score(plan, local)
        tie_key = (*impact, plan)
        if base is not None:
            tie_key = (sum(a != b for a, b in zip(plan, base)), *tie_key)
        if best is None or tie_key < best[0]:
            best = (tie_key, plan, impact)
    check(best is not None, "无可行排程")
    return best[1], best[2], count


def verify_independently(fallback, local, base=None):
    # Separate enumerator. The first two nights and the third night have
    # disjoint time domains. Validate each group's Cartesian product by
    # start-time counts, then score the combined plans across all nights.
    groups = [tuple(d for d in DEVICES if (d in RESERVE_DEVICES) == late)
              for late in (False, True)]
    available = []
    for group in groups:
        accepted = []
        for windows in itertools.product(*(window_choices(d, fallback) for d in group)):
            assignment = dict(zip(group, windows))
            if any(WINDOWS[assignment[d]][0] - WINDOWS[assignment[p]][1] < GAP
                   for d, p in PRECEDENCE.items() if d in assignment):
                continue
            if any(PARENT[a] == PARENT[b] and intersects(WINDOWS[assignment[a]], WINDOWS[assignment[b]])
                   for a, b in itertools.combinations(group, 2)):
                continue
            if any(sum(WINDOWS[v][0] <= WINDOWS[w][0] < WINDOWS[v][1] for v in windows)
                   > cap(w, fallback) for w in windows):
                continue
            accepted.append(assignment)
        available.append(accepted)
    plans = (tuple((early | late)[d] for d in DEVICES)
             for early, late in itertools.product(*available))
    return best_plan(plans, local, base)


def build_payload(audit, local):
    base, base_impact, base_count = best_plan(feasible_plans(False), local)
    fallback, backup_impact, backup_count = best_plan(feasible_plans(True), local, base)
    check((base, base_impact, base_count) == verify_independently(False, local), "主排程复核不一致")
    check((fallback, backup_impact, backup_count) == verify_independently(True, local, base), "备用排程复核不一致")
    counts = Counter(row[7] for row in audit)
    reasons = [[code, label, counts[code]] for code, label in REASONS.items()]
    schedule, evidence, summaries, checks = [], [], [], []
    for scenario, plan, values, count in (("base", base, base_impact, base_count),
                                         ("fallback", fallback, backup_impact, backup_count)):
        scenario_evidence = []
        for device, w, old in zip(DEVICES, plan, base):
            path = TABLE[device][0]
            protected, jobs, gpu, hits = local[device, w]
            schedule.append([scenario, device, *path.split("/"), wid(w),
                             *map(stamp, WINDOWS[w]), len(protected), len(jobs), gpu, int(w != old)])
            scenario_evidence.extend([scenario, device, path, wid(w), r.id, r.job, int(r.protected),
                                      stamp(r.start), stamp(r.end), stamp(left), stamp(right),
                                      minutes, r.gpu, minutes * r.gpu] for r, left, right, minutes in hits)
        calculated = (len({r[5] for r in scenario_evidence if r[6]}),
                      len({r[5] for r in scenario_evidence}), sum(r[-1] for r in scenario_evidence))
        check(calculated == values, f"{scenario} 明细与方案汇总不一致")
        check(len({r[4] for r in scenario_evidence}) == len(scenario_evidence), "方案内同一 worker 重复命中")
        evidence.extend(scenario_evidence)
        summaries.append([scenario, *values, sum(w != old for w, old in zip(plan, base)), count])
        is_fallback = scenario == "fallback"
        for night, date in enumerate((27, 28, 29), 1):
            night_windows = [w for w in plan if WINDOWS[w][0].day == date]
            peak = max((sum(WINDOWS[v][0] <= WINDOWS[w][0] < WINDOWS[v][1]
                            for v in night_windows) for w in night_windows), default=0)
            limit = 1 if is_fallback and night == 3 else 2
            checks.append([scenario, f"NIGHT_{night}_PEAK", f"7月{date}日班组峰值", peak, limit,
                           "通过" if peak <= limit else "失败"])
        selected = dict(zip(DEVICES, plan))
        for device, previous in PRECEDENCE.items():
            gap = (WINDOWS[selected[device]][0] - WINDOWS[selected[previous]][1]) // timedelta(minutes=1)
            checks.append([scenario, f"{previous}_{device}_GAP", f"{previous} 至 {device} 间隔分钟",
                           gap, 30, "通过" if gap >= 30 else "失败"])
        for key, label, violations in (
            ("WINDOW_VIOLATIONS", "获准窗口违规数", sum(w not in window_choices(d, is_fallback) for d, w in selected.items())),
            ("UPLINK_OVERLAPS", "同上联重叠对数", sum(PARENT[a] == PARENT[b] and intersects(WINDOWS[selected[a]], WINDOWS[selected[b]])
                                                          for a, b in itertools.combinations(selected, 2))),
            ("DEVICE_COVERAGE_ERRORS", "缺少或重复维护设备数", len(DEVICES) - len(selected)),
        ):
            checks.append([scenario, key, label, violations, 0, "通过" if violations == 0 else "失败"])
    check(all(r[-1] == "通过" for r in checks), "排程约束未通过")
    local_rows = [[d, TABLE[d][0], wid(w), *map(stamp, WINDOWS[w]),
                   int(w in window_choices(d, False)), 1, len(p), len(j), gpu]
                  for (d, w), (p, j, gpu, _) in local.items()]
    common = "方案内对保护和全部作业分别按作业名取并集，再累计命中 GPU·分钟。"
    rule = "依次最小化保护作业数、受影响作业数、GPU·分钟。各项相同时取设备顺序下字典序最小的窗口序列。"
    method = [
        ["base", "遍历各设备常规候选。按设备覆盖、班组并发、上联互斥和前置间隔筛除违规组合。",
         common + rule,
         "对全部可行组合计算排序键并取最小值。用独立分组枚举复核可行集计数和最优结果。"],
        ["fallback", "锁定主方案。对备用候选加上 W13 资格和第三晚单班组限制，保留其他硬约束。",
         common + "先最小化相对主方案窗口变更数。" + rule,
         "在固定主方案下穷举全部可行备用组合并比较排序键，再用独立分组枚举复核。"],
    ]
    contract = tomllib.loads((ROOT / "task.toml").read_text(encoding="utf-8"))["evidence"]
    return dict(audit=audit, reasons=reasons, schedule=schedule, evidence=evidence,
                summaries=summaries, candidates=local_rows, checks=checks, method=method,
                csv_columns=CSV_COLUMNS, contract=contract)


def brief_tables(data):
    metrics, slots = [table["columns"] for table in data["contract"]["docx_tables"]]
    metric_rows = [metrics] + [["主排程" if row[0] == "base" else "备用排程",
                                           *(f"{value:,}" for value in row[1:5])] for row in data["summaries"]]
    by_device = {(r[0], r[1]): r for r in data["schedule"]}
    entries = [slots]
    for device in DEVICES:
        row = [device, TABLE[device][0]]
        for scenario in ("base", "fallback"):
            item = by_device[scenario, device]
            lo, hi = (datetime.strptime(t, "%Y-%m-%d %H:%M") for t in item[6:8])
            end = hi.strftime("%H:%M" if lo.date() == hi.date() else "%m/%d %H:%M")
            row.append(f"{item[5]}  {lo:%m/%d %H:%M}–{end}")
        row.append(str(by_device["fallback", device][11]))
        entries.append(row)
    return metric_rows, entries


def brief_text(data):
    by_device = {(r[0], r[1]): r for r in data["schedule"]}
    changes = [f"{d} 从 {by_device['base', d][5]} 调至 {by_device['fallback', d][5]}"
               for d in DEVICES if by_device["fallback", d][-1]]
    change_line = "第三晚只剩一支班组时启用备用排程。" + (
        "调整 " + "、".join(changes) + "。" if changes else "已通知窗口全部保留。")
    skipped = sum(r[2] for r in data["reasons"] if r[0] != "OK")
    limits_line = (f"共排除 {skipped:,} 条 worker 记录。原因涉及作业或主机关联、运行区间和 GPU 字段，"
                   "明细见核算工作簿。GPU·分钟表示历史运行记录与窗口重叠的资源暴露量。"
                   "实际变更前须用当班作业清单复核。")
    return change_line, limits_line


def write_brief(path, data):
    from docx import Document
    from docx.enum.style import WD_STYLE_TYPE
    from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt, RGBColor

    doc = Document()
    doc.core_properties.title = "GPU 集群维护审批简报"
    doc.core_properties.author = "云平台运维团队"
    doc.core_properties.last_modified_by = "云平台运维团队"
    section = doc.sections[0]
    section.page_width, section.page_height = Inches(8.5), Inches(11)
    section.left_margin = section.right_margin = Inches(0.78)
    section.top_margin, section.bottom_margin = Inches(0.70), Inches(0.70)
    doc.styles.add_style("Brief Metadata", WD_STYLE_TYPE.PARAGRAPH)
    doc.styles.add_style("Brief Table", WD_STYLE_TYPE.PARAGRAPH)
    doc.styles.add_style("Brief Note", WD_STYLE_TYPE.PARAGRAPH)
    styles = [("Normal", 11, 0, 5), ("Title", 23, 0, 7), ("Heading 1", 13, 8, 4),
              ("Brief Metadata", 10, 0, 5), ("Brief Table", 10, 0, 0), ("Brief Note", 10, 5, 1)]
    for name, size, before, after in styles:
        style = doc.styles[name]
        style.font.name = "Microsoft YaHei"
        style.font.size = Pt(size)
        style.font.color.rgb = RGBColor.from_string("536474" if name == "Brief Note" else "000000")
        style.font.bold = name in {"Title", "Heading 1"}
        fonts = style._element.get_or_add_rPr().rFonts
        for key in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
            fonts.attrib.pop(qn(f"w:{key}"), None)
        for key in ("ascii", "hAnsi", "eastAsia", "cs"):
            fonts.set(qn(f"w:{key}"), "Microsoft YaHei")
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.line_spacing = 1.15
        if name == "Title":
            paragraph = style._element.get_or_add_pPr()
            for child in paragraph.findall(qn("w:pBdr")):
                paragraph.remove(child)

    def table(rows, widths, right_align=(), emphasis=()):
        t = doc.add_table(rows=len(rows), cols=len(widths))
        t.autofit = False
        t.alignment = WD_TABLE_ALIGNMENT.CENTER
        borders = OxmlElement("w:tblBorders")
        for side in ("top", "left", "bottom", "right", "insideH", "insideV"):
            border = OxmlElement(f"w:{side}")
            for attr, value in (("val", "single"), ("sz", "4"), ("color", "D9D9D9")):
                border.set(qn(f"w:{attr}"), value)
            borders.append(border)
        t._tbl.tblPr.append(borders)
        padding = OxmlElement("w:tblCellMar")
        for edge, twips in (("top", "45"), ("bottom", "45"), ("left", "125"), ("right", "125")):
            margin = OxmlElement(f"w:{edge}")
            margin.set(qn("w:w"), twips)
            margin.set(qn("w:type"), "dxa")
            padding.append(margin)
        t._tbl.tblPr.append(padding)
        t.rows[0]._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
        for i, width in enumerate(widths):
            t.columns[i].width = Inches(width)
        for j, row in enumerate(rows):
            for i, value in enumerate(row):
                cell = t.cell(j, i)
                cell.width = Inches(widths[i])
                cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                p = cell.paragraphs[0]
                p.style = doc.styles["Brief Table"]
                p.alignment = WD_ALIGN_PARAGRAPH.RIGHT if i in right_align else WD_ALIGN_PARAGRAPH.LEFT
                run = p.add_run(value)
                run.bold = j == 0 or (j, i) in emphasis
                if j == 0:
                    run.font.color.rgb = RGBColor(255, 255, 255)
                shade = OxmlElement("w:shd")
                fill = "203950" if j == 0 else "E5EFF8" if (j, i) in emphasis else "F3F7FA" if j % 2 == 0 else "FFFFFF"
                shade.set(qn("w:fill"), fill)
                cell._tc.get_or_add_tcPr().append(shade)

    impact_rows, schedule_rows = brief_tables(data)
    change_line, limits_line = brief_text(data)
    first = min(row[6] for row in data["schedule"])[:10]
    last = max(row[7] for row in data["schedule"])[:10]
    doc.add_paragraph(f"云平台运维团队  ·  变更审批会  ·  历史回放 {first} 至 {last}", "Brief Metadata")
    doc.add_paragraph("GPU 集群维护审批简报", "Title")
    doc.add_heading("审批建议", level=1)
    recommendation = doc.add_paragraph()
    recommendation.add_run("建议批准主排程及备用安排。 ").bold = True
    recommendation.add_run(change_line)
    doc.add_heading("主备方案影响", level=1)
    table(impact_rows, (1.06, 1.54, 1.54, 1.47, 1.33), right_align=(1, 2, 3, 4))
    doc.add_paragraph("受影响作业按方案去重。备用排程优先保留已通知窗口。", "Brief Note")
    doc.add_heading("维护窗口明细", level=1)
    changed_cells = {(i, col) for i, row in enumerate(schedule_rows) if i > 0 and row[4] == "1" for col in (3, 4)}
    table(schedule_rows, (0.61, 1.17, 2.06, 2.54, 0.56), right_align=(4,), emphasis=changed_cells)
    doc.add_heading("数据覆盖与执行复核", level=1)
    doc.add_paragraph(limits_line, "Brief Note")
    doc.save(path)


def verify_artifacts(folder, data):
    from read_evidence import read_submission, verify_links

    saved = read_submission(folder)
    check(saved["schedule"]["records"] == [list(map(str, r)) for r in data["schedule"]], "CSV 回读不一致")
    for table in data["contract"]["xlsx_tables"]:
        key = table["id"]
        records = data[key]
        if key == "summaries":
            records = [["主排程" if r[0] == "base" else "备用排程", *r[1:]] for r in records]
        normalized = [[None if x == "" else x for x in row] for row in records]
        check(saved["workbook"][key]["records"] == normalized, f"Excel {table['sheet']} 回读不一致")
    for table, rows in zip(data["contract"]["docx_tables"], brief_tables(data), strict=True):
        check(saved["brief"]["tables"][table["id"]]["records"] == rows[1:], "Word 表格回读不一致")
    sections = saved["brief"]["sections"]
    change, limits = brief_text(data)
    check(change in sections["审批建议"] and limits in sections["数据覆盖与执行复核"], "Word 正文回读不一致")
    verify_links(saved)


@contextmanager
def work_directory():
    temp_root = Path(tempfile.gettempdir()).resolve()
    stage = temp_root / f"gpu-reference-{uuid.uuid4().hex}"
    stage.mkdir()  # Inherit the temp directory ACL on Windows.
    try:
        yield stage
    finally:
        if stage.resolve().parent == temp_root:
            shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reference_answer",
                        help="A new destination directory. Existing results are preserved.")
    parser.add_argument("--review-dir", type=Path, help="Optional temporary workbook visual QA directory.")
    args = parser.parse_args()
    output = args.output_dir.resolve()
    check(not output.exists(), f"输出目录已存在，请用 --output-dir 指定新的目录：{output}")
    runtime = Path(os.environ.get("GPU_EVAL_RUNTIME", Path.home() / ".cache" / "codex-runtimes" /
                                   "codex-primary-runtime" / "dependencies"))
    node = runtime / "node" / "bin" / ("node.exe" if os.name == "nt" else "node")
    check(node.is_file(), "找不到 Codex Node 运行库。请设置 GPU_EVAL_RUNTIME。")
    if args.review_dir:
        args.review_dir.mkdir(parents=True, exist_ok=True)
    audit, workers = audit_workers(read_inputs())
    data = build_payload(audit, impacts(workers))
    print("Solved both scenarios and cross-checked exhaustive search.", flush=True)
    output.parent.mkdir(parents=True, exist_ok=True)
    with work_directory() as stage:
        artifacts = stage / "artifacts"
        artifacts.mkdir()
        evidence_json = stage / "evidence.json"
        evidence_json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        with (artifacts / "maintenance_schedule.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(CSV_COLUMNS)
            writer.writerows(data["schedule"])
        write_brief(artifacts / "change_brief.docx", data)
        command = [str(node), str(Path(__file__).with_name("export_workbook.mjs")),
                   str(evidence_json), str(artifacts / "impact_audit.xlsx"), str(runtime)]
        if args.review_dir:
            command.append(str(args.review_dir.resolve()))
        subprocess.run(command, check=True)
        verify_artifacts(artifacts, data)
        names = ("impact_audit.xlsx", "maintenance_schedule.csv", "change_brief.docx")
        output.mkdir()
        try:
            for name in names:
                shutil.copyfile(artifacts / name, output / name)
            verify_artifacts(output, data)
        except BaseException:
            for name in names:
                (output / name).unlink(missing_ok=True)
            output.rmdir()
            raise
    print(f"Verified and saved three reference files in {output}.")


if __name__ == "__main__":
    main()
