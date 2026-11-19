"""FastAPI entrypoint. Run: uvicorn app.main:app --host 0.0.0.0 --port 8000"""
from __future__ import annotations

import os

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import RedirectResponse

from . import db, logic, models

DESCRIPTION = """逐笔成交供数 API。

* `POST /v1/trades` 受理逐笔消息（成交/更正/撤销，按 股票代码+交易号+递增修订号）
* `GET /v1/symbols/{symbol}/bars` 风控首次查询：全量分钟柱 + 游标
* `GET /v1/symbols/{symbol}/changes?cursor=N` 之后用游标续拉变动分钟
  （跨分钟更正会交付两个分钟的变化；最后一笔撤销返回 delete 删除标记）
"""


def create_app(db_path: str) -> FastAPI:
    db.init_db(db_path)
    app = FastAPI(title="逐笔成交供数 API", version="1.0.0", description=DESCRIPTION)

    def get_conn():
        conn = db.connect(db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/docs")

    @app.get("/health", include_in_schema=False)
    def health():
        return {"status": "ok"}

    @app.post(
        "/v1/trades",
        response_model=models.IngestResult,
        responses={409: {"description": "同修订号但内容冲突"}},
        summary="受理一条逐笔消息（成交/更正/撤销）",
    )
    def submit_trade(msg: models.TradeMessage, conn=Depends(get_conn)):
        try:
            result = logic.apply_message(conn, msg)
        except logic.ConflictError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return models.IngestResult(
            symbol=msg.symbol, trade_no=msg.trade_no, revision=msg.revision, **result
        )

    @app.get(
        "/v1/trades/{symbol}/{trade_no}",
        response_model=models.TradeState,
        responses={404: {"description": "该 (symbol, trade_no) 不存在"}},
        summary="查看某笔逐笔的当前状态（调试用）",
    )
    def get_trade(symbol: str, trade_no: str, conn=Depends(get_conn)):
        state = logic.get_trade(conn, symbol, trade_no)
        if state is None:
            raise HTTPException(status_code=404, detail="trade not found")
        return state

    @app.get(
        "/v1/symbols/{symbol}/bars",
        response_model=models.BarsSnapshot,
        summary="风控首次查询：全量分钟柱 + 游标",
    )
    def get_bars(symbol: str, conn=Depends(get_conn)):
        return logic.snapshot_bars(conn, symbol)

    @app.get(
        "/v1/symbols/{symbol}/changes",
        response_model=models.ChangesPage,
        responses={409: {"description": "游标超过已提交水位，需要重新快照"}},
        summary="用游标续拉变动分钟",
    )
    def get_changes(
        symbol: str,
        cursor: int = Query(ge=0, description="上次拿到的游标"),
        limit: int = Query(default=500, ge=1, le=5000),
        conn=Depends(get_conn),
    ):
        try:
            return logic.pull_changes(conn, symbol, cursor, limit)
        except logic.CursorAheadError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    return app


app = create_app(os.environ.get("FEED_DB_PATH", "./feed.db"))
