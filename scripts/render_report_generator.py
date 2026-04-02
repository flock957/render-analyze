#!/usr/bin/env python3
"""Generate HTML render performance report with top-N issues and Framework root cause analysis.

V2: Only shows top 3-5 most important issues with:
- Per-issue Perfetto screenshots
- Android Framework source-level root cause analysis
- Optimization suggestions grounded in framework internals
"""
import argparse, base64, json, os, sys
from pathlib import Path
from datetime import datetime

OUTPUT_DIR = "/workspace/render_output"

SEVERITY_COLORS = {
    "high": "#ff4444",
    "medium": "#ffaa00",
    "low": "#44aa44",
    "normal": "#888888",
    "info": "#4488ff",
}

SEVERITY_LABELS = {
    "high": "严重",
    "medium": "中等",
    "low": "低",
    "normal": "正常",
}

JANK_TYPE_CN = {
    "AppDeadlineMissed": "应用侧超时",
    "App Deadline Missed": "应用侧超时",
    "BufferStuffing": "Buffer 塞满",
    "Buffer Stuffing": "Buffer 塞满",
    "SurfaceFlingerCpuDeadlineMissed": "SF 主线程 CPU 超时",
    "SurfaceFlingerGpuDeadlineMissed": "SF GPU 合成超时",
    "DisplayHal": "显示 HAL 延迟",
    "Display HAL": "显示 HAL 延迟",
    "PredictionError": "VSync 预测错误",
    "Prediction Error": "VSync 预测错误",
    "SurfaceFlingerScheduling": "SF 调度异常",
    "SurfaceFlingerStuffing": "SF 侧 Stuffing",
    "SurfaceFlinger Stuffing": "SF 侧 Stuffing",
    "DroppedFrame": "帧被丢弃",
    "Dropped Frame": "帧被丢弃",
    "Unknown": "未知原因",
    "Unknown Jank": "未知原因",
}

