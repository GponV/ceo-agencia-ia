from __future__ import annotations

import html
import hmac
import json
import os
import re
import shutil
import smtplib
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import urlencode, urlparse, urlunparse

import requests
import streamlit as st


ROOT = Path(__file__).resolve().parent
configured_leads_db = Path(
    os.environ.get(
        "APEX_LEADS_DB_PATH",
        str(ROOT / "apex_scraper" / "prospectos_apex_scraping.db"),
    )
).expanduser()
LEADS_DB_PATH = (
    configured_leads_db
    if configured_leads_db.is_absolute()
    else ROOT / configured_leads_db
).resolve()
SCRAPER_DIR = ROOT / "apex_scraper"
SCRAPER_DB_PATH = SCRAPER_DIR / "prospectos_apex_scraping.db"
LEAD_FUNNEL_STAGES = {
    "CONTACTADO_FASE_1",
    "PROPUESTA_PAGO_ENVIADA",
    "CLIENTE_CERRADO",
}
PENDING_DIR = ROOT / "02_BANDEJA_APROBACION" / "pendientes"
APPROVED_DIR = ROOT / "02_BANDEJA_APROBACION" / "aprobados"
DATA_DIR = ROOT / ".ceo_dashboard"
STATE_FILE = DATA_DIR / "state.json"
AGENCY_NAME = "Apex AI Agency"
SUPPORT_EMAIL = "hello.apexrepurposing@gmail.com"
BRAND_LOGO_SVG = """
<svg class="apex-mark" viewBox="0 0 48 48" role="img" aria-label="Apex AI Agency">
  <path d="M24 3.5 42 14v20L24 44.5 6 34V14L24 3.5Z" fill="none" stroke="#00F2FE" stroke-width="2"/>
  <path d="m14 31 10-17 10 17M18.2 24h11.6" fill="none" stroke="#00F2FE" stroke-linecap="round" stroke-linejoin="round" stroke-width="2.8"/>
  <circle cx="35.5" cy="12.5" r="2.5" fill="#10B981"/>
</svg>
"""
CONTRACT_STATUSES = {
    "pending_verification": "Pago reportado · pendiente de verificación",
    "paid_activation": "Pagado · en activación",
    "delivered_active": "Entregado y activo",
}


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


def pending_prototype_count(state: dict) -> int:
    return sum(
        record.get("status") in ("pending", "changes_requested")
        for record in state["prototypes"]
    )


def leads_db_display_path() -> str:
    try:
        return str(LEADS_DB_PATH.relative_to(ROOT))
    except ValueError:
        return str(LEADS_DB_PATH)


def normalize_search_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(character for character in normalized if not unicodedata.combining(character))


def is_financial_business(name: str, web: str, market: str) -> bool:
    searchable = normalize_search_text(f"{name} {web} {market}")
    return any(
        term in searchable
        for term in (
            "bancrecer",
            "banco",
            "bank",
            "fintech",
            "credito",
            "bancario",
            "financial",
            "finance",
        )
    )


def clean_lead_email(value: str) -> str:
    invalid_values = {"", "pendiente", "no detectado", "no disponible", "none", "null"}
    for candidate in re.split(r"[,;\s]+", str(value or "")):
        email = candidate.strip()
        if (
            email.casefold() not in invalid_values
            and re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email)
        ):
            return email
    return ""


def clean_lead_phone(value: str) -> str:
    invalid_values = {"", "pendiente", "no detectado", "no disponible", "none", "null"}
    for candidate in re.split(r"[,;]+", str(value or "")):
        phone = candidate.strip()
        if phone.casefold() in invalid_values:
            continue
        if sum(character.isdigit() for character in phone) >= 7:
            return phone
    return ""


def _open_leads_database(read_only: bool = True) -> sqlite3.Connection:
    if not LEADS_DB_PATH.is_file():
        raise FileNotFoundError(
            f"No se encontró la base de prospectos: {leads_db_display_path()}."
        )
    if read_only:
        connection = sqlite3.connect(
            f"{LEADS_DB_PATH.as_uri()}?mode=ro", uri=True, timeout=10
        )
    else:
        connection = sqlite3.connect(LEADS_DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def load_prospect_data() -> tuple[dict[str, int], list[dict]]:
    try:
        connection = _open_leads_database()
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"No se pudo abrir la base de prospectos "
            f"{leads_db_display_path()}: {exc}"
        ) from exc
    try:
        connection.execute("BEGIN")
        stats_row = connection.execute(
            """
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN UPPER(COALESCE(estado, '')) LIKE 'ALTA%' THEN 1 ELSE 0 END) AS high_priority,
                SUM(CASE WHEN UPPER(COALESCE(estado, '')) LIKE 'MEDIA%' THEN 1 ELSE 0 END) AS medium_priority
            FROM leads_scraping
            """
        ).fetchone()
        rows = connection.execute(
            """
            SELECT id, nombre, direccion, telefono, web, mercado, estado, email
            FROM leads_scraping
            ORDER BY nombre COLLATE NOCASE
            """
        ).fetchall()
        has_proposal_tracking = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'lead_proposals'
            """
        ).fetchone()
        if has_proposal_tracking:
            tracking_columns = {
                column["name"]
                for column in connection.execute(
                    "PRAGMA table_info(lead_proposals)"
                ).fetchall()
            }
            priority_column = (
                "priority_status"
                if "priority_status" in tracking_columns
                else "'' AS priority_status"
            )
            tracking_rows = connection.execute(
                f"""
                SELECT lead_id, status, {priority_column}, updated_at
                FROM lead_proposals
                ORDER BY updated_at, rowid
                """
            ).fetchall()
        else:
            tracking_rows = []
        connection.commit()
    except sqlite3.Error as exc:
        connection.rollback()
        raise RuntimeError(
            f"No se pudo consultar la tabla leads_scraping en "
            f"{leads_db_display_path()}: {exc}"
        ) from exc
    finally:
        connection.close()

    tracked_by_lead: dict[int, dict] = {}
    for tracking_row in tracking_rows:
        tracked_by_lead[int(tracking_row["lead_id"])] = dict(tracking_row)

    qualified_leads = []
    high_priority_count = 0
    medium_priority_count = 0
    contacted_count = 0
    closing_count = 0
    closed_count = 0
    for row in rows:
        lead = dict(row)
        lead["email"] = clean_lead_email(lead["email"])
        lead["telefono"] = clean_lead_phone(lead["telefono"])
        if is_financial_business(
            lead["nombre"], lead["web"], lead["mercado"]
        ):
            continue
        if not lead["email"] and not lead["telefono"]:
            continue
        tracked = tracked_by_lead.get(int(lead["id"]), {})
        raw_status = str(lead["estado"] or "")
        priority_status = str(tracked.get("priority_status") or "")
        if raw_status.upper().startswith(("ALTA", "MEDIA")):
            priority_status = raw_status
        funnel_status = str(tracked.get("status") or "")
        if raw_status in LEAD_FUNNEL_STAGES and funnel_status not in LEAD_FUNNEL_STAGES:
            funnel_status = raw_status
        if not priority_status.upper().startswith(("ALTA", "MEDIA")):
            continue
        lead["prioridad"] = priority_status
        lead["funnel_status"] = funnel_status or "SIN CONTACTAR"
        if priority_status.upper().startswith("ALTA"):
            high_priority_count += 1
        elif priority_status.upper().startswith("MEDIA"):
            medium_priority_count += 1
        if lead["funnel_status"] == "CONTACTADO_FASE_1":
            contacted_count += 1
        elif lead["funnel_status"] == "PROPUESTA_PAGO_ENVIADA":
            closing_count += 1
        elif lead["funnel_status"] == "CLIENTE_CERRADO":
            closed_count += 1
        qualified_leads.append(lead)

    stats = {
        "total": int(stats_row["total"] or 0),
        "clean": len(qualified_leads),
        "high_priority": high_priority_count,
        "medium_priority": medium_priority_count,
        "qualified": len(qualified_leads),
        "contacted_phase_one": contacted_count,
        "payment_proposal_sent": closing_count,
        "closed_clients": closed_count,
    }
    return stats, qualified_leads


def update_lead_proposal_status(
    lead_id: int, proposal_id: str, status: str, business_name: str
) -> None:
    try:
        connection = _open_leads_database(read_only=False)
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"No se pudo abrir la base para registrar la propuesta: {exc}"
        ) from exc
    try:
        with connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lead_proposals (
                    proposal_id TEXT PRIMARY KEY,
                    lead_id INTEGER NOT NULL,
                    business_name TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    priority_status TEXT NOT NULL DEFAULT ''
                )
                """
            )
            tracking_columns = {
                column["name"]
                for column in connection.execute(
                    "PRAGMA table_info(lead_proposals)"
                ).fetchall()
            }
            if "priority_status" not in tracking_columns:
                connection.execute(
                    "ALTER TABLE lead_proposals ADD COLUMN "
                    "priority_status TEXT NOT NULL DEFAULT ''"
                )
            lead_row = connection.execute(
                "SELECT estado FROM leads_scraping WHERE id = ?", (lead_id,)
            ).fetchone()
            if lead_row is None:
                raise ValueError(f"No existe el lead SQLite con ID {lead_id}.")
            existing_tracking = connection.execute(
                """
                SELECT priority_status FROM lead_proposals
                WHERE proposal_id = ?
                """,
                (proposal_id,),
            ).fetchone()
            current_status = str(lead_row["estado"] or "")
            priority_status = (
                current_status
                if current_status.upper().startswith(("ALTA", "MEDIA"))
                else (
                    str(existing_tracking["priority_status"] or "")
                    if existing_tracking
                    else ""
                )
            )
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            connection.execute(
                """
                INSERT INTO lead_proposals
                    (proposal_id, lead_id, business_name, status, created_at,
                     updated_at, priority_status)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(proposal_id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at,
                    priority_status = CASE
                        WHEN excluded.priority_status != ''
                        THEN excluded.priority_status
                        ELSE lead_proposals.priority_status
                    END
                """,
                (
                    proposal_id,
                    lead_id,
                    business_name,
                    status,
                    now,
                    now,
                    priority_status,
                ),
            )
            connection.execute(
                "UPDATE leads_scraping SET estado = ? WHERE id = ?",
                (status, lead_id),
            )
            if status == "CLIENTE_CERRADO":
                connection.execute(
                    """
                    UPDATE lead_proposals
                    SET status = ?, updated_at = ?
                    WHERE lead_id = ?
                    """,
                    (status, now, lead_id),
                )
    except sqlite3.Error as exc:
        raise RuntimeError(
            f"No se pudo registrar el seguimiento de la propuesta del lead "
            f"{lead_id} en SQLite: {exc}"
        ) from exc
    finally:
        connection.close()


def mark_matching_leads_as_closed(business_name: str, customer_email: str) -> int:
    try:
        connection = _open_leads_database(read_only=True)
    except sqlite3.Error as exc:
        raise RuntimeError(f"No se pudo abrir la base para cerrar el lead: {exc}") from exc
    try:
        matches = connection.execute(
            "SELECT id, nombre, email FROM leads_scraping"
        ).fetchall()
        has_tracking = connection.execute(
            """
            SELECT 1 FROM sqlite_master
            WHERE type = 'table' AND name = 'lead_proposals'
            """
        ).fetchone()
        lead_matches = []
        normalized_business = normalize_search_text(business_name)
        normalized_email = customer_email.strip().casefold()
        for lead in matches:
            emails = {
                item.strip().casefold()
                for item in re.split(r"[,;]+", str(lead["email"] or ""))
                if "@" in item
            }
            if (
                (normalized_business and normalize_search_text(lead["nombre"]) == normalized_business)
                or (normalized_email and normalized_email in emails)
            ):
                lead_matches.append(
                    {
                        "id": int(lead["id"]),
                        "nombre": str(lead["nombre"] or business_name),
                    }
                )
        proposals_by_lead: dict[int, str] = {}
        if has_tracking and lead_matches:
            placeholders = ",".join("?" for _ in lead_matches)
            tracking_rows = connection.execute(
                f"""
                SELECT lead_id, proposal_id, updated_at
                FROM lead_proposals
                WHERE lead_id IN ({placeholders})
                ORDER BY updated_at
                """,
                tuple(lead["id"] for lead in lead_matches),
            ).fetchall()
            for tracking in tracking_rows:
                proposals_by_lead[int(tracking["lead_id"])] = str(
                    tracking["proposal_id"]
                )
    except sqlite3.Error as exc:
        raise RuntimeError(f"No se pudieron buscar leads para cerrar: {exc}") from exc
    finally:
        connection.close()

    for lead in lead_matches:
        proposal_id = proposals_by_lead.get(lead["id"]) or (
            f"closed-sale-{lead['id']}-{uuid.uuid4().hex[:8]}"
        )
        update_lead_proposal_status(
            lead["id"],
            proposal_id,
            "CLIENTE_CERRADO",
            lead["nombre"],
        )
    return len(lead_matches)


