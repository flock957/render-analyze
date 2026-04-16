---
name: render-performance-workflow
description: 完整渲染 Jank 分析工作流 - 从 trace 分析到竖屏长图截图报告（v4.2 portrait-longshot）
type: repo
version: 4.2
agent: CodeActAgent

# ===== 动态输入表单（HiClaw 平台用）=====
input_form:
  - key: trace_path
    type: file
    label: Trace 文件
    placeholder: /workspace/trace.perfetto-trace
    accept: .perfetto-trace,.pb,.pftrace
    required: true
  - key: focus
    type: select
    label: 分析重点
    default: full
    options:
      - label: 完整分析
        value: full
        desc: 分析 + 截图 + 报告
      - label: 快速分析（不截图）
        value: fast
        desc: 分析 + 报告，跳过截图
  - key: top_n
    type: number
    label: Top N 问题数
    default: 5
    min: 1
    max: 20
    placeholder: 报告中展示的最严重问题数量
  - key: extra
    type: text
    label: 补充说明
    placeholder: 可选，如关注某个场景或进程...
    required: false

submit_message: |
  Execute skill: render-performance-workflow. Follow the skill instructions.

  **Trace file path**: {{trace_path}}
  **Analysis focus**: {{focus}}
  **Top N issues**: {{top_n}}
  {{extra}}

  Please execute the render performance analysis workflow:
  1. Setup environment (perfetto, playwright, chromium)
  2. Analyze jank frames from the trace (one SQL-driven pass)
  3. Capture Perfetto UI screenshots for the top {{top_n}} issues
  4. Generate the HTML render report

phases:
  - key: setup
    label: 环境初始化
    desc: 安装 perfetto / playwright / chromium
    output: null
  - key: analyze
    label: Jank 分析
    desc: SQL 驱动的目标进程 + jank 帧 + 线程映射分析
    output: render_analysis_output/app_jank.json
  - key: screenshot
    label: Perfetto 截图
    desc: 为 Top N 问题抓取全局图 + 局部细节图（竖屏长图 2144×3196 像素）
    output: render_analysis_output/screenshots/screenshot_manifest.json
    optional: true
  - key: report
    label: 生成报告
    desc: HTML 渲染性能报告（含嵌入截图 + Framework 根因分析）
    output: render_analysis_output/render_report.html

reports:
  - label: 渲染性能报告
    file: render_analysis_output/render_report.html
---

# 渲染 Jank 分析工作流 (v4.1 portrait-longshot)

## 概述

自动化分析 Android 渲染 jank 问题的完整流水线。**分析逻辑全程无 LLM 参与**，
所有脚本位于 `scripts/` 目录，任何人 clone 后拿到的报告结构、根因分析、截图
逻辑完全一致。

**两种使用方式**:
- **HiClaw 平台**: LLM 读本 skill 文件，按下面的"Agent 执行指南"调度 4 个 phase
- **Standalone**: `python3 scripts/run_workflow.py --trace ... --output-dir ...` 一条命令跑完

## Agent 执行指南（HiClaw 平台用）

### 严格约束

1. **禁止自行编写 SQL 查询或分析代码** — 只能调用指定脚本
2. **禁止修改已有脚本**
3. **trace 路径来自输入表单的 `{{trace_path}}`**，不要猜测
4. **遇到脚本错误立即停止并报告**

### 内网部署注意

`capture_screenshots.py` 默认访问公网 `https://ui.perfetto.dev`。内网部署
（如荣耀内网）无法访问公网时，通过环境变量切到内网自部署的 Perfetto UI:

```bash
export PERFETTO_UI_URL=https://perfetto.rnd.hihonor.com/
```

外网环境不用设置这个变量，脚本默认就用 `ui.perfetto.dev`。内网环境如果
没设置会导致 `page.goto` 超时，最终截到的是 Perfetto UI 加载初始化时的
空白画面（trace 根本没加载进去）。如果你所在的 OpenHands 部署把这个变量
注入到 sandbox，所有命令会自动继承，不需要每条命令手动 export。

