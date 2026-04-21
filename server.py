"""
server.py – Serveur d'exécution Python style Pydroid
─────────────────────────────────────────────────────
Installation :  pip install fastapi uvicorn
Lancement :     uvicorn server:app --host 0.0.0.0 --port $PORT
"""

import asyncio, sys, os, json, subprocess, ast
from typing import Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="PyServer")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

running_procs: dict[str, subprocess.Popen] = {}


# ──────────────────────────────────────────
#  POST /check  — vérification de syntaxe
# ──────────────────────────────────────────
class CheckBody(BaseModel):
    code: str

@app.post("/check")
def check_syntax(body: CheckBody):
    """
    Réponse :
        { "errors": [] }
        { "errors": [{ "line": 3, "col": 5, "message": "invalid syntax" }] }
    """
    errors = []
    try:
        ast.parse(body.code)
    except SyntaxError as e:
        errors.append({
            "line":    e.lineno  or 0,
            "col":     e.offset  or 0,
            "message": e.msg     or "Erreur de syntaxe",
            "text":    e.text.strip() if e.text else "",
        })
    return {"errors": errors}


# ──────────────────────────────────────────
#  WebSocket /ws/run  — exécution streamée
# ──────────────────────────────────────────
@app.websocket("/ws/run")
async def ws_run(ws: WebSocket):
    """
    Client → Serveur (1er message) :
        { "session_id": "abc", "code": "print('hi')", "stdin": "" }

    Serveur → Client :
        { "type": "start" }
        { "type": "stdout", "data": "hi\n" }
        { "type": "stderr", "data": "Traceback..." }
        { "type": "exit",   "code": 0 }

    Client → Serveur (pendant l'exécution) :
        { "type": "kill" }
        { "type": "stdin", "data": "42\n" }
    """
    await ws.accept()
    session_id: Optional[str] = None
    proc: Optional[subprocess.Popen] = None
    exit_code = -1

    try:
        raw = await ws.receive_text()
        payload: dict = json.loads(raw)

        code: str       = payload.get("code", "")
        session_id: str = payload.get("session_id", "default")
        stdin_init: str = payload.get("stdin", "")

        tmp = f"/tmp/pyrun_{session_id}.py"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(code)

        proc = subprocess.Popen(
            [sys.executable, "-u", tmp],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,
        )
        running_procs[session_id] = proc

        if stdin_init:
            try:
                proc.stdin.write(stdin_init)
                proc.stdin.flush()
            except BrokenPipeError:
                pass

        await ws.send_text(json.dumps({"type": "start"}))

        async def pipe_to_ws(stream, stream_type: str):
            loop = asyncio.get_event_loop()
            while True:
                line = await loop.run_in_executor(None, stream.readline)
                if not line:
                    break
                try:
                    await ws.send_text(json.dumps({"type": stream_type, "data": line}))
                except Exception:
                    break

        async def listen_client():
            while proc.poll() is None:
                try:
                    msg = await asyncio.wait_for(ws.receive_text(), timeout=0.2)
                    cmd = json.loads(msg)
                    if cmd.get("type") == "kill":
                        _kill(proc)
                        break
                    elif cmd.get("type") == "stdin":
                        try:
                            proc.stdin.write(cmd.get("data", ""))
                            proc.stdin.flush()
                        except BrokenPipeError:
                            pass
                except asyncio.TimeoutError:
                    continue
                except Exception:
                    _kill(proc)
                    break

        await asyncio.gather(
            pipe_to_ws(proc.stdout, "stdout"),
            pipe_to_ws(proc.stderr, "stderr"),
            listen_client(),
        )

        proc.wait()
        exit_code = proc.returncode

    except WebSocketDisconnect:
        if proc: _kill(proc)
        return
    except Exception as e:
        try:
            await ws.send_text(json.dumps({"type": "stderr", "data": f"[Server error] {e}\n"}))
        except Exception:
            pass
    finally:
        if session_id:
            running_procs.pop(session_id, None)
            try: os.remove(f"/tmp/pyrun_{session_id}.py")
            except Exception: pass

    try:
        await ws.send_text(json.dumps({"type": "exit", "code": exit_code}))
    except Exception:
        pass


# ──────────────────────────────────────────
#  POST /kill/{session_id}
# ──────────────────────────────────────────
@app.post("/kill/{session_id}")
def kill_session(session_id: str):
    proc = running_procs.get(session_id)
    if not proc:
        raise HTTPException(status_code=404, detail="Session introuvable")
    _kill(proc)
    running_procs.pop(session_id, None)
    return {"status": "killed"}


# ──────────────────────────────────────────
#  GET /  — healthcheck
# ──────────────────────────────────────────
@app.get("/")
def health():
    return {"status": "ok", "active_sessions": list(running_procs.keys())}


# ──────────────────────────────────────────
#  Utilitaire
# ──────────────────────────────────────────
def _kill(proc: subprocess.Popen):
    try:
        os.killpg(os.getpgid(proc.pid), 9)
    except Exception:
        try: proc.kill()
        except Exception: pass

