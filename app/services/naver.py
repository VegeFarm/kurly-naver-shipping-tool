from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from ..config import settings


@dataclass
class ConfirmResult:
    success_ids: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)


class NaverClient:
    def configured(self) -> bool:
        return bool(settings.naver_relay_url and settings.naver_relay_token)

    async def confirm_product_orders(self, product_order_ids: list[str]) -> ConfirmResult:
        ids = list(dict.fromkeys(str(x).strip() for x in product_order_ids if str(x).strip()))
        if not ids:
            return ConfirmResult()
        if not self.configured():
            raise RuntimeError("NAVER_RELAY_URL / NAVER_RELAY_TOKEN 환경변수를 설정하세요.")

        url = f"{settings.naver_relay_url}/naver/orders/confirm"
        headers = {
            "Authorization": f"Bearer {settings.naver_relay_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        timeout = httpx.Timeout(settings.naver_relay_timeout_seconds)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(url, headers=headers, json={"productOrderIds": ids})
        except httpx.TimeoutException as exc:
            raise RuntimeError(
                f"Mac 네이버 중계 서버 응답 시간 초과 ({settings.naver_relay_timeout_seconds}초)"
            ) from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Mac 네이버 중계 서버 연결 실패: {exc}") from exc

        raw_text = resp.text
        try:
            payload = resp.json() if resp.content else {}
        except Exception as exc:
            raise RuntimeError(
                f"Mac 네이버 중계 서버가 JSON이 아닌 응답을 반환했습니다. "
                f"HTTP={resp.status_code}, 응답={raw_text[:500]}"
            ) from exc

        if resp.status_code >= 400:
            detail = payload.get("detail") if isinstance(payload, dict) else None
            raise RuntimeError(
                f"Mac 네이버 중계 서버 오류 (HTTP {resp.status_code}): "
                f"{detail or raw_text[:500]}"
            )

        success_ids = [str(x) for x in (payload.get("success_ids") or []) if x]
        failures = []
        for item in payload.get("failures") or []:
            if isinstance(item, dict):
                failures.append(
                    {
                        "productOrderId": str(item.get("productOrderId") or ""),
                        "code": str(item.get("code") or ""),
                        "message": str(item.get("message") or ""),
                    }
                )

        # relay가 처리 결과를 누락한 경우 성공으로 간주하지 않는다.
        accounted = set(success_ids) | {x["productOrderId"] for x in failures}
        for pid in ids:
            if pid not in accounted:
                failures.append(
                    {
                        "productOrderId": pid,
                        "code": "RELAY_UNACCOUNTED",
                        "message": "중계 서버 응답에 해당 상품주문번호 처리 결과가 없습니다.",
                    }
                )

        return ConfirmResult(success_ids=success_ids, failures=failures)


naver_client = NaverClient()
