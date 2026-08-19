import os
import hmac
import hashlib
import base64
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, status, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel
import anthropic, json, io

load_dotenv()

app = FastAPI()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVO_MEMORIA = os.path.join(BASE_DIR, "memoria.json")
CARPETA_ARCHIVOS = os.path.join(BASE_DIR, "archivos")
INDICE_ARCHIVOS = os.path.join(BASE_DIR, "archivos_indice.json")
CREDENCIALES_SHEETS = os.path.join(BASE_DIR, "credentials.json")
PERFIL_FILE = os.path.join(BASE_DIR, "perfil.md")

CLAUDE_API_KEY = os.environ.get("CLAUDE_API_KEY", "")
HONEY_USER = os.environ.get("HONEY_USER", "bernardo")
HONEY_PASS = os.environ.get("HONEY_PASS", "")
SECRET_KEY = os.environ.get("SECRET_KEY") or (HONEY_PASS + "honey-fallback-secret")

# Cuantos mensajes recientes se le mandan a Claude (el Perfil va siempre aparte)
MAX_MENSAJES = 40

os.makedirs(CARPETA_ARCHIVOS, exist_ok=True)

PERFIL_TEMPLATE = """# Perfil de Bernardo

## Quien soy
Trabajo en el area administrativa y contable de una empresa argentina. Mis tareas
incluyen contabilidad general, liquidacion de sueldos, gestion de proyectos y BI.
Estoy aprendiendo a programar (soy principiante).

## Como me gusta que me hables
- En espanol, de "vos" (Argentina).
- Directo y practico, sin vueltas.
- En temas tecnicos, con paciencia y explicando donde va cada cosa.

## Mis tareas recurrentes
- Liquidacion de sueldos (mensual).
- Analisis de datos con Google Sheets.

## Proyectos activos
- (completar)

## Procesos que le ensene a HONEY
- (se van agregando a medida que le ensenes)
"""

# ---- Login por cookie firmada ----
COOKIE_NAME = "honey_session"
COOKIE_DIAS = 30

def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")

def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))

def crear_token(username: str) -> str:
    exp = int(datetime.now(ZoneInfo("UTC")).timestamp()) + COOKIE_DIAS * 86400
    payload = f"{username}|{exp}"
    firma = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).digest()
    return _b64e(payload.encode()) + "." + _b64e(firma)

def validar_token(token: str):
    try:
        p_b64, f_b64 = token.split(".", 1)
        payload = _b64d(p_b64).decode()
        firma = _b64d(f_b64)
        esperada = hmac.new(SECRET_KEY.encode(), payload.encode(), hashlib.sha256).digest()
        if not hmac.compare_digest(firma, esperada):
            return None
        username, exp = payload.split("|", 1)
        if int(exp) < int(datetime.now(ZoneInfo("UTC")).timestamp()):
            return None
        return username
    except Exception:
        return None

def usuario_de_request(request: Request):
    token = request.cookies.get(COOKIE_NAME, "")
    return validar_token(token) if token else None

def requerir_login(request: Request):
    usuario = usuario_de_request(request)
    if not usuario:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Sesion no valida")
    return usuario

SYSTEM_PROMPT = """Sos HONEY, el asistente personal de inteligencia artificial de Bernardo Diaz.

No sos un chatbot generico. Sos un sistema disenado especificamente para trabajar con Bernardo, conocer sus proyectos, entender como trabaja, y ayudarlo a avanzar en sus objetivos dia a dia.

EL PROYECTO HONEY IA
Bernardo es el creador del proyecto HONEY IA: una plataforma de IA personal, modular y privada. Vos sos la version en desarrollo de ese sistema.

COMO TENES QUE COMPORTARTE
- Habla siempre en espanol con trato de "vos" (Argentina)
- Se directo y practico — no te explayes si no es necesario
- Si no entendes algo, pregunta antes de asumir
- Si no sabes algo, decilo — nunca inventes respuestas
- Cuando Bernardo te ensene un proceso o un dato importante sobre el o su trabajo, sugerile guardarlo en su Perfil para acordarte a futuro
- Para temas tecnicos: escribi el codigo completo, explica que hace y deci donde pegarlo
- Cuando analices datos de Google Sheets, se especifico con los numeros y valores reales

PRINCIPIOS
- La privacidad es lo primero — toda la informacion es confidencial
- La memoria es tu activo mas importante
- Sos un colaborador, no solo una herramienta"""

