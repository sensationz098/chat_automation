import os
from dotenv import load_dotenv
from langchain_community.vectorstores import FAISS
from langchain_google_genai import (
    GoogleGenerativeAIEmbeddings,
    ChatGoogleGenerativeAI
)
from qdrant_client import QdrantClient
from langchain_qdrant import QdrantVectorStore
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from prompt import SYSTEM_PROMPT
import asyncio
client = QdrantClient(
    url=os.getenv("QDRANT_URL"),
    api_key=os.getenv("QDRANT_API_KEY"),
)

load_dotenv()

# embeddings = GoogleGenerativeAIEmbeddings(
#     model="models/gemini-embedding-2",
#     google_api_key=os.getenv("GEMINI_API_KEY")
# )

embeddings = OpenAIEmbeddings(
    model="text-embedding-3-small",
    api_key=os.getenv("OPENAI_API_KEY")
)

# db = None
# retriever = None

# try:
#     db = FAISS.load_local(
#         "faiss_indexx",
#         embeddings,
#         allow_dangerous_deserialization=True
#     )
#     retriever = db.as_retriever(search_kwargs={"k": 5})
# except Exception as e:
#     print(f"[rag.py] Could not load FAISS index, RAG will be disabled: {e}")


retriever = None

try:
    db = QdrantVectorStore(
        client=client,
        collection_name="knowledge_base",
        embedding=embeddings,
    )

    retriever = db.as_retriever(
        search_kwargs={"k": 5}
    )
except Exception as e:
    print(f"[rag.py] Could not connect to Qdrant, RAG will be disabled: {e}")

llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    google_api_key=os.getenv("GEMINI_API_KEY")
)

llm = ChatOpenAI(
    model="gpt-4.1-nano",
    api_key=os.getenv("OPENAI_API_KEY"),
    temperature=0
)

from prompt import format_system_prompt

SYSTEM_PROMPT = format_system_prompt({"stage": "NEW"})

def _build_history_messages(chat_history: list = None):
    """
    Converts stored history (list of {"role": ..., "content": ...}
    dicts, oldest first) into LangChain message objects the model can
    read as prior conversation turns.
    """
    if not chat_history:
        return []

    messages = []
    # Cap history to recent 10 turns for sharp focus
    recent_turns = chat_history[-10:] if len(chat_history) > 10 else chat_history
    for turn in recent_turns:
        if turn["role"] == "user":
            messages.append(HumanMessage(content=turn["content"]))
        elif turn["role"] == "assistant":
            messages.append(AIMessage(content=turn["content"]))
    return messages


# =============================================================================
# TRANSACTIONAL INPUT & RETRIEVAL QUERY HELPERS
# =============================================================================

def _is_transactional_input(text: str) -> bool:
    """
    Checks if user input is a pure procedural choice/confirmation rather
    than an informational question or video link request.
    Prevents bypassing RAG retrieval when users ask for video links or demo classes.
    """
    txt = text.lower().strip()
    
    # Priority keywords for video/demo requests and informational queries (NEVER skip RAG for these)
    info_keywords = [
        "video", "videos", "demo", "recording", "recordings",
        "sample", "watch", "trial", "link", "youtube", "teacher",
        "syllabus", "fees", "fee", "cost", "price"
    ]
    if any(kw in txt for kw in info_keywords):
        return False  # Force RAG vector search to run
        
    # List of simple procedural trigger words (e.g., timing selection or simple greetings)
    procedural_triggers = [
        "yes", "yeah", "yep", "sure", "ok", "okay", "hi", "hii", "hello", "hey",
        "morning", "evening", "afternoon", "6-7am", "7-8am", "8-9am", "10-11am",
        "12-1pm", "4-5pm", "5-6pm", "6-7pm", "7-8pm", "1 month", "3 months", "6 months",
        "1 year", "installed", "downloaded", "done"
    ]
    # Return True ONLY if text exactly matches a simple procedural trigger word
    return txt in procedural_triggers


def _build_retrieval_query(question: str, chat_history: list = None) -> str:
    """
    Combines recent chat conversation history with the incoming question
    to give the vector retriever full context for semantic search.
    """
    if not chat_history:
        return question

    # Extract last 4 turns of chat history to build context window
    recent = chat_history[-4:]
    context_str = " ".join(turn["content"] for turn in recent)
    return f"{context_str} {question}"


async def ask_rag(question: str, chat_history: list = None, state: dict = None) -> str:
    """
    RAG query function. Retrives relevant Sensationz PDF knowledge chunks
    and passes them alongside session state to OpenAI LLM.
    """
    try:
        # Format custom system prompt with user's active session state
        sys_prompt_content = format_system_prompt(state or {"stage": "NEW"})
        context = ""
        
        # Run vector database search if retriever is loaded and query is not a simple procedural selection
        if retriever is not None and not _is_transactional_input(question):
            retrieval_query = _build_retrieval_query(question, chat_history)
            try:
                docs = await retriever.ainvoke(retrieval_query)
            except Exception:
                docs = await asyncio.to_thread(retriever.invoke, retrieval_query)
            context = "\n\n".join([doc.page_content for doc in docs])

        # Construct message payload for LangChain OpenAI LLM
        messages = [SystemMessage(content=sys_prompt_content)]
        messages.extend(_build_history_messages(chat_history))
        
        user_msg = f"Context:\n{context}\n\nCustomer's message: {question}" if context else f"Customer's message: {question}"
        messages.append(HumanMessage(content=user_msg))

        # Call LLM model asynchronously to generate response
        try:
            response = await llm.ainvoke(messages)
        except Exception:
            response = await asyncio.to_thread(llm.invoke, messages)

        return response.content.strip()

    except Exception as e:
        error = str(e).lower()
        if "quota" in error or "429" in error or "resource_exhausted" in error:
            return "Sorry, the AI service has reached its usage limit. Please try again later."
        print(f"[rag.py] ask_rag error: {e}")
        return "Sorry, I'm unable to process your request right now."


async def ask_rag_async(question: str, chat_history: list = None, state: dict = None) -> str:
    """Non-blocking async RAG query function."""
    return await ask_rag(question, chat_history, state)


def stream_rag(question: str, chat_history: list = None, state: dict = None):
    """
    Streaming RAG query function used by main.py and tasks.py.
    Retrieves vector search knowledge chunks and yields the complete AI reply.
    """
    try:
        # Format custom system prompt with user's active session state
        sys_prompt_content = format_system_prompt(state or {"stage": "NEW"})
        context = ""
        
        # Run vector search if retriever is active and query is an informational question/video request
        if retriever is not None and not _is_transactional_input(question):
            retrieval_query = _build_retrieval_query(question, chat_history)
            docs = retriever.invoke(retrieval_query)
            context = "\n\n".join([doc.page_content for doc in docs])

        # Assemble prompt message sequence
        messages = [SystemMessage(content=sys_prompt_content)]
        messages.extend(_build_history_messages(chat_history))

        user_msg = f"Context:\n{context}\n\nCustomer's message: {question}" if context else f"Customer's message: {question}"
        messages.append(HumanMessage(content=user_msg))

        # Generate complete answer using LLM
        response = llm.invoke(messages)
        answer = response.content.strip()
        
        # Yield the clean full response string
        if answer:
            yield answer

    except Exception as e:
        error = str(e).lower()
        if "quota" in error or "429" in error or "resource_exhausted" in error:
            yield "Sorry, the AI service has reached its daily usage limit. Please try again later."
            return
        print(f"[rag.py] stream_rag error: {e}")
        yield "Sorry, I'm unable to answer your question right now."
