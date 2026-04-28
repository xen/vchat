import logging
from pathlib import Path
import os
import yaml
from expandvars import expandvars

__all__ = ("config",)


# C loader is much faster than Python based loader, but it requires libyaml to be
# installed into the system (or libyaml-dev)
try:
    YamlLoader = yaml.CSafeLoader
except AttributeError:
    YamlLoader = yaml.SafeLoader


class ValidationError(Exception):
    """Raised during the validation process of the config."""


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
        # Loader subclasses SafeLoader/CSafeLoader and only adds scalar constructors.
        return yaml.load(source, Loader)  # nosec B506
    finally:
        if hasattr(source, "close"):
            source.close()


def _load_config():
    default_file = Path(__file__).parent / "config.yaml"
    with default_file.open() as f:
        default_config = yaml_load(f)

    local_config = {}
    local_path = Path(__file__).parent.parent / "local.yaml"
    if local_path.exists():
        with local_path.open(encoding="utf-8") as cf:
            local_config = yaml_load(cf) or {}

    def merge(d1, d2):
        result = {}
        for k, v in d1.items():
            if k in d2:
                if isinstance(v, dict):
                    result[k] = merge(v, d2[k])
                else:
                    result[k] = d2[k]
            else:
                result[k] = v
        return result

    merged_config = merge(default_config, local_config)
    if not merged_config.get("openai_api_key"):
        merged_config["openai_api_key"] = os.getenv("OPENAI_API_KEY")

    if not merged_config.get("gigachat_api_key"):
        merged_config["gigachat_api_key"] = (
            os.getenv("GIGACHAT_API_KEY")
            or os.getenv("GIGACHAT_AUTH_KEY")
            or os.getenv("GIGACHAT_BASIC_AUTH")
        )
    return merged_config


config = _load_config()

logging.basicConfig()
logger = logging.getLogger(__name__)
if config.get("dump_config", False):
    logger.info("Configuration: %s", config)

conflevel = config.get("loglevel", "INFO")
if conflevel not in ["CRITICAL", "INFO", "DEBUG", "WARNING", "ERROR"]:
    conflevel = "INFO"
logger.setLevel(conflevel)
