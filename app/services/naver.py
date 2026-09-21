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
        return bool(settings.naver_client_id.strip() and settings.naver_client_secret.strip())

    def _signature(self, timestamp_ms: int) -> str:
        client_id = settings.naver_client_id.strip()
        client_secret = settings.naver_client_secret.strip()
        if not client_id or not client_secret:
            raise RuntimeError("NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 환경변수를 설정하세요.")
        # 네이버 커머스API의 client_secret은 bcrypt salt 형식($2a$/$2b$/$2y$)이어야 합니다.
        if not client_secret.startswith(("$2a$", "$2b$", "$2y$")):
            raise RuntimeError(
                "NAVER_CLIENT_SECRET 형식이 올바르지 않습니다. "
                "네이버 커머스API센터에서 발급한 애플리케이션 시크릿을 사용하세요 "
                "(보통 $2a$ 또는 $2b$로 시작하는 bcrypt salt 형식)."
            )
        password = f"{client_id}_{timestamp_ms}".encode("utf-8")
        try:
            hashed = bcrypt.hashpw(password, client_secret.encode("utf-8"))
        except ValueError as exc:
            raise RuntimeError(
                "NAVER_CLIENT_SECRET로 전자서명을 만들 수 없습니다. "
                "커머스API센터의 Client Secret 값을 다시 확인하세요."
            ) from exc
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

            token_url = f"{settings.naver_base_url}/v1/oauth2/token"
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
                    resp = await client.post(token_url, data=form)
            except httpx.RequestError as exc:
                raise RuntimeError(f"네이버 토큰 서버 연결 실패: {exc}") from exc
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"네이버 토큰 발급 실패 (HTTP {resp.status_code}): {resp.text[:500]}"
                )
            try:
                payload = resp.json()
            except ValueError as exc:
                raise RuntimeError(
                    f"네이버 토큰 응답이 JSON이 아닙니다. HTTP={resp.status_code}, "
                    f"Content-Type={resp.headers.get('content-type','')}, 응답={resp.text[:500]}"
                ) from exc
            token = (
                payload.get("access_token")
                or payload.get("accessToken")
                or (payload.get("data") or {}).get("access_token")
                or (payload.get("data") or {}).get("accessToken")
            )
            if not token:
                raise RuntimeError("네이버 토큰 응답에서 access_token을 찾지 못했습니다.")
            expires = int(payload.get("expires_in") or payload.get("expiresIn") or 10800)
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
            try:
                payload = resp.json()
            except ValueError:
                return ConfirmResult(
                    failures=[
                        {
                            "productOrderId": pid,
                            "code": "INVALID_RESPONSE",
                            "message": (
                                f"네이버 발주확인 응답이 JSON이 아닙니다. "
                                f"HTTP={resp.status_code}, 응답={resp.text[:250]}"
                            ),
                        }
                        for pid in product_order_ids
                    ]
                )
            success_ids = _extract_success_ids(payload)
            failures = _extract_failures(payload)
            # 200 응답인데 성공/실패 목록이 모두 비어 있으면 성공으로 추정하지 않는다.
            # 실제 응답을 실패 사유로 남겨 잘못된 로컬 완료 기록을 방지한다.
            accounted = set(success_ids) | {str(x.get("productOrderId") or "") for x in failures}
            for pid in product_order_ids:
                if pid not in accounted:
                    failures.append(
                        {
                            "productOrderId": pid,
                            "code": "UNRECOGNIZED_RESPONSE",
                            "message": f"네이버 응답 구조를 확인할 수 없습니다: {str(payload)[:350]}",
                        }
                    )
            return ConfirmResult(success_ids=success_ids, failures=failures)

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
