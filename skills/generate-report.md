---
name: generate-report
description: Phase 3 - 生成 HTML 渲染分析报告（内嵌截图）
type: skill
script: scripts/generate_report.py
---

# Generate Report

将分析结果 + 截图生成独立 HTML 报告（Phase 3，run_workflow.py 的最后一阶段）。

## 使用

```bash
python3 scripts/generate_report.py \
  --analysis-dir /path/to/output \
  --output /path/to/render_report.html
```

`python3` 没有特殊依赖（标准库即可），不需要 perfetto / playwright。

## 报告内容
- 概览统计（总帧数、jank 帧数、jank 率）
- Jank 类型分布表
- Top-5 问题详情 + 内嵌 base64 截图（概览 + 详情对比）
- 线程映射信息
