def test_health_is_live_without_db(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
