from __future__ import annotations

import html
import hmac
import json
import re
import shutil
import smtplib
import ssl
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse, urlunparse

import requests
import streamlit as st


ROOT = Path(__file__).resolve().parent
PENDING_DIR = ROOT / "02_BANDEJA_APROBACION" / "pendientes"
APPROVED_DIR = ROOT / "02_BANDEJA_APROBACION" / "aprobados"
DATA_DIR = ROOT / ".ceo_dashboard"
STATE_FILE = DATA_DIR / "state.json"
AGENCY_NAME = "Apex AI Agency"


@st.cache_resource
def get_state_lock():
    return threading.RLock()


@st.cache_resource
def get_background_executor() -> ThreadPoolExecutor:
    return ThreadPoolExecutor(max_workers=4, thread_name_prefix="ceo-outbound")


@st.cache_resource
def get_background_jobs() -> tuple[dict[str, dict], threading.Lock]:
    return {}, threading.Lock()


STATE_LOCK = get_state_lock()
BACKGROUND_EXECUTOR = get_background_executor()
BACKGROUND_JOBS, BACKGROUND_JOBS_LOCK = get_background_jobs()

NICHES = {
    "Contratistas de Servicios del Hogar (Miami, EE. UU.)": {
        "zone": "Miami, Florida, EE. UU.",
        "businesses": [
            ("Brisa Home Comfort", "Aire acondicionado y climatización"),
            ("Palma Plumbing Co.", "Plomería residencial"),
            ("Coastline Electric", "Servicios eléctricos del hogar"),
            ("Sunrise Roof & Repair", "Techos y reparaciones"),
            ("Evergreen Home Care", "Mantenimiento del hogar"),
            ("Miami Cool Air", "Instalación y servicio de aire acondicionado"),
            ("Coral Gate Contractors", "Remodelación residencial"),
            ("Atlantic Spark Services", "Electricidad y pequeñas reparaciones"),
            ("Palm Grove Plumbing", "Plomería y detección de fugas"),
            ("Bayview Home Pros", "Reparaciones y mantenimiento"),
        ],
    },
    "Clínicas de Estética y Odontología (Madrid, España)": {
        "zone": "Madrid, España",
        "businesses": [
            ("Clínica Luz de Madrid", "Estética facial y cuidado de la piel"),
            ("Sonrisa Retiro", "Odontología familiar"),
            ("Estudio Piel & Calma", "Tratamientos estéticos personalizados"),
            ("Clínica Dental Prado", "Odontología y salud bucodental"),
            ("Centro Aura Madrid", "Medicina estética"),
            ("Sonrisa Chamberí", "Odontología preventiva"),
            ("Clínica Derma Sol", "Dermatología estética"),
            ("Dental Plaza Norte", "Odontología general y estética"),
            ("Espacio Belleza Serrano", "Estética avanzada"),
            ("Clínica Oral Castellana", "Cuidado dental integral"),
        ],
    },
}

PAYMENT_OPTIONS = {
    "Binance Pay / USDT": 38000,
    "Payoneer (Tarjeta/ACH)": 40000,
    "PayPal": 42500,
}
STATUS_LABELS = {
    "pending": "Pendiente de aprobación",
    "changes_requested": "Ajustes solicitados",
    "sending": "Envío de correo en curso",
    "approved": "Correo enviado y aprobado",
    "sent_archive_failed": "Correo enviado; archivo pendiente",
}


def default_state() -> dict:
    return {
        "prototypes": [],
        "income": [],
        "clients": [],
        "settings": {
            "prototype_base_url": "",
            "payment_links": {method: "" for method in PAYMENT_OPTIONS},
        },
    }


def load_state() -> dict:
    with STATE_LOCK:
        return _load_state()


def _load_state() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    APPROVED_DIR.mkdir(parents=True, exist_ok=True)
    if not STATE_FILE.exists():
        return default_state()
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"El archivo de datos {STATE_FILE.relative_to(ROOT)} no contiene JSON válido."
        ) from exc
    if not isinstance(state, dict) or any(
        not isinstance(state.get(key), list) for key in ("prototypes", "income", "clients")
    ):
        raise ValueError(
            f"El archivo {STATE_FILE.relative_to(ROOT)} no tiene el formato esperado."
        )
    defaults = default_state()
    settings = state.setdefault("settings", defaults["settings"])
    if not isinstance(settings, dict):
        raise ValueError("La configuración guardada no tiene el formato esperado.")
    settings.pop("agency_name", None)
    for key, value in defaults["settings"].items():
        settings.setdefault(key, value)
    if not isinstance(settings["payment_links"], dict):
        raise ValueError("Los enlaces de pago guardados no tienen el formato esperado.")
    for method in PAYMENT_OPTIONS:
        settings["payment_links"].setdefault(method, "")
    return state


def save_state(state: dict) -> None:
    with STATE_LOCK:
        _save_state(state)


def _save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary_file = STATE_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_file.replace(STATE_FILE)


def required_secret(name: str) -> str:
    try:
        value = str(st.secrets[name]).strip()
    except (KeyError, FileNotFoundError) as exc:
        raise RuntimeError(
            f"Falta configurar el secreto {name} en Streamlit secrets."
        ) from exc
    if not value:
        raise RuntimeError(f"El secreto {name} está vacío.")
    return value


def notificar_ceo_whatsapp(mensaje: str) -> str | None:
    try:
        api_id = required_secret("GREEN_API_ID")
        api_token = required_secret("GREEN_API_TOKEN")
        chat_id = f"{required_secret('CEO_WHATSAPP')}"
        if not re.fullmatch(r"\d+@c\.us", chat_id):
            raise RuntimeError(
                "CEO_WHATSAPP debe ser el chatId personal con formato numérico y sufijo @c.us."
            )
        clean_message = mensaje.strip()
        if not clean_message:
            raise ValueError("El mensaje de WhatsApp no puede estar vacío.")
        endpoint = (
            f"https://{api_id}.api.greenapi.com/waInstance{api_id}"
            f"/sendMessage/{api_token}"
        )
        response = requests.post(
            endpoint,
            json={"chatId": chat_id, "message": clean_message},
            timeout=10,
        )
        response.raise_for_status()
    except Exception as exc:
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
        error_type = "HTTP" if status_code else type(exc).__name__
        status = f" {status_code}" if status_code else ""
        return f"Green-API no pudo enviar la notificación ({error_type}{status})."
    return None


