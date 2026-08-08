from locust import HttpUser,task


class ChatUser(HttpUser):


    @task
    def send_message(self):

        self.client.post(
            "/message",
            params={
            "user_id":"123",
            "text":"hello"
            }
        )

