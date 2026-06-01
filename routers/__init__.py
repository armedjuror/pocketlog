from routers.oauth  import router as oauth_router
from routers.api_v1 import router as api_v1_router

routers = [oauth_router, api_v1_router]
