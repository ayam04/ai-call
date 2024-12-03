import asyncio
import base64
import json
import sys
import traceback
import io
import wave

import numpy as np
import webrtcvad
import websockets
import openai

from deepgram import (
    DeepgramClient,
    DeepgramClientOptions,
    SpeakOptions,
    PrerecordedOptions
)

def load_config(file_path='config.json'):
    """Load configuration from a JSON file."""
    try:
        with open(file_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Config file {file_path} not found.")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Error decoding JSON from {file_path}")
        sys.exit(1)

# Load configuration
CONFIG = load_config()

# Initialize clients with error handling
try:
    dg_client = DeepgramClient(CONFIG['deepgram_api_key'])
    dg_config = DeepgramClientOptions(api_key=CONFIG['deepgram_api_key'])
except KeyError as e:
    print(f"Missing configuration key: {e}")
    sys.exit(1)

# Set OpenAI API key
try:
    openai.api_key = CONFIG['openai_api_key']
except KeyError:
    print("OpenAI API key is missing from configuration")
    sys.exit(1)

# Interview system message
SYSTEM_MSG = """You are an AI interviewer for HYRGPT, a platform designed to assist companies in screening and interviewing candidates. Your task is to conduct a structured telephonic interview with each candidate, which will take approximately 5 to 10 minutes to complete.

Here's how you should proceed:

1. Begin the interview by asking:
   "Hello, this is Reva from Hire GPT. Do you have 10 minutes to complete the interview now?"

2. Interview Questions:
   - Please tell me more about your recent work experience.
   - Can you describe your experience with developing mobile applications for both iOS and Android platforms?
   - What are some best practices you follow when integrating APIs into mobile applications?
   - How do you ensure that the mobile applications you develop are user-friendly and meet UI/UX design standards?
   - What tools do you use for testing and optimization?
   - Describe a situation where you faced a significant challenge during a mobile development project.
   - Imagine you are working on a tight deadline for a mobile application launch. How would you prioritize tasks and manage your time effectively to meet the deadline?

3. Interview Guidelines:
   - Maintain a professional tone with warmth
   - Acknowledge previous responses before asking next question
   - If a candidate asks to skip a question, note "skipped by user"
   - If a candidate asks to repeat a question, do so
   - If a candidate wants to end the interview early, submit collected answers
   - Do not provide hints for questions

4. Closing: Thank the candidate and mention follow-up via email."""

# Initialize messages with system context
messages = [{"role": "system", "content": SYSTEM_MSG}]

async def transcribe_audio(audio_chunk):
    try:
        audio_data = np.frombuffer(audio_chunk, dtype=np.uint8)
        wav_buffer = io.BytesIO()
        
        with wave.open(wav_buffer, 'wb') as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(8000)
            wav_file.writeframes(audio_data.tobytes())
        
        wav_buffer.seek(0)
        
        payload = {
            "buffer": wav_buffer.read(),
        }

        options = PrerecordedOptions(
            model="nova-2-conversationalai",
            smart_format=True,
            channels=1,
            sample_rate=8000
        )

        response = dg_client.listen.rest.v("1").transcribe_file(payload, options)
        
        transcription = response.results.channels[0].alternatives[0].transcript
        if transcription != "":
            print("Transcription: ", transcription)
        return transcription

    except Exception as e:
        print("Transcription error:")
        traceback.print_exc()
        return ""

async def generate_response(input_text, plivo_ws):
    try:
        messages.append({"role": "user", "content": input_text})

        response = openai.ChatCompletion.create(
            model="gpt-4o-mini",
            messages=messages
        )

        reply = response['choices'][0]['message']['content']
        messages.append({"role": "assistant", "content": reply})

        print("OpenAI Response: ", reply)

        # Wait for TTS to complete before allowing new input
        await text_to_speech_file(reply, plivo_ws)
        
    except Exception as e:
        print("Response generation error:")
        traceback.print_exc()
        await text_to_speech_file("I'm sorry, could you repeat that?", plivo_ws)

async def text_to_speech_file(text: str, plivo_ws):
    try:
        options = SpeakOptions(
            model="aura-hera-en",
            encoding="mulaw",
            sample_rate=8000
        )

        audio_buffer = io.BytesIO()

        response = await dg_client.speak.asyncrest.v("1").stream_raw(
            {"text": text}, 
            options
        )
        
        async for chunk in response.aiter_bytes():
            audio_buffer.write(chunk)
        
        await response.aclose()
        audio_bytes = audio_buffer.getvalue()
        
        base64_audio = base64.b64encode(audio_bytes).decode('utf-8')
        
        await plivo_ws.send(json.dumps({
            "event": "playAudio",
            "media": {
                "contentType": "audio/x-mulaw",
                "sampleRate": 8000,
                "payload": base64_audio
            }
        }))

    except Exception as e:
        print("Text-to-Speech error:")
        traceback.print_exc()

async def plivo_handler(plivo_ws):
    async def plivo_receiver(plivo_ws, sample_rate=8000, silence_threshold=0.5):
        print('Plivo receiver started')

        # Initialize voice activity detection (VAD) with sensitivity level
        vad = webrtcvad.Vad(1)  # Level 1 is least sensitive

        inbuffer = bytearray(b'')  # Buffer to hold received audio chunks
        silence_start = 0  # Track when silence begins
        chunk = None  # Audio chunk

        try:
            async for message in plivo_ws:
                try:
                    # Decode incoming messages from the WebSocket
                    data = json.loads(message)

                    # If 'media' event, process the audio chunk
                    if data['event'] == 'media':
                        media = data['media']
                        chunk = base64.b64decode(media['payload'])
                        inbuffer.extend(chunk)

                    # If 'stop' event, end receiving process
                    if data['event'] == 'stop':
                        break

                    if chunk is None:
                        continue

                    # Check if the chunk contains speech or silence
                    is_speech = vad.is_speech(chunk, sample_rate)

                    if not is_speech:  # Detected silence
                        silence_start += 0.2  # Increment silence duration (200ms steps)
                        if silence_start >= silence_threshold:  # If silence exceeds threshold
                            if len(inbuffer) > 2048:  # Process buffered audio if large enough
                                transcription = await transcribe_audio(inbuffer)
                                if transcription != '':
                                    await generate_response(transcription, plivo_ws)
                            inbuffer = bytearray(b'')  # Clear buffer after processing
                            silence_start = 0  # Reset silence timer
                    else:
                        silence_start = 0  # Reset if speech is detected
                except Exception as e:
                    print(f"Error processing message: {e}")
                    traceback.print_exc()
        except websockets.exceptions.ConnectionClosedError as e:
            print(f"Websocket connection closed")
        except Exception as e:
            print(f"Error processing message: {e}")
            traceback.print_exc()

    # Start the receiver for WebSocket messages
    await plivo_receiver(plivo_ws)

async def router(websocket, path):
    if path == '/stream':
        print('Plivo connection incoming')
        await plivo_handler(websocket)

def main():
    try:
        server = websockets.serve(router, 'localhost', 5000)
        asyncio.get_event_loop().run_until_complete(server)
        asyncio.get_event_loop().run_forever()
    except Exception as e:
        print(f"Failed to start server: {e}")
        sys.exit(1)

if __name__ == '__main__':
    sys.exit(main() or 0)