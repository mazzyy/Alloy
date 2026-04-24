from fastapi import APIRouter

from app.api.routes import items, login, private, users, utils
from app.core.config import settings

# <<ALLOY_DEPS_IMPORT>>
# Block-injected imports land below this line. Do not remove — the
# scaffolder uses this marker as the anchor for `patches:` entries that
# need to add module-level imports (auth/clerk, auth/jwt, etc.).

api_router = APIRouter()
api_router.include_router(login.router)
api_router.include_router(users.router)
api_router.include_router(utils.router)
api_router.include_router(items.router)

# <<ALLOY_ROUTER_INCLUDE>>
# Block-injected `api_router.include_router(...)` calls land below this
# line. Order matters when multiple blocks add overlapping prefixes —
# the scaffolder applies them in block-resolution order, which is sorted.


if settings.ENVIRONMENT == "local":
    api_router.include_router(private.router)
