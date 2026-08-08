from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings
from langchain_qdrant import QdrantVectorStore
import os
from qdrant_client import QdrantClient

from dotenv import load_dotenv

# ----------------------------------------
# Load Environment Variables
# ----------------------------------------
load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
QDRANT_URL = os.getenv("QDRANT_URL")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY")
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "knowledge_base")

# ----------------------------------------
# Validate Environment Variables
# ----------------------------------------
if not OPENAI_API_KEY:
    raise ValueError("❌ OPENAI_API_KEY not found.")

if not QDRANT_URL:
    raise ValueError("❌ QDRANT_URL not found.")

if not QDRANT_API_KEY:
    raise ValueError("❌ QDRANT_API_KEY not found.")

# ----------------------------------------
# PDF Path
# ----------------------------------------
PDF_PATH = "D:\\whatsapp automate\\Sensationz Medias.pdf"   # Example: "data/yoga.pdf"

if not os.path.exists(PDF_PATH):
    raise FileNotFoundError(f"❌ PDF not found: {PDF_PATH}")

print("📄 Loading PDF...")

loader = PyPDFLoader(PDF_PATH)
documents = loader.load()

print(f"✅ Loaded {len(documents)} pages")

# ----------------------------------------
# Split into Chunks
# ----------------------------------------
splitter = RecursiveCharacterTextSplitter(
    chunk_size=400,
    chunk_overlap=100,
)

chunks = splitter.split_documents(documents)

print(f"✅ Created {len(chunks)} chunks")

# ----------------------------------------
# OpenAI Embeddings
# ----------------------------------------
print("🔹 Initializing OpenAI Embeddings...")

embeddings = OpenAIEmbeddings(
    model="text-embedding-3-small",
    api_key=OPENAI_API_KEY,
)

# ----------------------------------------
# Upload to Qdrant
# ----------------------------------------
print("⬆ Uploading to Qdrant...")

QdrantVectorStore.from_documents(
    documents=chunks,
    embedding=embeddings,
    url=QDRANT_URL,
    api_key=QDRANT_API_KEY,
    collection_name=QDRANT_COLLECTION,
)

print("\n🎉 Upload completed successfully!")
print(f"Collection Name: {QDRANT_COLLECTION}")
print(f"Uploaded Chunks: {len(chunks)}")