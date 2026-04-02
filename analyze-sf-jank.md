---
name: analyze-sf-jank
type: knowledge
triggers:
  - sf jank
  - surfaceflinger
  - SF卡顿
  - display hal
---

# SurfaceFlinger Jank 分析

分析 SurfaceFlinger 层的 7 类 Jank：SF CPU/GPU 超时、Display HAL 延迟、VSync 预测错误、SF 调度异常、SF Stuffing、帧丢弃。

## 脚本

python3 /workspace/custom/skill_examples/render_skills/scripts/analyze_sf_jank.py --jank-types "SurfaceFlingerCpuDeadlineMissed,SurfaceFlingerGpuDeadlineMissed,DisplayHal,PredictionError,SurfaceFlingerScheduling,SurfaceFlingerStuffing,DroppedFrame" --port 9001

## 参数

- --jank-types: 逗号分隔的 SF Jank 类型列表
- --port: trace_processor 端口

## 输出字段

- sf_cpu: SF CPU 超时分析（主线程耗时、锁竞争、Layer 更新量）
- sf_gpu: SF GPU 超时分析（GPU 合成耗时、Layer 数量）
- display_hal: Display HAL 分析（HWC/present 事件）
- prediction_error / sf_scheduling / sf_stuffing / dropped: 各类问题帧统计
- issue_regions: 精确的问题帧时间戳（用于截图）
