import os, re, json, time, threading, queue
from pathlib import Path
import serial, numpy as np, pandas as pd
from dotenv import load_dotenv
from flask import Flask, jsonify, request, send_from_directory

load_dotenv()
PUERTO = os.getenv("PUERTO", "COM8")  # USB para probar; luego el COM Bluetooth
BAUDIOS = 9600  # igual que Serial.begin() del ESP32
SIN_DATOS_S = 8  # si el ESP32 no manda nada en 8 s, se cierra y se reabre el puerto
ARCHIVO_CFG = Path(__file__).with_name("config.json")  # aquí se guardan los parámetros editados


# ---- Parámetros configurables (se editan en la pestaña "Configuración") ----
def C(clave, grupo, etiqueta, tipo, defecto, mn=None, mx=None, unidad="", ayuda=""):
    return dict(clave=clave, grupo=grupo, etiqueta=etiqueta, tipo=tipo, defecto=defecto,
                min=mn, max=mx, unidad=unidad, ayuda=ayuda)


CAMPOS = [
    C("riego_por_humedad", "Riego", "Regar cuando el suelo esté seco", "bool", True,
      ayuda="Riega automáticamente cada vez que la humedad del suelo baja del umbral."),
    C("umbral_riego", "Riego", "Umbral de suelo seco", "int", 30, 0, 100, "%",
      ayuda="Si la humedad del suelo baja de este valor, se considera seco."),
    C("objetivo_pct", "Riego", "Humedad objetivo", "int", 60, 1, 100, "%",
      ayuda="El riego se detiene antes de tiempo si el suelo llega a este valor."),
    C("riego_s", "Riego", "Duración de cada riego", "int", 5, 1, 30, "s",
      ayuda="Máximo 30 s: es el límite de seguridad del ESP32."),
    C("espera_min", "Riego", "Espera mínima entre riegos", "int", 10, 0, 1440, "min",
      ayuda="Tiempo mínimo entre un riego y el siguiente."),
    C("riego_por_tiempo", "Riego", "Regar cada cierto tiempo", "bool", False,
      ayuda="Riego programado por intervalo, además del riego por humedad."),
    C("intervalo_min", "Riego", "Intervalo de riego programado", "int", 360, 1, 10080, "min",
      ayuda="Cada cuántos minutos se riega. Se omite si el suelo ya está en la humedad objetivo."),
    C("fan_auto", "Ventilador", "Ventilador automático por temperatura", "bool", True,
      ayuda="Si se desactiva, el ventilador solo se maneja a mano."),
    C("t_fan_on", "Ventilador", "Temperatura de encendido", "float", 28, 0, 60, "°C",
      ayuda="El ventilador se enciende por encima de esta temperatura."),
    C("t_fan_off", "Ventilador", "Temperatura de apagado", "float", 26, 0, 60, "°C",
      ayuda="El ventilador se apaga por debajo de esta temperatura (debe ser menor que la de encendido)."),
    C("manual_min", "Ventilador", "Respeto del control manual", "int", 5, 0, 120, "min",
      ayuda="Si tocas el ventilador o la luz a mano, el modo automático no los cambia durante este tiempo."),
    C("luz_auto", "Luz azul", "Luz azul por horario", "bool", False,
      ayuda="Enciende y apaga la luz azul automáticamente según el horario."),
    C("luz_on", "Luz azul", "Hora de encendido", "time", "18:00", ayuda="Formato 24 h."),
    C("luz_off", "Luz azul", "Hora de apagado", "time", "06:00",
      ayuda="Puede ser menor que la de encendido (la luz pasa la noche encendida)."),
    C("guardar_s", "Sensores y datos", "Guardar lecturas cada", "int", 180, 10, 3600, "s",
      ayuda="Cada cuánto se guarda una lectura en la base de datos (además de cada acción)."),
    C("suelo_seco", "Sensores y datos", "Calibración: sensor en aire (0 %)", "int", 4095, 0, 4095, "",
      ayuda="Lectura cruda del sensor de suelo al aire."),
    C("suelo_humedo", "Sensores y datos", "Calibración: sensor en agua (100 %)", "int", 2500, 0, 4095, "",
      ayuda="Lectura cruda del sensor de suelo sumergido en agua."),
]
HORA_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def validar(datos):
    """Devuelve (valores_limpios, errores). Los campos que falten toman su valor por defecto."""
    out, err = {}, []
    for c in CAMPOS:
        k, t = c["clave"], c["tipo"]
        v = datos.get(k, c["defecto"])
        try:
            if t == "bool":
                if not isinstance(v, bool):
                    raise ValueError("debe ser sí/no")
            elif t == "time":
                if not (isinstance(v, str) and HORA_RE.match(v)):
                    raise ValueError("debe tener formato HH:MM")
            else:
                if isinstance(v, bool) or v is None or v == "":
                    raise ValueError("debe ser un número")
                v = float(v)
                if t == "int":
                    if v != int(v):
                        raise ValueError("debe ser un número entero")
                    v = int(v)
                if v < c["min"] or v > c["max"]:
                    raise ValueError(f"debe estar entre {c['min']} y {c['max']}")
            out[k] = v
        except (ValueError, TypeError) as e:
            err.append(f"{c['etiqueta']}: {e}")
    if not err:
        if out["t_fan_off"] >= out["t_fan_on"]:
            err.append("La temperatura de apagado debe ser menor que la de encendido")
        if out["umbral_riego"] >= out["objetivo_pct"]:
            err.append("La humedad objetivo debe ser mayor que el umbral de suelo seco")
        if out["suelo_seco"] - out["suelo_humedo"] < 100:
            err.append("Calibración: la lectura en aire debe ser mayor que la lectura en agua (mínimo 100 de diferencia)")
    return out, err


