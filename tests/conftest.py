import socket

import pytest

from songseek.config import Config
from songseek.service import Service


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("离线测试不允许联网")
    monkeypatch.setattr(socket, "create_connection", blocked)
    monkeypatch.setattr(socket.socket, "connect", blocked)


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("SONGSEEK_MOCK", "1")
    monkeypatch.setenv("SONGSEEK_HOME", str(tmp_path / "home"))
    value = Config()
    value.update({"output_dir": str(tmp_path / "out"), "group_by_playlist": False})
    return value


@pytest.fixture
def service(config):
    value = Service(config)
    value.events.interval = 0  # tests assert exact event sequences; coalescing is covered in test_jobs
    yield value
    value.close()
