import os
from dotenv import load_dotenv
from langchain_community.vectorstores import FAISS
from langchain_google_genai import (
    GoogleGenerativeAIEmbeddings,
    ChatGoogleGenerativeAI
)
from langchain_community.retrievers import BM25Retriever
from langchain_classic.retrievers import EnsembleRetriever
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_openai import OpenAIEmbeddings, ChatOpenAI
from prompt import SYSTEM_PROMPT
load_dotenv()

# embeddings = GoogleGenerativeAIEmbeddings(
#     model="models/gemini-embedding-2",
#     google_api_key=os.getenv("GEMINI_API_KEY")
# )

embeddings = OpenAIEmbeddings(
    model="text-embedding-3-small",
    api_key=os.getenv("OPENAI_API_KEY")
)

db = None
retriever = None

try:
    db = FAISS.load_local(
        "faiss_indexx",
        embeddings,
        allow_dangerous_deserialization=True
    )
    retriever = db.as_retriever(search_kwargs={"k": 5})
except Exception as e:
    print(f"[rag.py] Could not load FAISS index, RAG will be disabled: {e}")

# llm = ChatGoogleGenerativeAI(
#     model="gemini-2.5-flash",
#     google_api_key=os.getenv("GEMINI_API_KEY")
# )

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


def ask_rag(question: str, chat_history: list = None, state: dict = None) -> str:
    """
    Synchronous RAG query function. Retrives relevant Sensationz PDF knowledge chunks
    and passes them alongside session state to OpenAI LLM.
    """
    try:
        # Format custom system prompt with user's active session state
        sys_prompt_content = format_system_prompt(state or {"stage": "NEW"})
        context = ""
        
        # Run vector database search if retriever is loaded and query is not a simple procedural selection
        if retriever is not None and not _is_transactional_input(question):
            retrieval_query = _build_retrieval_query(question, chat_history)
            docs = retriever.invoke(retrieval_query)
            context = "\n\n".join([doc.page_content for doc in docs])

        # Construct message payload for LangChain OpenAI LLM
        messages = [SystemMessage(content=sys_prompt_content)]
        messages.extend(_build_history_messages(chat_history))
        
        user_msg = f"Context:\n{context}\n\nCustomer's message: {question}" if context else f"Customer's message: {question}"
        messages.append(HumanMessage(content=user_msg))

        # Call LLM model to generate response
        response = llm.invoke(messages)
        return response.content

    except Exception as e:
        error = str(e).lower()
        if "quota" in error or "429" in error or "resource_exhausted" in error:
            return "Sorry, the AI service has reached its usage limit. Please try again later."
        print(f"[rag.py] ask_rag error: {e}")
        return "Sorry, I'm unable to process your request right now."


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


# import os
# from dotenv import load_dotenv
# from langchain_community.vectorstores import FAISS
# from langchain_community.retrievers import BM25Retriever
# from langchain_classic.retrievers import EnsembleRetriever
# from langchain_google_genai import (
#     GoogleGenerativeAIEmbeddings,
#     ChatGoogleGenerativeAI
# )
# from prompt import SYSTEM_PROMPT
# from langchain_core.messages import SystemMessage, HumanMessage, AIMessage

# load_dotenv()

# # embeddings = GoogleGenerativeAIEmbeddings(
# #     model="models/gemini-embedding-001",
# #     google_api_key=os.getenv("GEMINI_API_KEY")
# # )

# db = None
# retriever = None

# try:
#     db = FAISS.load_local(
#         "faiss_index",
#         embeddings,
#         allow_dangerous_deserialization=True
#     )
#     vector_retriever = db.as_retriever(search_kwargs={"k": 3})

#     # Pull the original documents back out of the FAISS docstore to
#     # build a BM25 (keyword-based) index alongside the vector one.
#     # No changes needed to your ingestion pipeline for this — FAISS
#     # already keeps the original chunks internally.
#     all_docs = list(db.docstore._dict.values())
#     bm25_retriever = BM25Retriever.from_documents(all_docs)
#     bm25_retriever.k = 3

#     # Hybrid retrieval: combines keyword matching (BM25 — good at
#     # exact terms like product names, codes) with semantic similarity
#     # (vector — good at meaning/paraphrasing). weights control how much
#     # each contributes to the final ranking.
#     retriever = EnsembleRetriever(
#         retrievers=[bm25_retriever, vector_retriever],
#         weights=[0.4, 0.6],
#     )
#     print(f"[rag.py] Hybrid retriever ready — {len(all_docs)} documents indexed for BM25 + vector search.")

# except Exception as e:
#     print(f"[rag.py] Could not load FAISS index, RAG will be disabled: {e}")

# # llm = ChatGoogleGenerativeAI(
# #     model="gemini-2.5-flash",
# #     google_api_key=os.getenv("GEMINI_API_KEY")
# # )

