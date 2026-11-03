#!/bin/bash
# 바로ERP GCP 데모 환경 구성(기획안 9-1 '데모 환경 구성'). 여러 번 실행해도 같은 결과가 되도록 만든다.
#
#   PROJECT_ID=my-project ./deploy/gcp/setup.sh
#
# 만드는 것: Artifact Registry, Cloud Storage 버킷(첨부·백업), Secret Manager 비밀값,
#   e2-micro PostgreSQL VM(외부 IP 없음), 방화벽, Cloud Run 서비스(Direct VPC egress),
#   Cloud Scheduler 백업 작업. 결제 예산 알림은 콘솔에서 따로 설정한다(월 1만 원).
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID를 지정해 주세요}"
REGION=${REGION:-us-central1}            # 무료 등급 리전: us-west1·us-central1·us-east1
ZONE=${ZONE:-${REGION}-a}
NETWORK=${NETWORK:-default}
SUBNET=${SUBNET:-default}
SERVICE=${SERVICE:-baroerp-demo}
REPO=${REPO:-baroerp}
VM=${VM:-baroerp-db}
BUCKET=${BUCKET:-${PROJECT_ID}-baroerp-demo}
RUN_SA=baroerp-run@${PROJECT_ID}.iam.gserviceaccount.com
DB_SA=baroerp-db@${PROJECT_ID}.iam.gserviceaccount.com
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
gc services enable run.googleapis.com compute.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com \
  storage.googleapis.com iap.googleapis.com

echo "▶ 서비스 계정"
exists gc iam service-accounts describe "$RUN_SA" ||
  gc iam service-accounts create baroerp-run --display-name="바로ERP Cloud Run"
exists gc iam service-accounts describe "$DB_SA" ||
  gc iam service-accounts create baroerp-db --display-name="바로ERP DB VM"

echo "▶ Artifact Registry"
exists gc artifacts repositories describe "$REPO" --location="$REGION" ||
  gc artifacts repositories create "$REPO" --location="$REGION" --repository-format=docker

echo "▶ Cloud Storage 버킷(첨부 attachments/, 앱 백업 backups/, DB 덤프 pgdump/)"
exists gc storage buckets describe "gs://$BUCKET" ||
  gc storage buckets create "gs://$BUCKET" --location="$REGION" --uniform-bucket-level-access \
    --public-access-prevention
cat > /tmp/baroerp-lifecycle.json <<'JSON'
{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 7, "matchesPrefix": ["pgdump/"]}}]}
JSON
gc storage buckets update "gs://$BUCKET" --lifecycle-file=/tmp/baroerp-lifecycle.json
gc storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$RUN_SA" --role=roles/storage.objectAdmin >/dev/null
gc storage buckets add-iam-policy-binding "gs://$BUCKET" \
  --member="serviceAccount:$DB_SA" --role=roles/storage.objectCreator >/dev/null

echo "▶ 비밀값"
secret baroerp-db-password
secret baroerp-secret-key
secret baroerp-data-key "$(python3 -c 'import base64, os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
secret baroerp-task-token
gc secrets add-iam-policy-binding baroerp-db-password \
  --member="serviceAccount:$DB_SA" --role=roles/secretmanager.secretAccessor >/dev/null

echo "▶ 네트워크: 비공개 Google 액세스(외부 IP 없는 VM → Cloud Storage), 방화벽"
APP_CIDR=$(gc compute networks subnets describe "$SUBNET" --region="$REGION" --format='value(ipCidrRange)')
gc compute networks subnets update "$SUBNET" --region="$REGION" --enable-private-ip-google-access
exists gc compute firewall-rules describe baroerp-db-from-run ||
  gc compute firewall-rules create baroerp-db-from-run --network="$NETWORK" \
    --allow=tcp:5432 --source-ranges="$APP_CIDR" --target-tags=baroerp-db
exists gc compute firewall-rules describe baroerp-db-iap-ssh ||
  gc compute firewall-rules create baroerp-db-iap-ssh --network="$NETWORK" \
    --allow=tcp:22 --source-ranges=35.235.240.0/20 --target-tags=baroerp-db