client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

def cargar_perfil():
    if os.path.exists(PERFIL_FILE):
        with open(PERFIL_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def guardar_perfil(texto):
    with open(PERFIL_FILE, "w", encoding="utf-8") as f:
        f.write(texto)

def contexto_fecha():
    ahora = datetime.now(ZoneInfo("America/Argentina/Buenos_Aires"))
    dias = ["lunes", "martes", "miercoles", "jueves", "viernes", "sabado", "domingo"]
    meses = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
             "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    return (f"\n\nCONTEXTO ACTUAL\nHoy es {dias[ahora.weekday()]} "
            f"{ahora.day} de {meses[ahora.month-1]} de {ahora.year}, "
            f"{ahora.strftime('%H:%M')} hs (hora de Argentina).")

def system_completo():
    s = SYSTEM_PROMPT
    perfil = cargar_perfil().strip()
    if perfil:
        s += "\n\n===== PERFIL DE BERNARDO (memoria persistente, siempre vigente) =====\n" + perfil
    s += contexto_fecha()
    return s

def cargar_historial():
    if os.path.exists(ARCHIVO_MEMORIA):
        with open(ARCHIVO_MEMORIA, "r", encoding="utf-8") as f:
            data = json.load(f)
            return [m for m in data if m["role"] != "system"]
    return []

def guardar_historial(h):
    with open(ARCHIVO_MEMORIA, "w", encoding="utf-8") as f:
        json.dump(h, f, ensure_ascii=False, indent=2)

def historial_para_api(h):
    """Manda solo los ultimos MAX_MENSAJES, empezando siempre por un 'user'."""
    recientes = h[-MAX_MENSAJES:]
    while recientes and recientes[0]["role"] != "user":
        recientes = recientes[1:]
    return recientes

def cargar_indice():
    if os.path.exists(INDICE_ARCHIVOS):
        with open(INDICE_ARCHIVOS, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def guardar_indice(indice):
    with open(INDICE_ARCHIVOS, "w", encoding="utf-8") as f:
        json.dump(indice, f, ensure_ascii=False, indent=2)

def extraer_texto(filename, contenido):
    ext = filename.lower().split(".")[-1]
    try:
        if ext == "pdf":
            import pypdf
            reader = pypdf.PdfReader(io.BytesIO(contenido))
            return "\n".join(p.extract_text() or "" for p in reader.pages)
        elif ext in ["docx", "doc"]:
            import docx
            doc = docx.Document(io.BytesIO(contenido))
            return "\n".join(p.text for p in doc.paragraphs)
        elif ext in ["xlsx", "xls"]:
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(contenido), data_only=True)
            texto = []
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                texto.append(f"=== Hoja: {sheet} ===")
                for row in ws.iter_rows(values_only=True):
                    fila = [str(c) if c is not None else "" for c in row]
                    if any(f.strip() for f in fila):
                        texto.append(" | ".join(fila))
            return "\n".join(texto)
        elif ext == "txt":
            return contenido.decode("utf-8", errors="ignore")
        else:
            return None
    except Exception as e:
        return f"Error al leer el archivo: {str(e)}"

def leer_google_sheet(url):
    import gspread
    with open(CREDENCIALES_SHEETS, "r") as f:
        gc = gspread.service_account_from_dict(json.load(f))
    sh = gc.open_by_url(url)
    texto = []
    for ws in sh.worksheets():
        texto.append(f"=== Hoja: {ws.title} ===")
        datos = ws.get_all_values()
        for fila in datos:
            if any(c.strip() for c in fila):
                texto.append(" | ".join(fila))
    return sh.title, "\n".join(texto)

