import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

const [dataPath, outputPath, runtime, reviewDir] = process.argv.slice(2);
const resolveFromRuntime = createRequire(path.join(runtime, "node", "package.json"));
const { Workbook, SpreadsheetFile } = await import(
  pathToFileURL(resolveFromRuntime.resolve("@oai/artifact-tool")).href
);
const data = JSON.parse(await fs.readFile(dataPath, "utf8"));
const book = Workbook.create();
const color = {
  navy: "#203950", ink: "#263846", muted: "#526477", blue: "#255980",
  line: "#D6E1EB", stripe: "#F3F7FA", accent: "#E5EFF8", white: "#FFFFFF",
};
const font = "Microsoft YaHei";
const title = "GPU 集群维护排程";
const tables = new Map(data.contract.xlsx_tables.map((spec) => [spec.id, spec]));
const tableHeaders = (key) => [...tables.get(key).columns, ...(tables.get(key).optional_columns ?? [])];

const details = [
  { name: "排程明细", key: "schedule", subtitle: "按设备查看获准窗口、局部影响及相对主排程的调整标记。",
    widths: [12, 10, 8, 8, 8, 10, 22, 22, 13, 14, 14, 13], numeric: [8, 9, 10, 11] },
  { name: "约束复核", key: "checks", subtitle: "对照各方案的实际峰值、最短间隔与违规记录。",
    widths: [13, 31, 30, 14, 14, 13], numeric: [3, 4] },
  { name: "候选影响", key: "candidates", subtitle: "每行对应一台设备和一个候选窗口。同一作业可能命中多个窗口。",
    widths: [10, 19, 10, 22, 22, 15, 16, 13, 14, 14], numeric: [5, 6, 7, 8, 9] },
  { name: "命中证据", key: "evidence", subtitle: "追溯选中窗口命中的 worker、时段与 GPU 暴露量。",
    widths: [12, 10, 19, 10, 14, 27, 13, 22, 22, 22, 22, 14, 11, 14], numeric: [6, 11, 12, 13] },
  { name: "核验汇总", key: "reasons", subtitle: "按规则顺序归入首个排除原因。每条记录只归入一项。",
    widths: [24, 44, 20], numeric: [2] },
  { name: "逐条核验", key: "audit", subtitle: "保留原始 worker ID、时间和首个排除原因，便于回查。",
    widths: [14, 26, 20, 22, 22, 13, 11, 23, 35, 68, 14], numeric: [5, 6, 10] },
  { name: "核验方法", key: "method", subtitle: "说明两套方案如何筛选、排序和独立复核最优性。",
    widths: [12, 46, 57, 51], numeric: [] },
].map((spec) => ({ ...spec, headers: tableHeaders(spec.key) }));

function range(sheet, row, col, rows, columns) {
  return sheet.getRangeByIndexes(row - 1, col - 1, rows, columns);
}

function line(sheet, row, span, text, style) {
  const target = range(sheet, row, 2, 1, span);
  target.merge();
  sheet.getCell(row - 1, 1).values = [[text]];
  target.format = {
    font: { name: font, size: style.size, bold: style.bold ?? false, color: style.color },
    verticalAlignment: "center", rowHeight: style.height, horizontalAlignment: "left", wrapText: true,
  };
  if (style.fill) target.format.fill = style.fill;
}

function layout(sheet, widths, heading, subtitle) {
  sheet.showGridLines = false;
  sheet.getCell(0, 0).format.columnWidth = 3.2;
  sheet.getRange("A1").format.rowHeight = 11;
  widths.forEach((width, index) => { sheet.getCell(0, index + 1).format.columnWidth = width; });
  line(sheet, 2, widths.length, heading, { size: 20, height: 36, color: color.navy, bold: true });
  line(sheet, 3, widths.length, subtitle, { size: 10, height: 24, color: color.muted });
}

