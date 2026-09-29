#!/usr/bin/env python3
"""Comprueba el recibo de la ultima tx y los saldos tras la venta."""
import sys

sys.path.insert(0, "/opt/ladderbot")
import ladderbot as lb

HASH = sys.argv[1] if len(sys.argv) > 1 else None
secrets = lb.load_secrets(strict_permissions=True)
w3 = lb.get_w3(secrets, require_private_rpc=True)
acct = lb.get_account(w3, secrets)
cfg, _ = lb.load_config(require_budget=False)
token = next(t for t in cfg["tokens"] if t["id"] == "GEODNET")
usdt, token_addr, quoter, router, usdt_c, token_c = lb.contracts(w3, token)

if HASH:
    r = w3.eth.get_transaction_receipt(HASH)
    print("tx", HASH)
    print("  estado   ", "OK" if r.status == 1 else "FALLIDA", f"(bloque {r.blockNumber})")
    print("  gas usado", r.gasUsed, "· en MATIC", w3.from_wei(r.gasUsed * r.effectiveGasPrice, "ether"))
    gastado, recibido = lb.transfer_totals(r, token_addr, acct.address)
    print("  GEOD movido    ", lb.human(gastado, 18) if gastado else "0")
    print("  USDT recibido  ", lb.human(recibido, 6) if recibido else "0")

print("GEOD ahora ", lb.human(lb.balance_raw(token_c, acct.address), 18))
print("USDT ahora ", lb.human(lb.balance_raw(usdt_c, acct.address), 6))
print("MATIC      ", w3.from_wei(w3.eth.get_balance(acct.address), "ether"))
