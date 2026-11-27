"""Read the declared assessment evidence from XLSX, CSV and DOCX artifacts."""

import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = {"主排程": "base", "备用排程": "fallback", "base": "base", "fallback": "fallback"}
STAMP = "%Y-%m-%d %H:%M"


def config():
    return tomllib.loads((ROOT / "task.toml").read_text(encoding="utf-8"))


def load_table(sheet, spec, search_rows):
    required = spec["columns"]
    width = len(required)
    rows = sheet.iter_rows(max_row=search_rows, values_only=True)
    for header_row, cells in enumerate(rows, 1):
        for col in range(len(cells) - width + 1):
            if list(cells[col:col + width]) != required:
                continue
            names = list(required)
            for optional in spec.get("optional_columns", []):
                index = col + len(names)
                if index < len(cells) and cells[index] == optional:
                    names.append(optional)
            body = sheet.iter_rows(min_row=header_row + 1, min_col=col + 1,
                                   max_col=col + len(names), values_only=True)
            records = []
            for row in body:
                if all(v is None or v == "" for v in row):
                    break
                records.append(list(row))
            return {"columns": names, "records": records, "sheet": sheet.title,
                    "header_row": header_row, "first_column": col + 1}
    raise ValueError(f"{sheet.title}：前 {search_rows} 行未找到约定表头")


def read_workbook(file, contract, issues):
    from openpyxl import load_workbook

    book = load_workbook(file, data_only=True, read_only=True)
    tables = {}
    try:
        for spec in contract["xlsx_tables"]:
            name = spec["sheet"]
            try:
                tables[spec["id"]] = load_table(book[name], spec, contract["workbook_header_max_row"])
            except (ValueError, KeyError) as error:
                issues[f"xlsx/{name}"] = str(error)
    finally:
        book.close()
    return tables


def read_schedule(file, spec, issues):
    with file.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.reader(stream, strict=True)
        header = next(reader, [])
        records = [row for row in reader if row]
    if header != spec["columns"]:
        issues["csv/header"] = "表头与题面不一致"
    if len(records) != spec["data_rows"] or any(len(row) != len(header) for row in records):
        issues["csv/shape"] = "数据行数或行字段数量不符"
    if header == spec["columns"]:
        for number, row in enumerate(records, 2):
            if len(row) >= 8 and any(not valid_csv_time(v) for v in row[6:8]):
                issues.setdefault("csv/timestamps", []).append(number)
    return {"columns": header, "records": records}


def read_brief(file, contract, issues):
    from docx import Document

    doc = Document(file)
    tables = {}
    for spec in contract["docx_tables"]:
        matches = [t for t in doc.tables if t.rows and [c.text.strip() for c in t.rows[0].cells] == spec["columns"]]
        if len(matches) != 1:
            issues[f"docx/{spec['id']}"] = "未找到唯一的约定表格"
            continue
        width = len(spec["columns"])
        records = []
        for line, row in enumerate(matches[0].rows[1:], 2):
            before = getattr(row, "grid_cols_before", 0)
            values = [""] * before + [c.text.strip() for c in row.cells]
            if before or len(values) != width:
                issues.setdefault(f"docx/{spec['id']}/shape", []).append(line)
            records.append((values + [""] * width)[:width])
        tables[spec["id"]] = {"columns": spec["columns"], "records": records}
    known = set(contract["brief_headings"])
    sections = {}
    current = None
    for paragraph in doc.paragraphs:
        line = paragraph.text.strip()
        if line in known:
            current = line
            sections.setdefault(line, [])
        elif current and line:
            sections[current].append(line)
    for name in contract["brief_headings"]:
        if name not in sections:
            issues[f"docx/{name}"] = "缺少约定标题"
    return {"tables": tables, "sections": {k: "\n".join(v) for k, v in sections.items()}}


def read_submission(folder, strict=True):
    task = config()
    contract = task["evidence"]
    issues = {}
    sections = {}
    jobs = {
        "workbook": ("impact_audit.xlsx", read_workbook, contract),
        "schedule": ("maintenance_schedule.csv", read_schedule, task["deliverables"][1]),
        "brief": ("change_brief.docx", read_brief, contract),
    }
    for key, (name, reader, spec) in jobs.items():
        try:
            sections[key] = reader(folder / name, spec, issues)
        except (ImportError, PermissionError):
            raise
        except Exception as error:
            issues[name] = str(error)
    if strict and issues:
        raise ValueError("; ".join(f"{k}: {v}" for k, v in issues.items()))
    return {**sections, "issues": issues}


