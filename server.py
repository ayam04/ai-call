import plivo
from quart import Quart, websocket, Response, request
import asyncio
import websockets
import json
import base64
from datetime import datetime, timedelta
from conns import db_name
from pydantic import BaseModel
import bson
from quart_cors import cors
import requests
from dataclasses import dataclass
from typing import Dict, Optional

with open('config.json') as f:
    config = json.load(f)

@dataclass
class CallSession:
    candidate_name: str
    role: str
    jd: str
    company: str
    additional_info: str
    questions: list
    screening_id: str
    transcript: list
    call_uuid: Optional[str] = None
    stream_id: Optional[str] = None
    websocket_connections: Dict = None

    def __post_init__(self):
        if self.websocket_connections is None:
            self.websocket_connections = {}

class data(BaseModel):
    phone: int
    name: str
    id: str

screenings = db_name['screenings']
jobs = db_name['jobs']
jobquestions = db_name['jobquestions']
companies = db_name['companies']

NUMBER = config['PLIVO_NUMBER']
DEEPGRAM_API_KEY = config['DEEPGRAM_API_KEY']
AUTH_ID = config['PLIVO_AUTH_ID']
AUTH_TOKEN = config['PLIVO_AUTH_TOKEN']

client = plivo.RestClient(auth_id=AUTH_ID, auth_token=AUTH_TOKEN)

with open("prompt.txt", "r") as file:
    prompt = file.read()

app = Quart(__name__)
app = cors(app, allow_origin="*", allow_headers=["*"], allow_methods=["*"])

active_calls: Dict[str, CallSession] = {}

@app.before_serving
async def setup_cors():
    def _add_cors_headers(response):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Headers'] = '*'
        response.headers['Access-Control-Allow-Methods'] = '*'
        return response
    
    app.after_request(_add_cors_headers)

@app.post('/make-a-call')
async def make_outbound_call():
    request_data = await request.get_json()
    req = data(**request_data)

    screenData = screenings.find_one({"_id": bson.ObjectId(str(req.id))})
    jobData = jobs.find_one({"_id": bson.ObjectId(str(screenData['jobId']))})
    
    questions = []
    questionsData = jobquestions.find({'jobId': bson.ObjectId(str(screenData['jobId']))})
    for i in questionsData:
        questions.append(i['question'])

    companyData = companies.find_one({"_id": bson.ObjectId(str(jobData['companyId']))})

    call_session = CallSession(
        candidate_name=req.name,
        role=jobData['title'],
        jd=jobData['jobDescription'],
        company=companyData['name'],
        additional_info=f"About the company: {companyData['aboutUs']}",
        questions=questions,
        screening_id=req.id,
        transcript=[]
    )

    call_id = f"call_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{req.id}"
    
    call_response = client.calls.create(
        from_=NUMBER,
        to_="91"+str(req.phone)[-10:],
        answer_url=f"https://5947-2401-4900-5ba6-2cb8-914b-cdce-7688-9631.ngrok-free.app/webhook/{call_id}",
        answer_method='GET',
    )
    
    call_session.call_uuid = call_response.request_uuid
    active_calls[call_id] = call_session

    return {
        "status": "Call initiated", 
        "call_id": call_id,
        "call_uuid": call_response.request_uuid
    }

@app.route("/webhook/<call_id>", methods=["GET", "POST"])
async def webhook(call_id):
    if call_id not in active_calls:
        return Response("Invalid call ID", status=400)

    xml_data = f'''<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Record recordSession="true" redirect="false" />
        <Stream streamTimeout="86400" keepCallAlive="true" bidirectional="true" contentType="audio/x-mulaw;rate=8000" audioTrack="inbound">
            wss://{request.host}/media-stream/{call_id}
        </Stream>
    </Response>
    '''
    return Response(xml_data, mimetype='application/xml')

@app.websocket('/media-stream/<call_id>')
async def handle_message(call_id):
    if call_id not in active_calls:
        return

    call_session = active_calls[call_id]
    plivo_ws = websocket

    url = "wss://agent.deepgram.com/agent"
    headers = {"Authorization": f"Token {DEEPGRAM_API_KEY}"}

    try:
        async with websockets.connect(url, extra_headers=headers) as deepgram_ws:
            call_session.websocket_connections = {
                'plivo': plivo_ws,
                'deepgram': deepgram_ws
            }

            await send_session_update(call_session)
            receive_task = asyncio.create_task(
                receive_from_plivo(call_session)
            )

            try:
                async for message in deepgram_ws:
                    await receive_from_deepgram(message, call_session)
            except Exception as inner_e:
                print(f"Inner websocket error for call {call_id}: {inner_e}")
            
            await receive_task

    except Exception as e:
        print(f"Error in call {call_id}: {e}")
    finally:
        await save_transcript(call_session)
        if call_id in active_calls:
            del active_calls[call_id]

