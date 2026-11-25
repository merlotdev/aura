cat << "EOF" > app.py
import pandas as pd
import plotly.express as px
import streamlit as st
from streamlit_gsheets import GSheetsConnection

st.set_page_config(
    page_title="Aura • Meu Patrimônio", page_icon="✨", layout="centered"
)

# Trava de segurança simples para app na nuvem
if "autenticado" not in st.session_state:
    st.session_state.autenticado = False

if not st.session_state.autenticado:
    st.title("✨ Aura")
    senha = st.text_input("Digite sua senha de acesso:", type="password")
    if st.button("Entrar"):
        if senha == st.secrets["APP_PASSWORD"]:
            st.session_state.autenticado = True
            st.rerun()
        else:
            st.error("Senha incorreta.")
    st.stop()

# Conexão com o Google Sheets
conn = st.connection("gsheets", type=GSheetsConnection)


def carregar_dados():
    df = conn.read(ttl=0)
    return df.dropna(how="all")


df = carregar_dados()

st.title("✨ Aura")
st.caption("Seu painel de acompanhamento e colheita de rendimentos")

aba_resumo, aba_farm, aba_novo = st.tabs(
    ["📊 Resumo", "🌱 Meus Farms", "➕ Novo Aporte"]
)

with aba_resumo:
    if df.empty:
        st.info(
            "Sua carteira está vazia. Cadastre seu primeiro ativo na aba **Novo Aporte**!"
        )
    else:
        df["Quantidade"] = pd.to_numeric(df["Quantidade"])
        df["Preco_Medio"] = pd.to_numeric(df["Preco_Medio"])
        df["Total_Investido"] = df["Quantidade"] * df["Preco_Medio"]
        patrimonio_total = df["Total_Investido"].sum()

        st.metric(label="Patrimônio Aplicado", value=f"R$ {patrimonio_total:,.2f}")

        fig = px.pie(
            df,
            values="Total_Investido",
            names="Classe",
            hole=0.45,
            color_discrete_sequence=px.colors.qualitative.Pastel,
        )
        fig.update_layout(margin=dict(t=20, b=20, l=20, r=20), height=300)
        st.plotly_chart(fig, use_container_width=True)

with aba_farm:
    if df.empty:
        st.write("Nenhum ativo cadastrado ainda.")
    else:
        st.subheader("Ativos em Carteira")
        df["Total_Investido"] = df["Quantidade"] * df["Preco_Medio"]
        st.dataframe(
            df[["Ativo", "Classe", "Quantidade", "Preco_Medio", "Total_Investido"]],
            use_container_width=True,
            hide_index=True,
        )

with aba_novo:
    st.subheader("Registrar Aporte")
    with st.form("form_aporte", clear_on_submit=True):
        ativo = (
            st.text_input("Código do Ativo (ex: PETR4, MXRF11, Tesouro Selic)")
            .upper()
            .strip()
        )
        classe = st.selectbox(
            "Classe", ["Renda Fixa", "Ações", "FIIs", "Cripto", "Exterior"]
        )
        col1, col2 = st.columns(2)
        with col1:
            qtd = st.number_input(
                "Quantidade", min_value=0.0001, format="%.4f", step=1.0
            )
        with col2:
            preco = st.number_input(
                "Preço Pago (R$)", min_value=0.01, format="%.2f", step=10.0
            )

        salvar = st.form_submit_button("Salvar na Nuvem", use_container_width=True)

        if salvar and ativo:
            novo_registro = pd.DataFrame(
                [
                    {
                        "Ativo": ativo,
                        "Classe": classe,
                        "Quantidade": qtd,
                        "Preco_Medio": preco,
                    }
                ]
            )
            df_atualizado = pd.concat([df, novo_registro], ignore_index=True)
            conn.update(data=df_atualizado)
            st.success(f"{ativo} salvo na nuvem com sucesso!")
            st.rerun()
EOF
