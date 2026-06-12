
import re
import streamlit as st
import json
from pathlib import Path
from datetime import datetime
import time
from html import escape
import pandas as pd
import threading
from contextlib import redirect_stdout, redirect_stderr
import connexion_decodeur
"""
Fonctions importées de connexion_decodeur :

load_json : Charge un fichier JSON. Retourne {} si le fichier n'existe pas.
save_json : Écrit un dictionnaire dans un fichier JSON.
run_inventory : lance la connexion au lecteur et le processus d'inventaire
parse_antennas : Parse une liste d'antennes au format '1,2,3'. et retourne une liste d'entiers représentant les numéros d'antennes
get_storage_preferences : Retourne les préférences concernant le nom des fichiers de stockage de configuration selon un preset donné
update_preset_storage_preferences : Met à jour les préférences de nom de fichiers de stockage de configuration d'un preset existant
get_last_used_preset : 	Retourne le nom du dernier preset utilisé, ou None si aucun ou invalide
get_presets : Retourne le JSON complet de presets stockés dans le fichier PRESETS_FILE
valider_parametres : Vérifie tous les paramètres du lecteur. Retourne la liste des erreurs.
sauvegarder_preset : Sauvegarde un preset dans presets.json.
supprimer_preset : Supprime un preset. Retourne True si succès.
charger_preset : Charge un preset. Retourne None si introuvable.
set_last_used_preset : Enregistre le nom du dernier preset utilisé (ou None pour réinitialiser)
"""
# Import des constantes de configuration par défaut pour la connexion au lecteur RFID, 
# ce qui permet d'avoir des valeurs par défaut cohérentes dans l'interface Streamlit et dans les presets
from connexion_decodeur import (
    DEFAULT_IP, DEFAULT_PORT, DEFAULT_TIMEOUT, DEFAULT_ANTENNAS,
)

# ────────────────────────────────────────────────────────────────────
# Configuration Streamlit
# ────────────────────────────────────────────────────────────────────

