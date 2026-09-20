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

    kurly_base_url: str = os.getenv("KURLY_BASE_URL", "").rstrip("/")
    kurly_client_id: str = os.getenv("KURLY_CLIENT_ID", "")
    kurly_secret_key: str = os.getenv("KURLY_SECRET_KEY", "")
    kurly_solution_code: str = os.getenv("KURLY_SOLUTION_CODE", "")
    kurly_concurrency: int = max(1, min(_int("KURLY_CONCURRENCY", 5), 20))
    kurly_timeout_seconds: int = max(3, _int("KURLY_TIMEOUT_SECONDS", 12))
    kurly_policy_address_field: str = os.getenv("KURLY_POLICY_ADDRESS_FIELD", "address")

    naver_client_id: str = os.getenv("NAVER_CLIENT_ID", "")
    naver_client_secret: str = os.getenv("NAVER_CLIENT_SECRET", "")
    naver_token_type: str = os.getenv("NAVER_TOKEN_TYPE", "SELF").upper()
    naver_account_id: str = os.getenv("NAVER_ACCOUNT_ID", "")
    naver_base_url: str = os.getenv(
        "NAVER_BASE_URL", "https://api.commerce.naver.com/external"
    ).rstrip("/")

    runtime_dir: str = os.getenv("RUNTIME_DIR", "/tmp/kurly_naver_shipping_tool")


settings = Settings()