class Mensaje(BaseModel):
    texto: str

class CargarArchivo(BaseModel):
    filename: str

class SheetURL(BaseModel):
    url: str

class Perfil(BaseModel):
    texto: str

# ---------------- LOGIN ----------------
@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request, error: str = ""):
    if usuario_de_request(request):
        return RedirectResponse("/", status_code=302)
    aviso = '<div class="err">Usuario o contrasena incorrectos</div>' if error else ""
    return LOGIN_HTML.replace("<!--ERROR-->", aviso)

@app.post("/login")
async def login_post(usuario: str = Form(...), password: str = Form(...)):
    ok = hmac.compare_digest(usuario, HONEY_USER) and hmac.compare_digest(password, HONEY_PASS)
    if not ok:
        return RedirectResponse("/login?error=1", status_code=302)
    resp = RedirectResponse("/", status_code=302)
    resp.set_cookie(COOKIE_NAME, crear_token(usuario), max_age=COOKIE_DIAS * 86400,
                    httponly=True, samesite="lax")
    return resp

@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=302)
    resp.delete_cookie(COOKIE_NAME)
    return resp

# ---------------- APP ----------------
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    if not usuario_de_request(request):
        return RedirectResponse("/login", status_code=302)
    return HTML

@app.get("/perfil")
async def get_perfil(usuario: str = Depends(requerir_login)):
    texto = cargar_perfil()
    return {"texto": texto if texto else PERFIL_TEMPLATE}

@app.post("/perfil")
async def post_perfil(data: Perfil, usuario: str = Depends(requerir_login)):
    guardar_perfil(data.texto)
    return {"ok": True}

@app.post("/chat")
async def chat(mensaje: Mensaje, usuario: str = Depends(requerir_login)):
    h = cargar_historial()
    h.append({"role": "user", "content": mensaje.texto})
    response = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=2048,
        system=system_completo(),
        messages=historial_para_api(h)
    )
    texto = response.content[0].text
    h.append({"role": "assistant", "content": texto})
    guardar_historial(h)
    return {"respuesta": texto}

@app.get("/historial")
async def historial(usuario: str = Depends(requerir_login)):
    return cargar_historial()

@app.post("/upload")
async def upload(archivo: UploadFile = File(...), usuario: str = Depends(requerir_login)):
    contenido = await archivo.read()
    texto = extraer_texto(archivo.filename, contenido)
    if texto is None:
        return {"error": "Formato no soportado. Usa PDF, Word, Excel o TXT."}
    if len(texto) > 50000:
        texto = texto[:50000] + "\n\n[Archivo recortado por tamano]"
    ruta = os.path.join(CARPETA_ARCHIVOS, archivo.filename)
    with open(ruta + ".txt", "w", encoding="utf-8") as f:
        f.write(texto)
    indice = cargar_indice()
    indice = [a for a in indice if a["nombre"] != archivo.filename]
    indice.insert(0, {"nombre": archivo.filename, "fecha": datetime.now().strftime("%d/%m/%Y %H:%M"), "tipo": "archivo"})
    guardar_indice(indice)
    return {"filename": archivo.filename, "texto": texto}

@app.post("/cargar-archivo")
async def cargar_archivo(data: CargarArchivo, usuario: str = Depends(requerir_login)):
    ruta = os.path.join(CARPETA_ARCHIVOS, data.filename + ".txt")
    if not os.path.exists(ruta):
        return {"error": "Archivo no encontrado"}
    with open(ruta, "r", encoding="utf-8") as f:
        texto = f.read()
    return {"filename": data.filename, "texto": texto}

@app.get("/archivos")
async def listar_archivos(usuario: str = Depends(requerir_login)):
    return cargar_indice()

