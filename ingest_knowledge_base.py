"""
ingest_knowledge_base.py — Deterministic Section-Based Qdrant Ingestion.
Splits knowledge_base_clean.txt by logical sections so no teacher profile,
fee table, or schedule is ever broken across chunks.
"""

import os
import re
from dotenv import load_dotenv
from qdrant_client import QdrantClient
from qdrant_client.http import models
from langchain_openai import OpenAIEmbeddings
from langchain_core.documents import Document
from langchain_qdrant import QdrantVectorStore

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
QDRANT_URL = os.getenv("QDRANT_URL")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "knowledge_base")

if not OPENAI_API_KEY or not QDRANT_URL or not QDRANT_API_KEY:
    raise ValueError("Missing environment variables: OPENAI_API_KEY, QDRANT_URL, or QDRANT_API_KEY")

KB_PATH = "knowledge_base_clean.txt"
if not os.path.exists(KB_PATH):
    raise FileNotFoundError(f"Knowledge base file not found: {KB_PATH}")

print(f"Reading {KB_PATH}...")
with open(KB_PATH, "r", encoding="utf-8") as f:
    raw_text = f.read()

# Split by section headers: --- SECTION: ... ---
sections = re.split(r"(?=--- SECTION: )", raw_text)
docs = []

for sec in sections:
    sec_clean = sec.strip()
    if not sec_clean:
        continue
    
    # Extract section name if present
    match = re.search(r"--- SECTION: (.*?) ---", sec_clean)
    section_name = match.group(1) if match else "GENERAL"
    
    # If the section is very large (e.g. FAQ with 20 questions), we can split into logical sub-blocks
    if len(sec_clean) > 2000 and "FREQUENTLY ASKED QUESTIONS" in section_name:
        qa_pairs = sec_clean.split("\n\nQ: ")
        for i, qa in enumerate(qa_pairs):
            prefix = "" if qa.startswith("---") or qa.startswith("Q: ") else "Q: "
            docs.append(Document(
                page_content=f"--- SECTION: FAQ ---\n{prefix}{qa.strip()}",
                metadata={"section": "FAQ", "sub_idx": i}
            ))
    else:
        docs.append(Document(
            page_content=sec_clean,
            metadata={"section": section_name}
        ))

print(f"Created {len(docs)} clean section-based documents:")
for i, d in enumerate(docs):
    first_line = d.page_content.splitlines()[0] if d.page_content.splitlines() else ""
    print(f"  [{i+1}] {d.metadata.get('section')}: {first_line[:60]} ({len(d.page_content)} chars)")

print("\nInitializing OpenAI Embeddings (text-embedding-3-large)...")
embeddings = OpenAIEmbeddings(
    model="text-embedding-3-large",
    api_key=OPENAI_API_KEY,
)

print(f"Connecting to Qdrant at {QDRANT_URL}...")
client = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY, timeout=120)

# Delete existing collection to purge old fragmented chunks
collections_response = client.get_collections()
existing_names = [c.name for c in collections_response.collections]

if QDRANT_COLLECTION in existing_names:
    print(f"Deleting old collection '{QDRANT_COLLECTION}' to purge old broken chunks...")
    client.delete_collection(collection_name=QDRANT_COLLECTION)
    print(f"Old collection '{QDRANT_COLLECTION}' deleted.")

print(f"Creating clean collection '{QDRANT_COLLECTION}' (dim=3072, Cosine)...")
client.create_collection(
    collection_name=QDRANT_COLLECTION,
    vectors_config=models.VectorParams(
        size=3072,
        distance=models.Distance.COSINE
    )
)

print(f"Uploading {len(docs)} clean section documents to Qdrant...")
vectorstore = QdrantVectorStore(
    client=client,
    collection_name=QDRANT_COLLECTION,
    embedding=embeddings,
)

vectorstore.add_documents(docs)
print(f"SUCCESS: Successfully indexed {len(docs)} clean sections into Qdrant collection '{QDRANT_COLLECTION}'!")