def backup_leads_database() -> Path:
    backup_dir = DATA_DIR / "database_backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / (
        f"prospectos_apex_scraping_before_purge_"
        f"{datetime.now().astimezone().strftime('%Y%m%d_%H%M%S_%f')}.db"
    )
    source = _open_leads_database(read_only=True)
    destination = sqlite3.connect(backup_path, timeout=10)
    try:
        source.backup(destination)
    except sqlite3.Error as exc:
        raise RuntimeError(f"No se pudo crear respaldo antes de la purga: {exc}") from exc
    finally:
        destination.close()
        source.close()
    return backup_path


def run_scraper_script(script_name: str) -> dict:
    allowed_scripts = {
        "extractor.py",
        "enriquecedor.py",
        "limpiar_bancos.py",
        "cualificador.py",
    }
    if script_name not in allowed_scripts:
        raise ValueError("Script de prospección no autorizado.")
    script_path = SCRAPER_DIR / script_name
    if not script_path.is_file():
        raise FileNotFoundError(f"No se encontró el motor: {script_path.name}.")
    if LEADS_DB_PATH.resolve() != SCRAPER_DB_PATH.resolve():
        raise RuntimeError(
            "Los motores actuales usan la base local de apex_scraper. Ajusta "
            "APEX_LEADS_DB_PATH a esa ruta antes de ejecutarlos."
        )
    backup_path = ""
    if script_name == "limpiar_bancos.py":
        backup_path = str(backup_leads_database())
    completed = subprocess.run(
        [sys.executable, str(script_path)],
        cwd=SCRAPER_DIR,
        capture_output=True,
        text=True,
    )
    return {
        "script": script_name,
        "returncode": completed.returncode,
        "stdout": completed.stdout[-12000:],
        "stderr": completed.stderr[-12000:],
        "backup_path": backup_path,
    }


def run_scraper_pipeline() -> dict:
    steps = []
    for script_name in (
        "extractor.py",
        "enriquecedor.py",
        "limpiar_bancos.py",
        "cualificador.py",
    ):
        result = run_scraper_script(script_name)
        steps.append(result)
        if result["returncode"] != 0:
            return {"returncode": result["returncode"], "steps": steps}
    return {"returncode": 0, "steps": steps}


def default_state() -> dict:
    return {
        "prototypes": [],
        "income": [],
        "clients": [],
        "contracts": [],
        "purchase_notifications": [],
        "activity_alerts": [],
        "quote_requests": [],
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
        not isinstance(state.get(key), list)
        for key in ("prototypes", "income", "clients")
    ):
        raise ValueError(
            f"El archivo {STATE_FILE.relative_to(ROOT)} no tiene el formato esperado."
        )
    defaults = default_state()
    for key in (
        "contracts",
        "purchase_notifications",
        "activity_alerts",
        "quote_requests",
    ):
        value = state.setdefault(key, [])
        if not isinstance(value, list):
            raise ValueError(f"La sección {key} guardada no tiene el formato esperado.")
        if any(not isinstance(item, dict) for item in value):
            raise ValueError(f"La sección {key} contiene registros inválidos.")
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


def support_email_visual() -> str:
    try:
        sender = str(st.secrets["SMTP_EMAIL"]).strip()
    except (KeyError, FileNotFoundError):
        return SUPPORT_EMAIL
    parsed = parseaddr(sender)[1]
    return parsed if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", parsed) else SUPPORT_EMAIL


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
    message.add_alternative(render_email_html(cuerpo), subtype="html")
    try:
        with smtplib.SMTP_SSL(
            "smtp.gmail.com", 465, timeout=10, context=ssl.create_default_context()
        ) as smtp:
            smtp.login(sender, password)
            refused = smtp.send_message(message)
    except Exception as exc:
        raise RuntimeError(
            f"No se pudo enviar el correo por SMTP ({type(exc).__name__})."
        ) from None
    if refused:
        raise RuntimeError("El servidor SMTP rechazó el destinatario.")


def brand_logo_html() -> str:
    return (
        '<div class="apex-brand">'
        f"{BRAND_LOGO_SVG}"
        '<span class="apex-brand-copy"><strong>Apex AI</strong>'
        '<small>AGENCY</small></span></div>'
    )


def render_email_html(body: str) -> str:
    content_blocks = []
    for line in body.splitlines():
        escaped_line = html.escape(line.strip())
        if not escaped_line:
            continue
        url_match = re.fullmatch(r"https?://[^\s]+", line.strip())
        if url_match and valid_web_url(url_match.group(0)):
            url = html.escape(url_match.group(0), quote=True)
            content_blocks.append(
                f'<p class="cta-wrap"><a class="cta" href="{url}" '
                f'target="_blank" rel="noopener noreferrer">Abrir enlace seguro</a></p>'
            )
        else:
            content_blocks.append(f"<p>{escaped_line}</p>")
    body_content = "\n".join(content_blocks)
    return f"""<!doctype html>
<html lang="es">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <style>
    body{{margin:0;background:#f1f5f9;color:#182230;font-family:Arial,Helvetica,sans-serif}}
    .page{{width:100%;padding:28px 12px;background:#f1f5f9}}
    .card{{max-width:620px;margin:0 auto;overflow:hidden;border:1px solid #e2e8f0;border-radius:18px;background:#fff;box-shadow:0 12px 35px #0b0f1912}}
    .header{{padding:24px 30px;background:#0B0F19;color:#fff}}
    .apex-brand{{display:flex;align-items:center;gap:12px;color:#fff;text-decoration:none}}
    .apex-mark{{width:42px;height:42px;flex:0 0 42px}}
    .apex-brand-copy{{display:flex;flex-direction:column;line-height:1}}
    .apex-brand-copy strong{{font-size:18px;letter-spacing:.02em}}
    .apex-brand-copy small{{margin-top:5px;color:#00F2FE;font-size:9px;font-weight:700;letter-spacing:.28em}}
    .accent{{height:3px;background:linear-gradient(90deg,#00F2FE,#10B981)}}
    .content{{padding:28px 30px 24px;font-size:15px;line-height:1.7;overflow-wrap:anywhere}}
    .content p{{margin:0 0 13px}}
    .cta-wrap{{margin:20px 0!important}}
    .cta{{display:inline-block;border-radius:9px;background:#00bcca;padding:12px 19px;color:#06151a!important;font-weight:700;text-decoration:none}}
    .footer{{padding:18px 30px;background:#f8fafc;color:#64748b;font-size:12px;line-height:1.6}}
    .footer a{{color:#087d89;text-decoration:none;font-weight:700}}
    @media only screen and (max-width:520px){{.page{{padding:12px 6px}}.header,.content,.footer{{padding-left:20px;padding-right:20px}}.content{{font-size:14px}}}}
  </style>
</head>
<body>
  <div class="page"><div class="card">
    <div class="header">{brand_logo_html()}</div>
    <div class="accent"></div>
    <main class="content">{body_content}</main>
    <footer class="footer">
      Apex AI Agency · Soluciones digitales para negocios locales<br>
      Soporte oficial: <a href="mailto:{SUPPORT_EMAIL}">{SUPPORT_EMAIL}</a>
    </footer>
  </div></div>
</body>
</html>"""


def render_delivery_note(contract: dict) -> str:
    delivery_date = contract.get("delivery_date") or "Pendiente de coordinar"
    warranty = contract.get("warranty") or "30 días de soporte correctivo desde la activación."
    amount = PAYMENT_OPTIONS.get(contract.get("payment_method"), 0)
    return (
        f"NOTA DE ENTREGA OFICIAL — {AGENCY_NAME}\n\n"
        f"Cliente: {contract.get('customer_name', '')}\n"
        f"Negocio: {contract.get('business_name', '')}\n"
        f"Fecha de entrega: {delivery_date}\n"
        f"Pasarela: {contract.get('payment_method', '')}\n"
        f"Importe registrado: {money(amount)}\n"
        f"Referencia de pago: {contract.get('payment_reference') or 'No indicada'}\n\n"
        "SERVICIOS INCLUIDOS\n"
        f"{contract.get('delivery_details') or 'Sistema de Captación Web; Cotizador IA; Integración de mensajería'}\n\n"
        f"Garantía: {warranty}\n\n"
        "Soporte oficial: hello.apexrepurposing@gmail.com\n\n"
        f"Documento emitido por el equipo de {AGENCY_NAME}.\n"
        "Este documento confirma la entrega registrada en nuestro sistema."
    )


def enviar_nota_entrega_cliente(contract: dict) -> None:
    if (
        contract.get("status") != "delivered_active"
        or not contract.get("payment_verified")
    ):
        raise ValueError(
            "La nota oficial requiere pago verificado y contrato entregado y activo."
        )
    recipient = contract.get("customer_email", "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", recipient):
        raise ValueError("El contrato no contiene un correo válido del cliente.")
    subject = f"Nota de Entrega Oficial — {AGENCY_NAME} — {contract.get('business_name', '')}"
    enviar_correo_cliente(recipient, subject, render_delivery_note(contract))


def enviar_alerta_ceo(tipo_evento: str, detalles_cliente: dict) -> dict:
    event_labels = {
        "new_purchase": "Nueva compra o activación",
        "delivery_note_request": "Solicitud de Nota de Entrega",
        "custom_quote_request": "Solicitud de presupuesto personalizado",
    }
    if tipo_evento not in event_labels:
        raise ValueError("Tipo de alerta no reconocido.")
    if not isinstance(detalles_cliente, dict):
        raise ValueError("Los detalles de la alerta deben ser un objeto.")

    alert = {
        "id": uuid.uuid4().hex,
        "event_type": tipo_evento,
        "event_label": event_labels[tipo_evento],
        "customer_name": str(detalles_cliente.get("customer_name", "Cliente")).strip(),
        "customer_email": str(detalles_cliente.get("customer_email", "")).strip(),
        "business_name": str(detalles_cliente.get("business_name", "No indicado")).strip(),
        "details": str(detalles_cliente.get("details", "")).strip(),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "email_queued",
    }
    with STATE_LOCK:
        state = load_state()
        state["activity_alerts"].insert(0, alert)
        save_state(state)

    subject = f"[{AGENCY_NAME}] {alert['event_label']} · {alert['business_name']}"
    body = (
        f"Evento: {alert['event_label']}\n"
        f"Negocio: {alert['business_name']}\n"
        f"Cliente: {alert['customer_name']}\n"
        f"Correo: {alert['customer_email'] or 'No indicado'}\n\n"
        f"Detalles:\n{alert['details'] or 'Sin detalles adicionales'}\n\n"
        f"Fecha: {alert['created_at']}\n"
        f"ID de alerta: {alert['id']}"
    )
    try:
        enqueue_background_job(
            f"Alerta para CEO: {alert['event_label']}",
            enviar_correo_cliente,
            SUPPORT_EMAIL,
            subject,
            body,
            kind="ceo_alert",
            record_id=alert["id"],
        )
    except Exception as exc:
        with STATE_LOCK:
            state = load_state()
            saved_alert = next(
                (item for item in state["activity_alerts"] if item["id"] == alert["id"]),
                None,
            )
            if saved_alert:
                saved_alert["status"] = "queue_failed"
                saved_alert["email_error"] = type(exc).__name__
                save_state(state)
        alert["status"] = "queue_failed"
        alert["email_error"] = type(exc).__name__
        return alert
    return alert


def estimate_custom_quote(extras: str) -> dict:
    normalized = "".join(
        character
        for character in unicodedata.normalize("NFKD", extras.casefold())
        if not unicodedata.combining(character)
    )
    line_items = [
        ("Implementación base", PAYMENT_OPTIONS["Binance Pay / USDT"]),
    ]
    if any(term in normalized for term in ("automatizacion", "automation", "integracion")):
        line_items.append(("Automatización adicional", 15000))
    if any(term in normalized for term in ("sede", "sucursal", "ubicacion", "location")):
        line_items.append(("Sede adicional", 12500))
    if any(term in normalized for term in ("avanzado", "premium", "personalizado", "custom")):
        line_items.append(("Diseño avanzado", 20000))
    total_cents = sum(amount for _, amount in line_items)
    return {
        "line_items": [
            {"description": description, "amount_cents": amount}
            for description, amount in line_items
        ],
        "total_cents": total_cents,
        "status": "estimated_pending_approval",
    }


