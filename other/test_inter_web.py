import requests

API_KEY = ""
url = "https://api.interakt.ai/v1/public/message/"

headers = {
    "Authorization": f"Basic {API_KEY}",
    "Content-Type": "application/json"
}

payload = {
    "countryCode": "+91",
    "phoneNumber": "7361045453",
    "type": "Text",
    "data": {
        "message": "Hello! This is an AI assistant."
    }
}

response = requests.post(
    url,
    headers=headers,
    json=payload
)

print(response.status_code)
print(response.text) #this one works, now if anyone send message to this number ai to reply just autorely testing purpose like i am chatbot if use fastapi u can use