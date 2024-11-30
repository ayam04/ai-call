import asyncio
import base64
import json
import sys
import webrtcvad
import websockets
from deepgram import Deepgram
import io
import traceback
import numpy as np
import wave
from openai import OpenAI
import os
from datetime import datetime

from gtts import gTTS

with open("config.json", 'r') as f:
    CONFIG = json.load(f)

dg_client = Deepgram(CONFIG['deepgram_api_key'])
openai_client = OpenAI(api_key=CONFIG['openai_api_key'])

def generate_interview_questions():
    messages = [
        {
            "role": "system", 
            "content": "You are an expert technical interviewer for data science positions. Generate 2 challenging and thoughtful interview questions that test both technical knowledge and problem-solving skills in data science."
        },
        {
            "role": "user", 
            "content": "Please generate 2 in-depth data science interview questions that assess a candidate's understanding of advanced concepts, analytical thinking, and practical application of data science principles."
        }
    ]
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=300,
            temperature=0.7
        )
        
        questions_text = response.choices[0].message.content.strip()
        questions = [q.strip() for q in questions_text.split('\n') if q.strip()]
        
        return questions[:2] if len(questions) >= 2 else [
            "Can you explain a complex machine learning concept you've worked with?",
            "Describe a challenging data preprocessing problem you've solved."
        ]
    
    except Exception as e:
        print(f"Error generating questions: {e}")
        return [
            "Can you explain a complex machine learning concept you've worked with?",
            "Describe a challenging data preprocessing problem you've solved."
        ]

class InterviewState:
    def __init__(self):
        self.stage = 'greeting'
        self.questions = generate_interview_questions()
        self.current_question_index = -1
        self.answers = []
        self.interview_started = False
        self.interview_completed = False

interview_state = InterviewState()

async def transcribe_audio(audio_chunk, channels=1, sample_width=2, frame_rate=8000):
    try:
        audio_data = np.frombuffer(audio_chunk, dtype=np.int16)
        wav_io = io.BytesIO()

        with wave.open(wav_io, 'wb') as wav_file:
            wav_file.setnchannels(channels)
            wav_file.setsampwidth(sample_width)
            wav_file.setframerate(frame_rate)
            wav_file.writeframes(audio_data.tobytes())

        wav_io.seek(0)

        response = await dg_client.transcription.prerecorded({
            'buffer': wav_io,
            'mimetype': 'audio/wav'
        }, {
            'punctuate': True
        })

        transcription = response['results']['channels'][0]['alternatives'][0]['transcript']

        if transcription != '':
            print("Transcription: ", transcription)

        return transcription
    except Exception as e:
        print("An error occurred during transcription:")
        traceback.print_exc()

def save_interview_results(questions, answers):
    os.makedirs('interviews', exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f'interviews/interview_{timestamp}.json'
    
    with open(filename, 'w') as f:
        json.dump({
            'timestamp': timestamp,
            'questions': questions,
            'answers': answers
        }, f, indent=4)
    
    print(f"Interview results saved to {filename}")

async def text_to_speech_file(text: str, plivo_ws):
    tts = gTTS(text=text, lang='en', slow=False)
    
    tts_io = io.BytesIO()
    tts.write_to_fp(tts_io)
    tts_io.seek(0)
    audio_data = tts_io.read()
    encode = base64.b64encode(audio_data).decode('utf-8')

    await plivo_ws.send(json.dumps({
        "event": "playAudio",
        "media": {
            "contentType": "audio/mp3",
            "payload": encode
        }
    }))

async def generate_response(input_text, plivo_ws):
    global interview_state

    input_text = input_text.lower().strip()

    if interview_state.stage == 'greeting':
        if 'yes' in input_text or 'start' in input_text:
            interview_state.interview_started = True
            interview_state.stage = 'question'
            await text_to_speech_file("Great! Let's start the data science interview. ", plivo_ws)
            await ask_next_question(plivo_ws)
        else:
            await text_to_speech_file("I'm sorry, but this is an interview call. Would you like to proceed with the interview?", plivo_ws)

    elif interview_state.stage == 'question':
        if interview_state.current_question_index >= 0:
            interview_state.answers.append(input_text)
        
        await ask_next_question(plivo_ws)

async def ask_next_question(plivo_ws):
    global interview_state

    interview_state.current_question_index += 1

    if interview_state.current_question_index < len(interview_state.questions):
        current_question = interview_state.questions[interview_state.current_question_index]
        await text_to_speech_file(current_question, plivo_ws)
        interview_state.stage = 'question'
    else:
        interview_state.interview_completed = True
        await text_to_speech_file("Thank you for completing the data science interview. Your responses have been recorded.", plivo_ws)
        save_interview_results(interview_state.questions, interview_state.answers)

async def plivo_handler(plivo_ws):
    async def plivo_receiver(plivo_ws, sample_rate=8000, silence_threshold=0.5):
        print('Plivo receiver started')

        vad = webrtcvad.Vad(1)

        inbuffer = bytearray(b'')
        silence_start = 0
        chunk = None

        await text_to_speech_file("Hello! Would you like to proceed with the data science interview?", plivo_ws)

        try:
            async for message in plivo_ws:
                try:
                    data = json.loads(message)

                    if data['event'] == 'media':
                        media = data['media']
                        chunk = base64.b64decode(media['payload'])
                        inbuffer.extend(chunk)

                    if data['event'] == 'stop':
                        break

                    if chunk is None:
                        continue

                    is_speech = vad.is_speech(chunk, sample_rate)

                    if not is_speech:
                        silence_start += 0.2
                        if silence_start >= silence_threshold:
                            if len(inbuffer) > 2048:
                                transcription = await transcribe_audio(inbuffer)
                                if transcription != '':
                                    await generate_response(transcription, plivo_ws)
                            inbuffer = bytearray(b'')
                            silence_start = 0
                    else:
                        silence_start = 0
                except Exception as e:
                    print(f"Error processing message: {e}")
                    traceback.print_exc()
        except websockets.exceptions.ConnectionClosedError:
            print(f"Websocket connection closed")
        except Exception as e:
            print(f"Error processing message: {e}")
            traceback.print_exc()

    await plivo_receiver(plivo_ws)

async def router(websocket, path):
    if path == '/stream':
        print('Plivo connection incoming')
        await plivo_handler(websocket)

def main():
    print("Generated Interview Questions:")
    for q in interview_state.questions:
        print(f"- {q}")
    
    server = websockets.serve(router, 'localhost', 5000)

    asyncio.get_event_loop().run_until_complete(server)
    asyncio.get_event_loop().run_forever()

if __name__ == '__main__':
    sys.exit(main() or 0)