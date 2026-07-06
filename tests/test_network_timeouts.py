import ast
from pathlib import Path


HTTP_METHODS = {"get", "post", "put", "patch", "delete", "request"}
PYTHON_ROOTS = (Path("vchat"), Path("jobs"), Path("gigatest.py"))


def _python_files() -> list[Path]:
    files: list[Path] = []
    for root in PYTHON_ROOTS:
        if root.is_file():
            files.append(root)
        else:
            files.extend(root.rglob("*.py"))
    return sorted(files)


def _uses_aiohttp_client_session(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "aiohttp"
            and func.attr == "ClientSession"
        ):
            return True
    return False


def _is_network_call(node: ast.Call, *, module_uses_aiohttp_client: bool) -> bool:
    if not isinstance(node.func, ast.Attribute) or node.func.attr not in HTTP_METHODS:
        return False

    value = node.func.value
    if isinstance(value, ast.Name) and value.id == "requests":
        return True
    return (
        module_uses_aiohttp_client
        and isinstance(value, ast.Name)
        and value.id == "session"
    )


def test_network_requests_have_explicit_timeouts() -> None:
    missing: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(), filename=str(path))
        module_uses_aiohttp_client = _uses_aiohttp_client_session(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not _is_network_call(
                node,
                module_uses_aiohttp_client=module_uses_aiohttp_client,
            ):
                continue
            if not any(keyword.arg == "timeout" for keyword in node.keywords):
                missing.append(f"{path}:{node.lineno}")

    assert missing == []


def test_gigachat_does_not_define_provider_specific_timeout_settings() -> None:
    config_source = Path("vchat/settings.py").read_text()

    assert "gigachat_oauth_timeout_seconds" not in config_source
    assert "gigachat_request_timeout_seconds" not in config_source
    assert "gigachat_suggest_timeout_seconds" not in config_source
