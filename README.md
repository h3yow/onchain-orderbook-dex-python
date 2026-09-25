# 本地链上订单簿 DEX：撮合与结算服务

用本地账本模拟链上资产账户与市场行情的订单簿 DEX，提供限价单撮合、原子结算、
幂等下单、风控与暂停恢复能力。不依赖外部链、真实凭据或付费服务。

## 运行方式

```bash
uv sync                 # 安装依赖
uv run python main.py   # 启动服务（默认 127.0.0.1，随机端口）
uv run pytest -q        # 运行测试
```

环境变量：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEX_DATA_PATH` | `dex_state.json` | 本地状态快照文件路径 |
| `DEX_ADMIN_TOKEN` | `admin-secret` | 管理接口令牌 |

## 接口语义

### 账户（本地账本，模拟链上资产）

- `POST /accounts/deposit` `{account_id, asset, amount}` — 充值（资产总量的唯一入口）
- `GET /accounts/{account_id}` — 查询各资产 `available` / `frozen` 余额

### 交易

- `POST /orders` `{account_id, market, side, price, quantity, idempotency_key}` — 限价下单。
  返回订单视图（含 `status`、`filled_quantity`、`remaining`）及本次成交列表。
- `POST /orders/{order_id}/cancel` `{account_id}` — 撤单，解冻剩余额度。
- `GET /orders/{order_id}` — 查询订单。
- `GET /markets/{market}/book?levels=10` — 订单簿深度。
- `GET /markets/{market}/trades` — 成交记录。

内置市场：`BTC-USD`、`ETH-USD`。

### 管理（需请求头 `X-Admin-Token`）

- `POST /admin/pause` / `POST /admin/resume` — 暂停/恢复撮合。
- `POST /admin/risk-params` `{market, max_deviation_bps?, reference_ttl_seconds?, reference_price?}`
  — 调整风险参数、上报参考价。

## 撮合与结算规则

- **价格优先、时间优先**：taker 按对手方最优价逐档成交，同价位按挂单先后（`seq`）。
- **成交价 = maker 价**；taker 买单按自身限价冻结 quote，价差在结算时退回。
- **部分成交**后剩余数量保留在簿中，状态为 `PARTIALLY_FILLED`，可继续成交或撤销。
- **自成交防护**：撮合时跳过同账户的 maker 订单（保留在簿中，不与自身成交）。
- **原子结算**：每笔成交先校验双方冻结额度再修改余额；买单冻结 `price*qty` 的
  quote，卖单冻结 `qty` 的 base；成交时买方付 quote 得 base、卖方付 base 得 quote。
- **并发安全**：所有状态变更在同一把锁内完成，并发提交不会超额成交、重复扣款、
  出现负余额或破坏资产总量守恒（每次变更后校验不变量）。

## 幂等规则

- 下单必须携带 `idempotency_key`。
- 相同 key + **完全相同**的请求内容（账户/市场/方向/价格/数量）：直接复用首次结果，
  不重复冻结、不重复撮合（包括并发与重启后的重放）。
- 相同 key + 不同内容：拒绝，返回 `IDEMPOTENCY_CONFLICT`。
- 失败的请求不占用幂等键，修正后可重试。

## 错误分类

| 错误码 | HTTP | 含义 |
|---|---|---|
| `INVALID_QUANTITY` | 400 | 数量非法（非正数/非数字/方向非法） |
| `INVALID_PRICE` | 400 | 价格非法（非正数） |
| `UNKNOWN_MARKET` | 400 | 市场不存在 |
| `INSUFFICIENT_BALANCE` | 400 | 可用余额不足以冻结 |
| `INSUFFICIENT_FROZEN` | 400 | 冻结额度不足（结算/解冻时） |
| `ORDER_NOT_FOUND` | 404 | 订单不存在 |
| `ORDER_NOT_CANCELLABLE` | 400 | 订单已终结，不能撤销 |
| `FORBIDDEN` | 403 | 越权（他人订单/错误管理令牌） |
| `UNAUTHORIZED` | 401 | 缺少管理令牌 |
| `MARKET_PAUSED` | 409 | 已暂停，禁止撮合 |
| `REFERENCE_PRICE_MISSING` | 409 | 参考价缺失 |
| `REFERENCE_PRICE_STALE` | 409 | 参考价过期（超过 TTL） |
| `PRICE_DEVIATION_EXCEEDED` | 400 | 价格偏离参考价超过阈值 |
| `IDEMPOTENCY_CONFLICT` | 409 | 幂等键被不同内容复用 |

错误响应格式：`{"error": {"code": "...", "message": "..."}}`。

## 风险控制与暂停恢复

- **暂停/恢复**：仅管理员可调用。暂停期间所有新下单被拒绝（`MARKET_PAUSED`），
  不产生新成交；撤单仍允许（只释放冻结，不新增成交）。暂停状态持久化，重启后保持。
- **参考价风控**：下单价相对参考价偏离超过 `max_deviation_bps`（默认 1000bps=10%）
  时拒绝；参考价缺失或超过 `reference_ttl_seconds`（默认 300s）未更新时同样拒绝。
- **参数调整**：偏离阈值与 TTL 可由管理员在线调整并持久化。

## 持久化与重启恢复

每次状态变更（充值/下单/成交/撤单/风控调整）后，将账本、全部订单、成交记录、
幂等结果与风控配置以 JSON 快照原子写入（tmp + rename）`DEX_DATA_PATH`。
重启时从快照恢复：存活订单重建订单簿，余额、幂等结果与暂停状态保持一致，
原有可用能力不受影响。

## 本地验证

```bash
uv run pytest -q          # 全部测试
uv run pytest -q -s       # 查看关键输入、订单状态迁移与判定依据日志
```

测试覆盖：部分成交、价格/时间优先、自成交跳过、撤单解冻、非法参数与余额不足、
幂等重放与冲突、并发下单/撤单（无超额成交且资产守恒）、暂停期间撮合与恢复、
参考价缺失/过期/偏离、管理鉴权、重启后订单/余额/幂等/暂停状态恢复。
