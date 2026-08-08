import time


def process_message(message):

    print(
        "Processing:",
        message
    )


    # simulate AI delay
    time.sleep(3)


    response = (
        "AI reply for: "
        + message
    )


    return response

