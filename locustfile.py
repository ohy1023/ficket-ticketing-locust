import random
import string
import datetime
import gevent
import json
import base64
import hmac
import hashlib
import jwt
from websocket import WebSocketApp
from locust import HttpUser, TaskSet, task, between

# 이미 생성된 userId를 저장하는 집합
generated_user_ids = set()

def generate_random_username(length=6):
    return ''.join(random.choices(string.ascii_letters, k=length))

def generate_unique_user_id():
    while True:
        user_id = random.randint(11, 10000000)
        if user_id not in generated_user_ids:
            generated_user_ids.add(user_id)
            return user_id

def create_signature(secret_key, message):
    signature = hmac.new(secret_key.encode(), message.encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature).decode().rstrip("=")

def generate_jwt():
    header = {"alg": "HS256"}
    payload = {
        "userId": generate_unique_user_id(),
        "userName": generate_random_username(),
        "socialId": random.randint(1000000000, 9999999999),
        "iat": int(datetime.datetime.now(datetime.timezone.utc).timestamp()),
        "exp": int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=1)).timestamp())
    }
    secret_key = "awfweoafjaweofnmawsweafaweffewfaeklfniowehfioweaowieaf"
    encoded_header = base64.urlsafe_b64encode(json.dumps(header).encode()).decode().rstrip("=")
    encoded_payload = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")
    message = f"{encoded_header}.{encoded_payload}"
    signature = create_signature(secret_key, message)
    return f"{message}.{signature}"

def extract_user_id(jwt_token):
    # Decode JWT payload without verification (for simplicity)
    try:
        decoded_token = jwt.decode(jwt_token, options={"verify_signature": False})
        return decoded_token.get("userId")
    except Exception as e:
        print(f"Error decoding JWT: {e}")
        return None

class TicketingBehavior(TaskSet):
    event_id = 5  # eventId 변수로 분리

    def on_start(self):
        self.jwt_token = generate_jwt()

    @task
    def view_event(self):
        self.client.get(f"/api/v1/events/detail/{self.event_id}")

    @task
    def ticketing(self):
        headers = {"Authorization": f"Bearer {self.jwt_token}"}
        can_enter_response = self.client.get(f"/api/v1/queues/{self.event_id}/can-enter", headers=headers)

        if can_enter_response.status_code == 200:
            try:
                can_enter_data = can_enter_response.text.strip().lower() == "true"
                if can_enter_data:
                    self.handle_occupy_slot(headers)
                else:
                    self.handle_enter_queue(headers)
            except Exception as e:
                print(f"Error processing can-enter response: {e}")
        else:
            print(f"Error: can-enter request failed with status code {can_enter_response.status_code}")

    def handle_occupy_slot(self, headers):
        occupy_response = self.client.post(f"/api/v1/queues/{self.event_id}/occupy-slot", headers=headers)
        if occupy_response.status_code == 200:
            print("Occupy slot successful")
            gevent.spawn_later(60, self.leave_queue, headers)
        else:
            print(f"Error: occupy-slot request failed with status code {occupy_response.status_code}")

    def handle_enter_queue(self, headers):
        enter_response = self.client.get(f"/api/v1/queues/{self.event_id}/enter", headers=headers)
        if enter_response.status_code == 200:
            print("Enter queue successful")
            self.connect_websocket()
        else:
            print(f"Error: enter request failed with status code {enter_response.status_code}")

    def connect_websocket(self):
        # URL 인코딩된 Bearer 토큰
        # jwt = f"Bearer%20{self.jwt_token}"
        user_id = extract_user_id(self.jwt_token)
        websocket_url = f"ws://localhost:9000/queue-status/{self.event_id}"

        headers = {
            "X-User-Id": str(user_id),  # Add userId in the header
        }

        # 메시지 핸들러
        def on_message(ws, message):
            print(f"WebSocket message received: {message}")

            try:
                # JSON 메시지 파싱
                data = json.loads(message)

                # queueStatus가 Completed인지 확인
                if data.get("queueStatus") == "Completed":
                    print("Queue status is 'Completed'. Closing WebSocket.")
                    ws.close()
            except json.JSONDecodeError:
                print("Received message is not valid JSON")

        # 에러 핸들러
        def on_error(ws, error):
            print(f"WebSocket error: {error}")

        # 연결 종료 핸들러
        def on_close(ws, close_status_code, close_msg):
            print(f"WebSocket connection closed: {close_status_code}, {close_msg}")

        # 연결 성공 핸들러
        def on_open(ws):
            print("WebSocket connection established")

        # WebSocket 초기화
        ws = WebSocketApp(
            websocket_url,
            header=headers,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        ws.on_open = on_open

        try:
            gevent.spawn(ws.run_forever)
        except Exception as e:
            print(f"WebSocket connection failed: {e}")
        finally:
            ws = None  # 연결 종료 후 WebSocket 객체 해제

    def leave_queue(self, headers):
        response = self.client.delete(f"/api/v1/queues/{self.event_id}/release-slot", headers=headers)
        if response.status_code == 200:
            print("Successfully left the queue.")
        else:
            print(f"Error: leave queue request failed with status code {response.status_code}")

class TicketingUser(HttpUser):
    tasks = [TicketingBehavior]
    wait_time = between(1, 3)
    host = "http://localhost:9000"
