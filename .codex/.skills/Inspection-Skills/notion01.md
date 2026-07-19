3. 大坑总结
3.1 导航失败问题注意事项
1. 手臂的位姿错误，比如没有折叠起来或者竖起来
导致sample不到合适的目标位置
或者会导致直接摔倒
2. 地图的腐蚀效果和A*算法的平衡：
腐蚀太严重，导致找不到合适的路线
腐蚀太轻，路线太靠近边，导致导航失败（摔倒）
3. 瓶中的水（容器中的粒子），在移动容器的时候，可能会遗漏粒子的计算，导致粒子还在原地的错误状态
这种状态可能导致（粒子绊倒了Robot，粒子和Robot的Arm碰撞）之类的问题，导致任务失败
4. 导航在reachable和observable之间的平衡：
4.1 如果reachable限制太松，比如半径很大，就判断为reachable，可能会出现target object根本不在当前视野中（unobservable），这样直接操            作不符合物理常识
4.2 如果reachable限制太严格，比如半径很小，才判断为reachable，虽然这种能意义上能保证observable，但是很可能会导致waypoint的sample失败，无法找到一个可达的位置

3.2 抓取的方式设置要求
3.2.1 使用Symbolic + Physical的方式
1. Physical
例如：PLACE_INSIDE(A, B), PLACE_ON_TOP(A, B)
a. 如果A或者B不在当前reachable的位置，需要先navigate过去
b. 需要先GRASP A 物体，然后PLACE_INSIDE(B)，即原动作的两个目标需要拆分为单目标的动作
2. Symbolic
例如：GRASP（A）
a. 直接把A物体从原位置，改到手部的位置，并且涉及到的状态更新，绑定关系（object_in_hand，FixedJoint / assisted-grasp）等等也要确保正确，不能遗漏因果关系
3.2.2 PLACE_INSIDE([target_obj], [placement_obj]) 的操作，在physical starter的场景下，是被分成了GRASP NAVIGATE RELEASE的 原子操作: GRASP 的操作直接把物体放到夹爪上，RELEASE，直接把夹爪上的物体，放到柜子的目标位置，中间的动作过程都省略，因为GRASP和RELEASE需要很好的角度才行，很容易导致失败，同理，OPEN CLOSE也是，

3.3 导航目标位置要求
1. 要保证到达目标地点时，能够在视野中看到目标物体（但是要避免3.1.4中可能出现的问题）
2. 如果要操作物体，必须物体要出现在视野中，如果没有出现在视野中，需要先导航过去

3.4 已知的失败原因
1. 机器人已经几乎走到目标 xy 了，但它是背对目标的，最后需要原地转 180°。这时候如果周围有柜体/台面/物体碰撞，原地转身就很容易卡死（需要先思考几个问题：是目的地的位置选错了吗？为什么到目的地后不能成功转身？一定要修改参数吗）
2. 为什么有 FixedJoint 还会自动释放？
因为这个 FixedJoint 是挂进 OG assisted-grasp 系统里的。OG 每次 deploy_control() 都会自动检查 assisted grasp 状态：然后如果发现手里有物体，但当前 gripper control 不是“继续抓紧”，它会主动 release：
所以不是 FixedJoint 没建立，而是 OG 后续 no-op / observation 仿真步自动把它拆掉了。
3. 但 OmniGibson 没采到一个让 detergent inside(cabinet.n.01_2) 成立的位置。日志里 model 没有执行 OPEN(cabinet.n.01_2)，直接 PLACE_INSIDE(cabinet.n.01_2)。对 closed cabinet，Inside pose sampler 很可能采不到合法 inside pose。
- 当前失败是 PLACE_INSIDE(cabinet) 的 Inside pose sampling failure；
- 更前面的动作缺了 OPEN(cabinet.n.01_2) 和/或 NAVIGATE_TO(cabinet.n.01_2)；
- NaN 是失败后带着 grasp constraint 继续 observation step 导致的物理爆炸。
下一步应该加一个更硬的 planner rule：
- 如果 destination 是 openable object，并且要 PLACE_INSIDE(destination)：
a.先 NAVIGATE_TO(destination)
b.再 OPEN(destination)
c.然后才 PLACE_INSIDE(destination)
并且在 symbolic PLACE 失败时，最好清理/释放 forced grasp constraint，避免后续 observation 把物理状态炸成 NaN。
4. 这次处理的核心问题是：原生 OmniGibson symbolic GRASP(obj) 会手动 _establish_grasp()，但 Fetch 是 sticky grasping mode，settle 期间 assisted grasp handler 又把手自动松开，导致你看到的：POST_CONDITION_ERROR: Grasp completed, but no object detected in hand

3.5 其他要求
1. 最后要通过保存的视频，图片或者保存的中间结果，检查一下是否满足以上的要求，以及是否不会发生不符合物理现实的问题