# ---------------------------------------------------------------------------
# Android Framework source-level root cause analysis per jank type
# ---------------------------------------------------------------------------
FRAMEWORK_ANALYSIS = {
    "app_deadline": {
        "title": "App Deadline Missed - 应用侧帧超时根因分析",
        "call_chain": [
            "VSYNC-app 信号到达",
            "Choreographer.doFrame()",
            "  → INPUT callbacks (处理触摸/按键事件)",
            "  → ANIMATION callbacks (属性动画/过渡动画)",
            "  → TRAVERSAL: ViewRootImpl.performTraversals()",
            "    → performMeasure() → performLayout() → performDraw()",
            "  → ThreadedRenderer.draw() → syncFrameState",
            "RenderThread: nSyncAndDrawFrame → issueDrawCommands",
            "RenderThread: queueBuffer → 提交给 SurfaceFlinger",
        ],
        "source_refs": [
            ("Choreographer.java", "frameworks/base/core/java/android/view/Choreographer.java",
             "doFrame() 接收 VSYNC-app 信号后依次分发 INPUT → ANIMATION → TRAVERSAL 回调。"
             "帧起点 = doFrame 开始，终点 = max(GPU完成时间, queueBuffer时间)。"
             "如果总时间超过 VSYNC 间隔 (16.6ms@60Hz / 11.1ms@90Hz)，标记为 JANK_APP_DEADLINE_MISSED。"),
            ("ViewRootImpl.java", "frameworks/base/core/java/android/view/ViewRootImpl.java",
             "performTraversals() 是帧渲染主入口，依次执行 measure → layout → draw。"
             "Trace 中看 'performTraversals' slice 内部哪个阶段耗时最长即为瓶颈。"
             "常见: measure/layout 慢 → View 层级问题; draw 慢 → Canvas 绘制过重。"),
            ("ThreadedRenderer.java", "frameworks/base/core/java/android/view/ThreadedRenderer.java",
             "draw() 将 DisplayList 同步到 RenderThread (syncFrameState)，"
             "然后 RenderThread 执行 nSyncAndDrawFrame 提交 GPU 指令。"
             "Trace 中看 'syncFrameState' 耗时 → 主线程和 RenderThread 的同步开销。"),
        ],
        "trace_diagnosis": [
            "在 Perfetto 中定位 Actual Timeline 的红色帧，查看对应的 `Choreographer#doFrame` slice",
            "展开 doFrame 内部: 检查 input/animation/traversal 各阶段耗时占比",
            "检查 `performTraversals` 内 measure vs layout vs draw 哪个最长",
            "检查 RenderThread 的 `DrawFrame` / `syncFrameState` 耗时",
            "检查主线程是否有 `Binder.transact`、`GC`、`JIT compiling` 等阻塞 slice",
            "检查线程状态: Running (绿色) vs Sleeping (蓝色) vs Runnable (白色) vs Uninterruptible (橙色)",
        ],
        "root_causes": [
            "**Measure/Layout 过重**: View 层级深、RelativeLayout 嵌套、RecyclerView 多类型 item",
            "**Draw 过重**: Canvas.drawBitmap/drawPath 指令多, 自定义 View onDraw 复杂",
            "**Input/Animation 回调耗时**: 触摸事件处理或动画计算占用了大部分帧时间",
            "**主线程 I/O 阻塞**: SharedPreferences.commit()、数据库查询、文件读写",
            "**主线程 Binder 调用**: 同步 IPC 等待远端进程响应 (ContentProvider/Service)",
            "**GC / JIT**: 运行时垃圾回收暂停 (concurrent GC 影响小但 full GC 影响大)，JIT 编译暂停",
            "**锁竞争**: synchronized/ReentrantLock 等待其他线程释放锁",
        ],
        "optimizations": [
            "使用 `ConstraintLayout` 减少嵌套层级，避免 `RelativeLayout` 嵌套导致双 measure",
            "RecyclerView: `setHasFixedSize(true)` + DiffUtil + 预创建 ViewHolder",
            "将耗时 Bitmap 解码移到子线程，使用 Glide/Coil 异步加载",
            "主线程 I/O: SharedPreferences.commit() → apply(), 数据库操作移到子线程",
            "使用 `ViewPropertyAnimator` 或 `RenderThread` 动画替代主线程动画",
            "减少 `Canvas.saveLayer()` 调用（触发 offscreen buffer 分配）",
            "使用 Systrace/Perfetto 标记 `Trace.beginSection()` 定位业务代码瓶颈",
        ],
    },
    "buffer_stuffing": {
        "title": "Buffer Stuffing - BufferQueue 塞满根因分析",
        "call_chain": [
            "App RenderThread: nSyncAndDrawFrame → issueDrawCommands",
            "App RenderThread: Surface.queueBuffer() → 提交 buffer 给 BufferQueue",
            "App RenderThread: Surface.dequeueBuffer() → 尝试获取下一个空 buffer",
            "  → BufferQueueProducer.dequeueBuffer() 阻塞（所有 slot 被占）",
            "SF: onMessageRefresh → acquireBuffer() → latchBuffer() → 消费 buffer",
        ],
        "source_refs": [
            ("BufferQueueProducer.cpp", "frameworks/native/libs/gui/BufferQueueProducer.cpp",
             "dequeueBuffer() 在 BufferQueue 所有 slot 被占用时阻塞。"
             "Triple buffering (3 buffers) 下，如果 SF 合成慢导致前两帧还没消费，"
             "第三帧 dequeue 就会阻塞 App 的 RenderThread。"
             "Trace 中表现为 'dequeueBuffer' slice 持续 > 5ms。"),
            ("BufferLayerConsumer.cpp", "frameworks/native/libs/gui/BufferLayerConsumer.cpp",
             "SurfaceFlinger 在 onMessageRefresh 中调用 acquireBuffer 消费 buffer。"
             "如果 SF 侧合成延迟（Display HAL 或 GPU 合成慢），消费速度跟不上生产速度。"),
        ],
        "trace_diagnosis": [
            "检查 RenderThread 的 `dequeueBuffer` slice 耗时（正常 < 1ms，阻塞时 > 5ms）",
            "检查 Actual Timeline: 帧是否标记为 'Late Present' 但 on_time_finish=true",
            "检查 SF Actual Timeline 是否有对应的延迟（黄色帧表示 SF 导致的卡顿）",
            "检查 SF 的 `onMessageRefresh` / `commit` / `composite` 总耗时",
            "通常与 Display HAL / SF Stuffing 同时出现，需要联合分析",
        ],
        "root_causes": [
            "**SurfaceFlinger 消费慢**: SF 合成时间长（层数多/GPU 合成回退），buffer 消费速度低于生产速度",
            "**Display HAL 级联**: presentFence 延迟 → buffer 无法释放 → dequeueBuffer 阻塞",
            "**App 连续快速渲染**: fling/动画场景下 App 渲染速度 > SF 消费速度",
            "**GPU 合成回退**: HWC 无法处理某些 Layer，回退到 GPU 合成导致 SF 耗时增加",
        ],
        "optimizations": [
            "优先排查 Display HAL / SF 侧延迟 — Buffer Stuffing 通常是下游问题的级联",
            "减少 Layer 数量降低 SF 合成时间",
            "检查 `dumpsys SurfaceFlinger --comp-type` 确认是否有 GPU 合成回退",
            "如果 App 侧无 deadline missed，问题主要在 SF/Display 侧",
        ],
    },
    "display_hal": {
        "title": "Display HAL - 显示硬件延迟根因分析",
        "call_chain": [
            "SurfaceFlinger.onMessageRefresh()",
            "  → commit() → composite() → presentDisplay()",
            "HWComposer.presentAndGetReleaseFences()",
            "HWC HAL: presentDisplay() → 提交帧到显示控制器",
            "Kernel: DRM/KMS → Display Controller → Panel",
            "返回 presentFence → SF 在下一帧等待此 fence",
        ],
        "source_refs": [
            ("HWComposer.cpp", "frameworks/native/services/surfaceflinger/DisplayHardware/HWComposer.cpp",
             "presentAndGetReleaseFences() 调用 HWC HAL 的 presentDisplay()，"
             "HAL 返回一个 presentFence。SF 在下一帧开始时等待这个 fence 信号。"
             "Trace 中关键 slice: 'waiting for presentFence NNN'（NNN 是 fence ID）。"),
            ("SurfaceFlinger.cpp", "frameworks/native/services/surfaceflinger/SurfaceFlinger.cpp",
             "postComposition() 中检查 presentFence。"
             "正常 presentFence 等待 < 1ms（fence 已在上一帧完成时 signal）。"
             "如果持续 > 16ms，说明显示硬件未能在一个 VSYNC 内完成帧呈现。"),
        ],
        "trace_diagnosis": [
            "在 SF 进程中搜索 'waiting for presentFence' slice，检查耗时（正常 < 1ms）",
            "检查 SF Actual Timeline 帧颜色: 红色 = SF 导致的 jank",
            "检查 'HWC::presentDisplay' 或 'hwc_commit' slice 耗时",
            "检查 SF commit/composite 总耗时是否正常（正常 < 5ms）",
            "如果 commit/composite 正常但 presentFence 慢 → 硬件问题",
            "如果 commit/composite 也慢 → 可能是 Layer 太多导致 HWC 回退 GPU",
        ],
        "root_causes": [
            "**HWC overlay 回退**: Layer 类型/数量超出 HWC 能力，回退到 GPU 合成",
            "**DDR 带宽竞争**: 显示控制器读 framebuffer 与 CPU/GPU 内存访问竞争",
            "**Panel 刷新率切换**: 60→90→120Hz 切换导致 VSYNC 间隔不稳定",
            "**Thermal 降频**: 高温导致 GPU/Display 频率降低，帧呈现变慢",
            "**HWC 驱动 bug**: 厂商 HWC HAL 实现问题（常见于低端机/旧驱动）",
        ],
        "optimizations": [
            "检查 HWC 合成方式: `dumpsys SurfaceFlinger --comp-type` 确认是否 GPU 回退",
            "减少 overlay layer 数量，确保关键 layer 走 HWC 硬件合成",
            "检查 DDR 频率: `cat /sys/class/devfreq/*/cur_freq`",
            "排查 thermal: `dumpsys thermalservice` 看是否触发降频",
            "联系硬件厂商确认 HWC 驱动是否有已知问题",
        ],
    },
    "sf_cpu": {
        "title": "SF CPU Deadline Missed - SurfaceFlinger 主线程超时根因分析",
        "call_chain": [
            "VSYNC-sf 信号到达",
            "SurfaceFlinger.onMessageRefresh()",
            "  → handleTransaction(): 处理 App 提交的 Surface 状态变更",
            "  → handleComposition(): 计算 Layer 可见区域/混合模式/变换矩阵",
            "  → composite(): GPU 或 HWC 合成",
            "  → postComposition(): fence 管理、帧统计",
        ],
        "source_refs": [
            ("SurfaceFlinger.cpp", "frameworks/native/services/surfaceflinger/SurfaceFlinger.cpp",
             "onMessageRefresh() 是 SF 的主帧循环，对应 VSYNC-sf 信号。"
             "当总处理时间超过 VSYNC 间隔时标记为 SF_CPU_DEADLINE_MISSED。"
             "Trace 中看 'onMessageRefresh' 或 'commit' + 'composite' slice 总时长。"),
            ("CompositionEngine.cpp", "frameworks/native/services/surfaceflinger/CompositionEngine/",
             "handleComposition 计算每个 Layer 的可见区域、混合模式。"
             "Layer 数量是核心因子 — 每多一个 Layer 约增加 0.1-0.5ms。"
             "Trace 中检查 'composite layers' 或 'RenderEngine' 相关 slice。"),
        ],
        "trace_diagnosis": [
            "在 SF 进程找 'onMessageRefresh' / 'commit' / 'composite' slice",
            "检查 'handleTransaction' 耗时 — Layer 状态变更处理",
            "检查 'composite layers' 耗时 — 合成计算",
            "检查 SF 线程状态: 是否有 Runnable (排队等 CPU) 或 Uninterruptible (等 I/O)",
            "检查 SF Binder 线程的 'setTransactionState' — 频繁事务导致锁竞争",
            "Layer 数量: `dumpsys SurfaceFlinger --list` 查看当前 Layer 列表",
        ],
        "root_causes": [
            "**Layer 数量过多**: App 大量独立 Surface (多窗口/画中画/浮窗/SurfaceView)",
            "**锁竞争**: SF 主线程等待 mStateLock，被 Binder 线程 setTransactionState 阻塞",
            "**CPU 调度**: SF 线程被高优先级中断或 RT 任务抢占，处于 Runnable 状态",
            "**GPU 合成回退**: HWC 无法合成某些 Layer，回退到 GPU (RenderEngine) 合成",
        ],
        "optimizations": [
            "减少 Layer: 合并不必要的 SurfaceView，使用 TextureView 替代",
            "检查 SF 调度: `ps -eT -o pid,tid,cls,rtprio,comm | grep surfaceflinger`",
            "减少 Binder 事务频率（减少 setTransactionState 调用）",
            "检查 GPU 合成: `dumpsys SurfaceFlinger --comp-type` 看是否有 CLIENT (GPU) 合成",
        ],
    },
    "sf_gpu": {
        "title": "SF GPU Deadline Missed - SurfaceFlinger GPU 合成超时根因分析",
        "call_chain": [
            "SurfaceFlinger.composite()",
            "  → RenderEngine::drawLayers() (GPU 合成路径)",
            "  → OpenGL/Vulkan 指令提交",
            "GPU 执行合成",
            "GPU fence signal → 合成完成",
        ],
        "source_refs": [
            ("RenderEngine.cpp", "frameworks/native/libs/renderengine/",
             "当 HWC 无法处理某些 Layer（如 YUV 格式、特殊混合模式、旋转等），"
             "SF 回退到 GPU 合成 (RenderEngine)。GPU 指令提交后通过 fence 等待完成。"
             "如果 GPU fence 未在 deadline 前 signal，标记为 SF_GPU_DEADLINE_MISSED。"),
            ("HWComposer.cpp", "frameworks/native/services/surfaceflinger/DisplayHardware/HWComposer.cpp",
             "validateDisplay() 决定每个 Layer 走 HWC overlay 还是 GPU 合成。"
             "HWC_COMPOSITION_CLIENT 表示 GPU 合成。Layer 越多/越复杂，GPU 合成越慢。"),
        ],
        "trace_diagnosis": [
            "检查 SF 的 'RenderEngine' / 'GLES' / 'drawLayers' slice 耗时",
            "检查 GPU completion track — fence signal 是否延迟",
            "检查 'validateDisplay' 后有多少 Layer 走了 CLIENT (GPU) 合成",
            "SF Actual Timeline 帧如果 on_time_finish=false 且 gpu_composition=true → GPU 瓶颈",
            "检查 GPU 频率: `cat /sys/class/devfreq/*/cur_freq` (是否被 thermal 降频)",
        ],
        "root_causes": [
            "**GPU 合成 Layer 过多**: 大量 Layer 回退到 GPU 合成，GPU 负载过重",
            "**GPU 降频**: Thermal throttling 导致 GPU 频率降低",
            "**复杂合成**: Layer 使用了 GPU 才能处理的特性（YUV、特殊 blend mode、旋转）",
            "**GPU 被 App 占用**: App 的 RenderThread GPU 工作与 SF GPU 合成竞争 GPU 资源",
        ],
        "optimizations": [
            "减少 CLIENT 合成 Layer: 避免使用 GPU-only 特性（如 SurfaceView rotation）",
            "降低分辨率或 Layer 尺寸减少 GPU 填充率压力",
            "检查 GPU 频率和 thermal: `dumpsys thermalservice`",
            "减少 overdraw: 确保不透明 Layer 设置 opaque flag",
        ],
    },
    "prediction_error": {
        "title": "Prediction Error - VSync 预测错误根因分析",
        "call_chain": [
            "VSyncPredictor.nextAnticipatedVSyncTimeFrom()",
            "  → 线性回归模型预测下一个 VSYNC 时间",
            "FrameTimeline: 比较 expectedVsync vs actualPresent",
            "  → 偏差超过阈值 → 标记 PredictionError",
        ],
        "source_refs": [
            ("VSyncPredictor.cpp", "frameworks/native/services/surfaceflinger/Scheduler/VSyncPredictor.cpp",
             "VSyncPredictor 使用线性回归模型基于历史 VSYNC 时间戳预测下一个 VSYNC。"
             "当实际 present 时间与预测偏差超过 half-VSYNC 时，标记为 PredictionError。"
             "模型需要几帧来适应刷新率变化。"),
            ("Scheduler.cpp", "frameworks/native/services/surfaceflinger/Scheduler/Scheduler.cpp",
             "Scheduler 管理 VSYNC-app 和 VSYNC-sf 的 phase offset。"
             "当刷新率切换时，phase offset 需要重新计算，过渡期容易出现预测错误。"),
        ],
        "trace_diagnosis": [
            "检查 Expected Timeline vs Actual Timeline: 帧的预期时间窗口和实际时间是否偏差大",
            "检查是否有刷新率切换事件 (60→90→120Hz)",
            "检查 VSYNC 信号间隔是否稳定",
            "PredictionError 帧通常在 Actual Timeline 显示为浅绿色（高延迟但平滑）",
            "通常是系统级问题，App 侧无法直接修复",
        ],
        "root_causes": [
            "**刷新率切换**: 60↔90↔120Hz 变化导致 VSYNC 间隔突变，预测模型来不及适应",
            "**不规则帧提交**: App 帧提交间隔不均匀，VSYNC phase offset 不准",
            "**Thermal 降频**: GPU/Display 频率变化影响 VSYNC 节奏",
            "**多进程干扰**: 多个 App 同时渲染导致 VSYNC 调度混乱",
        ],
        "optimizations": [
            "检查刷新率: `dumpsys SurfaceFlinger | grep 'active mode'`",
            "锁定刷新率避免频繁切换: `Surface.setFrameRate()`",
            "PredictionError 通常是系统级问题，App 侧可通过稳定帧率间接改善",
        ],
    },
    "sf_stuffing": {
        "title": "SF Stuffing - SurfaceFlinger 侧帧堆积根因分析",
        "call_chain": [
            "SF: 上一帧的 composite/present 还未完成",
            "  → 新的 VSYNC-sf 到达",
            "  → 新帧的 commit 被延迟",
            "Display frame 实际持续时间 > 1 VSYNC 间隔",
            "连续帧堆积 → 延迟累加 → 用户感知卡顿",
        ],
        "source_refs": [
            ("FrameTimeline.cpp", "frameworks/native/services/surfaceflinger/FrameTimeline/FrameTimeline.cpp",
             "当 SF 的 display frame 实际持续时间超过预期时，"
             "标记为 SurfaceFlingerStuffing。典型场景: 上一帧的 presentFence 延迟,"
             "导致 SF 开始处理新帧时已经晚于 VSYNC-sf。"
             "Trace 中表现为 SF Actual Timeline 连续多帧持续时间 > 16.6ms。"),
        ],
        "trace_diagnosis": [
            "检查 SF Actual Timeline: 是否有连续多帧 > 1 VSYNC 间隔",
            "检查前一帧的 'waiting for presentFence' 是否超时 — 通常是级联原因",
            "检查 SF 的 commit/composite 自身耗时是否正常",
            "如果 SF 自身工作 < 5ms 但帧持续时间 > 16ms → Display HAL 级联",
            "如果 SF 自身工作 > 16ms → SF CPU 瓶颈",
        ],
        "root_causes": [
            "**Display HAL 级联**: presentFence 延迟 → 上一帧卡住 → 新帧排队",
            "**SF CPU 瓶颈级联**: SF 合成慢 → 上一帧未完成 → 新帧 stuffing",
            "**GPU 合成延迟**: GPU fence 信号晚 → 上一帧 composite 阶段延迟",
        ],
        "optimizations": [
            "SF Stuffing 是级联效应，优先排查上游根因:",
            "  → 如果伴随 Display HAL: 排查 HWC/驱动/thermal",
            "  → 如果伴随 SF CPU: 减少 Layer 数量",
            "  → 如果伴随 SF GPU: 减少 GPU 合成回退",
        ],
    },
    "dropped": {
        "title": "Dropped Frame - 帧丢弃根因分析",
        "call_chain": [
            "App 提交帧 → queueBuffer → BufferQueue",
            "SF acquireBuffer → 获取帧",
            "帧的 target present time 已过 (错过了目标 VSYNC)",
            "BufferQueue 中有更新的帧可用",
            "SF 丢弃旧帧，使用最新帧 → 用户感知跳帧",
        ],
        "source_refs": [
            ("FrameTimeline.cpp", "frameworks/native/services/surfaceflinger/FrameTimeline/FrameTimeline.cpp",
             "Dropped Frame 是最严重的 jank 类型。当帧错过目标 VSYNC 且有更新帧时，"
             "旧帧被丢弃。Trace 中 Actual Timeline 显示为蓝色帧。"
             "用户感知: 动画/滑动中突然跳了一帧，视觉上「抖动」。"),
            ("SurfaceFlinger.cpp", "frameworks/native/services/surfaceflinger/SurfaceFlinger.cpp",
             "handlePageFlip 中，SF 从 BufferQueue acquire 最新 buffer。"
             "如果队列中有多个 pending buffer，旧 buffer 被跳过 (dropped)。"),
        ],
        "trace_diagnosis": [
            "Actual Timeline 中蓝色帧 = Dropped Frame",
            "检查被 drop 的帧对应的 App doFrame 耗时 — 是否 > 2 VSYNC",
            "检查是否有连续多帧 drop — 严重卡顿的标志",
            "检查 App 主线程是否有长 slice (Binder/GC/I/O) 阻塞了多帧",
            "检查 SF 侧是否有 Display HAL / GPU 延迟导致消费慢",
            "Dropped Frame 通常是其他 jank 类型的严重后果",
        ],
        "root_causes": [
            "**严重 App 侧 Jank**: 连续多帧 doFrame > 2 VSYNC，buffer 堆积后旧帧被丢弃",
            "**SF 侧严重延迟**: SF 合成严重滞后，多帧排队后只取最新帧",
            "**系统负载过高**: CPU/GPU 资源不足导致渲染 pipeline 整体延迟",
            "**主线程完全阻塞**: ANR 级别的阻塞 (> 100ms) 导致连续多帧被 drop",
        ],
        "optimizations": [
            "Dropped Frame 是「症状」不是「原因」— 需要先解决上游 jank:",
            "  → 检查 App doFrame 耗时，优化主线程工作量",
            "  → 检查是否有 Binder/GC/I/O 导致的主线程长阻塞",
            "  → 检查系统负载: `top -d 1` 看 CPU 使用率",
            "  → 检查 SF 侧 Display HAL / GPU 延迟",
        ],
    },
}


