# 바로ERP (BaroERP)

입력하면 바로, 장부가 끝난다 — 소규모 기업 매입·매출·재고 관리.
기획안: [baroerp.md](baroerp.md)

## 기술 스택

FastAPI(Python 3.12) + Jinja2 + HTMX · SQLAlchemy 2.0 + Alembic · SQLite(설치형) / PostgreSQL(SaaS·데모) · APScheduler

## 시작하기

```bash
cp .env.example .env
uv sync
uv run alembic upgrade head
uv run baroerp            # http://localhost:8000 (자동 리로드)
```

- 첫 접속 시 `/setup`에서 사업장·대표 계정을 만들고, 대표는 2단계 인증용 간편 비밀번호(숫자 6자리)를 등록한 뒤 사용합니다. 로그인과 승인(지급·폐기) 때 입력하며, 5번 연속 틀리면 15분 동안 잠깁니다.
- 거래처 사업자 상태 자동 조회는 공공데이터포털 ‘국세청 사업자등록정보 진위확인 및 상태조회’ 서비스키를 `.env`의 `NTS_API_KEY`에 넣으면 켜집니다(없으면 ‘미확인’으로 둠).
- 데모에서는 `.env`에 `DEMO_MODE=true`를 넣으면 사업자번호를 10자리 숫자인지만 확인합니다(검증번호 확인 생략, 가상 번호 입력용).
- 폐기 증빙 첨부는 `STORAGE_LOCAL_DIR`(기본 `./data/attachments`)에 저장합니다. 내장 백업에 함께 들어갑니다.
- 운영 환경에서는 거래처 담당자 연락처 암호화 키 `DATA_KEY`를 따로 지정하고 안전하게 보관하세요. 키를 잃으면 복호화할 수 없습니다.
- 헬스체크: `GET /healthz` · API 문서: `/docs`
- 테스트: `uv run pytest` · 린트: `uv run ruff check . && uv run ruff format .`

## 간편 비밀번호 초기화

대표가 간편 비밀번호(2단계 인증)를 잊었거나 5번 틀려 잠겼을 때, 다른 대표가 없다면 서버에서 초기화합니다.
초기화한 계정은 다음 로그인 때 이메일·비밀번호로 들어와 간편 비밀번호를 새로 등록합니다.

```bash
uv run python -m app.manage reset-pin --email owner@example.kr                  # 설치형(직접 실행)
docker compose exec app python -m app.manage reset-pin --email owner@example.kr # 설치형(Docker)
PROJECT_ID=my-project ./deploy/gcp/reset-pin.sh owner@example.kr               # GCP 데모(Cloud Run 작업)
```

## 백업·복원

- **설정 > 백업·복원**(대표만): 지금 백업, 내려받기, 복원. 매일 03:00 자동 백업, 최근 7개 보관(`BACKUP_KEEP`).
- 백업 파일은 zip 하나(테이블 JSON + 증빙 첨부)라 SQLite ↔ PostgreSQL 사이에도 복원됩니다.
- 복원 조건: **같은 앱 버전(DB 마이그레이션)과 같은 `DATA_KEY`**. 복원 직전 상태는 `…-before-restore.zip`으로 자동 보관됩니다.
- 설치형은 `data/backups`를 외장 디스크·NAS로 복사해 두세요.

## 데모 준비

```bash
uv run python -m app.demo files data/demo        # 시연용 엑셀(거래처·품목·홈택스 세금계산서)
uv run python -m app.demo seed --email owner@demo.kr --password '********'   # 빈 DB에 3개월 거래 이력
uv run python scripts/perf_check.py              # 주요 화면 응답 시간·메모리 확인
```

시연 순서와 금액은 [baroerp.md](baroerp.md) 9-1장 데모 시나리오·S5 구현 기준을 따릅니다.
세금계산서 파일은 만든 날짜로 작성일자가 들어가므로 시연 당일에 만드세요(`--date`로 지정 가능).

## GCP 데모 배포

