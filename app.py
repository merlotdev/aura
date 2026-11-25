import streamlit as st
import pandas as pd
import plotly.express as px
import yfinance as yf
from datetime import date
from streamlit_gsheets import GSheetsConnection

st.set_page_config(page_title="Aura • Meu Patrimônio", page_icon="✨", layout="centered")

# --- SEGURANÇA ---
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

# --- CONEXÃO COM GOOGLE SHEETS ---
conn = st.connection("gsheets", type=GSheetsConnection)

def carregar_aba(nome_aba, colunas_padrao):
    try:
        df = conn.read(worksheet=nome_aba, ttl=0)
        df = df.dropna(how="all")
        if df.empty:
            return pd.DataFrame(columns=colunas_padrao)
        return df
    except Exception:
        return pd.DataFrame(columns=colunas_padrao)

df_carteira = carregar_aba("Carteira", ["Ativo", "Classe", "Quantidade", "Preco_Medio"])
df_colheita = carregar_aba("Colheita", ["Data", "Ativo", "Tipo", "Valor"])

# --- BUSCA DE COTAÇÕES (Cache de 15 minutos) ---
@st.cache_data(ttl=900)
def buscar_preco_atual(ativo, classe, preco_medio):
    ativo = str(ativo).strip().upper()
    if classe in ["Ações", "FIIs"]:
        ticker = f"{ativo}.SA" if not ativo.endswith(".SA") else ativo
    elif classe == "Cripto":
        ticker = f"{ativo}-BRL" if not ativo.endswith("-BRL") else ativo
    elif classe == "Exterior":
        ticker = ativo
    else:
        return float(preco_medio)
    
    try:
        dados = yf.Ticker(ticker).history(period="1d")
        if not dados.empty:
            return float(dados["Close"].iloc[-1])
    except Exception:
        pass
    return float(preco_medio)

# --- INTERFACE PRINCIPAL ---
st.title("✨ Aura")
st.caption("Acompanhamento de patrimônio e colheita de rendimentos")

aba_resumo, aba_farm, aba_colheita, aba_novo = st.tabs([
    "📊 Resumo", "🌱 Farms", "🌾 Colheita", "➕ Lançar"
])

# Processamento dos cálculos da carteira
if not df_carteira.empty:
    df_carteira["Quantidade"] = pd.to_numeric(df_carteira["Quantidade"])
    df_carteira["Preco_Medio"] = pd.to_numeric(df_carteira["Preco_Medio"])
    df_carteira["Total_Aplicado"] = df_carteira["Quantidade"] * df_carteira["Preco_Medio"]
    
    # Busca cotação atual para cada linha
    df_carteira["Preco_Atual"] = df_carteira.apply(
        lambda row: buscar_preco_atual(row["Ativo"], row["Classe"], row["Preco_Medio"]),
        axis=1
    )
    df_carteira["Saldo_Atual"] = df_carteira["Quantidade"] * df_carteira["Preco_Atual"]
    df_carteira["Lucro_R$"] = df_carteira["Saldo_Atual"] - df_carteira["Total_Aplicado"]
    df_carteira["Lucro_%"] = ((df_carteira["Preco_Atual"] / df_carteira["Preco_Medio"]) - 1) * 100

