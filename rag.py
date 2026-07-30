# import os

# from dotenv import load_dotenv

# from langchain_community.vectorstores import FAISS

# from langchain_google_genai import (
#     GoogleGenerativeAIEmbeddings,
#     ChatGoogleGenerativeAI
# )

# load_dotenv()

# embeddings = GoogleGenerativeAIEmbeddings(
#     model="models/gemini-embedding-2",
#     google_api_key=os.getenv("GEMINI_API_KEY")
# )


# db = FAISS.load_local(
#     "faiss_index",
#     embeddings,
#     allow_dangerous_deserialization=True
# )


# retriever = db.as_retriever(
#     search_kwargs={
#         "k":3
#     }
# )


# llm = ChatGoogleGenerativeAI(
#     model="gemini-2.5-flash",
#     google_api_key=os.getenv("GEMINI_API_KEY")
# )



# def ask_rag(question):
#     try:
#         docs = retriever.invoke(question)

#         context = "\n\n".join([doc.page_content for doc in docs])

#         prompt = f"""
#         Context:
#         {context}

#         Question:
#         {question}
#         """

#         response = llm.invoke(prompt)
#         return response.content

#     except Exception as e:
#         error = str(e).lower()

#         if "quota" in error or "429" in error or "resource_exhausted" in error:
#             return (
#                 "Sorry, the AI service has reached its daily usage limit. "
#                 "Please try again later."
#             )

#         return "Sorry, I'm unable to answer your question right now."


import os

from dotenv import load_dotenv

from langchain_community.vectorstores import FAISS

from langchain_google_genai import (
    GoogleGenerativeAIEmbeddings,
    ChatGoogleGenerativeAI
)

load_dotenv()

# Correct embedding model name — "gemini-embedding-2" doesn't exist.
embeddings = GoogleGenerativeAIEmbeddings(
    model="models/gemini-embedding-001",
    google_api_key=os.getenv("GEMINI_API_KEY")
)

# Load the FAISS index defensively. If the "faiss_index" folder doesn't
# exist yet (e.g. you haven't built it), this used to crash the whole
# app on import — which meant the webhook route never came up and
# WhatsApp never got a reply. Now it degrades gracefully instead.
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

# gemini-1.5-flash is an older-generation model being phased out —
# use a current one instead.
llm = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    google_api_key=os.getenv("GEMINI_API_KEY")
)

def ask_rag(question: str) -> str:
    if retriever is None:
        return (
            "Sorry, my product knowledge base isn't set up yet. "
            "Ask me something else in the meantime!"
        )

    try:
        docs = retriever.invoke(question)
        context = "\n\n".join([doc.page_content for doc in docs])

        prompt = f"""
        Context:
        {context}

        Question:
        {question}
        """

        response = llm.invoke(prompt)
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