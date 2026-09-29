#!/usr/bin/env python3
"""Precio de GEODNET y gráfico para el comando /p del bot de Telegram.

Solo lectura: no firma transacciones, no toca el estado del bot ni la cesta.
- Precio de ahora: el Quoter de Uniswap V3, el mismo con el que opera el bot.
- Velas: API pública de GeckoTerminal (sin clave), con caché de 2 minutos.
- Si la API no responde: gráfico con las muestras locales que va guardando.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, "/opt/ladderbot")
import ladderbot as lb  # noqa: E402

TOKEN = "GEODNET"
POOL = "0x39bfc628a34b07c5d65f207e59077dd3c8f10730"  # GEOD/USDT 0,3 % (Uniswap V3, Polygon)
RED = "polygon_pos"
MADRID = ZoneInfo("Europe/Madrid")

URL_VELAS = ("https://api.geckoterminal.com/api/v2/networks/{red}/pools/{pool}"
             "/ohlcv/hour?aggregate=1&limit={n}")
CACHE_TTL = 120
FUENTE_TTF = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FUENTE_TTF_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

ANCHO, ALTO = 1000, 540
FONDO = (14, 20, 28)
REJILLA = (38, 50, 64)
TEXTO = (198, 208, 218)
TEXTO_TENUE = (130, 145, 160)
SUBE = (46, 204, 113)
BAJA = (231, 76, 60)
AHORA = (255, 193, 7)


def num(valor, decimales: int = 4) -> str:
    """Número con coma decimal, como se escribe en España."""
    return f"{Decimal(str(valor)):.{decimales}f}".replace(".", ",")


def cache_path() -> Path:
    return lb.STATE_DIR / "precio_cache.json"


def hist_path() -> Path:
    return lb.STATE_DIR / "precio_hist.csv"


def _token(w3=None):
    cfg, estrategias = lb.load_config(require_budget=False)
    token = next(t for t in cfg["tokens"] if t["id"] == TOKEN)
    return token, estrategias[TOKEN]


def precio_actual() -> Decimal:
    """Precio de venta de 1 GEOD en USDT según el Quoter real del bot."""
    secrets = lb.load_secrets(strict_permissions=True)
    w3 = lb.get_w3(secrets, require_private_rpc=True)
    token, _ = _token()
    crudo = lb.quote_raw(w3, token, "sell", 10 ** int(token["token_decimals"]))
    return Decimal(lb.human(crudo, 6))


def velas(n: int = 24, ttl: int = CACHE_TTL):
    """Velas horarias del pool. Devuelve (lista, origen)."""
    cache = cache_path()
    try:
        guardado = json.loads(cache.read_text(encoding="utf-8"))
        if guardado.get("velas") and time.time() - guardado.get("ts", 0) < ttl:
            return guardado["velas"], "caché"
    except Exception:
        pass
    peticion = urllib.request.Request(
        URL_VELAS.format(red=RED, pool=POOL, n=n),
        headers={"Accept": "application/json", "User-Agent": "ladderbot-telegram/1.0"},
    )
    with urllib.request.urlopen(peticion, timeout=20) as respuesta:
        datos = json.loads(respuesta.read().decode())
    lista = datos["data"]["attributes"]["ohlcv_list"]
    cache.write_text(json.dumps({"ts": time.time(), "velas": lista}), encoding="utf-8")
    return lista, "GeckoTerminal"


def apunta_muestra(precio: Decimal) -> None:
    """Guarda el precio de este momento en el histórico local (plan B)."""
    ruta = hist_path()
    nuevo = not ruta.exists()
    with ruta.open("a", encoding="utf-8") as fichero:
        if nuevo:
            fichero.write("ts,precio\n")
        fichero.write(f"{int(time.time())},{precio}\n")


def muestras_locales(limite: int = 288):
    """Últimas muestras locales como [(ts, precio)]."""
    ruta = hist_path()
    if not ruta.exists():
        return []
    filas = []
    for linea in ruta.read_text(encoding="utf-8").splitlines()[1:][-limite:]:
        try:
            ts, valor = linea.split(",")
            filas.append((int(ts), float(valor)))
        except ValueError:
            continue
    return filas


def _fuente(ruta: str, tam: int):
    from PIL import ImageFont

    try:
        return ImageFont.truetype(ruta, tam)
    except Exception:
        return ImageFont.load_default(size=tam)


def dibujar(velas_lista, precio: Decimal, estrategia: dict, salida: Path) -> Path:
    """Dibuja el gráfico de velas con la línea del precio de ahora y los peldaños."""
    from PIL import Image, ImageDraw

    imagen = Image.new("RGB", (ANCHO, ALTO), FONDO)
    d = ImageDraw.Draw(imagen)
    f_titulo = _fuente(FUENTE_TTF_B, 24)
    f_normal = _fuente(FUENTE_TTF, 16)
    f_peq = _fuente(FUENTE_TTF, 13)

    izq, der, arriba, abajo = 24, ANCHO - 156, 74, ALTO - 56

    altos = [float(v[2]) for v in velas_lista]
    bajos = [float(v[3]) for v in velas_lista]
    precio_f = float(precio)
    techo = max(altos + [precio_f])
    suelo = min(bajos + [precio_f])
    holgura = max((techo - suelo) * 0.14, techo * 0.002)
    techo += holgura
    suelo -= holgura

    def Y(valor: float) -> float:
        return arriba + (techo - valor) / (techo - suelo) * (abajo - arriba)

    n = len(velas_lista)
    paso = (der - izq) / max(n, 1)

    def X(indice: int) -> float:
        return izq + paso * (indice + 0.5)

    # rejilla y etiquetas de precio a la derecha
    for i in range(6):
        valor = techo - (techo - suelo) * i / 5
        y = Y(valor)
        d.line([(izq, y), (der, y)], fill=REJILLA, width=1)
        # no se rotula si va a chocar con la línea del precio de ahora
        if abs(y - Y(precio_f)) > 18:
            d.text((der + 10, y - 8), num(valor, 4), font=f_peq, fill=TEXTO_TENUE)

    # peldaños de la cesta más cercanos por arriba y por abajo
    compras = sorted(float(p["price"]) for p in estrategia.get("buy", []))
    ventas = sorted(float(p["price"]) for p in estrategia.get("sell", []))
    referencia = []
    debajo = [p for p in compras if p < precio_f]
    encima = [p for p in ventas if p > precio_f]
    if debajo:
        referencia.append((max(debajo), "compra"))
    if encima:
        referencia.append((min(encima), "venta"))
    for valor, etiqueta in referencia:
        if not (suelo <= valor <= techo):
            continue
        y = Y(valor)
        for x in range(int(izq), int(der), 12):
            d.line([(x, y), (x + 6, y)], fill=(70, 96, 124), width=1)
        ancho_txt = d.textlength(f"{etiqueta} {num(valor, 3)}", font=f_peq)
        d.rectangle([izq + 8, y - 9, izq + 18 + ancho_txt, y + 9], fill=(24, 34, 46))
        d.text((izq + 13, y - 7), f"{etiqueta} {num(valor, 3)}", font=f_peq, fill=(140, 190, 235))

    # velas
    for indice, vela in enumerate(velas_lista):
        apertura, alto_v, bajo_v, cierre = (float(vela[1]), float(vela[2]),
                                            float(vela[3]), float(vela[4]))
        color = SUBE if cierre >= apertura else BAJA
        x = X(indice)
        d.line([(x, Y(alto_v)), (x, Y(bajo_v))], fill=color, width=2)
        cuerpo = max(abs(Y(apertura) - Y(cierre)), 2)
        d.rectangle([x - max(paso * 0.30, 2), min(Y(apertura), Y(cierre)),
                     x + max(paso * 0.30, 2), min(Y(apertura), Y(cierre)) + cuerpo], fill=color)

    # línea del precio de ahora
    y_ahora = Y(precio_f)
    for x in range(int(izq), int(der), 14):
        d.line([(x, y_ahora), (x + 8, y_ahora)], fill=AHORA, width=2)
    etiqueta = f"ahora {num(precio_f, 5)}"
    caja = d.textbbox((0, 0), etiqueta, font=f_normal)
    ancho_et = caja[2] - caja[0]
    x_ini = der + 6
    x_fin = min(x_ini + ancho_et + 12, ANCHO - 4)
    d.rectangle([x_ini, y_ahora - 14, x_fin, y_ahora + 14], fill=(60, 46, 0))
    d.text((x_ini + 6, y_ahora - 11), etiqueta, font=f_normal, fill=AHORA)

    # títulos y ejes
    d.text((izq + 4, 16), "GEODNET / USDT", font=f_titulo, fill=TEXTO)
    d.text((izq + 4, 46), "velas de 1 hora · últimas 24 h · Pool Uniswap V3 0,3 %",
           font=f_peq, fill=TEXTO_TENUE)
    for salto in (0, n // 4, n // 2, 3 * n // 4, n - 1):
        ts = int(velas_lista[salto][0])
        formato = "%d-%m %H:%M" if salto == 0 else "%H:%M"
        hora = datetime.fromtimestamp(ts, MADRID).strftime(formato)
        x = X(salto)
        ancho_h = d.textlength(hora, font=f_peq)
        # el primero se ancla a la izquierda para que no se salga
        x_txt = izq if salto == 0 else x - ancho_h / 2
        d.text((x_txt, abajo + 8), hora, font=f_peq, fill=TEXTO_TENUE)

    pie = (f"máx {num(max(altos), 5)} · mín {num(min(bajos), 5)} · "
           f"{datetime.now(MADRID).strftime('%d-%m %H:%M')}")
    d.text((izq + 4, ALTO - 30), pie, font=f_peq, fill=TEXTO_TENUE)

    salida.parent.mkdir(parents=True, exist_ok=True)
    imagen.save(salida, format="PNG", optimize=True)
    return salida


def resumen():
    """Devuelve (texto, ruta_png o None, origen de las velas)."""
    precio = precio_actual()
    try:
        apunta_muestra(precio)
    except Exception:
        pass
    _, estrategia = _token()
    png = None
    origen = "sin datos"
    lista, origen = velas()
    if lista:
        destino = lb.STATE_DIR / "precio_chart.png"
        png = dibujar(lista, precio, estrategia, destino)
        altos = max(float(v[2]) for v in lista)
        bajos = min(float(v[3]) for v in lista)
    else:
        altos = bajos = None
    estado = lb.STATE_DIR / f"{TOKEN}.json"
    posicion = presupuesto = None
    try:
        datos = json.loads(estado.read_text(encoding="utf-8"))
        posicion = Decimal(str(datos.get("position", "0")))
        presupuesto = Decimal(str(datos.get("budget", "0")))
    except Exception:
        pass

    lineas = [f"GEODNET · {num(precio, 5)} USDT"]
    if altos is not None:
        lineas.append(f"24 h: mín {num(bajos, 5)} · máx {num(altos, 5)}")
    if posicion is not None and presupuesto is not None:
        lineas.append(f"cesta: {num(posicion, 4)} GEOD · {num(presupuesto, 2)} USDT")
    compras = sorted(float(p["price"]) for p in estrategia.get("buy", []))
    ventas = sorted(float(p["price"]) for p in estrategia.get("sell", []))
    debajo = [p for p in compras if p < float(precio)]
    encima = [p for p in ventas if p > float(precio)]
    siguiente = []
    if debajo:
        siguiente.append(f"compra {num(max(debajo), 3)}")
    if encima:
        siguiente.append(f"venta {num(min(encima), 3)}")
    if siguiente:
        lineas.append("próximo peldaño: " + " · ".join(siguiente))
    return "\n".join(lineas), png, origen


def main() -> int:
    texto, png, origen = resumen()
    print(texto)
    print(f"gráfico: {png} (velas: {origen})" if png else f"sin gráfico (velas: {origen})")
    return 0 if png else 1


if __name__ == "__main__":
    raise SystemExit(main())
