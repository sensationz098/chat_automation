from fastapi import FastAPI
from rq import Queue
from redis import Redis

from app.tasks import process_message
from app.database import SessionLocal,engine
from app.models import Base,Message


app = FastAPI()


Base.metadata.create_all(
    engine
)


redis_conn = Redis(
    host="localhost",
    port=6379
)


queue = Queue(
    "chatbot",
    connection=redis_conn
)



@app.post("/message")
def send_message(
    user_id:str,
    text:str
):


    db = SessionLocal()


    msg = Message(
        user_id=user_id,
        text=text
    )


    db.add(msg)

    db.commit()



    job = queue.enqueue(
        process_message,
        text
    )


    return {

        "status":"queued",

        "job_id":job.id

    }
