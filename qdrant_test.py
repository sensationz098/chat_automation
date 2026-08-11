from qdrant_client import QdrantClient
from dotenv import load_dotenv
import os
load_dotenv()
# Replace with your credentials
QDRANT_URL = os.getenv("QDRANT_URL")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")

client = QdrantClient(
    url=QDRANT_URL,
    api_key=QDRANT_API_KEY,
)

try:
    collections = client.get_collections()
    print("✅ Successfully connected to Qdrant!")
    print("\nCollections:")
    print(collections)
except Exception as e:
    print("❌ Connection Failed")
    print(e)


# from qdrant_client import QdrantClient
# from dotenv import load_dotenv
# import os

# load_dotenv()

# client = QdrantClient(
#     url=os.getenv("QDRANT_URL"),
#     api_key=os.getenv("QDRANT_API_KEY"),
# )

# collections = client.get_collections()

# print("Collections:")
# for collection in collections.collections:
#     print(f"- {collection.name}")

# info = client.get_collection("knowledge_base")

# print(type(info))
# print(info)
# print(info.model_dump())   # Pydantic v2

