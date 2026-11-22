"""/api/arr router: auth required, lists and grabs readable, admin-only imports."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.fastapi import arr
from src.api.fastapi.auth_dependencies import require_admin, require_authentication
from src.services.media_list_service import load_billboard_entries
from tests.unit._arr_db import arr_db, set_settings  # noqa: F401


def make_client(user=True, admin=True):
    app = FastAPI()
    app.include_router(arr.router)
    if user:
        app.dependency_overrides[require_authentication] = lambda: {
            "authenticated": True
        }
    if admin:
        app.dependency_overrides[require_admin] = lambda: {
            "authenticated": True,
            "role": "admin",
        }
    return TestClient(app)


def test_requires_a_session():
    response = make_client(user=False, admin=False).get("/api/arr/lists")
    assert response.status_code == 401


def test_status_lists_and_grabs(arr_db):
    set_settings(lists_auto_add_wanted=True)
    load_billboard_entries(
        [{"year": 1984, "rank": 1, "title": "When Doves Cry", "artist": "Prince"}]
    )
    client = make_client()
    status = client.get("/api/arr/status").json()
    assert status["indexers"]["usable"] is False
    assert status["youtube_cooldown_seconds"] == 0

    lists = client.get("/api/arr/lists").json()["lists"]
    assert (
        lists[0]["name"] == "Billboard Year-End Hot 100 1984"
        and lists[0]["entries"] == 1
    )
    assert client.post("/api/arr/lists/link").json()["created"] == 1
    detail = client.get(f"/api/arr/lists/{lists[0]['id']}").json()
    assert detail["entries"][0]["status"] == "WANTED"
    assert client.get("/api/arr/lists/999").status_code == 404
    assert client.get("/api/arr/indexer/grabs").json() == {"grabs": []}


def test_library_import_is_admin_only_and_checks_the_path(arr_db, tmp_path):
    assert (
        make_client(admin=False)
        .post("/api/arr/library/import", json={"path": str(tmp_path)})
        .status_code
        == 401
    )
    client = make_client()
    assert (
        client.post(
            "/api/arr/library/import", json={"path": "/nope/missing"}
        ).status_code
        == 400
    )
    result = client.post(
        "/api/arr/library/import", json={"path": str(tmp_path), "dry_run": True}
    ).json()
    assert result["files"] == 0 and result["dry_run"] is True