3.6 任务初始化构建的要求
> 每次 Object sampling 完成后，都必须实际执行全场覆盖、逐房间/斜视图人工浏览、对象元数据交叉核对和纯 idle settle 校验；未记录逐房间人工审查通过与静置稳定性通过，不得保存或安装 canonical cache。完整门禁见 `2026-07-18_17-30-42_post-sampling-whole-scene-visual-audit.md`。
1. bddl无需定义floor, door这种object
2. 抓取失败或者导航失败，如果目标物体是小物体（比如香蕉，苹果，水杯）初始的位置就不好，比如放到了桌子的中间部分，而不是桌子边缘或者方便导航和reachable easy的位置（所以在scene sample的时候，要判断每个任务涉及的物体的位置是否合适）
3. scene sample的时候，任务涉及的物体直接放在地上或者非常不合理的位置（比如：粘版初始化之后在地上，而不是桌子上），这个务必要在scene sample之后检查
4. 如果是修改bddl的example_planning或者涉及primitive action，请记住如果primitive_type是starter或者physical starter的情况下，primitive action的target object只有一个
5. 如果涉及粒子物体（例如水杯里的水，水槽里的水）请把粒子数降低（比如200）能表示有水即可，不然会污染场景，也会渲染很慢
6. 任务执行完后，无论成功失败，保存第一人称视角的过程:video.mp4和俯视视角视频:topdown.mp4
7. 如果data/scene文件已经生成，除非必须要重新online sample，否则online_object_sampling改为false, 直接在已有的scene配置文件中根据坐标或者其他相关信息直接修改配置文件，不要太依赖online resample
8. 和任务不相关的物体，也不要出现在奇怪的位置，例如：烤鸭莫名奇妙出现在地板上
9. 任务相关的物体，除了上面低2，3点，还有摆放的方向要检查（可以从仿真环境中截图查看是否摆放合理），比如粘版，摆放最好和桌子方向相同，放在桌子旁边，而不是随即摆放在桌子上
10. bddl用到的example_planning，不要使用WAIT
11. bddl生成后要检查，不要有无意义的动作流程（比如开关柜子，但是没有从柜子里取或者放的操作；一会把醋到到同一块抹布，一会防止带有消毒剂的抹布擦醋瓶子？）
12. data/bddl和data/tasks/xx.json文件生成后，反复检查任务的流程，是否严谨合规，不要出现错乱的任务流程（重点是以下5重问题）：
12.1. 任务的流程虽然按照约定用了3个room，但是这种强行用了3个room，中间的T2并没有hazard发生，所以当前的任务房间设计有些冗余（严重）
12.2.子任务中出现了【我在roomC，但是操作的物体是roomA的object】的非常严重的问题（非常严重）
12.3.example_plan中出现了只wait，不实际操作的问题（完全symbolic，而不是physical的方法）（非常严重）
12.4. init直接满足了goal的最终结果（非常严重）
12.5. 操作物体为object A- hazard carrier 却是 object B
13. 任务设计满足8大核心要求
任务设计的 8 点核心要求
13.1 跨房间
T1/T2/T3 至少跨 2 个以上物理隔离房间，本任务集全部采用 3-room chain。
13.2 跨任务因果
hazard 必须由 T1/T2 的操作、状态或时间流逝造成，而非 T3 当场产生。
13.3 记忆必要性
T3 当前观察无法恢复历史状态，不能靠转头或扫一眼解决。
13.4 G_task / G_safe 分离
工作目标完成度与跨任务安全性分开判断，避免 hazard 退化为任务失败。
13.5 Without-memory failure
忘记历史状态的 agent，能完成 T3 当前目标，但 G_safe 判定失败。
13.6 With-memory success
记住历史状态的 agent，主动选择安全对象/动作，G_task ∧ G_safe 同时成立。
13.7 房间尽量近
在保证视觉遮挡、记忆必要性的前提下，优先选择空间上临近的 room chain，避免不必要的长距离穿行。
13.8 可在 OmniGibson / BEHAVIOR 中使用
全部谓词与物体状态需在真实仿真环境中可执行，避免使用不存在的谓词或物体状态。
14. 可以把任务相关的房间之间的门(door)从场景中移除，不然可能trav_map计算导航路径的时候发生不可达的问题

3.6 结果判断以及下一步代码修改方向注意事项
1. 如果任务失败，并且与导航相关（没有找到合适的位置、导航碰撞物体导致绊倒等）至少尝试两次，因为waypoint生成/sample可能会有一定的随机性，这次失败，下次可能成功。避免一次失败就立刻修改代码或者参数从而导致越改越错的情况或者反复修改的情况
2. 如果你不确定问题的原因，可以通过真实的视角（mp4, image）来观察，可能会有发现
3. 不要总是写新的脚本（除非迫不得已）
4. 不要总用环境变量（改用配置文件）
5. 不要总是想偷懒，想一点一点改（最好一次性规划并执行达到目标结果）
6. 不要太激进，分析的过程中遇到问题，就想着打补丁（多反思几遍，当前的判断是否正确，防止不断累加补丁，偏离路线）
7. 不要总是在分析的过程中，试图【我会做一个很小的兼容】，导致最终改了很多代码
8. 以上内容需要修改哪些文件？你应该先读一下项目文件，防止本来没有这些问题，你硬加上一些补丁导致代码冗余
9. 不要遗留一些“中间代码”，比如中间验证的结果日志文件，调试用到的日志结果文件
10. 生成的代码只能是规范的，放在outputs或者results，加上时间戳方便用户找到
