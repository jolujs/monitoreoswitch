#!/usr/bin/env python3
"""
Monitor de stock: Nintendo Switch 2 - The Legend of Zelda 40th Anniversary Edition
Tienda: Mercado Libre Chile (producto de catálogo MLC79375219)

Cuando el producto pasa a "disponible" avisa por:
  1. Llamada de Telegram vía CallMeBot (https://www.callmebot.com/blog/telegram-phone-call-using-api/)
  2. Mensaje de tu propio bot de Telegram con el link de compra

Modos:
  python monitor_stock.py              -> bucle infinito (para tu PC), revisa cada INTERVALO_SEG
  python monitor_stock.py --once       -> una sola revisión (para GitHub Actions / cron)
  python monitor_stock.py --check      -> solo imprime el estado, no notifica
  python monitor_stock.py --test       -> manda una llamada y un mensaje de prueba

Configuración por variables de entorno (o archivo .env junto al script):
  TELEGRAM_BOT_TOKEN   token del bot (de @BotFather)
  TELEGRAM_CHAT_ID     tu chat id (de @userinfobot)
  CALLMEBOT_USER       tu usuario de Telegram con @ (ej: @jose123)
  ML_CLIENT_ID         ID de tu app en developers.mercadolibre.cl (API oficial)
  ML_CLIENT_SECRET     Clave secreta de esa app
  ML_ACCESS_TOKEN      (alternativa) token ya generado, vence en ~6 horas
  SELLER_ID            (opcional) avisar solo si vende este vendedor (tienda Nintendo)
  OFFICIAL_STORE_ID    (opcional) avisar solo si vende esta tienda oficial
  ITEM_ID              (pruebas) revisar una publicación puntual, ej. MLC4537055428
  PRODUCT_ID           (opcional) producto a vigilar, código MLC... después de /p/ (por defecto Zelda)
  PRODUCT_NAME         (opcional) nombre que aparece en el mensaje y la llamada
  MENSAJES_POR_AVISO   (opcional) mensajes de Telegram por aviso, por defecto 5
  RESUMEN_CADA_MIN     (opcional) cada cuántos minutos manda el status, por defecto 60
  INTERVALO_SEG        (opcional) segundos entre revisiones, por defecto 300 (mínimo 120)
"""

import html as htmllib
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

def _cargar_env_temprano():
    """Lee .env antes de definir las constantes, para que PRODUCT_ID etc. se tomen de ahí."""
    env = Path(__file__).with_name(".env")
    if not env.exists():
        return
    for linea in env.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if linea and not linea.startswith("#") and "=" in linea:
            k, v = linea.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_cargar_env_temprano()

