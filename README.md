# 充电站月度核算平台（FastAPI + PostgreSQL + React）

把**物业总表读数、设备累计读数、充电订单、分时电价**四类数据接起来，按站点时区与电价生效边界
逐段拆分、同范围核对，生成物业与站方可以逐段对账的月度账单。系统**不接真实电表、不发起扣款**，
只做数据核对、账单计算与依据留痕。

在月度核算之外，本版新增**跨月回补核算**：财务在旧月份账单确认之后，仍能处理迟到订单、
被纠正的表读数和撤销的重复记录，且不覆盖已确认账单、不把补来的累计读数整段重加。

## 一、业务规则与实现位置

| 规则（题目要求） | 实现方式 |
| --- | --- |
| 导入保留原始文件与行号，按来源记录号识别重复 | `import_files` 落盘原件+sha256；`import_rows` 保存物理行号与原始行文本；`source_records`+`record_revisions` 按 `(站点, 来源类型, record_no)` 管理版本 |
| 同号内容变化进修订，不覆盖已被账单采用的数据 | 未采用：新版本 applied、旧版本 superseded；已采用：新版本 `blocked_adopted`，下一轮核算前才提升（`services/importer.py`）；连续 blocked 修订号按最大版本单调递增，避免同号 |
| 时间先转站点时区，再按电价边界拆区间 | 存储统一 UTC，展示用站点本地时间；`tariff_svc.tariff_boundaries` 生成本地午夜/分时切换/电价版本边界 |
| 读数差值与订单用电在相同范围比较 | 以总表相邻读数构成计量区间，设备表/订单/公共负荷按时长裁剪到同一区间后比较（`reconciliation.py`） |
| 跨电价时段缺中间计量值只能出估算项 | 有边界中间读数→实测拆分；否则把**已知总电量**按持续时间拆分并标注 `estimated`；缺起止读数且无上报值→`pending` 待补，绝不按时间编电量 |
| 保留充电、经确认公共负荷、未解释差额，不按订单摊平 | 账单三类成分分列；未确认公共负荷生成物业核对事项且不计入；未解释差额超站点阈值→账单挂起，不自动归零 |
| 读数回退先查设备更换，无依据整段挂起、不产生负电量 | `build_spans`：有换表记录且首尾读数齐全→桥接计算；否则 `suspended`，并生成运营核对事项 |
| 同设备重复上报只计一次；不同订单抢同一时段交运营确认 | 完全同时段→停用重复单+核对事项；部分重叠→`order_conflicts`，确认前两张单都不计入 |
| 作业冻结读数/电价/订单/规则，重跑沿用相同输入，执行期更正留下一轮 | `job_inputs` 快照 + 实体 `adopted_by_job_id`；重跑复制快照且电价查询限定冻结版本；期间更正自动 blocked |
| 确认前检查新修订；已确认账单只做差额更正 | `billing.check_new_revisions`（含 blocked 修订，已回补处理的 backfilled 版本除外）阻止确认；`bill_adjustments` 保留原值、以 delta 更正，不替换原字段 |
| 物业只能处理分配给自己的核对事项 | JWT 角色 + `user_sites` 授权范围，后端每个接口强制校验（`deps.py`） |
| 金额小数精度 | 金额按 4 位小数 `ROUND_HALF_UP` 计算（展示 2 位），电量 4 位；拆分尾差并入末段保证合计守恒 |
| **旧月已确认后处理迟到/纠正/撤销，旧账金额不覆盖** | 回补只产出 `backfill_adjustment_items`（delta），已确认账单原值冻结；当前应付=旧账+人工更正+已确认回补项（`backfill.bill_payable_view`），不能只凭月份查到一个看似完整的总数 |
| **先定位区间/电价段/账单，再撤原贡献+计修正值** | `backfill._locate_intervals` 定位更正行参与的区间与账单；订单/公共/电价按原贡献台账撤一计一；累计读数只重算以该读数为边界的相邻 span，绝不整段重加 |
| **作业按受影响区间分段保存进度，失败只重算未完成段** | `backfill_segments` 每段独立 SAVEPOINT 落 running/done/suspended/failed 与 attempts；续算只挑未完成段，已完成段不重算 |
| **同源修订按来源顺序收齐，缺中间版本则挂起** | `backfill.check_revision_chain` 校验版本连续与 `replaces_revision_no` 衔接；缺失/断链产出 suspended 调整项与运营核对事项，不用最新行猜历史 |
| **新到更正进下一批，已启动批次只用固定数据集合** | `backfill_batch_inputs` 建批即冻结来源记录+修订版本；批次后到达的更正不在本批，下一批候选才能发现 |
| **两批次影响同一原贡献时防双撤** | `backfill_contributions` 台账记录 active/withdrawn 与撤回批次；后批次在前批生效记录上再撤再计，每个电价段始终只有一条 active，历史行最多被撤回一次 |
| **调整项、双方复核、后续账单关联分别保存** | `backfill_adjustment_items` / `backfill_reviews`（物业、财务各一条）/ `backfill_bill_links` 三表分离；物业否认只退回该调整，不阻断无争议月份 |
| **原始事件、原计算、回补差额逐项对平，找不到来源不进结算** | 每个调整项存 before/after 电量金额、电价、依据状态、舍入差与 `detail_json` 对平过程；挂起项金额为 0 且不计入应付 |

