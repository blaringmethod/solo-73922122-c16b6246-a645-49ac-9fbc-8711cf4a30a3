# 联考选项人数核对与供数系统

教务员核对一场联考的选项人数表，确认数据可供题库分析。Flask 网页 + SQLite 单库，
围绕“**只有找到能同时复现全部人数的卷型分配才允许发布供数**”这一规则实现完整状态机。

## 业务规则与实现对照

| 需求 | 实现 |
| --- | --- |
| 导入作答 CSV（卡号、可空卷型码、题位、所选项）与甲乙卷对照表 | `app/services.py` 的 `import_answer_csv` / `import_mapping_csv`，表头校验、同卡一卷型校验、重复作答校验，失败整事务回滚 |
| 同卡两条作答按某卷型换算后落到同一题号（即使标准选项相同），该卷型不能用于这张卡；不同卡各占一个题位落到同一题号则正常 | `_card_paper_issue` / `_contrib_vector` 增加同卡同题号冲突检测；求解器把冲突卷型作为该卡不可用分支排除，继续寻找能复现整表的分配；`recompute_mapped` 不再因 `answers_mapped` 主键冲突炸库 |
| 换表时已识别卷型的卡有冲突：报出卡号和题号，保留原对照表、换算明细和供数状态 | `import_mapping_csv` 落库前对新表整体预检（`_known_card_paper_issues`），命中即抛错回滚，错误信息含卡号、题位、题号；缺码卡不参与预检 |
| 供数中换表，冲突只在缺码卡的现用推定卷型：新表与停供同时生效，按新表找到合法分配才恢复 | 新表照常提交，`_revalidate_after_change` 复核现用分配失败即置 PAUSED 并清空失效分配；`/resume` 按新表重新求解，成功才回到 SUPPLYING |
| 题位与选项换成题库题号与标准选项 | 对照表映射写入 `mapping` 表；`recompute_mapped` 按生效卷型生成 `answers_mapped` |
| 登记实收甲卷份数 | `meta.received_a`，核对时作为硬约束 |
| 每张卡整表共用一个卷型；已识别卷型照用 | `cards.is_known=1` 的卡固定原码，仅 `is_known=0` 的缺失卡参与分配求解 |
| 甲卷总数须等于实收份数 | 求解器约束 `need_a = 实收 − 已知甲`，并由 `_finalize` 独立复核 |
| 只有能同时复现所有人数的分配才可发布 | 回溯求解 + 单元上下界剪枝；解出后再整表重算逐单元比对，全部相等才置 VERIFIED；发布前二次复核 |
| 核对页列出一组可行分配与各卡甲/乙可用性 | `/check` 页面给出每张缺失卡的甲/乙分配、逐单元目标/实测对比，并标注不可用卷型及原因（同卡同题号/无映射） |
| 更正卷型码或实收份数：现用分配仍能证明整表则继续供数，否则立即暂停 | `correct_paper` / `correct_received_a` → `_post_correction` 统一处理；SUPPLYING 下失效即转 PAUSED 并清空失效分配 |
| 停供后可核对同一张表，找到符合最新资料的分配才恢复 | PAUSED 下“重新核对并恢复供数”按钮 → 求解成功才回到 SUPPLYING；暂停期间的更正不自动恢复 |
| 核对期间仍可更正，保存失败沿用已提交结果 | 所有写操作包在事务里，校验失败抛 `ImportError_` 回滚，页面回显原因、状态不变；核对/恢复路由额外兜底，任何保存异常都回显为失败而非 500 |

### 状态机

```
IMPORTED ──核对通过──▶ VERIFIED ──发布──▶ SUPPLYING
   ▲                      │                  │
   └────更正失效──────────┘                  │更正后现用分配失效
                                             ▼
                                          PAUSED ──重新核对找到可行分配──▶ SUPPLYING
```

## 目录结构

```
app/
  db.py          SQLite 连接、建表、状态常量
  csv_utils.py   CSV 读取、卷型码归一化（甲/乙、A/B、空=缺失）
  services.py    导入、换算（含同卡同题号冲突检测）、可行分配求解、状态机与更正逻辑
  web.py         Flask 路由
  templates/     总览、核对供数、数据查看三个页面
samples/         演示 CSV（含一份篡改人数的失败用例）
samples/collision/  换表/同卡同题号冲突专项演示数据（v1 正常、v2 缺码卡冲突、v3 已识别卡冲突）
scripts/make_samples.py           演示数据生成器
scripts/make_collision_samples.py 冲突/换表专项数据生成器
tests/           selftest.py（47 项端到端）、http_smoke.py（26 项真实 HTTP）、
                 test_solver.py（13 项求解器）、test_collision.py（29 项冲突与换表规则）
Dockerfile, docker-compose.yml, requirements.txt
```

## 启动方式

### Docker Compose（交付方式）

```bash
docker compose up --build
# 浏览器打开 http://localhost:5000
```

SQLite 库文件保存在命名卷 `exam-data`（容器内 `/data/exam.db`），容器重建不丢数据。