@app.post("/sheets")
async def sheets(data: SheetURL, usuario: str = Depends(requerir_login)):
    try:
        titulo, texto = leer_google_sheet(data.url)
        if len(texto) > 50000:
            texto = texto[:50000] + "\n\n[Planilla recortada por tamano]"
        indice = cargar_indice()
        nombre = f"[Sheet] {titulo}"
        indice = [a for a in indice if a["nombre"] != nombre]
        indice.insert(0, {"nombre": nombre, "fecha": datetime.now().strftime("%d/%m/%Y %H:%M"), "tipo": "sheet", "url": data.url})
        guardar_indice(indice)
        ruta = os.path.join(CARPETA_ARCHIVOS, titulo + ".txt")
        with open(ruta, "w", encoding="utf-8") as f:
            f.write(texto)
        return {"titulo": titulo, "texto": texto}
    except Exception as e:
        return {"error": str(e)}

LOGIN_HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>HONEY - Ingresar</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: #0A0A0A; color: #EDE8DF; font-family: system-ui, sans-serif; min-height: 100dvh; display: flex; align-items: center; justify-content: center; padding: 24px; }
.card { width: 100%; max-width: 360px; background: #111110; border: 1px solid #2A2820; border-radius: 16px; padding: 32px 24px; }
.logo { width: 56px; height: 56px; background: #F0A028; border-radius: 14px; display: flex; align-items: center; justify-content: center; font-size: 30px; margin: 0 auto 16px; color: #0A0A0A; font-weight: 700; }
h1 { font-size: 20px; color: #F0A028; text-align: center; margin-bottom: 4px; }
.sub { font-size: 13px; color: #8A8578; text-align: center; margin-bottom: 22px; }
label { font-size: 12px; color: #8A8578; display: block; margin-bottom: 6px; }
input { width: 100%; background: #1A1A18; border: 1px solid #2A2820; border-radius: 10px; color: #EDE8DF; font-size: 16px; padding: 12px 14px; outline: none; margin-bottom: 14px; font-family: inherit; }
input:focus { border-color: #F0A028; }
button { width: 100%; background: #F0A028; color: #0A0A0A; border: none; border-radius: 10px; padding: 13px; font-size: 15px; font-weight: 700; cursor: pointer; }
.err { background: #3A1A1A; border: 1px solid #5A2A2A; color: #E88; font-size: 13px; padding: 10px 12px; border-radius: 10px; margin-bottom: 16px; text-align: center; }
</style>
</head>
<body>
<form class="card" method="post" action="/login">
  <div class="logo">H</div>
  <h1>HONEY</h1>
  <div class="sub">Tu asistente personal</div>
  <!--ERROR-->
  <label>Usuario</label>
  <input name="usuario" autocomplete="username" autocapitalize="none" required>
  <label>Contrasena</label>
  <input name="password" type="password" autocomplete="current-password" required>
  <button type="submit">Ingresar</button>
</form>
</body>
</html>"""

HTML = """<!DOCTYPE html>
<html lang="es">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>HONEY IA</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; -webkit-tap-highlight-color: transparent; }
:root { --amarillo: #F0A028; --fondo: #0A0A0A; --panel: #111110; --borde: #2A2820; --texto: #EDE8DF; }
html, body { height: 100%; }
body { background: var(--fondo); color: var(--texto); font-family: system-ui, sans-serif; height: 100dvh; display: flex; flex-direction: column; overflow: hidden; }
header { background: var(--panel); border-bottom: 1px solid var(--borde); padding: 12px 16px; display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.logo { width: 30px; height: 30px; background: var(--amarillo); border-radius: 8px; display: flex; align-items: center; justify-content: center; font-size: 15px; font-weight: 700; color: #0A0A0A; }
header h1 { font-size: 16px; font-weight: 700; color: var(--amarillo); }
.icon-btn { background: none; border: 1px solid var(--borde); color: #C8B890; border-radius: 8px; width: 38px; height: 38px; font-size: 18px; cursor: pointer; display: flex; align-items: center; justify-content: center; text-decoration: none; }
.spacer { flex: 1; }
.main { display: flex; flex: 1; overflow: hidden; position: relative; }
#sidebar { width: 250px; background: #0D0D0B; border-right: 1px solid var(--borde); display: flex; flex-direction: column; flex-shrink: 0; }
.sidebar-section { padding: 12px 16px; border-bottom: 1px solid var(--borde); }
.sidebar-section span { font-size: 11px; font-weight: 700; letter-spacing: 0.08em; text-transform: uppercase; color: #504A40; }
.btn-perfil { margin: 10px; padding: 10px; background: #1A1A18; border: 1px solid var(--borde); border-radius: 8px; color: #C8B890; font-size: 13px; font-weight: 600; text-align: center; cursor: pointer; }
.btn-perfil:active { border-color: var(--amarillo); color: var(--amarillo); }
.sheet-input-area { padding: 10px; border-bottom: 1px solid var(--borde); display: flex; flex-direction: column; gap: 6px; }
#sheet-url { background: #1A1A18; border: 1px solid var(--borde); border-radius: 8px; color: var(--texto); font-size: 14px; padding: 9px 10px; width: 100%; outline: none; font-family: inherit; }
#sheet-url:focus { border-color: var(--amarillo); }
.btn-sheet { background: #1E3A1E; border: 1px solid #2A4A2A; border-radius: 8px; color: #78C878; font-size: 13px; font-weight: 600; padding: 9px 10px; cursor: pointer; text-align: center; }
#archivos-lista { flex: 1; overflow-y: auto; padding: 8px; }
.archivo-item { padding: 10px 12px; border-radius: 8px; cursor: pointer; margin-bottom: 4px; border: 1px solid transparent; }
.archivo-item:active { background: #161612; border-color: var(--borde); }
.archivo-item .nombre { font-size: 13px; color: #C8B890; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.archivo-item .fecha { font-size: 11px; color: #504A40; margin-top: 2px; }
.sin-archivos { padding: 20px 12px; font-size: 13px; color: #504A40; text-align: center; line-height: 1.6; }
#chat-area { flex: 1; display: flex; flex-direction: column; overflow: hidden; min-width: 0; }
#mensajes { flex: 1; overflow-y: auto; padding: 18px 16px; display: flex; flex-direction: column; gap: 14px; -webkit-overflow-scrolling: touch; }
.msg { max-width: 88%; padding: 11px 15px; border-radius: 14px; font-size: 15.5px; line-height: 1.6; white-space: pre-wrap; word-wrap: break-word; }
.usuario { background: var(--amarillo); color: #0A0A0A; font-weight: 500; align-self: flex-end; border-bottom-right-radius: 4px; }
.honey { background: #161612; border: 1px solid var(--borde); align-self: flex-start; border-bottom-left-radius: 4px; }
.archivo-badge { background: #1A1A18; border: 1px solid #F0A02840; border-radius: 10px; padding: 8px 12px; font-size: 13px; color: var(--amarillo); align-self: flex-end; }
.pensando { color: #7A7466; font-style: italic; }
.sistema { font-size: 12px; color: #504A40; text-align: center; align-self: center; }
#input-area { background: var(--panel); border-top: 1px solid var(--borde); padding: 10px 12px calc(10px + env(safe-area-inset-bottom)); display: flex; gap: 8px; align-items: flex-end; flex-shrink: 0; }
#texto { flex: 1; background: #1A1A18; border: 1px solid var(--borde); border-radius: 12px; color: var(--texto); font-size: 16px; padding: 11px 14px; resize: none; font-family: inherit; line-height: 1.4; max-height: 120px; outline: none; min-width: 0; }
#texto:focus { border-color: var(--amarillo); }
.btn-attach { background: #1A1A18; border: 1px solid var(--borde); border-radius: 12px; min-width: 44px; height: 44px; cursor: pointer; font-size: 20px; color: #8A8578; flex-shrink: 0; }
.btn-send { background: var(--amarillo); color: #0A0A0A; border: none; border-radius: 12px; padding: 0 18px; height: 44px; font-size: 14px; font-weight: 700; cursor: pointer; flex-shrink: 0; }
.btn-send:disabled { background: #2A2820; color: #504A40; }
.bienvenida { text-align: center; padding: 48px 24px; color: #504A40; margin: auto; }
.bienvenida .icon { font-size: 44px; margin-bottom: 12px; }
.bienvenida h2 { font-size: 18px; color: #8A8578; margin-bottom: 8px; }
#file-input { display: none; }
#backdrop { display: none; }
/* Modal perfil */
#perfil-modal { display: none; position: fixed; inset: 0; z-index: 50; background: rgba(0,0,0,.6); align-items: center; justify-content: center; padding: 16px; }
#perfil-modal.abierto { display: flex; }
.perfil-card { background: var(--panel); border: 1px solid var(--borde); border-radius: 14px; width: 100%; max-width: 620px; max-height: 88dvh; display: flex; flex-direction: column; }
.perfil-head { padding: 16px 18px; border-bottom: 1px solid var(--borde); display: flex; align-items: center; }
.perfil-head h3 { font-size: 16px; color: var(--amarillo); flex: 1; }
.perfil-head .cerrar { background: none; border: none; color: #8A8578; font-size: 22px; cursor: pointer; }
.perfil-body { padding: 14px 18px; overflow-y: auto; }
.perfil-body p { font-size: 12.5px; color: #7A7466; margin-bottom: 10px; line-height: 1.5; }
#perfil-texto { width: 100%; min-height: 320px; background: #1A1A18; border: 1px solid var(--borde); border-radius: 10px; color: var(--texto); font-size: 14px; padding: 12px; font-family: ui-monospace, monospace; line-height: 1.5; outline: none; resize: vertical; }
#perfil-texto:focus { border-color: var(--amarillo); }
.perfil-foot { padding: 12px 18px; border-top: 1px solid var(--borde); display: flex; gap: 8px; justify-content: flex-end; align-items: center; }
.perfil-foot .estado { flex: 1; font-size: 12px; color: #78C878; }
.btn-guardar { background: var(--amarillo); color: #0A0A0A; border: none; border-radius: 10px; padding: 10px 18px; font-size: 14px; font-weight: 700; cursor: pointer; }
.btn-cancelar { background: #1A1A18; border: 1px solid var(--borde); color: #C8B890; border-radius: 10px; padding: 10px 16px; font-size: 14px; cursor: pointer; }
/* ----- CELULAR ----- */
@media (max-width: 760px) {
  #hamb { display: flex; }
  #sidebar { position: absolute; top: 0; left: 0; bottom: 0; z-index: 20; transform: translateX(-100%); transition: transform .2s ease; box-shadow: 2px 0 16px rgba(0,0,0,.5); }
  body.menu-abierto #sidebar { transform: translateX(0); }
  #backdrop { display: block; position: absolute; inset: 0; background: rgba(0,0,0,.5); z-index: 15; opacity: 0; pointer-events: none; transition: opacity .2s; }
  body.menu-abierto #backdrop { opacity: 1; pointer-events: auto; }
  .msg { max-width: 92%; }
}
@media (min-width: 761px) { #hamb { display: none; } }
</style>
</head>
<body>
<header>
  <button class="icon-btn" id="hamb" onclick="toggleMenu()" title="Menu">&#9776;</button>
  <div class="logo">H</div>
  <h1>HONEY</h1>
  <div class="spacer"></div>
  <span style="font-size:12px;color:#504A40;">claude</span>
  <a href="/logout" class="icon-btn" title="Salir">&#8631;</a>
</header>
<div class="main">
  <div id="backdrop" onclick="toggleMenu()"></div>
  <div id="sidebar">
    <div class="btn-perfil" onclick="abrirPerfil()">&#128100; Mi perfil</div>
    <div class="sidebar-section"><span>Archivos</span></div>
    <div id="archivos-lista"><div class="sin-archivos">Todavia no subiste archivos</div></div>
  </div>
  <div id="chat-area">
    <div id="mensajes">
      <div class="bienvenida">
        <div class="icon">H</div>
        <h2>Hola, soy HONEY</h2>
        <p>Escribime abajo para empezar.</p>
      </div>
    </div>
    <div id="input-area">
      <input type="file" id="file-input" accept=".pdf,.docx,.doc,.xlsx,.xls,.txt">
      <button class="btn-attach" onclick="document.getElementById('file-input').click()" title="Adjuntar">+</button>
      <textarea id="texto" placeholder="Escribi tu mensaje..." rows="1"></textarea>
      <button class="btn-send" id="btn" onclick="enviar()">Enviar</button>
    </div>
  </div>
</div>
<div id="perfil-modal">
  <div class="perfil-card">
    <div class="perfil-head"><h3>Mi perfil</h3><button class="cerrar" onclick="cerrarPerfil()">&times;</button></div>
    <div class="perfil-body">
      <p>Esto es la memoria permanente de HONEY: quien sos, como te gusta que te hable, tus tareas y los procesos que le ensenes. Lo lee siempre. Editalo libremente.</p>
      <textarea id="perfil-texto" placeholder="Cargando..."></textarea>
    </div>
    <div class="perfil-foot">
      <span class="estado" id="perfil-estado"></span>
      <button class="btn-cancelar" onclick="cerrarPerfil()">Cancelar</button>
      <button class="btn-guardar" onclick="guardarPerfil()">Guardar</button>
    </div>
  </div>
</div>
<script>
const md = document.getElementById('mensajes');
const tx = document.getElementById('texto');
const btn = document.getElementById('btn');
const fi = document.getElementById('file-input');

function toggleMenu() { document.body.classList.toggle('menu-abierto'); }
function cerrarMenu() { document.body.classList.remove('menu-abierto'); }

tx.addEventListener('input', () => { tx.style.height='auto'; tx.style.height=Math.min(tx.scrollHeight,120)+'px'; });
tx.addEventListener('keydown', e => { if(e.key==='Enter'&&!e.shiftKey&&window.innerWidth>760){e.preventDefault();enviar();} });

async function req(url, opts) {
  const r = await fetch(url, opts);
  if (r.status === 401) { window.location.href = '/login'; throw new Error('sin sesion'); }
  return r;
}

async function abrirPerfil() {
  cerrarMenu();
  document.getElementById('perfil-estado').textContent = '';
  document.getElementById('perfil-modal').classList.add('abierto');
  const t = document.getElementById('perfil-texto');
  t.value = 'Cargando...';
  try {
    const r = await req('/perfil');
    const d = await r.json();
    t.value = d.texto || '';
  } catch(e) { t.value = ''; }
}
function cerrarPerfil() { document.getElementById('perfil-modal').classList.remove('abierto'); }
async function guardarPerfil() {
  const texto = document.getElementById('perfil-texto').value;
  const est = document.getElementById('perfil-estado');
  est.style.color = '#7A7466'; est.textContent = 'Guardando...';
  try {
    await req('/perfil', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({texto})});
    est.style.color = '#78C878'; est.textContent = 'Guardado \\u2713';
    setTimeout(cerrarPerfil, 700);
  } catch(e) { est.style.color = '#E88'; est.textContent = 'Error al guardar'; }
}

async function conectarSheet() {
  const url = document.getElementById('sheet-url').value.trim();
  if (!url) return;
  cerrarMenu(); btn.disabled = true;
  const p = agregar('HONEY esta leyendo la planilla...', 'honey pensando');
  try {
    const r = await req('/sheets', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({url})});
    const d = await r.json();
    if (d.error) { p.textContent = 'Error: ' + d.error; }
    else {
      const msg = 'Te comparto esta planilla de Google Sheets: ' + d.titulo + '\\n\\nContenido:\\n' + d.texto;
      const r2 = await req('/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({texto: msg})});
      const d2 = await r2.json();
      p.textContent = d2.respuesta; p.classList.remove('pensando');
      cargarListaArchivos();
      document.getElementById('sheet-url').value = '';
    }
  } catch(e) { p.textContent = 'Error al conectar con la planilla.'; }
  btn.disabled = false;
}

async function cargarListaArchivos() {
  try {
    const r = await req('/archivos');
    const lista = await r.json();
    const cont = document.getElementById('archivos-lista');
    if (lista.length === 0) { cont.innerHTML = '<div class="sin-archivos">Todavia no subiste archivos</div>'; return; }
    cont.innerHTML = lista.map(a => `
      <div class="archivo-item" onclick="recargarArchivo('${a.nombre.replace(/'/g,"\\'")}')">
        <div class="nombre">${a.nombre}</div>
        <div class="fecha">${a.fecha}</div>
      </div>`).join('');
  } catch(e) {}
}

async function recargarArchivo(nombre) {
  cerrarMenu(); btn.disabled = true;
  agregar('Cargando: ' + nombre, 'sistema');
  const nombreLimpio = nombre.replace('[Sheet] ', '');
  const r = await req('/cargar-archivo', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({filename: nombreLimpio})});
  const d = await r.json();
  if (d.error) { agregar('Error: ' + d.error, 'sistema'); btn.disabled=false; return; }
  const p = agregar('HONEY esta analizando ' + nombre + '...', 'honey pensando');
  const r2 = await req('/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({texto: 'Volve a analizar este archivo: ' + d.filename + '\\n\\n' + d.texto})});
  const d2 = await r2.json();
  p.textContent = d2.respuesta; p.classList.remove('pensando');
  btn.disabled = false;
}

fi.addEventListener('change', async () => {
  const file = fi.files[0];
  if (!file) return;
  cerrarMenu(); btn.disabled = true;
  agregar(file.name, 'archivo-badge');
  const p = agregar('HONEY esta leyendo el archivo...', 'honey pensando');
  const form = new FormData();
  form.append('archivo', file);
  try {
    const r = await req('/upload', {method:'POST', body: form});
    const d = await r.json();
    if (d.error) { p.textContent = 'Error: ' + d.error; }
    else {
      const r2 = await req('/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({texto: 'Te comparto este archivo: ' + d.filename + '\\n\\nContenido:\\n' + d.texto})});
      const d2 = await r2.json();
      p.textContent = d2.respuesta; p.classList.remove('pensando');
      cargarListaArchivos();
    }
  } catch(e) { p.textContent = 'Error al procesar el archivo.'; }
  btn.disabled = false; fi.value = '';
});

function agregar(texto, tipo) {
  const b = document.querySelector('.bienvenida');
  if(b) b.remove();
  const d = document.createElement('div');
  d.className = 'msg ' + tipo;
  d.textContent = texto;
  md.appendChild(d);
  md.scrollTop = md.scrollHeight;
  return d;
}

async function enviar() {
  const msg = tx.value.trim();
  if(!msg) return;
  tx.value=''; tx.style.height='auto'; btn.disabled=true;
  agregar(msg, 'usuario');
  const p = agregar('HONEY esta pensando...', 'honey pensando');
  try {
    const r = await req('/chat', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({texto:msg})});
    const d = await r.json();
    p.textContent = d.respuesta; p.classList.remove('pensando');
  } catch(e) { p.textContent = 'Error al conectar.'; }
  btn.disabled=false; tx.focus();
}

async function cargarHistorial() {
  try {
    const r = await req('/historial');
    const h = await r.json();
    if (h.length) {
      const b = document.querySelector('.bienvenida'); if(b) b.remove();
      h.forEach(m => agregar(m.content, m.role === 'user' ? 'usuario' : 'honey'));
    }
  } catch(e) {}
}

cargarHistorial();
cargarListaArchivos();
</script>
</body>
</html>"""
