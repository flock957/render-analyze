---
name: setup-env
description: 渲染性能分析环境初始化 - 自动安装所有依赖（trace_processor、playwright、chromium）
phase: 0
---

# 环境初始化

在开始渲染性能分析之前，确保所有工具和依赖已就绪。

## 依赖清单

| 工具 | 用途 | 来源 |
|------|------|------|
| Python 3.8+ | 脚本运行环境 | 系统自带 |
| requests | trace_processor HTTP 查询 | pip |
| playwright | 控制 Chromium 截图 | pip |
| Chromium | Perfetto UI 无头浏览器截图 | playwright install |
| trace_processor_shell | Perfetto SQL 查询引擎 | get.perfetto.dev |

## 执行

运行环境初始化脚本：

```bash
python3 scripts/setup_env.py
```

## 检查模式（仅检查不安装）

```bash
python3 scripts/setup_env.py --check-only
```

## 可选参数

- `--skip-browser`: 跳过 Chromium 安装（截图功能将不可用）
- `--skip-trace-processor`: 跳过 trace_processor 下载（如已手动安装）

## 输出

脚本输出 JSON 格式的环境状态：

```json
{
  "python_version": "3.12.0",
  "platform": "Linux",
  "packages": {"requests": true, "playwright": true},
  "chromium": true,
  "trace_processor": true,
  "all_ready": true
}
```

## 注意

- 截图功能需要 Chromium，如果安装失败，分析流程仍可正常运行，只是报告中不含截图
- trace_processor_shell 会自动下载到 `~/.local/share/perfetto/prebuilts/`
- 首次运行可能需要几分钟下载依赖