```bash
PROJECT_ID=my-project ./deploy/gcp/setup.sh       # 최초 1회: 인프라 전체 구성 + 첫 배포
gcloud builds submit --config cloudbuild.yaml      # 이후 배포
```

Cloud Run(앱) + e2-micro VM(PostgreSQL 16, 외부 IP 없음) + Cloud Storage(첨부·백업·DB 덤프) +
Secret Manager + Cloud Scheduler(매일 백업). 스크립트는 다시 실행해도 같은 결과가 되도록 작성했습니다.
결제 예산 알림(월 1만 원)과 main 브랜치 자동 배포 트리거는 콘솔에서 설정합니다.

## 배포 형태

`DEPLOY_MODE` 환경 변수 하나로 고릅니다.

| 값 | 제품 | DB |
| --- | --- | --- |
| `single` | 설치형·GCP 데모(사업장 1개) | SQLite 기본, PostgreSQL 선택 |
| `saas` | 클라우드(다중 사업장) | PostgreSQL |

```bash
docker compose up -d                 # SQLite
docker compose --profile pg up -d    # PostgreSQL (.env의 DATABASE_URL 변경)
```

컨테이너는 기동 시 `alembic upgrade head`를 실행하고, Cloud Run의 `PORT`를 따릅니다.

## 구조

```
src/app/
  main.py          앱 생성, 미들웨어, 라우터 등록
  config.py        환경 설정(DEPLOY_MODE, DATABASE_URL 등)
  db.py            엔진·세션(SQLite WAL, PG 연결 풀)
  auth.py          세션 로그인, 대표 2단계 인증 강제, require_menu 권한 의존성
  permissions.py   메뉴 목록, 역할별 기본 권한, 사업장별 조정
  audit.py         등록·수정·삭제 자동 감사 로그(Session 이벤트)
  csrf.py          폼·HTMX CSRF 검증
  security.py      비밀번호·간편 비밀번호 해시(scrypt)
  export.py        목록 엑셀 내보내기 공통
  listing.py       목록 페이지 나누기·HTMX 부분 응답
  services/        업무 규칙(기준정보, 전표·재고·폐기·실사, 수금·지급·채권채무, 거래처 상황판, 세금계산서 대사, 분개, 저장소, 백업·복원)
  demo.py          데모 시연 파일·거래 이력 생성
  manage.py        서버 관리 명령(간편 비밀번호 초기화)
  timeutil.py      UTC 저장 ↔ KST 표시
  scheduler.py     내장 예약 작업(Cloud Run에서는 비활성)
  templating.py    Jinja2 설정, render()·flash()
  models/          ORM 모델 — 업무 테이블은 TenantMixin(business_id) 사용
  routers/         화면(HTMX)·API 라우터
  templates/       Jinja2 템플릿
  static/          CSS 등 정적 파일
migrations/        Alembic 마이그레이션
deploy/gcp/        GCP 데모 환경 구성 스크립트
scripts/           성능 확인 등 보조 스크립트
tests/
```

## 모델 추가 규칙

- 업무 테이블은 `TenantMixin`을 상속해 `business_id`를 둡니다.
- SQLite·PostgreSQL 양쪽에서 동작하도록 DB 전용 타입·기능을 쓰지 않습니다.
- 모델 변경 후 `uv run alembic revision --autogenerate -m "..."` → 생성 파일 검토 → `upgrade head`.
- 감사 로그는 `TenantMixin` 모델에 자동 적용됩니다. 비밀값 컬럼은 `app/audit.py`의 `MASKED_FIELDS`에 추가합니다.

## 화면 추가 규칙

- 라우트는 `require_menu("메뉴키")`로 권한을 확인하고, `render(request, ...)`로 그립니다.
- 폼에는 `{% import "_macros.html" as m with context %}` 후 `{{ m.csrf() }}`를 넣습니다(HTMX 요청은 헤더로 자동 전송).
- 디자인은 `static/css/app.css`의 컴포넌트(`.card`, `.stat-*`, `.list-row`, `.btn`, `.input`, `.table`, `.badge`, `.tabs`, `.filters`)를 재사용합니다.