function writeTable(sheet, start, headers, records, { numeric = [], banded = true, height = 21 } = {}) {
  const header = range(sheet, start, 2, 1, headers.length);
  header.values = [headers];
  header.format = { fill: color.navy, font: { name: font, size: 10, bold: true, color: color.white },
    horizontalAlignment: "center", verticalAlignment: "center", rowHeight: 29, wrapText: true };
  const body = range(sheet, start + 1, 2, records.length, headers.length);
  body.values = records.map((row) => row.map((v) => v === "" ? null : v));
  body.format = { font: { name: font, size: 10, color: color.ink }, verticalAlignment: "center", rowHeight: height };
  for (const col of numeric) {
    const numbers = range(sheet, start + 1, col + 2, records.length, 1);
    numbers.setNumberFormat("#,##0");
    numbers.format.horizontalAlignment = "center";
  }
  if (banded) {
    for (let i = 0; i < records.length; i += 2) range(sheet, start + 1 + i, 2, 1, headers.length).format.fill = color.stripe;
  }
  range(sheet, start + records.length, 2, 1, headers.length).format.borders = {
    bottom: { style: "thin", color: color.line },
  };
}

const summary = book.worksheets.add("方案汇总");
summary.tabColor = color.blue;
const starts = data.schedule.map((r) => r[6].slice(0, 10)).sort();
const ends = data.schedule.map((r) => r[7].slice(0, 10)).sort();
const changes = data.schedule.filter((row) => row[0] === "fallback" && row[11] === 1);
const baseWindows = new Map(data.schedule.filter((row) => row[0] === "base").map((row) => [row[1], row[5]]));
const changeText = changes.map((row) => `${row[1]} 从 ${baseWindows.get(row[1])} 调至 ${row[5]}`).join("、");
const headline = changes.length ? `第三晚减员时启用备用排程。${changeText}。` : "第三晚减员可保留全部已通知窗口。";
layout(summary, [18, 22, 22, 22, 20, 18], title,
  `历史回放  ${starts[0]} 至 ${ends.at(-1)}    ${baseWindows.size} 台维护设备`);
line(summary, 5, 6, headline, { size: 12, height: 34, color: color.navy, bold: true, fill: color.accent });
const displayed = data.summaries.map(([scenario, ...result]) => [scenario === "base" ? "主排程" : "备用排程", ...result]);
writeTable(summary, 7, tableHeaders("summaries"), displayed, { numeric: [1, 2, 3, 4, 5], height: 32 });
summary.getRange("B8:B9").format.font.bold = true;
line(summary, 11, 6, "核算与复核口径", { size: 12, height: 28, color: color.blue, bold: true });
const notes = [
  "影响指标按方案去重作业。GPU·分钟按每条命中 worker 的 GPU 数乘实际重叠分钟计算，区间左闭右开。",
  "主排程依次比较保护作业、全部作业和 GPU·分钟。备用排程首先最小化窗口变更数。目标值相同时按窗口 ID 序列决胜。",
  "穷举获准组合并检查班组并发、上联互斥和前置间隔，使用分组枚举复核。按窗口核对请查候选影响与命中证据。",
];
notes.forEach((text, i) => line(summary, 12 + i, 6, text, { size: 10, height: 23, color: color.ink }));

for (const spec of details) {
  const sheet = book.worksheets.add(spec.name);
  layout(sheet, spec.widths, spec.name, spec.subtitle);
  const height = spec.key === "method" ? 80 : 21;
  writeTable(sheet, 5, spec.headers, data[spec.key], { numeric: spec.numeric, banded: spec.key !== "audit", height });
  if (spec.key === "method") range(sheet, 6, 2, data.method.length, spec.headers.length).format.wrapText = true;
  sheet.freezePanes.freezeRows(5);
  if (spec.key === "schedule") {
    data.schedule.forEach((row, i) => {
      if (row[11] === 1) range(sheet, 6 + i, 2, 1, spec.headers.length).format.fill = color.accent;
    });
  }
}

book.recalculate();
const inspection = await book.inspect({ kind: "table", range: "方案汇总!B7:G9",
  include: "values,formulas", tableMaxRows: 3, tableMaxCols: 6 });
if (inspection.ndjson.includes("#REF!") || inspection.ndjson.includes("#VALUE!")) {
  throw new Error("Workbook summary contains an Excel error");
}
if (reviewDir) {
  const views = [["方案汇总", "A1:H15"], ...details.map((s) => [s.name,
    `A1:${String.fromCharCode(65 + s.headers.length)}${Math.min(9, data[s.key].length + 5)}`])];
  for (const [i, [sheetName, cells]] of views.entries()) {
    const preview = await book.render({ sheetName, range: cells, format: "png", scale: 1 });
    await fs.writeFile(path.join(reviewDir, `sheet-${i + 1}.png`), new Uint8Array(await preview.arrayBuffer()));
  }
}
await (await SpreadsheetFile.exportXlsx(book)).save(outputPath);
