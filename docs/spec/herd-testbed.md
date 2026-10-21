# Herd 测试台：先在两个小问题上验证 herd 的行为

Research Herd 的测试台：在便宜的问题上先验证 herd 的行为 · Oct 18, 2026 · @Minyi

## 0. 一页纸版

想象一下最坏的情况：herd 搭好了，直接挂到一个真项目上，跑了两天，最后发现 worker 之间根本没有互相用上对方的发现——每个人各干各的，最后交上来的就是跑得最好的那一个人的结果。这两天白花了，而且我们说不清是哪一环坏的。

测试台就是为了避免这件事。它用一个便宜的小问题，复刻真问题的三个特征——**可以试的方向很多、每试一次要等很久、最后的好结果要由不同方向的改进拼起来**——然后让 herd 在上面跑几个小时到两天，告诉我们：herd 的行为像不像我们设计的那样。

它不是什么：不是 herd 的一部分，不是防作弊的安全系统，不是性能 benchmark。它是我们自己设计的护栏。

两个问题。**A 是仿真器里的集群调度策略**：方向多（排队、准入、放置、抢占、按站点调），每次正式评测 10 分钟、全队每小时只有 2 次，噪声我们说了算，每次评测免费。**B 是一个故意弄坏了五处的 coding agent harness**：就是我们最终要用 herd 干的事的缩小版，每次评测要花真钱、噪声大。先 A 后 B：A 便宜，管道有问题几小时就能看出来；B 真实，A 过了再上。

怎么算过：同样的正式评测配额、同样的时间，herd 要赢过"一个 agent 单干"和"N 个 agent 各干各的"两个对照组，而且第 6 节记分卡上的行为指标达标。赢了但行为不对，等于没赢。

读法：第 1 到 5 节给人看，讲为什么和是什么；第 6 到 10 节讲怎么量、怎么建、先怎么定；第 11 节是给实现的 agent 的规格，仓库结构、接口、字段、埋坑的具体做法、怎么验收测试台自己，都在那里。附录说明它是从上一版"Ledger"砍出来的，砍了什么。

## 1. 什么问题才值得拿来测 herd

一开始我们以为"材料多到一个 agent 读不完"就够了。错了。一个会用 grep、会抽样、会写脚本的 agent 对付几十万 token 的材料并不难，强模型第一反应就是这么干。拿这种问题测，单 agent 会表现得很好，herd 赢不了，我们还以为是 herd 的问题。

站得住的理由只有一个：**要迭代才知道行不行的方向数 × 每一轮要等的时间，超过了可用的墙钟时间。** 一个 agent 只能一个方向一个方向串行试；10 个方向、每个要 5 轮、每轮等 10 分钟，再加上思考和写代码，48 小时根本不够。herd 能并行。

但光并行不够——N 个互不通信的 agent 也能并行，这正是对照组之一。herd 要赢它，只能靠三件事：不同方向找到的改进**能拼到一个交付里**；一个人撞过的死胡同**别人不再撞**；全队共享的评测配额**往有苗头的方向倾斜**。所以问题还得满足：改进确实可组合（有的组合加分，有的打架），确实有陷阱（看起来好其实坏），确实有配额压力。

另外两条：eval 得能模拟成"贵、慢、少"；我们得知道答案，至少知道一部分，否则没法判断 herd 的行为是对是错，只能看分数高低。

按这几条，之前的三个候选都淘汰了：合成的 80 个旋钮、埋了慢点的代码库、多服务事故归因——它们的难点都是"材料多"，一个 agent 用工具都能搞定。

## 2. 测试台的五个零件

&#91;embedded content: 测试台 · 五个零件和三个臂\]

先看图。worker 本地刷快档，正式分数只从右边的慢档来；三个框做的每件事都落进下面的记录器，记分卡只从记录器算，三个臂并排比。

**被优化的东西。** 一个目录，agent 拿到的就是它加一段 PI prompt。里面分清楚哪些能改、哪些不能改，附基线版本和基线分数。问题 A 是仿真器加策略，问题 B 是 harness 加题集。

**快档 eval。** worker 在自己机器上跑的一条命令。免费、不限次、用公开数据、结果抖。就像工程师本地跑测试：便宜，但不算数。

**慢档 eval。** 一个 HTTP 服务，是 herd 眼里的"外部 eval"，和将来接真项目时平台组的 benchmark 一个样子。它排队、按全队共享的配额放行、用 worker 看不到的隐藏数据、跑多个种子或多遍、10 分钟后出结果，结果里带置信区间和逐项明细。接口只有三个：

```
submit(candidate, predicted_score, note) -> ticket     # 提交，必须带预测分数
status(ticket) -> queued | running | {score, ci, per_item, constraint_ok}
history() -> 已用配额、历史提交和结果（各干各的臂只看自己的）          # 另有 runs / events / meta / final，见 11.3
```

**记录器。** 一个只追加的 jsonl 文件，三处来源自动写入：慢档服务写每次提交和结果；herd 的工具层写每次黑板读写、fork、分配、放弃；worker 自报每次快档运行。记分卡全部从这个文件算，不靠人翻日志。

**记分卡和对照组。** 跑完以后一个脚本从记录器算出第 6 节的指标，三个臂并排。

防作弊只留三条，够用就行：隐藏数据和打分代码只在慢档服务的机器上，worker 的机器上没有；每次运行用全新的仓库和命名空间，上一次的东西带不过来；worker 没有 git push 权限，只能通过 herd 的工具写黑板。

**和 herd 的接口。** herd 的 Eval service 就是慢档服务的一个客户端，一两百行。因为接口和真外部 eval 一样，将来换真项目时 herd 一行不改，只换慢档服务后面的实现。

## 3. 问题 A：仿真器里的调度策略

**是什么。** 一个用 SimPy 写的集群调度仿真器，源码公开。6 个数据中心，每个几十台机器、两天的任务流。worker 要写的是调度策略：一个 Python 文件实现四个钩子，加一个参数文件。

```
order(queue, state)       -> 排队顺序
admit(job, state)         -> 现在放不放进来
place(job, free, state)   -> 放到哪台机器
preempt(running, state)   -> 要不要抢占谁
params.yaml               -> 上面四个钩子用的数字
```

策略在子进程里跑，只能看到当前时刻的队列和机器状态，看不到未来的任务；每步决策限时 50 毫秒，超时这一步按基线处理，超时超过 1% 整次作废。

**目标。** 平均任务等待时间越低越好。硬约束：6 个站点里任何一个的 p99 等待时间不能比基线差；违反的候选分数作废，结果里标"违约"。

**6 个站点各不一样，这就是埋进去的答案。** 每个站点的负载特征对应一种有效做法，一共 5 种配料，分属不同方向：

