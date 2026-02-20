from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    api_prefix: str = "/api/v1"
    api_key: str = "change-me"
    encryption_key: str = ""
    database_url: str = "sqlite:///./pawluxe.db"
    upload_dir: Path = Path("storage/uploads")
    encrypted_dir: Path = Path("storage/encrypted")
    export_dir: Path = Path("storage/exports")
    ffmpeg_bin: str = "ffmpeg"
    stream_signing_key: str = "change-me-stream-signing-key"
    stream_token_ttl_seconds: int = 120
    stream_base_url: str = "https://stream.example.com/live"
    owner_play_live_enabled: bool = False
    cors_allow_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    # Tracking runtime settings.
    tracking_device: str = "cuda:0"
    yolo_model: str = "yolo11n.pt"
    engine_root: Path = Path("/mnt/d/99.C-lab/git/Engine")
    deep_sort_model: str = "mobilenetv2_x1_0_msmt17"
    deep_sort_max_dist: float = 0.6
    deep_sort_max_iou_distance: float = 0.75
    deep_sort_max_age: int = 20
    deep_sort_n_init: int = 5
    deep_sort_nn_budget: int = 10000

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")


settings = Settings()