def load_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def load_screenshots(output_dir: Path) -> dict[str, str]:
    """Load screenshots as base64 strings, keyed by screenshot name."""
    manifest_path = output_dir / "screenshots" / "screenshot_manifest.json"
    if not manifest_path.exists():
        return {}
    manifest = json.loads(manifest_path.read_text())
    if manifest.get("skipped_reason") or manifest.get("captured", 0) == 0:
        return {}
    screenshots = {}
    for shot in manifest.get("screenshots", []):
        if not shot.get("success") or not shot.get("file"):
            continue
        img_path = output_dir / "screenshots" / shot["file"]
        if img_path.exists():
            img_data = base64.b64encode(img_path.read_bytes()).decode()
            screenshots[shot["name"]] = img_data

    # Also load multi-shot files (e.g. 00_App Jank Frame #123_0.png, _1.png)
    screenshots_dir = output_dir / "screenshots"
    if screenshots_dir.exists():
        for img_file in sorted(screenshots_dir.glob("*.png")):
            if img_file.name not in [s.get("file") for s in manifest.get("screenshots", [])]:
                img_data = base64.b64encode(img_file.read_bytes()).decode()
                screenshots[img_file.stem] = img_data

    return screenshots


def severity_badge(severity: str) -> str:
    color = SEVERITY_COLORS.get(severity, "#888")
    label = SEVERITY_LABELS.get(severity, severity)
    return f'<span class="badge" style="background:{color}">{label}</span>'


