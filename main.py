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



@app.get("/webhook")
async def verify(
    request: Request
):

    params = request.query_params


    mode = params.get(
        "hub.mode"
    )


    token = params.get(
        "hub.verify_token"
    )


    challenge = params.get(
        "hub.challenge"
    )


    if (
        mode=="subscribe"
        and token==VERIFY_TOKEN
    ):

        return PlainTextResponse(
            challenge
        )


    return PlainTextResponse(
        "failed",
        status_code=403
    )





@app.post("/webhook")
async def webhook(
    request: Request
):

    data = await request.json()


    try:

        message = (
            data["entry"][0]
            ["changes"][0]
            ["value"]
            ["messages"][0]
        )


        user_phone = message["from"]

        user_text = message["text"]["body"]


        answer = ask_rag(
            user_text
        )


        send_message(
            user_phone,
            answer
        )


    except Exception as e:

        print(e)


    return {
        "status":"ok"
    }