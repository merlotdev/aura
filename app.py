from __future__ import annotations

import hashlib
import math
import uuid
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import pandas as pd
import plotly.express as px
import requests
import streamlit as st
import yfinance as yf
from streamlit_gsheets import GSheetsConnection


# =============================================================================
# CONFIGURAÇÃO
# =============================================================================
st.set_page_config(
    page_title="Aura • Meu Patrimônio",
    page_icon="✨",
    layout="wide",
)

SHEET_CARTEIRA = "Carteira"
SHEET_COLHEITA = "Colheita"

COLUNAS_CARTEIRA = [
    "ID",
    "Data_Aporte",
    "Ativo",
    "Classe",
    "Quantidade",
    "Preco_Medio",
    "Indexador",
    "Percentual_Indice",
    "Observacoes",
]

COLUNAS_COLHEITA = ["ID", "Data", "Ativo", "Tipo", "Valor"]

CLASSES = ["Renda Fixa", "Ações", "FIIs", "Cripto", "Exterior"]
INDEXADORES = ["Manual", "CDI", "Selic"]

BCB_API = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{codigo}/dados"
FUSO_BRASIL = ZoneInfo("America/Sao_Paulo")


def hoje_brasil() -> date:
    return datetime.now(FUSO_BRASIL).date()


def data_preenchida(valor: object) -> bool:
    """Retorna False também para pandas.NaT (que pode se comportar como datetime)."""
    try:
        return not bool(pd.isna(valor))
    except (TypeError, ValueError):
        return False


def formatar_data_segura(valor: object, formato: str = "%d/%m/%Y", ausente: str = "Sem data") -> str:
    """Formata datas sem chamar strftime em NaT ou em valores inválidos."""
    if not data_preenchida(valor):
        return ausente
    try:
        return pd.Timestamp(valor).strftime(formato)
    except (TypeError, ValueError, OverflowError):
        return ausente


class ErroLeituraPlanilha(RuntimeError):
    """Erro de leitura: nunca deve ser tratado como planilha vazia."""


class ErroDadosBCB(RuntimeError):
    """Erro ao consultar ou interpretar dados do Banco Central."""


# =============================================================================
# AUTENTICAÇÃO
# =============================================================================
def autenticar() -> None:
    if "autenticado" not in st.session_state:
        st.session_state.autenticado = False

    try:
        senha_correta = st.secrets["APP_PASSWORD"]
    except Exception:
        st.error("Configure APP_PASSWORD nos Secrets do Streamlit antes de executar o Aura.")
        st.stop()

    if not st.session_state.autenticado:
        st.title("✨ Aura")
        st.caption("Acesso privado ao seu painel patrimonial")
        senha = st.text_input("Digite sua senha de acesso", type="password")
        if st.button("Entrar", type="primary", use_container_width=True):
            if senha and senha == senha_correta:
                st.session_state.autenticado = True
                st.rerun()
            st.error("Senha incorreta.")
        st.stop()


# =============================================================================
# GOOGLE SHEETS — LEITURA E GRAVAÇÃO SEGURAS
# =============================================================================
def conectar_google_sheets() -> GSheetsConnection:
    try:
        return st.connection("gsheets", type=GSheetsConnection)
    except Exception as exc:
        st.error(f"Não foi possível conectar ao Google Sheets: {exc}")
        st.stop()


def ler_aba(conn: GSheetsConnection, nome_aba: str, colunas: list[str]) -> pd.DataFrame:
    """Lê uma aba. Erro de conexão é propagado e não vira DataFrame vazio."""
    try:
        df = conn.read(worksheet=nome_aba, ttl=0)
    except Exception as exc:
        raise ErroLeituraPlanilha(
            f"Falha ao ler a aba '{nome_aba}'. Nenhum dado será gravado para evitar sobrescrita. Detalhe: {exc}"
        ) from exc

    if df is None:
        df = pd.DataFrame(columns=colunas)
    else:
        df = df.dropna(how="all").copy()

    for coluna in colunas:
        if coluna not in df.columns:
            df[coluna] = pd.NA

    return df[colunas].copy()


def id_legado_estavel(row: pd.Series, indice: int) -> str:
    """Cria ID repetível para linhas antigas até que sejam gravadas com ID."""
    conteudo = "|".join(
        "" if pd.isna(row.get(c)) else str(row.get(c))
        for c in ["Ativo", "Classe", "Quantidade", "Preco_Medio", "Data_Aporte"]
    )
    digest = hashlib.sha256(f"{indice}|{conteudo}".encode("utf-8")).hexdigest()[:16]
    return f"LEG-{digest}"


