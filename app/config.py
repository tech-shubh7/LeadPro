from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    database_url: str = "postgresql+psycopg://postgres@localhost:5432/leadpro"  # password goes in .env

    serper_api_key: str = ""
    openai_api_key: str = ""
    companies_house_api_key: str = ""
    generect_api_key: str = ""

    llm_mode: str = "openai"  # "openai" or "manual" (hybrid: LLM steps answered via work/llm_tasks files)
    llm_max_page_chars: int = 15000  # page text shown to the LLM per source (verification uses full text)
    llm_extract_model: str = "gpt-6-luna"
    llm_judge_model: str = "gpt-6.1-terra"

    freshness_days: int = 90
    max_freshness_days: int = 365
    qualify_score: int = 70
    icp_only_min_fit: int = 32  # of 40: ICP-only leads (no signal) qualify on firmographic fit alone
    max_decision_makers: int = 2
    page_cache_ttl_hours: int = 24
    max_cost_per_job_inr: float = 50.0
    usd_to_inr: float = 88.0
    overfetch_factor: float = 4.0
    discovery_rounds: int = 3  # plan -> search -> triage rounds
    discovery_queries_per_round: int = 8
    triage_min_fit: int = 5  # 0-10 ICP fit from snippet needed to become a candidate
    company_news_queries: int = 1  # per-candidate targeted news search (multi-source evidence)
    research_concurrency: int = 5
    revalidate_after_days: int = 7
    email_reuse_max_days: int = 60  # a stored verified email is reused (not re-paid) only if verified this recently

    # Prices (USD), verified 2026-10-29/30. Serper: $50 / 50k credits; >10 results = 2 credits.
    serper_usd_per_credit: float = 0.001
    # Generect API: finder billed only when a valid email is found; validation billed per email.
    generect_usd_per_found_email: float = 0.02
    generect_usd_per_validation: float = 0.005
    # OpenAI per 1M tokens: (input, cached input, output)
    model_prices_usd_per_mtok: dict[str, tuple[float, float, float]] = {
        "gpt-6-luna": (0.10, 0.01, 0.50),
        "gpt-6.1-terra": (1.00, 0.05, 5.00),
    }

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