def enviar_correo_cliente(destinatario: str, asunto: str, cuerpo: str) -> None:
    sender = required_secret("SMTP_EMAIL")
    password = required_secret("SMTP_PASSWORD")
    parsed_sender = parseaddr(sender)[1]
    parsed_recipient = parseaddr(destinatario)[1]
    if (
        not parsed_sender
        or parsed_sender != sender
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", parsed_sender)
    ):
        raise ValueError("SMTP_EMAIL debe contener una dirección de correo válida.")
    if (
        not parsed_recipient
        or parsed_recipient != destinatario.strip()
        or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", parsed_recipient)
    ):
        raise ValueError("Indica un correo válido para el prospecto antes de aprobar.")
    if "\r" in asunto or "\n" in asunto:
        raise ValueError("El asunto del correo no puede contener saltos de línea.")

    message = EmailMessage()
    message["From"] = sender
    message["To"] = parsed_recipient
    message["Subject"] = asunto
    message.set_content(cuerpo)
    try:
        with smtplib.SMTP_SSL(
            "smtp.gmail.com", 465, timeout=20, context=ssl.create_default_context()
        ) as smtp:
            smtp.login(sender, password)
            refused = smtp.send_message(message)
    except (smtplib.SMTPException, OSError, TimeoutError) as exc:
        raise RuntimeError(
            f"No se pudo enviar el correo por SMTP ({type(exc).__name__})."
        ) from None
    if refused:
        raise RuntimeError("El servidor SMTP rechazó el destinatario.")


def enqueue_background_job(
    label: str,
    function: Callable[..., object],
    *args,
    kind: str = "notification",
    record_id: str = "",
) -> str:
    job_id = uuid.uuid4().hex
    future = BACKGROUND_EXECUTOR.submit(function, *args)
    with BACKGROUND_JOBS_LOCK:
        BACKGROUND_JOBS[job_id] = {
            "id": job_id,
            "label": label,
            "kind": kind,
            "record_id": record_id,
            "future": future,
            "status": "queued",
            "error": "",
        }
        completed_ids = [
            old_id
            for old_id, job in BACKGROUND_JOBS.items()
            if job["status"] in ("succeeded", "failed")
        ]
        for old_id in completed_ids[:-100]:
            del BACKGROUND_JOBS[old_id]
    return job_id


def money(cents: int) -> str:
    return f"${cents / 100:,.2f} USD"


def safe_text(value: str) -> str:
    return html.escape(value.strip(), quote=True)


def ceo_whatsapp_digits() -> str:
    try:
        chat_id = str(st.secrets["CEO_WHATSAPP"]).strip()
    except (KeyError, FileNotFoundError):
        return ""
    match = re.fullmatch(r"(\d+)@c\.us", chat_id)
    return match.group(1) if match else ""


def slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "prototipo"


def valid_web_url(value: str) -> str:
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return candidate


