import plivo
from quart import Quart, websocket, Response, request
import asyncio
import websockets
import json
import base64
from datetime import datetime
from conns import db_name
from pydantic import BaseModel
import bson
from quart_cors import cors
import requests

with open('config.json') as f:
    config = json.load(f)

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

client = plivo.RestClient(auth_id= AUTH_ID, auth_token= AUTH_TOKEN)

active_calls = {}

with open("prompt.txt", "r") as file:
    prompt = file.read()

app = Quart(__name__)
app = cors(app, allow_origin="*", allow_headers=["*"], allow_methods=["*"])

@app.before_serving
async def setup_cors():
    def _add_cors_headers(response):
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Headers'] = '*'
        response.headers['Access-Control-Allow-Methods'] = '*'
        return response
    
    app.after_request(_add_cors_headers)

app.config['PROVIDE_AUTOMATIC_OPTIONS'] = True
app.config['transcript'] = []
app.config['transcript_filename'] = None

@app.post('/make-a-call')
async def make_outbound_call(): 
    request_data = await request.get_json()
    req = data(**request_data)

    screenData = screenings.find_one({"_id": bson.ObjectId(str(req.id))})

    jobData = jobs.find_one({"_id": bson.ObjectId(str(screenData['jobId']))})
    role = jobData['title']
    jd = jobData['jobDescription']

    questions = []
    questionsData = jobquestions.find({'jobId':bson.ObjectId(str(screenData['jobId']))})
    for i in questionsData:
        questions.append(i['question']) 

    companyData = companies.find_one({"_id":bson.ObjectId(str(jobData['companyId']))}) 
    company = companyData['name']
    companyAbout = companyData['aboutUs']

    app.config['candidate_name'] = req.name
    app.config['role'] = role
    app.config['jd'] = jd
    app.config['screeningId'] = req.id
    app.config["company"] = company
    app.config['additional_info'] = f"ABout the company: {companyAbout}"
    app.config["questions"] = questions

    app.config['PROVIDE_AUTOMATIC_OPTIONS'] = True 
    app.config['transcript'] = []
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    app.config['transcript_filename'] = f"transcript_{req.name}_{company}_{timestamp}.json"

    call_response = client.calls.create(
        from_= NUMBER,
        to_="91"+str(req.phone)[-10:],
        answer_url="https://2de2-115-245-68-163.ngrok-free.app/webhook",
        answer_method='GET',
    )

    app.config['current_call_uuid'] = call_response.request_uuid
    print(f"Call initiated with UUID: {app.config['current_call_uuid']}")
    
    active_calls[app.config['current_call_uuid']] = {
        'stream_id': None,
        'candidate_name': req.name,
        'company': app.config["company"]
    }
    
    return {"status": "Call initiated", "call_uuid": app.config['current_call_uuid']}

@app.route("/webhook", methods=["GET", "POST"])
def home():
    xml_data = f'''<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Record recordSession="true" redirect="false" />
        <Stream streamTimeout="86400" keepCallAlive="true" bidirectional="true" contentType="audio/x-mulaw;rate=8000" audioTrack="inbound" >
            wss://{request.host}/media-stream
        </Stream>
    </Response>
    '''
    return Response(xml_data, mimetype='application/xml')


@app.websocket('/media-stream')
async def handle_message():
    print('client connected')
    plivo_ws = websocket 
    url = "wss://agent.deepgram.com/agent"
    headers = {
        "Authorization": f"Token {DEEPGRAM_API_KEY}",
    }

    try: 
        async with websockets.connect(url, extra_headers=headers) as deepgram_ws:
            print('connected to the Deepgram WSS')

            await send_Session_update(deepgram_ws)
            
            receive_task = asyncio.create_task(receive_from_plivo(plivo_ws, deepgram_ws))
            
            try:
                async for message in deepgram_ws:
                    await receive_from_deepgram(message, plivo_ws)
            except Exception as inner_e:
                print(f"Inner websocket error: {inner_e}")
            await receive_task
    
    except asyncio.CancelledError:
        print('User hanged up')
        receive_task.cancel()
    except websockets.ConnectionClosed:
        print("Connection closed by OpenAI server")
        receive_task.cancel()
    except Exception as e:
        print(f"Error during OpenAI's websocket communication: {e}")
        receive_task.cancel()
    finally:
        await save_transcript()