# --- ABA 1: RESUMO ---
with aba_resumo:
    if df_carteira.empty:
        st.info("Sua carteira está vazia. Vá na aba **➕ Lançar** para cadastrar seu primeiro ativo!")
    else:
        total_aplicado = df_carteira["Total_Aplicado"].sum()
        saldo_atual = df_carteira["Saldo_Atual"].sum()
        lucro_total = saldo_atual - total_aplicado
        rentabilidade_pct = ((saldo_atual / total_aplicado) - 1) * 100 if total_aplicado > 0 else 0

        col1, col2 = st.columns(2)
        with col1:
            st.metric(
                label="Patrimônio Atual",
                value=f"R$ {saldo_atual:,.2f}",
                delta=f"R$ {lucro_total:,.2f} ({rentabilidade_pct:.1f}%)"
            )
        with col2:
            st.metric(
                label="Total Investido",
                value=f"R$ {total_aplicado:,.2f}"
            )

        fig = px.pie(
            df_carteira,
            values="Saldo_Atual",
            names="Classe",
            hole=0.45,
            color_discrete_sequence=px.colors.qualitative.Pastel
        )
        fig.update_layout(margin=dict(t=20, b=20, l=20, r=20), height=300)
        st.plotly_chart(fig, use_container_width=True)

        if st.button("🔄 Atualizar Cotações Agora", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

# --- ABA 2: MEUS FARMS ---
with aba_farm:
    if df_carteira.empty:
        st.write("Nenhum ativo cadastrado ainda.")
    else:
        st.subheader("Posição Atualizada")
        tabela_exibicao = df_carteira[[
            "Ativo", "Classe", "Quantidade", "Preco_Medio", "Preco_Atual", "Saldo_Atual", "Lucro_%"
        ]].copy()
        st.dataframe(
            tabela_exibicao.style.format({
                "Quantidade": "{:.4f}",
                "Preco_Medio": "R$ {:.2f}",
                "Preco_Atual": "R$ {:.2f}",
                "Saldo_Atual": "R$ {:.2f}",
                "Lucro_%": "{:+.2f}%"
            }),
            use_container_width=True,
            hide_index=True
        )

# --- ABA 3: COLHEITA (DIVIDENDOS) ---
with aba_colheita:
    if df_colheita.empty:
        st.info("Nenhuma colheita registrada ainda. Use a aba **➕ Lançar** para registrar dividendos e rendimentos!")
    else:
        df_colheita["Valor"] = pd.to_numeric(df_colheita["Valor"])
        df_colheita["Data"] = pd.to_datetime(df_colheita["Data"])
        df_colheita["Mes"] = df_colheita["Data"].dt.strftime("%Y-%m")

        total_colhido = df_colheita["Valor"].sum()
        st.metric(label="Total Colhido (Proventos Acumulados)", value=f"R$ {total_colhido:,.2f}")

        # Gráfico de barras por mês
        resumo_mensal = df_colheita.groupby("Mes", as_index=False)["Valor"].sum().sort_values("Mes")
        fig_bar = px.bar(
            resumo_mensal,
            x="Mes",
            y="Valor",
            text_auto=".2f",
            title="Evolução Mensal da Colheita (R$)",
            color_discrete_sequence=["#77DD77"]
        )
        fig_bar.update_layout(margin=dict(t=40, b=20, l=20, r=20), height=300, xaxis_title="", yaxis_title="")
        st.plotly_chart(fig_bar, use_container_width=True)

        st.subheader("Histórico de Recebimentos")
        df_hist = df_colheita.sort_values("Data", ascending=False).copy()
        df_hist["Data"] = df_hist["Data"].dt.strftime("%d/%m/%Y")
        st.dataframe(
            df_hist[["Data", "Ativo", "Tipo", "Valor"]].style.format({"Valor": "R$ {:.2f}"}),
            use_container_width=True,
            hide_index=True
        )

# --- ABA 4: LANÇAR APORTE OU COLHEITA ---
with aba_novo:
    tipo_lancamento = st.radio(
        "O que você deseja registrar?",
        ["📈 Novo Aporte (Compra)", "🌾 Nova Colheita (Dividendo/Rendimento)"],
        horizontal=True
    )

    if tipo_lancamento == "📈 Novo Aporte (Compra)":
        with st.form("form_aporte", clear_on_submit=True):
            ativo = st.text_input("Código do Ativo (ex: PETR4, MXRF11, BTC, Tesouro Selic)").upper().strip()
            classe = st.selectbox("Classe", ["Renda Fixa", "Ações", "FIIs", "Cripto", "Exterior"])
            col1, col2 = st.columns(2)
            with col1:
                qtd = st.number_input("Quantidade", min_value=0.0001, format="%.4f", step=1.0)
            with col2:
                preco = st.number_input("Preço Pago (R$)", min_value=0.01, format="%.2f", step=10.0)
            
            salvar = st.form_submit_button("Salvar Aporte", use_container_width=True)

            if salvar and ativo:
                novo = pd.DataFrame([{
                    "Ativo": ativo,
                    "Classe": classe,
                    "Quantidade": qtd,
                    "Preco_Medio": preco
                }])
                df_final = pd.concat([df_carteira[["Ativo", "Classe", "Quantidade", "Preco_Medio"]], novo], ignore_index=True)
                conn.update(worksheet="Carteira", data=df_final)
                st.success(f"Aporte em {ativo} registrado!")
                st.rerun()

    else:
        with st.form("form_colheita", clear_on_submit=True):
            data_rec = st.date_input("Data do Recebimento", value=date.today())
            ativo_rec = st.text_input("Ativo Pagador (ex: MXRF11, PETR4, CDB)").upper().strip()
            tipo_rec = st.selectbox("Tipo de Provento", ["Dividendo", "Rendimento FII", "JCP", "Rendimento Renda Fixa", "Staking/Cripto"])
            valor_rec = st.number_input("Valor Líquido Recebido (R$)", min_value=0.01, format="%.2f", step=5.0)
            
            salvar_rec = st.form_submit_button("Salvar Colheita", use_container_width=True)

            if salvar_rec and ativo_rec:
                novo_rec = pd.DataFrame([{
                    "Data": data_rec.strftime("%Y-%m-%d"),
                    "Ativo": ativo_rec,
                    "Tipo": tipo_rec,
                    "Valor": valor_rec
                }])
                df_col_limpo = df_colheita[["Data", "Ativo", "Tipo", "Valor"]].copy()
                if not df_col_limpo.empty:
                    df_col_limpo["Data"] = pd.to_datetime(df_col_limpo["Data"]).dt.strftime("%Y-%m-%d")
                df_col_final = pd.concat([df_col_limpo, novo_rec], ignore_index=True)
                conn.update(worksheet="Colheita", data=df_col_final)
                st.success(f"Colheita de R$ {valor_rec:.2f} ({ativo_rec}) salva!")
                st.rerun()
