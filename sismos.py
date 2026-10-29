"""
Noether Sismos — alertas de sismos para Discord.

Cada ejecución:
  1. Consulta al USGS los sismos de magnitud >= 4 de las últimas horas (todo el mundo).
  2. Descarta los que ya se publicaron (se recuerdan en estado.json).
  3. Publica cada sismo nuevo en el webhook de Discord, del más antiguo al más reciente.
  4. Menciona en cascada los roles M5..M9: un sismo de 7.3 menciona M7, M6 y M5.

La URL del webhook se lee de la variable de entorno DISCORD_WEBHOOK
(en GitHub Actions viene del secret del mismo nombre).
"""

import json
import math
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# ---------------------------------------------------------------- configuración

# Magnitud mínima para publicar en el canal.
MAGNITUD_MINIMA = 4.0

# IDs de los roles de Discord (clic derecho sobre el rol -> Copiar ID, con el
# Modo desarrollador activado). Deja "" en los que no existan todavía.
ROLES = {
    5: "1565511730623156254",   # M5
    6: "1565511788198498335",   # M6
    7: "1565511845211807875",   # M7
    8: "1565511898995236894",   # M8
    9: "1565511938681741393",   # M9
}

# Cuántas horas hacia atrás se consulta en cada ejecución. Debe ser bastante
# mayor que el intervalo del cron, para cubrir los retrasos de GitHub Actions.
VENTANA_HORAS = 6

# Cuántos IDs de sismos se recuerdan para no repetir publicaciones.
MAX_RECORDADOS = 500

ARCHIVO_ESTADO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "estado.json")
USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"

# ---------------------------------------------------------------- estado


def cargar_estado():
    try:
        with open(ARCHIVO_ESTADO, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None  # primera ejecución


def guardar_estado(ids_vistos):
    recientes = ids_vistos[-MAX_RECORDADOS:]
    with open(ARCHIVO_ESTADO, "w", encoding="utf-8") as f:
        json.dump({"vistos": recientes}, f, indent=1)
        f.write("\n")


# ---------------------------------------------------------------- datos


def consultar_usgs():
    inicio = datetime.now(timezone.utc) - timedelta(hours=VENTANA_HORAS)
    params = {
        "format": "geojson",
        "starttime": inicio.strftime("%Y-%m-%dT%H:%M:%S"),
        "minmagnitude": MAGNITUD_MINIMA,
        "orderby": "time-asc",
    }
    for intento in range(3):
        try:
            r = requests.get(USGS_URL, params=params, timeout=30)
            r.raise_for_status()
            return r.json()["features"]
        except (requests.RequestException, ValueError, KeyError) as e:
            print(f"Error consultando USGS (intento {intento + 1}): {e}")
            time.sleep(5)
    sys.exit(1)


# ---------------------------------------------------------------- mensaje


def roles_a_mencionar(magnitud):
    """Cascada: todos los roles cuyo umbral es <= magnitud (floor, nunca redondeo)."""
    nivel = math.floor(magnitud)
    return [rid for umbral, rid in sorted(ROLES.items()) if umbral <= nivel and rid]


def color_por_magnitud(m):
    if m >= 7:
        return 0xC0392B   # rojo
    if m >= 6:
        return 0xE67E22   # naranja
    if m >= 5:
        return 0xD9B44A   # oro
    return 0x5B7DB1       # azul


def texto_magnitud(m):
    """Muestra la magnitud tal como la reporta el USGS (6.95 no se convierte en 7.0)."""
    t = f"{m:.2f}".rstrip("0")
    return t + "0" if t.endswith(".") else t


def construir_mensaje(sismo):
    p = sismo["properties"]
    lon, lat, prof = sismo["geometry"]["coordinates"]
    mag = p.get("mag") or 0.0
    mtxt = texto_magnitud(mag)
    lugar = p.get("place") or "Ubicación desconocida"
    unix = int(p["time"] / 1000)

    roles = roles_a_mencionar(mag)
    contenido = " ".join(f"<@&{rid}>" for rid in roles)

    campos = [
        {"name": "Magnitud", "value": f"**{mtxt}** {p.get('magType') or ''}".strip(), "inline": True},
        {"name": "Profundidad", "value": f"{prof:.0f} km" if prof is not None else "—", "inline": True},
        {"name": "Hora", "value": f"<t:{unix}:f>\n<t:{unix}:R>", "inline": True},
        {"name": "Coordenadas", "value": f"{lat:.2f}°, {lon:.2f}°", "inline": True},
    ]
    if p.get("tsunami"):
        campos.append({"name": "⚠️ Tsunami", "value": "El USGS marca este evento como zona con posible tsunami. Consulta las alertas oficiales.", "inline": False})
    if p.get("alert"):
        campos.append({"name": "Alerta PAGER", "value": p["alert"].upper(), "inline": True})

    embed = {
        "title": f"M {mtxt} — {lugar}",
        "url": p.get("url"),
        "color": color_por_magnitud(mag),
        "fields": campos,
        "footer": {"text": "Datos: USGS · Noether Society"},
        "timestamp": datetime.fromtimestamp(unix, timezone.utc).isoformat(),
    }
    payload = {
        "embeds": [embed],
        # Solo se permiten las menciones de los roles elegidos; nada de @everyone.
        "allowed_mentions": {"parse": [], "roles": roles},
    }
    if contenido:
        payload["content"] = contenido
    return payload


def ids_de(sismo):
    """Un mismo sismo puede cambiar de ID cuando otra red sísmica lo reporta;
    el USGS lista todos sus IDs en properties.ids (",us7000abc,at00xyz,")."""
    extra = (sismo["properties"].get("ids") or "").strip(",").split(",")
    return {sismo["id"], *[i for i in extra if i]}


# ---------------------------------------------------------------- envío


def enviar(webhook, payload):
    for intento in range(5):
        r = requests.post(webhook, json=payload, timeout=30)
        if r.status_code == 429:  # límite de velocidad de Discord
            espera = float(r.json().get("retry_after", 2))
            time.sleep(espera + 0.5)
            continue
        if r.ok:
            return True
        print(f"Discord respondió {r.status_code}: {r.text[:300]}")
        return False
    return False


# ---------------------------------------------------------------- principal


def main():
    prueba = "--prueba" in sys.argv          # imprime en vez de enviar
    webhook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not webhook and not prueba:
        sys.exit("Falta la variable de entorno DISCORD_WEBHOOK.")

    estado = cargar_estado()
    sismos = consultar_usgs()

    if estado is None:
        # Primera ejecución: memoriza lo reciente sin publicarlo, para no
        # inundar el canal con las últimas horas de sismos.
        guardar_estado([i for s in sismos for i in sorted(ids_de(s))])
        print(f"Primera ejecución: {len(sismos)} sismos memorizados sin publicar.")
        return

    vistos = estado.get("vistos", [])
    vistos_set = set(vistos)
    nuevos = [s for s in sismos if not (ids_de(s) & vistos_set)]
    print(f"{len(sismos)} sismos en la ventana, {len(nuevos)} nuevos.")

    for s in nuevos:
        payload = construir_mensaje(s)
        if prueba:
            print(json.dumps(payload, ensure_ascii=False, indent=1))
            ok = True
        else:
            ok = enviar(webhook, payload)
            time.sleep(1)
        if ok:
            for i in sorted(ids_de(s) - vistos_set):
                vistos.append(i)
                vistos_set.add(i)

    guardar_estado(vistos)


if __name__ == "__main__":
    main()
