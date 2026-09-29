# Ladderbot v2: puesta en marcha para gente que no quiere pensar

Bot que compra y vende **GEODNET** contra **USDT** en Polygon, solo, por peldaños de precio.
Tú defines la escalera; él espera y ejecuta. No predice nada, no hace trading "inteligente":
hace exactamente lo que pone tu fichero de estrategia.

Este tutorial asume que **no sabes nada** de Linux ni de cripto. Se copia y se pega.

---

## 0. Antes de empezar: léete esto (2 minutos, importante)

- El bot **mueve dinero real**. Cada compra y cada venta es una transacción de verdad, con comisión.
- El bot **no te avisa para pedir permiso**. Si toca un peldaño, opera. Eso es lo que le pides.
- La **clave privada** de la wallet la tendrás escrita en un fichero del servidor. Si alguien entra ahí, se lleva el dinero. Por eso: **wallet nueva y dedicada**, nunca la tuya de siempre.
- Si el precio se hunde, tu escalera de compra irá comprando por tramos. Es lo que quieres cuando baja, pero **gasta dinero**. Ponle un presupuesto que puedas perder.
- Con `--dry-run` **no firma ni envía nada**. Usa eso todo lo que quieras. Es gratis.

---

## 1. Lo que necesitas antes de tocar nada

| Qué | Por qué | Cómo |
|---|---|---|
| Una máquina Linux encendida 24/7 | El bot tiene que estar mirando el precio | Un VPS barato, un mini PC o un contenedor. No vale un portátil que apagas |
| Python 3.12 | Las dependencias están fijadas para esa versión | `python3 --version`. Si no es 3.12: `apt install python3.12 python3.12-venv` |
| Una wallet **nueva** | Si te roban la clave, pierdes solo lo que hay ahí | MetaMask, "Crear cuenta nueva". Apunta la frase semilla en papel |
| USDT en Polygon en esa wallet | Es lo que el bot usa para comprar | Envía USDT **por la red Polygon**, no por Ethereum |
| Un poco de POL (antes MATIC) | Es la gasolina de las transacciones | Con 1 o 2 € tienes para mucho |
| Un RPC privado gratis | Es la "línea telefónica" al blockchain. Los públicos se cortan y el bot se niega a usarlos en modo real | Alchemy o Infura, plan gratuito. Te dan una URL larga con una clave dentro |
| (Opcional) Bot de Telegram + tu chat id | Para que te avise de compras y ventas | Se explica en el paso 3 |

---

## 2. Instalar (copiar y pegar)

Descomprime el paquete y ejecuta el instalador:

```bash
tar -xzf ladderbot-v2-20260919.tar.gz
cd ladderbot-v2
sudo bash install.sh
```

El instalador hace todo esto, no tienes que entenderlo:

1. Crea un usuario del sistema llamado `ladderbot` (el bot no corre como root).
2. Copia el programa a `/opt/ladderbot`.
3. Crea su entorno de Python e instala las dependencias (web3).
4. Crea los ficheros de configuración a partir de las plantillas y cierra los permisos.
5. Instala el servicio, pero **no lo arranca**.

Al terminar te imprime los 5 pasos que quedan. Son los de este tutorial.

---

## 3. Rellenar los datos secretos

```bash
sudo nano /opt/ladderbot/secrets/ladderbot.env
```

Tiene que quedar así, con tus valores:

