from langchain_community.document_loaders import PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_openai import OpenAIEmbeddings
from langchain_community.vectorstores import FAISS

import os
from dotenv import load_dotenv


load_dotenv()


loader = PyPDFLoader(
    "D:\\whatsapp automate\\Sensationz Medias.pdf"
)

documents = loader.load()


splitter = RecursiveCharacterTextSplitter(
    chunk_size=400,
    chunk_overlap=100
)


chunks = splitter.split_documents(documents)


# embeddings = GoogleGenerativeAIEmbeddings(
#     model="models/gemini-embedding-2",
#     google_api_key=os.getenv("GEMINI_API_KEY")
# )


embeddings = OpenAIEmbeddings(
    model="text-embedding-3-small",   # Lowest-cost recommended OpenAI embedding model
    api_key=os.getenv("OPENAI_API_KEY")
)


vector_db = FAISS.from_documents(
    chunks,
    embeddings
)


vector_db.save_local(
    "faiss_indexx"
)


print("FAISS index created")