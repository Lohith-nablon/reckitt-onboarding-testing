"""The REST API the front end uses, screen by screen."""
from fastapi.testclient import TestClient

from onboarding_poc.api.app import create_app


def test_screens_end_to_end(cfg, svc):
    c = TestClient(create_app(cfg, svc))
    H = {"x-dev-user": "ui.user@nablon.ai"}
    assert c.get("/api/health").json()["ok"]
    assert len(c.get("/api/samples").json()) == 3
    # Start onboarding
    sid = c.post("/api/sessions", json={"name": "Tesco", "market": "UK", "retailer": "TESCO"}, headers=H).json()["session_id"]
    form = c.get(f"/api/sessions/{sid}/form").json()
    assert form["fields"] and "completeness" in form
    c.put(f"/api/sessions/{sid}/profile", json={"values": {"retailer_contact_name": "A. Buyer"}}, headers=H)
    # Add files
    data = svc.ctx.storage.read_bytes(svc.samples["tesco_daily"])
    r = c.post(f"/api/sessions/{sid}/files", files=[("files", ("tesco.csv", data, "text/csv")),
                                                    ("files", ("bad.exe", b"x", "application/octet-stream"))], headers=H).json()
    assert len(r["added"]) == 1 and len(r["errors"]) == 1
    fid = r["added"][0]["file_id"]
    assert c.get(f"/api/sessions/{sid}/files/{fid}/preview").json()["sheets"][0]["rows"]
    # AI Discovery + Mapping
    c.post(f"/api/sessions/{sid}/analyse", headers=H)
    rep = c.get(f"/api/sessions/{sid}/discovery/{fid}").json()
    assert rep["sheets"][0]["columns"]
    maps = c.get(f"/api/sessions/{sid}/mappings").json()
    mid = next(m["mapping_id"] for m in maps if m["target_table"] == "bronze.pos_sellout")
    canvas = c.get(f"/api/sessions/{sid}/mappings/{mid}/canvas").json()
    assert canvas["sources"] and canvas["targets"] and canvas["links"]
    assert c.get(f"/api/sessions/{sid}/mappings/{mid}/preview").json()["rows"]
    assert c.get(f"/api/sessions/{sid}/mappings/{mid}/veritas").status_code == 200
    bad = c.put(f"/api/sessions/{sid}/mappings/{mid}/links/attribute_1", json={"change": {"action": "DIRECT_COLUMN",
                "source_columns": ["missing"]}}, headers=H)
    assert bad.status_code == 400
    for x in c.get(f"/api/sessions/{sid}/conversions").json():
        if x["issue"] and x["status"] == "proposed":
            c.post(f"/api/sessions/{sid}/conversions/{x['rule_id']}", json={"approve": True}, headers=H)
    r = c.post(f"/api/sessions/{sid}/gates/A/approve", json={}, headers=H)
    assert r.status_code == 200 and r.json()["published"]["items"], r.text
    # Pipeline hand-off
    p = c.get(f"/api/sessions/{sid}/pipeline").json()
    assert not p["all_loaded"] and p["can_run_locally"]
    assert "INSERT INTO" in c.get(f"/api/sessions/{sid}/pipeline/sql/{p['items'][0]['mapping_id']}").text
    assert c.post(f"/api/sessions/{sid}/pipeline/confirm", json={}, headers=H).status_code == 400
    assert c.post(f"/api/sessions/{sid}/pipeline/run-locally", headers=H).json()["all_loaded"]
    assert c.post(f"/api/sessions/{sid}/pipeline/confirm", json={"note": "run"}, headers=H).status_code == 200
    # Harmonisation
    c.post(f"/api/sessions/{sid}/harmonisation/run", headers=H)
    c.post(f"/api/sessions/{sid}/harmonisation/codes/bulk-accept", json={"min_confidence": 0.0}, headers=H)
    h = c.get(f"/api/sessions/{sid}/harmonisation").json()
    assert h["bronze"] and h["started"]
    for x in h["value_rules"]:
        if x["status"] == "proposed":
            c.post(f"/api/sessions/{sid}/harmonisation/standards/{x['rule_id']}", json={}, headers=H)
    for x in [r for r in h["codes"] if r["status"] == "needs_review" and not r["decision"]]:
        c.post(f"/api/sessions/{sid}/harmonisation/codes/{x['match_id']}", json={"action": "leave_empty"}, headers=H)
    r = c.post(f"/api/sessions/{sid}/gates/B/approve", json={}, headers=H)
    assert r.status_code == 200, r.text
    # Validate & sign off
    v = c.post(f"/api/sessions/{sid}/validate", headers=H).json()
    assert all(not x["blocking"] for x in v)
    assert c.get(f"/api/sessions/{sid}/signoff").json()["ready"]
    m = c.post(f"/api/sessions/{sid}/gates/C/approve", json={"note": "ok"}, headers=H).json()
    assert m["signed_off_by"] == "ui.user@nablon.ai"
    assert any(x["kind"] == "silver" for x in c.get(f"/api/sessions/{sid}/silver").json())
    outs = c.get(f"/api/sessions/{sid}/outputs").json()
    k = next(o["key"] for o in outs if o["key"].endswith(".sql"))
    assert c.get(f"/api/sessions/{sid}/outputs/download", params={"key": k}).status_code == 200
    assert c.get(f"/api/sessions/{sid}/outputs/download", params={"key": "state/x"}).status_code == 400
    # Journey + trace
    j = c.get(f"/api/sessions/{sid}/journey").json()
    assert j["status"] == "signed_off" and "ui.user@nablon.ai" in j["decisions"]["people"]
    assert c.get(f"/api/sessions/{sid}/trace", params={"module": "M3"}).json()
    assert c.get("/api/library/mappings").json()
    assert c.get("/api/sessions/nope").status_code == 404


def test_serves_front_end(svc):
    from pathlib import Path
    from onboarding_poc.config import load_config
    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if not (dist / "index.html").is_file():
        import pytest
        pytest.skip("front end not built")
    cfg = load_config(path="/nonexistent", storage={"backend": "local", "local_root": svc.ctx.cfg.storage.local_root},
                      frontend_dist=str(dist))
    c = TestClient(create_app(cfg, svc))
    assert "<div id=\"root\">" in c.get("/").text
    assert "<div id=\"root\">" in c.get("/s/abc/mapping").text      # SPA fallback
    assert c.get("/api/health").json()["ok"]