async def receive_from_plivo(plivo_ws, deepgram_ws):
    print('receiving from plivo')
    BUFFER_SIZE = 20 * 160
    inbuffer = bytearray(b"")
    global stream_id
    try:
        while True:
            message = await plivo_ws.receive()
            data = json.loads(message)
            if data['event'] == 'media' and deepgram_ws.open:
                chunk = base64.b64decode(data['media']['payload'])
                inbuffer.extend(chunk)
            elif data['event'] == "start":
                print('Plivo Audio stream has started')
                stream_id = data['start']['streamId']
                print('stream id: ', stream_id)
                current_call_uuid = app.config.get('current_call_uuid')
                if current_call_uuid and current_call_uuid in active_calls:
                    active_calls[current_call_uuid]['stream_id'] = stream_id
            elif data['event'] == "stop":
                print('Plivo Audio stream stopped')
                break
            
            while len(inbuffer) >= BUFFER_SIZE:
                chunk = inbuffer[:BUFFER_SIZE]
                await deepgram_ws.send(chunk)
                inbuffer = inbuffer[BUFFER_SIZE:]

    except websockets.ConnectionClosed:
        print('Connection closed for the plivo audio streaming servers')
        if deepgram_ws.open:
            await deepgram_ws.close()
    except Exception as e:
        print(f"Error during Plivo's websocket communication: {e}")


async def receive_from_deepgram(message, plivo_ws):
    global stream_id
    try:
        if type(message) == str:
            response = json.loads(message)
            print('response received from Deepgram WSS: ', response)
            
            if response.get('type') == 'ConversationText':
                app.config['transcript'].append({
                    'role': response['role'],
                    'message': response['content']
                })
            
            elif response.get('type') == 'UserStartedSpeaking':
                if not stream_id:
                    print("Warning: stream_id not set")
            
            elif response.get('type') == 'FunctionCallRequest':
                if response['function_name'] == 'endCall':
                    current_call_uuid = app.config.get('current_call_uuid')
                    print(f"Received endCall request, current_call_uuid: {current_call_uuid}")
                    # Get the current call's UUID from active_calls
                    if current_call_uuid:
                        result = await end_call(current_call_uuid)
                        functionCallResponse = {
                            "type": "FunctionCallResponse",
                            "function_call_id": response['function_call_id'],
                            "output": "Call ended successfully. Goodbye!" if result["status"] == "success" else f"Failed to end call: {result['message']}"
                        }
                        await plivo_ws.send(json.dumps(functionCallResponse))
                    else:
                        print("No current call UUID available")
            
            if response['type'] == 'AgentStartedSpeaking':
                print(f"Agent speaking. Latencies - TTT: {response.get('ttt_latency', 'N/A')}, Total: {response.get('total_latency', 'N/A')}")
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
        print(f"Error during Deepgram's websocket communication: {e}")
    
    
async def send_Session_update(deepgram_ws):
    name = app.config.get('candidate_name')
    role = app.config.get('role')
    jd = app.config.get('jd')
    info = app.config.get('additional_info')
    company = app.config.get('company')
    questions = app.config.get('questions')

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
            "container": "none"
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
            "instructions": (prompt.format(**{"name":name, "role": role, "jd": jd, "info": info, "company": company, "questions": questions})).strip(),
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
            "content": f"Hello, this is Reva from {company}. I have called you for a job interview. Am I talking to {name}?",
            "role": "assistant"
        }
        ],
        "replay": True
    }
}
    await deepgram_ws.send(json.dumps(session_update))

async def save_transcript():
    if app.config['transcript_filename']:
        try:
            if app.config['transcript']:
                screeningId = app.config.get('screeningId')
                payload = {
                    'screeningConversation': app.config['transcript']
                }

                url = f'https://devnodeapi.hyrgpt.com/v1/screening-status/{screeningId}'
                response = requests.patch(url, json=payload)
                
                if response.status_code == 200:
                    print(f"Transcript saved successfully for screening ID: {screeningId}")
                else:
                    print(f"Error saving transcript. Status code: {response.status_code}")
            else:
                print("No transcript to save.")
        except Exception as e:
            print(f"Error saving transcript: {e}")
    else:
        print("No transcript filename configured.")

async def end_call(call_uuid: str, reason: str = None):
    try:
        # Log the call termination attempt with reason
        print(f"Attempting to end call {call_uuid} - Reason: {reason}")
        
        # Validate call exists
        if not call_uuid:
            return {
                "status": "error",
                "message": "No active call UUID provided",
                "reason": reason
            }
            
        # End the call through Plivo
        response = client.calls.delete(call_uuid=call_uuid)
        
        # Clean up call tracking
        if call_uuid in active_calls:
            # Store any relevant call data before deletion
            call_data = active_calls[call_uuid]
            del active_calls[call_uuid]
            
            # Log call details for analytics
            print(f"Call ended - UUID: {call_uuid}")
            print(f"Call duration: {call_data.get('duration', 'unknown')}")
            print(f"Termination reason: {reason}")
            
        # Reset current call tracking
        app.config['current_call_uuid'] = None
        
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
            "reason": reason,
            "error_details": str(e)
        }
        
    except Exception as e:
        error_msg = f"Unexpected error ending call: {str(e)}"
        print(error_msg)
        return {
            "status": "error",
            "message": error_msg,
            "reason": reason,
            "error_details": str(e)
        }

if __name__ == "__main__":
    app.run(port=3010)