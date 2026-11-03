"""최소 사양 성능 확인(기획안 9-1 데모 완료 기준: 화면 응답 1초 이내, 앱 메모리 300MB 이하).

    uv run python scripts/perf_check.py [--months 3] [--rounds 5]

빈 임시 DB에 데모 이력(app.demo seed)을 채우고, 주요 화면을 앱 안에서(TestClient) 여러 번 요청해
서버 처리 시간과 최대 메모리를 잰다. 네트워크·콜드 스타트는 포함하지 않는다.
"""

import argparse
import os
import resource
import statistics
import sys
import tempfile
import time

parser = argparse.ArgumentParser()
parser.add_argument("--months", type=int, default=3)
parser.add_argument("--rounds", type=int, default=5)
args = parser.parse_args()

tmp = tempfile.mkdtemp(prefix="baroerp-perf-")
os.environ.update(
    DATABASE_URL=f"sqlite:///{tmp}/perf.db",
    SCHEDULER_ENABLED="false",
    DEBUG="true",
    STORAGE_LOCAL_DIR=f"{tmp}/att",
    BACKUP_LOCAL_DIR=f"{tmp}/bak",
)

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import demo  # noqa: E402
from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base, Partner, Voucher  # noqa: E402

PAGES = ["/", "/sales", "/purchase", "/inventory", "/inventory/moves", "/receivables",
         "/payables", "/funds", "/tax-invoice/list", "/reports", "/master/items",
         "/master/partners", "/settings/audit"]  # fmt: skip

Base.metadata.create_all(engine)
t0 = time.perf_counter()
with SessionLocal() as db:
    demo.seed(db, args.months)
    vouchers = len(db.scalars(select(Voucher.id)).all())
    partner_id = db.scalar(select(Partner.id))
    sale_id = db.scalar(select(Voucher.id).where(Voucher.kind == "sales"))
print(f"데모 이력 {args.months}개월: 전표 {vouchers}건 ({time.perf_counter() - t0:.1f}초)")
PAGES += [f"/sales/{sale_id}", f"/sales/{sale_id}/print", f"/receivables/{partner_id}",
          f"/partners/{partner_id}"]  # fmt: skip
PAGES.insert(PAGES.index("/tax-invoice/list"), "/partners")
owner_email = demo.DEMO_ACCOUNTS["owner"][1]

with TestClient(app, follow_redirects=False) as c:
    token = c.get("/login").text.split('name="csrf_token" value="')[1].split('"')[0]
    c.post("/login", data={"email": owner_email, "password": demo.DEMO_PASSWORD,
                           "csrf_token": token})  # fmt: skip
    token = c.get("/login/pin").text.split('name="csrf_token" value="')[1].split('"')[0]
    c.post("/login/pin", data={"pin": demo.DEMO_PIN, "csrf_token": token})

    slow = []
    print(f"{'화면':<28}{'중앙값':>10}{'최대':>10}")
    for path in PAGES:
        times = []
        for _ in range(args.rounds):
            start = time.perf_counter()
            res = c.get(path)
            times.append(time.perf_counter() - start)
            assert res.status_code == 200, (path, res.status_code)
        worst = max(times)
        if worst > 1:
            slow.append(path)
        print(f"{path:<30}{statistics.median(times) * 1000:>8.0f}ms{worst * 1000:>8.0f}ms")

rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # Linux: KB
print(f"최대 메모리(RSS) {rss_mb:.0f}MB")
ok = not slow and rss_mb <= 300
print("✔ 기준 충족" if ok else f"✘ 기준 미달: 1초 초과 {slow}, 메모리 {rss_mb:.0f}MB")
sys.exit(0 if ok else 1)
