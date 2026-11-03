#!/bin/bash
# 바로ERP GCP 데모 환경 구성(기획안 9-1 '데모 환경 구성'). 여러 번 실행해도 같은 결과가 되도록 만든다.
#
#   PROJECT_ID=my-project ./deploy/gcp/setup.sh
#
# 만드는 것: Artifact Registry, Cloud Storage 버킷(첨부·백업), Secret Manager 비밀값,
#   Cloud Run 서비스, Cloud Scheduler 데모 초기화 작업. 결제 예산 알림은 콘솔에서 따로 설정한다(월 1만 원).
#
# DB는 컨테이너 안 SQLite(/app/data)다. 인스턴스가 새로 뜰 때마다 데모 데이터로 처음 상태가 되고
# (Dockerfile의 app.demo ensure), 인스턴스마다 DB가 따로이므로 최대 인스턴스는 반드시 1로 둔다.
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID를 지정해 주세요}"
REGION=${REGION:-us-central1}
SERVICE=${SERVICE:-baroerp-demo}
REPO=${REPO:-baroerp}
BUCKET=${BUCKET:-${PROJECT_ID}-baroerp-demo}
RUN_SA=baroerp-run@${PROJECT_ID}.iam.gserviceaccount.com
HERE=$(cd "$(dirname "$0")" && pwd)

gc() { gcloud --project "$PROJECT_ID" "$@"; }
exists() { "$@" >/dev/null 2>&1; }
secret() {  # 없으면 무작위 값으로 만든다
  local name=$1 value=${2:-}
  if ! exists gc secrets describe "$name"; then
    value=${value:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')}
    printf '%s' "$value" | gc secrets create "$name" --data-file=- --replication-policy=automatic
  fi
}

echo "▶ API 사용 설정"
gc services enable run.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
  secretmanager.googleapis.com cloudscheduler.googleapis.com storage.googleapis.com

echo "▶ 서비스 계정"
exists gc iam service-accounts describe "$RUN_SA" ||
  gc iam service-accounts create baroerp-run --display-name="바로ERP Cloud Run"

echo "▶ Artifact Registry"
exists gc artifacts repositories describe "$REPO" --location="$REGION" ||
  gc artifacts repositories create "$REPO" --location="$REGION" --repository-format=docker

echo "▶ Cloud Storage 버킷(첨부 attachments/, 백업 backups/)"
exists gc storage buckets describe "gs://$BUCKET" ||
  gc storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access \
    --public-access-prevention
# 데모 데이터는 매일 처음 상태로 돌아가므로 첨부는 2일, 백업은 7일 뒤 지운다
LIFECYCLE=$(mktemp)
cat > "$LIFECYCLE" <<'JSON'
{"rule": [
  {"action": {"type": "Delete"}, "condition": {"age": 2, "matchesPrefix": ["attachments/"]}},
  {"action": {"type": "Delete"}, "condition": {"age": 7, "matchesPrefix": ["backups/"]}}
]}
JSON
gc storage buckets update "gs://$BUCKET" --lifecycle-file="$LIFECYCLE"
rm -f "$LIFECYCLE"
gc storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$RUN_SA" --role=roles/storage.objectAdmin >/dev/null

echo "▶ 비밀값"
secret baroerp-secret-key
secret baroerp-data-key "$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
secret baroerp-task-token
for s in baroerp-secret-key baroerp-data-key baroerp-task-token; do
  gc secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:$RUN_SA" --role=roles/secretmanager.secretAccessor >/dev/null
done

echo "▶ 이미지 빌드"
IMAGE=${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/app:setup-$(date +%Y%m%d%H%M%S)
gc builds submit --tag "$IMAGE" "$HERE/../.."

echo "▶ Cloud Run(1 vCPU·512MiB, 최소 0·최대 1 인스턴스, SQLite)"
gc run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" \
  --service-account="$RUN_SA" --allow-unauthenticated \
  --cpu=1 --memory=512Mi --cpu-boost --min-instances=0 --max-instances=1 --concurrency=40 \
  --set-env-vars="DEPLOY_MODE=single,DEMO_MODE=true,DEBUG=false,SCHEDULER_ENABLED=false,DATABASE_URL=sqlite:///./data/baroerp.db,STORAGE_BACKEND=gcs,STORAGE_BUCKET=$BUCKET" \
  --set-secrets="SECRET_KEY=baroerp-secret-key:latest,DATA_KEY=baroerp-data-key:latest,TASK_TOKEN=baroerp-task-token:latest"
URL=$(gc run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')

TOKEN=$(gc secrets versions access latest --secret=baroerp-task-token)
schedule() {  # 이름, cron(KST), 경로
  local args=(--location="$REGION" --schedule="$2" --time-zone="Asia/Seoul"
    --uri="$URL$3" --http-method=POST --headers="Authorization=Bearer $TOKEN"
    --attempt-deadline=300s)
  if exists gc scheduler jobs describe "$1" --location="$REGION"; then
    gc scheduler jobs update http "$1" "${args[@]}"
  else
    gc scheduler jobs create http "$1" "${args[@]}"
  fi
}
# 인스턴스가 하루 넘게 살아 있을 때를 위해 매일 처음 상태로(DB가 매일 바뀌어 앱 백업 작업은 두지 않는다)
echo "▶ Cloud Scheduler: 매일 04:00 KST 데모 데이터 처음 상태로"
schedule baroerp-demo-reset "0 4 * * *" /tasks/demo-reset

echo "▶ 데모 데이터 확인(첫 기동 때 자동으로 채워짐)"
curl -fsS -m 120 -o /dev/null "$URL/intro"

# Cloud Build 서비스 계정이 Cloud Run을 배포할 수 있게(cloudbuild.yaml의 run deploy)
PROJECT_NUMBER=$(gc projects describe "$PROJECT_ID" --format='value(projectNumber)')
CB_SA=${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com
gc projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$CB_SA" \
  --role=roles/run.admin --condition=None >/dev/null
gc iam service-accounts add-iam-policy-binding "$RUN_SA" --member="serviceAccount:$CB_SA" \
  --role=roles/iam.serviceAccountUser >/dev/null

cat <<DONE

✔ 데모 환경을 만들었어요: $URL
  - 첫 화면은 제품 소개(/intro)예요. 대표·영업·구매·관리 계정으로 로그인 없이 둘러볼 수 있어요
  - 데모 계정: demo1~4@example.com · 비밀번호 12345678 · 대표 간편 비밀번호 142536
  - 데이터는 인스턴스가 새로 뜰 때(한동안 요청이 없어 꺼진 뒤, 새 배포)와 매일 새벽 4시에 처음 상태로 돌아가요
  - 지금 바로 처음 상태로: curl -X POST -H "Authorization: Bearer \$(gcloud secrets versions access latest --secret=baroerp-task-token)" $URL/tasks/demo-reset
  - 시연 파일: uv run python -m app.demo files
  - main 브랜치 푸시 자동 배포: 콘솔 Cloud Build > 트리거에서 저장소를 연결하고
    구성 파일로 cloudbuild.yaml을 지정하세요(저장소 연결은 GitHub 인증이 필요해 스크립트로 하지 않아요)
  - 시연 중에 데이터가 초기화되지 않게(콜드 스타트도 없음):
    gcloud run services update $SERVICE --region=$REGION --min-instances=1   (끝나면 0)
DONE