def defectos():
    return {c["clave"]: c["defecto"] for c in CAMPOS}


def cargar_cfg():
    try:
        guardado = json.loads(ARCHIVO_CFG.read_text(encoding="utf-8"))
        ok, err = validar({**defectos(), **guardado})
        if not err:
            return ok
        print("config.json inválido, se usan los valores por defecto:", err)
    except FileNotFoundError:
        pass
    except Exception as e:
        print("No se pudo leer config.json:", e)
    return defectos()


def guardar_cfg():
    tmp = ARCHIVO_CFG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, ARCHIVO_CFG)


cfg = cargar_cfg()

sb = None
if os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_KEY"):
    from supabase import create_client
    sb = create_client(os.getenv("SUPABASE_URL"), os.getenv("SUPABASE_KEY"))

app = Flask(__name__, static_folder="static")
est, auto = {"conectado": False}, {"on": False}
ser, ultimo_riego, ult_guardado, previo = None, None, 0, None
riego_fin, riego_origen, apagando = None, None, False
ultimo_dato, motivo_prev = 0, ""
manual_hasta = {"F": 0, "L": 0}  # hasta cuándo se respeta el control manual de ventilador / luz
lock_ser = threading.Lock()
cola_bd = queue.Queue(maxsize=500)
TZ = "America/Bogota"


def pct(raw):
    p = (cfg["suelo_seco"] - raw) / (cfg["suelo_seco"] - cfg["suelo_humedo"]) * 100
    return int(max(0, min(100, p)))


def enviar(c):
    s = ser
    if not (s and s.is_open):
        print("No se pudo enviar", c, "(puerto cerrado)")
        return False
    try:
        with lock_ser:
            s.write((c + "\n").encode())
        return True
    except Exception as e:  # write_timeout o puerto caído
        print("Envío falló:", c, e)
        return False


# ---- Base de datos en segundo plano: nunca bloquea el lector serial ----
def bd(tabla, fila):
    if sb:
        try:
            cola_bd.put_nowait((tabla, fila))
        except queue.Full:
            print("DB: cola llena, se descarta un registro")


def bd_worker():
    while True:
        tabla, fila = cola_bd.get()
        try:
            sb.table(tabla).insert(fila).execute()
        except Exception as e:
            print("DB:", e)


def evento(tipo, detalle=""):
    bd("eventos", {"tipo": tipo, "detalle": detalle})


def guardar(d):
    global ult_guardado
    ult_guardado = time.time()
    if d["temp"] > -1 and d["hum"] > -1:
        bd("lecturas", {k: d[k] for k in ["suelo", "suelo_pct", "temp", "hum", "bomba", "fan", "luz"]})


def regar(origen="manual"):
    global ultimo_riego, riego_fin, riego_origen, apagando
    if est.get("fan") or not est.get("conectado"):
        return False  # nunca bomba y ventilador a la vez
    ahora = time.time()
    riego_fin, riego_origen, apagando = ahora + cfg["riego_s"], origen, False  # primero se programa el apagado
    if not enviar("B1"):
        riego_fin = None
        return False
    ultimo_riego = ahora
    est["bomba"] = 1
    evento("riego", origen)
    return True


def vigilante():
    """Apaga la bomba a tiempo aunque no lleguen datos o el lector esté ocupado.
    Repite B0 cada 1.5 s hasta que el ESP32 confirme bomba=0."""
    global apagando
    ult_b0 = 0
    while True:
        time.sleep(0.5)
        fin = riego_fin
        if fin is None:
            continue
        lleno = riego_origen in ("auto", "programado") and est.get("suelo_pct", 0) >= cfg["objetivo_pct"]
        if (time.time() >= fin or lleno) and time.time() - ult_b0 > 1.5:
            apagando = True
            ult_b0 = time.time()
            enviar("B0")


