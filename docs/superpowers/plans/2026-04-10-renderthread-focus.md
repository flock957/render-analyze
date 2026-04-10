# 图形渲染全管线焦点强化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让截图、证据收集、根因分析覆盖 Android 图形渲染完整 7 层管线（UI Thread → RenderThread/Skia → GPU → BufferQueue → SF → RenderEngine → HWC/Display），替代当前只覆盖 SF/fence 后 3 层的状况。

**Architecture:** 6 个 Task 解决 6 个独立问题：(1) 目标进程用 jank 帧数选取；(2) Pin 策略 main+RenderThread 置顶 + hwuiTask 发现；(3) 证据 SQL 限定 target 进程；(4) Detail 截图 expand RenderThread；(5) KEYWORDS 全 7 层扩充；(6) FRAMEWORK_KB 补完 Skia/HWUI/GPU/SF 合成策略全链路 + skill 同步。

**Tech Stack:** Python3, perfetto TraceProcessor SQL, Playwright, Pillow

---

## 问题诊断（从 v4 trace 实测数据确认）

### 根因 1: 目标进程选错

当前用总 running time 选出 `aweme` (0 jank, 0 RenderThread)。真正 jank 冠军 `om.vivo.upslide` (122 jank, 1073ms RenderThread, 完整 HWUI 管线)。

### 根因 2: Pin 缺 RenderThread

aweme 没有 RenderThread → pin_patterns 跳过 → 截图完全看不到渲染管线。

### 根因 3: 证据 SQL 不限进程

region 内搜全局，binder/doFrame 被其他进程淹没。

### 根因 4: Detail 没展开 RenderThread

pinned track 折叠，看不到 DrawFrames 子 slice（syncFrameState → renderFrameImpl → flush → eglSwap）。

### 根因 5: 关键词只覆盖后 3 层

7 层管线中，只有 SF/HWC/Display 的关键词比较全。前 4 层（UI Thread / RenderThread / GPU / BufferQueue）大量关键 slice 未被覆盖。

### 根因 6: FRAMEWORK_KB 缺 Skia/HWUI/GPU 管线

调用链到 `queueBuffer` 就结束。缺：DrawFrames → renderFrameImpl → flush commands → eglSwapBuffers → Waiting for GPU → shader_compile 完整链路。SF 侧也缺 `prepareFrame`/`composeSurfaces` 等关键步骤。

---

