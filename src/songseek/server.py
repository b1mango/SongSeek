import asyncio
import json
import queue
import subprocess
import sys
import time
import webbrowser
from importlib.metadata import version
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import anyio

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response, StreamingResponse
from pydantic import Field, SecretStr
from starlette.background import BackgroundTask
from starlette.concurrency import iterate_in_threadpool
from starlette.exceptions import HTTPException

from . import netproxy
from .tools import availability, resource_dir
from .config import Config
from .errors import SongSeekError, logger, public_error
from .matching import same_recording
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
    lyrics_wanted: bool | None = None


class Choose(Model):
    candidate_id: int = Field(gt=0)


class SearchOne(Model):
    providers: list[str] | None = None
    query: str = Field(min_length=1, max_length=1000)
    duration: float | None = Field(default=None, gt=0)


class SearchTrack(Model):
    query: str | None = Field(default=None, min_length=1, max_length=1000)
    providers: list[str] | None = None


class TrackIds(Model):
    track_ids: list[int] | None = None


class DownloadRequest(TrackIds):
    lyrics: bool | None = None


class ArtistDownload(Model):
    lyrics: bool | None = None
    providers: list[str] | None = None


class LyricsRequest(Model):
    force: bool = False


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
    proxy_mode: Literal["auto", "manual", "off"] | None = None
    proxy: str | None = None
    search_concurrency: int | None = Field(default=None, ge=1, le=32)
    download_concurrency: int | None = Field(default=None, ge=1, le=16)
    auto_approve: bool | None = None
    lyrics: bool | None = None
    lyrics_translation: bool | None = None
    lyrics_embed: bool | None = None
    theme: Literal["system", "light", "dark"] | None = None
    netease_cookie_set: bool | None = None
    netease_cookie: SecretStr | None = None


class ProxyTest(Model):
    mode: Literal["auto", "manual", "off"] | None = None
    proxy: str | None = None


class PickFolder(Model):
    initial: str | None = None


class OpenURL(Model):
    url: str


class ParseInput(Model):
    text: str | None = Field(default=None, max_length=20 * 1024 * 1024)
    url: str | None = Field(default=None, max_length=2000)
    mapping: dict | None = None