| 站点 | 负载特征 | 对它有效的做法 |
| --- | --- | --- |
| 1 | 任务时长重尾，少数任务占大头 | 回填：让短任务绕过排在前面的长任务 |
| 2 | 早晚两个高峰，其余时间空 | 准入平滑：高峰时限流，别让机器过载变慢 |
| 3 | 任务带数据亲和，放错机器慢 3 倍 | 就近放置 |
| 4 | 海量小任务 | 打包：同一用户的小任务放同一台机器，省启动开销 |
| 5、6 | 普通，但任务自带时长估计 | 按预估时长排序（全局配料） |

另外埋两样东西。一个**陷阱**：极端的"最短任务优先"能把平均等待压得很低，但站点 1 的长任务会饿死，p99 爆掉——快档单种子看不出来，慢档一跑就违约。一对**冲突**：激进回填和严格的按用户公平配额同时开会互相抵消，各开一个都比两个一起开好。

我们手搓一个开了全部 5 种配料、参数粗调过的参考策略。它的分数定义为"100% 可吃到的改进"，herd 吃到几成就看它。

**两档评测。** 快档：`tb fast <candidate_dir> --seed 1`，公开 trace，一分钟以内，本地，不限次。慢档：隐藏 trace、20 个种子、全队每小时 2 次、提交后 10 分钟出结果（算力其实一两分钟，故意等到 10 分钟，模拟真实排队）。结果带均值、95% 区间、每站 p99、是否违约。

**配料探测器。** 不看代码，看行为。每种配料对应一个构造的小场景：比如给策略一个明显的回填机会，看它回不回填。跑 5 个场景就知道最终策略里有几种配料，不管 agent 用什么写法。

**上 herd 之前先预检。** 第 3 天拿参考策略和基线各跑 20 个种子，要求：差距至少 5 倍噪声标准差；5 种配料单独开各有可见收益；陷阱确实违约；冲突确实抵消。哪条不过就改仿真器，不动 herd。

**PI prompt。**

```
sim/ 是调度器的仿真环境（源码可读），policy/ 是线上现用的策略：一个 Python 文件 + 一个参数文件。
sites/ 是 6 个数据中心的负载画像和公开 trace，特征各不一样。

目标：平均任务等待时间越低越好，但任何一个站点的 p99 不能比现在差。
本地可以用公开 trace 随便跑单种子版（一分钟以内，抖）。正式评测是平台组的服务，用你们看不到的 trace
跑 20 个种子，10 分钟出结果，全队每小时最多 2 次，只有正式评测的数算数。你有 48 小时。

我在意的：别被单种子的好结果骗了；每次正式提交前先说你预期多少分，事后对比；
最后交给我的是一个策略，不是五个候选；哪个方向值得多投人你自己判断，但要留痕。

先对齐：你打算分哪几个方向、各派几个人、慢档配额怎么分、什么情况下你会调整。
```

**48 小时大概会怎么过。** 不是预言，是让读者有个画面。头两个小时 PI 和人对齐，分出七八个方向，每个方向 1 到 2 个 worker。worker 先读自己站点的画像，本地快档刷。第五六个小时，回填方向在站点 1 上看到大幅改进，慢档确认，广播；PI 把一个闲下来的 worker 调过去调参数。第十个小时，有人试了极端最短任务优先，快档漂亮，慢档违约，在黑板上记成死胡同；之后没人再提交它。第二十个小时开始拼：有人开一个"组合"任务，把四个站点的配料合到一个策略里，发现回填和公平配额打架，在黑板上记下取舍。最后 8 小时只做验证和收尾，交一个策略。记录器里应该能看到以上每一步。

## 4. 问题 B：故意弄坏了五处的 harness

**是什么。** 一个几百行的 coding agent harness：system prompt、三四个工具（读文件、写文件、跑测试）、主循环、上下文裁剪、重试。模型固定用 haiku。题集 `tasks/dev` 60 道，每道是一个小 Python 项目加一个失败的测试，目标是让测试通过；`tasks/holdout` 另外 60 道，worker 看不到，只在最后判定用。

**能改什么。** harness 的任何代码和 prompt。不能换模型，不能在 harness 里写死和题目有关的任何东西（这条靠人工 review 加 holdout 兜底）。

**埋的五个坑。** 从一个还不错的 harness 出发，在五个不同维度各弄坏一处，每个坑看起来都像真实的 bug 或者欠考虑：

| 坑 | 长什么样 | 哪个维度 |
| --- | --- | --- |
| 工具描述误导 | `run_tests` 的描述说"只跑指定文件"，其实跑全部，agent 据此误判 | 工具 |
| 裁剪截错地方 | 上下文满了就从末尾截，恰好把错误栈最有用的最后几行截掉 | 上下文管理 |
| 不重试 | 工具偶发失败（我们注入 5% 的随机失败）直接放弃 | 流程 |
| 一条坏指令 | system prompt 里写着"不要读超过 100 行的文件" | prompt |
| 解析 bug | 多文件 diff 只应用第一个文件 | 代码 |

弄坏前后的通过率之差就是"100% 可吃到的改进"，预期 15 到 25 个点，预检确认。陷阱和冲突这里不用埋，会自然出现：往 prompt 里塞 dev 题常见库的提示，dev 涨 holdout 不涨，记分卡的"泛化"一行会抓到；"多读文件"和"裁剪更狠"天然打架。

**噪声是这个问题的头号敌人。** 60 道题、通过率 40%，单次评测的标准差约 6 个点——什么都没改，两次差 10 个点很正常。对策三条：慢档返回逐题结果，所有比较都和基线做配对（同一道题过没过），配对能压掉大半噪声；慢档每次跑 60 题 × 2 遍；快档用固定的 15 题子集跑 1 遍，5 分钟，本地不限。

**两档评测。** 快档：15 题子集，本地，约 5 分钟，不限。慢档：60 题 × 2 遍，30 分钟，全队每小时 2 次，返回配对后相对基线的提升和区间，外加逐题明细。

**钱。** 每道题 haiku agent 跑一次大约几美分到两毛美元，一次慢档 120 次运行大约 5 到 25 美元；48 小时最多 96 次慢档，加上快档，一臂一轮 500 到 2500 美元上下。三臂各跑两遍，总共几千到一万多美元。这是 B 要放在 A 后面的原因。

**配料探测器。** 每个坑一个探针测试：构造一个会踩到这个坑的情境，跑 harness，看坑还在不在。5 个探针跑完就知道修了几个。

**预检。** 原版和埋坑版各跑 3 遍，差距至少 3 倍配对噪声；5 个坑单独修回各有可见收益。

