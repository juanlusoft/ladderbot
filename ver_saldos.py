#!/usr/bin/env python3
"""Saldos reales en cadena de la wallet del bot (no firma nada)."""
import sys

sys.path.insert(0, "/opt/ladderbot")
import ladderbot as lb

secrets = lb.load_secrets(strict_permissions=True)
w3 = lb.get_w3(secrets, require_private_rpc=True)
acct = lb.get_account(w3, secrets)
cfg, _ = lb.load_config(require_budget=False)
token = next(t for t in cfg["tokens"] if t["id"] == "GEODNET")
usdt, token_addr, quoter, router, usdt_c, token_c = lb.contracts(w3, token)

print("wallet      ", acct.address)
print("GEOD        ", lb.human(lb.balance_raw(token_c, acct.address), 18))
print("USDT        ", lb.human(lb.balance_raw(usdt_c, acct.address), 6))
print("MATIC gas   ", w3.from_wei(w3.eth.get_balance(acct.address), "ether"))
print("nonce latest", w3.eth.get_transaction_count(acct.address))
print("nonce pend  ", w3.eth.get_transaction_count(acct.address, "pending"))
print("allowance   ", lb.human(token_c.functions.allowance(acct.address, router.address).call(), 18))
print("precio 1GEOD", lb.human(lb.quote_raw(w3, token, "sell", 10**18), 6), "USDT")
