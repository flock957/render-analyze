# Android Render Performance Analyzer

Perfetto trace 自动化渲染性能分析工具。输入一个 Android trace 文件，自动输出包含 Top 5 卡顿问题 + Perfetto 截图 + Android Framework 源码级根因分析的 HTML 报告。

## 快速开始

### 一键运行（推荐）

```bash
python3 scripts/run_analysis.py --trace /path/to/your.perfetto-trace
```

一条命令自动完成全部 10 个阶段：环境安装 → 加载 trace → 分析 Jank → 截图 → 生成报告。

可选参数：
```bash
# 自定义输出目录和 Top N
python3 scripts/run_analysis.py --trace /path/to/trace --output-dir ./my_output --top-n 3

# 跳过截图（加速分析）
python3 scripts/run_analysis.py --trace /path/to/trace --skip-screenshot

# 跳过环境安装（已安装过）
python3 scripts/run_analysis.py --trace /path/to/trace --skip-setup
```

### 给 AI Agent

如果你使用 AI 编程助手（Claude Code、OpenHands、Cursor 等），只需对 AI 说：

> 运行 `python3 scripts/run_analysis.py --trace /path/to/your.perfetto-trace` 分析这个 trace

AI 只需执行这一条命令，无需理解中间流程。

如需了解每个阶段的细节，参考 `render-performance-workflow.md`。

### 手动分步执行

如需手动控制每个阶段，参考 `render-performance-workflow.md` 中的详细步骤。

报告输出到 `/workspace/render_output/render_report.html`，单文件含内嵌截图，可直接用浏览器打开或分享。

## Trace 文件要求

- 格式：`.perfetto-trace`（Perfetto 原生格式）
- 系统：Android 12+
- 必须包含 **FrameTimeline** 数据（采集时需要开启 `android.surfaceflinger.frametimeline` category）
- 建议采集时长 10-30 秒，包含明显卡顿场景

### 如何采集带 FrameTimeline 的 trace

```bash
# 方式1：使用 perfetto 命令行
adb shell perfetto \
  -c - --txt \
  -o /data/misc/perfetto-traces/trace.perfetto-trace \
  <<EOF
buffers: { size_kb: 63488 fill_policy: RING_BUFFER }
data_sources: { config { name: "linux.ftrace" ftrace_config {
  ftrace_events: "sched/sched_switch"
  ftrace_events: "power/suspend_resume"
  ftrace_events: "power/gpu_frequency"
}}}
data_sources: { config { name: "android.surfaceflinger.frametimeline" }}
duration_ms: 15000
EOF

# 方式2：使用系统 System Tracing app
# Settings → Developer options → System Tracing → Categories 中勾选 "Frame Timeline"

# 导出到电脑
adb pull /data/misc/perfetto-traces/trace.perfetto-trace ./
```

## 报告内容

生成的 HTML 报告包含：

1. **概览** — 总帧数、Jank 帧数、Jank 率、类型分布表
2. **Top 5 重点问题**，每个问题含：
   - 关键指标数据（超时帧数、阻塞次数、presentFence 等待时间等）
   - Perfetto UI 截图（聚焦到故障帧，显示相关线程轨道）
   - Android Framework 源码级根因分析（调用链 + AOSP 源码引用 + 优化建议）

### 支持分析的 Jank 类型

| 类型 | 层级 | 说明 |
|------|------|------|
| App Deadline Missed | App | 应用侧帧超时（doFrame > 16.6ms） |
| Buffer Stuffing | App | BufferQueue 被塞满 |
| Display HAL | System | 显示硬件 presentFence 延迟 |
| SF CPU Deadline Missed | System | SurfaceFlinger 主线程合成超时 |
| SF GPU Deadline Missed | System | GPU 合成 fence 超时 |
| Prediction Error | System | VSync 预测模型错误 |
| SF Stuffing | System | SurfaceFlinger 帧堆积 |
| Dropped Frame | System | 帧被丢弃 |

## 目录结构

```
render_skills/
├── README.md                           # 本文件
├── render-performance-workflow.md      # 主工作流定义（10 phases）
│
├── setup-env.md                        # Skill: 环境初始化
├── init-render-jank-metric.md          # Skill: Jank 指标初始化
├── analyze-jank-types.md              # Skill: Jank 类型识别
├── analyze-app-jank.md               # Skill: 应用层分析
├── analyze-sf-jank.md                # Skill: SF 层分析
├── capture-trace-screenshot.md       # Skill: Perfetto 截图
├── generate-report.md                # Skill: 报告生成
│
└── scripts/
    ├── run_analysis.py                # 一键运行全部分析（推荐入口）
    ├── setup_env.py                   # 环境初始化（安装所有依赖）
    ├── trace_processor_init.py        # 启动 trace_processor 查询服务
    ├── find_foreground_process.py     # 识别前台进程
    ├── tp_query.py                    # trace_processor HTTP 查询工具
    ├── init_render_jank_metric.py     # 初始化 Jank 分析表
    ├── analyze_jank_types.py          # Jank 类型分布统计
    ├── analyze_app_jank.py            # 应用层 Jank 分析
    ├── analyze_sf_jank.py             # SF 层 Jank 分析
    ├── capture_trace_screenshot.py    # Perfetto UI 自动截图
    ├── render_report_generator.py     # HTML 报告生成
    └── trace_processor_cleanup.py     # 停止 trace_processor
```

## 环境要求

| 依赖 | 用途 | 安装方式 |
|------|------|---------|
| Python 3.8+ | 运行脚本 | 系统自带 |
| requests | trace_processor 查询 | `setup_env.py` 自动安装 |
| playwright | 浏览器自动化 | `setup_env.py` 自动安装 |
| Chromium | Perfetto UI 截图 | `setup_env.py` 自动安装 |
| trace_processor_shell | Perfetto SQL 引擎 | `setup_env.py` 自动安装 |

运行 `python3 scripts/setup_env.py` 即可一键安装全部依赖。

截图功能依赖 playwright + chromium，如果安装失败，分析流程仍可正常运行，报告中会标注截图不可用。

## License

MIT
