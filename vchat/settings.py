import logging
from pathlib import Path
import base64
import io
import os
from typing import Annotated, Any, Literal
import yaml
from aiohttp import web
from expandvars import expandvars
from itsdangerous import URLSafeTimedSerializer
from pydantic import BaseModel, ConfigDict, Field, ValidationError as PydanticError
from pydantic import field_validator
from redis.asyncio.client import Redis

DeviceConfig = Literal["auto", "cpu", "cuda", "mps"]

__all__ = (
    "AppConfig",
    "LOGGER_KEY",
    "REDIS_KEY",
    "SIGNER_KEY",
    "STATIC_VERSION_KEY",
    "cfg",
)

LOGGER_KEY: web.AppKey[logging.Logger] = web.AppKey("logger", logging.Logger)
REDIS_KEY: web.AppKey[Redis] = web.AppKey("redis", Redis)
SIGNER_KEY: web.AppKey[URLSafeTimedSerializer] = web.AppKey(
    "signer", URLSafeTimedSerializer
)
STATIC_VERSION_KEY: web.AppKey[str] = web.AppKey("static_version", str)


YamlLoader = yaml.CSafeLoader


class ValidationError(Exception):
    """Raised during the validation process of the config."""


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret_key: str
    cookie_key: str
    mode: Literal["local", "stage", "production"] = "stage"
    crawler_user_agent: str = "Dzen-AI/1.0"

    max_upload_size: int = Field(5 * 1024 * 1024, ge=1)
    raw_content_max_bytes: int = Field(10 * 1024 * 1024, ge=1)
    enable_https_middleware: bool = True
    cookie_name: str = "USER"
    cookie_domain: str = "chat.vbudushee.ru"
    cookie_secure: bool = True
    session_max_age_seconds: int = Field(2_592_000, ge=1)
    auth_session_time: int = Field(0, ge=0)
    auth_session_idle_timeout_seconds: int = Field(14_400, ge=0)

    redis_uri: str = "redis://localhost:6379/30"
    database_uri: str = "postgresql+asyncpg://xen@localhost:5432/vchat"
    api_update_timestamp_ttl_seconds: int = Field(60, ge=1)
    api_update_nonce_ttl_seconds: int = Field(180, ge=1)
    api_update_rate_limit_window_seconds: int = Field(60, ge=1)
    api_update_rate_limit_requests: int = Field(60, ge=1)
    widget_page_discovery_enabled: bool = False

    celery_redis_uri: str = "redis://localhost:6379/"
    celery_broker_db: int = Field(31, ge=0)
    celery_backend_db: int = Field(32, ge=0)
    celery_default_queue: str = "celery"
    celery_visibility_timeout: int = Field(21_600, ge=1)
    celery_worker_concurrency: int = Field(4, ge=1)
    celery_worker_max_tasks_per_child: int = Field(100, ge=1)
    celery_worker_max_memory_per_child_kb: int = Field(524_288, ge=1)

    loglevel: Literal["CRITICAL", "INFO", "DEBUG", "WARNING", "ERROR"] = "INFO"
    log_format: str = "text"
    log_config: str | None = "vchat/logging.ini"
    sql_echo: bool = False
    time_zone: str = "Europe/Moscow"

    public_url: str = "https://chat.vbudushee.ru"
    allowed_origins: list[str] = Field(default_factory=list)
    trusted_proxy_cidrs: list[str] = Field(default_factory=list)

    chat_provider: str = "gigachat"
    chat_model: str = "GigaChat-2-Pro"
    chat_aux_provider: str = "gigachat"
    chat_aux_model: str = "GigaChat-2"
    chat_response_max_tokens: int = Field(900, ge=1)
    chat_suggestions_max_context_chars: int = Field(3000, ge=1)
    llm_request_timeout_seconds: float = Field(60.0, gt=0)
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    yandex_api_key: str = ""
    yandex_base_url: str = ""
    gigachat_api_key: str = ""
    gigachat_base_url: str = "https://gigachat.devices.sberbank.ru/api/v1"
    gigachat_oauth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    gigachat_scope: str = "GIGACHAT_API_CORP"
    gigachat_verify_ssl_certs: bool = True
    gigachat_test_model: str = "GigaChat-2-Pro"
    gigachat_test_question: str | None = None
    gigachat_models: list[str | dict[str, Any]] = Field(default_factory=list)

    openai_guardrails_enabled: bool = True
    guardrails_ru_pii_enabled: bool = True
    embedding_model_id: str = "deepvk/USER-bge-m3"
    reranker_model_id: str = "BAAI/bge-reranker-v2-m3"
    embedding_device: DeviceConfig = "auto"
    reranker_device: DeviceConfig = "auto"
    vec_dim: int = Field(1024, ge=1)
    embedding_max_seq_length: int = Field(8192, ge=1)
    embedding_chunk_max_tokens: int = Field(3500, ge=1)
    embedding_chunk_overlap_tokens: int = Field(400, ge=0)
    embedding_chunk_max_chars: int = Field(12_000, ge=1)
    embedding_block_max_chars: int = Field(48_000, ge=1)
    embedding_entity_scan_max_chars: int = Field(24_000, ge=1)
    embedding_encode_batch_max_chars: int = Field(12_000, ge=1)
    embedding_document_max_chars: int = Field(100_000, ge=1)
    embedding_pending_chunks_batch_size: int = Field(8, ge=1)
    embedding_pending_chunks_max_inflight: int = Field(32, ge=1)
    embedding_pending_chunks_counter_ttl_seconds: int = Field(600, ge=60)
    embedding_ensure_pending_chunks_ttl_seconds: int = Field(120, ge=30)
    embedding_refresh_project_index_ttl_seconds: int = Field(300, ge=60)
    embedding_index_document_schedule_ttl_seconds: int = Field(21_600, ge=300)
    page_shingle_insert_batch_size: int = Field(2000, ge=100)
    embedding_model_reset_after_documents: int = Field(20, ge=0)
    embedding_worker_instances: Annotated[int, Field(ge=1)] | Literal["auto"] = "auto"
    embedding_worker_cpu_reserve: int = Field(1, ge=0)
    request_embedding_concurrency: int = Field(1, ge=1)
    request_embedding_executor_workers: int = Field(1, ge=1)
    request_embedding_queue_timeout_seconds: float = Field(20.0, gt=0)
    request_embedding_queue_warn_seconds: float = Field(1.0, gt=0)
    request_embedding_torch_threads: int = Field(1, ge=1)
    metadata_only_csv_min_chars: int = Field(50_000, ge=1)
    metadata_only_raw_size_min_bytes: int = Field(1_000_000, ge=1)

    auth_basic_enabled: bool = True
    auth_ldap_enabled: bool = False
    admin_user_create_enabled: bool = True
    ldap_server: str = "ldap://ldap.example.com:389"
    ldap_use_ssl: bool = False
    ldap_bind_dn: str = ""
    ldap_bind_password: str = ""
    ldap_search_base: str = ""
    ldap_search_filter: str = "(mail={email})"
    ldap_attr_name: str = "displayName"
    ldap_required_group_dn: str = ""
    ldap_member_of_attr: str = "memberOf"

    readiness_celery_timeout_seconds: float = Field(1.0, gt=0)
    crawler_concurrent_requests: int = Field(16, ge=1)
    crawler_download_delay: float = Field(0.0, ge=0)
    crawler_download_timeout: float = Field(30.0, gt=0)

    @field_validator("secret_key", "cookie_key", mode="before")
    @classmethod
    def _decode_binary_secret(cls, value: Any) -> Any:
        if isinstance(value, bytes):
            return base64.urlsafe_b64encode(value).decode("ascii")
        return value

    @field_validator("loglevel", mode="before")
    @classmethod
    def _normalize_loglevel(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.upper()
        return value

    @field_validator("embedding_worker_instances", mode="before")
    @classmethod
    def _normalize_embedding_worker_instances(cls, value: Any) -> Any:
        if value == "":
            return "auto"
        return value

    @property
    def csrf_secret(self) -> bytes:
        return self.secret_key.encode("utf-8")


def yaml_load(source, loader=YamlLoader):
    # took from https://github.com/mkdocs/mkdocs/blob/master/mkdocs/utils/__init__.py#L62
    """
    Wrap PyYaml's loader so we can extend it to suit our needs.
    Load all strings as unicode and validate for boolean values.
    """

    class Loader(loader):
        """
        Define a custom loader derived from the global loader to leave the
        global loader unaltered.
        """

    def str_to_bool(value):
        """
        Convert possible boolean strings to Python boolean values.
        """
        if isinstance(value, str):
            value_lower = value.lower()
            if value_lower in ["true", "yes", "on", "1"]:
                return True
            if value_lower in ["false", "no", "off", "0"]:
                return False
        return value

    def construct_yaml_str(self, node):
        """
        Override the default string handling function to validate and convert
        boolean-like strings to Python booleans.
        """
        value = self.construct_scalar(node)
        return str_to_bool(value)

    def construct_yaml_env(self, node):
        """Construct env variables and validate them for boolean-like values."""
        value = self.construct_scalar(node)
        expanded_value = expandvars(value)
        return str_to_bool(expanded_value)

    # Attach our constructors to the custom loader
    Loader.add_constructor("tag:yaml.org,2002:str", construct_yaml_str)
    Loader.add_constructor("!env", construct_yaml_env)

    try:
        # Loader subclasses the required C loader and only adds scalar constructors.
        return yaml.load(source, Loader)  # nosec B506
    finally:
        if hasattr(source, "close"):
            source.close()


def _load_config():
    default_file = Path(__file__).parent / "config.yaml"
    with default_file.open() as f:
        default_config = yaml_load(f)
    default_security_values = {}
    for key in ("secret_key", "cookie_key"):
        default_value = default_config[key]
        if isinstance(default_value, bytes):
            default_value = base64.urlsafe_b64encode(default_value).decode("ascii")
        default_security_values[key] = str(default_value or "").strip()

    local_config = {}
    local_path = Path(__file__).parent.parent / "local.yaml"
    if local_path.exists():
        with local_path.open(encoding="utf-8") as cf:
            local_config = yaml_load(cf) or {}

    def merge(d1, d2):
        result = {}
        for k, v in d1.items():
            if k in d2:
                if isinstance(v, dict) and isinstance(d2[k], dict):
                    result[k] = merge(v, d2[k])
                else:
                    result[k] = d2[k]
            else:
                result[k] = v
        for k, v in d2.items():
            if k not in result:
                result[k] = v
        return result

    merged_config = merge(default_config, local_config)
    model_keys = set(AppConfig.model_fields)
    for env_key, env_value in os.environ.items():
        key = env_key.lower()
        if key not in model_keys:
            continue
        merged_config[key] = yaml_load(io.StringIO(env_value))

    try:
        validated_config = AppConfig.model_validate(merged_config)
    except PydanticError as exc:
        lines = ["Config validation failed:"]
        for error in exc.errors():
            path = ".".join(str(part) for part in error["loc"]) or "<root>"
            lines.append(f"- {path}: {error['msg']}")
        raise ValidationError("\n".join(lines)) from exc

    if validated_config.mode == "production":
        unsafe_keys = [
            key
            for key in ("secret_key", "cookie_key")
            if not (value := getattr(validated_config, key).strip())
            or value == default_security_values[key]
            or value.lower() in {"change-me", "changeme"}
        ]
        if unsafe_keys:
            keys = ", ".join(unsafe_keys)
            raise ValidationError(
                "Production config must override security keys: "
                f"{keys}. Set them in local.yaml or environment variables."
            )

    return validated_config


cfg = _load_config()

logging.basicConfig()
logger = logging.getLogger(__name__)
logger.setLevel(cfg.loglevel)