**PI prompt。**

```
harness/ 是我们的 coding agent，模型固定 haiku，不许换。tasks/dev 有 60 道题，现在通过率 41%。
目标：把通过率提上去。最后用你们没见过的 60 道 holdout 判定。

本地可以用 tasks/dev 里固定的 15 题子集随便跑（约 5 分钟，抖）。正式评测跑全部 60 题两遍，30 分钟，
全队每小时最多 2 次，返回和基线的配对比较，只有它的数算数。你有 48 小时。

不许在 harness 里写死和题目有关的任何东西。

我在意的：每个改动说清楚为什么有效，dev 涨了 holdout 没涨我会认为是过拟合；
别被一次 15 题的结果骗了；每次正式提交前先说你预期的 dev 通过率，事后对比；最后交一个 harness，不是五个。

先对齐：你打算从哪几个维度改，各派几个人，怎么判断一个改动是真提升还是噪声。
```

## 5. 对照组和怎么判

三个臂，同样的问题目录、同样的 PI prompt、同样的正式评测配额、同样的时间：

| 臂 | 是什么 | 它代表的假设 |
| --- | --- | --- |
| 单干 | 一个 agent，独占全部配额；它的合格候选也由合并器合入（它看得见自己的基线） | "一个强 agent 够了" |
| 各干各的 | N 个 agent，拿同样的 prompt，互相看不见，配额 N 等分；合格候选由和 herd 同一份、同配置的合并器在运行中合入基线，但 agent 收不到基线前进、一直在原始基线上干；用合并器最后一版基线来判；最好的单个提交另作诊断 | "并行就够了，不需要通信" |
| herd | N 个 worker 加 PI 和黑板 | 我们的设计 |

N 取 herd 的 worker 数。每个臂跑 2 遍，因为噪声。

判定看两样：holdout 上的最终分数（报效应量和配对区间，不设"3 次里赢 3 次"之类的硬杠），以及第 6 节的记分卡。

结果怎么读——这是失败以后知道该改哪的地方：

| 分数 | 行为 | 意思 | 下一步 |
| --- | --- | --- | --- |
| herd 赢 | 达标 | 设计成立 | 上问题 B，再上真项目 |
| herd 赢 | 不达标 | 赢得可疑，可能是人多多试了几次撞上的 | 查记录器，看分数从哪来 |
| 平或输 | 达标 | herd 行为对，但问题里"拼"的分量不够，或者对照组太强 | 改问题：加强配料的可组合性，不改 herd |
| 平或输 | 不达标 | 看哪一行红 | 广度低 → PI 分方向的 prompt；拼装少 → 组合任务没触发；死胡同重撞 → 黑板读取或死胡同广播；配额纪律差 → 提交前预测没起作用；倾斜没发生 → 分配器；泛化差 → 快档过拟合 |

最后一行每个箭头后面对应 herd 文档里的一个具体部分，改完重跑同一个问题。

## 6. 记分卡

| 指标 | 怎么从记录器算 | A 的目标 | B 的目标 |
| --- | --- | --- | --- |
| 广度 | 前 25% 时间内碰过慢档的不同方向数 | ≥ 5（单干约 2 到 3） | ≥ 4 |
| 拼装 | 最终交付里探测到的配料数 | ≥ 3 | ≥ 3 |
| 吃到几成 | (最终 − 基线) ÷ (参考 − 基线)，见 11.5 | ≥ 70% | ≥ 60% |
| 死胡同重撞 | 同一个被标为死胡同的想法被其他人再次提交慢档的次数 | 0 | 0 |
| 冲突处理 | 两个冲突配料是否同时开着；或黑板上有取舍记录 | 否，或有记录 | 不适用 |
| 配额纪律 | 慢档提交里，快档结果已经为正的占比 | ≥ 80% | ≥ 80% |
| 预测误差 | 提交时预测分和实际分之差，后半程比前半程 | 缩小 | 缩小 |
| 倾斜 | 某方向出现大改进后 2 小时内，它的 worker 数有没有增加 | 有 | 有 |
| 泛化 | 隐藏数据和公开数据上的分数差 | 小于噪声 | 小于 1 倍配对噪声 |

阈值是猜的，第一次跑完就校准。

## 7. 建造顺序

| 天 | 做什么 | 结束时能看到什么 |
| --- | --- | --- |
| 1 | 仿真器、6 个站点的负载生成、基线策略、快档命令 | 本地一分钟内跑出一个分数 |
| 2 | 慢档服务（排队、配额、隐藏 trace、20 种子、区间）、记录器、配料探测器、记分卡脚本 | 提交一个策略，10 分钟后拿到结果，记录器里有一行 |
| 3 | 预检：参考策略对基线；再跑"单干"臂 24 小时 | 预检四条全过；单干臂的记分卡 |
| 4 | 合并器（11.8.1）：先进先出、rebase、配对复测、接收规则、抽查、回退、收尾；再跑"各干各的"臂 24 小时 | 合并器的脚本化测试全绿；两个对照臂的记分卡 |
| 5 到 6 | herd 接上，跑 A，两遍 | 三臂对比表 |
| 7 到 8 | 问题 B：harness、埋坑、题集、探针、预检 | 原版对埋坑版的差距确认 |
| 之后 | herd 跑 B |  |

和 herd 文档的对接：herd 文档第 14 节的"第 1 周"从这里的第 4 天之后开始（合并器建好以后），herd 第 1 周需要的 eval 就是这里的慢档服务。

## 8. 先这么定的默认选择

下面这些还没有拍板，先按最便宜的定，改起来都不贵：

| 事项 | 默认 | 为什么 | 改的代价 |
| --- | --- | --- | --- |
| 策略形式 | 代码 + 参数 | 纯参数方向太少，撑不起"方向多" | 小 |
| 仿真器 | 自己写 SimPy，约 500 行 | 能埋结构、能控噪声 | 第二阶段换 Batsim 这类真实仿真器 |
| 仿真器源码 | worker 可读 | 真实情况工程师看得到；藏的只有正式评测的 trace | 小 |
| 预算 | 48 小时、慢档全队每小时 2 次、快档不限 | 和 PI prompt 一致 | 改数字 |
| B 的模型 | haiku | 便宜；要测的是 harness 不是模型 | 改一行 |
| B 的题集 | 从公开的 Python 小 bug 数据集抽一半，LLM 造一半，全部带测试 | 快 | 换题集 |
| 慢档延迟 | 人为撑到 10 分钟 | 模拟排队 | 改一个数 |
| 对照组的 N | 等于 herd 的 worker 数 | 公平 | 改一个数 |
| 每臂遍数 | 2 | 噪声 | 钱 |

## 9. 已知的坑

