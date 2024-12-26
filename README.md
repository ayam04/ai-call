# Phone Interview Assistant

An automated phone interview system using Plivo for telephony, Deepgram for speech recognition and AI agent capabilities, and MongoDB for data storage.

## Local Testing

1. run: python server.py
2. open any api client like rapid API client
3. paste the /make-a-call endpoint in POST request and pass this body in the same:

{
  "phone": 9999999999,
  "name": "Ayam",
  "id": "66796c746e6b34001c4480df"
}

## Deployment Testing

1. paste this endpoint: "https://phoneagent.hyrgpt.com/make-a-call" in postman with POST request
2. use the same sample data as in local testing for the deployment code.

## Important Points

1. https://phoneagent.hyrgpt.com is our dev public deployment URL
2. All API Keys are available in the config file
3. DO NOT CHANGE THE PORT ON WHICH THE APP IS RUNNING

## System Architecture

### Components
- Quart Web Server: Async web framework handling HTTP and WebSocket connections
- Plivo Integration: Handles outbound calls and audio streaming
- Deepgram AI: Provides speech-to-text and AI agent capabilities
- MongoDB: Stores screening data, jobs, questions, and interview transcripts

### Database Collections
- phoneScreening: Stores interview transcripts
- screenings: Interview session data
- jobs: Job posting information
- jobquestions: Interview questions for specific jobs
- companies: Company information

### Main Features

1. Automated Calling
   - Makes outbound calls to candidates using Plivo
   - Uses configured phone number from config.json

2. Real-time Audio Processing
   - Bidirectional audio streaming
   - 8kHz mu-law audio encoding
   - Buffer management for audio chunks

3. Interview Flow
   - AI agent introduces itself as company representative
   - Conducts interview based on job description and questions
   - Records and transcribes entire conversation
   - Saves interview transcript to database

4. Configuration Requirements
   - config.json file containing:
     - PLIVO_NUMBER
     - DEEPGRAM_API_KEY
     - PLIVO_AUTH_ID
     - PLIVO_AUTH_TOKEN
   - prompt.txt file for AI agent instructions

### API Endpoints

1. POST /make-a-call
   - Initiates phone interview
   - Required payload: phone number, candidate name, screening ID
   - Fetches relevant job and company data
   - Starts automated interview process

2. GET/POST /webhook
   - Handles Plivo call events
   - Sets up audio recording and streaming

3. WebSocket /media-stream
   - Manages real-time audio communication
   - Handles bidirectional audio between Plivo and Deepgram

## Error Handling
- WebSocket connection management
- Audio stream buffering
- Transcript saving
- Comprehensive error logging

## Requirements
- Python 3.9+
- MongoDB
- Plivo account
- Deepgram account
- Required Python packages: plivo, quart, websockets, pydantic, quart-cors