from fastapi import APIRouter
from asyncio import to_thread
from src.services.sql import select_sql

router = APIRouter()


@router.get("/sqb_team/{tag}")
async def get_sqb_team(tag: str):
    return await to_thread(select_sql, tag=tag)


