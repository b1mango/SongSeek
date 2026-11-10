import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from songseek.server import create_app


def finish(client, identifier):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = next(j for j in client.get("/api/jobs").json() if j["id"] == identifier)
        if job["status"] != "running":
            return job
        time.sleep(.02)
    raise AssertionError("任务未完成")


def test_full_mock_workflow(config):
    with TestClient(create_app(config)) as client:
        assert client.get("/api/providers").json()[0]["id"] == "mock"
        parsed = client.post("/api/import/parse", files={"file": ("songs.csv", "歌名,歌手,时长\n晴天,周杰伦,180\n".encode())}).json()
        created = client.post("/api/playlists", json={"name": "冒烟", "source": parsed["source"], "rows": parsed["rows"]})
        assert created.status_code == 200
        identifier = created.json()["id"]
        job = client.post(f"/api/playlists/{identifier}/search", json={}).json()
        assert finish(client, job["id"])["status"] == "done"
        track = client.get(f"/api/playlists/{identifier}").json()["tracks"][0]
        assert track["status"] == "matched"
        candidates = client.get(f"/api/tracks/{track['id']}/candidates").json()
        assert {c["verdict"] for c in candidates} == {"match", "review", "reject"}
        chosen = client.post(f"/api/tracks/{track['id']}/choose", json={"candidate_id": candidates[0]["id"]}).json()
        assert chosen["chosen_by"] == track["chosen_by"]  # 选择当前候选是幂等操作。
        preview = client.get(candidates[0]["preview_url"], headers={"Range": "bytes=0-99"})
        assert preview.status_code == 206 and len(preview.content) == 100
        job = client.post(f"/api/playlists/{identifier}/download", json={}).json()
        assert finish(client, job["id"])["failed"] == 0
        track = client.get(f"/api/playlists/{identifier}").json()["tracks"][0]
        assert track["status"] == "done" and Path(track["file"]["path"]).is_file()
        audio = client.get(f"/api/tracks/{track['id']}/audio", headers={"Range": "bytes=0-9"})
        assert audio.status_code == 206 and len(audio.content) == 10
        exported = client.get(f"/api/playlists/{identifier}/export.csv")
        assert exported.status_code == 200 and "晴天" in exported.text and "done" in exported.text
        assert client.delete(f"/api/playlists/{identifier}").status_code == 200
        assert Path(track["file"]["path"]).is_file()


def test_cookie_never_returned_or_in_validation_error(config, caplog):
    with TestClient(create_app(config)) as client:
        cookie = "MUSIC_U=synthetic-do-not-return; __csrf=synthetic-csrf; NTES_PASSPORT=synthetic-private"
        response = client.put("/api/settings", json={"netease_cookie": cookie})
        assert response.status_code == 200 and response.json()["netease_cookie_set"] is True
        assert cookie not in response.text
        assert cookie not in client.get("/api/settings").text
        assert config.cookie_path.read_text() == "MUSIC_U=synthetic-do-not-return; __csrf=synthetic-csrf"
        assert config.cookie_path.stat().st_mode & 0o777 == 0o600
        failed = client.put("/api/settings", json={"netease_cookie": cookie, "search_concurrency": 0})
        assert failed.status_code == 422 and cookie not in failed.text
        for value in ("synthetic-do-not-return", "synthetic-csrf", "synthetic-private"):
            assert value not in response.text and value not in caplog.text and value not in config.path.read_text()
        assert client.put("/api/settings", json={"netease_cookie": ""}).json()["netease_cookie_set"] is False


def test_cookie_is_not_used_on_netease_cdn_or_redirect(config, monkeypatch):
    import httpx
    from songseek.providers.netease import NeteaseProvider, parse_audio
    from songseek.models import RawCandidate
    config.update({"netease_cookie": "MUSIC_U=synthetic-for-api; NTES_PASSPORT=discard-me"})
    provider = NeteaseProvider(config)
    def handle(request):
        assert request.headers["Cookie"] == "MUSIC_U=synthetic-for-api"
        return httpx.Response(200, json={"code": 200})
    original = httpx.Client
    monkeypatch.setattr(provider, "client", lambda **kwargs: original(transport=httpx.MockTransport(handle)))
    assert provider.call("/api/test", authenticated=True)["code"] == 200
    source = parse_audio({"code": 200, "data": [{"id": 42, "code": 200, "url": "https://m7.music.126.net/a.mp3", "br": 320000}]}, RawCandidate("netease", "42", "晴天"))
    assert "Cookie" not in source.headers


@pytest.mark.parametrize(("cookie", "expected"), [
    ("MUSIC_U=synthetic-value", "MUSIC_U=synthetic-value"),
    ("other=private; __csrf=token; MUSIC_U=abc%2Fdef==; more=private", "MUSIC_U=abc%2Fdef==; __csrf=token"),
    ("Cookie: MUSIC_U=one; __csrf=two", "MUSIC_U=one; __csrf=two"),
    ("cookie: other=private; MUSIC_U=one; MUSIC_U=one", "MUSIC_U=one"),
])
def test_netease_cookie_extracts_only_supported_fields(config, cookie, expected):
    with TestClient(create_app(config)) as client:
        response = client.put("/api/settings", json={"netease_cookie": cookie})
        assert response.status_code == 200 and response.json()["netease_cookie_set"]
        assert config.cookie_path.read_text() == expected
        assert expected not in response.text


