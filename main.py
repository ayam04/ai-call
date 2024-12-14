import os
import asyncio
import uvicorn
from fastapi import FastAPI
from hypercorn.asyncio import serve
from hypercorn.config import Config
from server import app as quart_app, client
from pydantic import BaseModel
from dotenv import load_dotenv

load_dotenv()
class data(BaseModel):
    phone: int
    name: str

fastapi_app = FastAPI()

@fastapi_app.post("/start-quart-app")
async def start_quart_app(req: data):
    config = Config()
    config.bind = [f"localhost:{quart_app.config.get('PORT', 5000)}"]
    quart_app.config['candidate_name'] = req.name

    client.calls.create(
        from_=os.getenv('PLIVO_FROM_NUMBER'),
        to_=f"91{req.phone}",
        answer_url=os.getenv('PLIVO_ANSWER_XML'),
        answer_method='GET',
    )
    
    asyncio.create_task(serve(quart_app, config))
    
    return {"status": "Call initiated"}

if __name__ == "__main__":
    uvicorn.run("main:fastapi_app", port=8000, reload=True)