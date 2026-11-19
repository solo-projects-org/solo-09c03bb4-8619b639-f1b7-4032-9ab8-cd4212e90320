# 逐笔成交供数 API（FastAPI + SQLite）

盘中风控断线恢复场景：接收交易所逐笔消息（成交 / 更正 / 撤销），按分钟聚合成 K 线柱，
风控侧首次全量快照 + 游标，之后用游标续拉变动分钟，保证不重、不漏、不看到半提交状态。

## 快速开始

```bash
docker compose up --build
# 打开 Swagger UI: http://localhost:8000/docs
```

在 Swagger 上可直接 `POST /v1/trades` 提交逐笔消息、用 `/v1/symbols/{symbol}/bars`
拿快照和游标、再用 `/v1/symbols/{symbol}/changes?cursor=...` 续拉变动。

本地开发（无 Docker）：

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000          # 默认库文件 ./feed.db，可用 FEED_DB_PATH 覆盖
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/trades` | 受理一条逐笔消息（成交/更正/撤销） |
| GET | `/v1/symbols/{symbol}/bars` | 风控首次查询：该股票全部分钟柱 + 游标 |
| GET | `/v1/symbols/{symbol}/changes?cursor=N&limit=500` | 用游标续拉变动分钟 |
| GET | `/v1/trades/{symbol}/{trade_no}` | 查看某笔逐笔当前状态（调试） |

逐笔消息：

```json
{
  "symbol": "600519", "trade_no": "000123", "revision": 1,
  "op": "trade",                       // trade | correction | cancel
  "trade_time_utc_ms": 1700000000000,  // 成交时刻，UTC 毫秒
  "price_cents": 168500,               // 价格，整数，单位分
  "qty": 100                           // 股数
}
```

## 语义规则

**修订号（按 股票代码+交易号 维度）**

* `revision > 已存`：受理（允许跳号）；`cancel` 打撤销标记，`trade/correction` 覆盖内容
* `revision == 已存` 且内容相同：`duplicate`，幂等成功，不产生任何变更
* `revision == 已存` 但内容不同：**409 拒绝**
* `revision < 已存`：`stale_ignored`，迟到旧修订只忽略、**绝不回退**新状态
* 对未知逐笔的 `cancel` 记为墓碑，防止迟到的旧成交"复活"

**分钟柱口径**：开/收盘价 = 该分钟按 `(成交时刻, 交易号)` 排序的第一/最后一笔价格
（纯数字交易号按数值比较）；高/低 = 最大/最小价；另含总股数与笔数。

**游标续拉**

* 快照返回的 `cursor` 与返回的分钟柱是同一读事务里的一致点
* 跨分钟更正在同一事务里交付**两个分钟**的变化（旧分钟更新或删除 + 新分钟上线）
* 某分钟最后一笔被撤销/改走时，交付 `type=delete`、`bar=null` 的**删除标记**
* 游标超过已提交水位返回 409，提示重新快照

**一致性**：每次写入把"逐笔状态 + 分钟柱重算 + 变更日志"放进**同一个 SQLite 事务**
（`BEGIN IMMEDIATE`），失败整体回滚；读取只在单事务里读已提交数据；
WAL + `synchronous=FULL`，进程重启后已提交游标不会丢、不会跳号、不留半更新。

## 自测

```bash
.venv/bin/pip install pytest httpx
python -m pytest tests/ -v        # 20 个关键场景用例
```

覆盖：OHLCV 口径与同毫秒按交易号决胜、幂等重送、同修订冲突 409、迟到旧修订不回退、
跳号受理、同分钟/跨分钟更正（两分钟交付）、最后一笔撤销的删除标记、游标续拉流程、
游标超前 409、**重启后状态与游标连续**、**写入中途失败整体回滚**（monkeypatch 注入故障）、
参数校验、未知逐笔撤销墓碑。

## 限制与待办

* SQLite 单写者：容器固定单 worker；多实例水平扩展需换外部数据库（接口与事务边界不变）
* 未做鉴权/限流、数据保留期清理、批量提交接口
* 本环境无 Docker 守护进程，镜像未实际构建；Dockerfile/compose 经静态检查，
  同一入口（`uvicorn app.main:app`）已在本地用 venv 完整跑通上述场景
