from pathlib import Path

from fastapi.testclient import TestClient


def settings_for(tmp_path: Path):
    from intel_daily.config import AppSettings

    return AppSettings(
        db_path=tmp_path / "intel.db",
        admin_username="admin",
        admin_password="correct-horse",
    )


def test_health_is_public(tmp_path: Path) -> None:
    from intel_daily.web import create_app

    app = create_app(settings_for(tmp_path))
    response = TestClient(app).get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_dashboard_requires_basic_auth(tmp_path: Path) -> None:
    from intel_daily.web import create_app

    app = create_app(settings_for(tmp_path))
    client = TestClient(app)

    unauthenticated = client.get("/")
    authenticated = client.get("/", auth=("admin", "correct-horse"))

    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["www-authenticate"] == "Basic"
    assert authenticated.status_code == 200
    assert "新能源每日情报" in authenticated.text


def test_post_test_send_requires_auth(tmp_path: Path) -> None:
    from intel_daily.web import create_app

    app = create_app(settings_for(tmp_path))
    response = TestClient(app).post("/test-send")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Basic"
    assert "立即测试发送" not in response.text


def test_post_test_send_shows_success_result(tmp_path: Path, monkeypatch) -> None:
    from intel_daily.web import create_app
    from intel_daily.worker import TestSendResult

    app = create_app(settings_for(tmp_path))
    monkeypatch.setattr(
        "intel_daily.worker.perform_test_send",
        lambda db_path: TestSendResult(
            ok=True, status="sent", message="已发送 3 条 · 120 字符", chars=120
        ),
    )

    response = TestClient(app).post("/test-send", auth=("admin", "correct-horse"))

    assert response.status_code == 200
    assert "已发送 3 条 · 120 字符" in response.text
    assert "立即测试发送" in response.text


def test_post_test_send_shows_error_message(tmp_path: Path, monkeypatch) -> None:
    from intel_daily.web import create_app
    from intel_daily.worker import TestSendResult

    app = create_app(settings_for(tmp_path))
    monkeypatch.setattr(
        "intel_daily.worker.perform_test_send",
        lambda db_path: TestSendResult(
            ok=False, status="error", message="飞书接口不可达: 模拟超时", chars=0
        ),
    )

    response = TestClient(app).post("/test-send", auth=("admin", "correct-horse"))

    assert response.status_code == 200
    assert "飞书接口不可达: 模拟超时" in response.text


def test_cross_origin_write_is_rejected(tmp_path: Path) -> None:
    """T10：带跨站 Origin 的写操作即使凭据正确也必须拒绝（补 Basic 认证的 CSRF 面）。"""
    from intel_daily.web import create_app

    app = create_app(settings_for(tmp_path))
    client = TestClient(app)

    response = client.post(
        "/sources/3/enabled",
        data={"enabled": "true"},
        auth=("admin", "correct-horse"),
        headers={"Origin": "http://evil.example.com"},
        follow_redirects=False,
    )

    assert response.status_code == 403
    from intel_daily.sources import SourceRepository

    assert SourceRepository(tmp_path / "intel.db").get(3).enabled is False


def test_cross_origin_write_rejected_even_without_credentials(tmp_path: Path) -> None:
    from intel_daily.web import create_app

    app = create_app(settings_for(tmp_path))

    response = TestClient(app).post(
        "/settings/schedule",
        data={},
        headers={"Origin": "https://attacker.test"},
    )

    assert response.status_code == 403


def test_same_origin_write_is_allowed(tmp_path: Path) -> None:
    from intel_daily.web import create_app

    app = create_app(settings_for(tmp_path))
    client = TestClient(app)  # 默认 Host 为 testserver

    response = client.post(
        "/sources/3/enabled",
        data={"enabled": "true"},
        auth=("admin", "correct-horse"),
        headers={"Origin": "http://testserver"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    from intel_daily.sources import SourceRepository

    assert SourceRepository(tmp_path / "intel.db").get(3).enabled is True


def test_write_without_origin_still_works(tmp_path: Path) -> None:
    """curl / 运维脚本不带 Origin，不能被同源校验误伤（现有测试也依赖此语义）。"""
    from intel_daily.web import create_app

    app = create_app(settings_for(tmp_path))

    response = TestClient(app).post(
        "/sources/3/enabled",
        data={"enabled": "true"},
        auth=("admin", "correct-horse"),
        follow_redirects=False,
    )

    assert response.status_code == 303


def test_read_requests_ignore_origin_check(tmp_path: Path) -> None:
    from intel_daily.web import create_app

    app = create_app(settings_for(tmp_path))

    response = TestClient(app).get(
        "/",
        auth=("admin", "correct-horse"),
        headers={"Origin": "http://evil.example.com"},
    )

    assert response.status_code == 200


def test_main_binds_loopback_by_default(monkeypatch, tmp_path: Path) -> None:
    """T10：`python -m intel_daily.web` 默认只绑 127.0.0.1，可用 INTEL_WEB_HOST 覆盖。"""
    import intel_daily.web as web_module

    captured: dict = {}

    def fake_run(app, **kwargs) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(web_module.uvicorn, "run", fake_run)
    monkeypatch.setenv("INTEL_DB_PATH", str(tmp_path / "intel.db"))
    monkeypatch.setenv("INTEL_ADMIN_USERNAME", "admin")
    monkeypatch.setenv("INTEL_ADMIN_PASSWORD", "correct-horse-battery")
    monkeypatch.delenv("INTEL_WEB_HOST", raising=False)

    web_module.main()

    assert captured["host"] == "127.0.0.1"

    captured.clear()
    monkeypatch.setenv("INTEL_WEB_HOST", "0.0.0.0")
    web_module.main()

    assert captured["host"] == "0.0.0.0"
