from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    excel_password: str = os.getenv("EXCEL_PASSWORD", "0000")
    output_excel_password: str = os.getenv("OUTPUT_EXCEL_PASSWORD", "")

    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./local.db")

    # Render에서는 컬리 API를 직접 호출하지 않고 Mac relay만 호출합니다.
    kurly_relay_url: str = os.getenv("KURLY_RELAY_URL", "").rstrip("/")
    kurly_relay_secret: str = os.getenv("KURLY_RELAY_SECRET", "")
    kurly_relay_timeout_seconds: int = max(5, _int("KURLY_RELAY_TIMEOUT_SECONDS", 90))
    kurly_relay_batch_size: int = max(1, min(_int("KURLY_RELAY_BATCH_SIZE", 300), 1000))

    # 네이버 API도 Render에서 직접 호출하지 않고 기존 Mac relay를 통해 호출합니다.
    naver_relay_url: str = os.getenv("NAVER_RELAY_URL", "").rstrip("/")
    naver_relay_token: str = os.getenv("NAVER_RELAY_TOKEN", "")
    naver_relay_timeout_seconds: int = max(5, _int("NAVER_RELAY_TIMEOUT_SECONDS", 90))

    runtime_dir: str = os.getenv("RUNTIME_DIR", "/tmp/kurly_naver_shipping_tool")


settings = Settings()
