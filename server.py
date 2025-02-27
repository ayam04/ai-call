import bson.errors
import plivo
from quart import Quart, websocket, Response, request
import asyncio
import websockets
import json
import base64
from datetime import datetime, timedelta, timezone
from conns import *
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
    skills: list
    screening_questions: Optional[list] = None
    knowledge_base: Optional[str] = None
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
    # tone: str

class scoreData(BaseModel):
    screeningId: str
    generateScore: bool

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

@app.route('/')
async def health_check():
    routes = []
    for rule in app.url_map.iter_rules():
        routes.append({
            'endpoint': rule.endpoint,
            'methods': list(rule.methods),
            'path': str(rule)
        })

    ws_status = "Not tested"
    try:
        async with websockets.connect(
            "wss://agent.deepgram.com/agent",
            extra_headers={"Authorization": f"Token {DEEPGRAM_API_KEY}"}
        ) as ws:
            ws_status = "Connected"
    except Exception as e:
        ws_status = f"Error: {str(e)}"

    active_calls_info = {
        'count': len(active_calls),
        'call_ids': list(active_calls.keys())
    }

    plivo_status = "Not tested"
    try:
        account_details = client.account.get()
        plivo_status = "Connected"
    except Exception as e:
        plivo_status = f"Error: {str(e)}"

    db_status = "Not tested"
    try:
        db_name.command('ping')
        db_status = "Connected"
    except Exception as e:
        db_status = f"Error: {str(e)}"

    status_info = {
        'status': 'running',
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S %Z'),
        "account_details": account_details,
        'routes': routes,
        'connections': {
            'deepgram_websocket': ws_status,
            'plivo_api': plivo_status,
            'mongodb': db_status
        },
        'active_calls': active_calls_info,
        'version': '1.2.0'
    }

    return status_info

