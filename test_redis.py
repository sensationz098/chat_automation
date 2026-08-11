# import redis

# # Connect to Redis running on your computer
# r = redis.Redis(
#     host="localhost",
#     port=6379,
#     decode_responses=True
# )

# # Test connection
# try:
#     r.ping()
#     print("Redis is connected! ✅")
# except redis.ConnectionError:
#     print("Redis is NOT connected ❌")


from redis_client import get_upstash_redis

try:
    redis = get_upstash_redis()

    # Write simple key
    redis.set("test_key", "Hello Redis")

    # Read simple key
    value = redis.get("test_key")

    # Test hset with mapping dictionary (resolves unexpected keyword argument 'mapping' error)
    redis.hset("test_hash", mapping={"user": "123", "status": "active"})
    hash_val = redis.hgetall("test_hash")

    print("[SUCCESS] Upstash Redis connected successfully!")
    print("Key Value:", value)
    print("Hash Values:", hash_val)

except Exception as e:
    print("[ERROR] Redis connection failed!")
    print("Error:", e)


