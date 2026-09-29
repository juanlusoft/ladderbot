#!/usr/bin/env python3
"""Comandos de Telegram del LadderBot (@criptolabsbot).

Escucha las actualizaciones del bot y responde a los comandos. Es un proceso
aparte del bot de trading: solo lee la cadena y manda mensajes, nunca firma ni
toca el estado de la escalera.

Comandos:
  /p  (o /precio)  precio de ahora + gráfico de las últimas 24 h
  /help            qué sabe hacer

Uso:
  telegram_cmd.py                 bucle de escucha (lo arranca systemd)
  telegram_cmd.py --test          manda una vez la respuesta de /p y sale
  telegram_cmd.py --registrar     publica el menú de comandos en Telegram
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path

import requests

sys.path.insert(0, "/opt/ladderbot")
import ladderbot as lb  # noqa: E402
import precio_chart as pc  # noqa: E402

API = "https://api.telegram.org/bot{token}/{metodo}"
AYUDA = (
    "LadderBot · comandos\n"
    "/p · precio de ahora y gráfico de las últimas 24 h\n"
    "/version · versión instalada\n"
    "/update · instala la versión publicada en UPDATE_MANIFEST_URL\n"
    "/update URL SHA256 · instala un paquete HTTPS verificado\n\n"
    "Los avisos de compras y ventas siguen llegando solos."
)
ESPERA = 30
SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


def log(texto: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {texto}", flush=True)


def offset_path() -> Path:
    return lb.STATE_DIR / "telegram_offset.json"


def leer_offset():
    try:
        return json.loads(offset_path().read_text(encoding="utf-8")).get("offset")
    except Exception:
        return None


def guardar_offset(offset: int) -> None:
    if offset is None:
        return
    try:
        offset_path().write_text(json.dumps({"offset": offset}), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        log(f"no se pudo guardar el offset: {exc}")


def telegram(token: str, metodo: str, datos=None, ficheros=None, timeout: int = 70):
    respuesta = requests.post(API.format(token=token, metodo=metodo),
                              data=datos or {}, files=ficheros, timeout=timeout)
    if respuesta.status_code != 200:
        raise RuntimeError(f"telegram {metodo}: {respuesta.status_code} {respuesta.text[:200]}")
    return respuesta.json()


def responder_precio(token: str, chat: str) -> None:
    """Manda el precio de ahora con su gráfico."""
    try:
        texto, png, origen = pc.resumen()
    except Exception as exc:  # noqa: BLE001
        log(f"error sacando el precio: {exc}")
        telegram(token, "sendMessage", {"chat_id": chat,
                                        "text": f"No he podido leer el precio: {exc}"})
        return
    if png:
        with open(png, "rb") as fichero:
            telegram(token, "sendPhoto",
                     {"chat_id": chat, "caption": texto},
                     {"photo": ("precio.png", fichero, "image/png")})
        log(f"/p enviado con gráfico ({origen})")
    else:
        telegram(token, "sendMessage", {"chat_id": chat, "text": texto})
        log(f"/p enviado sin gráfico (velas: {origen})")


def version_instalada() -> str:
    try:
        return (lb.BASE / "VERSION").read_text(encoding="utf-8").strip()
    except Exception:
        return "anterior a 2.1.0"


def datos_actualizacion(texto: str, manifest_url: str | None) -> dict[str, str]:
    partes = texto.split()
    if len(partes) == 3:
        url, digest = partes[1], partes[2].lower()
    elif len(partes) == 1 and manifest_url:
        if not manifest_url.startswith("https://"):
            raise ValueError("UPDATE_MANIFEST_URL debe usar https://")
        respuesta = requests.get(manifest_url, timeout=20)
        respuesta.raise_for_status()
        if len(respuesta.content) > 64 * 1024:
            raise ValueError("manifiesto OTA demasiado grande")
        manifest = respuesta.json()
        url, digest = str(manifest.get("url", "")), str(manifest.get("sha256", "")).lower()
    else:
        raise ValueError("usa /update o /update URL_HTTPS SHA256")
    if not url.startswith("https://"):
        raise ValueError("el paquete OTA debe usar https://")
    if not SHA256_RE.fullmatch(digest):
        raise ValueError("el SHA-256 debe tener 64 caracteres hexadecimales")
    return {"url": url, "sha256": digest, "requested_at": str(int(time.time()))}


def solicitar_actualizacion(token: str, chat: str, texto: str,
                            manifest_url: str | None) -> None:
    destino = lb.STATE_DIR / "update-request.json"
    if destino.exists():
        telegram(token, "sendMessage", {"chat_id": chat,
                                        "text": "Ya hay una actualización OTA pendiente."})
        return
    try:
        solicitud = datos_actualizacion(texto, manifest_url)
    except Exception as exc:
        telegram(token, "sendMessage", {"chat_id": chat, "text": f"OTA rechazada: {exc}"})
        return
    temporal = destino.with_name(destino.name + f".{os.getpid()}.tmp")
    temporal.write_text(json.dumps(solicitud), encoding="utf-8")
    os.chmod(temporal, 0o600)
    telegram(token, "sendMessage", {
        "chat_id": chat,
        "text": "OTA aceptada. Se verificará el SHA-256, el manifiesto interno, "
                "la ausencia de transacciones pendientes y el doctor antes de reactivar.",
    })
    os.replace(temporal, destino)
    log("solicitud OTA entregada al actualizador root")


def atender(update: dict, token: str, autorizado: str,
            manifest_url: str | None = None) -> bool:
    """Devuelve True si ha respondido a un comando."""
    mensaje = update.get("message") or update.get("edited_message") or {}
    texto = (mensaje.get("text") or "").strip()
    chat = mensaje.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    if not texto.startswith("/"):
        return False
    if chat_id != str(autorizado):
        log(f"comando ignorado de chat no autorizado {chat_id}")
        return False
    comando = texto.split()[0].split("@")[0].lower()
    if comando in ("/p", "/precio"):
        responder_precio(token, chat_id)
        return True
    if comando == "/version":
        telegram(token, "sendMessage", {"chat_id": chat_id,
                                        "text": f"LadderBot v{version_instalada()}"})
        return True
    if comando == "/update":
        solicitar_actualizacion(token, chat_id, texto, manifest_url)
        return True
    if comando in ("/help", "/start", "/ayuda"):
        telegram(token, "sendMessage", {"chat_id": chat_id, "text": AYUDA})
        return True
    return False


def ciclo(token: str, autorizado: str, offset, atender_primero: bool,
          manifest_url: str | None = None) -> int:
    """Una vuelta de escucha; devuelve el offset nuevo."""
    parametros = {"timeout": ESPERA}
    if offset is not None:
        parametros["offset"] = offset
    try:
        datos = telegram(token, "getUpdates", parametros, timeout=ESPERA + 20)
    except Exception as exc:  # noqa: BLE001
        log(f"getUpdates falló: {exc}")
        time.sleep(5)
        return offset
    for update in datos.get("result", []):
        offset = update["update_id"] + 1
        if atender_primero:
            try:
                atender(update, token, autorizado, manifest_url)
            except Exception:  # noqa: BLE001
                log("fallo atendiendo un update:\n" + traceback.format_exc(limit=3))
    return offset


def main(argv: list[str]) -> int:
    secretos = lb.load_secrets(strict_permissions=True)
    token = secretos.get("TELEGRAM_BOT_TOKEN")
    chat = secretos.get("TELEGRAM_CHAT_ID")
    manifest_url = secretos.get("UPDATE_MANIFEST_URL") or None
    if not token or not chat:
        print("faltan TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID en secrets")
        return 2

    if "--test" in argv:
        responder_precio(token, chat)
        return 0

    if "--registrar" in argv:
        telegram(token, "setMyCommands", {
            "commands": json.dumps([{"command": "p", "description": "Precio de ahora con gráfico"},
                                    {"command": "version", "description": "Versión instalada"},
                                    {"command": "update", "description": "Actualizar por OTA"},
                                    {"command": "help", "description": "Qué sabe hacer el bot"}]),
        })
        log("menú de comandos publicado en Telegram")
        return 0

    # arranque: si no hay offset guardado, se descarta lo atrasado sin responder
    offset = leer_offset()
    primera = offset is None
    if primera:
        offset = ciclo(token, chat, None, atender_primero=False,
                       manifest_url=manifest_url)
        guardar_offset(offset)
        log("arrancado; lo que hubiera atrasado no se responde"
            + (f" (offset {offset})" if offset else ""))
    else:
        log(f"arrancado; escuchando desde el offset {offset}")

    while True:
        nuevo = ciclo(token, chat, offset, atender_primero=True,
                      manifest_url=manifest_url)
        if nuevo != offset:
            offset = nuevo
            guardar_offset(offset)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
