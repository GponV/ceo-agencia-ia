from __future__ import annotations

import html
import json
import re
import shutil
import time
import uuid
from datetime import date, datetime
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components


ROOT = Path(__file__).resolve().parent
PENDING_DIR = ROOT / "02_BANDEJA_APROBACION" / "pendientes"
APPROVED_DIR = ROOT / "02_BANDEJA_APROBACION" / "aprobados"
DATA_DIR = ROOT / ".ceo_dashboard"
STATE_FILE = DATA_DIR / "state.json"

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
PAYMENT_COPY = (
    "1. Binance Pay / USDT — $380 USD (5% de descuento)\n"
    "   Enlace oficial: pendiente de configurar.\n"
    "2. Payoneer (Tarjeta/ACH) — $400 USD\n"
    "   Enlace oficial: pendiente de configurar.\n"
    "3. PayPal — $425 USD\n"
    "   Enlace oficial: pendiente de configurar."
)
STATUS_LABELS = {
    "pending": "Pendiente de aprobación",
    "changes_requested": "Ajustes solicitados",
    "approved": "Aprobado; envío simulado",
}


def default_state() -> dict:
    return {"prototypes": [], "income": [], "clients": []}


def load_state() -> dict:
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
    return state


def save_state(state: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temporary_file = STATE_FILE.with_suffix(".tmp")
    temporary_file.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_file.replace(STATE_FILE)


def money(cents: int) -> str:
    return f"${cents / 100:,.2f} USD"


def safe_text(value: str) -> str:
    return html.escape(value.strip(), quote=True)


def slugify(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return normalized or "prototipo"


def make_prototype_html(name: str, service: str, zone: str) -> str:
    business = safe_text(name)
    category = safe_text(service)
    location = safe_text(zone)
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
    <style>body{{font-family:"DM Sans",sans-serif}}h1,h2,h3{{font-family:"Playfair Display",serif}}</style>
  </head>
  <body class="bg-stone-50 text-stone-900 antialiased">
    <div class="bg-emerald-950 px-5 py-2 text-center text-xs font-medium tracking-wide text-emerald-50">
      DEMOSTRACIÓN CONCEPTUAL · Negocio simulado · No publicado
    </div>
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
  </body>
</html>"""


def create_prototypes(niche: str, objective: str) -> list[dict]:
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
        html_content = make_prototype_html(name, service, NICHES[niche]["zone"])
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


def register_change_request(state: dict, record: dict, note: str) -> None:
    record["status"] = "changes_requested"
    record["note"] = note.strip()
    record["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    folder = safe_prototype_folder(record, PENDING_DIR)
    (folder / "proposal.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    save_state(state)


def proposal_email(record: dict) -> str:
    return (
        f"Asunto: Una idea para {record['business_name']}\n\n"
        f"Hola, equipo de {record['business_name']}:\n\n"
        f"Preparamos un concepto de página web para {record['service'].lower()} "
        f"en {record['zone']}, pensado para presentar sus servicios y facilitar "
        "las solicitudes de cita.\n\n"
        "La página es solo un prototipo de demostración; no está publicada y su "
        "formulario de reservas no está conectado. Podemos personalizarla juntos.\n\n"
        f"{PAYMENT_COPY}\n\n"
        "Gracias,\nEquipo de Prueba Anticipada"
    )


def show_prototype_preview(record: dict, key_prefix: str) -> None:
    folder = ROOT / record["folder"]
    prototype_path = folder / "index.html"
    if not prototype_path.is_file():
        st.error(f"No se encuentra el prototipo local: {record['folder']}\\index.html")
        return
    prototype_html = prototype_path.read_text(encoding="utf-8")
    col_preview, col_download = st.columns([1, 1])
    with col_preview:
        if st.button("Vista previa del prototipo", key=f"{key_prefix}_preview"):
            st.session_state[f"{key_prefix}_show_preview"] = not st.session_state.get(
                f"{key_prefix}_show_preview", False
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
        components.html(prototype_html, height=760, scrolling=True)
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
                created = create_prototypes(selected_niche, objective)
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
        st.success(
            f"{len(created)} prototipo(s) listo(s) para revisar en la pestaña de aprobación."
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
        "Human-in-the-Loop: aprobar simula el envío y archiva el prototipo; "
        "no se envía ningún correo real ni se publica en producción."
    )
    pending = [
        record
        for record in state["prototypes"]
        if record.get("status") in ("pending", "changes_requested")
    ]
    if not pending:
        st.success("No hay propuestas pendientes. Lanza una operación para generar prototipos.")
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
            show_prototype_preview(record, key)
            with st.expander("Vista previa del correo y opciones de pago"):
                st.code(proposal_email(record), language="text")
                st.caption(
                    "No se proporcionaron URLs de pago: los enlaces oficiales quedan "
                    "marcados como pendientes y no se inventan."
                )
            approve_col, reject_col = st.columns(2)
            with approve_col:
                if st.button(
                    "✅ Aprobar y Enviar Propuesta",
                    key=f"approve_{key}",
                    type="primary",
                    use_container_width=True,
                ):
                    try:
                        approve_prototype(state, record)
                    except (OSError, ValueError, KeyError) as exc:
                        st.error(f"No se pudo aprobar esta propuesta: {exc}")
                    else:
                        st.session_state["flash_message"] = (
                            f"{record['business_name']}: prototipo archivado; envío simulado, "
                            "sin correo real."
                        )
                        st.rerun()
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
                show_prototype_preview(record, f"approved_{record['id']}")


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

    with st.sidebar:
        st.markdown("# 🎛️ CEO")
        st.markdown("### Command Center")
        st.caption("Operaciones locales · Aprobación humana")
        st.divider()
        st.markdown("**Estado de la fábrica**")
        pending_count = sum(
            record.get("status") in ("pending", "changes_requested")
            for record in state["prototypes"]
        )
        st.metric("Propuestas por revisar", pending_count)
        st.caption("No se envían correos ni se ejecutan pagos desde esta app.")
    st.title("CEO Command Center")
    st.caption("Tu centro de mando para prototipos, aprobaciones y operaciones de la agencia.")
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


if __name__ == "__main__":
    main()
