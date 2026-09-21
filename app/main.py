from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel

from .config import settings
from .db import (
    contact_history_stats,
    contacted_order_ids,
    confirmed_product_order_ids,
    init_db,
    mark_orders_contacted,
    mark_product_orders_confirmed,
    reset_contact_history,
)
from .jobs import load_meta, new_job_dir, save_meta
from .services.excel_service import (
    build_filtered_workbook,
    decrypt_excel_bytes,
    digits_only,
    format_korean_phone,
    parse_orders,
    select_contact_phone,
)
from .services.kurly import kurly_client
from .services.naver import naver_client


app = FastAPI(title="새벽배송/익일배송 분류", docs_url=None, redoc_url=None)


@app.on_event("startup")
def startup():
    init_db()



@app.get("/healthz")
def healthz():
    return {"ok": True, "build": "naver-relay-confirm-v1"}


@app.get("/", response_class=HTMLResponse)
def index():
    html = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


def _public_job(meta: dict) -> dict:
    nextday_orders = meta.get("nextday_orders", [])
    order_ids = list(dict.fromkeys(x["order_id"] for x in nextday_orders if x.get("order_id")))
    contacted = contacted_order_ids(order_ids)
    new_orders = [x for x in nextday_orders if x.get("order_id") not in contacted]

    mobile_keys = set()
    mobile_phones = []
    manual_keys = set()
    manual_contacts = []
    for item in new_orders:
        phone = format_korean_phone(item.get("phone", ""))
        key = digits_only(phone)
        if not key:
            continue

        if item.get("contact_kind") == "mobile":
            if key in mobile_keys:
                continue
            mobile_keys.add(key)
            mobile_phones.append(phone)
            continue

        buyer_name = (item.get("buyer_name") or "이름없음").strip()
        manual_key = (buyer_name, key)
        if manual_key in manual_keys:
            continue
        manual_keys.add(manual_key)
        manual_contacts.append(
            {
                "buyer_name": buyer_name,
                "phone": phone,
                "display": f"{buyer_name} {phone}",
            }
        )

    dawn_ids = meta.get("dawn_product_order_ids", [])
    confirmed = confirmed_product_order_ids(dawn_ids)

    return {
        "job_id": meta["job_id"],
        "summary": meta["summary"],
        "contacts": {
            # phones는 기존 프론트/호환성을 위해 휴대전화 목록과 동일하게 유지
            "phones": mobile_phones,
            "mobile_phones": mobile_phones,
            "manual_contacts": manual_contacts,
            "new_order_count": len({x["order_id"] for x in new_orders}),
            "contactable_order_count": len(
                {x["order_id"] for x in new_orders if digits_only(x.get("phone", ""))}
            ),
            "already_contacted_order_count": len(contacted),
            "phone_count": len(mobile_phones),
            "mobile_phone_count": len(mobile_phones),
            "manual_contact_count": len(manual_contacts),
            "missing_phone_order_count": len(
                {x["order_id"] for x in new_orders if not digits_only(x.get("phone", ""))}
            ),
        },
        "naver": {
            "total_product_orders": len(dawn_ids),
            "already_confirmed_locally": len(confirmed),
            "pending_product_orders": len(set(dawn_ids) - confirmed),
        },
        "unknowns": meta.get("unknowns", []),
        "contact_history": _serialize_stats(contact_history_stats()),
    }


