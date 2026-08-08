from rq import Worker,Queue
from redis import Redis

redis_conn = Redis(
    host="localhost",
    port=6379
)

queue = Queue(
    "chatbot",
    connection=redis_conn
)
worker = Worker(
    [queue]
)

worker.work()
