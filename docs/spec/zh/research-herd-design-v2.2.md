# Research Herd 设计文档

大规模 agent 协作研究系统 · Oct 17, 2026 · @Minyi

## 0. 读法与状态标签

这份文档的目标不是一步到位，是让第一轮 PoC 能快速建起来、跑起来、坏掉的时候知道改哪里。为此每个设计决定都带一个状态标签，说明它能不能改、怎么改；第 15 节的决策登记表把所有决定按编号列齐，是实现和复盘时的唯一检查清单。

| 标签 | 含义 | PoC 失败时怎么办 |
| --- | --- | --- |
| \[约束\] | 环境或问题强加的，改它等于换问题 | 不改设计，改问题定义或环境 |
| \[推论 P#\] | 从第 2 节某条原则推出来的。原则成立它就成立 | 先质疑原则；原则不倒，推论不动 |
| \[假设 H#\] | 我们的猜测，第 3 节登记，第 11 节有验证实验，第 15 节有 fallback | 切到登记表里的 fallback，不重新设计 |
| \[默认\] | 参数初值，附取值理由，随意调 | 调 |

没有标签的句子是解释，不是决定。读者分两种：要实现它的 agent 先看 0.1 节的讨论轨迹对齐思路，再看第 14 节的最小落地版和第 15 节的登记表，再按需回看各节；要判断它对不对的人先看第 1 到 3 节，那里是全部前提。

本文的 v1 经过一次独立审稿，记录在附录 A；v2 在 v1 基础上重写，审稿指出的问题都已并入正文，附录 A 保留是为了让人知道第一稿错在哪。附录 B 是一个假想 case 的逐事件走查。v2.2（2026-10-18）：coding agent 在 v2 上自审出一版 v3，经独立审稿后决定不采纳它的转向（按杠杆拆任务、静态分工的对照组），只把与转向无关的改进并入本文：D72 到 D77，第 7 节 hook 表里的重启路径，第 8 节规则 6，第 14.1 节的测试台改动，H1 和 H3 的判定标准。审稿记录在《Research Herd v3 审稿》。第 29 轮又去掉了所有为省 token 而设的机制（P10，D47 撤销）。第 30 轮按 Opus 的审稿（文件 Research-Herd-v2.2-fixes-zh.md）修了两处统计错误（抽查规则、相互作用阈值），把合并器改为各臂相同，推迟 D78，并把 D72 到 D78 接进 schema、组件表、臂开关表、L0 测试和附录 C。

## 0.1 讨论轨迹：这份设计是怎么来的

读这一节的目的不是了解历史，是知道每个设计回答的是哪个问题。要改一个决定时，先找到它对应的那一行，确认改完之后那个问题仍然有答案。设计是两天里一轮轮追问出来的，每一轮都是一个具体的痛点或一个约束收紧，把前一轮的答案推翻了一半。

| # | 这一轮问的是 | 转折或洞察 | 落到文档 |
| --- | --- | --- | --- |
| 1 | 100 台机器、50 个 agent 解一个难题，有没有现成的 orchestration 框架？10,000 个 agent 解千禧年问题你会怎么做？不要简单 divide & conquer，要大量通信 | 现成框架解的是"每个 agent 跑得快不快"，而这个问题的本质是共享认知状态：黑板、验证器做信任层、按功能分工不按子问题分工、注意力是真正的瓶颈、多样性要显式管理 | P3、P7；角色表（§4） |
| 2 | 我要解的问题绝大部分有外部 eval | 从"研究所"变成"搜索"：AlphaEvolve 一系的 archive 可以借；reward hacking 是必然不是风险，共享会放大它；best-of-N 是必须跑的对照 | 反 hacking（§9）、B1 基线（§11） |
| 3 | eval 很贵很慢，支持不了高并发 | 稀缺资源不是 agent 也不是 token，是 eval 名额；agent 的工作从生成变成预测；分层保真度、提交前同行评审、带预测提交 | C1、D37、D40；阶梯（§9） |
| 4 | 核心是信息共享，但我以前做的时候 handoff 总是丢信息：A 看到一条线索，交出去就没了，别人也恢复不了 | 病根是压缩发生在价值已知之前。agent 比人多一个能力：上下文可以复制。fork 代替 handoff、回头问、摘要只做索引、推转拉、线索做成可执行 artifact | P2；D4 到 D8 |
| 5 | 黑板可以，但每个 agent 多久读一次？ | 按事件和状态读，不按时间：固定频率没有正确值。硬中断、软注入、按需检索；卡住了就多读 | P5；D17 到 D19 |
| 6 | 那黑板是不是需要一个很强的 agent 来管？ | 一半对：需要强的判断，但不能让它坐在读路径上，否则它就是新的有损压缩器和单点偏见源。判断什么重要和控制谁能看见什么，必须是两个东西 | P4；D13 到 D16 |
| 7 | 黑板其实可以直接用 git repo | 是，而且概念几乎一一对应；git 缺的是通知、语义检索、结构化查询、大文件，各补一层薄的；append-only 是必须的约定 | D30 到 D33 |
| 8 | 我用过 kanban 式的 dispatcher，单个强 agent 能解的问题拆开反而变差：一个读 email、一个读 Slack，合并后信息全损，没有那个 click | 这是对多 agent 最根本的反驳，而且它是对的。click 只在单个上下文里发生；并行的应该是尝试，不是材料；join 由一个脑子做；任务是目标不是指令 | P1；D1 到 D3 |
| 9 | 那研究问题上大规模 agent 怎么规避这个？ | 规模用在尝试数上不用在切问题上；不预先分解；层级按抽象分不按材料分；跨方向的 click 没人持有，要排进日程 | P8；D27、Scout |
| 10 | 我还没把这些原则 map 到可落地的工程实践 | 第一次写成系统：仓库布局、五个服务、worker 循环和 hook、baseline、指标、建造顺序 | §5 到 §8、§11、§14 的骨架 |
| 11 | 我不想让 herd 只做单一优化，我要一个能和它细致讨论、对齐了再开始的地方 | 人是密度最高也最容易被压缩丢掉的信息源。PI agent、charter、teach-back、讨论全文入库、首波从 PI fork、escalation；跨问题持久的是 PI 不是 herd | P6；D20 到 D23；§10 |
| 12 | 这是不是需要最强的模型来细化每个 agent 的 goal？ | 一半对：强模型细化的是问题，不是每个 agent 的指令，否则又是 dispatcher 陷阱；worker 也不能弱，因为 click 在 worker 里 | 模型分级（§12）、D47 |
| 13 | 写 design doc，self review，走一个假想 case | v1 写完，独立审稿指出的最大漏洞是闭环断裂：eval 结果回来没人接。从原则往下推时最容易犯的错：没有从一个 worker 的一天从头走到尾 | P9、D28；附录 A、B |
| 14 | 不是所有问题都有固定 eval：优化 harness 代码库有，抽象问题只有 topline 而且不固定 | 三种形态；形态 B 里改尺子是工作的一部分；只有 topline 能动预算是反 Goodhart 的硬规则 | D12；metric claim；§9 三种形态 |
| 15 | eval 可能是外部工具、本地 dataset、Kusto、flight 信号，要有自由度 | eval 不是一个组件，是一组 signal source 的插件接口；声明属性，阶梯、标定、预算从声明推导；exposure 归因 | C6、D39 |
| 16 | 最后修订一版：逻辑要 coherent，每个设计说清是强制约束、猜的、还是从原则来的，PoC 失败后另一个 agent 不用猜；不追求一步到位，要快速落地可改进 | v2 重写：状态标签、硬约束表、带证伪条件的原则表、假设登记、决策登记表、最小落地版 | §0、§1 到 §3、§14、§15 |
| 17 | eval 机器给多了，它通常是外部的；不明确的是怎么保证探索又广又深，发现好方向怎么动态加机器加 agent | eval 外部化后集群的本质是注意力容量。分配单位是 slot；快反应和永久化分开（surge）；深度来自 fork 不来自人数；广度靠硬底线；第一版分配是确定性规则 | §9.1；D49 到 D51 |
| 18 | 这个思考合理吗？类似的我还没想到的有哪些？ | 成功情形之外要答失败情形：什么时候停、人的注意力怎么分、硬约束做门不做分；其余列为开放问题 | §9.2；D52 到 D54 |
| 19 | 还有什么问题？够不够交给 agent 开始 PoC？ | 实现者视角的审稿：任务队列、事件 schema、eval\_request 语义、H1 的臂都没闭合。给 agent 的不是"去实现设计"，是"去验证 H1"，第一个交付物是落地计划不是代码 | D55 到 D58；附录 C |
| 20 | 我有现成的 k8s / Terraform 集群仓库——但应该分层，agent 拿什么起、每个 agent 怎么实现都不该管；把有效的部分实现在一起，不 glue 到某个集群仓库或 agent 实现上 | 要建的是一个独立的 runtime + orchestration：四层、六个端口；herd 工具以 MCP server 暴露让 harness 可替换；每端口一个真适配器加一个内存适配器；内存版是迭代速度的来源 | §4.1；D59 到 D61；H12 |
| 21 | 外部审稿：H1 的分数不可信；worker 能读到隐藏材料；hook 漏掉内建工具；fork 解不了上下文满；D51 和 D12 矛盾 | 五条都是关于 git 和 harness 的事实，不是设计偏好：判定改为 holdout 重评自选候选；私有存储；hook 三层；上下文满是压缩不是 fork；分配只吃顶层 | D62、D63；D60、D5、D51 改写 |
| 22 | optimization 不是只找一条最优路径，不同路径在不同地方各有优化，你自己的例子就是组合，为什么设计里说 best-of-N？ | 名字从对照臂渗进了判定。产出是组合不是单点：候选多父、组合任务、interaction claim；对照臂结束后也合并，H1 检验的是运行中共享的增量；方向是 portfolio 不是 bandit arm。同轮并入外部审稿：两份 holdout、臂的物理隔离、push 凭证、候选格式 | D64 到 D69；D62、D58、D51 改写；H13 |
| 23 | 我要开始实现 backbone 了，但不能只有 unit test；需要几个好问题和一个简单、不费时间的 case 来确定它 work，比直接 hook 到大项目跑两天才发现问题好 | 单元测试和 H1 之间缺一层。三级验收：L0 插头断言，L1 专门设计的合成问题 Knobs（每组 knob 针对一个机制，作为 backbone 的正对照）和 10 个自动判的问题，L2 真问题；上一级不全绿不进下一级 | D70；§14.1 |
| 24 | 测试问题不能简单到一个 agent 就能做完；外部 eval harness 上要有 disclosure；先建这些 test guardrails——这本身不亚于一个单独的 design | Knobs 改为材料超过单个上下文，B0 作控制臂。Eval 的可信度独立成一份设计《Ledger》：威胁模型作脖柱、十二条不变量、信任边界、账本、Vault、断路器、判定引擎、攻击脚本作为验收；Critic 搬进信任区；Ledger 先于 Knobs 先于 herd | 当时写成《Ledger：Eval 信任层设计》；第 25 轮推翻，重写为《Herd 测试台》 |
| 25 | 这不是 eval 信任层，是我们自己 design 的 guardrail，用便宜的方式模拟一个问题来 benchmark 我们的行为；文档写得不是人话；有没有简单的开源能复用 | Ledger 砍成测试台：五个零件（问题目录、快档、慢档、记录器、记分卡 + 对照组），防作弊只留三条；文档改为人话版在前、给实现者的部分在后；开源：SimPy、FastAPI + Redis、Inspect AI 可选，Gas Town 借概念不整个用 | 《Herd 测试台》全文重写；本文 §6、§14、§14.1、附录 C、D63 改引用 |
| 26 | 之前的 use case 只是 context 大，agent 用 bash 或抽样也能做到；4 可以，其实就是策略多、单个 agent 没法同时 explore 很多方向；5 也可以；暂时就用这两个 | 选题标准改为：要迭代的方向数 × 每轮等待时间 > 墙钟时间，且改进可组合、有陷阱、有配额压力；材料多不算，Knobs 和另外两个候选淘汰；定 A（仿真器调度策略，埋 5 配料 1 陷阱 1 冲突）和 B（harness 埋 5 个坑），先 A 后 B；herd 要赢的不是广，是拼、不重撞、配额倾斜 | 《Herd 测试台》§1、§3、§4；本文 §14.1 L1 改写，D71 |
| 27 | 让 coding agent 自审 v2 出了 v3，请验证它；我并没有理解，有多个方面的 research 和在每一个方面深度优化会冲突吗；你这个中英结合我看不懂 | v3 是换方向不是修订：任务按杠杆拆分、加 Mapper、对照组改成静态分工，它自己的模拟不支持这些结论。宽和深不冲突，v3 真正押的是"开局就按杠杆分人"，输掉的是多条路同攻一个方向。v3 带来几样与方向无关的好东西：判定协议、配对测量、合并流程棘轮、不靠 checkpoint 的重启路径、出网白名单；它对测试台的三处挑错成立 | 《Research Herd v3 审稿》；本节 |
| 28 | 我觉得 v2 更好；可以（并入改进，出 v2.2） | v2 为主干，任务形态不变（每人拿一个 approach 攻整个问题，D1、D24），不按杠杆派活，不要 Mapper，不要 B2 臂。只并入与转向无关的改进，全部文字用中文写清楚 | D72–D77；第 7 节 hook 表；第 8 节规则 6；第 14.1 节；H1、H3 行；《Herd 测试台》改三处 |
| 29 | token 不限量；我的点在于不要因为模型和 token 影响开发效率或者增加工程复杂度 | 设计里真正在省的一直是 eval 名额、墙钟、上下文窗口和人的注意力，不是 token；但有几处是为省 token 或省钱加的，全部去掉：模型分级和 H6（所有角色同一个最强模型）、Librarian 的分批参数、Budget meter 和 constraints.md 里的 token 预算、记分卡的成本项、测试台的 token 上限。反过来，只花模型调用不花 eval 的事都可以做冗余。剩下唯一要核对的是 API 的并发和速率上限 | P10；D47 撤销；D78；第 6、12 节；《Herd 测试台》第 6、7、11 节 |
| 30 | Opus review 了 v2.2，出了一份修改清单，你自查一下，valid 的就修；四件需人定的事拍板：合并器各臂相同、预审推迟、S1 收得到基线前进和地图、周计划不顺延只是示意 | 清单全部成立，三个模拟重跑一致。两个统计错误：抽查规则拿被选中的测量去比（赢家诅咒），没人作弊也 58% 到 99% 误报，改成对同一段做两次新测量比差；相互作用的 70% 规则误报过半，改成差值超 2σ√2。一个混淆：合并器只给了 S2，改为四臂同配置运行中都跑，共享只指 worker 能否看到基线前进。一个泄露：预审的否决理由会把为什么得分泄给 worker，推迟。一个落地漏洞：D72 到 D78 没接进 schema、组件表、臂开关表、L0 和附录 C，补齐。运行次数不再写死 3，收尾顺序写死，地图的第一批脚本由人写。连带后果：合并器提前到测试台第 1 步 | D62、D65、D69、D72–D79；第 5、6、8、14 节；附录 C；《Herd 测试台》和《一页对齐》同步；审稿文件 Research-Herd-v2.2-fixes-zh.md |

**贯穿全程的三条线**

1. 问题的定义从"调度"移到了"信息与注意力"。第 1 轮把它从调度问题改成共享认知状态问题，第 4 轮把共享的障碍定位到压缩时机，第 8 轮把分工的错误定位到按材料切分，第 17 轮把集群的本质定位到注意力容量。每一步都是把一个看似工程的问题还原成一个认知的问题。
2. 每次约束收紧都反转了设计重心。有 eval → 搜索；eval 稀缺 → 预测；eval 外部 → 分配；eval 不固定 → 改尺子也是工作。读者拿到一个新的约束时，应该预期同样的反转，而不是在现有设计上打补丁。
3. 从原则到工程再到抽象再到最小版，每一次外部审稿都找到了上一步没走完的闭环。这是设计的工作方式本身的结论：原则对不保证系统对，要从一个 worker 的一天、一个实现者的第一天从头走到尾。所以本文的完成标准不是完美，是可以交给一个 agent 开工、失败了知道改哪里。

**两条没有改过的信念**，它们是其他一切的前提：多 agent 的价值在于独立尝试的数量，不在于把一个脑子的工作切给多个脑子；任何不能在同预算下赢过 best-of-N 的协作机制都不值得保留。如果你要改这两条，改的不是这份设计，是问题。

## 1. 问题与硬约束

**问题**：给定一个有外部 eval 信号的研究或工程问题、一个 100 台机器的集群、几十个带完整 harness 的 LLM agent，设计一个系统让它们协作，在同样的 eval 预算下，交付的最终组合稳定超过 best-of-N（各跑各的、同一合并器下互不共享）。注意交付物通常是多个方向改进的组合，不是一条路径的单点（D64）。同时给人一个持久的对齐入口：先讨论清楚再启动，运行中随时介入。

下面八条是环境强加的，不是设计选择。每条后面是它逼出来的设计方向，正文里的决定都能追溯到这里或第 2 节。

| # | 约束 | 它逼出来的设计 |
| --- | --- | --- |
| C1 \[约束\] | Eval 昂贵、慢、并发有限，而且通常是外部系统：提交、等待、收结果，集群不跑它。典型值：小时到天级，每天几十次 | Eval 是稀缺资源，系统围绕"每次 eval 花在哪"设计，agent 的工作是预测而不只是生成（第 9 节） |
| C2 \[约束\] | 单个 agent 的上下文有限 | 不能让每个 agent 看全部产出，必须有过滤、分层、按需检索（第 8 节） |
| C3 \[约束\] | LLM 输出有方差，并且会利用 eval 的漏洞 | 任何分数都带 n 和区间；eval 的定义对 worker 不可读；共享会放大 hacking，防线在 eval 侧（第 9 节） |
| C4 \[约束\] | Agent 会死，上下文会满，任务会被打断 | 状态不能只存在 agent 里；worker 无状态，状态在仓库和 checkpoint（第 5、7 节） |
| C5 \[约束\] | 人的时间是最稀缺也最高密度的信号 | 人的介入要排队、要预算、要无损保留；herd 不读人，读 charter（第 10 节） |
| C6 \[约束\] | 不同问题的 eval 形态不同：外部工具、本地数据集、Kusto 查询、flight 信号、人 | Eval 是插件接口，阶梯和配额从声明推导（第 9 节） |
| C7 \[约束\] | 100 台机器主要给 worker 和它们的 sandbox（本地代理跑在这里）；worker 是 API-bound，瞬时 token 速率受 API 配额限制 | 机器分配和退避策略（第 12 节） |
| C8 \[约束\] | 第一版规模 50 个 agent，设计目标到几百，不到几千 | 单 git 仓库作黑板是可行范围内最简单的选择（第 5 节） |

**非目标**

- 不是通用 workflow 引擎，不做 kanban 式任务 DAG 分发。
- 不做模型训练。差异只来自 harness、角色和上下文。
- 不以没有任何信号的问题为设计目标。第 9 节的形态 C（只有人的判断）能跑，但价值保证最弱。
- 第一版不做 UI，人机入口是一个长期会话。
- 第一版不追求 1,000 个以上 agent。

## 2. 原则

十条原则。每条写清来源（我们为什么信）、它推出了哪些决定（第 15 节的 D 编号）、以及它错了会看到什么。原则不是教条：最后一列就是推翻它的证据。

| # | 原则 | 来源 | 推出的决定 | 它错了会看到什么 |
| --- | --- | --- | --- | --- |
| P1 | 并行的是尝试，不是材料。Click 发生在单个上下文里，按来源或子任务切分会把需要 join 的信息分到两个脑子 | 实践经验：一个 agent 读 email、一个读 Slack，合并后信息全损；kanban 式 dispatcher 在研究问题上效果差 | D1 任务形态唯一；D2 不预先分解；D3 join 由单个 agent 做 | 按材料切分的对照组不比全材料组差 |
| P2 | 压缩只在价值已知后发生。线索的价值滞后显现，提前摘要是盲目投影 | 实践经验：handoff 中线索丢失且不可恢复 | D4 轨迹全量保留；D5 fork 代替 handoff；D6 摘要只做索引；D7 检索由接收方拉；D8 surprise 单独记录 | handoff 实验里摘要组和 fork 组没有差距 |
| P3 | 验证器是信任层，eval 是稀缺资源 | DeepMind 100 agent 实验：一个 agent 找到 exploit，几分钟内被其他 agent 复用；幻觉随 agent 数放大 | D9 按 claim 类型的状态机；D10 eval 定义对 worker 不可读；D11 Critic 审计；D12 只有最高 trust 的信号能动预算 | 不隔离 eval 的对照组没有出现 hacking 传播 |
| P4 | 判断什么重要的 agent，和控制谁能看见什么的机制，是两个东西 | 推理：一个强 agent 管黑板会变成新的有损压缩器和单点偏见源 | D13 第 0 层纯代码；D14 Librarian 是编辑不是守门人；D15 Planner 只写 decisions；D16 Scout 独立 | 单个强 agent 管黑板的对照组做废功更少 |
| P5 | 读黑板按事件和状态触发，不按时间 | 推理：定时轮询要么污染上下文要么错过关键信息，而且没有正确的频率 | D17 三通道；D18 游标 diff 截断；D19 卡住时多读 | 固定频率的对照组做废功和中断成本都不差 |
| P6 | 人是密度最高的信息源，对齐先于启动 | 实践经验：两小时讨论压成一页 problem.md 是全系统最大的一次 handoff 损失 | D20 charter 协商与签字；D21 讨论全文入库可检索；D22 首波 worker 从 PI 上下文 fork；D23 人的洞察写成 claim | 读 spec 的首波 worker 不比 fork 的差（H10） |
| P7 | 多样性要显式管理 | 文献：swarm 规模的边际递减一部分来自重复；实践：agent 趋同到同一想法 | D24 approach portfolio 与 lease；D25 dead\_end 是一等公民；D26 提交 eval 前强制检索 | 候选多样性不下降，dead\_end 不被重走 |
| P8 | 层级按抽象分，不按材料分 | 推理：横切材料的层级让跨方向的 click 无人持有 | D16 Scout；D27 每层一个 agent 持有该层全貌 | Scout 产出的跨方向 insight 复用率为零（H9） |
| P9 | Eval 结果必须有接收者 | v1 审稿发现的漏洞：异步 eval 回来时提交者已释放任务，闭环断裂 | D28 result\_ready 绑定提交者 checkpoint 并生成后续任务 | 不绑定的对照组里 eval 结果仍被及时消化 |
| P10 | 模型和 token 不是约束。不为省 token 加任何机制，不为省钱用弱模型，不给 token 设预算。要省的只有四样：eval 名额、墙钟、每个 agent 的上下文窗口、人的注意力。反过来，只花模型调用不花 eval 的事都可以做冗余 | 第 29 轮：token 不限量，不要让模型和 token 影响开发效率或增加工程复杂度 | D47 模型分级和 H6 撤销；Budget meter 不计 token；Librarian 不分批；记分卡的成本只计 eval；D78 用 token 换 eval | API 的并发或速率上限成了瓶颈（那时要省的也不是 token，是并发） |

## 3. 假设登记

这些是我们猜的。每条写清为什么信、哪个实验验证（第 11 节）、失败了退到哪（第 15 节对应决定的 fallback）。PoC 的目的就是把这张表逐条变成已验证或已推翻。

| # | 假设 | 为什么信 | 验证实验 | 失败的 fallback |
| --- | --- | --- | --- | --- |
| H1 | 同等 eval 预算下，运行中共享 insight 和 dead\_end 的 herd，其最终组合稳定胜过孤立运行、由同一个 Integrator 合并但 worker 看不见基线前进的 best-of-N | 人类研究组的经验；dead\_end 共享直接省掉重复 | S2（Planner 关）vs B1 @ 5 到 10 个 worker，运行次数按 D72 试点算，预注册效应量；S1 作中间臂定位通信内容的价值 | 系统退化为 best-of-N + 去重 + 分层 eval，仍比裸 best-of-N 省 eval |
| H2 | fork 的 handoff 保真度远高于摘要 | LLM 的上下文可以复制，人脑不能 | handoff 保真度实验 | 退到轨迹指针 + 回头问，放弃 fork |
| H3 | tool call 边界的轮询足以替代推送式硬中断 | 任务 20 分钟到 2 小时，事件率低，一个 tool call 的间隔远小于任务长度。前提是 harness 有 tool hook，否则粒度只是 herd 工具调用（D60） | 中断时延和做废功诊断 | 加 NATS 推送 |
| H4 | 单个 git 仓库撑到几百个 agent | 300 commit/小时远低于 git 的上限 | merge queue 积压指标 | 按方向分仓，或 source of truth 换数据库 |
| H5 | 本地代理和 T1 能和顶层 eval 标定出可用的相关性 | FunSearch、AlphaEvolve 一类工作都靠便宜代理驱动搜索 | 相关性记录 | 退到只用 T0 加顶层 eval，减少 worker 数，让 worker 更多做推理少做试验 |
| H7 | 单个 Planner 可接受到几百个 agent | 决策低频，读的是 digest 不是轨迹 | Planner 单次运行时长随方向数的曲线 | 按方向分片 |
| H8 | 预测记录能标定 agent 的判断力，并且值得据此加权 | 校准文献 | 预测误差与后续候选成功率的相关性 | 取消加权，只记录 |
| H9 | Scout 能产生有复用价值的跨方向 insight | 附录 B 的走查；人类实验室的 group meeting | cross\_approach insight 的复用率 | 取消 Scout，跨方向连接靠人 |
| H10 | 首波 worker 从 PI agent 的上下文 fork 优于只读 spec | 对话记录继承了隐性的品味和边界 | 首波一半 fork、一半读 spec 的对照 | 全部读 spec，PI 的对话只作为可检索材料 |
| H11 | 跨机 fork 含 sandbox 状态在工程上可行且成本可接受 | 对话是文本，sandbox 可快照 | 第 0 周 spike：迁移时间和快照大小 | 对话 + 命令日志重放，接受保真度打折 |
| H12 | 六个端口的边界是对的：换 Executor 和 Harness 的适配器不改变 S2 对 B1 的结论 | 设计里 L2 只依赖 L1；herd 工具的 hook 住在服务端，harness 相关的能力是端口上的显式字段 | 第 4 周换 k8s Executor，第 5 周换第二个 Harness，重跑 S2 vs B1 @ 5 | 结论变了就说明有泄漏，把泄漏的能力提成端口的显式字段，不回退到 glue |
| H13 | 在可组合的问题上，按"对当前最优组合的边际增量"分配优于按方向自身最优分分配 | 方向互补时，自身分高的方向可能和组合重叠 | 第 5 周起 A/B 两种 improvement\_rate | 保留方向自身分 |

## 4. 系统概览

五层。每层的存在都能追溯到一条原则或约束：判断层存在是因为 P4（判断和可见性分离），共享状态层存在是因为 C4 和 P2（状态在 agent 之外、无损），机制层存在是因为 P4 的另一半（可见性由纯代码控制）和 C2（必须过滤），执行层是 worker，人机层是 P6。Worker 只和仓库与机制层打交道，从不直接给其他 agent 发消息；判断层只读 digest、写决定，不在 worker 的读路径上。

&#91;embedded content: 系统架构 · 五层，worker 只通过机制层和仓库交互\]

读法：向下的箭头是信息流向 worker 的三条通道（diff、检索、硬中断），向上的箭头是 worker 的贡献经 merge queue 进仓库。Eval 结果写回仓库，再经 hook 变成事件。图里的 Router 在第一版是 harness 内的一次检查，不是独立服务（D29）。

| 角色 | 数量 | 模型 | 触发 | 读 | 写 | 来源 |
| --- | --- | --- | --- | --- | --- | --- |
| PI agent | 1，跨问题持久 | 最强 | 人的消息；Planner 的升级请求 | charter、digest、decisions、任意轨迹 | charter、author=human 的 claim、首波 fork | P6 |
| Planner | 1 | 最强 | 每个名额窗口的顶层 eval 结果到齐后 | 全部 digest、eval 统计 | 只写 decisions | P4、D15 |
| Scout | 1，第 5 周起 | 最强 | 每 K 小时 | 全部 digest、surprise、高分 diff | 跨方向 insight；从自身上下文 fork worker | P8、H9 |
| Critic | 1 到 3 | 最强 | 候选进 top-k 或分数跳升，限速 | 候选、eval 明细、eval 代码、holdout | audit，分公开结论和不公开理由 | P3 |
| Librarian | 第一版 1 个 | 最强 | 连续跑 | 新 claim、高分 diff、轨迹 | digest、insight、标签 | P4 |
| Worker | 50 | 尽量强 | 领任务 | spec、charter、digest、diff、检索 | claim、candidate、轨迹、checkpoint | P1 |
| Indexer、Router 检查、Merge queue、Eval service、Budget meter、Integrator | 各 1 | 无 | 仓库事件、队列、tool call 边界 | 仓库、KV | 索引、中断、main、result、计量 | P4 第 0 层 |

## 4.1 架构分层与端口

**D59 \[推论；约束 C8 的反面\] herd 是一个独立的 runtime + orchestration，通过端口接外部世界，不绑定任何集群管理或 agent 实现。** 理由：本文讨论出的有效部分（黑板、状态机、eval 阶梯、分配、通信规则、对齐协议）和"agent 用什么起、每个 agent 具体怎么实现"是正交的；绑在一起，换一个 harness 或换一个集群就要重写，PoC 的结论也没法迁移。

```
L3  问题实例      charter/、spec.md、eval/sources/、approaches/          每个问题一份
L2  herd runtime  黑板约定、状态机、事件、Indexer、任务队列、Eval service、
                  分配、路由规则、停止规则、Budget meter、Integrator、
                  角色程序（PI、Planner、Critic、Librarian、Scout）      要建的东西
L1  端口          Executor、Harness、SignalSource、Store、Human、Model     接口；L2 只依赖这一层
L0  适配器        k8s / 本地进程 / Ray；Claude Code / Agent SDK / OpenHands；
                  git / Redis / S3 / sqlite-vec；CLI / Slack             可替换；集群仓库在这里
```

**六个端口**

| 端口 | L2 需要它做什么 | 第一版适配器 | 之后 |
| --- | --- | --- | --- |
| Executor | `start(agent_spec, task, ctx) → handle`；`checkpoint(handle) → blob`；`restore(blob) → handle`；`kill`；心跳 | 本地子进程 | k8s pod（接集群仓库）、Ray |
| Harness | build(task, ctx, tool\_endpoint, hooks) → agent\_spec，交给 Executor 起；进程通过 herd 的工具服务调工具，结束时留下 claim；export\_state(handle) → blob / import\_state(blob)（有 supports\_checkpoint 才有）。能力声明：supports\_tool\_hooks 和 `supports_checkpoint` | Claude Code headless | Agent SDK、OpenHands、自写循环 |
| SignalSource | 第 9 节的声明；`submit(candidate)` / `poll` / push 回调 | PoC 阶段由测试台的慢档服务实现（另一份文档《Herd 测试台》）；herd 侧只有一个 HTTP 客户端 | Kusto、flight、人 |
| Store | VersionedStore（git 语义：append、merge 序号、hook）、KV、ObjectStore、VectorIndex | 内存版（测试用）+ 真 git / Redis / S3 / sqlite-vec | 换实现不改 L2 |
| Human | PI agent 和人之间的消息通道 | CLI 长期会话 | Slack、Web |
| Model | `complete(role, messages, tools)`；embed(texts) → vectors（Indexer、去重、diff 排序都要；内存适配器用词袋或 hash） | 一张按角色的模型路由表 | 按角色换模型，A/B |

**D60 \[推论 P4\] hook 分三层，按它关心的事放。** 第一层，herd 工具的 hook 住在 MCP 工具服务端：repo\_write 的前缀检查、eval\_request 前的检索和 gate、立即 push、surprise 落盘。这些只关心 herd 工具，服务端就够，任何会说 MCP 的 harness 不改一行就能当 worker。第二层，中断检查需要看到 harness 的全部 tool call，而 Claude Code 内建的 Bash、Read、Edit 不经过 MCP server，所以它用 harness 自己的 tool hook（Claude Code 的 PreToolUse）；Harness 端口为此有可选能力 `supports_tool_hooks`，没有的 harness 只在 herd 工具调用时查，粒度更粗。第三层，Executor 兜底：心跳由 Executor 的 sidecar 按时间打，不依赖任何 tool call；Router 命中后 5 分钟 \[默认\] 没被确认，Executor 发 SIGTERM 触发 checkpoint 并带中断内容重启；进程退出时没有 claim 就带提示重启一次。v2 初稿说"hook 全在服务端"是错的，它漏了 harness 的内建工具。

**Harness 端口会泄漏的地方，写明不藏。** fork 的保真度取决于 harness 能不能导出和导入对话状态。端口上这是可选能力：`supports_checkpoint` 为 true 的 harness 支持真 fork；为 false 的退化到"从轨迹指针重启"，H2 的收益打折。Store 端口的 VersionedStore 也有一点泄漏：L2 依赖"merge 序号全局单调"这个语义，适配器必须保证。

**角色程序不是端口。** PI、Planner、Critic、Librarian、Scout 是 herd 自己的代码，用 Model 端口调模型、用 Store 端口读写。换模型是配置，换角色逻辑是改 herd 本身。

**D61 \[假设 H12\] 抽象的验证方法与节制。** 端口对不对，用换适配器来验：同一个问题、同一份 charter，Executor 从本地进程换成 k8s，Harness 从 Claude Code 换成 Agent SDK，S2 对 B1 的结论不该变。节制三条：第一版每个端口只做一个真实适配器加一个内存适配器；第二个真实适配器放到 H1 之后，从第二个的差异里提炼接口，不要在只有一个实现的时候设计通用性；内存适配器不是可选项，它让整个 herd 能在一个进程里几秒钟跑完一个微型问题，是迭代速度的来源，也是每次改动的回归测试。

**仓库形态**

```
herd/
  core/        L2：blackboard, queue, eval, allocation, rules, stop, budget
  roles/       L2：pi, planner, critic, librarian, scout（prompt + 程序）
  tools/       L2：MCP 工具服务，herd 工具的 hook 在这里（D60 第一层）
  ports/       L1：executor, harness, signal_source, store, human, model
  adapters/    L0：executor/{local,k8s}, harness/{claude_code,sdk},
                   store/{memory,git,redis,s3,sqlite_vec}, human/{cli}, model/{router}
  problems/    L3：每个问题一个目录
  tests/       用 memory 适配器跑的端到端
```

集群仓库不并进来，它是 `adapters/executor/k8s` 的依赖。

## 5. 数据模型

**D30 \[推论 P2、P4；假设 H4\] 一个 git 仓库是唯一的 source of truth。** 所有派生状态（SQLite 索引、embedding、当前状态）都能从它重建。选 git 不是省事：commit history 是 append-only 日志（P2 要求无损），branch 是 fork，merge conflict 是免费的冲突检测，blame 是 credit assignment，CI 是验证器门禁（P3），PR 是提交前评审，bisect 能找出哪次改动让分数掉了。Claude Code 这类 harness 对 git 的熟练程度也远超任何自定义 API。规模上限是 H4。

**目录布局**

```
charter/                     第 10 节。status 不是 aligned，herd 不启动
  params.yaml                机器可读参数：截断值、surge 的 k、底线、注意力分钟、预算上限
spec.md                      问题陈述 + eval 接口，charter 签字后由 PI agent 生成（H1 阶段人手写）
approaches/<id>.md           方向。frontmatter: status, parent。不存份额，份额在 allocation/
allocation/<local_id>.json   当前分配（生效顺序按合入的 seq，同事件）：每方向 target_slots 和 eval_share；分配规则或 Planner 覆盖写入
claims/<agent>/<id>.md       一条贡献，按作者分目录
candidates/<hash>/
  candidate.*                候选本体，首创者写；CI 按首创者判权
  prediction.json            首创者写
  local.json                 首创者的本地代理 eval 记录
  result.json                只有 Eval service 能写
  lineage.json               parents: […]，可多父（组合候选，D64）；Eval service 校验父 hash 后写
digests/<approach>.md        Librarian 维护，硬上限 2,000 token [默认]
decisions/<ts>.md            Planner 写，可被申诉
events/<agent>/<local_id>.json  状态变更事件，按发起者分目录，永不改原文件；写者不知道 seq
events/_seq/<seq>             只有 merge queue 能写：指向被合入的事件文件，seq 在合入时才分配（D56）
traces/<agent>/<task>.ptr    指向对象存储的指针 + 摘要 + surprise 标记
checkpoints/<agent>/<ts>.ptr 对话 + sandbox 状态的 checkpoint 指针
eval/sources/<id>.yaml       source 的公开元数据；定义本体在私有存储（D10、D63）
```

**D76 \[推论 P2、P4；来自 v3 审稿\] v2.2 新增三个目录。** `map/` 是机会地图：把差距按原因拆开的测量脚本，和它们在每一版基线上的输出（map/gap.md、map/measurements/\<id>/），每次 baseline\_advanced 后由 Integrator 重跑，人和 agent 都能读、能引用、能重跑。第一批测量脚本由人在建问题时手写，和 spec.md、首批 approaches 并列进门槛前清单，首批 approaches 对照 map/gap.md 来写；测试台的两个问题由问题包自带。S2 的 worker 可以往 map/measurements/ 加新脚本，走 repo\_write 和 CI 的 schema 检查；S1 收得到更新后的地图，但不能加脚本。它是共享信息，不是派活依据：任务形态不变（D1），方向仍由 PI 和 Planner 按 approach portfolio 管（D24）；地图告诉大家差距在哪、哪部分还没人解释得了，PI 可以据此开一个探索方向。共享的是能重跑的测量而不是摘要，这正是 P2。`baseline/<k>.json`：每一版基线含哪些候选、配对结果、gate 结果（D74）。`report/`：运行结束生成的归因报告和合并日志。事件类型相应补齐：baseline\_advanced、integration\_failed、interaction\_found、map\_updated、audit\_checked、baseline\_reverted、dedup\_hit；claim 状态加 quarantined。

**不在 git 里、在 KV 里的三样**：lease（D32）、任务队列、eval 队列状态。它们的变化频率比 commit 高两个量级。

**D63 \[约束：git 没有按路径的读权限\] 不在黑板仓库里、在私有存储里的东西**：eval 定义与数据、holdout、audit 的理由区、Critic 的笔记。黑板仓库对 worker 是全量可 clone 的（P1 要求 worker 看全部材料），所以"仓库里某个目录 worker 不可读"在 git 上做不到；任何 worker 不该看的东西都不能进黑板，只能放一个 worker pod 没有凭证的独立存储（另一个仓库或对象存储 bucket）。黑板里只放 source 的公开元数据和 audit 的公开结论。PoC 阶段隐藏数据和打分代码只在测试台慢档服务的机器上，worker 机器上没有。只有 Eval service、Critic 和人有私有存储的凭证。PI agent 没有：首波 worker 从它的上下文 fork（D22），它读过的东西会被继承，所以它只能看 audit 的公开结论。

**D31 \[推论 C4、P2\] append-only。** 每条贡献是一个新文件，任何 agent 不改别人的文件；状态变化只能通过 events 表达；worker 只能写自己前缀下的路径，CI 检查。这三条让 50 个 agent 并发 push 几乎不冲突，也让历史不可篡改。按作者分目录是为了让 CI 的路径检查可执行（v1 审稿）。

**D32 \[约束：心跳频率\] lease 不进 git。** lease 是派生状态，放 KV（Redis 或 NATS KV）：`{agent, approach, target_claims, depends_on, task_id, heartbeat, files_touched}`。心跳由 Executor 的 sidecar 每 30 秒 \[默认\] 打一次，与 tool call 无关（D60），每次心跳同时对沙箱跑 git diff --name-only，把 files\_touched 写进 lease（第 8 节规则 6 和 D74 用它）；TTL 2 小时 \[默认\]，超时由 Indexer 的清理任务回收，回收前先问 Executor 该 handle 是否还活着。原因：心跳频率和 commit 频率差两个量级，放 git 里会把仓库刷爆。

**D33 \[推论 P2\] 轨迹和 checkpoint 在对象存储，只归档不删除。** 仓库里只有指针。evidence 指针必须永远可解析，CI 在合入时验证。30 天后压缩到冷存储 \[默认\]。

**claim 的 frontmatter**

```yaml
id: c-2026-10-17-0412
type: result | insight | dead_end | metric | question | audit
status: proposed            # 初始状态；当前状态由 events 算出
approach: a-03
author: worker-17 | librarian-1 | critic-1 | scout | eval_service | human
tags: [low_signal, new_approach, cross_approach]   # 可选；Librarian 或作者写
blamed_constraint: constraints.md#…                # 可选；dead_end 把原因归到某条约束时填（D52）
depends_on: [c-…]           # 可以依赖 proposed 的 claim，但必须标注其当前状态
evidence:                    # 至少一条，否则 CI 拒绝
  - trace: traces/worker-17/t-0391.ptr#L120-L188
  - candidate: candidates/9f3a…/result.json
surprise: true | false       # 作者标记"和预期不一样"（D8）
reuse_count: 0               # Indexer 维护，不由作者写
```

**D9 \[推论 P3\] 每类 claim 有自己的状态机，不共用一个 verified。** v1 只有一个 verified，CI 只能验证 result，insight 永远停在 proposed，P3 又禁止 build on 未验证的东西，等于封死了共享。改成按类型分：

| claim type | 状态 | 谁能推进 |
| --- | --- | --- |
| result | proposed → 按 source 阶梯逐级 verified，任一步可 → refuted；verified → quarantined → verified \| refuted（抽查二分出的肇事者，D72） | Eval service 按层级；Critic 可 refuted；Integrator 可置 quarantined，只有人能解除；人可任意 |
| insight | proposed → corroborated（被 ≥ 2 个顶层 verified 的 result 引用，或 Critic 确认）；可 → refuted | Indexer 按复用自动；Critic；人 |
| dead\_end | proposed → confirmed（Critic 确认，或第二个独立 worker 复现）；可 → reinstated | Critic；worker 复现；人 |
| metric | proposed → calibrated（与上一层 trust 更高的 source 相关性 ≥ 阈值、样本 ≥ N）→ deprecated | Eval service 自动；Critic；人 |
| question | open → answered | 任何角色 |
| audit | 终态 | Critic 写，分公开结论和 worker 不可读的理由 |
| approach | active → frozen → dead\_end；可 → reinstated | Planner（预算份额超 30% 的要升级给人）；人 |

**D34 \[推论 P3；v1 审稿\] 事件的三条配套规则。** 优先级人 > Critic > Eval service / CI / Integrator > Planner > Indexer > Scout / Librarian / worker，冲突时高的覆盖；单一来源的 refuted 不级联，只有人或 Critic 发出、或被第二来源确认的 refuted 才触发第 8 节的硬中断，`reinstated` 事件撤销；事件顺序用 merge 序号，不用跨机时钟。

**D67 \[约束：git 和 shell 都不能限制读；外部审稿 N7\] 臂和运行的隔离靠物理分开，不靠规则。** D58 说 B1 的 worker 只读自己的前缀，但 worker 有完整 clone 和 shell，规则挡不住。所以：每次运行一个 `run_id`，自己的仓库、KV 命名空间、eval 配额，12 次运行互不可见；B1 的每个 worker 用自己单独的仓库，结束后由 Eval service 汇总做合并；S1 的"只看候选和分数"是 Indexer 发布的一个过滤镜像仓库，worker clone 的是镜像不是原仓。

**D68 \[推论 D31；外部审稿 N8\] worker 的 clone 是只读的，push 凭证只在 MCP 工具服务手里。** 否则 worker 用 git push 绕过 repo\_write 的前缀检查和立即 push 的记账。repo\_write 在服务端 commit 并 push，worker 的本地 clone 只负责读，定期 fetch。CI 的路径检查仍保留，作为第二道门。

**D69 \[外部审稿 C8；第 30 轮改哈希\] 候选的格式。** `candidates/<hash>/manifest.yaml`：`{kind: patch | config | artifact, target: <基线标识，不再允许目标仓库 sha>, parents: […], approach, author, created_at}`，加 payload：patch 是对 target 的 unified diff，config 是一个文件，artifact 是一个目录。hash = hash(基线标识 + 规范化后的 payload)（D73）：同一个 diff 打在不同基线上是不同的候选，否则 Integrator 的复测会撞上 dedup\_hit、静默地不跑，而那次复测本该抓出相互作用。去重按这个 hash；相似度检索（needs\_confirmation）仍只算 payload。组合候选的 payload 是合并后的完整 patch，不是父的引用。

**D55 \[推论 C4\] 任务队列的定义。** v1 和 v2 初稿都引用了 `claim_task()` 却没定义队列，这是实现第一天就会卡的地方。队列是 KV 里的一个按 priority 排序的集合：

```yaml
task:
  id: t-0391
  approach: a-03
  goal: "用方法 Y 推进整个问题" | "根据候选 c-xxx 的结果决定下一步" | "给 dead_end c-yyy 找反例" | "组合候选 c-a 与 c-b"（D64）
      | "把候选 c-x rebase 到 baseline@k 并解决冲突"（compose，D74；执行者是池外新开的 agent） | "integration_failed 后的后续"（D74）
  seed_context: checkpoints/worker-17/…ptr | null   # 非空 = fork 式恢复
  priority: 0 | 1 | 2                                # 0 最高
  for_agent: worker-17 | null                        # 非空 = 优先给它，它没 lease 时进公共队列
  created_by: indexer | human | scout | planner | integrator
```

谁生成：Indexer 按每方向的 target\_slots 维持常规任务（goal 用方向模板），少了就补；result\_ready 到达时 Indexer 无条件生成后续任务，priority 0，for\_agent 是提交者；人、Scout、Planner 可以注入。队列空时 worker 领 exploration 模板任务（新方向、长尾方向、给 dead\_end 找反例）。H1 阶段还没有 checkpoint（第 4 周才接），所以 result\_ready 后续任务的 seed\_context 为空，ctx 里带 result claim 指针和该 worker 自己的 claims；submitter\_checkpoint 字段可空。`claim_task()` 的实现：先取 for\_agent 等于自己的，再按 priority，再按方向的 `target_slots - current_slots` 最大者。

**D56 \[推论 P3\] 事件 schema 与 merge 序号。** 所有状态由事件回放算出，所以事件必须有统一结构：

```yaml
event:
  seq: 10482            # 由 merge queue 在合入 main 时分配，全局单调；写者提交时这个字段为空
  local_id: critic-1/00017   # 写者自己的序号，就是文件名
  ts: 2026-10-18T07:02:11Z
  actor: critic-1
  role: human | critic | eval_service | ci | indexer | planner | scout | librarian | integrator | worker
  type: claim_status | approach_status | result_ready | surge_start | surge_end
      | plateau | escalation | lease_stale | allocation | reinstated
      | baseline_advanced | baseline_reverted | integration_failed | interaction_found
      | map_updated | audit_checked | dedup_hit                      # D74、D76 加的
  target: c-0211 | a-03 | t-0391
  from: proposed        # 状态类事件用
  to: refuted
  source: bench_full    # result 类事件用
  reason: "eval 解析器 bug"
  evidence: [claims/critic-1/audit-0211.md]

# result_ready 的额外字段（D28、D57、D73）
  candidate: 9f3a…
  claim_id: c-…            # eval_request 时自动生成的 result claim
  submitter: worker-07
  submitter_checkpoint: checkpoints/worker-07/…ptr
  baseline: 6              # 配对的那版基线
  paired: {mean: 1.8, se: 0.6, n_items: 120, n_reps: 1}   # n_reps 只计 Eval service 自己跑的复测，缓存命中不算
  per_item: eval/results/…/per_item.json                   # 指针；比例型指标另带 bootstrap 区间
  score: 63.4              # 绝对分，只作诊断

# Integrator 发的事件的字段（D74）
# baseline_advanced:   baseline: 7, parent_baseline: 6, candidate: <hash>, files_changed: [prompt_builder.py],
#                      paired: {mean, se, n_items, n_reps}, guard_pending: true | false
# baseline_reverted:   from_baseline: 9, new_baseline: 10, removed: [<hash>], reason: audit | guard:<name>   # 记录只追加
# integration_failed:  candidate, baseline, paired, reason: below_1se | gate:<name> | conflict_unresolved
# interaction_found:   candidate, baseline, earlier: {mean, se}, remeasured: {mean, se}, overlaps_with: [<hash>]
# audit_checked:       segment: [j, k], daily: {mean, se}, audit: {mean, se}, threshold, flagged
# map_updated:         baseline, measurements: [<id>], output: map/measurements/<id>/<baseline>/
# dedup_hit:           candidate, original_claim
```

seq 的分配：merge queue 是唯一写黑板仓库 main 的进程（被优化系统仓库的基线分支由 Integrator 写，D74；两个仓库、两个写者），每合入一个带事件的 commit 就写一个 \`events/\_seq/\<seq>\` 指向该事件文件，并把 seq 写进 merge commit 的 trailer。Store 端口的 \`merge(branch) → seq\` 就是这个语义。优先级冲突按 role 解决（D34）。Indexer 按 seq 回放，重建永远确定。

## 6. 组件

十一个组件，六个没有模型。**D13 \[推论 P4\]**：模型只出现在需要判断的地方，而且判断的 agent 从不坐在读路径上；控制谁能看见什么的全部是纯代码，因为纯代码不会总结、不会有偏见、能被审计。

| 组件 | 模型 | 职责 | 触发 | 失败时 | 来源 |
| --- | --- | --- | --- | --- | --- |
| Indexer | 无 | 解析 frontmatter 进 SQLite；embedding 新 claim、digest、轨迹切片；按 events 重算当前状态；维护 reuse\_count；提供 `diff_since(cursor, working_set)` 和 `search(query, k)`；lease TTL 清理；为 result\_ready 生成后续任务（D28） | 仓库 post-receive hook | 可从仓库全量重建；重建期间 worker 只读 spec 和 digest | D13 |
| Router 检查 | 无 | worker 用游标问 Indexer "有没有事件命中我的 lease"，命中按第 8 节规则处理。检查点：harness 有 tool hook 则每次 tool call，否则每次 herd 工具调用；Executor 按 D60 兜底。规则是数据 `router/rules.yaml` | tool call 边界 | 跳过，下一个同步点通过 diff 看到 | D29 \[假设 H3\]：第一版不做 NATS 推送 |
| Merge queue | 无 | worker push 到自己 branch，按序 rebase 到 main。CI 两级：路径、schema、evidence 指针检查同步秒级；验证器异步，结果以事件回写 | push | 积压是 H4 的信号 | D31 |
| Eval service | 无 | 外部 eval 的薄客户端：把 eval\_request 转成 submit，轮询 status，写 result.json 和 result\_ready。PoC 阶段对接的是测试台的慢档服务（另一份文档《Herd 测试台》），接口 submit / status / history 和真外部 eval 一致；配额、隐藏数据、多种子都在那边，不在这里 | eval\_request；push 型 source 的到达 | 队列持久化在 KV | 第 9 节 |
| Budget meter | 无 | 按角色、方向、source 计量 eval 机器小时（token 只记录不设上限，P10），对照 charter/constraints.md，超额触发 escalation | 每次 API 调用和 eval 完成 | 计量滞后但不阻塞 | v1 审稿：没有它预算只是一句话 |
| Librarian | 最强，同其他角色（P10） | 连续跑，每轮处理上次之后的新 commit，没有分批参数（P10）：给新 claim 打标签（含 low\_signal）；重写 digest（结构固定：当前最优、活跃假设、corroborated insight、proposed insight、dead end、open question；≤ 2,000 token）；从高分候选相对父代的 diff 提炼 insight。第一版 1 个，不分方向 | 有新 commit 就跑 | digest 过期，worker 可绕过直接检索 | D14：编辑不是守门人 |
| Critic | 最强 | 解释高分候选为什么得分；对照 holdout；查与 dead\_end 的相似度；查是否利用 eval 漏洞。写 audit：公开结论 + worker 不可读的理由。唯一能读 eval 代码的 agent | 候选进 top-k 或跳升超 2 个历史标准差 \[默认\]；限速每小时 6 次 \[默认\] | 积压按跳升幅度排队 | D11 \[推论 P3\]；理由不公开是因为"为什么得分"就是 hacking 说明书 |
| Planner | 最强 \[假设 H7\] | 读全部 digest 和 eval 统计，只写 decisions：各方向预算份额、冻结或撤资、队列重排、点名 Librarian 或 Critic。每个决定带理由。escalation 列表里的决定只能写 request 给 PI agent | 每个名额窗口的顶层 eval 结果到齐后；兜底每 N 小时 \[默认 顶层 eval 时长的一半\] | 上一份 decisions 继续生效；worker 自领任务不依赖它 | D15 \[推论 P4\]：不做全景、不 fork、不调通信参数 |
| Scout | 最强 \[假设 H9\] | 每 K 小时 \[默认 6\] 读全部 digest，抽查 surprise 和高分 diff，专找跨方向连接；写 cross\_approach insight；值得追的直接从自身上下文 fork worker。不写 decisions、不碰预算 | 定时 | 跨方向连接靠人 | D16 \[推论 P4、P8\]：持有全貌的脑子和分配预算的脑子分开 |
| PI agent | 最强 | 只和人对话。协商 charter，签字后生成 spec.md 和 approaches/，fork 首波 worker。运行期做控制台。跨问题持久 | 人的消息；Planner 的升级请求 | 人直接改 charter | P6，第 10 节 |
| Integrator | 无 | 唯一能写被优化系统仓库基线分支的进程。verified 候选先进先出：rebase；有冲突就发 compose 任务（池外新 agent 执行）；基线动过就 remeasure 一次配对；按接收规则合入，发 baseline\_advanced 等事件。每 3 次合入批量跑贵 gate，检出问题就二分、写排除肇事者的新基线。每 2 次合入触发抽查（两次新测量）。baseline\_advanced 后重跑 map/ 的测量脚本。运行结束执行收尾顺序和归因。四个臂同配置 | result claim 到 verified；预算用完 | 队列在 KV，从 baseline/\<k>.json 恢复 | D74 \[推论 P1、D64\]；第 30 轮 |

## 7. Worker harness

**D35 \[推论 C4\] worker 无状态。** 它的所有状态要么在仓库，要么在对象存储的 checkpoint。死了就从 checkpoint 或任务队列重来。原则不靠 prompt 里的叮嘱，靠 hook：prompt 的行为在长任务里会漂移，hook 每次都执行，而且能被日志审计。Prompt 只负责说清楚目标和品味。

**D1 \[推论 P1\] 任务形态只有一种**：`{approach, goal, seed_context?}`。goal 是"用方法 Y 推进整个问题"或"根据候选 c-xxx 的 eval 结果决定下一步"，从不是"读 X"或"看 Y 目录"。每个 worker 拿到全部材料。seed\_context 非空表示从该 checkpoint 恢复对话再追加 goal，fork、eval 结果后续、PI 首波都走这条路。

```
loop:
  task = claim_task()
  ctx = spec.md + charter 摘要 + digests/<approach> + diff_since(cursor, working_set)
  acquire_lease(approach, goal, target_claims)   # KV，不进 git（D32）
    # target_claims = goal 里解析出的 claim id；之后每次 repo_write 写 claim，
    #   MCP 服务端把新 claim 加进 lease.target_claims，其 depends_on 并进 lease.depends_on
  run_agent(ctx, tools, hooks)
    # 每条 claim / surprise / eval_request 产生时立即 commit + push（D36）
  release_lease(); checkpoint(); push(branch)
```

**工具**

| 工具 | 权限 | 来源 |
| --- | --- | --- |
| repo\_read | 全仓库，含别人的 claim、decisions、charter 讨论全文 | P1：看全部材料 |
| repo\_write | 只新增，只在自己前缀下，CI 二次检查 | D31 |
| search(query, k) | 语义检索 claims、digests、轨迹切片、charter 讨论 | D7 推转拉 |
| eval\_request(candidate, parents, predictions{source: score}, rationale, confirm\_token?, baseline?) → {request\_id, claim\_id, baseline, status: queued \| needs\_confirmation{similar: \[…\]} \| dedup\_hit \| gate\_failed \| invalid\_request, degraded\_sources: \[…\]}。两阶段：第一次调用若检索到相似候选或 dead\_end，返回 needs\_confirmation 和一个 confirm\_token，worker 带 token 再调一次才入队（D26）。baseline 缺省为当前基线；Integrator 的复测走内部接口 remeasure(candidate, baseline)，不经过 needs\_confirmation，也不经过去重（D74）。入队时服务自动生成一条 type=result 的 claim（evidence 指向候选）并返回 claim\_id，后续每级结果都是这条 claim 的状态事件；服务按声明自动逐级执行，worker 不自选层；parents 用于 lineage.json 的父 hash 校验，组合候选有多个（D57、D64） | 只能排队，不能直接跑，不能读 eval 定义 | D10 |
| register\_exposure(candidate, tag, window) | push 型 source 用，登记后信号才归到候选 | 第 9 节 |
| fork(reason, goal) | 保存 checkpoint 并起一个兄弟 worker | D5 \[假设 H2、H11\] |
| ask\_checkpoint(agent, ts, question) | 恢复某 checkpoint 的只读副本回答问题，含问 PI agent 的对齐对话 | D5 |
| surprise(note, pointer) | 写一条 type=question、surprise=true 的 claim，不要求有结论 | D8 |
| sandbox | 本任务的隔离计算环境，本地代理 eval 跑在这里 | C1 |

**Hooks**

| 时机 | 动作 | 来源 |
| --- | --- | --- |
| 每次 tool call 前 | 查有无事件命中本 lease（第 8 节规则）。需要 harness 的 tool hook；没有的 harness 只在 herd 工具调用时查，Executor 按 D60 兜底 | D17、D32 |
| 长 sandbox 调用期间 | 心跳由 Executor sidecar 按时间打（每 30 秒 \[默认\]），与 tool call 无关，长 sandbox 调用不会被误判 stale | D32 |
| 每条 claim / surprise / eval\_request 产生时 | 立即 commit + push | D36 \[推论 P5\]：共享时延不能等于任务时长 |
| eval\_request 前 | 自动检索相似候选和 confirmed 的 dead\_end，有命中则返回 needs\_confirmation，worker 带 confirm\_token 再调才入队 | D26 \[推论 P7\] |
| eval\_request 时 | 必须带每层的预测分 predictions 和 rationale，缺一拒绝；gate（D54）在这里先跑；本地代理结果写 local.json | D37 \[假设 H8\] |
| eval 结果到达 | Indexer 无条件生成后续任务，for\_agent 指向提交者；提交者持有 lease 时规则 1 的硬中断内容就是"去领它"，否则任务进公共队列前排（D55） | D28 \[推论 P9\] |
| 连续 N 次失败 \[默认 3\] | 强制刷新 digest 和一次检索。失败 = 一次本地代理或便宜层结果未优于该 worker 当前最优，或一次 eval\_request 被 gate / 去重拒绝 | D19 \[推论 P5\] |
| 发现异常 | 产出可执行的最小复现（失败测试、具体例子、notebook）作为 evidence | D8 \[推论 P2\]：artifact 不可压缩，散文会丢 |
| 上下文接近上限 | 不是 fork：fork 复制整个对话，子进程起点就在上限。做法是写一条结构化 contribution claim（含未完成的线索和自己的 surprise），checkpoint，然后以压缩上下文（spec + digest + 自己的 claims + 轨迹指针）起一个继续任务；它可以 ask\_checkpoint 自己的过去。按 P2 这是有损的，所以要尽量早 checkpoint。v2.2 把这条路径写具体（来自 v3 审稿）：触发点是窗口的 70% \[默认\]；Harness 适配器关掉自动压缩，用量从会话记录的 usage 字段算；新会话的起始包 = 原起始包 + 自己本任务的 claims + 中断内容（若有）+ 工作目录 diff + 最后 20 次工具调用 \[默认\]；进程被杀时由 sidecar 代写一条指向轨迹的 stub claim。这条路径不依赖 checkpoint，gate 前就能用，所有臂相同；fork 仍是 gate 后的事（D5） | C4 |
| 结束前 | 必须写 ≥ 1 条 claim 带 evidence；允许 dead\_end 含"本次无发现"，不允许空；上传轨迹；保存 checkpoint | D4、D25 |

**D5 \[推论 P2；假设 H2、H11\] fork 代替 handoff。** Checkpoint = 完整对话 + sandbox 状态（工作目录快照或可重放的命令日志）+ 工具状态指针。只存对话不存 sandbox 的 checkpoint 是半个：fork 出来的 worker 继承了想法，丢了工作目录。Fork = 用该记录在任意机器起新会话、恢复 sandbox、追加 goal；有 prompt cache 时对话部分是增量 token，sandbox 恢复成本是第 0 周 spike 要测的数字。回头问 = 只读副本，不恢复 sandbox，不写任何东西。fork 只用于在决策点分叉，不用于上下文解压：Executor 记录每个 checkpoint 的上下文用量，fork 优先选用量低于 60% \[默认\] 的 checkpoint，超过的先压缩再 fork。

## 8. 通信

**D38 \[推论 P1、P4\] agent 之间不互发消息。** 所有通信是对仓库的写和读。原因两个：点对点消息不可审计、不可回放，10,000 个 agent 的 P2P 是灾难；更重要的是，写进仓库的东西才会被索引、被检索、被 Librarian 看到，消息不会。

**D17 \[推论 P5\] 读分三条通道，由事件和 agent 状态触发，不由时钟触发。**

| 通道 | 触发 | 内容 | 成本 | 时效 |
| --- | --- | --- | --- | --- |
| 硬中断 | Router 检查命中 lease | 当前任务失效，或本 agent 的 eval 结果到了 | 一次 KV 读（Indexer 命中后写 interrupt:\<agent>，Router 检查只读这个键），每次 tool hook 或 herd 工具调用 | 下一个检查点，秒到分钟级 \[假设 H3\]；Executor 5 分钟兜底 |
| 软注入 | 到达同步点：领任务前、eval\_request 前、一次失败后、任务结束时、连续 N 次失败 | `diff_since(cursor, working_set)`，按相关性排序，截断 500 token \[默认\] | 500 token | 分钟到小时级 |
| 按需检索 | 形成假设或撞到异常时自己调用，部分由 hook 强制 | search 返回 top-k claim 和轨迹切片 | 按次 | 即时 |

**路由规则（`router/rules.yaml`，改规则不改代码）**

对每条活跃 lease `{agent, approach, target_claims, depends_on}`：

1. 本 agent 提交的候选 `result_ready` → 硬中断，内容是"去领 for\_agent 指向你的后续任务"（D28、D55）。
2. `target_claims` 中任一条的状态推进或 refuted → 硬中断。
3. `approach` 被 frozen、dead\_end 或预算归零 → 硬中断附 decision。
4. `depends_on` 中任一条的 refuted，且由人或 Critic 发出、或被第二来源确认 → 硬中断。单一来源的 refuted 只进 diff（D34）。
5. author=human 的 claim 标注了本 approach → 硬中断。人的介入优先级最高（P6）。
6. 有候选合入基线（baseline\_advanced），且本 lease 的 files\_touched 和合入的文件有交集 → 硬中断，内容是"rebase 到新基线"；没有交集 → 软中断，等 diff（D74）。
7. 其他 → 不推，等 diff。

**D18 \[推论 C2\] diff 的相关性排序与截断。** `working_set` = lease 里的 approach + target\_claims + 最近检索过的 claim id。排序：同 approach 的 verified > 同 approach 的 dead\_end > 跨 approach 的 insight > 其他。截断后附一行"还有 N 条，用 search 查"。截断值是 charter 里的参数，PI agent 按人的意见改，不是 Planner 的决定（D15）。

**D19 \[推论 P5\] 卡住时多读。** 连续失败越多，同步点越密；N 次失败后每次 tool call 都做一次 diff 检查直到有进展。理由：连续失败是"别人可能已经做过"的最强信号。

**D16 跨方向的 click 由 Scout 专职产生（第 6 节）**，因为没有任何 worker 同时持有两个方向的细节，不能指望自发出现；Scout 值得追的线索直接从自身上下文 fork，不写任务描述交给别人（D5）。

**D23 \[推论 P6\] 人的洞察写成 claim。** PI agent 把人的话写成 author=human 的 claim，evidence 指向对话 checkpoint，规则 5 让它对点名的方向立即可见；worker 可以 ask\_checkpoint 回头问 PI agent 那段对话的细节。

## 9. Eval

Eval 是稀缺资源（C1），所以系统的目标是让每一次昂贵的 eval 落在信息量最大的候选上，agent 的工作从"生成候选"变成"预测哪个候选值得一次 eval"。Eval 又是插件（C6），所以本节先定义接口，再定义从接口推出来的通用机制，最后说三种形态的差别。

**D39 \[约束 C6\] Eval 是一组 signal source，每个 source 自己声明属性，一切从声明推导。**

```yaml
# eval/sources/<id>.yaml   必填 7 个，其余可选，大多数外部 eval 只填必填
id: bench_full
mode: pull | push            # 必填。pull 按候选提交；push 自己按节奏到达
latency: 6h                  # 必填。提交到出结果
budget: {per_day: 40, allocated_by: planner | human}   # 必填
cost_machine_hours: 48       # 必填。一次执行的机器小时，外部系统就填它的计费折算；阶梯排序和 Budget meter 都按它算（D42）
trust: 0.9                   # 必填。0 到 1，决定阶梯位置和能否动永久预算
readable_by: [critic]        # 必填。决定定义存在哪：含 worker 则在黑板，否则在私有存储（D10、D63）
kind: tool | dataset | query | flight | human          # 可选，默认 tool
attribution: candidate | exposure                      # 可选，默认 candidate
concurrency: 9               # 可选，默认不限（外部系统自己排队）
noise: {n_for_ci: 3}         # 可选，默认 1；result 永远带 n（C3）
drift: fixed | drifting      # 可选，默认 fixed
escalate_on_request: false   # 可选；flight 为 true
compare_against: concurrent_control | none             # 可选；观察型信号用
```

| source | 典型声明 | 它改变了什么 |
| --- | --- | --- |
| 外部评测工具 | pull，candidate，小时级，trust 高，fixed | 基础设计 |
| 本地 dataset | pull，candidate，分钟级，trust 中，fixed | 自动切子集成更便宜的一层；worker 可能过拟合它，readable\_by 收紧 |
| Kusto 查询 | push，exposure，延迟等于数据管道滞后，trust 中高，drifting | 候选先登记 exposure；Critic 查混杂；只和同期对照比 |
| Flight | push，exposure，天级，并发几个，trust 最高，drifting | 推上 flight 走 escalation；result 带显著性 |
| 人 | pull，candidate，小时到天级，trust 高但噪声大 | 经 PI agent 排队；评审结果成为 metric 的标定样本 |

**从声明推导出来的通用机制**

- **阶梯 \[推论 C1\]**：按 cost\_machine\_hours 和 latency 排序，"过了便宜的才能上贵的"。T0 到 T3 只是某个问题里各 source 的位置标签，不是写死的层。
- **标定 \[推论 P3\]**：每个非顶层 source 对上一层 trust 更高的 source 维护相关性记录，低于阈值 \[默认 0.3，样本 20\] 自动降级，worker 提交时被告知"此 source 当前不可信"。这就是 metric claim 的通用形式。
- **D12 \[推论 P3\] 只有 trust 最高的那一层能改 Planner 的预算分配**，代理分只能决定队列内排序。这是反 Goodhart 的硬规则，Planner 不能覆盖。代价是 topline 稀疏时预算调整慢，这是有意选的保守。
- **D40 \[约束 C1；假设 H5\] 配额与本地代理**：顶层 eval 每天几十次，对 50 个 worker 是每人一天不到一次，所以 worker 循环的主驱动是 T0 加 sandbox 里自建的本地代理。本地代理是一等机制：结果和配置写 local.json，Eval service 用它和上层标定，相关性好的推广成正式的一层。配额：每 worker 每天 T1 ≤ 20 次 \[默认\]，顶层名额由 Planner 按方向分。第一版队列就是配额加 FIFO，排序留到标定数据够了之后（D41 推迟）。
- **D37 \[假设 H8\] 预测记录**：每次提交带每层的预测分 predictions 和 rationale，每层结果回来记录该层的误差。用途：训代理打分器（推迟）；标定 agent 判断力，加权只作用于 information 份额，不作用于 exploitation，否则奖励保守候选；让 agent 少提交低置信度的东西。
- **D28 \[推论 P9\] 结果有接收者**：result\_ready 绑定提交者和它提交时的 checkpoint，Indexer 自动生成后续任务进队列前排。
  - **D57 \[推论 C1、P3\] eval\_request 的闭合**：签名和返回值见第 7 节工具表。入队时自动生成一条 type=result 的 claim 并返回 claim\_id，后续每级结果是这条 claim 的状态事件，result\_ready 带 candidate、claim\_id、submitter、submitter\_checkpoint、baseline、paired{mean, se, n\_items, n\_reps}、per\_item 指针，绝对分只作诊断（D56、D73）；两阶段确认用 confirm\_token；parents 校验 lineage；分配状态只落在 allocation/；参数在 charter/params.yaml。

**反 hacking \[推论 P3、C3\]**

- D10：eval 定义、数据、holdout、audit 理由区放在 worker 没有凭证的私有存储（D63），不在黑板仓库里；靠仓库路径做读隔离在 git 上做不到。worker 只有 eval\_request 和 register\_exposure。
- D11：Critic 是唯一能读 eval 代码的角色；audit 的理由区 worker 不可读。
- holdout 由人维护，agent 从未见过，定期复核 top-k；与上层分数差异超阈值自动 refuted 并告警。
- 分数跳升超 2 个历史标准差 \[默认\] 是告警不是庆祝，自动触发 Critic。
- refuted 的候选及其子代全部标记。
- 出网白名单（D75）：worker 沙箱只通模型 API、herd 工具服务、声明过的上下文源和包镜像，各臂相同。否则 worker 可以去拿公开 benchmark 的答案。

**D66 \[约束 C3；外部审稿 N1\] 两份 holdout。** 运行中的复核（Critic 查 top-k、跳升告警）和最终判定如果用同一份 holdout，共享臂触碰 holdout 的次数更多，判定就偏向它。所以分两份：`holdout_run` 给运行中的 Critic 用，有次数上限 \[默认每臂每运行 10 次\]，各臂相同；`holdout_verdict` 封存，整个运行期间任何角色不能请求，只在运行结束后评每臂的最终交付物一次，n = params.verdict\_n \[默认 5\]，固定不自适应。两份都在问题建立的第一天切好，是数据不是代码，所以第 1 周就有。

**D73 \[约束 C3；来自 v3 审稿\] 配对测量。** 每次 eval 提交的是候选和它所基于的那版基线，两者在同一批数据、同一时段上跑，结果是逐项差值：均值、标准误、项数、复测次数 n。配对去掉了题目难度和时段的影响，这通常是噪声里最大的两块。n 只计 Eval service 自己跑的复测，缓存命中不算。比例型指标（如命中率）分子分母分别加总后再算差，区间用 bootstrap。候选的哈希 = 基线标识 + 规范化后的载荷（修正 D69）：同一个 diff 打在不同基线上是不同的候选。测试台的慢档返回逐种子分，任意两个候选的配对差事后就能算，不额外花顶层 eval——前提是同种子下 eval 是确定性的（问题 A 的仿真器是；问题 B 里有模型调用，仍要同批、同时段跑）。确定性的便宜层没有标准误，按"增益 > 0"晋级。

**D74 \[推论 P1、D3、D64；来自 v3 审稿\] 合并流程：基线一步步往前走，每合入一个就重测一次。** D64 说交付物是组合，这条把组合怎么发生写具体。判断仍由 agent 做，记账由代码做（P4）。

- 基线有版本：baseline@0 是原始系统，每合入一个候选前进一版。只有 Integrator 这段纯代码能写基线分支，每版记在 baseline/\<k>.json：含哪些候选、配对结果、gate 结果。
- verified 的候选按验证时间先进先出。按预测增益排会奖励吹牛。
- 合入前先 rebase 到当前基线。有文本冲突 → 发一个 compose 任务（D3、D64）；执行者是 worker 池外新开的 agent，不带任何臂的上下文，产出只回 Integrator，各臂相同（B1 里不能交给 B1 的 worker，否则破坏孤立；S2 的 worker 另外可以领 D64 的"组合候选"任务，那属于共享）。合好的结果作为新候选重新排队。无冲突但基线已经动过 → 对当前基线做一次配对复测；基线没动就复用验证那次，不多花。复测走内部接口 remeasure(candidate, baseline)，不经过 needs\_confirmation，也不经过去重。
- 接收规则 \[默认\]：配对增益超过 1 个标准误（单次配对测量的标准误：基线动过用复测那次的，没动用验证那次的）且过全部 gate 就合入；否则记 integration\_failed，给作者一个后续任务。相互作用：早先增益减复测增益超过 2σ√2 才记一条 interaction\_found（"不到早先的 70%"那种写法误报过半，因为早先那次是过了门槛的、本来就偏高）；Integrator 只发事件，Critic 复核后才写 tags=\[interaction\] 的 insight（D64）。这个阈值的依据只有模拟：eval 稀缺时它的净收益约是严格版的 2 倍、"复测三次看 95% 下界"的 2.9 倍（更接近现实的设定下是 1.2 到 1.6 倍），代价是十分之一到五分之一的合入是略有害的补丁（每次运行合计 −0.3 到 −1.4）。接收规则在预注册时选定，H1 期间不在运行中切换；要用严格版（先筛 0.5σ，再 3 次新复测，90% 下界 > 0），试点就用严格版。估噪声 σ 时用 A/A 复测的上界，不用点估计，否则这条规则会退化成"大于 0 就合"。
- 每次合入发 baseline\_advanced 事件，带 files\_changed；和本 lease 的 files\_touched 有交集的收硬中断去 rebase，其他人收软中断（第 8 节规则 6）。files\_touched 由 Executor 的 sidecar 在每次心跳时对沙箱跑 git diff --name-only 写进 lease（D32）。B1 的 worker 收不到这个事件，一直在 baseline@0 上干，Integrator 在后台合并它们 verified 的候选；S1 和 S2 收得到（D58）。
- 贵的 gate 每 3 次合入批量检一次，这期间的基线标 guard\_pending，可以在其上继续干；检出问题就二分找肇事者、写一版排除它的新基线（记录只追加，不改旧的）。交付物永远不是 guard\_pending 的。
- 运行结束的收尾顺序各臂相同：停止接收；对所有 guard\_pending 的基线补检 gate（固定的预算外配额，检出问题按原流程二分）；最终抽查；冻结交付物 = 最后一版非 guard\_pending 的基线；所有运行都结束后一起评 holdout\_verdict。归因在 holdout\_verdict 评完之后做，只用日常数据，固定配额，不影响判定："逐个方向去掉"，写 report/attribution.md：每个方向贡献多少（带区间）、总增益和各项之和的差（即相互作用）、死胡同。这份报告本身是交付物。运行中没有预算做消融，抓错误合入靠 D72 的抽查，不靠消融。
- 合并器各臂相同（第 30 轮）：Integrator 在 B0、B1、S1、S2 里都在运行中跑，接收规则、gate、guard 批次、抽查配置一样，复测花各臂自己的顶层名额。"共享"只指 worker 能不能收到 baseline\_advanced 并 rebase。每臂的交付物 = 该臂 Integrator 最后一版非 guard\_pending 的基线；单个最优候选的分另记为 B1-single 诊断列。不再有"结束后 git apply 贪心合并"这一步：那样 S2 多出来的分数里，分不清多少来自共享、多少来自合并器更好。Integrator 写的是被优化系统仓库的基线分支；黑板仓库的 main 仍只由 merge queue 写（D56），两者是两个仓库。

**D75 \[约束；来自 v3 审稿\] 出网白名单。** worker 的沙箱只能访问模型 API、herd 工具服务、声明过的上下文源和包镜像，别的一律不通，各臂相同。上下文源的白名单写在 charter/egress.md，没写的不通。否则 worker 可以去拿公开 benchmark 的答案。

**D78 \[推论 P10；第 30 轮推迟\] 用 token 换 eval——推迟到 H1 之后单独 A/B。** 原意是候选进慢档前由 2 个 Critic 各审一次，否决就不占名额。审稿指出三个问题：否决理由写成公开 claim 会把"为什么得分"泄露给 worker（D11 不公开理由正是因为它就是 hacking 说明书）；Critic 的 LLM 审计在门槛后，H1 阶段没人执行；只在 S2 开又是一个混淆。所以 H1 阶段不做，之后作为单独的 A/B 再试，那时由一个不读 eval 代码的预审员做，各臂相同。保留的两条：快档复测不限次数；compose 任务可以同时派给几个 agent，但多个版本只用快档挑，选出一版才进慢档。

**三种形态 \[约束 C6\]**

| 形态 | 顶层信号 | 下层信号 | 和基础设计的差别 |
| --- | --- | --- | --- |
| A 固定 eval | 给定的工具或数据集 | T0、T1、本地代理 | 无。eval 有噪声时 result 带 n 和区间 |
| B 有 topline 但不固定 | 周级大评测或线上指标，稀疏 | herd 自己设计并标定的代理 | 改尺子是工作的一部分：metric claim；队列多一类"改进测量"任务；Planner 多一份 measurement 预算 \[默认 15%\]；Critic 多查代理提升有没有转移到 topline；charter/progress.md 必须写明 topline 和代理边界 |
| C 没有 metric | 人的判断，经 PI agent，有预算 | Critic 加多个 worker 盲排名的共识 | 人是 T2；候选是文档不是代码；评审积累后训 judge 转为 B；价值保证最弱，不建议从这里起步 |

形态 A 的一个特例：被优化的就是 harness 本身。Eval = 用候选 harness 跑 held-out 任务集，每个候选至少 n 次；worker 自己跑在冻结版本上，候选只在 eval 沙箱里运行，否则 herd 在改自己脚下的地板。

## 9.1 动态分配：探索的广度与深度

这一节回答两个问题：怎么保证探索足够广又足够深；一个 agent 发现好方向时，怎么动态把更多 agent 和机器给它。v1 把这件事藏在"Planner 分预算"一句话里，不够具体，下面是机械的定义。

**D49 \[推论 C1、C4\] 分配的单位是 slot，不是机器。** 两种 slot：worker slot（一个 worker 进程加 sandbox）和 eval slot（外部 eval 的日配额）。每个方向有 `target_slots[a]` 和 `eval_share[a]`。"给方向加 agent"的机械含义是提高它的 target\_slots 和 eval 队列权重：worker 不被杀，完成当前任务后按新权重领任务（lease 上限按方向计算）；只有方向被 frozen 才硬中断。"给方向加机器"的含义是该方向的 worker 获得更多 sandbox 并发（本地代理跑更多）和更高的 eval 队列优先级。分配状态只有一个落点：allocation/\<local\_id>.json，生效顺序按合入 seq，由分配规则或 Planner 的覆盖写入，approaches 的 frontmatter 不存份额（D31）。H1 的对照阶段每方向 slot 数写死在 spec.md，D49 到 D51 全部关闭（D42 规则 1）。

**D50 \[推论 P3、P7\] 快反应和永久化分开：surge 机制。** 顶层 eval 太慢，等它回来再加码会错过窗口；便宜信号又不可信，直接动预算是 Goodhart。所以：

| 阶段 | 条件 | 动作 | 上限与期限 |
| --- | --- | --- | --- |
| 触发 | 一个方向在便宜层连续 k 个候选 \[默认 3\] 超过全局当前最优，且 Critic 未否决 | 该方向 target\_slots 翻倍，eval\_share 翻倍 | 单方向 ≤ 总 slot 的 40% \[默认\]；期限到下一次顶层结果回来，或 24 小时 \[默认\] |
| 固化 | 顶层 eval verified 了 surge 期间的候选 | surge 的份额转为永久预算 | 仍受 40% 上限 |
| 回收 | 顶层未确认，或期限到 | 份额回到 surge 前，释放的 slot 进 exploration 池 | 同一方向 48 小时内不能再 surge \[默认\] |

这和 D12 一致：顶层信号决定永久预算，便宜信号只能借、不能占。

**深度来自 fork，不来自人数。** surge 进来的 worker 不从零开始，从发现者在发现时刻的 checkpoint fork（D5），每个带不同的 sub-goal：做变体、做消融、和另一个方向组合、测鲁棒性、补 evidence。十个从零开始的 agent 是广度，十个继承了发现上下文的 agent 才是深度。sub-goal 由发现者在 surge 触发时列出 \[默认 5 类模板，发现者可改\]，这是它写进 claim 的一部分。

**D51 \[推论 P7\] 广度靠硬底线，不靠判断。**

- exploration 池的底线随时间退火：前 3 天 50%，之后 25% \[默认\]；永远不被 surge 占用。早期广，后期深。池里的 worker 领的任务是：新方向、长尾方向、给已有 dead\_end 找反例。
- 任何方向 ≤ 40%；每个 active 方向 ≥ 1 个 slot 保活，防止一个方向在轮到它之前就饿死。
- Worker 可以写 type=question、tag=new\_approach 的 claim 提出新方向，自动批准到 active 方向数上限 \[默认 8\]，超过的由 Planner 或人批。
- dead\_end 冻结方向释放的 slot 回到 exploration 池，不回到当前最优方向。

**第一版的分配规则是确定性的，不是 LLM。** `target_slots[a] ∝ softmax(improvement_rate[a] / τ)`，improvement\_rate 是该方向最近一个窗口 \[默认 24 小时\] 内顶层信号的最优值提升速率。只用顶层，不用代理，否则和 D12 矛盾；第一版用方向自己的最优分，第二版改用"加入当前最优组合后的边际增量"（D64、H13）；顶层窗口之间分配不变，变化只能来自有上限有期限的 surge（D50）；再套 25% 池、40% 上限、1 slot 保活。τ \[默认 1.0\] 越大越平均。Planner（LLM）只能带理由覆盖这个规则，覆盖本身写进 decisions 可被申诉。理由：确定性规则可预测、可调、一周就能落地；LLM 做分配是推迟项，等有了对照数据再 A/B。

**广度和深度各一个指标。** 广度 = active 方向数 × 候选 diff 的 embedding 多样性；深度 = 每方向 lineage 的最长链长和 corroborated insight 数。两个都进 dashboard，surge 和回收的每一次都记事件。

**D64 \[推论 P1、P7；第 22 轮\] 优化的产出是组合，不是单条路径的最优点。** 附录 B 和 harness 走查里最大的一步都来自组合：a-02 的截断修复加 a-04 的验证循环。不同方向在不同的地方各有改进，最终交付物是它们里不冲突的那部分合在一起。所以候选从一开始就是可组合的对象：lineage 允许多父；"组合候选 c-a 与 c-b"是任务模板之一，由 worker 在 sandbox 里做（冲突需要判断，不是机械 merge）；组合候选和普通候选一样走 eval 阶梯和 gate；Critic 多一项检查：组合分和各部分分的关系（≈ 和 → 可加；< max → 冲突；之间 → 部分重叠），写成 type=insight、tags=\[interaction\] 的 claim，这是只有组合才能产生的知识。

对分配的推论：在可组合的问题上，方向之间不是争一个奖的 bandit arm，是一个 portfolio，一个方向的价值是它对当前组合的边际贡献。所以 exploration 底线应该更高、kill 更少；D51 的 improvement\_rate 第二版改为"加入当前最优组合后的增量"（H13）。

**D65 \[第 22 轮\] best-of-N 是对照臂的名字，不是交付物的形状。** 它从 swarm scaling 文献和第 2 轮讨论里来，含义只是"N 个 worker 各跑各的、互不共享"。v2 初稿让这个名字渗进了判定（"选一个最优候选"），和 D64 矛盾。改法：所有臂的候选都由同一个 Integrator 在运行中合并（D74、D58），B1 只是 worker 看不见基线前进，这样 H1 检验的是"运行中共享"相对于"同一合并器下不共享"的增量，而不是"有没有合并"或"合并器好不好"的增量；单个最优候选的 holdout 分作为 B1-single 诊断列保留。

## 9.2 停止规则、人的注意力、硬约束门

三件第一轮 PoC 就会撞上、但 v1 没写的事。它们和 9.1 是同一类问题：资源有限时，注意力和信任该放在哪。

**D52 \[推论 C1、C5\] 停止规则。** 没有停止规则的系统会一直花钱。四种停止：

| 触发 | 条件 | 动作 |
| --- | --- | --- |
| 预算用完 | Budget meter 达到 constraints.md 的上限 | 自动停，汇报 |
| 平台期 | 顶层最优分连续 N 个名额窗口 \[默认 3\] 无改进，且 exploration 池在同期没有产出新的 corroborated insight | 发 plateau 事件，escalation 给人，选项：继续、改 charter、停。超时默认动作：冻结全部方向 |
| 全部冻结 | 所有方向 frozen 或 dead\_end | 自动停，汇报 |
| 达标 | 顶层分达到 progress.md 的目标 | 汇报，人决定是否继续 |

"该换 charter 而不是继续"的两个信号，由 Librarian 标注、PI agent 呈现：多个方向的 dead\_end 都把原因归到同一条约束上（claim 带 `blamed_constraint`）；人注入的洞察连续两次没有产生 corroborated 的后果。这两个信号说明问题定义可能错了，不是执行错了。

**D53 \[约束 C5\] 人的注意力分配。** 人每天给 herd 的时间写在 charter（`attention_minutes_per_day` \[默认 30\]）。PI agent 维护一个优先队列，每次呈现带"预计占你几分钟"：

1. 临近超时的 escalation
2. Critic 推翻当前最优
3. surge 的固化或回收需要人批的（只在方向份额超 30% 时）
4. surprise 抽样：每天 N 条 \[默认 3\]，按 Librarian 标的跨方向性排序
5. top-k 抽查防 hacking：每天 1 条
6. 常规汇报

超出预算的一律攒到周汇报。人看过什么、跳过什么记成事件，这是 taste.md 最可靠的来源：人实际把注意力花在哪，比人说自己关心什么更准。

**D54 \[推论 P3；约束 C3\] 硬约束做门，不做分数。** constraints.md 里能自动检查的约束（非回归测试、延迟上限、成本上限、权限模型不变、安全扫描）编译成 gate，在 eval\_request 前作为 T0 的一部分执行，result 写回后再执行一次。不通过直接 refuted，不进分数，Planner 不能权衡，只有人能改 gate 列表。区分两类东西：score 可以权衡，gate 不能。gate 失败的候选写一条 type=dead\_end、reason=gate:\<name> 的 claim，这样提交前检索也能提醒别人。harness 自优化那类问题里，"分数好但破坏了任务集之外的东西"是最常见的事故，这是它的防线。

## 10. 对齐与运行期人机交互

**D20 \[推论 P6\] herd 不读人，只读 charter 和 PI agent。** 人和 PI agent 的讨论是全系统密度最高的信息（C5），所以 D21：讨论全文入库、可检索、可回头问；D22：首波 worker 直接从对齐后的上下文 fork \[假设 H10\]。

**charter 目录**

```
charter/
  status.md         draft | aligned | revising；不是 aligned 不启动
  problem.md        问题本身，协商过的表述
  why.md            你为什么关心，什么结果算有意思
  constraints.md    预算（eval 名额、天数；不设 token 预算，P10）、不许碰的东西；Budget meter 对照它
  progress.md       什么算进展，什么只是动静；形态 B 必须写明 topline 和代理边界
  taste.md          偏好：优雅 vs 暴力、保守 vs 激进、直接跳过的方向类型；什么算 gaming
  non_goals.md      明确不做的
  escalation.md     哪些决定必须回到人；默认动作和超时
  assumptions.md    PI agent 列出的理解和假设，人逐条确认或推翻
  disagreements.md  没达成一致、但同意先这样做的点
  sources.md        signal source 列表，逐个确认 trust 和 readable_by（必答题）
  egress.md         出网白名单：模型 API、herd 工具、上下文源、包镜像（D75）
  transcript.ptr    对齐对话全文的 checkpoint 指针
```

**对齐协议**

1. 人描述问题。PI agent 先写 teach-back：用自己的话复述，列出它默认了但人没说的假设。弱模型会说"明白了"，强模型会说"你没说但我默认了这三件事，对不对"，这是 PI agent 必须用最强模型的原因。
2. 人改。PI agent 提 3 到 5 个初始方向，每个带理由和对一天、一周后状态的预测；方向之间用 embedding 距离做一次机械的多样性检查（P7）。
3. 分歧显式写进 disagreements.md，不抹平。
4. 人签字，status → aligned。PI agent 生成 spec.md 和 approaches/，从自己的 checkpoint fork 首波 worker（每方向 2 到 3 个 \[默认\]，goal 各异），其余 worker 从队列领常规任务。
5. charter 任何文件改动 → status 自动变 revising → 受影响方向的 worker 收软注入（problem.md 或 constraints.md 变了则硬中断）。PI agent 重新对齐后改回 aligned。

**escalation.md \[约束 C5\]**

```yaml
must_escalate:
  - kill_approach_with_budget_share_over: 0.3
  - total_spend_over_fraction: 0.8
  - any_change_to: [problem.md, constraints.md]
  - critic_refutes_current_best: true
  - any_source_with: escalate_on_request
default_when_unanswered:
  timeout_hours: 8
  action: freeze_affected_approach   # 不是 kill，不是继续
```

人不在线时 Planner 不能自己拍板，也不能无限等。默认动作是冻结受影响方向、名额暂时转给其他方向，人回来再决定。

**运行期控制台：四种操作**

| 操作 | 人说的话 | PI agent 做的事 | 落到仓库 |
| --- | --- | --- | --- |
| 问 | "方向 3 卡在哪" | 读 digest，必要时 ask\_checkpoint 问相关 worker，用原文核实后回答 | 不写 |
| 注入洞察 | "我觉得 X 和 Y 有关" | 写 author=human 的 claim，evidence 指向对话 checkpoint，标注方向 | claims + events；规则 5 硬中断 |
| 改航向 | "别做方向 2 了" | 改 charter，status → revising，重新对齐后改回 | charter + events |
| 回头问 | "worker 17 昨天看到了什么" | ask\_checkpoint(worker-17, ts, question) | 不写 |

升级请求的呈现格式固定：要决定什么、两到三个选项、Planner 的倾向和理由、不决定的默认动作和超时。汇报默认每个顶层 eval 批次后一次，三行：最优分变化、新 corroborated 的 insight、预算调整。PI agent 不替人做 escalation 里的决定，不替人改 charter。

**跨问题持久的是 PI agent，不是 herd。** 每个问题一份 charter、一个 herd，用完即散。PI agent 积累的是 taste.md 的历史和讨论记录，下一个问题的对齐从上一个的 taste 开始。

## 11. 实验设计

第 3 节的每个假设对应一个实验。系统只有在同样的 eval 预算下稳定赢过 baseline 才有资格谈规模，所以 baseline 先跑。

**臂**

| 名称 | 配置 | 检验的假设 |
| --- | --- | --- |
| B0 单强体 | 一个最强模型的 agent，全量检索，自己的笔记文件，长时间跑，同样 eval 名额 | 问题是不是根本不该拆（P1 的反面） |
| B1 best-of-N | N 个 worker，各自孤立，不共享任何东西，同样 eval 名额 | 并行本身的价值 |
| S1 只共享分数 | 完整系统，但 worker、Librarian、Planner 都只能看到候选和分数 | 中间臂：定位分数共享和 insight 共享的差别 |
| S2 完整系统 | 本文设计，Planner 关闭 | H1 |
| S2+P | S2 加 Planner | Planner 的增量 |

预期 S2 > S1 > B1 > B0 在 eval 稀缺的问题上成立；B0 > B1 在"只有一个 click 且材料装得下一个上下文"的问题上成立。S1 和 S2 没有差距 → insight 共享没起作用，回去查 Librarian 和 diff 排序，不是加 worker。

**D42 \[v1 审稿\] 对照的四条规则，少一条不可判**

1. 所有臂用同一个分配策略：对照阶段 Planner 关闭，全部 FIFO 加固定配额；Planner 单独做 S2 vs S2+P。否则通信价值和分配策略混在一起。
2. 计量单位是 eval 机器小时，所有 source 全算。S2 多用的便宜 eval 不能白用。
3. 先小后大，多次独立运行：5 到 10 个 worker，每臂至少 3 次独立运行（下限；实际次数按 D72 的试点和功效算）。本文其他地方写的 seed 都指一次独立运行：LLM API 不可复现，差异来自运行本身。单次运行的"稳定超过"不可测。
4. 预注册分两步：先按 D72 的试点（3 个批次）测臂间和运行间的方差，再据此定效应量和运行次数（例如"S2 比 B1+M 在 holdout\_verdict 上高 1.5 分且配对 t 检验显著"），封存后开跑，不事后挑指标。阈值在测方差之前定是猜。具体协议见 D72。

**D62 判定分数不是 best-so-far。** 噪声 eval 下，"目前最优分"只要多跑几次就会涨：多个噪声样本的最大值随样本数上升，和臂的真实质量无关。判定协议改为：每臂在固定 eval 预算用完时，交付物是该臂 Integrator 最后一版非 guard\_pending 的基线（D74：合并器各臂相同、运行中都跑，不由 LLM 选）；交付物在 holdout\_verdict 上重评 n 次 \[默认 5\] 取均值，这就是臂的分数。交付物通常是组合不是单点（D64、D65）。best\_score 曲线只用于诊断。附带的好处是它同时考了臂的选择能力，这正是 eval 稀缺下要的能力。

**D72 \[约束 C3；来自 v3 审稿\] 判定协议：三份数据，先试点再定运行次数，配对 t 检验。** 这条把 D42 第 4 条和 D66 写成可执行的步骤，替掉原来"高 1.5 分且 3 次全成立"那种在测噪声之前就定下的杠杠。

- **数据分三份**，第一天切好：日常用的；运行中抽查用的（holdout\_run）；最终判定用的（holdout\_verdict）。判定那份整个实验只碰一次：所有运行结束后一起评，评完之前谁也看不到数字。
- **运行中的抽查规则各臂相同**：每 2 次合入抽查一次，总数不超过 D66 的上限 10，其中留 3 次给二分和最终抽查。抽的是一段：上次抽查过的 baseline@j 到当前的 baseline@k。对这一段同时做两次新的配对测量，一次在日常数据上，一次在抽查数据上，两者之差超过 2.5·√(σ\_日常² + σ\_抽查²) 才标记（σ 是各数据集 A/A 复测得到的单次配对噪声；两份数据一样大时就是 2.5σ√2），然后二分找到肇事的那次合入并回退，它的 claim 置为 quarantined，只有人能解除。不能拿合入时测得的增益去比：能过门槛的测量本来就偏高，这个偏差随合入次数线性累积，而原来的 2σ√k 阈值只按 √k 涨，模拟里没有任何过拟合时每次运行也有 58% 到 99% 会误报；两次新测量的版本误报约每次运行 4%。日常数据那次新测量走抽查的独立容量，不占每小时限速，也不计入 holdout\_run 的 10 次上限。
- **试点**：正式运行前，B1+M 和 S2 各跑 3 个批次（同一批次各臂并行；B0 便宜，可以一起带上），交付物在抽查数据上各评 5 次，得到配对噪声 σ\_paired 和运行间噪声 σ\_run；每个源再做 A/A 复测。这 5 次计入该运行自己的 10 次抽查上限，所以试点运行里的运行中抽查最多 5 次。试点不进判定。试点要等 S2 的组件全部建完才能跑。
- **预注册**：试点后、正式运行前写死：最小有意义效应（人定）、运行次数、配额和延迟、判定协议、接收规则（默认版还是严格版，运行中不再切）、排除规则、结论表。运行次数 = 让功效 ≥ 80% 的最小 n，σ\_run 取试点估计的 1.5 倍（3 个批次只有 2 个自由度，1.5 倍大约是 64% 上界，两臂合并估计约 78%，所以试点跑 3 个批次而不是 2）；若 S2 要同时赢两个对照组，按联合功效算，各自 90% 才有约 83% 联合。只跑 3 次是不够的：效应要到运行间噪声的 2.3 倍才有 80% 功效，效应是 1.5 倍时只有一半机会测出来；所以本文其他地方写的"3 次运行"都只是下限，实际次数按这里算。
- **检验**：单侧配对 t 检验，α = 0.05，按批次配对：同一批次里各臂同时跑，各自独立的 run\_id 命名空间，同一个冻结的构建，这样模型供应商的漂移在差值里抵消。同时报效应量和 90% 区间。预算不够 n 次，就标为探索性运行：报区间，不过 gate。
- **为什么换掉 3/3**：模拟显示它的假阳性随噪声漂移，效应恰好等于阈值时只有约 12% 通过；配对 t 检验在各噪声水平下都保持 5% 假阳性。模拟脚本在 herd-sim/，审稿见《Research Herd v3 审稿》（文件 Research-Herd-v3-Review.md）第 4 节。

**D58 臂的开关表。** 每个臂具体开什么、关什么，随预注册一起签字，否则"共享"的含义不唯一。

| 开关 | B0 | B1 | S1 | S2 |
| --- | --- | --- | --- | --- |
| repo\_read 范围 | 全部 | spec.md、approaches/、自己的前缀 | 前述 + candidates/ 和 result 事件 | 全部 |
| worker ctx 组成 | spec + 自己的笔记 | spec + 自己的 claims | spec + 自己的 claims + 全局候选分数表 | spec + digest + diff\_since |
| search | 自己的笔记 | 关 | 只索引 candidates | 全开 |
| diff\_since | 无 | 关 | 只含 result 事件 | 全开 |
| 路由规则 1（自己的 result\_ready） | 开 | 开 | 开 | 开 |
| 路由规则 2 到 5 | 无 | 关 | 关 | 开 |
| eval\_request 前的相似候选检索（needs\_confirmation） | 无 | 关 | 开 | 开 |
| Eval service 侧的 hash 去重 | 无 | 关：重复候选真跑真计费，否则 B1 白得共享 | 开 | 开 |
| D26 提交前检索 dead\_end | 无 | 关 | 关 | 开 |
| D19 连续失败强制刷新 | 无 | 关 | 关 | 开 |
| Librarian | 无 | 关 | 关 | 开 |
| Critic / holdout | holdout 自动复核 | 同 | 同 | 同 |
| gate（D54） | 开 | 开 | 开 | 开 |
| 分配 | 无 | 每方向 slot 写死在 spec.md | 同 | 同；D49 到 D51 关 |
| Planner | 无 | 关 | 关 | 关（S2+P 单独跑） |
| fork / ask\_checkpoint | 无 | 关 | 关 | 关（H2 单独实验） |
| Integrator（D74） | 开，同配置 | 开，同配置；worker 不可见 | 开，同配置 | 开，同配置 |
| baseline\_advanced 推给 worker、规则 6 | 单 agent，自己的基线自己看得见 | 关：worker 一直在 baseline@0 上干 | 开 | 开 |
| compose 任务的执行者 | 池外新 agent | 同 | 同 | 同；另外 worker 可领组合候选任务（D64） |
| 抽查（D72） | 同一规则 | 同 | 同 | 同 |
| 出网白名单（D75）；不靠 checkpoint 的重启（D77） | 开 | 开 | 开 | 开 |
| 预审（D78） | 推迟，H1 阶段各臂都不开 | 同 | 同 | 同 |
| map/（D76） | 初始地图，随自己的基线重跑 | 只有 baseline@0 上的初始地图 | 随 baseline\_advanced 更新；不能加脚本 | 全开；worker 可加脚本 |
| 最终交付物（D62、D65） | 该臂 Integrator 最后一版非 guard\_pending 的基线，在 holdout\_verdict 上重评 n 次取均值即臂的分数；单个最优候选的分另记为 B1-single 诊断列（D62、D65、D74） | 同 | 同 | 同 |

**假设与实验的对应**

| 假设 | 实验 | 指标 | 预注册标准（示例，开跑前定） |
| --- | --- | --- | --- |
| H1 共享胜过孤立 | S2（Planner 关）vs B1 @ 5，运行次数按 D72 试点算，S1 作中间臂 | 最终交付物的 holdout 分（D62、D65） | S2 赢 B1+M：试点后预注册，人定最小有意义效应；单侧配对 t 检验，α = 0.05；运行次数按功效 ≥ 80% 从试点噪声算，噪声乘 1.5 留余量；同时报效应量和 90% 区间（D72） |
| H2 fork 保真度 | handoff 实验：一个 worker 跑到 surprise claim，分三组各 5 个 agent 续跑一小时——摘要组、轨迹组、fork 组，用便宜 eval 评候选 | 三组候选分数分布；与原 worker 意图的一致性（盲评） | fork 组均值高于摘要组 2 倍以上 |
| H3 轮询够用 | 记录每次硬中断的实际时延；做废功事后抽样 | 中断时延分布；做废功比例 | 到 5 分钟兜底的中断占比 ≤ 10%；中断后的做废功（事件合入到 agent 确认之间的 token 和工具调用）单独报。原来的"中位 < 5 分钟"被 5 分钟兜底保证了，测不出东西 |
| H4 单仓库 | 持续运行一周 | merge queue 积压、CI 排队 | 积压中位 < 2 分钟 |
| H5 代理可标定 | 相关性记录 | 本地代理和 T1 对顶层的相关性 | 至少一层 ≥ 0.5 |
| H7 单 Planner | 方向数 5、10、20 | Planner 单次运行时长、decisions 质量（人评） | 时长 < 名额窗口的 1/4 |
| H8 预测加权 | 预测误差与后续成功率 | 相关性 | ≥ 0.3，否则取消加权 |
| H9 Scout | 第 5 周起，Scout 开关对照 | cross\_approach insight 的复用率 | ≥ 1 条被 ≥ 2 次引用/周 |
| H10 首波 fork | 首波一半 fork、一半读 spec | 一天后候选多样性和便宜 eval 分数 | fork 组不差且多样性不低 |
| H11 跨机 fork | 第 0 周 spike | 迁移时间、快照大小 | 迁移 < 2 分钟，快照 < 1 GB |

**四个常设指标**

| 指标 | 定义 | 采集 |
| --- | --- | --- |
| 固定预算下所选候选的 holdout 分 | 判定用。每臂预算用完时自己选一个候选，holdout 重评 n 次取均值；best\_score 对累计 eval 机器小时的曲线只做诊断 | Eval service |
| dead\_end 重走次数 | 候选与 confirmed 的 dead\_end 相似度超阈值、仍被提交到 T1 以上的次数 | Indexer 自动，不依赖被测系统内的标注 |
| insight 复用次数 | type=insight 被 depends\_on 或被顶层 verified 候选引用的次数分布 | Indexer 的 reuse\_count |
| 预测误差 | 按 agent、按方向的 predicted 与 actual 的差 | Eval service |

辅助：候选 diff 的 embedding 多样性（趋同告警）、各层相关性、Critic 的 refuted 率、做废功比例（事后抽样标注，只用于诊断不用于判定）。所有臂记录完整轨迹，事后能回放。

## 12. 资源与规模

**机器分配 \[约束 C7\]，100 台**

| 用途 | 台数 | 说明 |
| --- | --- | --- |
| Worker + sandbox | 80 | 每台 3 到 4 个 worker 和沙箱；本地代理 eval 跑在这里。eval 是外部系统时这是集群的主要用途 |
| 本地便宜 eval 层 | 12 | 只在问题有可本地跑的子集或小实例时启用；否则并入 worker 池 |
| 仓库、Indexer、Eval service（只是外部队列的客户端）、Budget meter、对象存储网关 | 4 | git 和对象存储各一个热备副本，仓库每小时 bundle 到对象存储 |
| 备用 | 4 | 副本切换和 surge 时的临时 sandbox |

这个表是示意，具体数字由 sources.md 的声明算出来。eval 自己跑在集群上的问题（形态 A 的 harness 自优化之类）把 worker 池按 eval 的占用让出去。

**模型 \[P10\]**：所有角色用同一个最强模型，不分级。首波 worker 从 PI fork，本来就是同一个模型（D22）。v2.2 以前这里有一张分级表（D47），第 29 轮撤销。

**量级估算**

每个 worker 持续运行约 2M 到 5M token/小时，50 个 worker 的瞬时速率可能到每秒几万 token。token 不设预算（P10），唯一要在启动前核对的是 API 的并发和速率上限：它决定能同时开多少个 agent，harness 里要有退避（C7）。

**规模上限 \[假设 H4、H7\]**

| 规模 | 瓶颈 | 处理 |
| --- | --- | --- |
| 50 | 无 | 本文设计 |
| 200 到 500 | CI 时长；Router 检查的 O(事件 × lease) | CI 分级已做；Router 检查按 approach 分片 |
| 1,000+ | 单仓库 clone 和 merge 吞吐；Librarian 数量 | 按方向分仓；source of truth 换数据库，git 退化为审计日志 |

**单任务时长 \[默认 20 分钟到 2 小时\]**：短于 10 分钟的任务硬中断没有意义，只在任务边界读即可；长于 4 小时的要强制 checkpoint 分段。这个默认值也是 H3 成立的前提。

## 13. 失败模式

每条都有一个能被观测到的信号，否则对策没法触发；最后一列说压力落在哪个决定上，PoC 里出现这个信号就去登记表查那个决定的 fallback。

| 失败模式 | 信号 | 对策 | 压力落在 |
| --- | --- | --- | --- |
| Reward hacking 通过共享传播 | 分数跳升；顶层与 holdout 差异；Critic refuted 率上升 | D10 隔离；holdout；跳升触发 Critic；refuted 子代连带；audit 理由不公开 | D10、D11；残余：Critic 被骗，人抽查 |
| 所有 worker 趋同 | 候选 diff 的 embedding 多样性下降 | exploration 份额上调；新方向强制 fork 自不同 checkpoint；lease 限同方向并发 | D24；残余：真的只有一条路 |
| 黑板变噪声 | diff 截断率高；search 复用率低；dead\_end 重走上升 | low\_signal 标签；digest 结构和上限；evidence 必填 | D6、D14 |
| 垃圾 claim | question 占比高；reuse\_count 长期 0 | 允许"本次无发现"的 dead\_end；reuse\_count 进队列权重 | D4 结束前必写 |
| Eval 结果无人消化 | result\_ready 后 24 小时无后续候选 | D28 | D28；残余：checkpoint 过旧 |
| 假 refuted 级联 | reinstated 次数；级联后被撤销的任务 | 单来源不级联；二次确认；reinstated | D34 |
| Planner 单点 | 运行失败或超时；decisions 停更 | 上一份 decisions 续用；worker 自领任务 | H7 |
| Librarian 偏见 | digest 覆盖率低 | worker 可绕过；Critic 抽查一致性 | D14 |
| 硬中断打断正在 click 的 worker | 中断后重进状态做废功 | 只在 tool call 边界；中断前 checkpoint；附"可 fork 继续" | D17 |
| 人成为瓶颈 | escalation 积压；默认动作触发次数 | 默认动作和超时；批量呈现 | C5；残余：默认动作选错 |
| Rebase 风暴 | merge queue 积压 | append-only；CI 分级；批量 rebase | D31、H4 |
| git 或对象存储单点 | 服务不可达 | 热备副本；每小时 bundle | D30 |
| evidence 指针悬空 | CI 解析失败 | 只归档不删；合入时解析 | D33 |
| 长 sandbox 期间 lease 误判 | 同一任务两个 lease | 后台心跳；回收前查进程 | D32 |
| API 限流 | 429 比例 | 退避；按角色配额 | C7 |
| 预算无人执行 | 累计超 constraints | Budget meter | v1 审稿 |
| claim 正文做 prompt injection | Librarian 或 Planner 读到异常指令 | 进 digest 前只保留结构化字段；Planner 只读结构化部分 | D13、D15 |
| 预测加权奖励保守候选 | 高准确度 agent 的候选方差异常低 | 加权只作用于 information 份额 | D37、H8 |
| 便宜 eval 层与顶层不相关 | 标定相关性低 | 自动降级；退到 T0 加顶层 | H5 |
| Fork 继承错误上下文 | 同一 checkpoint 的 worker 集体走偏 | 首波 fork 数量限制；goal 必须不同；多样性检查 | D22、H10 |
| 形态 B 的代理被 game | 代理涨、topline 不涨 | D12 只有 topline 动预算；Critic 查转移；proxy\_only 标记 | D12 |
| 组合冲突或负交互 | 组合候选的分低于各部分的最大值 | 组合候选也过 gate 和 eval；Critic 写 interaction claim；冲突的两方向不再自动组合 | D64 |
| 臂之间泄漏 | B1 的 worker 轨迹里出现其他 worker 的候选 id | 每运行、每 B1 worker 独立仓库和命名空间；worker 无 push 凭证 | D67、D68 |

## 14. 最小落地版与建造顺序

前置：测试台（另一份文档《Herd 测试台》）先建 3 天，和 herd 的第 0 周 spike 并行；herd 第 1 周用的 eval 就是测试台的慢档服务。第一轮 PoC 的目的只有一个：把 H1 变成已验证或已推翻。为此分两栏：门槛前是验证 H1 必需的，门槛后第 4 周起接入；都不影响 H1 的推迟。砍掉的在登记表里标"推迟"，不是删除。

**最小落地版包含**

**门槛前（验证 H1 必需，第 1 到 3 周）**

- 六个端口的接口、内存适配器、本地 Executor、MCP 工具服务（D59、D60）；charter 里门槛前就要用的四个文件由人手写：params.yaml、constraints.md、progress.md、status.md（对齐流程本身在门槛后）；仓库约定、状态机、append-only、CI 同步检查、事件 schema（D30、D31、D9、D34、D56）
- 任务队列和 lease 在 KV（D55、D32）
- 一个 worker 的完整循环和全部 hook，含立即 push 和结束前必写（D1、D35、D36、D4）
- Indexer：SQLite、embedding、diff\_since、search、result\_ready 后续任务（D13、D18、D28）
- tool call 边界的 Router 检查（D29）
- Eval service：source 声明、阶梯推导、去重、预测记录、配额加 FIFO、gate（D39、D37、D40、D54）
- 一个 Librarian（D14）
- holdout 自动复核；Critic 的 LLM 审计放门槛后
- Budget meter 和简化的停止规则：预算用完、达标（D43、D52 的两条）
- spec.md 和首批 approaches 由人手写；每方向 slot 写死
- 臂开关表和预注册（D42、D58）
- Integrator：先进先出、rebase、compose 任务、配对复测、接收规则、guard 批次、收尾顺序（D74）；四个臂同配置，测试台的两个对照臂也用它
- 配对测量：result\_ready 带 baseline 和 paired，候选哈希含基线标识（D73、D69）
- 抽查和试点：两次新测量的抽查规则、试点 3 个批次、A/A 复测（D72）
- 出网白名单（D75）；不靠 checkpoint 的重启路径（D77，gate 前就能用）
- 机会地图：人手写第一批测量脚本，Integrator 在 baseline\_advanced 后重跑（D76）

**门槛后（第 4 周起接入）**

- checkpoint、fork、ask\_checkpoint，第 0 周 spike 的产出（D5）
- PI agent、charter 目录、escalation、人的注意力队列（D20 到 D23、D53）
- Critic 的 LLM 审计（D11）
- Planner 只写带理由的覆盖（D15）
- 确定性分配规则、surge、广度底线（D49 到 D51）
- 平台期和全部冻结的停止规则（D52 其余两条）

**推迟到 H1 验证之后，每项单独 A/B**

- Scout（D16、H9）
- 每方向一个 Librarian
- NATS 推送（H3 失败时才做）
- 代理打分器和新颖度排序（D41）
- ASHA early stopping
- 扩到 50 以上

&#91;embedded content: 建造顺序 · 第 0 周 spike 并行，四周主线，每周一个门槛\]

读法：菱形是门槛。第 3 周的门槛是 H1 的第一次真正检验，过不了就回去改通信层，不加 worker。

0. 第 0 周（与第 1、2 周并行，两周）：spike H11。checkpoint 带 sandbox 状态，能在另一台机器恢复并继续。产出是一个最小迁移实现加迁移时间和快照大小的数字。不先做掉它，D5 和 D22 都是愿望。
1. 第 1 周：六个端口的接口代码、内存适配器、本地子进程 Executor、MCP 工具服务；仓库目录和 schema；状态机和事件权限；CI 同步检查；Indexer 的 SQLite；一个 worker 的完整循环；Eval service 的 T0 和顶层层，带去重、预测记录、result\_ready；两份 holdout 切好封存（D66）；Budget meter。跑 L0 全绿，再跑 B0。
2. 第 2 周：lease 在 KV；merge queue 和分级 CI；便宜 eval 层和相关性标定；本地代理记录；embedding 和 search；配额加 FIFO；每运行独立的 run\_id 命名空间和 B1 的每 worker 独立仓库（D67）；worker 只读 clone（D68）。跑测试台问题 A 到记分卡全绿；加 Integrator 和配对测量（D73、D74），测试台的两个对照臂改用它。
3. 第 3 周：Router 检查；diff\_since 和游标；全部 hook；一个 Librarian；Critic 和 holdout。跑 S2（Planner 关）vs B1 @ 5，S1 作中间臂：组件齐了先按 D72 试点 3 个批次，再预注册、再跑正式 H1，运行次数按功效算。周数是示意，按门槛走，不够就顺延。
4. 第 4 周：k8s Executor 适配器（第二个适配器，顺便验 H12）；接入 checkpoint、fork、ask\_checkpoint，跑 handoff 实验判定 H2；PI agent 和 charter；Planner；扩到 20。跑 S2 @ 20 和 S2 vs S2+P。
5. 第 5 周起：推迟清单逐项 A/B；扩到 50；让系统在 20 个 worker 上连续跑一周看它怎么坏，再加功能。

## 14.1 验收阶梯：接真问题之前怎么知道 backbone 对了

**D70 \[第 23 轮\] 三级验收，每级回答一个问题，花的时间差一个量级；上一级不全绿不进下一级。** 单元测试和 H1 之间缺一层：直接 hook 到大问题上跑两天才发现 backbone 有问题，代价太高，而且分不清是系统坏了还是假设错了。

| 级 | 问的问题 | 时间 | 配置 | 失败说明 |
| --- | --- | --- | --- | --- |
| L0 插头 | 管道和不变量对不对 | 秒级 | 一个进程，内存适配器，脚本化 worker（不用 LLM） | 代码错 |
| L1 测试台问题 A、B | 机制在真 LLM worker 手里是否按设计起作用 | A 两天一轮，B 两天一轮 | N 个 LLM worker，本地 Executor，慢档人造稀缺，三个臂同配额 | backbone 坏了，或问题里拼的分量不够；不是假设错了 |
| L2 真问题 | H1 成不成立 | 天级 | 第 11 节 | 假设错了，或问题不适合 |

**L0：插头测试的 20 条断言**（每条一个测试，脚本化 worker 按固定策略动，全部在内存适配器上几秒跑完）

1. claim\_task 的选取顺序：for\_agent 优先，再 priority，再 target\_slots 差最大的方向（D55）。
2. 写一条 claim → 合入产生 seq → Indexer 从零回放两次，状态完全一致（D56）。
3. eval\_request 两阶段：有相似候选 → needs\_confirmation；带 token 再调 → queued 且 result claim 已生成（D57）。
4. 同基线、同 payload 第二次提交 → dedup\_hit，配额不减；只改基线标识，哈希就变（D69、D73）。
5. result\_ready → 后续任务 for\_agent 是提交者；提交者有 lease 时 interrupt:\<agent> 被写（D28）。
6. 单来源 refuted 不级联；Critic 来源的级联到 depends\_on，reinstated 撤销（D34）。
7. gate 失败 → 不进分数，写 dead\_end reason=gate:\<name>（D54）。
8. lease TTL 过期 → 任务回队列一次且仅一次（D32）。
9. Budget meter 累计到上限 → stop 事件（D52）。
10. 臂开关表：B1 配置下 search 和 diff\_since 返回空，路由规则 2 到 5 不触发；S2 全开（D58）。
11. Integrator：有文本冲突 → 发 compose 任务；无冲突但基线动过 → 恰好一次配对复测；增益不过 1 个标准误 → integration\_failed 并给作者后续任务；B1 配置下 worker 收不到 baseline\_advanced（D74、D58）。
12. worker 角色读 private:// → 拒绝；worker 对仓库 push → 拒绝（D63、D68；内存适配器也要模拟权限）。
13. result\_ready 带 baseline、配对增益、标准误、n\_items、n\_reps；n\_reps 不计缓存命中（D73）。
14. baseline\_advanced 的 files\_changed 和 lease 的 files\_touched 有交集 → 硬中断；没有交集 → 软中断（第 8 节规则 6）。
15. 一个 guard 批次失败 → 二分，写一版排除肇事者的新基线，旧记录不变；交付物永远不是 guard\_pending（D74）。
16. 抽查：脚本化的过拟合候选被标记、回退、置 quarantined；一次干净的脚本化运行，7 次抽查一次都不报警（D72）。
17. worker 沙箱访问白名单外的主机 → 拒绝（D75）。
18. 上下文用量超过 70% → 写 claim 并带起始包开新会话；进程被 kill -9 → sidecar 代写 stub claim（D77）。
19. 臂的公平性：Integrator 配置在四个臂里逐项相同；compose 任务的执行者不在任何臂的 worker 池里（D58）。
20. 收尾顺序：预算用完 → 停止接收 → guard\_pending 补检 → 最终抽查 → 冻结交付物；交付物非 guard\_pending（D74、D79）。

**L1：测试台的两个问题（A 仿真器调度策略，B 埋坑 harness）**

问题本身、埋的结构、记分卡、对照组、建造顺序都在另一份文档《Herd 测试台》里，这里只说它在阶梯里的位置。它的价值是作为 backbone 的正对照：问题是为共享设计的，所以 herd 必须赢"单干"和"各干各的"两个对照组；不赢就是系统坏了，或者问题里"拼"的分量不够。它不能证明 H1。之前的合成问题 Knobs 废掉了：它的难点是材料超过一个上下文窗口，而一个会用 grep 和抽样的 agent 对付得了（第 26 轮）。站得住的理由是"要迭代的方向数 × 每轮要等的时间超过墙钟时间"，A 和 B 都是这个结构。

**L1 要回答的问题** = 测试台文档第 6 节的记分卡：广度、拼装、吃到几成、死胡同重撞、冲突处理、配额纪律、预测误差、倾斜、泛化，全部从记录器自动算，不靠人读。原来的 10 个问题并入其中。

问题 A 过了再上 B，B 过了才进 L2。测试台按 v3 审稿接受三条改动：数据分四份（公开、日常、抽查、判定）；对照组按合并后的交付物判（B1+M，单个最好的另作诊断）；每个调用者只看自己的提交历史。v3 要求的"再埋两个杠杆"不要，那是为它的静态分工对照组服务的。L2 的问题由人选（附录 C），但必须保留人造稀缺：即使真 eval 分钟级，Eval service 也把 latency 垫到 ≥ 10 分钟、budget 压到全队每小时 2 次，否则测的不是这个设计存在的理由（C1）。

## 15. 决策登记表

所有决定按编号列齐。实现时这是检查清单；PoC 出问题时，先从第 13 节的信号找到压力落在哪个 D，再来这里看它的状态：约束不动，推论先质疑原则，假设直接切 fallback，默认值直接调。"推迟"的行不在最小落地版里。

| D# | 决定 | 状态与依据 | 推翻它需要看到什么 | Fallback | 节 |
| --- | --- | --- | --- | --- | --- |
| D1 | 任务形态唯一：用方法 Y 推进整个问题，每个 worker 看全部材料 | 推论 P1 | 按材料切分的对照组不差 | 允许 dispatcher 按材料切（等于放弃 P1） | 7 |
| D2 | 不预先分解；子问题只能由看过全局的 agent 在工作中提出，且能脱离父上下文陈述 | 推论 P1 | 同上 | 同上 | 7 |
| D3 | 任何合并由一个持有原文检索权的 agent 做，不拼摘要 | 推论 P1 | 拼摘要的对照组不差 | 允许机械合并 | 8 |
| D4 | 轨迹全量保留；worker 结束前必写 ≥ 1 条带 evidence 的 claim | 推论 P2 | 垃圾 claim 比例高且无法用标签压住 | 改为结束前必写 contribution 摘要，轨迹仍保留 | 7 |
| D5 | fork 代替 handoff，只用于决策点分叉，不用于上下文解压；checkpoint 含 sandbox 状态和上下文用量 | 推论 P2；假设 H2、H11 | H2：摘要组 ≈ fork 组；H11：迁移太慢或快照太大 | 轨迹指针 + 回头问；或对话 + 命令日志重放 | 7 |
| D6 | 摘要只做索引，不替代原文 | 推论 P2 | 无 | 无 | 8 |
| D7 | 检索由接收方在需要时拉，不靠推送摘要 | 推论 P2 | 无 | 无 | 8 |
| D8 | surprise 单独记录，鼓励产出可执行复现 | 推论 P2 | surprise claim 从未被复用 | 取消工具，保留 evidence 要求 | 7 |
| D9 | 每类 claim 有自己的状态机 | 推论 P3；v1 审稿 | 无 | 无 | 5 |
| D10 | eval 定义、holdout、audit 理由在 worker 无凭证的私有存储；黑板只放公开元数据（git 没有按路径读权限） | 推论 P3；约束 C3 | 无 | 无 | 9 |
| D11 | Critic 审计高分候选；audit 分公开结论和不公开理由；限速 | 推论 P3 | Critic refuted 率为零且无 hacking | 降低触发频率，不取消 | 6 |
| D12 | 只有 trust 最高的信号能改永久预算；便宜信号只能触发有上限、有期限的 surge（D50） | 推论 P3（反 Goodhart） | topline 极稀疏导致预算长期僵化 | 允许 calibrated 代理按折扣动预算，Critic 加密检查转移 | 9 |
| D13 | 第 0 层纯代码：索引、路由、合并、eval 服务、计量 | 推论 P4 | 无 | 无 | 6 |
| D14 | Librarian 是编辑不是守门人，worker 可绕过 | 推论 P4 | digest 覆盖率 < 80% | 查 digest 结构，或分方向加 Librarian | 6 |
| D15 | Planner 只写 decisions，不做全景、不 fork、不调通信参数 | 推论 P4；假设 H7 | Planner 运行时长超名额窗口 1/4 | 按方向分片 | 6 |
| D16 | Scout 独立角色做跨方向连接 | 推论 P4、P8；假设 H9；推迟 | 复用率为零 | 取消，靠人 | 6 |
| D17 | 三通道：硬中断、软注入、按需检索 | 推论 P5 | 固定频率对照组不差 | 固定频率 | 8 |
| D18 | 游标 diff，按相关性排序，截断 500 token | 推论 C2；默认值 500 | diff 截断率高且做废功高 | 调截断；改排序 | 8 |
| D19 | 连续 N 次失败后同步点加密，N=3 | 推论 P5；默认值 | 无 | 调 N | 8 |
| D20 | charter 协商与签字，aligned 才启动 | 推论 P6 | 无 | 无 | 10 |
| D21 | 对齐讨论全文入库可检索可回头问 | 推论 P6 | 无 | 无 | 10 |
| D22 | 首波 worker 从 PI agent 上下文 fork | 推论 P6；假设 H10 | fork 组不优于读 spec 组 | 全部读 spec | 10 |
| D23 | 人的洞察写成 author=human 的 claim，规则 5 硬中断 | 推论 P6 | 无 | 无 | 8 |
| D24 | approach portfolio；lease 限同方向并发；预算按方向 | 推论 P7 | 多样性从不下降 | 放松并发限制 | 6 |
| D25 | dead\_end 一等公民，有 confirmed 状态 | 推论 P7 | 无 | 无 | 5 |
| D26 | eval\_request 前强制检索相似候选和 dead\_end | 推论 P7 | 重走次数为零 | 改为提示不强制 | 7 |
| D27 | 每层一个 agent 持有该层全貌，层级按抽象分 | 推论 P8 | 无 | 无 | 8 |
| D28 | result\_ready 绑定提交者 checkpoint，自动生成后续任务 | 推论 P9；v1 审稿 | 无 | 无 | 9 |
| D29 | Router 检查用 harness 的 tool hook（有则每次 tool call，无则只在 herd 工具调用），Executor 5 分钟兜底 SIGTERM；不做 NATS | 假设 H3 | 到 5 分钟兜底的中断超过 10%，或中断后做废功高 | 加 NATS 推送 | 6 |
| D30 | 单 git 仓库是唯一 source of truth | 推论 P2、P4；假设 H4 | merge queue 积压中位 > 2 分钟 | 按方向分仓；或换数据库 | 5 |
| D31 | append-only；只写自己前缀；按作者分目录 | 推论 C4、P2 | 无 | 无 | 5 |
| D32 | lease 在 KV，TTL 2 小时；心跳由 Executor sidecar 按时间打，不依赖 tool call | 约束（心跳频率）；默认值 | 无 | 调 TTL | 5 |
| D33 | 轨迹和 checkpoint 在对象存储，只归档不删 | 推论 P2 | 存储成本不可接受 | 删 30 天前未被引用的 | 5 |
| D34 | 事件优先级；单来源 refuted 不级联；reinstated；merge 序号排序 | 推论 P3；v1 审稿 | 无 | 无 | 5 |
| D35 | worker 无状态；原则靠 hook 不靠 prompt | 约束 C4 | 无 | 无 | 7 |
| D36 | 每条 claim 产生时立即 push | 推论 P5；v1 审稿 | commit 率压垮 CI | 批量 push，上限 5 分钟 | 7 |
| D37 | 提交必带预测；预测准确度只加权 information 份额 | 假设 H8 | 误差与成功率相关性 < 0.3 | 只记录不加权 | 9 |
| D38 | agent 之间不互发消息，一切通过仓库 | 推论 P1、P4 | 无 | 无 | 8 |
| D39 | eval 是 signal source 插件接口 | 约束 C6 | 无 | 无 | 9 |
| D40 | 配额加 FIFO；本地代理是一等机制 | 约束 C1；假设 H5 | 无任何一层相关性 ≥ 0.5 | 退到 T0 加顶层，减 worker 数 | 9 |
| D41 | 代理打分器和新颖度排序 | 推迟 | 标定数据够了之后 A/B | 不上 | 9 |
| D42 | 对照四条规则：同分配策略、机器小时计量、多 seed、预注册 | v1 审稿 | 无 | 无 | 11 |
| D43 | Budget meter 按角色、方向、source 计量 | v1 审稿 | 无 | 无 | 6 |
| D44 | ASHA early stopping | 推迟 | eval 是长 job 时 A/B | 不上 | 9 |
| D45 | charter 的 sources.md 是必答题 | 推论 C6 | 无 | 无 | 10 |
| D46 | escalation 默认动作是冻结，超时 8 小时 | 约束 C5；默认值 | 冻结造成的损失大于继续 | 改默认动作 | 10 |
| D47 | 撤销（第 29 轮，P10）：原为模型分级，现在所有角色同一个最强模型 | 第 29 轮 | — | — | 12 |
| D48 | 跳升阈值 2 个标准差；Critic 限速 6 次/小时；Scout 每 6 小时；首波每方向 2 到 3 个 | 默认值 | 无 | 调 | 各节 |
| D49 | 分配单位是 slot；加 agent = 提 target\_slots 和队列权重，不杀 worker | 推论 C1、C4 | 换方向的延迟（任务时长）导致 surge 窗口错过 | surge 时对低优先方向的 worker 软注入"完成后换方向"，必要时 checkpoint 后硬中断 | 9.1 |
| D50 | surge：便宜信号触发临时加码（翻倍、≤40%、到顶层结果或 24 小时），顶层确认才固化；surge worker 从发现者 checkpoint fork 并带不同 sub-goal | 推论 P3、P7；默认值 k=3、40%、24h、48h 冷却 | surge 被顶层确认的比例很低（< 30%）说明便宜信号太差；或 surge 从未触发 | 提高 k 或只允许 calibrated 代理触发；或取消 surge，只按顶层分配 | 9.1 |
| D51 | 广度硬底线：exploration ≥ 25%、单方向 ≤ 40%、每方向 ≥ 1 slot、新方向自动批到 8 个；第一版分配规则是 softmax 确定性规则，只吃顶层信号（与 D12 一致），LLM 只能带理由覆盖 | 推论 P7；默认值 | 多样性指标在底线下仍持续下降；或 Planner 覆盖率 > 50% 说明规则太差 | 调底线和 τ；或把分配交给 LLM Planner（推迟项提前） | 9.1 |
| D52 | 停止规则：预算、平台期、全部冻结、达标；换 charter 的两个信号 | 推论 C1、C5；默认值 N=3 | 平台期误报（停了之后人工发现还有进展） | 加大 N；平台期只报不停 | 9.2 |
| D53 | 人的注意力按预算和优先队列分配；看过什么记成事件喂 taste | 约束 C5；默认值 30 分钟/天 | 人总是跳过队列直接翻仓库，说明队列排序不对 | 按人实际点开的类型重排 | 9.2 |
| D54 | 可自动检查的硬约束编译成 gate，提交前和结果后执行，不进分数，Planner 不能权衡 | 推论 P3；约束 C3 | 无 | 无 | 9.2 |
| D55 | 任务队列在 KV，task schema 固定；Indexer 维持常规任务和 result\_ready 后续任务；claim\_task 的选取顺序 | 推论 C4；v2 审稿 | 无 | 无 | 5 |
| D56 | 事件 schema 固定；seq 由合入 main 的串行进程分配；Indexer 按 seq 回放 | 推论 P3；v2 审稿 | 无 | 无 | 5 |
| D57 | eval\_request 带每层预测，服务自动逐级执行并返回状态；分配状态只在 allocation/；参数在 charter/params.yaml | 推论 C1、P3；v2 审稿 | 无 | 无 | 5、9、9.1 |
| D58 | 臂的开关表随预注册签字；H1 的门槛是 S2（Planner 关）对 B1，S1 作中间臂 | v2 审稿 | 无 | 无 | 11 |
| D59 | herd 是独立 runtime，四层，L2 只依赖六个端口；集群仓库和 harness 都是 L0 适配器 | 推论；v2 讨论 | 无 | 无 | 4.1 |
| D60 | hook 分三层：herd 工具的 hook 在 MCP 服务端；中断检查用 harness 的 tool hook（可选能力 supports\_tool\_hooks）；心跳和兜底在 Executor sidecar | 推论 P4 | 某个必需 hook 无法在服务端实现 | 该 hook 成为 Harness 端口的必需能力，缩小可接的 harness 集合 | 4.1 |
| D61 | 每端口一个真实适配器加一个内存适配器；第二个真实适配器放 H1 之后；内存版是回归测试 | 假设 H12 | 换适配器后结论变了 | 把泄漏的能力提成端口的显式字段 | 4.1 |
| D62 | 臂的分数 = 固定预算用完时Integrator 最后一版非 guard\_pending 基线的 holdout 重评均值，不是 best-so-far；seed 指独立运行 | 约束 C3；外部审稿 | 无 | 无 | 11 |
| D63 | 两个存储：黑板仓库全量可 clone；私有存储放 eval 定义、holdout、audit 理由，worker pod 和 PI agent 都无凭证（PI 的上下文会被 fork 继承） | 约束（git 无按路径读权限）；外部审稿 | 无 | 无 | 5、9 |
| D64 | 产出是组合不是单点：候选多父、组合任务模板、interaction claim；方向是 portfolio，exploration 底线更高 | 推论 P1、P7；第 22 轮 | 问题本身不可组合（单一证明、互斥方案） | charter 标 composable=false，合并流程退化为取单个最优 | 9.1 |
| D65 | best-of-N 只是对照臂名；所有臂的候选由同一个 Integrator 在运行中合并，B1+M = worker 看不见基线前进；B1-single 作诊断 | 第 22 轮 | 无 | 无 | 9.1、11 |
| D66 | 两份 holdout：holdout\_run 给运行中 Critic，有次数上限；holdout\_verdict 封存，只评最终交付物，n 固定 | 约束 C3；外部审稿 N1、N5 | 无 | 无 | 9 |
| D67 | 隔离靠物理分开：每运行一个 run\_id 的仓库、KV、配额；B1 每 worker 独立仓库；S1 用过滤镜像仓 | 约束（git/shell 不能限读）；外部审稿 N7 | 无 | 无 | 5 |
| D68 | worker clone 只读，push 凭证只在 MCP 工具服务 | 推论 D31；外部审稿 N8 | 无 | 无 | 5 |
| D69 | 候选格式：manifest.yaml + payload（patch / config / artifact），hash = 基线标识 + payload，相似度只算 payload（第 30 轮） | 外部审稿 C8 | 无 | 无 | 5 |
| D70 | 三级验收：L0 插头 12 条断言（秒）、L1 测试台问题 A、B（天）、L2 真问题（天）；上一级不全绿不进下一级；L2 保留人造稀缺 | 第 23 轮；外部审稿 A4 | L1 全绿但 L2 系统性失败，说明测试台问题漏了某个机制 | 给测试台问题加针对该机制的结构，回到 L1 | 14.1 |
| D71 | PoC 前先在测试台的两个问题上验证 herd 行为（A 仿真器调度策略、B 埋坑 harness），三个臂同配额同时间；herd 的 Eval service 是测试台慢档服务的薄客户端，接口与真外部 eval 一致，换真项目不改 herd | 第 25、26 轮 | herd 赢但记分卡不达标；或平/输但达标 | 前者查记录器看分数从哪来；后者改问题不改 herd；不达标按测试台文档第 5 节的表改对应部分 | 14.1；《Herd 测试台》 |
| D72 | 判定协议：数据分三份，先试点再按功效定运行次数，单侧配对 t 检验；抽查 = 对同一段基线做两次新测量（日常 vs 抽查数据）比差，阈值 2.5σ√2，各臂相同（第 30 轮改） | 第 27、28 轮（v3 审稿） | 试点算出的运行次数远超预算；或预注册后测出的效应远小于人定的最小有意义效应 | 前者降为探索性运行、如实报告；后者先查噪声估计是不是偏小，再考虑换问题 | 9；14 |
| D73 | 配对测量：候选和基线同批同时段跑，报逐项差和标准误；候选哈希含基线标识；慢档返回逐种子分 | 同上 | 配对差的方差不比绝对分小；或同一个 diff 打在不同基线上被当成同一候选去重 | 前者查两边数据批次是否真的对齐；后者查哈希实现 | 9 |
| D74 | 合并流程（Integrator，四臂同配置、运行中都跑）：基线逐步前进，先进先出，合前 rebase，冲突交池外新 agent 合，基线动过则一次配对复测，增益超 1 个标准误且过 gate 才合入；相互作用按 2σ√2 记；运行后出归因报告（第 30 轮改） | 同上 | 合入的东西里接近零的补丁占比超过五分之一；或交付物的分数低于某个单独候选的配对分 | 前者切严格版接收规则；后者是相互作用，归因报告要能指出是哪两个候选 | 9；8 |
| D75 | 出网白名单：worker 沙箱只通模型 API、herd 工具服务、声明过的上下文源和包镜像 | 同上 | 记录器里出现白名单外的访问；或 worker 的答案和公开资料高度一致 | 封沙箱、候选标 refuted；查适配器的网络配置 | 9 |
| D76 | 机会地图只是共享信息，不是派活依据；新增 map/、baseline/、report/ 三个目录和配套事件类型；第一批测量脚本由人在建问题时写，Integrator 重跑；S1 收得到更新 | 同上 | PI 或 Planner 开始按地图条目一对一派人（等于回到 v3 的静态分工）；或地图没人读 | 前者在 Planner 的 prompt 里明说地图不是任务表；后者去掉地图，不算错 | 5 |
| D77 | 不靠 checkpoint 的重启路径：窗口 70% 触发，关自动压缩，起始包四样，进程被杀由 sidecar 代写 stub claim；gate 前就能用，各臂相同 | 同上 | 重启后 agent 重做已经做过的事超过一次；或到 5 分钟兜底的中断占比超过 10%（H3） | 起始包里多带几次工具调用；查用量计算是否滞后 | 7 |
| D78 | 推迟（第 30 轮）：预审 H1 之后单独 A/B，届时由不读 eval 代码的预审员做、各臂相同；保留快档复测不限次，compose 多版只用快档挑 | 第 29 轮（P10） | Critic 的否决和慢档结果不相关（被否决的候选补跑慢档并不差） | 降到 1 个或去掉预审 | 9 |
| D79 | 收尾顺序各臂相同：停止接收 → guard\_pending 补检 → 最终抽查 → 冻结交付物 → 所有运行结束后一起评 holdout\_verdict；归因在判定之后、只用日常数据；接收规则预注册时选定，运行中不切 | 第 30 轮（Opus 审稿） | 两臂交付物里 guard\_pending 的处理不一样；或归因碰了判定数据 | 查收尾配额是否各臂相同；归因改用日常数据 | 9 |

## 附录 A：v1 的独立审稿记录

本附录是 v1 的历史记录：表里的节号和 P 编号指 v1 的编号，v2 的对应决定见第 15 节登记表。审稿由一个没有参与前期讨论的 agent 完成，只看导出的文档。它给了 12 条意见，5 条严重，我全部采纳或部分采纳，正文已按下表改过。保留这份记录是为了让读者知道第一稿错在哪，以及哪些改动是被迫的。

| # | 严重度 | 审稿指出的问题 | 处理 | 改在哪 |
| --- | --- | --- | --- | --- |
| 1 | 严重 | verified 的定义在 P4、第 4 节、第 5 节互相冲突。CI 只能验证 result，insight 和 dead\_end 永远停在 proposed，而 P4 又禁止 build on 未 verified 的东西，等于封死了 S2 的核心假设 | 采纳。按 claim type 分状态机，加统一的事件权限表；允许引用 proposed 的 insight 但必须标注状态 | 第 2 节 P4、第 4 节状态机表 |
| 2 | 严重 | eval 结果没有接收者。eval\_request 异步、T2 小时级，任务结束就释放 lease，"我的候选得了几分、下一步怎么改"这个闭环无人负责 | 采纳。result\_ready 事件绑定提交者的 checkpoint，自动生成后续任务 | 第 5 节 Eval service、第 6 节 hook、第 7 节规则 1 |
| 3 | 严重 | 写规则和目录布局不自洽：lease 每次 tool call 刷心跳却放在 git 里；claims 和 candidates 没有 agent 前缀，CI 路径检查无法执行；result.json 落在 worker 可写目录下可伪造 | 采纳。lease 移到 KV；claims 和 events 按作者分目录；candidates 内按写者分文件，result.json 只有 Eval service 能写 | 第 4 节布局和 lease |
| 4 | 严重 | eval 吞吐和需求不匹配：10 并发 T2 对 50 个 worker 是每人每 5 小时一次；sandbox 只有 6 台；worker 必然在 sandbox 自建代理 eval，既不在资源计划里也绕开了标定 | 采纳。本地代理成为受记录、受标定的一等机制；sandbox 加到 15 台；明示每 worker 配额 | 第 8 节配额与本地代理、第 12 节机器表 |
| 5 | 严重 | 实验不可判：四臂各需全量预算且单次运行，LLM 方差下"稳定超过"不可测；B1 的名额分配和 S2 的 Planner 不同，通信价值和分配策略混淆；只数 T2 次数而 S2 额外消耗 T1 和 T3；做废功由被测系统内的 Librarian 标注 | 采纳。所有臂固定同一分配策略；按 eval 机器小时计量；5 到 10 个 worker、3 个 seed、预注册效应量；做废功换成 Indexer 自动计数的 dead\_end 重走次数 | 第 11 节指标表和公平性 |
| 6 | 中等 | Critic 是泄漏通道：它是唯一读 eval 代码的角色，却把"为什么得分"写成全员可读的 audit，正是第 8 节警告的 hacking 传播路径；触发条件和 T3"极少"矛盾 | 采纳。audit 分公开结论和 worker 不可读的理由区；触发加速率限制 | 第 5 节 Critic、第 8 节 |
| 7 | 中等 | refuted 级联没有刹车：验证器抖动会产生假 refuted，没有撤销路径，CI 和 Critic 冲突时优先级未定义，跨机时间戳不可靠 | 采纳。事件优先级人 > Critic > CI；单来源 refuted 不级联；加 reinstated 事件；用 merge 序号排序 | 第 4 节配套规则、第 7 节规则 4 |
| 8 | 中等 | 可见性时延矛盾：循环只在任务结束 push，共享时延等于任务时长，第 7 节却说硬中断秒级 | 采纳。每条 claim、surprise、eval\_request 产生时立即 push；commit 率重算为 300/小时，CI 分级 | 第 5 节 merge queue、第 6 节循环 |
| 9 | 中等 | 漏掉的失败模式：git 和对象存储单点；30 天删轨迹后 evidence 悬空；长 sandbox 调用期间无心跳；API 限流；token 预算没有组件执行；claim 正文的 prompt injection | 采纳。逐条补进第 13 节；加 Budget meter 组件；轨迹只归档不删 | 第 5 节 Budget meter、第 13 节 |
| 10 | 中等 | 第一版过度设计：NATS 硬中断、代理模型、ASHA、新颖度排序、每方向 Librarian、全景都排在第 3 周门槛前；第 4 周塞了太多东西；fork 和 ask\_checkpoint 的跨机可移植性是最大工程风险却没有 spike | 大部分采纳。硬中断改为 tool call 边界轮询，NATS 推迟；代理模型、ASHA、新颖度、每方向 Librarian、Scout 全部推迟到第 5 周起单独 A/B；加第 0 周 spike；第 4 周扩到 20 而不是 50。ASHA 保留在设计里，因为它是 eval 吞吐的主要杠杆 | 第 5 节 Router、第 14 节 |
| 11 | 轻微 | 参数未定义或可博弈：按预测准确度加权会奖励保守候选；候选 embedding 对代码类产物无定义；"一批"、top-k 的 k 未定义；根目录和 charter 下有两个 problem.md | 采纳。加权只作用于 information 份额；embedding 取相对父代的 diff；一批 = 一个小时名额窗口；根文件改名 spec.md | 第 4、5、8 节 |
| 12 | 轻微 | P5 和第 7 节冲突：Planner 在全景过程里 fork worker、调 diff 截断，正在变成 P5 警告的"管黑板的强 agent" | 采纳。全景独立为 Scout 角色；diff 参数归 charter；Planner 只写 decisions | 第 5 节 Scout 和 Planner、第 7 节 |

**审稿的总体判断**

最大的风险不是规模，是闭环断裂：eval 结果无人接收、insight 无法 verified、吞吐不够，三者叠加会让 50 个 worker 多数时间在等待或重复。最值得先验证的假设是"同等 eval 预算下共享 insight 胜过孤立 best-of-N"，而且必须用 5 到 10 个 worker、固定分配策略、多 seed，先把 S1 对 B1 的差距做出来再谈 50。在此之前应把 fork 和 ask\_checkpoint 的跨机可移植性作为独立 spike 先做掉，它决定 P2 和 P7 能否成立。

我同意这个判断。第一稿把注意力放在了通信层的设计上，把"eval 结果回来之后谁接"这种最基本的闭环漏掉了，这是在讨论里从原则往下推时最容易犯的错：原则都对，但没有人从一个 worker 的一天从头走到尾。附录 B 的走查在改完之后重新对过，Day 1 第 6 小时那一行就是补上的闭环。

**修订后仍然开放的点**

- Critic 自己被骗怎么办，目前只有人抽查。
- 本地代理和 T1 的相关性如果一直上不去，整个"eval 稀缺"的设计退化成 best-of-N 加去重。
- 第 0 周 spike 如果发现 sandbox 快照太大、恢复太慢，fork 要降级为"对话 + 命令日志重放"，P2 的保真度会打折。

**v2 外部审稿（2026-10-18，5 条，全部采纳）**

| 问题 | 为什么对 | 改在哪 |
| --- | --- | --- |
| H1 的分数不可信：best-so-far 随噪声 eval 次数上涨；seed 未定义 | 多个噪声样本的最大值随样本数上升，与质量无关；LLM API 不可 seed | D62；§11 指标表和 D42 规则 3 |
| worker 能读到隐藏材料：git clone 没有按路径的读权限 | readable\_by 在同一个仓库里是空话 | D63；D10 改写；§5 目录、§9 声明 |
| hook 漏掉大部分 tool call：Claude Code 内建的 Bash、Read、Edit 不走 MCP server | "hook 全在服务端"只对 herd 工具成立 | D60 改成三层；心跳移到 Executor sidecar；Harness 端口加 supports\_tool\_hooks；D29、D32、H3 |
| fork 不能解决上下文满：子进程起点就在上限 | fork 复制整个对话 | §7 hook 表改为压缩 + 继续任务；D5 限定 fork 用途，checkpoint 记上下文用量 |
| D51 和 D12 矛盾：代理分进了 softmax，等于动了永久预算 | D12 只允许顶层动永久预算 | D51 改为只吃顶层，窗口之间只有 surge |

**v2b 实现就绪度审稿（2026-10-18，独立 agent，实现者视角，12 条，全部采纳）**

结论：没有结构性错误，缺的是接口层面的字段和一致性。已改：事件文件名改用 local\_id，seq 由 merge queue 在合入时写 `events/_seq/`（D56）；各臂的最终候选选择规则进 D58；eval\_request 自动生成 result claim 并返回 claim\_id，result\_ready 带完整字段，两阶段确认用 confirm\_token，加 parent（D57）；source 声明加必填 cost\_machine\_hours；Model 端口加 embed；Harness 端口给出 build / export\_state / import\_state 签名；lease 的 target\_claims 由 goal 解析加 repo\_write 时服务端维护；Router 检查只读 KV 的 interrupt:\<agent>；D58 补路由规则、服务侧去重、D19、ctx 组成几行；门槛前加 charter 的四个手写文件；event.role 加 librarian，claim.author 加 eval\_service，frontmatter 加 tags 和 blamed\_constraint；D34 优先级补齐全部角色；PI agent 不持有私有存储凭证；D19 的"失败"给了定义；H2 实验移到第 4 周。

**v2 外部审稿第二批（2026-10-18，来自另一个 agent 的验证文件，全部采纳）**

| 编号 | 问题 | 改在哪 |
| --- | --- | --- |
| N1 | 运行中复核和最终判定用同一份 holdout，偏向共享臂 | D66 两份 holdout |
| N4 | B1 在 holdout 存在之前就跑；§14 和附录 C 的时序不一致 | holdout 第 1 天切好；附录 C 第一天的 5 worker 是 smoke 不是正式臂 |
| 阈值 | 3/3 的标准在测方差之前就定了 | D42 规则 4 改为两步：先试跑测方差再封存 |
| N5 | 最终选择的 n 怎么增长没定义 | D66：n = params.verdict\_n 固定 |
| A4 | 分钟级 eval 的 PoC 测不到稀缺 | D70：L1 和 L2 都保留人造稀缺 |
| N7 | 臂不隔离：B1 worker 能读其他 worker 的候选；12 次运行没分开 | D67 |
| N8 | worker 能直接 git push 绕过工具检查 | D68 |
| C8 | 候选格式未定义 | D69 |
| N2 | 第 4 周之前没有 checkpoint，但 result\_ready 引用了它 | D55：H1 阶段 seed\_context 和 submitter\_checkpoint 可空 |

## 附录 B：假想 case 走查

用一个假想问题把每个机制都走一遍（走查写于 v1，机制在 v2 中编号为 D1 到 D58，逻辑未变）。所有分数、次数、agent 编号都是示意，用来看机制怎么咬合，不是预测。

**问题**

把一个 7B 开源模型在一套数学推理 benchmark 上的分数从 61 提到 70，只改 post-training 数据配方，不改模型。Eval：T2 = 8 张 GPU 跑 6 小时 SFT 再评测，eval 是外部系统，10 并发，每天约 40 次 T2 名额。T1 = 1B 模型 40 分钟 + benchmark 子集，T0 = 数据格式和去重统计，秒级。T3 = 人维护的 holdout benchmark 变体。

**Day 0：对齐（两小时）**

| 步骤 | 发生了什么 | 落到仓库 |
| --- | --- | --- |
| 人描述 | "61 到 70，只改数据，不许任何形式的 benchmark 污染" | transcript.ptr |
| Teach-back | PI agent 复述，列出三条默认假设：只允许 SFT；数据来源限于现有 3 个数据池；70 指 T3 分数 | assumptions.md |
| 人纠正 | 第一条错，RL 可以但预算小；第二条对，合成数据可以但要标来源；第三条对 | assumptions.md 更新 |
| 品味 | "要知道为什么有效，不要堆数据"；"一条过滤规则比十个 trick 好" | taste.md |
| 方向 | PI 提 5 个：a-01 用 verifier 过滤错解；a-02 难度课程；a-03 合成多步推理数据；a-04 格式一致性；a-05 小预算 RL | approaches/ |
| 分歧 | PI 认为 a-04 低价值，人坚持保留，预算 10% | disagreements.md |
| 升级规则 | kill 预算超 30% 的方向要人；T3 跌超 2 点要人；Critic 推翻当前最优要人；超时 8 小时冻结 | escalation.md |
| 签字 | status → aligned。PI 从自己的 checkpoint fork 首波 12 个 worker（每方向 2 到 3 个，goal 各异），其余 38 个从任务队列领常规任务 | status.md、checkpoints/pi/ |

**Day 1：启动**

| 时间 | 角色 | 事件 | 机制 |
| --- | --- | --- | --- |
| 0 到 2 小时 | 全部 worker | 读 spec.md 和 digest（此时为空），在 sandbox 里分析数据池，大量 T0 提交 | 任务形态是"推进整个问题"，没有"读数据池 X"这种切片 |
| 3 小时 | worker-07（a-01） | 提交候选 c-9f3a，predicted 64.0，理由"verifier 过滤掉 18% 错解"。Hook 检索无相似候选。T1 相对 baseline +2.1，进 T2 队列 | eval\_request 前强制检索；带预测提交 |
| 4 小时 | worker-19（a-01） | 提交几乎相同的候选。Hash 不同，但 hook 报告"与 c-9f3a 相似度 0.93，已在 T2 队列"。worker-19 撤回，改做 verifier 的变体 | 去重在提交前发生，省下一次 T2 |
| 6 小时 | Eval service | 首批 T2 结果：c-9f3a 实际 63.4，误差 -0.6。result\_ready 事件绑定 worker-07 提交时的 checkpoint；它已结束那个任务，Indexer 以该 checkpoint 为 seed 生成后续任务，被 worker-07 自己领走继续 | 预测记录 |
| 6.5 小时 | Librarian-a01 | 第 10 个 commit 触发，写 a-01 的 digest，提炼 insight c-0102："过滤后数据的难度分布明显左移" | Librarian 从 diff 提炼 insight |
| 8 小时 | worker-23（a-03） | 发现合成数据里多步题的 T1 失败集中在"单位换算"子类。调 surprise() 写 claim，附一个能跑的 notebook 复现。调 fork() 起一个兄弟追这条线，自己继续主线 | 线索变成可执行 artifact；fork 代替 handoff |

**Day 2：第一次分配，第一次复用，第一次硬中断**

| 时间 | 角色 | 事件 | 机制 |
| --- | --- | --- | --- |
| 早 | Planner | 首批完整 T2 结果到齐后首次运行。a-01 领先，a-04 无进展。写 decisions：a-01 份额 35%，a-04 降到 5%（不 kill，因为 disagreements.md 里有记录），exploration 30% | Planner 读 digest 不读轨迹；决定带理由 |
| 上午 | worker-31（a-02） | 连续 3 次 T1 失败。Hook 强制刷新 digest 和检索，看到 c-0102"过滤后难度左移"，意识到自己的课程切分点基于过滤前的分布。改切分点后 T1 +1.4 | 卡住时多读；insight 跨方向复用，reuse\_count +1 |
| 下午 | Critic-1 | 审计 a-04 的高分 claim c-0211，发现"格式一致性的效果"实际来自 eval 解析器的一个 bug。发 refuted 事件，写 audit claim | Critic 是唯一能读 eval 代码的角色 |
| 下午 | Router | worker-12 的 lease 里 target\_claims 含 c-0211 的子 claim，depends\_on 命中 refuted → 硬中断，附 audit。worker-12 在下一个 tool call 边界收到，checkpoint 后释放 lease，转领新任务 | 规则 4；中断只在 tool call 边界生效 |

**Day 3：Reward hacking 和一次升级**

| 时间 | 角色 | 事件 | 机制 |
| --- | --- | --- | --- |
| 早 | worker-41（a-03） | 候选 c-77be 的 T1 +5.8，是历史最高的 2.5 倍 | 跳升超 2 个标准差 → 告警不是庆祝 |
| 早 | Eval service | 自动把 c-77be 送 Critic，暂停其 T2 | 跳升自动触发 Critic |
| 中午 | Critic-2 | 审计发现合成数据的生成模板是 benchmark 题目的改写，语义污染。请求 T3：-0.3。发 refuted；c-77be 的两个子代一并标 refuted | T3 只有 Critic 能请求 |
| 中午 | Router | 两个在 c-77be 基础上工作的 worker 被硬中断 | 规则 4 |
| 下午 | Planner → PI agent | 命中 escalation 的"Critic 推翻当前最优"。PI 带给人："改写级别的相似算不算污染？"三个选项：A 任何与 benchmark 模板相似度超 0.8 的合成数据禁止；B 只禁原题；C 允许但在 T3 单独报告。Planner 倾向 A。超时默认：冻结 a-03 | escalation 格式固定 |
| 下午 | 人 | 选 A。PI 改 constraints.md，status → revising，向 a-03 的 worker 软注入，改回 aligned | 改航向走 charter，不走 prompt |
| 晚 | Librarian-a03 | 写 dead\_end claim："合成数据模板源于 benchmark 的任何形式都是 dead end，T3 不涨"。后续任何 a-03 的 eval\_request 前检索都会撞到它 | dead\_end 是一等公民 |

**Day 4：全景过程和人的洞察**

| 时间 | 角色 | 事件 | 机制 |
| --- | --- | --- | --- |
| 早 | Scout（全景 agent） | 读全部 digest，抽查 surprise claim。发现 a-01 过滤后的难度左移和 a-02 课程后半段数据不足是同一件事的两面。写 cross\_approach insight，并直接从全景上下文 fork 出 worker-52，goal："过滤 + 对过滤后的难度缺口做定向合成，只用非 benchmark 来源" | 跨方向 click 由一个持有全貌的脑子产生；fork 不写任务描述 |
| 上午 | 人 → PI agent | "我记得有篇论文说 SFT 数据里保留一定比例的错误解加 critique 反而有帮助。" PI 写 author=human 的 claim，evidence 指向这段对话，标注 a-01 | 人的洞察无损入库 |
| 上午 | Router | 规则 5 命中，a-01 的 worker 硬中断看到。worker-07 用 ask\_checkpoint 问 PI 那段对话里人有没有提具体比例，得到"没有，人也不确定"，fork 一个兄弟去扫比例 | 回头问 |

**Day 5 到 7：收敛**

| 时间 | 角色 | 事件 | 机制 |
| --- | --- | --- | --- |
| Day 5 | worker-52 | 候选 c-c1d0：predicted 67.5，T1 +4.9，T2 66.8，T3 66.5。Critic 审计通过：得分来自难度覆盖，不是污染。verified | 跨方向 insight 兑现 |
| Day 5 | worker-07 的 fork | critique 数据比例扫到 15% 最好，在 c-c1d0 基础上 T2 +0.8。合并候选 c-e4a2 T2 67.9 | 人的洞察兑现 |
| Day 6 | Planner | 把 a-05 的小预算 RL 放到 c-e4a2 上。T2 69.2，T3 68.9 | 预算跟着进展走 |
| Day 7 | PI agent → 人 | 汇报：T3 68.9，目标 70 未达；按 progress.md 的定义（每周 +4 为显著进展）属于显著进展。建议下周重点：RL 预算上调、a-04 关闭 | 汇报三行 |

**七天后的账（示意）**

| 指标 | 数值 | 说明 |
| --- | --- | --- |
| T2 使用 | 268 / 280 | 去重和 T1 筛掉了约 60% 的提交 |
| 最优 T3 | 61 → 68.9 | B1 best-of-N 对照组同期 65.1；S1 只共享分数 66.3 |
| dead\_end 重走次数 | 7 次 | 其中 4 次是跨方向的 dead\_end 在 diff 排序里排太后，worker 没看到。改排序规则 |
| insight 复用 | 12 条中 5 条 reuse ≥ 2，最高 7 | 最高的是 c-0102"过滤后难度左移" |
| 预测误差中位 | 0.9 分 | worker-07 和 worker-52 最准，队列权重上调 |
| 候选多样性 | Day 2 触发趋同告警 | exploration 从 30% 上调到 40% 后恢复 |
| Critic refuted | 3 次 | 一次 eval bug，一次语义污染，一次子代连带 |
| Handoff 实验 | 摘要组 +0.6，轨迹组 +1.7，fork 组 +3.1（T1 均值） | fork 和摘要差 5 倍，通信层预算按此调 |

**这个 case 验证了什么，没验证什么**

验证了：九条原则各自在至少一个事件里起了作用；eval 稀缺下提交前的检索和预测能省下大量 T2；硬中断在 refuted 传播时有用；人的介入通过 charter 和 claim 两条路都能无损进入。

没验证：Planner 的分配决定是否最优（没有对照）；全景过程如果没发现那个 click 系统会怎样；50 个 worker 是不是真的比单个强 agent 好（要和 B0 比，这个 case 没跑 B0）。

## 附录 C：PoC 启动包（给实现 agent 的第一条指令）

这一页可以直接作为给实现 agent 的第一条消息。它的第一个交付物不是代码，是一份落地计划，人批了再写代码。

前置：测试台（另一份文档《Herd 测试台》）的前 3 天先完成，预检四条全过；herd 的 Eval service 只是它慢档服务的客户端。**范围**：第 14 节"门槛前"那一栏。不做 fork、PI agent、Planner、Critic 的 LLM 审计、surge。目标是第 3 周末跑出 S2（Planner 关）对 B1 @ 5 个 worker 的预注册判定（运行次数按 D72 试点算；预算不够就标为探索性运行），把 H1 变成已验证或已推翻。

**第一个交付物（第 1 天）：落地计划，含五样东西**

1. interfaces.md：task、event（含 Integrator 的新事件）、lease（含 files\_touched）、eval\_request（含 baseline）、baseline/\<k>.json 的 schema，以第 5、9 节为准，补齐字段类型和错误码；臂开关表（D58）的最终版；`router/rules.yaml` 的格式和第一版规则；`charter/params.yaml` 的键；私有存储的布局和凭证边界（D63）；D62 的 holdout 重评流程。
2. 端口接口的代码：第 4.1 节的六个端口写成接口文件，加内存适配器和本地子进程 Executor。这是 herd 作为独立 runtime 的边界，先于任何集群或 harness 的接入。Harness 的第一个适配器用 Claude Code headless，herd 工具通过 MCP 工具服务暴露且 hook 在服务端；中断检查用 Claude Code 的 PreToolUse hook；心跳和兜底在 Executor sidecar（D60 三层）。k8s 是 Executor 的第二个适配器，第 4 周接，映射表见下。
3. 仓库骨架：第 5 节的目录，空文件和 schema 示例。
4. 第 1 到 3 周的里程碑，每周末的门槛按第 14 节。
5. 下面这张问题表的答案栏，空着的不能自己猜。

**k8s Executor 适配器的默认映射（第二个适配器，第 4 周；实现 agent 读集群仓库后填实）**

| 设计组件 | k8s 资源 | 说明 |
| --- | --- | --- |
| Worker × N | 一个 Deployment，replicas = 总 slot 数 | 方向之间的分配是队列权重，不是 pod 数；surge 只改权重，必要时再 scale |
| sandbox | 每任务一个临时 pod，或 worker pod 内的子容器 | 工作目录挂 PVC 或任务结束时 tar 到对象存储，作为 checkpoint 的一半 |
| git 仓库 + CI hook | 单副本 StatefulSet 跑 git server，pre-receive 做同步检查，post-receive 调 Indexer | 热备副本按已有模式 |
| merge queue | 单副本 Deployment，串行 ff-merge，分配 seq | 单副本是刻意的，seq 必须全局单调 |
| Indexer | Deployment，接 post-receive 的 webhook | SQLite 加向量扩展，或集群里已有的数据库 |
| KV：lease、任务队列、eval 队列状态 | 集群里已有的 Redis 或等价物 | 没有就加一个 |
| Eval service | Deployment，对外部 eval 系统的客户端加队列 | 外部 eval 时它很薄 |
| Budget meter | Indexer 的一部分或独立小服务 |  |
| 对象存储：轨迹、checkpoint | 集群已有的 |  |
| Librarian、Critic、PI agent | 各一个 Deployment 或 CronJob | PI agent 是长期存在的单副本 |
| pod 间通信层 | 只给基础设施 RPC 用：worker 调 Eval service、读写 KV | 不给 agent 互发消息用（D38），否则黑板不完整 |

**必须先问人的问题**

| 问题 | 为什么不能猜 |
| --- | --- |
| PoC 用哪个具体问题；eval 的延迟、日配额、并发；有没有 holdout | 决定阶梯、配额、要不要本地代理。系统本身用测试台的问题 A、B 验，不用真问题；L2 的真问题即使 eval 快也要由慢档服务强制人造稀缺（latency ≥ 10 分钟，全队每小时 2 次） |
| 门槛的预注册：效应量、N 是 5 还是 10、seed 数、每臂 eval 机器小时 | 没有预注册的对照不可判（D42） |
| 用哪个 harness、哪个模型 ID（所有角色同一个）；API 的并发和速率上限 | 决定 hook 机制和 checkpoint 格式；决定能同时开多少个 agent |
| 谁手写 spec.md 和首批 approaches；每方向几个 slot | H1 阶段没有 PI agent |
| 集群仓库的访问权；集群里已有哪些服务（KV、对象存储、git、镜像仓库、数据库）；worker pod 的资源规格和总数上限 | 决定选型 |
| 每个 source 的 trust 和 readable\_by（D45）；私有存储用另一个仓库还是对象存储 bucket；worker pod 的凭证怎么隔离 | 决定定义放黑板还是私有存储（D63）；隔离做不到则反 hacking 全部失效 |

**计划批准后，第一天的三件事**

1. 用内存适配器在一个进程里跑通整个 herd 的微型版：一个玩具问题、假 SignalSource（固定延迟返回分数）、两个 worker，走完 claim\_task → eval\_request → result\_ready → 后续任务的闭环。这是以后每次改动的回归测试。
2. 换上真 git、KV 和本地子进程 Executor，同一套 L2 代码在测试台问题 A 上起 5 个孤立 worker 跑一小时作为 smoke（不是正式臂），拿到第一条 best\_score 诊断曲线，并走一遍 Integrator（合入、复测、抽查、收尾）和 holdout\_verdict 流程确认判定管线能通。B1 是所有臂的底座，先跑它最不浪费；5 到 10 个 worker 一台大机器就够，k8s 要到 20 以上才需要。
3. 把跑出来的第一个坏掉的地方写成第 13 节的新行。

**第一轮的完成定义**

- L0 的 20 条断言和测试台问题 A 的记分卡全绿（§14.1）；然后 S2 对 B1+M @ 5，运行次数按 D72 试点算出（预算不够就标为探索性运行），按预注册标准出结果。赢、输、不可判，三种都算完成。
- 四个常设指标的 dashboard 能看。
- 登记表里被这一轮碰到的每条 D 标了"验证 / 推翻 / 未触及"。
- 一份复盘：哪些 hook 和默认值起了作用，下一轮的默认值改成什么。

**不要做的事**

- 不要先把全部组件写完再跑。先跑 B1。
- 不要在没有预注册的情况下开始对照。
- 不要自己决定 PoC 用什么问题。
- 碰到文档里没定义的东西，先查第 15 节；没有就加一行 D 并标"实现时定"，不要悄悄决定。
