# plugins/__init__.py
# ── Supported platforms ────────────────────────────────────────────────────
# This is the single source of truth for which bot integrations are enabled.
# Add a platform name here AND import its router below to activate it.

SUPPORTED_PLUGINS: set[str] = {
    "telegram",
    # "whatsapp",
    # "email",
}

# ── Router registration ────────────────────────────────────────────────────
from plugins.telegram import router as telegram_router

routers = [telegram_router]

# from plugins.whatsapp import router as whatsapp_router
# routers = [telegram_router, whatsapp_router]
