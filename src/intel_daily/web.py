import secrets
import os
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

from intel_daily.config import AppSettings
from intel_daily.db import init_database
from intel_daily.scheduling import (
    ScheduleSettings,
    SettingsRepository,
    next_due_after,
    normalize_lines,
)
from intel_daily.sources import SourceNotFoundError, SourceRepository
from intel_daily.runs import Run, RunRepository

security = HTTPBasic()
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

# 写操作必须同源（Basic 认证单独挡不住跨站表单/脚本提交）。
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _utc_now() -> datetime:
    """真实时钟入口（集中一处，便于测试注入，避免测试钉死具体日期）。"""
    return datetime.now(timezone.utc)


def _zone_of(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except Exception:
        return ZoneInfo("UTC")


def _origin_matches(origin: str, host: str) -> bool:
    """Origin 与请求 Host 是否同源（只比较主机名与端口）。"""
    parsed = urlparse(origin)
    if not parsed.netloc:
        return False
    return parsed.netloc.lower() == (host or "").lower()


def _as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _fmt_run_time(value: datetime | None, tz_name: str = "Asia/Shanghai") -> str:
    value = _as_aware(value)
    if value is None:
        return "—"
    try:
        zone = ZoneInfo(tz_name)
    except Exception:
        zone = ZoneInfo("UTC")
    return value.astimezone(zone).strftime("%Y-%m-%d %H:%M")


def _fmt_duration(run: Run) -> str:
    started = _as_aware(run.started_at)
    finished = _as_aware(run.finished_at)
    if started is None or finished is None:
        return "—"
    seconds = max(0, int((finished - started).total_seconds()))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, rest = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {rest} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 时 {minutes} 分"


templates.env.globals.update(
    fmt_run_time=_fmt_run_time,
    fmt_duration=_fmt_duration,
)


def require_admin(
    request: Request,
    credentials: Annotated[HTTPBasicCredentials, Depends(security)],
) -> str:
    settings: AppSettings = request.app.state.settings
    username_ok = secrets.compare_digest(
        credentials.username.encode("utf-8"),
        settings.admin_username.encode("utf-8"),
    )
    password_ok = secrets.compare_digest(
        credentials.password.encode("utf-8"),
        settings.admin_password.encode("utf-8"),
    )
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=401,
            detail="认证失败",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


def create_app(settings: AppSettings) -> FastAPI:
    init_database(settings.db_path)
    app = FastAPI(title="新能源每日情报")
    app.state.settings = settings
    app.mount(
        "/static",
        StaticFiles(directory=Path(__file__).parent / "static"),
        name="static",
    )

    @app.middleware("http")
    async def enforce_same_origin_writes(request: Request, call_next):
        """跨站写操作一律拒绝：Origin 存在但与本站 Host 不同源 → 403。

        浏览器发出的跨站 POST 必然带 Origin；命令行/健康检查等无 Origin 的
        请求不受影响（登录仍是 Basic 认证，这里只补 CSRF 面）。
        """
        if request.method in _WRITE_METHODS:
            origin = request.headers.get("origin")
            if origin and not _origin_matches(origin, request.headers.get("host", "")):
                return JSONResponse(
                    status_code=403,
                    content={"detail": "跨站写操作被拒绝（Origin 与本站不同源）"},
                )
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    def _dashboard_context(db_path: Path, test_send_result=None) -> dict:
        return {
            "page_title": "概览",
            "sources": SourceRepository(db_path).list_all(),
            "runs": RunRepository(db_path).list_recent(),
            "run_timezone": SettingsRepository(db_path).get_schedule().timezone,
            "test_send_result": test_send_result,
        }

    @app.get("/", response_class=HTMLResponse)
    def dashboard(
        request: Request,
        _: Annotated[str, Depends(require_admin)],
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=_dashboard_context(settings.db_path),
        )

    @app.post("/test-send", response_class=HTMLResponse)
    def test_send(
        request: Request,
        _: Annotated[str, Depends(require_admin)],
    ) -> HTMLResponse:
        from intel_daily.worker import perform_test_send

        result = perform_test_send(settings.db_path)
        return templates.TemplateResponse(
            request=request,
            name="dashboard.html",
            context=_dashboard_context(settings.db_path, test_send_result=result),
        )

    @app.get("/sources", response_class=HTMLResponse)
    def sources_page(
        request: Request,
        _: Annotated[str, Depends(require_admin)],
    ) -> HTMLResponse:
        repository = SourceRepository(settings.db_path)
        return templates.TemplateResponse(
            request=request,
            name="sources.html",
            context={
                "page_title": "信源",
                "sources": repository.list_all(),
            },
        )

    @app.post("/sources/{source_id}/enabled")
    def set_source_enabled(
        source_id: int,
        _: Annotated[str, Depends(require_admin)],
        enabled: Annotated[str, Form()],
    ) -> RedirectResponse:
        enabled_values = {"true": True, "false": False}
        if enabled not in enabled_values:
            raise HTTPException(status_code=422, detail="enabled 必须是 true 或 false")
        try:
            SourceRepository(settings.db_path).set_enabled(
                source_id,
                enabled_values[enabled],
            )
        except SourceNotFoundError as error:
            raise HTTPException(status_code=404, detail="信源不存在") from error
        return RedirectResponse(url="/sources", status_code=303)

    @app.post("/sources/{source_id}/ai")
    def set_source_external_ai(
        source_id: int,
        _: Annotated[str, Depends(require_admin)],
        allow: Annotated[str, Form()],
    ) -> RedirectResponse:
        allow_values = {"true": True, "false": False}
        if allow not in allow_values:
            raise HTTPException(status_code=422, detail="allow 必须是 true 或 false")
        try:
            SourceRepository(settings.db_path).set_allow_external_ai(
                source_id,
                allow_values[allow],
            )
        except SourceNotFoundError as error:
            raise HTTPException(status_code=404, detail="信源不存在") from error
        return RedirectResponse(url="/sources", status_code=303)

    @app.get("/digest", response_class=HTMLResponse)
    def digest_page(
        request: Request,
        _: Annotated[str, Depends(require_admin)],
    ) -> HTMLResponse:
        from intel_daily.digest import plan_entries
        from intel_daily.notifier import fetch_items

        # 日期标签与取数窗口都按配置时区（与 worker 的“当日”定义保持一致），
        # 不再用 UTC 日期：跨零点的条目曾让页面标题与简报对不上。
        schedule = SettingsRepository(settings.db_path).get_schedule()
        now = _utc_now()
        date_label = now.astimezone(_zone_of(schedule.timezone)).date().isoformat()
        plan = plan_entries(fetch_items(settings.db_path, now=now), ai=None)
        return templates.TemplateResponse(
            request=request,
            name="digest.html",
            context={
                "page_title": "今日情报",
                "date_label": date_label,
                "grouped": plan.grouped,
            },
        )

    @app.get("/runs", response_class=HTMLResponse)
    def runs_page(
        request: Request,
        _: Annotated[str, Depends(require_admin)],
    ) -> HTMLResponse:
        db_path = settings.db_path
        return templates.TemplateResponse(
            request=request,
            name="runs.html",
            context={
                "page_title": "运行状态",
                "runs": RunRepository(db_path).list_recent(),
                "run_timezone": SettingsRepository(db_path).get_schedule().timezone,
            },
        )

    @app.get("/runs.json")
    def runs_json(
        _: Annotated[str, Depends(require_admin)],
    ) -> list[dict[str, object]]:
        return [
            {
                "id": run.id,
                "kind": run.kind,
                "status": run.status,
                "status_label": run.status_label,
                "finished_at": (
                    run.finished_at.isoformat() if run.finished_at else None
                ),
                "collected_count": run.collected_count,
                "selected_count": run.selected_count,
                "sent_count": run.sent_count,
                "failed_count": run.failed_count,
                "error_summary": run.error_summary,
            }
            for run in RunRepository(settings.db_path).list_recent(limit=50)
        ]

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(
        request: Request,
        _: Annotated[str, Depends(require_admin)],
    ) -> HTMLResponse:
        schedule = SettingsRepository(settings.db_path).get_schedule()
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                "page_title": "关注与推送",
                "schedule": schedule,
                "next_due": next_due_after(
                    _utc_now(),
                    schedule,
                ),
                "feishu_configured": bool(os.getenv("INTEL_FEISHU_WEBHOOK")),
                "ai_configured": bool(os.getenv("INTEL_AI_API_KEY")),
            },
        )

    @app.post("/settings/schedule")
    def save_schedule(
        _: Annotated[str, Depends(require_admin)],
        timezone_name: Annotated[str, Form(alias="timezone")],
        daily_time: Annotated[str, Form()],
        weekdays_only: Annotated[str, Form()],
        topics: Annotated[str, Form()],
        keywords: Annotated[str, Form()],
        exclude_words: Annotated[str, Form()],
    ) -> RedirectResponse:
        try:
            ZoneInfo(timezone_name)
        except Exception as error:
            raise HTTPException(status_code=422, detail="时区无效") from error
        try:
            parsed_daily_time = time.fromisoformat(daily_time)
        except ValueError as error:
            raise HTTPException(status_code=422, detail="推送时间格式应为 HH:MM") from error
        weekdays_only_values = {"true": True, "false": False}
        if weekdays_only not in weekdays_only_values:
            raise HTTPException(status_code=422, detail="仅工作日必须是 true 或 false")
        saved = ScheduleSettings(
            timezone=timezone_name,
            daily_time=parsed_daily_time,
            weekdays_only=weekdays_only_values[weekdays_only],
            topics=normalize_lines(topics),
            keywords=normalize_lines(keywords),
            exclude_words=normalize_lines(exclude_words),
        )
        SettingsRepository(settings.db_path).save_schedule(saved)
        return RedirectResponse(url="/settings", status_code=303)

    return app


import uvicorn


def main() -> None:
    settings = AppSettings.from_env()
    # 直接运行默认只绑回环：控制台的写操作（改信源/测试发送）不应暴露到局域网；
    # 容器内需要经端口映射可达时，显式设置 INTEL_WEB_HOST=0.0.0.0。
    uvicorn.run(
        create_app(settings),
        host=os.getenv("INTEL_WEB_HOST", "127.0.0.1"),
        port=int(os.getenv("INTEL_PORT", "8000")),
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
