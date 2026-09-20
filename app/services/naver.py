from __future__ import annotations

import asyncio
import base64
import time
from dataclasses import dataclass, field

import bcrypt
import httpx

from ..config import settings


@dataclass
class ConfirmResult:
    success_ids: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)


def _chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _extract_success_ids(data: dict) -> list[str]:
    body = data.get("data") or {}
    out: list[str] = []
    for key in ("successProductOrderIds", "successProductOrderInfos"):
        values = body.get(key) or []
        for item in values:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                pid = item.get("productOrderId") or item.get("id")
                if pid:
                    out.append(str(pid))
    return out


def _extract_failures(data: dict) -> list[dict]:
    body = data.get("data") or {}
    failures = []
    for item in body.get("failProductOrderInfos") or []:
        if isinstance(item, dict):
            failures.append(
                {
                    "productOrderId": str(item.get("productOrderId") or ""),
                    "code": str(item.get("code") or ""),
                    "message": str(item.get("message") or ""),
                }
            )
    return failures


class NaverClient:
    def __init__(self):
        self._token = ""
        self._token_expire_at = 0.0
        self._token_lock = asyncio.Lock()

    def configured(self) -> bool:
        return bool(settings.naver_client_id and settings.naver_client_secret)

    def _signature(self, timestamp_ms: int) -> str:
        password = f"{settings.naver_client_id}_{timestamp_ms}".encode("utf-8")
        salt = settings.naver_client_secret.encode("utf-8")
        hashed = bcrypt.hashpw(password, salt)
        return base64.b64encode(hashed).decode("utf-8")

    async def _issue_token(self, force: bool = False) -> str:
        if not self.configured():
            raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수를 설정하세요.")
        if not force and self._token and time.time() < self._token_expire_at - 120:
            return self._token

        async with self._token_lock:
            if not force and self._token and time.time() < self._token_expire_at - 120:
                return self._token
            ts = int(time.time() * 1000)
            form = {
                "client_id": settings.naver_client_id,
                "timestamp": str(ts),
                "client_secret_sign": self._signature(ts),
                "grant_type": "client_credentials",
                "type": settings.naver_token_type,
            }
            if settings.naver_token_type == "SELLER":
                if not settings.naver_account_id:
                    raise RuntimeError("NAVER_TOKEN_TYPE=SELLER이면 NAVER_ACCOUNT_ID가 필요합니다.")
                form["account_id"] = settings.naver_account_id

            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(f"{settings.naver_base_url}/v1/oauth2/token", data=form)
            if resp.status_code >= 400:
                raise RuntimeError(f"네이버 토큰 발급 실패 ({resp.status_code}): {resp.text[:300]}")
            payload = resp.json()
            token = payload.get("access_token") or (payload.get("data") or {}).get("access_token")
            if not token:
                raise RuntimeError("네이버 토큰 응답에서 access_token을 찾지 못했습니다.")
            expires = int(payload.get("expires_in") or 10800)
            self._token = token
            self._token_expire_at = time.time() + expires
            return token

    async def _confirm_chunk(self, product_order_ids: list[str], token: str) -> ConfirmResult:
        url = f"{settings.naver_base_url}/v1/pay-order/seller/product-orders/confirm"
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(
                url,
                json={"productOrderIds": product_order_ids},
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            if resp.status_code == 401:
                token = await self._issue_token(force=True)
                resp = await client.post(
                    url,
                    json={"productOrderIds": product_order_ids},
                    headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                )
            if resp.status_code >= 400:
                return ConfirmResult(
                    failures=[
                        {
                            "productOrderId": pid,
                            "code": str(resp.status_code),
                            "message": resp.text[:250],
                        }
                        for pid in product_order_ids
                    ]
                )
            payload = resp.json()
            return ConfirmResult(
                success_ids=_extract_success_ids(payload),
                failures=_extract_failures(payload),
            )

    async def confirm_product_orders(self, product_order_ids: list[str]) -> ConfirmResult:
        ids = list(dict.fromkeys(str(x) for x in product_order_ids if x))
        token = await self._issue_token()
        final = ConfirmResult()
        # 공식 제한: 요청 1회당 최대 30개. 의도치 않은 중복/순서 꼬임을 줄이기 위해 순차 처리.
        for chunk in _chunks(ids, 30):
            result = await self._confirm_chunk(chunk, token)
            final.success_ids.extend(result.success_ids)
            final.failures.extend(result.failures)
        return final


naver_client = NaverClient()