### 🔴 命令模板（必须遵守，否则进度条无法点亮、下载按钮无法出现）

**每个阶段的脚本调用必须使用如下绝对路径模板。绝对不要 `cd` 到脚本目录，
绝对不要用 `scripts/xxx.py` 这类相对路径。**

```bash
python3 /workspace/custom/skill_examples/render_skills/scripts/<脚本名>.py \
  <脚本参数> \
  --output-dir "$(pwd)/render_analysis_output"
```

要点：
- **`$(pwd)` 在每条命令开头被展开**，得到当前 conversation 的工作目录（由平台
  自动隔离到 `/workspace/project/<conv_hex>/`）。后端进度条、下载按钮、产物
  列表都基于这个目录探测，**必须用 `$(pwd)/render_analysis_output`**，
  不能 hardcode `/workspace/render_output`。
- 所有 phase 的输出统一落到 `$(pwd)/render_analysis_output/`，前端进度条按
  相对路径 `render_analysis_output/...`（见 phases 字段）轮询。
- **绝对不要** `cd /workspace/custom/.../scripts && ...` — 一旦 `cd` 出
  conversation 目录，`$(pwd)` 就会改变，输出会落到错的地方。
- **绝对不要** `python3 scripts/...` — 依赖 LLM 恰好在 skill 目录，实际
  conversation cwd 是 `/tmp/openhands-sandboxes/...`，会直接 "No such file" 报错。

### 执行步骤

| 阶段 | 脚本 | 关键参数 |
|------|------|----------|
| 0. 环境初始化 | `setup_env.py` | 无(自动安装依赖) |
| 1. Jank 分析 | `analyze_jank.py` | `--trace {{trace_path}}` |
| 2. 截图（可选） | `capture_screenshots.py` | `--trace {{trace_path}} --analysis-dir "$(pwd)/render_analysis_output"` |
| 3. 生成报告 | `generate_report.py` | `--analysis-dir "$(pwd)/render_analysis_output"` |

每条命令都按上方命令模板展开为绝对路径。如果 `{{focus}}` == `fast`，跳过
阶段 2（截图），直接到阶段 3。

### 阶段 0: 环境初始化

```bash
python3 /workspace/custom/skill_examples/render_skills/scripts/setup_env.py
```

自动安装所有依赖:
- **perfetto**: Python 绑定，内置 trace_processor
- **playwright + Chromium**: Perfetto UI 无头浏览器截图
- **Pillow**: detail 截图标注

**验证:** 输出 `all_ready: true`。

### 阶段 1: Jank 分析

```bash
python3 /workspace/custom/skill_examples/render_skills/scripts/analyze_jank.py \
  --trace {{trace_path}} \
  --output-dir "$(pwd)/render_analysis_output"
```

一次性完成: 目标进程定位（按 jank 帧数最多的 app）、全局帧统计、jank 类型分布、
Top N 帧富化（region_range、keywords_hit、evidence_slices、target_ts、focus_track、
problem_description、screenshot_reasoning）、线程映射（主线程 / RenderThread /
hwuiTask / GPU completion / SF / RenderEngine / HWC）。

**产物（都在 `$(pwd)/render_analysis_output/` 下）:**
- `target_process.json` — 目标进程
- `app_jank.json` — 完整 jank 分析结果（含 top_frames 富化字段）
- `sf_jank.json` — SurfaceFlinger 层 jank
- `jank_types.json` — Jank 类型分布
- `thread_map.json` — 截图用的 18 个 track pin 关键字
- `tp_state.json` — trace 基础状态

**验证:** `app_jank.json` 中 `top_frames` 长度 > 0 或 `jank_rate == 0`。

### 阶段 2: Perfetto 截图（可选）

```bash
python3 /workspace/custom/skill_examples/render_skills/scripts/capture_screenshots.py \
  --trace {{trace_path}} \
  --analysis-dir "$(pwd)/render_analysis_output" \
  --output-dir "$(pwd)/render_analysis_output/screenshots"
```

