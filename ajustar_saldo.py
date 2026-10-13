#!/usr/bin/env python3
"""Actualiza capital y recompensas externas sin alterar la puerta de ventas.

Cada pasada:
  1. lee en la cadena los GEOD y los USDT de la wallet;
  2. si el bot tiene una operacion pendiente, no toca nada y avisa;
  3. pone el presupuesto de la estrategia = los USDT disponibles;
  4. incorpora recompensas minadas a la posición cuando la configuración lo permite;
  5. no desbloquea ventas parciales ni rearma peldaños automáticamente;
  6. imprime una unica linea de resumen.
"""
import json
import sys
from decimal import Decimal

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
    saldo_observado_antes = Decimal(previo.get("last_wallet_balance") or geod) if previo else geod

    if usdt <= 0:
        print(f"AVISO LadderBot: no hay USDT en la wallet (GEOD {geod}); no toco el presupuesto")
        return 0

    # El presupuesto y la caja siguen el USDT real.
    datos = json.loads(ruta_estrategia.read_text(encoding="utf-8"))
    datos["budget"] = str(usdt)
    ruta_estrategia.write_text(json.dumps(datos, indent=2) + "\n", encoding="utf-8")

    # El presupuesto forma parte del hash de estrategia. Actualizamos el estado
    # conservado al nuevo hash sin reconstruirlo ni rearmar peldaños.
    _, estrategias = lb.load_config(require_budget=False)
    estrategia = estrategias[TOKEN]
    if previo is None:
        estado = lb.new_state(TOKEN, estrategia, geod)
    else:
        estado = previo
        estado["strategy_hash"] = lb.strategy_hash(estrategia)
    estado["budget"] = lb.dstr(usdt)
    estado["cash_available"] = lb.dstr(usdt)
    if externos := geod - saldo_observado_antes:
        posicion = Decimal(estado["position"])
        if externos > 0 and lb.include_external_deposits(token):
            coste = Decimal(str(token.get("external_token_cost", "0")))
            lb._update_avg_cost(estado, externos, externos * coste)
            estado["position"] = lb.dstr(posicion + externos)
        elif externos < 0:
            estado["position"] = lb.dstr(max(Decimal(0), posicion + externos))
    estado["last_wallet_balance"] = lb.dstr(geod)
    lb.save_state(TOKEN, estrategia, estado)

    limpia_backups()

    def num(valor: Decimal, decimales: int = 8) -> str:
        texto = f"{valor:.{decimales}f}".rstrip("0").rstrip(".")
        return (texto or "0").replace(".", ",")

    partes = [
        f"presupuesto {num(Decimal(estado['budget']), 6)} USDT",
        f"posición gestionada {num(Decimal(estado['position']), 4)} GEOD",
        f"saldo wallet {num(geod, 4)} GEOD",
        f"precio {num(precio, 6)}",
    ]
    if externos > 0:
        partes.append(f"entraron {num(externos, 4)} GEOD externos gestionados")
    elif externos < 0:
        partes.append(f"salieron {num(-externos, 4)} GEOD de la wallet")
    if Decimal(estado["position"]) != posicion_antes:
        partes.append("AVISO: cambió la posición gestionada")
    print("LadderBot: saldo al día · " + " · ".join(partes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
