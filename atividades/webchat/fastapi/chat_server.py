# chat_server.py
from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Set, List
import os, json, threading

import jwt  # pip install pyjwt
import pika  # pip install pika
from passlib.context import CryptContext  # pip install passlib[bcrypt]
from fastapi import (
    FastAPI, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query
)
from fastapi.staticfiles import StaticFiles
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

# =========================
# Configuração
# =========================
JWT_SECRET = os.getenv("JWT_SECRET", "troque-este-segredo")  # use Secret Manager/variável de ambiente em produção
JWT_ALG = os.getenv("JWT_ALG", "HS256")
ACCESS_TTL_MIN = int(os.getenv("ACCESS_TTL_MIN", "60"))

# Configuração RabbitMQ
RABBITMQ_HOST = os.getenv("RABBITMQ_HOST", "localhost")
RABBITMQ_PORT = int(os.getenv("RABBITMQ_PORT", "5672"))
RABBITMQ_USER = os.getenv("RABBITMQ_USER", "guest")
RABBITMQ_PASS = os.getenv("RABBITMQ_PASS", "guest")
BROADCAST_EXCHANGE = "chat_broadcast"

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/login")

# Usuários pré-definidos (apenas estes podem usar o chat)
_raw_users = {
    "alice": "alice@123",
    "bob":   "bob@123",
    "carol": "carol@123",
}
USERS: Dict[str, Dict[str, str]] = {
    u: {"username": u, "password_hash": pwd.hash(p)} for u, p in _raw_users.items()
}

# =========================
# Modelos HTTP
# =========================
class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_at: int

class MeOut(BaseModel):
    username: str

class PublicUser(BaseModel):
    username: str
    online: bool = Field(description="Se o usuário está conectado via WebSocket")

# =========================
# Helpers de tempo/JWT
# =========================
def _now():
    return datetime.now(timezone.utc)

def _iso():
    return _now().isoformat()

def make_access_token(sub: str) -> TokenOut:
    exp = _now() + timedelta(minutes=ACCESS_TTL_MIN)
    payload = {
        "sub": sub,
        "type": "access",
        "iat": int(_now().timestamp()),
        "exp": int(exp.timestamp()),
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)
    return TokenOut(access_token=token, expires_at=int(exp.timestamp()))

def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido")

def current_username(token: str = Depends(oauth2_scheme)) -> str:
    payload = decode_token(token)
    if payload.get("type") != "access":
        raise HTTPException(status_code=401, detail="Token não é de acesso")
    username = payload.get("sub")
    if username not in USERS:
        raise HTTPException(status_code=403, detail="Usuário não autorizado")
    return username

# =========================
# App & CORS
# =========================
app = FastAPI(title="Chat em FastAPI + JWT (HTTP + WebSocket)")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],   # restrinja em produção
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve static files from ./client at /client
app.mount("/client", StaticFiles(directory="client", html=True), name="client")

# =========================
# Camada API (auth, perfil, listagem)
# =========================
@app.post("/login", response_model=TokenOut)
def login(form: OAuth2PasswordRequestForm = Depends()):
    user = USERS.get(form.username)
    if not user or not pwd.verify(form.password, user["password_hash"]):
        raise HTTPException(status_code=401, detail="Credenciais inválidas")
    return make_access_token(form.username)

@app.get("/me", response_model=MeOut)
def me(username: str = Depends(current_username)):
    return MeOut(username=username)

@app.get("/users", response_model=List[PublicUser])
def list_users(_: str = Depends(current_username)):
    return [PublicUser(username=u, online=u in manager.active) for u in sorted(USERS.keys())]

@app.get("/users/online", response_model=List[PublicUser])
def list_online(_: str = Depends(current_username)):
    return [PublicUser(username=u, online=True) for u in sorted(manager.active.keys())]

@app.get("/healthz")
def healthz():
    return {"status": "ok", "time": _iso()}

class MessageBroker:
    def __init__(self):
        self._init_connection()

    def _init_connection(self):
        # Conexão com RabbitMQ
        credentials = pika.PlainCredentials(RABBITMQ_USER, RABBITMQ_PASS)
        parameters = pika.ConnectionParameters(
            host=RABBITMQ_HOST,
            port=RABBITMQ_PORT,
            credentials=credentials,
            heartbeat=600,
            blocked_connection_timeout=300
        )
        self.connection = pika.BlockingConnection(parameters)
        self.channel = self.connection.channel()
        
        # Declara exchange para broadcast
        self.channel.exchange_declare(
            exchange=BROADCAST_EXCHANGE,
            exchange_type='fanout',
            durable=True
        )

    def declare_user_queue(self, username: str):
        """Cria fila exclusiva para usuário"""
        self.channel.queue_declare(queue=f"user_{username}", durable=True)

    def send_direct(self, username: str, message: dict):
        """Envia mensagem direta para usuário"""
        try:
            self.channel.basic_publish(
                exchange='',
                routing_key=f"user_{username}",
                body=json.dumps(message),
                properties=pika.BasicProperties(delivery_mode=2)  # persistente
            )
        except Exception as e:
            print(f"Erro ao enviar mensagem para {username}: {e}")
            self._init_connection()

    def broadcast(self, message: dict):
        """Envia mensagem para todos via exchange"""
        try:
            self.channel.basic_publish(
                exchange=BROADCAST_EXCHANGE,
                routing_key='',
                body=json.dumps(message),
                properties=pika.BasicProperties(delivery_mode=2)
            )
        except Exception as e:
            print(f"Erro ao fazer broadcast: {e}")
            self._init_connection()

