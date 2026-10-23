# 工具说明（所有臂相同）

- 问题目录：`problem/`（sim/、sites/、policy/、map/、baseline_scores.json）。`problem/sim/policy_api.py` 写了四个钩子的约定和 State 字段。
- 你的工作目录：`work/`，一开始是基线策略的拷贝（policy.py + params.yaml）。候选 = 一个含 policy.py 和 params.yaml 的目录。
- `baseline/`：当前基线。若基线有更新，`BASELINE_CHANGELOG.md` 会写明；`BASELINE_TARGET` 是它的标识。从 `baseline/` 或 `work/` 整目录拷贝出来的候选会带着 `manifest.yaml`（里面的 target 记着它基于哪一版），`tb fast`/`tb submit` 据此计算哈希；只拷贝单个文件时用 `--target baseline@k` 指明。
- 快档（本地、免费、不限次、单种子、抖）：`tb fast <dir> --seed N`，N 只能是 1 到 5。`--out r.json` 把逐站、逐类明细写到文件。
  `sites/` 里的负载画像和公开 trace 是前一个月的样本；正式评测用的是你们看不到的、更近的 trace，负载和机器表现都有出入。本地分数只能当方向参考，别指望它和正式分数对得上。
- 机会地图：`python problem/map/gap.py r.json` 读快档明细，写出按站点、按作业类拆开的差距（gap.md）。
- 慢档（正式评测、稀缺）：`tb submit <dir> --predict <你预期的分数> --note "..." [--direction 标签] [--parents h1,h2]`。
  分数 = −（六站平均等待秒数的平均），越高越好。必须带预测。返回 ticket。
- `tb status <ticket>`：结果在出结果时间之前不可见。`tb wait <ticket>`：一直等到结果可见再返回（默认最多 30 分钟）。`tb history`：已用配额和你能看到的历史。
- `tb final <ticket|hash>`：声明你的最终交付。
- 会话可能结束后被重新启动，新会话不记得之前做过什么：随时把计划、进展、等待中的 ticket 写进 `PLAN.md`。
- 无人在线：不要等人回答。把你的对齐内容（方向、人手、配额怎么分、什么时候调整）写进 `PLAN.md`，然后按它执行。