async def receive_from_plivo(call_session: CallSession):
    plivo_ws = call_session.websocket_connections['plivo']
    deepgram_ws = call_session.websocket_connections['deepgram']
    
    BUFFER_SIZE = 20 * 160
    inbuffer = bytearray(b"")

    try:
        while True:
            message = await plivo_ws.receive()
            data = json.loads(message)
            
            if data['event'] == 'media' and deepgram_ws.open:
                chunk = base64.b64decode(data['media']['payload'])
                inbuffer.extend(chunk)
            elif data['event'] == "start":
                call_session.stream_id = data['start']['streamId']
            elif data['event'] == "stop":
                break

            while len(inbuffer) >= BUFFER_SIZE:
                chunk = inbuffer[:BUFFER_SIZE]
                await deepgram_ws.send(chunk)
                inbuffer = inbuffer[BUFFER_SIZE:]

    except Exception as e:
        print(f"Error in Plivo communication: {e}")

async def receive_from_deepgram(message, call_session: CallSession):
    plivo_ws = call_session.websocket_connections['plivo']
    
    try:
        if isinstance(message, str):
            response = json.loads(message)
            
            if response.get('type') == 'ConversationText':
                call_session.transcript.append({
                    'role': response['role'],
                    'message': response['content']
                })
            
            elif response.get('type') == 'FunctionCallRequest':
                if response['function_name'] == 'endCall':
                    result = await end_call(call_session.call_uuid, response.get('parameters', {}).get('reason'))
                    functionCallResponse = {
                        "type": "FunctionCallResponse",
                        "function_call_id": response['function_call_id'],
                        "output": "Call ended successfully. Goodbye!" if result["status"] == "success" else f"Failed to end call: {result['message']}"
                    }
                    await plivo_ws.send(json.dumps(functionCallResponse))

                elif response['function_name'] == 'rescheduleInterview':
                    # Extract parameters with default values
                    params = response.get('parameters', {})
                    preferred_date = params.get('preferred_date') or (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
                    preferred_time = params.get('preferred_time') or (datetime.now() + timedelta(days=1)).strftime("%H:%M")
                    reason = params.get('reason', 'Candidate requested reschedule')
                    print(params)
                    # Validate required parameters
                    if not all([preferred_date, preferred_time]):
                        functionCallResponse = {
                            "type": "FunctionCallResponse",
                            "function_call_id": response['function_call_id'],
                            "output": "Missing required parameters for rescheduling"
                        }
                    else:
                        result = await reschedule_interview(
                            call_session.screening_id,
                            preferred_date,
                            preferred_time,
                            reason
                        )
                        functionCallResponse = {
                            "type": "FunctionCallResponse",
                            "function_call_id": response['function_call_id'],
                            "output": result["message"]
                        }
                    await plivo_ws.send(json.dumps(functionCallResponse))
            
            if response.get('type') == 'AgentStartedSpeaking':
                print(f"Agent speaking for call {call_session.screening_id}")
                
        else:
            audioDelta = {
                "event": "playAudio",
                "media": {
                    "contentType": 'audio/x-mulaw',
                    "sampleRate": 8000,
                    "payload": base64.b64encode(message).decode("ascii")
                }
            }
            await plivo_ws.send(json.dumps(audioDelta))
    
    except Exception as e:
        print(f"Error in Deepgram communication: {e}")

current_datetime = datetime.now().strftime("%Y-%m-%d %H:%M:%S IST")

async def send_session_update(call_session: CallSession):
    deepgram_ws = call_session.websocket_connections['deepgram']
    
    session_update = {
        "type": "SettingsConfiguration",
        "audio": {
            "input": {
                "encoding": "mulaw",
                "sample_rate": 8000
            },
            "output": {
                "encoding": "mulaw",
                "sample_rate": 8000,
                "container": "none",
            }
        },
        "agent": {
            "listen": {
                "model": "nova-2"
            },
            "think": {
                "provider": {
                    "type": "open_ai"
                },
                "model": "gpt-4o-mini",
                "instructions": prompt.format(
                    name=call_session.candidate_name,
                    role=call_session.role,
                    jd=call_session.jd,
                    info=call_session.additional_info,
                    company=call_session.company,
                    questions=call_session.questions
                ).strip(),
                "functions": [
                    {
                        "name": "endCall",
                        "description": "End the current phone call. Must be called using proper function calling format.",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "reason": {
                                    "type": "string",
                                    "enum": ["wrong_person", "user_request", "reschedule", "interview_complete", "declined_interview"],
                                    "description": "The reason for ending the call"
                                }
                            },
                            "required": ["reason"]
                        }
                    },
                    {
                        "name": "rescheduleInterview",
                        "description": f"""Reschedule the interview for a new time slot. Time should be in IST. The date and time right now is: {current_datetime}""",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "preferred_date": {
                                    "type": "string",
                                    "description": "The preferred date in YYYY-MM-DD format"
                                },
                                "preferred_time": {
                                    "type": "string",
                                    "description": "The preferred time in 24-hour HH:mm format (IST)"
                                },
                                "reason": {
                                    "type": "string",
                                    "description": "Reason for rescheduling"
                                }
                            },
                            "required": ["preferred_date", "preferred_time", "reason"]
                        }
                    }
                ]
            },
            "speak": {
                "model": "aura-hera-en"
            }
        },
        "context": {
            "messages": [
                {
                    "content": f"Hello, this is Reva from {call_session.company}. I have called you for a job interview. Am I talking to {call_session.candidate_name}?",
                    "role": "assistant"
                }
            ],
            "replay": True
        }
    }
    await deepgram_ws.send(json.dumps(session_update))

