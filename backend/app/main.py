from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routers import (
    after_sales,
    auth,
    catalog,
    chat,
    conversations,
    health,
    memories,
    orders,
)
from app.core.config import get_settings

settings = get_settings()

app = FastAPI(title="PC Agent API", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.backend_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(health.router, prefix="/api")
app.include_router(auth.router, prefix="/api")
app.include_router(conversations.router, prefix="/api")
app.include_router(memories.router, prefix="/api")
app.include_router(chat.router, prefix="/api")
app.include_router(catalog.router, prefix="/api")
app.include_router(orders.router, prefix="/api")
app.include_router(after_sales.router, prefix="/api")


@app.get("/")
async def root():
    return {"name": "PC Agent API", "health": "/api/health"}