def render_custom_quote(quote_request: dict) -> str:
    items = "\n".join(
        f"• {item['description']}: {money(item['amount_cents'])}"
        for item in quote_request["line_items"]
    )
    return (
        f"PRESUPUESTO ESTIMADO — {AGENCY_NAME}\n\n"
        f"Cliente: {quote_request['customer_name']}\n"
        f"Negocio: {quote_request['business_name']}\n"
        f"Alcance solicitado: {quote_request['scope']}\n\n"
        "DESGLOSE ESTIMADO\n"
        f"{items}\n\n"
        f"Total estimado: {money(quote_request['total_cents'])}\n\n"
        "Esta propuesta es una estimación sujeta a revisión y aprobación expresa "
        "del equipo de Apex AI Agency. No constituye una orden de trabajo ni inicia "
        "una activación. El alcance y los plazos se confirmarán por escrito antes "
        "de iniciar cualquier trabajo.\n\n"
        f"Soporte: {SUPPORT_EMAIL}\n"
        f"Equipo de {AGENCY_NAME}"
    )


def solicitar_presupuesto_personalizado(datos_solicitud: dict) -> dict:
    required_fields = ("customer_name", "customer_email", "business_name", "scope")
    if any(
        not str(datos_solicitud.get(field, "")).strip()
        for field in required_fields
    ):
        raise ValueError("Completa nombre, correo, negocio y alcance solicitado.")
    customer_email = str(datos_solicitud["customer_email"]).strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", customer_email):
        raise ValueError("Indica una dirección de correo válida.")

    quote_request = {
        "id": uuid.uuid4().hex,
        "prototype_id": str(datos_solicitud.get("prototype_id", "")),
        "customer_name": str(datos_solicitud["customer_name"]).strip(),
        "customer_email": customer_email,
        "business_name": str(datos_solicitud["business_name"]).strip(),
        "scope": str(datos_solicitud["scope"]).strip()[:4000],
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    quote_request.update(estimate_custom_quote(quote_request["scope"]))
    with STATE_LOCK:
        state = load_state()
        state["quote_requests"].insert(0, quote_request)
        save_state(state)

    document = render_custom_quote(quote_request)
    subject = f"Presupuesto estimado de {AGENCY_NAME} · {quote_request['business_name']}"
    enqueue_background_job(
        f"Presupuesto para cliente: {quote_request['business_name']}",
        enviar_correo_cliente,
        customer_email,
        subject,
        document,
    )
    enqueue_background_job(
        f"Presupuesto para CEO: {quote_request['business_name']}",
        enviar_correo_cliente,
        SUPPORT_EMAIL,
        f"[Revisión CEO] {subject}",
        document,
    )
    enviar_alerta_ceo(
        "custom_quote_request",
        {
            **quote_request,
            "details": (
                f"Alcance: {quote_request['scope']}\n"
                f"Total estimado: {money(quote_request['total_cents'])}\n"
                f"Solicitud: {quote_request['id']}"
            ),
        },
    )
    return quote_request


def render_quote_request_form(
    business_name: str, prototype_id: str, key_prefix: str
) -> dict | None:
    st.markdown("#### Solicitar Presupuesto a la Medida")
    st.caption(
        "La estimación aplica tarifas base y extras por alcance; queda sujeta a "
        "revisión y aprobación del CEO antes de convertirse en una propuesta definitiva."
    )
    with st.form(f"custom_quote_form_{key_prefix}", clear_on_submit=True):
        customer_name = st.text_input("Nombre", key=f"{key_prefix}_customer_name")
        customer_email = st.text_input("Correo electrónico", key=f"{key_prefix}_customer_email")
        submitted_business_name = st.text_input(
            "Nombre del negocio",
            value=business_name,
            key=f"{key_prefix}_business_name",
        )
        scope = st.text_area(
            "Alcance adicional solicitado",
            placeholder="Ej.: automatización extra, varias sedes o diseño avanzado",
            key=f"{key_prefix}_scope",
        )
        submitted = st.form_submit_button(
            "Enviar Solicitud de Cotización", type="primary"
        )
    if not submitted:
        return None
    try:
        quote_request = solicitar_presupuesto_personalizado(
            {
                "customer_name": customer_name,
                "customer_email": customer_email,
                "business_name": submitted_business_name,
                "scope": scope,
                "prototype_id": prototype_id,
            }
        )
    except ValueError as exc:
        st.error(str(exc))
        return None
    except RuntimeError as exc:
        st.error(f"La solicitud quedó registrada, pero falló un envío: {exc}")
        return None
    except OSError as exc:
        st.error(f"No se pudo guardar la solicitud de presupuesto: {exc}")
        return None
    return quote_request


def approve_custom_quote(quote_id: str) -> None:
    with STATE_LOCK:
        state = load_state()
        quote_request = next(
            (item for item in state["quote_requests"] if item["id"] == quote_id),
            None,
        )
        if quote_request is None:
            raise ValueError("No se encontró la solicitud de presupuesto.")
        if quote_request.get("status") == "approved":
            raise ValueError("Este presupuesto ya fue aprobado.")
        quote_request["status"] = "approved"
        quote_request["approved_at"] = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        save_state(state)
    enqueue_background_job(
        f"Confirmación de presupuesto aprobado: {quote_request['business_name']}",
        enviar_correo_cliente,
        quote_request["customer_email"],
        f"Presupuesto aprobado — {AGENCY_NAME}",
        (
            f"Hola, {quote_request['customer_name']}:\n\n"
            "El CEO de Apex AI Agency aprobó la estimación adjunta como propuesta "
            "económica para revisión. El trabajo solo comenzará después de aceptar "
            "por escrito el alcance y completar los acuerdos de activación.\n\n"
            f"{render_custom_quote(quote_request)}"
        ),
    )


def notificar_compra_ceo(datos_compra: dict) -> dict:
    required_fields = ("business_name", "customer_name", "customer_email", "payment_method")
    if any(not str(datos_compra.get(field, "")).strip() for field in required_fields):
        raise ValueError("Faltan datos obligatorios para registrar la solicitud de compra.")
    method = datos_compra["payment_method"]
    if method not in PAYMENT_OPTIONS:
        raise ValueError("Selecciona una pasarela de pago oficial.")
    amount_cents = PAYMENT_OPTIONS[method]
    payment_verified = bool(datos_compra.get("payment_verified", False))
    contract_id = uuid.uuid4().hex
    notification = {
        "id": uuid.uuid4().hex,
        "contract_id": contract_id,
        "business_name": datos_compra["business_name"].strip(),
        "customer_name": datos_compra["customer_name"].strip(),
        "customer_email": datos_compra["customer_email"].strip(),
        "payment_method": method,
        "amount_cents": amount_cents,
        "payment_reference": datos_compra.get("payment_reference", "").strip(),
        "notes": datos_compra.get("notes", "").strip(),
        "delivery_note_requested": bool(datos_compra.get("delivery_note_requested")),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "status": "verified" if payment_verified else "pending_verification",
    }
    contract = {
        "id": contract_id,
        "prototype_id": datos_compra.get("prototype_id", ""),
        "business_name": notification["business_name"],
        "customer_name": notification["customer_name"],
        "customer_email": notification["customer_email"],
        "payment_method": method,
        "amount_cents": amount_cents,
        "payment_reference": notification["payment_reference"],
        "customer_notes": notification["notes"],
        "status": "paid_activation" if payment_verified else "pending_verification",
        "payment_verified": payment_verified,
        "delivery_note_requested": notification["delivery_note_requested"],
        "created_at": notification["created_at"],
        "delivery_date": "",
        "warranty": "30 días de soporte correctivo desde la activación.",
        "internal_notes": "",
    }
    with STATE_LOCK:
        state = load_state()
        reference = notification["payment_reference"]
        duplicate = reference and any(
            item.get("business_name") == notification["business_name"]
            and item.get("payment_reference") == reference
            for item in state["purchase_notifications"]
        )
        if duplicate:
            raise ValueError(
                "Ya existe un registro con esa referencia de pago para este negocio."
            )
        state["purchase_notifications"].insert(0, notification)
        state["contracts"].append(contract)
        save_state(state)

    if payment_verified:
        try:
            notification["matched_leads"] = mark_matching_leads_as_closed(
                notification["business_name"],
                notification["customer_email"],
            )
        except RuntimeError as exc:
            notification["lead_sync_error"] = str(exc)
            with STATE_LOCK:
                state = load_state()
                saved_notification = next(
                    (
                        item
                        for item in state["purchase_notifications"]
                        if item.get("id") == notification["id"]
                    ),
                    None,
                )
                if saved_notification:
                    saved_notification["lead_sync_error"] = str(exc)
                    save_state(state)

    purchase_details = {
        "customer_name": notification["customer_name"],
        "customer_email": notification["customer_email"],
        "business_name": notification["business_name"],
        "details": (
            f"Pasarela: {method}\n"
            f"Importe: {amount_cents / 100:.0f} USD\n"
            f"Referencia: {notification['payment_reference'] or 'No indicada'}\n"
            f"Estado del pago: "
            f"{'verificado manualmente' if payment_verified else 'pendiente de verificación'}\n"
            f"Leads cerrados en SQLite: {notification.get('matched_leads', 0)}\n"
            f"Notas: {notification['notes'] or 'Ninguna'}"
        ),
    }
    enviar_alerta_ceo("new_purchase", purchase_details)
    if notification["delivery_note_requested"]:
        payment_message = (
            "Tu pago fue verificado. La Nota de Entrega Oficial se enviará cuando el "
            "sistema esté entregado y activo."
            if payment_verified
            else "El pago figura como pendiente de verificación; por eso este mensaje no es "
            "un comprobante de pago ni confirma que el sistema ya fue entregado."
        )
        acknowledgement = (
            f"Hola, {notification['customer_name']}:\n\n"
            f"Recibimos tu solicitud de activación y de Nota de Entrega para "
            f"{notification['business_name']}. {payment_message} Cuando la entrega "
            "sea confirmada, enviaremos la Nota de Entrega Oficial.\n\n"
            f"Soporte: {SUPPORT_EMAIL}\nEquipo de {AGENCY_NAME}"
        )
        enqueue_background_job(
            f"Acuse de solicitud: {notification['business_name']}",
            enviar_correo_cliente,
            notification["customer_email"],
            f"Solicitud recibida — {AGENCY_NAME}",
            acknowledgement,
        )
        enviar_alerta_ceo(
            "delivery_note_request",
            {
                "customer_name": notification["customer_name"],
                "customer_email": notification["customer_email"],
                "business_name": notification["business_name"],
                "details": (
                    f"Referencia del pago: "
                    f"{notification['payment_reference'] or 'No indicada'}\n"
                    "El reporte permanece pendiente de verificación."
                ),
            },
        )
    return notification


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


def valid_iso_date(value: str) -> bool:
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def safe_text(value: str) -> str:
    return html.escape(value.strip(), quote=True)


def slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "prototipo"


def valid_web_url(value: str) -> str:
    candidate = value.strip()
    parsed = urlparse(candidate)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return ""
    return candidate


def payment_value(settings: dict, method: str) -> str:
    links = settings.get("payment_links", {})
    value = links.get(method, "") if isinstance(links, dict) else ""
    return str(value).strip() if value is not None else ""


def paypal_me_url(value: str) -> str:
    candidate = valid_web_url(value)
    if not candidate or urlparse(candidate).scheme != "https":
        return ""
    hostname = (urlparse(candidate).hostname or "").lower()
    return candidate if hostname in ("paypal.me", "www.paypal.me") else ""


def make_prototype_html(
    name: str,
    service: str,
    zone: str,
    payment_links: dict | None = None,
    payment_options_open: bool = False,
    track_payment_open: bool = False,
) -> str:
    business = safe_text(name)
    category = safe_text(service)
    location = safe_text(zone)
    support_email = safe_text(SUPPORT_EMAIL)
    niche_type = "clinic" if "Madrid" in zone else "home_service"
    payment_links = payment_links or {}
    payment_rows = []
    payment_copy_values = []
    for method, cents in PAYMENT_OPTIONS.items():
        configured_value = str(payment_links.get(method, "") or "").strip()
        label = method
        if method == "Binance Pay / USDT":
            label += " · 5% de descuento"
        safe_value = safe_text(configured_value)
        safe_label = safe_text(label)
        price = f"${cents / 100:.0f} USD"
        if not configured_value:
            content = "<small>Datos de pago pendientes de configurar</small>"
        else:
            copy_id = f"payment-copy-{len(payment_copy_values)}"
            payment_copy_values.append(configured_value)
            copy_button = (
                f'<button class="copy-payment" type="button" data-copy-id="{copy_id}" '
                'aria-label="Copiar datos de pago">Copiar</button>'
            )
            if method == "PayPal" and paypal_me_url(configured_value):
                paypal_url = safe_text(paypal_me_url(configured_value))
                content = (
                    f'<a class="payment-destination" href="{paypal_url}" '
                    f'target="_blank" rel="noopener noreferrer">{safe_value}</a>'
                    f"{copy_button}"
                )
            else:
                content = (
                    f'<span class="payment-destination">{safe_value}</span>{copy_button}'
                )
            content = (
                f'<span id="{copy_id}" class="copy-source">{safe_value}</span>{content}'
            )
        payment_rows.append(
            f'<div class="payment-link"><strong>{safe_label} · {price}</strong>'
            f'<div class="payment-detail">{content}</div></div>'
        )
    payment_options = "\n".join(payment_rows)
    payment_copy_json = json.dumps(payment_copy_values, ensure_ascii=True).replace(
        "<", "\\u003c"
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
      .preview-toolbar{{position:fixed;inset:0 0 auto;z-index:50;display:flex;align-items:center;justify-content:space-between;gap:1rem;background:#0B0F19;color:white;padding:.8rem clamp(1rem,4vw,3rem);box-shadow:0 8px 24px #0003;border-bottom:1px solid #00f2fe55}}
      .preview-toolbar p{{margin:0;font-size:.9rem;font-weight:600}}
      .apex-brand{{display:inline-flex;align-items:center;gap:.7rem;color:#fff;text-decoration:none;white-space:nowrap}}
      .apex-mark{{width:40px;height:40px;flex:0 0 40px}}
      .apex-brand-copy{{display:flex;flex-direction:column;line-height:1}}
      .apex-brand-copy strong{{font-family:"DM Sans",sans-serif;font-size:1rem;letter-spacing:.01em}}
      .apex-brand-copy small{{margin-top:.28rem;color:#00F2FE;font-size:.55rem;font-weight:700;letter-spacing:.28em}}
      .payment-menu{{position:relative}}
      .payment-menu summary{{cursor:pointer;list-style:none;border-radius:999px;background:#10B981;color:#052117;padding:.7rem 1rem;font-size:.85rem;font-weight:700;white-space:nowrap;box-shadow:0 6px 18px #10b98135}}
      .payment-menu summary::-webkit-details-marker{{display:none}}
      .payment-list{{position:absolute;right:0;top:calc(100% + .65rem);display:grid;gap:.45rem;width:min(21rem,90vw);border:1px solid #e7e5e4;border-radius:1rem;background:white;padding:.85rem;box-shadow:0 16px 45px #0003;color:#1c1917}}
      .payment-link{{display:block;border-radius:.7rem;background:#f5f5f4;padding:.75rem;color:#14532d;font-size:.88rem;font-weight:700;text-decoration:none}}
      .payment-link small{{display:block;margin-top:.2rem;color:#78716c;font-weight:400}}
      .payment-detail{{display:flex;align-items:center;justify-content:space-between;gap:.75rem;margin-top:.5rem;color:#334155;font-size:.82rem;font-weight:500;overflow-wrap:anywhere}}
      .payment-destination{{min-width:0;white-space:pre-wrap;overflow-wrap:anywhere}}
      a.payment-destination{{color:#075985;font-weight:700;text-decoration:underline}}
      .copy-source{{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0);white-space:pre-wrap}}
      .copy-payment{{flex:0 0 auto;border:0;border-radius:999px;background:#0B0F19;color:#00F2FE;padding:.4rem .7rem;font-size:.72rem;font-weight:700;cursor:pointer}}
      .copy-payment:hover{{background:#162436}}
      .copy-notice{{position:fixed;right:1rem;bottom:1rem;z-index:60;border-radius:.7rem;background:#0B0F19;color:#fff;padding:.7rem 1rem;font-size:.82rem;box-shadow:0 8px 24px #0003}}
      .simulator-card{{border:1px solid #e7e5e4;border-radius:1.25rem;background:#fff;padding:1.25rem}}
      .simulator-card input,.simulator-card select{{width:100%;border:1px solid #d6d3d1;border-radius:.65rem;padding:.7rem;margin:.35rem 0 .8rem}}
      .simulator-result{{border-radius:.75rem;background:#ecfdf5;padding:.9rem;color:#14532d;font-weight:700}}
      .faq-item{{border-bottom:1px solid #e7e5e4;padding:1rem 0}}
      @media(max-width:520px){{.preview-toolbar{{align-items:flex-start;flex-direction:column}}body{{padding-top:130px}}.payment-list{{left:0;right:auto}}}}
    </style>
  </head>
  <body class="bg-stone-50 text-stone-900 antialiased">
    <aside class="preview-toolbar">
      <div class="flex items-center gap-3">
        {brand_logo_html()}
        <p>Vista previa exclusiva para {business} · Expira en 48h</p>
      </div>
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
      <section class="mx-auto max-w-6xl px-6 py-16">
        <p class="text-sm font-bold uppercase tracking-[.18em] text-emerald-800">Pruébalo en vivo</p>
        <h2 class="mt-3 text-4xl">Herramientas interactivas</h2>
        <p class="mt-3 max-w-2xl text-stone-600">Simulaciones orientativas para mostrar cómo podría ayudar un sitio activo. Los valores no son cotizaciones finales ni una reserva real.</p>
        <div class="mt-8 grid gap-6 md:grid-cols-2">
          <article class="simulator-card">
            <h3 class="text-2xl">Calculadora de presupuestos instantánea</h3>
            <label for="estimate-service" class="text-sm font-semibold">Servicio</label>
            <select id="estimate-service"></select>
            <label for="estimate-units" class="text-sm font-semibold">Cantidad / sesiones</label>
            <input id="estimate-units" type="number" min="1" max="20" value="1">
            <div id="estimate-result" class="simulator-result" aria-live="polite">Estimación orientativa</div>
          </article>
          <article class="simulator-card">
            <h3 class="text-2xl">Simulador de asistente IA 24/7</h3>
            <label for="assistant-question" class="text-sm font-semibold">Prueba una pregunta habitual</label>
            <select id="assistant-question">
              <option value="price">¿Cuánto cuesta y qué incluye?</option>
              <option value="availability">¿Tienen disponibilidad esta semana?</option>
              <option value="appointment">Quiero reservar una cita</option>
            </select>
            <div id="assistant-result" class="simulator-result" aria-live="polite"></div>
            <p class="mt-3 text-sm text-stone-500">Ejemplo de interacción; no envía mensajes ni agenda citas.</p>
          </article>
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
    <footer class="border-t border-stone-200 px-6 py-6 text-center text-xs text-stone-500">© 2026 {business} · Concepto de demostración sin conexión de reservas · Vista creada por {AGENCY_NAME} · <a href="mailto:{support_email}">{support_email}</a></footer>
    <script>
      const paymentCopyValues = {payment_copy_json};
      document.querySelectorAll(".copy-payment").forEach(button => {{
        button.addEventListener("click", async () => {{
          const index = Number(button.dataset.copyId.replace("payment-copy-", ""));
          const value = paymentCopyValues[index];
          if (!value) return;
          try {{
            await navigator.clipboard.writeText(value);
          }} catch (error) {{
            const helper = document.createElement("textarea");
            helper.value = value;
            helper.style.position = "fixed";
            helper.style.opacity = "0";
            document.body.appendChild(helper);
            helper.select();
            const copied = document.execCommand("copy");
            helper.remove();
            if (!copied) {{
              window.prompt("Copia estos datos de pago:", value);
              return;
            }}
          }}
          const notice = document.createElement("div");
          notice.className = "copy-notice";
          notice.textContent = "Datos de pago copiados";
          document.body.appendChild(notice);
          window.setTimeout(() => notice.remove(), 1800);
        }});
      }});
      const nicheType = "{niche_type}";
      const services = nicheType === "clinic"
        ? [{{name:"Consulta inicial",low:50,high:90}},{{name:"Tratamiento estético",low:90,high:250}},{{name:"Consulta dental",low:40,high:100}}]
        : [{{name:"Diagnóstico en domicilio",low:75,high:150}},{{name:"Reparación estándar",low:150,high:450}},{{name:"Instalación completa",low:450,high:1200}}];
      const serviceSelect = document.getElementById("estimate-service");
      services.forEach((service,index) => {{
        const option = document.createElement("option");
        option.value = index;
        option.textContent = service.name;
        serviceSelect.appendChild(option);
      }});
      function updateEstimate() {{
        const service = services[Number(serviceSelect.value)] || services[0];
        const count = Math.min(20,Math.max(1,Number(document.getElementById("estimate-units").value)||1));
        const low = service.low*count;
        const high = service.high*count;
        const currency = nicheType === "clinic" ? "EUR" : "USD";
        document.getElementById("estimate-result").textContent =
          "Rango estimado: " + new Intl.NumberFormat("es-ES",{{style:"currency",currency}}).format(low) +
          " – " + new Intl.NumberFormat("es-ES",{{style:"currency",currency}}).format(high) +
          " · Solicita una evaluación para confirmar.";
      }}
      const assistantAnswers = {{
        price:"Te orientamos según el servicio y tus necesidades. ¿Qué servicio te interesa? Puedo ayudarte a solicitar una cita.",
        availability:"Podemos revisar horarios contigo. ¿Qué día te viene mejor? Esta demostración no confirma disponibilidad real.",
        appointment:"¡Claro! Para solicitar una cita, indícanos el servicio y el horario que prefieres. Un especialista confirmará la reserva."
      }};
      function updateAssistant() {{
        document.getElementById("assistant-result").textContent =
          assistantAnswers[document.getElementById("assistant-question").value];
      }}
      serviceSelect.addEventListener("change",updateEstimate);
      document.getElementById("estimate-units").addEventListener("input",updateEstimate);
      document.getElementById("assistant-question").addEventListener("change",updateAssistant);
      updateEstimate();
      updateAssistant();
    </script>
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


def generate_proposal_copy_with_ai(
    lead: dict, api_key: str, model: str = "gpt-4o-mini"
) -> tuple[str, str]:
    prompt = (
        "Redacta en español un primer correo B2B breve, humano y personalizado para "
        "un negocio local. Devuelve únicamente un objeto JSON con las claves "
        "'subject' y 'body'. Menciona una oportunidad para mejorar la captación sin "
        "afirmar que auditaste el sitio ni inventar problemas o resultados. Invita a "
        "probar un prototipo interactivo e incluye exactamente el marcador "
        "{{DEMO_LINK}} en una línea propia. No incluyas precios, pagos ni promesas "
        "de resultados. Termina con una pregunta sencilla sobre conectar el flujo "
        "a WhatsApp. Firma como Apex AI Agency y añade hello.apexrepurposing@gmail.com."
    )
    business_context = {
        "business_name": lead["nombre"],
        "market": lead.get("mercado") or "",
        "location": lead.get("direccion") or "",
        "website": lead.get("web") or "",
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "response_format": {"type": "json_object"},
                "messages": [
                    {"role": "system", "content": prompt},
                    {
                        "role": "user",
                        "content": json.dumps(business_context, ensure_ascii=False),
                    },
                ],
                "temperature": 0.5,
            },
            timeout=10,
        )
        response.raise_for_status()
        payload = response.json()
        content = payload["choices"][0]["message"]["content"]
        generated = json.loads(content)
    except requests.RequestException as exc:
        raise RuntimeError(
            f"No se pudo contactar al proveedor de IA ({type(exc).__name__})."
        ) from exc
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "El proveedor de IA devolvió una respuesta inválida; no se creó el borrador."
        ) from exc

    if not isinstance(generated, dict):
        raise RuntimeError(
            "El proveedor de IA devolvió una respuesta inválida; no se creó el borrador."
        )
    subject_value = generated.get("subject")
    body_value = generated.get("body")
    if not isinstance(subject_value, str) or not isinstance(body_value, str):
        raise RuntimeError(
            "La respuesta de IA no incluyó asunto y cuerpo de texto válidos."
        )
    subject = subject_value.strip()
    body = body_value.strip()
    if (
        not subject
        or len(subject) > 180
        or "\n" in subject
        or "\r" in subject
        or not body
        or len(body) > 5000
        or "{{DEMO_LINK}}" not in body
    ):
        raise RuntimeError(
            "La respuesta de IA no incluyó asunto, cuerpo y marcador de demo válidos."
        )
    if SUPPORT_EMAIL not in body:
        body = f"{body.rstrip()}\n\nEquipo de {AGENCY_NAME}\n{SUPPORT_EMAIL}"
    return subject, body


def generate_and_store_lead_proposal(
    lead: dict, api_key: str, model: str, payment_links: dict
) -> str | None:
    subject, message = generate_proposal_copy_with_ai(lead, api_key, model)
    proposal_id = (
        f"{slugify(lead['nombre'])}-lead-{lead['id']}-{uuid.uuid4().hex[:8]}"
    )
    folder = PENDING_DIR / proposal_id
    folder.mkdir(parents=False, exist_ok=False)
    zone = str(lead.get("direccion") or lead.get("mercado") or "Zona por confirmar")
    service = str(lead.get("mercado") or "Negocio local")
    record = {
        "id": proposal_id,
        "lead_id": int(lead["id"]),
        "business_name": str(lead["nombre"]),
        "service": service,
        "niche": "Prospección SQLite",
        "zone": zone,
        "objective": "Borrador generado con IA desde la base local de prospectos.",
        "status": "pending",
        "note": "",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "folder": str(folder.relative_to(ROOT)),
        "contact_email": clean_lead_email(lead.get("email", "")),
        "proposal_subject": subject,
        "proposal_message": message,
    }
    try:
        (folder / "index.html").write_text(
            make_prototype_html(
                record["business_name"],
                record["service"],
                record["zone"],
                payment_links,
            ),
            encoding="utf-8",
        )
        (folder / "proposal.json").write_text(
            json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with STATE_LOCK:
            state = load_state()
            state["prototypes"].append(record)
            save_state(state)
    except (OSError, RuntimeError, ValueError) as exc:
        raise RuntimeError(
            f"Se generó el contenido, pero no se pudo guardar íntegramente "
            f"la propuesta de {record['business_name']}: {exc}"
        ) from exc
    try:
        update_lead_proposal_status(
            int(lead["id"]), proposal_id, "pending_approval", record["business_name"]
        )
    except RuntimeError as exc:
        return (
            f"El borrador está en la bandeja de aprobación, pero no se pudo actualizar "
            f"su seguimiento SQLite: {exc}"
        )
    return None


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
) -> str | None:
    current_state = load_state()
    record = next(
        (
            item
            for item in current_state["prototypes"]
            if item.get("id") == record_id
        ),
        None,
    )
    if record is None or record.get("status") != "sending":
        raise ValueError("La propuesta no está en estado de envío aprobado.")
    enviar_correo_cliente(recipient, subject, body)
    funnel_warning = ""
    if record and record.get("lead_id") is not None:
        try:
            update_lead_proposal_status(
                int(record["lead_id"]),
                record_id,
                "CONTACTADO_FASE_1",
                record["business_name"],
            )
        except (RuntimeError, ValueError) as exc:
            funnel_warning = (
                f"El correo Fase 1 se envió, pero falló la sincronización SQLite: {exc}"
            )
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

    return funnel_warning or None


def dispatch_phase_two_proposal(
    record_id: str, recipient: str, subject: str, body: str
) -> str | None:
    enviar_correo_cliente(recipient, subject, body)
    warning = ""
    with STATE_LOCK:
        state = load_state()
        record = next(
            (item for item in state["prototypes"] if item.get("id") == record_id),
            None,
        )
        if record is None:
            return (
                "El correo Fase 2 se envió, pero no se encontró el registro local "
                "para guardar el seguimiento."
            )
        record["phase_two_sent_at"] = datetime.now().astimezone().isoformat(
            timespec="seconds"
        )
        record["phase_two_recipient"] = recipient
        try:
            save_state(state)
        except OSError as exc:
            warning = (
                f"El correo Fase 2 se envió, pero no se pudo guardar su historial "
                f"local: {exc}"
            )
        lead_id = record.get("lead_id")
        business_name = record["business_name"]
    if lead_id is not None:
        try:
            update_lead_proposal_status(
                int(lead_id),
                record_id,
                "PROPUESTA_PAGO_ENVIADA",
                business_name,
            )
        except (RuntimeError, ValueError) as exc:
            sync_warning = (
                f"El correo Fase 2 se envió, pero falló la sincronización SQLite: {exc}"
            )
            warning = f"{warning} {sync_warning}".strip()
    return warning or None


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
            query=urlencode({"demo": record["id"]}),
            fragment="",
        )
    )


def local_demo_url(record: dict) -> str:
    return f"http://127.0.0.1:8501/?{urlencode({'demo': record['id']})}"


def proposal_phase_one(record: dict, demo_link: str) -> str:
    if record.get("proposal_message"):
        return str(record["proposal_message"]).replace("{{DEMO_LINK}}", demo_link)
    return (
        f"Hola, equipo de {record['business_name']}:\n\n"
        f"Analizamos oportunidades de captación para negocios de {record['zone']} "
        "y notamos que podrían estar perdiendo clientes al no contar con un "
        "cotizador instantáneo disponible 24/7. Preparamos un prototipo "
        "interactivo y personalizado para que lo prueben en vivo:\n"
        f"{demo_link}\n\n"
        "¿Te gustaría que adaptemos el flujo de captación a tus canales de contacto esta semana?"
        f"\n\nUn saludo,\nEquipo de {AGENCY_NAME}\n{SUPPORT_EMAIL}"
    )


def proposal_phase_two(record: dict, settings: dict) -> str:
    payment_links = settings.get("payment_links", {})
    options = []
    for method, amount in (
        ("Binance Pay / USDT", "$380 USD · 5% de descuento"),
        ("Payoneer (Tarjeta/ACH)", "$400 USD"),
        ("PayPal", "$425 USD"),
    ):
        configured_value = str(payment_links.get(method, "") or "").strip()
        data_line = (
            f"\n  Datos configurados: {configured_value}"
            if configured_value
            else "\n  Datos oficiales pendientes de configurar."
        )
        options.append(f"• {method}: {amount}{data_line}")
    return (
        f"Hola, equipo de {record['business_name']}:\n\n"
        "¡Qué bueno que les interese! Podemos dejar su prototipo personalizado "
        "listo en su dominio en 24 horas, con el flujo de contacto adaptado a su negocio.\n\n"
        "Para comenzar, pueden elegir la opción que mejor les convenga:\n"
        + "\n".join(options)
        + f"\n\nUn saludo,\nEquipo de {AGENCY_NAME}\n{SUPPORT_EMAIL}"
    )


def email_action_link(message: str, subject: str, recipient_email: str) -> str:
    email = recipient_email.strip()
    return f"mailto:{email}?{urlencode({'subject': subject, 'body': message})}"


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
        st.success(f"{len(created)} prototipo(s) listo(s) para revisión.")
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


def render_prospecting_tab(state: dict) -> None:
    st.subheader("Prospección comercial desde SQLite")
    st.caption(
        f"Fuente: `{leads_db_display_path()}`. La consulta es de solo lectura "
        "para leads; los borradores y su seguimiento se guardan aparte."
    )
    try:
        stats, leads = load_prospect_data()
    except (FileNotFoundError, RuntimeError) as exc:
        st.error(f"No se pudieron cargar los prospectos: {exc}")
        return

    render_scraper_controls()

    total_col, high_col, medium_col, contacted_col, closing_col, closed_col = st.columns(6)
    total_col.metric("Total limpios", f"{stats['clean']:,}")
    high_col.metric("Alta prioridad", f"{stats['high_priority']:,}")
    medium_col.metric("Media prioridad", f"{stats['medium_priority']:,}")
    contacted_col.metric("Contactados · Fase 1", f"{stats['contacted_phase_one']:,}")
    closing_col.metric("En cierre · Fase 2", f"{stats['payment_proposal_sent']:,}")
    closed_col.metric("Clientes cerrados", f"{stats['closed_clients']:,}")
    st.caption(
        "Total limpios incluye leads contactables de prioridad alta/media, excluye "
        "bancos y financieras, y conserva las etapas del embudo tras recalificar."
    )

    priority_filter = st.selectbox(
        "Prioridad",
        ("Todas", "Alta", "Media"),
        key="prospecting_priority_filter",
    )
    search_term = st.text_input(
        "Buscar por negocio o mercado",
        key="prospecting_search",
        placeholder="Nombre del negocio o categoría",
    )
    visible_leads = [
        lead
        for lead in leads
        if (
            priority_filter == "Todas"
            or str(lead["prioridad"]).upper().startswith(priority_filter.upper())
        )
        and (
            not search_term.strip()
            or search_term.casefold()
            in f"{lead['nombre']} {lead['mercado']}".casefold()
        )
    ]
    display_rows = [
        {
            "ID": lead["id"],
            "Negocio": lead["nombre"],
            "Prioridad": lead["prioridad"],
            "Etapa": lead["funnel_status"],
            "Mercado": lead["mercado"],
            "Zona": lead["direccion"],
            "Web": lead["web"],
            "Email": lead["email"] or "—",
            "Teléfono": lead["telefono"] or "—",
        }
        for lead in visible_leads
    ]
    st.dataframe(
        display_rows,
        use_container_width=True,
        hide_index=True,
        height=min(460, max(180, len(display_rows) * 36 + 38)),
    )
    if not visible_leads:
        st.info("No hay leads que coincidan con los filtros seleccionados.")
        return

    leads_by_id = {int(lead["id"]): lead for lead in visible_leads}
    selected_id = st.selectbox(
        "Selecciona un prospecto para preparar su propuesta",
        list(leads_by_id),
        format_func=lambda lead_id: (
            f"{leads_by_id[lead_id]['nombre']} · "
            f"{leads_by_id[lead_id]['mercado']} · ID {lead_id}"
        ),
        key="selected_prospect_id",
    )
    selected_lead = leads_by_id[selected_id]
    existing_proposals = [
        record
        for record in state["prototypes"]
        if record.get("lead_id") == selected_id
    ]
    with BACKGROUND_JOBS_LOCK:
        proposal_is_running = any(
            job.get("kind") == "lead_proposal"
            and job.get("record_id") == str(selected_id)
            and job.get("status") in ("queued", "running")
            for job in BACKGROUND_JOBS.values()
        )
    if proposal_is_running:
        st.info("La propuesta de este prospecto se está generando en segundo plano.")
    elif existing_proposals:
        latest_proposal = max(
            existing_proposals,
            key=lambda record: record.get("created_at", ""),
        )
        st.info(
            "Este prospecto ya tiene un borrador: "
            f"{STATUS_LABELS.get(latest_proposal.get('status'), latest_proposal.get('status'))}. "
            "Revísalo en la Bandeja de Aprobación antes de generar otro."
        )
    st.write(
        f"**Email:** {selected_lead['email'] or 'No disponible'}  \n"
        f"**Teléfono:** {selected_lead['telefono'] or 'No disponible'}  \n"
        f"**Web:** {selected_lead['web'] or 'No disponible'}"
    )
    try:
        api_key = str(st.secrets["OPENAI_API_KEY"]).strip()
    except (KeyError, FileNotFoundError):
        api_key = ""
    try:
        model = str(st.secrets.get("OPENAI_MODEL", "gpt-4o-mini")).strip()
    except (FileNotFoundError, AttributeError):
        model = "gpt-4o-mini"
    if not api_key:
        st.warning(
            "Para habilitar la generación con IA, configura `OPENAI_API_KEY` en "
            "Streamlit Secrets. No se enviarán propuestas automáticamente."
        )
    st.caption(
        "El proveedor de IA recibe nombre del negocio, mercado, zona y web pública; "
        "no recibe el correo ni el teléfono. El resultado se guarda en Pendientes y "
        "solo se envía después de tu aprobación explícita."
    )
    if st.button(
        "✨ Generar propuesta con IA y enviar a revisión",
        type="primary",
        use_container_width=True,
        disabled=not api_key or bool(existing_proposals) or proposal_is_running,
        key="generate_prospect_proposal",
    ):
        enqueue_background_job(
            f"Propuesta IA para {selected_lead['nombre']}",
            generate_and_store_lead_proposal,
            dict(selected_lead),
            api_key,
            model or "gpt-4o-mini",
            dict(state["settings"].get("payment_links", {})),
            kind="lead_proposal",
            record_id=str(selected_lead["id"]),
        )
        st.session_state["flash_message"] = (
            f"Se inició la preparación del borrador para {selected_lead['nombre']}; "
            "no se enviará sin aprobación."
        )
        st.rerun()


def queue_scraper_job(label: str, script_name: str) -> None:
    if scraper_job_is_active():
        st.warning("Ya hay un motor de prospección en ejecución.")
        return
    enqueue_background_job(
        label,
        run_scraper_script,
        script_name,
        kind="scraper",
        record_id=script_name,
    )
    st.session_state["flash_message"] = f"Motor en segundo plano: {label}."
    st.rerun()


def scraper_job_is_active() -> bool:
    with BACKGROUND_JOBS_LOCK:
        return any(
            job.get("kind") == "scraper"
            and job.get("status") in ("queued", "running")
            for job in BACKGROUND_JOBS.values()
        )


def render_scraper_controls() -> None:
    with st.expander("⚙️ Ejecutar Motores de Extracción y Limpieza"):
        scraper_busy = scraper_job_is_active()
        st.caption(
            "Los motores operan sobre `apex_scraper/prospectos_apex_scraping.db`. "
            "La purga elimina permanentemente filas bancarias; antes de ejecutarla "
            "se crea un respaldo SQLite en `.ceo_dashboard/database_backups/`."
        )
        if scraper_busy:
            st.info("Hay un motor activo; espera a que termine antes de iniciar otro.")
        extract_col, enrich_col = st.columns(2)
        with extract_col:
            if st.button(
                "🔎 Buscar nuevos prospectos",
                key="run_scraper_extractor",
                disabled=scraper_busy,
                use_container_width=True,
            ):
                queue_scraper_job("Buscar nuevos prospectos", "extractor.py")
        with enrich_col:
            if st.button(
                "✉️ Enriquecer emails y teléfonos",
                key="run_scraper_enricher",
                disabled=scraper_busy,
                use_container_width=True,
            ):
                queue_scraper_job(
                    "Enriquecer emails y teléfonos", "enriquecedor.py"
                )

        purge_confirmed = st.checkbox(
            "Confirmo la eliminación permanente de bancos y financieras "
            "(se guardará un respaldo antes de continuar).",
            key="confirm_scraper_purge",
        )
        purge_col, score_col = st.columns(2)
        with purge_col:
            if st.button(
                "🧹 Purgar bancos y financieras",
                key="run_scraper_purge",
                disabled=not purge_confirmed or scraper_busy,
                use_container_width=True,
            ):
                queue_scraper_job(
                    "Purgar bancos y financieras", "limpiar_bancos.py"
                )
        with score_col:
            if st.button(
                "📊 Recalificar Lead Scoring y exportar CSV",
                key="run_scraper_scoring",
                disabled=scraper_busy,
                use_container_width=True,
            ):
                queue_scraper_job(
                    "Recalificar Lead Scoring y exportar CSV", "cualificador.py"
                )

        pipeline_confirmed = st.checkbox(
            "Confirmo el pipeline completo, incluida la purga irreversible "
            "de bancos y financieras tras crear un respaldo.",
            key="confirm_scraper_pipeline",
        )
        if st.button(
            "▶️ Ejecutar Pipeline Completo en Cadena",
            key="run_scraper_pipeline",
            disabled=not pipeline_confirmed or scraper_busy,
            type="primary",
            use_container_width=True,
        ):
            enqueue_background_job(
                "Pipeline completo de scraping y calificación",
                run_scraper_pipeline,
                kind="scraper",
                record_id="pipeline",
            )
            st.session_state["flash_message"] = (
                "Pipeline completo en segundo plano; la purga solo se ejecutará "
                "después de crear el respaldo."
            )
            st.rerun()


def enqueue_phase_two_email(
    record: dict, recipient: str, settings: dict
) -> None:
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", recipient.strip()):
        st.error("Introduce un correo válido para enviar la Fase 2.")
        return
    payment_links = settings.get("payment_links", {})
    if not all(str(value).strip() for value in payment_links.values()):
        st.error("Configura los datos oficiales de las tres pasarelas antes de enviar.")
        return
    enqueue_background_job(
        f"Envío Fase 2 a {record['business_name']}",
        dispatch_phase_two_proposal,
        record["id"],
        recipient.strip(),
        f"Siguiente paso para activar {record['business_name']}",
        proposal_phase_two(record, settings),
        kind="phase_two",
        record_id=record["id"],
    )
    st.success(f"Mensaje Fase 2 encolado para {record['business_name']}.")


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
                source_label = (
                    f"Prospecto real · ID SQLite {record['lead_id']}"
                    if record.get("lead_id") is not None
                    else "Prospecto simulado"
                )
                st.caption(
                    f"{record['service']} · {record['zone']} · {source_label}"
                )
            with status_col:
                st.markdown(
                    f"**{STATUS_LABELS.get(record['status'], record['status'])}**"
                )
            if record.get("note"):
                st.info(f"Nota de ajustes: {record['note']}")
            show_prototype_preview(record, key, state["settings"])
            email_col, save_col = st.columns([3, 1])
            with email_col:
                recipient_email = st.text_input(
                    "Correo del prospecto",
                    value=record.get("contact_email", ""),
                    key=f"email_{key}",
                )
            with save_col:
                st.markdown("<div style='height:28px'></div>", unsafe_allow_html=True)
                if st.button("Guardar contacto", key=f"save_contact_{key}"):
                    record["contact_email"] = recipient_email.strip()
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
                email_url = email_action_link(
                    phase_one_message,
                    record.get(
                        "proposal_subject", "Tu prototipo personalizado"
                    ),
                    record.get("contact_email", ""),
                )
                st.link_button(
                    "Abrir correo — Mensaje 1 (Prototipo)",
                    email_url,
                    disabled=not share_url or not recipient_email.strip(),
                    use_container_width=True,
                )
            with phase_two_tab:
                phase_two_message = proposal_phase_two(record, state["settings"])
                payment_ready = all(
                    str(url).strip()
                    for url in state["settings"]["payment_links"].values()
                )
                st.code(phase_two_message, language="text")
                email_url = email_action_link(
                    phase_two_message,
                    "Siguiente paso para activar su web",
                    record.get("contact_email", ""),
                )
                st.link_button(
                    "Abrir correo — Mensaje 2 (Links de Pago)",
                    email_url,
                    disabled=(
                        not payment_ready
                        or not record.get("contact_email", "").strip()
                    ),
                    use_container_width=True,
                )
                if st.button(
                    "💳 Enviar Links de Pago por SMTP Automático",
                    key=f"send_phase_two_{key}",
                    type="primary",
                    disabled=(
                        not payment_ready
                        or not re.fullmatch(
                            r"[^@\s]+@[^@\s]+\.[^@\s]+",
                            recipient_email.strip(),
                        )
                    ),
                    use_container_width=True,
                ):
                    record["contact_email"] = recipient_email.strip()
                    save_state(state)
                    enqueue_phase_two_email(
                        record, recipient_email, state["settings"]
                    )
                if not payment_ready:
                    st.caption(
                        "Configura los datos oficiales de cada pasarela en la barra lateral "
                        "para activar los botones de envío de esta fase."
                    )
                else:
                    st.caption(
                        "Puedes abrir un borrador manual o despachar el mensaje por SMTP "
                        "con el botón automático."
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
                        record["status"] = "sending"
                        record.pop("last_send_error", None)
                        save_state(state)
                        enqueue_background_job(
                            f"Envío de propuesta a {record['business_name']}",
                            dispatch_approved_proposal,
                            record["id"],
                            record["contact_email"],
                            record.get(
                                "proposal_subject",
                                f"Una idea para {record['business_name']}",
                            ),
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
                with st.container(border=True):
                    st.markdown(
                        f"**{record['business_name']}** · {record['zone']} · "
                        f"aprobado {record.get('approved_at', '')[:10]}"
                    )
                    approved_email = st.text_input(
                        "Correo del cliente",
                        value=record.get("contact_email", ""),
                        key=f"approved_contact_{record['id']}",
                    )
                    phase_two_message = proposal_phase_two(record, state["settings"])
                    st.code(phase_two_message, language="text")
                    approved_email_url = email_action_link(
                        phase_two_message,
                        f"Siguiente paso para activar {record['business_name']}",
                        approved_email,
                    )
                    st.link_button(
                        "Abrir correo — Mensaje 2 (Links de Pago)",
                        approved_email_url,
                        disabled=not approved_email.strip(),
                        use_container_width=True,
                    )
                    if st.button(
                        "💳 Enviar Links de Pago por SMTP Automático",
                        key=f"send_approved_phase_two_{record['id']}",
                        type="primary",
                        disabled=not approved_email.strip()
                        or not all(
                            str(value).strip()
                            for value in state["settings"]["payment_links"].values()
                        ),
                        use_container_width=True,
                    ):
                        record["contact_email"] = approved_email.strip()
                        save_state(state)
                        enqueue_phase_two_email(
                            record, approved_email, state["settings"]
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
    st.markdown("### Proyecciones de ingresos")
    projection_columns = st.columns(4)
    projected_clients = {
        method: projection_columns[index].slider(
            f"Ventas/mes · {method} (${amount / 100:.0f})",
            min_value=0,
            max_value=50,
            value=0,
            key=f"projection_{slugify(method)}",
        )
        for index, (method, amount) in enumerate(PAYMENT_OPTIONS.items())
    }
    monthly_maintenance = projection_columns[3].slider(
        "Clientes activos de mantenimiento",
        min_value=0,
        max_value=500,
        value=min(
            500,
            len([client for client in state["clients"] if client.get("active", True)]),
        ),
        key="projection_maintenance",
    )
    projection_by_method = {
        method: projected_clients[method] * amount
        for method, amount in PAYMENT_OPTIONS.items()
    }
    projected_monthly_cents = (
        sum(projection_by_method.values()) + monthly_maintenance * 10000
    )
    projection_metrics = st.columns(3)
    projection_metrics[0].metric(
        "Ingreso semanal estimado", money(round(projected_monthly_cents * 12 / 52))
    )
    projection_metrics[1].metric(
        "Ingreso mensual proyectado", money(projected_monthly_cents)
    )
    projection_metrics[2].metric(
        "Facturación anualizada", money(projected_monthly_cents * 12)
    )
    breakdown = st.columns(4)
    for column, method in zip(breakdown[:3], PAYMENT_OPTIONS):
        column.metric(
            f"Proyección · {method}", money(projection_by_method[method])
        )
    breakdown[3].metric(
        "Proyección · mantenimiento",
        money(monthly_maintenance * 10000),
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
    st.markdown("### Contratos y Notas de Entrega")
    st.caption(
        "Los reportes públicos quedan pendientes de verificación. Registra el pago "
        "solo después de comprobarlo; la Nota de Entrega Oficial se habilita al marcar "
        "el contrato como entregado y activo."
    )
    with st.expander("Registrar una venta verificada manualmente"):
        with st.form("verified_sale_form", clear_on_submit=True):
            sale_business = st.text_input("Nombre del negocio")
            sale_customer = st.text_input("Nombre del cliente")
            sale_email = st.text_input("Correo del cliente")
            sale_method = st.selectbox(
                "Pasarela y precio",
                list(PAYMENT_OPTIONS),
                format_func=lambda method: (
                    f"{method} — ${PAYMENT_OPTIONS[method] / 100:.0f} USD"
                ),
                key="sale_payment_method",
            )
            sale_reference = st.text_input("Referencia de pago")
            verified_sale = st.checkbox(
                "Confirmo que verifiqué el pago fuera de esta aplicación.",
                value=False,
            )
            sale_submitted = st.form_submit_button(
                "Registrar venta verificada", type="primary"
            )
            if sale_submitted:
                if not verified_sale:
                    st.error("Confirma la verificación real del pago antes de registrar la venta.")
                elif not sale_business.strip() or not sale_customer.strip():
                    st.error("El negocio y el nombre del cliente son obligatorios.")
                elif not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", sale_email.strip()):
                    st.error("Indica un correo válido del cliente.")
                else:
                    try:
                        sale_notification = notificar_compra_ceo(
                            {
                                "business_name": sale_business,
                                "customer_name": sale_customer,
                                "customer_email": sale_email,
                                "payment_method": sale_method,
                                "payment_reference": sale_reference,
                                "payment_verified": True,
                            }
                        )
                    except (ValueError, OSError) as exc:
                        st.error(f"No se pudo registrar la venta: {exc}")
                    else:
                        state = load_state()
                        state["income"].append(
                            {
                                "id": uuid.uuid4().hex,
                                "method": sale_method,
                                "amount_cents": PAYMENT_OPTIONS[sale_method],
                                "description": (
                                    f"{sale_business.strip()} · {sale_reference.strip()}"
                                ).strip(" ·"),
                                "date": date.today().isoformat(),
                                "contract_id": sale_notification["contract_id"],
                            }
                        )
                        save_state(state)
                        st.session_state["flash_message"] = (
                            f"Venta verificada registrada para {sale_business.strip()}."
                        )
                        st.rerun()
    with st.expander("Solicitar Presupuesto a la Medida"):
        quote_request = render_quote_request_form(
            "Cliente prospecto",
            "",
            "ceo_quote_request",
        )
        if quote_request:
            st.success(
                f"Solicitud guardada. Estimación: "
                f"{money(quote_request['total_cents'])}; enviada al cliente y al CEO "
                "para revisión y aprobación."
            )
    if state["quote_requests"]:
        st.markdown("#### Solicitudes de presupuesto para seguimiento")
        for quote_request in state["quote_requests"][:20]:
            with st.container(border=True):
                title_col, amount_col = st.columns([3, 1])
                with title_col:
                    st.markdown(
                        f"**{quote_request['business_name']}** · "
                        f"{quote_request['customer_name']}"
                    )
                    st.caption(
                        f"{quote_request['customer_email']} · "
                        f"{quote_request['created_at']}"
                    )
                    st.write(quote_request["scope"])
                    st.caption(
                        " · ".join(
                            f"{item['description']}: {money(item['amount_cents'])}"
                            for item in quote_request["line_items"]
                        )
                    )
                with amount_col:
                    st.metric("Estimación", money(quote_request["total_cents"]))
                    if quote_request.get("status") == "approved":
                        st.success("Aprobado")
                    elif st.button(
                        "Aprobar presupuesto",
                        key=f"approve_quote_{quote_request['id']}",
                        type="primary",
                    ):
                        try:
                            approve_custom_quote(quote_request["id"])
                        except (OSError, RuntimeError, ValueError) as exc:
                            st.error(f"No se pudo aprobar el presupuesto: {exc}")
                        else:
                            st.session_state["flash_message"] = (
                                f"Presupuesto aprobado para "
                                f"{quote_request['business_name']}."
                            )
                            st.rerun()
    for contract in state["contracts"]:
        contract_id = contract.get("id") or uuid.uuid4().hex
        contract["id"] = contract_id
        with st.expander(
            f"{contract.get('business_name', 'Contrato')} · "
            f"{CONTRACT_STATUSES.get(contract.get('status'), 'Pendiente')}"
        ):
            with st.form(f"contract_form_{contract_id}"):
                contract_col1, contract_col2 = st.columns(2)
                with contract_col1:
                    status = st.selectbox(
                        "Estado del contrato",
                        list(CONTRACT_STATUSES),
                        index=(
                            list(CONTRACT_STATUSES).index(contract["status"])
                            if contract.get("status") in CONTRACT_STATUSES
                            else 0
                        ),
                        format_func=CONTRACT_STATUSES.get,
                        key=f"contract_status_{contract_id}",
                    )
                    payment_method = st.selectbox(
                        "Pasarela utilizada",
                        list(PAYMENT_OPTIONS),
                        index=(
                            list(PAYMENT_OPTIONS).index(contract["payment_method"])
                            if contract.get("payment_method") in PAYMENT_OPTIONS
                            else 0
                        ),
                        key=f"contract_method_{contract_id}",
                    )
                    st.metric(
                        "Monto oficial",
                        money(PAYMENT_OPTIONS[payment_method]),
                    )
                    payment_reference = st.text_input(
                        "Referencia o ID de pago",
                        value=contract.get("payment_reference", ""),
                        key=f"contract_reference_{contract_id}",
                    )
                    payment_verified = st.checkbox(
                        "Pago verificado manualmente",
                        value=contract.get("payment_verified", False),
                        key=f"contract_verified_{contract_id}",
                    )
                with contract_col2:
                    delivery_date = st.text_input(
                        "Fecha de entrega",
                        value=contract.get("delivery_date", ""),
                        placeholder="YYYY-MM-DD",
                        key=f"contract_delivery_date_{contract_id}",
                    )
                    warranty = st.text_input(
                        "Garantía",
                        value=contract.get(
                            "warranty",
                            "30 días de soporte correctivo desde la activación.",
                        ),
                        key=f"contract_warranty_{contract_id}",
                    )
                    internal_notes = st.text_area(
                        "Notas internas del CEO",
                        value=contract.get("internal_notes", ""),
                        key=f"contract_notes_{contract_id}",
                    )
                delivery_details = st.text_area(
                    "Detalle de la Nota de Entrega para el cliente",
                    value=contract.get(
                        "delivery_details",
                        "Sistema de Captación Web\nCotizador IA\nIntegración con canales de contacto",
                    ),
                    key=f"contract_delivery_details_{contract_id}",
                )
                contract_saved = st.form_submit_button(
                    "💾 Guardar Control Interno"
                )
                if contract_saved:
                    effective_status = (
                        "paid_activation"
                        if payment_verified and status == "pending_verification"
                        else status
                    )
                    if effective_status in ("paid_activation", "delivered_active") and not payment_verified:
                        st.error(
                            "Verifica el pago antes de marcarlo como pagado o entregado."
                        )
                    elif effective_status == "delivered_active" and not delivery_date.strip():
                        st.error("Indica la fecha efectiva de entrega.")
                    elif (
                        contract.get("payment_verified")
                        and payment_method != contract.get("payment_method")
                    ):
                        st.error(
                            "La pasarela no se puede cambiar después de verificar el pago."
                        )
                    elif delivery_date.strip() and not valid_iso_date(delivery_date.strip()):
                        st.error("La fecha debe tener formato YYYY-MM-DD.")
                    else:
                        was_verified = contract.get("payment_verified", False)
                        contract.update(
                            {
                                "status": effective_status,
                                "payment_method": payment_method,
                                "amount_cents": PAYMENT_OPTIONS[payment_method],
                                "payment_reference": payment_reference.strip(),
                                "payment_verified": payment_verified,
                                "delivery_date": delivery_date.strip(),
                                "warranty": warranty.strip(),
                                "internal_notes": internal_notes.strip(),
                                "delivery_details": delivery_details.strip(),
                            }
                        )
                        if payment_verified and not was_verified:
                            state["income"].append(
                                {
                                    "id": uuid.uuid4().hex,
                                    "method": payment_method,
                                    "amount_cents": PAYMENT_OPTIONS[payment_method],
                                    "description": (
                                        f"{contract.get('business_name')} · "
                                        f"{payment_reference.strip()}"
                                    ).strip(" ·"),
                                    "date": date.today().isoformat(),
                                    "contract_id": contract_id,
                                }
                            )
                        if payment_verified:
                            for notification in state["purchase_notifications"]:
                                if notification.get("contract_id") == contract_id:
                                    notification["status"] = "verified"
                                    notification["verified_at"] = (
                                        datetime.now()
                                        .astimezone()
                                        .isoformat(timespec="seconds")
                                    )
                        save_state(state)
                        if payment_verified:
                            try:
                                contract["matched_leads"] = mark_matching_leads_as_closed(
                                    contract["business_name"],
                                    contract["customer_email"],
                                )
                                contract.pop("lead_sync_error", None)
                            except RuntimeError as exc:
                                contract["lead_sync_error"] = str(exc)
                                st.session_state["flash_warning"] = (
                                    f"El pago quedó verificado, pero no se pudo "
                                    f"sincronizar el lead en SQLite: {exc}"
                                )
                            save_state(state)
                        st.session_state["flash_message"] = (
                            f"Control interno guardado para {contract.get('business_name')}."
                        )
                        st.rerun()
            if (
                contract.get("status") != "delivered_active"
                or not contract.get("payment_verified")
            ):
                st.info(
                    "La Nota de Entrega Oficial se habilitará cuando el pago esté "
                    "verificado y el contrato figure como «Entregado y activo»."
                )
            if st.button(
                "📧 Generar y Enviar Nota de Entrega Oficial al Cliente",
                key=f"send_delivery_note_{contract_id}",
                disabled=(
                    contract.get("status") != "delivered_active"
                    or not contract.get("payment_verified")
                ),
                use_container_width=True,
            ):
                enqueue_background_job(
                    f"Nota de entrega: {contract.get('business_name')}",
                    enviar_nota_entrega_cliente,
                    dict(contract),
                )
                st.success("El envío de la Nota de Entrega quedó en cola.")

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
        st.markdown(
            f"""
            <div class="sidebar-brand">
              {brand_logo_html()}
              <span class="sidebar-role">CEO COMMAND CENTER</span>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.caption(f"Correo oficial de soporte: {support_email_visual()}")
        with st.expander("Configuración de publicación y pagos", expanded=False):
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
                st.markdown("**Datos oficiales de pago**")
                payment_links = {}
                for method in PAYMENT_OPTIONS:
                    placeholders = {
                        "Binance Pay / USDT": "UID45489956",
                        "Payoneer (Tarjeta/ACH)": "Correo o datos de la cuenta receptora en USD",
                        "PayPal": "https://paypal.me/tuusuario o correo de PayPal",
                    }
                    field = (
                        st.text_area
                        if method == "Payoneer (Tarjeta/ACH)"
                        else st.text_input
                    )
                    payment_links[method] = field(
                        method,
                        value=settings["payment_links"].get(method, ""),
                        placeholder=placeholders[method],
                        key=f"payment_url_{slugify(method)}",
                        **(
                            {"height": 72}
                            if method == "Payoneer (Tarjeta/ACH)"
                            else {}
                        ),
                    )
                save_settings = st.form_submit_button(
                    "Guardar configuración", use_container_width=True
                )
                if save_settings:
                    if (
                        prototype_base_url.strip()
                        and (
                            not valid_web_url(prototype_base_url)
                            or urlparse(prototype_base_url).scheme != "https"
                        )
                    ):
                        st.error(
                            "La URL pública de demos debe ser una dirección HTTPS válida."
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
        pending_count = pending_prototype_count(state)
        st.metric("Propuestas por revisar", pending_count)
        st.caption(
            "Los mensajes salen solo tras acciones explícitas; los pagos no se procesan aquí."
        )
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
                elif job["kind"] == "lead_proposal":
                    refresh_app = True
            else:
                if job["kind"] == "scraper":
                    job["scraper_result"] = result
                    return_code = int(result.get("returncode", 1))
                    job["status"] = "succeeded" if return_code == 0 else "failed"
                    job["result"] = (
                        "Motor completado."
                        if return_code == 0
                        else f"El motor terminó con código {return_code}."
                    )
                    if return_code != 0:
                        job["error"] = job["result"]
                    refresh_app = True
                elif result:
                    job["status"] = "warning"
                    job["warning"] = str(result)
                else:
                    job["status"] = "succeeded"
                    job["result"] = "Tarea completada."
                refresh_app = refresh_app or job["kind"] in (
                    "approval",
                    "lead_proposal",
                    "phase_two",
                )
        elif job["status"] == "queued" and future.running():
            job["status"] = "running"
        if job["status"] in ("succeeded", "warning", "failed") and job["kind"] == "ceo_alert":
            with STATE_LOCK:
                state = load_state()
                alert = next(
                    (
                        item
                        for item in state["activity_alerts"]
                        if item.get("id") == job["record_id"]
                    ),
                    None,
                )
                if alert:
                    alert["status"] = (
                        "email_sent"
                        if job["status"] == "succeeded"
                        else "email_failed"
                    )
                    if job["status"] != "succeeded":
                        alert["email_error"] = (
                            job.get("error") or job.get("warning", "Error de envío")
                        )
                    save_state(state)
            refresh_app = True

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
            if job.get("kind") == "scraper" and job.get("scraper_result"):
                result = job["scraper_result"]
                with st.expander(f"Ver salida: {job['label']}"):
                    steps = (
                        result.get("steps", [])
                        if isinstance(result.get("steps"), list)
                        else [result]
                    )
                    for step in steps:
                        st.markdown(f"**{step.get('script', 'Motor')}**")
                        if step.get("backup_path"):
                            st.caption(f"Respaldo previo a la purga: {step['backup_path']}")
                        if step.get("stdout"):
                            st.code(step["stdout"], language="text")
                        if step.get("stderr"):
                            st.error(step["stderr"])
    if refresh_app:
        st.rerun()


def render_recent_purchase_notifications(state: dict) -> None:
    st.markdown("### 🔔 Alertas y Actividad Reciente")
    alerts = state["activity_alerts"][:8]
    purchases = state["purchase_notifications"][:8]
    if not alerts and not purchases and not state["quote_requests"]:
        with st.container(border=True):
            icon_col, message_col = st.columns([1, 8])
            with icon_col:
                st.markdown(
                    '<div class="empty-activity-icon">✦</div>',
                    unsafe_allow_html=True,
                )
            with message_col:
                st.markdown("**Tu actividad empieza aquí**")
                st.caption(
                    "Aún no hay alertas ni solicitudes. Lanza tu primera operación "
                    "o envía una solicitud de presupuesto para iniciar el seguimiento."
                )
        return
    if alerts:
        st.markdown("**Alertas por correo**")
        for alert in alerts:
            with st.container(border=True):
                left, right = st.columns([4, 1])
                with left:
                    st.markdown(
                        f"**{alert.get('event_label', 'Actividad')}** · "
                        f"{alert.get('business_name', 'Negocio')}"
                    )
                    st.caption(
                        f"{alert.get('created_at', '')} · "
                        f"{alert.get('customer_name', 'Cliente')} · "
                        f"{alert.get('customer_email') or 'Correo no indicado'}"
                    )
                    if alert.get("details"):
                        st.caption(alert["details"])
                with right:
                    status = alert.get("status", "email_queued")
                    if status in ("email_queued", "queued", "running"):
                        st.info("Correo en cola")
                    elif status == "email_sent":
                        st.success("Correo enviado")
                    else:
                        st.warning(
                            f"Envío pendiente de revisión: "
                            f"{alert.get('email_error', 'error no especificado')}"
                        )
    if state["quote_requests"]:
        st.markdown("**Presupuestos personalizados**")
        for quote_request in state["quote_requests"][:5]:
            status = (
                "Aprobado por el CEO"
                if quote_request.get("status") == "approved"
                else "Pendiente de aprobación"
            )
            st.caption(
                f"{quote_request['created_at']} · {quote_request['business_name']} · "
                f"{money(quote_request['total_cents'])} · {status}"
            )
    if not purchases:
        return
    st.markdown("**Reportes de pago y activación**")
    recent = purchases
    for notification in recent:
        verified = notification.get("status") == "verified"
        status_text = "Pago verificado manualmente" if verified else "Pendiente de verificación"
        with st.container(border=True):
            left, right = st.columns([3, 1])
            with left:
                st.markdown(
                    f"**{notification.get('business_name', 'Negocio')}** · "
                    f"{notification.get('customer_name', 'Cliente')}"
                )
                st.caption(
                    f"{notification.get('created_at', '')} · "
                    f"{notification.get('payment_method', '')} · "
                    f"{money(notification.get('amount_cents', 0))} · "
                    f"{notification.get('customer_email', '')}"
                )
                if notification.get("payment_reference"):
                    st.caption(f"Referencia: {notification['payment_reference']}")
            with right:
                if verified:
                    st.success(status_text)
                else:
                    st.warning(status_text)


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
            payment_options_open=payment_options_open,
            track_payment_open=not payment_options_open,
        ),
        height=1550,
    )
    st.subheader("✅ Ya realicé el pago / Activar mi sistema ahora")
    st.caption(
        "Este formulario registra tu reporte para que el equipo verifique la referencia. "
        "No procesa pagos ni confirma automáticamente una transacción."
    )
    payment_labels = {
        method: f"{method} — ${amount / 100:.0f} USD"
        for method, amount in PAYMENT_OPTIONS.items()
    }
    with st.form(f"purchase_report_{record['id']}", clear_on_submit=True):
        customer_name = st.text_input("Nombre")
        customer_email = st.text_input("Correo electrónico")
        payment_method = st.selectbox(
            "Pasarela utilizada", list(PAYMENT_OPTIONS), format_func=payment_labels.get
        )
        payment_reference = st.text_input("Número de referencia de pago")
        customer_notes = st.text_area("Notas adicionales (opcional)")
        request_delivery_note = st.checkbox(
            "Solicito recibir mi Nota de Entrega oficial automáticamente en mi correo",
            value=True,
        )
        submitted = st.form_submit_button(
            "Enviar reporte de pago y solicitar activación", type="primary"
        )
        if submitted:
            if not customer_name.strip():
                st.error("Indica tu nombre.")
            elif not re.fullmatch(
                r"[^@\s]+@[^@\s]+\.[^@\s]+", customer_email.strip()
            ):
                st.error("Indica una dirección de correo válida.")
            elif not payment_reference.strip():
                st.error("Indica la referencia del pago para que podamos verificarla.")
            else:
                try:
                    notification = notificar_compra_ceo(
                        {
                            "business_name": record["business_name"],
                            "customer_name": customer_name,
                            "customer_email": customer_email,
                            "payment_method": payment_method,
                            "payment_reference": payment_reference,
                            "notes": customer_notes,
                            "delivery_note_requested": request_delivery_note,
                            "prototype_id": record["id"],
                        }
                    )
                except (ValueError, OSError) as exc:
                    st.error(f"No se pudo registrar la solicitud: {exc}")
                else:
                    st.session_state[f"purchase_confirmation_{record['id']}"] = (
                        notification["id"]
                    )
                    if notification.get("lead_sync_error"):
                        st.session_state[
                            f"purchase_sync_warning_{record['id']}"
                        ] = notification["lead_sync_error"]
                    st.rerun()
    if st.session_state.get(f"purchase_confirmation_{record['id']}"):
        st.success(
            "Recibimos tu solicitud. Verificaremos el pago y te contactaremos; "
            "la activación se coordina después de esa verificación. La Nota de Entrega "
            "oficial se enviará cuando el sistema esté entregado y activo."
        )
    sync_warning = st.session_state.get(
        f"purchase_sync_warning_{record['id']}"
    )
    if sync_warning:
        st.warning(
            "La solicitud quedó registrada, pero no se pudo sincronizar el embudo "
            f"SQLite: {sync_warning}"
        )

    with st.container(border=True):
        quote_request = render_quote_request_form(
            record["business_name"],
            record["id"],
            f"public_quote_{record['id']}",
        )
        if quote_request:
            st.session_state[f"quote_confirmation_{record['id']}"] = quote_request["id"]
    if st.session_state.get(f"quote_confirmation_{record['id']}"):
        st.success(
            "La solicitud de presupuesto fue registrada y la estimación se envió "
            "para revisión. La propuesta definitiva requiere aprobación expresa."
        )

    st.subheader("Preguntas frecuentes")
    for question, answer in (
        (
            "¿Cuánto tiempo toma dejarlo instalado en mi negocio?",
            "La activación se coordina en un plazo estimado de 24 horas tras confirmar el pago y los accesos necesarios.",
        ),
        (
            "¿Funciona con mis canales de contacto actuales y mi dominio?",
            "Sí. Se puede configurar para integrarse con tus canales de contacto y dominio actuales; la configuración final se confirma al activar el proyecto.",
        ),
        (
            "¿Puedo solicitar Nota de Entrega o comprobante corporativo?",
            "Sí. Puedes solicitar la Nota de Entrega aquí; se envía automáticamente después de verificar el pago y registrar la entrega.",
        ),
        (
            "¿Dónde recibo soporte técnico directo?",
            f"Atención prioritaria en {SUPPORT_EMAIL}.",
        ),
    ):
        with st.expander(question):
            st.write(answer)


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
        :root { --apex-space: #0B0F19; --apex-cyan: #00F2FE; --apex-green: #10B981; }
        .stApp { background: radial-gradient(ellipse at 80% -15%, #12333b 0, transparent 36%), #0B0F19; }
        [data-testid="stHeader"] { background: rgba(11, 15, 25, .9); }
        [data-testid="stSidebar"] { background: linear-gradient(180deg, #101827, #0c121d); border-right: 1px solid #243243; }
        [data-testid="stSidebar"] > div:first-child { padding-top: 1rem; }
        .sidebar-brand { margin: 0 0 1.2rem; padding: .25rem .2rem 1rem; border-bottom: 1px solid #263243; }
        .sidebar-brand .apex-mark { width: 46px; height: 46px; }
        .sidebar-brand .apex-brand-copy strong { font-size: 1.05rem; color: white; }
        .sidebar-brand .apex-brand-copy small { color: var(--apex-cyan); }
        .sidebar-role { display: block; margin: .75rem 0 0 .2rem; color: #8493a7; font-size: .65rem; font-weight: 700; letter-spacing: .18em; }
        .apex-brand { color: #fff !important; }
        .apex-brand-copy strong { color: #f8fbff !important; }
        .apex-brand-copy small { color: #00F2FE !important; }
        .empty-activity-icon {
            display: grid; width: 2.5rem; height: 2.5rem; place-items: center;
            border: 1px solid #00f2fe55; border-radius: 14px;
            background: linear-gradient(145deg, #112b36, #111b28);
            color: #00F2FE; font-size: 1.25rem;
        }
        .engine-status {
            display: inline-flex; align-items: center; gap: .55rem;
            margin: .4rem 0 1.2rem; padding: .42rem .72rem;
            border: 1px solid #10b98155; border-radius: 999px;
            background: #10b98112; color: #b5f5dd;
            font-size: .78rem; font-weight: 650; letter-spacing: .01em;
        }
        .engine-status-dot {
            width: .48rem; height: .48rem; border-radius: 50%;
            background: #10B981; box-shadow: 0 0 12px #10b981aa;
        }
        h1, h2, h3 { letter-spacing: -.025em; }
        h1 { color: #f7fbff; }
        [data-testid="stCaptionContainer"], [data-testid="stMarkdownContainer"] p { color: #aebacc; }
        [data-testid="stMetric"] {
            background: linear-gradient(145deg, #141f2d, #101824); border: 1px solid #29394a;
            padding: 1rem 1.1rem; border-radius: 16px; box-shadow: 0 12px 30px #0002;
        }
        [data-testid="stVerticalBlockBorderWrapper"] {
            background: linear-gradient(145deg, #111b28, #0e1621); border-color: #26374a;
            border-radius: 18px; padding: .7rem; box-shadow: 0 14px 34px #0002;
        }
        .stButton > button, .stDownloadButton > button {
            border: 1px solid #314357; border-radius: 11px; min-height: 2.8rem;
            background: linear-gradient(135deg, #182638, #121d2b); color: #edf8ff; font-weight: 650;
            transition: border-color .18s ease, transform .18s ease, box-shadow .18s ease;
        }
        .stButton > button:hover, .stDownloadButton > button:hover {
            border-color: var(--apex-cyan); color: white; transform: translateY(-1px);
            box-shadow: 0 7px 22px #00f2fe1c;
        }
        .stButton > button[kind="primary"] {
            border-color: #10B981; background: linear-gradient(135deg, #10B981, #087c62); color: #031a14;
        }
        .stTextArea textarea, .stTextInput input, [data-baseweb="select"] > div,
        [data-testid="stNumberInput"] input {
            background: #101a27; border-color: #304052; border-radius: 10px; color: #f1f5f9;
        }
        .stTextArea textarea:focus, .stTextInput input:focus, [data-testid="stNumberInput"] input:focus {
            border-color: var(--apex-cyan); box-shadow: 0 0 0 1px var(--apex-cyan);
        }
        [data-baseweb="tab-list"] { gap: .45rem; border-bottom: 1px solid #29384a; }
        [data-baseweb="tab"] { border-radius: 10px 10px 0 0; color: #aebacc; }
        [aria-selected="true"][data-baseweb="tab"] { color: var(--apex-cyan); }
        [data-testid="stForm"] { border-color: #29394a; border-radius: 16px; background: #0e1723; }
        [data-testid="stAlert"] { border-radius: 12px; }
        a { color: var(--apex-cyan); }
        @media(max-width:700px) {
            [data-testid="stMetric"] { padding: .8rem; }
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
        render_demo_view(demo_record, state, payments_opened)
        st.stop()

    if not require_ceo_login():
        st.stop()

    render_sidebar_settings(state)
    title_column, status_column = st.columns([3, 2], vertical_alignment="center")
    with title_column:
        st.title(f"{AGENCY_NAME} — CEO Command Center")
    with status_column:
        st.markdown(
            '<div class="engine-status"><span class="engine-status-dot"></span>'
            "⚡ Motor de Agentes: Activo</div>",
            unsafe_allow_html=True,
        )
    st.caption("Centro de mando de Apex AI Agency para prototipos, aprobaciones y operaciones.")
    flash = st.session_state.pop("flash_message", None)
    if flash:
        st.success(flash)
    flash_warning = st.session_state.pop("flash_warning", None)
    if flash_warning:
        st.warning(flash_warning)
    render_recent_purchase_notifications(state)

    pending_count = pending_prototype_count(state)
    factory_tab, prospecting_tab, approval_tab, finance_tab = st.tabs(
        [
            "🏭 Lanzar Operación (Fábrica)",
            "🔎 Prospección SQLite",
            f"🛡️ Bandeja de Aprobación ({pending_count})",
            "💰 Control Financiero y Mantenimiento ($)",
        ]
    )
    with factory_tab:
        render_factory_tab(state)
    with prospecting_tab:
        render_prospecting_tab(state)
    with approval_tab:
        render_approval_tab(state)
    with finance_tab:
        render_finance_tab(state)
    render_background_jobs()


if __name__ == "__main__":
    main()
