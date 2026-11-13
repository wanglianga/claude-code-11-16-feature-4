import os
import sys
from pathlib import Path

# 必须在导入 app 之前指定独立测试库
os.environ["DATABASE_URL"] = (
    "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/charging_settlement_test"
)
os.environ.setdefault("JWT_SECRET", "test-secret")

import psycopg2  # noqa: E402
from psycopg2 import sql  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))


def _reset_test_db():
    conn = psycopg2.connect(
        host="127.0.0.1", port=5432, user="ccdev", password="ccdev", dbname="ccdev"
    )
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS charging_settlement_test")
        cur.execute("CREATE DATABASE charging_settlement_test OWNER ccdev")
    conn.close()


_reset_test_db()

from app.database import Base, SessionLocal, engine  # noqa: E402
from app import seed  # noqa: E402

Base.metadata.create_all(bind=engine)
seed.STORAGE_DIR = str(BACKEND / "data" / "test_originals")
seed.init_db()

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_database():
    """每个用例独立重建并重新播种，保证用例间互不污染。"""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    seed.init_db()
    yield
