#!/usr/bin/env python3
"""Deja el LadderBot apuntando al saldo que haya de verdad en la wallet.

Cada pasada:
  1. lee en la cadena los GEOD y los USDT de la wallet;
  2. si el bot tiene una operacion pendiente, no toca nada y avisa;
  3. pone el presupuesto de la estrategia = los USDT disponibles (use lo que haya);
  4. re-arma el lado de la escalera que se haya agotado, para el ciclo nuevo;
  5. reconcilia: posicion = GEOD de la cadena, caja = USDT de la cadena;
  6. imprime una unica linea de resumen.
"""
import json
import sys
from decimal import Decimal
from types import SimpleNamespace

sys.path.insert(0, "/opt/ladderbot")
import ladderbot as lb

TOKEN = "GEODNET"
BAKS_A_GUARDAR = 10


def limpia_backups() -> None:
    baks = sorted(
        lb.STATE_DIR.glob(f"{TOKEN}.json.*.bak"), key=lambda ruta: ruta.stat().st_mtime
    )
    for viejo in baks[:-BAKS_A_GUARDAR]:
        try:
            viejo.unlink()
        except OSError:
            pass


def main() -> int:
    cfg, estrategias = lb.load_config(require_budget=False)
    token = next((fila for fila in cfg["tokens"] if fila["id"] == TOKEN), None)
    if token is None:
        print("AVISO LadderBot: no encuentro GEODNET en config.json")
        return 1
    estrategia = estrategias[TOKEN]
    ruta_estrategia = lb.STRATEGY_DIR / token["strategy"]

    previo = lb.load_state(TOKEN, estrategia, False)
    if previo and previo.get("pending"):
        pendiente = previo["pending"]
        ref = (pendiente.get("tx") or {}).get("hash") or pendiente.get("phase")
        print(f"AVISO LadderBot: hay una operacion pendiente ({ref}); no he tocado el saldo")
        return 0

    secrets = lb.load_secrets(strict_permissions=True)
    w3 = lb.get_w3(secrets, require_private_rpc=True)
    cuenta = lb.get_account(w3, secrets)
    _, _, _, _, usdt_c, token_c = lb.contracts(w3, token)
    geod = lb.human(lb.balance_raw(token_c, cuenta.address), int(token["token_decimals"]))
    usdt = lb.human(lb.balance_raw(usdt_c, cuenta.address), 6)
    precio = lb.human(lb.quote_raw(w3, token, "sell", 10**int(token["token_decimals"])), 6)
    posicion_antes = Decimal(previo["position"]) if previo else Decimal(0)

    if usdt <= 0:
        print(f"AVISO LadderBot: no hay USDT en la wallet (GEOD {geod}); no toco el presupuesto")
        return 0

    # el presupuesto es lo que haya: asi la escalera puede gastarlo todo
    datos = json.loads(ruta_estrategia.read_text(encoding="utf-8"))
    datos["budget"] = str(usdt)
    ruta_estrategia.write_text(json.dumps(datos, indent=2) + "\n", encoding="utf-8")

    # hay que releer: al cambiar el fichero cambia el hash de la estrategia
    _, estrategias = lb.load_config(require_budget=False)
    estrategia = estrategias[TOKEN]

    lb.reconcile(
        SimpleNamespace(
            token=TOKEN,
            cash_available=str(usdt),
            avg_cost="0",
            manage_current_position=True,
            reset_rungs=False,
            ack_pending=None,
        )
    )

    # si la escalera se agoto (todo comprado o todo vendido), se arma otra vez
    estado = lb.load_state(TOKEN, estrategia, False)
    rearmados = []
    for lado in ("buy", "sell"):
        ids = [fila["id"] for fila in estrategia[lado]]
        if ids and all(ident in set(estado[f"{lado}_filled"]) for ident in ids):
            estado[f"{lado}_filled"] = []
            rearmados.append({"buy": "compras", "sell": "ventas"}[lado])
    if rearmados:
        lb.save_state(TOKEN, estrategia, estado)

    limpia_backups()
    nuevos = Decimal(estado["position"]) - posicion_antes

    def num(valor: Decimal, decimales: int = 8) -> str:
        texto = f"{valor:.{decimales}f}".rstrip("0").rstrip(".")
        return (texto or "0").replace(".", ",")

    partes = [
        f"presupuesto {num(Decimal(estado['budget']), 6)} USDT",
        f"posición {num(Decimal(estado['position']), 4)} GEOD",
        f"precio {num(precio, 6)}",
    ]
    if nuevos > 0:
        partes.append(f"entraron {num(nuevos, 4)} GEOD del minado")
    elif nuevos < 0:
        partes.append(f"salieron {num(-nuevos, 4)} GEOD")
    if rearmados:
        partes.append(f"escalera re-armada: {', '.join(rearmados)}")
    print("LadderBot: saldo al día · " + " · ".join(partes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
