# 새벽배송 / 익일배송 분류 + 네이버 발주확인

네이버 스마트스토어 `전체주문발주발송관리.xlsx`를 업로드하면 다음 순서로 처리합니다.

1. 비밀번호 `0000`(환경변수로 변경 가능) 자동 해제
2. `통합배송지` 기준으로 중복 주소 제거
3. 컬리 KLS 배송운영 정책 API로 주소별 새벽/하루 배송 판정
4. **새벽배송** 상품주문만 화면에서 확인 후 네이버 발주확인 API 호출
5. **익일배송** 주문만 원본 네이버 엑셀 양식 그대로 `익일배송지역.xlsx` 생성
6. 익일배송의 `구매자연락처`를 중복 제거해 한 줄씩 복사
7. 연락완료 주문번호를 DB에 저장하여 다음 업로드 때 중복 연락 방지
8. 연락 이력은 자동 만료하지 않고 `연락 이력 초기화`를 직접 승인할 때만 삭제

## 중요 동작

- 업로드만으로 네이버 발주확인이 실행되지는 않습니다. `네이버 발주확인` 버튼 → 확인창 승인 후 실행됩니다.
- 네이버 발주확인은 공식 제한에 맞춰 상품주문번호를 **30개씩 자동 분할**합니다.
- 컬리 API 오류/주소 판정 실패 건은 익일배송으로 임의 분류하지 않고 `판정 실패`로 따로 표시합니다.
- 같은 배송지의 여러 상품 행은 컬리 API를 한 번만 호출합니다.
- 익일배송 연락 중복 판단은 **주문번호 기준**입니다. 같은 주문이 오후 파일에도 남아 있어도 연락완료 처리했다면 다시 표시되지 않습니다.
- 연락처 표시/복사 값은 이름 없이 번호만 나옵니다. `010`, `02`, `031` 등 모두 동일하게 처리합니다.
- 출력 `익일배송지역.xlsx`는 기본적으로 일반 `.xlsx`로 생성합니다. 출력 파일에도 비밀번호를 걸고 싶으면 `OUTPUT_EXCEL_PASSWORD=`을 설정할 수 있습니다.

## 사용 엑셀 열

현재 네이버 주문 엑셀에서 다음 열 이름을 찾아 사용합니다. 열 위치가 바뀌어도 이름이 같으면 동작합니다.

- `통합배송지`
- `우편번호`
- `상품주문번호`
- `주문번호`
- `구매자연락처`

업로드하신 2026-09-20 샘플은 `발주발송관리` 시트, 헤더 2행 구조였으며 이 구조를 그대로 지원합니다.

---

# Render 배포

## 1. GitHub에 올리기

이 폴더 전체를 새 GitHub 저장소 루트에 올립니다. `.env` 파일이나 실제 API 키는 GitHub에 올리지 마세요.

## 2. Render Web Service 만들기

방법 A: 저장소의 `render.yaml`을 사용해 Blueprint로 생성

방법 B: 직접 Web Service 생성

- Runtime: Python
- Build Command: `pip install -r requirements.txt`
- Start Command: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Health Check Path: `/healthz`

## 3. Render Postgres 연결

연락 이력을 재배포/재시작 뒤에도 유지하려면 Render Postgres가 필요합니다.

Render Postgres의 **Internal Database URL**을 Web Service 환경변수 `DATABASE_URL`에 넣습니다.

`DATABASE_URL`을 넣지 않으면 SQLite로 동작하지만 Render 재배포 시 이력이 사라질 수 있으므로 운영에서는 권장하지 않습니다.

## 4. 환경변수

필수:

```env
APP_USERNAME=admin
APP_PASSWORD=충분히긴비밀번호
EXCEL_PASSWORD=0000
# 선택: 다운로드 파일도 암호화하려면 0000 입력
OUTPUT_EXCEL_PASSWORD=
DATABASE_URL=Render_Postgres_Internal_Database_URL

KURLY_BASE_URL=컬리_PROD_Base_URL
KURLY_CLIENT_ID=컬리_PROD_Client_ID
KURLY_SECRET_KEY=컬리_PROD_Secret_Key
KURLY_CONCURRENCY=5
KURLY_POLICY_ADDRESS_FIELD=address

NAVER_CLIENT_ID=네이버_커머스_API_Client_ID
NAVER_CLIENT_SECRET=네이버_커머스_API_Client_Secret
NAVER_TOKEN_TYPE=SELF
NAVER_BASE_URL=https://api.commerce.naver.com/external
```

