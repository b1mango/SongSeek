import base64
import json
import subprocess
from dataclasses import asdict

import httpx
import pytest
from fastapi.testclient import TestClient

from songseek import covers
from songseek.downloader import tag
from songseek.matching import evaluate
from songseek.models import DownloadedFile, ParsedRow, RawCandidate
from songseek.server import create_app

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')


@pytest.fixture
def track(service):
    playlist = service.create_playlist('封面', 'text', [ParsedRow(title='晴天', artist='周杰伦', duration=180)])
    return service.tracks(playlist.id)[0]


def add_candidate(service, track, source_id, cover=None, **values):
    raw = RawCandidate(**({'provider': 'mock', 'source_id': source_id, 'title': track.title,
                          'artist': track.artist, 'duration': 180, 'extra': {'cover_url': cover}} | values))
    candidate = evaluate(track, raw, 'mock')
    candidate.id = service.db.insert('candidates', candidate.model_dump(), track_id=track.id,
                                     provider='mock', source_id=source_id, raw=json.dumps(asdict(raw)))
    return candidate


def mock_http(monkeypatch, service, handler):
    def client(timeout):
        assert timeout == 5
        return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    monkeypatch.setattr(service.providers['mock'], 'client', client)


def test_candidate_prefers_own_cover_and_caches(service, track, monkeypatch):
    chosen = add_candidate(service, track, 'chosen', 'https://cover.test/own')
    add_candidate(service, track, 'other', 'https://cover.test/other')
    calls = []
    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=PNG, headers={'Content-Type': 'application/octet-stream'})
    mock_http(monkeypatch, service, handle)
    with TestClient(create_app(service=service)) as client:
        for _ in range(2):
            result = client.get(f'/api/candidates/{chosen.id}/cover')
            assert result.status_code == 200 and result.content == PNG
            assert result.headers['content-type'] == 'image/png'
            assert result.headers['cache-control'] == 'public, max-age=86400'
    assert calls == ['/own']


@pytest.mark.parametrize('same_available', [True, False])
def test_candidate_fallback_order(service, track, monkeypatch, same_available):
    chosen = add_candidate(service, track, 'chosen', 'https://cover.test/own')
    other = add_candidate(service, track, 'other', 'https://cover.test/other', duration=None)
    candidate = service.candidate(other.id)
    candidate.title = '晴天 (Live)'
    candidate.flags = ['live']
    candidate.verdict = 'review'
    service.db.save('candidates', candidate.model_dump())
    add_candidate(service, track, 'same', 'https://cover.test/same')
    rejected = add_candidate(service, track, 'reject', 'https://cover.test/reject')
    rejected.verdict = 'reject'
    service.db.save('candidates', rejected.model_dump())
    calls = []
    def handle(request):
        calls.append(request.url.path)
        if request.url.path == '/own' or (request.url.path == '/same' and not same_available):
            return httpx.Response(404)
        return httpx.Response(200, content=PNG)
    mock_http(monkeypatch, service, handle)
    with TestClient(create_app(service=service)) as client:
        assert client.get(f'/api/candidates/{chosen.id}/cover').content == PNG
    assert calls == (['/own', '/same'] if same_available else ['/own', '/same', '/other'])


def test_no_cover_skips_rejected_and_other_tracks(service, track, monkeypatch):
    chosen = add_candidate(service, track, 'chosen')
    rejected = add_candidate(service, track, 'reject', 'https://cover.test/reject')
    rejected.verdict = 'reject'
    service.db.save('candidates', rejected.model_dump())
    playlist = service.create_playlist('别的清单', 'text', [ParsedRow(title='晴天', artist='周杰伦')])
    add_candidate(service, service.tracks(playlist.id)[0], 'other-track', 'https://cover.test/other')
    mock_http(monkeypatch, service, lambda request: pytest.fail('不应请求被拒绝或其他曲目的封面'))
    with TestClient(create_app(service=service)) as client:
        for path in [f'/api/candidates/{chosen.id}/cover', f'/api/tracks/{track.id}/cover']:
            result = client.get(path)
            assert result.status_code == 404
            assert result.json() == {'error': {'code': 'cover_not_found', 'message': '没有找到可用封面'}}
        for path in ['/api/candidates/9999/cover', '/api/tracks/9999/cover']:
            result = client.get(path)
            assert result.status_code == 404 and result.json()['error']['code'] == 'not_found'