为 `app_jank.json` 里 Top N 问题各抓 2 张竖屏长图: 全局图（Actual Timeline + pin
的所有关键 track）+ 局部细节图（target_ts ± 窗口内的 slice + Pillow 红框标注）。

**重要: 此步骤为可选。** 如果截图失败（chromium 无法启动、Perfetto UI 加载超时等），
记录失败原因并继续下一步 — **不要因为截图失败而停止工作流**。

### 阶段 3: 生成报告

```bash
python3 /workspace/custom/skill_examples/render_skills/scripts/generate_report.py \
  --analysis-dir "$(pwd)/render_analysis_output" \
  --output "$(pwd)/render_analysis_output/render_report.html"
```

生成 HTML 渲染性能报告，包含:
- 概览统计（总帧数、Jank 率、类型分布）
- Top N 重点问题（每个含: 问题帧元数据、证据 slices、嵌入的截图、Framework 根因分析）
- 7 种 jank 类型的 FRAMEWORK_KB 根因分析（调用链 + 源码引用 + Perfetto 诊断指南 + 优化建议）

### 完成后

向用户汇总:
1. Jank 类型分布概览
2. 最严重的 Top N 卡顿问题 + 根因
3. Android Framework 层面的优化建议
4. 报告文件位于 `$(pwd)/render_analysis_output/render_report.html`
   （前端会在这个相对路径自动探测并弹出下载按钮）

### 反例（绝对不要这样）

```bash
# ❌ 错：相对脚本路径，LLM 实际 cwd 是 /tmp/openhands-sandboxes，会直接报错
#    "No such file or directory"
python3 scripts/setup_env.py

# ❌ 错：cd 改变了 $(pwd)，输出会落到 /workspace/custom/.../scripts/render_analysis_output/
cd /workspace/custom/skill_examples/render_skills/scripts && python3 analyze_jank.py ...

# ❌ 错：hardcode 共享路径，会被多个任务互相覆盖，前端进度条/下载按钮都探测不到
python3 /workspace/.../analyze_jank.py ... --output-dir /workspace/render_output
```

---

## Standalone 使用（不依赖 HiClaw）

### 前置条件

已激活的 venv 安装了 `perfetto>=0.16.0`、`playwright>=1.57.0`、`Pillow>=10.0`，
并且 `playwright install chromium` 已执行。详见 `docs/quickstart.md`。

```bash
# 在线安装（一键）
./scripts/setup.sh

# 或 离线安装（从 offline bundle）
python3 scripts/setup_offline.py
```

### 一键执行

```bash
# 完整流程
python3 scripts/run_workflow.py \
  --trace /path/to/trace.perfetto-trace \
  --output-dir /path/to/output

# 只分析不截图（快速）
python3 scripts/run_workflow.py \
  --trace /path/to/trace --output-dir /out --no-screenshots
```

`run_workflow.py` 是 standalone 的 orchestrator，按 Phase 1→2→3 串行执行
所有脚本，**不需要 LLM 介入**。如果存在 `vendor/ms-playwright/` 目录（offline
bundle 场景），run_workflow.py 自动设置 `PLAYWRIGHT_BROWSERS_PATH`，用户无需
手动 export 任何环境变量。

---

## 技术参考

### Phase 1 详细说明

- SQL 查询 jank 帧统计
- **识别目标进程**: 按 **jank 帧数** 排名取最多的 App 进程（若进程名为 NULL 则以主线程名代替）
- 按 jank 类型分组，每类取最严重的帧，选 top-5
- **为每个 top frame 增补证据字段**: `target_ts` / `focus_track` / `evidence_slices` / `keywords_hit` / `region_range` / `problem_description` / `screenshot_reasoning`
- 构建完整渲染管线的 thread_map（覆盖 App → SF → HWC → CrtcCommit），包含 app_hwui_threads
- 关键词覆盖 7 层渲染管线（~50 个 slice 名）

### Phase 2: 8 步截图策略（每个 jank 帧执行一次）