def normalizar_carteira(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for coluna in COLUNAS_CARTEIRA:
        if coluna not in df.columns:
            df[coluna] = pd.NA

    # Linhas antigas não tinham ID. O ID legado é estável enquanto a ordem
    # da planilha não for alterada e será persistido na próxima gravação.
    ids = []
    for indice, row in df.iterrows():
        valor_id = row.get("ID")
        if pd.isna(valor_id) or not str(valor_id).strip():
            ids.append(id_legado_estavel(row, int(indice)))
        else:
            ids.append(str(valor_id).strip())
    df["ID"] = ids

    df["Ativo"] = df["Ativo"].fillna("").astype(str).str.strip().str.upper()
    df["Classe"] = df["Classe"].fillna("").astype(str).str.strip()
    df["Indexador"] = df["Indexador"].fillna("Manual").astype(str).str.strip()
    df["Observacoes"] = df["Observacoes"].fillna("").astype(str)

    df["Quantidade"] = pd.to_numeric(df["Quantidade"], errors="coerce")
    df["Preco_Medio"] = pd.to_numeric(df["Preco_Medio"], errors="coerce")
    df["Percentual_Indice"] = pd.to_numeric(df["Percentual_Indice"], errors="coerce")
    df["Percentual_Indice"] = df["Percentual_Indice"].fillna(100.0)
    df["Data_Aporte"] = pd.to_datetime(df["Data_Aporte"], errors="coerce").dt.date

    # Compatibilidade com os registros da versão anterior.
    df.loc[df["Indexador"].eq(""), "Indexador"] = "Manual"
    return df[COLUNAS_CARTEIRA]


def normalizar_colheita(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    for coluna in COLUNAS_COLHEITA:
        if coluna not in df.columns:
            df[coluna] = pd.NA

    ids = []
    for indice, row in df.iterrows():
        valor_id = row.get("ID")
        if pd.isna(valor_id) or not str(valor_id).strip():
            base = "|".join(str(row.get(c, "")) for c in ["Data", "Ativo", "Tipo", "Valor"])
            digest = hashlib.sha256(f"{indice}|{base}".encode("utf-8")).hexdigest()[:16]
            ids.append(f"LEG-{digest}")
        else:
            ids.append(str(valor_id).strip())
    df["ID"] = ids

    df["Data"] = pd.to_datetime(df["Data"], errors="coerce").dt.date
    df["Ativo"] = df["Ativo"].fillna("").astype(str).str.strip().str.upper()
    df["Tipo"] = df["Tipo"].fillna("").astype(str).str.strip()
    df["Valor"] = pd.to_numeric(df["Valor"], errors="coerce")
    return df[COLUNAS_COLHEITA]


def preparar_gravacao(df: pd.DataFrame, tipo: str) -> pd.DataFrame:
    """Converte datas/ausências em valores serializáveis para o Sheets."""
    resultado = df.copy()
    if tipo == "carteira":
        resultado = resultado[COLUNAS_CARTEIRA]
        resultado["Data_Aporte"] = resultado["Data_Aporte"].map(
            lambda valor: formatar_data_segura(valor, "%Y-%m-%d", "")
        )
    else:
        resultado = resultado[COLUNAS_COLHEITA]
        resultado["Data"] = resultado["Data"].map(
            lambda valor: formatar_data_segura(valor, "%Y-%m-%d", "")
        )
    return resultado.where(pd.notna(resultado), "")


def gravar_aba(conn: GSheetsConnection, nome_aba: str, df: pd.DataFrame, tipo: str) -> None:
    """Atualiza a aba somente com DataFrame já lido com sucesso nesta sessão."""
    dados = preparar_gravacao(df, tipo)
    try:
        conn.update(worksheet=nome_aba, data=dados)
    except Exception as exc:
        raise RuntimeError(
            f"Não foi possível gravar a aba '{nome_aba}'. Confira permissões e conexão. Detalhe: {exc}"
        ) from exc


# =============================================================================
# API OFICIAL DO BANCO CENTRAL — SGS
# =============================================================================
@st.cache_data(ttl=24 * 60 * 60, show_spinner=False)
def buscar_serie_bcb(codigo: int, data_inicio_iso: str, data_fim_iso: str) -> pd.DataFrame:
    """Consulta série diária no SGS, em blocos inferiores ao limite de 10 anos."""
    inicio = date.fromisoformat(data_inicio_iso)
    fim = date.fromisoformat(data_fim_iso)
    if inicio > fim:
        return pd.DataFrame(columns=["Data", "Taxa"])

    partes: list[pd.DataFrame] = []
    cursor = inicio
    headers = {"User-Agent": "AuraPatrimonio/2.0 (consulta de dados publicos)"}

    while cursor <= fim:
        fim_bloco = min(cursor + timedelta(days=9 * 365), fim)
        params = {
            "formato": "json",
            "dataInicial": cursor.strftime("%d/%m/%Y"),
            "dataFinal": fim_bloco.strftime("%d/%m/%Y"),
        }
        url = BCB_API.format(codigo=codigo)
        try:
            resposta = requests.get(url, params=params, headers=headers, timeout=30)
            resposta.raise_for_status()
            payload = resposta.json()
        except (requests.RequestException, ValueError) as exc:
            raise ErroDadosBCB(f"Falha na consulta da série SGS {codigo}: {exc}") from exc

        if not isinstance(payload, list):
            raise ErroDadosBCB(f"Resposta inesperada da série SGS {codigo}.")

        if payload:
            bloco = pd.DataFrame(payload)
            if not {"data", "valor"}.issubset(bloco.columns):
                raise ErroDadosBCB(f"Formato inesperado na série SGS {codigo}.")
            bloco["Data"] = pd.to_datetime(bloco["data"], format="%d/%m/%Y", errors="coerce").dt.date
            # O SGS pode entregar decimal com vírgula ou ponto.
            bloco["Taxa"] = pd.to_numeric(
                bloco["valor"].astype(str).str.replace(",", ".", regex=False),
                errors="coerce",
            )
            partes.append(bloco[["Data", "Taxa"]].dropna())

        cursor = fim_bloco + timedelta(days=1)

    if not partes:
        return pd.DataFrame(columns=["Data", "Taxa"])

    resultado = pd.concat(partes, ignore_index=True)
    return resultado.drop_duplicates(subset=["Data"]).sort_values("Data").reset_index(drop=True)


def fator_acumulado_indice(
    data_aporte: date,
    data_final: date,
    indexador: str,
    percentual_indice: float,
) -> tuple[Optional[float], str]:
    """Acumula taxas diárias (% a.d.) do CDI/ Selic, sem impostos ou tarifas."""
    if indexador not in ("CDI", "Selic"):
        return None, "Indexador manual: sem atualização automática"
    if not data_aporte:
        return None, "Data do aporte ausente: informe a data para calcular"
    if data_aporte >= data_final:
        return 1.0, "Sem dias úteis acumulados após o aporte"

    codigo = 12 if indexador == "CDI" else 11
    inicio = data_aporte + timedelta(days=1)
    serie = buscar_serie_bcb(codigo, inicio.isoformat(), data_final.isoformat())
    if serie.empty:
        return None, f"Sem dados disponíveis para {indexador} no período"

    percentual = max(float(percentual_indice), 0.0) / 100.0
    fatores = 1.0 + (serie["Taxa"].astype(float) / 100.0) * percentual
    fator = float(math.prod(fatores.tolist()))
    return fator, f"Estimativa bruta por {indexador}; última observação: {serie['Data'].max():%d/%m/%Y}"


@st.cache_data(ttl=4 * 60 * 60, show_spinner=False)
def cotacao_brl_por_usd(data_referencia_iso: str) -> tuple[Optional[float], Optional[str]]:
    """Série SGS 1: câmbio USD venda, em BRL por USD."""
    fim = date.fromisoformat(data_referencia_iso)
    inicio = fim - timedelta(days=20)
    serie = buscar_serie_bcb(1, inicio.isoformat(), fim.isoformat())
    if serie.empty:
        return None, None
    linha = serie.iloc[-1]
    return float(linha["Taxa"]), linha["Data"].isoformat()


# =============================================================================
# COTAÇÕES DE MERCADO
# =============================================================================
@st.cache_data(ttl=15 * 60, show_spinner=False)
def buscar_cotacao_yahoo(ativo: str, classe: str) -> tuple[Optional[float], Optional[str], str]:
    ativo = str(ativo).strip().upper()
    if not ativo:
        return None, None, "Código do ativo vazio"

    if classe in ("Ações", "FIIs"):
        ticker = ativo if ativo.endswith(".SA") else f"{ativo}.SA"
    elif classe == "Cripto":
        ticker = ativo if ativo.endswith("-BRL") else f"{ativo}-BRL"
    elif classe == "Exterior":
        ticker = ativo
    else:
        return None, None, "Classe sem cotação Yahoo"

    try:
        historico = yf.Ticker(ticker).history(period="5d", auto_adjust=False)
        if historico.empty or "Close" not in historico:
            return None, None, f"Sem cotação para {ticker}"
        historico = historico.dropna(subset=["Close"])
        if historico.empty:
            return None, None, f"Cotação indisponível para {ticker}"
        valor = float(historico["Close"].iloc[-1])
        data_cotacao = historico.index[-1].date().isoformat()
        return valor, data_cotacao, f"Yahoo Finance: {ticker}"
    except Exception as exc:
        return None, None, f"Falha na cotação {ticker}: {exc}"


# =============================================================================
# PROCESSAMENTO DA CARTEIRA E CONSOLIDAÇÃO
# =============================================================================
def calcular_carteira(df_origem: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    df = df_origem.copy()
    avisos: list[str] = []
    if df.empty:
        return df, avisos

    validos = (
        df["Ativo"].ne("")
        & df["Quantidade"].notna()
        & df["Preco_Medio"].notna()
        & df["Quantidade"].gt(0)
        & df["Preco_Medio"].gt(0)
    )
    invalidos = int((~validos).sum())
    if invalidos:
        avisos.append(f"{invalidos} lançamento(s) têm ativo, quantidade ou preço inválido e foram excluídos dos cálculos.")
    df = df.loc[validos].copy()
    if df.empty:
        return df, avisos

    df["Total_Aplicado"] = df["Quantidade"] * df["Preco_Medio"]
    df["Preco_Atual"] = pd.NA
    df["Saldo_Atual"] = pd.NA
    df["Lucro_R$"] = pd.NA
    df["Lucro_%"] = pd.NA
    df["Data_Cotacao"] = ""
    df["Status_Cotacao"] = ""

    hoje = hoje_brasil()
    precisa_fx = bool(df["Classe"].eq("Exterior").any())
    fx_atual: Optional[float] = None
    fx_data: Optional[str] = None
    if precisa_fx:
        try:
            fx_atual, fx_data = cotacao_brl_por_usd(hoje.isoformat())
        except ErroDadosBCB as exc:
            avisos.append(str(exc))

    cache_indices: dict[tuple[str, str, float], tuple[Optional[float], str]] = {}

    for indice, row in df.iterrows():
        classe = str(row["Classe"])
        quantidade = float(row["Quantidade"])
        preco_medio = float(row["Preco_Medio"])
        total_aplicado = float(row["Total_Aplicado"])

        if classe == "Renda Fixa":
            aporte_data = row["Data_Aporte"]
            indexador = str(row["Indexador"] or "Manual")
            percentual_bruto = row["Percentual_Indice"]
            percentual = 100.0 if pd.isna(percentual_bruto) else float(percentual_bruto)
            if pd.isna(aporte_data):
                fator, status = None, "Data do aporte ausente; informe a data para calcular"
            elif indexador in ("CDI", "Selic"):
                chave = (aporte_data.isoformat(), indexador, percentual)
                if chave not in cache_indices:
                    try:
                        cache_indices[chave] = fator_acumulado_indice(aporte_data, hoje, indexador, percentual)
                    except ErroDadosBCB as exc:
                        cache_indices[chave] = (None, str(exc))
                fator, status = cache_indices[chave]
            else:
                fator, status = None, "Indexador manual: sem atualização automática"

            if fator is None:
                # Sem data, índice ou resposta da API não existe valor atualizado confiável.
                # Não usamos o principal como se fosse saldo atual.
                df.at[indice, "Status_Cotacao"] = status
                continue

            saldo = total_aplicado * fator
            preco_atual = saldo / quantidade
            df.at[indice, "Preco_Atual"] = preco_atual
            df.at[indice, "Saldo_Atual"] = saldo
            df.at[indice, "Lucro_R$"] = saldo - total_aplicado
            df.at[indice, "Lucro_%"] = ((saldo / total_aplicado) - 1.0) * 100.0 if total_aplicado else 0.0
            df.at[indice, "Status_Cotacao"] = status
            df.at[indice, "Data_Cotacao"] = hoje.isoformat()
            continue

        try:
            cotacao, data_cotacao, status = buscar_cotacao_yahoo(row["Ativo"], classe)
        except Exception as exc:
            cotacao, data_cotacao, status = None, None, f"Erro ao buscar cotação: {exc}"

        if cotacao is None:
            df.at[indice, "Status_Cotacao"] = status
            # Não substitui cotação desconhecida pelo preço médio de compra.
            continue

        if classe == "Exterior":
            if fx_atual is None:
                df.at[indice, "Status_Cotacao"] = "Cotação em USD, mas câmbio USD/BRL indisponível"
                continue
            cotacao *= fx_atual
            status += f" • USD/BRL {fx_atual:.4f} ({fx_data})"

        saldo = quantidade * float(cotacao)
        df.at[indice, "Preco_Atual"] = float(cotacao)
        df.at[indice, "Saldo_Atual"] = saldo
        df.at[indice, "Lucro_R$"] = saldo - total_aplicado
        df.at[indice, "Lucro_%"] = ((float(cotacao) / preco_medio) - 1.0) * 100.0 if preco_medio else 0.0
        df.at[indice, "Data_Cotacao"] = data_cotacao or ""
        df.at[indice, "Status_Cotacao"] = status

    # Conversão explícita das colunas numéricas preenchidas no loop.
    for coluna in ["Preco_Atual", "Saldo_Atual", "Lucro_R$", "Lucro_%"]:
        df[coluna] = pd.to_numeric(df[coluna], errors="coerce")

    return df, avisos


def consolidar_ativos(df_calculado: pd.DataFrame) -> pd.DataFrame:
    """Agrupa ativos iguais e calcula preço médio ponderado por quantidade."""
    if df_calculado.empty:
        return pd.DataFrame()

    df = df_calculado.copy()
    df = df[df["Saldo_Atual"].notna()].copy()
    if df.empty:
        return pd.DataFrame()

    df["Ativo_Grupo"] = df["Ativo"].astype(str).str.strip().str.upper()
    resumo = (
        df.groupby(["Ativo_Grupo", "Classe"], as_index=False)
        .agg(
            Quantidade=("Quantidade", "sum"),
            Total_Aplicado=("Total_Aplicado", "sum"),
            Saldo_Atual=("Saldo_Atual", "sum"),
        )
    )
    resumo["Preco_Medio_Ponderado"] = resumo.apply(
        lambda r: r["Total_Aplicado"] / r["Quantidade"] if r["Quantidade"] else 0.0,
        axis=1,
    )
    resumo["Lucro_R$"] = resumo["Saldo_Atual"] - resumo["Total_Aplicado"]
    resumo["Lucro_%"] = resumo.apply(
        lambda r: (r["Lucro_R$"] / r["Total_Aplicado"] * 100.0) if r["Total_Aplicado"] else 0.0,
        axis=1,
    )
    return resumo.sort_values(["Classe", "Ativo_Grupo"]).reset_index(drop=True)


def encontrar_duplicados(df: pd.DataFrame) -> pd.DataFrame:
    """Identifica aportes idênticos nos campos de negócio, ignorando o ID."""
    if df.empty:
        return pd.DataFrame()
    copia = df.copy()
    copia["Data_Comparacao"] = copia["Data_Aporte"].map(
        lambda x: formatar_data_segura(x, "%Y-%m-%d", "SEM_DATA")
    )
    copia["Quantidade_Comparacao"] = copia["Quantidade"].round(8)
    copia["Preco_Comparacao"] = copia["Preco_Medio"].round(8)
    chaves = [
        "Ativo", "Classe", "Data_Comparacao", "Quantidade_Comparacao",
        "Preco_Comparacao", "Indexador", "Percentual_Indice",
    ]
    contagem = copia.groupby(chaves, dropna=False)["ID"].transform("size")
    return copia.loc[contagem > 1, ["ID", "Data_Aporte", "Ativo", "Classe", "Quantidade", "Preco_Medio", "Indexador"]].copy()


def moeda_br(valor: Optional[float]) -> str:
    if valor is None or pd.isna(valor):
        return "Não disponível"
    texto = f"{float(valor):,.2f}"
    texto = texto.replace(",", "X").replace(".", ",").replace("X", ".")
    return f"R$ {texto}"


def formatar_tabela(df: pd.DataFrame, mapa: dict[str, str]) -> pd.io.formats.style.Styler:
    return df.style.format(mapa, na_rep="—")


# =============================================================================
# APLICAÇÃO
# =============================================================================
autenticar()
conn = conectar_google_sheets()

try:
    df_carteira_bruta = ler_aba(conn, SHEET_CARTEIRA, COLUNAS_CARTEIRA)
    df_colheita_bruta = ler_aba(conn, SHEET_COLHEITA, COLUNAS_COLHEITA)
except ErroLeituraPlanilha as exc:
    st.error(str(exc))
    st.stop()

# Se a aba está realmente vazia, as funções abaixo devolvem DataFrames vazios.
df_carteira = normalizar_carteira(df_carteira_bruta)
df_colheita = normalizar_colheita(df_colheita_bruta)

df_calculado, avisos_calculo = calcular_carteira(df_carteira)
df_consolidado = consolidar_ativos(df_calculado)

st.title("✨ Aura • Meu Patrimônio")
st.caption("Acompanhamento patrimonial, aportes, cotação e rendimentos")

with st.sidebar:
    st.subheader("Controle")
    st.caption("Cotações: Yahoo Finance • Renda fixa: Banco Central / SGS")
    if st.button("🔄 Atualizar cotações e taxas", use_container_width=True):
        buscar_cotacao_yahoo.clear()
        buscar_serie_bcb.clear()
        cotacao_brl_por_usd.clear()
        st.rerun()
    if st.button("Sair", use_container_width=True):
        st.session_state.autenticado = False
        st.rerun()

abas = st.tabs(["📊 Resumo", "🌱 Farms", "🌾 Colheita", "➕ Lançar", "🧰 Gerenciar aportes"])
aba_resumo, aba_farms, aba_colheita, aba_lancar, aba_gerenciar = abas


# =============================================================================
# ABA 1 — RESUMO
# =============================================================================
with aba_resumo:
    if avisos_calculo:
        for aviso in avisos_calculo:
            st.warning(aviso)

    if df_calculado.empty:
        st.info("Sua carteira ainda não possui aportes válidos. Use a aba **Lançar** para cadastrar o primeiro ativo.")
    else:
        total_aplicado = float(df_calculado["Total_Aplicado"].sum())
        saldo_disponivel = float(df_calculado["Saldo_Atual"].sum(min_count=1)) if df_calculado["Saldo_Atual"].notna().any() else 0.0
        linhas_sem_cotacao = int(df_calculado["Saldo_Atual"].isna().sum())
        saldo_conhecido = float(df_calculado["Saldo_Atual"].sum(skipna=True))
        lucro_conhecido = saldo_conhecido - float(
            df_calculado.loc[df_calculado["Saldo_Atual"].notna(), "Total_Aplicado"].sum()
        )
        aplicado_com_cotacao = float(
            df_calculado.loc[df_calculado["Saldo_Atual"].notna(), "Total_Aplicado"].sum()
        )
        rentabilidade_conhecida = (lucro_conhecido / aplicado_com_cotacao * 100.0) if aplicado_com_cotacao else 0.0

        c1, c2, c3 = st.columns(3)
        c1.metric("Patrimônio com valor calculado", moeda_br(saldo_conhecido))
        c2.metric("Total aplicado (linhas calculadas)", moeda_br(aplicado_com_cotacao))
        c3.metric("Resultado calculado", moeda_br(lucro_conhecido), f"{rentabilidade_conhecida:+.2f}%")

        if linhas_sem_cotacao:
            st.warning(
                f"{linhas_sem_cotacao} lançamento(s) estão sem cotação/atualização. "
                "O patrimônio acima não inclui esses valores; veja a aba Farms."
            )

        if not df_consolidado.empty:
            grafico = df_consolidado.groupby("Classe", as_index=False)["Saldo_Atual"].sum()
            fig = px.pie(
                grafico,
                values="Saldo_Atual",
                names="Classe",
                hole=0.48,
                color_discrete_sequence=px.colors.qualitative.Pastel,
            )
            fig.update_layout(margin=dict(t=20, b=20, l=20, r=20), height=360)
            st.plotly_chart(fig, use_container_width=True)

        st.caption(
            "Os valores sem cotação não são substituídos pelo preço de compra. "
            "A rentabilidade exibida considera apenas lançamentos com saldo calculado."
        )


# =============================================================================
# ABA 2 — FARMS / POSIÇÃO CONSOLIDADA
# =============================================================================
with aba_farms:
    st.subheader("Posição consolidada por ativo")
    if df_consolidado.empty:
        st.info("Ainda não há posições com saldo atualizado.")
    else:
        exibicao = df_consolidado.rename(columns={
            "Ativo_Grupo": "Ativo",
            "Preco_Medio_Ponderado": "Preço Médio Ponderado",
            "Total_Aplicado": "Total Aplicado",
            "Saldo_Atual": "Saldo Atual",
            "Lucro_R$": "Resultado (R$)",
            "Lucro_%": "Resultado (%)",
        })
        st.dataframe(
            formatar_tabela(exibicao, {
                "Quantidade": "{:,.6f}",
                "Preço Médio Ponderado": "R$ {:,.4f}",
                "Total Aplicado": "R$ {:,.2f}",
                "Saldo Atual": "R$ {:,.2f}",
                "Resultado (R$)": "R$ {:+,.2f}",
                "Resultado (%)": "{:+.2f}%",
            }),
            use_container_width=True,
            hide_index=True,
        )

    st.divider()
    st.subheader("Detalhamento dos aportes")
    if df_calculado.empty:
        st.info("Nenhum aporte válido encontrado.")
    else:
        detalhes = df_calculado[[
            "Data_Aporte", "Ativo", "Classe", "Quantidade", "Preco_Medio",
            "Total_Aplicado", "Preco_Atual", "Saldo_Atual", "Lucro_R$", "Lucro_%", "Status_Cotacao",
        ]].copy()
        detalhes["Data_Aporte"] = detalhes["Data_Aporte"].map(
            lambda x: formatar_data_segura(x, "%d/%m/%Y", "Sem data")
        )
        detalhes = detalhes.rename(columns={
            "Data_Aporte": "Data", "Preco_Medio": "Preço Médio do Lote",
            "Total_Aplicado": "Total Aplicado", "Preco_Atual": "Preço Atual",
            "Saldo_Atual": "Saldo Atual", "Lucro_R$": "Resultado (R$)",
            "Lucro_%": "Resultado (%)", "Status_Cotacao": "Status da atualização",
        })
        st.dataframe(
            formatar_tabela(detalhes, {
                "Quantidade": "{:,.6f}",
                "Preço Médio do Lote": "R$ {:,.4f}",
                "Total Aplicado": "R$ {:,.2f}",
                "Preço Atual": "R$ {:,.4f}",
                "Saldo Atual": "R$ {:,.2f}",
                "Resultado (R$)": "R$ {:+,.2f}",
                "Resultado (%)": "{:+.2f}%",
            }),
            use_container_width=True,
            hide_index=True,
        )

    st.caption(
        "Preço médio ponderado = soma (quantidade × preço pago) ÷ quantidade total. "
        "A consolidação agrupa por código do ativo e classe."
    )


# =============================================================================
# ABA 3 — COLHEITA
# =============================================================================
with aba_colheita:
    if df_colheita.empty:
        st.info("Nenhuma colheita registrada ainda. Use a aba Lançar para registrar rendimentos.")
    else:
        validos_colheita = df_colheita[df_colheita["Valor"].notna() & df_colheita["Data"].notna()].copy()
        total_colhido = float(validos_colheita["Valor"].sum()) if not validos_colheita.empty else 0.0
        st.metric("Total colhido (proventos registrados)", moeda_br(total_colhido))

        if not validos_colheita.empty:
            validos_colheita["Mes"] = pd.to_datetime(validos_colheita["Data"]).dt.strftime("%Y-%m")
            mensal = validos_colheita.groupby("Mes", as_index=False)["Valor"].sum().sort_values("Mes")
            fig_bar = px.bar(
                mensal,
                x="Mes",
                y="Valor",
                text_auto=".2f",
                title="Evolução mensal da colheita (R$)",
                color_discrete_sequence=["#77DD77"],
            )
            fig_bar.update_layout(margin=dict(t=40, b=20, l=20, r=20), height=340, xaxis_title="", yaxis_title="R$")
            st.plotly_chart(fig_bar, use_container_width=True)

            st.subheader("Histórico de recebimentos")
            historico = validos_colheita.sort_values("Data", ascending=False).copy()
            historico["Data"] = historico["Data"].map(lambda x: formatar_data_segura(x, "%d/%m/%Y", "Sem data"))
            historico = historico[["Data", "Ativo", "Tipo", "Valor"]]
            st.dataframe(
                formatar_tabela(historico, {"Valor": "R$ {:,.2f}"}),
                use_container_width=True,
                hide_index=True,
            )


# =============================================================================
# ABA 4 — LANÇAR APORTE OU COLHEITA
# =============================================================================
with aba_lancar:
    tipo_lancamento = st.radio(
        "O que deseja registrar?",
        ["Novo aporte/compra", "Nova colheita/rendimento"],
        horizontal=True,
    )

    if tipo_lancamento == "Novo aporte/compra":
        st.subheader("Registrar aporte")
        with st.form("form_aporte", clear_on_submit=True):
            ativo = st.text_input("Código ou nome do ativo (ex.: PETR4, MXRF11, BTC, AAPL, CDB Banco X)")
            classe = st.selectbox("Classe", CLASSES)
            data_aporte = st.date_input("Data do aporte", value=hoje_brasil(), max_value=hoje_brasil())

            if classe == "Renda Fixa":
                valor_aplicado = st.number_input("Valor aplicado (R$)", min_value=0.01, value=1000.0, step=100.0)
                indexador = st.selectbox("Indexador", INDEXADORES, index=1)
                percentual = st.number_input(
                    "Percentual do indexador (%)",
                    min_value=0.0,
                    max_value=500.0,
                    value=100.0,
                    step=1.0,
                    help="Ex.: CDB 110% do CDI = indexador CDI e percentual 110. Tesouro Selic: Selic e 100.",
                )
                quantidade = 1.0
                preco_pago = float(valor_aplicado)
                st.caption(
                    "Para renda fixa, cada aporte é armazenado como um lote: quantidade 1 e preço igual ao principal aplicado."
                )
            else:
                col_a, col_b = st.columns(2)
                with col_a:
                    quantidade = st.number_input("Quantidade", min_value=0.000001, value=1.0, step=1.0, format="%.6f")
                with col_b:
                    preco_pago = st.number_input("Preço pago por unidade (R$)", min_value=0.0001, value=10.0, step=1.0)
                indexador = "Manual"
                percentual = 100.0
                st.caption(
                    "Para ativos do exterior, informe o custo por unidade em reais. A cotação atual em USD será convertida para BRL pelo câmbio diário do BCB."
                )

            observacoes = st.text_input("Observações (opcional)")
            permitir_semelhante = st.checkbox("Permitir mesmo assim se já existir aporte idêntico", value=False)
            salvar_aporte = st.form_submit_button("Salvar aporte", type="primary", use_container_width=True)

        if salvar_aporte:
            ativo_limpo = ativo.strip().upper()
            if not ativo_limpo:
                st.error("Informe o código ou nome do ativo.")
            elif quantidade <= 0 or preco_pago <= 0:
                st.error("Quantidade e preço/valor aplicado precisam ser maiores que zero.")
            else:
                novo = {
                    "ID": str(uuid.uuid4()),
                    "Data_Aporte": data_aporte,
                    "Ativo": ativo_limpo,
                    "Classe": classe,
                    "Quantidade": float(quantidade),
                    "Preco_Medio": float(preco_pago),
                    "Indexador": indexador,
                    "Percentual_Indice": float(percentual),
                    "Observacoes": observacoes.strip(),
                }
                candidato = pd.DataFrame([novo])
                teste = pd.concat([df_carteira, candidato], ignore_index=True)
                duplicados = encontrar_duplicados(teste)
                novo_id = novo["ID"]
                duplicado_novo = not duplicados.empty and duplicados["ID"].eq(novo_id).any()

                if duplicado_novo and not permitir_semelhante:
                    st.error(
                        "Já existe um aporte com o mesmo ativo, classe, data, quantidade, preço e indexador. "
                        "Marque a opção de confirmação no formulário somente se essa compra realmente ocorreu."
                    )
                else:
                    try:
                        gravar_aba(conn, SHEET_CARTEIRA, teste, "carteira")
                        st.success(f"Aporte de {ativo_limpo} registrado com sucesso.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))

    else:
        st.subheader("Registrar colheita")
        with st.form("form_colheita", clear_on_submit=True):
            data_recebimento = st.date_input("Data do recebimento", value=hoje_brasil(), max_value=hoje_brasil())
            ativo_recebedor = st.text_input("Ativo pagador (ex.: MXRF11, PETR4, CDB)")
            tipo_provento = st.selectbox(
                "Tipo de provento",
                ["Dividendo", "Rendimento FII", "JCP", "Rendimento Renda Fixa", "Staking/Cripto", "Outro"],
            )
            valor_recebido = st.number_input("Valor líquido recebido (R$)", min_value=0.01, value=10.0, step=5.0)
            salvar_colheita = st.form_submit_button("Salvar colheita", type="primary", use_container_width=True)

        if salvar_colheita:
            ativo_limpo = ativo_recebedor.strip().upper()
            if not ativo_limpo:
                st.error("Informe o ativo pagador.")
            elif valor_recebido <= 0:
                st.error("O valor recebido precisa ser maior que zero.")
            else:
                novo = pd.DataFrame([{
                    "ID": str(uuid.uuid4()),
                    "Data": data_recebimento,
                    "Ativo": ativo_limpo,
                    "Tipo": tipo_provento,
                    "Valor": float(valor_recebido),
                }])
                teste = pd.concat([df_colheita, novo], ignore_index=True)
                try:
                    gravar_aba(conn, SHEET_COLHEITA, teste, "colheita")
                    st.success(f"Colheita de {moeda_br(valor_recebido)} ({ativo_limpo}) salva.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))


# =============================================================================
# ABA 5 — GERENCIAR / EXCLUIR APORTES
# =============================================================================
with aba_gerenciar:
    st.subheader("Gerenciar e excluir aportes")
    st.warning(
        "A exclusão remove permanentemente o lançamento da aba Carteira. "
        "Ela não é uma venda e não cria registro de saída; use apenas para corrigir aportes duplicados ou lançados por engano."
    )

    if df_carteira.empty:
        st.info("Não há aportes cadastrados para gerenciar.")
    else:
        duplicados = encontrar_duplicados(df_carteira)
        if duplicados.empty:
            st.success("Não foram encontrados aportes idênticos pelos campos comparados.")
        else:
            st.error(f"Foram encontrados {len(duplicados)} lançamento(s) em grupos potencialmente duplicados.")
            tabela_dup = duplicados.copy()
            tabela_dup["Data_Aporte"] = tabela_dup["Data_Aporte"].map(
                lambda x: formatar_data_segura(x, "%d/%m/%Y", "Sem data")
            )
            tabela_dup = tabela_dup.rename(columns={
                "Data_Aporte": "Data", "Preco_Medio": "Preço pago", "Quantidade": "Quantidade"
            })
            st.dataframe(
                formatar_tabela(tabela_dup[["ID", "Data", "Ativo", "Classe", "Quantidade", "Preço pago", "Indexador"]], {
                    "Quantidade": "{:,.6f}", "Preço pago": "R$ {:,.4f}"
                }),
                use_container_width=True,
                hide_index=True,
            )

        st.divider()
        st.markdown("**Backup dos aportes**")
        backup_csv = preparar_gravacao(df_carteira, "carteira").to_csv(index=False).encode("utf-8-sig")
        st.download_button(
            "Baixar carteira em CSV antes de alterar",
            data=backup_csv,
            file_name=f"aura_carteira_backup_{hoje_brasil().isoformat()}.csv",
            mime="text/csv",
            use_container_width=True,
        )

        st.divider()
        st.markdown("**Ajustar data e indexador de aportes antigos de renda fixa**")
        renda_fixa = df_carteira[df_carteira["Classe"].eq("Renda Fixa")].copy()
        if not renda_fixa.empty:
            rf_rotulos = {}
            for _, linha in renda_fixa.iterrows():
                dt_txt = formatar_data_segura(linha["Data_Aporte"], "%d/%m/%Y", "data não cadastrada")
                rf_rotulos[linha["ID"]] = f"{linha['Ativo']} • {dt_txt} • {moeda_br(linha['Quantidade'] * linha['Preco_Medio'])} • {linha['ID'][:12]}"
            rf_id = st.selectbox(
                "Selecione um aporte de renda fixa para revisar",
                options=renda_fixa["ID"].tolist(),
                format_func=lambda item_id: rf_rotulos.get(item_id, item_id),
                key="rf_editar_id",
            )
            rf_row = renda_fixa.loc[renda_fixa["ID"].eq(rf_id)].iloc[0]
            data_rf_cadastrada = data_preenchida(rf_row["Data_Aporte"])
            data_rf_atual = rf_row["Data_Aporte"] if data_rf_cadastrada else hoje_brasil()
            indice_rf = INDEXADORES.index(rf_row["Indexador"]) if rf_row["Indexador"] in INDEXADORES else 0
            with st.form("form_ajustar_renda_fixa"):
                if not data_rf_cadastrada:
                    st.warning("Este registro antigo não possui data. Não mantenha a data de hoje por engano: selecione a data original da aplicação.")
                nova_data_rf = st.date_input("Data original do aporte", value=data_rf_atual, max_value=hoje_brasil())
                confirmar_data_real = st.checkbox(
                    "Confirmo que a data selecionada é a data original do aporte.",
                    value=data_rf_cadastrada,
                    disabled=data_rf_cadastrada,
                )
                novo_indexador_rf = st.selectbox("Indexador do contrato", INDEXADORES, index=indice_rf)
                novo_pct_rf = st.number_input(
                    "Percentual do indexador (%)",
                    min_value=0.0,
                    max_value=500.0,
                    value=(100.0 if pd.isna(rf_row["Percentual_Indice"]) else float(rf_row["Percentual_Indice"])),
                    step=1.0,
                )
                salvar_ajuste_rf = st.form_submit_button("Salvar dados de renda fixa", use_container_width=True)
            if salvar_ajuste_rf:
                if not confirmar_data_real:
                    st.error("Confirme a data original antes de salvar o aporte antigo.")
                else:
                    atualizado = df_carteira.copy()
                    mascara_rf = atualizado["ID"].eq(rf_id)
                    atualizado.loc[mascara_rf, "Data_Aporte"] = nova_data_rf
                    atualizado.loc[mascara_rf, "Indexador"] = novo_indexador_rf
                    atualizado.loc[mascara_rf, "Percentual_Indice"] = float(novo_pct_rf)
                    try:
                        gravar_aba(conn, SHEET_CARTEIRA, atualizado, "carteira")
                        st.success("Dados do aporte de renda fixa atualizados.")
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
        else:
            st.caption("Não há aportes classificados como Renda Fixa.")

        st.divider()
        st.markdown("**Todos os aportes registrados**")
        tabela_todos = df_carteira[[
            "ID", "Data_Aporte", "Ativo", "Classe", "Quantidade", "Preco_Medio", "Indexador", "Percentual_Indice"
        ]].copy()
        tabela_todos["Data_Aporte"] = tabela_todos["Data_Aporte"].map(
            lambda x: formatar_data_segura(x, "%d/%m/%Y", "Sem data")
        )
        tabela_todos = tabela_todos.rename(columns={
            "Data_Aporte": "Data", "Preco_Medio": "Preço pago", "Percentual_Indice": "% do índice"
        })
        st.dataframe(tabela_todos, use_container_width=True, hide_index=True)

        rotulos = {}
        for _, row in df_carteira.iterrows():
            data_txt = formatar_data_segura(row["Data_Aporte"], "%d/%m/%Y", "sem data")
            rotulos[row["ID"]] = (
                f"{row['Ativo']} • {row['Classe']} • {data_txt} • "
                f"Qtd. {row['Quantidade']} • {moeda_br(row['Preco_Medio'])} • ID {row['ID'][:12]}"
            )

        id_selecionado = st.selectbox(
            "Selecione o lançamento que deseja excluir",
            options=df_carteira["ID"].tolist(),
            format_func=lambda item_id: rotulos.get(item_id, item_id),
        )
        confirmar_exclusao = st.checkbox("Confirmo que selecionei o aporte correto e desejo excluí-lo permanentemente.")
        if st.button(
            "🗑️ Excluir aporte selecionado",
            type="primary",
            disabled=not confirmar_exclusao,
            use_container_width=True,
        ):
            restante = df_carteira.loc[df_carteira["ID"] != id_selecionado].copy()
            if len(restante) == len(df_carteira):
                st.error("O ID selecionado não foi encontrado. Atualize a página e tente novamente.")
            else:
                try:
                    gravar_aba(conn, SHEET_CARTEIRA, restante, "carteira")
                    st.success("Aporte excluído. Os demais lançamentos foram preservados.")
                    st.rerun()
                except Exception as exc:
                    st.error(str(exc))


# =============================================================================
# NOTAS METODOLÓGICAS
# =============================================================================
with st.expander("ℹ️ Critérios de cálculo e limitações"):
    st.markdown(
        """
        **Preço médio ponderado:** soma de (quantidade × preço pago) dividida pela quantidade total,
        agrupando o mesmo código de ativo e classe.

        **CDB atrelado ao CDI:** cada taxa diária do SGS é aplicada na proporção informada
        (por exemplo, 110% do CDI = taxa diária CDI × 1,10), com capitalização composta.

        **Tesouro Selic:** o cálculo utiliza a série diária da Selic como estimativa bruta de acumulação.
        Não representa o preço oficial de venda do título no Tesouro Direto e não inclui ágio/deságio,
        marcação a mercado, taxa de custódia, taxa da instituição, IR ou IOF.

        **Renda fixa manual:** quando o indexador for Manual ou a data estiver ausente, a aplicação
        não apresenta um saldo atualizado nem inventa rendimento; preencha os dados do contrato.

        **Ativos do exterior:** o preço atual obtido em USD é convertido para BRL pela série diária
        de câmbio do BCB. O preço de compra deve ser informado em reais.

        **Cotações indisponíveis:** não são substituídas pelo preço médio. O ativo aparece como sem
        cotação e fica fora do patrimônio atualizado até que exista dado válido.

        **Importante:** o cálculo é informativo e estimado. Não substitui extratos, informes da corretora,
        cálculo tributário ou preço de resgate/venda efetivo.
        """
    )