# Producto a vigilar: el código MLC... que aparece después de /p/ en el link
PRODUCT_ID = os.getenv("PRODUCT_ID", "MLC79375219").strip().upper()
PRODUCT_NAME = os.getenv("PRODUCT_NAME", "Nintendo Switch 2 - Zelda 40th Anniversary Edition")
PRODUCT_URL = os.getenv("PRODUCT_URL") or f"https://www.mercadolibre.cl/p/{PRODUCT_ID}"
DIAGNOSTICO = "--diag" in sys.argv
STATE_FILE = Path(__file__).with_name(".estado_stock.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)

DISPONIBLE = "DISPONIBLE"
AGOTADO = "AGOTADO"
DESCONOCIDO = "DESCONOCIDO"

# Textos que Mercado Libre muestra cuando no se puede comprar
TEXTOS_AGOTADO = [
    "sin stock",
    "publicación pausada",
    "publicacion pausada",
    "producto no disponible",
    "este producto no está disponible",
    "no hay vendedores",
    "agotado",
    "sin unidades",
    "avisarme cuando",
    "notificarme",
]
# Textos del botón de compra cuando sí se puede comprar
TEXTOS_DISPONIBLE = ["comprar ahora", "agregar al carrito"]


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


def cargar_env():
    env = Path(__file__).with_name(".env")
    if not env.exists():
        return
    for linea in env.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        k, v = linea.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def http_get(url, headers=None, timeout=25):
    h = {"User-Agent": USER_AGENT, "Accept-Language": "es-CL,es;q=0.9"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------- detección

_TOKEN_CACHE = {}


def obtener_token_ml():
    """Token de la API oficial. Usa ML_ACCESS_TOKEN si existe; si no, lo pide con
    ML_CLIENT_ID + ML_CLIENT_SECRET (app de developers.mercadolibre.cl) y lo guarda
    solo en memoria (nunca en disco) hasta que vence (~6 horas)."""
    if os.getenv("ML_ACCESS_TOKEN"):
        return os.getenv("ML_ACCESS_TOKEN"), None
    cid, secret = os.getenv("ML_CLIENT_ID"), os.getenv("ML_CLIENT_SECRET")
    if not cid or not secret:
        return None, "sin credenciales de la API (ML_CLIENT_ID / ML_CLIENT_SECRET)"
    cache = _TOKEN_CACHE
    if cache.get("ml_token") and cache.get("ml_token_vence", 0) > time.time() + 60:
        return cache["ml_token"], None
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials", "client_id": cid, "client_secret": secret,
    }).encode()
    req = urllib.request.Request(
        "https://api.mercadolibre.com/oauth/token", data=data,
        headers={"Accept": "application/json",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            resp = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return None, f"no se pudo obtener token: HTTP {e.code} {e.read().decode()[:200]}"
    except Exception as e:  # noqa: BLE001
        return None, f"no se pudo obtener token: {e}"
    cache["ml_token"] = resp["access_token"]
    cache["ml_token_vence"] = time.time() + int(resp.get("expires_in", 21600))
    return cache["ml_token"], None


def api_get(ruta, token):
    _, body = http_get(
        "https://api.mercadolibre.com" + ruta,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    return json.loads(body)


def _tiene_stock(item):
    # Si la API no informa la cantidad, una publicación activa se considera comprable
    cantidad = item.get("available_quantity")
    return cantidad is None or cantidad > 0


def _del_vendedor(item):
    """Filtra por la tienda oficial si se configuró SELLER_ID u OFFICIAL_STORE_ID."""
    # 550063615 = tienda oficial Nintendo Chile (tienda oficial 1124). SELLER_ID= vacío lo desactiva
    seller, tienda = os.getenv("SELLER_ID", "550063615"), os.getenv("OFFICIAL_STORE_ID")
    if seller and str(item.get("seller_id")) != seller:
        return False
    if tienda and str(item.get("official_store_id")) != tienda:
        return False
    return True


def _describir(item):
    return (f"precio {item.get('price')}, vendedor {item.get('seller_id')}, "
            f"tienda oficial {item.get('official_store_id')}, publicación {item.get('item_id') or item.get('id')}")


def estado_por_item(item_id, token):
    """Publicación individual (links /up/... o con item_id MLC...)."""
    try:
        item = api_get(f"/items/{item_id}", token)
    except urllib.error.HTTPError as e:
        return DESCONOCIDO, f"API /items/{item_id}: HTTP {e.code} {e.read().decode()[:200]}"
    except Exception as e:  # noqa: BLE001
        return DESCONOCIDO, f"API /items falló: {e}"
    detalle = f"API: estado {item.get('status')}, stock {item.get('available_quantity')}, {_describir(item)}"
    if item.get("status") == "active" and _tiene_stock(item):
        return DISPONIBLE, detalle
    return AGOTADO, detalle


def estado_por_api():
    """API oficial de Mercado Libre (la forma permitida de consultar)."""
    token, error = obtener_token_ml()
    if not token:
        return DESCONOCIDO, error
    if os.getenv("ITEM_ID"):
        return estado_por_item(os.getenv("ITEM_ID"), token)
    try:
        data = api_get(f"/products/{PRODUCT_ID}", token)
    except urllib.error.HTTPError as e:
        return DESCONOCIDO, f"API /products: HTTP {e.code} {e.read().decode()[:200]}"
    except Exception as e:  # noqa: BLE001
        return DESCONOCIDO, f"API falló: {e}"
    ganador = data.get("buy_box_winner")
    if ganador and _tiene_stock(ganador) and _del_vendedor(ganador):
        return DISPONIBLE, f"API (ganador): {_describir(ganador)}"
    # Revisa todas las publicaciones activas del producto
    try:
        items = api_get(f"/products/{PRODUCT_ID}/items", token).get("results", [])
    except urllib.error.HTTPError as e:
        if e.code == 404:  # Mercado Libre responde 404 cuando no hay vendedores activos
            return AGOTADO, "API: sin publicaciones activas"
        return DESCONOCIDO, f"API /products/items: HTTP {e.code}"
    except Exception as e:  # noqa: BLE001
        return DESCONOCIDO, f"API /products/items falló: {e}"
    validos = [i for i in items if _tiene_stock(i) and _del_vendedor(i)]
    if validos:
        return DISPONIBLE, f"API: {len(validos)} publicación(es) con stock. {_describir(validos[0])}"
    otros = len(items)
    return AGOTADO, f"API: {otros} publicación(es) activas, ninguna del vendedor filtrado con stock"


def estado_por_html(html):
    """Analiza la página del producto. Devuelve (estado, detalle)."""
    if "suspicious-traffic" in html or "account-verification" in html:
        return DESCONOCIDO, ("Mercado Libre bloqueó la consulta (página de verificación de "
                             "tráfico). Usa la API oficial: ML_CLIENT_ID y ML_CLIENT_SECRET")
    # 1) JSON-LD de schema.org: la señal más estable
    for bloque in re.findall(
        r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S | re.I
    ):
        try:
            data = json.loads(bloque)
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            offers = item.get("offers") if isinstance(item, dict) else None
            if isinstance(offers, list):
                offers = offers[0] if offers else None
            if isinstance(offers, dict) and offers.get("availability"):
                disp = offers["availability"].lower()
                precio = offers.get("price")
                if "instock" in disp or "limitedavailability" in disp:
                    return DISPONIBLE, f"JSON-LD InStock, precio {precio}"
                if "outofstock" in disp or "soldout" in disp or "discontinued" in disp:
                    return AGOTADO, f"JSON-LD {offers['availability']}"

    # 1b) schema.org fuera de JSON-LD (por ejemplo en el estado inicial de la página)
    m = re.search(r"schema\.org\\?/(InStock|OutOfStock|SoldOut|LimitedAvailability)", html, re.I)
    if m:
        valor = m.group(1).lower()
        if valor in ("instock", "limitedavailability"):
            return DISPONIBLE, f"schema.org {m.group(1)}"
        return AGOTADO, f"schema.org {m.group(1)}"

    # 2) Texto visible
    texto = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    texto = htmllib.unescape(re.sub(r"<[^>]+>", " ", texto)).lower()
    texto = re.sub(r"\s+", " ", texto)
    agotado = next((t for t in TEXTOS_AGOTADO if t in texto), None)
    disponible = next((t for t in TEXTOS_DISPONIBLE if t in texto), None)
    if disponible and not agotado:
        return DISPONIBLE, f"texto '{disponible}'"
    if agotado:
        return AGOTADO, f"texto '{agotado}'"
    return DESCONOCIDO, "no se encontró señal de stock (¿cambió la página o hubo bloqueo?)"


def revisar():
    """Solo usa la API oficial. Los términos de Mercado Libre (cláusula 7.6) prohíben
    usar robots o scraping sobre el sitio, así que el script ya no lee la página web.
    estado_por_html() queda solo para analizar a mano un HTML guardado."""
    return estado_por_api()


def diagnosticar(html):
    """Guarda la página recibida y muestra pistas para ajustar la detección."""
    archivo = Path(__file__).with_name("pagina_ml.html")
    archivo.write_text(html, encoding="utf-8")
    titulo = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    log(f"Diagnóstico: {len(html)} caracteres, título: "
        f"{htmllib.unescape(titulo.group(1)).strip() if titulo else '(sin título)'}")
    bajo = html.lower()
    for clave in ["availability", "available_quantity", "comprar ahora", "agregar al carrito",
                  "sin stock", "no disponible", "pausada", "captcha", "account-verification",
                  "login", "suspicious", "MLC79375219"]:
        i = bajo.find(clave.lower())
        if i >= 0:
            trozo = re.sub(r"\s+", " ", html[max(0, i - 80): i + 120])
            log(f"  '{clave}': ...{trozo}...")
        else:
            log(f"  '{clave}': no aparece")
    log(f"Página guardada en {archivo}")


# ---------------------------------------------------------------- avisos

def llamar_callmebot(texto):
    user = (os.getenv("CALLMEBOT_USER") or "").strip()
    if not user:
        log("CALLMEBOT_USER no configurado, se omite la llamada")
        return False
    if not user.startswith(("@", "+")):
        user = "@" + user  # CallMeBot exige @usuario o +telefono
    url = "https://api.callmebot.com/start.php?" + urllib.parse.urlencode(
        {"user": user, "text": texto, "lang": "es-ES-Standard-A", "rpt": "3"}
    )
    intentos = int(os.getenv("CALLMEBOT_INTENTOS", "4"))
    for intento in range(1, intentos + 1):
        try:
            _, body = http_get(url, timeout=90)
            visible = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", body, flags=re.S | re.I)
            respuesta = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", visible)).strip()
        except Exception as e:  # noqa: BLE001
            respuesta = f"falló la conexión: {e}"
        log(f"CallMeBot (intento {intento}/{intentos}): {respuesta[:500]}")
        bajo = respuesta.lower()
        if "error" not in bajo and "skipped" not in bajo and "falló" not in bajo:
            return True
        if "wrong format" in bajo or "not authorized" in bajo or "authorize" in bajo and "autorization ok" not in bajo:
            return False  # reintentar no sirve: falta configurar o autorizar
        if intento < intentos:
            # CallMeBot no permite 2 llamadas al mismo usuario en menos de 65 s
            espera = 75 * intento
            log(f"CallMeBot ocupado, reintento en {espera}s")
            time.sleep(espera)
    mensaje_telegram(f"📞 No pude llamarte (CallMeBot saturado). ¡Hay stock! Compra aquí: {PRODUCT_URL}")
    return False


def mensaje_telegram(texto):
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat:
        log("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID no configurados, se omite el mensaje")
        return False
    data = urllib.parse.urlencode({"chat_id": chat, "text": texto}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            log(f"Telegram: HTTP {r.status}")
        return True
    except Exception as e:  # noqa: BLE001
        log(f"Telegram falló: {e}")
        return False


def avisar(detalle):
    """1 mensaje -> empieza la llamada -> mientras llama, el resto de los mensajes."""
    cantidad = max(1, int(os.getenv("MENSAJES_POR_AVISO", "5")))

    def texto(n):
        return f"🚨 ({n}/{cantidad}) ¡HAY STOCK! {PRODUCT_NAME}\n{detalle}\nCompra aquí: {PRODUCT_URL}"

    mensaje_telegram(texto(1))
    llamada = threading.Thread(
        target=llamar_callmebot,
        args=(f"Atención. {PRODUCT_NAME} tiene stock en Mercado Libre. Entra a comprar ya.",),
        daemon=True,
    )
    llamada.start()
    for n in range(2, cantidad + 1):
        time.sleep(3)  # espaciados para que el teléfono suene varias veces
        mensaje_telegram(texto(n))
    llamada.join()  # espera a que terminen los reintentos de la llamada


# ---------------------------------------------------------------- estado

def leer_estado():
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:  # noqa: BLE001
        return {}


def guardar_estado(d):
    STATE_FILE.write_text(json.dumps(d))


def ciclo(notificar=True):
    estado, detalle = revisar()
    log(f"{estado}: {detalle}")
    if not notificar:
        return estado
    previo = leer_estado()
    ahora = time.time()
    if estado == DISPONIBLE:
        # Avisa (mensajes + llamada) en CADA revisión mientras haya stock
        avisar(detalle)
        previo["ultimo_aviso"] = ahora
    elif estado == DESCONOCIDO:
        previo["fallos"] = previo.get("fallos", 0) + 1
        if previo["fallos"] == 6:  # ~30 min sin poder leer la página
            mensaje_telegram(f"⚠️ El monitor no puede leer la página hace un rato: {detalle}")
    if estado != DESCONOCIDO:
        previo["fallos"] = 0
        previo["estado"] = estado
    registrar_para_resumen(previo, estado, ahora)
    guardar_estado(previo)
    return estado


def registrar_para_resumen(previo, estado, ahora):
    """Cuenta las revisiones y cada RESUMEN_CADA_MIN manda un status por Telegram."""
    r = previo.setdefault("resumen", {"inicio": ahora, "revisiones": 0, "disponible": [], "fallos": 0})
    r["revisiones"] += 1
    if estado == DISPONIBLE:
        r["disponible"].append(ahora)
    elif estado == DESCONOCIDO:
        r["fallos"] += 1
    minutos = max(1, int(os.getenv("RESUMEN_CADA_MIN", "60")))
    if ahora - r["inicio"] < minutos * 60:
        return
    hora = lambda t: datetime.fromtimestamp(t).strftime("%H:%M")  # noqa: E731
    veces = len(r["disponible"])
    if veces == 0:
        linea = "❌ No estuvo disponible ninguna vez."
    else:
        horas = ", ".join(hora(t) for t in r["disponible"][:12])
        linea = f"✅ Estuvo disponible {veces} {'vez' if veces == 1 else 'veces'} ({horas})."
    texto = (f"📊 Status {PRODUCT_NAME}\n"
             f"Período {hora(r['inicio'])} a {hora(ahora)}: {r['revisiones']} revisiones.\n{linea}")
    if r["fallos"]:
        texto += f"\n⚠️ {r['fallos']} revisiones fallaron (sin respuesta de la API)."
    texto += f"\n🔗 {PRODUCT_URL}"
    mensaje_telegram(texto)
    previo["resumen"] = {"inicio": ahora, "revisiones": 0, "disponible": [], "fallos": 0}


def main():
    cargar_env()
    args = set(sys.argv[1:])
    if "--test" in args:
        mensaje_telegram(f"✅ Prueba del monitor de {PRODUCT_NAME}. Si te llega esto, los avisos funcionan.")
        llamar_callmebot("Prueba del monitor de stock. Si escuchas esto, funciona.")
        return
    if "--check" in args:
        ciclo(notificar=False)
        return
    if "--once" in args:
        ciclo()
        return
    intervalo = max(120, int(os.getenv("INTERVALO_SEG", "300")))
    log(f"Monitoreando {PRODUCT_URL} cada ~{intervalo}s. Ctrl+C para salir.")
    while True:
        try:
            ciclo()
        except Exception as e:  # noqa: BLE001
            log(f"Error inesperado: {e}")
        time.sleep(intervalo + random.randint(0, 30))


if __name__ == "__main__":
    main()