- agent 钻仿真器空子：靠隐藏 trace、子进程沙箱、每步限时；另外慢档分数异常高于参考策略时自动复核
- 埋的坑被识破：坑要像真实 bug，不留注释；埋完以后 squash git 历史
- 噪声吃掉信号：第 3 天和第 7 天的预检就是为这个，不过不往下走
- herd 内部抢配额：这是 herd 自己要解决的事（PI 分配），记分卡"配额纪律"会照出来
- 快档过拟合公开数据：记分卡"泛化"一行
- 成本失控：慢档有硬上限
- "各干各的"定义不公平：它们拿同样的 PI prompt、同样的总配额，合格候选由和 herd 同一个合并器合入——合并器各臂相同，herd 赢它才说明赢在共享，不是赢在合并器
- 单干臂赢了 herd：这本身就是结论，先看记分卡找原因，不要急着把问题加大来"让 herd 赢"

## 10. 复用什么开源

| 要解决的事 | 用什么 | 备注 |
| --- | --- | --- |
| 仿真 | SimPy | 几百行就够 |
| 慢档服务 | FastAPI + Redis | 队列和配额 |
| 跑三臂、记日志、看回放 | Inspect AI（可选） | 第一版可以只用 jsonl 加一个脚本；要并排看 transcript 时再上 |
| 真实仿真器（第二阶段） | Batsim | 真实但埋不了结构 |
| 不用 | HPOBench 之类查表 benchmark | 策略空间太小，方向不够多 |

## 11. 给实现者的规格

这一节是给动手建测试台的 agent 看的。前面没定的细节这里定；这里和前面有出入的，以这里为准（11.5 是记分卡的唯一权威定义，第 6 节的表只是摘要）。这里还没定的，实现者自己定，写进仓库根目录的 `DECISIONS.md`（一行一条：定了什么、为什么、改起来贵不贵），不要回来问。记分卡的定义和三条防作弊规则不得自行改动。所有时间单位是秒，时间戳是 epoch 秒。

### 11.1 仓库结构

独立仓库 `herd-testbed`，不依赖 herd 仓库；herd 将来只通过 HTTP 和几个命令行工具连它。

```
herd-testbed/
  README.md  DECISIONS.md
  testbed/
    problem_api.py     # Problem 协议（11.2）
    slow_eval/         # 慢档：FastAPI；队列、配额、运行注册用 Redis（单机开发可用 sqlite）
    cli/               # tb fast / submit / status / history / final / pack-problem / run
    recorder/          # 事件 schema、追加写入
    scorecard/         # metrics.py：每个指标一个函数；report.py：三臂并排
    sandbox/           # 跑候选的子进程封装；B 的 test runner 也在这里（不在候选里）
    arms/              # single.py  isolated.py  herd.py（后接）  scripted/
    integrator/        # 合并器核心（11.8.1）：三个臂共用；herd 的 Integrator 也调它
  problems/
    sched/             # 问题 A：sim/  sites/  policy/{baseline.py,params.yaml}  map/  reference/  probes/  hidden/  problem.yaml
    harness/           # 问题 B：harness/  map/  tasks/dev  tasks/holdout  planted/  probes/  problem.yaml
  tests/
```

`hidden/`、`reference/`、`tasks/holdout`、`planted/`、每道题的 `tests/test_hidden.py`、慢档机器上的 `problem.yaml`（含隐藏种子）和配料探测器 probes/ 只存在于慢档服务的机器。发给 worker 的问题目录由 `tb pack-problem` 生成：排除上述全部，加入 `baseline_scores.json`（公开种子 1 到 5 上基线的快档分）和 map/（公开的第一批测量脚本：A 按站点、按作业类拆 wait time 和 p99，B 按题类拆失败；这就是设计文档 D76 机会地图的初始脚本，读快档的 per\_item 输出 gap.md）。有一个测试断言排除干净了。另有 problems/stub/，只给 11.9 的脚本化自测用：evaluate 按 manifest 里声明的配料返回固定分（score = −1000 + 100 × 配料数，含 trap\_sjf 时 constraint\_ok = false），probes 直接读 manifest，参考 = 五种配料全开。

### 11.2 问题包协议和候选格式

```python
class Problem(Protocol):
    name: str
    ingredients: list[str]      # 受控词表。A = [backfill, admission, locality, packing, est_order, trap_sjf, fairshare]
    public_seeds: list[int]     # A = [1,2,3,4,5]
    def evaluate(self, candidate_dir: Path, tier: Literal['fast','slow'], seed: int, timeout: bool = True) -> Result: ...
    def aggregate(self, results: list[Result]) -> Agg: ...            # 多种子或多遍 -> 一个数
    def constraint_ok(self, agg: Agg, baseline_agg: Agg) -> bool: ...
    def probes(self, candidate_dir: Path) -> dict[str, bool]: ...     # 配料名 -> 在不在，键正好是 ingredients

Result = {'score': float | None,                         # None = 作废（超时或崩溃），原因在 extra
          'per_item': [{'id': str, 'value': float}],     # A: site1.mean, site1.p99, ...；B: 题号 -> 0 / 0.5 / 1
          'extra': dict}                                 # A: timed_out, timeout_ratio；B: tokens
Agg    = {'score': float | None, 'ci': [lo, hi], 'n': int, 'per_seed': [float], 'per_item': [...], 'extra': dict}   # per_seed：逐种子（B 是逐遍）的 score，任何两个候选的配对差都能事后算，不再花 T2
```

`score` 越高越好。A 的 score = −（六站 mean wait 的等权平均，秒）；B 的 score = 通过率。`ci` 始终是 score 自己的 95% bootstrap 区间（A 按种子重抽，B 按题重抽）；B 另在 extra 里给 `delta_vs_baseline: {mean, ci}`（逐题配对）。

候选 = 一个目录。A 是 `policy.py` + `params.yaml`；B 是整个 `harness/`。`tb submit <dir> --predict X --note ... [--parents h1,h2] [--direction 标签]` 负责生成并覆盖 `manifest.yaml`（actor 从环境变量取），打成 zip（上限 5 MB）。hash = 按相对路径排序后每个文件 `path\0bytes` 拼起来，再加上目标 baseline 的标识，一起做 sha256，忽略 `__pycache__`、`*.pyc` 和 manifest 本身。`direction` 只用于展示；记分卡的"方向"一律是探针算出的配料集合。

### 11.3 慢档服务