async def save_transcript(call_session: CallSession):
    try:
        if call_session.transcript:
            payload = {
                'screeningConversation': call_session.transcript
            }
            
            url = f'https://devnodeapi.hyrgpt.com/v1/screening-status/{call_session.screening_id}'
            response = requests.patch(url, json=payload)
            
            if response.status_code == 200:
                print(f"Transcript saved for screening ID: {call_session.screening_id}")
            else:
                print(f"Error saving transcript. Status code: {response.status_code}")
    except Exception as e:
        print(f"Error saving transcript: {e}")

async def end_call(call_uuid: str, reason: str = None):
    try:
        if not call_uuid:
            return {
                "status": "error",
                "message": "No active call UUID provided",
                "reason": reason
            }
            
        response = client.calls.delete(call_uuid=call_uuid)
        
        return {
            "status": "success",
            "message": "Call ended successfully",
            "reason": reason,
            "plivo_response": response
        }
        
    except plivo.exceptions.PlivoRestError as e:
        error_msg = f"Plivo error ending call: {str(e)}"
        print(error_msg)
        return {
            "status": "error",
            "message": error_msg,
            "error_details": str(e)
        }
        
    except Exception as e:
        error_msg = f"Unexpected error ending call: {str(e)}"
        print(error_msg)
        return {
            "status": "error",
            "message": error_msg,
            "error_details": str(e)
        }

async def reschedule_interview(screening_id: str, preferred_date: str, preferred_time: str, reason: str = None):
    try:
        try:
            date_obj = datetime.strptime(f"{preferred_date} {preferred_time}", "%Y-%m-%d %H:%M")
            
            if date_obj < datetime.now():
                return {
                    "status": "error",
                    "message": "Cannot schedule interview for a past date and time."
                }
                
        except ValueError as e:
            return {
                "status": "error",
                "message": f"Invalid date or time format: {str(e)}. Please use YYYY-MM-DD for date and HH:mm for time."
            }

        # Format for database storage (IST timezone)
        rescheduled_datetime = f"{preferred_date}T{preferred_time}:00+05:30"

        # Update the screening record
        result = screenings.update_one(
            {"_id": bson.ObjectId(screening_id)},
            {
                "$set": {
                    "rescheduledTo": rescheduled_datetime,
                    "rescheduledReason": reason or "Candidate requested reschedule",
                    "status": "rescheduled",
                    "updatedAt": datetime.utcnow().isoformat()
                }
            }
        )

        if result.modified_count > 0:
            return {
                "status": "success",
                "message": f"Interview rescheduled successfully to {preferred_date} at {preferred_time} IST"
            }
        else:
            return {
                "status": "error",
                "message": "Failed to update screening record"
            }

    except Exception as e:
        error_msg = f"Error rescheduling interview: {str(e)}"
        print(error_msg)
        return {
            "status": "error",
            "message": error_msg,
            "error_details": str(e)
        }

if __name__ == "__main__":
    app.run(port=3010)
