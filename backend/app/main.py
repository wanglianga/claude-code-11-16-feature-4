from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .database import Base, engine
from .routers import (
    auth,
    backfill,
    bills,
    conflicts,
    entities,
    imports,
    jobs,
    sites,
    source_records,
    tasks,
)

app = FastAPI(
    title="充电站月度核算平台",
    description="总表/设备读数/订单/分时电价联动的逐段核对账单服务",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)


@app.get("/api/health")
def health():
    return {"status": "ok"}


for r in (
    auth.router, sites.router, imports.router, jobs.router, bills.router,
    tasks.router, conflicts.router, source_records.router, entities.router,
    backfill.router,
):
    app.include_router(r)
