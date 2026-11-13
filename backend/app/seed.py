"""初始化表结构、账号与站点，并导入样例计量文件。"""
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select

from .database import Base, SessionLocal, engine
from . import models
from .security import hash_password
from .services.importer import ImportError_, import_file

SAMPLE_DIR = Path(__file__).parent.parent / "sample_data"
STORAGE_DIR = "data/originals"

USERS = [
    ("finance", "Finance@123", "平台财务", models.ROLE_FINANCE, []),
    ("operator", "Operator@123", "运营专员", models.ROLE_OPERATOR, []),
    ("property_sh", "Property@123", "上海物业", models.ROLE_PROPERTY, ["S01"]),
    ("property_xj", "Property@123", "乌鲁木齐物业", models.ROLE_PROPERTY, ["S02"]),
]

IMPORT_PLAN = {
    "S01": [
        ("tariff", "tariffs.csv"),
        ("master_reading", "master_readings.csv"),
        ("device_reading", "device_readings.csv"),
        ("public_load", "public_loads.csv"),
        ("order", "orders_v1.csv"),
        ("order", "orders_v2_backfill.csv"),
    ],
    "S02": [
        ("tariff", "tariffs.csv"),
        ("master_reading", "master_readings.csv"),
        ("device_reading", "device_readings.csv"),
        ("replacement", "replacements.csv"),
        ("public_load", "public_loads.csv"),
        ("order", "orders.csv"),
    ],
}


def init_db():
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        if db.scalar(select(models.Site).limit(1)):
            print("种子数据已存在，跳过")
            return
        sites = {
            "S01": models.Site(code="S01", name="上海虹桥充电站", timezone="Asia/Shanghai",
                               diff_threshold_pct=Decimal("0.02")),
            "S02": models.Site(code="S02", name="乌鲁木齐经开充电站", timezone="Asia/Urumqi",
                               diff_threshold_pct=Decimal("0.02")),
        }
        db.add_all(sites.values())
        db.flush()
        for username, pwd, display, role, site_codes in USERS:
            user = models.User(
                username=username, password_hash=hash_password(pwd),
                display_name=display, role=role,
            )
            db.add(user)
            db.flush()
            for code in site_codes:
                db.add(models.UserSite(user_id=user.id, site_id=sites[code].id))
        db.flush()

        for code, plan in IMPORT_PLAN.items():
            site = sites[code]
            for source_type, filename in plan:
                path = SAMPLE_DIR / code / filename
                content = path.read_bytes()
                result = import_file(
                    db, site, source_type, filename, content, "seed", STORAGE_DIR
                )
                print(f"[{code}] {filename}: {result['summary']}")
        db.commit()
        print("种子数据初始化完成")
    except (Exception, ImportError_):
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    init_db()
