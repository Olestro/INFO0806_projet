import ipaddress
import streamlit as st


def inject_styles() -> None:
    st.markdown(
        """
        <style>
            .sidebar-logo {
                margin-top: 3rem;
                padding-top: 1rem;
                border-top: 1px solid rgba(255, 255, 255, 0.35);
                text-align: center;
                font-weight: 700;
                letter-spacing: 1px;
            }
            .section-box {
                border: 1px solid #d9d9d9;
                border-radius: 10px;
                padding: 1rem;
                margin-bottom: 1rem;
                background: #fafafa;
            }
            .antenna-title {
                font-weight: 600;
                margin-bottom: 0.5rem;
            }
            .presets-box {
                border: 1px solid #d9d9d9;
                border-radius: 10px;
                padding: 1rem;
                background: #fafafa;
            }
            .subheader {
                font-size: 1.05rem;
                font-weight: 700;
                margin-top: 0.5rem;
                margin-bottom: 0.5rem;
            }
            .divider {
                margin: 0.8rem 0 1rem 0;
                border-top: 1px solid #e4e4e4;
            }
        </style>
        """,
        unsafe_allow_html=True,
    )


def afficher_sidebar() -> None:
    with st.sidebar:
        st.button("Accueil", use_container_width=True)
        st.button("Mode Classique", use_container_width=True)
        st.button("Mode Course", use_container_width=True)
        st.button("Mode Graphique", use_container_width=True)
        st.button("Mode Brut", use_container_width=True)

        st.markdown("\n\n\n")
        st.markdown('<div class="sidebar-logo">[LOGO]</div>', unsafe_allow_html=True)


def afficher_antenne(index: int, tx_default: float = 30.0, rx_default: float = -70.0) -> dict:
    st.markdown('<div class="section-box">', unsafe_allow_html=True)
    st.markdown(f'<div class="antenna-title">Puissance Antenne {index}</div>', unsafe_allow_html=True)

    col_tx, col_rx = st.columns(2)
    with col_tx:
        tx_power = st.number_input(
            "Tx Power",
            min_value=0.0,
            max_value=33.0,
            value=tx_default,
            step=0.5,
            key=f"tx_{index}",
        )
    with col_rx:
        rx_sensitivity = st.number_input(
            "Rx Sensitivity",
            min_value=-100.0,
            max_value=0.0,
            value=rx_default,
            step=1.0,
            key=f"rx_{index}",
        )

    st.markdown("</div>", unsafe_allow_html=True)
    return {"antenne": index, "tx_power": tx_power, "rx_sensitivity": rx_sensitivity}


def ip_est_valide(ip: str) -> bool:
    if not ip.strip():
        return False
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def main() -> None:
    st.set_page_config(page_title="Paramétrage Antennes", layout="wide")
    inject_styles()
    afficher_sidebar()

    st.markdown("<h1 style='text-align: center;'>Accueil - Paramétrage</h1>", unsafe_allow_html=True)
    adresse_ip = st.text_input("Adresse IP", value="10.42.0.15", placeholder="Ex: 192.168.1.100")

    st.markdown('<div class="subheader">Paramétrage</div>', unsafe_allow_html=True)
    btn_col1, btn_col2, btn_col3 = st.columns(3)
    with btn_col1:
        st.button("Paramétrage manuel de l’application", use_container_width=True)
    with btn_col2:
        st.button("Paramétrage global des antennes", use_container_width=True)
    with btn_col3:
        st.button("Paramétrage individuelle des antennes", use_container_width=True)

    st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

    gauche, droite = st.columns([3, 1])

    with gauche:
        st.subheader("Paramètres des antennes")
        config_antennes = []
        for i in range(1, 5):
            config_antennes.append(afficher_antenne(i))

    with droite:
        st.markdown('<div class="presets-box">', unsafe_allow_html=True)
        st.subheader("Presets")

        st.markdown("**Charger un preset**")
        st.selectbox("Liste", ["Preset 1", "Preset 2", "Preset 3", "Preset 4"], key="load_preset")

        st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

        st.markdown("**Sauvegarder un preset**")
        st.text_input("Nom du preset", value="Preset 1", key="save_preset_name")

        st.markdown('<div class="divider"></div>', unsafe_allow_html=True)

        st.markdown("**Supprimer un preset**")
        st.text_input("Nom du preset à supprimer", value="Preset 1", key="delete_preset_name")

        st.markdown("</div>", unsafe_allow_html=True)

    if st.button("Appliquer les paramètres", type="primary"):
        if not ip_est_valide(adresse_ip):
            st.error("Adresse IP invalide. Merci de saisir une IPv4 ou IPv6 valide.")
        else:
            st.success("Paramètres appliqués (simulation UI uniquement).")
            st.info(f"Adresse IP : {adresse_ip}")
            st.write("Configuration antennes :", config_antennes)


if __name__ == "__main__":
    main()
