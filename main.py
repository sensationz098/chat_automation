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


@app.get("/webhook")
async def verify(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(challenge)

    return PlainTextResponse("Verification failed", status_code=403)