# SYSTEM_PROMPT= SYSTEM_PROMPT


# def _build_history_messages(question: str, chat_history: list = None) -> str:
#     if not chat_history:
#         return question
#     recent = chat_history[-20:]
#     context_str = " ".join(turn["content"] for turn in recent)
#     return f"{context_str} {question}"


# def split_into_chunks(text: str, min_len: int = 15) -> list:
#     """
#     Splits a full answer into natural "message bubble" pieces at
#     sentence boundaries — the same splitting behavior stream_rag uses
#     live, but usable on any already-complete string. This is what makes
#     a cached answer split into the same multi-message reply style as a
#     freshly generated one, instead of arriving as one big block.
#     """
#     chunks = []
#     buffer = text.strip()

#     while buffer:
#         cut = None
#         for stop in [". ", "! ", "? ", "\n"]:
#             idx = buffer.find(stop)
#             if idx != -1 and (cut is None or idx < cut):
#                 cut = idx + len(stop)

#         if cut and cut < len(buffer) and len(buffer[:cut].strip()) >= min_len:
#             chunks.append(buffer[:cut].strip())
#             buffer = buffer[cut:]
#         else:
#             chunks.append(buffer.strip())
#             break

#     return chunks if chunks else [text.strip()]


# def ask_rag(question: str, chat_history: list = None) -> str:
#     if retriever is None:
#         return (
#             "Sorry, my product knowledge base isn't set up yet. "
#             "Ask me something else in the meantime!"
#         )

#     try:
#         docs = retriever.invoke(question)
#         context = "\n\n".join([doc.page_content for doc in docs])

#         messages = [SystemMessage(content=SYSTEM_PROMPT)]
#         messages.extend(_build_history_messages(chat_history))
#         messages.append(HumanMessage(content=f"Context:\n{context}\n\nCustomer's message: {question}"))

#         response = llm.invoke(messages)
#         return response.content

#     except Exception as e:
#         error = str(e).lower()

#         if "quota" in error or "429" in error or "resource_exhausted" in error:
#             return (
#                 "Sorry, the AI service has reached its daily usage limit. "
#                 "Please try again later."
#             )

#         print(f"[rag.py] ask_rag error: {e}")
#         return "Sorry, I'm unable to answer your question right now."


# def stream_rag(question: str, chat_history: list = None):
#     """
#     Generator version of ask_rag — yields text chunks as the model
#     generates them, instead of waiting for the whole reply.

#     chat_history: optional list of {"role": "user"/"assistant",
#     "content": "..."} dicts, oldest first — lets the model understand
#     follow-up messages instead of treating each question in isolation.

#     Note: WhatsApp can't edit an already-sent message, so this can't
#     "type live" into one bubble like a chat UI. What it CAN do is let
#     the caller send each chunk as its own WhatsApp message, arriving in
#     quick succession — which reads like a real person sending a couple
#     of short messages in a row, rather than one long paragraph block.
#     """
#     if retriever is None:
#         yield (
#             "Sorry, my product knowledge base isn't set up yet. "
#             "Ask me something else in the meantime!"
#         )
#         return

#     try:
#         docs = retriever.invoke(question)
#         context = "\n\n".join([doc.page_content for doc in docs])

#         messages = [SystemMessage(content=SYSTEM_PROMPT)]
#         messages.extend(_build_history_messages(chat_history))
#         messages.append(HumanMessage(content=f"Context:\n{context}\n\nCustomer's message: {question}"))

#         buffer = ""
#         for chunk in llm.stream(messages):
#             piece = chunk.content or ""
#             if not piece:
#                 continue
#             buffer += piece

#             # Flush the buffer as a "message bubble" whenever we hit a
#             # natural break (sentence end, or newline) and it's long
#             # enough to be worth sending on its own. (Can't reuse
#             # split_into_chunks here — it assumes the full text is
#             # already known, which isn't true mid-stream.)
#             while True:
#                 cut = None
#                 for stop in [". ", "! ", "? ", "\n"]:
#                     idx = buffer.find(stop)
#                     if idx != -1 and (cut is None or idx < cut):
#                         cut = idx + len(stop)
#                 if cut and cut < len(buffer) and len(buffer[:cut].strip()) >= 15:
#                     yield buffer[:cut].strip()
#                     buffer = buffer[cut:]
#                 else:
#                     break

#         if buffer.strip():
#             yield buffer.strip()

#     except Exception as e:
#         error = str(e).lower()

#         if "quota" in error or "429" in error or "resource_exhausted" in error:
#             yield (
#                 "Sorry, the AI service has reached its daily usage limit. "
#                 "Please try again later."
#             )
#             return

#         print(f"[rag.py] stream_rag error: {e}")
#         yield "Sorry, I'm unable to answer your question right now."