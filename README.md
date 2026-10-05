# 本地链上订单簿 DEX（撮合与结算服务）

一个完全本地运行的订单簿 DEX 撮合/结算服务：用**本地账本**模拟链上资产账户与市场行情，
不依赖外部区块链节点、真实凭据或任何付费服务。技术栈沿用项目原有方式：FastAPI + uv，
状态持久化到本地 JSON 账本文件（`ledger.json`），服务重启后订单、余额、成交与幂等结果一致恢复。

## 运行

```bash
uv sync                       # 安装依赖（fastapi / pytest / httpx）
uv run python main.py         # 启动服务（沿用骨架：127.0.0.1，端口 0=随机分配，看启动日志）
uv run pytest -s              # 运行全部测试（-s 可看到关键输入与状态迁移日志）
```

服务首次启动会自动创建默认市场 `BTC-USDC`（base=BTC, quote=USDC，tick=0.01，lot=0.001）。

## 记账模型

- 每个账户每种资产有 `balance`（账户总资产，**包含冻结部分**）与 `frozen`（因挂单被预留的部分）。
- `available = balance - frozen`，即可用于下单/转出的额度。
- **买单**按下单额 `price × quantity` 冻结 quote（如 USDC）；**卖单**按 `quantity` 冻结 base（如 BTC）。
- 成交结算以 **maker（挂单方）价格** 成交，双方冻结与余额在同一把锁内原子更新：
  - 买方：`quote: balance -= cost（实际成交价×数量）, frozen -= 买方限价×数量`；`base: balance += qty`。
    即买方按**自己的限价**解冻该部分预留，成交价低于限价时多预留的差额**立即回到可用额度**。
  - 卖方：`base: balance -= qty, frozen -= qty`；`quote: balance += cost`
- 撤单仅释放**剩余数量**对应的冻结（买单释放 `remaining × price`，卖单释放 `remaining`）；
  已成交部分不受影响。
- 任何成交都只在账户间转移资产，系统内每种资产的 `Σ balance` 保持不变（守恒）。
  账本额外记录每种资产的累计注资额 `credited`，作为全量对账的守恒基线。

## 撮合规则

- **价格优先、时间优先**：买盘按价格从高到低、卖盘从低到高；同价按挂单序号 FIFO。
- 吃单按 maker 价格逐个成交，可跨多档深度，产生**部分成交**；未成交（或剩余）部分留在簿中继续有效。
- **禁止自成交**：吃单遇到同账户挂单会跳过（该挂单保留在簿中且仍有效），继续寻找下一个对手。
- 不能越过限价成交（买价 < 卖价 不撮合）。
- 所有提交由服务级锁串行化，并发提交不会出现超额成交、重复扣款、负余额或资产不守恒。

## 风控与暂停/恢复

每市场维护参考价 `reference price` 与风控参数：

| 参数 | 默认 | 含义 |
| --- | --- | --- |
| `max_deviation_bps` | 1000（10%） | 允许成交价偏离参考价的最大基点（双边对称） |
| `reference_max_age_ms` | 60000 | 参考价有效期（毫秒），超过即视为过期 |

下单时按顺序判定，任何一项不满足都**不产生成交**（订单仍然挂入簿中，响应带 `blocked_reason`）：

1. 市场被暂停 → `MARKET_HALTED`；恢复后新提交可正常撮合（暂停期间已挂订单不丢失）。
2. 无参考价 → `REFERENCE_UNAVAILABLE`。
3. 参考价距当前时间超过有效期 → `REFERENCE_STALE`。
4. 对手盘价格超出 `[ref×(1-bps/10000), ref×(1+bps/10000)]` → 撮合在该价格停止（簿按价格有序，
   后续更差价格也不会成交）。

撤单不受暂停/行情限制，随时允许。

## 资金占用明细

`GET /accounts/{account}/locks?market=<可选>` 返回该账户的占用清单与资产汇总：