def error_response(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def create_app(config: Config | None = None, service: Service | None = None, *,
               port: int | None = None, desktop=None) -> FastAPI:
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
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"} if port is not None else {"testserver", "127.0.0.1", "localhost"}
        if request.headers.get("host", "").lower() not in allowed:
            return error_response("仅允许本机地址访问", "invalid_host", 400)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and (urlsplit(origin).netloc.lower() not in allowed or urlsplit(origin).scheme != "http"):
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
        if any(v is None and k not in ("duration", "lyrics_wanted") for k, v in values.items()):
            raise SongSeekError("只有时长和歌词选择可以设为空值", "invalid_request")
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
        return current().search_one(body.query, body.duration, body.providers)

    @app.post("/api/search/stream")
    async def search_one_stream(body: SearchOne):
        if not body.query.strip():
            raise SongSeekError("请输入搜索词", "invalid_query")
        service = current()

        async def stream():
            events = asyncio.Queue()
            loop = asyncio.get_running_loop()

            def emit(event):
                loop.call_soon_threadsafe(events.put_nowait, event)

            def search():
                try:
                    service.search_one(body.query, body.duration, body.providers, on_event=emit)
                except Exception as error:
                    emit({"type": "error", "message": public_error(error)})

            async def work():
                # 断连仍让线程完成搜索；任务组等待它退出后才释放请求生命周期。
                await anyio.to_thread.run_sync(search)

            async with anyio.create_task_group() as tasks:
                tasks.start_soon(work)
                while True:
                    event = await events.get()
                    yield json.dumps(event, ensure_ascii=False) + "\n"
                    if event["type"] in ("done", "artist_done", "error"):
                        break

        return StreamingResponse(stream(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.post("/api/playlists/{identifier}/search")
    def search_playlist(identifier: int, body: TrackIds = TrackIds()):
        return current().jobs.start("search", identifier, body.track_ids)

    @app.post("/api/playlists/{identifier}/download")
    def download_playlist(identifier: int, body: DownloadRequest = DownloadRequest()):
        return current().jobs.start("download", identifier, body.track_ids, lyrics=body.lyrics,
                                    set_lyrics="lyrics" in body.model_fields_set)

    @app.get("/api/candidates/{identifier}/cover")
    def candidate_cover(identifier: int):
        data, content_type = current().covers.candidate(identifier)
        return Response(data, media_type=content_type, headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/tracks/{identifier}/cover")
    def track_cover(identifier: int):
        data, content_type = current().covers.track(identifier)
        # A track's cover changes when another candidate is chosen or the file is re-downloaded.
        return Response(data, media_type=content_type, headers={"Cache-Control": "no-cache"})

    @app.get("/api/candidates/{identifier}/lyrics")
    def candidate_lyrics(identifier: int):
        return current().lyrics.preview(identifier)

    @app.get("/api/tracks/{identifier}/lyrics")
    def track_lyrics(identifier: int):
        return current().lyrics.read(current().track(identifier))

    @app.post("/api/tracks/{identifier}/lyrics")
    def save_lyrics(identifier: int, body: LyricsRequest = LyricsRequest()):
        return current().lyrics_track(identifier, force=body.force)

    @app.post("/api/playlists/{identifier}/lyrics")
    def playlist_lyrics(identifier: int, body: LyricsRequest = LyricsRequest()):
        return current().jobs.start("lyrics", identifier, force=body.force)

    @app.get("/api/jobs")
    def jobs():
        return current().jobs.list()

    @app.post("/api/jobs/{identifier}/cancel")
    def cancel_job(identifier: int):
        return current().jobs.cancel(identifier)

    def open_preview(raw, label: str, request: Request, fallback: bool):
        service = current()
        provider = service.providers[raw.provider]
        headers = {"X-Preview-Source": quote(label, safe="")}
        if fallback:
            headers["X-Preview-Fallback"] = "1"
        source = provider.resolve(raw)
        if source.local_path or raw.provider in ("bilibili", "youtube"):
            path = Path(source.local_path) if source.local_path else service.previews.get(raw, source, provider)
            with path.open("rb"):
                stat = path.stat()
            return FileResponse(path, stat_result=stat, headers=headers)

        resources = ExitStack()
        try:
            resources.enter_context(provider.slot())
            client = resources.enter_context(provider.client(timeout=provider.remaining(30)))
            upstream_headers = provider.headers() | source.headers
            if request.headers.get("range"):
                upstream_headers["Range"] = request.headers["range"]
            response = client.send(client.build_request("GET", source.url, headers=upstream_headers), stream=True)
            resources.callback(response.close)
            if response.status_code == 416:
                headers["Content-Range"] = response.headers.get("content-range", "bytes */0")
                resources.close()
                return Response(status_code=416, headers=headers)
            response.raise_for_status()
            if response.headers.get("content-encoding", "identity") != "identity":
                raise SongSeekError("音源未提供可直接转发的音频流", "invalid_audio")
            if "text/" in response.headers.get("content-type", "") or "json" in response.headers.get("content-type", ""):
                raise SongSeekError("试听地址返回了非音频内容", "invalid_audio")
        except Exception:
            resources.close()
            raise

        async def chunks():
            try:
                async for chunk in iterate_in_threadpool(response.iter_raw()):
                    yield chunk
            except Exception as error:
                logger.warning("试听流中断：%s", public_error(error))
            finally:
                resources.close()

        headers.update({key: response.headers[key] for key in ("content-length", "content-range", "accept-ranges", "content-type") if key in response.headers})
        return StreamingResponse(chunks(), status_code=response.status_code, headers=headers,
                                 media_type=response.headers.get("content-type", "audio/mpeg"),
                                 background=BackgroundTask(resources.close))

    @app.get("/api/candidates/{identifier}/preview")
    def preview(identifier: int, request: Request):
        service = current()
        chosen = service.candidate(identifier)
        track = service.track(chosen.track_id)
        orders = {p.id: p.order for p in service.config.providers}
        alternatives = sorted((c for c in service.candidates(track.id)
                               if c.id != chosen.id and not c.needs_member and same_recording(track, chosen, c)),
                              key=lambda c: (c.provider == chosen.provider, orders.get(c.provider, 999), c.rank))
        errors = []
        for candidate in [chosen] + alternatives[:3]:
            try:
                return open_preview(service.raw_candidate(candidate.id), candidate.provider_label, request, candidate.id != chosen.id)
            except Exception as error:
                errors.append(f"{candidate.provider_label}：{public_error(error)}")
        suffix = "已尝试的同版本候选均无法试听" if alternatives else "其他音源没有同一版本可试听"
        raise SongSeekError("；".join(dict.fromkeys(errors + [suffix])), "preview_failed", 502)

    @app.get("/api/artist-songs/{token}/preview")
    def artist_preview(token: str, request: Request):
        return current().artist_songs.preview(token, lambda raw, label, fallback: open_preview(raw, label, request, fallback))

    @app.get("/api/artist-songs/{token}/cover")
    def artist_cover(token: str):
        data, content_type = current().covers.raws(current().artist_songs.get(token))
        return Response(data, media_type=content_type, headers={"Cache-Control": "public, max-age=7200"})

    @app.get("/api/artist-songs/{token}/lyrics")
    def artist_lyrics(token: str):
        return current().lyrics.artist_song(token)

    @app.post("/api/artist-songs/{token}/download")
    def artist_download(token: str, body: ArtistDownload = ArtistDownload()):
        return current().download_artist_song(token, body.lyrics, body.providers)

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
        updated = current().config.update(values)
        if "theme" in values and desktop is not None:
            desktop.apply_theme(updated.theme)
        return updated

    @app.get("/api/health")
    def health():
        return {"ok": True}

    @app.get("/api/proxy")
    def proxy_status():
        return netproxy.status(current().config.settings)

    @app.post("/api/proxy/test")
    def proxy_test(body: ProxyTest = ProxyTest()):
        settings = current().config.settings.model_copy(deep=True)
        if body.mode is not None:
            settings.proxy_mode = body.mode
        if body.proxy is not None:
            settings.proxy = body.proxy
        return netproxy.test_connection(settings)

    @app.get("/api/system")
    def system():
        return {"desktop": desktop is not None, "platform": sys.platform, "version": version("songseek"),
                "data_dir": str(current().config.home), "tools": availability()}

    @app.post("/api/system/pick-folder")
    def pick_folder(body: PickFolder = PickFolder()):
        if desktop is None or desktop.window is None:
            raise SongSeekError("请在桌面应用中选择文件夹", "not_desktop", 409)
        return {"path": desktop.pick_folder(body.initial)}

    @app.post("/api/system/titlebar-double-click")
    def titlebar_double_click():
        if desktop is not None and desktop.window is not None:
            desktop.titlebar_double_click()
        return {"ok": True}

    @app.post("/api/system/open-url")
    def open_url(body: OpenURL):
        parsed = urlsplit(body.url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise SongSeekError("仅支持 http/https 网页地址", "invalid_url")
        webbrowser.open(body.url)
        return {"ok": True}

    dist = resource_dir() / "web" / "dist"

    @app.get("/{path:path}")
    def frontend(path: str):
        if path == "api" or path.startswith("api/"):
            raise SongSeekError("接口不存在", "not_found", 404)
        if not (dist / "index.html").is_file():
            return PlainTextResponse("前端未构建：cd web && npm run build")
        candidate = (dist / path).resolve()
        if candidate.is_relative_to(dist.resolve()) and candidate.is_file():
            return FileResponse(candidate)
        # The saved theme goes straight into the page, so it never paints in the wrong colours first.
        page = (dist / "index.html").read_text("utf-8")
        page = page.replace("<html ", f'<html data-theme-pref="{current().config.settings.theme}" ', 1)
        return HTMLResponse(page, headers={"Cache-Control": "no-cache"})

    return app
