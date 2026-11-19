from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "dev"
    database_url: str = "postgresql+psycopg://postgres@localhost:5432/leadpro"  # password goes in .env

    serper_api_key: str = ""
    openai_api_key: str = ""
    gemini_api_key: str = ""
    companies_house_api_key: str = ""
    generect_api_key: str = ""
    hunter_api_key: str = ""
    tinyfish_api_key: str = ""
    reed_api_key: str = ""  # Reed.co.uk Jobseeker API: UK job ads from direct employers
    # LinkedIn's logged-out job search (no key). On by default at the operator's choice (2026-11-17); its
    # terms restrict automated access, so it can be turned off here. Pages per search and ads read in full
    # per run are capped to stay a light, polite user.
    linkedin_jobs_enabled: bool = True
    linkedin_pages_per_search: int = 3  # 10 ads a page: 5 keywords x 3 pages = up to 150 ads
    linkedin_max_descriptions: int = 30
    # Pacing measured live 2026-11-18: 429s come in short bursts and a 15 s wait cleared each one. The
    # backoff doubles per retry (15, 30, 60 s) and the run's total backoff is capped, then LinkedIn closes.
    linkedin_pause_s: float = 4.0
    linkedin_backoff_s: float = 15.0
    linkedin_backoff_budget_s: float = 300.0
    # Search providers in order; the next is used only when one errors. Serper (Google) first for result
    # quality and dates; TinyFish search as the free fallback. Providers without a key are skipped.
    search_provider_order: list[str] = ["serper", "tinyfish"]
    tinyfish_usd_per_agent_step: float = 0.016
    # Model answers are reused for identical questions for this many days (0 disables): a continued run
    # replays what it already researched for free. Evals always bypass it.
    llm_cache_days: int = 7  # TinyAgent list price per step (tinyfish.ai/pricing)

    # "openai" | "gemini" | "manual" (hybrid: LLM steps answered via work/llm_tasks files)
    llm_mode: str = "openai"
    llm_max_page_chars: int = 15000  # page text shown to the LLM per source (verification uses full text)
    # Pages that cannot hold a qualifying signal (undated or dated before the window) only add background
    # facts or evidence against buying, so the model reads this much of each (extraction_service.build_sources).
    llm_supporting_page_chars: int = 3000
    # Proof runs only: also read every company's pages the old, full way and compare (research_check_service).
    # Roughly doubles the model cost of researching each company, so it is off for normal runs.
    research_check: bool = False
    llm_extract_model: str = "gpt-6-luna"
    llm_judge_model: str = "gpt-6.1-terra"
    # Used when llm_mode is "gemini". Same split: a cheap model for high-volume extraction and triage,
    # a strong one for judging.
    # Both default to the same GA model. Preview model names are withdrawn without notice (2.5-flash
    # and 2.5-pro already were), and a 404 mid-run costs a whole job, so GA beats a cheaper preview.
    gemini_extract_model: str = "gemini-3.8-flash"
    gemini_judge_model: str = "gemini-3.8-flash"
    # Tried in order when the model above stays overloaded (503) or rate-limited (429) after its retries:
    # "high demand" on a shared endpoint is routine, and a run should not be lost to it. Unpriced models
    # are charged at the pessimistic fallback rate below, so a fallback can only overstate cost.
    gemini_fallback_models: list[str] = ["gemini-3.6-flash", "gemini-3.5-flash-lite"]

    # Operator console. A token turns on sign-in (set it whenever the console is not on localhost);
    # empty means open, which is only safe bound to 127.0.0.1.
    web_auth_token: str = ""
    web_output_dir: str = "output"
    web_host: str = "127.0.0.1"
    web_port: int = 8000
    log_level: str = "INFO"
    # Run queue: a worker claims queued runs with a lease and heartbeats while working; a lease that
    # expires (crash, kill -9) hands the run to the next worker. The web server runs one embedded worker
    # unless EMBEDDED_WORKER=false (then run `python -m worker.run_worker` separately).
    embedded_worker: bool = True
    worker_concurrency: int = 2
    run_lease_seconds: int = 120
    run_max_attempts: int = 3

    freshness_days: int = 90
    max_freshness_days: int = 365
    qualify_score: int = 70
    # Watchlist: a lead with a fresh verified signal scoring at least this (but under qualify_score), from a
    # company fitting at least watchlist_min_fit of 40. Calibrated on jobs 26-28 (2026-11-18): keeps the
    # 55-65 agencies with real hiring signs, leaves out WPP (fit 23: too large to buy outside capacity).
    watchlist_min_score: int = 55
    watchlist_min_fit: int = 25
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
    research_concurrency: int = 8  # companies researched at once; each fetches its pages in parallel too
    revalidate_after_days: int = 7
    email_reuse_max_days: int = (
        60  # a stored verified email is reused (not re-paid) only if verified this recently
    )

    # Prices (USD), verified 2026-10-29/30. Serper: $50 / 50k credits; >10 results = 2 credits.
    serper_usd_per_credit: float = 0.001
    # Generect API: finder billed only when a valid email is found; validation billed per email.
    generect_usd_per_found_email: float = 0.02
    generect_usd_per_validation: float = 0.005
    # Hunter bills credits from a plan, not per call. These are what one found email / one verification
    # costs on that plan; set them to yours. Left pessimistic so the spend cap can only stop runs early.
    hunter_usd_per_found_email: float = 0.05
    hunter_usd_per_verification: float = 0.025
    # Email waterfall, in order: the first VALID (deliverable, not catch-all) address wins. Finders without
    # an API key are skipped.
    email_finder_order: list[str] = ["generect", "hunter"]
    # OpenAI per 1M tokens: (input, cached input, output)
    model_prices_usd_per_mtok: dict[str, tuple[float, float, float]] = {
        "gpt-6-luna": (0.10, 0.01, 0.50),
        "gpt-6.1-terra": (1.00, 0.05, 5.00),
        # Gemini list prices (paid tier). An AI Studio key on the free tier is billed at zero but is
        # rate-limited, so these are an upper bound and the cost cap still applies. Thinking tokens
        # are billed as output.
        "gemini-2.5-flash": (0.30, 0.03, 2.50),
        "gemini-2.5-pro": (1.25, 0.125, 10.00),
        "gemini-2.5-flash-lite": (0.10, 0.01, 0.40),
        # Paid-tier list prices from ai.google.dev pricing, checked 2026-11-14. The 3.6/3.8 Flash rates are
        # promotional until 2026-12-31 and double on 2027-01-01 ($1.50 / $0.15 / $7.50): update then.
        "gemini-3.8-flash": (0.75, 0.075, 3.75),
        "gemini-3.6-flash": (0.75, 0.075, 3.75),
        "gemini-3.5-flash-lite": (0.30, 0.03, 2.50),
        "gemini-3.1-flash-lite": (0.25, 0.025, 1.50),
        "gemini-3.5-flash": (1.50, 0.15, 9.00),  # legacy; secondary source, not on the official page
        "gemini-3-flash-preview": (0.30, 0.03, 2.50),
        "gemini-3.1-flash-lite-preview": (0.10, 0.01, 0.40),
        "gemini-3.1-pro-preview": (1.25, 0.125, 10.00),
    }
    # Charged for a model with no entry above. Deliberately pessimistic: an unpriced model must never
    # read as free, or the per-job cost cap would stop protecting anything.
    fallback_price_usd_per_mtok: tuple[float, float, float] = (1.25, 0.125, 10.00)

    def price_for(self, model: str) -> tuple[float, float, float]:
        return self.model_prices_usd_per_mtok.get(model, self.fallback_price_usd_per_mtok)

    @property
    def is_dev(self) -> bool:
        return self.app_env == "dev"


@lru_cache
def get_settings() -> Settings:
    return Settings()
