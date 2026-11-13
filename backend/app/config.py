from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = (
        "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/charging_settlement"
    )
    jwt_secret: str = "dev-secret-change-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 720
    # 金额/电量统一精度：金额保留 4 位小数计算、2 位展示；电量保留 4 位
    money_scale: int = 4
    money_display_scale: int = 2
    energy_scale: int = 4
    # 未解释差额超出该比例（相对总表电量）时账单暂停确认
    default_diff_threshold_pct: str = "0.02"


@lru_cache
def get_settings() -> Settings:
    return Settings()
