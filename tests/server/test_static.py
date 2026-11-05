import pytest

from sift.server.static import _file_inside
from tests.helpers import H, make_client


@pytest.fixture
def static_client(paths, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<div id=root>")
    (dist / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("TOP-SECRET")
    return make_client(paths, web_dist=dist)


def test_root_and_spa_routes_serve_index(static_client):
    for path in ("/", "/backup", "/cleanup/review"):
        r = static_client.get(path)
        assert r.status_code == 200 and "<div id=root>" in r.text


def test_assets_served_without_token(static_client):
    r = static_client.get("/assets/app.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]


def test_api_paths_not_swallowed_by_fallback(static_client):
    assert static_client.get("/api/nope", headers=H).json()["code"] == "not_found"


def test_unbuilt_ui_message(client):
    assert "make web" in client.get("/").text


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/assets/../../secret.txt",
        "/%2e%2e/secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "/assets/..%2f..%2fsecret.txt",
        "/%2e%2e%2fsecret.txt",
        "//etc/passwd",
        "/assets//etc/passwd",
    ],
)
def test_traversal_never_leaves_web_dist(static_client, path):
    r = static_client.get(path)
    assert "TOP-SECRET" not in r.text and "root:" not in r.text
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert "<div id=root>" in r.text


def test_static_requires_allowed_host(static_client):
    r = static_client.get("/", headers={"Host": "evil.example"})
    assert r.status_code == 403


def test_file_inside_refuses_escapes(tmp_path):
    dist = (tmp_path / "dist").resolve()
    (dist / "assets").mkdir(parents=True)
    (dist / "assets" / "app.js").write_text("x")
    (tmp_path / "secret.txt").write_text("TOP-SECRET")
    (dist / "link").symlink_to(tmp_path / "secret.txt")
    assert _file_inside(dist, "/assets/app.js") == dist / "assets" / "app.js"
    for rel in ("/../secret.txt", "/assets/../../secret.txt", "/etc/passwd", "/link", "/a\0b"):
        assert _file_inside(dist, rel) is None
