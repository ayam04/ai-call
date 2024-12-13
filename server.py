import plivo
from quart import Quart, websocket, Response, request
import asyncio
import websockets
import json
import base64
from dotenv import load_dotenv
import os

load_dotenv()

DEEPGRAM_API_KEY = os.getenv('DEEPGRAM_API_KEY')

with open("prompt.txt", "r") as file: 
    prompt = file.read()

app = Quart(__name__)
stream_id = ""

@app.route("/webhook", methods=["GET", "POST"])
def home():
    xml_data = f'''<?xml version="1.0" encoding="UTF-8"?>
    <Response>
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
            
            async for message in deepgram_ws:
                await receive_from_deepgram(message, plivo_ws)
            
            await receive_task
    
    except asyncio.CancelledError:
        print('client disconnected')
    except websockets.ConnectionClosed:
        print("Connection closed by OpenAI server")
    except Exception as e:
        print(f"Error during OpenAI's websocket communication: {e}")

            
async def receive_from_plivo(plivo_ws, deepgram_ws):
    print('receiving from plivo')
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
                print('Plivo Audio stream has started')
                stream_id = data['start']['streamId']
                print('stream id: ', stream_id)
            
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
    try:
        if type(message) == str:
            response = json.loads(message)
            print('response received from Deepgram WSS: ', response)
            if response['type'] == 'SettingsApplied':
                print('Settings successfully applied')
            if response['type'] == 'Welcome':
                print('Received welcome message')
            elif response['type'] == 'UserStartedSpeaking':
                clear_message = {
                    "event": "clearAudio",
                    "stream_id": stream_id
                }
                await plivo_ws.send(json.dumps(clear_message))
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
                "instructions": (prompt.format(**{"name":name}).strip())
            },
            "speak": {
                "model": "aura-hera-en" 
            }
        },
        "context": {
            "messages": [
            {
                "content": f"Hello, this is Reva from Hire GPT. I have called you for a quick telephonic interview. Am I talking to {name} right now?",
                "role": "assistant"
            }
            ],
            "replay": True
        }
    }
    await deepgram_ws.send(json.dumps(session_update))

def function_call_output(arg, item_id, call_id):
    sum = int(arg['num1']) + int(arg['num2'])
    conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "id": item_id,
            "type": "function_call_output",
            "call_id": call_id,
            "output": str(sum)
        }
    }
    return conversation_item

client = plivo.RestClient(auth_id=os.getenv('PLIVO_AUTH_ID'), auth_token=os.getenv('PLIVO_AUTH_TOKEN'))