def screenshot_html(screenshots: dict, name_keywords: list[str]) -> str:
    """Find and render ALL screenshots matching keywords (supports multi-shot)."""
    matched = []
    for key, b64 in screenshots.items():
        for kw in name_keywords:
            if kw.lower() in key.lower():
                matched.append((key, b64))
                break
    if not matched:
        return '<div class="no-screenshot">截图未生成</div>'
    html = ''
    for key, b64 in matched:
        html += f'''
            <div class="screenshot">
                <img src="data:image/png;base64,{b64}" alt="{key}"
                     onclick="this.classList.toggle('expanded')"
                     title="点击查看大图 / Click to enlarge" />
                <p class="screenshot-label">Perfetto 截图: {key}</p>
            </div>'''
    return html
    return '<div class="no-screenshot">截图未生成 (可能 pin 或导航失败)</div>'


def _classify_issue_category(name: str) -> str:
    """Classify an issue name to a framework analysis category."""
    name_lower = name.lower()
    if "app jank" in name_lower or "app deadline" in name_lower:
        return "app_deadline"
    if "buffer stuffing" in name_lower:
        return "buffer_stuffing"
    if "display hal" in name_lower:
        return "display_hal"
    if "sf cpu" in name_lower:
        return "sf_cpu"
    if "sf gpu" in name_lower:
        return "sf_cpu"  # similar analysis
    if "prediction" in name_lower:
        return "prediction_error"
    if "sf stuffing" in name_lower or "surfaceflinger stuffing" in name_lower:
        return "sf_stuffing"
    if "dropped" in name_lower:
        return "dropped"
    return "app_deadline"


