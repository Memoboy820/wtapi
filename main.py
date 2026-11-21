from fastapi import FastAPI
from src.api import main_router
from fastapi.middleware.cors import CORSMiddleware


app = FastAPI()

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

