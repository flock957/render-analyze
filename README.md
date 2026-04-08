# Trace Screenshot Skill

Perfetto trace 截图工具，可作为独立 skill 接入任何 workflow。

## 目录结构

```
trace_screenshot_skill/
├── capture-trace-screenshot.md    # Skill 描述文件（放入 workflow 的 skill 目录）
├── scripts/
│   └── capture_trace_screenshot.py  # 截图脚本
├── requirements.txt               # Python 依赖
├── setup.bat                      # Windows 环境安装
├── setup.sh                       # Linux/Mac 环境安装
├── TRACE_SCREENSHOT_GUIDE.md      # 完整使用指南（给 AI 看的）
└── README.md                      # 本文件
```

## 安装

```bash
# Windows
setup.bat

# Linux / Mac
./setup.sh
```

安装完成后会在当前目录生成 `.venv/` 虚拟环境。

## 接入你的 Workflow

### 1. 复制文件

把 `capture-trace-screenshot.md` 放到你的 skill 目录：
```
your_workflow/
├── your-workflow.md
├── capture-trace-screenshot.md    # ← 复制过来
└── scripts/
    └── capture_trace_screenshot.py  # ← 复制过来
```

### 2. 在 workflow 的 phases 中添加截图阶段

```yaml
phases:
  # ... 你的分析阶段 ...
  - key: screenshot
    label: 截图（可选）
    desc: 捕获 Perfetto UI 问题片段截图
    output: /your/output/screenshots/screenshot_manifest.json
  - key: report
    label: 生成报告
    desc: 输出报告
    output: /your/output/report.html
```

### 3. 在 workflow 正文中添加调用

```markdown
## 截图阶段（可选）

python3 /path/to/scripts/capture_trace_screenshot.py \
    --trace $TRACE_FILE \
    --analysis-dir /your/output \
    --output-dir /your/output/screenshots

如果输出中 skipped_reason 不为空，跳过截图继续下一步。
```

## 输入要求

分析结果 JSON 文件需包含：
```json
{
    "has_issue": true,
    "severity": "high",
    "start_time": 187654000000000,
    "end_time": 187656500000000
}
```

## 输出

```
screenshots/
├── 00_全局概览.png
├── 01_主线程状态.png
├── ...
└── screenshot_manifest.json
```

## 详细文档

见 `TRACE_SCREENSHOT_GUIDE.md`，可直接给 AI 作为参考文档使用。
