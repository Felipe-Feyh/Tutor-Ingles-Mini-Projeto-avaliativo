"""
Servidor MCP "DicionarioEN" — dicionario de INGLES real, via Free Dictionary API.

API publica e gratuita (sem chave): https://dictionaryapi.dev
Endpoint: https://api.dictionaryapi.dev/api/v2/entries/en/<palavra>

Tools:
  - definir(word)               -> definicoes (por classe gramatical) + exemplos
  - sinonimos_antonimos(word)   -> sinonimos e antonimos
  - fonetica(word)              -> transcricao fonetica (IPA) + link de audio

Por que isso e util no tutor de ingles: em vez de o LLM "chutar" definicoes, ele
consulta um dicionario de verdade (grounding). Otimo para criar flashcards com
definicoes e exemplos reais, e para o aluno ver a pronuncia (IPA/audio).
"""

from mcp.server.fastmcp import FastMCP
import requests
import json
import os
import sys
import webbrowser

mcp = FastMCP("DicionarioEN")

_URL = "https://api.dictionaryapi.dev/api/v2/entries/en/"
_TIMEOUT = 15

# Pasta onde os audios de pronuncia sao baixados.
_BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_AUDIO_DIR = os.path.join(_BASE, "dados", "audio")


def _buscar(word: str):
    """Consulta a API. Retorna a lista de entradas (dict) ou uma string de erro."""
    palavra = word.strip().lower()
    if not palavra:
        return "Erro: informe uma palavra."
    try:
        resp = requests.get(_URL + palavra, timeout=_TIMEOUT)
    except Exception as e:  # noqa: BLE001
        return f"Erro de conexao: {e}"
    if resp.status_code == 404:
        return f"Palavra '{palavra}' nao encontrada no dicionario de ingles."
    if resp.status_code != 200:
        return f"Erro da API (HTTP {resp.status_code})."
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return "Erro: resposta invalida da API."


@mcp.tool()
def definir(word: str) -> str:
    """Retorna as definicoes em ingles de uma palavra, agrupadas por classe gramatical.

    Use para saber o significado de uma palavra em ingles, com exemplos de uso reais.
    Ex: 'o que significa reliable?', 'define the word deadline'.
    """
    dados = _buscar(word)
    if isinstance(dados, str):
        return dados

    resultado = {"word": dados[0].get("word", word), "meanings": []}
    for entrada in dados:
        for m in entrada.get("meanings", []):
            defs = []
            for d in m.get("definitions", [])[:3]:  # no maximo 3 por classe
                item = {"definition": d.get("definition", "")}
                if d.get("example"):
                    item["example"] = d["example"]
                defs.append(item)
            if defs:
                resultado["meanings"].append(
                    {"partOfSpeech": m.get("partOfSpeech", ""), "definitions": defs}
                )
    if not resultado["meanings"]:
        return f"Sem definicoes disponiveis para '{word}'."
    return json.dumps(resultado, ensure_ascii=False, indent=2)


@mcp.tool()
def sinonimos_antonimos(word: str) -> str:
    """Retorna sinonimos e antonimos (em ingles) de uma palavra.

    Use quando o aluno quiser palavras parecidas ou opostas em ingles.
    Ex: 'sinonimos de happy', 'qual o oposto de reliable?'.
    """
    dados = _buscar(word)
    if isinstance(dados, str):
        return dados

    sinonimos, antonimos = set(), set()
    for entrada in dados:
        for m in entrada.get("meanings", []):
            sinonimos.update(m.get("synonyms", []))
            antonimos.update(m.get("antonyms", []))
            for d in m.get("definitions", []):
                sinonimos.update(d.get("synonyms", []))
                antonimos.update(d.get("antonyms", []))

    if not sinonimos and not antonimos:
        return f"Sem sinonimos/antonimos disponiveis para '{word}'."
    return json.dumps(
        {"word": word.lower().strip(),
         "synonyms": sorted(sinonimos)[:15],
         "antonyms": sorted(antonimos)[:15]},
        ensure_ascii=False, indent=2,
    )


