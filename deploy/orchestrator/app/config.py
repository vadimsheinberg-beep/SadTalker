from functools import lru_cache
from pathlib import Path

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./tzoar.db"
    redis_url: str = "redis://localhost:6379/0"
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = "tzoar"
    s3_secret_key: str = "tzoar-secret"
    s3_bucket: str = "tzoar-artifacts"
    qdrant_url: str = "http://localhost:6333"

    policy_path: Path = Path("/app/policy/POLICY.yaml")

    # GPU backend: local | contabo | runpod | vast
    gpu_backend: str = "contabo"
    gpu_node_url: str = "http://gpu-node:9200"
    gpu_price_per_hour: float = 0.0

    corpus_snapshot: str = "unset"


@lru_cache
def settings() -> Settings:
    return Settings()


@lru_cache
def policy() -> dict:
    here = Path(__file__).resolve()
    candidates = [settings().policy_path, *(p / "policy" / "POLICY.yaml" for p in here.parents)]
    for path in candidates:
        if path.exists():
            return yaml.safe_load(path.read_text(encoding="utf-8"))
    raise FileNotFoundError("POLICY.yaml не найден: задайте POLICY_PATH")
