import os
from dotenv import load_dotenv
from langchain_community.vectorstores import FAISS
from langchain_google_genai import (
    GoogleGenerativeAIEmbeddings,
    ChatGoogleGenerativeAI
)

from langchain_openrouter import ChatOpenRouter
from langchain_core.messages import SystemMessage, HumanMessage
from langchain.agents import create_agent
load_dotenv()

embeddings = GoogleGenerativeAIEmbeddings(
    model="models/gemini-embedding-001",
    google_api_key=os.getenv("GEMINI_API_KEY")
)

db = None
retriever = None

try:
    db = FAISS.load_local(
        "faiss_index",
        embeddings,
        allow_dangerous_deserialization=True
    )
    retriever = db.as_retriever(search_kwargs={"k": 3})
except Exception as e:
    print(f"[rag.py] Could not load FAISS index, RAG will be disabled: {e}")

llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    google_api_key=os.getenv("GEMINI_API_KEY")
)

# llm = ChatOpenRouter(
#     model="anthropic/claude-sonnet-4.5",
#     temperature=0,
#     max_tokens=1024,
#     max_retries=2,

# )


SYSTEM_PROMPT = """You are a friendly, helpful assistant for a grocery/shopping
business, chatting with customers over WhatsApp. Reply the way a warm,
knowledgeable staff member would — not like a search engine reading out
a database entry.

How to answer:
- Keep it short and conversational, like a real WhatsApp message — a
  couple of sentences, not a formatted report.
- Answer using ONLY the context provided below. If the context doesn't
  have what's needed, say so honestly and naturally (e.g. "I don't see
  that in our current stock, but I can check with the team") — never
  invent products, prices, or details that aren't in the context.
- When it's genuinely useful, add one short, relevant practical note a
  real staff member would mention unprompted — e.g. an allergen warning
  on food items, a storage tip, an expiry/freshness note, or a safety
  note for anything a customer might reasonably need to know before
  buying or using it. Only add this when it's actually relevant and
  useful — don't force it into every reply.
- Match the customer's tone — casual if they're casual, quick if they
  seem in a hurry.
- Never sound robotic or overly formal. Avoid phrases like "Based on
  the provided context" or "According to our database."
"""


def ask_rag(question: str) -> str:
    if retriever is None:
        return (
            "Sorry, my product knowledge base isn't set up yet. "
            "Ask me something else in the meantime!"
        )

    try:
        docs = retriever.invoke(question)
        context = "\n\n".join([doc.page_content for doc in docs])

        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=f"Context:\n{context}\n\nCustomer's message: {question}"),
        ]

        response = llm.invoke(messages)
        return response.content

    except Exception as e:
        error = str(e).lower()

        if "quota" in error or "429" in error or "resource_exhausted" in error:
            return (
                "Sorry, the AI service has reached its daily usage limit. "
                "Please try again later."
            )

        print(f"[rag.py] ask_rag error: {e}")
        return "Sorry, I'm unable to answer your question right now."


def stream_rag(question: str):
    if retriever is None:
        yield (
            "Sorry, my product knowledge base isn't set up yet. "
            "Ask me something else in the meantime!"
        )
        return

    try:
        docs = retriever.invoke(question)
        context = "\n\n".join([doc.page_content for doc in docs])

        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=f"Context:\n{context}\n\nCustomer's message: {question}"),
        ]

        buffer = ""
        for chunk in llm.stream(messages):
            piece = chunk.content or ""
            if not piece:
                continue
            buffer += piece

            while True:
                cut = None
                for stop in [". ", "! ", "? ", "\n"]:
                    idx = buffer.find(stop)
                    if idx != -1 and (cut is None or idx < cut):
                        cut = idx + len(stop)
                if cut and cut < len(buffer) and len(buffer[:cut].strip()) >= 15:
                    yield buffer[:cut].strip()
                    buffer = buffer[cut:]
                else:
                    break

        if buffer.strip():
            yield buffer.strip()

    except Exception as e:
        error = str(e).lower()

        if "quota" in error or "429" in error or "resource_exhausted" in error:
            yield (
                "Sorry, the AI service has reached its daily usage limit. "
                "Please try again later."
            )
            return

        print(f"[rag.py] stream_rag error: {e}")
        yield "Sorry, I'm unable to answer your question right now."