@pytest.mark.parametrize(("cookie", "message"), [
    ("other=private; __csrf=token", "没有找到 MUSIC_U"),
    ("MUSIC_U=; other=private", "MUSIC_U 的值为空或格式不正确"),
    ("MUSIC_U=first; MUSIC_U=second", "多个不同的 MUSIC_U"),
    ("MUSIC_U=first; __csrf=one; __csrf=two", "多个不同的 __csrf"),
    ("MUSIC_U=one\nother=secret", "不能包含换行"),
    ("MUSIC_U=not 中文", "MUSIC_U 的值为空或格式不正确"),
])
def test_netease_cookie_invalid_input_keeps_previous_cookie(config, cookie, message, caplog):
    with TestClient(create_app(config)) as client:
        assert client.put("/api/settings", json={"netease_cookie": "MUSIC_U=original"}).status_code == 200
        response = client.put("/api/settings", json={"netease_cookie": cookie})
        assert response.status_code == 400 and message in response.json()["error"]["message"]
        assert config.cookie_path.read_text() == "MUSIC_U=original"
        assert cookie not in response.text and cookie not in caplog.text


def test_not_found_failed_and_review(config):
    with TestClient(create_app(config)) as client:
        assert client.post("/api/search", json={"query": "不存在 周杰伦", "duration": 180}).json()["track"]["status"] == "not_found"
        reviewed = client.post("/api/search", json={"query": "晴天 - 周杰伦"}).json()
        assert reviewed["track"]["status"] == "needs_review"
        track_id = reviewed["track"]["id"]
        assert client.post("/api/tracks/batch", json={"ids": [track_id], "action": "accept_best"}).json()["updated"][0]["status"] == "matched"
        failed = client.post("/api/search", json={"query": "失败 - 周杰伦", "duration": 180}).json()["track"]
        job = client.post(f"/api/playlists/{failed['playlist_id']}/download", json={"track_ids": [failed["id"]]}).json()
        assert finish(client, job["id"])["failed"] == 1
        track = next(t for t in client.get(f"/api/playlists/{failed['playlist_id']}").json()["tracks"] if t["id"] == failed["id"])
        assert track["status"] == "failed" and "取流失败" in track["error"]


def test_errors_mapping_mutation_and_dedup(config):
    with TestClient(create_app(config)) as client:
        assert client.get("/api/tracks/999/candidates").json()["error"]["code"] == "not_found"
        result = client.post("/api/import/parse", files={"file": ("a.csv", b"custom,who\nA,B")}, data={"mapping": '{"title":"custom","artist":"who"}'})
        assert result.status_code == 200 and result.json()["rows"][0]["title"] == "A"
        result = client.post("/api/search", json={"query": "晴天 - 周杰伦", "duration": 180}).json()
        identifier = result["track"]["id"]
        before = [c["id"] for c in result["candidates"]]
        after = client.post(f"/api/tracks/{identifier}/search", json={"query": "晴天 - 周杰伦"}).json()
        assert before == [c["id"] for c in after["candidates"]]
        updated = client.patch(f"/api/tracks/{identifier}", json={"duration": None, "note": "备注"}).json()
        assert updated["status"] == "pending" and updated["candidate_count"] == 0
        assert client.put("/api/settings", json={"filename_template": "{__class__}"}).status_code == 400
        assert client.post("/api/search", json={"query": "晴天"}, headers={"Origin": "https://example.com"}).status_code == 403
        assert client.get("/api/unknown").status_code == 404


def test_preview_proxies_range_and_provider_headers(config, monkeypatch):
    import httpx
    from songseek.models import AudioSource
    with TestClient(create_app(config)) as client:
        result = client.post("/api/search", json={"query": "晴天 - 周杰伦", "duration": 180}).json()
        identifier = result["candidates"][0]["id"]
        provider = client.app.state.service.providers["mock"]
        monkeypatch.setattr(provider, "resolve", lambda raw: AudioSource("https://audio.example/song", {"Referer": "https://required.example/"}))
        def handle(request):
            assert request.headers["Range"] == "bytes=3-6"
            assert request.headers["Referer"] == "https://required.example/"
            return httpx.Response(206, headers={"content-type": "audio/mpeg", "content-range": "bytes 3-6/10", "accept-ranges": "bytes"}, stream=httpx.ByteStream(b"abcd"))
        monkeypatch.setattr(provider, "client", lambda **kwargs: httpx.Client(transport=httpx.MockTransport(handle)))
        response = client.get(f"/api/candidates/{identifier}/preview", headers={"Range": "bytes=3-6"})
        assert response.status_code == 206 and response.content == b"abcd"
        assert response.headers["Content-Range"] == "bytes 3-6/10"
        assert client.get("/api/candidates/999999/preview").status_code == 404