def framework_analysis_html(category: str) -> str:
    """Generate HTML for Android Framework root cause analysis."""
    info = FRAMEWORK_ANALYSIS.get(category)
    if not info:
        return ""

    html = f'<div class="framework-analysis">'
    html += f'<h4>Android Framework 根因分析</h4>'

    # Call chain
    html += '<div class="call-chain"><h5>调用链路</h5><div class="chain">'
    for i, step in enumerate(info["call_chain"]):
        if i > 0:
            html += '<span class="chain-arrow">→</span>'
        html += f'<span class="chain-step">{step}</span>'
    html += '</div></div>'

    # Source references
    html += '<div class="source-refs"><h5>源码分析</h5>'
    for fname, fpath, desc in info["source_refs"]:
        html += f'''
        <div class="source-ref">
            <div class="source-file"><code>{fname}</code>
                <span class="source-path">{fpath}</span>
            </div>
            <p>{desc}</p>
        </div>'''
    html += '</div>'

    # Trace diagnosis guide
    trace_diag = info.get("trace_diagnosis", [])
    if trace_diag:
        html += '<div class="trace-diagnosis"><h5>Perfetto Trace 诊断指南</h5><ul>'
        for step in trace_diag:
            html += f'<li>{step}</li>'
        html += '</ul></div>'

    # Root causes
    html += '<div class="root-causes"><h5>可能的根因</h5><ul>'
    for cause in info["root_causes"]:
        html += f'<li>{cause}</li>'
    html += '</ul></div>'

    # Optimizations
    html += '<div class="optimizations"><h5>优化建议</h5><ul class="opt-list">'
    for opt in info["optimizations"]:
        html += f'<li>{opt}</li>'
    html += '</ul></div>'

    html += '</div>'
    return html


