# GPU 集群维护排程与训练任务影响审计


本项目使用阿里云匿名生产集群记录，评测 Agent 能否核算交换机维护对训练作业的影响，并为变更审批会提交全局最优的主排程和备用排程。[数据发布仓库](https://github.com/alibaba/alibaba-lingjun-dataset-2023)

## 任务入口

以项目目录为路径起点。`instruction.md` 是正式交办，`task.toml` 是随题的规则附表，载明设备与窗口、排除顺序和交付字段。三份原始输入是 `inputs/job.csv`、`inputs/worker.csv` 和 `inputs/topo.csv`。来源、固定提交版本、下载时间和 SHA-256 记录在 `manifest.json`。

## 完成交付

被测 Agent 在 `outputs/` 下交付 3 个文件。三份文件须能打开，排程和指标相互一致，并提供可核验的全局最优主备排程。

| 路径 | 交付标准 |
|---|---|
| `outputs/impact_audit.xlsx` | Excel 影响核算。含逐 worker 核验、排除汇总、候选窗口影响、命中证据、完整排程、方案汇总、约束复核及核验方法。表头按 `task.toml`。 |
| `outputs/maintenance_schedule.csv` | UTF-8，表头外 20 条数据行，先 `base` 后 `fallback`，各按设备编号排序。列序以正式题面及任务配置为准。 |
| `outputs/change_brief.docx` | 最多 2 页。含审批建议和数据限制正文，以及 `task.toml` 规定的主备方案影响与维护窗口明细两张表。 |

主排程最晚于 `2023-07-29 23:00` 完成。备用排程最晚于 `2023-07-30 00:00` 完成。两个截止点均为历史回放时间轴上的窗口结束时间。

## 出题和盲测

为每个模型准备独立目录和新会话。考生包只放 `instruction.md`、规则附表 `task.toml` 和 `inputs/` 下的三份原始 CSV，共五份文件。保留相对路径并确保模型能将交付物写入 `outputs/`。

启动语：请按 `instruction.md` 和 `task.toml` 完成任务，使用 `inputs/` 中的三份原始数据，将三份交付物保存到 `outputs/`。

`reference_answer/` 是标准参考输出。`reference_code/solve.py` 复算并生成三份文件。`reference_code/grade_submission.py` 从参评提交读取证据，并用固定原始数据核对。`rubric.json`、`task_draft.md`、参考脚本和参考结果均由出题方留存。

在项目目录的 PowerShell 中可复跑到一个新目录，并对已有参考文件进行完整回读校验。

```powershell
$runtime = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies'
& (Join-Path $runtime 'python/python.exe') -B .\reference_code\solve.py --output-dir .\reference_check
& (Join-Path $runtime 'python/python.exe') -B .\reference_code\read_evidence.py .\reference_answer --verify-reference
```

## 对单个模型评分

将模型的三份原始交付文件保存在其独立的 `outputs/` 下。输出目录和报告路径按实际运行替换。可用同一版本的 Word 或 LibreOffice 将该模型的简报导出 PDF，保存至评分侧目录，并在评分时传给脚本。页数和正文由同一份原始 DOCX 取证。未提供 PDF 时，O04 页数项进入复核。

```powershell
$runtime = Join-Path $env:USERPROFILE '.cache/codex-runtimes/codex-primary-runtime/dependencies'
$py = Join-Path $runtime 'python/python.exe'
& $py -B .\reference_code\grade_submission.py '.\模型评测\GPT-6.1Sol\outputs' --brief-pdf '.\模型评测\GPT-6.1Sol\run_evidence\change_brief.pdf' --report .\score-auto-new.json
```

报告逐条列出 30 个考点，含规则、证据文件及表或章节、应有值、提交值、分数和待复核状态。大表展示差异样例，保留核对单位的完整计数。总量与方案目标用原始数据按提交排程复算。跨文件一致性另算。CSV 无法辨识排程时，可用工作簿排程继续判定业务项。

| 考点 | 从提交中取证 |
|---|---|
| D01–D07 | Excel 逐条核验、核验汇总和命中证据。 |
| I01–I07 | Excel 候选影响、命中证据、方案汇总，结合提交的排程复算。 |
| S01–S09 | CSV 或 Excel 排程、Excel 约束复核和核验方法。 |
| O01–O07 | 三份交付的文件结构、CSV 原值、Word 两张表及审批建议和限制，另核对同一 DOCX 的渲染页数。 |

带 PDF、可正常读取的完整提交中，脚本可判 96 分。由评分人按报告中的原文复核 S09.2 的可行性及目标方法、S09.3 的最优性验证、O05.1 的审批建议、O05.2 的数据边界。每项按 rubric 给 0 或 1 分。Excel 公式应以可信的重算结果取证。若自动读取器漏读了已有证据，评分人可核查原文件，以 `"basis": "读取或取证限制"` 补充该分项的人工记录，报告同时保留其自动得分。

将所有待复核分项记录成 UTF-8 JSON。下面仅演示一条记录的字段。

```json
{
  "O05.1": {
    "score": 1,
    "reviewer": "评分人编号",
    "evidence": "change_brief.docx / 审批建议 / 第 1 段",
    "reason": "说明了主排程的班组条件及第三晚减员时切换到备用排程的条件。"
  }
}
```