@app.post('/make-a-call')
async def make_outbound_call():
    request_data = await request.get_json()
    req = data(**request_data)

    screenData = screenings.find_one({"_id": bson.ObjectId(str(req.id))})
    jobData = jobs.find_one({"_id": bson.ObjectId(str(screenData['jobId']))})
    
    questions = []
    squestions = []
    questionsData = jobquestions.find({'jobId': bson.ObjectId(str(screenData['jobId'])), 'type': {"$ne":'screening'}})
    for i in questionsData:
        questions.append(i['question'])

    try:
        questions_screeningData = jobquestions.find({'jobId': bson.ObjectId(str(screenData['jobId'])), "type": "screening"})
        for i in questions_screeningData:
            squestions.append(i['question']) 
    except:
        pass

    companyData = companies.find_one({"_id": bson.ObjectId(str(jobData['companyId']))})

    call_session = CallSession(
        candidate_name=req.name,
        role=jobData['title'],
        jd=jobData['jobDescription'],
        company=companyData['name'],
        additional_info=f"About the company: {companyData['aboutUs']}",
        questions=questions,
        screening_questions=squestions,
        screening_id=req.id,
        knowledge_base=jobData['knowledgeBase'],
        skills=jobData['skill'],
        # tone=req.tone,
        transcript=[]
    )

    call_id = f"call_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{req.id}"
    
    call_response = client.calls.create(
        from_=NUMBER,
        to_="91"+str(req.phone)[-10:],
        answer_url=f"{config['server']['url']}/webhook/{call_id}",
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

    print(f"Generating webhook response for call_id: {call_id}")
    print(f"Request host: {request.host}")
    
    xml_data = f'''<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Record 
            recordSession="true" 
            redirect="false" 
            maxLength="3600" 
            callbackUrl="{config['server']['url']}/recording_callback?screening_id={active_calls[call_id].screening_id}"
            callbackMethod="POST"
        />
        <Stream 
            streamTimeout="86400" 
            keepCallAlive="true" 
            bidirectional="true" 
            contentType="audio/x-mulaw;rate=8000" 
            audioTrack="inbound">
            wss://{request.host}/media-stream/{call_id}
        </Stream>
    </Response>
    '''
    return Response(xml_data, mimetype='application/xml')

@app.websocket('/media-stream/<call_id>')
async def handle_message(call_id):
    try:
        print(f"Websocket connection attempt for call_id: {call_id}")
        
        if call_id not in active_calls:
            print(f"Call ID {call_id} not found in active_calls")
            return

        call_session = active_calls[call_id]
        plivo_ws = websocket

        print(f"Active calls: {list(active_calls.keys())}")

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
            try:
                await save_transcript(call_session.transcript, call_session.screening_id)
                await score_candidate(call_session.transcript, call_session.screening_id, call_session.skills)
            except Exception as e:
                print(f"Error during transcript saving or candidate scoring: {e}")
            if call_id in active_calls:
                del active_calls[call_id]
    except Exception as e:
        print(f"Error in websocket connection: {e}")
        raise

@app.route('/recording_callback', methods=["POST"])
async def recording_callback():
    try:
        form_data = await request.form
        recording_url = (
            form_data.get("RecordingUrl")
            or form_data.get("recording_url")
            or form_data.get("RecordUrl")
        )
        screening_id = form_data.get("screening_id") or request.args.get("screening_id")
        
        if recording_url and screening_id:
            screenings.update_one(
                {"_id": bson.ObjectId(screening_id)},
                {"$set": {"callRecordingURL": recording_url}}
            )
            print(f"Recording saved for screening_id: {screening_id}")
        else:
            print("Missing recording URL or screening_id in callback data.")
    except Exception as e:
        print(f"Error processing recording callback: {e}")
    return Response("OK", status=200)

@app.post('/phone-agent-score')
async def phone_agent_score():
    try:
        req = await request.get_json()
        req = scoreData(**req)
        if req.generateScore:
            recordingVal = screenings.find_one({"_id": bson.ObjectId(str(req.screeningId))}).get('callRecordingURL') or screenings.find_one({"_id": bson.ObjectId(str(req.screeningId))}).get('recording_url')
            scoreVal = screenings.find_one({"_id": bson.ObjectId(str(req.screeningId))}).get('screeningAssessmentScore') or screenings.find_one({"_id": bson.ObjectId(str(req.screeningId))}).get('CandidateScore')
            
            if recordingVal and scoreVal:
                screenings.update_one(
                    {"_id": bson.ObjectId(str(req.screeningId))},
                    {"$set": {
                        "callRecordingURL": recordingVal,
                        "screeningAssessmentScore": scoreVal
                        }},
                )

            return {"status": "success", "message": "Scores refreshed successfully"}
        else:
            return {"status": "error", "message": "generateScore is true"}

    except Exception as e:
        return {"status": "error", "message": str(e)}

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

def check_end_call_trigger(message: str) -> bool:
    goodbye_phrases = [
        'goodbye',
        # "I'll be ending the call now.",
        # 'have a great day',
        # "We'll see you soon"
        # 'end of interview', 
        # 'interview is complete', 
        # 'talk to you later'
    ]
    
    lowercase_message = message.lower()
    return any(phrase in lowercase_message for phrase in goodbye_phrases)

async def receive_from_deepgram(message, call_session: CallSession):
    plivo_ws = call_session.websocket_connections['plivo']
    deepgram_ws = call_session.websocket_connections['deepgram']
    
    try:
        if isinstance(message, str):
            response = json.loads(message)
            print( response)
            
            if response['type'] == 'ConversationText':
                current_message = response.get('content', '').strip()
                
                call_session.transcript.append({
                    'role': response['role'],
                    'message': current_message
                })
                
                if check_end_call_trigger(current_message):
                    result = await end_call(call_session.call_uuid, 'wrong_person')
                    print(f"Auto-ending call due to goodbye phrase: {current_message}")
            
            elif response['type'] == 'FunctionCallRequest':
                if response['function_name'] == 'endCall':
                    reason = response.get('input', {})
                    result = await end_call(call_session.call_uuid, reason)
                    print(result)
                    functionCallResponse = {
                        "type": "FunctionCallResponse",
                        "function_call_id": response['function_call_id'],
                        "output": "Call ended successfully. Goodbye!" if result["status"] == "success" else f"Failed to end call: {result['message']}"
                    }

                    print(functionCallResponse)
                    await deepgram_ws.send(json.dumps(functionCallResponse))

                elif response['function_name'] == 'rescheduleInterview':
                    params = response.get('input', {})
                    preferred_date = params.get('preferred_date')
                    preferred_time = params.get('preferred_time')
                    reason = params.get('reason', 'Candidate requested reschedule')

                    if not preferred_date or not preferred_time:
                        functionCallResponse = {
                            "type": "FunctionCallResponse",
                            "function_call_id": response['function_call_id'],
                            "output": "Missing required parameters for rescheduling. Please provide both date and time."
                        }
                    else:
                        result = reschedule_interview(
                            call_session.screening_id,
                            preferred_date,
                            preferred_time,
                            reason
                        )
                        print(result)
                        functionCallResponse = {
                            "type": "FunctionCallResponse",
                            "function_call_id": response['function_call_id'],
                            "output": result["status"]
                        }
                    
                    print(functionCallResponse)
                    await deepgram_ws.send(json.dumps(functionCallResponse))
            
            if response["type"] == 'AgentStartedSpeaking':
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
                    "model": "nova-3"
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
                        questions=call_session.questions,
                        squestions=call_session.screening_questions,
                        # tone= call_session.tone,
                        knowledge_base=call_session.knowledge_base
                    ).strip(),
                    "functions": [
                        {
                            "name": "endCall",
                            "description": "End the current phone call. Must be called using proper function calling format. This function will be executed in all scenarios to ensure the call is ended appropriately.",
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
                            "description": f"""Reschedule the interview for a new time slot. Time should be in IST. The date and time right now is: {current_datetime}, use this without asking the user about tomrrow's date if they ask to reschedule the call tomorrow. Also dont ask the time zone, silently take it as IST""",
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
                    "model": "aura-asteria-en"
                }
            }
        }
    await deepgram_ws.send(json.dumps(session_update))

