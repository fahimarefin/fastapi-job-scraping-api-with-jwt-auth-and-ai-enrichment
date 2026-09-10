# app/core/database.py
import logging
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorDatabase, AsyncIOMotorCollection
from pymongo import ASCENDING
from app.core.config import get_settings

logger = logging.getLogger(__name__)

class MongoDB:
    client: AsyncIOMotorClient | None = None
    db: AsyncIOMotorDatabase | None = None

db_instance = MongoDB()


async def connect_to_mongo() -> None:
    """Initialize the MongoDB client connection and create necessary indexes."""
    settings = get_settings()
    logger.info("Connecting to MongoDB at: %s", settings.mongodb_url.split("@")[-1])  # log host without credentials
    try:
        db_instance.client = AsyncIOMotorClient(
            settings.mongodb_url,
            serverSelectionTimeoutMS=5000,
        )
        db_instance.db = db_instance.client[settings.mongodb_db_name]
        
        # Test connection
        await db_instance.client.admin.command("ping")
        logger.info("Successfully connected to MongoDB database: %s", settings.mongodb_db_name)
        
        # Ensure unique indexes on user collection
        users_col = db_instance.db["users"]
        await users_col.create_index([("email", ASCENDING)], unique=True)
        await users_col.create_index([("username", ASCENDING)], unique=True)
        logger.info("MongoDB indexes verified for 'users' collection")
    except Exception as exc:
        logger.error("Failed to connect to MongoDB: %s", exc)
        # We do not crash immediately so the app can still serve non-DB routes if needed,
        # but DB operations will fail with a clear message.


async def close_mongo_connection() -> None:
    """Close the MongoDB connection pool."""
    if db_instance.client is not None:
        logger.info("Closing MongoDB connection pool...")
        db_instance.client.close()
        db_instance.client = None
        db_instance.db = None
        logger.info("MongoDB connection closed.")


def get_database() -> AsyncIOMotorDatabase:
    """Dependency / helper to get database instance."""
    if db_instance.db is None:
        settings = get_settings()
        if db_instance.client is None:
            db_instance.client = AsyncIOMotorClient(settings.mongodb_url)
        db_instance.db = db_instance.client[settings.mongodb_db_name]
    return db_instance.db


def get_user_collection() -> AsyncIOMotorCollection:
    """Helper to get 'users' collection."""
    db = get_database()
    return db["users"]