```
1. Reset:         UnpinAll → CollapseAll
2. 建立节点:      ExpandAllGroups → CollapseAllGroups
                  （使所有 track 节点进入 DOM，解决虚拟化滚动导致节点缺失）
3. Pin 精简管线:  surfaceflinger {tid} + composer-servic（仅 2 条顶层 track）
4. Expand 目标:   ExpandTracksByRegex(进程名) + ExpandTracksByRegex("Expected Timeline|Actual Timeline")
5. Collapse 噪声: CollapseTracksByRegex("CPU Scheduling|CPU Frequency|Ftrace|GPU|Scheduler|System|Kernel")
6. Force-hide UI: ToggleLeftSidebar + ToggleDrawer + CSS 注入隐藏 cookie 横幅
7. 全局图:        setVisibleWindow(全 trace) → omnibox 搜索 "DrawFrames" → scrollBy(0,160) → 截图
8. 细节图:        setVisibleWindow(target_ts ± window) → 恢复 scroll → Pillow 叠加标注
```

### 7 层画面组成（从上到下）

| 层 | Track | 说明 |
|----|-------|------|
| Pinned-L5 | `surfaceflinger {tid}` | SF 主线程 |
| Pinned-L7 | `composer-servic` | HWC 硬件合成器 |
| Middle | `Expected Timeline` / `Actual Timeline` | 帧 jank 标记 |
| Bottom-L1 | App 主线程 | doFrame / performTraversals |
| Bottom-L2 | `RenderThread` | DrawFrames / flush commands |
| Bottom-L3 | `hwuiTask*` + `GPU completion` | GPU 完成信号 |

### Phase 3: FRAMEWORK_KB 根因分析

7 种 jank 类型硬编码，**不依赖 LLM**:
- **App Deadline Missed**: 15 步调用链（完整 Skia/HWUI）
- **SurfaceFlinger GPU Deadline Missed**: 15 步调用链（CLIENT 合成路径）
- **SF CPU Deadline Missed**: 10 步调用链
- **Display HAL**: 9 步调用链
- **Buffer Stuffing**: 7 步调用链
- **Prediction Error** / **SurfaceFlinger Scheduling**: 基础描述

### 可复现性保证

| 组件 | 确定性 | 说明 |
|------|--------|------|
| Phase 1 SQL 分析 | **100%** | 同一 trace 产出完全相同 JSON |
| Phase 2 截图 | **95%+** | 依赖 Perfetto UI 渲染，微小像素差异 |
| Phase 3 报告 | **100%** | 读 JSON + 拼 HTML，无随机性 |
| Framework 根因分析 | **100%** | FRAMEWORK_KB 硬编码 |

### Perfetto API 参考

| 功能 | 命令/API | 备注 |
|------|----------|------|
| Pin tracks | `app.commands.runCommand('dev.perfetto.PinTracksByRegex', regex)` | 132 个注册命令 |
| Unpin all | `dev.perfetto.UnpinAllTracks` | |
| Collapse | `dev.perfetto.CollapseAllGroups` | |
| Expand | `dev.perfetto.ExpandAllGroups` / `ExpandTracksByRegex` | |
| Sidebar toggle | `dev.perfetto.ToggleLeftSidebar` | |
| Drawer toggle | `dev.perfetto.ToggleDrawer` | |
| Zoom | `app._activeTrace.timeline.setVisibleWindow(HPTS(HPT(BigInt), dur))` | |

### Pin 策略（v4.1 — 精简顶层 Pin + Expand Frame Timeline）

`PinTracksByRegex` **只能 pin 顶层 track**。策略: 只 pin SF + HWC 2 条，
App 层通过 Expand 展开。

| 步骤 | 操作 | 说明 |
|------|------|------|
| Pin L5 | `surfaceflinger {tid}` | SF 主线程 |
| Pin L7 | `composer-servic` | HWC 硬件合成器 |
| Expand | 目标进程名 regex | 展开 App 进程组 |
| Expand | `Expected Timeline\|Actual Timeline` | 展开 Frame Timeline 子组 |
| Collapse | CPU/GPU/Ftrace/Kernel 等 | 减少噪声 |