class ConnectionManager:
    def __init__(self):
        self.active: Dict[str, WebSocket] = {}  # username -> ws
        self.typing: Set[str] = set()          # usuários digitando
        self.broker = MessageBroker()

    async def connect(self, username: str, websocket: WebSocket):
        old = self.active.get(username)
        if old:
            try:
                await old.close(code=4001)
            except Exception:
                pass
        self.active[username] = websocket
        self.broker.declare_user_queue(username)

    def disconnect(self, username: str):
        self.active.pop(username, None)
        self.typing.discard(username)

    async def send_to(self, username: str, payload: dict) -> bool:
        # Para mensagens, usa RabbitMQ
        if payload.get("type") == "message":
            self.broker.send_direct(username, payload)
            return True
        
        # Para outros tipos (presence, typing), usa WebSocket
        ws = self.active.get(username)
        if not ws:
            return False
        try:
            await ws.send_json(payload)
            return True
        except Exception:
            self.disconnect(username)
            return False

    async def broadcast(self, payload: dict):
        # Mensagens vão pelo RabbitMQ
        if payload.get("type") == "message":
            self.broker.broadcast(payload)
            return

        # Outros tipos (presence, typing) continuam via WebSocket
        dead: Set[str] = set()
        for user, ws in self.active.items():
            try:
                await ws.send_json(payload)
            except Exception:
                dead.add(user)
        for u in dead:
            self.disconnect(u)

    def roster_payload(self) -> dict:
        return {
            "type": "presence",
            "online": sorted(self.active.keys()),
            "timestamp": _iso(),
        }

    def typing_payload(self) -> dict:
        return {
            "type": "typing",
            "users": sorted(self.typing),
            "timestamp": _iso(),
        }

manager = ConnectionManager()

@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket, token: Optional[str] = Query(default=None)):
    # Aceita para poder fechar com códigos customizados
    await websocket.accept()
    if not token:
        await websocket.close(code=4401)  # Unauthorized
        return

    # Autenticação via JWT
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            await websocket.close(code=4401); return
        username = payload.get("sub")
        if username not in USERS:
            await websocket.close(code=4403); return
    except HTTPException:
        await websocket.close(code=4401); return

    # Configuração RabbitMQ para o usuário
    manager.broker.declare_user_queue(username)

    # Conecta e anuncia presença
    await manager.connect(username, websocket)
    await manager.broadcast({"type": "system", "text": f"{username} entrou no chat.", "timestamp": _iso()})
    await manager.broadcast(manager.roster_payload())
    await manager.broadcast(manager.typing_payload())  # mantém cliente em sincronia

    try:
        while True:
            raw = await websocket.receive_text()
            # Protocolo simples baseado em "type"
            # - {"type":"who"}                    -> devolve presença atual
            # - {"type":"typing","state":"start"} -> usuário começou a digitar
            # - {"type":"typing","state":"stop"}  -> usuário parou de digitar
            # - {"type":"message","text":"..."}   -> broadcast
            # - {"type":"message","text":"...","to":"bob"} -> DM para bob
            try:
                obj = json.loads(raw)
            except Exception:
                # Texto puro -> trata como mensagem broadcast
                obj = {"type": "message", "text": raw}

            kind = obj.get("type")

            if kind == "who":
                await websocket.send_json(manager.roster_payload())
                await websocket.send_json(manager.typing_payload())
                continue

            if kind == "typing":
                state = str(obj.get("state", "")).lower()
                if state == "start":
                    manager.typing.add(username)
                elif state == "stop":
                    manager.typing.discard(username)
                await manager.broadcast(manager.typing_payload())
                continue

            if kind == "message":
                text = str(obj.get("text", "")).strip()
                if not text:
                    continue
                to_user = obj.get("to")
                payload = {
                    "type": "message",
                    "sender": username,
                    "text": text,
                    "to": to_user,
                    "sent_at": _iso(),
                }
                if to_user:
                    ok = await manager.send_to(to_user, payload)
                    # confirma ao remetente (e informa offline)
                    ack = {"type": "delivery", "to": to_user, "status": "delivered" if ok else "offline", "timestamp": _iso()}
                    await manager.send_to(username, ack)
                else:
                    await manager.broadcast(payload)
                continue

            # Mensagens desconhecidas: ignore ou logue
            await manager.send_to(username, {"type": "error", "message": "invalid_payload", "timestamp": _iso()})

    except WebSocketDisconnect:
        manager.disconnect(username)
        await manager.broadcast({"type": "system", "text": f"{username} saiu do chat.", "timestamp": _iso()})
        await manager.broadcast(manager.roster_payload())
        await manager.broadcast(manager.typing_payload())
    except Exception:
        manager.disconnect(username)
        await websocket.close(code=1011)