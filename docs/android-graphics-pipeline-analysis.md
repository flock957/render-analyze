# Android 图形渲染全管线分析 & RenderAnalyze 改进方案

> 版本: 2026-04-10 | 项目: [flock957/render-analyze](https://github.com/flock957/render-analyze) feat/portrait-longshot 分支

## 1. 背景

render-analyze 是一个**纯 Python 无 LLM** 的自动化工具，从 Perfetto trace 中提取 Android 渲染 jank 问题，自动截图 + 生成带根因分析的 HTML 报告。

当前版本（v4 portrait-longshot）的截图和根因分析**偏重 SurfaceFlinger / HWC / Display 后端**，对应用侧的 UI Thread、RenderThread (HWUI/Skia)、GPU 管线覆盖不足。本文档分析完整管线并提出改进方案。

---

## 2. Android 图形渲染完整管线（7 层）

一帧从 App 到屏幕要经过 7 层。每层都可能成为瓶颈。

```
┌─────────────────────────────────────────────────────────────────────┐
│ Layer 1: UI Thread (主线程)                                          │
│                                                                     │
│  Choreographer#doFrame                                              │
│   ├── input          (触摸/按键回调)                                 │
│   ├── animation      (属性动画/过渡动画)                             │
│   ├── traversal      (View 树遍历)                                  │
│   │   ├── measure    (View 测量)                                    │
│   │   ├── layout     (View 布局)                                    │
│   │   └── draw       (Canvas 指令录制 → DisplayList)                │
│   │       └── Record View#draw()                                    │
│   └── postAndWait    (同步 DisplayList 到 RenderThread)             │
│                                                                     │
│   可能的阻塞: Binder IPC / GC / JIT / Monitor contention / inflate  │
├─────────────────────────────────────────────────────────────────────┤
│ Layer 2: RenderThread (HWUI/Skia 渲染线程)                           │
│                                                                     │
│  DrawFrames (HWUI 帧入口)                                           │
│   ├── syncFrameState     (从 UI 线程同步 RenderNode 树)             │
│   │   └── prepareTree    (准备 DisplayList)                         │
│   ├── renderFrameImpl    (Skia SkCanvas 指令录制)                   │
│   │   └── Drawing W H    (具体 Skia 绘制操作)                       │
│   ├── flush commands     (GrContext::flush → 提交 GPU 指令)         │
│   │   └── OpsTask::onExecute (Skia GPU Op 批处理)                   │
│   │       ├── FillRectOp     (矩形填充)                             │
│   │       ├── TextureOp      (纹理绘制)                             │
│   │       └── PathStencilCoverOp (路径模板)                         │
│   ├── eglSwapBuffersWithDamageKHR (EGL 提交帧 buffer)              │
│   │   └── queueBuffer   (提交到 BufferQueue)                       │
│   ├── dequeueBuffer      (获取下一个空 buffer, 可能阻塞)            │
│   └── [可选] shader_compile / cache_miss (首次编译 shader)          │
│              driver_compile_shader / driver_link_program             │
├─────────────────────────────────────────────────────────────────────┤
│ Layer 3: GPU 硬件执行                                                │
│                                                                     │
│  [GPU completion 线程]                                              │
│   └── waitForever / waiting for GPU completion NNN                  │
│  [hwuiTask0/1] Skia tile worker 线程                                │
├─────────────────────────────────────────────────────────────────────┤
│ Layer 4: BufferQueue (生产者-消费者)                                  │
│                                                                     │
│  App (Producer):  queueBuffer → dequeueBuffer                      │
│  SF (Consumer):   acquireBuffer → latchBuffer → releaseBuffer       │
│  阻塞场景: 所有 buffer slot 被占 → dequeueBuffer 等待 SF 消费       │
├─────────────────────────────────────────────────────────────────────┤
│ Layer 5: SurfaceFlinger (合成)                                       │
│                                                                     │
│  onMessageRefresh (SF 主帧循环, 由 VSYNC-sf 触发)                   │
│   ├── latchBuffers         (获取 App 提交的 buffer)                 │
│   ├── rebuildLayerStacks   (计算 Layer 可见区域/层级)               │
│   ├── prepareFrame         (决定合成策略)                            │
│   │   └── chooseCompositionStrategy                                 │
│   │       └── HwcPresentOrValidateDisplay (询问 HWC 能否 overlay)   │
│   ├── finishFrame          (执行合成)                                │
│   │   └── composeSurfaces  (GPU fallback 合成路径)                  │
│   ├── postFramebuffer      (提交合成结果到 display)                 │
│   └── postComposition      (fence 管理)                             │
│       └── present          (等待 presentFence)                      │
├─────────────────────────────────────────────────────────────────────┤
│ Layer 6: SF RenderEngine (GPU 合成回退)                              │
│                                                                     │
│  仅当 HWC 无法 overlay 合成时触发:                                   │
│  REThreaded::drawLayers → SkiaGL::drawLayers                       │
│   ├── DrawImage / FillRectOp (Skia 合成 Op)                        │
│   ├── OpsTask::onExecute                                            │
│   ├── flush + flush surface                                         │
│   └── mapExternalTextureBuffer / unmapExternalTextureBuffer         │
├─────────────────────────────────────────────────────────────────────┤
│ Layer 7: HWC → Display (硬件合成 + 显示)                             │
│                                                                     │
│  [composer-servic 线程]                                             │
│   └── PerformCommit → HWDeviceDRM::Commit                          │
│       └── AtomicCommit → DRMAtomicReq::Commit (内核 ioctl)         │
│  [HWC release 线程]                                                 │
│   └── waitForever (等待上一帧 buffer 释放)                          │
│  [crtc_commit / crtc_event 内核线程]                                │
│   └── DRM/KMS 帧提交 → Display Controller → Panel 显示             │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. 实测数据（V4 Trace: vivo V2312A, 抖音场景, 64MB, 11.5s）

### 3.1 RenderThread (pid=5497, tid=6241) — 当前工具完全缺失

| Slice | 次数 | 总耗时 | 最大单次 | 说明 |
|-------|------|--------|---------|------|
| Drawing W H | 298 | 783ms | 33.3ms | Skia 绘制操作 (最大瓶颈!) |
| eglSwapBuffersWithDamageKHR | 434 | 492ms | 31.5ms | EGL 提交 |
| queueBuffer | 868 | 464ms | 31.2ms | 提交 buffer |
| flush commands | 434 | 419ms | 26.2ms | GPU 指令提交 |
| OpsTask::onExecute | 627 | 280ms | 25.9ms | Skia GPU Op |
| dequeueBuffer | 868 | 218ms | 7.8ms | 获取空 buffer |
| FillRectOp | 930 | 80ms | 11.0ms | 矩形填充 Op |
| **shader_compile** | **9** | **68ms** | **11.4ms** | **Shader 编译! 冷启动 jank** |
| cache_miss | 9 | 66ms | 11.2ms | ShaderCache 未命中 |
| renderFrameImpl | 434 | 62ms | 1.5ms | Skia 录制 |
| syncFrameState | 434 | 32ms | 3.9ms | 同步 RenderNode |
| driver_link_program | 9 | 37ms | 6.2ms | GPU 驱动链接 shader |
| driver_compile_shader | 18 | 26ms | 2.9ms | GPU 驱动编译 shader |

### 3.2 UI Thread (pid=5497, tid=5497)

| Slice | 次数 | 总耗时 | 最大单次 | 说明 |
|-------|------|--------|---------|------|
| traversal | 429 | 1074ms | 94.0ms | View 遍历 (最大单帧瓶颈!) |
| draw | 437 | 576ms | 25.7ms | Canvas 录制 |
| postAndWait | 434 | 484ms | 25.6ms | 同步到 RT |
| binder transaction | 132 | 300ms | 18.5ms | Binder IPC |
| animation | 357 | 198ms | 17.4ms | 动画 |
| input | 119 | 166ms | 32.8ms | 触摸事件 |
| inflate | 16 | 107ms | 30.2ms | View inflate |
| measure | 63 | 59ms | 44.9ms | 测量 |

### 3.3 SurfaceFlinger (tid=1388) — 当前覆盖较好

| Slice | 次数 | 总耗时 | 最大单次 | 说明 |
|-------|------|--------|---------|------|
| present | 1678 | 3675ms | 18.0ms | presentFence 等待 |
| prepareFrame | 839 | 1274ms | 7.1ms | 合成策略 |
| chooseCompositionStrategy | 839 | 1261ms | 7.1ms | HWC/GPU 选择 |
| postComposition | 839 | 326ms | 1.6ms | 后处理 |
| composeSurfaces | 839 | 193ms | 5.0ms | GPU 合成 |
| latchBuffers | 971 | 162ms | 0.9ms | 获取 buffer |

### 3.4 GPU & HWC

| 组件 | Slice | 总耗时 | 说明 |
|------|-------|--------|------|
| GPU completion | waitForever | 708ms | GPU fence 等待 |
| HWC release | waitForever | 854ms | HWC buffer 释放 |
| composer-servic | PerformCommit | 1460ms | HWC 提交 |
| composer-servic | AtomicCommit | 1303ms | DRM ioctl |

---

## 4. 当前工具覆盖度分析

| 层级 | 当前覆盖 | 缺失的关键 Slice |
|------|---------|-----------------|
| **L1: UI Thread** | doFrame, Binder, GC | `traversal`, `input`, `animation`, `measure`, `draw`, `inflate`, `postAndWait` |
| **L2: RenderThread** | DrawFrames, flush commands, eglSwap | `Drawing`, `OpsTask::onExecute`, `shader_compile`, `cache_miss`, `prepareTree`, `allocateHelper` |
| **L3: GPU** | pin 了 GPU completion | 无关键词匹配 `waitForever`, `waiting for GPU completion` |
| **L4: BufferQueue** | 覆盖较好 | — |
| **L5: SF** | onMessageRefresh, commit, composite | `prepareFrame`, `composeSurfaces`, `latchBuffers`, `present`, `postComposition` |
| **L6: SF RenderEngine** | RenderEngine 关键词 | `REThreaded::drawLayers`, `SkiaGL::drawLayers` |
| **L7: HWC/Display** | presentFence, crtc_commit | `HWDeviceDRM::Commit`, `AtomicCommit`, `DRMAtomicReq`, `waitForever` |

**结论: Layer 1-3 (应用侧 + GPU) 覆盖严重不足，这恰恰是大多数 App Deadline Missed 的根因所在。**

---

## 5. 改进方案（6 个 Task）

### Task 1: 目标进程选择

**问题**: 按总 running time 选目标会选到后台进程。应改为按 **jank 帧数** 选取。

**改法**: `analyze_jank.py` 的目标选择 SQL 改用 `actual_frame_timeline_slice` 的 `jank_type != 'None'` 按 `COUNT(*)` 排序。

### Task 2: Pin 策略

**问题**: App RenderThread 没被 pin。

**改法**: `_build_pin_patterns` 强制 main thread + RenderThread 排在 Timeline 后最前面。新增 `hwuiTask` 线程发现。

### Task 3: 证据 SQL 限定进程

**问题**: 证据搜索不限进程，被无关 binder/doFrame 淹没。

**改法**: `_find_target_ts` 和 `_collect_evidence` 加 `target_pid` 参数，优先搜 target 进程，不够再 fallback 全局。

### Task 4: Detail 截图展开 RenderThread

**问题**: Pinned track 折叠看不到子 slice。

**改法**: App Deadline Missed / Buffer Stuffing 类型的 detail 截图前 `ExpandTracksByRegex('RenderThread')` 展开。

### Task 5: 关键词全 7 层扩充

**问题**: 只覆盖后 3 层。

**改法**: `KEYWORDS_BY_JANK_TYPE` 按 7 层系统性重写，每种 jank 类型都覆盖对应层的关键 slice。

### Task 6: FRAMEWORK_KB 补全

**问题**: 调用链不完整，缺 Skia/HWUI/GPU/SF 细节。

**改法**:
- App Deadline Missed: 9 步 → 14 步（完整 Skia/HWUI GPU 管线）
- SF CPU Deadline Missed: 6 步 → 10 步（含 prepareFrame/composeSurfaces）
- Display HAL: 6 步 → 9 步（含 HWDeviceDRM/AtomicCommit）
- Buffer Stuffing: 5 步 → 7 步（从 DrawFrames 到 presentFence）
- 新增 source_refs: CanvasContext.cpp, EglManager.cpp, ShaderCache.cpp

---

## 6. Shader Compilation — 一个被忽视的 Jank 源

v4 trace 中发现 **9 次 shader_compile**，每次 7-11ms（足以丢帧）。

```
[RenderThread] shader_compile  11.4ms
[RenderThread] cache_miss      11.2ms
[RenderThread] shader_compile  11.0ms
[RenderThread] cache_miss      10.6ms
...（共 9 次）
```

**机制**: 当 Skia 遇到未编译过的 GPU shader（新的圆角、阴影、模糊 effect），会在 RenderThread **同步编译**，阻塞当前帧。这在冷启动和首次进入新页面时尤其常见。

**AOSP 源码**: `frameworks/base/libs/hwui/pipeline/skia/ShaderCache.cpp`

**优化方向**:
- 使用 Vulkan pipeline cache (Android 13+)
- ShaderCache warmup: 预先渲染常用 effect 触发 shader 编译
- 减少 shader 变体: 避免在 draw 时频繁切换 blend mode / shader

---

## 7. 完整 Trace Slice 参考表

### UI Thread 关键 Slice

| Slice 名称 | 出现条件 | 超时阈值 | 说明 |
|-----------|----------|---------|------|
| `Choreographer#doFrame XXXXX` | 每帧 | > 16.6ms@60Hz | doFrame 总入口 |
| `input` | 有触摸/按键 | > 5ms | 输入事件处理 |
| `animation` | 有动画 | > 3ms | 动画计算 |
| `traversal` | 每帧 | > 8ms | View 树遍历总时间 |
| `measure` | 每帧 | > 3ms | View 测量 |
| `layout` | 需要 layout | > 2ms | View 布局 |
| `draw` | 每帧 | > 5ms | Canvas 指令录制 |
| `Record View#draw()` | 每帧 | > 2ms | 具体 View draw |
| `postAndWait` | 每帧 | > 3ms | 同步到 RenderThread |
| `inflate` | 创建 View | > 10ms | XML inflate |
| `binder transaction` | IPC 调用 | > 5ms | 同步 Binder |
| `Monitor contention` | 锁竞争 | 任何出现 | 锁等待 |

### RenderThread 关键 Slice

| Slice 名称 | 出现条件 | 超时阈值 | 说明 |
|-----------|----------|---------|------|
| `DrawFrames XXXXX` | 每帧 | > 16.6ms | HWUI 帧入口 |
| `syncFrameState` | 每帧 | > 2ms | 同步 RenderNode |
| `prepareTree` | 每帧 | > 1ms | 准备 DisplayList |
| `renderFrameImpl` | 每帧 | > 3ms | Skia 指令录制 |
| `Drawing X Y W H` | 每帧 | > 5ms | 具体 Skia 绘制 |
| `flush commands` | 每帧 | > 5ms | GPU 指令提交 |
| `OpsTask::onExecute` | 每帧 | > 3ms | GPU Op 执行 |
| `FillRectOp` | 有矩形 | > 1ms | 矩形 GPU Op |
| `TextureOp` | 有纹理 | > 1ms | 纹理 GPU Op |
| `PathStencilCoverOp` | 有路径 | > 1ms | 路径 GPU Op |
| `eglSwapBuffersWithDamageKHR` | 每帧 | > 3ms | EGL 提交 |
| `Waiting for GPU` | GPU 繁忙 | > 5ms | GPU fence 等待 |
| `queueBuffer` | 每帧 | > 2ms | 提交到 BufferQueue |
| `dequeueBuffer` | 每帧 | > 2ms | 获取空 buffer |
| `shader_compile` | 首次 shader | 任何出现 | shader 编译阻塞 |
| `cache_miss` | shader 未缓存 | 任何出现 | 伴随 shader_compile |
| `allocateHelper` | 新 buffer | > 3ms | GraphicBuffer 分配 |

### SurfaceFlinger 关键 Slice

| Slice 名称 | 出现条件 | 超时阈值 | 说明 |
|-----------|----------|---------|------|
| `onMessageRefresh` | 每帧 | > 4ms | SF 帧循环入口 |
| `latchBuffers` | 有新 buffer | > 1ms | 获取 App buffer |
| `rebuildLayerStacks` | Layer 变化 | > 1ms | 重建 Layer 层级 |
| `prepareFrame` | 每帧 | > 3ms | 合成策略 |
| `chooseCompositionStrategy` | 每帧 | > 2ms | HWC/GPU 选择 |
| `HwcPresentOrValidateDisplay` | 每帧 | > 2ms | HWC 验证 |
| `finishFrame` | 每帧 | > 2ms | 执行合成 |
| `composeSurfaces` | GPU 合成 | > 3ms | GPU fallback |
| `postFramebuffer` | 每帧 | > 1ms | 提交到 display |
| `postComposition` | 每帧 | > 1ms | fence 管理 |
| `present` | 每帧 | > 16.6ms | presentFence 等待 |

### HWC/Display 关键 Slice

| Slice 名称 | 线程 | 超时阈值 | 说明 |
|-----------|------|---------|------|
| `PerformCommit` | composer-servic | > 3ms | HWC 提交入口 |
| `HWDeviceDRM::Commit` | composer-servic | > 3ms | DRM 提交 |
| `AtomicCommit` | composer-servic | > 2ms | 原子提交 |
| `DRMAtomicReq::Commit` | composer-servic | > 1ms | 内核 ioctl |
| `waitForever` | HWC release | > 16.6ms | buffer 释放等待 |
| `crtc_commit` | kernel | — | DRM/KMS 帧提交 |

---

## 8. 工具运行方式

```bash
git clone -b feat/portrait-longshot https://github.com/flock957/render-analyze.git
cd render-analyze
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium

python3 scripts/run_workflow.py \
  --trace /path/to/trace.perfetto-trace \
  --output-dir ./output
```

3 个 Phase 自动执行（~70s），生成 HTML 报告（含竖屏长图截图 + 问题帧元数据 + 证据 + Framework 根因分析）。**全程零 LLM 参与**。
