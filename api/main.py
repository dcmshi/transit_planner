"""
FastAPI application assembly — the `uvicorn api.main:app` entry point.

The application is split by concern:
  api/lifespan.py  — startup/shutdown, APScheduler jobs, the ingest slot
  api/routes.py    — endpoint handlers and the route-scoring pipeline
  api/cache.py     — the route cache (TTL, negative caching, single-flight)
  api/ratelimit.py — per-IP sliding-window rate limiting
  api/schemas.py   — Pydantic response models
"""

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.lifespan import lifespan
from api.routes import router
from config import CORS_ORIGINS

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="GO Transit Reliability Router",
    description="Reliability-first routing for GO bus routes (Toronto ↔ Guelph).",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(router)
