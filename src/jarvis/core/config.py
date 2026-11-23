from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    env: str = "dev"
    log_level: str = "INFO"

    llm_api_key: str = ""
    llm_base_url: str = "https://api.aitunnel.ru/v1"
    llm_model: str = "deepseek-v4-flash-0731"
    llm_fallback_model: str = "gemini-2.5-flash-lite"
    daily_budget_usd: float = 0.10
    monthly_budget_usd: float = 3.00
    llm_timeout_s: float = 60.0

    db_path: str = "data/jarvis.db"
    cache_dir: str = "data/cache"
    # Optional single public origin for a Cloudflare Tunnel + Access front door.
    # Empty means the app accepts loopback hosts only, as before.
    public_origin: str = ""

    tbank_mcp_url: str = ""
    sber_mcp_url: str = ""
    moex_mcp_url: str = ""

    moex_mcp_command: str = ""
    tbank_mcp_command: str = ""
    sber_mcp_command: str = ""

    llm_mode: str = "offline"
    aitunnel_model: str = "qwen3.7-flash"
    aitunnel_catalog_path: str = ""
    aitunnel_key_budget_confirmed: bool = False
    daily_budget_rub: float = 2.0
    monthly_budget_rub: float = 50.0
    request_ceiling_rub: float = 0.10

    model_config = SettingsConfigDict(env_file=".env", env_prefix="JARVIS_", extra="ignore")


settings = Settings()