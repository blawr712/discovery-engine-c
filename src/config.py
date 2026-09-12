import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
DATA_DIR = BASE_DIR / "data"

STRATEGY_PATH = CONFIG_DIR / "strategy.json"
SETTINGS_PATH = CONFIG_DIR / "settings.json"


def load_json(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Missing config file: {path}")

    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


STRATEGY = load_json(STRATEGY_PATH)
SETTINGS = load_json(SETTINGS_PATH)

DIRECTORIES = SETTINGS.get("directories", {})
OUTPUT_DIR = BASE_DIR / DIRECTORIES.get("output_directory", "data/exports")
CACHE_DIR = BASE_DIR / DIRECTORIES.get("cache_directory", "data/cache")
RUN_DIR = BASE_DIR / DIRECTORIES.get("run_directory", "data/runs")
HISTORY_DATABASE = BASE_DIR / DIRECTORIES.get(
    "history_database", "data/history/discovery_history.sqlite3"
)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

CACHE_CONFIG = SETTINGS.get("cache", {})
CACHE_ENABLED = CACHE_CONFIG.get("enabled", True)
CACHE_METADATA_VERSION = CACHE_CONFIG.get("metadata_version", "v1")
CACHE_METADATA_TTL_HOURS = CACHE_CONFIG.get("metadata_ttl_hours", 168)
CACHE_PRICE_HISTORY_TTL_HOURS = CACHE_CONFIG.get(
    "price_history_ttl_hours",
    24,
)
CACHE_SHARE_HISTORY_TTL_HOURS = CACHE_CONFIG.get(
    "share_history_ttl_hours",
    168,
)

RUNTIME_CONFIG = SETTINGS.get("runtime", {})
MAX_CONCURRENT_DOWNLOADS = RUNTIME_CONFIG.get(
    "max_concurrent_downloads",
    5,
)
METADATA_CONCURRENT_DOWNLOADS = RUNTIME_CONFIG.get(
    "metadata_concurrent_downloads",
    MAX_CONCURRENT_DOWNLOADS,
)
PRICE_CONCURRENT_DOWNLOADS = RUNTIME_CONFIG.get(
    "price_concurrent_downloads",
    MAX_CONCURRENT_DOWNLOADS,
)

RETRY_CONFIG = SETTINGS.get("retry", {})
RETRY_ENABLED = RETRY_CONFIG.get("enabled", True)
RETRY_MAX_ATTEMPTS = RETRY_CONFIG.get("max_attempts", 3)
RETRY_BASE_DELAY_SECONDS = RETRY_CONFIG.get("base_delay_seconds", 0.5)
RETRY_MAX_DELAY_SECONDS = RETRY_CONFIG.get("max_delay_seconds", 8)
RETRY_JITTER_SECONDS = RETRY_CONFIG.get("jitter_seconds", 0.25)

RATE_LIMIT_CONFIG = SETTINGS.get("rate_limit", {})
RATE_LIMIT_ENABLED = RATE_LIMIT_CONFIG.get("enabled", True)
METADATA_INTERVAL_SECONDS = RATE_LIMIT_CONFIG.get(
    "metadata_interval_seconds",
    1.0,
)
PRICE_INTERVAL_SECONDS = RATE_LIMIT_CONFIG.get(
    "price_interval_seconds",
    0.2,
)
RATE_LIMIT_COOLDOWN_SECONDS = RATE_LIMIT_CONFIG.get(
    "cooldown_seconds",
    300,
)
MAX_RATE_LIMIT_COOLDOWN_EVENTS = RATE_LIMIT_CONFIG.get(
    "max_cooldown_events",
    3,
)

RUN_STATE_CONFIG = SETTINGS.get("run_state", {})
RESUME_ENABLED = RUN_STATE_CONFIG.get("resume_enabled", True)
RETRY_ERRORS_ON_RESUME = RUN_STATE_CONFIG.get(
    "retry_errors_on_resume",
    True,
)

RESEARCH_CONFIG = SETTINGS.get("research", {})
RESEARCH_AI_ENABLED = RESEARCH_CONFIG.get("ai_synthesis_enabled", False)
RESEARCH_DEFAULT_TOP = int(
    RESEARCH_CONFIG.get("default_top_candidates", 10)
)
RESEARCH_PROMPT_VERSION = str(
    RESEARCH_CONFIG.get("prompt_version", "v0.3-research-3")
)
RESEARCH_CACHE_DIR = BASE_DIR / RESEARCH_CONFIG.get(
    "cache_directory",
    "data/cache/research",
)
SYNTHESIS_CONFIG = RESEARCH_CONFIG.get("synthesis", {})
SYNTHESIS_PROVIDER = str(SYNTHESIS_CONFIG.get("provider", "openai"))
SYNTHESIS_MODEL = str(SYNTHESIS_CONFIG.get("model", "gpt-5.6-sol"))
SYNTHESIS_MAX_OUTPUT_TOKENS = int(SYNTHESIS_CONFIG.get("maximum_output_tokens", 8000))
SYNTHESIS_API_BASE_URL = str(SYNTHESIS_CONFIG.get("api_base_url", "https://api.openai.com/v1"))
RESEARCH_REVIEW_CONFIG = RESEARCH_CONFIG.get("review", {})
EVIDENCE_CONFIG = RESEARCH_CONFIG.get("evidence", {})
EVIDENCE_CACHE_DIR = BASE_DIR / EVIDENCE_CONFIG.get("cache_directory", "data/cache/evidence")
EVIDENCE_TTL_HOURS = float(EVIDENCE_CONFIG.get("cache_ttl_hours", 168))
EVIDENCE_MAX_DOCUMENTS = int(EVIDENCE_CONFIG.get("maximum_documents_per_company", 5))
EVIDENCE_MAX_AGE_DAYS = int(EVIDENCE_CONFIG.get("maximum_age_days", 730))
EVIDENCE_MAX_EXCERPT_CHARS = int(EVIDENCE_CONFIG.get("maximum_excerpt_characters", 12000))
EVIDENCE_ALLOWED_DOMAINS = tuple(EVIDENCE_CONFIG.get("allowed_domains", ["sec.gov"]))
EVIDENCE_SEC_FORMS = tuple(EVIDENCE_CONFIG.get("sec_forms", ["10-K", "10-Q", "8-K", "20-F", "40-F", "6-K"]))

BENCHMARKS = STRATEGY["benchmarks"]

MARKET_CAP_CONFIG = STRATEGY["universe"]["market_cap"]
MIN_MARKET_CAP = MARKET_CAP_CONFIG["minimum"]
SWEET_SPOT_MIN = MARKET_CAP_CONFIG["sweet_spot_min"]
SWEET_SPOT_MAX = MARKET_CAP_CONFIG["sweet_spot_max"]
MAX_MARKET_CAP = MARKET_CAP_CONFIG["maximum"]

SECTOR_BONUSES = STRATEGY["sector_bonus"]
WEIGHTS = STRATEGY["weights"]
SCORING_CONFIG = STRATEGY["scoring"]
ASSET_CLASSIFICATION_CONFIG = STRATEGY.get("asset_classification", {})
FUNDAMENTAL_WEIGHTS = STRATEGY.get("fundamental_weights", {})
FUNDAMENTAL_SCORING_CONFIG = STRATEGY.get("fundamental_scoring", {})
FUNDAMENTAL_DATA_POLICY = STRATEGY.get("fundamental_data_policy", {})
MOONSHOT_CONFIG = STRATEGY.get("moonshot", {})
REPORTS_CONFIG = STRATEGY.get("reports", {})
CALIBRATION_CONFIG = STRATEGY.get("calibration", {})
