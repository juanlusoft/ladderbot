#!/usr/bin/env python3
"""Ladderbot v2: escalera de compra/venta para Uniswap v3 en Polygon.

Propiedades de seguridad principales:
- presupuesto explícito; nunca adopta todo el saldo USDT de la wallet;
- dry-run y estado real completamente separados;
- una sola instancia por modo;
- límite del peldaño incorporado a amountOutMinimum;
- transacciones firmadas persistidas antes de transmitirlas y recuperables;
- importes reales obtenidos de los eventos Transfer del recibo;
- estado corrupto o antiguo detiene el bot en vez de reiniciar la escalera.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import time
import traceback
import urllib.parse
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

BASE = Path(__file__).resolve().parent
CFG_PATH = BASE / "config.json"
STRATEGY_DIR = BASE / "strategies"
STATE_DIR = BASE / "state"
DRY_STATE_DIR = STATE_DIR / "dry-run"
SECRETS_PATH = BASE / "secrets" / "ladderbot.env"

CHAIN_ID = 137
USDT_POLYGON = "0xc2132D05D31c914a87C6611C10748AEb04B58e8F"
QUOTER_V3 = "0xb27308f9F90D607463bb33eA1BeBb41C27CE5AB6"
ROUTER_V3 = "0xE592427A0AEce92De3Edee1F18E0157C05861564"
DEFAULT_RPC = "https://polygon-bor-rpc.publicnode.com"
MIN_NOTIONAL = Decimal(2)
STATE_SCHEMA = 2
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_W3_CACHE: dict[str, Any] = {}


class LadderbotError(RuntimeError):
    pass


class ConfigError(LadderbotError):
    pass


class StateError(LadderbotError):
    pass


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


def log(message: str) -> None:
    print(f"[{now()}] {message}", flush=True)


def dec(value: Any, name: str = "valor") -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ConfigError(f"{name} no es decimal: {value!r}") from exc
    if not result.is_finite():
        raise ConfigError(f"{name} debe ser finito")
    return result


def dstr(value: Decimal) -> str:
    return format(value, "f")


def human(raw: int, decimals: int) -> Decimal:
    return Decimal(raw).scaleb(-decimals)


def raw_amount(amount: Decimal, decimals: int) -> int:
    return int(amount.scaleb(decimals).to_integral_value(rounding=ROUND_DOWN))


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"no existe {path}") from exc
    except (OSError, ValueError) as exc:
        raise ConfigError(f"no se puede leer JSON válido de {path}: {exc}") from exc


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def append_jsonl(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = (json.dumps(value, ensure_ascii=False) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)


def load_secrets(strict_permissions: bool) -> dict[str, str]:
    if not SECRETS_PATH.exists():
        return {}
    if strict_permissions:
        mode = stat.S_IMODE(SECRETS_PATH.stat().st_mode)
        if mode & 0o077:
            raise ConfigError(
                f"{SECRETS_PATH} tiene permisos {mode:04o}; exige chmod 600"
            )
        parent_mode = stat.S_IMODE(SECRETS_PATH.parent.stat().st_mode)
        if parent_mode & 0o077:
            raise ConfigError(
                f"{SECRETS_PATH.parent} tiene permisos {parent_mode:04o}; exige chmod 700"
            )
    result: dict[str, str] = {}
    with SECRETS_PATH.open("r", encoding="utf-8") as handle:
        for number, original in enumerate(handle, 1):
            line = original.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                raise ConfigError(f"línea {number} inválida en {SECRETS_PATH}")
            key, value = line.split("=", 1)
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result[key.strip()] = value
    return result


def _valid_address(value: Any, name: str) -> str:
    text = str(value)
    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", text):
        raise ConfigError(f"{name} no es una dirección EVM válida")
    return text


def strategy_hash(strategy: dict[str, Any]) -> str:
    canonical = json.dumps(strategy, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def validate_strategy(strategy: Any, path: Path) -> dict[str, Any]:
    if not isinstance(strategy, dict):
        raise ConfigError(f"{path}: la raíz debe ser un objeto")
    budget = dec(strategy.get("budget", 0), f"{path}: budget")
    if budget < 0:
        raise ConfigError(f"{path}: budget no puede ser negativo")

    all_ids: set[str] = set()
    for side in ("buy", "sell"):
        rows = strategy.get(side)
        if not isinstance(rows, list) or not rows:
            raise ConfigError(f"{path}: {side} debe ser una lista no vacía")
        prices: set[Decimal] = set()
        all_count = 0
        pct_sum = Decimal(0)
        for index, rung in enumerate(rows):
            label = f"{path}: {side}[{index}]"
            if not isinstance(rung, dict):
                raise ConfigError(f"{label} debe ser un objeto")
            rid = str(rung.get("id", ""))
            if not ID_RE.fullmatch(rid) or rid in all_ids:
                raise ConfigError(f"{label}.id falta, es inseguro o está duplicado")
            all_ids.add(rid)
            price = dec(rung.get("price"), f"{label}.price")
            if price <= 0 or price in prices:
                raise ConfigError(f"{label}.price debe ser positivo y único por lado")
            prices.add(price)
            if rung.get("all"):
                if side != "sell":
                    raise ConfigError(f"{label}: all solo está permitido en ventas")
                all_count += 1
            else:
                pct = dec(rung.get("pct"), f"{label}.pct")
                if pct <= 0 or pct > 100:
                    raise ConfigError(f"{label}.pct debe estar entre 0 y 100")
                pct_sum += pct
        if side == "buy" and pct_sum > 100:
            raise ConfigError(f"{path}: los porcentajes de compra suman más de 100")
        if side == "sell" and all_count > 1:
            raise ConfigError(f"{path}: solo puede existir una venta all")
    return strategy


def load_config(require_budget: bool = False) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    cfg = read_json(CFG_PATH)
    if not isinstance(cfg, dict):
        raise ConfigError("config.json debe contener un objeto")
    if "telegram_bot_token" in cfg:
        raise ConfigError("mueve telegram_bot_token a secrets/ladderbot.env")
    tokens = cfg.get("tokens")
    if not isinstance(tokens, list) or not tokens:
        raise ConfigError("config.json no contiene tokens")
    ids: set[str] = set()
    strategies: dict[str, dict[str, Any]] = {}
    for index, token in enumerate(tokens):
        label = f"tokens[{index}]"
        if not isinstance(token, dict):
            raise ConfigError(f"{label} debe ser un objeto")
        tid = str(token.get("id", ""))
        if not ID_RE.fullmatch(tid) or tid in ids:
            raise ConfigError(f"{label}.id falta, es inseguro o está duplicado")
        ids.add(tid)
        if token.get("network") != "polygon":
            raise ConfigError(f"{label}: solo se admite network=polygon")
        _valid_address(token.get("erc20"), f"{label}.erc20")
        decimals = int(token.get("token_decimals", -1))
        if decimals < 0 or decimals > 36:
            raise ConfigError(f"{label}.token_decimals está fuera de rango")
        fee = int(token.get("pool_fee", 0))
        if fee <= 0 or fee >= 1_000_000:
            raise ConfigError(f"{label}.pool_fee está fuera de rango")
        slippage = dec(token.get("slippage", "0.01"), f"{label}.slippage")
        if slippage <= 0 or slippage > Decimal("0.05"):
            raise ConfigError(f"{label}.slippage debe ser > 0 y <= 0.05")
        external_cost = dec(
            token.get("external_token_cost", 0), f"{label}.external_token_cost"
        )
        if external_cost < 0:
            raise ConfigError(f"{label}.external_token_cost no puede ser negativo")
        filename = token.get("strategy")
        if not isinstance(filename, str) or Path(filename).name != filename:
            raise ConfigError(f"{label}.strategy debe ser un nombre de archivo")
        path = STRATEGY_DIR / filename
        strategy = validate_strategy(read_json(path), path)
        if require_budget and dec(strategy["budget"], "budget") <= 0:
            raise ConfigError(
                f"{path}: configura un budget USDT positivo antes de ejecutar"
            )
        strategies[tid] = strategy
    poll = int(cfg.get("poll_seconds", 30))
    if poll < 5:
        raise ConfigError("poll_seconds no puede ser inferior a 5")
    max_gas = dec(cfg.get("max_gas_gwei", 500), "max_gas_gwei")
    if max_gas <= 0:
        raise ConfigError("max_gas_gwei debe ser positivo")
    confirmations = int(cfg.get("confirmations", 2))
    if confirmations < 1 or confirmations > 100:
        raise ConfigError("confirmations debe estar entre 1 y 100")
    return cfg, strategies


def get_w3(secrets: dict[str, str], require_private_rpc: bool = False):
    rpc = secrets.get("POLYGON_RPC")
    if require_private_rpc and not rpc:
        raise ConfigError("define POLYGON_RPC explícitamente para el modo real")
    rpc = rpc or DEFAULT_RPC
    fallback = secrets.get("POLYGON_RPC_FALLBACK")
    endpoints = tuple(dict.fromkeys(value for value in (rpc, fallback) if value))
    cache_key = "|".join(endpoints)
    if cache_key not in _W3_CACHE:
        from web3 import Web3

        class FailoverHTTPProvider(Web3.HTTPProvider):
            """Mantiene el último RPC sano y prueba el alternativo ante errores HTTP."""

            def __init__(self, urls: tuple[str, ...]):
                super().__init__(urls[0], request_kwargs={"timeout": 20})
                self._providers = [
                    Web3.HTTPProvider(url, request_kwargs={"timeout": 20})
                    for url in urls
                ]
                self._active = 0

            def make_request(self, method: str, params: Any):
                last_error: Exception | None = None
                order = list(range(self._active, len(self._providers)))
                order.extend(range(0, self._active))
                for index in order:
                    try:
                        response = self._providers[index].make_request(method, params)
                        self._active = index
                        return response
                    except Exception as exc:
                        last_error = exc
                assert last_error is not None
                raise last_error

        _W3_CACHE[cache_key] = Web3(FailoverHTTPProvider(endpoints))
    return _W3_CACHE[cache_key]


def get_account(w3, secrets: dict[str, str]):
    private_key = secrets.get("POLYGON_PRIVATE_KEY")
    if not private_key:
        raise ConfigError("falta POLYGON_PRIVATE_KEY en secrets/ladderbot.env")
    account = w3.eth.account.from_key(private_key)
    configured = secrets.get("POLYGON_ADDRESS")
    if configured and w3.to_checksum_address(configured) != account.address:
        raise ConfigError(
            "POLYGON_ADDRESS no coincide con la dirección derivada de POLYGON_PRIVATE_KEY"
        )
    return account


_QUOTER_ABI = [{
    "type": "function",
    "name": "quoteExactInputSingle",
    "inputs": [
        {"name": "tokenIn", "type": "address"},
        {"name": "tokenOut", "type": "address"},
        {"name": "fee", "type": "uint24"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"},
    ],
    "outputs": [{"name": "amountOut", "type": "uint256"}],
    "stateMutability": "nonpayable",
}]

_ROUTER_ABI = [{
    "type": "function",
    "name": "exactInputSingle",
    "inputs": [{"name": "params", "type": "tuple", "components": [
        {"name": "tokenIn", "type": "address"},
        {"name": "tokenOut", "type": "address"},
        {"name": "fee", "type": "uint24"},
        {"name": "recipient", "type": "address"},
        {"name": "deadline", "type": "uint256"},
        {"name": "amountIn", "type": "uint256"},
        {"name": "amountOutMinimum", "type": "uint256"},
        {"name": "sqrtPriceLimitX96", "type": "uint160"},
    ]}],
    "outputs": [{"name": "amountOut", "type": "uint256"}],
    "stateMutability": "payable",
}]

_ERC20_ABI = [
    {"type": "function", "name": "decimals", "inputs": [],
     "outputs": [{"type": "uint8"}], "stateMutability": "view"},
    {"type": "function", "name": "balanceOf",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"type": "uint256"}], "stateMutability": "view"},
    {"type": "function", "name": "allowance",
     "inputs": [{"name": "owner", "type": "address"},
                {"name": "spender", "type": "address"}],
     "outputs": [{"type": "uint256"}], "stateMutability": "view"},
    {"type": "function", "name": "approve",
     "inputs": [{"name": "spender", "type": "address"},
                {"name": "value", "type": "uint256"}],
     "outputs": [{"type": "bool"}], "stateMutability": "nonpayable"},
]


def contracts(w3, token: dict[str, Any]):
    usdt = w3.to_checksum_address(USDT_POLYGON)
    token_addr = w3.to_checksum_address(token["erc20"])
    quoter = w3.eth.contract(address=w3.to_checksum_address(QUOTER_V3), abi=_QUOTER_ABI)
    router = w3.eth.contract(address=w3.to_checksum_address(ROUTER_V3), abi=_ROUTER_ABI)
    usdt_c = w3.eth.contract(address=usdt, abi=_ERC20_ABI)
    token_c = w3.eth.contract(address=token_addr, abi=_ERC20_ABI)
    return usdt, token_addr, quoter, router, usdt_c, token_c


def quote_raw(w3, token: dict[str, Any], side: str, amount_in_raw: int) -> int:
    usdt, token_addr, quoter, _, _, _ = contracts(w3, token)
    token_in, token_out = (usdt, token_addr) if side == "buy" else (token_addr, usdt)
    return int(quoter.functions.quoteExactInputSingle(
        token_in, token_out, int(token["pool_fee"]), amount_in_raw, 0
    ).call())


def execution_guard(
    side: str,
    amount_in_raw: int,
    quote_out_raw: int,
    rung_price: Decimal,
    token_decimals: int,
    slippage: Decimal,
) -> tuple[Decimal, int, bool]:
    if amount_in_raw <= 0 or quote_out_raw <= 0:
        return Decimal(0), 0, False
    if side == "buy":
        in_usdt = human(amount_in_raw, 6)
        out_tokens = human(quote_out_raw, token_decimals)
        effective = in_usdt / out_tokens
        rung_min = int(
            (in_usdt / rung_price).scaleb(token_decimals)
            .to_integral_value(rounding=ROUND_CEILING)
        )
        eligible = effective <= rung_price
    else:
        in_tokens = human(amount_in_raw, token_decimals)
        out_usdt = human(quote_out_raw, 6)
        effective = out_usdt / in_tokens
        rung_min = int(
            (in_tokens * rung_price).scaleb(6)
            .to_integral_value(rounding=ROUND_CEILING)
        )
        eligible = effective >= rung_price
    slippage_min = int(
        (Decimal(quote_out_raw) * (Decimal(1) - slippage))
        .to_integral_value(rounding=ROUND_DOWN)
    )
    return effective, max(rung_min, slippage_min), eligible


def state_root(dry_run: bool) -> Path:
    return DRY_STATE_DIR if dry_run else STATE_DIR


def state_path(token_id: str, dry_run: bool = False) -> Path:
    if not ID_RE.fullmatch(token_id):
        raise StateError(f"id de token inseguro: {token_id!r}")
    return state_root(dry_run) / f"{token_id}.json"


def new_state(token_id: str, strategy: dict[str, Any], baseline: Decimal | None) -> dict[str, Any]:
    budget = dec(strategy["budget"], "budget")
    return {
        "schema": STATE_SCHEMA,
        "token": token_id,
        "strategy_hash": strategy_hash(strategy),
        "budget": dstr(budget),
        "cash_available": dstr(budget),
        "position": "0",
        "avg_cost": "0",
        "total_spent": "0",
        "total_received": "0",
        "buy_filled": [],
        "sell_filled": [],
        "last_wallet_balance": dstr(baseline) if baseline is not None else None,
        "pending": None,
        "paused": False,
        "history": [],
        "created": now(),
        "updated": now(),
    }


def validate_state(state: Any, token_id: str, strategy: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(state, dict):
        raise StateError(f"estado de {token_id} no es un objeto")
    if state.get("schema") != STATE_SCHEMA:
        raise StateError(
            f"estado legacy/incompatible de {token_id}; ejecuta reconcile de forma explícita"
        )
    if state.get("token") != token_id:
        raise StateError(f"el estado de {token_id} pertenece a otro token")
    if state.get("strategy_hash") != strategy_hash(strategy):
        raise StateError(
            f"la estrategia de {token_id} cambió; revisa y ejecuta reconcile"
        )
    required = {
        "budget", "cash_available", "position", "avg_cost", "total_spent",
        "total_received", "buy_filled", "sell_filled", "pending", "paused", "history"
    }
    missing = required.difference(state)
    if missing:
        raise StateError(f"estado de {token_id} incompleto: {sorted(missing)}")
    for field in ("budget", "cash_available", "position", "avg_cost", "total_spent", "total_received"):
        if dec(state[field], field) < 0:
            raise StateError(f"estado de {token_id}: {field} es negativo")
    return state


def load_state(token_id: str, strategy: dict[str, Any], dry_run: bool = False) -> dict[str, Any] | None:
    path = state_path(token_id, dry_run)
    if not path.exists():
        return None
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as exc:
        raise StateError(
            f"estado corrupto o ilegible en {path}; el bot se detiene: {exc}"
        ) from exc
    return validate_state(value, token_id, strategy)


def save_state(token_id: str, strategy: dict[str, Any], state: dict[str, Any], dry_run: bool = False) -> None:
    validate_state(state, token_id, strategy)
    state["updated"] = now()
    atomic_json(state_path(token_id, dry_run), state)


def balance_raw(contract, address: str) -> int:
    return int(contract.functions.balanceOf(address).call())


def get_or_create_live_state(w3, account, token: dict[str, Any], strategy: dict[str, Any]):
    state = load_state(token["id"], strategy, False)
    if state is not None:
        return state
    _, _, _, _, _, token_c = contracts(w3, token)
    baseline = human(balance_raw(token_c, account.address), int(token["token_decimals"]))
    state = new_state(token["id"], strategy, baseline)
    save_state(token["id"], strategy, state, False)
    log(f"{token['id']}: estado v2 creado; posición existente no gestionada")
    return state


def get_or_create_dry_state(token: dict[str, Any], strategy: dict[str, Any]):
    state = load_state(token["id"], strategy, True)
    if state is None:
        state = new_state(token["id"], strategy, None)
        save_state(token["id"], strategy, state, True)
    return state


def _update_avg_cost(state: dict[str, Any], tokens: Decimal, cost: Decimal) -> None:
    previous = dec(state["position"], "position")
    previous_cost = previous * dec(state["avg_cost"], "avg_cost")
    total = previous + tokens
    state["avg_cost"] = dstr((previous_cost + cost) / total if total else Decimal(0))


def sync_external_position(w3, account, token: dict[str, Any], strategy: dict[str, Any], state: dict[str, Any]) -> None:
    _, _, _, _, _, token_c = contracts(w3, token)
    actual = human(balance_raw(token_c, account.address), int(token["token_decimals"]))
    last_text = state.get("last_wallet_balance")
    if last_text is None:
        state["last_wallet_balance"] = dstr(actual)
        save_state(token["id"], strategy, state)
        return
    last = dec(last_text, "last_wallet_balance")
    delta = actual - last
    if delta == 0:
        return
    managed = dec(state["position"], "position")
    if delta > 0 and token.get("include_external_deposits", False):
        external_cost = dec(token.get("external_token_cost", 0), "external_token_cost")
        _update_avg_cost(state, delta, delta * external_cost)
        state["position"] = dstr(managed + delta)
        log(f"{token['id']}: se incorporan {delta:f} tokens externos")
    elif delta < 0:
        reduction = min(-delta, managed)
        if reduction:
            state["position"] = dstr(managed - reduction)
            log(f"ALERTA {token['id']}: salida externa reduce posición gestionada en {reduction:f}")
    state["last_wallet_balance"] = dstr(actual)
    save_state(token["id"], strategy, state)


def telegram_send(secrets: dict[str, str], text: str) -> bool:
    token = secrets.get("TELEGRAM_BOT_TOKEN")
    chat = secrets.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        return False
    try:
        payload = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage", data=payload
        )
        with urllib.request.urlopen(request, timeout=10):
            pass
        return True
    except Exception as exc:  # noqa: BLE001 - Telegram nunca debe romper el trading
        log(f"telegram error: {exc}")
        return False


def preflight(w3, account, cfg: dict[str, Any], tokens: list[dict[str, Any]]) -> None:
    if int(w3.eth.chain_id) != CHAIN_ID:
        raise LadderbotError(f"RPC conectado a chainId {w3.eth.chain_id}, se esperaba 137")
    for address, name in ((QUOTER_V3, "Quoter"), (ROUTER_V3, "SwapRouter"), (USDT_POLYGON, "USDT")):
        if not w3.eth.get_code(w3.to_checksum_address(address)):
            raise LadderbotError(f"{name} no tiene código en el RPC seleccionado")
    _, _, _, _, usdt_c, _ = contracts(w3, tokens[0])
    if int(usdt_c.functions.decimals().call()) != 6:
        raise LadderbotError("USDT no declara 6 decimales")
    for token in tokens:
        _, token_addr, _, _, _, token_c = contracts(w3, token)
        if not w3.eth.get_code(token_addr):
            raise LadderbotError(f"{token['id']}: contrato sin código")
        actual_decimals = int(token_c.functions.decimals().call())
        if actual_decimals != int(token["token_decimals"]):
            raise LadderbotError(
                f"{token['id']}: config declara {token['token_decimals']} decimales, contrato {actual_decimals}"
            )
        quote_raw(w3, token, "buy", 1_000_000)
    max_gas_wei = int(dec(cfg.get("max_gas_gwei", 500)).scaleb(9))
    if int(Decimal(int(w3.eth.gas_price)) * Decimal("1.20")) > max_gas_wei:
        raise LadderbotError("gas actual supera max_gas_gwei")
    if int(w3.eth.get_balance(account.address)) == 0:
        raise LadderbotError("wallet sin POL para pagar gas")


def sign_transaction(w3, account, fn, cfg: dict[str, Any]) -> dict[str, Any]:
    if int(w3.eth.chain_id) != CHAIN_ID:
        raise LadderbotError("chainId cambió antes de firmar")
    nonce = int(w3.eth.get_transaction_count(account.address, "pending"))
    gas_price = int(Decimal(int(w3.eth.gas_price)) * Decimal("1.20"))
    max_gas = int(dec(cfg.get("max_gas_gwei", 500)).scaleb(9))
    if gas_price > max_gas:
        raise LadderbotError(
            f"gasPrice {Decimal(gas_price).scaleb(-9):f} gwei supera el máximo"
        )
    gas = int(fn.estimate_gas({"from": account.address}))
    tx = fn.build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gas": int(Decimal(gas) * Decimal("1.30")),
        "gasPrice": gas_price,
        "chainId": CHAIN_ID,
    })
    signed = account.sign_transaction(tx)
    raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
    raw_bytes = bytes(raw)
    return {
        "nonce": nonce,
        "hash": w3.keccak(raw_bytes).hex(),
        "raw": "0x" + raw_bytes.hex(),
        "signed_at": now(),
    }


def _receipt(w3, tx_hash: str):
    from web3.exceptions import TransactionNotFound

    try:
        return w3.eth.get_transaction_receipt(tx_hash)
    except TransactionNotFound:
        return None


def _confirmed(w3, receipt, cfg: dict[str, Any]) -> bool:
    confirmations = int(cfg.get("confirmations", 2))
    return int(w3.eth.block_number) >= int(receipt.blockNumber) + confirmations - 1


def broadcast_signed(w3, tx_meta: dict[str, Any], cfg: dict[str, Any], wait: bool):
    receipt = _receipt(w3, tx_meta["hash"])
    if receipt is None:
        try:
            raw = bytes.fromhex(tx_meta["raw"].removeprefix("0x"))
            sent = w3.eth.send_raw_transaction(raw)
            if sent.hex().lower() != tx_meta["hash"].lower():
                raise LadderbotError("el RPC devolvió un hash distinto para la transacción firmada")
        except ValueError as exc:
            message = str(exc).lower()
            harmless = ("already known", "known transaction", "nonce too low")
            if not any(text in message for text in harmless):
                raise
        if wait:
            timeout = int(cfg.get("receipt_timeout", 180))
            try:
                receipt = w3.eth.wait_for_transaction_receipt(tx_meta["hash"], timeout=timeout)
            except Exception as exc:  # noqa: BLE001 - cualquier timeout conserva pending
                log(f"tx {tx_meta['hash']} sigue pendiente/desconocida: {exc}")
                return None
        else:
            return None
    if not _confirmed(w3, receipt, cfg):
        return None
    return receipt


def quote_pending(w3, token: dict[str, Any], pending: dict[str, Any]):
    amount_in_raw = int(pending["amount_in_raw"])
    quote = quote_raw(w3, token, pending["side"], amount_in_raw)
    effective, minimum, eligible = execution_guard(
        pending["side"], amount_in_raw, quote, dec(pending["rung_price"]),
        int(token["token_decimals"]), dec(token.get("slippage", "0.01"))
    )
    return quote, minimum, effective, eligible


def _log_hex(value: Any) -> str:
    if hasattr(value, "hex"):
        text = value.hex()
        return text if text.startswith("0x") else "0x" + text
    return str(value)


def transfer_totals(receipt, token_address: str, wallet: str) -> tuple[int, int]:
    token_address = token_address.lower()
    wallet = wallet.lower()
    sent = 0
    received = 0
    for event in receipt.logs:
        if str(event.address).lower() != token_address or len(event.topics) < 3:
            continue
        if _log_hex(event.topics[0]).lower() != TRANSFER_TOPIC:
            continue
        from_addr = "0x" + _log_hex(event.topics[1]).removeprefix("0x")[-40:]
        to_addr = "0x" + _log_hex(event.topics[2]).removeprefix("0x")[-40:]
        data = event.data
        amount = int.from_bytes(bytes(data), "big") if not isinstance(data, str) else int(data, 16)
        if from_addr.lower() == wallet:
            sent += amount
        if to_addr.lower() == wallet:
            received += amount
    return sent, received


def apply_trade(
    state: dict[str, Any], side: str, rung_id: str,
    tokens: Decimal, usdt: Decimal, price: Decimal, tx_hash: str | None,
    dry_run: bool,
) -> dict[str, Any]:
    if side == "buy":
        _update_avg_cost(state, tokens, usdt)
        state["position"] = dstr(dec(state["position"]) + tokens)
        state["cash_available"] = dstr(max(Decimal(0), dec(state["cash_available"]) - usdt))
        state["total_spent"] = dstr(dec(state["total_spent"]) + usdt)
        state["buy_filled"].append(rung_id)
        state["sell_filled"] = []
        realized = Decimal(0)
    else:
        realized = usdt - tokens * dec(state["avg_cost"])
        state["position"] = dstr(max(Decimal(0), dec(state["position"]) - tokens))
        state["cash_available"] = dstr(dec(state["cash_available"]) + usdt)
        state["total_received"] = dstr(dec(state["total_received"]) + usdt)
        state["sell_filled"].append(rung_id)
        state["buy_filled"] = []
    entry = {
        "ts": now(), "side": side, "rung_id": rung_id,
        "tokens": dstr(tokens), "usdt": dstr(usdt), "price": dstr(price),
        "realized": dstr(realized), "dry": dry_run,
    }
    if tx_hash:
        entry["tx"] = tx_hash
    state["history"] = (state.get("history", []) + [entry])[-1000:]
    return entry


def finalize_swap(w3, account, token, strategy, state, receipt, secrets) -> None:
    pending = state["pending"]
    usdt, token_addr, _, _, _, token_c = contracts(w3, token)
    usdt_sent, usdt_received = transfer_totals(receipt, usdt, account.address)
    token_sent, token_received = transfer_totals(receipt, token_addr, account.address)
    if pending["side"] == "buy":
        spent_raw, received_raw = usdt_sent, token_received
    else:
        spent_raw, received_raw = token_sent, usdt_received
    if spent_raw <= 0 or received_raw <= 0:
        pending["phase"] = "needs_reconciliation"
        pending["receipt_block"] = int(receipt.blockNumber)
        state["paused"] = True
        save_state(token["id"], strategy, state)
        telegram_send(secrets, f"🚨 {token['id']}: recibo sin transfers esperados; PAUSADO")
        raise StateError(f"{token['id']}: no se pudieron reconciliar los eventos Transfer")
    if pending["side"] == "buy":
        tokens = human(received_raw, int(token["token_decimals"]))
        usdt_value = human(spent_raw, 6)
    else:
        tokens = human(spent_raw, int(token["token_decimals"]))
        usdt_value = human(received_raw, 6)
    actual_price = usdt_value / tokens
    entry = apply_trade(
        state, pending["side"], pending["rung_id"], tokens, usdt_value,
        actual_price, pending["tx"]["hash"], False
    )
    state["last_wallet_balance"] = dstr(
        human(balance_raw(token_c, account.address), int(token["token_decimals"]))
    )
    state["pending"] = None
    save_state(token["id"], strategy, state)
    try:
        append_jsonl(STATE_DIR / "trades.jsonl", {"token": token["id"], **entry})
    except Exception as exc:  # noqa: BLE001 - el estado principal ya es durable
        log(f"journal secundario error (estado ya confirmado): {exc}")
    side = entry["side"].upper()
    telegram_send(
        secrets,
        f"✅ {token['id']}: {side} {tokens:f} tok / {usdt_value:f} USDT @ {actual_price:f}\n"
        f"🔗 https://polygonscan.com/tx/{entry['tx']}"
    )
    log(f"{token['id']}: {side} confirmado @ {actual_price:f} tx={entry['tx']}")


def clear_failed_pending(token, strategy, state, secrets, reason: str) -> None:
    tx_hash = (state.get("pending") or {}).get("tx", {}).get("hash")
    state["pending"] = None
    save_state(token["id"], strategy, state)
    suffix = f" tx={tx_hash}" if tx_hash else ""
    log(f"{token['id']}: orden fallida: {reason}{suffix}")
    telegram_send(secrets, f"❌ {token['id']}: {reason}{suffix}")


def process_pending(w3, account, cfg, token, strategy, state, secrets, wait_new: bool = False) -> bool:
    """Procesa una orden pendiente. Devuelve True si aún queda pendiente."""
    while state.get("pending"):
        pending = state["pending"]
        phase = pending["phase"]
        if phase == "needs_reconciliation":
            log(f"{token['id']}: PAUSADO; orden necesita reconciliación manual")
            return True
        if phase in {"approval_zero_signed", "approval_amount_signed", "swap_signed"}:
            if (
                phase == "swap_signed"
                and int(pending.get("deadline", 0)) < int(time.time())
                and _receipt(w3, pending["tx"]["hash"]) is None
            ):
                pending["phase"] = "needs_reconciliation"
                state["paused"] = True
                save_state(token["id"], strategy, state)
                telegram_send(
                    secrets,
                    f"🚨 {token['id']}: swap firmado expiró sin recibo; PAUSADO",
                )
                return True
            receipt = broadcast_signed(w3, pending["tx"], cfg, wait_new)
            wait_new = False
            if receipt is None:
                log(f"{token['id']}: pendiente {phase} tx={pending['tx']['hash']}")
                return True
            if int(receipt.status) != 1:
                clear_failed_pending(token, strategy, state, secrets, f"{phase} revertida")
                return False
            if phase == "swap_signed":
                finalize_swap(w3, account, token, strategy, state, receipt, secrets)
                return False
            pending["phase"] = "prepared"
            pending.pop("tx", None)
            save_state(token["id"], strategy, state)
            continue

        if phase not in {"prepared", "ready_swap"}:
            raise StateError(f"{token['id']}: fase pendiente desconocida {phase!r}")

        quote, minimum, effective, eligible = quote_pending(w3, token, pending)
        if not eligible:
            log(
                f"{token['id']}: precio efectivo {effective:f} ya no cumple peldaño "
                f"{pending['rung_price']}; orden cancelada sin marcar"
            )
            state["pending"] = None
            save_state(token["id"], strategy, state)
            return False

        usdt, token_addr, _, router, usdt_c, token_c = contracts(w3, token)
        input_c = usdt_c if pending["side"] == "buy" else token_c
        actual_input = balance_raw(input_c, account.address)
        amount_in_raw = int(pending["amount_in_raw"])
        if actual_input < amount_in_raw:
            log(f"{token['id']}: saldo insuficiente; orden cancelada sin marcar")
            state["pending"] = None
            save_state(token["id"], strategy, state)
            return False

        if phase == "prepared":
            allowance = int(input_c.functions.allowance(account.address, router.address).call())
            if allowance < amount_in_raw:
                approve_value = 0 if allowance else amount_in_raw
                fn = input_c.functions.approve(router.address, approve_value)
                pending["tx"] = sign_transaction(w3, account, fn, cfg)
                pending["phase"] = "approval_zero_signed" if allowance else "approval_amount_signed"
                save_state(token["id"], strategy, state)
                wait_new = True
                continue
            pending["phase"] = "ready_swap"
            save_state(token["id"], strategy, state)
            continue

        deadline = int(time.time()) + int(cfg.get("transaction_deadline_seconds", 300))
        params = (
            usdt if pending["side"] == "buy" else token_addr,
            token_addr if pending["side"] == "buy" else usdt,
            int(token["pool_fee"]), account.address, deadline,
            amount_in_raw, minimum, 0,
        )
        fn = router.functions.exactInputSingle(params)
        pending["quote_out_raw"] = str(quote)
        pending["minimum_out_raw"] = str(minimum)
        pending["effective_quote"] = dstr(effective)
        pending["deadline"] = deadline
        pending["tx"] = sign_transaction(w3, account, fn, cfg)
        pending["phase"] = "swap_signed"
        save_state(token["id"], strategy, state)
        wait_new = True
    return False


def make_pending(side: str, rung: dict[str, Any], amount_in_raw: int) -> dict[str, Any]:
    return {
        "order_id": hashlib.sha256(
            f"{time.time_ns()}:{side}:{rung['id']}".encode()
        ).hexdigest()[:24],
        "side": side,
        "rung_id": rung["id"],
        "rung_price": dstr(dec(rung["price"])),
        "amount_in_raw": str(amount_in_raw),
        "phase": "prepared",
        "created": now(),
    }


def check_live_token(w3, account, cfg, token, strategy, secrets) -> None:
    state = get_or_create_live_state(w3, account, token, strategy)
    if state.get("pending"):
        process_pending(w3, account, cfg, token, strategy, state, secrets)
        return
    if state.get("paused"):
        return
    sync_external_position(w3, account, token, strategy, state)

    budget = dec(state["budget"])
    cash = dec(state["cash_available"])
    for rung in sorted(strategy["buy"], key=lambda row: dec(row["price"]), reverse=True):
        if rung["id"] in state["buy_filled"]:
            continue
        planned = budget * dec(rung["pct"]) / 100
        amount = min(planned, cash)
        if amount < MIN_NOTIONAL:
            log(f"{token['id']}: compra {rung['id']} espera capital (disponible {cash:f})")
            break
        amount_in_raw = raw_amount(amount, 6)
        quote = quote_raw(w3, token, "buy", amount_in_raw)
        effective, _, eligible = execution_guard(
            "buy", amount_in_raw, quote, dec(rung["price"]),
            int(token["token_decimals"]), dec(token["slippage"])
        )
        if eligible:
            state["pending"] = make_pending("buy", rung, amount_in_raw)
            save_state(token["id"], strategy, state)
            log(f"{token['id']}: prepara BUY {amount:f} USDT @ efectivo {effective:f}")
            process_pending(w3, account, cfg, token, strategy, state, secrets, wait_new=True)
        break

    if state.get("pending"):
        return
    position = dec(state["position"])
    if position <= 0:
        return
    for rung in sorted(strategy["sell"], key=lambda row: dec(row["price"])):
        if rung["id"] in state["sell_filled"]:
            continue
        amount = position if rung.get("all") else position * dec(rung["pct"]) / 100
        amount_in_raw = raw_amount(amount, int(token["token_decimals"]))
        if amount_in_raw <= 0:
            break
        quote = quote_raw(w3, token, "sell", amount_in_raw)
        effective, _, eligible = execution_guard(
            "sell", amount_in_raw, quote, dec(rung["price"]),
            int(token["token_decimals"]), dec(token["slippage"])
        )
        if human(quote, 6) < MIN_NOTIONAL:
            log(f"{token['id']}: venta {rung['id']} espera (< {MIN_NOTIONAL} USDT)")
            break
        if eligible:
            state["pending"] = make_pending("sell", rung, amount_in_raw)
            save_state(token["id"], strategy, state)
            log(f"{token['id']}: prepara SELL {amount:f} tok @ efectivo {effective:f}")
            process_pending(w3, account, cfg, token, strategy, state, secrets, wait_new=True)
        break


def check_dry_token(w3, token, strategy) -> None:
    state = get_or_create_dry_state(token, strategy)
    if state.get("paused"):
        return
    budget = dec(state["budget"])
    cash = dec(state["cash_available"])
    for rung in sorted(strategy["buy"], key=lambda row: dec(row["price"]), reverse=True):
        if rung["id"] in state["buy_filled"]:
            continue
        amount = min(budget * dec(rung["pct"]) / 100, cash)
        if amount < MIN_NOTIONAL:
            break
        raw_in = raw_amount(amount, 6)
        quote = quote_raw(w3, token, "buy", raw_in)
        effective, _, eligible = execution_guard(
            "buy", raw_in, quote, dec(rung["price"]),
            int(token["token_decimals"]), dec(token["slippage"])
        )
        if eligible:
            tokens = human(quote, int(token["token_decimals"]))
            entry = apply_trade(state, "buy", rung["id"], tokens, amount, effective, None, True)
            save_state(token["id"], strategy, state, True)
            append_jsonl(DRY_STATE_DIR / "trades.jsonl", {"token": token["id"], **entry})
            log(f"{token['id']} [DRY] BUY {amount:f} USDT @ {effective:f}")
        break
    position = dec(state["position"])
    if position <= 0:
        return
    for rung in sorted(strategy["sell"], key=lambda row: dec(row["price"])):
        if rung["id"] in state["sell_filled"]:
            continue
        amount = position if rung.get("all") else position * dec(rung["pct"]) / 100
        raw_in = raw_amount(amount, int(token["token_decimals"]))
        quote = quote_raw(w3, token, "sell", raw_in)
        effective, _, eligible = execution_guard(
            "sell", raw_in, quote, dec(rung["price"]),
            int(token["token_decimals"]), dec(token["slippage"])
        )
        if eligible and human(quote, 6) >= MIN_NOTIONAL:
            actual_tokens = human(raw_in, int(token["token_decimals"]))
            received = human(quote, 6)
            entry = apply_trade(state, "sell", rung["id"], actual_tokens, received, effective, None, True)
            save_state(token["id"], strategy, state, True)
            append_jsonl(DRY_STATE_DIR / "trades.jsonl", {"token": token["id"], **entry})
            log(f"{token['id']} [DRY] SELL {actual_tokens:f} tok @ {effective:f}")
        break


@contextmanager
def instance_lock(dry_run: bool):
    root = state_root(dry_run)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    path = root / "ladderbot.lock"
    handle = path.open("a+", encoding="utf-8")
    os.chmod(path, 0o600)
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        handle.close()
        raise LadderbotError(f"ya existe otra instancia para {'dry-run' if dry_run else 'real'}") from exc
    try:
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} started={now()}\n")
        handle.flush()
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


def run(dry_run: bool, once: bool) -> None:
    cfg, strategies = load_config(require_budget=True)
    tokens = cfg["tokens"]
    secrets = load_secrets(strict_permissions=not dry_run)
    w3 = get_w3(secrets, require_private_rpc=not dry_run)
    account = None
    if not dry_run:
        account = get_account(w3, secrets)
        preflight(w3, account, cfg, tokens)
    with instance_lock(dry_run):
        log(f"ladderbot v2 {'DRY-RUN' if dry_run else 'REAL'} iniciado")
        while True:
            for token in tokens:
                try:
                    if dry_run:
                        check_dry_token(w3, token, strategies[token["id"]])
                    else:
                        check_live_token(w3, account, cfg, token, strategies[token["id"]], secrets)
                except StateError:
                    raise
                except Exception as exc:  # noqa: BLE001 - aislamiento por token
                    log(f"error procesando {token['id']}: {exc}\n{traceback.format_exc()}")
                    telegram_send(secrets, f"🚨 {token['id']}: {exc}")
            if once:
                return
            time.sleep(int(cfg.get("poll_seconds", 30)))


def status(dry_run: bool) -> None:
    cfg, strategies = load_config(require_budget=False)
    secrets = load_secrets(strict_permissions=False)
    w3 = get_w3(secrets)
    for token in cfg["tokens"]:
        strategy = strategies[token["id"]]
        try:
            state = load_state(token["id"], strategy, dry_run)
        except StateError as exc:
            print(f"{token['id']}: ERROR {exc}")
            continue
        if state is None:
            print(f"{token['id']}: sin estado {'dry-run' if dry_run else 'real'}")
            continue
        try:
            quote = quote_raw(w3, token, "buy", 100_000_000)
            px, _, _ = execution_guard(
                "buy", 100_000_000, quote, Decimal(999999),
                int(token["token_decimals"]), dec(token["slippage"])
            )
            price_text = f"{px:f}"
        except Exception:  # noqa: BLE001 - status continúa sin precio
            price_text = "—"
        pending = state.get("pending")
        pending_text = f"{pending['phase']}:{pending['rung_id']}" if pending else "—"
        print(
            f"{token['id']:12} px={price_text} budget={state['budget']} "
            f"cash={state['cash_available']} pos={state['position']} avg={state['avg_cost']} "
            f"buys={len(state['buy_filled'])} sells={len(state['sell_filled'])} "
            f"pending={pending_text} {'PAUSED' if state.get('paused') else ''}"
        )


def doctor() -> None:
    cfg, strategies = load_config(require_budget=True)
    secrets = load_secrets(strict_permissions=True)
    w3 = get_w3(secrets, require_private_rpc=True)
    account = get_account(w3, secrets)
    preflight(w3, account, cfg, cfg["tokens"])
    print(f"OK config y cadena; wallet derivada: {account.address}")
    for token in cfg["tokens"]:
        state = load_state(token["id"], strategies[token["id"]], False)
        state_text = "nuevo" if state is None else "v2 válido"
        print(f"OK {token['id']}: contrato, decimales, pool y quote; estado {state_text}")


def reconcile(args) -> None:
    cfg, strategies = load_config(require_budget=True)
    token = next((row for row in cfg["tokens"] if row["id"] == args.token), None)
    if token is None:
        raise ConfigError(f"token desconocido: {args.token}")
    strategy = strategies[token["id"]]
    cash = dec(args.cash_available, "cash_available")
    avg_cost = dec(args.avg_cost, "avg_cost")
    if cash < 0 or avg_cost < 0:
        raise ConfigError("cash_available y avg_cost no pueden ser negativos")
    path = state_path(token["id"], False)
    legacy = None
    if path.exists():
        try:
            legacy = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            legacy = None
        if isinstance(legacy, dict) and legacy.get("pending"):
            old_pending = legacy["pending"]
            pending_ref = (old_pending.get("tx") or {}).get("hash") or old_pending.get(
                "order_id", "PREPARED"
            )
            if args.ack_pending != pending_ref:
                raise StateError(
                    "hay una orden pendiente; comprueba su recibo y repite con "
                    f"--ack-pending {pending_ref}"
                )
        backup = path.with_name(f"{path.name}.{int(time.time())}.bak")
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
        print(f"backup: {backup}")
    secrets = load_secrets(strict_permissions=True)
    w3 = get_w3(secrets, require_private_rpc=True)
    account = get_account(w3, secrets)
    _, _, _, _, _, token_c = contracts(w3, token)
    actual = human(balance_raw(token_c, account.address), int(token["token_decimals"]))
    state = new_state(token["id"], strategy, actual)
    state["cash_available"] = dstr(cash)
    if args.manage_current_position:
        state["position"] = dstr(actual)
        state["avg_cost"] = dstr(avg_cost)
    if isinstance(legacy, dict) and legacy.get("schema") == STATE_SCHEMA:
        state["total_spent"] = dstr(dec(legacy.get("total_spent", 0)))
        state["total_received"] = dstr(dec(legacy.get("total_received", 0)))
        state["history"] = list(legacy.get("history", []))[-1000:]
    if legacy and not args.reset_rungs:
        for side in ("buy", "sell"):
            by_price = {dec(row["price"]): row["id"] for row in strategy[side]}
            valid_ids = set(by_price.values())
            filled: list[str] = []
            for old in legacy.get(f"{side}_filled", []):
                if isinstance(old, str) and old in valid_ids:
                    if old not in filled:
                        filled.append(old)
                    continue
                try:
                    price = dec(old.get("price") if isinstance(old, dict) else old)
                    if price in by_price and by_price[price] not in filled:
                        filled.append(by_price[price])
                except ConfigError:
                    continue
            state[f"{side}_filled"] = filled
    save_state(token["id"], strategy, state)
    print(
        f"{token['id']}: reconciliado; posición gestionada={state['position']} "
        f"cash={state['cash_available']} avg_cost={state['avg_cost']}"
    )


def set_paused(token_id: str, paused: bool) -> None:
    cfg, strategies = load_config(require_budget=False)
    token = next((row for row in cfg["tokens"] if row["id"] == token_id), None)
    if token is None:
        raise ConfigError(f"token desconocido: {token_id}")
    state = load_state(token_id, strategies[token_id], False)
    if state is None:
        raise StateError(f"{token_id}: no existe estado")
    if not paused and (state.get("pending") or {}).get("phase") == "needs_reconciliation":
        raise StateError("no se puede reanudar una orden que necesita reconciliación")
    state["paused"] = paused
    save_state(token_id, strategies[token_id], state)
    print(f"{token_id}: {'pausado' if paused else 'reanudado'}")


def build_report(dry_run: bool) -> str:
    cfg, strategies = load_config(require_budget=False)
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    rows: list[dict[str, Any]] = []
    positions: list[str] = []
    for token in cfg["tokens"]:
        state = load_state(token["id"], strategies[token["id"]], dry_run)
        if state is None:
            continue
        positions.append(f"{token['id']}: {state['position']} tok; cash {state['cash_available']} USDT")
        for row in state.get("history", []):
            try:
                stamp = datetime.strptime(row["ts"], "%Y-%m-%d %H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (KeyError, ValueError):
                continue
            if stamp >= cutoff:
                rows.append(row)
    buys = [row for row in rows if row["side"] == "buy"]
    sells = [row for row in rows if row["side"] == "sell"]
    bought = sum((dec(row["usdt"]) for row in buys), Decimal(0))
    sold = sum((dec(row["usdt"]) for row in sells), Decimal(0))
    realized = sum((dec(row["realized"]) for row in sells), Decimal(0))
    return "\n".join([
        f"Informe 7 días {'DRY-RUN' if dry_run else 'REAL'}",
        f"Operaciones: {len(rows)} ({len(buys)} compras / {len(sells)} ventas)",
        f"Comprado: {bought:f} USDT · Vendido: {sold:f} USDT",
        f"Resultado realizado sin gas: {realized:f} USDT",
        *positions,
    ])


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("--dry-run", action="store_true")
    run_parser.add_argument("--once", action="store_true", help="un solo ciclo")
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--dry-run", action="store_true")
    report_parser = sub.add_parser("report")
    report_parser.add_argument("--dry-run", action="store_true")
    sub.add_parser("doctor")
    rec = sub.add_parser("reconcile")
    rec.add_argument("--token", required=True)
    rec.add_argument("--cash-available", required=True)
    rec.add_argument("--avg-cost", required=True)
    rec.add_argument("--manage-current-position", action="store_true")
    rec.add_argument("--reset-rungs", action="store_true")
    rec.add_argument("--ack-pending")
    pause = sub.add_parser("pause")
    pause.add_argument("--token", required=True)
    resume = sub.add_parser("resume")
    resume.add_argument("--token", required=True)
    args = parser.parse_args()
    try:
        if args.command == "run":
            run(args.dry_run, args.once)
        elif args.command == "status":
            status(args.dry_run)
        elif args.command == "doctor":
            doctor()
        elif args.command == "reconcile":
            reconcile(args)
        elif args.command == "pause":
            set_paused(args.token, True)
        elif args.command == "resume":
            set_paused(args.token, False)
        else:
            print(build_report(args.dry_run))
    except (LadderbotError, ConfigError, StateError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


if __name__ == "__main__":
    main()
