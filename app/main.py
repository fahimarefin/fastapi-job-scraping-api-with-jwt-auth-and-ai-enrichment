# app/main.py
import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app.api.routes import router as jobs_router, categories_router
from app.core.config import get_settings
from app.models.schemas import HealthResponse

settings = get_settings()

logging.basicConfig(
    level=logging.DEBUG if settings.debug else logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description=(
        "Automatically scrapes job postings across 10 industries and 60+ hardcoded roles. "
        "No parameters required — location is fixed to USA and limit is 20 results per role. "
        "Aggregates data from Indeed, LinkedIn, Glassdoor, ZipRecruiter, and custom career pages."
    ),
)

# Explicit allowed origins — dashboard (dev + prod) and n8n webhook host
ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://10.0.80.79:5173",
    "http://206.162.244.135:5173",     # Add your VPS frontend origin here
    "http://206.162.244.135:8000",
    "https://n8n.srv1795032.hstgr.cloud",
]


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

app.include_router(jobs_router)
app.include_router(categories_router)


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