```json
{
  "account": "alice",
  "market": null,
  "locks": [
    {"order_id": "ord_...", "market": "BTC-USDC", "side": "BUY",
     "price": "100", "remaining_quantity": "0.5",
     "asset": "USDC", "locked": "50"}
  ],
  "totals": {
    "USDC": {"balance": "10000", "frozen": "50",
             "available": "9950", "locked": "50"}
  }
}
```

- `locks` 逐笔列出每张**未结束**（`NEW`/`PARTIALLY_FILLED`）挂单当前的占用：
  市场、方向、价格、剩余数量、占用资产与金额（买单 `剩余×限价` 的 quote，卖单 `剩余` 的 base）。
  已完全成交或已撤销的订单不出现、不占用任何资金。
- `totals` 是**账户级**汇总（即使传了 `market` 过滤也仍是全账户口径）：
  `balance`/`frozen`/`available` 与实际账本一致；`locked` 是本次清单中各资产占用之和。
  不传 `market` 时，任意时刻每种资产都有 `locked == frozen`，清单与账户实际占用完全对得上。
- 买单成交价优于限价时，差额在成交结算当场解冻（见「记账模型」），
  因此清单与可用额度不会残留已成交部分的占用。

## 批量撤单

`POST /orders/batch-cancel`，body：`account`、可选 `market`、可选 `order_ids`、可选 `idempotency_key`。

两种模式（都在服务级锁内一次完成，不会与新下单/成交交错出半释放状态）：

1. **显式模式**（给了 `order_ids`）：先整体校验，列表中只要存在
   **不存在 / 不属于该账户 / 已结束 / 重复**（或不在指定 `market`）的订单，
   整批不生效、资金一分不变，响应 `applied=false`，问题项逐条 `status="failed"` 并给出
   `reason`（`order not found` / `order belongs to another account` /
   `order already <STATUS>` / `duplicate order_id in request` / 市场不符），
   其余项标记 `skipped` + `reason="batch_aborted"`。
   全部合法时才逐单撤销，`applied=true`。
2. **范围模式**（不给 `order_ids`）：撤销该账户当前仍有效的全部挂单
   （可用 `market` 限定单个市场，否则全部市场）。范围内已结束的挂单不视为失败，
   逐条 `status="skipped"` 并注明 `order already <STATUS>`。

响应字段：`applied`、`cancelled`/`skipped`/`failed` 计数、`results[]`
（撤成功的带 `asset` 与 `released` 金额）。批量撤单支持 `idempotency_key`：
重复提交同一批（内容指纹一致）返回**原结果**，不重复释放资金；
同 key 不同内容返回 `IDEMPOTENCY_CONFLICT`。幂等记录持久化，重启后仍有效。

## 全量对账

`GET /admin/reconcile`（需 `X-Admin-Token`）。**只读**，不修改任何状态，
也不依赖参考价/市场状态（暂停、参考价缺失时照常可用）。判定标准：

1. **资产守恒**：每种资产 `Σ balance == credited`（累计注资额）；
2. **冻结完整**：每 `(账户, 资产)` `frozen ≥ 0`、`balance ≥ 0`、`frozen ≤ balance`；
3. **占用对账**：每 `(账户, 资产)` 的 `frozen` 恰好等于该账户所有未结束挂单
   在该资产上的剩余预留之和。

响应：`ok`、`checked_accounts`、`checked_assets`、`discrepancies[]`。
每条不一致都指明 `kind`（`ASSET_NOT_CONSERVED` / `FROZEN_MISMATCH` /
`NEGATIVE_FROZEN` / `NEGATIVE_BALANCE` / `FROZEN_EXCEEDS_BALANCE`）、
`account`（守恒类为 `null`）、`asset`、`expected`、`actual`。
正常下单、部分成交、撤单、暂停、参考价异常与服务重启之后均应得到 `ok=true`。

## 幂等

