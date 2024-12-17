import plivo
from quart import Quart, websocket, Response, request
import asyncio
import websockets
import json
import base64
from datetime import datetime
from conns import db_name

phoneScreening = db_name['phoneScreening']

with open('config.json') as f:
    config = json.load(f)

DEEPGRAM_API_KEY = config['DEEPGRAM_API_KEY']
AUTH_ID = config['PLIVO_AUTH_ID']
AUTH_TOKEN = config['PLIVO_AUTH_TOKEN']

with open("prompt.txt", "r") as file:
    prompt = file.read()

app = Quart(__name__)
stream_id = ""

@app.route("/webhook", methods=["GET", "POST"])
def home():
    xml_data = f'''<?xml version="1.0" encoding="UTF-8"?>
    <Response>
        <Record recordSession="true" redirect="false" />
        <Stream streamTimeout="86400" keepCallAlive="true" bidirectional="true" contentType="audio/x-mulaw;rate=8000" audioTrack="inbound" >
            ws://{request.host}/media-stream
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
                    'speaker': response['role'],
                    'text': response['content'],
                    'timestamp': datetime.now().isoformat()
                })
            
            if response.get('type') == 'UserStartedSpeaking':
                if not stream_id:
                    print("Warning: stream_id not set")
            
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
                "instructions": (prompt.format(**{"name":name, "role": role, "jd": jd, "info": info, "company": company, "questions": questions})).strip()
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
                phoneScreening.insert_one({
                        'candidate_name': app.config.get('candidate_name'),
                        'role': app.config.get('role'),
                        'company': app.config.get('company'),
                        'screeningId': app.config.get('screeningId'),
                        'transcript': app.config['transcript']
                    })
                print(f"Transcript saved to db as {app.config['transcript_filename']}")
            else:
                print("No transcript to save.")
        except Exception as e:
            print(f"Error saving transcript: {e}")
    else:
        print("No transcript filename configured.")

client = plivo.RestClient(auth_id= AUTH_ID, auth_token= AUTH_TOKEN)