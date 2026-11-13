from sift.core.migrations import bundled_migrations

LATEST = bundled_migrations()[-1].version
from fastapi import FastAPI
from fastapi.testclient import TestClient

from sift.server.app import create_app
from sift.server.errors import install_error_handlers
from sift.server.security import SecurityMiddleware
from tests.helpers import H, make_client


def test_status_requires_token(client):
    r = client.get("/api/status")
    assert r.status_code == 401
    assert r.json() == {
        "code": "bad_token",
        "message": "This request is not from the Sift window.",
        "detail": None,
    }


def test_status_ok(client):
    body = client.get("/api/status", headers=H).json()
    assert body["app"]["version"] == "0.1.0" and body["app"]["schema_version"] == LATEST
    assert body["device"] is None and body["ssd"] is None
    assert body["mac_folder"]["path"].endswith("PhoneBackup")
    assert body["mac_folder"]["free_bytes"] > 0


def test_status_reports_config_warnings(paths):
    paths.config.write_text('[phone]\nadb_server_port = "abc"\n')
    client = make_client(paths)
    w = client.get("/api/status", headers=H).json()["app"]["config_warnings"]
    assert any("phone.adb_server_port" in x for x in w)


def test_wrong_host_rejected(client):
    r = client.get("/api/status", headers={**H, "host": "evil.example:8765"})
    assert r.status_code == 403 and r.json()["code"] == "bad_host"
    assert r.json()["message"] == "This request was refused."


def test_wrong_token_rejected(client):
    r = client.get("/api/status", headers={"X-Sift-Token": "nope"})
    assert r.status_code == 401 and r.json()["code"] == "bad_token"


def test_token_in_query_not_accepted_for_status(client):
    assert client.get("/api/status?token=t0k").status_code == 401


def test_non_ascii_token_is_a_401_not_a_crash(client):
    r = client.get("/api/status?token=%C3%A9")
    assert r.status_code == 401


def test_no_cors_headers(client):
    r = client.get("/api/status", headers={**H, "origin": "http://evil.example"})
    assert "access-control-allow-origin" not in r.headers


def test_unknown_api_path_is_json_404(client):
    r = client.get("/api/nope", headers=H)
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_unknown_api_path_needs_token_first(client):
    assert client.get("/api/nope").status_code == 401


def test_non_api_path_needs_no_token(client):
    r = client.get("/nothing-here")
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_non_api_path_still_checks_host(client):
    r = client.get("/nothing-here", headers={"host": "evil.example:8765"})
    assert r.status_code == 403 and r.json()["code"] == "bad_host"


def test_query_token_accepted_only_for_events_and_media(ctx):
    # Middleware only, over stub routes: the real /api/events would stream forever.
    app = FastAPI()
    app.add_middleware(SecurityMiddleware, ctx=ctx)
    install_error_handlers(app)

    @app.get("/api/events")
    def events() -> dict[str, str]:
        return {"ok": "events"}

    @app.get("/api/media/{name}")
    def media(name: str) -> dict[str, str]:
        return {"ok": name}

    @app.post("/api/events")
    def post_events() -> dict[str, str]:
        return {"ok": "post"}

    c = TestClient(app, base_url="http://127.0.0.1:8765")
    assert c.get("/api/events?token=t0k").json() == {"ok": "events"}
    assert c.get("/api/media/x.jpg?token=t0k").json() == {"ok": "x.jpg"}
    assert c.get("/api/events?token=wrong").status_code == 401
    assert c.post("/api/events?token=t0k").status_code == 401
    assert c.get("/api/eventsX?token=t0k").status_code == 401


def test_validation_error_is_422_with_detail(ctx):
    app = create_app(ctx)

    @app.get("/api/num")
    def num(n: int) -> int:
        return n

    c = TestClient(app, base_url="http://127.0.0.1:8765")
    r = c.get("/api/num?n=abc", headers=H)
    assert r.status_code == 422
    body = r.json()
    assert body["code"] == "invalid_request"
    assert isinstance(body["detail"], list) and body["detail"]


def test_api_error_uses_its_fields(ctx):
    from sift.server.errors import ApiError

    app = create_app(ctx)

    @app.get("/api/teapot")
    def teapot() -> None:
        raise ApiError(418, "teapot", "I am a teapot.", {"a": 1})

    c = TestClient(app, base_url="http://127.0.0.1:8765")
    r = c.get("/api/teapot", headers=H)
    assert r.status_code == 418
    assert r.json() == {"code": "teapot", "message": "I am a teapot.", "detail": {"a": 1}}


def test_unhandled_error_hides_traceback(ctx, caplog):
    app = create_app(ctx)

    @app.get("/api/boom")
    def boom():
        raise RuntimeError("secret internals")

    r = TestClient(app, base_url="http://127.0.0.1:8765", raise_server_exceptions=False).get(
        "/api/boom", headers=H
    )
    assert r.status_code == 500
    assert r.json() == {
        "code": "internal",
        "message": "Something went wrong. Details are in Logs.",
        "detail": None,
    }
    assert "secret internals" not in r.text
    # The traceback goes to the log file, never to the client.
    logged = [rec for rec in caplog.records if rec.name == "sift.server"]
    assert logged and logged[0].exc_info is not None
