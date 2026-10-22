"""Un modelo de mentira con la API de OpenAI, para probar un Hermes sin llamar a un modelo real.

Corre adentro del contenedor de Hermes de prueba (CI o un descartable), en 127.0.0.1, y el
profile lo usa con ``provider: custom`` y ``base_url: http://127.0.0.1:<puerto>/v1``.

Por cada pedido a ``/v1/chat/completions`` anota una linea JSON en el registro: el modelo pedido,
cuantas herramientas trajo el pedido y si el SOUL esperado llego entero adentro de algun mensaje
de sistema. Contesta con lo mismo en una linea, asi la prueba lo lee de la respuesta del agente:

    MENTIRA herramientas=0 soul=entero modelo=prueba/modelo

Con ``--conocimiento`` mira tambien un texto que el bridge manda como mensaje ``system`` en cada
pedido (el conocimiento comercial renderizado), y suma al final si llego entero y cuantos bytes de
sistema vio el modelo:

    MENTIRA herramientas=0 soul=entero modelo=prueba/modelo conocimiento=entero sistema=41234

Solo libreria estandar. Uso:

    python3 modelo_de_mentira.py --puerto 9999 --soul /ruta/SOUL.md --registro /tmp/pedidos.jsonl \
        [--conocimiento /ruta/conocimiento.md]
"""

from __future__ import annotations

import argparse
import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def _textos_de_sistema(mensajes: list) -> list[str]:
    textos = []
    for mensaje in mensajes or []:
        if not isinstance(mensaje, dict) or mensaje.get("role") not in ("system", "developer"):
            continue
        contenido = mensaje.get("content")
        if isinstance(contenido, str):
            textos.append(contenido)
        elif isinstance(contenido, list):
            textos.append("".join(p.get("text", "") for p in contenido if isinstance(p, dict)))
    return textos


def _estado(esperado: str, sistema: str) -> str:
    limpio = esperado.strip()
    if limpio and limpio in sistema:
        return "entero"
    if limpio and limpio[:200] in sistema:
        return "cortado"
    return "ausente"


def resumir_pedido(cuerpo: dict, soul: str, conocimiento: str | None = None) -> dict:
    sistema = "\n".join(_textos_de_sistema(cuerpo.get("messages", [])))
    resumen = {
        "modelo": cuerpo.get("model", ""),
        "herramientas": len(cuerpo.get("tools") or []),
        "soul": _estado(soul, sistema),
        "stream": bool(cuerpo.get("stream")),
    }
    if conocimiento is not None:
        resumen["conocimiento"] = _estado(conocimiento, sistema)
        resumen["sistema"] = len(sistema.encode("utf-8"))
    return resumen


def respuesta(resumen: dict) -> str:
    texto = (
        f"MENTIRA herramientas={resumen['herramientas']} soul={resumen['soul']} "
        f"modelo={resumen['modelo']}"
    )
    if "conocimiento" in resumen:
        texto += f" conocimiento={resumen['conocimiento']} sistema={resumen['sistema']}"
    return texto


def armar_servidor(
    puerto: int, soul_path: Path, registro: Path, conocimiento_path: Path | None = None
) -> ThreadingHTTPServer:
    class Manejador(BaseHTTPRequestHandler):
        def log_message(self, *args):  # sin ruido en la salida de la prueba
            return

        def _json(self, codigo: int, cuerpo: dict) -> None:
            datos = json.dumps(cuerpo).encode()
            self.send_response(codigo)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(datos)))
            self.end_headers()
            self.wfile.write(datos)

        def do_GET(self):  # noqa: N802
            if self.path.rstrip("/").endswith("/models"):
                self._json(200, {"object": "list", "data": [{"id": "prueba/modelo", "object": "model"}]})
            else:
                self._json(404, {"error": {"message": "no existe"}})

        def do_POST(self):  # noqa: N802
            largo = int(self.headers.get("Content-Length") or 0)
            cuerpo = json.loads(self.rfile.read(largo) or b"{}")
            if not self.path.rstrip("/").endswith("/chat/completions"):
                self._json(404, {"error": {"message": "no existe"}})
                return
            soul = soul_path.read_text(encoding="utf-8") if soul_path.exists() else ""
            conocimiento = (
                conocimiento_path.read_text(encoding="utf-8") if conocimiento_path is not None else None
            )
            resumen = resumir_pedido(cuerpo, soul, conocimiento)
            with registro.open("a", encoding="utf-8") as f:
                f.write(json.dumps(resumen) + "\n")
            texto = respuesta(resumen)
            creado = int(time.time())
            if resumen["stream"]:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                trozos = [
                    {"role": "assistant", "content": texto},
                ]
                for delta in trozos:
                    evento = {
                        "id": "mentira",
                        "object": "chat.completion.chunk",
                        "created": creado,
                        "model": resumen["modelo"],
                        "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
                    }
                    self.wfile.write(f"data: {json.dumps(evento)}\n\n".encode())
                final = {
                    "id": "mentira",
                    "object": "chat.completion.chunk",
                    "created": creado,
                    "model": resumen["modelo"],
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
                self.wfile.write(f"data: {json.dumps(final)}\n\n".encode())
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
                return
            self._json(
                200,
                {
                    "id": "mentira",
                    "object": "chat.completion",
                    "created": creado,
                    "model": resumen["modelo"],
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": texto},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                },
            )

    return ThreadingHTTPServer(("127.0.0.1", puerto), Manejador)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--puerto", type=int, default=9999)
    parser.add_argument("--soul", type=Path, required=True)
    parser.add_argument("--registro", type=Path, required=True)
    parser.add_argument("--conocimiento", type=Path, default=None)
    args = parser.parse_args()
    armar_servidor(args.puerto, args.soul, args.registro, args.conocimiento).serve_forever()


if __name__ == "__main__":
    main()
