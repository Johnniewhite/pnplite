from __future__ import annotations

from typing import Optional

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase
from fastapi import FastAPI

from app.config.settings import Settings


class Mongo:
    client: Optional[AsyncIOMotorClient] = None
    db: Optional[AsyncIOMotorDatabase] = None


mongo = Mongo()


async def connect_to_mongo(app: FastAPI, settings: Settings):
    mongo.client = AsyncIOMotorClient(settings.mongo_uri)
    mongo.db = mongo.client.get_default_database()
    app.state.mongo = mongo


async def close_mongo_connection(app: FastAPI):
    if mongo.client:
        mongo.client.close()
