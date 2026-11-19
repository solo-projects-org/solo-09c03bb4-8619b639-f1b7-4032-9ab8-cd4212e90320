# 逐笔成交供数 API（FastAPI + SQLite）

盘中风控断线恢复场景：接收交易所逐笔消息（成交 / 更正 / 撤销），按分钟聚合成 K 线柱，
风控侧首次全量快照 + 游标，之后用游标续拉变动分钟，保证不重、不漏、不看到半提交状态。

## 快速开始

```bash
docker compose up --build
# 打开 Swagger UI: http://localhost:8000/docs
```

在 Swagger 上可直接 `POST /v1/trades` 提交逐笔消息、用 `/v1/symbols/{symbol}/bars`
拿快照和游标、再用 `/v1/symbols/{symbol}/changes?cursor=...` 续拉变动；
日志增长后用 `POST /v1/symbols/{symbol}/journal/reclaim` 按股票回收。

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
| POST | `/v1/symbols/{symbol}/journal/reclaim` | 按股票回收变更日志，只保留最近 `keep` 条（`keep=0` 清空） |
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

**日志回收（按股票）**

* `POST /v1/symbols/{symbol}/journal/reclaim`，请求体 `{"keep": N}`：删除该股票
  除最近 N 条以外的全部变更日志（N=0 即清空该股票日志），返回删除条数、该股票
  回收水位 `reclaim_watermark` 与全局水位 `global_high_water`
* 回收只动**该股票**的日志行和它自己的水位；其他股票的日志与游标完全不受影响，
  不会被误报过期
* 游标 **严格小于**该股票回收水位的续拉请求返回 **410 Gone**，提示日志已回收、
  需重新快照；游标等于水位（从最后删除点之后继续）仍然有效，仍能收到后续的
  更正和删除
* 回收水位持久化在 `reclaim_watermarks` 表，**服务重启后依然识别过期游标**；
  回收接口可重复调用，水位只增不减
* 全局水位存于 `meta` 表（历史已提交最大 seq）：即使全部日志被删空，快照仍返回
  已提交的全局水位；journal 的 `AUTOINCREMENT` 保证新成交继续使用**递增且不复用**
  的序号
* 回收在单个 `BEGIN IMMEDIATE` 事务内完成"删日志 + 更新水位"，任一步失败整体
  回滚，原日志与原水位都保留；读路径（快照/续拉）在单事务里同时读日志和两个水位，
  与逐笔提交、回收交错时依靠 WAL 快照隔离看到一致的日志与水位
* 回收只清理变更日志，`trades` 与 `minute_bars`（快照数据）不受影响

**一致性**：每次写入把"逐笔状态 + 分钟柱重算 + 变更日志"放进**同一个 SQLite 事务**
（`BEGIN IMMEDIATE`），失败整体回滚；读取只在单事务里读已提交数据；
WAL + `synchronous=FULL`，进程重启后已提交游标不会丢、不会跳号、不留半更新。

## 自测

```bash
.venv/bin/pip install pytest httpx
python -m pytest tests/ -v        # 32 个关键场景用例（含 12 个日志回收场景）
```

覆盖：OHLCV 口径与同毫秒按交易号决胜、幂等重送、同修订冲突 409、迟到旧修订不回退、
跳号受理、同分钟/跨分钟更正（两分钟交付）、最后一笔撤销的删除标记、游标续拉流程、
游标超前 409、**重启后状态与游标连续**、**写入中途失败整体回滚**（monkeypatch 注入故障）、
参数校验、未知逐笔撤销墓碑，以及日志回收：按条数保留与水位推进、keep=0 清空、
过期游标 410 后重新快照恢复、有效游标继续收到更正、**跨股票不误报过期**、
**清空后快照仍返回全局水位且新序号递增不复用**、回收幂等与水位单调、
**重启后仍识别过期游标**、**回收失败回滚保留原日志和水位**、
WAL 快照隔离下回收与续拉交错的一致性、老版本数据库平滑升级。

## 限制与待办

* SQLite 单写者：容器固定单 worker；多实例水平扩展需换外部数据库（接口与事务边界不变）
* 未做鉴权/限流、批量提交接口；日志回收目前按股票手动触发，尚未做定时/自动保留策略
* 本环境无 Docker 守护进程，镜像未实际构建；Dockerfile/compose 经静态检查，
  同一入口（`uvicorn app.main:app`）已在本地用 Python 3.11 完整跑通上述场景
