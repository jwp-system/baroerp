#!/bin/bash
# e2-micro DB VM 시작 스크립트(부팅마다 실행, 여러 번 실행해도 같은 결과).
# PostgreSQL 16 설치·소형 설정, 앱 계정 생성, 매일 pg_dump → Cloud Storage(7일 보관은 버킷 수명 주기).
set -euo pipefail

meta() { curl -sf -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/attributes/$1"; }
BUCKET=$(meta bucket)
APP_CIDR=$(meta app-cidr)
PG_SECRET=$(meta db-password-secret)

if ! command -v psql >/dev/null || ! psql --version | grep -q " 16"; then
  apt-get update
  apt-get install -y curl ca-certificates gnupg
  install -d /usr/share/postgresql-common/pgdg
  curl -sf -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc
  . /etc/os-release
  echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" \
    > /etc/apt/sources.list.d/pgdg.list
  apt-get update
  apt-get install -y postgresql-16
fi

CONF=/etc/postgresql/16/main
# RAM 1GB: 작게 설정
cat > "$CONF/conf.d/baroerp.conf" <<CONF
listen_addresses = '*'
shared_buffers = 128MB
max_connections = 20
work_mem = 4MB
maintenance_work_mem = 32MB
effective_cache_size = 384MB
timezone = 'UTC'
CONF
HBA_LINE="host baroerp baroerp ${APP_CIDR} scram-sha-256"
grep -qxF "$HBA_LINE" "$CONF/pg_hba.conf" || echo "$HBA_LINE" >> "$CONF/pg_hba.conf"
systemctl restart postgresql

DB_PASS=$(gcloud secrets versions access latest --secret="$PG_SECRET")
sudo -u postgres psql -v ON_ERROR_STOP=1 -v pass="$DB_PASS" <<'SQL'
SELECT 'CREATE ROLE baroerp LOGIN' WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'baroerp')\gexec
ALTER ROLE baroerp PASSWORD :'pass';
SELECT 'CREATE DATABASE baroerp OWNER baroerp' WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'baroerp')\gexec
SQL

cat > /usr/local/bin/baroerp-pgdump <<SCRIPT
#!/bin/bash
set -euo pipefail
sudo -u postgres pg_dump -Fc baroerp | gcloud storage cp - "gs://${BUCKET}/pgdump/baroerp-\$(date -u +%Y%m%d-%H%M).dump"
SCRIPT
chmod +x /usr/local/bin/baroerp-pgdump
# 매일 03:30 KST(18:30 UTC). 앱 내장 백업(03:00, Cloud Scheduler)과 별도로 DB 전체 덤프
echo "30 18 * * * root /usr/local/bin/baroerp-pgdump >> /var/log/baroerp-pgdump.log 2>&1" > /etc/cron.d/baroerp-pgdump
