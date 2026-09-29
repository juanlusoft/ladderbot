#!/usr/bin/env python3
"""Mira a que hora entran los GEOD de minero en la wallet (ultimos dias)."""
import sys
from datetime import datetime, timezone

sys.path.insert(0, "/opt/ladderbot")
import ladderbot as lb

TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DIAS = int(sys.argv[1]) if len(sys.argv) > 1 else 10

secrets = lb.load_secrets(strict_permissions=True)
w3 = lb.get_w3(secrets, require_private_rpc=True)
acct = lb.get_account(w3, secrets)
cfg, _ = lb.load_config(require_budget=False)
token = next(t for t in cfg["tokens"] if t["id"] == "GEODNET")

cabecera = w3.eth.block_number
desde = cabecera - DIAS * 43200  # ~2 s por bloque
topic_destino = "0x" + "0" * 24 + acct.address[2:].lower()

logs = []
for inicio in range(desde, cabecera, 10000):
    fin = min(inicio + 9999, cabecera)
    logs += w3.eth.get_logs(
        {
            "address": w3.to_checksum_address(token["erc20"]),
            "fromBlock": inicio,
            "toBlock": fin,
            "topics": [TOPIC, None, topic_destino],
        }
    )

print(f"bloque actual {cabecera}; ingresos encontrados: {len(logs)}")
for log in logs[-DIAS:]:
    bloque = w3.eth.get_block(log["blockNumber"])
    cuando = datetime.fromtimestamp(bloque["timestamp"], timezone.utc)
    importe = lb.human(int(log["data"], 16), 18)
    print(f"  {cuando.strftime('%d-%m-%Y %H:%M UTC')}  ({cuando.astimezone().strftime('%H:%M Madrid')})  +{importe}")
