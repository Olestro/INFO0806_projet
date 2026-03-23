"""Application Streamlit pour la gestion de lecteur RFID Impinj R220.

Interface complète pour:
- Gérer les presets (créer, charger, supprimer)
- Visualiser les tags RFID lus
- Lancer des scans
"""

import streamlit as st
import json
from pathlib import Path
from datetime import datetime
import time
import pandas as pd
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

# ────────────────────────────────────────────────────────────────────
# Initialisation Session State
# ────────────────────────────────────────────────────────────────────

if "current_preset" not in st.session_state:
    st.session_state.current_preset = None
if "scan_running" not in st.session_state:
    st.session_state.scan_running = False
if "tags_data" not in st.session_state:
    st.session_state.tags_data = []


# ────────────────────────────────────────────────────────────────────
# PAGE 1: Accueil - Gestion des Presets
# ────────────────────────────────────────────────────────────────────

def page_accueil():
    """Page d'accueil: gestion des presets et paramètres du lecteur."""
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
        ip_addr = st.text_input("Adresse IP :", value=DEFAULT_IP, key="main_ip")
        
        st.markdown("---")
        
        # 1. Paramétrage manuel de l'application
        with st.expander("📋 Paramétrage manuel de l'application", expanded=True):
            col_p1, col_p2 = st.columns(2)
            with col_p1:
                port = st.number_input("Port :", value=DEFAULT_PORT, min_value=1, max_value=65535)
                timeout = st.number_input("Timeout (s) :", value=DEFAULT_TIMEOUT, min_value=0.1, step=0.1)
            with col_p2:
                antennas_str = st.text_input(
                    "Antennes (séparées par virgules) :",
                    value=",".join(str(a) for a in DEFAULT_ANTENNAS),
                    placeholder="1,2,3"
                )
                duration = st.number_input("Durée du scan (s, 0=infini) :", value=0, min_value=0)
        
        st.markdown("---")
        
        # 2. Paramétrage global des antennes
        with st.expander("🌐 Paramétrage global des antennes", expanded=False):
            col_g1, col_g2 = st.columns(2)
            with col_g1:
                global_tx_power = st.slider(
                    "Tx Power Global (dBm) :",
                    min_value=0, max_value=33, value=30,
                    key="global_tx"
                )
            with col_g2:
                global_rx_sensitivity = st.slider(
                    "Rx Sensitivity Global (dBm) :",
                    min_value=-100, max_value=0, value=-60,
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
                            min_value=0, max_value=33, value=30,
                            key=f"ant_{ant}_tx"
                        )
                    with col_a2:
                        rx = st.slider(
                            f"Rx Sensitivity :",
                            min_value=-100, max_value=0, value=-60,
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
                    cfg = charger_preset(preset_name)
                    if cfg:
                        st.session_state.current_preset = preset_name
                        st.success(f"✅ '{preset_name}' chargé!")
                        st.rerun()
                
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
                                antennas, duration, False
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
                        result = run_inventory(
                            cfg["ip"], cfg["port"], cfg["timeout"],
                            cfg["antennas"], cfg["duration"],
                            cfg.get("afficher_antennes", False)
                        )
                        if result == 0:
                            st.success("✅ Scan terminé!")
                        else:
                            st.error(f"❌ Erreur (code: {result})")
                    except Exception as e:
                        st.error(f"❌ Exception: {e}")
                    finally:
                        st.session_state.scan_running = False


# ────────────────────────────────────────────────────────────────────
# PAGE 2: Visualisation des Tags RFID
# ────────────────────────────────────────────────────────────────────

def page_tags():
    """Page d'affichage des tags RFID lus."""
    st.title("📊 Tags RFID Détectés")
    st.markdown("---")
    
    # Charger les tags
    tags_data = load_json(TAGS_FILE)
    tags_list = tags_data.get("tags", [])
    
    if not tags_list:
        st.info("❌ Aucun tag détecté. Lance un scan d'abord!")
    else:
        st.success(f"✅ **{len(tags_list)}** tags détectés")
        
        # Options de filtrage
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            filter_antenna = st.multiselect(
                "Filter par antenne:",
                sorted(list(set(tag["antenna"] for tag in tags_list))),
                default=None
            )
        
        with col2:
            sort_option = st.selectbox(
                "Trier par:",
                ["Récent d'abord", "Plus ancien", "RSSI (fort)", "RSSI (faible)"]
            )
        
        with col3:
            auto_refresh = st.checkbox("Auto-refresh (5s)", value=False)
        
        with col4:
            if st.button("🗑️ Effacer les tags"):
                save_json(TAGS_FILE, {"tags": []})
                st.success("✅ Tags effacés!")
                st.rerun()
        
        # Appliquer les filtres
        filtered_tags = tags_list.copy()
        
        # Filtre antenne
        if filter_antenna:
            filtered_tags = [tag for tag in filtered_tags if tag["antenna"] in filter_antenna]
        
        # Tri
        if sort_option == "Récent d'abord":
            filtered_tags.sort(key=lambda x: x.get("client_timestamp", ""), reverse=True)
        elif sort_option == "Plus ancien":
            filtered_tags.sort(key=lambda x: x.get("client_timestamp", ""))
        elif sort_option == "RSSI (fort)":
            filtered_tags.sort(key=lambda x: int(x.get("rssi", 0)), reverse=True)
        elif sort_option == "RSSI (faible)":
            filtered_tags.sort(key=lambda x: int(x.get("rssi", 0)))
        
        st.markdown(f"#### Affichage: {len(filtered_tags)} tag(s)")
        
        # Présenter sous forme de tableau
        display_data = []
        for tag in filtered_tags:
            # Convertir le timestamp
            client_ts = tag.get("client_timestamp", "N/A")
            try:
                # Formatter le timestamp pour meilleure lisibilité
                dt = datetime.fromisoformat(client_ts)
                ts_display = dt.strftime("%H:%M:%S.%f")[:-3]
            except:
                ts_display = client_ts[-8:] if len(client_ts) > 8 else client_ts
            
            display_data.append({
                "EPC": tag.get("epc", "N/A"),
                "RSSI (dBm)": tag.get("rssi", "?"),
                "Antenne": tag.get("antenna", "?"),
                "Nombre Vues": tag.get("seen_count", 1),
                "Timestamp": ts_display
            })
        
        df = pd.DataFrame(display_data)
        st.dataframe(df, use_container_width=True, hide_index=True)
        
        # Statistiques
        st.markdown("---")
        st.subheader("📈 Statistiques")
        
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            st.metric("Total Tags Lus", len(filtered_tags))
        
        with col2:
            if filtered_tags:
                avg_rssi = sum(int(tag.get("rssi", 0)) for tag in filtered_tags) / len(filtered_tags)
                st.metric("RSSI Moyen", f"{avg_rssi:.0f} dBm")
            else:
                st.metric("RSSI Moyen", "N/A")
        
        with col3:
            if filtered_tags:
                total_views = sum(tag.get("seen_count", 1) for tag in filtered_tags)
                st.metric("Total Vues", total_views)
            else:
                st.metric("Total Vues", 0)
        
        with col4:
            if filtered_tags:
                num_antennas = len(set(tag["antenna"] for tag in filtered_tags))
                st.metric("Antennes Actives", num_antennas)
            else:
                st.metric("Antennes Actives", 0)
        
        # Export données
        st.markdown("---")
        st.subheader("📥 Export")
        
        col1, col2 = st.columns(2)
        
        with col1:
            csv = df.to_csv(index=False)
            st.download_button(
                label="📥 Télécharger en CSV",
                data=csv,
                file_name=f"rfid_tags_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv"
            )
        
        with col2:
            json_data = json.dumps(tags_list, indent=2, ensure_ascii=False)
            st.download_button(
                label="📥 Télécharger en JSON",
                data=json_data,
                file_name=f"rfid_tags_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json",
                mime="application/json"
            )
        
        # Auto-refresh
        if auto_refresh:
            time.sleep(5)
            st.rerun()


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
        ["🏠 Accueil", "📊 Tags RFID", "⚙️ Configuration"],
        index=0
    )
    
    st.sidebar.markdown("---")
    
    # Afficher la page sélectionnée
    if page == "🏠 Accueil":
        page_accueil()
    elif page == "📊 Tags RFID":
        page_tags()
    elif page == "⚙️ Configuration":
        page_configuration()
    

# ────────────────────────────────────────────────────────────────────
# Point d'entrée
# ────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    main()
