---
name: generate-report
type: knowledge
triggers:
  - 生成报告
  - report
  - 渲染报告
  - render report
  - 分析报告
---

# 渲染性能分析报告生成

汇总所有分析结果和截图，生成 HTML 渲染性能报告。报告聚焦 Top 5 最严重问题，每个问题附带 Perfetto 截图和 Android Framework 源码级根因分析。

## 脚本

```bash
python3 scripts/render_report_generator.py \
  --output-dir <分析输出目录> \
  --top-n 5
```

## 参数

- `--output-dir`: 包含分析 JSON 和截图的目录（默认 /workspace/render_output）
- `--top-n`: 报告中展示的重点问题数量（默认 5）

## 输入文件

报告生成器从 output-dir 读取以下文件：

| 文件 | 来源 | 用途 |
|------|------|------|
| `jank_types.json` | analyze_jank_types.py | 概览统计、Jank 类型分布 |
| `app_jank.json` | analyze_app_jank.py | 应用层问题详情 |
| `sf_jank.json` | analyze_sf_jank.py | SF 层问题详情 |
| `screenshots/screenshot_manifest.json` | capture_trace_screenshot.py | 截图清单 |
| `screenshots/*.png` | capture_trace_screenshot.py | 截图文件 |

## 报告结构

### 1. 概览
- 总帧数、Jank 帧数、Jank 率
- Jank 类型分布表（Top 8 类型，按帧数排序）

### 2. Top N 重点问题分析

每个问题包含：

#### a. 问题数据
- 问题类型、严重程度、影响帧数、最长耗时
- 关键指标（doFrame 超时数、dequeueBuffer 阻塞数、presentFence 等待时间等）
- Top 3 问题帧列表

#### b. Perfetto 截图
- 嵌入 base64 PNG，点击可放大
- 截图聚焦到故障帧时间范围和相关线程轨道

#### c. Android Framework 根因分析

每种 Jank 类型对应一套完整的源码级分析：

| Jank 类型 | 分析框架源码 |
|-----------|-------------|
| App Deadline Missed | Choreographer.java → ViewRootImpl.java → ThreadedRenderer.java |
| Buffer Stuffing | BufferQueueProducer.cpp → BufferLayerConsumer.cpp |
| Display HAL | HWComposer.cpp → SurfaceFlinger.cpp (presentFence) |
| SF CPU | SurfaceFlinger.cpp (onMessageRefresh → handleComposition) |
| Prediction Error | VSyncPredictor.cpp |
| SF Stuffing | FrameTimeline.cpp |
| Dropped Frame | FrameTimeline.cpp → SurfaceFlinger.cpp (handlePageFlip) |

每种分析包含：
- **调用链路**: 从入口函数到问题点的完整路径
- **源码引用**: AOSP 文件路径 + 关键逻辑说明
- **可能根因**: 基于 Framework 内部机制的根因推断
- **优化建议**: 具体可操作的优化方向

## 输出

- `render_report.html`: 完整 HTML 报告（含内嵌截图，单文件可直接分享）

## 报告样式

- 暗色主题（#0d1117 背景）
- 响应式布局，支持移动端查看
- 截图点击放大（CSS toggle）
- 源码引用高亮（monospace 字体 + 蓝色标注）

## Top N 问题选择逻辑

1. 从 app_jank 和 sf_jank 中提取所有问题类型
2. 每种类型取最严重的 Top 1 帧
3. 按严重度（high > medium > low）、耗时降序排序
4. 取 Top N

## 后续优化方向

- 支持 PDF 导出
- 增加火焰图/时间线可视化（SVG）
- 对比分析（两个 trace 的前后对比）
- 自动关联 App 源码（基于 stack trace）
