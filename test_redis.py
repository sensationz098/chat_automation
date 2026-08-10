import redis

# Connect to Redis running on your computer
r = redis.Redis(
    host="localhost",
    port=6379,
    decode_responses=True
)

# Test connection
try:
    r.ping()
    print("Redis is connected! ✅")
except redis.ConnectionError:
    print("Redis is NOT connected ❌")