async def save_transcript(transcript: list, screening_id: str):
    try:
        if transcript and screening_id:
            payload = {
                'screeningConversation': transcript
            }
            
            url = f'https://devnodeapi.hyrgpt.com/v1/screening-status/{screening_id}'
            response = requests.patch(url, json=payload)
            
            if response.status_code == 200:
                print(f"Transcript saved for screening ID: {screening_id}")
            else:
                print(f"Error saving transcript. Status code: {response.status_code}")

    except Exception as e:
        print(f"Error saving transcript: {e}")

async def end_call(call_uuid: str, reason: str = None):
    try:
        if not call_uuid:
            print(f"Attempted to end call with no UUID. Reason: {reason}")
            return {
                "status": "error",
                "message": "No active call UUID provided",
                "reason": reason
            }
        
        try:
            response = client.calls.delete(call_uuid=call_uuid)
            
            print(f"Call {call_uuid} ended successfully. Reason: {reason}")
            
            return {
                "status": "success",
                "message": "Call ended successfully",
                "reason": reason,
                "plivo_response": response
            }
        
        except plivo.exceptions.PlivoRestError as plivo_err:
            error_msg = f"Plivo error ending call {call_uuid}: {str(plivo_err)}"
            print(error_msg)
            
            return {
                "status": "error",
                "message": error_msg,
                "error_details": str(plivo_err)
            }
        
    except Exception as e:
        error_msg = f"Unexpected error ending call {call_uuid}: {str(e)}"
        print(error_msg)
        
        return {
            "status": "error",
            "message": error_msg,
            "error_details": str(e)
        }

