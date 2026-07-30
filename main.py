from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse

import os

from dotenv import load_dotenv

from rag import ask_rag

from whatsapp import send_message



load_dotenv()


app = FastAPI()


VERIFY_TOKEN = os.getenv(
    "VERIFY_TOKEN"
)



@app.post("/webhook")
async def verify(request: Request):
    data = await request.json()

    print("Webhook received!")
    print(data)

    try:
        message = data["entry"][0]["changes"][0]["value"]["messages"][0]

        user_phone = message["from"]
        user_text = message["text"]["body"]

        print("Phone:", user_phone)
        print("Message:", user_text)

        # Test without RAG
        send_message(user_phone, "Hello from FastAPI!")

    except Exception as e:
        import traceback
        traceback.print_exc()

    return {"status": "ok"}


# @app.get("/webhook")
# async def verify(request: Request):
#     mode = request.query_params.get("hub.mode")
#     token = request.query_params.get("hub.verify_token")
#     challenge = request.query_params.get("hub.challenge")

#     if mode == "subscribe" and token == VERIFY_TOKEN:
#         return PlainTextResponse(challenge)

#     return PlainTextResponse("Verification failed", status_code=403)