def _serialize_stats(stats: dict) -> dict:
    return {
        "count": stats["count"],
        "oldest": stats["oldest"].isoformat() if stats["oldest"] else None,
        "newest": stats["newest"].isoformat() if stats["newest"] else None,
    }


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...)):
    filename = file.filename or "orders.xlsx"
    if not filename.lower().endswith(".xlsx"):
        raise HTTPException(status_code=400, detail=".xlsx 파일만 업로드할 수 있습니다.")
    raw = await file.read()
    if len(raw) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="파일이 너무 큽니다. 최대 25MB입니다.")

    try:
        plain = decrypt_excel_bytes(raw, settings.excel_password)
        rows, sheet_meta = parse_orders(plain)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not rows:
        raise HTTPException(status_code=400, detail="주문 데이터가 없습니다.")
    if not kurly_client.configured():
        raise HTTPException(status_code=503, detail="Mac 컬리 중계 서버 환경변수가 설정되지 않았습니다.")

    # 동일 배송지는 한 번만 컬리 API 호출
    unique_addresses: dict[str, str] = {}
    for row in rows:
        unique_addresses.setdefault(row.address_key, row.address)
    try:
        policy_results = await kurly_client.lookup_many(unique_addresses)
    except Exception as exc:
        # 인증 실패, IP 화이트리스트, Base URL 오류 등 컬리 연결 문제를
        # 브라우저에서 바로 확인할 수 있도록 안전한 오류 메시지로 전달한다.
        raise HTTPException(status_code=502, detail=f"컬리 API 연결 실패: {exc}") from exc

    dawn_rows = []
    day_rows = []
    unknown_rows = []
    for row in rows:
        result = policy_results[row.address_key]
        if result.mode == "DAWN":
            dawn_rows.append(row)
        elif result.mode == "DAY":
            day_rows.append(row)
        else:
            unknown_rows.append((row, result.error))

    job_id, job_dir = new_job_dir()
    nextday_bytes = build_filtered_workbook(
        plain,
        [r.row_number for r in day_rows],
        output_password=settings.output_excel_password,
    )
    nextday_path = job_dir / "익일배송지역.xlsx"
    nextday_path.write_bytes(nextday_bytes)

    # 익일 연락은 주문번호 단위. 같은 주문이 여러 상품 행이어도 1건으로 저장.
    # 구매자 번호가 010이 아니면 수취인 연락처를 확인하고, 수취인이 010이면
    # 문자 대상 번호로 사용한다. 둘 다 휴대전화가 아니면 수동 확인 목록으로 보낸다.
    nextday_by_order: dict[str, dict] = {}
    for r in day_rows:
        contact_kind, contact_phone, contact_source = select_contact_phone(
            r.buyer_phone, r.recipient_phone
        )
        candidate = {
            "order_id": r.order_id,
            "buyer_name": r.buyer_name,
            "buyer_phone": format_korean_phone(r.buyer_phone),
            "recipient_phone": format_korean_phone(r.recipient_phone),
            "phone": contact_phone,
            "contact_kind": contact_kind,
            "contact_source": contact_source,
            "address_hash": r.address_hash,
        }
        existing = nextday_by_order.get(r.order_id)
        if existing is None:
            nextday_by_order[r.order_id] = candidate
        elif existing.get("contact_kind") != "mobile" and contact_kind == "mobile":
            # 같은 주문의 다른 상품 행에서 더 좋은(휴대전화) 연락처가 발견되면 교체
            nextday_by_order[r.order_id] = candidate

    unknown_by_address = {}
    for r, error in unknown_rows:
        unknown_by_address.setdefault(
            r.address_key,
            {"address": r.address, "zip_code": r.zip_code, "error": error},
        )

    dawn_product_order_ids = list(dict.fromkeys(r.product_order_id for r in dawn_rows))
    summary = {
        "total_rows": len(rows),
        "unique_addresses": len(unique_addresses),
        "dawn_rows": len(dawn_rows),
        "dawn_product_orders": len(dawn_product_order_ids),
        "dawn_addresses": len({r.address_key for r in dawn_rows}),
        "nextday_rows": len(day_rows),
        "nextday_product_orders": len({r.product_order_id for r in day_rows}),
        "nextday_orders": len(nextday_by_order),
        "nextday_addresses": len({r.address_key for r in day_rows}),
        "unknown_rows": len(unknown_rows),
        "unknown_addresses": len(unknown_by_address),
        "sheet": sheet_meta,
    }
    meta = {
        "job_id": job_id,
        "source_filename": filename,
        "summary": summary,
        "dawn_product_order_ids": dawn_product_order_ids,
        "nextday_orders": list(nextday_by_order.values()),
        "unknowns": list(unknown_by_address.values()),
    }
    save_meta(job_dir, meta)
    return _public_job(meta)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    try:
        _, meta = load_meta(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    return _public_job(meta)


@app.get("/api/jobs/{job_id}/nextday.xlsx")
def download_nextday(job_id: str):
    try:
        path, _ = load_meta(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")
    xlsx = path / "익일배송지역.xlsx"
    if not xlsx.exists():
        raise HTTPException(status_code=404, detail="익일배송 엑셀을 찾을 수 없습니다.")
    return FileResponse(
        xlsx,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename="익일배송지역.xlsx",
    )


class ConfirmBody(BaseModel):
    confirm: bool = False


@app.post("/api/jobs/{job_id}/naver-confirm")
async def naver_confirm(job_id: str, body: ConfirmBody):
    # 이 엔드포인트 전체를 감싸서 예상하지 못한 예외도 브라우저에 원인을 표시한다.
    try:
        if not body.confirm:
            raise HTTPException(status_code=400, detail="발주확인 동의가 필요합니다.")
        if not naver_client.configured():
            raise HTTPException(status_code=503, detail="Mac 네이버 중계 서버 환경변수가 설정되지 않았습니다.")

        try:
            _, meta = load_meta(job_id)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")

        all_ids = list(dict.fromkeys(str(x) for x in meta.get("dawn_product_order_ids", []) if x))
        already = confirmed_product_order_ids(all_ids)
        pending = [x for x in all_ids if x not in already]
        if not pending:
            return {
                "success": 0,
                "failed": 0,
                "skipped_already_confirmed": len(already),
                "failures": [],
            }

        result = await naver_client.confirm_product_orders(pending)

        if result.success_ids:
            mark_product_orders_confirmed(result.success_ids)

        return {
            "success": len(result.success_ids),
            "failed": len(result.failures),
            "skipped_already_confirmed": len(already),
            "failures": result.failures,
        }
    except HTTPException:
        raise
    except Exception as exc:
        # Client ID/Secret 같은 비밀값은 반환하지 않고 예외 종류와 메시지만 전달한다.
        raise HTTPException(
            status_code=502,
            detail=f"네이버 발주확인 오류 [{type(exc).__name__}]: {exc}",
        ) from exc


@app.post("/api/jobs/{job_id}/contacts/mark")
def mark_contacts(job_id: str, body: ConfirmBody):
    if not body.confirm:
        raise HTTPException(status_code=400, detail="연락완료 확인이 필요합니다.")
    try:
        _, meta = load_meta(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")

    records = meta.get("nextday_orders", [])
    existing = contacted_order_ids([x["order_id"] for x in records])
    new_records = [
        x for x in records
        if x["order_id"] not in existing and digits_only(x.get("phone", ""))
    ]
    inserted = mark_orders_contacted(new_records)
    return {"marked": inserted, "contact_history": _serialize_stats(contact_history_stats())}


@app.get("/api/contact-history")
def contact_history():
    return _serialize_stats(contact_history_stats())


class ResetBody(BaseModel):
    confirm: bool = False


@app.post("/api/contact-history/reset")
def reset_history(body: ResetBody):
    if not body.confirm:
        raise HTTPException(status_code=400, detail="초기화 확인이 필요합니다.")
    deleted = reset_contact_history()
    return {"deleted": deleted, "contact_history": _serialize_stats(contact_history_stats())}