- 下单/撤单可携带 `idempotency_key`。服务端对 key 保存**请求内容指纹 + 原始响应**。
- 同 key 且内容（市场、账户、方向、价格、数量，或撤单的 order_id/account）完全一致：
  直接返回**原结果**，不重复下单、不重复冻结/扣款，成交后重放仍返回原成交结果。
- 同 key 但内容不一致：拒绝，返回 `IDEMPOTENCY_CONFLICT`。幂等记录持久化，重启后继续有效。
- 不携带 key 的请求各自独立，不做去重。

## 错误分类（HTTP 400，JSON `{"error": <code>, "detail": ...}`）

| code | 触发条件 |
| --- | --- |
| `INVALID_QUANTITY` | 数量 ≤ 0 或不是最小步长 lot_size 的整数倍 |
| `INVALID_PRICE` | 价格 ≤ 0、非有限数或不是 tick_size 的整数倍 |
| `UNKNOWN_MARKET` | 市场不存在 |
| `UNKNOWN_ACCOUNT` | 账户不存在（保留） |
| `INSUFFICIENT_BALANCE` | 可用余额（balance−frozen）不足以支付下单冻结 |
| `INSUFFICIENT_FROZEN` | 撤单/结算时发现冻结额度异常（内部不一致的可区分告警） |
| `ORDER_NOT_ACTIVE` | 订单不存在、属于他人、或已成交/已撤，不可再操作 |
| `IDEMPOTENCY_CONFLICT` | 幂等键被不同请求内容复用 |
| `MARKET_HALTED` | 市场暂停期间提交，订单挂簿但不撮合 |
| `REFERENCE_UNAVAILABLE` | 参考价缺失 |
| `REFERENCE_STALE` | 参考价过期 |
| `PRICE_OUT_OF_BAND` | 价格偏离超阈值（判定依据；停止撮合，订单仍挂簿） |
| `UNAUTHORIZED` | 管理接口缺少/错误的 `X-Admin-Token` |
| `INVALID_RISK_PARAMS` | 风控参数非法（负数等） |

> 说明：被风控/暂停拦截的订单不会作为 4xx 拒绝（下单本身合法且冻结成功），
> 而是 200 返回订单，字段 `blocked_reason` 标明未撮合的可判定原因。

## HTTP 接口

### 交易（无需鉴权）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/orders` | 下限价单；body：`market, account, side(BUY/SELL), price, quantity, idempotency_key?` |
| POST | `/orders/cancel` | 撤单；body：`order_id, account, idempotency_key?` |
| POST | `/orders/batch-cancel` | 批量撤单；body：`account, market?, order_ids?, idempotency_key?`（见「批量撤单」） |
| GET | `/orders/{order_id}` | 查询订单（含状态、已成交、剩余数量） |
| GET | `/accounts/{account}/balances/{asset}` | 查询余额/冻结/可用 |
| GET | `/accounts/{account}/locks?market=` | 资金占用明细：逐笔挂单占用 + 账户资产汇总（见「资金占用明细」） |
| GET | `/markets?market=` | 行情快照（买卖盘、暂停状态、参考价、风控参数） |

订单状态：`NEW`、`PARTIALLY_FILLED`、`FILLED`、`CANCELLED`（拒绝不建单，直接错误响应）。

### 管理（需要请求头 `X-Admin-Token`，默认 `admin-token`，测试中为 `secret`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/credit` | 本地账本给账户注资：`account, asset, amount` |
| POST | `/admin/reference-price` | 推送/更新参考价：`market, price` |
| POST | `/admin/risk-params` | 调整风控：`market, max_deviation_bps, reference_max_age_ms` |
| POST | `/admin/halt` | 暂停/恢复：`market, halted(true/false)` |
| GET | `/admin/reconcile` | 全量对账（只读）：资产守恒 + 占用对账（见「全量对账」） |

## 本地验证示例

