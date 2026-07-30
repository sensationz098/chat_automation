# from fastapi import FastAPI, Request
# from fastapi.responses import PlainTextResponse

# import os

# from dotenv import load_dotenv

# from rag import ask_rag

# # from whatsapp import send_message



# load_dotenv()


# app = FastAPI()


# VERIFY_TOKEN = os.getenv(
#     "VERIFY_TOKEN"
# )



# @app.post("/webhook")
# async def verify(request: Request):
#     data = await request.json()

#     print("Webhook received!")
#     print(data)

#     try:
#         message = data["entry"][0]["changes"][0]["value"]["messages"][0]

#         user_phone = message["from"]
#         user_text = message["text"]["body"]

#         print("Phone:", user_phone)
#         print("Message:", user_text)

#         # Test without RAG
#         send_message(user_phone, "Hello from FastAPI!")

#     except Exception as e:
#         import traceback
#         traceback.print_exc()

#     return {"status": "ok"}


# # @app.get("/webhook")
# # async def verify(request: Request):
# #     mode = request.query_params.get("hub.mode")
# #     token = request.query_params.get("hub.verify_token")
# #     challenge = request.query_params.get("hub.challenge")

# #     if mode == "subscribe" and token == VERIFY_TOKEN:
# #         return PlainTextResponse(challenge)

# #     return PlainTextResponse("Verification failed", status_code=403)


from fastapi import FastAPI, Request, Query
from fastapi.responses import PlainTextResponse

import os

from dotenv import load_dotenv

from rag import ask_rag
from whatsapp import send_message


load_dotenv()

app = FastAPI()

VERIFY_TOKEN = os.getenv("VERIFY_TOKEN")


@app.get("/webhook")
async def verify_webhook(
    hub_mode: str = Query(alias="hub.mode", default=None),
    hub_challenge: str = Query(alias="hub.challenge", default=None),
    hub_verify_token: str = Query(alias="hub.verify_token", default=None),
):
    """
    Meta calls this once when you click 'Verify and Save' in the
    Configuration tab. Without this route, webhook verification
    fails with no useful error message.
    """
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(content=hub_challenge, status_code=200)
    return PlainTextResponse(content="Verification failed", status_code=403)


@app.post("/webhook")
async def receive_message(request: Request):
    data = await request.json()

    print("Webhook received!")
    print(data)

    try:
        message = data["entry"][0]["changes"][0]["value"]["messages"][0]

        user_phone = message["from"]
        user_text = message["text"]["body"]

        print("Phone:", user_phone)
        print("Message:", user_text)

        # Try the RAG pipeline; if anything about it fails (index not
        # built yet, quota hit, etc.) fall back to a plain reply instead
        # of silently never responding.
        try:
            reply_text = ask_rag(user_text)
        except Exception:
            import traceback
            traceback.print_exc()
            reply_text = "Hello from FastAPI! (RAG unavailable right now)"

        send_message(user_phone, reply_text)

    except Exception:
        import traceback
        traceback.print_exc()

    return {"status": "ok"}