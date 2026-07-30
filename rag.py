import os

from dotenv import load_dotenv

from langchain_community.vectorstores import FAISS

from langchain_google_genai import (
    GoogleGenerativeAIEmbeddings,
    ChatGoogleGenerativeAI
)

load_dotenv()

embeddings = GoogleGenerativeAIEmbeddings(
    model="models/gemini-embedding-2",
    google_api_key=os.getenv("GEMINI_API_KEY")
)


db = FAISS.load_local(
    "faiss_index",
    embeddings,
    allow_dangerous_deserialization=True
)


retriever = db.as_retriever(
    search_kwargs={
        "k":3
    }
)


llm = ChatGoogleGenerativeAI(
    model="gemini-1.5-flash",
    google_api_key=os.getenv("GEMINI_API_KEY")
)



def ask_rag(question):
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

        return "Sorry, I'm unable to answer your question right now."