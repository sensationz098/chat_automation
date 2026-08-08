from redis import Redis
r = Redis.from_url("redis://default:gQAAAAAAAvuxAAIgcDI1YmU1YjRlZGRiZGU0OTFkOTZiZGNkZTViZmUwZmVlMQ@liked-aardvark-195505.upstash.io:6379")
print(r.ping())  # should print True