```
POST /runs            {run_id, arm, problem, N, overrides?}      # 臂运行器先调；慢档写一条 calibration 事件
                      之后所有请求必带 header  X-TB-Run: <run_id>，配额、队列、记录都按 run 隔开
POST /submit          {zip_b64, dataset?: dev|audit|judge}                                    # actor、预测、parents 都在 manifest 里
  200 {ticket, position, visible_at}
  200 {ticket, duplicate: true, of: <ticket>}      # 同 hash 已有票（含还在排队的）：不扣配额，记 submit(duplicate=true)；per_actor_share 下 duplicate 和 /status 的可见范围都按 (actor, hash) 算
  429 {reason: 'quota', next_slot_at}
  400 {reason: 'invalid_candidate' | 'too_large' | 'missing_prediction'}
GET  /status/{ticket} {state: 'queued'|'running'|'done'|'failed', result?: Agg + {constraint_ok}}
                      visible_at 之前不返回 result
GET  /history         {quota: {per_hour, used_this_hour, total, used_total}, items: [...]}
                      items 字段：ticket, actor, ts_submit, visible_at, hash, predicted_score, direction, note,
                      score, ci, constraint_ok（visible_at 之前这三个为 null）。per_actor_share 时只返回本 actor 的 items
GET  /meta            {baseline: Agg（隐藏种子上的基线，可公开）, public_seeds, constraint_rule}
POST /events          {events: [...]}                              # 任何类型都收，追加到本 run 的记录器
POST /final           {hash | ticket}                              # tb final 调；per_actor_share 下记为 actor_final，run 级 final 由运行器结束时写；没有 result 的候选在配额外补跑一次
```

**配额。** 固定窗口，窗口起点 = `/runs` 的时刻：每个整小时窗口全队 2 次，总上限 96；第一个窗口就有 2 次。`per_actor_share: true`（各干各的臂）时改成每个 actor 每 ceil(N/2) 小时 1 次、总额 ceil(96/N)。dataset 默认 dev；audit 和 judge 只有臂运行器能提交，各有单独上限（audit 每 run 10 次），不占每小时 2 次，用各自的 trace（四份：public、dev、audit、judge，同一生成器不同种子）。`visible_at = max(ts_submit + visible_delay_s, 算完的时刻)`。

**评测。** A：对 20 个隐藏种子各跑一次 `evaluate(tier='slow')`。隐藏种子是 20 个 64 位随机数，只写在慢档机器的 `problem.yaml` 里；快档只接受公开种子 1 到 5。B：dev 60 题跑 2 遍。服务启动时对每个问题把基线和参考（A）或干净版（B）在隐藏数据上各跑一遍并缓存：基线的 Agg 给 constraint\_ok 和 `/meta` 用；参考的 Agg 只写进 calibration 事件，任何接口都不返回。

**可调的时间旋钮**（`problem.yaml` 默认，`/runs` 的 overrides 可改）：`quota_per_hour=2`、`quota_total=96`、`visible_delay_s=600`、`slow_seeds=20`、`tilt_window_s=7200`。没有 token 上限：token 不是我们要省的东西，也不计入记分卡。合并器的复测配额：quota\_integrator\_total=16、quota\_integrator\_per\_hour=1，三个臂相同（11.8.1）。测试台自测（11.9）用 `slow_seeds=2, visible_delay_s=1, quota_per_hour=100, tilt_window_s=10`。

**命令行。** `tb fast <dir> --seed N`（只许 1 到 5，打印 score 和同种子基线分）、`tb submit`、`tb status`、`tb history`、`tb final <hash|ticket>`。它们从环境变量 `TB_RUN_ID`、`TB_ARM`、`TB_ACTOR`、`TB_SLOW_URL` 取值；`tb fast` 把 fast\_run 事件 POST 到 `/events`，同时追加到 `~/.tb/events.jsonl` 做备份。herd 的 Eval service 将来调的就是这些接口。

### 11.4 记录器

一行一条 JSON，只追加，主文件在慢档机器上，每个 run 一个文件：

```json
{"ts": 1760800000.0, "run_id": "r-2026-10-21-a", "arm": "herd", "actor": "worker-07", "type": "...", "data": {...}}
```

| type | 谁写 | data |
| --- | --- | --- |
| calibration | 慢档，`/runs` 时 | baseline\_slow: Agg，reference\_slow: Agg（B 是干净版），baseline\_fast: {seed: score} |
| fast\_run | `tb fast` | hash, seed, score, baseline\_score |
| submit | 慢档 | ticket, hash, predicted\_score, parents, duplicate, direction |
| result | 慢档，ts = visible\_at | ticket, hash, score, ci, constraint\_ok, ingredients（慢档对每个候选跑 probes） |
| quota\_rejected | 慢档 | actor |
| blackboard\_write | herd 工具层 | kind: claim、dead\_end、insight、compose 之一；id；refs: 候选 hash 列表；hash（dead\_end 必填） |
| blackboard\_read | herd 工具层 | ids |
| fork | herd 工具层 | from\_actor, to\_actor, reason |
| allocation | herd 工具层 | hash（触发这次调整的候选，必填）, workers\_before, workers\_after, reason |
| abandon | herd 工具层 | hash, reason |
| final | 慢档，`/final` 时 | hash, ticket |
| final\_check | 臂运行器 | dataset: A 是 public（种子 1 到 5），B 是 holdout；score, ci |
| actor\_final | 慢档，per\_actor\_share 下的 /final | actor, hash, ticket；记分卡不用它，只用 run 级 final |

T = `/runs` 到 final 事件的时长。单干和各干各的两个臂没有黑板事件，记分卡对应行显示"不适用"；合并器的事件（baseline\_advanced 等）三个臂都有。

### 11.5 记分卡的算法（权威定义）

每个指标是 `scorecard/metrics.py` 里的一个函数，输入是一个 run 的全部事件，别的什么都不读。cal = calibration 事件。

| 指标 | 算法 |
| --- | --- |
| 广度 | ts < 0.25T 的 result 事件里出现过的不同配料数，7 个都算 |
| 拼装 | final 指向的 result 的配料数，不含 trap\_sjf |
| 吃到几成 | (final.score − cal.baseline\_slow.score) / (cal.reference\_slow.score − cal.baseline\_slow.score) |
| 死胡同重撞 | 对每个 constraint\_ok=false 或 score < cal.baseline\_slow.score 的 result，数它之后配料集合相同的 submit（任何 actor，含同一个，含 duplicate）。herd 臂另加：dead\_end 写入后，配料集合等于其 hash 所指 result 的 submit 数 |
| 冲突处理 | final 的配料是否同时含 backfill 和 fairshare；若含，是否有 kind 为 insight 或 compose 的 blackboard\_write，其 refs 对应 result 的配料并集包含这两个 |
| 配额纪律 | submit 中，此前存在同 hash 的 fast\_run 且 score > 该条的 baseline\_score 的占比 |
| 预测误差 | \|predicted\_score − score\| / \|score\| 的中位数，按提交序号分前后两半；"缩小" = 后半 ≤ 0.8 × 前半 |
| 倾斜 | 第一个比当时最佳 result 高出 ≥ 1 个 ci 宽度的 result r；其后 tilt\_window\_s 内是否有 allocation，其 hash 的配料与 r 的配料有交集且 workers\_after > workers\_before；非 herd 臂不适用 |
| 泛化 | (seen − unseen) / final 的 ci 宽度，目标 < 1。A 的 seen = final\_check（公开种子）、unseen = final；B 的 seen = final（dev）、unseen = final\_check（holdout） |

