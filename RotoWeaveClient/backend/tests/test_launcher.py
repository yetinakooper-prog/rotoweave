from __future__ import annotations

import io
import time

from backend import launcher


class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, status: int = 200) -> None:
        super().__init__(payload)
        self.status = status

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def test_running_version_reads_health_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda *args, **kwargs: FakeResponse(b'{"version":"4.0.0"}'),
    )

    assert launcher._running_version() == "4.0.0"
    assert launcher._is_running() is True


def test_running_version_rejects_malformed_health_payload(monkeypatch) -> None:
    monkeypatch.setattr(
        launcher.urllib.request,
        "urlopen",
        lambda *args, **kwargs: FakeResponse(b"not-json"),
    )

    assert launcher._running_version() is None


def test_is_running_rejects_an_older_version(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "_running_version", lambda: "1.1.0")

    assert launcher._is_running() is False


def test_first_instance_ready_opens_with_its_bootstrap(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(launcher, "_is_running", lambda: True)
    monkeypatch.setattr(launcher, "_open_tool", lambda: opened.append("bootstrap"))
    monkeypatch.setattr(launcher, "_open_existing_tool", lambda: opened.append("existing"))

    launcher._open_when_ready()

    assert opened == ["bootstrap"]


def test_browser_open_can_be_disabled_for_packaged_smoke(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setenv("ROTOWEAVE_LAUNCHER_NO_BROWSER", "1")
    monkeypatch.setattr(launcher.webbrowser, "open", lambda url: opened.append(url))

    launcher._open_tool()
    launcher._open_existing_tool()

    assert opened == []


def test_second_instance_ready_reuses_existing_browser_session(monkeypatch) -> None:
    opened: list[str] = []
    monkeypatch.setattr(launcher, "_is_running", lambda: True)
    monkeypatch.setattr(launcher, "_open_tool", lambda: opened.append("bootstrap"))
    monkeypatch.setattr(launcher, "_open_existing_tool", lambda: opened.append("existing"))

    launcher._open_when_ready(existing_instance=True)

    assert opened == ["existing"]


def test_service_host_starts_only_after_health_and_stops_cleanly(monkeypatch) -> None:
    class FakeServer:
        should_exit = False

        def run(self) -> None:
            while not self.should_exit:
                time.sleep(0.01)

    server = FakeServer()
    monkeypatch.setattr(launcher.uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher.uvicorn, "Server", lambda _config: server)
    monkeypatch.setattr(launcher, "_is_running", lambda: True)

    host = launcher.ServiceHost()
    host.start(timeout_seconds=0.2)
    assert host.alive is True
    host.stop()
    assert host.alive is False
    assert server.should_exit is True


def test_service_host_reports_bind_failure(monkeypatch) -> None:
    class FailedServer:
        should_exit = False

        def run(self) -> None:
            raise SystemExit(1)

    monkeypatch.setattr(launcher.uvicorn, "Config", lambda *args, **kwargs: object())
    monkeypatch.setattr(launcher.uvicorn, "Server", lambda _config: FailedServer())
    monkeypatch.setattr(launcher, "_is_running", lambda: False)

    host = launcher.ServiceHost()
    try:
        host.start(timeout_seconds=0.5)
    except RuntimeError as exc:
        assert "端口" in str(exc) or "退出" in str(exc)
    else:
        raise AssertionError("bind failure must be visible to the launcher")