## 二、目录结构

```
16/
├── docker-compose.yml          # 一键启动 pg + backend + frontend
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py             # FastAPI 入口（启动自动建表，/docs Swagger UI）
│   │   ├── models.py           # 原件索引/修订/计算依据/结算修订/跨月回补 分层表
│   │   ├── importer 服务 / reconciliation 引擎 / tariff / billing / backfill / security / deps
│   │   ├── routers/            # auth sites imports jobs bills tasks conflicts source-records entities backfill
│   │   └── seed.py             # 账号、站点、样例数据幂等播种
│   ├── sample_data/
│   │   ├── S01、S02/           # 两站点月度核算样例 CSV（生成器 generate_samples.py）
│   │   └── S01/backfill/       # 跨月回补演示：纠正读数/迟到订单/撤销/电价更正/断链挂起
│   └── tests/                  # pytest 37 个用例（含 test_backfill.py 15 个回补专项）
└── frontend/
    ├── Dockerfile + nginx.conf # 静态构建 + SPA/API 反代
    └── src/pages/              # 登录、账单下钻、跨月回补、作业、导入、核对事项、冲突、实体查询
```

## 三、一键启动（Docker Compose）

```bash
cd /workspace/16
docker compose up --build
```

- 前端：http://localhost:8080
- 后端 API：http://localhost:8077 ，Swagger UI：http://localhost:8077/docs
- PostgreSQL：localhost:5432（库/用户/密码 `ccdev`/`ccdev`/`ccdev`，库名 `charging_settlement`）
- 后端容器启动时自动建表并幂等播种账号、站点与样例数据。
- 国内网络构建可传镜像源：
  `docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple --build-arg NPM_REGISTRY=https://registry.npmmirror.com`

## 四、本地开发启动（无 Docker 时）

前置：本机 PostgreSQL 已建库（本隔离环境为 127.0.0.1:5432）。

```bash
# 1. 后端
cd backend
/opt/python-3.12/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python sample_data/generate_samples.py     # 生成两站点样例 CSV
.venv/bin/python -m app.seed                         # 建表 + 账号 + 导入样例
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8077

# 2. 前端（另开终端）
cd frontend
/opt/node-22/bin/npm install --cache /tmp/npm-cache
/opt/node-22/bin/npm run dev                         # http://127.0.0.1:5173 ，/api 自动代理到 8077
```

## 五、测试账号

