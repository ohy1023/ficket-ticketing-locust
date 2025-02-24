import random
import string
import datetime
import gevent
import json
import base64
import hmac
import hashlib
import jwt
import logging
from websocket import WebSocketApp
from locust import HttpUser, TaskSet, task, between

# 로깅 설정
logging.basicConfig(
    level=logging.INFO,  # 로그 레벨 (DEBUG, INFO, WARNING, ERROR, CRITICAL)
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# 이미 생성된 userId를 저장하는 집합
generated_user_ids = set()


def generate_random_username(length=6):
    return ''.join(random.choices(string.ascii_letters, k=length))


def generate_unique_user_id():
    while True:
        user_id = random.randint(3, 10000000)
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
    try:
        decoded_token = jwt.decode(jwt_token, options={"verify_signature": False})
        return decoded_token.get("userId")
    except Exception as e:
        logging.error(f"JWT 디코딩 중 오류 발생: {e}")
        return None


class TicketingBehavior(TaskSet):
    event_id = 20010  # eventId 변수로 분리

    def on_start(self):
        self.jwt_token = generate_jwt()
        logging.info(f"JWT 생성 완료 - 사용자 ID: {extract_user_id(self.jwt_token)}")

    @task
    def view_event(self):
        response = self.client.get(f"/api/v1/events/detail/{self.event_id}")
        if response.status_code == 200:
            logging.info(f"이벤트 상세 정보 조회 성공 - 이벤트 ID: {self.event_id}")
        else:
            logging.error(f"이벤트 상세 정보 조회 실패 - 상태 코드: {response.status_code}")

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
                logging.error(f"대기열 진입 여부 처리 중 오류 발생: {e}")
        else:
            logging.error(f"대기열 진입 여부 요청 실패 - 상태 코드: {can_enter_response.status_code}")

    def handle_occupy_slot(self, headers):
        occupy_response = self.client.post(f"/api/v1/queues/{self.event_id}/occupy-slot", headers=headers)
        if occupy_response.status_code == 200:
            logging.info(f"슬롯 점유 성공 - 이벤트 ID: {self.event_id}")

            # 60초 ~ 180초 (1분 ~ 3분) 사이 랜덤 시간 후 leave_slot 호출
            random_delay = random.uniform(60, 180)
            logging.info(f"슬롯 해제 예정 - {random_delay:.2f}초 후")
            gevent.spawn_later(random_delay, self.leave_slot, headers)
        else:
            logging.error(f"슬롯 점유 요청 실패 - 상태 코드: {occupy_response.status_code}")

    def handle_enter_queue(self, headers):
        enter_response = self.client.get(f"/api/v1/queues/{self.event_id}/enter", headers=headers)
        if enter_response.status_code == 200:
            logging.info(f"대기열 참여 성공 - 이벤트 ID: {self.event_id}")
            self.connect_websocket(headers)
        else:
            logging.error(f"대기열 참여 요청 실패 - 상태 코드: {enter_response.status_code}")

    def connect_websocket(self, headers):
        user_id = extract_user_id(self.jwt_token)
        websocket_url = f"wss://api.ficket.shop/queue-status/{self.event_id}"

        headers = {
            "X-User-Id": str(user_id),
        }

        def on_message(ws, message):
            logging.info(f"WebSocket 메시지 수신: {message}")

            try:
                data = json.loads(message)
                if data.get("queueStatus") == "Completed":
                    ws.close()
                    random_delay = random.uniform(60, 180)  # 60초 ~ 180초
                    logging.info(f"슬롯 해제 예약 - {random_delay:.2f}초 후")
                    gevent.spawn_later(random_delay, self.leave_slot, headers)
                elif data.get("queueStatus") == "Cancelled":
                    logging.info("대기열 상태: 'Cancelled'. WebSocket 연결 종료.")
                    ws.close()
            except json.JSONDecodeError:
                logging.error("수신한 메시지가 유효한 JSON 형식이 아님")

        def on_error(ws, error):
            logging.error(f"WebSocket 오류 발생: {error}")

        def on_close(ws, close_status_code, close_msg):
            logging.info(f"WebSocket 연결 종료 - 상태 코드: {close_status_code}, 메시지: {close_msg}")

        def on_open(ws):
            logging.info("WebSocket 연결 성공")

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
            logging.error(f"WebSocket 연결 실패: {e}")
        finally:
            ws = None

    def leave_slot(self, headers):
        response = self.client.delete(f"/api/v1/queues/{self.event_id}/release-slot", headers=headers)
        if response.status_code == 200:
            logging.info("슬롯 해제 성공")
        else:
            logging.error(f"슬롯 해제 요청 실패 - 상태 코드: {response.status_code}")


class TicketingUser(HttpUser):
    tasks = [TicketingBehavior]
    wait_time = between(1, 3)  # 1초 ~ 3초 간격
    host = "https://api.ficket.shop"
