#!/bin/bash
# 데모 환경(Cloud Run)에서 간편 비밀번호(2단계 인증) 초기화·잠금 해제.
# Cloud Run 서비스에는 접속할 수 없으므로, 같은 이미지·DB 설정으로 Cloud Run 작업을 한 번 실행한다.
#
#   PROJECT_ID=my-project ./deploy/gcp/reset-pin.sh owner@demo.kr
set -euo pipefail

: "${PROJECT_ID:?PROJECT_ID를 지정해 주세요}"
EMAIL=${1:?초기화할 사용자 이메일을 지정해 주세요}
REGION=${REGION:-us-central1}
NETWORK=${NETWORK:-default}
SUBNET=${SUBNET:-default}
SERVICE=${SERVICE:-baroerp-demo}
JOB=${JOB:-baroerp-manage}
RUN_SA=baroerp-run@${PROJECT_ID}.iam.gserviceaccount.com

gc() { gcloud --project "$PROJECT_ID" "$@"; }
exists() { "$@" >/dev/null 2>&1; }

# 지금 서비스가 쓰는 이미지로 실행해야 DB 버전이 맞는다
IMAGE=$(gc run services describe "$SERVICE" --region="$REGION" \
  --format='value(spec.template.spec.containers[0].image)')
JOB_ARGS=(--image="$IMAGE" --region="$REGION" --service-account="$RUN_SA"
  --network="$NETWORK" --subnet="$SUBNET" --vpc-egress=private-ranges-only
  --set-env-vars="DEPLOY_MODE=single,DEMO_MODE=true,SCHEDULER_ENABLED=false"
  --set-secrets="DATABASE_URL=baroerp-database-url:latest,SECRET_KEY=baroerp-secret-key:latest,DATA_KEY=baroerp-data-key:latest"
  --command=python --args="-m,app.manage,reset-pin,--email,$EMAIL"
  --max-retries=0 --task-timeout=120s)
if exists gc run jobs describe "$JOB" --region="$REGION"; then
  gc run jobs update "$JOB" "${JOB_ARGS[@]}"
else
  gc run jobs create "$JOB" "${JOB_ARGS[@]}"
fi
gc run jobs execute "$JOB" --region="$REGION" --wait
echo "✔ $EMAIL 간편 비밀번호를 초기화했어요. 다음 로그인 때 새로 등록해요"
