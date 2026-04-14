from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
import random
import time
import uuid

app = FastAPI()

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def root():
    return {"message": "Hangman server is running"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/app")
def serve_app():
    return FileResponse("static/index.html")


# -----------------------------
# In-memory state
# -----------------------------
waiting_player_id = None
players = {}
rooms = {}


# -----------------------------
# Helpers
# -----------------------------
def now_ts():
    return time.time()


def make_id(prefix="id"):
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def choose_host_and_guest(player1_id, player2_id):
    pair = [player1_id, player2_id]
    random.shuffle(pair)
    return pair[0], pair[1]


def room_public_state(room):
    host_name = players[room["host_id"]]["name"] if room["host_id"] in players else "HOST"
    guest_name = players[room["guest_id"]]["name"] if room["guest_id"] in players else "GUEST"

    return {
        "room_id": room["room_id"],
        "state": room["state"],
        "host_name": host_name,
        "guest_name": guest_name,
        "attempts_used": room["attempts_used"],
        "attempts_left": max(0, 6 - room["attempts_used"]),
        "word_length": len(room["word"]) if room["word"] else 0,
        "filled_letters": room["filled_letters"],
        "hints": room["hints"],
        "selected_index": room.get("selected_index"),
        "result_message": room.get("result_message"),
        "result_deadline": room.get("result_deadline"),
        "host_connected": room["host_connected"],
        "guest_connected": room["guest_connected"],
        "correct_word": room["word"] if room["state"] in ["guest_won", "host_won"] else None,
    }


def reset_for_new_round(room):
    room["state"] = "host_setup"
    room["word"] = ""
    room["hints"] = []
    room["filled_letters"] = []
    room["attempts_used"] = 0
    room["selected_index"] = None
    room["result_message"] = None
    room["result_deadline"] = None


def move_room_to_waiting(room, disconnected_player_id=None):
    global waiting_player_id

    room["state"] = "waiting"
    room["word"] = ""
    room["hints"] = []
    room["filled_letters"] = []
    room["attempts_used"] = 0
    room["selected_index"] = None
    room["result_message"] = "상대방이 나가서 대기 상태로 돌아갔습니다."
    room["result_deadline"] = None

    if disconnected_player_id == room["host_id"]:
        room["host_connected"] = False
    if disconnected_player_id == room["guest_id"]:
        room["guest_connected"] = False

    remaining_player_id = None
    if room["host_connected"] and room["host_id"] in players:
        remaining_player_id = room["host_id"]
    elif room["guest_connected"] and room["guest_id"] in players:
        remaining_player_id = room["guest_id"]

    if remaining_player_id:
        waiting_player_id = remaining_player_id
        players[remaining_player_id]["status"] = "waiting"
        players[remaining_player_id]["room_id"] = None
        players[remaining_player_id]["role"] = None
    else:
        waiting_player_id = None


def update_room_timers(room):
    if room["state"] in ["guest_won", "host_won"]:
        deadline = room.get("result_deadline")
        if deadline and now_ts() >= deadline:
            host_still_here = room["host_connected"] and room["host_id"] in players
            guest_still_here = room["guest_connected"] and room["guest_id"] in players

            if host_still_here and guest_still_here:
                reset_for_new_round(room)
                players[room["host_id"]]["status"] = "matched"
                players[room["guest_id"]]["status"] = "matched"
            else:
                move_room_to_waiting(room)


def get_player_room(player_id):
    player = players.get(player_id)
    if not player:
        return None
    room_id = player.get("room_id")
    if not room_id:
        return None
    return rooms.get(room_id)


# -----------------------------
# Request models
# -----------------------------
class StartMatchRequest(BaseModel):
    name: str


class HostSetupRequest(BaseModel):
    word: str
    first_hint: str


class SelectSlotRequest(BaseModel):
    index: int


class FillLetterRequest(BaseModel):
    letter: str


class AddHintRequest(BaseModel):
    hint: str


class LeaveRequest(BaseModel):
    player_id: str


# -----------------------------
# Matchmaking
# -----------------------------
@app.post("/match/start")
def start_match(req: StartMatchRequest):
    global waiting_player_id

    player_id = make_id("player")
    player_name = req.name.strip() if req.name.strip() else "PLAYER"

    players[player_id] = {
        "player_id": player_id,
        "name": player_name,
        "room_id": None,
        "role": None,
        "status": "searching",
    }

    if waiting_player_id is None or waiting_player_id not in players:
        waiting_player_id = player_id
        players[player_id]["status"] = "waiting"

        return {
            "player_id": player_id,
            "status": "waiting",
            "message": "상대를 기다리는 중입니다."
        }

    other_player_id = waiting_player_id
    waiting_player_id = None

    room_id = make_id("room")
    host_id, guest_id = choose_host_and_guest(other_player_id, player_id)

    room = {
        "room_id": room_id,
        "host_id": host_id,
        "guest_id": guest_id,
        "host_connected": True,
        "guest_connected": True,
        "state": "host_setup",
        "word": "",
        "hints": [],
        "filled_letters": [],
        "attempts_used": 0,
        "selected_index": None,
        "result_message": None,
        "result_deadline": None,
    }
    rooms[room_id] = room

    for pid in [host_id, guest_id]:
        players[pid]["room_id"] = room_id
        players[pid]["status"] = "matched"

    players[host_id]["role"] = "host"
    players[guest_id]["role"] = "guest"

    return {
        "player_id": player_id,
        "status": "matched",
        "room_id": room_id,
        "role": players[player_id]["role"],
        "message": "매칭이 완료되었습니다."
    }


@app.get("/player/{player_id}")
def get_player_state(player_id: str):
    player = players.get(player_id)
    if not player:
        return {"error": "Player not found"}

    room = get_player_room(player_id)
    if room:
        update_room_timers(room)

    player = players.get(player_id)
    if not player:
        return {"error": "Player not found"}

    return {
        "player_id": player["player_id"],
        "name": player["name"],
        "status": player["status"],
        "room_id": player["room_id"],
        "role": player["role"],
    }


# -----------------------------
# Room state
# -----------------------------
@app.get("/room/{room_id}")
def get_room_state(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return {"error": "Room not found"}

    update_room_timers(room)
    return room_public_state(room)


# -----------------------------
# Host setup
# -----------------------------
@app.post("/room/{room_id}/host/setup")
def host_setup(room_id: str, req: HostSetupRequest, player_id: str):
    room = rooms.get(room_id)
    if not room:
        return {"error": "Room not found"}

    if room["host_id"] != player_id:
        return {"error": "출제자만 설정할 수 있습니다."}

    if room["state"] != "host_setup":
        return {"error": "지금은 문제를 설정할 수 없습니다."}

    word = req.word.strip().lower()
    first_hint = req.first_hint.strip()

    if not word.isalpha():
        return {"error": "단어는 영어 알파벳만 입력하세요."}

    if len(word) < 2 or len(word) > 12:
        return {"error": "단어 길이는 2~12자로 해주세요."}

    room["word"] = word
    room["hints"] = [first_hint] if first_hint else []
    room["filled_letters"] = [""] * len(word)
    room["attempts_used"] = 0
    room["selected_index"] = None
    room["state"] = "playing"
    room["result_message"] = None
    room["result_deadline"] = None

    return {
        "message": "문제가 설정되었습니다.",
        "room": room_public_state(room)
    }


# -----------------------------
# Guest actions
# -----------------------------
@app.post("/room/{room_id}/guest/select-slot")
def guest_select_slot(room_id: str, req: SelectSlotRequest, player_id: str):
    room = rooms.get(room_id)
    if not room:
        return {"error": "Room not found"}

    if room["guest_id"] != player_id:
        return {"error": "참가자만 칸을 선택할 수 있습니다."}

    if room["state"] != "playing":
        return {"error": "지금은 칸을 선택할 수 없습니다."}

    index = req.index
    if index < 0 or index >= len(room["filled_letters"]):
        return {"error": "잘못된 칸입니다."}

    if room["filled_letters"][index]:
        return {"error": "이미 채워진 칸입니다."}

    room["selected_index"] = index
    return {
        "message": "칸이 선택되었습니다.",
        "selected_index": room["selected_index"]
    }


@app.post("/room/{room_id}/guest/fill-letter")
def guest_fill_letter(room_id: str, req: FillLetterRequest, player_id: str):
    room = rooms.get(room_id)
    if not room:
        return {"error": "Room not found"}

    if room["guest_id"] != player_id:
        return {"error": "참가자만 글자를 넣을 수 있습니다."}

    if room["state"] != "playing":
        return {"error": "지금은 글자를 넣을 수 없습니다."}

    index = room.get("selected_index")
    if index is None:
        return {"error": "먼저 칸을 선택하세요."}

    letter = req.letter.strip().lower()
    if len(letter) != 1 or not letter.isalpha():
        return {"error": "영문 알파벳 1개만 입력하세요."}

    correct_letter = room["word"][index]

    if letter == correct_letter:
        room["filled_letters"][index] = letter
        room["selected_index"] = None

        if "".join([c if c else "_" for c in room["filled_letters"]]) == room["word"]:
            room["state"] = "guest_won"
            room["result_message"] = "참가자 승리! 6번 안에 정답을 완성했습니다."
            room["result_deadline"] = now_ts() + 5

        return {
            "correct": True,
            "message": "정답입니다.",
            "room": room_public_state(room)
        }

    room["attempts_used"] += 1
    room["selected_index"] = None

    if room["attempts_used"] >= 6:
        room["state"] = "host_won"
        room["result_message"] = "출제자 승리! 참가자가 6번 안에 맞히지 못했습니다."
        room["result_deadline"] = now_ts() + 5
    else:
        room["state"] = "awaiting_hint"
        room["result_message"] = "오답입니다. 출제자가 힌트를 입력할 때까지 기다리세요."

    return {
        "correct": False,
        "message": "오답입니다.",
        "room": room_public_state(room)
    }


# -----------------------------
# Hint
# -----------------------------
@app.post("/room/{room_id}/hint")
def add_hint(room_id: str, req: AddHintRequest, player_id: str):
    room = rooms.get(room_id)
    if not room:
        return {"error": "Room not found"}

    if room["host_id"] != player_id:
        return {"error": "출제자만 힌트를 추가할 수 있습니다."}

    if room["state"] != "awaiting_hint":
        return {"error": "참가자가 틀린 뒤에만 힌트를 추가할 수 있습니다."}

    hint = req.hint.strip()
    if not hint:
        return {"error": "힌트를 입력하세요."}

    room["hints"].append(hint)
    room["state"] = "playing"
    room["result_message"] = "새 힌트가 추가되었습니다. 참가자가 다시 도전할 수 있습니다."

    return {
        "message": "힌트가 추가되었습니다.",
        "hints": room["hints"]
    }


# -----------------------------
# Leave
# -----------------------------
@app.post("/player/leave")
def leave_player(req: LeaveRequest):
    global waiting_player_id

    player = players.get(req.player_id)
    if not player:
        return {"message": "이미 정리된 플레이어입니다."}

    room = get_player_room(req.player_id)

    if waiting_player_id == req.player_id:
        waiting_player_id = None

    if room:
        move_room_to_waiting(room, disconnected_player_id=req.player_id)

    if req.player_id in players:
        del players[req.player_id]

    return {"message": "플레이어가 나갔습니다."}

