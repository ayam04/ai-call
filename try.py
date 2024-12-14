import ngrok
from dotenv import load_dotenv
load_dotenv()
import os

# Establish connectivity
listener = ngrok.forward("localhost:5000", authtoken=os.getenv("NGROK_AUTH"))

# Output ngrok url to console
print(f"Ingress established at {listener.url()}")