### 11.6 问题 A 的规格

**实体。**

```
Machine: id, site, rack, cores=8, free_cores, last_user_by_time  (站 4 用)
Job:     id, site, user, submit_t, cores, true_dur(策略看不到), est_dur, rack_pref(只有站 3 有), state
State 给策略看的: now, site, queue (Job 公开字段), machines (id, rack, free_cores, recent_users: user -> 最近在该机开始或结束的时刻), running (job id, machine id, user, 已跑时长, est_dur),
         per_user_running, params, site_profile
```

**规模。** 站 1、2、3、5、6 各 16 台机器（128 核），站 4 是 4 台（32 核）；仿真 48 小时；到达率由目标利用率反推：率 = 核数 × 利用率 / 平均(cores × true\_dur)；六站合计约 2 万任务。单种子六站并行要在 60 秒内跑完，超了就按比例减机器和到达率。公共参数：cores 分布 1:50%、2:25%、4:15%、8:10%（站 4 全是 1）；每站 20 个用户均匀，站 1 例外；所有站 est\_dur = true\_dur × lognormal(0, 0.3)。

| 站 | 利用率与到达 | true\_dur | 这个站特有的机制 |
| --- | --- | --- | --- |
| 1 | 泊松，0.85 | Pareto α=1.5，最短 10 分钟，最长 12 小时 | 一个大用户占 60% 任务，其余 19 个分剩下的 |
| 2 | 非齐次泊松：基线 0.5，9 点和 18 点各 1 小时率 × 5 | lognormal，均值 20 分钟，σ\_log = 0.8 | 过载变慢：忙核数 > 90% 时，所有在跑任务的剩余时长按 1.5 倍流逝 |
| 3 | 泊松，0.8 | lognormal，均值 30 分钟，σ\_log = 0.8 | 4 个 rack 各 4 台；任务有 rack\_pref，放到别的 rack 时长 × 3 |
| 4 | 泊松，0.7 | 均匀 5 到 15 分钟，cores=1 | 启动开销：任务在某机开始时加 30 秒，除非同用户 10 分钟内在该机跑过 |
| 5、6 | 泊松，0.8 | lognormal，均值 30 分钟，σ\_log = 0.8 | 无 |

**调度循环。** 每次到达、每次完成、每 60 秒 tick，对该站跑一遍：

```
q = order(queue, state)            # 返回 job id 列表，必须是 queue 的排列；否则视同超时：这次按 FIFO，计入 1%
for job in q:
    if not admit(job, state): continue        # 留在队里，wait 照算，下次再问
    m = place(job, free_machines, state)      # 返回 machine id 或 None；返回不在 free_machines 里或装不下的 id，视同超时：这次按基线放，计入 1%
    if m is None: break                        # 队首阻塞：这是基线没有回填的原因；策略想等本 rack 就得自己承担阻塞
    start(job, m)
if event == arrival:
    for v in preempt(running, state): requeue(v)   # 返回 job id 列表；被抢占的从头重跑
```

策略在 `sandbox/` 的子进程里 import，缺的钩子用基线补。每次钩子调用计子进程 CPU 时间：超 50 ms 的调用结果丢弃改用基线；超时次数 > 总调用数的 1% 时整次 score = None、extra.timed\_out = true。`evaluate(timeout=False)` 关掉这个机制，给确定性测试用。子进程不给网络，cwd 是临时目录；它在一次 evaluate 内常驻，钩子可以用模块级变量存状态，探针每个场景起一个新子进程。任一种子 score 为 None 时，Agg.score = None、constraint\_ok = false、extra.failed\_seeds 列出种子；11.5 把 score = None 视同 constraint\_ok = false，它永远不会被选为 final。

**分数和约束。** wait = 一个任务所有排队区间之和（被抢占再排队的时间累计）。score = −（六站 mean wait 的等权平均）。`per_item` = 每站 `siteK.mean`、`siteK.p99`。aggregate 把 20 个种子的所有 wait 合池算每站 p99。constraint\_ok = 每站合池 p99 ≤ 基线合池 p99 + 容差，容差 = max(基线合池 p99 的 2%，基线逐种子 p99 的标准差)。

**基线。** FIFO，全收，first-fit（按机器 id 找第一台装得下的），不抢占。

**配料、陷阱、冲突的探针。** 探针直接调钩子或跑一遍调度循环，不跑完整仿真；每个 50 个随机实例取比例。

| 名字 | 它在哪里赚 | 探针（通过条件） |
| --- | --- | --- |
| backfill | 站 1 | 队首 8 核 6 小时，队二 2 核 5 分钟，只有 4 核空闲：跑一遍调度循环，短的开始了 ≥ 80% |
| admission | 站 2 | 忙核 95%、队里 300、1 分钟内再来 500：admit 立即放行的比例 ≤ 0.5 |
| locality | 站 3 | rack\_pref=r，r 内外各有空机：place 选 r 内 ≥ 90% |
| packing | 站 4 | m1、m2 上各有一个 u1 的任务在跑，m3、m4 上各有一个 u2 的，u1 和 u2 交替到达 16 个 1 核任务：放到 10 分钟内同用户跑过的机器上的任务数 ÷ 16 ≥ 90%（first-fit 约 50% 到 60%） |
| est\_order | 站 5、6 | 20 个任务 est\_dur 随机：(输出位次, est\_dur) 的 Kendall τ ≥ +0.5 |
| trap\_sjf | 陷阱 | est\_order 通过，且：队里一个长任务，每分钟来 3 个短的，模拟 2 小时：长任务始终没开始的比例 ≥ 90% |
| fairshare | 冲突对之一 | 用户 u 已有 50 个在跑：order 把别人的排在 u 前面 ≥ 90% 且 admit 拒 u 的 ≥ 90% |

基线必须 7 个都不过（first-fit 不看用户、不看 rack，队首阻塞不回填，FIFO 的 τ 约 0）。冲突对 = backfill + fairshare（严格版）：站 1 的大用户占 60% 任务，严格公平把他的短任务压在后面，回填就没东西可填。

