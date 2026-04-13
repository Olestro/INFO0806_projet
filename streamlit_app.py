"""Application Streamlit pour la gestion de lecteur RFID Impinj R220.

Interface complète pour:
- Gérer les presets (créer, charger, supprimer)
- Visualiser les Mode Classique lus
- Lancer des scans
"""

import re
import streamlit as st
import json
from pathlib import Path
from datetime import datetime
import time
from contextlib import redirect_stdout, redirect_stderr
from html import escape
import pandas as pd
import threading
import uuid
from connexion_decodeur import (
    load_json, save_json, valider_parametres, sauvegarder_preset,
    charger_preset, supprimer_preset, lister_presets, parse_antennas,
    DEFAULT_IP, DEFAULT_PORT, DEFAULT_TIMEOUT, DEFAULT_ANTENNAS,
    run_inventory
)

# ────────────────────────────────────────────────────────────────────
# Configuration Streamlit
# ────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="RFID Reader Manager",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ────────────────────────────────────────────────────────────────────
# Chemins
# ────────────────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).parent
PRESETS_FILE = SCRIPT_DIR / "presets.json"
TAGS_FILE = SCRIPT_DIR / "tags.json"
CONFIG_FILE = SCRIPT_DIR / "config.json"
WHITELIST_FILE = SCRIPT_DIR / "whitelist.json"
LIVE_LOG_FILE = SCRIPT_DIR / "rfid_terminal_live.log"
ARCHIVE_LOG_FILE = SCRIPT_DIR / "rfid_terminal_archive.log"
CLASSIC_SCAN_THREADS: dict[str, threading.Thread] = {}

def normalize_epc(value: str) -> str:
    """Normalise un EPC pour comparaison robuste."""
    return str(value).strip().lower()


def load_whitelist() -> list[str]:
    """Charge la whitelist depuis whitelist.json, meme si le fichier est vide/mal forme."""
    if not WHITELIST_FILE.exists() or WHITELIST_FILE.stat().st_size == 0:
        return []

    try:
        raw = load_json(WHITELIST_FILE)
    except Exception:
        return []

    epcs: list[str] = []
    if isinstance(raw, dict):
        candidates = raw.get("epcs", raw.get("whitelist", []))
    elif isinstance(raw, list):
        candidates = raw
    else:
        candidates = []

    for item in candidates:
        epc = normalize_epc(item)
        if epc and epc not in epcs:
            epcs.append(epc)

    return epcs


def save_whitelist(epcs: list[str]) -> None:
    """Sauvegarde la whitelist au format {epcs:[...]}"""
    unique_epcs = []
    for epc in epcs:
        norm = normalize_epc(epc)
        if norm and norm not in unique_epcs:
            unique_epcs.append(norm)
    save_json(WHITELIST_FILE, {"epcs": unique_epcs})


def append_terminal_log(message: str) -> None:
    """Ajoute des lignes au log live et a l'archive avec timestamp."""
    LIVE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = message.splitlines() or [""]
    with open(LIVE_LOG_FILE, "a", encoding="utf-8") as live_f, open(ARCHIVE_LOG_FILE, "a", encoding="utf-8") as archive_f:
        for line in lines:
            formatted = f"[{timestamp}] {line}\n"
            live_f.write(formatted)
            archive_f.write(formatted)