```dotenv
POLYGON_RPC=https://polygon-mainnet.g.alchemy.com/v2/XXXXXXXXXXXX
POLYGON_PRIVATE_KEY=0x1234...tu_clave...
POLYGON_ADDRESS=0xTuDireccionPublica
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

**Dónde saca cada cosa:**

- `POLYGON_RPC`: es la "línea telefónica" al blockchain. **No hace falta registrarse en ningún sitio**: estas dos funcionan sin cuenta y sin clave, y están probadas con el propio código del bot (`2026-09-19`):

  ```dotenv
  POLYGON_RPC=https://polygon-bor-rpc.publicnode.com
  POLYGON_RPC=https://polygon.gateway.tenderly.co
  ```

  La primera es la recomendada (es la que usa el bot que ya está funcionando) y aguanta consultas largas de historial. La segunda vale como recambio si la primera se cae: ponla en la misma línea y reinicia el servicio. No hay que cambiar nada más.

  Si algún día quieres una con clave propia (más estable, porque las gratuitas limitan peticiones), Alchemy es una opción:
  1. Entra en `https://dashboard.alchemy.com/signup` y crea la cuenta (email y contraseña, nada más).
  2. Si acabas de registrarte **ya tienes una app y una clave por defecto**: puedes usar esa y saltarte los dos pasos siguientes.
  3. Para hacer una tuya: en el menú del equipo abre **Team Overview**, ve a la pestaña **Apps** y pulsa **Create new app**. Ponle un nombre y marca la cadena **Polygon** (Mainnet).
  4. Al crearla te lleva a la página de la app. **Arriba a la derecha está la API key**: cópiala.
  5. Abre la pestaña **Endpoints** y copia la URL **HTTP** de Polygon Mainnet. Tiene esta pinta:
     `https://polygon-mainnet.g.alchemy.com/v2/TU_API_KEY`
  6. Pega esa URL **entera** en `POLYGON_RPC`, con el `https://` y sin espacios.
  - Ojo con la opción de **allowlist de IPs**: si la activas, tendrás que añadir la IP de tu servidor (la ves con `curl -s ifconfig.me`) o el bot no podrá conectar.
  - Otros proveedores con clave propia valen igual: Infura, QuickNode, dRPC. Cualquier URL que empiece por `https://` y devuelva datos de Polygon sirve.
- `POLYGON_PRIVATE_KEY`: en MetaMask, menú de la cuenta, "Detalles de la cuenta", "Mostrar clave privada". **Cópiala sin espacios**, empieza por `0x` y tiene 66 caracteres.
- `POLYGON_ADDRESS`: la dirección pública de esa misma cuenta (la que empieza por `0x` y ves siempre).
- Telegram (opcional): habla con `@BotFather`, crea un bot, copia el token; y para el chat id, escribe a tu bot y mira `https://api.telegram.org/bot<TU_TOKEN>/getUpdates`.

Cierra los permisos, que si no el bot se niega a arrancar:

```bash
sudo chmod 700 /opt/ladderbot/secrets
sudo chmod 600 /opt/ladderbot/secrets/ladderbot.env
```

**Norma de oro:** ese fichero no se copia a ningún chat, ni a git, ni a una nota en la nube.

---

## 4. Definir la escalera (el fichero de estrategia)

```bash
sudo nano /opt/ladderbot/strategies/geodnet.json
```

Un ejemplo, para que se entienda:

```json
{
  "budget": "200",
  "buy": [
    { "id": "buy_020", "price": "0.20", "pct": "50" },
    { "id": "buy_018", "price": "0.18", "pct": "50" }
  ],
  "sell": [
    { "id": "sell_025", "price": "0.25", "pct": "50" },
    { "id": "sell_all", "price": "0.30", "all": true }
  ]
}
```

Qué significa cada cosa:

- **`budget`**: el dinero máximo que el bot puede gastar comprando, en USDT. Empieza en `"0"` y **doctor se niega a arrancar** así, a propósito: obliga a que pongas una cifra pensada.
- **`buy`**: peldaños de compra. `price` es el precio al que quiere comprar, `pct` el porcentaje del presupuesto que suelta en ese peldaño. En el ejemplo: si el precio baja a 0,20 suelta la mitad del presupuesto; si baja a 0,18, la otra mitad.
- **`sell`**: peldaños de venta. `pct` es el porcentaje **de la posición que tiene** (no del presupuesto). `"all": true` vende todo lo que quede.
- **`id`**: nombre de cada peldaño. Sirve para el estado y los informes. No repitas ids.
- Los porcentajes de compra deberían sumar 100. Los de venta, ir dejando algo para el siguiente.

Cosas que conviene saber:

