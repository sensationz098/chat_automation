import os
import time

from dotenv import load_dotenv
from qdrant_client import QdrantClient
from langchain_openai import OpenAIEmbeddings
from langchain_qdrant import QdrantVectorStore

load_dotenv()

# -----------------------------
# Qdrant
# -----------------------------
client = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY"),
    timeout=10,
)

embeddings = OpenAIEmbeddings(
    model="text-embedding-3-small",
    api_key=os.getenv("OPENAI_API_KEY"),
)

# -----------------------------
# Vector store
# -----------------------------
db = QdrantVectorStore(
    client=client,
    collection_name="knowledge_base",
    embedding=embeddings,
)


queries = [
    "What are the fees for yoga?",
    "What are the yoga class timings?",
    "Is yoga available online?",
    "What is the duration of the course?",
    "Who are the yoga teachers?",
    "WHat is the age person can join"
]


for query in queries:

    print("\n" + "=" * 60)
    print("QUERY:", query)

    # -----------------------------
    # Embedding + Qdrant retrieval
    # -----------------------------
    start = time.perf_counter()

    docs = db.similarity_search(
        query,
        k=3
    )

    retrieval_time = time.perf_counter() - start

    print(f"Retrieval time: {retrieval_time:.3f}s")
    print(f"Documents returned: {len(docs)}")

    for i, doc in enumerate(docs):

        print(f"\n--- Document {i + 1} ---")

        print("Content:")
        print(doc.page_content[:300])

        print("Metadata:")
        print(doc.metadata)