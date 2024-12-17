import asyncio
from fastapi import FastAPI
from hypercorn.asyncio import serve
from hypercorn.config import Config
from functions import app as quart_app, client
from pydantic import BaseModel
from conns import db_name
from bson.objectid import ObjectId
from datetime import datetime
import uvicorn
import json

with open('config.json') as f:
    config = json.load(f)

screenings = db_name['screenings']
jobs = db_name['jobs']
jobquestions = db_name['jobquestions']
companies = db_name['companies']
NUMBER = config['PLIVO_NUMBER']

class data(BaseModel):
    phone: int
    name: str
    id: str

router = FastAPI()

@router.post("/make-a-call")
async def make_outbound_call(req: data):
    screenData = screenings.find_one({"_id": ObjectId(str(req.id))})

    jobData = jobs.find_one({"_id": ObjectId(str(screenData['jobId']))})
    role = jobData['title']
    jd = jobData['jobDescription']

    questions = []
    questionsData = jobquestions.find({'jobId':ObjectId(str(screenData['jobId']))})
    for i in questionsData:
        questions.append(i['question']) 

    companyData = companies.find_one({"_id":ObjectId(str(jobData['companyId']))}) 
    company = companyData['name']
    companyAbout = companyData['aboutUs']

    config = Config()
    config.bind = [f"localhost:{quart_app.config.get('PORT', 5000)}"]
    quart_app.config['candidate_name'] = req.name
    quart_app.config['role'] = role
    quart_app.config['jd'] = jd
    quart_app.config['screeningId'] = req.id
    quart_app.config["company"] = company
    print(company)
    quart_app.config['additional_info'] = f"ABout the company: {companyAbout}"
    quart_app.config["questions"] = questions

    quart_app.config['transcript'] = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    quart_app.config['transcript_filename'] = f"transcript_{req.name}_{company}_{timestamp}.json"

    client.calls.create(
        from_= NUMBER,
        to_=f"91{req.phone}",
        answer_url="https://60c2-115-245-68-162.ngrok-free.app/webhook",
        answer_method='GET',
    )
    
    asyncio.create_task(serve(quart_app, config))
    
    return {"status": "Call initiated"}

if __name__ == "__main__":
    uvicorn.run("app:router",
                host=config["server"]["host"],
                port=config["server"]["port"], 
                reload=config["server"]["reload"],timeout_keep_alive=500)