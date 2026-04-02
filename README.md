# Android Render Performance Analyzer

Perfetto trace 自动化渲染性能分析工具。输入一个 Android trace 文件，自动输出包含 Top 5 卡顿问题 + Perfetto 截图 + Android Framework 源码级根因分析的 HTML 报告。

## 使用说明

### 第一步：克隆仓库

```bash
git clone https://github.com/flock957/render-analyze.git
cd render-analyze
```

### 第二步：安装依赖（首次使用）

```bash
python3 scripts/setup_env.py
```

自动安装：
- `requests` — trace_processor HTTP 查询
- `playwright` + `Chromium` — Perfetto UI 无头浏览器截图
- `trace_processor_shell` — Perfetto SQL 查询引擎（从 get.perfetto.dev 下载）

> 如果 playwright/chromium 安装失败，分析仍可正常运行，只是报告中不含截图。

### 第三步：运行分析

```bash
python3 scripts/run_analysis.py --trace /path/to/your.perfetto-trace
```

等待执行完成，报告输出到 `./render_output/render_report.html`。

### 常用参数

```bash
# 自定义输出目录
python3 scripts/run_analysis.py --trace ./my_trace.perfetto-trace --output-dir ./my_output

# 只看 Top 3 问题
python3 scripts/run_analysis.py --trace ./my_trace.perfetto-trace --top-n 3

# 跳过截图（加速，省去 playwright 依赖）
python3 scripts/run_analysis.py --trace ./my_trace.perfetto-trace --skip-screenshot

# 非首次运行跳过环境检查
python3 scripts/run_analysis.py --trace ./my_trace.perfetto-trace --skip-setup
```

### 给 AI Agent 使用

如果你使用 AI 编程助手（Claude Code、OpenHands、Cursor 等），只需对 AI 说：

> 按照 render-performance-workflow.md 分析 trace 文件 /path/to/xxx.perfetto-trace

或直接让 AI 执行：

```
python3 scripts/run_analysis.py --trace /path/to/xxx.perfetto-trace
```

## 工作原理

```
trace 文件
    │
    ▼
Phase 0: setup_env.py          ← 安装依赖
Phase 1: trace_processor_init   ← 启动 HTTP 查询服务（端口 9001）
Phase 2: find_foreground_process ← 识别前台 App 进程
Phase 3: init_render_jank_metric ← 初始化 FrameTimeline 分析表
Phase 4: analyze_jank_types      ← 统计 Jank 类型分布
Phase 5: analyze_app_jank        ← App 侧分析（doFrame 超时、Buffer 塞满）
Phase 6: analyze_sf_jank         ← SF 侧分析（Display HAL、Prediction Error、丢帧）
Phase 7: capture_trace_screenshot← Perfetto UI 截图（可选）
Phase 8: trace_processor_cleanup ← 停止查询服务
Phase 9: render_report_generator ← 生成 HTML 报告
    │
    ▼
render_report.html  ← 单文件，含内嵌截图，浏览器直接打开
```

## Trace 文件要求

- 格式：`.perfetto-trace`（Perfetto 原生格式）
- 系统：**Android 12+**
- 必须包含 **FrameTimeline** 数据

### 如何采集 trace

**方式 1：perfetto 命令行**

```bash
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

adb pull /data/misc/perfetto-traces/trace.perfetto-trace ./
```

**方式 2：系统自带**

Settings → Developer options → System Tracing → Categories 勾选 **Frame Timeline**，录制 10-30 秒包含卡顿的场景。

## 报告内容

| 章节 | 内容 |
|------|------|
| 概览 | 总帧数、Jank 帧数、Jank 率、类型分布表 |
| Top 5 问题 | 每个问题的关键指标 + Perfetto 截图 + Framework 源码根因分析 |
| 根因分析 | AOSP 调用链、源码文件引用（Choreographer.java / HWComposer.cpp 等）、优化建议 |

### 支持的 Jank 类型

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
render-analyze/
├── README.md                           # 本文件
├── render-performance-workflow.md      # 工作流定义（10 phases）
├── *.md                               # 各阶段 Skill 说明文档
└── scripts/
    ├── run_analysis.py                # 一键入口（推荐）
    ├── setup_env.py                   # 环境初始化
    ├── trace_processor_init.py        # 启动查询服务
    ├── trace_processor_cleanup.py     # 停止查询服务
    ├── find_foreground_process.py     # 前台进程识别
    ├── tp_query.py                    # HTTP 查询工具
    ├── init_render_jank_metric.py     # Jank 指标初始化
    ├── analyze_jank_types.py          # Jank 类型分布
    ├── analyze_app_jank.py            # 应用层分析
    ├── analyze_sf_jank.py             # SF 层分析
    ├── capture_trace_screenshot.py    # Perfetto 截图
    └── render_report_generator.py     # 报告生成
```

## 环境要求

- Python 3.8+
- Linux（trace_processor_shell 为 Linux ELF 二进制）
- 网络（首次下载 trace_processor + Perfetto UI 截图需要访问外网）

所有依赖通过 `python3 scripts/setup_env.py` 一键安装。

## License

MIT
