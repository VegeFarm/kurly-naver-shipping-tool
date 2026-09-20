# 배포 체크리스트

- [ ] GitHub 새 저장소에 이 폴더 전체 업로드
- [ ] Render Web Service 연결
- [ ] Render Postgres 생성 또는 기존 DB 연결
- [ ] `DATABASE_URL` = Postgres Internal Database URL
- [ ] `APP_PASSWORD` 강한 비밀번호 설정
- [ ] `KURLY_BASE_URL` PROD 주소 설정
- [ ] `KURLY_CLIENT_ID` PROD 값 설정
- [ ] `KURLY_SECRET_KEY` PROD 값 설정
- [ ] Render outbound IP를 컬리 Whitelist에 등록
- [ ] `NAVER_CLIENT_ID` 설정
- [ ] `NAVER_CLIENT_SECRET` 설정
- [ ] 소량 주문 엑셀로 컬리 새벽/익일 판정 확인
- [ ] `익일배송지역.xlsx` 양식 확인
- [ ] 테스트 주문으로 네이버 발주확인 검증
- [ ] 연락처 복사 → 연락완료 → 재업로드 시 중복 제외 확인
- [ ] 연락 이력 초기화 확인창 및 초기화 후 재표시 확인
