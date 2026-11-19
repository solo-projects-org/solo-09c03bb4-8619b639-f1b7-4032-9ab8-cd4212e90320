"""Pydantic request/response models (also drive the Swagger schema)."""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

INT64_MAX = 9_223_372_036_854_775_807

Op = Literal["trade", "correction", "cancel"]


class TradeMessage(BaseModel):
    """One exchange message: a trade, a correction, or a cancellation.

    Keyed by (symbol, trade_no); `revision` must increase per key.
    For op=trade/correction the payload fields are required; for op=cancel
    they are ignored.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "symbol": "600519",
                    "trade_no": "000123",
                    "revision": 1,
                    "op": "trade",
                    "trade_time_utc_ms": 1700000000000,
                    "price_cents": 168500,
                    "qty": 100,
                }
            ]
        }
    )

    symbol: str = Field(min_length=1, max_length=32, description="股票代码")
    trade_no: str = Field(min_length=1, max_length=64, description="交易号（交易所逐笔编号）")
    revision: int = Field(ge=1, le=INT64_MAX, description="修订号，同一 (symbol, trade_no) 上递增")
    op: Op = Field(description="trade=成交, correction=更正, cancel=撤销")
    trade_time_utc_ms: Optional[int] = Field(default=None, ge=0, le=INT64_MAX, description="成交时刻，UTC 毫秒")
    price_cents: Optional[int] = Field(default=None, gt=0, le=INT64_MAX, description="价格，整数，单位为分")
    qty: Optional[int] = Field(default=None, gt=0, le=INT64_MAX, description="成交股数")

    @model_validator(mode="after")
    def _payload_required(self) -> "TradeMessage":
        if self.op in ("trade", "correction"):
            missing = [
                name
                for name in ("trade_time_utc_ms", "price_cents", "qty")
                if getattr(self, name) is None
            ]
            if missing:
                raise ValueError(f"op={self.op} 缺少字段: {', '.join(missing)}")
        return self


class IngestResult(BaseModel):
    status: Literal["applied", "duplicate", "stale_ignored"] = Field(
        description="applied=已受理; duplicate=同修订同内容重送，幂等忽略; stale_ignored=迟到旧修订，已忽略"
    )
    symbol: str
    trade_no: str
    revision: int = Field(description="本条消息的修订号")
    current_revision: int = Field(description="该 (symbol, trade_no) 当前存储的修订号")
    affected_minutes: list[int] = Field(description="本次写入产生变更日志的分钟（UTC 毫秒分钟起点）")


class Bar(BaseModel):
    minute_start_utc_ms: int = Field(description="分钟起点，UTC 毫秒")
    open_cents: int = Field(description="开盘价（分），按 (成交时刻, 交易号) 第一笔")
    high_cents: int = Field(description="最高价（分）")
    low_cents: int = Field(description="最低价（分）")
    close_cents: int = Field(description="收盘价（分），按 (成交时刻, 交易号) 最后一笔")
    volume: int = Field(description="总股数")
    trade_count: int = Field(description="笔数")


class BarsSnapshot(BaseModel):
    symbol: str
    cursor: int = Field(description="游标：快照一致点的 journal 最大 seq，后续用 /changes?cursor= 续拉")
    bars: list[Bar]


class MinuteChange(BaseModel):
    seq: int
    minute_start_utc_ms: int
    type: Literal["upsert", "delete"] = Field(description="upsert=分钟柱新值; delete=该分钟已无任何成交")
    bar: Optional[Bar] = Field(description="type=delete 时为 null（删除标记）")


class ChangesPage(BaseModel):
    symbol: str
    next_cursor: int = Field(description="下次续拉使用的游标")
    has_more: bool = Field(description="是否可能还有更多（本页达到 limit）")
    changes: list[MinuteChange]


class ReclaimRequest(BaseModel):
    """日志回收请求：按股票只保留最近 keep 条变更日志（keep=0 即全部清空）。"""

    keep: int = Field(
        ge=0,
        le=1_000_000_000,
        description="该股票保留的最近日志条数；0 表示清空该股票的全部日志",
    )


class ReclaimResult(BaseModel):
    symbol: str = Field(description="被回收日志的股票代码")
    keep: int = Field(description="请求保留的最近日志条数（0=全部清空）")
    deleted_count: int = Field(description="本次实际删除的日志条数")
    reclaim_watermark: int = Field(
        description="该股票当前的回收水位（已删除日志的最大 seq）；"
        "游标 <= 该水位的续拉请求会被 410 拒绝并提示重新快照"
    )
    global_high_water: int = Field(
        description="全局已提交水位（历史最大 seq），日志清空后仍保留，新成交序号在此之上继续递增"
    )


class TradeState(BaseModel):
    symbol: str
    trade_no: str
    revision: int
    op: Op
    trade_time_utc_ms: Optional[int]
    price_cents: Optional[int]
    qty: Optional[int]
    cancelled: bool
