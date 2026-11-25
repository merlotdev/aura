
    
  
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

