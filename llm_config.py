"""
Configuracao central do LLM.

Por padrao usamos o Groq (free tier rapido e generoso). Se voce quiser usar
o Gemini, mude PROVIDER=google no arquivo .env.

As chaves de API sao lidas do arquivo .env (nunca coloque chave direto no codigo!).
"""

import os
from dotenv import load_dotenv

# Carrega variaveis do arquivo .env (que fica ao lado deste arquivo).
load_dotenv()

PROVIDER = os.getenv("PROVIDER", "groq").lower()


def get_llm(temperature: float = 0.0):
    """Cria e devolve o objeto de chat do LLM escolhido em PROVIDER."""
    if PROVIDER == "groq":
        from langchain_groq import ChatGroq

        if not os.getenv("GROQ_API_KEY"):
            raise RuntimeError("GROQ_API_KEY ausente. Configure no arquivo .env")
        modelo = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
        return ChatGroq(model=modelo, temperature=temperature)

    if PROVIDER == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI

        if not os.getenv("GOOGLE_API_KEY"):
            raise RuntimeError("GOOGLE_API_KEY ausente. Configure no arquivo .env")
        modelo = os.getenv("GOOGLE_MODEL", "gemini-2.5-flash")
        return ChatGoogleGenerativeAI(model=modelo, temperature=temperature)

    raise RuntimeError(f"PROVIDER desconhecido: {PROVIDER}. Use 'groq' ou 'google'.")
