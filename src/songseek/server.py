import asyncio
import json
import queue
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import Field, SecretStr
from starlette.exceptions import HTTPException

from .config import Config
from .errors import SongSeekError, logger, public_error
from .models import Model, ParsedRow, Source
from .service import Service


class CreatePlaylist(Model):
    name: str = Field(min_length=1, max_length=200)
    source: Source
    rows: list[ParsedRow]


class RenamePlaylist(Model):
    name: str = Field(min_length=1, max_length=200)


class UpdateTrack(Model):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    artist: str | None = Field(default=None, max_length=500)
    album: str | None = Field(default=None, max_length=500)
    duration: float | None = Field(default=None, gt=0)
    note: str | None = Field(default=None, max_length=2000)


class Choose(Model):
    candidate_id: int = Field(gt=0)


class SearchOne(Model):
    query: str = Field(min_length=1, max_length=1000)
    duration: float | None = Field(default=None, gt=0)


class SearchTrack(Model):
    query: str | None = Field(default=None, min_length=1, max_length=1000)
    providers: list[str] | None = None


class TrackIds(Model):
    track_ids: list[int] | None = None


class Batch(Model):
    ids: list[int]
    action: Literal["accept_best", "skip", "unskip", "retry"]


class ProviderUpdate(Model):
    id: str
    enabled: bool
    order: int = Field(ge=0)


class SettingsUpdate(Model):
    output_dir: str | None = None
    filename_template: str | None = Field(default=None, max_length=500)
    group_by_playlist: bool | None = None
    proxy: str | None = None
    search_concurrency: int | None = Field(default=None, ge=1, le=32)
    download_concurrency: int | None = Field(default=None, ge=1, le=16)
    auto_approve: bool | None = None
    netease_cookie_set: bool | None = None
    netease_cookie: SecretStr | None = None


class ParseInput(Model):
    text: str | None = Field(default=None, max_length=20 * 1024 * 1024)
    url: str | None = Field(default=None, max_length=2000)
    mapping: dict | None = None


