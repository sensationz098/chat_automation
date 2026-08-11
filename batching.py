"""
batching.py — implements the "5 second gap" idea: instead of
processing every message the instant it arrives, wait a short window.
If another message from the SAME person arrives during that window,
combine them and restart the wait. Only process once nothing new has
come in for the full window.

Why this matters: people often send WhatsApp messages in quick bursts
("hi", then "do you have milk", then "and bread") instead of one
message. Without batching, that becomes 3 separate AI calls and 3
separate replies that don't understand each other. With batching, it
becomes ONE combined message, answered once, properly.

How it works (the "debounce with a token" pattern):
1. A message arrives -> push its text onto a Redis list for that
   phone, and generate a new random "token" stored as the CURRENT
   trigger for that phone. Schedule a delayed job (BATCH_WAIT_SECONDS
   from now) carrying that token.
2. When the delayed job fires, it checks: "is my token still the
   CURRENT trigger for this phone?" If yes -> nothing newer arrived
   during the wait, so process the whole batch now. If no -> a newer
   message came in and scheduled its OWN delayed job with a new
   token, so this older job does nothing (the newer one will handle
   everything, including this message's text, since it's all sitting
   in the same Redis list).
"""

import os
import uuid
from datetime import timedelta
from dotenv import load_dotenv
from redis import Redis
from rq import Queue
from rq_scheduler import Scheduler
from redis_client import get_redis_connection
from tasks import process_incoming_message

load_dotenv()

BATCH_WAIT_SECONDS = 5

redis_conn = get_redis_connection()
job_queue = Queue("interakt_messages", connection=redis_conn)
scheduler = Scheduler(queue=job_queue, connection=redis_conn)


def add_message_to_batch(phone: str, text: str):
    """
    Call this instead of enqueueing process_incoming_message directly.
    Adds the message to this phone's pending batch and (re)schedules
    the delayed processing job.
    """
    batch_key = f"batch:{phone}"
    trigger_key = f"batch_trigger:{phone}"

    # Add this message's text to the growing batch for this phone.
    redis_conn.rpush(batch_key, text)

    # Generate a fresh token and make it THE current trigger — any
    # earlier scheduled job (from a previous message in this same
    # burst) will see its own old token no longer matches this, and
    # will skip processing, deferring to this newest one instead.
    token = str(uuid.uuid4())
    redis_conn.set(trigger_key, token)

    scheduler.enqueue_in(
        timedelta(seconds=BATCH_WAIT_SECONDS),
        process_batch,
        phone,
        token,
    )
    print(f"[batching] {phone}: message added to batch, processing in {BATCH_WAIT_SECONDS}s "
          f"(unless another message arrives first)")


def process_batch(phone: str, token: str):
    """
    Runs after the wait window. Only actually processes if this job's
    token is still the CURRENT trigger — otherwise a newer message
    came in and a newer job superseded this one.
    """
    print(f"Process_batch STARTED: {phone}, token={token}")
    trigger_key = f"batch_trigger:{phone}"
    current_token = redis_conn.get(trigger_key)

    if current_token is None or current_token.decode() != token:
        print(f"[batching] {phone}: newer message arrived during wait — skipping, newer job will handle it.")
        return

    batch_key = f"batch:{phone}"
    messages = redis_conn.lrange(batch_key, 0, -1)
    combined_text = " ".join(m.decode() for m in messages)

    # Clear the batch now that we're processing it.
    redis_conn.delete(batch_key)
    redis_conn.delete(trigger_key)

    print(f"[batching] {phone}: processing combined batch -> '{combined_text}'")
    process_incoming_message(phone, combined_text)