def luz_debe_estar_on():
    """True si la hora actual (Bogotá) cae dentro del horario de luz configurado."""
    ahora = pd.Timestamp.now(tz=TZ)
    m = ahora.hour * 60 + ahora.minute
    h1, m1 = map(int, cfg["luz_on"].split(":"))
    h2, m2 = map(int, cfg["luz_off"].split(":"))
    ini, fin = h1 * 60 + m1, h2 * 60 + m2
    if ini == fin:
        return False
    return ini <= m < fin if ini < fin else (m >= ini or m < fin)


def logica(d):
    global motivo_prev
    ahora = time.time()
    espera = 0 if ultimo_riego is None else max(0, int(cfg["espera_min"] * 60 - (ahora - ultimo_riego)))
    suelo = d["suelo_pct"]
    seco = cfg["riego_por_humedad"] and suelo < cfg["umbral_riego"]
    toca = cfg["riego_por_tiempo"] and (ultimo_riego is None or ahora - ultimo_riego >= cfg["intervalo_min"] * 60)

    # --- riego automático (tiene prioridad sobre el ventilador) ---
    if riego_fin is not None or d["bomba"]:
        motivo = "regando"
    elif not (seco or toca):
        motivo = (f"suelo {suelo} % (umbral {cfg['umbral_riego']} %): no hace falta regar"
                  if cfg["riego_por_humedad"] else "riego por humedad desactivado")
        if cfg["riego_por_tiempo"]:
            motivo += "; riego programado activo"
    elif espera > 0:
        motivo = f"necesita riego, pero faltan {espera} s desde el último riego"
    elif not seco and suelo >= cfg["objetivo_pct"]:
        motivo = f"riego programado omitido: el suelo ya está en {suelo} %"
    elif d["fan"]:
        motivo = "apago el ventilador para poder regar"
        enviar("F0")
        evento("ventilador", "auto off (riego)")
    else:
        motivo = "suelo seco: iniciando riego" if seco else "riego programado: iniciando"
        regar("auto" if seco else "programado")
    est["motivo"] = motivo
    if motivo != motivo_prev:
        print("AUTO:", motivo)
        motivo_prev = motivo

    # --- luz azul por horario (no depende de la bomba) ---
    if cfg["luz_auto"] and ahora >= manual_hasta["L"]:
        quiere = luz_debe_estar_on()
        if quiere != bool(d["luz"]):
            enviar("L1" if quiere else "L0")
            evento("luz", "auto on" if quiere else "auto off")

    # --- ventilador automático ---
    if riego_fin is not None or d["bomba"]:
        return  # con la bomba en marcha no se toca el ventilador
    if not cfg["fan_auto"] or ahora < manual_hasta["F"]:
        return  # desactivado, o lo tocaste a mano: el modo auto lo respeta unos minutos
    if d["temp"] > cfg["t_fan_on"] and not d["fan"]:
        enviar("F1")
        evento("ventilador", "auto on")
    elif 0 < d["temp"] < cfg["t_fan_off"] and d["fan"]:
        enviar("F0")
        evento("ventilador", "auto off")


def lector():
    global ser, previo, riego_fin, apagando, ultimo_dato
    while True:
        try:
            ser = serial.Serial(PUERTO, BAUDIOS, timeout=1, write_timeout=2)
            ultimo_dato = time.time()
            print("Puerto abierto:", PUERTO)
            while True:
                linea = ser.readline().decode(errors="ignore").strip()
                if time.time() - ultimo_dato > SIN_DATOS_S:
                    raise IOError(f"el ESP32 no manda datos desde hace {SIN_DATOS_S} s")
                if not linea.startswith("{"):
                    continue
                try:
                    d = json.loads(linea)
                    d["suelo_pct"] = pct(d["suelo"])
                except (ValueError, KeyError):
                    continue
                ultimo_dato = time.time()
                est.update(d, conectado=True)
                if apagando and not d["bomba"]:
                    riego_fin, apagando = None, False  # el ESP32 confirmó: bomba apagada
                elif d["bomba"] and riego_fin is None:
                    riego_fin = time.time()  # red de seguridad: bomba prendida sin riego en curso -> apagar
                acc = (d["bomba"], d["fan"], d["luz"])
                cambio = previo is not None and acc != previo
                previo = acc
                if cambio or time.time() - ult_guardado > cfg["guardar_s"]:
                    guardar(d)
                if auto["on"]:
                    logica(d)
        except Exception as e:
            est["conectado"] = False
            print("Serial:", e)
        finally:
            try:
                if ser:
                    ser.close()  # si no se cierra, Windows deja el COM bloqueado y no reconecta
            except Exception:
                pass
        time.sleep(3)


@app.get("/")
def home():
    return send_from_directory("static", "index.html")


@app.get("/api/estado")
def estado():
    hace = None if ultimo_riego is None else int(time.time() - ultimo_riego)
    return jsonify(**est, auto=auto["on"], hace_riego=hace, umbral=cfg["umbral_riego"])