def reschedule_interview(screening_id: str, preferred_date: str, preferred_time: str, reason: str = None):
    try:
        try:
            from dateutil.parser import parse
            from dateutil.relativedelta import relativedelta
            
            parsed_datetime = parse(f"{preferred_date} {preferred_time}")
            
            ist_datetime = parsed_datetime.astimezone(timezone(timedelta(hours=5, minutes=30)))
            
            max_future_date = datetime.now(ist_datetime.tzinfo) + relativedelta(months=3)
            
            if ist_datetime < datetime.now(ist_datetime.tzinfo):
                return {
                    "status": "error",
                    "message": "Interview cannot be scheduled in the past."
                }
            
            if ist_datetime > max_future_date:
                return {
                    "status": "error",
                    "message": "Interview cannot be scheduled more than 3 months in the future."
                }
            
            rescheduled_datetime = ist_datetime.isoformat()
            print(rescheduled_datetime)
            
        except ValueError as e:
            return {
                "status": "error",
                "message": f"Invalid date or time format: {str(e)}. Please use a clear date and time."
            }

        screenings.update_one(
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

        print("reschedule completed")
        
        return {'status':"success", "message": "rescheduled call successfully!"}

    except Exception as e:
        error_msg = f"Error rescheduling interview: {str(e)}"
        print(error_msg)
        
        import traceback
        traceback.print_exc()
        
        return {
            "status": "error",
            "message": error_msg,
            "error_details": str(e)
        }

async def score_candidate(transcript, screening_id, skills):
    if transcript:
        messages = [
            {'role': 'system', 'content': """You are a helpful Interview Reviewer. You are reviewing a candidate for a job role.

            Using the transcript of an interview conversation with a candidate, generate a JSON object with the following keys and structure. The JSON must be returned on 1 SINGLE LINE (no newline characters) and contain only the JSON object (no extra explanation): 
            
            {"overAllSummary":"a 200-300 words summary","shortSummary":"a 100-150 words summary","strength":["strength 1 in string","strength 2 in string"],"weakness":["weakness 1 in string","weakness 2 in string"],"fitForTheRole":"Add the scoring criteria here to decide","intentWiseDistribution":[{{ skills_distribution }}],"assessmentScore":"Average of all the scores inside the intentWiseDistribution","communicationDistribution":[{{ communication_distribution }}],"communicationScore":"Average of all the scores inside the communicationDistribution","communicationSummary":"a 200-300 words summary"}
            """},
            {'role': 'user', 'content': f'''transcript: {transcript}'''}
        ]
        
        skills_distribution = ','.join([
            f'{{"intentName":"{skill}","score":0,"summary":"a 100 words short summary"}}' for skill in skills
        ])
        
        communication_keys = ["Clarity", "Relevance", "Vocabulary", "Response Time", "Engagement"]
        communication_distribution = ','.join([
            f'{{"intentName":"{key}","score":0,"summary":"a 100 words short summary"}}' for key in communication_keys
        ])
        
        messages[0]['content'] = messages[0]['content'].replace('{{ skills_distribution }}', skills_distribution)
        messages[0]['content'] = messages[0]['content'].replace('{{ communication_distribution }}', communication_distribution)
        
        try:
            chat = openai_client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                temperature=0.5,
                top_p=1,
                n=1,
                stop=None
            )
            
            response = json.loads(chat.choices[0].message.content)
            if response:
                screenings.update_one(
                    {"_id": bson.ObjectId(screening_id)},
                    {"$set": {"screeningAssessmentScore": response}}
                )
                print(f"Scores saved for screening ID: {screening_id}")
        except json.JSONDecodeError as e:
            print(f"JSON decoding error: {e}")
        except bson.errors.InvalidBSON as e:
            print(f"Invalid screening ID: {e}")
        except Exception as e:
            print(f"An unexpected error occurred: {e}")

if __name__ == "__main__":
    app.run(port=3010)