def _collect_top_issues(jank_types_data, app_jank_data, sf_jank_data, top_n=5):
    """Collect and rank top N issues across all analysis data.

    Returns list of dicts with: name, category, severity, dur_ms, details, keywords
    """
    issues = []

    # App Jank issues
    if app_jank_data and app_jank_data.get("has_issue"):
        adm = app_jank_data.get("app_deadline_missed")
        if adm and adm.get("top_frames"):
            top_frame = adm["top_frames"][0]
            issues.append({
                "name": f"App Deadline Missed (Frame #{top_frame.get('id', '?')})",
                "category": "app_deadline",
                "severity": "high",
                "dur_ms": top_frame.get("actual_dur_ms", top_frame.get("dur", 0) / 1e6),
                "frame_count": adm.get("jank_frames", 0),
                "details": adm,
                "keywords": ["App Jank", "app_deadline"],
            })

        bs = app_jank_data.get("buffer_stuffing")
        if bs and bs.get("top_frames"):
            top_frame = bs["top_frames"][0]
            issues.append({
                "name": f"Buffer Stuffing (Frame #{top_frame.get('id', '?')})",
                "category": "buffer_stuffing",
                "severity": "medium",
                "dur_ms": top_frame.get("dur_ms", top_frame.get("dur", 0) / 1e6),
                "frame_count": bs.get("jank_frames", 0),
                "details": bs,
                "keywords": ["Buffer Stuffing", "buffer_stuffing"],
            })

    # SF Jank issues
    if sf_jank_data and sf_jank_data.get("has_issue"):
        sf_issue_map = {
            "display_hal": ("Display HAL Jank", "display_hal", "high"),
            "sf_cpu": ("SF CPU Deadline Missed", "sf_cpu", "high"),
            "sf_gpu": ("SF GPU Deadline Missed", "sf_cpu", "high"),
            "sf_stuffing": ("SF Stuffing", "sf_stuffing", "medium"),
            "prediction_error": ("VSync Prediction Error", "prediction_error", "medium"),
            "dropped": ("Dropped Frame", "dropped", "high"),
            "sf_scheduling": ("SF Scheduling", "sf_cpu", "medium"),
        }
        for key, (title, category, default_sev) in sf_issue_map.items():
            data = sf_jank_data.get(key)
            if not data or not data.get("top_frames"):
                continue
            top_frame = data["top_frames"][0]
            issues.append({
                "name": f"{title} (Token #{top_frame.get('display_frame_token', '?')})",
                "category": category,
                "severity": default_sev,
                "dur_ms": top_frame.get("dur_ms", top_frame.get("dur", 0) / 1e6),
                "frame_count": data.get("jank_frames", 0),
                "details": data,
                "keywords": [title, key, title.split()[0]],  # e.g. "SF" for partial match
            })

    # Sort: severity (high first), then duration descending
    severity_order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda i: (severity_order.get(i["severity"], 3), -i["dur_ms"]))

    return issues[:top_n]


def generate_html(output_dir: Path, top_n: int = 5) -> str:
    jank_types_data = load_json(output_dir / "jank_types.json")
    app_jank_data = load_json(output_dir / "app_jank.json")
    sf_jank_data = load_json(output_dir / "sf_jank.json")
    screenshots = load_screenshots(output_dir)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Android 渲染性能分析报告</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif; background: #0d1117; color: #e6edf3; line-height: 1.6; }}