## Android 图形渲染 7 层管线（v4 trace 实测数据）

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 1: UI Thread (主线程)                                      │
│  Choreographer#doFrame                                          │
│   ├── input (32.8ms max)                                        │
│   ├── animation (17.4ms max)                                    │
│   ├── traversal (94ms max!) ← measure → layout → draw          │
│   │   └── draw → Record View#draw() → postAndWait              │
│   └── inflate (30.2ms max)                                      │
├─────────────────────────────────────────────────────────────────┤
│ Layer 2: RenderThread (HWUI/Skia)  ← 当前完全缺失              │
│  DrawFrames                                                     │
│   ├── syncFrameState → prepareTree                             │
│   ├── Drawing W H (Skia 绘制, 783ms total!)                    │
│   │   └── renderFrameImpl (Skia 指令录制)                      │
│   ├── flush commands (419ms) → OpsTask::onExecute (279ms)      │
│   │   ├── FillRectOp (80ms) / TextureOp (34ms)                │
│   │   └── PathStencilCoverOp (34ms)                            │
│   ├── eglSwapBuffersWithDamageKHR (492ms)                      │
│   ├── shader_compile (68ms, 9次, max 11ms!) ← 冷启动 jank     │
│   └── dequeueBuffer / queueBuffer                              │
├─────────────────────────────────────────────────────────────────┤
│ Layer 3: GPU 硬件                                               │
│  [GPU completion] waitForever (708ms)                           │
│  [hwuiTask0/1] Skia tile workers                               │
├─────────────────────────────────────────────────────────────────┤
│ Layer 4: BufferQueue                                            │
│  queueBuffer → acquireBuffer → latchBuffer → releaseBuffer     │
├─────────────────────────────────────────────────────────────────┤
│ Layer 5: SurfaceFlinger                                         │
│  onMessageRefresh                                               │
│   ├── latchBuffers (162ms)                                     │
│   ├── prepareFrame (1273ms!) → chooseCompositionStrategy       │
│   │   └── HwcPresentOrValidateDisplay (1204ms)                 │
│   ├── finishFrame → composeSurfaces (192ms)                    │
│   ├── postComposition → present (3675ms, 含 fence wait)        │
│   └── postFramebuffer (125ms)                                  │
├─────────────────────────────────────────────────────────────────┤
│ Layer 6: SF RenderEngine (GPU 合成回退)                         │
│  REThreaded::drawLayers → SkiaGL::drawLayers (156ms)           │
│   └── flush + OpsTask::onExecute                               │
├─────────────────────────────────────────────────────────────────┤
│ Layer 7: HWC → Display                                          │
│  [composer-servic] PerformCommit → HWDeviceDRM::AtomicCommit   │
│  [HWC release] waitForever (854ms)                             │
│  [crtc_commit:113] DRM/KMS 帧提交                              │
└─────────────────────────────────────────────────────────────────┘
```

---

## 文件结构

| 文件 | 修改类型 | 职责 |
|------|----------|------|
| `scripts/analyze_jank.py:89-110` | **重写** | Task 1: 目标进程选择改用 jank 帧数 |
| `scripts/analyze_jank.py:445-491` | **修改** | Task 2: _build_pin_patterns 强制 RenderThread 置顶 + hwuiTask |
| `scripts/analyze_jank.py:305-404` | **修改** | Task 3: _enrich/_find_target_ts/_collect_evidence 加进程限定 |
| `scripts/capture_screenshots.py:175-245` | **修改** | Task 4: Detail 截图 expand RenderThread |
| `scripts/analyze_jank.py:12-39` | **重写** | Task 5: KEYWORDS_BY_JANK_TYPE 全 7 层扩充 |
| `scripts/generate_report.py:15-272` | **修改** | Task 6: FRAMEWORK_KB 补完全管线 |
| `skills/*.md` | **更新** | Task 6: 文档同步 |

---

### Task 1: 修复目标进程选择 — 改用 jank 帧数

**Files:**
- Modify: `scripts/analyze_jank.py:89-110`

- [ ] **Step 1: 替换目标进程选择 SQL**

将 `analyze_jank.py` 的 Step 2 (约第 89-110 行) 整段替换为：

```python
    # --- Step 2: Find target process (by jank frame count, not running time) ---
    print("  [1.2] Finding target process...")

    # Primary: pick the process with the most jank frames
    q_jank_target = tp.query("""
        SELECT p.name, p.pid,
               COUNT(*) as jank_count,
               SUM(aft.dur)/1e6 as jank_dur_ms
        FROM actual_frame_timeline_slice aft
        LEFT JOIN process_track pt ON aft.track_id = pt.id
        LEFT JOIN process p ON pt.upid = p.upid
        WHERE aft.jank_type != 'None'
          AND p.pid IS NOT NULL
        GROUP BY p.pid
        ORDER BY jank_count DESC
        LIMIT 10
    """)
    jank_candidates = [
        {"process_name": r.name or f"pid_{r.pid}", "pid": r.pid,
         "jank_count": r.jank_count, "jank_dur_ms": r.jank_dur_ms}
        for r in q_jank_target
    ]

    # Fallback: if no jank frames found, use running time (old method)
    if jank_candidates:
        target = jank_candidates[0]
        method = "jank_frame_count"
        print(f"        Target (by jank): {target['process_name']} "
              f"(pid={target['pid']}, {target['jank_count']} jank frames)")
    else:
        q_run = tp.query("""
            SELECT p.name, p.pid, SUM(dur)/1e6 as total_ms
            FROM sched s JOIN thread t ON s.utid=t.utid
            JOIN process p ON t.upid=p.upid
            WHERE p.name IS NOT NULL AND p.name != ''
            GROUP BY p.pid ORDER BY total_ms DESC LIMIT 10
        """)
        jank_candidates = [
            {"process_name": r.name, "pid": r.pid, "total_running_ms": r.total_ms}
            for r in q_run
        ]
        target = jank_candidates[0] if jank_candidates else {
            "process_name": "unknown", "pid": 0}
        method = "running_time"
        print(f"        Target (by running): {target['process_name']} (pid={target['pid']})")

    # Resolve NULL process names from main thread name (common in some traces)
    if target["process_name"].startswith("pid_"):
        q_tname = tp.query(f"""
            SELECT t.name FROM thread t
            JOIN process p ON t.upid = p.upid
            WHERE p.pid = {target['pid']} AND t.tid = {target['pid']}
              AND t.name IS NOT NULL AND t.name != ''
            LIMIT 1
        """)
        for r in q_tname:
            target["process_name"] = r.name
            print(f"        Resolved name from main thread: {r.name}")

    _write(output / "target_process.json", {
        "method": method,
        "process_name": target["process_name"],
        "pid": target["pid"],
        "jank_count": target.get("jank_count", 0),
        "candidates": jank_candidates,
    })
```

- [ ] **Step 2: 验证**

```bash
python3 scripts/analyze_jank.py \
  --trace /home/wq/workspace/test_render_traces/render_trace_v4.perfetto-trace \
  --output-dir /tmp/task1_test
python3 -c "import json; d=json.load(open('/tmp/task1_test/target_process.json')); print(d['process_name'], d['pid'], d.get('method'))"
```

预期: `om.vivo.upslide 5497 jank_frame_count`

- [ ] **Step 3: Commit**

```bash
git add scripts/analyze_jank.py
git commit -m "fix(analyze): select target process by jank frame count, not running time"
```

---

### Task 2: Pin 策略 — Main Thread + RenderThread 置顶 + hwuiTask

**Files:**
- Modify: `scripts/analyze_jank.py` — thread_map 查询 + `_build_pin_patterns`

- [ ] **Step 1: 在 thread_map 查询中增加 hwuiTask 发现**

在现有 `q_app_render` 查询之后加：

```python
    # App's hwuiTask helper threads (Skia GPU tile workers)
    q_hwui = tp.query(f"""
        SELECT t.name, t.tid FROM thread t
        JOIN process p ON t.upid = p.upid
        WHERE p.pid = {target_pid}
          AND (t.name LIKE 'hwuiTask%' OR t.name = 'GPU completion')
        ORDER BY t.tid LIMIT 4
    """)
    app_hwui = [{"name": r.name, "tid": r.tid} for r in q_hwui]
```

把 `app_hwui` 加入 `thread_map` JSON 输出和 `_build_pin_patterns` 调用参数。

- [ ] **Step 2: 重写 `_build_pin_patterns`**

```python
def _build_pin_patterns(target, app_main, app_render, app_hwui,
                        sf_main_tid, sf_pid,
                        sf_render_engine, sf_gpu, sf_binder,
                        hwc_threads, crtc_threads):
    """Build pin patterns. Order = top to bottom in Perfetto pinned area.
    App main + RenderThread are always pinned first (after Timeline)."""
    patterns = []

    # 1. Frame Timeline
    patterns.append("Expected Timeline")
    patterns.append("Actual Timeline")

    # 2. App main thread (最重要 — 紧跟 Timeline)
    if app_main:
        patterns.append(f"{app_main[0]['name']} {app_main[0]['tid']}")

    # 3. App RenderThread (第二重要)
    if app_render:
        patterns.append(f"RenderThread {app_render[0]['tid']}")

    # 4. App hwuiTask + GPU completion (Skia 辅助)
    for t in app_hwui[:2]:
        patterns.append(f"{t['name']} {t['tid']}")

    # 5. SF main
    if sf_main_tid:
        patterns.append(f"surfaceflinger {sf_main_tid}")

    # 6-9: 后续不变
    if sf_render_engine:
        patterns.append(f"RenderEngine {sf_render_engine[0]['tid']}")
    if sf_gpu:
        patterns.append(f"GPU completion {sf_gpu[0]['tid']}")
    if sf_binder:
        patterns.append(f"{sf_binder[0]['name']}")
    for t in hwc_threads[:1]:
        patterns.append(f"{t['name']}")
    for t in crtc_threads[:1]:
        patterns.append(f"{t['name']}")

    return patterns
```

- [ ] **Step 3: 验证**

```bash
python3 -c "import json; print('\n'.join(json.load(open('/tmp/task2_test/thread_map.json'))['pin_patterns']))"
```

预期前 4 行: `Expected Timeline` / `Actual Timeline` / `om.vivo.upslide 5497` / `RenderThread 6241`

- [ ] **Step 4: Commit**

```bash
git add scripts/analyze_jank.py
git commit -m "feat(analyze): pin main thread + RenderThread at top, discover hwuiTask"
```

---

### Task 3: 证据 SQL 限定目标进程

**Files:**
- Modify: `scripts/analyze_jank.py` — `_find_target_ts`, `_collect_evidence`, `_enrich_top_frame`

- [ ] **Step 1: 给 `_find_target_ts` 和 `_collect_evidence` 加 target_pid 参数**

`_find_target_ts`: 加 `JOIN thread_track/thread/process` 和 `AND p.pid = {target_pid}` 过滤。如果 target_pid 限定搜不到结果，递归调用自己 `target_pid=None` 做全局 fallback。

`_collect_evidence`: 同理加 pid 过滤。如果 target 限定结果 < 3 条，补充全局搜索去重后合并。

- [ ] **Step 2: `_enrich_top_frame` 传入 target_pid**

调用处改为: `_enrich_top_frame(tp, frame, trace_start, trace_end, target_pid=target["pid"])`

函数签名: `def _enrich_top_frame(tp, frame, trace_start, trace_end, target_pid=None)`

内部透传给 `_find_target_ts(…, target_pid=target_pid)` 和 `_collect_evidence(…, target_pid=target_pid)`。

- [ ] **Step 3: 验证**

```bash
python3 -c "
import json
for f in json.load(open('/tmp/task3_test/app_jank.json'))['top_frames']:
    threads = set(e['thread'] for e in f.get('evidence_slices',[]))
    print(f'{f[\"jank_type\"]}: {threads}')
"
```

预期: 主要出现 `om.vivo.upslide` / `RenderThread` / `surfaceflinger`，不再有 `binder:4807` 等无关进程。

- [ ] **Step 4: Commit**

```bash
git add scripts/analyze_jank.py
git commit -m "fix(analyze): scope evidence SQL to target process with global fallback"
```

---

### Task 4: Detail 截图展开 RenderThread

**Files:**
- Modify: `scripts/capture_screenshots.py` — detail 截图段

- [ ] **Step 1: Detail 截图前 expand RenderThread + main thread**

在 `capture_screenshots.py` 的 detail 截图段，`_zoom_to` 之后、`_focus_track_y` 之前加：

```python
                # Expand RenderThread and main thread for deep slice visibility
                if "App Deadline" in jank_type or "Buffer Stuffing" in jank_type:
                    _cmd(page, 'dev.perfetto.ExpandTracksByRegex', 'RenderThread')
                    time.sleep(0.3)
                    _cmd(page, 'dev.perfetto.ExpandTracksByRegex', target['process_name'])
                    time.sleep(0.3)
```

- [ ] **Step 2: Global 截图前加 CollapseAll 护栏**

在 global 截图段开头加：

```python
                _cmd(page, 'dev.perfetto.CollapseAllGroups')
                time.sleep(0.2)
```

- [ ] **Step 3: 验证**

跑 workflow，查看 detail 截图中 RenderThread 是否展开，能看到 `DrawFrames → syncFrameState → renderFrameImpl → flush commands → eglSwapBuffers` 层级。

- [ ] **Step 4: Commit**

```bash
git add scripts/capture_screenshots.py
git commit -m "feat(capture): expand RenderThread in detail screenshots for slice depth"
```

---

### Task 5: KEYWORDS_BY_JANK_TYPE 全 7 层扩充

**Files:**
- Modify: `scripts/analyze_jank.py:12-39`

- [ ] **Step 1: 重写完整关键词集合**

```python
KEYWORDS_BY_JANK_TYPE = {
    "App Deadline Missed": [
        # Layer 1: UI Thread
        "doFrame", "traversal", "performTraversals",
        "input", "animation",
        "measure", "layout", "draw",
        "Record View#draw()", "postAndWait", "inflate",
        "Binder", "GC", "JIT", "Monitor contention",
        # Layer 2: RenderThread (HWUI/Skia)
        "DrawFrame", "DrawFrames",
        "syncFrameState", "prepareTree",
        "renderFrameImpl", "Drawing",
        "flush commands", "OpsTask",
        "eglSwapBuffers", "Waiting for GPU",
        "queueBuffer", "dequeueBuffer",
        # Layer 2b: Shader compilation (冷启动 jank 常见源)
        "shader_compile", "cache_miss",
        "driver_compile_shader", "driver_link_program",
        # Layer 2c: Buffer allocation
        "allocateHelper",
    ],
    "Buffer Stuffing": [
        # Layer 4: BufferQueue
        "dequeueBuffer", "queueBuffer", "acquireBuffer", "latchBuffer",
        # Layer 2: RenderThread 上游
        "DrawFrames", "renderFrameImpl", "flush commands",
        "eglSwapBuffers", "Waiting for GPU",
        # Layer 5: SF 下游消费
        "latchBuffers", "onMessageRefresh",
    ],
    "SurfaceFlinger CPU Deadline Missed": [
        # Layer 5: SF 主帧循环
        "onMessageRefresh", "commit", "composite",
        "handleTransaction", "handleComposition",
        "latchBuffers", "rebuildLayerStacks",
        "prepareFrame", "chooseCompositionStrategy",
        "finishFrame", "composeSurfaces",
        "postComposition", "postFramebuffer",
        "present",
        # Layer 6: RenderEngine (GPU 合成回退)
        "RenderEngine", "REThreaded::drawLayers",
        "SkiaGL::drawLayers",
    ],
    "Display HAL": [
        # Layer 5: SF fence
        "presentFence", "waiting for presentFence",
        "present", "postComposition",
        # Layer 7: HWC/Display
        "presentDisplay", "composer", "hwc",
        "HwcPresentOrValidateDisplay",
        "HWDeviceDRM", "AtomicCommit", "DRMAtomicReq",
        "crtc_commit", "PerformCommit",
        # Layer 7b: HWC release
        "waitForever",
    ],
    "Prediction Error": [
        "Expected Timeline", "Actual Timeline", "VSync",
    ],
    "SurfaceFlinger Scheduling": [
        "surfaceflinger", "onMessageRefresh", "sched",
        "Runnable",
    ],
}
```

- [ ] **Step 2: 同步更新 skills/analyze-jank.md 和 skills/capture-screenshots.md 中的关键词表**

- [ ] **Step 3: 验证**

```bash
python3 scripts/analyze_jank.py --trace ... --output-dir /tmp/task5_test
python3 -c "
import json
for f in json.load(open('/tmp/task5_test/app_jank.json'))['top_frames']:
    print(f'{f[\"jank_type\"]}: keywords_hit={f[\"keywords_hit\"]}')
    for e in f['evidence_slices'][:3]:
        print(f'    {e[\"name\"]}@{e[\"thread\"]} {e[\"dur_ms\"]}ms')
"
```

预期: App Deadline Missed 命中 `traversal`, `draw`, `DrawFrames`, `flush commands`, `eglSwapBuffers` 等全管线 slice。

- [ ] **Step 4: Commit**

```bash
git add scripts/analyze_jank.py skills/analyze-jank.md skills/capture-screenshots.md
git commit -m "feat(analyze): expand keywords to cover full 7-layer graphics pipeline"
```

---

### Task 6: FRAMEWORK_KB 补完全管线 + Skill 同步 + 端到端验证

**Files:**
- Modify: `scripts/generate_report.py:15-272` (FRAMEWORK_KB)
- Modify: `skills/workflow.md`, `skills/analyze-jank.md`, `skills/capture-screenshots.md`, `skills/generate-report.md`

这是最大的 Task。分 6 个 sub-step。

- [ ] **Step 1: App Deadline Missed — 补完 RenderThread/Skia/GPU 管线**

`call_chain` 扩展为完整 14 步（当前 9 步），覆盖到 Waiting for GPU：

```python
    "App Deadline Missed": {
        "cn_name": "应用侧超时",
        "call_chain": [
            "VSYNC-app 信号到达",
            "Choreographer.doFrame()",
            "  → INPUT callbacks (处理触摸/按键事件)",
            "  → ANIMATION callbacks (属性动画/过渡动画)",
            "  → TRAVERSAL: ViewRootImpl.performTraversals()",
            "    → performMeasure() → performLayout() → performDraw()",
            "  → ThreadedRenderer.draw() → postAndWait 同步到 RenderThread",
            "RenderThread: DrawFrames (HWUI 帧入口)",
            "  → syncFrameState (从 UI 线程同步 RenderNode 树 + prepareTree)",
            "  → renderFrameImpl (Skia SkCanvas 指令录制: drawBitmap/drawPath/drawText)",
            "  → flush commands (Skia GrContext::flush → OpsTask::onExecute → GPU Op 批处理)",
            "    → FillRectOp / TextureOp / PathStencilCoverOp (具体 Skia GPU Op)",
            "  → eglSwapBuffersWithDamageKHR (EGL 提交帧 buffer → 等待 GPU fence)",
            "  → Waiting for GPU (GPU completion fence — GPU 完成所有绘制)",
            "  → queueBuffer → 提交帧到 BufferQueue → SurfaceFlinger 消费",
        ],
```

新增 2 个 source_ref (CanvasContext.cpp + EglManager.cpp)：

```python
            {
                "file": "CanvasContext.cpp",
                "path": "frameworks/base/libs/hwui/renderthread/CanvasContext.cpp",
                "desc": "draw() 是 RenderThread 的帧入口，对应 Trace 中的 'DrawFrames' slice。"
                        "内部调用链: prepareTree → syncFrameState → renderFrameImpl → "
                        "flush(GrContext) → eglSwapBuffers。"
                        "瓶颈定位: renderFrameImpl 长 → Skia 绘制指令多（复杂 Canvas 操作）；"
                        "flush commands 长 → GPU Op 执行慢或 Op 数量多；"
                        "eglSwapBuffers 长 → GPU 渲染慢或 buffer 争用。",
            },
            {
                "file": "EglManager.cpp",
                "path": "frameworks/base/libs/hwui/renderthread/EglManager.cpp",
                "desc": "eglSwapBuffersWithDamageKHR() 提交 GPU 完成的帧 buffer 到 BufferQueue。"
                        "正常 < 2ms。如果 > 5ms → GPU 未完成渲染（Waiting for GPU fence），"
                        "或 BufferQueue 满（triple buffering 下所有 slot 被占）。"
                        "后面紧跟的 'Waiting for GPU' slice 长度 = GPU 实际渲染耗时。",
            },
            {
                "file": "ShaderCache.cpp",
                "path": "frameworks/base/libs/hwui/pipeline/skia/ShaderCache.cpp",
                "desc": "Skia shader 首次编译时触发 'shader_compile' + 'cache_miss' slice。"
                        "每次 7-11ms，冷启动时可能连续 9+ 次。"
                        "优化: 使用 Vulkan pipeline cache 或 ShaderCache warmup 预热。",
            },
```

新增 trace_guide 5 条：

```python
            "**展开 RenderThread** 的 DrawFrames slice，查看子 slice 层级:",
            "  syncFrameState → renderFrameImpl → flush commands → eglSwapBuffers → Waiting for GPU",
            "如果 renderFrameImpl 长: Skia 绘制指令多 → 用 GPU Inspector 检查 draw call 数量",
            "如果 flush commands 长: GPU 驱动提交慢 → 检查 OpsTask::onExecute 中哪个 Op 最耗时",
            "如果 eglSwapBuffers + Waiting for GPU 长: GPU 执行慢 → 检查 GPU 频率和 shader 复杂度",
            "检查是否有 'shader_compile' / 'cache_miss' slice — 每次 7-11ms 的冷启动 jank 源",
```

新增 root_causes 3 条：

```python
            "**RenderThread GPU 管线瓶颈**: renderFrameImpl/flush commands/eglSwapBuffers 某段超长",
            "**Skia 绘制指令过多**: 大量 drawBitmap/drawPath/drawText 或 saveLayer，表现为 Drawing slice 和 OpsTask 耗时长",
            "**Shader 编译卡顿 (冷启动)**: 首次渲染特定 effect 时触发 shader_compile，每次 7-11ms",
            "**GPU 频率低 / Thermal 降频**: flush commands 和 Waiting for GPU 同时变长",
```

新增 optimizations 3 条：

```python
            "**展开 DrawFrames** slice 做 RenderThread 瓶颈定位: syncFrameState / renderFrameImpl / flush / eglSwap / Waiting for GPU",
            "减少 Canvas.drawPath() 复杂度, 对静态 Path 使用 PathMeasure 缓存",
            "使用 ShaderCache warmup 减少冷启动 shader_compile 卡顿",
```

- [ ] **Step 2: SurfaceFlinger CPU Deadline Missed — 补 prepareFrame/composeSurfaces**

扩展 call_chain，在 `handleTransaction` 和 `composite` 之间加入 SF 实际子步骤：

```python
        "call_chain": [
            "VSYNC-sf 信号到达",
            "SurfaceFlinger.onMessageRefresh()",
            "  → latchBuffers(): 从 BufferQueue 获取 App 提交的 buffer",
            "  → rebuildLayerStacks(): 计算 Layer 可见区域和层级",
            "  → prepareFrame(): 决定合成策略 (HWC overlay vs GPU fallback)",
            "    → chooseCompositionStrategy() → HwcPresentOrValidateDisplay()",
            "  → finishFrame(): 执行合成",
            "    → composeSurfaces(): GPU 合成路径 (RenderEngine::drawLayers)",
            "  → postFramebuffer(): 提交合成结果到显示控制器",
            "  → postComposition(): fence 管理、present fence 等待、帧统计",
        ],
```

新增 trace_guide:

```python
            "重点检查 'prepareFrame' / 'chooseCompositionStrategy' 耗时 — v4 trace 中这段占 1273ms",
            "如果 composeSurfaces 长 → Layer 过多导致 GPU 合成回退，检查 REThreaded::drawLayers",
```

- [ ] **Step 3: Display HAL — 补 HWC 驱动细节**

扩展 call_chain 加入 HWDeviceDRM / AtomicCommit：

```python
        "call_chain": [
            "SurfaceFlinger.onMessageRefresh()",
            "  → prepareFrame() → HwcPresentOrValidateDisplay()",
            "  → postFramebuffer() → HWComposer.presentAndGetReleaseFences()",
            "HWC HAL: presentDisplay() → 提交帧到显示控制器",
            "  → [composer-servic] PerformCommit → HWDeviceDRM::Commit",
            "    → HWDeviceDRM::AtomicCommit → DRMAtomicReq::Commit",
            "Kernel: DRM/KMS → crtc_commit → Display Controller → Panel",
            "返回 presentFence → SF 在下一帧 postComposition 中等待此 fence",
            "[HWC release] waitForever → 释放上一帧 buffer",
        ],
```

- [ ] **Step 4: Buffer Stuffing — 补上游 RenderThread 视角**

扩展 call_chain，从 RenderThread 的角度看 buffer 争用:

```python
        "call_chain": [
            "App RenderThread: DrawFrames → renderFrameImpl → flush commands",
            "App RenderThread: eglSwapBuffersWithDamageKHR → queueBuffer (提交 buffer)",
            "App RenderThread: dequeueBuffer → 尝试获取下一个空 buffer",
            "  → BufferQueueProducer.dequeueBuffer() 阻塞（所有 slot 被占）",
            "  → 阻塞原因: SF 还没消费前面的 buffer (SF 合成慢/presentFence 慢)",
            "SF: onMessageRefresh → latchBuffers → acquireBuffer() → 消费 buffer",
            "SF: present → waiting for presentFence → 等待上一帧的显示完成",
        ],
```

- [ ] **Step 5: 端到端验证**

```bash
cd /home/wq/trace_screenshot_skill
PLAYWRIGHT_BROWSERS_PATH=/home/wq/.cache/ms-playwright \
  .venv/bin/python3 scripts/run_workflow.py \
    --trace /home/wq/workspace/test_render_traces/render_trace_v4.perfetto-trace \
    --output-dir /home/wq/render_output_portrait
```

验证清单:
- [ ] target = `om.vivo.upslide` (pid=5497), method=jank_frame_count
- [ ] pin_patterns 前 4: Timeline / Timeline / `om.vivo.upslide 5497` / `RenderThread 6241`
- [ ] evidence_slices 包含 `traversal`, `DrawFrames`, `flush commands`, `eglSwapBuffers` 等
- [ ] detail 截图 RenderThread 展开可见 DrawFrames 子 slice 树
- [ ] report 中 App Deadline Missed 调用链包含 14 步完整管线
- [ ] report 中 SF CPU Deadline Missed 包含 prepareFrame/composeSurfaces
- [ ] 全部 2144×3196 竖屏

- [ ] **Step 6: Skill 同步 + Commit + Push**

更新 skills/workflow.md, analyze-jank.md, capture-screenshots.md, generate-report.md。

```bash
git add scripts/ skills/ requirements.txt
git commit -m "feat: full 7-layer graphics pipeline coverage in keywords, KB, and screenshots"
git push
```

---

## 预期改善对比

| 维度 | 修改前 | 修改后 |
|------|--------|--------|
| 目标进程 | aweme (0 jank) | om.vivo.upslide (122 jank) |
| Pin 前 4 条 | Timeline + aweme | Timeline + **upslide main + RenderThread + hwuiTask** |
| Detail 截图 | 折叠 track | **展开 DrawFrames → renderFrameImpl → flush → eglSwap** |
| 证据 slices | 混杂无关进程 binder | **限定 target: traversal/DrawFrames/flush/composite** |
| App Deadline KB | 9 步止于 queueBuffer | **14 步含完整 Skia/HWUI GPU 管线** |
| SF Deadline KB | 6 步缺 prepareFrame | **10 步含 chooseCompositionStrategy/composeSurfaces** |
| Display HAL KB | 6 步缺 DRM | **9 步含 HWDeviceDRM/AtomicCommit/DRMAtomicReq** |
| Buffer Stuffing KB | 5 步缺 RenderThread | **7 步从 DrawFrames 开始到 presentFence** |
| 关键词覆盖层级 | 后 3 层 (SF/HWC/Display) | **全 7 层 (UI→RT→GPU→Buffer→SF→RE→HWC)** |
| 新增关键 slice | — | shader_compile, Drawing, OpsTask, prepareFrame, composeSurfaces, HWDeviceDRM, waitForever |