def error_response(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(config: Config | None = None, service: Service | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        app.state.service = service or Service(config)
        try:
            yield
        finally:
            app.state.service.close()

    app = FastAPI(title="觅音 SongSeek", lifespan=lifespan)

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        if request.url.hostname not in ("127.0.0.1", "localhost", "testserver"):
            return error_response("仅允许本机地址访问", "invalid_host", 400)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).hostname not in ("127.0.0.1", "localhost"):
                return error_response("仅允许本机页面操作", "invalid_origin", 403)
        return await call_next(request)

    @app.exception_handler(SongSeekError)
    async def songseek_error(request, error):
        return error_response(str(error), error.code, error.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # 验证错误中的 input 可能含 Cookie，永远不将其序列化返回。
        return error_response("请求字段或类型不正确", "invalid_request", 422)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return error_response("接口不存在" if error.status_code == 404 else "请求无法处理", "not_found" if error.status_code == 404 else "http_error", error.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        logger.error("请求处理失败：%s", type(error).__name__)
        return error_response(public_error(error), "internal_error", 500)

    def current() -> Service:
        return app.state.service

    @app.post("/api/import/parse")
    async def parse_import(request: Request):
        if "multipart/form-data" in request.headers.get("content-type", ""):
            async with request.form(max_files=1, max_fields=2, max_part_size=20 * 1024 * 1024) as form:
                upload = form.get("file")
                if upload is None or not hasattr(upload, "read"):
                    raise SongSeekError("请上传清单文件", "invalid_import")
                data = await upload.read(20 * 1024 * 1024 + 1)
                if len(data) > 20 * 1024 * 1024:
                    raise SongSeekError("清单文件不能超过 20MB", "file_too_large", 413)
                try:
                    mapping = json.loads(form["mapping"]) if form.get("mapping") else None
                except (ValueError, TypeError):
                    raise SongSeekError("列映射必须是 JSON 对象", "invalid_mapping") from None
                if mapping is not None and not isinstance(mapping, dict):
                    raise SongSeekError("列映射必须是 JSON 对象", "invalid_mapping")
                return await asyncio.to_thread(current().parse, data=data, filename=upload.filename or "", mapping=mapping)
        try:
            body = ParseInput.model_validate(await request.json())
        except (ValueError, TypeError):
            raise SongSeekError("导入请求格式不正确", "invalid_import") from None
        return await asyncio.to_thread(current().parse, text=body.text, url=body.url, mapping=body.mapping)

    @app.post("/api/playlists")
    def create_playlist(body: CreatePlaylist):
        return current().create_playlist(body.name, body.source, body.rows)

    @app.get("/api/playlists")
    def playlists():
        return current().playlists()

    @app.get("/api/playlists/{identifier}")
    def playlist(identifier: int):
        return {"playlist": current().playlist(identifier), "tracks": current().tracks(identifier)}

    @app.patch("/api/playlists/{identifier}")
    def rename_playlist(identifier: int, body: RenamePlaylist):
        return current().rename_playlist(identifier, body.name)

    @app.delete("/api/playlists/{identifier}")
    def delete_playlist(identifier: int):
        current().delete_playlist(identifier)
        return {"ok": True}

    @app.get("/api/playlists/{identifier}/export.csv")
    def export_playlist(identifier: int):
        name = current().playlist(identifier).name
        return Response(current().export_csv(identifier), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(name, safe='')}.csv"})

    @app.patch("/api/tracks/{identifier}")
    def update_track(identifier: int, body: UpdateTrack):
        values = body.model_dump(exclude_unset=True)
        if any(v is None and k != "duration" for k, v in values.items()):
            raise SongSeekError("只有时长可以设为空值", "invalid_request")
        return current().update_track(identifier, values)

    @app.delete("/api/tracks/{identifier}")
    def delete_track(identifier: int):
        current().delete_track(identifier)
        return {"ok": True}

    @app.get("/api/tracks/{identifier}/candidates")
    def candidates(identifier: int):
        return current().candidates(identifier)

    @app.post("/api/tracks/{identifier}/choose")
    def choose(identifier: int, body: Choose):
        return current().choose(identifier, body.candidate_id)

    @app.post("/api/tracks/{identifier}/search")
    def search_track(identifier: int, body: SearchTrack):
        return current().search_track(identifier, body.query, body.providers)

    @app.post("/api/tracks/batch")
    def batch(body: Batch):
        return {"updated": current().batch(body.ids, body.action)}

    def downloaded(identifier: int) -> Path:
        track = current().track(identifier)
        if not track.file or not Path(track.file.path).is_file():
            raise SongSeekError("已下载的文件不存在", "file_not_found", 404)
        return Path(track.file.path)

    @app.get("/api/tracks/{identifier}/audio")
    def track_audio(identifier: int):
        return FileResponse(downloaded(identifier))

    @app.post("/api/tracks/{identifier}/reveal")
    def reveal(identifier: int):
        path = downloaded(identifier)
        if sys.platform == "darwin":
            command = ["open", "-R", str(path)]
        elif sys.platform == "win32":
            command = ["explorer", "/select,", str(path)]
        else:
            command = ["xdg-open", str(path.parent)]
        subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return {"ok": True}

    @app.post("/api/search")
    def search_one(body: SearchOne):
        if not body.query.strip():
            raise SongSeekError("请输入搜索词", "invalid_query")
        return current().search_one(body.query, body.duration)

    @app.post("/api/playlists/{identifier}/search")
    def search_playlist(identifier: int, body: TrackIds = TrackIds()):
        return current().jobs.start("search", identifier, body.track_ids)

    @app.post("/api/playlists/{identifier}/download")
    def download_playlist(identifier: int, body: TrackIds = TrackIds()):
        return current().jobs.start("download", identifier, body.track_ids)

    @app.get("/api/jobs")
    def jobs():
        return current().jobs.list()

    @app.post("/api/jobs/{identifier}/cancel")
    def cancel_job(identifier: int):
        return current().jobs.cancel(identifier)

    @app.get("/api/candidates/{identifier}/preview")
    def preview(identifier: int, request: Request):
        raw = current().raw_candidate(identifier)
        provider = current().providers[raw.provider]
        try:
            source = provider.resolve(raw)
            if source.local_path:
                return FileResponse(source.local_path)
            if raw.provider in ("bilibili", "youtube"):
                return FileResponse(current().previews.get(raw, source, provider))
            provider.semaphore.acquire()
            client = provider.client(timeout=30)
            headers = provider.headers() | source.headers
            if request.headers.get("range"):
                headers["Range"] = request.headers["range"]
            response = client.send(client.build_request("GET", source.url, headers=headers), stream=True)
            if response.status_code == 416:
                content_range = response.headers.get("content-range", "bytes */0")
                response.close()
                client.close()
                provider.semaphore.release()
                return Response(status_code=416, headers={"Content-Range": content_range})
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity") != "identity":
                raise SongSeekError("音源未提供可直接转发的音频流", "invalid_audio")
            if "text/" in response.headers.get("content-type", "") or "json" in response.headers.get("content-type", ""):
                raise SongSeekError("试听地址返回了非音频内容", "invalid_audio")
        except Exception as error:
            if "client" in locals():
                client.close()
                provider.semaphore.release()
            raise SongSeekError(public_error(error), "preview_failed", 502) from None

        def chunks():
            try:
                yield from response.iter_raw()
            except Exception as error:
                logger.warning("试听流中断：%s", public_error(error))
            finally:
                response.close()
                client.close()
                provider.semaphore.release()

        exposed = {key: response.headers[key] for key in ("content-length", "content-range", "accept-ranges", "content-type") if key in response.headers}
        return StreamingResponse(chunks(), status_code=response.status_code, headers=exposed, media_type=response.headers.get("content-type", "audio/mpeg"))

    @app.get("/api/events")
    async def events(request: Request):
        broadcaster = current().events
        listener = broadcaster.subscribe()

        async def stream():
            ping = time.monotonic()
            try:
                yield ": connected\n\n"
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.to_thread(listener.get, True, 1)
                        yield event
                    except queue.Empty:
                        pass
                    if time.monotonic() - ping >= 15:
                        yield ": ping\n\n"
                        ping = time.monotonic()
            finally:
                broadcaster.unsubscribe(listener)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/providers")
    def providers():
        return sorted(current().config.providers, key=lambda p: p.order)

    @app.put("/api/providers")
    def update_providers(body: list[ProviderUpdate]):
        return current().config.update_providers([p.model_dump() for p in body])

    @app.post("/api/providers/health")
    def providers_health():
        return current().health()

    @app.get("/api/settings")
    def settings():
        return current().config.settings

    @app.put("/api/settings")
    def update_settings(body: SettingsUpdate):
        values = body.model_dump(exclude_unset=True, exclude={"netease_cookie"})
        if any(value is None for value in values.values()):
            raise SongSeekError("设置字段不能为空值", "invalid_settings")
        if body.netease_cookie is not None:
            values["netease_cookie"] = body.netease_cookie.get_secret_value()
        return current().config.update(values)

    dist = Path(__file__).resolve().parents[2] / "web" / "dist"

    @app.get("/{path:path}")
    def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise SongSeekError("接口不存在", "not_found", 404)
        if not (dist / "index.html").is_file():
            return PlainTextResponse("前端未构建：cd web && npm run build")
        candidate = (dist / path).resolve()
        if candidate.is_relative_to(dist.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(dist / "index.html")

    return app
