def test_healthz(client):
    res = client.get("/healthz")
    assert res.status_code == 200
    assert res.json()["status"] == "ok"


def test_dashboard_renders(owner_client):
    res = owner_client.get("/")
    assert res.status_code == 200
    assert "이번 달 돈의 흐름" in res.text