@mcp.tool()
def fonetica(word: str) -> str:
    """Retorna a transcricao fonetica (IPA) e um link de audio da pronuncia, se houver.

    Use quando o aluno quiser saber como se pronuncia uma palavra em ingles.
    """
    dados = _buscar(word)
    if isinstance(dados, str):
        return dados

    ipa, audio = "", ""
    for entrada in dados:
        if not ipa and entrada.get("phonetic"):
            ipa = entrada["phonetic"]
        for p in entrada.get("phonetics", []):
            if not ipa and p.get("text"):
                ipa = p["text"]
            if not audio and p.get("audio"):
                audio = p["audio"]
    if not ipa and not audio:
        return f"Sem dados de pronuncia para '{word}'."
    return json.dumps(
        {"word": word.lower().strip(), "ipa": ipa or "(nao disponivel)",
         "audio": audio or "(nao disponivel)"},
        ensure_ascii=False, indent=2,
    )


def _extrair_ipa_audio(dados):
    """Extrai (ipa, audio_url) de uma resposta da API."""
    ipa, audio = "", ""
    for entrada in dados:
        if not ipa and entrada.get("phonetic"):
            ipa = entrada["phonetic"]
        for p in entrada.get("phonetics", []):
            if not ipa and p.get("text"):
                ipa = p["text"]
            if not audio and p.get("audio"):
                audio = p["audio"]
    return ipa, audio


@mcp.tool()
def tocar_pronuncia(word: str) -> str:
    """Baixa e TOCA o audio da pronuncia de uma palavra em ingles no computador.

    Use quando o aluno quiser OUVIR como se fala uma palavra.
    Baixa o mp3 para dados/audio/ e abre no player padrao do sistema.
    """
    dados = _buscar(word)
    if isinstance(dados, str):
        return dados
    _, audio = _extrair_ipa_audio(dados)
    if not audio:
        return f"Sem audio de pronuncia disponivel para '{word}'."

    try:
        os.makedirs(_AUDIO_DIR, exist_ok=True)
        caminho = os.path.join(_AUDIO_DIR, f"{word.lower().strip()}.mp3")
        if not os.path.exists(caminho):
            r = requests.get(audio, timeout=_TIMEOUT)
            r.raise_for_status()
            with open(caminho, "wb") as f:
                f.write(r.content)
        # Toca no player padrao do SO (nao bloqueia).
        if sys.platform.startswith("win"):
            os.startfile(caminho)  # type: ignore[attr-defined]
        else:
            webbrowser.open(f"file://{caminho}")
        return f"Tocando a pronuncia de '{word}'. Arquivo: {caminho}"
    except Exception as e:  # noqa: BLE001
        # Se nao conseguir tocar localmente, ao menos devolve o link.
        return f"Nao consegui tocar o audio ({e}). Ouca aqui: {audio}"


@mcp.tool()
def resumo_completo(word: str) -> str:
    """Retorna TUDO sobre uma palavra em ingles numa unica consulta.

    Ideal para montar um flashcard completo de uma vez: junta a definicao (em
    ingles), um exemplo real, sinonimos, a transcricao fonetica (IPA) e o link do
    audio. Use quando o aluno pedir para 'aprender' uma palavra.
    """
    dados = _buscar(word)
    if isinstance(dados, str):
        return dados

    definicao, exemplo, classe = "", "", ""
    sinonimos = set()
    # 1) colhe sinonimos de todos os sentidos
    for entrada in dados:
        for m in entrada.get("meanings", []):
            sinonimos.update(m.get("synonyms", []))
            for d in m.get("definitions", []):
                sinonimos.update(d.get("synonyms", []))
    # 2) prefere um sentido que tenha EXEMPLO (definicao e exemplo do mesmo sentido)
    for entrada in dados:
        for m in entrada.get("meanings", []):
            for d in m.get("definitions", []):
                if d.get("example"):
                    definicao = d.get("definition", "")
                    exemplo = d["example"]
                    classe = m.get("partOfSpeech", "")
                    break
            if exemplo:
                break
        if exemplo:
            break
    # 3) se nenhum sentido tinha exemplo, usa a primeira definicao disponivel
    if not definicao:
        for entrada in dados:
            for m in entrada.get("meanings", []):
                if m.get("definitions"):
                    definicao = m["definitions"][0].get("definition", "")
                    classe = m.get("partOfSpeech", "")
                    break
            if definicao:
                break

    ipa, audio = _extrair_ipa_audio(dados)

    return json.dumps(
        {"word": dados[0].get("word", word),
         "partOfSpeech": classe,
         "definition_en": definicao,
         "example_en": exemplo,
         "synonyms": sorted(sinonimos)[:8],
         "ipa": ipa,
         "audio": audio},
        ensure_ascii=False, indent=2,
    )


if __name__ == "__main__":
    mcp.run(transport="stdio")