- El precio de un peldaño es un **límite duro**: el bot nunca vende por debajo de ese precio ni "casi" lo toca. O está, o no está.
- Un peldaño de venta de 10 USDT solo se ejecuta si esa venta vale **más de 2 USDT** (mínimo del exchange). Si tu posición es pequeña, verás "venta espera" y es normal.
- Si no hay ningún peldaño activo (el precio está entre tu compra más alta y tu venta más baja), el bot **espera y no hace nada**. Es su estado habitual.
- Si cambias este fichero después de haber arrancado, el bot se para con "la estrategia cambió; revisa y ejecuta reconcile". Es una red de seguridad, no un error: ver el punto 6.

---

## 5. Probar sin gastar un céntimo

**Antes de nada:** la sección 4 tiene que estar hecha y el `budget` tiene que ser mayor que 0.
Si lo dejas en `"0"`, cualquiera de estos comandos te dirá
"configura un budget USDT positivo antes de ejecutar" y no pasará de ahí. Es a propósito.

Los tres comandos, en este orden. Cópialos tal cual (ojo a las dos rayas de `--`):

```bash
# 1) Revisa que todo está bien: claves, red, contratos, permisos. NO firma nada.
sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ladderbot.py doctor

# 2) Crea el estado inicial del bot (sin tocar la wallet).
sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ladderbot.py reconcile \
  --token GEODNET --cash-available 0 --avg-cost 0

# 3) Un ciclo completo en simulación: mira el precio real y te cuenta qué HARÍA.
sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ladderbot.py run --dry-run --once
```

- **`doctor`** tiene que terminar en verde. Si algo está mal (clave con espacios, permisos abiertos, red equivocada), lo dice y ahí se acaba.
- **`reconcile`** es "aquí empieza la cuenta". Con `--cash-available 0` le dices: de momento no gastes nada. Más adelante le pondrás el dinero real.
- **`run --dry-run --once`** no firma ni envía. Puedes repetirlo mil veces.

Para ver cómo va la simulación:

```bash
sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ladderbot.py status --dry-run
```

---

## 6. Encenderlo de verdad

Solo cuando los tres comandos del punto 5 salgan bien:

```bash
sudo systemctl enable --now ladderbot
```

Comprobar que está vivo y ver lo que hace en directo:

```bash
systemctl status ladderbot
sudo journalctl -u ladderbot -f      # Ctrl+C para salir
```

Para pararlo, pausarlo o reanudarlo:

```bash
sudo systemctl stop ladderbot
sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ladderbot.py pause  --token GEODNET
sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ladderbot.py resume --token GEODNET
```

**Reconciliar** (el comando que más vas a necesitar). Se usa después de cambiar la estrategia,
de meter o sacar tokens a mano, o de resolver una transacción pendiente:

```bash
# Le dices cuánto USDT puede gastar y a qué coste medio tienes lo que ya hay
sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ladderbot.py reconcile \
  --token GEODNET --cash-available 100 --avg-cost 0.22
```

Opciones que conviene conocer:

- `--manage-current-position`: el bot pasa a gestionar **también los GEOD que ya están en la wallet**. **Sin este flag, el bot solo vende lo que él mismo compra** y no toca lo que hubiera antes. Es la opción más conservadora y por eso es la de por defecto.
- `--avg-cost`: solo sirve para el informe de ganancias. Da igual para cuándo compra o vende.
- `--reset-rungs`: borra el histórico de peldaños ya usados y los vuelve a armar todos.

---

## 7. Qué mirar cada día (10 segundos)

```bash
# Resumen del bot: presupuesto, lo que tiene, peldaños usados y últimos movimientos
sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ladderbot.py status
sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ladderbot.py report

# Dinero de verdad en la wallet: saldos, gas, precio actual
sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ver_saldos.py

# Comprobar una transacción concreta (el hash lo da el bot en sus avisos)
sudo -u ladderbot /opt/ladderbot/.venv/bin/python /opt/ladderbot/ver_tx.py 0xhash
```

---

## 8. Cuando algo va mal (los 9 sustos habituales)