def excel_time(value):
    if isinstance(value, datetime):
        if value.second or value.microsecond or value.tzinfo:
            raise ValueError(f"Excel 时间超出题面分钟口径：{value}")
        return value.strftime(STAMP)
    for fmt in (STAMP, "%Y/%m/%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
        try:
            parsed = datetime.strptime(str(value).strip(), fmt)
        except ValueError:
            continue
        if parsed.second == 0:
            return parsed.strftime(STAMP)
    raise ValueError(f"Excel 时间无法解析为分钟：{value}")


def valid_csv_time(value):
    try:
        return datetime.strptime(value, STAMP).strftime(STAMP) == value
    except ValueError:
        return False


def count_value(value):
    text = str(value).strip()
    if not re.fullmatch(r"(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.0+)?", text):
        raise ValueError(f"指标无法解析为非负整数：{value}")
    return int(text.split(".")[0].replace(",", ""))


def parse_word_window(text, year):
    slot = re.fullmatch(r"(W\d{2})\s+(\d{2}/\d{2})\s+(\d{2}:\d{2})\s*[–-]\s*(?:(\d{2}/\d{2})\s+)?(\d{2}:\d{2})", text)
    if slot is None:
        raise ValueError(f"Word 窗口无法解析：{text}")
    window, date, begin, end_date, end = slot.groups()
    result = [datetime.strptime(f"{year}/{d} {hour}", "%Y/%m/%d %H:%M").strftime(STAMP)
              for d, hour in ((date, begin), (end_date or date, end))]
    return (window, *result)


def verify_links(data):
    """Check the delivered links among the workbook, CSV and Word.

    This is an evidence consistency check. It does not decide feasibility, optimality or prose quality.
    """
    book, csv_data, brief = (data[k] for k in ("workbook", "schedule", "brief"))
    schedule = {(r[0], r[1]): [*r[:8], *map(count_value, r[8:12])] for r in csv_data["records"]}
    if len(schedule) != len(csv_data["records"]):
        raise ValueError("CSV 中方案和设备键重复")
    excel_schedule = {}
    for row in book["schedule"]["records"]:
        scenario = SCENARIOS[row[0]]
        key = (scenario, row[1])
        if key in excel_schedule:
            raise ValueError(f"Excel 中排程键重复：{key}")
        excel_schedule[key] = [scenario, *map(str, row[1:6]),
                               *map(excel_time, row[6:8]), *map(count_value, row[8:12])]
    if schedule != excel_schedule:
        raise ValueError("Excel 与 CSV 的排程或局部指标不一致")

    excel_summaries = book["summaries"]["records"]
    summaries = {SCENARIOS[r[0]]: list(map(count_value, r[1:5])) for r in excel_summaries}
    brief_metrics = brief["tables"]["brief_metrics"]["records"]
    word_metrics = {SCENARIOS[r[0]]: list(map(count_value, r[1:5])) for r in brief_metrics}
    if len(brief_metrics) != 2 or len(excel_summaries) != 2 or summaries != word_metrics or set(summaries) != {"base", "fallback"}:
        raise ValueError("Word 与 Excel 的方案级指标不一致")

    seen = set()
    year = config()["completion"]["base_latest_end"][:4]
    for device, path, main, backup, changed in brief["tables"]["brief_schedule"]["records"]:
        for scenario, window in (("base", main), ("fallback", backup)):
            key = (scenario, device)
            if key in seen or key not in schedule:
                raise ValueError(f"Word 窗口键缺失或重复：{key}")
            seen.add(key)
            row = schedule[key]
            if path != "/".join(row[2:5]) or parse_word_window(window, year) != tuple(row[5:8]):
                raise ValueError(f"Word 与 CSV 的窗口不一致：{key}")
        if count_value(changed) != schedule["fallback", device][11]:
            raise ValueError(f"Word 与 CSV 的变更标记不一致：{device}")
    if seen != set(schedule):
        raise ValueError("Word 存在未列出的排程设备")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--verify-reference", action="store_true", help="Recompute the reference and compare all evidence.")
    parser.add_argument("--brief-pdf", type=Path, help="PDF rendered from this DOCX in the grading environment.")
    args = parser.parse_args()
    evidence = read_submission(args.folder, strict=False)
    status = {"structure_issues": evidence["issues"], "workbook_rows": {
        key: len(value["records"]) for key, value in evidence.get("workbook", {}).items()
    }, "csv_rows": len(evidence.get("schedule", {}).get("records", [])),
        "Word_tables": list(evidence.get("brief", {}).get("tables", {})),
        "pending_judgment": ["最优性说明", "审批建议和数据限制", "有歧义的时间抄录证据"]}
    if not evidence["issues"]:
        try:
            verify_links(evidence)
            status["cross_file_consistency"] = "pass"
        except (ValueError, IndexError, KeyError, TypeError) as error:
            status["cross_file_consistency"] = str(error)
    if args.brief_pdf:
        from pypdf import PdfReader

        pages = len(PdfReader(args.brief_pdf).pages)
        status["Word_pages"] = pages
        status["Word_page_cap_pass"] = pages <= config()["deliverables"][2]["max_pages"]
    else:
        status["Word_pages"] = "awaiting render"
    if args.verify_reference:
        from solve import audit_workers, build_payload, impacts, read_inputs, verify_artifacts

        audit, workers = audit_workers(read_inputs())
        verify_artifacts(args.folder, build_payload(audit, impacts(workers)))
        status["reference_recompute"] = "pass"
    print(json.dumps(status, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