def read_terminal_log(max_lines: int = 400, source: str = "live") -> str:
    """Lit les dernieres lignes du log terminal live ou archive."""
    log_file = LIVE_LOG_FILE if source == "live" else ARCHIVE_LOG_FILE
    if not log_file.exists():
        return ""
    with open(log_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    content = "".join(lines[-max_lines:])
    # Insere un saut de ligne avant chaque entree commencant par un timestamp [YYYY-MM-DD HH:MM:SS]
    content = re.sub(r"(?<!\A)(\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\])", r"\n\1", content).lstrip("\n")
    return content


class TerminalLogStream:
    """Flux texte compatible stdout/stderr qui ecrit dans le log en continu."""
    def __init__(self) -> None:
        self._buffer = ""

    def write(self, text: str) -> int:
        if not text:
            return 0

        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            append_terminal_log(line)

        return len(text)

    def flush(self) -> None:
        if self._buffer:
            append_terminal_log(self._buffer)
            self._buffer = ""


def run_scan_with_logging(cfg: dict, preset_name: str) -> int:
    """Execute run_inventory en capturant stdout/stderr dans le terminal log."""
    append_terminal_log(
        f"Demarrage scan preset='{preset_name}' "
        f"ip={cfg['ip']} port={cfg['port']} antennas={cfg['antennas']} duration={cfg['duration']}"
    )

    terminal_stream = TerminalLogStream()
    with redirect_stdout(terminal_stream), redirect_stderr(terminal_stream):
        result = run_inventory(
            cfg["ip"], cfg["port"], cfg["timeout"],
            cfg["antennas"], cfg["duration"],
            cfg.get("afficher_antennes", False)
        )
    terminal_stream.flush()
    return result

def _get_session_uid() -> str:
    if "session_uid" not in st.session_state:
        st.session_state.session_uid = str(uuid.uuid4())
    return st.session_state.session_uid


def _parse_record_datetime(value) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            return _parse_record_datetime(int(text))
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 1e12:
            timestamp /= 1_000_000
        try:
            return datetime.fromtimestamp(timestamp)
        except (ValueError, OSError, OverflowError):
            return None
    return None


def _load_tags_payload(uploaded_file=None) -> list[dict]:
    if uploaded_file is not None:
        try:
            payload = json.loads(uploaded_file.getvalue().decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"Impossible de lire le fichier JSON importé: {exc}") from exc
    else:
        try:
            payload = load_json(TAGS_FILE)
        except Exception:
            return []

    if isinstance(payload, dict):
        tags = payload.get("tags", [])
    elif isinstance(payload, list):
        tags = payload
    else:
        raise ValueError("Format JSON non pris en charge.")

    if not isinstance(tags, list):
        raise ValueError("Le JSON doit contenir une liste de tags.")

    return [tag for tag in tags if isinstance(tag, dict)]


def _build_classic_tag_table(raw_tags: list[dict], debounce_seconds: int) -> pd.DataFrame:
    rows: dict[str, dict] = {}
    default_time = datetime.min

    def sort_key(record: dict) -> datetime:
        return (
            _parse_record_datetime(
                record.get("client_timestamp")
                or record.get("reader_timestamp")
                or record.get("timestamp")
            )
            or default_time
        )

    for record in sorted(raw_tags, key=sort_key):
        tag_id = str(record.get("epc") or record.get("EPC") or record.get("tag_id") or "").strip()
        if not tag_id:
            continue

        record_time = _parse_record_datetime(
            record.get("client_timestamp")
            or record.get("reader_timestamp")
            or record.get("timestamp")
        ) or datetime.now()

        antenna_value = record.get("antenna", record.get("AntennaID"))
        try:
            antenna_value = int(antenna_value) if antenna_value is not None and antenna_value != "" else None
        except (TypeError, ValueError):
            antenna_value = str(antenna_value) if antenna_value is not None else None

        rssi_value = record.get("rssi", record.get("PeakRSSI"))
        try:
            rssi_value = float(rssi_value)
        except (TypeError, ValueError):
            rssi_value = None

        row = rows.get(tag_id)
        if row is None:
            rows[tag_id] = {
                "tag_id": tag_id,
                "read_count": 1,
                "first_seen": record_time,
                "last_seen": record_time,
                "last_raw_seen": record_time,
                "antenna": antenna_value,
                "rssi_max": rssi_value,
            }
            continue

        if (record_time - row["last_raw_seen"]).total_seconds() > debounce_seconds:
            row["read_count"] += 1

        row["last_raw_seen"] = record_time
        row["last_seen"] = record_time
        if antenna_value is not None:
            row["antenna"] = antenna_value
        if rssi_value is not None:
            row["rssi_max"] = rssi_value if row["rssi_max"] is None else max(row["rssi_max"], rssi_value)

    if not rows:
        return pd.DataFrame(
            columns=["Tag ID", "Nb lectures", "Premiere visualisation", "Derniere visualisation", "Antenne", "RSSI max"]
        )

    data = []
    for row in rows.values():
        data.append({
            "Tag ID": row["tag_id"],
            "Nb lectures": row["read_count"],
            "Premiere visualisation": row["first_seen"].strftime("%Y-%m-%d %H:%M:%S"),
            "Derniere visualisation": row["last_seen"].strftime("%Y-%m-%d %H:%M:%S"),
            "Antenne": row["antenna"],
            "RSSI max": row["rssi_max"],
            "_sort_time": row["last_seen"],
        })

    df = pd.DataFrame(data)
    df = df.sort_values("_sort_time", ascending=False).drop(columns=["_sort_time"])
    return df.reset_index(drop=True)


def _render_classic_tag_table(raw_tags: list[dict], debounce_minutes: int) -> pd.DataFrame:
    debounce_seconds = max(0, int(debounce_minutes * 60))
    return _build_classic_tag_table(raw_tags, debounce_seconds)


def _start_classic_scan(cfg: dict, preset_name: str) -> bool:
    session_uid = _get_session_uid()
    existing_thread = CLASSIC_SCAN_THREADS.get(session_uid)
    if existing_thread is not None and existing_thread.is_alive():
        return False

    def worker() -> None:
        try:
            result = run_scan_with_logging(cfg, preset_name)
            if result == 0:
                append_terminal_log("Scan termine avec succes (mode classique)")
            else:
                append_terminal_log(f"Erreur scan mode classique code={result}")
        except Exception as exc:
            append_terminal_log(f"Exception mode classique: {exc}")
        finally:
            CLASSIC_SCAN_THREADS.pop(session_uid, None)

    thread = threading.Thread(target=worker, daemon=True)
    CLASSIC_SCAN_THREADS[session_uid] = thread
    thread.start()
    return True


def _is_classic_scan_running() -> bool:
    session_uid = _get_session_uid()
    thread = CLASSIC_SCAN_THREADS.get(session_uid)
    return thread is not None and thread.is_alive()



# ────────────────────────────────────────────────────────────────────
# Initialisation Session State
# ────────────────────────────────────────────────────────────────────

if "current_preset" not in st.session_state:
    st.session_state.current_preset = None
if "scan_running" not in st.session_state:
    st.session_state.scan_running = False
if "tags_data" not in st.session_state:
    st.session_state.tags_data = []
if "main_ip" not in st.session_state:
    st.session_state.main_ip = DEFAULT_IP
if "main_port" not in st.session_state:
    st.session_state.main_port = DEFAULT_PORT
if "main_timeout" not in st.session_state:
    st.session_state.main_timeout = DEFAULT_TIMEOUT
if "main_antennas" not in st.session_state:
    st.session_state.main_antennas = ",".join(str(a) for a in DEFAULT_ANTENNAS)
if "main_duration" not in st.session_state:
    st.session_state.main_duration = 0
if "global_tx" not in st.session_state:
    st.session_state.global_tx = 31.5
if "global_rx" not in st.session_state:
    st.session_state.global_rx = -80.0


# ────────────────────────────────────────────────────────────────────
# PAGE 1: Accueil - Gestion des Presets
# ────────────────────────────────────────────────────────────────────

def page_accueil():
    """Page d'accueil: gestion des presets et paramètres du lecteur."""
    # Applique un preset au debut du run pour pouvoir mettre a jour les widgets sans erreur Streamlit.
    if "preset_to_apply" in st.session_state:
        preset_to_apply = st.session_state.pop("preset_to_apply")
        cfg = charger_preset(preset_to_apply)
        if cfg:
            st.session_state.current_preset = preset_to_apply
            st.session_state.main_ip = cfg.get("ip", DEFAULT_IP)
            st.session_state.main_port = cfg.get("port", DEFAULT_PORT)
            st.session_state.main_timeout = cfg.get("timeout", DEFAULT_TIMEOUT)
            st.session_state.main_antennas = ",".join(str(a) for a in cfg.get("antennas", DEFAULT_ANTENNAS))
            st.session_state.main_duration = cfg.get("duration", 0)
            st.session_state.global_tx = cfg.get("global_tx_power", 31.5)
            st.session_state.global_rx = cfg.get("global_rx_sensitivity", -80.0)

            antenna_cfg = cfg.get("antenna_params", {})
            for ant in cfg.get("antennas", []):
                ant_cfg = antenna_cfg.get(str(ant), antenna_cfg.get(ant, {}))
                st.session_state[f"ant_{ant}_tx"] = ant_cfg.get("tx_power", st.session_state.global_tx)
                st.session_state[f"ant_{ant}_rx"] = ant_cfg.get("rx_sensitivity", st.session_state.global_rx)

            st.session_state.load_feedback = f"✅ '{preset_to_apply}' chargé!"

    st.title("📡 Centre de Contrôle RFID")
    st.markdown("---")
    
    # Créer deux colonnes principales
    col_left, col_right = st.columns([1.2, 1], gap="large")
    
    # ══════════════════════════════════════════════════════════════════
    # COLONNE GAUCHE: Paramétrage du lecteur
    # ══════════════════════════════════════════════════════════════════
    with col_left:
        st.subheader("⚙️ Configuration du Lecteur")
        
        # Adresse IP
        ip_addr = st.text_input("Adresse IP :", key="main_ip")
        
        st.markdown("---")
        
        # 1. Paramétrage manuel de l'application
        with st.expander("📋 Paramétrage manuel de l'application", expanded=True):
            col_p1, col_p2 = st.columns(2)
            with col_p1:
                port = st.number_input("Port :", min_value=1, max_value=65535, key="main_port")
                timeout = st.number_input("Timeout (s) :", min_value=0.1, step=0.1, key="main_timeout")
            with col_p2:
                antennas_str = st.text_input(
                    "Antennes (séparées par virgules) :",
                    key="main_antennas",
                    placeholder="1,2,3"
                )
                duration = st.number_input("Durée du scan (s, 0=infini) :", min_value=0, key="main_duration")
        
        st.markdown("---")
        
        # 2. Paramétrage global des antennes
        with st.expander("🌐 Paramétrage global des antennes", expanded=False):
            col_g1, col_g2 = st.columns(2)
            with col_g1:
                global_tx_power = st.slider(
                    "Tx Power Global (dBm) :",
                    min_value=0.0, max_value=31.5, step=0.5,
                    key="global_tx"
                )
            with col_g2:
                global_rx_sensitivity = st.slider(
                    "Rx Sensitivity Global (dBm) :",
                    min_value=-100.0, max_value=0.0, step=0.5,
                    key="global_rx"
                )
        
        st.markdown("---")
        
        # 3. Paramétrage individuel des antennes
        with st.expander("📡 Paramétrage individuel des antennes", expanded=False):
            # Récupérer les antennes valides
            try:
                antennas = parse_antennas(antennas_str)
            except:
                antennas = DEFAULT_ANTENNAS
            
            antenna_params = {}
            for ant in antennas:
                with st.container(border=True):
                    st.markdown(f"#### Antenne {ant}")
                    col_a1, col_a2 = st.columns(2)
                    
                    with col_a1:
                        tx = st.slider(
                            f"Tx Power :",
                            min_value=0.0, max_value=31.5, value=global_tx_power, step=0.5,
                            key=f"ant_{ant}_tx"
                        )
                    with col_a2:
                        rx = st.slider(
                            f"Rx Sensitivity :",
                            min_value=-100.0, max_value=0.0, value=global_rx_sensitivity, step=0.5,
                            key=f"ant_{ant}_rx"
                        )
                    
                    antenna_params[ant] = {"tx_power": tx, "rx_sensitivity": rx}
    
    # ══════════════════════════════════════════════════════════════════
    # COLONNE DROITE: Gestion des presets
    # ══════════════════════════════════════════════════════════════════
    with col_right:
        st.subheader("📂 Gestion des Presets")
        
        # Section 1: Charger un Preset
        with st.container(border=True):
            st.markdown("### 📥 Charger un Preset")
            presets = load_json(PRESETS_FILE)
            
            if not presets:
                st.info("Aucun preset disponible")
            else:
                preset_name = st.selectbox(
                    "Sélectionne un preset :",
                    list(presets.keys()),
                    key="load_select"
                )
                
                if st.button("✅ Charger", type="primary", use_container_width=True, key="btn_load"):
                    st.session_state.preset_to_apply = preset_name
                    st.rerun()

                if "load_feedback" in st.session_state:
                    st.success(st.session_state.pop("load_feedback"))
                
                # Afficher les détails
                if preset_name in presets:
                    cfg = presets[preset_name]
                    with st.expander("📊 Détails", expanded=False):
                        st.write(f"**IP :** {cfg['ip']}")
                        st.write(f"**Port :** {cfg['port']}")
                        st.write(f"**Antennes :** {','.join(str(a) for a in cfg['antennas'])}")
                        st.write(f"**Durée :** {cfg['duration']}s")
        
        st.markdown("")
        
        # Section 2: Sauvegarder un Preset
        with st.container(border=True):
            st.markdown("### 💾 Sauvegarder un Preset")
            preset_name_save = st.text_input("Nom du preset :", placeholder="mon_preset", key="save_name")
            
            if st.button("💾 Sauvegarder", type="primary", use_container_width=True, key="btn_save"):
                if not preset_name_save:
                    st.error("❌ Le nom du preset est obligatoire!")
                else:
                    try:
                        antennas = parse_antennas(antennas_str)
                    except Exception as e:
                        st.error(f"❌ Erreur antennes: {e}")
                        antennas = None
                    
                    if antennas:
                        erreurs = valider_parametres(ip_addr, port, timeout, antennas, duration)
                        if erreurs:
                            for err in erreurs:
                                st.error(f"❌ {err}")
                        else:
                            sauvegarder_preset(
                                preset_name_save, ip_addr, port, timeout,
                                antennas, duration, False,
                                global_tx_power=global_tx_power,
                                global_rx_sensitivity=global_rx_sensitivity,
                                antenna_params=antenna_params
                            )
                            st.success(f"✅ Preset '{preset_name_save}' créé!")
                            st.rerun()
        
        st.markdown("")
        
        # Section 3: Supprimer un Preset
        with st.container(border=True):
            st.markdown("### 🗑️ Supprimer un Preset")
            presets = load_json(PRESETS_FILE)
            
            if not presets:
                st.info("Aucun preset à supprimer")
            else:
                preset_to_del = st.selectbox(
                    "Sélectionne un preset :",
                    list(presets.keys()),
                    key="del_select"
                )
                
                if st.button("🗑️ Supprimer", type="secondary", use_container_width=True, key="btn_del"):
                    if supprimer_preset(preset_to_del):
                        st.success(f"✅ '{preset_to_del}' supprimé!")
                        st.rerun()
                    else:
                        st.error(f"❌ Erreur lors de la suppression")
    
    st.markdown("---")
    
    # Section Lancer le Scan
    st.subheader("▶️ Lancer un Scan RFID")
    
    presets = load_json(PRESETS_FILE)
    col_scan1, col_scan2 = st.columns([2, 1])
    
    with col_scan1:
        if not st.session_state.current_preset:
            st.warning("⚠️ Charge d'abord un preset dans la section 'Charger un Preset'")
        else:
            cfg = charger_preset(st.session_state.current_preset)
            if cfg:
                st.info(f"✅ Preset actif: **{st.session_state.current_preset}**")
                
                # Afficher la configuration
                col_meta1, col_meta2, col_meta3, col_meta4 = st.columns(4)
                with col_meta1:
                    st.metric("IP", cfg["ip"])
                with col_meta2:
                    st.metric("Port", cfg["port"])
                with col_meta3:
                    st.metric("Antennes", ",".join(str(a) for a in cfg["antennas"]))
                with col_meta4:
                    st.metric("Durée", f"{cfg['duration']}s" if cfg['duration'] > 0 else "∞")
    
    with col_scan2:
        if st.session_state.current_preset:
            cfg = charger_preset(st.session_state.current_preset)
            if cfg and st.button("🚀 Démarrer", type="primary", use_container_width=True, key="btn_start_scan"):
                st.session_state.scan_running = True
                with st.spinner("Connexion au lecteur RFID..."):
                    try:
                        result = run_scan_with_logging(cfg, st.session_state.current_preset)
                        if result == 0:
                            append_terminal_log("Scan termine avec succes")
                            st.success("✅ Scan terminé!")
                        else:
                            append_terminal_log(f"Erreur scan code={result}")
                            st.error(f"❌ Erreur (code: {result})")
                    except Exception as e:
                        append_terminal_log(f"Exception Streamlit: {e}")
                        st.error(f"❌ Exception: {e}")
                    finally:
                        st.session_state.scan_running = False


# ────────────────────────────────────────────────────────────────────
# PAGE 2: Mode Classique
# ────────────────────────────────────────────────────────────────────

def page_mode_classique():
    """Page d'affichage du tableau des tags en mode classique."""
    st.title("🚴 Mode Classique")
    st.markdown("---")
    st.caption("Les lectures rapprochées sont regroupées selon une fenêtre de non-relecture configurable.")

    if "classique_debounce_minutes" not in st.session_state:
        st.session_state.classique_debounce_minutes = 10
    if "classique_auto_refresh" not in st.session_state:
        st.session_state.classique_auto_refresh = True
    if "classique_refresh_interval" not in st.session_state:
        st.session_state.classique_refresh_interval = 3

    debounce_minutes = st.slider(
        "Fenêtre de non-relecture (minutes)",
        min_value=1,
        max_value=60,
        value=st.session_state.classique_debounce_minutes,
        step=1,
        key="classique_debounce_minutes",
    )

    tab_live, tab_import = st.tabs(["📡 Live", "📁 Fichier JSON"])

    with tab_live:
        st.subheader("Vue live")
        st.caption("Le tableau lit le fichier de tags et se met à jour pendant un scan lancé depuis cette page.")

        live_col1, live_col2, live_col3 = st.columns([1.5, 1, 1])
        with live_col1:
            auto_refresh = st.checkbox(
                "Actualisation automatique",
                value=st.session_state.classique_auto_refresh,
                key="classique_auto_refresh",
            )
        with live_col2:
            refresh_interval = st.slider(
                "Intervalle (s)",
                min_value=1,
                max_value=10,
                value=st.session_state.classique_refresh_interval,
                step=1,
                key="classique_refresh_interval",
            )
        with live_col3:
            if st.button("🔄 Rafraîchir maintenant", use_container_width=True):
                st.rerun()

        scan_running = _is_classic_scan_running()
        if scan_running:
            st.info("Scan en cours: les données live continuent d'être consolidées pendant l'écriture du fichier JSON.")
        else:
            st.success("Aucun scan live en cours.")

        if st.session_state.current_preset:
            cfg = charger_preset(st.session_state.current_preset)
            if cfg:
                cfg_col1, cfg_col2, cfg_col3, cfg_col4 = st.columns(4)
                with cfg_col1:
                    st.metric("IP", cfg["ip"])
                with cfg_col2:
                    st.metric("Port", cfg["port"])
                with cfg_col3:
                    st.metric("Antennes", ",".join(str(a) for a in cfg["antennas"]))
                with cfg_col4:
                    st.metric("Durée", f"{cfg['duration']}s" if cfg["duration"] > 0 else "∞")

                scan_col1, scan_col2 = st.columns([1, 1])
                with scan_col1:
                    if st.button(
                        "🚀 Démarrer le scan live",
                        type="primary",
                        use_container_width=True,
                        disabled=scan_running,
                    ):
                        if _start_classic_scan(cfg, st.session_state.current_preset):
                            st.rerun()
                        else:
                            st.warning("Un scan est déjà en cours pour cette session.")
                with scan_col2:
                    st.button("⏳ Scan en cours" if scan_running else "Scan arrêté", disabled=True, use_container_width=True)
        else:
            st.warning("Charge d'abord un preset dans l'onglet Accueil pour lancer un scan live.")

        try:
            raw_tags = _load_tags_payload()
            df_live = _render_classic_tag_table(raw_tags, debounce_minutes)
        except ValueError as exc:
            st.error(str(exc))
            raw_tags = []
            df_live = pd.DataFrame()

        if st.session_state.classique_auto_refresh and scan_running:
            time.sleep(refresh_interval)
            st.rerun()

        live_summary_col1, live_summary_col2, live_summary_col3 = st.columns(3)
        with live_summary_col1:
            st.metric("Lectures brutes", len(raw_tags))
        with live_summary_col2:
            st.metric("Tags agrégés", len(df_live))
        with live_summary_col3:
            st.metric("Fenêtre", f"{debounce_minutes} min")

        if df_live.empty:
            st.info("Aucune lecture disponible pour le moment.")
        else:
            st.dataframe(df_live, use_container_width=True, hide_index=True)

    with tab_import:
        st.subheader("Import JSON")
        st.caption("Charge un fichier JSON brut contenant la clé 'tags' ou une liste de lectures de tags.")

        uploaded_file = st.file_uploader("Choisir un fichier JSON", type=["json"], key="classique_json_upload")

        if uploaded_file is None:
            st.info(f"Aucun fichier importé. Le tableau affichera le contenu local de {TAGS_FILE.name} si disponible.")

        try:
            raw_tags_import = _load_tags_payload(uploaded_file)
            df_import = _render_classic_tag_table(raw_tags_import, debounce_minutes)
        except ValueError as exc:
            st.error(str(exc))
            raw_tags_import = []
            df_import = pd.DataFrame()

        import_summary_col1, import_summary_col2, import_summary_col3 = st.columns(3)
        with import_summary_col1:
            st.metric("Lectures brutes", len(raw_tags_import))
        with import_summary_col2:
            st.metric("Tags agrégés", len(df_import))
        with import_summary_col3:
            st.metric("Fenêtre", f"{debounce_minutes} min")

        if df_import.empty:
            st.info("Aucune lecture à afficher depuis cette source JSON.")
        else:
            st.dataframe(df_import, use_container_width=True, hide_index=True)
 



# ────────────────────────────────────────────────────────────────────
# PAGE 3: Configuration
# ────────────────────────────────────────────────────────────────────

def page_configuration():
    """Page de configuration de l'application."""
    st.title("⚙️ Configuration")
    st.markdown("---")
    
    st.subheader("💾 Stockage des Données")
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.metric("Fichier Presets", PRESETS_FILE.name)
        st.metric("Fichier Tags", TAGS_FILE.name)
        st.metric("Fichier Config", CONFIG_FILE.name)
    
    with col2:
        # Vérifier l'existence des fichiers
        presets_exists = "✅ Existe" if PRESETS_FILE.exists() else "❌ N'existe pas"
        tags_exists = "✅ Existe" if TAGS_FILE.exists() else "❌ N'existe pas"
        config_exists = "✅ Existe" if CONFIG_FILE.exists() else "❌ N'existe pas"
        
        st.info(f"""
        **État des fichiers:**
        - Presets: {presets_exists}
        - Tags: {tags_exists}
        - Config: {config_exists}
        """)
    
    st.markdown("---")
    st.subheader("🔧 Gestion des Données")
    
    col1, col2 = st.columns(2)
    
    with col1:
        if st.button("🗑️ Effacer TOUS les presets", use_container_width=True):
            save_json(PRESETS_FILE, {})
            st.success("✅ Tous les presets ont été supprimés!")
            st.rerun()
    
    with col2:
        if st.button("🗑️ Effacer TOUS les tags", use_container_width=True):
            save_json(TAGS_FILE, {"tags": []})
            st.success("✅ Tous les tags ont été supprimés!")
            st.rerun()
    
    st.markdown("---")
    st.subheader("ℹ️ À Propos")
    st.info("""
    **Application RFID Reader Manager**
    - Interface Streamlit pour Impinj R220
    - Gestion des presets et visualisation des tags
    - Version: 1.0
    """)
    
    # Preset actuellement chargé
    if st.session_state.current_preset:
        st.success(f"✅ Preset actuellement actif: **{st.session_state.current_preset}**")


# ────────────────────────────────────────────────────────────────────
# PAGE 4 : Mode Brut
# ────────────────────────────────────────────────────────────────────

def page_mode_brut():
    """Page pour voir un terminal brut des logs et interactions."""
    st.title("💻 Mode Brut")
    st.markdown("---")
    st.info("Affiche la sortie brute capturée pendant les scans (style terminal).")

    st.subheader("▶️ Lancer un Scan RFID (Mode Brut)")
    col_scan1, col_scan2 = st.columns([2, 1])

    with col_scan1:
        if not st.session_state.current_preset:
            st.warning("⚠️ Charge d'abord un preset dans la section 'Charger un Preset'")
        else:
            cfg = charger_preset(st.session_state.current_preset)
            if cfg:
                st.info(f"✅ Preset actif: **{st.session_state.current_preset}**")
                col_meta1, col_meta2, col_meta3, col_meta4 = st.columns(4)
                with col_meta1:
                    st.metric("IP", cfg["ip"])
                with col_meta2:
                    st.metric("Port", cfg["port"])
                with col_meta3:
                    st.metric("Antennes", ",".join(str(a) for a in cfg["antennas"]))
                with col_meta4:
                    st.metric("Durée", f"{cfg['duration']}s" if cfg['duration'] > 0 else "∞")

    with col_scan2:
        if st.session_state.current_preset:
            cfg = charger_preset(st.session_state.current_preset)
            if cfg and st.button("🚀 Démarrer", type="primary", use_container_width=True, key="btn_start_scan_brut"):
                st.session_state.scan_running = True
                with st.spinner("Connexion au lecteur RFID..."):
                    try:
                        result = run_scan_with_logging(cfg, st.session_state.current_preset)
                        if result == 0:
                            append_terminal_log("Scan termine avec succes")
                            st.success("✅ Scan terminé!")
                        else:
                            append_terminal_log(f"Erreur scan code={result}")
                            st.error(f"❌ Erreur (code: {result})")
                    except Exception as e:
                        append_terminal_log(f"Exception Streamlit: {e}")
                        st.error(f"❌ Exception: {e}")
                    finally:
                        st.session_state.scan_running = False

    st.markdown("---")

    col1, col2, col3 = st.columns([1, 1, 2])
    with col1:
        max_lines = st.selectbox("Dernieres lignes", [100, 200, 400, 800], index=2)
    with col2:
        source = st.selectbox("Source", ["live", "archive"], index=0)
    with col3:
        if st.button("🧹 Effacer le terminal live", use_container_width=True):
            LIVE_LOG_FILE.write_text("", encoding="utf-8")
            st.success("Terminal live efface (archive conservee)")
            st.rerun()

    if st.button("🔄 Rafraichir", use_container_width=True):
        st.rerun()

    logs = read_terminal_log(max_lines=max_lines, source=source)
    if not logs:
        logs = "Aucune sortie disponible. Lance un scan pour remplir le terminal."

    active_log_name = LIVE_LOG_FILE.name if source == "live" else ARCHIVE_LOG_FILE.name

    terminal_html = f"""
    <div style=\"background:#0a0f14; color:#9ef01a; border-radius:10px; padding:14px; border:1px solid #263341;\">
        <div style=\"color:#8aa0b2; margin-bottom:8px; font-family:Consolas, 'Courier New', monospace;\">
            RFID terminal - {active_log_name}
        </div>
        <div style="margin:0; font-family:Consolas, 'Courier New', monospace; font-size:13px; line-height:1.6;">{escape(logs).replace(chr(10), '<br>')}</div>
    </div>
    """
    st.markdown(terminal_html, unsafe_allow_html=True)
    
# ────────────────────────────────────────────────────────────────────
# PAGE 5 : Mode Graphique
# ────────────────────────────────────────────────────────────────

def page_mode_graphique():
    """Page pour visualiser les tags en temps réel avec graphiques."""
    st.title("📊 Mode Graphique")
    st.markdown("---")
    st.info("Affiche les tags detectes avec statistiques de flux, graphiques et tri des resultats traites.")

    st.subheader("Source JSON")
    json_files = sorted([p.name for p in SCRIPT_DIR.glob("*.json")])

    if not json_files:
        st.warning("Aucun fichier JSON trouve dans le dossier du projet.")
        return

    default_name = TAGS_FILE.name if TAGS_FILE.name in json_files else json_files[0]
    selected_json = st.selectbox(
        "Fichier de donnees pour les graphiques",
        options=json_files,
        index=json_files.index(default_name),
        help="Selectionne un JSON deja present dans le projet."
    )

    selected_path = SCRIPT_DIR / selected_json
    try:
        tags_data = load_json(selected_path)
    except Exception as exc:
        st.error(f"Impossible de lire {selected_json}: {exc}")
        return

    # Supporte soit {"tags": [...]}, soit une liste brute de tags.
    if isinstance(tags_data, dict):
        tags_list = tags_data.get("tags", [])
    elif isinstance(tags_data, list):
        tags_list = tags_data
    else:
        tags_list = []

    st.caption(f"Source active: {selected_json}")

    if not tags_list:
        st.warning("Aucune donnee exploitable de tags dans ce fichier JSON.")
        return

    whitelist_epcs = load_whitelist()
    if not whitelist_epcs:
        st.warning("Whitelist vide: aucun tag ne sera retenu. Ajoute des EPC dans la page Whitelist.")
        return

    df = pd.DataFrame(tags_list)
    if df.empty:
        st.warning("Donnees tags invalides ou vides.")
        return

    # Harmonise le timestamp selon les formats utilises dans les fichiers.
    ts_col = "client_timestamp" if "client_timestamp" in df.columns else "timestamp"
    if ts_col not in df.columns:
        st.error("Aucun champ timestamp/client_timestamp detecte dans les donnees tags.")
        return

    df["timestamp_dt"] = pd.to_datetime(df[ts_col], errors="coerce")
    df["epc"] = df.get("epc", "N/A").astype(str)
    df["epc_norm"] = df["epc"].map(normalize_epc)
    df["antenna"] = pd.to_numeric(df.get("antenna", 0), errors="coerce").fillna(0).astype(int)
    df["rssi"] = pd.to_numeric(df.get("rssi", 0), errors="coerce")
    df["seen_count"] = pd.to_numeric(df.get("seen_count", 1), errors="coerce").fillna(1)
    df["passages"] = df["seen_count"].clip(lower=1)

    total_before_whitelist = len(df)
    df = df[df["epc_norm"].isin(set(whitelist_epcs))].copy()
    retained_count = len(df)

    st.caption(
        f"Filtre whitelist applique: {retained_count}/{total_before_whitelist} lignes retenues "
        f"({len(whitelist_epcs)} EPC autorises)."
    )

    if df.empty:
        st.warning("Aucun tag du fichier selectionne ne correspond a la whitelist.")
        return

    # Cooldown anti-surlecture: ignore les lectures trop proches pour un meme EPC/antenne.
    st.subheader("Parametres RF et Cooldown")
    p1, p2, p3 = st.columns(3)
    with p1:
        cooldown_min = st.slider(
            "Cooldown EPC+antenne (minutes)",
            min_value=0,
            max_value=60,
            value=10,
            step=1,
            help="Ignore une lecture si le meme EPC sur la meme antenne est deja passe il y a moins de X minutes."
        )
    with p2:
        ref_rssi_1m = st.slider(
            "RSSI reference a 1m (dBm)",
            min_value=-80,
            max_value=-30,
            value=-52,
            step=1,
            help="Parametre du modele de distance (approximation)."
        )
    with p3:
        path_loss_n = st.slider(
            "Facteur environnement n",
            min_value=1.5,
            max_value=4.0,
            value=2.2,
            step=0.1,
            help="Plus n est grand, plus la distance estimee augmente vite."
        )

    df = df.sort_values("timestamp_dt").copy()
    cooldown_s = cooldown_min * 60
    if cooldown_s > 0:
        prev_ts = df.groupby(["epc_norm", "antenna"])["timestamp_dt"].shift(1)
        delta_s = (df["timestamp_dt"] - prev_ts).dt.total_seconds()
        keep_mask = prev_ts.isna() | delta_s.isna() | (delta_s >= cooldown_s)
        kept = int(keep_mask.sum())
        df = df[keep_mask].copy()
        st.caption(f"Cooldown applique: {kept}/{retained_count} lectures conservees.")

    if df.empty:
        st.warning("Aucune lecture restante apres cooldown.")
        return

    # Estimation de distance RFID basee sur le RSSI (modele log-distance simplifie).
    df["distance_est_m"] = 10 ** ((ref_rssi_1m - df["rssi"]) / (10 * path_loss_n))
    df["distance_est_m"] = df["distance_est_m"].clip(lower=0.05, upper=1000)

    # Filtres et tri
    st.subheader("Filtres et Tri")
    col_f1, col_f2, col_f3, col_f4 = st.columns(4)

    with col_f1:
        antenna_options = sorted(df["antenna"].dropna().unique().tolist())
        antenna_filter = st.multiselect("Filtrer par antenne", antenna_options)

    with col_f2:
        epc_options = sorted(df["epc"].dropna().unique().tolist())
        epc_filter = st.multiselect("Filtrer par EPC", epc_options)

    with col_f3:
        sort_by = st.selectbox(
            "Trier resultats traites par",
            ["Passages", "RSSI moyen", "Ecart moyen (s)", "Derniere detection", "EPC"]
        )

    with col_f4:
        sort_order = st.selectbox("Ordre", ["Decroissant", "Croissant"])

    filtered = df.copy()
    if antenna_filter:
        filtered = filtered[filtered["antenna"].isin(antenna_filter)]
    if epc_filter:
        filtered = filtered[filtered["epc"].isin(epc_filter)]

    if filtered.empty:
        st.warning("Aucune donnee apres filtrage.")
        return

    # Statistiques de flux globales
    valid_ts = filtered["timestamp_dt"].dropna().sort_values()
    total_passages = int(filtered["passages"].sum())
    unique_tags = int(filtered["epc"].nunique())

    avg_rate_ppm = 0.0
    avg_gap_s = 0.0
    if len(valid_ts) >= 2:
        duration_s = max((valid_ts.iloc[-1] - valid_ts.iloc[0]).total_seconds(), 1.0)
        avg_rate_ppm = (total_passages / duration_s) * 60.0
        global_gaps = valid_ts.diff().dropna().dt.total_seconds()
        if not global_gaps.empty:
            avg_gap_s = float(global_gaps.mean())

    st.subheader("Statistiques du Flux")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        st.metric("Passages (total)", total_passages)
    with c2:
        st.metric("Tags uniques", unique_tags)
    with c3:
        st.metric("Vitesse moyenne", f"{avg_rate_ppm:.2f} passages/min")
    with c4:
        st.metric("Ecart moyen", f"{avg_gap_s:.2f} s")

    # Resultats traites par EPC
    work = filtered.sort_values("timestamp_dt").copy()
    work["gap_s"] = work.groupby("epc")["timestamp_dt"].diff().dt.total_seconds()

    processed = (
        work.groupby("epc", as_index=False)
        .agg(
            passages=("passages", "sum"),
            rssi_moyen=("rssi", "mean"),
            ecart_moyen_s=("gap_s", "mean"),
            premiere_detection=("timestamp_dt", "min"),
            derniere_detection=("timestamp_dt", "max"),
            antennes_actives=("antenna", lambda s: ",".join(str(x) for x in sorted(set(s))))
        )
    )

    processed["duree_presence_s"] = (
        processed["derniere_detection"] - processed["premiere_detection"]
    ).dt.total_seconds().fillna(0)

    processed["vitesse_passages_min"] = processed.apply(
        lambda row: (row["passages"] / max(row["duree_presence_s"], 1.0)) * 60.0,
        axis=1,
    )

    sort_map = {
        "Passages": "passages",
        "RSSI moyen": "rssi_moyen",
        "Ecart moyen (s)": "ecart_moyen_s",
        "Derniere detection": "derniere_detection",
        "EPC": "epc",
    }
    processed = processed.sort_values(
        by=sort_map[sort_by],
        ascending=(sort_order == "Croissant"),
        na_position="last",
    )

    # Mise en forme pour affichage tableau
    processed_display = processed.copy()
    processed_display["rssi_moyen"] = processed_display["rssi_moyen"].round(2)
    processed_display["ecart_moyen_s"] = processed_display["ecart_moyen_s"].round(3)
    processed_display["duree_presence_s"] = processed_display["duree_presence_s"].round(3)
    processed_display["vitesse_passages_min"] = processed_display["vitesse_passages_min"].round(2)
    processed_display["premiere_detection"] = processed_display["premiere_detection"].astype(str)
    processed_display["derniere_detection"] = processed_display["derniere_detection"].astype(str)

    st.subheader("Resultats Traites")
    st.dataframe(processed_display, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.subheader("Graphiques")

    g1, g2 = st.columns(2)

    with g1:
        st.markdown("**Histogramme du nombre de passages par EPC**")
        hist_epc = processed[["epc", "passages"]].set_index("epc")
        st.bar_chart(hist_epc)

    with g2:
        st.markdown("**Flux de passages dans le temps (par minute)**")
        timeline = work.dropna(subset=["timestamp_dt"]).set_index("timestamp_dt")
        if timeline.empty:
            st.info("Pas assez de timestamps valides pour le flux temporel.")
        else:
            flow = timeline["passages"].resample("1min").sum().rename("passages")
            st.line_chart(flow)

    st.markdown("**Histogramme des ecarts entre passages (global)**")
    if len(valid_ts) >= 2:
        gaps = valid_ts.diff().dropna().dt.total_seconds()
        bins = [0, 0.2, 0.5, 1, 2, 5, 10, 30, 60, float("inf")]
        labels = ["0-0.2s", "0.2-0.5s", "0.5-1s", "1-2s", "2-5s", "5-10s", "10-30s", "30-60s", ">60s"]
        gap_hist = pd.cut(gaps, bins=bins, labels=labels, include_lowest=True).value_counts().sort_index()
        st.bar_chart(gap_hist)
    else:
        st.info("Pas assez de points temporels pour calculer les ecarts entre passages.")

    st.markdown("**Distance estimee par antenne (a partir du RSSI)**")
    distance_df = filtered.dropna(subset=["distance_est_m"]).copy()
    if distance_df.empty:
        st.info("Pas assez de donnees RSSI valides pour estimer la distance.")
    else:
        dist_by_ant = distance_df.groupby("antenna", as_index=False).agg(
            distance_moyenne_m=("distance_est_m", "mean"),
            distance_mediane_m=("distance_est_m", "median"),
        )
        st.bar_chart(dist_by_ant.set_index("antenna")[["distance_moyenne_m", "distance_mediane_m"]])


# ────────────────────────────────────────────────────────────────────
# PAGE 6 : Gestion Whitelist
# ────────────────────────────────────────────────────────────────

def page_whitelist():
    """Page de gestion des EPC autorises (whitelist)."""
    st.title("✅ Gestion Whitelist")
    st.markdown("---")
    st.info("Les graphiques ne retiennent que les tags dont l'EPC est present dans whitelist.json.")

    whitelist_epcs = load_whitelist()
    st.metric("EPC autorises", len(whitelist_epcs))

    col_add, col_remove = st.columns(2)

    with col_add:
        st.subheader("Ajouter des EPC")
        epc_input = st.text_area(
            "Un EPC par ligne (ou separes par virgule)",
            placeholder="b'e2004703914064269d7b0114'"
        )
        if st.button("➕ Ajouter", use_container_width=True):
            parts = []
            for line in epc_input.splitlines():
                parts.extend(line.split(","))

            to_add = [normalize_epc(x) for x in parts if normalize_epc(x)]
            if not to_add:
                st.warning("Aucun EPC valide a ajouter.")
            else:
                updated = whitelist_epcs.copy()
                for epc in to_add:
                    if epc not in updated:
                        updated.append(epc)
                save_whitelist(updated)
                st.success(f"{len(updated) - len(whitelist_epcs)} EPC ajoute(s).")
                st.rerun()

        uploaded = st.file_uploader("Importer whitelist JSON", type=["json"])
        if uploaded is not None:
            try:
                imported = json.load(uploaded)
                if isinstance(imported, dict):
                    imported_epcs = imported.get("epcs", imported.get("whitelist", []))
                elif isinstance(imported, list):
                    imported_epcs = imported
                else:
                    imported_epcs = []

                merged = whitelist_epcs.copy()
                for epc in imported_epcs:
                    norm = normalize_epc(epc)
                    if norm and norm not in merged:
                        merged.append(norm)
                save_whitelist(merged)
                st.success("Whitelist importee.")
                st.rerun()
            except Exception as exc:
                st.error(f"Import impossible: {exc}")

    with col_remove:
        st.subheader("Supprimer des EPC")
        if whitelist_epcs:
            to_remove = st.multiselect("Selection EPC a retirer", whitelist_epcs)
            if st.button("🗑️ Retirer la selection", use_container_width=True):
                updated = [epc for epc in whitelist_epcs if epc not in to_remove]
                save_whitelist(updated)
                st.success(f"{len(whitelist_epcs) - len(updated)} EPC supprime(s).")
                st.rerun()
        else:
            st.caption("Whitelist vide.")

        if st.button("❌ Vider la whitelist", use_container_width=True):
            save_whitelist([])
            st.success("Whitelist videe.")
            st.rerun()

    st.markdown("---")
    st.subheader("Contenu actuel")
    if whitelist_epcs:
        st.dataframe(pd.DataFrame({"epc": whitelist_epcs}), use_container_width=True, hide_index=True)
    else:
        st.info("Aucun EPC dans la whitelist.")

    st.download_button(
        "📥 Exporter whitelist.json",
        data=json.dumps({"epcs": whitelist_epcs}, indent=2, ensure_ascii=False),
        file_name="whitelist.json",
        mime="application/json",
    )





# ────────────────────────────────────────────────────────────────────
# NAVIGATION - Fonction principale
# ────────────────────────────────────────────────────────────────────

def main():
    """Fonction principale avec navigation."""
    st.sidebar.title("🎯 Menu Principal")
    
    # Afficher le preset actuel dans la sidebar
    if st.session_state.current_preset:
        st.sidebar.success(f"📌 Preset: **{st.session_state.current_preset}**")
    else:
        st.sidebar.warning("⚠️ Aucun preset chargé")
    
    st.sidebar.markdown("---")
    
    # Sélecteur de page
    page = st.sidebar.radio(
        "Sélectionne une page:",
        ["🏠 Accueil", "🚴 Mode Classique", "📊 Mode Graphique", "✅ Whitelist", "⚙️ Configuration", "💻 Mode Brut"],
        index=0
    )
    
    st.sidebar.markdown("---")
    
    # Afficher la page sélectionnée
    if page == "🏠 Accueil":
        page_accueil()
    elif page == "🚴 Mode Classique":
        page_mode_classique()
    elif page == "📊 Mode Graphique":
        page_mode_graphique()
    elif page == "✅ Whitelist":
        page_whitelist()
    elif page == "⚙️ Configuration":
        page_configuration()
    elif page == "💻 Mode Brut":
        page_mode_brut()

# ────────────────────────────────────────────────────────────────────
# Point d'entrée
# ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