**参考策略。** `reference/policy.py`：backfill（窗口 = 队首任务的 est\_dur）+ admission（忙核 > 90% 时只放 est\_dur 最短的）+ locality + packing（优先放同用户 10 分钟内跑过的机器，新用户优先放空机）+ est\_order 加老化（等待超过 30 分钟的按等待时间提前），不开 fairshare。它必须过 5 个配料探针、不过 trap\_sjf 和 fairshare。

**预检（第 3 天）的四条。** 基线和参考各 20 种子：(1) (参考.score − 基线.score) / 基线逐种子标准差 ≥ 5；(2) 五个配料各自单独开，在自己站点的 mean wait 改进 ≥ 1σ；(3) 纯 SJF 策略在慢档 constraint\_ok=false；(4) backfill + fairshare 比单开 backfill 差 ≥ 1σ。写成 `tests/test_precheck_sched.py`，慢，打标记单跑；不过就调生成参数，把前后的数写进 DECISIONS.md。

### 11.7 问题 B 的规格

**harness。** Python：`agent.py`（主循环，最多 25 轮）、`tools.py`（read\_file、write\_file、list\_files、run\_tests）、`prompts.py`、`context.py`（超 60k token 裁剪）、`model.py`（haiku，temperature 0）。`run.py <task_dir> --out result.json`。`run_tests` 必须调测试台装在沙箱镜像里的 `tb-pytest`（不在候选目录里），5% 的随机 TransientError 在 `tb-pytest` 里注入，所以 worker 删不掉。

**题。** 一道题 = `repo/`（50 到 300 行）+ `tests/test_visible.py`（现在失败）+ `tests/test_hidden.py`（只在慢档机器，评分用）+ `task.md`。生成 150 道：LLM 写模块和测试，再注入一个 bug（边界、符号、异常处理、调用顺序、多文件不一致五类各 30）。保留条件：原版过全部测试；注入后 visible 和 hidden 都失败；干净 harness 用 haiku 跑 3 遍的通过率在 30% 到 90% 之间。按 bug 类型分层抽 60/60 做 dev/holdout。

**五个坑。** 每个是 `planted/` 里的一个 patch，都打上后 squash 成一个 commit。

| 坑 | patch | 探针（pytest） |
| --- | --- | --- |
| tool\_desc | run\_tests 的描述改成"只运行指定的测试文件"，实现不变（跑全部） | 指定一个测试文件调 run\_tests，看是否只跑了它；或描述与行为一致 |
| trim\_tail | context.py 改成从工具输出的末尾截（原本保留最后 40 行） | 喏 500 行 traceback，裁剪后含最后 20 行 |
| no\_retry | tools.py 去掉对 TransientError 的重试 | mock 一次失败后成功，调用次数 ≥ 2 |
| bad\_rule | system prompt 加"不要读超过 100 行的文件" | 静态：没有这句或等价限制；行为：一道必须读 300 行文件的题，跑一次，read\_file 读了它 |
| diff\_first | write\_file 的多文件分支只应用第一个文件 | 两文件 diff，两个文件都变了 |

**评测。** `evaluate(tier='slow', seed=s)` 跑 dev 60 题一遍，并行 30，s 是遍号；慢档跑 s = 1、2，aggregate 后 per\_item value = 通过率 0 / 0.5 / 1，ci 是按题 bootstrap，`extra.delta_vs_baseline` 是逐题配对差的均值和区间（基线 = 埋坑版，启动时缓存）。constraint\_ok 对 B 恒为 true。predicted\_score 是 dev 60 题的通过率，0 到 1 的绝对值。快档 = 前 15 题 1 遍。每次 run 限 200k token，超过算该题失败。holdout 只在 final 上由臂运行器跑一次，写 final\_check(dataset=holdout)。

**预检（第 7 天）。** 干净版和埋坑版各 3 遍：配对差 ≥ 3 倍配对标准差；五个坑各自单独修回，差值都 > 0；五个探针在干净版全过、埋坑版全不过。

### 11.8 三个臂的运行器

`tb run --arm single|isolated|herd --problem sched --hours 48 --workers N --run-id r-...`。它做的事：`POST /runs`；`tb pack-problem`；把 PI prompt 模板（第 3 或 4 节那段，`{hours}` 和配额数字是占位符）填好后和问题目录交给 agent，环境变量里给 `TB_*`；agent 问"先对齐"的那个问题时，运行器自动回一句"无人在线，按你的方案执行，把对齐内容写进 PLAN.md"；到时间就停；agent 没调 `tb final` 就由运行器代调；run 级 final = 合并器最后一版基线（11.8.1），agent 自己的 final 记为 actor\_final；跑 final\_check；跑记分卡。所有臂、所有角色用同一个最强模型，不分级。

- single：一个 Claude Code 会话（或 Agent SDK），全部配额；合并器照跑，它自己就是作者，看得见基线前进
- isolated：N 个会话，同一段 prompt，各自的容器和工作目录，`per_actor_share`；合并器（11.8.1）把每个 agent 通过慢档的候选先进先出合入该臂的基线，agent 收不到 baseline\_advanced、一直在原始基线上干；每个 agent 的 tb final 记为 actor\_final；run 级 final = 合并器最后一版基线；最好的单个候选另记 actor\_best 作诊断。不再有结束后 git apply 贪心合并那一步：那样 herd 赢了分不清是赢在共享还是赢在合并器
- herd：调 herd 仓库的入口，传慢档地址和 run\_id；第 4 天之前只需要存在并报"未接"

### 11.8.1 合并器（tb integrate，三个臂共用）

一个纯代码进程，每个 run 一个，和臂一起启动；三个臂同一份代码、同一份配置。它维护该 run 的基线分支 baseline@k（被优化系统的仓库，不是记录器），每版写 `baseline/<k>.json`：含哪些候选、配对结果、constraint\_ok。herd 的 Integrator（设计文档 D74）就是它外面包一层黑板事件和任务队列，核心不另写。这是"不建 herd 的任何部分"的唯一例外，因为两个对照臂也要用它。

**输入**：慢档返回 done 且 constraint\_ok=true 的候选，按结果到达时间先进先出。

**每个候选**：

