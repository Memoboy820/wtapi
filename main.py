from fastapi import FastAPI
from contextlib import asynccontextmanager, suppress
from src.api import main_router
from fastapi.middleware.cors import CORSMiddleware
from src.services.sql import init_sql
from asyncio import to_thread
import asyncio
from src.task.sqb_teams import main


@asynccontextmanager
async def lifespan(app: FastAPI):
    await asyncio.to_thread(init_sql)

    collector_task = asyncio.create_task(main())

    try:
        yield
    finally:
        collector_task.cancel()
        with suppress(asyncio.CancelledError):
            await collector_task


app = FastAPI(lifespan=lifespan)


app.add_middleware(
    CORSMiddleware, 
    allow_origins=["*"], 
    allow_methods=["*"],
    allow_credentials=False, 
    allow_headers=["*"]
)
#  uvicorn main:app --reload 
#  fastapi dev FastApiStructure\scr\main.py

app.include_router(main_router)

