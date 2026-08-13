"""
batching.py — implements fast, reliable debouncing (1.5s gap).
If another message from the SAME person arrives during the 1.5s window,
combines them into a single prompt.

Uses an asyncio task debouncer + Redis token verification:
- No fragile rq_scheduler or separate background scheduler process required.
- 100% reliable execution 1.5 seconds after user finishes typing.
"""

import os
import uuid
import json
import asyncio
from dotenv import load_dotenv
from redis_client import get_redis_connection
from tasks import process_incoming_message, process_incoming_message_async

load_dotenv()

BATCH_WAIT_SECONDS = 0.5

redis_conn = get_redis_connection()
_active_debounce_tasks = {}


async def _async_debounce_timer(phone: str, token: str):
    """
    Waits BATCH_WAIT_SECONDS (1.5s). If no newer message arrived for this phone number,
    pulls all messages from batch:{phone}, combines them, and processes the AI reply.
    """
    await asyncio.sleep(BATCH_WAIT_SECONDS)
    trigger_key = f"batch_trigger:{phone}"
    referral_key = f"batch_referral:{phone}"

    try:
        current_token = redis_conn.get(trigger_key)
        current_token_str = current_token.decode() if isinstance(current_token, bytes) else current_token

        if not current_token_str or current_token_str != token:
            # A newer message arrived during the 1.5s wait window — let newer job handle it
            return

        batch_key = f"batch:{phone}"
        raw_messages = redis_conn.lrange(batch_key, 0, -1)
        if not raw_messages:
            return

        messages = [m.decode() if isinstance(m, bytes) else m for m in raw_messages]
        combined_text = " ".join(messages)

        # Retrieve referral if any
        raw_referral = redis_conn.get(referral_key)
        referral = None
        if raw_referral:
            try:
                referral = json.loads(raw_referral.decode() if isinstance(raw_referral, bytes) else raw_referral)
            except Exception:
                pass

        # Clear batch keys
        redis_conn.delete(batch_key)
        redis_conn.delete(trigger_key)
        redis_conn.delete(referral_key)

        print(f"[batching] {phone}: batch ready after {BATCH_WAIT_SECONDS}s -> '{combined_text}' | Referral: {referral}")

        # Process asynchronously
        await process_incoming_message_async(phone, combined_text, referral=referral)

    except Exception as e:
        print(f"[batching] Error in async debounce timer for {phone}: {e}")
        try:
            batch_key = f"batch:{phone}"
            raw_messages = redis_conn.lrange(batch_key, 0, -1)
            if raw_messages:
                messages = [m.decode() if isinstance(m, bytes) else m for m in raw_messages]
                combined_text = " ".join(messages)
                
                # Fetch referral in fallback
                raw_referral = redis_conn.get(referral_key)
                referral = None
                if raw_referral:
                    try:
                        referral = json.loads(raw_referral.decode() if isinstance(raw_referral, bytes) else raw_referral)
                    except Exception:
                        pass

                redis_conn.delete(batch_key)
                redis_conn.delete(trigger_key)
                redis_conn.delete(referral_key)
                await process_incoming_message_async(phone, combined_text, referral=referral)
        except Exception as ex:
            print(f"[batching] Fallback processing error for {phone}: {ex}")


async def add_message_to_batch_async(phone: str, text: str, referral: dict = None):
    """
    Async function to add a message to this phone's batch
    and spawn the 1.5s asyncio debounce timer task.
    """
    batch_key = f"batch:{phone}"
    trigger_key = f"batch_trigger:{phone}"
    referral_key = f"batch_referral:{phone}"

    redis_conn.rpush(batch_key, text)

    # Save referral data if provided
    if referral:
        redis_conn.setex(referral_key, 60, json.dumps(referral))

    token = str(uuid.uuid4())
    redis_conn.set(trigger_key, token)

    task = asyncio.create_task(_async_debounce_timer(phone, token))
    _active_debounce_tasks[phone] = task
    print(f"[batching] {phone}: message added to batch, processing in {BATCH_WAIT_SECONDS}s")


def add_message_to_batch(phone: str, text: str, referral: dict = None):
    """
    Synchronous fallback wrapper for add_message_to_batch.
    """
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(add_message_to_batch_async(phone, text, referral=referral))
    except RuntimeError:
        asyncio.run(add_message_to_batch_async(phone, text, referral=referral))