# Configure le titre, l'icône et la mise en page de l'application Streamlit.
# Le layout "wide" permet d'utiliser toute la largeur de la page, et initial_sidebar_state="expanded" 
# ouvre la sidebar par défaut pour un accès rapide aux presets.
st.set_page_config(
    page_title="RFID Reader Manager",
    page_icon="📡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# ────────────────────────────────────────────────────────────────────
# Chemins
# ────────────────────────────────────────────────────────────────────

# SCRIPT_DIR est le répertoire où se trouve ce script streamlit_app.py. 
# Tous les fichiers de configuration, presets, tags, et logs sont situés dans ce même répertoire pour 
# simplifier la gestion des chemins et éviter les problèmes de permissions d'écriture dans d'autres dossiers.
SCRIPT_DIR = Path(__file__).parent

# PRESETS_FILE est la liste de tous les presets disponibles
PRESETS_FILE = SCRIPT_DIR / "presets.json"

# TAGS_FILE est le fichier où sont stockées les lectures de tags. Il peut être remplacé par un fichier uploadé via l'interface
TAGS_FILE = SCRIPT_DIR / "tags.json"

# BASE_CONFIG_FILE est le fichier de configuration par défaut utilisé pour les paramètres du lecteur. 
BASE_CONFIG_FILE = SCRIPT_DIR / "config.json"

# CONFIG_FILE est le fichier de configuration actif
CONFIG_FILE = BASE_CONFIG_FILE

# WHITELIST_FILE est le fichier où sont stockés les EPC autorisés et leurs correspondances avec les noms de coureurs.
WHITELIST_FILE = SCRIPT_DIR / "whitelist.json"

# LIVE_LOG_FILE est le fichier où sont écrites les sorties du terminal en temps réel pendant un scan actif.
# ARCHIVE_LOG_FILE est le fichier où sont archivées toutes les sorties du terminal, y compris les scans passés.
LIVE_LOG_FILE = SCRIPT_DIR / "rfid_terminal_live.log"
ARCHIVE_LOG_FILE = SCRIPT_DIR / "rfid_terminal_archive.log"

# _SCAN_STATE est un dictionnaire partagé pour suivre l'état du scan ("idle", "starting", "running", "stopping") 
# de manière thread-safe entre le thread de scan et l'interface Streamlit.
_SCAN_STATE = {"status": "idle"}
_SCAN_LOCK = threading.Lock()

# ────────────────────────────────────────────────────────────────────
# fonctions interne à l'interface Streamlit
# ────────────────────────────────────────────────────────────────────

def normalize_epc(value: str) -> str:
    """Normalise un EPC pour comparaison faire une comparaison insensible à la casse."""
    return str(value).strip().lower()


def load_whitelist_entries() -> list[dict[str, str]]:
    """Charge la whitelist et les correspondances EPC -> nom."""
    # Vérifier si le fichier de whitelist existe et n'est pas vide
    if not WHITELIST_FILE.exists() or WHITELIST_FILE.stat().st_size == 0:
        return []

    # Charger le contenu JSON du fichier de whitelist
    try:
        raw = connexion_decodeur.load_json(WHITELIST_FILE)
    except Exception:
        # En cas d'erreur de lecture/parsing, retourner une liste vide
        return []

    # Initialiser les structures de données pour stocker les entrées et déduire les doublons
    entries: list[dict[str, str]] = []
    seen: set[str] = set()

    # Déterminer le format du fichier et extraire les candidats EPC et le mapping correspondant
    # Le fichier de whitelist peut être dans différents formats:
    # 1) {"entries": [{"epc": "...", "name": "..." 
    if isinstance(raw, dict):
        # Format dictionnaire: extraire la clé "entries", sinon essayer "epcs" ou "whitelist"
        candidates = raw.get("entries")
        if candidates is None:
            candidates = raw.get("epcs", raw.get("whitelist", []))
        # Extraire le mapping EPC -> nom s'il existe
        mapping = raw.get("mapping", {})
    elif isinstance(raw, list):
        # Format liste brute d'EPC: ["epc1", "epc2", ...]
        candidates = raw
        mapping = {}
    else:
        # Format inconnu: initialiser avec des valeurs par défaut
        candidates = []
        mapping = {}
    
    # Traiter la liste des candidats EPC (format 2)
    # 2) Une liste brute d'EPC: ["epc1", "epc2", ...] ou [{"epc": "...", "name": "..."}, ...]
    if isinstance(candidates, list):
        for item in candidates:
            # Vérifier si l'élément est un dictionnaire ou une chaîne
            if isinstance(item, dict):
                # Format dictionnaire: extraire les clés "epc" et "name"
                epc = normalize_epc(item.get("epc", ""))
                name = str(item.get("name", "")).strip()
            else:
                # Format chaîne brute: normaliser directement l'EPC
                epc = normalize_epc(item)
                name = ""
            # Ajouter l'entrée si l'EPC est valide et non dupliqué
            if epc and epc not in seen:
                entries.append({"epc": epc, "name": name})
                seen.add(epc)
    
    # Traiter le mapping EPC -> nom (format 3)
    # 3) Un mapping EPC -> nom: {"mapping": {"epc1": "name1", "epc2": "name2", ...}}
    if isinstance(mapping, dict):
        for raw_epc, raw_name in mapping.items():
            # Normaliser l'EPC et le nom
            epc = normalize_epc(raw_epc)
            name = str(raw_name).strip()
            # Ignorer les EPC invalides
            if not epc:
                continue
            # Chercher si l'EPC existe déjà dans les entrées
            for entry in entries:
                if entry["epc"] == epc:
                    # Mettre à jour le nom existant si un nom valide est fourni
                    if name:
                        entry["name"] = name
                    break
            else:
                # L'EPC n'existe pas encore: ajouter une nouvelle entrée
                entries.append({"epc": epc, "name": name})

    # Retourner la liste complète des entrées normalisées et dédupliquées
    return entries


def load_whitelist() -> list[str]:
    """Retourne uniquement la liste des EPC autorises."""
    return [entry["epc"] for entry in load_whitelist_entries()]


def load_epc_correspondence() -> dict[str, str]:
    """Retourne la correspondance EPC -> nom."""
    return {entry["epc"]: entry["name"] for entry in load_whitelist_entries() if entry.get("name")}


def save_whitelist_entries(entries: list[dict[str, str]]) -> None:
    """Sauvegarde les EPC autorises et leur correspondance."""
    # Initialiser une liste pour stocker les entrées normalisées et dédupliquées
    normalized_entries: list[dict[str, str]] = []
    # Utiliser un ensemble pour tracker les EPC déjà traités et éviter les doublons
    seen: set[str] = set()
    
    # Parcourir chaque entrée fournie en paramètre
    for item in entries:
        # Normaliser le code EPC (conversion en minuscules, suppression des espaces, etc.)
        epc = normalize_epc(item.get("epc", ""))
        # Convertir le nom en chaîne et supprimer les espaces inutiles de début/fin
        name = str(item.get("name", "")).strip()
        
        # Vérifier que l'EPC est valide et pas déjà rencontré (déduplication)
        if epc and epc not in seen:
            # Ajouter l'entrée normalisée à la liste
            normalized_entries.append({"epc": epc, "name": name})
            # Marquer cet EPC comme traité
            seen.add(epc)

    # Sauvegarder dans le fichier JSON les données structurées de trois façons:
    connexion_decodeur.save_json(
        WHITELIST_FILE,
        {
            # Format 1: Liste complète avec EPC et nom associé
            "entries": normalized_entries,
            # Format 2: Liste simple des EPC uniquement pour filtrage rapide
            "epcs": [entry["epc"] for entry in normalized_entries],
            # Format 3: Dictionnaire de correspondance EPC -> nom pour les entrées nommées
            "mapping": {entry["epc"]: entry["name"] for entry in normalized_entries if entry["name"]},
        },
    )


def save_whitelist(epcs: list[str]) -> None:
    """Compatibilite: sauvegarde une whitelist sans noms."""
    save_whitelist_entries([{"epc": epc, "name": ""} for epc in epcs])


def get_epc_name(epc: str, correspondence: dict[str, str] | None = None) -> str:
    """Retourne le nom associe a un EPC si disponible."""
    mapping = correspondence or load_epc_correspondence()
    return mapping.get(normalize_epc(epc), "")


def format_epc_label(epc: str, correspondence: dict[str, str] | None = None) -> str:
    """Construit un label lisible Nom / EPC pour l'affichage sur l'interface"""
    name = get_epc_name(epc, correspondence)
    if name:
        return f"{name} | {epc}"
    return epc


def filter_tags_with_whitelist(raw_tags: list[dict], whitelist_epcs: list[str]) -> list[dict]:
    """Filtre une liste de lectures selon la whitelist EPC."""
    allowed = set(whitelist_epcs)
    filtered_tags = []
    # Pour chaque tag lu, on extrait son EPC en essayant différentes clés possibles (epc, EPC, tag_id) 
    # et on vérifie s'il est dans la whitelist.
    for tag in raw_tags:
        epc = normalize_epc(tag.get("epc") or tag.get("EPC") or tag.get("tag_id") or "")
        if epc and epc in allowed:
            filtered_tags.append(tag)
    return filtered_tags


def append_terminal_log(message: str) -> None:
    """Ajoute des lignes au log live et a l'archive avec le timestamp local de l'appareil."""
    LIVE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = message.splitlines() or [""]
    # On ajoute un timestamp local précis à chaque ligne du message 
    # et on écrit à la fois dans le log live (pour affichage en temps réel) 
    # et dans le log d'archive (pour conserver l'historique complet).
    with open(LIVE_LOG_FILE, "a", encoding="utf-8") as live_f, open(ARCHIVE_LOG_FILE, "a", encoding="utf-8") as archive_f:
        for line in lines:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
            formatted = f"[{timestamp}] {line}\n"
            live_f.write(formatted)
            archive_f.write(formatted)


"""
Une classe encapsulant un flux d'écriture qui redirige les sorties vers le log du terminal dans Streamlit.
Cette classe bufferise les écritures jusqu'à ce qu'un saut de ligne soit rencontré pour éviter d'écrire des 
lignes incomplètes dans le log.
"""
class TerminalLogStream:
    """Classe qui va permettre de rediriger stdout/stderr vers le log du terminal dans Streamlit."""

    def __init__(self) -> None:
        self._buffer = ""

    def write(self, text: str) -> int:
        """Ecrit du texte dans le log du terminal. Le texte est bufferisé jusqu'à ce qu'un saut de ligne soit rencontré."""
        if not text:
            return 0

        self._buffer += text
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            append_terminal_log(line)

        return len(text)

    def flush(self) -> None:
        """Vide le buffer restant dans le log du terminal."""
        if self._buffer:
            append_terminal_log(self._buffer)
            self._buffer = ""


def read_terminal_log(max_lines: int = 400, source: str = "live", newest_first: bool = False) -> str:
    """Lit les dernieres lignes du log terminal live ou archive et retourne max_lines lignes. 
    Si newest_first est True, les lignes sont retournées dans l'ordre du plus récent au plus ancien.
    """
    log_file = LIVE_LOG_FILE if source == "live" else ARCHIVE_LOG_FILE
    # Si le fichier de log n'existe pas, retourner une chaîne vide pour éviter les erreurs d'affichage.
    if not log_file.exists():
        return ""
    with open(log_file, "r", encoding="utf-8") as f:
        lines = f.readlines()
    selected_lines = lines[-max_lines:]
    if newest_first:
        selected_lines = list(reversed(selected_lines))
    content = "".join(selected_lines)
    # Insere un saut de ligne avant chaque entree commencant par un timestamp [YYYY-MM-DD HH:MM:SS]
    # Cela permet de mieux separer les differentes lignes dans l'affichage du terminal, 
    # surtout lorsque les messages sont longs ou contiennent des sauts de ligne internes.
    content = re.sub(r"(?<!\A)(\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:\.\d{3})?\])", r"\n\1", content).lstrip("\n")
    return content


def format_terminal_log_for_display(content: str) -> str:
    """Convertit les timestamps complets en affichage console compact [HH:MM:SS]."""
    return re.sub(
        r"\[(\d{4}-\d{2}-\d{2} )?(\d{2}:\d{2}:\d{2})(?:\.\d{3})?\]",
        r"[\2]",
        content,
    )


def _render_mode_brut_terminal_content(max_lines: int, source: str) -> None:
    """Affiche uniquement le contenu du terminal brut et stylise l'affichage du terminal."""
    # Lire les dernières lignes du log du terminal (live ou archive) et les formater pour l'affichage. 
    # Si aucun log n'est disponible, afficher un message d'information.
    logs = read_terminal_log(max_lines=max_lines, source=source, newest_first=True)
    if not logs:
        logs = "Aucune sortie disponible. Lance un scan pour remplir le terminal."
    else:
        logs = format_terminal_log_for_display(logs)

    # Construire un bloc HTML stylisé pour afficher le contenu du terminal avec un thème sombre et une police monospace.
    active_log_name = LIVE_LOG_FILE.name if source == "live" else ARCHIVE_LOG_FILE.name
    terminal_html = f"""
    <div style=\"background:#0a0f14; color:#9ef01a; border-radius:10px; padding:14px; border:1px solid #263341;\">
        <div style=\"color:#8aa0b2; margin-bottom:8px; font-family:Consolas, 'Courier New', monospace;\">
            RFID terminal - {active_log_name}
        </div>
        <div style=\"margin:0; height:520px; overflow-y:auto; font-family:Consolas, 'Courier New', monospace; font-size:13px; line-height:1.5; white-space:pre-wrap; word-break:break-word;\">{escape(logs).replace(chr(10), '<br>')}</div>
    </div>
    """
    # Afficher le bloc HTML dans Streamlit en autorisant le rendu HTML brut pour styliser le terminal.
    st.markdown(terminal_html, unsafe_allow_html=True)


def _init_scan_state() -> None:
    """Initialise les variables de session pour la gestion du scan."""
    if "scan_thread" not in st.session_state:
        st.session_state.scan_thread = None
    if "scan_stop_event" not in st.session_state:
        st.session_state.scan_stop_event = None
    if "scan_status" not in st.session_state:
        st.session_state.scan_status = "idle"


def _parse_record_datetime(value) -> datetime | None:
    """Parse une valeur de date/heure provenant d'un record de tag. Accepte les timestamps numériques, les chaînes ISO8601, ou les objets datetime."""
    if isinstance(value, datetime):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            # On convert les timestamps numériques en datetime. 
            # On gère à la fois les timestamps en secondes (10 chiffres) et en millisecondes (13 chiffres).
            return _parse_record_datetime(int(text))
        try:
            # On parse les chaînes de caractères en essayant de gérer les formats ISO8601 avec ou sans timezone.
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
    """"Charge les tags depuis un fichier JSON uploadé ou depuis le fichier de tags par défaut. 
    Accepte les formats avec une clé "tags" ou une liste brute de tags.
    """
    if uploaded_file is not None:
        try:
            # On lit le contenu du fichier uploadé et on le parse en JSON. 
            # Si le parsing échoue, une exception est levée pour informer l'utilisateur.
            payload = json.loads(uploaded_file.getvalue().decode("utf-8"))
        except Exception as exc:
            raise ValueError(f"Impossible de lire le fichier JSON importé: {exc}") from exc
    else:
        try:
            # Si aucun fichier n'est uploadé, on charge le contenu du fichier de tags par défaut.
            payload = connexion_decodeur.load_json(TAGS_FILE)
        except Exception:
            return []

    # Déterminer le format du JSON et extraire la liste des tags. 
    # Le JSON peut être soit un dictionnaire avec une clé "tags" contenant la liste, soit une liste brute de tags.
    if isinstance(payload, dict):
        tags = payload.get("tags", [])
    elif isinstance(payload, list):
        tags = payload
    else:
        raise ValueError("Format JSON non pris en charge.")

    if not isinstance(tags, list):
        raise ValueError("Le JSON doit contenir une liste de tags.")

    # On renvoie uniquement les éléments qui sont des dictionnaires, 
    # en filtrant les entrées invalides pour éviter les erreurs d'affichage ou de traitement ultérieur.
    return [tag for tag in tags if isinstance(tag, dict)]


def _build_classic_tag_table(
    raw_tags: list[dict],
    debounce_minutes: int,
    correspondence: dict[str, str] | None = None,
) -> pd.DataFrame:
    """Construit une table de tags avec les colonnes: Coureur, Tag ID, Nb lectures, Premiere visualisation, Derniere visualisation, Antenne, RSSI max.
    Pour l'affichage du tableau dans la page graphique.
    debounce_minutes correspond à la durée minimale entre 2 lectures d'un même tag pour qu'elles soient comptabilisées comme des lectures distinctes.
    """
    # Dictionnaire pour stocker les informations agrégées par tag_id
    rows: dict[str, dict] = {}
    # Valeur par défaut pour les timestamps manquants
    default_time = datetime.min
    # Conversion de la durée de debounce en secondes pour les comparaisons temporelles
    debounce_seconds = max(0, int(debounce_minutes * 60))

    # Fonction de tri pour trier les enregistrements par timestamp (en priorité: client_timestamp, puis reader_timestamp, puis timestamp)
    def sort_key(record: dict) -> datetime:
        # Essaie d'extraire un timestamp valide du record avec plusieurs clés possibles
        return (
            _parse_record_datetime(
                record.get("client_timestamp")
                or record.get("reader_timestamp")
                or record.get("timestamp")
            )
            or default_time
        )

    # Traitement des enregistrements bruts, triés par ordre chronologique
    for record in sorted(raw_tags, key=sort_key):
        # Extraction du tag_id en essayant plusieurs clés possibles (epc, EPC, tag_id)
        # On nettoie avec strip() pour enlever les espaces inutiles
        tag_id = str(record.get("epc") or record.get("EPC") or record.get("tag_id") or "").strip()
        # Ignore les enregistrements sans tag_id valide
        if not tag_id:
            continue

        # Extraction du timestamp de l'enregistrement avec fallback sur datetime.now() si absent
        record_time = _parse_record_datetime(
            record.get("client_timestamp")
            or record.get("reader_timestamp")
            or record.get("timestamp")
        ) or datetime.now()

        # Extraction et conversion de la valeur d'antenne (essaie plusieurs clés)
        antenna_value = record.get("antenna", record.get("AntennaID"))
        try:
            # Essaie de convertir en entier si la valeur n'est pas vide
            antenna_value = int(antenna_value) if antenna_value is not None and antenna_value != "" else None
        except (TypeError, ValueError):
            # En cas d'erreur de conversion, on garde la valeur en tant que string
            antenna_value = str(antenna_value) if antenna_value is not None else None

        # Extraction et conversion de la valeur RSSI (signal strength)
        rssi_value = record.get("rssi", record.get("PeakRSSI"))
        try:
            # Essaie de convertir en float pour le calcul du maximum
            rssi_value = float(rssi_value)
        except (TypeError, ValueError):
            # Si conversion échoue, on laisse à None
            rssi_value = None

        # Récupère la ligne existante ou en crée une nouvelle si c'est le premier enregistrement pour ce tag
        row = rows.get(tag_id)
        if row is None:
            # Initialisation d'une nouvelle entrée pour ce tag
            rows[tag_id] = {
                "tag_id": tag_id,
                "runner_name": get_epc_name(tag_id, correspondence),  # Récupère le nom du coureur associé
                "read_count": 1,  # Première lecture
                "first_seen": record_time,  # Première visualisation
                "last_seen": record_time,  # Dernière visualisation (ignorant le debounce)
                "last_raw_seen": record_time,  # Dernière lecture brute (pour le debounce)
                "antenna": antenna_value,
                "rssi_max": rssi_value,
            }
            continue

        # Pour les tags déjà vus: applique la logique de debounce
        # Ne compte la lecture que si elle dépasse le délai minimal configuré
        if (record_time - row["last_raw_seen"]).total_seconds() > debounce_seconds:
            row["read_count"] += 1

        # Mise à jour du dernier timestamp brut et du dernier timestamp "significatif"
        row["last_raw_seen"] = record_time
        row["last_seen"] = record_time
        # Mise à jour de l'antenne (dernière valeur reçue)
        if antenna_value is not None:
            row["antenna"] = antenna_value
        # Mise à jour du RSSI max (garde la valeur la plus élevée reçue)
        if rssi_value is not None:
            row["rssi_max"] = rssi_value if row["rssi_max"] is None else max(row["rssi_max"], rssi_value)

    # Retourne un DataFrame vide avec les colonnes attendues si aucun tag n'a été trouvé
    if not rows:
        return pd.DataFrame(
            columns=["Coureur", "Tag ID", "Nb lectures", "Premiere visualisation", "Derniere visualisation", "Antenne", "RSSI max"]
        )

    # Construction de la liste des données formatées pour le DataFrame
    data = []
    # On trie les tags par date de dernière visualisation (du plus récent au plus ancien) pour l'affichage dans le tableau
    for row in rows.values():
        data.append({
            "Coureur": row["runner_name"],
            "Tag ID": row["tag_id"],
            "Nb lectures": row["read_count"],
            # Conversion des timestamps en format lisible (YYYY-MM-DD HH:MM:SS)
            "Premiere visualisation": row["first_seen"].strftime("%Y-%m-%d %H:%M:%S"),
            "Derniere visualisation": row["last_seen"].strftime("%Y-%m-%d %H:%M:%S"),
            "Antenne": row["antenna"],
            "RSSI max": row["rssi_max"],
            # Colonne temporaire pour le tri
            "_sort_time": row["last_seen"],
        })

    # Création du DataFrame, tri par dernière visualisation (décroissant) et suppression de la colonne de tri
    df = pd.DataFrame(data)
    df = df.sort_values("_sort_time", ascending=False).drop(columns=["_sort_time"])
    return df.reset_index(drop=True)


def _scan_worker(
    cfg: dict,
    stop_event: threading.Event,
    completion_message: str,
) -> None:
    """Fonction qui tourne dans un thread séparé pour exécuter le scan RFID sans bloquer l'interface Streamlit."""
    # Acquiert le verrou pour mettre l'état du scan à "running" une fois que le thread est bien lancé,
    # ce qui évite les conditions de course avec le bouton Démarrer/Arrêter qui vérifierait l'état simultanément
    with _SCAN_LOCK:
        _SCAN_STATE["status"] = "running"

    # Enregistre dans le log du terminal les paramètres du scan (adresse IP, port, antennes et durée)
    # pour tracer le démarrage et faciliter le débogage
    append_terminal_log(
        f"Demarrage scan ip={cfg['ip']} port={cfg['port']} antennas={cfg['antennas']} duration={cfg['duration']}"
    )

    # Crée un flux personnalisé qui capturera tous les affichages (stdout/stderr) du scan
    terminal_stream = TerminalLogStream()
    try:
        # Redirige tous les prints et erreurs vers le flux de terminal personnalisé pour les afficher dans Streamlit
        with redirect_stdout(terminal_stream), redirect_stderr(terminal_stream):
            # Exécute la fonction bloquante run_inventory qui gère le scan RFID
            # - Reste active tant que le scan est en cours ou que la durée n'est pas écoulée
            # - S'arrête proprement si stop_event est déclenché (arrêt utilisateur)
            # - Retourne un code de résultat indiquant le succès (0) ou une erreur (autre valeur)
            result = connexion_decodeur.run_inventory(
                cfg["ip"],
                cfg["port"],
                cfg["timeout"],
                cfg["antennas"],
                cfg["duration"],
                cfg.get("afficher_antennes", False),
                stop_event=stop_event,
                client_holder=None,
            )
        # Force l'écriture de toutes les données en attente dans le flux vers le terminal Streamlit
        terminal_stream.flush()

        # Traite le résultat du scan :
        # - Si le code est 0 (succès) ET que stop_event n'est pas déclenché (pas d'arrêt manuel),
        #   affiche le message de complétion fourni en paramètre
        # - Si le code n'est pas 0, affiche une message d'erreur avec le code de résultat
        if result == 0 and not stop_event.is_set():
            append_terminal_log(completion_message)
        elif result != 0:
            append_terminal_log(f"Erreur scan code={result}")
    except Exception as exc:
        # Capture et enregistre toute exception levée pendant le scan pour informer l'utilisateur
        append_terminal_log(f"Exception scan: {exc}")
    finally:
        # En cas de succès, erreur ou exception, marque le scan comme terminé (état "idle")
        # dans l'état partagé protégé par le verrou pour éviter les conditions de course
        with _SCAN_LOCK:
            _SCAN_STATE["status"] = "idle"


def _start_scan(cfg: dict, completion_message: str) -> bool:
    """
    Démarre le thread de scan si aucun scan n'est en cours. Retourne True si le scan a été démarré, False sinon.
    """
    # Récupère le thread de scan stocké dans l'état de session de Streamlit
    thread = st.session_state.scan_thread
    # Vérifie si un thread de scan est déjà en cours d'exécution
    if thread is not None and thread.is_alive():
        # Retourne False pour indiquer qu'un scan est déjà actif et ne peut pas en démarrer un nouveau
        return False

    # Crée un événement d'arrêt (Event) pour permettre au thread de scan de s'arrêter gracieusement
    stop_event = threading.Event()
    # Crée un nouveau thread daemon qui exécutera la fonction _scan_worker
    thread = threading.Thread(
        target=_scan_worker,
        # Passe la configuration, l'événement d'arrêt et le message de complétion en arguments
        args=(cfg, stop_event, completion_message),
        # daemon=True permet au thread de s'arrêter avec l'application principale
        daemon=True,
    )
    # Stocke l'événement d'arrêt dans l'état de session pour pouvoir l'accéder ultérieurement
    st.session_state.scan_stop_event = stop_event
    # Stocke la référence du thread dans l'état de session pour pouvoir vérifier son statut
    st.session_state.scan_thread = thread
    # Met à jour le statut du scan à "starting" dans l'état de session Streamlit
    st.session_state.scan_status = "starting"
    # Met à jour l'état du scan à "starting" dans le dictionnaire partagé protégé par verrou
    # pour éviter les conditions de course avec le bouton Démarrer/Arrêter qui vérifie l'état du scan
    with _SCAN_LOCK:
        _SCAN_STATE["status"] = "starting"
    # Démarre l'exécution du thread de scan
    thread.start()
    # Retourne True pour indiquer que le scan a été démarré avec succès
    return True


def _stop_scan() -> bool:
    """
    Arrête le thread de scan s'il est en cours d'exécution. 
    Retourne True si une demande d'arrêt a été envoyée, False si aucun scan n'était actif.
    """
    # Récupère l'événement d'arrêt stocké dans l'état de session pour signaler au thread de s'arrêter
    stop_event = st.session_state.scan_stop_event
    # Récupère la référence du thread de scan depuis l'état de session pour vérifier son statut
    thread = st.session_state.scan_thread
    # Si aucun thread de scan n'est actif (None ou terminé), on retourne False pour indiquer qu'aucune action d'arrêt n'a été nécessaire.
    if thread is None or not thread.is_alive():
        # Met à jour le statut du scan à "idle" dans l'état de session Streamlit pour indiquer qu'aucun scan n'est en cours
        st.session_state.scan_status = "idle"
        # Protège l'accès au dictionnaire partagé _SCAN_STATE avec un verrou pour éviter les conditions de course
        with _SCAN_LOCK:
            # Met à jour l'état partagé du scan à "idle" pour que tous les threads voient le changement
            _SCAN_STATE["status"] = "idle"
        # Retourne False pour indiquer qu'aucun scan n'était actif à arrêter
        return False

    # Met à jour le statut du scan à "stopping" dans l'état de session Streamlit pour indiquer qu'on arrête le scan
    st.session_state.scan_status = "stopping"
    # Protège l'accès au dictionnaire partagé _SCAN_STATE avec un verrou pour éviter les conditions de course
    with _SCAN_LOCK:
        # Met à jour l'état partagé du scan à "stopping" pour que le thread de scan sache qu'il doit s'arrêter
        _SCAN_STATE["status"] = "stopping"
    # Vérifie que l'événement d'arrêt existe avant de le signaler au thread
    if stop_event is not None:
        # Signale l'événement d'arrêt pour demander au thread de scan de terminer son exécution gracieusement
        stop_event.set()
    # Enregistre un message dans le journal du terminal pour informer l'utilisateur qu'une demande d'arrêt a été envoyée
    append_terminal_log("Demande d'arret du scan en cours.")
    # Retourne True pour indiquer qu'une demande d'arrêt a été envoyée avec succès
    return True


def _get_scan_status() -> str:
    """Retourne l'etat du scan pour la session courante."""
    # Récupère la référence du thread de scan stockée dans l'état de session Streamlit
    thread = st.session_state.scan_thread
    # Protège l'accès au dictionnaire partagé _SCAN_STATE avec un verrou pour éviter les conditions de course
    with _SCAN_LOCK:
        # Récupère le statut du scan depuis l'état partagé thread-safe
        shared_status = _SCAN_STATE["status"]
    # Vérifie si le thread existe, est en cours d'exécution et que le statut partagé indique "idle"
    # Cette condition détecte une incohérence: le thread tourne mais l'état dit "idle"
    if thread is not None and thread.is_alive() and shared_status == "idle":
        # Corrige le statut en "running" pour refléter l'état réel du thread en exécution
        shared_status = "running"
    # Synchronise le statut du scan dans l'état de session Streamlit pour maintenir la cohérence
    st.session_state.scan_status = shared_status
    # Retourne le statut actuel du scan (peut être "idle", "running", "stopping", etc.)
    return shared_status

def _sync_global_antenna_power() -> None:
    """Applique les valeurs globales Tx/Rx a toutes les antennes configurees."""
    # Tente de récupérer et d'analyser la liste des antennes principales depuis l'état de session
    try:
        # Récupère la chaîne de caractères "main_antennas" depuis l'état de session Streamlit
        # avec une chaîne vide comme valeur par défaut si la clé n'existe pas
        antennas = connexion_decodeur.parse_antennas(st.session_state.get("main_antennas", ""))
    # Gère les exceptions qui pourraient survenir lors du parsing (format invalide, etc.)
    except Exception:
        # En cas d'erreur, utilise la liste d'antennes par défaut prédéfinie
        antennas = DEFAULT_ANTENNAS

    # Itère sur chaque antenne de la liste
    for ant in antennas:
        # Définit la puissance d'émission (Tx) pour l'antenne actuelle
        # Récupère la valeur globale "global_tx" avec 31.5 comme défaut
        st.session_state[f"ant_{ant}_tx"] = st.session_state.get("global_tx", 31.5)
        # Définit la puissance de réception (Rx) pour l'antenne actuelle
        # Récupère la valeur globale "global_rx" avec -80.0 comme défaut
        st.session_state[f"ant_{ant}_rx"] = st.session_state.get("global_rx", -80.0)


def _normalize_json_filename(value: str, default_name: str) -> str:
    """
    Normalise un nom de fichier JSON en s'assurant qu'il a l'extension .json et en extrayant 
    uniquement le nom de fichier sans chemin. Si la valeur est vide ou invalide, retourne le nom par défaut.
    """
    # Convertit la valeur en chaîne de caractères, utilise une chaîne vide si elle est None ou falsy
    # puis supprime les espaces blancs en début et fin de chaîne
    name = str(value or "").strip()
    
    # Vérifie si la chaîne normalisée est vide après suppression des espaces
    # Si c'est le cas, retourne le nom de fichier par défaut fourni en paramètre
    if not name:
        return default_name
    
    # Extrait uniquement le nom de fichier sans le chemin complet
    # Utilise Path pour gérer les séparateurs de répertoires indépendamment du système
    name = Path(name).name
    
    # Vérifie si le nom de fichier se termine par l'extension ".json" (insensible à la casse)
    # Si ce n'est pas le cas, ajoute l'extension ".json" au nom du fichier
    if not name.lower().endswith(".json"):
        name = f"{name}.json"
    
    # Retourne le nom de fichier normalisé avec l'extension .json
    return name


def _load_presets_root(path: Path) -> dict:
    """
    Charge le contenu brut du fichier de presets et retourne un dictionnaire avec une clé "presets" contenant les presets. 
     - Si le fichier contient déjà une clé "presets" avec un dictionnaire, elle est utilisée telle quelle.
     - Si le fichier contient un dictionnaire sans la clé "presets", ce dictionnaire est encapsulé dans une nouvelle clé "presets".
     - Si le fichier ne contient pas de dictionnaire, un dictionnaire vide est utilisé pour les presets.
     - En cas d'erreur de lecture ou de format, un dictionnaire avec des presets vides est retourné.
     Cette fonction garantit que le résultat a toujours la structure attendue pour les presets, quel que soit le format initial du fichier.
    """
    # Charge le fichier JSON à partir du chemin fourni en paramètre
    raw = connexion_decodeur.load_json(path)
    
    # Vérifie si le contenu chargé est un dictionnaire ET contient déjà une clé "presets" qui est elle-même un dictionnaire
    # Si c'est le cas, crée une copie du dictionnaire brut pour préserver la structure existante
    if isinstance(raw, dict) and isinstance(raw.get("presets"), dict):
        root = dict(raw)
    # Sinon, vérifie si le contenu est un dictionnaire mais sans clé "presets"
    # Dans ce cas, encapsule le dictionnaire brut sous une nouvelle clé "presets"
    elif isinstance(raw, dict):
        root = {"presets": dict(raw)}
    # Si le contenu n'est pas un dictionnaire (peut être None, une liste, une chaîne, etc.)
    # Crée un dictionnaire avec une clé "presets" vide pour assurer la structure attendue
    else:
        root = {"presets": {}}

    # Effectue une vérification de sécurité finale pour s'assurer que root["presets"] est toujours un dictionnaire
    # Cette vérification protège contre les cas où root["presets"] pourrait ne pas être un dictionnaire valide
    if not isinstance(root.get("presets"), dict):
        root["presets"] = {}
    
    # Retourne le dictionnaire structuré avec la garantie que la clé "presets" contient toujours un dictionnaire
    return root

def _load_storage_preferences_from_presets(preset_name: str | None = None) -> dict[str, str]:
    """Lit les prefs depuis presets.json.

    Source unique: preset.storage_preferences.
    """
    # Tente de récupérer les préférences de stockage du preset spécifié (ou du preset actif si preset_name est None)
    # Cette opération interroge le décodeur de connexion pour lire les données persistées
    try:
        return connexion_decodeur.get_storage_preferences(preset_name)
    # En cas d'erreur (fichier manquant, format invalide, accès refusé, etc.),
    # retourne un dictionnaire vide pour éviter de bloquer l'application
    # Les clés attendues seront alors absent ou initialisées par défaut ailleurs
    except Exception:
        return {}


def _save_storage_preferences_to_presets(presets_name: str, tags_name: str, config_name: str) -> None:
    """
    Persiste les chemins de stockage dans le preset actif et dans le fichier bootstrap pour que les 
    autres presets puissent y accéder.
    
    Cette fonction effectue une double sauvegarde :
    1) Mise à jour du fichier bootstrap (presets.json) avec le pointeur au preset actif
    2) Mise à jour du preset actif lui-même avec ses préférences de stockage
    
    Args:
        presets_name: Nom du fichier contenant les presets
        tags_name: Nom du fichier contenant les tags
        config_name: Nom du fichier contenant la configuration
    """
    # Construit un dictionnaire avec les trois chemins de stockage à persister
    # Ces chemins permettront au preset de retrouver ses données indépendamment
    prefs = {
        "presets_filename": presets_name,
        "tags_filename": tags_name,
        "config_filename": config_name,
    }
    
    # 1) BOOTSTRAPPING: Mise à jour du fichier bootstrap (presets.json)
    # Cette étape sauvegarde un pointeur vers le fichier presets actif dans le fichier bootstrap
    # Cela permet aux autres presets de localiser le preset actif sans scanner le système
    # Le fichier bootstrap sert de point d'entrée unique pour toute l'application
    try:
        # Charge la structure racine du fichier bootstrap
        bootstrap_root = _load_presets_root(PRESETS_FILE)
        # Enregistre le nom du fichier presets actif dans le bootstrap
        bootstrap_root["active_presets_filename"] = presets_name
        # Persiste les modifications dans le fichier bootstrap
        connexion_decodeur.save_json(PRESETS_FILE, bootstrap_root)
    except Exception:
        # En cas d'erreur lors de la sauvegarde du bootstrap, on continue silencieusement
        # L'application reste fonctionnelle même si le bootstrapping échoue
        pass

    # 2) SAUVEGARDE LOCALE: Mise à jour du preset actif uniquement
    # Cette étape persiste les préférences de stockage directement dans le preset actif
    # Cela garantit que chaque preset conserve ses propres chemins d'accès à ses données
    try:
        # Récupère l'identifiant ou le nom du preset actuellement actif depuis la session
        current = st.session_state.get("current_preset")
        # Vérifie que le preset actif existe et n'est pas None
        if current:
            # Met à jour les préférences de stockage pour ce preset spécifique
            connexion_decodeur.update_preset_storage_preferences(current, prefs)
    except Exception:
        # En cas d'erreur lors de la sauvegarde locale, on continue silencieusement
        # La perte de cette sauvegarde n'est pas critique puisque le bootstrap contient les infos essentielles
        pass


def _save_active_presets_filename(name: str) -> None:
    """Persiste le nom du fichier presets actif dans le fichier bootstrap."""
    try:
        # Charge le fichier bootstrap contenant les préférences globales
        bootstrap_root = _load_presets_root(PRESETS_FILE)
        # Met à jour le nom du fichier de presets actuellement actif dans la structure bootstrap
        bootstrap_root["active_presets_filename"] = name
        # Sauvegarde les modifications du bootstrap dans le fichier de configuration JSON
        connexion_decodeur.save_json(PRESETS_FILE, bootstrap_root)
    except Exception:
        # En cas d'erreur (fichier manquant, permission refusée, etc.), on continue silencieusement
        # L'application reste fonctionnelle même si la sauvegarde du bootstrap échoue
        pass


def _apply_storage_paths() -> None:
    """
    Applique les chemins de stockage configurés dans les variables de session à la configuration 
    globale utilisée par le module connexion_decodeur.    
    """
    # Déclare que les variables globales doivent être modifiées dans cette fonction
    global PRESETS_FILE, TAGS_FILE, CONFIG_FILE
    
    # Construit le chemin absolu du fichier de presets en combinant le répertoire script avec le nom de fichier
    # stocké dans la session (permet à l'utilisateur de choisir différents fichiers de presets)
    PRESETS_FILE = SCRIPT_DIR / st.session_state.presets_filename
    
    # Construit le chemin absolu du fichier de tags (étiquettes) en combinant le répertoire script 
    # avec le nom de fichier de tags issu de la session
    TAGS_FILE = SCRIPT_DIR / st.session_state.tags_filename
    
    # Construit le chemin absolu du fichier de configuration en combinant le répertoire script 
    # avec le nom de fichier de configuration issu de la session
    CONFIG_FILE = SCRIPT_DIR / st.session_state.config_filename
    
    # Synchronise les chemins de fichiers globaux avec le module connexion_decodeur pour s'assurer que
    # toutes les fonctions du module utilisent les mêmes fichiers de configuration (important pour la cohérence)
    connexion_decodeur.PRESETS_FILE = PRESETS_FILE
    connexion_decodeur.TAGS_FILE = TAGS_FILE
    connexion_decodeur.CONFIG_FILE = CONFIG_FILE



# ────────────────────────────────────────────────────────────────────
# Initialisation Session State
# ────────────────────────────────────────────────────────────────────

# ════════════════════════════════════════════════════════════════════════════════════════
# 1) BOOTSTRAPPING: Initialisation du système de configuration multi-fichiers
# ════════════════════════════════════════════════════════════════════════════════════════
#
# POURQUOI LE BOOTSTRAPPING EST NÉCESSAIRE:
# ─────────────────────────────────────────
# Le bootstrapping est une phase de démarrage critique qui résout un problème circulaire :
#   • Nous avons besoin de connaître quel "preset" est actif pour charger la bonne configuration
#   • Mais ces informations sont stockées dans presets.json lui-même
#   • Et le chemin de presets.json peut varier selon la session utilisateur
#
# ÉTAPES DU PROCESSUS:
# ───────────────────
# Étape 1 : Lecture du fichier presets.json racine
#   - Charge le fichier presets.json depuis la racine du script (chemin par défaut)
#   - Ce fichier agit comme "point d'entrée" contenant les préférences globales
#   - Il révèle quel preset doit être actif (active_presets_filename)
#
# Étape 2 : Détermination du preset actif
#   - Extrait la clé "active_presets_filename" du fichier racine
#   - Cette clé indique quel fichier de preset charger (ex: "presets_custom.json" au lieu de "presets.json")
#   - Permet à l'utilisateur d'avoir plusieurs "configurations de presets" différentes
#
# Étape 3 : Initialisation des variables de session Streamlit
#   - Crée les clés de session_state si elles n'existent pas
#   - Définit les valeurs par défaut pour tous les paramètres critiques
#   - Garantit que l'app ne crash pas si une clé est absente
#   - Prépare le terrain pour charger les préférences sauvegardées plus tard
#
# ════════════════════════════════════════════════════════════════════════════════════════

# Lecture des préférences globales depuis presets.json pour découvrir le preset actif
bootstrap_root = _load_presets_root(PRESETS_FILE)
active_presets_filename = None
if isinstance(bootstrap_root, dict):
    # Extrait le nom du fichier de preset actif depuis la racine
    active_presets_filename = bootstrap_root.get("active_presets_filename")

# Initialisation exhaustive des variables de session avec leurs valeurs par défaut
# Chaque variable représente un état critique nécessaire au fonctionnement de l'app
if "current_preset" not in st.session_state:
    st.session_state.current_preset = None
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
if "use_whitelist" not in st.session_state:
    st.session_state.use_whitelist = True
if "presets_filename" not in st.session_state:
    # Normalise le nom de fichier du preset actif découvert lors du bootstrap
    # Utilise le découvert, ou la valeur par défaut (PRESETS_FILE.name) s'il n'existe pas
    st.session_state.presets_filename = _normalize_json_filename(
        active_presets_filename,
        PRESETS_FILE.name,
    )

# 2) Important: met à jour PRESETS_FILE/connexion_decodeur.PRESETS_FILE avant de lire last_used_preset
connexion_decodeur.PRESETS_FILE = SCRIPT_DIR / st.session_state.presets_filename
PRESETS_FILE = connexion_decodeur.PRESETS_FILE

# 3) Déterminer le preset à charger automatiquement (si indiqué)
try:
    _last_used = connexion_decodeur.get_last_used_preset()
except Exception:
    _last_used = None

# 4) Préférences de stockage: si le dernier preset définit des prefs, elles priment
storage_prefs = _load_storage_preferences_from_presets(_last_used)
if "tags_filename" not in st.session_state:
    st.session_state.tags_filename = _normalize_json_filename(
        storage_prefs.get("tags_filename"),
        TAGS_FILE.name,
    )
if "config_filename" not in st.session_state:
    # Le fichier de config est optionnel, si le preset n'en définit pas, on continue d'utiliser le config.json par défaut
    st.session_state.config_filename = _normalize_json_filename(
        storage_prefs.get("config_filename"),
        CONFIG_FILE.name,
    )
if "presets_filename_input" not in st.session_state:
    # Le champ de saisie du nom de fichier presets dans l'interface doit être initialisé avec le nom du fichier presets actif, 
    # qui peut provenir du preset à charger automatiquement
    st.session_state.presets_filename_input = st.session_state.presets_filename
if "tags_filename_input" not in st.session_state:
    # Le champ de saisie du nom de fichier tags dans l'interface doit être initialisé avec le nom du fichier tags actif,
    # qui peut provenir du preset à charger automatiquement
    st.session_state.tags_filename_input = st.session_state.tags_filename
if "config_filename_input" not in st.session_state:
    # Meme chose pour le champ de saisie du nom de fichier config dans l'interface
    st.session_state.config_filename_input = st.session_state.config_filename
_init_scan_state()
_apply_storage_paths()

# 5) Prépare le chargement auto du dernier preset (avant création des widgets)
if _last_used and "preset_to_apply" not in st.session_state and not st.session_state.get("current_preset"):
    st.session_state.preset_to_apply = _last_used


# ────────────────────────────────────────────────────────────────────
# PAGE 1: Accueil - Gestion des Presets
# ────────────────────────────────────────────────────────────────────

def page_accueil():
    """Page d'accueil: gestion des presets et paramètres du lecteur.
    
    Cette page est composée de deux colonnes principales:
    - Colonne gauche: Configuration du lecteur RFID (IP, port, antennes, puissances, etc.)
    - Colonne droite: Gestion des presets (charger, sauvegarder, supprimer)
    
    Les presets permettent de sauvegarder et charger rapidement des configurations complètes
    du lecteur RFID avec tous les paramètres (IP, port, antennes, puissances Tx/Rx, etc.).
    """
    # Affiche le titre principal de la page avec un emoji 
    st.title("📡 Centre de Contrôle RFID")
    # Trace une ligne de séparation horizontale pour délimiter visuellement la section
    st.markdown("---")
    
    # Crée deux colonnes avec ratio 1.2:1 pour donner plus d'espace à la colonne gauche (config)
    # Le paramètre gap="large" ajoute un espacement horizontal significatif (16px) entre les colonnes
    # col_left: Réceptacle pour les éléments de configuration du lecteur (60% de l'espace)
    # col_right: Réceptacle pour les éléments de gestion des presets (40% de l'espace)
    col_left, col_right = st.columns([1.2, 1], gap="large")
    
    # ══════════════════════════════════════════════════════════════════
    # COLONNE GAUCHE: Paramétrage du lecteur
    # ══════════════════════════════════════════════════════════════════
    with col_left:
        # Affiche le titre de la section "Configuration du Lecteur" avec un emoji engrenage pour
        # indiquer visuellement qu'il s'agit d'une section de paramétrage/configuration
        st.subheader("⚙️ Configuration du Lecteur")
        
        # Champ de saisie pour l'adresse IP du lecteur RFID
        # Le paramètre key="main_ip" permet de stocker et récupérer la valeur depuis l'état de session Streamlit
        # Cette adresse IP est utilisée pour établir la connexion avec le lecteur RFID
        ip_addr = st.text_input("Adresse IP :", key="main_ip")
        
        # Trace une ligne de séparation horizontale pour améliorer la lisibilité visuelle
        st.markdown("---")
        
        # Section 1: Paramétrage manuel de l'application
        # Le paramètre expanded=True rend cette section visible par défaut à la première visite
        # Cet expander contient les paramètres essentiels pour la connexion et le scan RFID
        with st.expander("📋 Paramétrage manuel de l'application", expanded=True):
            # Divise cette section en deux colonnes pour une meilleure organisation et compacité
            # col_p1: Contient les paramètres de connexion (Port, Timeout)
            # col_p2: Contient les paramètres de scan (Antennes, Durée)
            col_p1, col_p2 = st.columns(2)
            
            # Colonne gauche: Paramètres de connexion
            with col_p1:
                # Champ numérique pour entrer le numéro de port du lecteur RFID
                # Limité entre 1 et 65535 (plage valide des ports TCP/IP)
                # key="main_port" identifie ce champ dans l'état de session Streamlit
                port = st.number_input("Port :", min_value=1, max_value=65535, key="main_port")
                
                # Champ numérique pour définir le délai d'expiration (timeout) en secondes
                # Minimum 0.1 secondes, incrément de 0.1 pour éviter les délais trop courts
                # key="main_timeout" identifie ce champ dans l'état de session Streamlit
                timeout = st.number_input("Timeout (s) :", min_value=0.1, step=0.1, key="main_timeout")
            
            # Colonne droite: Paramètres de scan
            with col_p2:
                # Champ de saisie de texte pour spécifier les numéros des antennes à utiliser
                # Les antennes doivent être séparées par des virgules (ex: "1,2,3")
                # Le placeholder "1,2,3" donne un exemple du format attendu
                # key="main_antennas" identifie ce champ dans l'état de session Streamlit
                antennas_str = st.text_input(
                    "Antennes (séparées par virgules) :",
                    key="main_antennas",
                    placeholder="1,2,3"
                )
                
                # Champ numérique pour entrer la durée du scan RFID en secondes
                # Une valeur de 0 signifie un scan infini (jusqu'à arrêt manuel)
                # key="main_duration" identifie ce champ dans l'état de session Streamlit
                duration = st.number_input("Durée du scan (s, 0=infini) :", min_value=0, key="main_duration")
        
        # Trace une ligne de séparation horizontale entre cette section et la section suivante
        st.markdown("---")
        
        # 2. Paramétrage global des antennes
        # Crée une section dépliable (expander) pour organiser les paramètres globaux des antennes
        # L'icône 🌐 représente les paramètres réseau/globaux
        # expanded=False signifie que cette section est repliée par défaut à l'ouverture de la page
        with st.expander("🌐 Paramétrage global des antennes", expanded=False):
            # Divise la section en 2 colonnes égales pour une meilleure organisation visuelle
            col_g1, col_g2 = st.columns(2)
            
            # Colonne gauche: paramètre de puissance de transmission
            with col_g1:
                # Slider pour ajuster la puissance de transmission globale (Tx Power)
                # Exprimée en dBm (décibels-milliwatt)
                # Plage: 0.0 à 31.5 dBm avec un pas de 0.5 dBm
                # key="global_tx" identifie ce paramètre dans l'état de session Streamlit pour y accéder ultérieurement
                # on_change=_sync_global_antenna_power déclenche une fonction de synchronisation lorsque la valeur change
                # Cette fonction met à jour les valeurs de chaque antenne individuelle avec cette valeur globale
                global_tx_power = st.slider(
                    "Tx Power Global (dBm) :",
                    min_value=0.0, max_value=31.5, step=0.5,
                    key="global_tx",
                    on_change=_sync_global_antenna_power,
                )
            
            # Colonne droite: paramètre de sensibilité de réception
            with col_g2:
                # Slider pour ajuster la sensibilité de réception globale (Rx Sensitivity)
                # Exprimée en dBm (décibels-milliwatt)
                # Plage: -100.0 à 0.0 dBm avec un pas de 0.5 dBm
                # Les valeurs négatives représentent des niveaux de signal faibles mais détectables
                # key="global_rx" identifie ce paramètre dans l'état de session Streamlit
                # on_change=_sync_global_antenna_power déclenche la même fonction de synchronisation pour propager
                # les changements aux antennes individuelles
                global_rx_sensitivity = st.slider(
                    "Rx Sensitivity Global (dBm) :",
                    min_value=-100.0, max_value=0.0, step=0.5,
                    key="global_rx",
                    on_change=_sync_global_antenna_power,
                )
        
        # Trace une ligne de séparation horizontale (---) pour délimiter visuellement cette section
        # de la section suivante (Paramétrage individuel des antennes)
        st.markdown("---")
        
        # 3. Paramétrage individuel des antennes
        with st.expander("📡 Paramétrage individuel des antennes", expanded=False):
            # Récupérer les antennes valides
            try:
                antennas = connexion_decodeur.parse_antennas(antennas_str)
            except:
                antennas = DEFAULT_ANTENNAS
            
            antenna_params = {}
            for ant in antennas:
                with st.container(border=True):
                    st.markdown(f"#### Antenne {ant}")
                    col_a1, col_a2 = st.columns(2)
                    
                    with col_a1:
                        # Ce sont les valeurs globales qui sont utilisées par défaut pour chaque antenne, 
                        # mais si l'utilisateur a déjà modifié les sliders individuels, 
                        # on garde ces valeurs personnalisées en mémoire dans session_state pour ne pas les écraser 
                        # lorsqu'on change les valeurs globales.
                        tx_key = f"ant_{ant}_tx"
                        tx_value = global_tx_power if tx_key not in st.session_state else st.session_state[tx_key]
                        tx = st.slider(
                            "Tx Power :",
                            min_value=0.0,
                            max_value=31.5,
                            value=tx_value,
                            step=0.5,
                            key=tx_key,
                        )
                    with col_a2:
                        # Même logique pour le Rx que pour le Tx, on utilise la valeur globale par défaut mais on conserve 
                        # les personnalisations individuelles en mémoire.
                        rx_key = f"ant_{ant}_rx"
                        rx_value = global_rx_sensitivity if rx_key not in st.session_state else st.session_state[rx_key]
                        rx = st.slider(
                            "Rx Sensitivity :",
                            min_value=-100.0,
                            max_value=0.0,
                            value=rx_value,
                            step=0.5,
                            key=rx_key,
                        )
                    
                    antenna_params[ant] = {"tx_power": tx, "rx_sensitivity": rx}
    
    # ══════════════════════════════════════════════════════════════════
    # COLONNE DROITE: Gestion des presets
    # ══════════════════════════════════════════════════════════════════
    # Affiche la section de gestion des presets dans la colonne de droite
    with col_right:
        # En-tête de la section presets avec icône de dossier
        st.subheader("📂 Gestion des Presets")
        
        # ════════════════════════════════════════════════════════════════
        # Section 1: Charger un Preset existant
        # ════════════════════════════════════════════════════════════════
        with st.container(border=True):
            # Titre de la sous-section avec icône de téléchargement
            st.markdown("### 📥 Charger un Preset")
            
            # Récupère la liste complète des presets disponibles depuis le décodeur
            presets = connexion_decodeur.get_presets()
            
            # Cas où aucun preset n'existe
            if not presets:
                st.info("Aucun preset disponible")
            else:
                # Affiche un selectbox pour choisir parmi les presets existants
                # La clé "load_select" est utilisée pour identifier cet élément dans session_state
                preset_name = st.selectbox(
                    "Sélectionne un preset :",
                    list(presets.keys()),
                    key="load_select"
                )
                
                # Bouton pour charger le preset sélectionné
                if st.button("✅ Charger", type="primary", use_container_width=True, key="btn_load"):
                    # Stocke le nom du preset à appliquer dans la session
                    st.session_state.preset_to_apply = preset_name
                    # Force le rechargement de la page pour appliquer le preset
                    st.rerun()

                # Affiche un message de feedback si l'utilisateur en a validé un précédemment
                if "load_feedback" in st.session_state:
                    # Affiche le message et le retire de la session (pop)
                    st.success(st.session_state.pop("load_feedback"))
                
                # Affiche les détails du preset sélectionné dans un expander replié par défaut
                if preset_name in presets:
                    # Récupère la configuration complète du preset
                    cfg = presets[preset_name]
                    # Crée un expander contenant les détails (peut être cliqué pour voir/masquer)
                    with st.expander("📊 Détails", expanded=False):
                        # Affiche l'adresse IP du preset
                        st.write(f"**IP :** {cfg['ip']}")
                        # Affiche le port de connexion
                        st.write(f"**Port :** {cfg['port']}")
                        # Affiche les antennes configurées (jointes avec des virgules)
                        st.write(f"**Antennes :** {','.join(str(a) for a in cfg['antennas'])}")
                        # Affiche la durée de lecture en secondes
                        st.write(f"**Durée :** {cfg['duration']}s")
        
        # Ajoute un espace vertical pour séparer les sections
        st.markdown("")
        
        # ════════════════════════════════════════════════════════════════
        # Section 2: Sauvegarder un nouveau Preset
        # ════════════════════════════════════════════════════════════════
        with st.container(border=True):
            # Titre de la sous-section avec icône de sauvegarde
            st.markdown("### 💾 Sauvegarder un Preset")
            
            # Champ de texte pour entrer le nom du preset à créer
            # La clé "save_name" identifie ce champ dans session_state
            preset_name_save = st.text_input("Nom du preset :", placeholder="mon_preset", key="save_name")

            # Checkbox pour définir ce preset comme celui à charger au démarrage
            # La valeur par défaut est True (coché)
            set_as_last = st.checkbox(
                "⭐ Définir comme preset chargé au démarrage",
                value=True,
                key="save_set_as_last",
            )
            
            # Bouton pour déclencher la sauvegarde du nouveau preset
            if st.button("💾 Sauvegarder", type="primary", use_container_width=True, key="btn_save"):
                # Vérifie que le nom du preset n'est pas vide
                if not preset_name_save:
                    st.error("❌ Le nom du preset est obligatoire!")
                else:
                    try:
                        # Tente de parser la chaîne d'antennes (ex: "1,2,3") en liste
                        antennas = connexion_decodeur.parse_antennas(antennas_str)
                    except Exception as e:
                        # Affiche l'erreur si le parsing échoue
                        st.error(f"❌ Erreur antennes: {e}")
                        # Initialise antennas à None pour indiquer l'erreur
                        antennas = None
                    
                    # Vérifie que le parsing des antennes a réussi
                    if antennas:
                        # Valide tous les paramètres du preset (IP, port, antennes, durée, etc.)
                        erreurs = connexion_decodeur.valider_parametres(ip_addr, port, timeout, antennas, duration)
                        
                        # Cas où des erreurs de validation ont été trouvées
                        if erreurs:
                            # Affiche chaque erreur individuellement
                            for err in erreurs:
                                st.error(f"❌ {err}")
                        else:
                            # Tous les paramètres sont valides, on peut sauvegarder le preset
                            connexion_decodeur.sauvegarder_preset(
                                preset_name_save,  # Nom donné par l'utilisateur
                                ip_addr,  # Adresse IP du décodeur
                                port,  # Port de connexion
                                timeout,  # Délai d'attente en secondes
                                antennas,  # Liste des antennes à utiliser
                                duration,  # Durée de lecture
                                False,  # Paramètre booléen (non spécifié, probablement un statut)
                                # Paramètres optionnels pour les puissances TX/RX
                                global_tx_power=global_tx_power,
                                global_rx_sensitivity=global_rx_sensitivity,
                                # Paramètres individuels par antenne
                                antenna_params=antenna_params,
                                # Préférences de stockage (noms des fichiers de config)
                                storage_preferences={
                                    "presets_filename": st.session_state.presets_filename,
                                    "tags_filename": st.session_state.tags_filename,
                                    "config_filename": st.session_state.config_filename,
                                },
                                # Indique si ce preset doit être chargé au démarrage
                                set_as_last=set_as_last,
                            )
                            # Affiche un message de succès
                            st.success(f"✅ Preset '{preset_name_save}' créé!")
                            # Force le rechargement pour mettre à jour la liste des presets
                            st.rerun()
        
        # Ajoute un espace vertical pour séparer les sections
        st.markdown("")
        
        # ════════════════════════════════════════════════════════════════
        # Section 3: Supprimer un Preset existant
        # ════════════════════════════════════════════════════════════════
        with st.container(border=True):
            # Titre de la sous-section 
            st.markdown("### 🗑️ Supprimer un Preset")
            
            # Récupère la liste actuelle des presets (peut avoir changé depuis le chargement)
            presets = connexion_decodeur.get_presets()
            
            # Cas où aucun preset n'existe (rien à supprimer)
            if not presets:
                st.info("Aucun preset à supprimer")
            else:
                # Affiche un selectbox pour choisir le preset à supprimer
                # La clé "del_select" identifie cet élément dans session_state
                preset_to_del = st.selectbox(
                    "Sélectionne un preset :",
                    list(presets.keys()),
                    key="del_select"
                )
                
                # Bouton pour confirmer et supprimer le preset sélectionné
                if st.button("🗑️ Supprimer", type="secondary", use_container_width=True, key="btn_del"):
                    # Tente de supprimer le preset et vérifie le résultat
                    if connexion_decodeur.supprimer_preset(preset_to_del):
                        # Affiche un message de succès si la suppression a réussi
                        st.success(f"✅ '{preset_to_del}' supprimé!")
                        # Force le rechargement pour mettre à jour la liste des presets
                        st.rerun()
                    else:
                        # Affiche un message d'erreur si la suppression a échoué
                        st.error(f"❌ Erreur lors de la suppression")
    


# ────────────────────────────────────────────────────────────────────
# PAGE 2: Mode Classique
# ────────────────────────────────────────────────────────────────────

def page_mode_classique():
    """Page d'affichage du tableau des tags en mode classique,
    on pourra afficher la lecture de tags en live ou en chargeant un fichier json de tags."""
    st.title("🚴 Mode Classique")
    st.markdown("---")
    st.caption("Les lectures rapprochées sont regroupées selon une fenêtre de non-relecture configurable.")

    whitelist_enabled = st.session_state.use_whitelist
    # Charge les entrées de la whitelist pour filtrer les tags
    whitelist_entries = load_whitelist_entries()
    # Extrait les EPCs de la whitelist pour utilisation ultérieure dans le filtrage
    whitelist_epcs = [entry["epc"] for entry in whitelist_entries]
    # Charge la correspondance EPC utilisée pour afficher les informations liées aux tags
    correspondence = load_epc_correspondence()

    # Affiche un avertissement si la whitelist est vide mais activée
    if whitelist_enabled and not whitelist_epcs:
        st.warning("Whitelist vide: aucun tag ne sera affiche en mode classique.")
    elif not whitelist_enabled:
        st.info("Whitelist ignoree: tous les tags lus sont pris en compte sur cette page.")

    # Le debounce sert à regrouper les lectures rapprochées d'un même tag pour n'afficher qu'une seule
    # ligne par tag dans le tableau.
    # Initialise les variables de session pour le mode classique si elles n'existent pas
    # Ces variables persistent à travers les reruns de l'application Streamlit
    
    # Initialise la fenêtre de debounce (non-relecture) à 10 minutes par défaut
    # Permet de regrouper les lectures rapprochées du même tag
    if "classique_debounce_minutes" not in st.session_state:
        st.session_state.classique_debounce_minutes = 10
    
    # Initialise l'actualisation automatique activée par défaut
    # Contrôle si le tableau se met à jour automatiquement selon l'intervalle défini
    if "classique_auto_refresh" not in st.session_state:
        st.session_state.classique_auto_refresh = True
    
    # Initialise l'intervalle d'actualisation à 3 secondes par défaut
    # Détermine la fréquence de rafraîchissement du tableau en mode live
    if "classique_refresh_interval" not in st.session_state:
        st.session_state.classique_refresh_interval = 3
    
    # Initialise la liste des tags bruts lus en dernier
    # Stock temporaire des données brutes des tags RFID
    if "classique_last_raw_tags" not in st.session_state:
        st.session_state.classique_last_raw_tags = []
    
    # Initialise le DataFrame vide pour les tags en mode live
    # Stocke les données formatées prêtes pour l'affichage dans le tableau
    if "classique_last_df_live" not in st.session_state:
        st.session_state.classique_last_df_live = pd.DataFrame()

    # Crée un slider pour configurer la fenêtre de debounce (non-relecture)
    # Permet à l'utilisateur de définir la durée minimale entre deux lectures du même tag
    # Valeurs: 0 à 60 minutes, par incrément d'1 minute
    debounce_minutes = st.slider(
        "Fenêtre de non-relecture (minutes)",
        min_value=0,
        max_value=60,
        value=st.session_state.classique_debounce_minutes,
        step=1,
        key="classique_debounce_minutes",
    )

    # Crée deux onglets: un pour la lecture en direct (live) et un pour importer des données depuis un fichier JSON
    tab_live, tab_import = st.tabs(["📡 Live", "📁 Fichier JSON"])

    # Section de l'onglet "Live" pour afficher les données en temps réel
    with tab_live:
        # Titre de la sous-section pour la vue en direct
        st.subheader("Vue live")
        # Description explicative du fonctionnement du tableau live
        st.caption("Le tableau lit le fichier de tags et se met à jour pendant un scan lancé depuis cette page.")

        # Crée trois colonnes pour les contrôles: checkbox, slider, et bouton
        # Les proportions sont 1.5:1:1 pour donner plus d'espace au premier élément
        live_col1, live_col2, live_col3 = st.columns([1.5, 1, 1])
        
        # Colonne 1: Checkbox pour activer/désactiver l'actualisation automatique
        with live_col1:
            auto_refresh = st.checkbox(
                "Actualisation automatique",
                value=st.session_state.classique_auto_refresh,
                key="classique_auto_refresh",
            )
        
        # Colonne 2: Slider pour définir l'intervalle d'actualisation en secondes
        # Valeurs entre 1 et 10 secondes, la valeur par défaut est récupérée de la session
        with live_col2:
            refresh_interval = st.slider(
                "Intervalle (s)",
                min_value=1,
                max_value=10,
                value=st.session_state.classique_refresh_interval,
                step=1,
                key="classique_refresh_interval",
            )
        
        # Colonne 3: Bouton de rafraîchissement manuel
        # Permet à l'utilisateur de forcer une mise à jour immédiate du tableau
        with live_col3:
            if st.button("🔄 Rafraîchir maintenant", use_container_width=True):
                # Réexécute l'application pour récupérer les dernières données
                st.rerun()

        # Récupère le statut actuel du scan RFID (starting, running, stopping, ou stopped)
        # Utilisé pour activer/désactiver les boutons et afficher les informations pertinentes
        scan_status = _get_scan_status()

        # Affiche les infos du preset pour rappeler à l'utilisateur les paramètres du scan en cours, et propose de démarrer/arrêter le scan.
        # Vérifie d'abord si un preset est sélectionné dans la session utilisateur
        if st.session_state.current_preset:
            # Charge la configuration du preset sélectionné depuis le décodeur
            cfg = connexion_decodeur.charger_preset(st.session_state.current_preset)
            
            # Si la configuration a été chargée avec succès, affiche les paramètres et les contrôles
            if cfg:
                # Crée 4 colonnes égales pour afficher les informations du preset
                cfg_col1, cfg_col2, cfg_col3, cfg_col4 = st.columns(4)
                
                # Colonne 1: Affiche l'adresse IP du décodeur RFID
                with cfg_col1:
                    st.metric("IP", cfg["ip"])
                
                # Colonne 2: Affiche le numéro de port utilisé pour la connexion
                with cfg_col2:
                    st.metric("Port", cfg["port"])
                
                # Colonne 3: Affiche la liste des antennes RFID configurées (séparées par des virgules)
                with cfg_col3:
                    st.metric("Antennes", ",".join(str(a) for a in cfg["antennas"]))
                
                # Colonne 4: Affiche la durée du scan en secondes, ou "∞" si le scan est infini (duration <= 0)
                with cfg_col4:
                    st.metric("Durée", f"{cfg['duration']}s" if cfg['duration'] > 0 else "∞")

                # Crée 2 colonnes égales pour les boutons de contrôle du scan
                scan_col1, scan_col2 = st.columns([1, 1])
                
                # Colonne 1: Bouton de démarrage du scan
                with scan_col1:
                    # Vérifie si un scan est actuellement actif (starting, running, ou stopping)
                    active = scan_status in {"starting", "running", "stopping"}
                    
                    # Bouton primaire pour démarrer le scan live
                    # Désactivé si un scan est déjà en cours
                    if st.button(
                        "🚀 Demarrer le scan live",
                        type="primary",
                        use_container_width=True,
                        disabled=active,
                    ):
                        # Tente de démarrer le scan avec la configuration actuelle
                        # Affiche un message de succès lorsque le scan est terminé
                        if _start_scan(cfg, "Scan termine avec succes (mode classique)"):
                            # Réexécute l'application pour mettre à jour l'interface
                            st.rerun()
                
                # Colonne 2: Bouton d'arrêt du scan
                with scan_col2:
                    # Bouton pour arrêter le scan en cours
                    # Désactivé si aucun scan n'est actuellement actif
                    if st.button(
                        "⏹ Arreter le scan",
                        use_container_width=True,
                        disabled=not active,
                    ):
                        # Tente d'arrêter le scan en cours
                        if _stop_scan():
                            # Réexécute l'application pour mettre à jour l'interface
                            st.rerun()
                        else:
                            # Affiche un avertissement si aucun scan n'était actif
                            st.warning("Aucun scan actif à arrêter.")
        else:
            # Affiche un avertissement si aucun preset n'est chargé
            # Guide l'utilisateur pour charger un preset depuis l'onglet Accueil
            st.warning("Charge d'abord un preset dans l'onglet Accueil pour lancer un scan live.")

        # Initialise le flag d'erreur de lecture à False
        # Ce flag sera défini à True si une erreur se produit lors de la lecture des tags
        read_error = False
        
        try:
            # Tente de charger le payload des tags depuis le fichier (tags.json)
            raw_tags = _load_tags_payload()
            
            # Si la liste blanche est activée, filtre les tags bruts pour ne garder que ceux dans la liste blanche
            if whitelist_enabled:
                raw_tags = filter_tags_with_whitelist(raw_tags, whitelist_epcs)
            
            # Construit le dataframe formaté des tags en mode classique (agrégation par EPC avec compte du nombre de lectures)
            # Utilise la correspondance EPC pour enrichir les données si disponible
            df_live = _build_classic_tag_table(raw_tags, debounce_minutes, load_epc_correspondence())
            
            # Sauvegarde les données dans la session pour les réutiliser en cas d'erreur lors du prochain rafraîchissement
            st.session_state.classique_last_raw_tags = raw_tags
            st.session_state.classique_last_df_live = df_live
        
        except ValueError as exc:
            # Gère les erreurs de décodage JSON (typiquement JSONDecodeError)
            # Cela se produit généralement si le fichier tags.json est lu pendant qu'il est écrit par le service de scan
            
            # Marque l'erreur pour l'afficher à l'utilisateur
            read_error = True
            
            # Récupère les tags et le dataframe sauvegardés précédemment dans la session
            # Cela permet d'afficher les dernières données valides même en cas d'erreur
            raw_tags = st.session_state.classique_last_raw_tags
            df_live = st.session_state.classique_last_df_live
            
            # Affiche un message d'information explicatif sur le problème de lecture
            st.caption(f"Lecture en cours (fichier en ecriture): {exc}")

        # Actualise automatiquement pendant un scan actif pour afficher les lectures au fur et à mesure
        # qu'elles arrivent dans le fichier, en respectant l'intervalle de rafraîchissement configuré.
        # Cette boucle de rafraîchissement améliore l'expérience utilisateur en fournissant des données
        # quasi en temps réel lors d'un scan actif (starting, running ou stopping).
        if st.session_state.classique_auto_refresh and scan_status in {"starting", "running", "stopping"}:
            # Attend l'intervalle de rafraîchissement configuré avant de réinitialiser la page
            time.sleep(refresh_interval)
            # Réinitialise et réexécute l'application pour afficher les données mises à jour
            st.rerun()

        # Crée trois colonnes affichent les métriques de synthèse en live
        # Ces métriques donnent un aperçu rapide du nombre de lectures et de la fenêtre de temps utilisée
        live_summary_col1, live_summary_col2, live_summary_col3 = st.columns(3)
        
        # Première métrique: affiche le nombre total de lectures brutes récupérées du fichier tags.json
        with live_summary_col1:
            st.metric("Lectures brutes", len(raw_tags))
        
        # Deuxième métrique: affiche le nombre de tags uniques après agrégation par EPC
        with live_summary_col2:
            st.metric("Tags agrégés", len(df_live))
        
        # Troisième métrique: affiche la fenêtre de temps de regroupement (debounce) en minutes
        with live_summary_col3:
            st.metric("Fenêtre", f"{debounce_minutes} min")

        # Vérifie si le dataframe en live est vide pour adapter l'affichage
        if df_live.empty:
            # Si le scan est actif, affiche un message spécifique selon le type d'erreur ou d'état
            if scan_status in {"starting", "running", "stopping"}:
                # Si une erreur de lecture s'est produite, affiche un message d'erreur informatif
                if read_error:
                    st.info("Scan actif: lecture temporairement indisponible (ecriture en cours).")
                # Sinon, c'est probablement parce que le scan attend des données
                else:
                    st.info("Scan actif: en attente de lectures...")
            # Si le scan n'est pas actif, affiche un message neutre
            else:
                st.info("Aucune lecture disponible pour le moment.")
        # Si le dataframe contient des données, affiche le tableau avec la largeur du conteneur
        else:
            st.dataframe(df_live, use_container_width=True, hide_index=True)
    
    # Onglet d'import: permet de charger un fichier JSON de tags pour afficher les lectures agrégées
    # dans le même format que la vue live, en respectant la même logique de regroupement par fenêtre
    # de non-relecture (debounce) et de filtrage par whitelist si activée.
    with tab_import:
        st.subheader("Import JSON")
        st.caption("Charge un fichier JSON brut contenant la clé 'tags' ou une liste de lectures de tags.")

        # Widget de téléchargement pour sélectionner un fichier JSON local
        # La clé "classique_json_upload" garantit que le widget conserve son état
        uploaded_file = st.file_uploader("Choisir un fichier JSON", type=["json"], key="classique_json_upload")

        # Affiche un message informatif si aucun fichier n'a été téléchargé
        if uploaded_file is None:
            st.info(f"Aucun fichier importé. Le tableau affichera le contenu local de {TAGS_FILE.name} si disponible.")

        # Traite le fichier JSON téléchargé avec gestion des erreurs
        try:
            # Charge les tags bruts depuis la charge utile JSON (fichier ou contenu)
            raw_tags_import = _load_tags_payload(uploaded_file)
            
            # Applique la whitelist si elle est activée pour filtrer les tags indésirables
            if whitelist_enabled:
                raw_tags_import = filter_tags_with_whitelist(raw_tags_import, whitelist_epcs)
            
            # Construit le dataframe formaté des tags importés selon la logique classique
            # (agrégation par EPC, comptage des lectures, regroupement par fenêtre de debounce)
            df_import = _build_classic_tag_table(raw_tags_import, debounce_minutes, load_epc_correspondence())
        
        # Capture les erreurs de décodage JSON ou de validation des données
        except ValueError as exc:
            # Affiche l'erreur à l'utilisateur de manière claire
            st.error(str(exc))
            # Initialise les variables avec des valeurs vides pour éviter les erreurs ultérieures
            raw_tags_import = []
            df_import = pd.DataFrame()

        # Crée trois colonnes équilibrées pour afficher les métriques de synthèse de l'import
        import_summary_col1, import_summary_col2, import_summary_col3 = st.columns(3)
        
        # Première métrique: affiche le nombre total de lectures brutes importées
        with import_summary_col1:
            st.metric("Lectures brutes", len(raw_tags_import))
        
        # Deuxième métrique: affiche le nombre de tags uniques après agrégation par EPC
        with import_summary_col2:
            st.metric("Tags agrégés", len(df_import))
        
        # Troisième métrique: affiche la fenêtre de temps de regroupement (debounce) en minutes
        with import_summary_col3:
            st.metric("Fenêtre", f"{debounce_minutes} min")

        # Vérifie si le dataframe importé est vide pour adapter l'affichage
        if df_import.empty:
            # Affiche un message informatif si aucune donnée n'a pu être extraite du JSON
            st.info("Aucune lecture à afficher depuis cette source JSON.")
        # Si le dataframe contient des données, affiche le tableau avec la largeur du conteneur
        else:
            st.dataframe(df_import, use_container_width=True, hide_index=True)
 


# ────────────────────────────────────────────────────────────────────
# PAGE 3: Configuration
# ────────────────────────────────────────────────────────────────────

def page_configuration():
    """Page de configuration de l'application,
    où l'on va pouvoir définir les noms des fichiers de stockage local, et aussi effacer les données de presets ou de tags si besoin 
    pour repartir sur une configuration propre.
    """
    # Affiche le titre principal
    st.title("⚙️ Configuration")
    st.markdown("---")

    # === SECTION 1: Configuration des noms de fichiers ===
    st.subheader("📝 Noms des fichiers")
    st.caption("Definis les noms des fichiers utilises pour le stockage local.")

    # Crée trois colonnes pour afficher les champs de saisie côte à côte
    name_col1, name_col2, name_col3 = st.columns(3)
    
    # Colonne 1: Champ d'entrée pour le nom du fichier des presets
    with name_col1:
        st.text_input("Presets (.json)", key="presets_filename_input")
    
    # Colonne 2: Champ d'entrée pour le nom du fichier des tags
    with name_col2:
        st.text_input("Tags (.json)", key="tags_filename_input")
    
    # Colonne 3: Champ d'entrée pour le nom du fichier de configuration
    with name_col3:
        st.text_input("Configuration (.json)", key="config_filename_input")

    # Bouton pour appliquer les nouveaux noms de fichiers
    if st.button("💾 Appliquer les noms", use_container_width=True):
        # Normalise le nom du fichier des presets (nettoie et valide le format)
        new_presets_name = _normalize_json_filename(
            st.session_state.presets_filename_input,
            PRESETS_FILE.name,
        )
        # Normalise le nom du fichier des tags
        new_tags_name = _normalize_json_filename(
            st.session_state.tags_filename_input,
            TAGS_FILE.name,
        )
        # Normalise le nom du fichier de configuration
        new_config_name = _normalize_json_filename(
            st.session_state.config_filename_input,
            CONFIG_FILE.name,
        )

        # Met à jour l'état de session avec les nouveaux noms normalisés
        st.session_state.presets_filename = new_presets_name
        st.session_state.tags_filename = new_tags_name
        st.session_state.config_filename = new_config_name
        
        # Applique les nouveaux chemins de stockage au niveau de l'application
        _apply_storage_paths()
        
        # Sauvegarde les préférences de stockage dans le fichier des presets
        _save_storage_preferences_to_presets(new_presets_name, new_tags_name, new_config_name)
        
        # Affiche un message de succès et relance l'application
        st.success("Noms mis a jour.")
        st.rerun()
    
    # === SECTION 2: Affichage des fichiers de stockage actuels ===
    st.subheader("💾 Stockage des Données")
    
    # Crée deux colonnes pour l'affichage des métriques
    col1, col2 = st.columns(2)
    
    # Affiche les noms actuels des fichiers de stockage
    with col1:
        # Métrique pour le fichier des presets
        st.metric("Fichier Presets", PRESETS_FILE.name)
        # Métrique pour le fichier des tags
        st.metric("Fichier Tags", TAGS_FILE.name)
        # Métrique pour le fichier de configuration
        st.metric("Fichier Config", CONFIG_FILE.name)
    
    # Séparateur visuel
    st.markdown("---")
    
    # === SECTION 3: Gestion des données (suppression) ===
    st.subheader("🔧 Gestion des Données")
    
    # Crée deux colonnes pour les boutons de suppression
    col1, col2 = st.columns(2)
    
    # Colonne 1: Bouton pour supprimer tous les presets
    with col1:
        if st.button("🗑️ Effacer TOUS les presets", use_container_width=True):
            # Sauvegarde un dictionnaire vide dans le fichier des presets
            connexion_decodeur.save_json(PRESETS_FILE, {})
            # Affiche un message de confirmation
            st.success("✅ Tous les presets ont été supprimés!")
            # Relance l'application pour appliquer les changements
            st.rerun()
    
    # Colonne 2: Bouton pour supprimer tous les tags
    with col2:
        if st.button("🗑️ Effacer TOUS les tags", use_container_width=True):
            # Sauvegarde une structure vide avec clé "tags" dans le fichier des tags
            connexion_decodeur.save_json(TAGS_FILE, {"tags": []})
            # Affiche un message de confirmation
            st.success("✅ Tous les tags ont été supprimés!")
            # Relance l'application pour appliquer les changements
            st.rerun()
    
    
    
    # === SECTION 4: Affichage du preset actuellement actif ===
    # Vérifie s'il existe un preset chargé dans l'état de session
    if st.session_state.current_preset:
        # Affiche le nom du preset actuellement actif avec un message de succès coloré
        st.success(f"✅ Preset actuellement actif: **{st.session_state.current_preset}**")


# ────────────────────────────────────────────────────────────────────
# PAGE 4 : Mode Brut
# ────────────────────────────────────────────────────────────────────

def page_mode_brut():
    """Page pour voir un terminal brut des logs et interactions,
    avec la possibilité de configurer l'intervalle d'actualisation et 
    de voir l'affichage en directe ou des logs enregistrés dans l'archive."""
    # Affiche le titre principal de la page
    st.title("💻 Mode Brut")
    # Ajoute une ligne de séparation horizontale
    st.markdown("---")
    # Affiche un message d'information descriptif sur le fonctionnement de la page
    st.info("Affiche la sortie brute capturée pendant les scans (style terminal).")

    # Crée deux colonnes pour les contrôles d'actualisation (1/4 et 3/4 de la largeur)
    refresh_col1, refresh_col2 = st.columns([1, 3])
    # Colonne 1: case à cocher pour activer/désactiver l'actualisation automatique
    with refresh_col1:
        # Récupère l'état de la case avec une clé unique pour maintenir l'état entre les relances
        auto_refresh = st.checkbox("Actualisation auto", value=True, key="brut_auto_refresh")
    # Colonne 2: curseur pour définir l'intervalle d'actualisation en millisecondes
    with refresh_col2:
        # Permet à l'utilisateur de sélectionner l'intervalle entre 500ms et 5000ms
        refresh_ms = st.slider(
            "Intervalle d'actualisation (ms)",
            min_value=500,
            max_value=5000,
            value=1500,
            step=250,
            key="brut_refresh_ms",
        )

    # Affiche un sous-titre pour la section de contrôle des scans
    st.subheader("▶️ Lancer un Scan RFID (Mode Brut)")
    # Crée deux colonnes pour les informations et les boutons de contrôle (2/3 et 1/3)
    col_scan1, col_scan2 = st.columns([2, 1])

    # Colonne 1: affichage des informations et du statut du scan
    with col_scan1:
        # Vérifie si un preset a été chargé dans la session
        if not st.session_state.current_preset:
            # Affiche un message d'avertissement si aucun preset n'est actif
            st.warning("⚠️ Charge d'abord un preset dans la section 'Charger un Preset'")
        else:
            # Charge la configuration du preset actuellement actif depuis le module connexion_decodeur
            cfg = connexion_decodeur.charger_preset(st.session_state.current_preset)
            # Vérifie que la configuration a été chargée avec succès
            if cfg:
                # Affiche le nom du preset actuellement actif en message de succès
                st.info(f"✅ Preset actif: **{st.session_state.current_preset}**")
                # Crée quatre colonnes pour afficher les paramètres du preset (IP, port, antennes, durée)
                col_meta1, col_meta2, col_meta3, col_meta4 = st.columns(4)
                # Colonne 1: affiche l'adresse IP du serveur RFID
                with col_meta1:
                    st.metric("IP", cfg["ip"])
                # Colonne 2: affiche le port de connexion au serveur
                with col_meta2:
                    st.metric("Port", cfg["port"])
                # Colonne 3: affiche la liste des antennes configurées (jointes par des virgules)
                with col_meta3:
                    st.metric("Antennes", ",".join(str(a) for a in cfg["antennas"]))
                # Colonne 4: affiche la durée du scan (affiche "∞" si la durée est infinie)
                with col_meta4:
                    st.metric("Durée", f"{cfg['duration']}s" if cfg['duration'] > 0 else "∞")
                # Récupère l'état actuel du scan (running, starting, stopping, ou stopped)
                scan_status = _get_scan_status()
                # Affiche un message selon l'état du scan
                if scan_status == "running":
                    # Le scan est en cours, l'affichage se met à jour automatiquement
                    st.success("✅ Scan en cours. Le terminal se met à jour automatiquement.")
                elif scan_status == "starting":
                    # Le scan est en train de démarrer
                    st.info("⏳ Lancement du scan... Le terminal va bientot se mettre a jour.")
                elif scan_status == "stopping":
                    # Le scan est en train de s'arrêter
                    st.warning("⏹️ Arrêt demandé. Le terminal va s'arreter.")
                else:
                    # Aucun scan n'est actuellement en cours
                    st.info("⏸️ Aucun scan détecté pour le moment.")

    # Colonne 2 : Boutons de contrôle du scan (démarrer/arrêter)
    with col_scan2:
        # Vérifie qu'un preset est actuellement sélectionné dans la session utilisateur
        if st.session_state.current_preset:
            # Charge la configuration du preset sélectionné via la connexion au décodeur
            cfg = connexion_decodeur.charger_preset(st.session_state.current_preset)
            # Valide que la configuration a bien été chargée
            if cfg:
                # Récupère l'état actuel du scan depuis la fonction utilitaire
                scan_status = _get_scan_status()
                # Détermine si le scan est actuellement actif (en cours de démarrage, en exécution ou en arrêt)
                active = scan_status in {"starting", "running", "stopping"}
                
                # Bouton pour démarrer un nouveau scan
                if st.button(
                    "🚀 Demarrer",  # Emoji fusée + texte "Démarrer"
                    type="primary",  # Style primaire (couleur saillante)
                    use_container_width=True,  # Occupe toute la largeur du conteneur
                    key="btn_start_scan_brut",  # Clé unique pour identifier ce bouton
                    disabled=active,  # Désactivé si un scan est déjà actif
                ):
                    # Lance le scan avec le message de succès défini
                    if _start_scan(cfg, "Scan termine avec succes"):
                        # Affiche un message de confirmation du lancement
                        st.success("✅ Scan lancé en arrière-plan")
                        # Réexécute l'application Streamlit pour rafraîchir l'affichage
                        st.rerun()
                
                # Bouton pour arrêter le scan en cours
                if st.button(
                    "⏹ Arreter le scan",  # Emoji stop + texte "Arrêter le scan"
                    use_container_width=True,  # Occupe toute la largeur du conteneur
                    key="btn_stop_scan_brut",  # Clé unique pour identifier ce bouton
                    disabled=not active,  # Désactivé si aucun scan n'est actif
                ):
                    # Tente d'arrêter le scan en cours
                    if _stop_scan():
                        # Affiche un message informatif si l'arrêt a réussi
                        st.info("Arrêt du scan demandé.")
                        # Réexécute l'application Streamlit pour rafraîchir l'affichage
                        st.rerun()
                    else:
                        # Affiche un avertissement si aucun scan n'était actif
                        st.warning("Aucun scan actif à arrêter.")

    # Ligne de séparation visuelle pour distinguer les sections
    st.markdown("---")

    # Section de configuration de l'affichage du terminal brut
    # Permet à l'utilisateur de définir le nombre de lignes affichées, la source des données,
    # et offre des outils pour nettoyer le terminal live ou rafraîchir manuellement l'affichage
    col1, col2, col3 = st.columns([1, 1, 2])  # Crée 3 colonnes avec largeurs proportionnelles 1:1:2
    
    with col1:
        # Sélecteur pour le nombre de dernières lignes du log à afficher
        # Les options sont 100, 200, 400 ou 800 lignes, avec 400 comme sélection par défaut
        max_lines = st.selectbox("Dernieres lignes", [100, 200, 400, 800], index=2)
    
    with col2:
        # Sélecteur pour choisir la source du contenu du terminal
        # "live" pour les logs en direct, "archive" pour les logs archivés
        source = st.selectbox("Source", ["live", "archive"], index=0)
    
    with col3:
        # Bouton pour effacer manuellement le contenu du terminal live
        if st.button("🧹 Effacer le terminal live", use_container_width=True):
            # Vide le fichier de log live en le réinitialisant avec une chaîne vide
            LIVE_LOG_FILE.write_text("", encoding="utf-8")
            # Affiche un message de confirmation à l'utilisateur
            st.success("Terminal live efface (archive conservee)")
            # Réexécute l'application pour refléter les changements
            st.rerun()

    # Bouton pour forcer un rafraîchissement manuel de l'interface
    if st.button("🔄 Rafraichir", use_container_width=True):
        # Réexécute l'application Streamlit pour mettre à jour tous les éléments
        st.rerun()

    # Calcule l'intervalle de rafraîchissement automatique
    # Si auto_refresh est activé, convertit refresh_ms en secondes avec formatage à 3 décimales
    # Sinon, reste None (pas de rafraîchissement automatique)
    refresh_interval = None if not auto_refresh else f"{refresh_ms / 1000:.3f}s"

    # Fragment Streamlit qui se réexécute automatiquement à chaque changement de refresh_interval
    # Cela se produit quand :
    # 1. L'utilisateur modifie l'intervalle d'actualisation automatique
    # 2. L'utilisateur active/désactive l'actualisation automatique
    # 3. L'utilisateur clique sur le bouton de rafraîchissement manuel
    # Ce mécanisme permet de mettre à jour efficacement le terminal avec les dernières lignes du log
    @st.fragment(run_every=refresh_interval)
    def render_mode_brut_terminal() -> None:
        """
        Fonction fragmentée qui affiche le contenu du terminal brut.
        Elle est réexécutée indépendamment du reste de la page selon l'intervalle défini.
        
        Paramètres utilisés (accessibles depuis le scope parent) :
        - max_lines : nombre de lignes de log à afficher
        - source : type de source ("live" ou "archive")
        """
        _render_mode_brut_terminal_content(max_lines, source)

    # Exécute la fonction fragmentée pour afficher le terminal brut avec les paramètres configurés
    render_mode_brut_terminal()
    
# ────────────────────────────────────────────────────────────────────
# PAGE 5 : Mode Graphique
# ────────────────────────────────────────────────────────────────

def page_mode_graphique():
    """Page pour visualiser les tags en temps réel avec graphiques,
    on pourra selectionner un fichier json contenant les tags avec la restriction ou non de a withelist et
    effectuer un filtre sur :
    - L'antenne
    - Le tags EPC ou coureur
    - par passage, RSSI moyen, écart moyen, dernière détection ou EPC
    - ordre croissant ou décroissant
    On peut y définir un cooldown avant relecture du tags.
    Cela affichera des graphiques de flux de tags dans le temps, des statistiques sur les tags les plus lus,
    on pourras également faie un focus sur un tag en particulier pour afficher des graphiques sur celui-ci
    """
    # Affiche le titre principal de la page
    st.title("📊 Mode Graphique")
    # Ajoute un séparateur visuel horizontal
    st.markdown("---")
    # Affiche un message informatif sur la fonctionnalité de la page
    st.info("Affiche les tags detectes avec statistiques de flux, graphiques et tri des resultats traites.")

    # Récupère l'état de la whitelist depuis la session Streamlit (indique si la whitelist est activée ou non)
    whitelist_enabled = st.session_state.use_whitelist

    # Titre de la section pour sélectionner le fichier JSON source
    st.subheader("Source JSON")
    # Récupère tous les fichiers JSON présents dans le répertoire du script, triés alphabétiquement
    json_files = sorted([p.name for p in SCRIPT_DIR.glob("*.json")])

    # Vérifie si aucun fichier JSON n'est trouvé dans le répertoire
    if not json_files:
        # Affiche un avertissement et quitte la fonction
        st.warning("Aucun fichier JSON trouve dans le dossier du projet.")
        return

    # Détermine le nom du fichier JSON par défaut
    # Si TAGS_FILE est présent dans la liste, l'utilise ; sinon, prend le premier fichier alphabétiquement
    default_name = TAGS_FILE.name if TAGS_FILE.name in json_files else json_files[0]
    # Crée une boîte de sélection pour permettre à l'utilisateur de choisir un fichier JSON
    selected_json = st.selectbox(
        "Fichier de donnees pour les graphiques",
        options=json_files,
        index=json_files.index(default_name),
        help="Selectionne un JSON deja present dans le projet."
    )

    # Construit le chemin complet du fichier JSON sélectionné en combinant le répertoire du script et le nom du fichier
    selected_path = SCRIPT_DIR / selected_json
    # Tente de charger le fichier JSON avec gestion d'erreur
    try:
        tags_data = connexion_decodeur.load_json(selected_path)
    except Exception as exc:
        # En cas d'erreur de lecture, affiche un message d'erreur et quitte
        st.error(f"Impossible de lire {selected_json}: {exc}")
        return

    # Normalise les données : supporte deux formats possibles
    # Format 1 : objet dictionnaire avec clé "tags" contenant la liste des tags
    # Format 2 : liste brute de tags directement
    if isinstance(tags_data, dict):
        # Cas dictionnaire : extrait la clé "tags", ou liste vide si absente
        tags_list = tags_data.get("tags", [])
    elif isinstance(tags_data, list):
        # Cas liste : utilise directement les données
        tags_list = tags_data
    else:
        # Cas inattendu : initialise une liste vide
        tags_list = []

    # Affiche le nom du fichier JSON actuellement utilisé comme source des données
    st.caption(f"Source active: {selected_json}")

    if not tags_list:
        st.warning("Aucune donnee exploitable de tags dans ce fichier JSON.")
        return
    # ========== CHARGEMENT ET VALIDATION DE LA WHITELIST ==========
    # Charge la liste blanche des EPC autorisés et la correspondance EPC <-> coureur
    whitelist_epcs = load_whitelist()
    correspondence = load_epc_correspondence()
    
    # Vérifie que la whitelist n'est pas vide si elle est activée
    # Affiche un avertissement et quitte si la whitelist est vide mais activée
    if whitelist_enabled and not whitelist_epcs:
        st.warning("Whitelist vide: aucun tag ne sera retenu. Ajoute des EPC dans la page Whitelist.")
        return
    
    # Informe l'utilisateur si la whitelist est désactivée (tous les tags seront utilisés)
    if not whitelist_enabled:
        st.info("Whitelist ignoree: toutes les lectures du fichier JSON sont prises en compte.")

    # ========== CONVERSION DES DONNÉES EN DATAFRAME ==========
    # Crée un DataFrame Pandas à partir de la liste de tags chargée
    df = pd.DataFrame(tags_list)
    
    # Vérifie que le DataFrame n'est pas vide après conversion
    if df.empty:
        st.warning("Donnees tags invalides ou vides.")
        return

    # ========== HARMONISATION ET VALIDATION DU TIMESTAMP ==========
    # Détecte la colonne timestamp appropriée : 'client_timestamp' ou 'timestamp'
    # Priorise 'client_timestamp' s'il existe, sinon utilise 'timestamp'
    ts_col = "client_timestamp" if "client_timestamp" in df.columns else "timestamp"
    
    # Vérifie que la colonne timestamp existe dans les données
    if ts_col not in df.columns:
        st.error("Aucun champ timestamp/client_timestamp detecte dans les donnees tags.")
        return
    
    # ========== PRÉPARATION ET NORMALISATION DES DONNÉES ==========
    # Convertit la colonne timestamp en format datetime (gère les erreurs avec 'coerce')
    df["timestamp_dt"] = pd.to_datetime(df[ts_col], errors="coerce")
    
    # Récupère et nettoie les EPC : conversion en string et suppression des espaces
    df["epc"] = df.get("epc", "").astype(str).str.strip()
    
    # Supprime les lignes avec des EPC vides (évite les entrées invalides)
    df = df[df["epc"] != ""].copy()
    
    # Normalise les EPC (ex: conversion de format, standardisation)
    df["epc_norm"] = df["epc"].map(normalize_epc)
    
    # Récupère le nom du coureur associé à chaque EPC via la correspondance
    df["coureur"] = df["epc"].map(lambda epc: get_epc_name(epc, correspondence))
    
    # Formate l'étiquette d'affichage pour chaque EPC (ex: "EPC: XXXX (Coureur)")
    df["tag_label"] = df["epc"].map(lambda epc: format_epc_label(epc, correspondence))
    
    # Convertit le numéro d'antenne en entier (par défaut 0 si absent)
    df["antenna"] = pd.to_numeric(df.get("antenna", 0), errors="coerce").fillna(0).astype(int)
    
    # Convertit le RSSI (Received Signal Strength Indicator) en numérique
    df["rssi"] = pd.to_numeric(df.get("rssi", 0), errors="coerce")
    
    # Récupère le nombre de fois que le tag a été vu (par défaut 1 si absent)
    df["seen_count"] = pd.to_numeric(df.get("seen_count", 1), errors="coerce").fillna(1)
    
    # Ignore seen_count: compte chaque enregistrement comme un passage unique
    # (Cela aligne le comportement avec le Mode Classique du système)
    df["passages"] = 1

    # ========== APPLICATION DE LA WHITELIST ==========
    # Sauvegarde le nombre de lignes avant filtrage whitelist
    total_before_whitelist = len(df)
    
    if whitelist_enabled:
        # Filtre le DataFrame : ne garde que les EPC normalisés présents dans la whitelist
        df = df[df["epc_norm"].isin(set(whitelist_epcs))].copy()
        retained_count = len(df)
        
        # Affiche un résumé du filtrage appliqué
        st.caption(
            f"Filtre whitelist applique: {retained_count}/{total_before_whitelist} lignes retenues "
            f"({len(whitelist_epcs)} EPC autorises)."
        )
    else:
        # Si whitelist désactivée, tous les enregistrements sont conservés
        retained_count = total_before_whitelist
        st.caption(f"Whitelist ignoree: {retained_count}/{total_before_whitelist} lignes retenues.")

    # Vérifie que des données restent après le filtrage whitelist
    if df.empty:
        if whitelist_enabled:
            st.warning("Aucun tag du fichier selectionne ne correspond a la whitelist.")
        else:
            st.warning("Aucun tag exploitable dans le fichier selectionne.")
        return

    # ========== INTERFACE DE FILTRAGE ET DE TRI ==========
    # Crée une section pour les filtres et options de tri
    st.subheader("Filtres et Tri")
    
    # Distribue les contrôles sur 4 colonnes
    col_f1, col_f2, col_f3, col_f4 = st.columns(4)

    # COLONNE 1 : Filtre par antenne
    with col_f1:
        # Récupère les numéros d'antennes uniques et les trie
        antenna_options = sorted(df["antenna"].dropna().unique().tolist())
        # Permet la sélection multiple d'antennes
        antenna_filter = st.multiselect("Filtrer par antenne", antenna_options)

    # COLONNE 2 : Filtre par EPC / coureur
    with col_f2:
        # Récupère les étiquettes EPC uniques et les trie
        epc_options = sorted(df["tag_label"].dropna().unique().tolist())
        # Permet la sélection multiple d'EPC
        epc_filter = st.multiselect("Filtrer par EPC / coureur", epc_options)

    # COLONNE 3 : Sélection du critère de tri
    with col_f3:
        # Permet de choisir la colonne sur laquelle trier les résultats
        sort_by = st.selectbox(
            "Trier resultats traites par",
            ["Passages", "RSSI moyen", "Ecart moyen (s)", "Derniere detection", "EPC"]
        )

    # COLONNE 4 : Sélection de l'ordre de tri
    with col_f4:
        # Permet de choisir l'ordre de tri (ascendant ou descendant)
        sort_order = st.selectbox("Ordre", ["Decroissant", "Croissant"])

    # ========== APPLICATION DES FILTRES ==========
    # Crée une copie du DataFrame pour appliquer les filtres
    filtered = df.copy()
    
    # Applique le filtre d'antenne si au moins une antenne a été sélectionnée
    if antenna_filter:
        filtered = filtered[filtered["antenna"].isin(antenna_filter)]
    
    # Applique le filtre d'EPC / coureur si au moins un EPC a été sélectionné
    if epc_filter:
        filtered = filtered[filtered["tag_label"].isin(epc_filter)]
    
    # Sauvegarde le nombre de lignes après filtrage
    filtered_count = len(filtered)

    # ========== CONFIGURATION DU COOLDOWN ANTI-SURLECTURE ==========
    # Crée une section pour les paramètres RF et cooldown
    st.subheader("Parametres RF et Cooldown")
    
    with st.container():
        # Curseur pour ajuster le délai minimum (cooldown) entre deux lectures du même EPC
        # Évite de compter deux fois la même lecture si elle se produit rapidement
        cooldown_min = st.slider(
            "Cooldown EPC (minutes)",
            min_value=0,
            max_value=60,
            value=1,
            step=1,
            help="Ignore une lecture si le meme EPC est deja passe il y a moins de X minutes."
        )

    # ========== APPLICATION DU COOLDOWN ==========
    # Trie les données filtrées par timestamp
    filtered = filtered.sort_values("timestamp_dt").copy()
    
    # Convertit le cooldown de minutes en secondes
    cooldown_s = cooldown_min * 60
    
    # Applique la logique de cooldown si le délai est > 0 et qu'il y a des données
    if cooldown_s > 0 and not filtered.empty:
        # Mode classique: dédoublonnage par EPC uniquement (pas de considération d'antenne)
        cooldown_group = ["epc_norm"]
        
        # Récupère le timestamp précédent pour chaque EPC groupé
        prev_ts = filtered.groupby(cooldown_group)["timestamp_dt"].shift(1)
        
        # Calcule l'écart en secondes entre deux lectures consécutives du même EPC
        delta_s = (filtered["timestamp_dt"] - prev_ts).dt.total_seconds()
        
        # Crée un masque booléen : garde une lecture si c'est la première (prev_ts.isna())
        # ou si l'écart est >= cooldown_s
        keep_mask = prev_ts.isna() | delta_s.isna() | (delta_s >= cooldown_s)
        
        # Compte le nombre de lectures conservées
        kept = int(keep_mask.sum())
        
        # Applique le masque pour garder seulement les lectures valides
        filtered = filtered[keep_mask].copy()
        
        # Affiche un résumé du cooldown appliqué
        st.caption(f"Cooldown applique: {kept}/{filtered_count} lectures conservees.")

    # Vérifie qu'il reste des données après l'application du cooldown
    if filtered.empty:
        st.warning("Aucune donnee apres filtrage.")
        return

    # ========== CALCUL DES STATISTIQUES GLOBALES DU FLUX ==========
    # Récupère les timestamps valides et les trie chronologiquement
    valid_ts = filtered["timestamp_dt"].dropna().sort_values()
    
    # Calcule le nombre total de passages dans les données filtrées
    total_passages = int(filtered["passages"].sum())
    
    # Compte le nombre de tags uniques (EPC distincts)
    unique_tags = int(filtered["epc"].nunique())

    # Initialise les variables de statistiques de flux
    avg_rate_ppm = 0.0  # Vitesse moyenne en passages par minute
    avg_gap_s = 0.0     # Écart moyen entre les lectures en secondes
    
    # Calcule les statistiques si au moins 2 timestamps sont disponibles
    if len(valid_ts) >= 2:
        # Calcule la durée totale entre le premier et le dernier timestamp
        # Utilise au minimum 1.0 secondes pour éviter la division par zéro
        duration_s = max((valid_ts.iloc[-1] - valid_ts.iloc[0]).total_seconds(), 1.0)
        
        # Calcule la vitesse moyenne : (passages / durée) * 60 pour obtenir passages/minute
        avg_rate_ppm = (total_passages / duration_s) * 60.0
        
        # Calcule les écarts entre lectures consécutives (en secondes)
        global_gaps = valid_ts.diff().dropna().dt.total_seconds()
        
        # Calcule l'écart moyen s'il y a au moins un écart
        if not global_gaps.empty:
            avg_gap_s = float(global_gaps.mean())

    # ========== AFFICHAGE DES STATISTIQUES DU FLUX ==========
    # Crée une section pour afficher les statistiques clés
    st.subheader("Statistiques du Flux")
    
    # Distribue les métriques sur 4 colonnes
    c1, c2, c3, c4 = st.columns(4)
    
    # Affiche le nombre total de passages
    with c1:
        st.metric("Passages (total)", total_passages)
    
    # Affiche le nombre de tags uniques
    with c2:
        st.metric("Tags uniques", unique_tags)
    
    # Affiche la vitesse moyenne en passages par minute
    with c3:
        st.metric("Vitesse moyenne", f"{avg_rate_ppm:.2f} passages/min")
    
    # Affiche l'écart moyen entre les lectures
    with c4:
        st.metric("Ecart moyen", f"{avg_gap_s:.2f} s")

    # ========== AGRÉGATION DES RÉSULTATS PAR EPC ==========
    # Crée une copie triée des données filtrées par timestamp
    work = filtered.sort_values("timestamp_dt").copy()
    
    # Calcule l'écart en secondes entre les lectures consécutives d'un même EPC
    work["gap_s"] = work.groupby("epc")["timestamp_dt"].diff().dt.total_seconds()

    # Agrège les données par EPC avec diverses statistiques
    processed = (
        work.groupby("epc", as_index=False)
        .agg(
            # Récupère le nom du coureur (première valeur non-vide trouvée)
            coureur=("coureur", lambda s: next((value for value in s if value), "")),
            # Somme les passages pour cet EPC
            passages=("passages", "sum"),
            # Calcule le RSSI moyen pour cet EPC
            rssi_moyen=("rssi", "mean"),
            # Calcule l'écart moyen entre les lectures de cet EPC
            ecart_moyen_s=("gap_s", "mean"),
            # Récupère la première détection (timestamp minimum)
            premiere_detection=("timestamp_dt", "min"),
            # Récupère la dernière détection (timestamp maximum)
            derniere_detection=("timestamp_dt", "max"),
            # Liste les antennes actives (uniques, séparées par des virgules, triées)
            antennes_actives=("antenna", lambda s: ",".join(str(x) for x in sorted(set(s))))
        )
    )

    # === FORMATAGE DES DONNÉES ===
    # Formatage des labels pour affichage
    # Applique la fonction format_epc_label à chaque ligne pour convertir l'EPC en label lisible
    # en utilisant la table de correspondance fournie
    processed["tag_label"] = processed.apply(
        lambda row: format_epc_label(row["epc"], correspondence),
        axis=1,
    )
    
    # Calcul de la durée de présence pour chaque EPC
    # Soustrait le premier timestamp du dernier timestamp pour obtenir la durée en secondes
    # Remplace les NaN par 0 pour éviter les erreurs de calcul (cas des EPC détectés une seule fois)
    processed["duree_presence_s"] = (
        processed["derniere_detection"] - processed["premiere_detection"]
    ).dt.total_seconds().fillna(0)

    # Calcul de la vitesse de passages par minute pour chaque EPC, en évitant la division par zéro
    # Formule: (nombre de passages / durée maximale de 1 seconde minimum) * 60 pour obtenir des passages/minute
    # Cela permet d'évaluer l'intensité des détections pour chaque tag
    processed["vitesse_passages_min"] = processed.apply(
        lambda row: (row["passages"] / max(row["duree_presence_s"], 1.0)) * 60.0,
        axis=1,
    )

    # === TRIAGE DES RÉSULTATS ===
    # Tri selon les critères sélectionnés, en utilisant une map pour faire le lien entre les options de tri 
    # et les colonnes du DataFrame. Cela centralise la configuration des critères disponibles.
    sort_map = {
        "Passages": "passages",                          # Trier par nombre total de passages
        "RSSI moyen": "rssi_moyen",                      # Trier par force du signal moyen
        "Ecart moyen (s)": "ecart_moyen_s",              # Trier par intervalle moyen entre lectures
        "Derniere detection": "derniere_detection",      # Trier par timestamp de dernière détection
        "EPC": "epc",                                    # Trier par identifiant EPC brut
    }

    # Applique le tri sur la colonne sélectionnée (sort_by)
    # Par défaut, les EPC sans RSSI ou sans écart moyen sont placés en bas du classement, 
    # que ce soit en ordre croissant ou décroissant, pour faciliter la lecture.
    processed = processed.sort_values(
        by=sort_map[sort_by],
        ascending=(sort_order == "Croissant"),           # True si ordre croissant, False si décroissant
        na_position="last",                              # Place les valeurs manquantes en fin de liste
    )

    # === PRÉPARATION DE L'AFFICHAGE ===
    # Création d'une copie du DataFrame pour la mise en forme sans modifier les données originales
    # Cette séparation permet de conserver les données brutes pour d'autres calculs
    processed_display = processed.copy()
    
    # Arrondir le RSSI moyen à 2 décimales pour améliorer la lisibilité (unité: dBm)
    processed_display["rssi_moyen"] = processed_display["rssi_moyen"].round(2)
    
    # Arrondir l'écart moyen entre lectures à 3 décimales (unité: secondes)
    processed_display["ecart_moyen_s"] = processed_display["ecart_moyen_s"].round(3)
    
    # Arrondir la durée de présence à 3 décimales (unité: secondes)
    processed_display["duree_presence_s"] = processed_display["duree_presence_s"].round(3)
    
    # Arrondir la vitesse de passages à 2 décimales (unité: passages/minute)
    processed_display["vitesse_passages_min"] = processed_display["vitesse_passages_min"].round(2)
    
    # Convertir les timestamps en chaînes de caractères pour un affichage lisible
    processed_display["premiere_detection"] = processed_display["premiere_detection"].astype(str)
    processed_display["derniere_detection"] = processed_display["derniere_detection"].astype(str)

    # === AFFICHAGE DU TABLEAU DE SYNTHÈSE ===
    # Affiche un sous-titre pour identifier la section
    st.subheader("Resultats Traites")
    
    # Affiche le tableau avec tous les résultats traités
    # use_container_width=True: le tableau prend toute la largeur disponible
    # hide_index=True: cache l'index du DataFrame pour un rendu plus propre
    st.dataframe(processed_display, use_container_width=True, hide_index=True)

    # Affiche une ligne de séparation visuelle
    st.markdown("---")
    
    # === SECTION GRAPHIQUES ===
    # Affiche le titre de la section
    st.subheader("Graphiques")

    # Crée deux colonnes côte à côte pour les graphiques
    g1, g2 = st.columns(2)

    # === GRAPHIQUE 1: Histogramme des passages par EPC ===
    with g1:
        # Titre du premier graphique
        st.markdown("**Histogramme du nombre de passages par EPC**")
        
        # Sélectionne les colonnes label et passages, puis utilise le label comme index
        # pour l'affichage en abscisse du graphique
        hist_epc = processed[["tag_label", "passages"]].set_index("tag_label")
        
        # Affiche un diagramme en barres montrant le nombre de détections par tag/coureur
        st.bar_chart(hist_epc)

    # === GRAPHIQUE 2: Flux temporel des passages ===
    with g2:
        # Titre du deuxième graphique
        st.markdown("**Flux de passages dans le temps (par minute)**")
        
        # Filtre les données pour garder uniquement les lignes avec timestamps valides
        # puis utilise le timestamp comme index pour un graphique temporel
        timeline = work.dropna(subset=["timestamp_dt"]).set_index("timestamp_dt")
        
        # Vérifie si le DataFrame filtré est vide (peut survenir si tous les timestamps sont invalides)
        if timeline.empty:
            # Affiche un message informatif au lieu du graphique
            st.info("Pas assez de timestamps valides pour le flux temporel.")
        else:
            # Regroupe les passages par minute et somme le nombre de passages pour chaque minute
            # Cela crée un flux montrant l'intensité des détections au cours du temps
            flow = timeline["passages"].resample("1min").sum().rename("passages")
            
            # Formate l'index (timestamps) en format "HHhMM" pour une meilleure lisibilité
            # Par exemple: 14h30, 14h31, etc.
            flow.index = flow.index.strftime("%Hh%M")
            
            # Affiche un graphique linéaire montrant l'évolution du flux de passages dans le temps
            st.line_chart(flow)

    # === GRAPHIQUE 3: Histogramme des écarts entre passages ===
    # Affiche le titre du graphique
    st.markdown("**Histogramme des ecarts entre passages (global)**")
    
    # Vérifie qu'il y a au moins 2 timestamps pour calculer des écarts
    if len(valid_ts) >= 2:
        # Calcule les différences entre timestamps consécutifs et les convertit en secondes
        gaps = valid_ts.diff().dropna().dt.total_seconds()
        
        # Définit les bornes des intervalles de temps pour regrouper les écarts
        # Cela permet d'identifier les patterns de détection (rapides, moyennes, lentes)
        bins = [0, 0.2, 0.5, 1, 2, 5, 10, 30, 60, float("inf")]
        
        # Étiquettes descriptives pour chaque intervalle
        # Facilite l'interprétation des résultats par l'utilisateur
        labels = ["0-0.2s", "0.2-0.5s", "0.5-1s", "1-2s", "2-5s", "5-10s", "10-30s", "30-60s", ">60s"]
        
        # Groupe les écarts dans les catégories définies, compte les occurrences et trie par index
        # Cela montre la distribution des intervalles de détection (utile pour comprendre les patterns)
        gap_hist = pd.cut(gaps, bins=bins, labels=labels, include_lowest=True).value_counts().sort_index()
        
        # Affiche un graphique en barres montrant la distribution des écarts de temps
        st.bar_chart(gap_hist)
    else:
        # Affiche un message informatif si les données sont insuffisantes
        # Cela peut survenir si le dataset ne contient que très peu de timestamps valides
        st.info("Pas assez de points temporels pour calculer les ecarts entre passages.")

    # Affiche une ligne de séparation visuelle entre les sections
    st.markdown("---")
    st.subheader("Suivi d'un tag / coureur")
    # Liste déroulante permettant de choisir le tag à analyser plus précisément.
    tracked_label = st.selectbox(
        "Choisir un tag a suivre",
        options=processed["tag_label"].tolist(),
    )
    # Récupère la ligne agrégée du tag sélectionné dans le tableau de synthèse.
    tracked_row = processed[processed["tag_label"] == tracked_label].iloc[0]
    # Récupère l'EPC associé au tag sélectionné afin de filtrer les lectures brutes correspondantes.
    tracked_epc = tracked_row["epc"]
    # Construit l'ensemble des lectures brutes liées à cet EPC, triées par horodatage.
    tracked_data = work[work["epc"] == tracked_epc].copy().sort_values("timestamp_dt")

    # Présente quatre indicateurs résumés pour donner une vue rapide sur le tag suivi.
    t1, t2, t3, t4 = st.columns(4)
    with t1:
        # Nombre total de lectures conservées après nettoyage / filtrage.
        st.metric("Lectures conservees", len(tracked_data))
    with t2:
        # Nombre total de passages détectés pour le tag.
        st.metric("Passages", int(tracked_row["passages"]))
    with t3:
        # RSSI moyen du tag, utile pour évaluer la qualité du signal.
        st.metric("RSSI moyen", f"{tracked_row['rssi_moyen']:.2f} dBm")
    with t4:
        # Écart moyen entre lectures successives, affiché à zéro si la valeur est absente.
        st.metric("Ecart moyen", f"{0 if pd.isna(tracked_row['ecart_moyen_s']) else tracked_row['ecart_moyen_s']:.2f} s")

    # Reconstitue une série temporelle par minute pour visualiser l'activité du tag.
    tracked_series = tracked_data.set_index("timestamp_dt")["passages"].resample("1min").sum()
    if not tracked_series.empty:
        # Convertit l'index datetime en libellés plus compacts pour l'affichage.
        tracked_series.index = tracked_series.index.strftime("%Hh%M")
        # Affiche la courbe des passages minute par minute.
        st.line_chart(tracked_series.rename("passages"))

    # Prépare un tableau détaillé avec les colonnes utiles à l'analyse fine.
    tracked_display = tracked_data[["timestamp_dt", "antenna", "rssi", "passages"]].copy()
    tracked_display.rename(
        columns={
            "timestamp_dt": "Horodatage",
            "antenna": "Antenne",
            "rssi": "RSSI",
            "passages": "Passages",
        },
        inplace=True,
    )
    # Simplifie l'affichage de l'horodatage en remplaçant le séparateur principal par 'h'.
    tracked_display["Horodatage"] = tracked_display["Horodatage"].astype(str).str.replace(":", "h", n=1)

    # Affiche toutes les lectures individuelles du tag suivi avec leurs métadonnées principales.
    st.dataframe(tracked_display, use_container_width=True, hide_index=True)


# ────────────────────────────────────────────────────────────────────
# PAGE 6 : Gestion Whitelist
# ────────────────────────────────────────────────────────────────

def page_whitelist():
    """Page de gestion des EPC autorises (whitelist).
        Permet d'ajouter ou de supprimer des EPC de la whitelist, soit un par un via un formulaire, soit en important une liste d'EPC depuis un fichier JSON, 
        soit en sélectionnant des EPC déjà présents dans la whitelist pour les supprimer. Affiche aussi le contenu actuel de la whitelist et permet de l'exporter au format JSON.
        Les graphiques de la page Mode Graphique ne retiennent que les tags dont l'EPC est présent dans whitelist.json, 
        à moins que l'utilisateur choisisse d'ignorer la whitelist pour les visualisations.
    """
    st.title("✅ Gestion Whitelist")
    st.markdown("---")
    st.info("Les graphiques ne retiennent que les tags dont l'EPC est present dans whitelist.json.")
    # Affiche l'état global de l'option de filtrage par whitelist.
    if st.session_state.use_whitelist:
        st.caption("Etat global: whitelist active sur les pages de visualisation.")
    else:
        st.caption("Etat global: whitelist ignoree sur les pages de visualisation.")

    # Affiche le nombre d'EPC actuellement autorises dans la whitelist, 
    # et propose d'ajouter ou de supprimer des EPC via un formulaire ou une selection.
    # Les données sont relues ici pour refléter immédiatement toute modification persistée.
    whitelist_entries = load_whitelist_entries()
    # Indicateur global du nombre de tags autorises, pour donner une vue d'ensemble rapide de la taille de la whitelist.
    st.metric("EPC autorises", len(whitelist_entries))

    # Deux colonnes: ajout à gauche, suppression à droite.
    col_add, col_remove = st.columns(2)

    with col_add:
        # Bloc de saisie pour ajouter manuellement des EPC.
        st.subheader("Ajouter des EPC")
        # Champ facultatif pour associer un nom lisible à l'EPC.
        runner_name = st.text_input("Nom associe (optionnel)", placeholder="Coureur 42")
        # Zone multi-ligne acceptant des EPC séparés par ligne ou par virgule.
        epc_input = st.text_area(
            "Un EPC par ligne (ou separes par virgule)",
            placeholder="b'e2004703914064269d7b0114'"
        )
        # Validation de l'ajout des EPC saisis.
        if st.button("➕ Ajouter", use_container_width=True):
            # Fractionne l'entrée sur les lignes puis sur les virgules pour gérer plusieurs formats.
            parts = []
            for line in epc_input.splitlines():
                parts.extend(line.split(","))

            # Normalise chaque valeur et conserve uniquement les EPC valides.
            to_add = [normalize_epc(x) for x in parts if normalize_epc(x)]
            if not to_add:
                st.warning("Aucun EPC valide a ajouter.")
            else:
                # Copie de travail de la whitelist avant toute modification.
                updated = whitelist_entries.copy()
                for epc in to_add:
                    # Détecte si l'EPC est déjà présent pour éviter les doublons.
                    existing = next((entry for entry in updated if entry["epc"] == epc), None)
                    if existing is None:
                        # Ajoute une nouvelle entrée avec le nom fourni, s'il existe.
                        updated.append({"epc": epc, "name": runner_name.strip()})
                    elif runner_name.strip():
                        # Met à jour le nom si l'EPC existe déjà et qu'un nom a été saisi.
                        existing["name"] = runner_name.strip()
                # Persiste la whitelist modifiée sur disque.
                save_whitelist_entries(updated)
                st.success("Whitelist mise a jour.")
                # Recharge l'application pour refléter immédiatement le changement.
                st.rerun()

        # Permet d'importer une liste d'EPC depuis un fichier JSON, soit au format {"entries": [...]}, soit une liste brute d'EPC,
        # en fusionnant avec les EPC deja presents dans la whitelist (sans dupliquer les EPC,
        # et en mettant a jour les noms associes si un EPC existe deja mais que le fichier importé en propose un nouveau).
        # Cette importation facilite les mises à jour massives.
        uploaded = st.file_uploader("Importer whitelist JSON", type=["json"])
        if uploaded is not None:
            try:
                # Charge le contenu JSON tel qu'envoyé par l'utilisateur.
                imported = json.load(uploaded)
                if isinstance(imported, dict):
                    imported_epcs = imported.get("entries")
                    if imported_epcs is None:
                        imported_epcs = imported.get("epcs", imported.get("whitelist", []))
                elif isinstance(imported, list):
                    # Accepte également une simple liste brute.
                    imported_epcs = imported
                else:
                    # Structure inattendue: on ignore l'import.
                    imported_epcs = []

                # Prépare une copie fusionnée de la whitelist actuelle.
                merged = whitelist_entries.copy()
                for epc in imported_epcs:
                    if isinstance(epc, dict):
                        # Cas d'un objet contenant un EPC et éventuellement un nom.
                        norm = normalize_epc(epc.get("epc", ""))
                        imported_name = str(epc.get("name", "")).strip()
                    else:
                        # Cas d'une valeur EPC brute.
                        norm = normalize_epc(epc)
                        imported_name = ""
                    if norm:
                        # Recherche d'une entrée existante pour éviter les doublons.
                        existing = next((entry for entry in merged if entry["epc"] == norm), None)
                        if existing is None:
                            # Ajoute l'EPC importé s'il n'existe pas encore.
                            merged.append({"epc": norm, "name": imported_name})
                        elif imported_name:
                            # Met à jour le nom si l'import fournit une valeur plus précise.
                            existing["name"] = imported_name
                # Sauvegarde la fusion puis rafraîchit l'interface.
                save_whitelist_entries(merged)
                st.success("Whitelist importee.")
                st.rerun()
            except Exception as exc:
                # Remonte toute erreur de lecture/parsing du fichier JSON importé.
                st.error(f"Import impossible: {exc}")

    with col_remove:
        # Affiche une liste des EPC actuellement dans la whitelist avec leurs noms associes, 
        # et permet de selectionner plusieurs EPC pour les supprimer d'un coup.
        # Cet espace sert à nettoyer la whitelist ou à retirer des entrées devenues inutiles.
        st.subheader("Supprimer des EPC")
        if whitelist_entries:
            # Génère des libellés lisibles pour l'interface de sélection multiple.
            labels = [format_epc_label(entry["epc"], {entry["epc"]: entry.get("name", "")}) for entry in whitelist_entries]
            # Permet de retrouver rapidement l'EPC brut à partir du libellé affiché.
            label_to_epc = {label: entry["epc"] for label, entry in zip(labels, whitelist_entries)}
            # Sélection des EPC à retirer.
            to_remove = st.multiselect("Selection EPC a retirer", labels)
            # Bouton de confirmation de suppression.
            if st.button("🗑️ Retirer la selection", use_container_width=True):
                # Transforme les libellés sélectionnés en EPC à supprimer.
                removed_epcs = {label_to_epc[label] for label in to_remove}
                # Construit la nouvelle whitelist en conservant uniquement les entrées non sélectionnées.
                updated = [entry for entry in whitelist_entries if entry["epc"] not in removed_epcs]
                # Sauvegarde la liste mise à jour.
                save_whitelist_entries(updated)
                # Indique combien d'entrées ont été supprimées.
                st.success(f"{len(whitelist_entries) - len(updated)} EPC supprime(s).")
                # Recharge pour mettre l'affichage à jour immédiatement.
                st.rerun()
        else:
            # Message affiché quand aucune entrée n'existe encore.
            st.caption("Whitelist vide.")

        # Bouton de purge totale de la whitelist.
        if st.button("❌ Vider la whitelist", use_container_width=True):
            # Écrit une liste vide pour supprimer toutes les entrées.
            save_whitelist_entries([])
            st.success("Whitelist videe.")
            # Recharge afin d'afficher l'état vidé immédiatement.
            st.rerun()

    # Séparateur visuel de fin de section.
    st.markdown("---")
    st.subheader("Contenu actuel")
    if whitelist_entries:
        whitelist_df = pd.DataFrame(whitelist_entries)
        whitelist_df.rename(columns={"epc": "EPC", "name": "Nom"}, inplace=True)
        st.dataframe(whitelist_df, use_container_width=True, hide_index=True)
    else:
        st.info("Aucun EPC dans la whitelist.")

    st.download_button(
        "📥 Exporter whitelist.json",
        data=json.dumps({"entries": whitelist_entries}, indent=2, ensure_ascii=False),
        file_name="whitelist.json",
        mime="application/json",
    )


# ────────────────────────────────────────────────────────────────────
# NAVIGATION - Fonction principale
# ────────────────────────────────────────────────────────────────────

def main():
    # Fonction principale de l'application Streamlit.
    # Cette fonction réalise les tâches suivantes :
    # - gestion du preset à appliquer au démarrage (utilise le dernier preset utilisé si aucun
    #   preset n'est explicitement demandé),
    # - application du preset (chargement des paramètres réseau, antennes, puissances, etc.),
    # - mise à jour des préférences de stockage si elles sont définies dans le preset,
    # - construction et affichage de la barre latérale (sidebar) contenant :
    #     * titre et feedback du preset chargé,
    #     * option pour activer/ignorer la whitelist,
    #     * sélecteur de page pour naviguer entre les différentes vues de l'app,
    #     * section "À propos".
    # - routage vers la page courante (accueil, mode classique, graphique, whitelist, config,
    #   mode brut).

    # Remarque : la fonction s'appuie sur st.session_state pour persister l'état entre
    # les reruns de Streamlit (ex : preset courant, fichiers de stockage, paramètres
    # d'antenne). Elle capture et ignore certaines exceptions pour garantir la
    # robustesse au démarrage (par ex. si la récupération du dernier preset échoue).
    # Si aucun preset n'est demandé explicitement via st.session_state['preset_to_apply'],
    # on tente de récupérer le dernier preset utilisé depuis le backend/stockage
    # (connexion_decodeur.get_last_used_preset()). En cas d'erreur, on ignore et on
    # continue sans preset.
    if "preset_to_apply" not in st.session_state and not st.session_state.get("current_preset"):
        try:
            last_used = connexion_decodeur.get_last_used_preset()
        except Exception:
            last_used = None
        if last_used:
            st.session_state.preset_to_apply = last_used

    # Si un preset a été demandé (st.session_state['preset_to_apply']), on le lit,
    # on charge sa configuration via connexion_decodeur.charger_preset et on
    # applique les paramètres présents dans ce preset sur st.session_state.
    # Cela permet à la sidebar et aux pages de refléter immédiatement les valeurs
    # définies dans le preset (IP, port, antennes, puissances, etc.).
    if "preset_to_apply" in st.session_state:
        preset_to_apply = st.session_state.pop("preset_to_apply")
        cfg = connexion_decodeur.charger_preset(preset_to_apply)
        if cfg:
            # Marque le preset comme dernier utilisé 
            try:
                connexion_decodeur.set_last_used_preset(preset_to_apply)
            except Exception:
                pass

            # Applique aussi les préférences de stockage éventuelles contenues dans
            # le preset (noms de fichiers pour presets, tags, config). Ces préférences
            # sont normalisées (_normalize_json_filename) avant d'être stockées dans
            # st.session_state et provoquent l'appel à _apply_storage_paths() pour
            # que l'application utilise les nouveaux chemins.
            prefs = cfg.get("storage_preferences") if isinstance(cfg, dict) else None
            if isinstance(prefs, dict):
                # normalise pour le preset
                st.session_state.presets_filename = _normalize_json_filename(
                    prefs.get("presets_filename"),
                    st.session_state.presets_filename,
                )
                # normalise pour les tags
                st.session_state.tags_filename = _normalize_json_filename(
                    prefs.get("tags_filename"),
                    st.session_state.tags_filename,
                )
                # normalise pour la config
                st.session_state.config_filename = _normalize_json_filename(
                    prefs.get("config_filename"),
                    st.session_state.config_filename,
                )
                # met à jour les champs de saisie correspondants dans la sidebar pour refléter les nouveaux chemins de stockage
                st.session_state.presets_filename_input = st.session_state.presets_filename
                st.session_state.tags_filename_input = st.session_state.tags_filename
                st.session_state.config_filename_input = st.session_state.config_filename
                _apply_storage_paths()
                _save_active_presets_filename(st.session_state.presets_filename)

            # Paramètres par défaut pour les opérations de scan et les réglages des
            # antennes. Ces valeurs sont extraites du preset (ou remplacées par
            # des constantes DEFAULT_* si absentes) et stockées dans st.session_state
            # pour être utilisées par les différentes pages (ex : mode classique).
            st.session_state.current_preset = preset_to_apply
            st.session_state.main_ip = cfg.get("ip", DEFAULT_IP)
            st.session_state.main_port = cfg.get("port", DEFAULT_PORT)
            st.session_state.main_timeout = cfg.get("timeout", DEFAULT_TIMEOUT)
            st.session_state.main_antennas = ",".join(str(a) for a in cfg.get("antennas", DEFAULT_ANTENNAS))
            st.session_state.main_duration = cfg.get("duration", 0)
            st.session_state.global_tx = cfg.get("global_tx_power", 31.5)
            st.session_state.global_rx = cfg.get("global_rx_sensitivity", -80.0)

            # Pour chaque antenne listée dans le preset, on récupère une configuration
            # spécifique (si fournie) et on initialise les champs st.session_state
            # correspondants (puissance TX et sensibilité RX). On utilise en fallback
            # les valeurs globales définies précédemment.
            antenna_cfg = cfg.get("antenna_params", {})
            for ant in cfg.get("antennas", []):
                ant_cfg = antenna_cfg.get(str(ant), antenna_cfg.get(ant, {}))
                st.session_state[f"ant_{ant}_tx"] = ant_cfg.get("tx_power", st.session_state.global_tx)
                st.session_state[f"ant_{ant}_rx"] = ant_cfg.get("rx_sensitivity", st.session_state.global_rx)

            st.session_state.load_feedback = f"✅ '{preset_to_apply}' chargé!"

    # Construction de la sidebar : titre principal
    st.sidebar.title("🎯 Menu Principal")
    
    # Afficher le preset actuellement chargé (s'il existe) sous forme de message
    # de succès, sinon on affiche un avertissement.
    if st.session_state.current_preset:
        st.sidebar.success(f"📌 Preset: **{st.session_state.current_preset}**")
    else:
        st.sidebar.warning("⚠️ Aucun preset chargé")
    
    st.sidebar.markdown("---")

    # Case à cocher pour activer ou non l'application de la whitelist. L'état est
    # conservé dans st.session_state['use_whitelist'] et utilisé par les pages qui
    # effectuent un filtrage sur les EPC/tag.
    st.sidebar.checkbox(
        "Prendre en compte la whitelist",
        key="use_whitelist",
        help="Applique ou ignore la whitelist sur les pages qui filtrent les tags.",
    )
    # Affichage d'un petit message informatif selon l'état de la whitelist pour que l'utilisateur sache si la whitelist est active ou non.
    if st.session_state.use_whitelist:
        st.sidebar.caption("Whitelist active")
    else:
        st.sidebar.caption("Whitelist ignoree")

    st.sidebar.markdown("---")
    
    # Sélecteur de page : permet de naviguer entre les différentes vues de l'application.
    page = st.sidebar.radio(
        "Sélectionne une page:",
        ["🏠 Accueil", "🚴 Mode Classique", "📊 Mode Graphique", "✅ Whitelist", "⚙️ Configuration", "💻 Mode Brut"],
        index=0
    )
    st.sidebar.markdown("---")
    st.sidebar.subheader("ℹ️ À Propos")
    st.sidebar.info("""
    **Application RFID Reader Manager**
    - Interface Streamlit
    - Version: 1.0
    - Auteur: KRZANOWSKI Néo & STEPHAN Mathieu
    """)
    st.sidebar.markdown("---")

    # Routage : appelle la fonction correspondant à la page sélectionnée.
    # Chaque fonction page_* est responsable d'afficher son contenu dans la
    # zone principale de l'application.
    # Le fait d'avoir séparé le contenu de chaque page dans des fonctions distinctes permet de mieux organiser le code 
    # et de faciliter la maintenance/développement de chaque page indépendamment.
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