@pytest.mark.parametrize('kind', ['html', 'oversized', 'declared_size', 'timeout', 'bad_url'])
def test_failed_remote_cover_returns_404(service, track, monkeypatch, kind):
    url = 'file:///tmp/cover.png' if kind == 'bad_url' else 'https://cover.test/image'
    candidate = add_candidate(service, track, 'chosen', url)
    def handle(request):
        if kind == 'timeout':
            raise httpx.ReadTimeout('timeout', request=request)
        if kind == 'declared_size':
            return httpx.Response(200, content=PNG, headers={'content-length': str(covers.MAX_SIZE + 1)})
        if kind == 'oversized':
            return httpx.Response(200, content=PNG + b'x' * covers.MAX_SIZE, headers={'content-length': '1'})
        return httpx.Response(200, content=b'<html>error</html>', headers={'content-type': 'image/png'})
    mock_http(monkeypatch, service, handle)
    with TestClient(create_app(service=service)) as client:
        result = client.get(f'/api/candidates/{candidate.id}/cover')
        assert result.status_code == 404 and result.json()['error']['code'] == 'cover_not_found'


@pytest.mark.parametrize('data,mime', [(b'\xff\xd8\xff\xe0image', 'image/jpeg'),
                                      (PNG, 'image/png'),
                                      (b'RIFF\x10\x00\x00\x00WEBPVP8 image', 'image/webp')])
def test_image_content_types(service, track, monkeypatch, data, mime):
    candidate = add_candidate(service, track, 'chosen', 'https://cover.test/image')
    mock_http(monkeypatch, service, lambda request: httpx.Response(200, content=data))
    with TestClient(create_app(service=service)) as client:
        result = client.get(f'/api/candidates/{candidate.id}/cover')
        assert result.content == data and result.headers['content-type'] == mime


def test_cover_lru_evicts_least_recently_used(service, track, monkeypatch):
    candidates = [add_candidate(service, track, str(i), f'https://cover.test/{i}') for i in range(65)]
    calls = []
    def handle(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=PNG)
    mock_http(monkeypatch, service, handle)
    for candidate in candidates[:64]:
        service.covers.candidate(candidate.id)
    service.covers.candidate(candidates[0].id)
    service.covers.candidate(candidates[64].id)
    service.covers.candidate(candidates[0].id)
    service.covers.candidate(candidates[1].id)
    assert len(service.covers.cache) == 64
    assert calls == [f'/{i}' for i in range(65)] + ['/1']


@pytest.mark.parametrize('extension', ['mp3', 'm4a', 'flac'])
def test_track_reads_real_embedded_cover(service, track, monkeypatch, tmp_path, extension):
    path = tmp_path / f'audio.{extension}'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440',
                    '-t', '0.1', str(path)], check=True, capture_output=True)
    raw = RawCandidate('mock', 'chosen', track.title, track.artist, duration=180)
    tag(path, track, raw, PNG)
    track.file = DownloadedFile(path=str(path), size=path.stat().st_size, format=extension,
                                bitrate=None, duration=.1, sha256='test')
    chosen = add_candidate(service, track, 'chosen', 'https://cover.test/remote')
    track.chosen_candidate_id = chosen.id
    service.save_track(track)
    mock_http(monkeypatch, service, lambda request: pytest.fail('内嵌封面优先，不应联网'))
    with TestClient(create_app(service=service)) as client:
        result = client.get(f'/api/tracks/{track.id}/cover')
        assert result.status_code == 200 and result.content == PNG
        assert result.headers['content-type'] == 'image/png'
        assert result.headers['cache-control'] == 'no-cache'


@pytest.mark.parametrize('file_state', ['none', 'missing', 'broken', 'no_cover'])
def test_track_falls_back_to_chosen_candidate(service, track, monkeypatch, tmp_path, file_state):
    chosen = add_candidate(service, track, 'chosen')
    add_candidate(service, track, 'fallback', 'https://cover.test/image')
    track.chosen_candidate_id = chosen.id
    if file_state != 'none':
        path = tmp_path / 'audio.mp3'
        if file_state == 'broken':
            path.write_bytes(b'broken')
        elif file_state == 'no_cover':
            subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'sine=frequency=440',
                            '-t', '0.1', str(path)], check=True, capture_output=True)
        track.file = DownloadedFile(path=str(path), size=0, format='mp3', bitrate=None,
                                    duration=.1, sha256='test')
    service.save_track(track)
    mock_http(monkeypatch, service, lambda request: httpx.Response(200, content=PNG))
    with TestClient(create_app(service=service)) as client:
        assert client.get(f'/api/tracks/{track.id}/cover').content == PNG
