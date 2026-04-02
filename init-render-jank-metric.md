---
name: init-render-jank-metric
type: knowledge
triggers:
  - jank metric
  - jank初始化
---

# Jank 指标初始化

运行 `android/jank/android_jank_cuj_init.sql` 初始化 Android Jank CUJ 分析指标表，为后续 Jank 类型识别和分析做准备。

## 前置条件

- trace_processor 已启动（Phase 1）
- 环境变量 `RENDER_OUTPUT` 已设置（run_analysis.py 自动处理）

## 脚本

```bash
python3 scripts/init_render_jank_metric.py --port 9001
```

## 参数

- `--port`: trace_processor 端口（默认 9001）

## 输出

输出到 `$RENDER_OUTPUT/jank_metric_init.json`：

- `metric_initialized`: true/false
- `status`: ready / error
