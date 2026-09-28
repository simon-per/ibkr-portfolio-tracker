"""
"Clearly separate", as a property of the import graph (docs/crypto.md).

The crypto book is kept out of every stock figure by never being written to a stock
table — which only holds while no stock module reads a crypto table and no crypto module
writes a stock one. Both directions are checked here over the AST of `app/`, with
allowlists rather than denylists: a new module on either side fails until someone decides
it belongs, which is the moment the question is cheap.

1. **Who may reference the crypto modules.** Only the crypto code itself and its
   registration points (models registry, router mounting, the scheduler's job, the auth
   prefix, the public scheduler routes that *exclude* crypto rows).
2. **What the crypto code may import.** Its own modules, and the shared infrastructure it
   genuinely needs — settings, database, clock, redaction, gates, FX, app settings, sync
   history. No stock model, service or repository.
"""
import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"

CRYPTO_MODULES = {
    "app.models.crypto",
    "app.services.crypto_service",
    "app.services.coinstats_client",
    "app.routers.crypto",
    "app.schemas.crypto",
    "app.cli.coinstats_probe",
}

# Non-crypto modules allowed to import a crypto module, each for a named reason.
ALLOWED_REFERRERS = {
    "app.models": "the models registry, so Alembic and create_all see the tables",
    "app.main": "mounts the /api/crypto router",
    "app.services.scheduler_service": "registers the crypto jobs and runs them",
    "app.routers.scheduler": "names the crypto run type in order to EXCLUDE it",
}

# Shared modules the crypto code may import. Nothing that holds stock data.
ALLOWED_SHARED = {
    "app.clock",
    "app.config",
    "app.database",
    "app.redact",
    "app.single_flight",
    "app.models.exchange_rate",
    "app.repositories.app_settings_repository",
    "app.repositories.sync_run_repository",
    "app.services.base_fx",
    "app.services.currency_service",
    "app.services.fx_preload",
    "app.services.native_amounts",
    # The router asks the scheduler for the crypto group's next run time.
    "app.services.scheduler_service",
}


def _module_name(path: Path) -> str:
    parts = path.relative_to(APP_DIR.parent).with_suffix("").parts
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _imports(path: Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
            # `from app.models import crypto` names the module through an attribute.
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return {name for name in found if name.startswith("app.")}


def _app_modules():
    for path in APP_DIR.rglob("*.py"):
        yield _module_name(path), path


def test_every_crypto_module_exists():
    names = {name for name, _ in _app_modules()}
    assert CRYPTO_MODULES <= names, sorted(CRYPTO_MODULES - names)


def test_only_the_allowlisted_modules_reference_the_crypto_code():
    offenders = {}
    for name, path in _app_modules():
        if name in CRYPTO_MODULES:
            continue
        crypto_refs = _imports(path) & CRYPTO_MODULES
        if crypto_refs and name not in ALLOWED_REFERRERS:
            offenders[name] = sorted(crypto_refs)
    assert not offenders, (
        f"Stock-side modules importing crypto code: {offenders}. The crypto book stays "
        f"out of every stock figure only while no stock reader touches it — if this "
        f"one must, add it to ALLOWED_REFERRERS with the reason."
    )


def test_the_crypto_code_imports_no_stock_model_service_or_repository():
    offenders = {}
    for name, path in _app_modules():
        if name not in CRYPTO_MODULES:
            continue
        foreign = {
            imported for imported in _imports(path)
            if imported not in CRYPTO_MODULES and imported not in ALLOWED_SHARED
            # `from app.models.crypto import X` also yields "app.models.crypto.X".
            and not any(imported.startswith(m + ".") for m in CRYPTO_MODULES | ALLOWED_SHARED)
        }
        if foreign:
            offenders[name] = sorted(foreign)
    assert not offenders, (
        f"Crypto modules importing beyond the shared allowlist: {offenders}. Crypto must "
        f"never write a stock table; widen ALLOWED_SHARED only for infrastructure."
    )
