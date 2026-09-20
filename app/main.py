from __future__ import annotations

import base64
import hashlib
import hmac
import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
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
)
from .services.kurly import kurly_client
from .services.naver import naver_client


app = FastAPI(title="새벽배송/익일배송 분류", docs_url=None, redoc_url=None)


@app.on_event("startup")
def startup():
    init_db()


def _unauthorized():
    return PlainTextResponse(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="Shipping Tool", charset="UTF-8"'},
    )


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)
    if not settings.app_password:
        return PlainTextResponse(
            "APP_PASSWORD 환경변수가 설정되지 않았습니다.", status_code=503
        )
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return _unauthorized()
    try:
        raw = base64.b64decode(auth[6:]).decode("utf-8")
        username, password = raw.split(":", 1)
    except Exception:
        return _unauthorized()
    if not (
        hmac.compare_digest(username, settings.app_username)
        and hmac.compare_digest(password, settings.app_password)
    ):
        return _unauthorized()
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index():
    html = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


def _public_job(meta: dict) -> dict:
    nextday_orders = meta.get("nextday_orders", [])
    order_ids = list(dict.fromkeys(x["order_id"] for x in nextday_orders if x.get("order_id")))
    contacted = contacted_order_ids(order_ids)
    new_orders = [x for x in nextday_orders if x.get("order_id") not in contacted]

    phone_keys = set()
    phones = []
    for item in new_orders:
        key = digits_only(item.get("phone", ""))
        if not key or key in phone_keys:
            continue
        phone_keys.add(key)
        phones.append(format_korean_phone(item.get("phone", "")))

    dawn_ids = meta.get("dawn_product_order_ids", [])
    confirmed = confirmed_product_order_ids(dawn_ids)

    return {
        "job_id": meta["job_id"],
        "summary": meta["summary"],
        "contacts": {
            "phones": phones,
            "new_order_count": len({x["order_id"] for x in new_orders}),
            "already_contacted_order_count": len(contacted),
            "phone_count": len(phones),
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
        raise HTTPException(status_code=503, detail="컬리 PROD API 환경변수가 설정되지 않았습니다.")

    # 동일 배송지는 한 번만 컬리 API 호출
    unique_addresses: dict[str, str] = {}
    for row in rows:
        unique_addresses.setdefault(row.address_key, row.address)
    policy_results = await kurly_client.lookup_many(unique_addresses)

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
    nextday_by_order: dict[str, dict] = {}
    for r in day_rows:
        nextday_by_order.setdefault(
            r.order_id,
            {
                "order_id": r.order_id,
                "phone": r.buyer_phone,
                "address_hash": r.address_hash,
            },
        )

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
    if not body.confirm:
        raise HTTPException(status_code=400, detail="발주확인 동의가 필요합니다.")
    if not naver_client.configured():
        raise HTTPException(status_code=503, detail="네이버 API 환경변수가 설정되지 않았습니다.")
    try:
        _, meta = load_meta(job_id)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="작업을 찾을 수 없습니다.")

    all_ids = list(dict.fromkeys(meta.get("dawn_product_order_ids", [])))
    already = confirmed_product_order_ids(all_ids)
    pending = [x for x in all_ids if x not in already]
    if not pending:
        return {"success": 0, "failed": 0, "skipped_already_confirmed": len(already), "failures": []}

    result = await naver_client.confirm_product_orders(pending)
    mark_product_orders_confirmed(result.success_ids)
    return {
        "success": len(result.success_ids),
        "failed": len(result.failures),
        "skipped_already_confirmed": len(already),
        "failures": result.failures,
    }


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
