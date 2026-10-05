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
  - 买方：`quote: balance -= cost`（实际成交额），`frozen -= 买单限价 × 成交数量`（该笔的预留额）；
    `base: balance += qty`。**成交价优于买单限价时，多预留的差额在结算当场释放回可用额度**，
    订单全部成交后不再占用任何资金。
  - 卖方：`base: balance -= qty, frozen -= qty`；`quote: balance += cost`
- 撤单仅释放**剩余数量**对应的冻结（买单释放 `remaining × price`，卖单释放 `remaining`）；
  已成交部分不受影响。
- 任何成交都只在账户间转移资产，系统内每种资产的 `Σ balance` 保持不变（守恒）。

## 资金占用明细

`GET /accounts/{account}/funds?market=<可选>` 返回该账户的资金占用清单：

- `locked[]`：逐笔列出每张**尚未结束**（`NEW` / `PARTIALLY_FILLED`）挂单的占用：
  `order_id, market, side, price, remaining_quantity, asset, locked_amount`
  （买单占用 `remaining × price` 的 quote，卖单占用 `remaining` 的 base）。
  已完全成交或已撤销的订单不会出现在清单中，也不再占用任何资金。
- `assets[]`：该账户每种资产的 `balance`（总余额）、`frozen`（总占用）、
  `available`（可用额度 = balance − frozen）。

清单由实时订单状态推导，与账本冻结计数器在任何时刻一一对应；
`GET /reconcile` 可对这一点做全账本校验。

## 批量撤单

两种方式，均在服务级锁内一次完成并只持久化一次，不存在"撤一半"的中间状态，
与并发下单/成交交错也不会出现负余额、重复释放或资产不守恒。

### 按显式订单号：`POST /orders/cancel-batch`

body：`account, order_ids[], idempotency_key?`。**整批原子**：

- 先校验全部条目，再统一执行。只要列表中出现**不存在、不属于该账户、已经结束
  （FILLED/CANCELLED）或重复**的订单号，整批不生效、账户资金一分不变，
  返回 `400 INVALID_BATCH`，`detail` 指明出问题的具体项（如 `item[2] order ord_x: not found`）。
- 全部合法时逐笔撤销，返回
  `{account, results: [{order_id, result: "cancelled", asset, released_amount}], cancelled_count, skipped_count: 0}`。

### 按账户/市场范围：`POST /orders/cancel-all`

body：`account, market?, idempotency_key?`。撤销该账户当前仍有效的全部挂单
（带 `market` 时只撤该市场）。范围内**已经不再有效**的挂单（如早已成交/撤销）
逐笔标记为 `skipped` 并附原因（`order already FILLED` 等），**不算失败**；
返回 `{account, market, results: [{order_id, market, result: "cancelled"|"skipped", ...}],
cancelled_count, skipped_count}`。

### 幂等

两种批量撤单都支持 `idempotency_key`：同一 key 重复提交同一批请求，直接返回**首次的原始结果**，
不重复释放资金；同 key 不同内容返回 `IDEMPOTENCY_CONFLICT`。幂等记录持久化，重启后仍有效。

## 全量对账

`GET /reconcile`（只读，不修改任何状态；不依赖参考价，市场暂停时同样可用）对全账本做三项检查：

1. **资产守恒**：每种资产全部账户 `balance` 之和 == 历史累计注资总额；
2. **占用一致**：每个账户每种资产的 `frozen` == 其所有未结束挂单占用之和；
3. **无负值**：任何 `balance` / `frozen` 不得为负。

返回 `{ok, checked_accounts, checked_assets, discrepancies[]}`。发现不一致时，
`discrepancies` 逐条给出 `kind`（`asset_conservation` / `frozen_mismatch` /
`negative_balance` / `negative_frozen`）、`account`（如适用）、`asset`、`expected`、`actual`。

## 与既有能力的兼容性

本层能力只新增接口与修正"成交价优于限价时多预留差额立即释放"的结算行为，
不改变已有接口的语义：下单/撤单/查询的请求与响应字段、错误码、幂等口径、风控与暂停行为均保持不变；
旧账本文件加载时自动迁移（按余额总额重建注资基线），重启后对账结论依然正确。

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
| `INVALID_BATCH` | 批量撤单列表含不存在/他人/已结束/重复的订单号，或为空；整批不生效 |

> 说明：被风控/暂停拦截的订单不会作为 4xx 拒绝（下单本身合法且冻结成功），
> 而是 200 返回订单，字段 `blocked_reason` 标明未撮合的可判定原因。

## HTTP 接口

### 交易（无需鉴权）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/orders` | 下限价单；body：`market, account, side(BUY/SELL), price, quantity, idempotency_key?` |
| POST | `/orders/cancel` | 撤单；body：`order_id, account, idempotency_key?` |
| POST | `/orders/cancel-batch` | 按订单号批量撤单（整批原子）；body：`account, order_ids[], idempotency_key?` |
| POST | `/orders/cancel-all` | 按账户（可选市场）一键撤单；body：`account, market?, idempotency_key?` |
| GET | `/orders/{order_id}` | 查询订单（含状态、已成交、剩余数量） |
| GET | `/accounts/{account}/balances/{asset}` | 查询余额/冻结/可用 |
| GET | `/accounts/{account}/funds?market=` | 资金占用明细（逐笔挂单占用 + 各资产总额/占用/可用） |
| GET | `/reconcile` | 全量对账（只读）：守恒、占用一致、无负值 |
| GET | `/markets?market=` | 行情快照（买卖盘、暂停状态、参考价、风控参数） |

订单状态：`NEW`、`PARTIALLY_FILLED`、`FILLED`、`CANCELLED`（拒绝不建单，直接错误响应）。

### 管理（需要请求头 `X-Admin-Token`，默认 `admin-token`，测试中为 `secret`）

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/admin/credit` | 本地账本给账户注资：`account, asset, amount` |
| POST | `/admin/reference-price` | 推送/更新参考价：`market, price` |
| POST | `/admin/risk-params` | 调整风控：`market, max_deviation_bps, reference_max_age_ms` |
| POST | `/admin/halt` | 暂停/恢复：`market, halted(true/false)` |

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

# 新能力：资金占用明细 / 批量撤单 / 全量对账
curl -s "localhost:8000/accounts/alice/funds"
curl -s localhost:8000/orders/cancel-batch \
  -d '{"account":"alice","order_ids":["ord_xxx","ord_yyy"],"idempotency_key":"bc-1"}'
curl -s localhost:8000/orders/cancel-all \
  -d '{"account":"alice","market":"BTC-USDC"}'
curl -s localhost:8000/reconcile
```

重启进程后再次查询，余额、未成交剩余、幂等结果与暂停/风控设置均与重启前一致。

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
- 资金占用明细：逐笔挂单占用与资产总额对得上；成交价优于限价后多预留差额立即释放；
  已成交/已撤订单不再占用任何资金
- 批量撤单：非法项（不存在/他人/已结束/重复，参数化）整批拒绝且资金不变；
  幂等重放返回原结果不重复释放；范围撤单跳过失效单并说明；300 单一调用处理；
  撤单与并发下单/成交交错后无负余额、守恒且对账通过
- 全量对账：正常流程/暂停/参考价缺失/重启后均一致；对冻结不一致、守恒破坏、
  负余额能定位账户/资产/期望值/实际值；对账本身只读

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
tests/         # pytest 用例（校验/撮合/幂等/风控/并发/持久化/HTTP/资金明细/批量撤单/对账）
```