echo "▶ DB VM(e2-micro, PostgreSQL 16)"
if ! exists gc compute instances describe "$VM" --zone="$ZONE"; then
  # 패키지 설치 동안만 임시 외부 IP를 붙이고, 설치가 끝나면 뗀다(외부 IPv4는 과금)
  gc compute instances create "$VM" --zone="$ZONE" --machine-type=e2-micro \
    --image-family=debian-12 --image-project=debian-cloud \
    --boot-disk-size=30GB --boot-disk-type=pd-standard \
    --network="$NETWORK" --subnet="$SUBNET" --tags=baroerp-db \
    --service-account="$DB_SA" --scopes=cloud-platform \
    --metadata="bucket=$BUCKET,app-cidr=$APP_CIDR,db-password-secret=baroerp-db-password" \
    --metadata-from-file=startup-script="$HERE/vm-startup.sh"
  echo "  PostgreSQL 설치를 기다리는 중…"
  for _ in $(seq 1 60); do
    if gc compute instances get-serial-port-output "$VM" --zone="$ZONE" 2>/dev/null |
      grep -q "startup-script exit status 0"; then
      break
    fi
    sleep 10
  done
  gc compute instances delete-access-config "$VM" --zone="$ZONE" --access-config-name="external-nat" || true
fi
DB_IP=$(gc compute instances describe "$VM" --zone="$ZONE" --format='value(networkInterfaces[0].networkIP)')
DB_PASS=$(gc secrets versions access latest --secret=baroerp-db-password)
secret baroerp-database-url "postgresql+psycopg://baroerp:${DB_PASS}@${DB_IP}:5432/baroerp"

for s in baroerp-database-url baroerp-secret-key baroerp-data-key baroerp-task-token; do
  gc secrets add-iam-policy-binding "$s" \
    --member="serviceAccount:$RUN_SA" --role=roles/secretmanager.secretAccessor >/dev/null
done

echo "▶ 이미지 빌드"
IMAGE=${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/app:setup-$(date +%Y%m%d%H%M%S)
gc builds submit --tag "$IMAGE" "$HERE/../.."

echo "▶ Cloud Run(1 vCPU·512MiB, 최소 0·최대 1 인스턴스)"
gc run deploy "$SERVICE" --image="$IMAGE" --region="$REGION" \
  --service-account="$RUN_SA" --allow-unauthenticated \
  --cpu=1 --memory=512Mi --min-instances=0 --max-instances=1 --concurrency=40 \
  --network="$NETWORK" --subnet="$SUBNET" --vpc-egress=private-ranges-only \
  --set-env-vars="DEPLOY_MODE=single,DEMO_MODE=true,DEBUG=false,SCHEDULER_ENABLED=false,STORAGE_BACKEND=gcs,STORAGE_BUCKET=$BUCKET,DB_POOL_SIZE=5" \
  --set-secrets="DATABASE_URL=baroerp-database-url:latest,SECRET_KEY=baroerp-secret-key:latest,DATA_KEY=baroerp-data-key:latest,TASK_TOKEN=baroerp-task-token:latest"
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
echo "▶ Cloud Scheduler: 매일 03:00 KST 앱 백업, 04:00 KST 데모 데이터 처음 상태로"
schedule baroerp-backup "0 3 * * *" /tasks/backup
schedule baroerp-demo-reset "0 4 * * *" /tasks/demo-reset

echo "▶ 데모 데이터 채우기(데모 계정·3개월 거래 이력)"
curl -fsS -m 300 -X POST -H "Authorization: Bearer $TOKEN" "$URL/tasks/demo-reset"
echo

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
  - 데이터는 매일 새벽 4시에 처음 상태로 돌아가요(이 스크립트를 다시 실행해도 처음 상태로)
  - 시연 파일: uv run python -m app.demo files
  - main 브랜치 푸시 자동 배포: 콘솔 Cloud Build > 트리거에서 저장소를 연결하고
    구성 파일로 cloudbuild.yaml을 지정하세요(저장소 연결은 GitHub 인증이 필요해 스크립트로 하지 않아요)
  - 시연 시간에만 콜드 스타트 없애기:
    gcloud run services update $SERVICE --region=$REGION --min-instances=1   (끝나면 0)
DONE
