---
name: generate-report
description: Phase 3 - 生成 HTML 渲染分析报告（内嵌截图 + 元数据 + 根因分析）
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

`python3` 没有特殊依赖（标准库即可），不需要 perfetto / playwright / Pillow。

## 报告结构

### 概览区
- 总帧数、jank 帧数、jank 率、jank 类型数
- Jank 类型分布表（按帧数排序，含平均耗时和严重程度标记）

### Top-5 问题区（每个问题一个卡片）

| 区块 | 内容 | 数据来源 |
|------|------|----------|
| Top 问题帧表 | 该类型 top-3 帧的 Frame ID / 耗时 / 类型 | `app_jank.json → type_details` |
| **问题帧元数据** | 问题类型、帧号、捷区范围、目标时刻、焦点轨道、命中关键词、问题描述、截图逻辑 | `app_jank.json → top_frames` |
| **证据 slices 表** | Top-5 slice 名 + 线程 + 耗时 + 起点 ts | `app_jank.json → evidence_slices` |
| **全局图 + 细节图** | 竖屏长图，base64 内嵌，点击放大 | `screenshots/` |
| **截图复盘说明** | 蓝色 callout，说明为什么截取这个区域 | `screenshot_reasoning` |
| **Framework 根因分析** | 调用链 → 源码引用 → Trace 诊断指南 → 根因 → 优化建议 | `FRAMEWORK_KB` 硬编码 |

## Framework 根因分析（FRAMEWORK_KB）

**完全硬编码在 generate_report.py 中**，不调用任何 LLM。6 种 jank 类型各有：

| 字段 | 说明 |
|------|------|
| `cn_name` | 中文名（如"应用侧超时"） |
| `call_chain` | AOSP 调用链路步骤（6-9 步） |
| `source_refs` | 源码文件引用 + 逐段说明 |
| `trace_guide` | 在 Perfetto 中的诊断步骤 |
| `root_causes` | 可能的根因列表 |
| `optimizations` | 优化建议列表 |

覆盖的类型：
1. **App Deadline Missed** — Choreographer.doFrame → performTraversals → ThreadedRenderer
2. **Display HAL** — HWComposer.presentAndGetReleaseFences → DRM/KMS
3. **SurfaceFlinger CPU Deadline Missed** — onMessageRefresh → composite
4. **Buffer Stuffing** — BufferQueueProducer.dequeueBuffer 阻塞
5. **Prediction Error** — VSyncPredictor 线性回归偏差
6. **SurfaceFlinger Scheduling** — SF 线程 Runnable 等待 CPU

**匹配逻辑：** `_find_kb(jank_type)` 先精确匹配，再子串匹配。复合类型
（如 `App Deadline Missed, Buffer Stuffing`）匹配第一个命中的子类型。

## 可复现性

同一组 JSON + 同一组截图 → **100% 相同的 HTML 报告**。
不同时间运行只有"生成时间"一行不同。
