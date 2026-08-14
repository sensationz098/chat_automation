import time

start = time.perf_counter()

# Put the actual code you want to measure here
# Example:
for i in range(100):
    x = i * 2

end = time.perf_counter()

print(f"Time taken: {end - start:.6f} seconds")
print(f"Time taken: {(end - start) * 1000:.3f} ms")

for i in range(1000):
    x = i * 2

end = time.perf_counter()

print(f"Time taken: {end - start:.6f} seconds")
print(f"Time taken: {(end - start) * 1000:.3f} ms")
for i in range(10000):
    x = i * 2

end = time.perf_counter()

print(f"Time taken: {end - start:.6f} seconds")
print(f"Time taken: {(end - start) * 1000:.3f} ms")
for i in range(1000000):
    x = i * 2

end = time.perf_counter()

print(f"Time taken: {end - start:.6f} seconds")
print(f"Time taken: {(end - start) * 1000:.3f} ms")