### 本机直接运行（无 Docker 时）

```bash
python3 -m pip install -r requirements.txt
EXAM_DB_PATH=./data/exam.db python3 -m flask --app app.web run --host 0.0.0.0 --port 5000
```

## 网页演示步骤（用 samples/ 里的现成 CSV）

1. 总览页依次导入：`answers.csv`（10 张卡，C009/C010 卷型码留空）→ `mapping.csv` →
   登记实收甲卷 **6** → `supply.csv`。
2. 进“核对与供数”页，点 **核对：求解并保存一组可行分配**。
   页面显示 15 个供数单元全部“✓ 一致”，两张缺失卡得到一甲一乙的分配，甲卷总数 6 = 实收 6。
3. 点 **发布供数**，状态变为“供数中”。
4. 在卡列表把 C006（原乙卷）更正为甲：页面提示现用分配无法证明整表，状态立即变“已暂停”，
   对比表出现“偏差”行。
5. 把 C006 改回乙（暂停期间可继续更正，但不会自动恢复），点 **重新核对并恢复供数**，
   找到符合最新资料的分配后恢复供数。
6. 再试把实收份数改成 7：同样立即暂停；改回 6 后重新核对恢复。
7. 导入 `supply_bad_counts.csv`（人数被篡改）后核对，必然提示无解，无法发布。

### 换表与同卡同题号冲突演示（samples/collision/）

1. 总览页依次导入 `samples/collision/answers.csv`（K1/K2 已识别，U1/U2 缺码）→
   `mapping_v1.csv` → `supply_v1.csv`，登记实收甲卷 **2**。
2. 核对页点“核对”：U1=甲、U2=乙，全部单元一致；发布供数，状态 SUPPLYING。
3. 换导 `mapping_v2_collision_unknown.csv`：U1 的甲卷、U2 的乙卷分别出现
   “同卡两题位落到同一题号”，而 K1/K2 不受影响。新对照表立即生效、供数同时暂停，
   卡表中甲/乙“可用性”列标出冲突卷型与原因；此时直接“重新核对”仍无解。
4. 导入配套的 `supply_v2.csv` 后点“重新核对并恢复供数”：求解器排除冲突分支，
   找到 U1=乙、U2=甲 的合法分配（甲卷仍为 2 份），恢复 SUPPLYING。
5. 再换导 `mapping_v3_collision_known.csv`：这次冲突落在已识别卡 K1 上，
   导入被**整体拒绝**，红色提示给出卡号 K1 与题号 Q1；原对照表、换算明细、
   分配与 SUPPLYING 状态全部保留。

CSV 表头约定：

- 作答：`卡号,卷型码,题位,所选项`（卷型码可空；支持 甲/乙、A/B）
- 对照表：`卷型码,题位,所选项,题号,标准选项`
- 待供数：`题号,标准选项,人数`

## 自测

```bash
# 端到端（Flask 测试客户端，覆盖全部状态流转与回滚）
python3 -I tests/selftest.py
# 求解器边界/性能（200 张缺失卡约 15ms）
python3 -I tests/test_solver.py
# 同卡同题号冲突与换表规则（29 项）
python3 -I tests/test_collision.py
# 真实 HTTP 冒烟（先启动服务）
SMOKE_BASE=http://127.0.0.1:5000 python3 -I tests/http_smoke.py
```

本机若 Flask 不在默认搜索路径，可用 `EXTRA_PYTHONPATH=/path/to/site-packages python3 -I ...`
（自测脚本支持该变量；Docker 内由 requirements.txt 安装，无需设置）。

## 设计说明与边界

- **同卡同题号冲突**：判定单位是“单卡 × 单卷型”。同一卡的两条作答按某卷型
  换算后落到同一题库题号即判该卷型对本卡不可用（与标准选项是否相同无关）；
  不同卡各自落到同一题号属正常汇总，不做限制。冲突检测同时落在三处：
  换算写入（`recompute_mapped`）、贡献向量（`_contrib_vector`，求解器据此排除分支）、
  换表预检（`_known_card_paper_issues`），避免再出现“求解通过、保存撞主键”的不一致。
- 求解器对“贡献向量完全相同”的卡分组合并，把 2^n 降为组内甲卷数量 k 的组合搜索，
  并以各单元人数的剩余可达上下界剪枝；搜索节点上限 30 万，超限返回明确原因而非假阳性。
- 每个解在保存前由 `_finalize` 脱离搜索结构、对整表重新换算复核，避免搜索逻辑自身的错误放行。
- 换表是“先预检、后落库”的单事务：预检只拦截已识别卷型卡的问题（其卷型不可再分配）；
  缺码卡的冲突允许新表生效，走暂停 → 重新核对恢复的既有状态机。
- 单场考试、单 SQLite 文件；重新导入作答 CSV 会清空上一场数据（事务内完成）。
- 开发用 Flask 内置服务器；生产部署建议换 gunicorn/uwsgi。
