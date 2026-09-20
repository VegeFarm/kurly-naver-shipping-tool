from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import settings

logger = logging.getLogger(__name__)


@dataclass
class PolicyResult:
    mode: str  # DAWN | DAY | UNKNOWN
    error: str = ""


def _walk_values(value: Any):
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk_values(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk_values(v)
    elif value is not None:
        yield value


def detect_delivery_mode(payload: Any) -> str:
    """응답 스키마 변경에 조금 견고하도록 문자열 값 전체에서 배송 타입을 판별한다."""
    values = [str(v).strip() for v in _walk_values(payload) if isinstance(v, (str, int, float))]
    upper = [v.upper() for v in values]

    dawn_exact = {"DAWN", "MORNING", "SAETBYEOL", "STAR", "새벽배송", "샛별배송", "샛별"}
    day_exact = {"DAY", "HARU", "NEXT_DAY", "익일배송", "하루배송", "하루"}

    if any(v in dawn_exact or "새벽" in v or "샛별" in v for v in upper + values):
        return "DAWN"
    if any(v in day_exact or "하루배송" in v or "익일배송" in v for v in upper + values):
        return "DAY"
    return "UNKNOWN"


def _find_token(payload: Any) -> tuple[str, int]:
    candidates = []
    expires = 900

    def visit(obj: Any):
        nonlocal expires
        if isinstance(obj, dict):
            for k, v in obj.items():
                lk = str(k).lower()
                if lk in {"accesstoken", "access_token", "token"} and isinstance(v, str):
                    candidates.append(v)
                elif lk in {"expire", "expiresin", "expires_in"}:
                    try:
                        expires = int(v)
                    except (TypeError, ValueError):
                        pass
                visit(v)
        elif isinstance(obj, list):
            for item in obj:
                visit(item)

    visit(payload)
    if not candidates:
        raise RuntimeError("컬리 토큰 응답에서 access token을 찾지 못했습니다.")
    return candidates[0], expires


class KurlyClient:
    def __init__(self):
        self._token = ""
        self._token_expire_at = 0.0
        self._token_lock = asyncio.Lock()

    def configured(self) -> bool:
        return bool(
            settings.kurly_auth_base_url
            and settings.kurly_api_base_url
            and settings.kurly_client_id
            and settings.kurly_secret_key
        )

    async def _issue_token(self, force: bool = False) -> str:
        if not self.configured():
            missing = []
            if not settings.kurly_auth_base_url:
                missing.append("KURLY_AUTH_BASE_URL")
            if not settings.kurly_api_base_url:
                missing.append("KURLY_API_BASE_URL")
            if not settings.kurly_client_id:
                missing.append("KURLY_CLIENT_ID")
            if not settings.kurly_secret_key:
                missing.append("KURLY_SECRET_KEY")
            raise RuntimeError("컬리 환경변수를 설정하세요: " + ", ".join(missing))
        if not force and self._token and time.time() < self._token_expire_at - 60:
            return self._token

        async with self._token_lock:
            if not force and self._token and time.time() < self._token_expire_at - 60:
                return self._token
            body = {
                "clientId": settings.kurly_client_id,
                "secretKey": settings.kurly_secret_key,
            }
            if settings.kurly_solution_code:
                body["solutionCode"] = settings.kurly_solution_code

            token_url = f"{settings.kurly_auth_base_url}/auth/token"
            async with httpx.AsyncClient(
                timeout=settings.kurly_timeout_seconds,
                follow_redirects=False,
            ) as client:
                try:
                    resp = await client.post(
                        token_url,
                        json=body,
                        headers={
                            "Accept": "application/json",
                            "Content-Type": "application/json",
                        },
                    )
                except httpx.HTTPError as exc:
                    raise RuntimeError(f"컬리 토큰 서버 연결 실패: {exc}") from exc

            # API 호스트가 아닌 웹 콘솔 주소를 넣었을 때 로그인 페이지 등으로
            # 리다이렉트되는 경우가 있어 JSON 파싱 전에 명확히 보여준다.
            if 300 <= resp.status_code < 400:
                location = resp.headers.get("location", "")
                raise RuntimeError(
                    "컬리 토큰 URL이 리다이렉트되었습니다 "
                    f"(HTTP {resp.status_code}, Location={location or '없음'}). "
                    "KURLY_AUTH_BASE_URL이 실제 PROD 인증 API 호스트인지 확인하세요."
                )

            content_type = resp.headers.get("content-type", "")
            preview = (resp.text or "").strip().replace("\n", " ")[:500]
            if resp.status_code >= 400:
                raise RuntimeError(
                    f"컬리 토큰 발급 실패 (HTTP {resp.status_code}, "
                    f"Content-Type={content_type or '없음'}): {preview or '[응답 본문 없음]'}"
                )

            if not resp.content:
                raise RuntimeError(
                    "컬리 토큰 발급 서버가 성공 상태를 반환했지만 응답 본문이 비어 있습니다. "
                    f"URL={token_url}, HTTP={resp.status_code}, "
                    f"Content-Type={content_type or '없음'}. "
                    "KURLY_AUTH_BASE_URL과 Render Outbound IP 화이트리스트를 확인하세요."
                )

            try:
                payload = resp.json()
            except ValueError as exc:
                raise RuntimeError(
                    "컬리 토큰 응답이 JSON이 아닙니다. "
                    f"URL={token_url}, HTTP={resp.status_code}, "
                    f"Content-Type={content_type or '없음'}, "
                    f"응답={preview or '[응답 본문 없음]'}. "
                    "KURLY_AUTH_BASE_URL이 실제 PROD 인증 API 호스트인지 확인하세요."
                ) from exc

            token, expires = _find_token(payload)
            self._token = token
            self._token_expire_at = time.time() + max(120, expires)
            return token

    async def lookup(self, address: str) -> PolicyResult:
        if not address.strip():
            return PolicyResult("UNKNOWN", "주소가 비어 있습니다.")

        token = await self._issue_token()
        body = {settings.kurly_policy_address_field: address.strip()}
        if not settings.kurly_api_base_url:
            return PolicyResult("UNKNOWN", "KURLY_API_BASE_URL 환경변수를 설정하세요.")
        url = f"{settings.kurly_api_base_url}/api/delivery-agency/v1/delivery-policies"

        async with httpx.AsyncClient(timeout=settings.kurly_timeout_seconds) as client:
            for attempt in range(4):
                try:
                    resp = await client.post(
                        url,
                        json=body,
                        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    )
                except httpx.HTTPError as exc:
                    if attempt == 3:
                        return PolicyResult("UNKNOWN", f"컬리 API 네트워크 오류: {exc}")
                    await asyncio.sleep(0.8 * (2**attempt))
                    continue

                if resp.status_code == 401 and attempt == 0:
                    token = await self._issue_token(force=True)
                    continue
                if resp.status_code == 429:
                    if attempt == 3:
                        return PolicyResult("UNKNOWN", "컬리 API 요청 제한(429)이 계속 발생했습니다.")
                    retry_after = resp.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 0.8 * (2**attempt)
                    except ValueError:
                        delay = 0.8 * (2**attempt)
                    await asyncio.sleep(min(delay, 8))
                    continue
                if resp.status_code >= 400:
                    return PolicyResult(
                        "UNKNOWN",
                        f"컬리 정책 조회 실패 ({resp.status_code}): {resp.text[:250]}",
                    )

                try:
                    payload = resp.json()
                except ValueError:
                    return PolicyResult("UNKNOWN", "컬리 응답이 JSON 형식이 아닙니다.")
                mode = detect_delivery_mode(payload)
                if mode == "UNKNOWN":
                    logger.warning("Kurly policy response could not be classified: %s", str(payload)[:1200])
                    return PolicyResult("UNKNOWN", "응답에서 새벽/하루 배송 유형을 판별하지 못했습니다.")
                return PolicyResult(mode)

        return PolicyResult("UNKNOWN", "컬리 API 처리 중 알 수 없는 오류")

    async def lookup_many(self, addresses: dict[str, str]) -> dict[str, PolicyResult]:
        semaphore = asyncio.Semaphore(settings.kurly_concurrency)
        results: dict[str, PolicyResult] = {}

        async def one(key: str, address: str):
            async with semaphore:
                results[key] = await self.lookup(address)

        await asyncio.gather(*(one(k, a) for k, a in addresses.items()))
        return results


kurly_client = KurlyClient()
