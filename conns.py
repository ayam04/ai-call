import json
import certifi
import pymongo
import openai

with open("config.json", "r") as f:
    config = json.load(f)

mongo_client = pymongo.MongoClient(config["mongodb"]["url"], tlsCAFile=certifi.where())
db_name = mongo_client[config["mongodb"]["dbName"]]

openai_client = openai.OpenAI(api_key=config["OPENAI_API_KEY"])