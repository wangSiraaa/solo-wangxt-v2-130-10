from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LEVEL_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg2://level:level@localhost:5432/leveling"
    redis_url: str = "redis://localhost:6379/0"
    algorithm_signature: str = "weighted-ls-v1"
    illconditioned_condition_number: float = Field(default=1.0e12)
    dense_qr_max_rows: int = Field(default=20_000)
    qr_rank_tol: float = Field(default=1.0e-9)
    closure_max_cycles: int = Field(default=500)


@lru_cache
def get_settings() -> Settings:
    return Settings()