def make_prototype_html(
    name: str,
    service: str,
    zone: str,
    payment_links: dict | None = None,
    whatsapp_number: str = "",
    payment_options_open: bool = False,
    track_payment_open: bool = False,
) -> str:
    business = safe_text(name)
    category = safe_text(service)
    location = safe_text(zone)
    payment_links = payment_links or {}
    payment_rows = []
    for method, cents in PAYMENT_OPTIONS.items():
        url = valid_web_url(payment_links.get(method, ""))
        label = method
        if method == "Binance Pay / USDT":
            label += " · 5% de descuento"
        if url:
            payment_rows.append(
                f'<a class="payment-link" href="{safe_text(url)}" target="_blank" '
                f'rel="noopener noreferrer">{safe_text(label)} · ${cents / 100:.0f} USD</a>'
            )
        else:
            payment_rows.append(
                f'<span class="payment-link">{safe_text(label)} · '
                f'${cents / 100:.0f} USD <small>Enlace pendiente de configurar</small></span>'
            )
    payment_options = "\n".join(payment_rows)
    phone_digits = re.sub(r"\D", "", whatsapp_number)
    whatsapp_button = ""
    if phone_digits:
        whatsapp_message = quote(
            "Hola, acabo de ver el prototipo de mi negocio y quiero activarlo"
        )
        whatsapp_button = (
            f'<a class="whatsapp-float" href="https://wa.me/{phone_digits}?text={whatsapp_message}" '
            'target="_blank" rel="noopener noreferrer">💬 Hablar con un especialista por WhatsApp para activar</a>'
        )
    payment_tracking_script = ""
    if track_payment_open:
        event_query = json.dumps(
            {"demo": name, "payments_opened": "1"}, ensure_ascii=True
        ).replace("<", "\\u003c")
        payment_tracking_script = f"""<script>
          document.querySelector(".payment-menu")?.addEventListener("toggle", function () {{
            if (this.open && window.top !== window) {{
              const query = new URLSearchParams({event_query});
              window.top.location.assign(window.top.location.pathname + "?" + query.toString());
            }}
          }});
        </script>"""
    details_open = " open" if payment_options_open else ""
    return f"""<!doctype html>
<html lang="es">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="description" content="{business}: {category} en {location}.">
    <title>{business} — {category}</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@400;500;600;700&family=Playfair+Display:wght@500;600;700&display=swap" rel="stylesheet">
    <style>
      body{{font-family:"DM Sans",sans-serif;padding-top:76px}}
      h1,h2,h3{{font-family:"Playfair Display",serif}}
      .preview-toolbar{{position:fixed;inset:0 0 auto;z-index:50;display:flex;align-items:center;justify-content:space-between;gap:1rem;background:#10271f;color:white;padding:.8rem clamp(1rem,4vw,3rem);box-shadow:0 8px 24px #0002}}
      .preview-toolbar p{{margin:0;font-size:.9rem;font-weight:600}}
      .payment-menu{{position:relative}}
      .payment-menu summary{{cursor:pointer;list-style:none;border-radius:999px;background:#d8f36a;color:#17220e;padding:.7rem 1rem;font-size:.85rem;font-weight:700;white-space:nowrap}}
      .payment-menu summary::-webkit-details-marker{{display:none}}
      .payment-list{{position:absolute;right:0;top:calc(100% + .65rem);display:grid;gap:.45rem;width:min(21rem,90vw);border:1px solid #e7e5e4;border-radius:1rem;background:white;padding:.85rem;box-shadow:0 16px 45px #0003;color:#1c1917}}
      .payment-link{{display:block;border-radius:.7rem;background:#f5f5f4;padding:.75rem;color:#14532d;font-size:.88rem;font-weight:700;text-decoration:none}}
      .payment-link:hover{{background:#e7f2e9}}
      .payment-link small{{display:block;margin-top:.2rem;color:#78716c;font-weight:400}}
      .whatsapp-float{{position:fixed;right:1.25rem;bottom:1.25rem;z-index:40;max-width:calc(100vw - 2.5rem);border-radius:999px;background:#15803d;padding:.9rem 1.2rem;color:white;font-size:.85rem;font-weight:700;text-align:center;text-decoration:none;box-shadow:0 8px 24px #0003}}
      .whatsapp-float:hover{{background:#166534}}
      @media(max-width:520px){{.preview-toolbar{{align-items:flex-start;flex-direction:column}}body{{padding-top:116px}}.payment-list{{left:0;right:auto}}}}
    </style>
  </head>
  <body class="bg-stone-50 text-stone-900 antialiased">
    <aside class="preview-toolbar">
      <p>⚡ Vista previa exclusiva creada por Apex AI Agency para {business}</p>
      <details class="payment-menu"{details_open}>
        <summary>🚀 Activar en mi dominio hoy</summary>
        <div class="payment-list" aria-label="Opciones de pago">
          {payment_options}
        </div>
      </details>
    </aside>
    <header class="mx-auto flex max-w-6xl items-center justify-between px-6 py-5">
      <a href="#" class="text-lg font-bold tracking-tight">{business}</a>
      <a href="#contacto" class="rounded-full bg-emerald-900 px-5 py-3 text-sm font-semibold text-white hover:bg-emerald-800">Solicitar una cita</a>
    </header>
    <main>
      <section class="mx-auto grid max-w-6xl items-center gap-10 px-6 py-14 md:grid-cols-2 md:py-24">
        <div>
          <p class="text-sm font-semibold uppercase tracking-[.18em] text-emerald-800">{location}</p>
          <h1 class="mt-5 text-5xl leading-tight sm:text-6xl">Un servicio cercano, hecho para ti.</h1>
          <p class="mt-6 max-w-lg text-lg leading-8 text-stone-600">Atención profesional en {category.lower()}, con soluciones claras y un trato personal en cada visita.</p>
          <a href="#contacto" class="mt-8 inline-flex rounded-full bg-emerald-900 px-7 py-4 font-semibold text-white hover:bg-emerald-800">Consulta disponibilidad <span class="ml-2" aria-hidden="true">→</span></a>
          <p class="mt-5 text-sm text-stone-500">Horario flexible · Atención local · Respuesta rápida</p>
        </div>
        <div class="rounded-[2rem] bg-emerald-100 p-4">
          <img class="h-80 w-full rounded-[1.5rem] object-cover md:h-[430px]" src="https://images.unsplash.com/photo-1600607687939-ce8a6c25118c?auto=format&fit=crop&w=1200&q=85" alt="Espacio luminoso y acogedor" />
        </div>
      </section>
      <section class="bg-white py-16">
        <div class="mx-auto max-w-6xl px-6">
          <p class="text-sm font-bold uppercase tracking-[.18em] text-emerald-800">Cómo podemos ayudarte</p>
          <h2 class="mt-3 text-4xl">Atención pensada para tu día a día.</h2>
          <div class="mt-9 grid gap-5 md:grid-cols-3">
            <article class="rounded-2xl border border-stone-200 p-6"><h3 class="text-2xl">Evaluación inicial</h3><p class="mt-3 leading-7 text-stone-600">Cuéntanos qué necesitas y te orientaremos con claridad.</p></article>
            <article class="rounded-2xl border border-stone-200 p-6"><h3 class="text-2xl">{category}</h3><p class="mt-3 leading-7 text-stone-600">Una solución profesional, adaptada a cada caso y a tus objetivos.</p></article>
            <article class="rounded-2xl border border-stone-200 p-6"><h3 class="text-2xl">Seguimiento personal</h3><p class="mt-3 leading-7 text-stone-600">Atención cercana antes, durante y después de tu visita.</p></article>
          </div>
        </div>
      </section>
      <section id="contacto" class="mx-auto max-w-6xl px-6 py-16">
        <div class="rounded-[2rem] bg-emerald-950 p-8 text-white md:p-12">
          <h2 class="text-4xl">¿Hablamos de lo que necesitas?</h2>
          <p class="mt-4 max-w-xl leading-7 text-emerald-100">Ponte en contacto para consultar horarios y encontrar el servicio adecuado para ti.</p>
          <p class="mt-7 font-semibold">{business} · {location}</p>
          <p class="mt-2 text-sm text-emerald-200">Prototipo de demostración: configura aquí el teléfono, el correo y el sistema de reservas reales.</p>
        </div>
      </section>
    </main>
    <footer class="border-t border-stone-200 px-6 py-6 text-center text-xs text-stone-500">© 2026 {business} · Concepto de demostración sin conexión de reservas.</footer>
    {whatsapp_button}
    {payment_tracking_script}
  </body>
</html>"""