| Lo que ves | Qué pasa | Qué hacer |
|---|---|---|
| `tiene permisos 0640; exige chmod 600` | Los secretos están demasiado abiertos | `chmod 700 /opt/ladderbot/secrets` y `chmod 600 /opt/ladderbot/secrets/ladderbot.env` |
| `no existe estado` | Nunca has reconciliado | Ejecuta el `reconcile` del punto 5 |
| `la estrategia cambió; revisa y ejecuta reconcile` | Editaste el JSON de la escalera | Vuelve a hacer `reconcile` |
| `doctor` se queja del presupuesto | `budget` está en `"0"` | Pon una cifra real en `strategies/geodnet.json` |
| `No matching distribution found` al instalar | Tu Python no es 3.12 | `apt install python3.12 python3.12-venv` y repite `install.sh` |
| `wallet sin POL para pagar gas` | La wallet no tiene POL/MATIC | Manda 1 o 2 € de POL **por la red Polygon** a esa dirección |
| Un **traceback enorme** acabando en `HTTPError: 401 Client Error` | El RPC no vale: URL mal copiada, clave caducada o un RPC público que pide autenticación | Coge otra vez la URL en Alchemy/Infura y pégala completa en `secrets/ladderbot.env` |
| `configura un budget USDT positivo antes de ejecutar` | `budget` sigue en `"0"` | Pon tu cifra en `strategies/geodnet.json` |
| No compra nunca | No hay peldaño tocado: el precio está entre tu compra más alta y tu venta más baja | Nada. Es su estado normal |
| `venta ... espera (< 2 USDT)` | El 10% de tu posición vale menos de 2 USDT | Espera a tener más, o vende un porcentaje mayor |
| `needs_reconciliation` | Una transacción quedó a medias | Mira el hash en Polygonscan, espera a que confirme y vuelve a reconciliar. **El bot no repite el swap solo** |
| `command not found: sudo` en un LXC | Ese contenedor no tiene sudo | `runuser -u ladderbot -- /opt/ladderbot/.venv/bin/python ...` |

Y una que no es un error: **no hay `telegram_bot_token` dentro de `config.json`**. El token va siempre en el fichero de secretos. Si lo metes en `config.json`, el bot se niega a cargar la configuración a propósito.

## Actualizar por OTA desde Telegram

La versión 2.1.1 instala `ladderbot-update.path`, un vigilante root separado del bot
de Telegram. El proceso de Telegram nunca obtiene privilegios ni sobrescribe código:
solo deja una solicitud de actualización en `state/`.

Con un paquete y su SHA-256:

```text
/update https://servidor/ladderbot-v2.2.0.tar.gz 0123...cdef
```

Para usar simplemente `/update`, el paquete ya configura esta URL HTTPS:

```dotenv
UPDATE_MANIFEST_URL=https://raw.githubusercontent.com/juanlusoft/ladderbot/main/latest.json
```

Formato de `latest.json`:

```json
{"version":"2.2.0","url":"https://servidor/ladderbot-v2.2.0.tar.gz","sha256":"..."}
```

La OTA se cancela si existe una transacción pendiente. Antes de reemplazar código
verifica dos capas de hashes y las rutas del archivo. Conserva configuración,
estrategia, secretos y estado. Después hace `doctor`; si falla, restaura el backup
y vuelve a arrancar la versión anterior.

---

## 9. Desinstalar

```bash
sudo systemctl disable --now ladderbot ladderbot-telegram ladderbot-update.path
sudo rm -f /etc/systemd/system/ladderbot.service \
  /etc/systemd/system/ladderbot-telegram.service \
  /etc/systemd/system/ladderbot-update.service \
  /etc/systemd/system/ladderbot-update.path
sudo systemctl daemon-reload
sudo rm -rf /opt/ladderbot
sudo userdel -r ladderbot
```

Antes de borrar nada, **copia la carpeta `/opt/ladderbot/secrets` y `/opt/ladderbot/state`**: ahí está la clave y el historial de operaciones.

---

## 10. Las cuatro normas de oro

1. **Wallet dedicada.** Si esa clave se filtra, se va lo que haya ahí y nada más.
2. **La clave privada vive solo en `/opt/ladderbot/secrets/ladderbot.env`.** No se pega en chats, ni en capturas, ni en repositorios.
3. **Presupuesto que puedas perder.** El bot ejecuta tu plan aunque el mercado se caiga.
4. **Ante la duda, `--dry-run` y `doctor`.** Son gratis y no firman nada.