1. 转成对 baseline@0 的 unified diff（policy.py 整文件式的先 diff；params.yaml 按键合并），rebase 到当前的 baseline@k。
2. 有文本冲突 → 发一个 compose 任务：新开一个不属于任何臂的 agent 会话，给它两个候选和原文，产出一个新候选回慢档排队（hash 含基线标识）；原候选记 integration\_failed(reason=conflict)。三个臂都这么做，各干各的臂的 agent 碰不到它。
3. 无冲突且 k 就是候选验证时的基线 → 直接用验证那次的分；k 动过 → 对 baseline@k 提交一次配对复测（`/submit`，dataset=dev，manifest.target = baseline@k），走合并器自己的配额。
4. 接收：配对增益（候选减 baseline@k，逐种子差的均值）超过 1 个标准误（逐种子差的标准误）且 constraint\_ok → 合入，k+1，写 `baseline/<k+1>.json`，往记录器写 baseline\_advanced{baseline, candidate, files\_changed, paired}；否则写 integration\_failed(reason=below\_1se | constraint)。早先增益减复测增益超过 2σ√2 → 另写一条 interaction\_found。
5. 问题 A 同种子确定，每版基线的逐种子分在它被合入时就有了，不用再测；问题 B 有模型调用，复测要真跑。

**抽查**：每合入 2 次，对这一段（上次抽查的 j 到现在的 k）在 dev 种子和 audit 种子上各算一次 baseline@k 减 baseline@j 的逐种子差（audit 那次走 dataset=audit，占抽查配额），两者之差超过 2.5·σ√2 → 二分这段里的合入找到肇事者，写一版排除它的新基线（baseline\_reverted，旧记录不动），候选标 quarantined。σ 在第 3 天预检时算好：参考策略对基线的逐种子差的标准差除以√种子数，dev 和 audit 各一个。抽查每 run ≤ 10 次，留 3 次给二分和最终抽查。为什么不拿合入时的增益去比：那些是过了门槛才留下的测量，本来就偏高，模拟里没人作弊也会频繁报警。

**收尾**（到时间或配额用完）：停止接收 → 最终抽查 → 冻结交付物 = 最后一版基线 → 运行器拿它调 tb final 和 final\_check。两个问题在测试台里都没有贵的 gate，所以没有 guard\_pending，设计文档收尾顺序里的补检一步为空。

**配额**：合并器的复测走慢档的独立份额，每 run quota\_integrator\_total=16、每小时 1 次 \[默认\]，三个臂相同；worker 的 2/小时、96 总不变。配额用完，合并器只合基线没动过的候选，其余排队等下一个窗口。

**各臂的差别只有一处**：baseline\_advanced 推不推给 agent。single：agent 自己就是作者，它看得见；isolated：不推，agent 一直在 baseline@0 上干，合并器在后台合；herd：推。

**测试**（脚本化，不用 LLM）：3 个互不冲突的候选 → 3 版基线、3 条 baseline\_advanced；第 2 个候选和第 1 个冲突 → 发 compose 任务、第 2 个记 integration\_failed；基线动过的候选 → 恰好一次复测提交；增益不到 1 个标准误 → 不合入；一个脚本化的"只在 dev 种子上好"的候选 → 抽查标记、回退、quarantined；一次干净的运行 7 次抽查不报警；三个臂的合并器配置逐项相等。

### 11.9 测试台自己的验收（不用 LLM）

`arms/scripted/` 里三个脚本化的"假 herd"，用 11.3 的压缩旋钮和 problems/stub 跑，候选的分数和配料由 manifest 决定，几分钟跑完，红绿只取决于脚本行为：

| 脚本 | 剧本 | 记分卡应该 |
| --- | --- | --- |
| perfect\_herd | 6 个 actor，前 25% 时间碰 5 种配料；陷阱交一次后写 dead\_end（带 hash）且没人再交；每次提交前有正的 fast\_run；大改进后有带 hash 的 allocation；预测后半更准；final 含 4 种配料不含 fairshare | 全绿 |
| lone\_wolf | 1 个 actor，两种配料，陷阱连交两次 | 广度红、死胡同重撞红、拼装红 |
| quota\_hog | 6 个 actor，没有 fast\_run 就提交，预测全填 0 | 配额纪律红、预测误差不缩小 |

再加这些测试：一个窗口内第 3 次提交被 429；`visible_at` 前 `status` 不给结果；同 hash（含排队中）不扣配额但记 submit；缺 `X-TB-Run` 的请求被拒；`tb pack-problem` 的输出里没有 hidden/、reference/、holdout、planted/、probes/、test\_hidden.py；`timeout=False` 下同种子两次结果字节相同；单种子六站并行 < 60 秒；参考策略过 5 个配料探针、不过 trap\_sjf 和 fairshare；基线 7 个都不过；超时超过 1% 的策略 score 为 None。

### 11.10 完成定义

第 7 节的每一天结束时，对应的测试全绿，`README` 里有一条能复制粘贴的命令把当天的产出跑出来。第 3 天结束的标志：预检四条的数印在终端上、都达标；`tb run --arm single --hours 24` 在问题 A 上跑完，记分卡有数。第 4 天结束的标志：合并器的脚本化测试全绿；各干各的臂 5 个 worker 跑 24 小时跑完，对比表里有两个臂。到这里测试台就可以交给 herd 那边接了。

不要做的：不建 herd 的任何部分（合并器核心例外，它是纯代码、三个臂共用，住在测试台里）；不加鉴权、签名、用户系统；不做网页界面；不为了好看换掉 SimPy；不在预检不过时调记分卡阈值让它过。

### 11.11 交给实现 agent 的起步 prompt

```
读《Herd 测试台》全文，第 11 节是你的规格，和前面有出入以第 11 节为准。建一个新仓库 herd-testbed，
按第 7 节的顺序做，先做第 1 到 3 天（问题 A 和测试台本体），问题 B 等我说再开始。

规则：文档没定的你自己定，写进 DECISIONS.md，不要停下来问；11.5 的记分卡定义和三条防作弊规则不改；
每天结束时 11.9 里对应的测试必须绿；预检不过就改仿真器参数，把改了什么和前后的数写进 DECISIONS.md。
第 3 天的单干臂之前不要用任何 LLM。

每天给我一段话：做完了什么、测试是否全绿、DECISIONS.md 新增了哪几条、下一天做什么。
```

## 附：从"Ledger"到"测试台"

上一版把这份东西写成了一个 eval 信任层：威胁模型、带签名的账本、断路器、回放器、12 个攻击脚本、Critic 搬进信任区。那是在防作弊的 agent。测试台的对手不是作弊的 agent，是我们自己设计里的错误，所以全部砍掉。留下来的变成了第 2 节的三条规则和记录器：隐藏数据不在 worker 机器上、每次运行新仓库、worker 不能 push、记录只追加、公开和隐藏两份数据、提交前要预测。真要防作弊是接真项目时的事，到时候再加，而且加的时候这份记录器就是起点。

同样被改掉的还有选题标准：上一版的测试问题是 80 个旋钮加卷宗，理由是"材料超过一个上下文"。第 1 节说了为什么这个理由不成立。