```bash
TOKEN="admin-token"
curl -s localhost:8000/admin/credit -H "X-Admin-Token: $TOKEN" \
  -d '{"account":"alice","asset":"USDC","amount":"10000"}'
curl -s localhost:8000/admin/credit -H "X-Admin-Token: $TOKEN" \
  -d '{"account":"bob","asset":"BTC","amount":"2"}'
curl -s localhost:8000/admin/reference-price -H "X-Admin-Token: $TOKEN" \
  -d '{"market":"BTC-USDC","price":"100"}'

curl -s localhost:8000/orders -d '{"market":"BTC-USDC","account":"bob",
  "side":"SELL","price":"100","quantity":"1"}'
curl -s localhost:8000/orders -d '{"market":"BTC-USDC","account":"alice",
  "side":"BUY","price":"100","quantity":"0.5","idempotency_key":"demo-1"}'
curl -s "localhost:8000/markets?market=BTC-USDC"

# 新能力：占用明细 / 批量撤单 / 全量对账
curl -s "localhost:8000/accounts/bob/locks"
curl -s localhost:8000/orders/batch-cancel \
  -d '{"account":"bob","market":"BTC-USDC","idempotency_key":"bc-demo-1"}'
curl -s localhost:8000/admin/reconcile -H "X-Admin-Token: $TOKEN"
```

重启进程后再次查询，余额、未成交剩余、幂等结果与暂停/风控设置均与重启前一致。

## 兼容性

本轮新增能力（占用明细、批量撤单、全量对账）均为**新增接口**，未改动任何既有接口的
路径、请求/响应字段与语义；唯一的内部行为修正是：买单成交价优于限价时，多预留的差额
从「永久滞留 frozen」改为「成交当场解冻回可用额度」——这正是占用明细/对账要求的
`frozen == Σ 未结束挂单占用` 不变式，既有用例已同步更新并全部通过。

## 测试覆盖

`uv run pytest -s` 覆盖以下场景，且断言失败原因可区分、成交前后资产守恒；
日志（`dex.service` / `dex.matching`）打印关键输入、冻结/结算金额、订单状态迁移与风控判定依据：

- 非法数量/价格、未知市场、余额不足、冻结不足等错误码区分
- 部分成交后剩余订单继续有效；多档深度吃单；价格优先与同价 FIFO
- 自成交跳过且双方订单保持有效；不可越限价成交
- 幂等键同内容重放 / 不同内容拒绝（逐字段参数化）/ 撤单幂等
- 10 账户并发抢购 1 BTC、5 账户并发吃单：无超额成交、无重复扣款、无负余额
- 暂停期间不产生新成交、恢复后恢复；参考价缺失 / 过期 / 偏离阈值拦截
- 管理接口鉴权；重启后订单、余额、冻结、成交、幂等、暂停与风控参数一致恢复
- 占用明细：逐笔占用与 frozen 精确对齐、市场过滤、成交价优于限价后差额立即解冻、
  部分成交+撤单后不再占用任何资金
- 批量撤单：范围模式（全市场/单市场、已结束单跳过）、显式模式原子性
  （不存在/他人/已结束/重复项整批不生效且指明问题项）、幂等重放不重复释放、
  200 张挂单与并发下单/成交交错后的资产守恒与 frozen==占用
- 全量对账：正常活动后 ok、暂停/参考价缺失下可用、注入冻结/守恒破坏后
  能定位账户/资产/期望/实际、只读性、重启后结论一致

## 目录结构

```
app/
  models.py    # 枚举、领域数据结构、API 请求模型
  errors.py    # 带可区分错误码的业务异常
  storage.py   # JSON 账本原子持久化（临时文件 + rename，Decimal 保精度）
  matching.py  # 价格/时间优先撮合 + 原子结算（堆式订单簿，重启可重建）
  service.py   # 业务编排：校验、冻结、风控闸门、幂等、暂停、锁与持久化
  api.py       # FastAPI 路由与统一错误响应
main.py        # 启动入口
tests/         # pytest 用例（校验/撮合/幂等/风控/并发/持久化/HTTP）
```