.container {{ max-width: 1200px; margin: 0 auto; padding: 24px; }}
h1 {{ font-size: 28px; margin-bottom: 8px; color: #fff; }}
h2 {{ font-size: 22px; margin: 32px 0 16px; padding-bottom: 8px; border-bottom: 1px solid #30363d; color: #58a6ff; }}
h3 {{ font-size: 18px; margin: 24px 0 12px; color: #e6edf3; }}
h4 {{ font-size: 16px; margin: 20px 0 10px; color: #79c0ff; }}
h5 {{ font-size: 14px; margin: 14px 0 8px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }}
.subtitle {{ color: #8b949e; font-size: 14px; margin-bottom: 24px; }}
.badge {{ display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 12px; font-weight: 600; color: #fff; margin-left: 8px; }}
.card {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; padding: 20px; margin-bottom: 20px; }}
.card-header {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; flex-wrap: wrap; gap: 8px; }}
table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 14px; }}
th {{ background: #21262d; padding: 10px 12px; text-align: left; color: #8b949e; font-weight: 600; }}
td {{ padding: 8px 12px; border-bottom: 1px solid #21262d; }}
tr:hover td {{ background: #161b22; }}
.stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin: 16px 0; }}
.stat-item {{ background: #21262d; border-radius: 8px; padding: 16px; text-align: center; }}
.stat-value {{ font-size: 32px; font-weight: 700; color: #58a6ff; }}
.stat-label {{ font-size: 13px; color: #8b949e; margin-top: 4px; }}

/* Screenshot */
.screenshot {{ text-align: center; margin: 16px 0; }}
.screenshot img {{
    max-width: 100%; border: 1px solid #30363d; border-radius: 8px;
    cursor: pointer; transition: all 0.3s ease;
}}
.screenshot img:hover {{ border-color: #58a6ff; box-shadow: 0 0 12px rgba(88,166,255,0.3); }}
.screenshot img.expanded {{
    position: fixed; top: 2vh; left: 2vw; width: 96vw; height: 96vh;
    object-fit: contain; z-index: 9999; background: rgba(0,0,0,0.95);
    border-radius: 12px; border: 2px solid #58a6ff;
}}
.screenshot-label {{ font-size: 12px; color: #8b949e; margin-top: 6px; }}
.no-screenshot {{ color: #484f58; font-style: italic; padding: 12px; text-align: center; }}

/* Framework Analysis */
.framework-analysis {{
    background: #0d1117; border: 1px solid #1f3a5f; border-radius: 8px;
    padding: 16px; margin: 16px 0;
}}
.call-chain {{ margin: 12px 0; }}
.chain {{ display: flex; flex-wrap: wrap; align-items: center; gap: 4px; padding: 8px; background: #161b22; border-radius: 6px; }}
.chain-step {{ background: #1a2332; padding: 4px 10px; border-radius: 4px; font-family: monospace; font-size: 13px; color: #79c0ff; white-space: nowrap; }}
.chain-arrow {{ color: #484f58; font-weight: bold; }}
.source-refs {{ margin: 12px 0; }}
.source-ref {{ margin: 10px 0; padding: 10px; background: #161b22; border-radius: 6px; border-left: 3px solid #1f6feb; }}
.source-file {{ margin-bottom: 6px; }}
.source-file code {{ color: #79c0ff; font-weight: 600; font-size: 14px; }}
.source-path {{ color: #484f58; font-size: 12px; margin-left: 8px; }}
.source-ref p {{ font-size: 13px; line-height: 1.5; color: #c9d1d9; }}
.trace-diagnosis {{ margin: 12px 0; }}
.trace-diagnosis ul {{ list-style: none; padding: 0; }}
.trace-diagnosis li {{ padding: 5px 0 5px 20px; font-size: 13px; color: #c9d1d9; position: relative; border-bottom: 1px solid #1a2332; }}
.trace-diagnosis li::before {{ content: ">>"; position: absolute; left: 0; color: #58a6ff; font-family: monospace; }}
.root-causes ul {{ list-style: none; padding: 0; }}
.root-causes li {{ padding: 6px 0; font-size: 14px; border-bottom: 1px solid #21262d; }}
.root-causes li:last-child {{ border-bottom: none; }}
.optimizations .opt-list {{ list-style: none; padding: 0; }}
.optimizations .opt-list li {{ padding: 6px 0 6px 20px; font-size: 14px; position: relative; }}
.optimizations .opt-list li::before {{ content: ">>"; position: absolute; left: 0; color: #3fb950; }}

/* Issue number badge */
.issue-num {{ display: inline-flex; align-items: center; justify-content: center; width: 28px; height: 28px; border-radius: 50%; background: #1f6feb; color: #fff; font-weight: 700; font-size: 14px; margin-right: 8px; }}

footer {{ text-align: center; padding: 32px 0; color: #484f58; font-size: 13px; border-top: 1px solid #21262d; margin-top: 40px; }}
</style>
</head>
<body>
<div class="container">
<h1>Android 渲染性能分析报告</h1>
<p class="subtitle">生成时间: {now} | HiClaw Render Performance Analyzer</p>
"""

    # --- Overview Stats ---
    if jank_types_data:
        total = jank_types_data.get("total_frames", 0)
        jank_count = jank_types_data.get("jank_frame_count", 0)
        jank_rate = jank_types_data.get("jank_rate_pct", 0)
        severity = jank_types_data.get("severity", "normal")
        detected_types = jank_types_data.get("detected_types", [])

        html += f"""
<h2>概览</h2>
<div class="stat-grid">
    <div class="stat-item">
        <div class="stat-value">{total}</div>
        <div class="stat-label">总帧数</div>
    </div>
    <div class="stat-item">
        <div class="stat-value" style="color:{SEVERITY_COLORS.get(severity, '#fff')}">{jank_count}</div>
        <div class="stat-label">Jank 帧数</div>
    </div>
    <div class="stat-item">
        <div class="stat-value" style="color:{SEVERITY_COLORS.get(severity, '#fff')}">{jank_rate:.1f}%</div>
        <div class="stat-label">Jank 率</div>
    </div>
    <div class="stat-item">
        <div class="stat-value">{len(detected_types)}</div>
        <div class="stat-label">Jank 类型数</div>
    </div>
</div>
"""
        # Jank type summary table (compact)
        jt_list = jank_types_data.get("jank_types", [])
        if jt_list:
            # Only show top types by frame count
            jt_sorted = sorted(jt_list, key=lambda x: -x.get("frame_count", 0))[:8]
            html += """
<div class="card">
<h3>Jank 类型分布 (Top)</h3>
<table>
<tr><th>类型</th><th>帧数</th><th>平均耗时</th><th>严重程度</th></tr>
"""
            for jt in jt_sorted:
                jtype = jt.get("jank_type", "?")
                cn = JANK_TYPE_CN.get(jtype, jtype)
                html += f"""<tr>
    <td>{cn} <code style="color:#484f58;font-size:11px">{jtype}</code></td>
    <td>{jt.get('frame_count', 0)}</td>
    <td>{jt.get('avg_dur_ms', 0):.1f} ms</td>
    <td>{severity_badge(jt.get('severity', 'normal'))}</td>
</tr>"""
            html += "</table></div>"

    # --- Top Issues with Framework Analysis ---
    top_issues = _collect_top_issues(jank_types_data, app_jank_data, sf_jank_data, top_n)

    if top_issues:
        html += f'<h2>Top {len(top_issues)} 重点问题分析</h2>'

        for idx, issue in enumerate(top_issues, 1):
            category = issue["category"]
            severity = issue["severity"]
            dur_ms = issue["dur_ms"]
            frame_count = issue.get("frame_count", 0)
            details = issue.get("details", {})

            html += f'''
<div class="card">
<div class="card-header">
    <h3><span class="issue-num">{idx}</span>{issue["name"]} {severity_badge(severity)}</h3>
    <span>{frame_count} 帧受影响 | 最长 {dur_ms:.1f}ms</span>
</div>
'''
            # Key metrics for this issue
            if category == "app_deadline":
                adm = details
                html += '<div class="stat-grid">'
                if adm.get("doframe_over_16ms", 0) > 0:
                    html += f'<div class="stat-item"><div class="stat-value">{adm["doframe_over_16ms"]}</div><div class="stat-label">doFrame > 16ms</div></div>'
                if adm.get("draw_over_16ms", 0) > 0:
                    html += f'<div class="stat-item"><div class="stat-value">{adm["draw_over_16ms"]}</div><div class="stat-label">DrawFrame > 16ms</div></div>'
                if adm.get("gpu_wait_events", 0) > 0:
                    html += f'<div class="stat-item"><div class="stat-value">{adm["gpu_wait_events"]}</div><div class="stat-label">GPU Wait</div></div>'
                html += '</div>'

                # Top frames table
                top_frames = adm.get("top_frames", [])[:3]
                if top_frames:
                    html += '<h4>Top 超时帧</h4><table><tr><th>Frame ID</th><th>耗时</th><th>类型</th></tr>'
                    for f in top_frames:
                        html += f'<tr><td>#{f.get("id","?")}</td><td>{f.get("actual_dur_ms",0):.1f}ms</td><td>{f.get("jank_type","")}</td></tr>'
                    html += '</table>'

            elif category == "buffer_stuffing":
                bs = details
                html += '<div class="stat-grid">'
                html += f'<div class="stat-item"><div class="stat-value">{bs.get("dequeue_blocked", 0)}</div><div class="stat-label">dequeueBuffer 阻塞</div></div>'
                html += f'<div class="stat-item"><div class="stat-value">{bs.get("queue_overflow", 0)}</div><div class="stat-label">Buffer Queue 溢出</div></div>'
                html += '</div>'

            elif category == "display_hal":
                dh = details
                html += f'<p>HWC 事件数: {dh.get("hwc_events", 0)}</p>'
                top_hwc = dh.get("top_hwc", [])[:3]
                if top_hwc:
                    html += '<h4>Top presentFence 等待</h4><table><tr><th>Fence</th><th>等待时间</th></tr>'
                    for h in top_hwc:
                        html += f'<tr><td>{h.get("name","?")}</td><td>{h.get("dur_ms",0):.1f}ms</td></tr>'
                    html += '</table>'

            else:
                # Generic: show top frames
                top_frames = details.get("top_frames", [])[:3]
                if top_frames:
                    html += '<h4>Top 问题帧</h4><table><tr><th>Frame Token</th><th>耗时</th></tr>'
                    for f in top_frames:
                        token = f.get("display_frame_token", f.get("id", "?"))
                        dur = f.get("dur_ms", f.get("dur", 0) / 1e6)
                        html += f'<tr><td>#{token}</td><td>{dur:.1f}ms</td></tr>'
                    html += '</table>'

            # Screenshot for this issue
            html += screenshot_html(screenshots, issue.get("keywords", [issue["name"]]))

            # Framework root cause analysis
            html += framework_analysis_html(category)

            html += '</div>'  # end card

    # --- Footer ---
    html += f"""
</div>
<footer>
    Generated by HiClaw Render Performance Analyzer | {now}<br>
    Android Framework source references based on AOSP main branch
</footer>
</body>
</html>"""

    return html


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default=OUTPUT_DIR)
    parser.add_argument("--top-n", type=int, default=5, help="Number of top issues to show")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    os.makedirs(output_dir, exist_ok=True)

    html = generate_html(output_dir, top_n=args.top_n)
    report_path = output_dir / "render_report.html"
    report_path.write_text(html, encoding="utf-8")

    print(f"[report] Render report generated: {report_path}")
    print(json.dumps({"report": str(report_path), "status": "ok"}))

if __name__ == "__main__":
    main()
