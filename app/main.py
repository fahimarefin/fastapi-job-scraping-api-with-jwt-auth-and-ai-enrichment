from contextlib import asynccontextmanager
import logging
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import router as jobs_router, categories_router, trigger_router
from app.api.candidate_routes import candidates_router
from app.api.auth_routes import auth_router
from app.api.deps import get_current_user
from app.core.config import get_settings
from app.core.database import connect_to_mongo, close_mongo_connection
from app.models.schemas import HealthResponse


settings = get_settings()

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)



@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    logger.info("Starting %s v%s...", settings.app_name, settings.app_version)
    await connect_to_mongo()
    yield
    # Shutdown
    logger.info("Shutting down %s...", settings.app_name)
    await close_mongo_connection()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="Job Scraper & Candidate Discovery API with MongoDB JWT Authentication",
    lifespan=lifespan,
)


# Explicit allowed origins — dashboard (dev + prod) and n8n webhook host
ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://10.0.80.79:5173",
    "http://206.162.244.135:5173",     # Add your VPS frontend origin here
    "http://206.162.244.135:8000",
    "https://n8n.srv1795032.hstgr.cloud",
    "http://206.162.244.135:3001",
    "https://n8n.rebekhoffman-staffing.com",   # added: your CDN/proxy domain
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# Authentication endpoints (register, login, token) remain public
app.include_router(auth_router)

# Protect all functional endpoints with JWT authentication
app.include_router(jobs_router, dependencies=[Depends(get_current_user)])
app.include_router(categories_router, dependencies=[Depends(get_current_user)])
app.include_router(trigger_router, dependencies=[Depends(get_current_user)])
app.include_router(candidates_router, dependencies=[Depends(get_current_user)])


@app.get("/health", response_model=HealthResponse, tags=["meta"])
async def health() -> HealthResponse:
    return HealthResponse(status="ok", version=settings.app_version)


@app.get("/", tags=["meta"])
async def root() -> dict:
    return {
        "name": settings.app_name,
        "version": settings.app_version,
        "docs": "/docs",
    }