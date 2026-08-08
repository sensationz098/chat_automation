import requests
from faker import Faker
import random

fake = Faker()
URL="http://localhost:8000/message"

for i in range(500):
    data={

        "user_id":fake.phone_number(),

        "text":
        random.choice(
        [
        "price?",
        "hello",
        "order status",
        "help"
        ]
        )

    }

    response=requests.post(
        URL,
        params=data
    )
    print(
        i,
        response.json()
    )