@app.get("/api/config")
def get_config():
    return jsonify(campos=CAMPOS, valores=cfg)


@app.post("/api/config")
def set_config():
    nuevo = request.get_json(silent=True) or {}
    ok, err = validar({**cfg, **nuevo})
    if err:
        return jsonify(ok=False, errores=err), 400
    cambios = [f"{k}={v}" for k, v in ok.items() if cfg.get(k) != v]
    cfg.update(ok)
    try:
        guardar_cfg()
    except Exception as e:
        print("No se pudo guardar config.json:", e)
        return jsonify(ok=False, errores=["No se pudo guardar el archivo de configuración"]), 500
    if cambios:
        evento("config", ", ".join(cambios))
    return jsonify(ok=True, valores=cfg)


@app.post("/api/config/reset")
def reset_config():
    cfg.update(defectos())
    try:
        guardar_cfg()
    except Exception as e:
        print("No se pudo guardar config.json:", e)
    evento("config", "valores por defecto restaurados")
    return jsonify(ok=True, valores=cfg)


@app.post("/api/cmd")
def cmd():
    c = (request.json or {}).get("cmd", "")
    if c == "riego":
        return jsonify(ok=regar("manual"))
    if c in ("F1", "F0", "L1", "L0"):
        if c == "F1" and (est.get("bomba") or riego_fin is not None):
            return jsonify(ok=False)
        if not enviar(c):
            return jsonify(ok=False)
        if c[0] in ("F", "L"):
            manual_hasta[c[0]] = time.time() + cfg["manual_min"] * 60
        est["fan" if c[0] == "F" else "luz"] = int(c[1])
        evento("ventilador" if c[0] == "F" else "luz", c)
        return jsonify(ok=True)
    if c in ("A1", "A0"):
        auto["on"] = c == "A1"
        if not auto["on"]:
            est["motivo"] = ""
        return jsonify(ok=True)
    return jsonify(ok=False), 400


@app.get("/api/historial")
def historial():
    if not sb:
        return jsonify(error="Supabase no configurado")
    r = sb.table("lecturas").select("*").order("created_at", desc=True).limit(40).execute().data
    return jsonify(filas=r)


@app.get("/api/analisis")
def analisis():
    if not sb:
        return jsonify(error="Supabase no configurado")
    rows = sb.table("lecturas").select("*").order("created_at", desc=True).limit(2000).execute().data
    df = pd.DataFrame(rows)
    if len(df) < 3:
        return jsonify(error="Aún hay pocos datos guardados")
    df["t"] = pd.to_datetime(df["created_at"], format="ISO8601", utc=True)
    df = df[(df.temp > -1) & (df.hum > -1)].sort_values("t")
    if len(df) < 3:
        return jsonify(error="Aún hay pocos datos guardados")
    serie = df.set_index("t")[["temp", "hum", "suelo_pct"]].resample("5min").mean().dropna().tail(60)
    horas = (df.t - df.t.iloc[0]).dt.total_seconds() / 3600
    pend = float(np.polyfit(horas, df.temp, 1)[0])
    ev = pd.DataFrame(sb.table("eventos").select("*").eq("tipo", "riego").limit(500).execute().data)
    hoy = 0
    if len(ev):
        t = pd.to_datetime(ev.created_at, format="ISO8601", utc=True).dt.tz_convert(TZ)
        hoy = int((t.dt.date == pd.Timestamp.now(tz=TZ).date()).sum())
    return jsonify(
        temp_prom=round(df.temp.mean(), 1), temp_max=float(df.temp.max()), temp_min=float(df.temp.min()),
        hum_prom=round(df.hum.mean()), suelo_prom=round(df.suelo_pct.mean()),
        tendencia=round(pend, 2), proyeccion=round(float(df.temp.iloc[-1]) + pend, 1),
        riegos_hoy=hoy, pct_fan=round(df.fan.mean() * 100, 1), n=len(df),
        serie=dict(x=[i.tz_convert(TZ).strftime("%H:%M") for i in serie.index],
                   temp=serie.temp.round(1).tolist(), hum=serie.hum.round().tolist(),
                   suelo=serie.suelo_pct.round().tolist()))


if sb:
    try:
        r = sb.table("eventos").select("created_at").eq("tipo", "riego").order("created_at", desc=True).limit(1).execute().data
        if r:
            ultimo_riego = pd.Timestamp(r[0]["created_at"]).timestamp()
    except Exception as e:
        print("DB:", e)

if __name__ == "__main__":
    if sb:
        threading.Thread(target=bd_worker, daemon=True).start()
    threading.Thread(target=vigilante, daemon=True).start()
    threading.Thread(target=lector, daemon=True).start()
    app.run(host="0.0.0.0", port=5000)