| 角色 | 用户名 | 密码 | 权限 |
| --- | --- | --- | --- |
| 平台财务 | `finance` | `Finance@123` | 全部站点；导入、发起/重跑核算、确认账单、登记差额更正、创建并执行回补批次、财务确认回补项入结算 |
| 运营 | `operator` | `Operator@123` | 导入；处理订单冲突、回退/换表/重复订单/回补断链挂起核对事项 |
| 上海物业 | `property_sh` | `Property@123` | 仅 S01，只能看到/回复分配给 property 的核对事项，对本站回补调整项复核确认或否认退回 |
| 乌鲁木齐物业 | `property_xj` | `Property@123` | 仅 S02，同上 |

## 六、样例数据覆盖的场景（均被测试与冒烟实际执行）

- **S01 上海（Asia/Shanghai，UTC+8）**：周读数总表 2600kWh；订单跨 peak/flat 且有 10:00 中间计量→实测拆分；
  跨 peak/valley 无中间计量→估算拆分；断网订单 `OR-SH-009` 先只报电量、后补起止读数（同号修订生效）；
  已确认公共照明 320kWh；两个电价版本 10-15 切换；未解释差额 30kWh（1.15%，阈值内，账单可确认）；
  作业后到达的同号更正 `orders_v3_after_adopt.csv` 演示 blocked_adopted。
- **S02 乌鲁木齐（Asia/Urumqi，UTC+6）**：M-C 换表带首尾读数→桥接 1650kWh；M-E 读数回退无换表依据→整段挂起；
  完全同时段重复订单只计一次；部分重叠订单冲突（运营确认前均不计）；无任何依据订单→待补；
  公共负荷未确认→物业核对事项、不计入；未解释差额 250kWh（7.81% > 2% 阈值）→账单挂起不可确认。
- **S01/backfill 跨月回补（先确认 10 月账单，再按编号顺序导入）**：
  - `01_corrected_master_reading.csv`：总表读数 6280→6290 的纠正，验证只重算相邻两个区间（+10/-10 守恒），不整段重加；
  - `02_late_order.csv`：新记录号迟到订单，从 0 计修正值（flat 段 +15 元）；
  - `03_revoke_duplicate_order.csv`：`is_revocation=true` 撤销重复订单，原贡献撤出到 0（peak/valley 共 -14 元）；
  - `04_tariff_correction.csv`：电价更正，电量不变仅金额与舍入变化；
  - `05_broken_chain_order.csv`：`replaces_revision_no=1` 与实际前序不符，触发修订链挂起，不用最新行猜历史。

### 跨月回补操作步骤（本地）

```bash
# 1. 先完成 10 月核算并在前端/接口确认账单（状态 confirmed）
# 2. 按序导入 backfill 目录 CSV（作业后导入的同号行会自动 blocked_adopted）
# 3. 财务在「跨月回补」页预览候选 → 创建并执行批次（分段进度可见，失败可续算）
# 4. 物业在调整项上「确认无误」或「否认退回」（否认只影响该条），财务再「确认入结算」
# 5. 在账单下钻页查看「当前应付」= 旧账 + 人工更正 + 已确认回补项，旧账金额保持不变
```

## 七、验证方式与实际验证结论

以下均为在本隔离环境**实际执行过**的验证（2026-11-13）：

1. **后端单元/规则测试**：`cd backend && PYTHONPATH=. .venv/bin/python -m pytest tests -q`
   → **37 passed**。在原有 19 个用例（原件留痕、修订、时区拆分、估算/待补、回退挂起、换表桥接、
   重复/冲突订单、阈值挂起、重跑冻结、精度守恒、站点隔离、确认拦截、差额更正、冲突处理）之外，新增：
   - `test_backfill.py` **15 个**：订单撤一计一与旧账不覆盖、回补修订不再被常规提升、
     累计读数仅重算相邻区间、迟到订单不重复计、撤销撤出到 0、电价更正电量不变、
     断链挂起且续算不重复、分段失败只重算未完成段、双批次防双撤、批次固定数据集合、
     物业否认隔离单条、财务确认前置物业复核、应付对平与挂起金额不进结算、后续账单关联分离、
     回补修订不再阻塞后续账单确认；
   - 导入器新增撤销实体 `revoked`、连续 blocked 修订号单调递增 2 个用例；
   - API 新增回补全链路（候选→建批→执行→分段→双方复核→应付）与角色拦截 1 个用例。