`KURLY_BASE_URL`은 계약/발급 안내에서 확인한 **PROD 호스트**를 넣으세요. STG 키와 PROD 키를 섞으면 안 됩니다.

화주사 자체 시스템이면 `KURLY_SOLUTION_CODE`는 비워둡니다. 컬리 문서상 외부 솔루션을 통한 호출일 때만 허용된 solutionCode를 사용합니다.

## 5. 컬리 IP Whitelist

컬리 KLS는 IP Whitelist 등록이 필요합니다. Render Web Service의 outbound IP를 컬리에 등록한 뒤 PROD API를 호출하세요.

---

# 컬리 API 관련

사용 API:

- 토큰: `POST /auth/token`
- 배송정책 조회: `POST /api/delivery-agency/v1/delivery-policies`

현재 컬리 개발자센터는 배송정책 API에 대해 “주소만 입력하면 배송대행 정책에 매칭되는 가용 운영정책을 가장 빠른 배송 가능일 기준으로 조회”한다고 안내합니다.

코드는 기본 요청 Body를 아래처럼 보냅니다.

```json
{"address": "서울특별시 ..."}
```

만약 실제 발급 계정의 최신 API 스키마에서 주소 필드명이 다르면 코드 수정 없이 Render 환경변수 `KURLY_POLICY_ADDRESS_FIELD`만 변경할 수 있습니다.

응답에서는 `DAWN/새벽/샛별`을 우선 새벽배송으로, `DAY/하루/익일`을 익일배송으로 판정합니다. 둘 다 판별되지 않으면 안전하게 `판정 실패` 처리합니다.

---

# 네이버 API 관련

사용 API:

- OAuth2 토큰: `POST /v1/oauth2/token`
- 발주확인: `POST /v1/pay-order/seller/product-orders/confirm`

요청 Body:

```json
{
  "productOrderIds": ["상품주문번호1", "상품주문번호2"]
}
```

한 번에 최대 30개 제한에 맞춰 서버가 자동 분할합니다.

직접 본인 스마트스토어용 애플리케이션이라면 보통 `NAVER_TOKEN_TYPE=SELF`로 사용합니다. 솔루션 제공자 구조에서 `SELLER` 토큰을 사용하는 경우 `NAVER_ACCOUNT_ID`도 설정하세요.

---

# 개인정보 / 보안

- API 키는 환경변수에만 저장합니다.
- Render URL은 주문 발주 기능이 있으므로 `APP_USERNAME` / `APP_PASSWORD` Basic Auth로 보호합니다.
- 연락 이력 DB에는 주문번호, 구매자 연락처, 주소 해시, 연락 시간만 저장합니다. 전체 배송지/구매자명은 장기 DB에 저장하지 않습니다.
- 업로드된 엑셀 및 생성 파일은 Render의 임시 디렉터리에 두며 24시간이 지난 작업 폴더는 다음 작업 시 자동 정리합니다.

---

# 로컬 실행

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
cp .env.example .env
uvicorn app.main:app --reload
```

브라우저: `http://127.0.0.1:8000`

## 테스트

```bash
pytest -q
```

---

# 첫 배포 후 권장 확인 순서

1. 컬리 PROD 환경변수 + Render outbound IP whitelist 확인
2. 주문 1~3건짜리 소량 엑셀로 배송 판정 확인
3. 실제 알고 있는 새벽배송 주소/익일배송 주소가 각각 맞게 분류되는지 확인
4. 익일배송 엑셀 다운로드 후 원본과 열/서식 확인
5. 테스트 주문으로만 네이버 발주확인 버튼 검증
6. 연락처 복사 → 연락완료 처리 → 같은 엑셀 재업로드 시 해당 주문 연락처가 제외되는지 확인
7. `연락 이력 초기화` → 확인창 승인 → 같은 주문이 다시 신규 연락 대상으로 나오는지 확인
