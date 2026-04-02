---
name: init-render-jank-metric
type: knowledge
triggers:
  - jank metric
  - jank初始化
---

# Jank 指标初始化

初始化 Android Jank CUJ 分析指标表，为后续 Jank 类型识别和分析做准备。

## 脚本

python3 /workspace/custom/skill_examples/render_skills/scripts/init_render_jank_metric.py --port 9001

## 输出字段

- metric_initialized: 是否成功初始化
- status: ready / error