def create_prototypes(
    niche: str, objective: str, payment_links: dict | None = None
) -> list[dict]:
    businesses = NICHES[niche]["businesses"]
    match = re.search(r"\b(\d{1,2})\s*(?:prototipos|prospectos|negocios)\b", objective, re.I)
    count = min(int(match.group(1)), 10) if match else 3
    count = max(count, 1)
    created = []
    for index in range(count):
        name, service = businesses[index % len(businesses)]
        unique_id = f"{slugify(name)}-{uuid.uuid4().hex[:8]}"
        folder = PENDING_DIR / unique_id
        folder.mkdir(parents=False, exist_ok=False)
        html_content = make_prototype_html(
            name,
            service,
            NICHES[niche]["zone"],
            payment_links,
            ceo_whatsapp_digits(),
        )
        (folder / "index.html").write_text(html_content, encoding="utf-8")
        record = {
            "id": unique_id,
            "business_name": name,
            "service": service,
            "niche": niche,
            "zone": NICHES[niche]["zone"],
            "objective": objective.strip(),
            "status": "pending",
            "note": "",
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "folder": str(folder.relative_to(ROOT)),
        }
        (folder / "proposal.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        created.append(record)
    return created


def safe_prototype_folder(record: dict, expected_parent: Path) -> Path:
    folder = (ROOT / record["folder"]).resolve()
    if folder.parent != expected_parent.resolve() or not folder.is_dir():
        raise ValueError(f"No se encuentra la carpeta local esperada para {record['business_name']}.")
    return folder


def approve_prototype(state: dict, record: dict) -> None:
    with STATE_LOCK:
        source = safe_prototype_folder(record, PENDING_DIR)
        destination = APPROVED_DIR / source.name
        if destination.exists():
            raise FileExistsError(f"Ya existe el destino de aprobación: {destination.name}")
        shutil.move(str(source), str(destination))
        record["status"] = "approved"
        record["approved_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        record["folder"] = str(destination.relative_to(ROOT))
        (destination / "proposal.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        save_state(state)


class EmailSentArchiveError(RuntimeError):
    pass


def dispatch_approved_proposal(
    record_id: str, recipient: str, subject: str, body: str
) -> str:
    enviar_correo_cliente(recipient, subject, body)
    try:
        with STATE_LOCK:
            state = load_state()
            record = next(
                item for item in state["prototypes"] if item.get("id") == record_id
            )
            if record.get("status") != "sending":
                raise ValueError("El estado de aprobación cambió durante el envío.")
            business_name = record["business_name"]
            approve_prototype(state, record)
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        try:
            with STATE_LOCK:
                state = load_state()
                record = next(
                    item
                    for item in state["prototypes"]
                    if item.get("id") == record_id
                )
                record["status"] = "sent_archive_failed"
                record["last_send_error"] = (
                    "Correo enviado, pero falló el archivado. No reenvíes sin revisar."
                )
                save_state(state)
        except (OSError, ValueError, KeyError, StopIteration) as recovery_error:
            raise EmailSentArchiveError(
                "El correo se envió, pero fallaron el archivado y la actualización "
                "del estado. Revisa los archivos antes de reenviar."
            ) from recovery_error
        raise EmailSentArchiveError(
            "El correo se envió, pero no se pudo archivar la propuesta. "
            "No vuelvas a enviarla sin revisar el estado."
        ) from exc

    enqueue_background_job(
        f"Notificación de propuesta enviada a {business_name}",
        notificar_ceo_whatsapp,
        f"✅ Propuesta enviada automáticamente a {business_name}",
    )
    return f"Correo enviado automáticamente a {business_name}."


def register_change_request(state: dict, record: dict, note: str) -> None:
    record["status"] = "changes_requested"
    record["note"] = note.strip()
    record["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    folder = safe_prototype_folder(record, PENDING_DIR)
    (folder / "proposal.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    save_state(state)


def public_demo_url(record: dict, settings: dict) -> str:
    base_url = valid_web_url(settings.get("prototype_base_url", ""))
    if not base_url or urlparse(base_url).scheme != "https":
        return ""
    parsed = urlparse(base_url)
    if parsed.hostname in ("localhost", "127.0.0.1", "::1"):
        return ""
    return urlunparse(
        parsed._replace(
            path=parsed.path or "/",
            query=urlencode({"demo": record["business_name"]}),
            fragment="",
        )
    )


def local_demo_url(record: dict) -> str:
    return f"http://127.0.0.1:8501/?{urlencode({'demo': record['business_name']})}"


def proposal_phase_one(record: dict, demo_link: str) -> str:
    return (
        f"Hola, equipo de {record['business_name']}:\n\n"
        "Notamos que podrían estar perdiendo clientes al no contar con un "
        "cotizador instantáneo disponible 24/7. Preparamos un prototipo "
        "interactivo y personalizado para que lo prueben en vivo:\n"
        f"{demo_link}\n\n"
        "¿Te gustaría que lo conectemos directo al WhatsApp de tu negocio esta semana?"
        f"\n\nUn saludo,\nEquipo de {AGENCY_NAME}"
    )


def proposal_phase_two(record: dict, settings: dict) -> str:
    payment_links = settings.get("payment_links", {})
    options = []
    for method, amount in (
        ("Binance Pay / USDT", "$380 USD · 5% de descuento"),
        ("Payoneer (Tarjeta/ACH)", "$400 USD"),
        ("PayPal", "$425 USD"),
    ):
        url = valid_web_url(payment_links.get(method, ""))
        link_line = f"\n  {url}" if url else "\n  Enlace oficial pendiente de configurar."
        options.append(f"• {method}: {amount}{link_line}")
    return (
        f"Hola, equipo de {record['business_name']}:\n\n"
        "¡Qué bueno que les interese! Podemos dejar su prototipo personalizado "
        "listo en su dominio en 24 horas, con el flujo de contacto adaptado a su negocio.\n\n"
        "Para comenzar, pueden elegir la opción que mejor les convenga:\n"
        + "\n".join(options)
        + f"\n\nUn saludo,\nEquipo de {AGENCY_NAME}"
    )


def message_action_links(
    record: dict,
    message: str,
    phase: str,
    recipient_email: str,
    recipient_phone: str,
) -> tuple[str, str]:
    phone_digits = re.sub(r"\D", "", recipient_phone)
    whatsapp = f"https://wa.me/{phone_digits}" if phone_digits else "https://wa.me/"
    whatsapp_url = f"{whatsapp}?{urlencode({'text': message})}"
    email = quote(recipient_email.strip(), safe="@.+-_")
    email_url = f"mailto:{email}?{urlencode({'subject': phase, 'body': message})}"
    return whatsapp_url, email_url


def show_prototype_preview(
    record: dict, key_prefix: str, settings: dict
) -> None:
    folder = ROOT / record["folder"]
    prototype_path = folder / "index.html"
    if not prototype_path.is_file():
        st.error(f"No se encuentra el prototipo local: {record['folder']}\\index.html")
        return
    prototype_html = make_prototype_html(
        record["business_name"],
        record["service"],
        record["zone"],
        settings.get("payment_links", {}),
        ceo_whatsapp_digits(),
    )
    share_url = public_demo_url(record, settings)
    col_preview, col_open, col_download = st.columns([1, 1, 1])
    with col_preview:
        if st.button("Vista previa del prototipo", key=f"{key_prefix}_preview"):
            st.session_state[f"{key_prefix}_show_preview"] = not st.session_state.get(
                f"{key_prefix}_show_preview", False
            )
    with col_open:
        st.link_button(
            "Abrir demo para compartir",
            share_url or local_demo_url(record),
            disabled=not share_url,
            help=(
                None
                if share_url
                else "Configura en la barra lateral una URL pública para habilitar un enlace compartible."
            ),
            use_container_width=True,
        )
    with col_download:
        st.download_button(
            "Descargar HTML",
            data=prototype_html,
            file_name=f"{record['id']}.html",
            mime="text/html",
            key=f"{key_prefix}_download",
        )
    if st.session_state.get(f"{key_prefix}_show_preview", False):
        st.iframe(prototype_html, height=760)
    if not share_url:
        st.caption(
            "Esta demo solo está disponible en este equipo. Configura una URL pública "
            "autorizada antes de compartirla con un prospecto."
        )
    st.caption(f"Archivo local: {record['folder']}\\index.html")


def render_factory_tab(state: dict) -> None:
    st.subheader("Lanzar una operación local")
    st.caption(
        "El investigador trabaja con prospectos de demostración predefinidos y "
        "el agente Dev crea páginas HTML locales. No se hace scraping ni se contacta a negocios."
    )
    selected_niche = st.selectbox("Nicho / zona", list(NICHES))
    objective = st.text_area(
        "Objetivo o instrucción estratégica del día",
        placeholder="Ej.: Crear 5 prototipos para empresas de aire acondicionado sin cotizador web.",
        height=110,
    )
    st.info(
        "Los nombres generados son simulados. Las propuestas quedarán en "
        "`02_BANDEJA_APROBACION/pendientes/` y requieren tu aprobación explícita."
    )
    if st.button(
        "🚀 Ejecutar Agentes en Segundo Plano",
        type="primary",
        use_container_width=True,
        disabled=not objective.strip(),
    ):
        progress = st.progress(0, text="Iniciando agentes locales…")
        with st.status("Operación de demostración en curso", expanded=True) as status:
            st.write("Agente Investigador: seleccionando prospectos simulados…")
            time.sleep(0.15)
            progress.progress(30, text="Prospectos de demostración preparados")
            st.write("Agente Dev: generando prototipos HTML locales…")
            try:
                created = create_prototypes(
                    selected_niche,
                    objective,
                    state["settings"].get("payment_links", {}),
                )
                state["prototypes"].extend(created)
                save_state(state)
            except OSError as exc:
                st.error(f"No se pudieron guardar los archivos locales: {exc}")
                status.update(label="La operación no pudo guardarse", state="error")
                progress.empty()
                return
            progress.progress(100, text="Prototipos guardados en la bandeja de aprobación")
            status.update(
                label=f"Listo: {len(created)} prototipo(s) local(es) creados",
                state="complete",
            )
        enqueue_background_job(
            "Notificación del lote de prototipos",
            notificar_ceo_whatsapp,
            f"🏭 Agentes terminaron {len(created)} prototipos. Entra a tu app para aprobarlos",
        )
        st.success(
            f"{len(created)} prototipo(s) listo(s). La notificación al CEO quedó en cola."
        )
        st.rerun()

    st.divider()
    st.markdown("**Operaciones recientes**")
    recent = sorted(
        state["prototypes"], key=lambda item: item.get("created_at", ""), reverse=True
    )[:5]
    if not recent:
        st.caption("Aún no se han lanzado operaciones.")
    else:
        for record in recent:
            st.markdown(
                f"- **{record['business_name']}** · {record['zone']} · "
                f"{STATUS_LABELS.get(record['status'], record['status'])}"
            )


def render_approval_tab(state: dict) -> None:
    st.subheader("Bandeja de aprobación del CEO")
    st.warning(
        "Human-in-the-Loop: el clic de aprobación envía por SMTP el correo de Fase 1 "
        "al prospecto y archiva la propuesta. Verifica el destinatario y el prototipo antes de aprobar."
    )
    pending = [
        record
        for record in state["prototypes"]
        if record.get("status") in ("pending", "changes_requested")
    ]
    if not pending:
        st.success("No hay propuestas pendientes. Lanza una operación para generar prototipos.")
    archive_attention = [
        record
        for record in state["prototypes"]
        if record.get("status") == "sent_archive_failed"
    ]
    for record in archive_attention:
        st.error(
            f"{record['business_name']}: el correo fue enviado, pero el archivado "
            "requiere revisión manual. No vuelvas a despacharlo."
        )
    for record in pending:
        key = record["id"]
        with st.container(border=True):
            title_col, status_col = st.columns([3, 1])
            with title_col:
                st.markdown(f"### {record['business_name']}")
                st.caption(f"{record['service']} · {record['zone']} · Prospecto simulado")
            with status_col:
                st.markdown(
                    f"**{STATUS_LABELS.get(record['status'], record['status'])}**"
                )
            if record.get("note"):
                st.info(f"Nota de ajustes: {record['note']}")
            show_prototype_preview(record, key, state["settings"])
            email_col, phone_col, save_col = st.columns([2, 2, 1])
            with email_col:
                recipient_email = st.text_input(
                    "Correo del prospecto",
                    value=record.get("contact_email", ""),
                    key=f"email_{key}",
                )
            with phone_col:
                recipient_phone = st.text_input(
                    "WhatsApp del prospecto (opcional)",
                    value=record.get("contact_whatsapp", ""),
                    key=f"phone_{key}",
                    placeholder="+1 305 555 0100",
                )
            with save_col:
                st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                if st.button("Guardar contacto", key=f"save_contact_{key}"):
                    record["contact_email"] = recipient_email.strip()
                    record["contact_whatsapp"] = recipient_phone.strip()
                    save_state(state)
                    st.session_state["flash_message"] = (
                        f"Contacto guardado para {record['business_name']}."
                    )
                    st.rerun()

            share_url = public_demo_url(record, state["settings"])
            demo_link = share_url or "[configura una URL pública para compartir esta demo]"
            phase_one_tab, phase_two_tab = st.tabs(
                [
                    "Fase 1: Mensaje de Gancho (Enviar Prototipo)",
                    "Fase 2: Mensaje de Cierre y Cobro (Enviar Links de Pago)",
                ]
            )
            with phase_one_tab:
                phase_one_message = proposal_phase_one(record, demo_link)
                st.code(phase_one_message, language="text")
                wa_url, email_url = message_action_links(
                    record,
                    phase_one_message,
                    "Tu prototipo personalizado",
                    record.get("contact_email", ""),
                    record.get("contact_whatsapp", ""),
                )
                whatsapp_col, email_col = st.columns(2)
                with whatsapp_col:
                    st.link_button(
                        "Abrir WhatsApp — Mensaje 1 (Prototipo)",
                        wa_url,
                        disabled=not share_url,
                        use_container_width=True,
                    )
                with email_col:
                    st.link_button(
                        "Abrir Correo — Mensaje 1 (Prototipo)",
                        email_url,
                        disabled=not share_url,
                        use_container_width=True,
                    )
            with phase_two_tab:
                phase_two_message = proposal_phase_two(record, state["settings"])
                payment_ready = all(
                    valid_web_url(url)
                    for url in state["settings"]["payment_links"].values()
                )
                st.code(phase_two_message, language="text")
                wa_url, email_url = message_action_links(
                    record,
                    phase_two_message,
                    "Siguiente paso para activar su web",
                    record.get("contact_email", ""),
                    record.get("contact_whatsapp", ""),
                )
                whatsapp_col, email_col = st.columns(2)
                with whatsapp_col:
                    st.link_button(
                        "Abrir WhatsApp — Mensaje 2 (Links de Pago)",
                        wa_url,
                        disabled=not payment_ready,
                        help=(
                            None
                            if record.get("contact_whatsapp", "").strip()
                            else "WhatsApp se abrirá para que elijas el destinatario."
                        ),
                        use_container_width=True,
                    )
                with email_col:
                    st.link_button(
                        "Abrir Correo — Mensaje 2 (Links de Pago)",
                        email_url,
                        disabled=(
                            not payment_ready
                            or not record.get("contact_email", "").strip()
                        ),
                        use_container_width=True,
                    )
                if not payment_ready:
                    st.caption(
                        "Configura las tres URLs oficiales de pago en la barra lateral "
                        "para activar los botones de envío de esta fase."
                    )
                else:
                    st.caption(
                        "Los botones abren un borrador en tu aplicación; revisa el contenido "
                        "y envíalo manualmente. No se envían mensajes desde el dashboard."
                    )
            if record.get("last_send_error"):
                st.error(f"Último intento: {record['last_send_error']}")
            approve_col, reject_col = st.columns(2)
            with approve_col:
                if st.button(
                    "✅ Aprobar y Despachar Automáticamente",
                    key=f"approve_{key}",
                    type="primary",
                    use_container_width=True,
                ):
                    if not re.fullmatch(
                        r"[^@\s]+@[^@\s]+\.[^@\s]+", recipient_email.strip()
                    ):
                        st.error("Introduce un correo válido antes de aprobar.")
                    elif not share_url:
                        st.error(
                            "Configura y autoriza una URL pública para la demo antes de enviar."
                        )
                    else:
                        record["contact_email"] = recipient_email.strip()
                        record["contact_whatsapp"] = recipient_phone.strip()
                        record["status"] = "sending"
                        record.pop("last_send_error", None)
                        save_state(state)
                        enqueue_background_job(
                            f"Envío de propuesta a {record['business_name']}",
                            dispatch_approved_proposal,
                            record["id"],
                            record["contact_email"],
                            f"Una idea para {record['business_name']}",
                            proposal_phase_one(record, share_url),
                            kind="approval",
                            record_id=record["id"],
                        )
                        st.success(
                            f"Correo encolado para {record['business_name']}; "
                            "se enviará en segundo plano."
                        )
            with reject_col:
                with st.form(f"reject_form_{key}"):
                    note = st.text_input(
                        "Nota rápida para el agente",
                        placeholder="Ej.: Cambiar colores y hacer más visible la reserva.",
                    )
                    submitted = st.form_submit_button(
                        "❌ Rechazar / Pedir Ajustes",
                        use_container_width=True,
                    )
                    if submitted:
                        if not note.strip():
                            st.warning("Escribe una nota para indicar qué debe cambiar.")
                        else:
                            try:
                                register_change_request(state, record, note)
                            except (OSError, ValueError, KeyError) as exc:
                                st.error(f"No se pudo guardar la nota de ajustes: {exc}")
                            else:
                                st.session_state["flash_message"] = (
                                    f"Se guardó la solicitud de ajustes para {record['business_name']}."
                                )
                                st.rerun()
    approved = [
        record
        for record in state["prototypes"]
        if record.get("status") == "approved"
    ]
    if approved:
        st.divider()
        with st.expander(f"Archivo aprobado ({len(approved)})"):
            for record in sorted(
                approved,
                key=lambda item: item.get("approved_at", ""),
                reverse=True,
            ):
                st.markdown(
                    f"- **{record['business_name']}** · {record['zone']} · "
                    f"aprobado {record.get('approved_at', '')[:10]}"
                )
                show_prototype_preview(
                    record, f"approved_{record['id']}", state["settings"]
                )


def render_finance_tab(state: dict) -> None:
    st.subheader("Control financiero y mantenimiento")
    st.caption(
        "Registro interno de ingresos recibidos; este panel no procesa pagos. "
        "El Modelo B representa mantenimiento recurrente de $100 USD/mes por cliente activo."
    )
    totals = {
        method: sum(
            item.get("amount_cents", 0)
            for item in state["income"]
            if item.get("method") == method
        )
        for method in PAYMENT_OPTIONS
    }
    metric_columns = st.columns(3)
    for column, method in zip(metric_columns, PAYMENT_OPTIONS):
        with column:
            st.metric(method, money(totals[method]))
    st.metric(
        "Ingresos acumulados",
        money(sum(totals.values())),
        help="Suma de los importes registrados manualmente en este panel.",
    )
    with st.expander("Registrar un ingreso recibido"):
        with st.form("income_form", clear_on_submit=True):
            payment_method = st.selectbox("Método de cobro", list(PAYMENT_OPTIONS))
            amount = st.number_input(
                "Importe recibido (USD)",
                min_value=0.01,
                value=PAYMENT_OPTIONS[payment_method] / 100,
                step=10.0,
            )
            description = st.text_input(
                "Referencia (opcional)", placeholder="Cliente o factura"
            )
            submitted = st.form_submit_button("Guardar ingreso", type="primary")
            if submitted:
                state["income"].append(
                    {
                        "id": uuid.uuid4().hex,
                        "method": payment_method,
                        "amount_cents": round(amount * 100),
                        "description": description.strip(),
                        "date": date.today().isoformat(),
                    }
                )
                save_state(state)
                st.session_state["flash_message"] = "Ingreso registrado localmente."
                st.rerun()
    if state["income"]:
        st.markdown("**Últimos ingresos registrados**")
        for item in sorted(
            state["income"], key=lambda entry: entry.get("date", ""), reverse=True
        )[:10]:
            reference = f" · {item['description']}" if item.get("description") else ""
            st.markdown(
                f"- {item.get('date', '')} · {item['method']} · "
                f"**{money(item['amount_cents'])}**{reference}"
            )

    st.divider()
    active_clients = [client for client in state["clients"] if client.get("active", True)]
    st.markdown("### Clientes activos · Modelo B")
    st.metric("Mantenimiento mensual recurrente", money(len(active_clients) * 10000))
    if not active_clients:
        st.caption("Todavía no hay clientes activos de mantenimiento.")
    else:
        for client in active_clients:
            client_col, date_col, action_col = st.columns([2, 1, 1])
            with client_col:
                st.markdown(f"**{client['name']}**")
            with date_col:
                st.caption(f"Desde {client['start_date']}")
            with action_col:
                if st.button("Marcar inactivo", key=f"inactive_{client['id']}"):
                    client["active"] = False
                    client["inactive_at"] = date.today().isoformat()
                    save_state(state)
                    st.rerun()
    with st.expander("Añadir cliente de mantenimiento"):
        with st.form("maintenance_client_form", clear_on_submit=True):
            client_name = st.text_input("Nombre del cliente")
            start_date = st.date_input("Inicio del mantenimiento", value=date.today())
            submitted = st.form_submit_button("Añadir cliente", type="primary")
            if submitted:
                if not client_name.strip():
                    st.warning("El nombre del cliente es obligatorio.")
                else:
                    state["clients"].append(
                        {
                            "id": uuid.uuid4().hex,
                            "name": client_name.strip(),
                            "start_date": start_date.isoformat(),
                            "monthly_amount_cents": 10000,
                            "active": True,
                        }
                    )
                    save_state(state)
                    st.session_state["flash_message"] = (
                        f"{client_name.strip()} añadido al Modelo B."
                    )
                    st.rerun()


def render_sidebar_settings(state: dict) -> None:
    settings = state["settings"]
    with st.sidebar:
        st.markdown("# 🎛️ CEO")
        st.markdown(f"### {AGENCY_NAME}")
        st.caption("Operaciones locales · Aprobación humana")
        with st.form("agency_settings_form"):
            prototype_base_url = st.text_input(
                "URL pública autorizada para demos",
                value=settings.get("prototype_base_url", ""),
                placeholder="https://demos.apexaiagency.com",
                help=(
                    "Base de una instancia autorizada que sirve app_ceo.py. No uses "
                    "localhost para compartir con prospectos."
                ),
            )
            st.markdown("**Enlaces oficiales de pago**")
            payment_links = {}
            for method in PAYMENT_OPTIONS:
                payment_links[method] = st.text_input(
                    method,
                    value=settings["payment_links"].get(method, ""),
                    placeholder="https://…",
                    key=f"payment_url_{slugify(method)}",
                )
            save_settings = st.form_submit_button(
                "Guardar configuración", use_container_width=True
            )
            if save_settings:
                invalid_urls = [
                    label
                    for label, url in [
                        ("URL pública de demos", prototype_base_url),
                        *payment_links.items(),
                    ]
                    if url.strip()
                    and (
                        not valid_web_url(url)
                        or urlparse(url).scheme != "https"
                    )
                ]
                if invalid_urls:
                    st.error(
                        "Usa URLs HTTPS completas para la demo y los enlaces de pago: "
                        + ", ".join(invalid_urls)
                    )
                else:
                    settings["prototype_base_url"] = prototype_base_url.strip()
                    settings["payment_links"] = {
                        method: url.strip() for method, url in payment_links.items()
                    }
                    save_state(state)
                    st.session_state["flash_message"] = (
                        "Enlaces de demo y pago guardados localmente."
                    )
                    st.rerun()
        st.warning(
            "Enlaces de demo externos solo después de aprobar el despliegue. "
            "No compartas públicamente una instancia del CEO Dashboard sin protegerla."
        )
        st.divider()
        st.markdown("**Estado de la fábrica**")
        pending_count = sum(
            record.get("status") in ("pending", "changes_requested", "sending")
            for record in state["prototypes"]
        )
        st.metric("Propuestas por revisar", pending_count)
        st.caption(
            "Los mensajes salen solo tras acciones explícitas; los pagos no se procesan aquí."
        )
        if st.button(
            "🔔 Probar Notificación a mi WhatsApp",
            use_container_width=True,
        ):
            enqueue_background_job(
                "Prueba de notificación Green-API",
                notificar_ceo_whatsapp,
                "🔔 Prueba de conexión del CEO Command Center: Green-API está configurada.",
            )
            st.success("Prueba enviada a la cola; revisa el estado abajo.")


@st.fragment(run_every="2s")
def render_background_jobs() -> None:
    with BACKGROUND_JOBS_LOCK:
        jobs = list(BACKGROUND_JOBS.values())
    refresh_app = False
    for job in jobs:
        future: Future = job["future"]
        if job["status"] in ("queued", "running") and future.done():
            try:
                result = future.result()
            except EmailSentArchiveError as exc:
                job["status"] = "failed"
                job["error"] = str(exc)
                refresh_app = True
            except (RuntimeError, ValueError, OSError, KeyError) as exc:
                job["status"] = "failed"
                job["error"] = str(exc)
                if job["kind"] == "approval":
                    with STATE_LOCK:
                        state = load_state()
                        record = next(
                            (
                                item
                                for item in state["prototypes"]
                                if item.get("id") == job["record_id"]
                            ),
                            None,
                        )
                        if record and record.get("status") == "sending":
                            record["status"] = "pending"
                            record["last_send_error"] = str(exc)
                            save_state(state)
                    refresh_app = True
            else:
                if result:
                    job["status"] = "warning"
                    job["warning"] = str(result)
                else:
                    job["status"] = "succeeded"
                    job["result"] = "Tarea completada."
                refresh_app = refresh_app or job["kind"] == "approval"
        elif job["status"] == "queued" and future.running():
            job["status"] = "running"

    with BACKGROUND_JOBS_LOCK:
        visible_jobs = list(BACKGROUND_JOBS.values())[-10:]
    if visible_jobs:
        st.markdown("#### Actividades en segundo plano")
        for job in reversed(visible_jobs):
            if job["status"] in ("queued", "running"):
                st.info(f"⏳ {job['label']}: procesando…")
            elif job["status"] == "succeeded":
                st.success(f"{job['label']}: {job.get('result', 'Completado.')}")
            elif job["status"] == "warning":
                st.warning(f"{job['label']}: {job['warning']}")
            else:
                st.error(f"{job['label']}: {job['error']}")
    if refresh_app:
        st.rerun()


def find_demo_record(state: dict, demo_query: str) -> dict | None:
    normalized = demo_query.strip().casefold()
    exact_matches = [
        record
        for record in state["prototypes"]
        if record.get("id") == demo_query
        or record.get("business_name", "").casefold() == normalized
    ]
    if exact_matches:
        return exact_matches[0] if len(exact_matches) == 1 else None
    slug_matches = [
        record
        for record in state["prototypes"]
        if slugify(record.get("business_name", "")) == slugify(demo_query)
    ]
    return slug_matches[0] if len(slug_matches) == 1 else None


def render_demo_view(
    record: dict, state: dict, payment_options_open: bool = False
) -> None:
    st.markdown(
        """
        <style>
        [data-testid="stSidebar"], [data-testid="stHeader"], footer {display:none !important}
        .block-container {padding:0 !important; max-width:none !important}
        </style>
        """,
        unsafe_allow_html=True,
    )
    st.title(f"Vista previa exclusiva para {record['business_name']}")
    if not ceo_whatsapp_digits():
        st.warning(
            "El contacto de WhatsApp del especialista no está disponible; "
            "configura CEO_WHATSAPP en Streamlit secrets."
        )
    st.caption(
        "Concepto de demostración. La activación requiere aprobación explícita, "
        "configuración de enlaces y publicación autorizada."
    )
    st.iframe(
        make_prototype_html(
            record["business_name"],
            record["service"],
            record["zone"],
            state["settings"].get("payment_links", {}),
            ceo_whatsapp_digits(),
            payment_options_open=payment_options_open,
            track_payment_open=not payment_options_open,
        ),
        height=1100,
    )


def require_ceo_login() -> bool:
    if st.session_state.get("ceo_authenticated", False):
        return True
    try:
        expected_password = required_secret("CEO_DASHBOARD_PASSWORD")
    except RuntimeError as exc:
        st.error(
            f"{exc} El panel administrativo no se habilita sin contraseña, "
            "para proteger datos financieros y operaciones al compartir demos."
        )
        return False
    st.title("Acceso CEO")
    st.caption("Inicia sesión para abrir el panel administrativo.")
    with st.form("ceo_login_form"):
        password = st.text_input("Contraseña del CEO", type="password")
        submitted = st.form_submit_button("Entrar", type="primary")
    if submitted:
        if hmac.compare_digest(password, expected_password):
            st.session_state["ceo_authenticated"] = True
            st.rerun()
        st.error("Contraseña incorrecta.")
    return False


def main() -> None:
    st.set_page_config(
        page_title="CEO Command Center",
        page_icon="🎛️",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    st.markdown(
        """
        <style>
        .stApp { background: #0b1017; }
        [data-testid="stHeader"] { background: rgba(11, 16, 23, .85); }
        [data-testid="stSidebar"] { background: #101824; border-right: 1px solid #263243; }
        [data-testid="stMetric"] {
            background: #121b27; border: 1px solid #253345;
            padding: 1rem 1.1rem; border-radius: 14px;
        }
        [data-testid="stVerticalBlockBorderWrapper"] {
            background: #101824; border-color: #293648;
            border-radius: 18px; padding: .7rem;
        }
        .stButton > button, .stDownloadButton > button {
            border-radius: 10px; min-height: 2.8rem; font-weight: 650;
        }
        .stTextArea textarea, .stTextInput input, [data-baseweb="select"] > div {
            background: #121b27;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )
    try:
        state = load_state()
    except (OSError, ValueError) as exc:
        st.error(f"No se pudieron cargar los datos locales: {exc}")
        st.stop()

    demo_id = st.query_params.get("demo")
    if demo_id:
        payments_opened = st.query_params.get("payments_opened") == "1"
        demo_record = find_demo_record(state, demo_id)
        if demo_record is None:
            st.error("No se encontró una demo con ese identificador.")
            st.stop()
        if (
            not payments_opened
            and st.session_state.get("notified_demo_open") != demo_record["id"]
        ):
            st.session_state["notified_demo_open"] = demo_record["id"]
            enqueue_background_job(
                f"Alerta de apertura de demo: {demo_record['business_name']}",
                notificar_ceo_whatsapp,
                f"👀 ¡Alerta CEO! El negocio {demo_record['business_name']} acaba de abrir su prototipo en vivo",
            )
        if payments_opened and st.session_state.get("notified_payments_open") != demo_record["id"]:
            st.session_state["notified_payments_open"] = demo_record["id"]
            enqueue_background_job(
                f"Opciones de pago abiertas: {demo_record['business_name']}",
                notificar_ceo_whatsapp,
                f"💳 ¡Alerta CEO! El negocio {demo_record['business_name']} desplegó las opciones de pago de su prototipo",
            )
        render_demo_view(demo_record, state, payments_opened)
        st.stop()

    if not require_ceo_login():
        st.stop()

    render_sidebar_settings(state)
    st.title(f"{AGENCY_NAME} — CEO Command Center")
    st.caption("Centro de mando de Apex AI Agency para prototipos, aprobaciones y operaciones.")
    flash = st.session_state.pop("flash_message", None)
    if flash:
        st.success(flash)

    factory_tab, approval_tab, finance_tab = st.tabs(
        [
            "🏭 Lanzar Operación (Fábrica)",
            "🛡️ Bandeja de Aprobación del CEO (Human-in-the-Loop)",
            "💰 Control Financiero y Mantenimiento ($)",
        ]
    )
    with factory_tab:
        render_factory_tab(state)
    with approval_tab:
        render_approval_tab(state)
    with finance_tab:
        render_finance_tab(state)
    render_background_jobs()


if __name__ == "__main__":
    main()
