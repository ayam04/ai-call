import json
import certifi
from pymongo import MongoClient

with open("config.json", "r") as f:
    config = json.load(f)

# MongoDB connection
mongo_client = MongoClient(config["mongodb"]["url"], tlsCAFile=certifi.where())
db_name = mongo_client[config["mongodb"]["dbName"]]