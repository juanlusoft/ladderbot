# Ladderbot v2.1.0

Bot de trading escalonado para **GEODNET/USDT en Polygon** (Uniswap v3), autónomo,
con systemd. Compra y vende por peldaños de precio que tú defines en un fichero.

**Empieza por [TUTORIAL.md](TUTORIAL.md).** Está escrito para copiar y pegar sin saber nada.

## Qué hay aquí

| Fichero | Para qué |
|---|---|
| `ladderbot.py` | El bot. Todos los comandos van con este fichero |
| `config.example.json` | Plantilla de configuración general (se copia a `config.json`) |
| `strategies/geodnet.example.json` | Plantilla de la escalera (se copia a `strategies/geodnet.json`) |
| `secrets/ladderbot.env.example` | Plantilla de los 5 datos privados |
| `systemd/ladderbot.service` | Servicio para que arranque solo y se levante si se cae |
| `install.sh` | Instalador: usuario, ficheros, entorno de Python, permisos y servicio |
| `requirements.lock` | Versiones fijas de las dependencias (reproducible; web3 7.16.0) |
| `ver_saldos.py` | Ver saldos, gas, allowance y precio actual |
| `ver_tx.py <hash>` | Ver el recibo de una transacción y lo que movió de verdad |
| `leer_ingresos.py [días]` | Ver a qué hora llegan los tokens de minero |
| `ajustar_saldo.py` | Ajusta presupuesto y posición al saldo real de la wallet y re-arma la escalera. Pensado para un cron diario |
| `telegram_cmd.py` | Comandos `/p`, `/version`, `/update` y `/help` |
| `precio_chart.py` | Precio real y gráfico de 24 horas |
| `updater.py` | OTA verificada con SHA-256, manifiesto, backup y rollback |

## Comandos

```bash
BOT="sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ladderbot.py"
$BOT doctor                                     # valida todo, no firma nada
$BOT reconcile --token GEODNET --cash-available 0 --avg-cost 0
$BOT run --dry-run --once                       # un ciclo simulado
$BOT run                                        # modo real (lo llama systemd)
$BOT status [--dry-run] / report / pause --token X / resume --token X
```

## Seguridad (lo que hace que no te sorprenda)

- Presupuesto **explícito**: el bot nunca adopta todo el USDT de la wallet.
- Modo simulación y estado real **separados** (uno no puede contaminar al otro).
- Una sola instancia por modo (fichero de bloqueo).
- El precio de cada peldaño es **límite duro** del swap, no una sugerencia.
- La transacción firmada se guarda **antes** de enviarla: si hay timeout, no repite el swap.
- Estado viejo, corrupto o con estrategia distinta **detiene el bot** en vez de reiniciar la escalera.
- Sin `--manage-current-position`, solo gestiona los tokens que él mismo compra: no toca lo que ya hubiera en la wallet.
- RPC con failover: PublicNode como primario y dRPC gratuito como respaldo.
- La OTA no toca `config.json`, `strategies/`, `secrets/` ni `state/`.
- `/update` se niega a actuar si hay una transacción pendiente, un hash incorrecto,
  una versión no posterior o un fichero fuera de la lista permitida.

## Actualización OTA por Telegram

El comando solo se acepta desde `TELEGRAM_CHAT_ID`:

```text
/update https://servidor/ladderbot-v2.2.0.tar.gz SHA256_DEL_PAQUETE
```

El instalador deja configurado el manifiesto oficial:
`https://raw.githubusercontent.com/juanlusoft/ladderbot/main/latest.json`.
Por tanto, normalmente basta con `/update`; también se admite la forma explícita
con URL y SHA-256.

El actualizador descarga por HTTPS, limita el tamaño, valida el SHA-256 externo y
los hashes internos, rechaza rutas peligrosas, comprueba que no haya operaciones
pendientes, guarda un backup, compila el código, ejecuta `doctor` y reactiva solo
los servicios que estaban activos. Cualquier fallo restaura automáticamente la
versión anterior.

## Comprobado

El paquete se ha probado en limpio (cuenta nueva, sin fondos, RPC público) con:
`doctor` (detecta correctamente la falta de POL para gas), `reconcile` (crea el estado),
`status` (lee el precio real de la pool) y `run --dry-run --once`. Sin firmar ninguna transacción.
