from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..config import settings


@dataclass
class PolicyResult:
    mode: str  # DAWN | DAY | UNKNOWN
    error: str = ""


class KurlyRelayClient:
    """Render -> Mac relay -> Kurly 로 배송정책을 조회합니다."""

    def configured(self) -> bool:
        return bool(settings.kurly_relay_url and settings.kurly_relay_secret)

    async def lookup_many(self, addresses: dict[str, str]) -> dict[str, PolicyResult]:
        if not self.configured():
            missing = []
            if not settings.kurly_relay_url:
                missing.append("KURLY_RELAY_URL")
            if not settings.kurly_relay_secret:
                missing.append("KURLY_RELAY_SECRET")
            raise RuntimeError("Mac 컬리 중계 환경변수를 설정하세요: " + ", ".join(missing))

        items = [
            {"key": str(key), "address": (address or "").strip()}
            for key, address in addresses.items()
        ]
        results: dict[str, PolicyResult] = {}

        # 파일이 아주 큰 경우에도 relay 한 요청이 과도하게 커지지 않도록 나눕니다.
        batch_size = settings.kurly_relay_batch_size
        url = f"{settings.kurly_relay_url}/v1/delivery-policies/batch"
        headers = {
            "X-Relay-Secret": settings.kurly_relay_secret,
            "Accept": "application/json",
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(
            timeout=settings.kurly_relay_timeout_seconds,
            follow_redirects=False,
        ) as client:
            for start in range(0, len(items), batch_size):
                chunk = items[start : start + batch_size]
                try:
                    resp = await client.post(url, json={"items": chunk}, headers=headers)
                except httpx.HTTPError as exc:
                    raise RuntimeError(f"Mac 컬리 중계 서버 연결 실패: {exc}") from exc

                if 300 <= resp.status_code < 400:
                    raise RuntimeError(
                        "Mac 컬리 중계 URL이 리다이렉트되었습니다 "
                        f"(HTTP {resp.status_code}, Location={resp.headers.get('location', '없음')}). "
                        "KURLY_RELAY_URL에는 Cloudflare Tunnel의 relay 주소만 넣으세요."
                    )

                if resp.status_code == 401:
                    raise RuntimeError(
                        "Mac 컬리 중계 인증 실패(401). Render와 Mac의 "
                        "KURLY_RELAY_SECRET / RELAY_SECRET 값이 같은지 확인하세요."
                    )
                if resp.status_code >= 400:
                    preview = (resp.text or "").strip().replace("\n", " ")[:700]
                    raise RuntimeError(
                        f"Mac 컬리 중계 서버 오류 (HTTP {resp.status_code}): "
                        f"{preview or '[응답 본문 없음]'}"
                    )

                try:
                    payload = resp.json()
                except ValueError as exc:
                    preview = (resp.text or "").strip().replace("\n", " ")[:500]
                    raise RuntimeError(
                        "Mac 컬리 중계 서버 응답이 JSON이 아닙니다: "
                        f"{preview or '[응답 본문 없음]'}"
                    ) from exc

                for row in payload.get("results", []):
                    key = str(row.get("key", ""))
                    mode = str(row.get("mode", "UNKNOWN")).upper()
                    if mode not in {"DAWN", "DAY", "UNKNOWN"}:
                        mode = "UNKNOWN"
                    if key:
                        results[key] = PolicyResult(mode=mode, error=str(row.get("error", "")))

        # relay가 일부 key를 반환하지 않는 비정상 상황은 판정실패로 남깁니다.
        for key in addresses:
            if str(key) not in results:
                results[str(key)] = PolicyResult(
                    mode="UNKNOWN",
                    error="Mac 컬리 중계 서버가 해당 주소의 결과를 반환하지 않았습니다.",
                )

        return results


kurly_client = KurlyRelayClient()
