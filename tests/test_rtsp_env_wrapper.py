import subprocess

from app.workers import run_rtsp_worker_from_env


def test_main_returns_2_without_camera_id(monkeypatch) -> None:
    monkeypatch.delenv("CAMERA_ID", raising=False)
    code = run_rtsp_worker_from_env.main()
    assert code == 2


def test_main_builds_command_with_expected_flags(monkeypatch) -> None:
    monkeypatch.setenv("CAMERA_ID", "cam-1")
    monkeypatch.setenv("ANIMAL_ID", "animal-1")
    monkeypatch.setenv("WORKER_DEVICE", "cpu")
    monkeypatch.setenv("RECORD_SEGMENTS", "1")
    monkeypatch.setenv("STREAM_URL", "rtsp://example/stream")
    monkeypatch.setenv("MAX_FRAMES", "100")
    monkeypatch.setenv("MAX_SECONDS", "30")

    captured = {}

    def fake_run(cmd, *args, **kwargs):
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(args=cmd, returncode=0)

    monkeypatch.setattr(run_rtsp_worker_from_env.subprocess, "run", fake_run)

    code = run_rtsp_worker_from_env.main()
    assert code == 0

    cmd = captured["cmd"]
    assert "--camera-id" in cmd and "cam-1" in cmd
    assert "--device" in cmd and "cpu" in cmd
    assert "--record-segments" in cmd
    assert "--stream-url" in cmd and "rtsp://example/stream" in cmd
    assert "--max-frames" in cmd and "100" in cmd
    assert "--max-seconds" in cmd and "30" in cmd
