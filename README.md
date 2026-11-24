# 工业设备升级跟踪系统（CNC Upgrade Tracker）

面向数控设备与边缘网关的厂商公告采集、构建身份治理与升级建议系统。
解决“升级通知散落在多门户、同版本号不同构建、地区站滞后、生产暂缓难以跟踪”
的工程问题，输出**可执行 / 需复核 / 不适用**三态升级建议，并保留完整证据链。

## 设计原则（对应业务纪律）

| 业务要求 | 系统实现 |
| --- | --- |
| 同版本号不代表同构建 | `builds` 以 `(产品, 版本号, 构建号)` 为唯一键；R1/R2 同版本号不同渠道是两条独立 `releases`，附件与哈希各自留存 |
| 版本相近/文件名相同不能自动等价 | 原文构建进入 `build_identities`（状态 `raw`），必须由**软件资产管理员**显式确认才绑定实体；未确认身份的建议一律 `needs_review` |
| 空结果≠没有新版本 | 解析成功但列表为空抛 `EmptySuspectError`，抓取记为 `empty_suspect` 并进待处理队列 |
| 访问失败/页面改版不能静默 | 网络/HTTP 失败 → `fetch_failure`；schema 不匹配/结构变化 → `parse_failure`，原始字节仍存 `raw_snapshots` |
| 来源变化、正文版本、附件校验可追溯 | 每次抓取存原始快照与 sha256；公告正文按哈希定址追加 `announcement_revisions`，旧正文不覆盖；附件同时保留厂商校验值与实际观测哈希 |
| 暂缓只对指定设备与期限有效 | `production_exceptions` 带设备、起止日期与审批状态；到期惰性标记 `expired`，未批准的 `pending` 不产生暂缓效力 |
| 例外到期/撤回版本/兼容纠正触发重算 | 建议输入参与 `inputs_hash` 计算（含评估日期），变化即重算；旧建议进 `recommendation_history`，不覆盖 |
| 已执行设备保留当时版本与批准 | 工程师确认回执后计划 `done` 并冻结；`execution_receipts.evidence_snapshot_json` 固化当时建议、兼容、例外、公告证据，后续撤回/纠正不改写 |
| 角色分离 | 资产管理员确认身份；产线工程师确认兼容矩阵与窗口/例外；班组长开工与回执；工程师确认升级结果 |

## 数据模型概览

```
sources ─< fetch_runs ─< raw_snapshots          （采集证据，只追加）
vendors ─< products ─< builds                   （身份实体）
releases >─ builds ─< release_artifacts         （发布与附件校验）
announcements ─< announcement_revisions ─< announcement_artifacts
announcement_targets ── products/builds（原文构建）
devices ─< inventory_events                     （设备构建变迁审计）
build_identities                                （raw → confirmed/rejected）
compatibility（supersedes 链，纠正不覆盖）
maintenance_windows / production_exceptions
recommendations（supersedes 链 + history）
upgrade_plans ─< upgrade_plan_events
execution_receipts（证据快照，冻结历史）
pending_queue（fetch_failure/parse_failure/empty_suspect/identity_review/...）
```

## 快速开始

仅依赖 Python 3.11+ 标准库（SQLite）。

```bash
# 开发环境可编辑安装（无第三方依赖）；或直接用 PYTHONPATH 运行
python3 -m pip install -e .

upgrade-tracker init-db
upgrade-tracker source-add src-cat 厂商目录 catalog <url或本地路径> json_catalog_v1
upgrade-tracker run-once
```

### 来源数据格式

来源解析器按 `sources.parser` 分派，当前内置：

- `json_catalog_v1`：`{"schema_version": "catalog/v1", "releases": [...], "advisories": [...]}`
- `json_advisory_v1`：`{"schema_version": "advisory/v1", "advisories": [...]}`
- `json_inventory_v1`：`{"schema_version": "inventory/v1", "devices": [...]}`
- `html_advisory_v1`：带 `data-advisory-key` / `data-target` 微格式的 HTML 公告页；
  页面改版导致标记缺失时按格式变化处理，进入待处理队列

新厂商接入 = 在 `parsers.py` 增加一个解析函数并登记到 `PARSERS`，
解析器只做结构翻译，不做版本等价推断。

### 典型操作流（角色视角）

```bash
# 资产管理员：确认原文构建身份
upgrade-tracker identity-list
upgrade-tracker identity-confirm <idn_id> <actor> <product_id> <build_id>

# 产线工程师：兼容矩阵、停机窗口、暂缓审批
upgrade-tracker compat-set <release_id> <product_id> compatible <actor>
upgrade-tracker window-add D3 2026-11-27 2026-12-05 <actor>
upgrade-tracker exception-request D3 2026-11-25 2026-11-30 连续生产 <actor>
upgrade-tracker exception-decide <exc_id> <actor> --approve

# 工程师：补录离线公告（附件自动计算 sha256）
upgrade-tracker advisory-manual offline.json <actor>

# 附件下载到隔离目录后核对厂商校验值（不符进待处理队列）
upgrade-tracker artifact-verify <artifact_id> /隔离目录/gw8.bin <actor>

# 系统：重算建议
upgrade-tracker recompute
upgrade-tracker reco-list --status actionable --line L1

# 班组长：查看证据、开工、回执
upgrade-tracker plan-list --line L1
upgrade-tracker plan-evidence <plan_id>
upgrade-tracker plan-start <plan_id> <actor>
upgrade-tracker receipt-submit <plan_id> <actor> 2.8.2+5

# 工程师：确认升级结果（冻结历史）
upgrade-tracker receipt-confirm <rcp_id> <actor> --approve
upgrade-tracker receipt-show <rcp_id>
```

## 建议判定逻辑

对每个 `(设备, 发布)` 依次过闸：

1. 发布撤回/下架 → `not_applicable`（撤回是独立硬事实，优先返回）
2. 身份被驳回 → `needs_review`
3. 设备或发布身份未确认 → `needs_review`（禁止凭文本相同判等价）
4. 设备已在该构建上 → `not_applicable`
5. 兼容矩阵缺失/pending → `needs_review`；判不兼容 → `not_applicable`
6. 无附件证据或无公告证据 → `needs_review`
7. 以上通过 → `actionable`；其中：
   - 生效中的生产例外 → 计划 `blocked`（原因写明例外期限）
   - 无未来停机窗口 → 计划 `blocked`（原因“尚无允许停机窗口”）
   - 两者皆备 → 计划 `scheduled`

## 定时运行

- 常驻：调用 `upgrade_tracker.scheduler.run_forever(conn, interval_seconds)`
- 系统 cron：周期执行 `upgrade-tracker run-once`（抓取 → 落库 → 全量重算）
- 单源失败不影响其他来源；失败计数与待处理数量在周期报告中输出

## 测试

```bash
python3 -m unittest discover -s tests -v
```

48 个用例覆盖：采集失败/空结果/格式变化入队、快照与正文版本哈希、
身份确认与驳回、三态判定、例外到期/撤销、撤回与兼容纠正重算、
离线补录证据、附件哈希校验、回执闭环与已执行历史冻结、CLI 端到端角色协作。

## 安全注意

- 下载的附件与外部来源内容是不可信数据：存放在独立目录，系统只读取与哈希，不执行；
  所有读取样本的脚本以 `python3 -I` 运行，避免目录内植入模块劫持。
- 来源凭据只存引用名（`sources.credentials`），不存明文。
