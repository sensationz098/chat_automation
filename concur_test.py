# import threading
# import time

# def square(num):
#     print(f"Square: {num*num}")
#     time.sleep(1)

# def cube(num):
#     print(f"Cube: {num*num*num}")
#     time.sleep(1)

# t1 = threading.Thread(target=square, args=(4,))
# t2 = threading.Thread(target=cube, args=(4,))

# t1.start()
# t2.start()
# t1.join()
# t2.join()

# print("Done!")


# import threading
# import time
# # Function to simulate a time-consuming task
# def print_numbers():
#     for i in range(1, 6):
#         print(f&quot;Printing number {i}&quot;)
#         time.sleep(1)  # Simulate a delay of 1 second
# # Function to simulate another task
# def print_letters():
#     for letter in 'Geeks':
#         print(f&quot;Printing letter {letter}&quot;)
#         time.sleep(1)  # Simulate a delay of 1 second
# # Create two thread objects, one for each function
# thread1 = threading.Thread(target=print_numbers)
# thread2 = threading.Thread(target=print_letters)

# # Start the threads
# thread1.start()
# thread2.start()

# # The main thread waits for both threads to finish
# thread1.join()
# thread2.join()

# print(&quot;Both threads have finished.&quot;)


# import concurrent.futures
# import time


# def simulate_user(user_id):
#     start = time.perf_counter()

#     print(f"User {user_id} started")

#     # Simulate user activity
#     time.sleep(2)

#     end = time.perf_counter()
#     user_time = end - start

#     print(f"User {user_id} finished in {user_time:.2f} seconds")

#     return user_id, user_time


# if __name__ == "__main__":

#     # 100 users
#     users = range(1, 101)

#     # Overall start time
#     total_start = time.perf_counter()

#     # Run 100 users concurrently
#     with concurrent.futures.ThreadPoolExecutor(max_workers=100) as executor:

#         results = list(executor.map(simulate_user, users))

#     # Overall finish time
#     total_end = time.perf_counter()

#     # Total execution time
#     total_time = total_end - total_start

#     print("\n==============================")
#     print("All 100 users finished")
#     print(f"Total time: {total_time:.2f} seconds")
#     print("==============================")

import asyncio
import random
import time

from rag import ask_rag


SAMPLE_MESSAGES = [
    "Hi",
    "Yes",
    "Other timings",
    "5 6 pm",
    "What are the fees?",
    "Do you have a demo class video?",
    "3 months",
    "1 year package",
    "Is this online live on Zoom?",
    "Teacher details kya hai?",
]

async def run_user(user_id):
    message = random.choice(SAMPLE_MESSAGES)

    start_time = time.perf_counter()

    print(f"User {user_id} started: {message}")

    try:
        # ask_rag is now async
        response = await ask_rag(message)

        elapsed = time.perf_counter() - start_time

        print(
            f"\nUser {user_id} finished in {elapsed:.2f}s\n"
            f"Message: {message}\n"
            f"RAG Reply: {response}\n"
        )

        return {
            "user_id": user_id,
            "message": message,
            "response": response,
            "time": elapsed,
            "success": True,
        }

    except Exception as e:
        elapsed = time.perf_counter() - start_time

        print(
            f"\nUser {user_id} FAILED in {elapsed:.2f}s\n"
            f"Message: {message}\n"
            f"Error: {e}\n"
        )

        return {
            "user_id": user_id,
            "message": message,
            "response": None,
            "time": elapsed,
            "success": False,
        }



async def main():

    total_start = time.perf_counter()

    # Create 100 concurrent users
    tasks = [
        run_user(user_id)
        for user_id in range(1, 10)
    ]

    # Run all users concurrently
    results = await asyncio.gather(*tasks)

    total_time = time.perf_counter() - total_start

    # Statistics
    successful = sum(
        1 for result in results
        if result["success"]
    )

    failed = len(results) - successful

    response_times = [
        result["time"]
        for result in results
        if result["success"]
    ]

    print("\n========================================")
    print("          TEST COMPLETED")
    print("========================================")
    print(f"Total users       : {len(results)}")
    print(f"Successful        : {successful}")
    print(f"Failed            : {failed}")
    print(f"Total batch time  : {total_time:.2f} seconds")

    if response_times:
        print(f"Fastest response  : {min(response_times):.2f} seconds")
        print(f"Slowest response  : {max(response_times):.2f} seconds")
        print(f"Average response  : {sum(response_times) / len(response_times):.2f} seconds")

    print("========================================")


if __name__ == "__main__":
    asyncio.run(main())