2. **真实 uvicorn + curl 端到端冒烟**：在独立 `charging_smoke` 库上登录财务 → 发起 10 月核算并确认
   → 依次导入纠正读数/迟到订单/撤销三个 CSV（blocked_adopted/applied 符合预期）→ 候选预览 3 条
   → 建批执行，3 个受影响区间全部 `done` → 7 条调整项（读数 ±10 守恒、迟到 +15、撤销 -14）
   → 物业复核 + 财务确认 → 应付视图 `原 456.80 + 回补确认差 1.00 = 457.80`，`/openapi.json` 返回 200，全部通过。
3. **前端**：`npm run build` TypeScript 类型检查与生产构建成功（含新增「跨月回补」页与账单应付面板）。
4. **Docker**：本隔离容器未安装 docker 守护进程，`Dockerfile`/`docker-compose.yml` 已交付但**未在此环境实际构建运行**；
   配置依据上述已验证的本地启动步骤编写，启动命令与端口见第三节。

## 八、主要 API 一览（详见 /docs Swagger UI）

- `POST /api/auth/login` 登录获取 JWT
- `POST /api/imports` 上传 CSV（multipart：site_id、source_type、file）；`GET /api/imports/files/{id}/rows` 原件逐行
- `POST /api/jobs` 发起核算（周期为站点本地时间，支持 `rerun_of_job_id` 冻结重跑）
- `GET /api/jobs/{id}/intervals`、`/splits` 区间与订单拆分下钻
- `GET /api/bills`、`/api/bills/{id}/precheck`、`/confirm`、`/adjustments`
- `GET /api/bills/{id}/payable` 当前应付（旧账+人工更正+已确认回补项）；`GET /api/bills/{id}/backfill-items` 账单关联回补项
- `GET /api/backfill/candidates` 回补候选预览；`POST /api/backfill/batches` 建批（冻结数据集合）；
  `POST /api/backfill/batches/{id}/run` 执行/断点续算；`GET /api/backfill/batches`、`/{id}`、`/{id}/segments`、`/{id}/items`
- `POST /api/backfill/items/{id}/reviews` 双方复核（物业 acknowledge/reject，财务 confirm）；
  `GET /api/backfill/items/{id}/reviews` 复核留痕；`POST /api/backfill/items/{id}/link`、`GET .../links` 后续账单关联
- `GET /api/tasks`、`POST /api/tasks/{id}/respond` 核对事项（含回补断链挂起事项，后端按角色与站点授权过滤）
- `GET /api/conflicts`、`POST /api/conflicts/{id}/resolve` 订单冲突
- `GET /api/source-records`、`/{id}/revisions` 来源记录与修订链
- `GET /api/entities/readings|orders|public-loads|replacements` 原始依据查询

## 九、CSV 列格式

所有文件首行为表头，必须含 `record_no`（来源记录号）。完整示例见 `backend/sample_data/S01`、`S02`：

- 读数（master_reading / device_reading）：`record_no,meter_no,device_id,reading_time,reading_kwh,is_correction`
- 订单 order：`record_no,order_no,device_id,start_time,end_time,start_reading,end_reading,reported_energy`
- 电价 tariff：`record_no,tariff_name,effective_date,end_date,segment_name,start_hour,end_hour,price`（一个 record_no 可含多个分时段行）
- 公共负荷 public_load：`record_no,load_name,start_time,end_time,energy_kwh,confirmed`
- 换表 replacement：`record_no,meter_no,effective_time,old_final_reading,new_initial_reading,evidence_note`
- 可选回补控制列（任意类型可附加，缺省按普通修订处理）：
  - `is_revocation`：`true` 表示撤销同号记录，原生效实体置 `revoked` 且不新增实体；
  - `replaces_revision_no`：声明本版本承接的来源版本号，回补按此校验修订链连续性，断链即挂起。
