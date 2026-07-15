"""
Agente Tutor de Ingles — implementado com LangGraph (StateGraph).

Objetivo: automatizar o ciclo de aprendizado de vocabulario em ingles (aprender,
revisar com repeticao espacada, ler e praticar), com persistencia em SQLite.

Fluxo do grafo:

  START
    → validar_entrada                      (validacao da entrada do usuario)
    → identificar_intencao                 (LLM classifica a intencao)
    → [roteamento condicional por intencao]
        ├─ "aprender"          → consultar_dicionario → montar_flashcard
        │                        → traduzir_definicao → salvar_memoria_sqlite
        ├─ "gerar_termos"      → gerar_lista_termos → salvar_multiplos_cards
        ├─ "leitura"           → gerar_leitura
        ├─ "responder_leitura" → avaliar_leitura → salvar_vocab_leitura
        ├─ "revisar"           → buscar_cards_revisao
        ├─ "responder_card"    → avaliar_resposta
        ├─ "remover"           → contar_para_remover        (pede confirmacao)
        ├─ "confirmar_remocao" → executar_remocao           (apos "confirmar")
        ├─ "progresso"         → buscar_estatisticas
        └─ "outro"             → (direto)
    → gerar_resposta_final                 (resposta estruturada ao usuario)
    → END
  (entrada invalida → gerar_resposta_erro → END)

Memoria:
- Curto prazo: estado compartilhado do grafo (EstadoAgente) + sessoes mantidas
  entre turnos no loop principal (fila de revisao, contexto de leitura,
  confirmacao de remocao).
- Longo prazo: banco SQLite (dados/ingles.db) com cards, agendamento SRS,
  acertos/erros e tema — persiste entre execucoes.

Ferramentas integradas:
- Free Dictionary API (dicionario_en_server): definicoes, exemplos, sinonimos,
  IPA e audio reais em ingles.
- SQLite (ingles_server): CRUD de flashcards + agendamento por repeticao espacada.

Uso:
    python agente_langgraph.py
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import unicodedata
from typing import Any, TypedDict

from dotenv import load_dotenv
from langgraph.graph import END, StateGraph

load_dotenv()

# Suprime logs HTTP do httpx
logging.getLogger("httpx").setLevel(logging.WARNING)

# ─── Importa funcoes dos servidores ──────────────────────────────────────────
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "servers"))
import dicionario_en_server as dic  # noqa: E402
import ingles_server as cards_db  # noqa: E402

# ─── LLM ─────────────────────────────────────────────────────────────────────
from llm_config import get_llm  # noqa: E402

llm = get_llm()


# ═══════════════════════════════════════════════════════════════════════════════
# ESTADO DO GRAFO
# ═══════════════════════════════════════════════════════════════════════════════

class EstadoAgente(TypedDict, total=False):
    entrada: str
    entrada_valida: bool
    erro_validacao: str
    intencao: str  # aprender | gerar_termos | revisar | responder_card | progresso | outro
    palavra: str
    tema: str  # tema para gerar lista de termos
    dados_dicionario: dict[str, Any]
    flashcard: dict[str, Any]
    resultado_sqlite: str
    termos_gerados: list[dict]  # lista de cards gerados para um tema
    texto_leitura: str  # texto gerado para pratica de leitura
    contexto_leitura: str  # texto+perguntas que o aluno esta respondendo (turno 2)
    feedback_leitura: str  # feedback do LLM sobre as respostas do aluno
    vocab_leitura: list[dict]  # palavras-chave do texto para virar cards
    cards_revisao: list[dict]
    filtrado_por_tema: bool  # se a revisao foi filtrada por um tema
    card_em_revisao: dict  # card que está sendo revisado
    qtd_para_remover: int  # quantos cards seriam removidos
    remocao_pendente: str  # tema pendente de confirmacao ('*' = todos)
    resultado_remocao: str
    avaliacao: dict  # resultado da avaliação da resposta
    estatisticas: dict[str, Any]
    resposta_final: str


# ═══════════════════════════════════════════════════════════════════════════════
# NOS DO GRAFO
# ═══════════════════════════════════════════════════════════════════════════════

def validar_entrada(state: EstadoAgente) -> EstadoAgente:
    """Valida a entrada do usuario: nao pode ser vazia nem muito longa."""
    entrada = (state.get("entrada") or "").strip()
    if not entrada:
        return {**state, "entrada_valida": False, "erro_validacao": "Entrada vazia."}
    if len(entrada) > 500:
        return {**state, "entrada_valida": False,
                "erro_validacao": "Entrada muito longa (max 500 caracteres)."}
    return {**state, "entrada": entrada, "entrada_valida": True, "erro_validacao": ""}


async def identificar_intencao(state: EstadoAgente) -> EstadoAgente:
    """Usa o LLM para classificar a intencao do usuario."""

    # Se existe card_em_revisao, qualquer entrada é resposta ao card
    if state.get("card_em_revisao"):
        return {**state, "intencao": "responder_card"}

    # Se existe contexto_leitura, a entrada sao as respostas as perguntas do texto
    if state.get("contexto_leitura"):
        return {**state, "intencao": "responder_leitura"}

    # Se ha uma remocao aguardando confirmacao, a entrada e a resposta (sim/nao)
    if state.get("remocao_pendente"):
        return {**state, "intencao": "confirmar_remocao"}

    prompt = f"""Voce e um classificador de intencao para um tutor de ingles.
Retorne APENAS um JSON puro (sem markdown, sem explicacao).

Categorias:
- "aprender": o usuario quer aprender UMA palavra especifica em ingles.
  Extraia a palavra no campo "palavra".
- "gerar_termos": o usuario quer aprender VARIOS termos sobre um tema/contexto.
  Ex: "termos sobre java", "vocabulario de programacao", "palavras sobre viagem".
  Extraia o tema no campo "tema".
- "leitura": o usuario quer LER um TEXTO/historia/paragrafo em ingles para praticar
  leitura (nao e flashcard). Palavras-chave: "texto", "leitura", "historia",
  "paragrafo", "ler", "reading". Extraia o assunto no campo "tema".
- "revisar": quer praticar/revisar os flashcards (SRS). Palavras-chave: "revisar",
  "praticar cards", "estudar meus cards".
- "remover": quer APAGAR/EXCLUIR/DELETAR cards. Se mencionar um tema, extraia no
  campo "tema"; se for tudo, deixe "tema" vazio.
- "progresso": quer ver estatisticas.
- "outro": qualquer outra coisa.

Exemplos:
- "quero aprender reliable" -> {{"intencao":"aprender","palavra":"reliable","tema":""}}
- "o que significa deadline?" -> {{"intencao":"aprender","palavra":"deadline","tema":""}}
- "termos sobre java" -> {{"intencao":"gerar_termos","palavra":"","tema":"linguagem de programacao java"}}
- "aprender termos sobre java" -> {{"intencao":"gerar_termos","palavra":"","tema":"linguagem de programacao java"}}
- "vocabulario de cozinha" -> {{"intencao":"gerar_termos","palavra":"","tema":"cozinha/culinaria"}}
- "retorne um texto relacionado a java" -> {{"intencao":"leitura","palavra":"","tema":"java"}}
- "quero praticar leitura sobre viagem" -> {{"intencao":"leitura","palavra":"","tema":"viagem"}}
- "me da um texto pra ler" -> {{"intencao":"leitura","palavra":"","tema":""}}
- "praticar sobre saudacoes" -> {{"intencao":"gerar_termos","palavra":"","tema":"saudacoes"}}
- "quero estudar palavras de negocios" -> {{"intencao":"gerar_termos","palavra":"","tema":"negocios"}}
- "bora revisar" -> {{"intencao":"revisar","palavra":"","tema":""}}
- "revisar termos sobre saudacoes" -> {{"intencao":"revisar","palavra":"","tema":"saudacoes"}}
- "revisar java" -> {{"intencao":"revisar","palavra":"","tema":"java"}}
- "apague todos os cards" -> {{"intencao":"remover","palavra":"","tema":""}}
- "quero recomecar do zero" -> {{"intencao":"remover","palavra":"","tema":""}}
- "remover cards de programacao" -> {{"intencao":"remover","palavra":"","tema":"programacao"}}
- "excluir os cards de saudacoes" -> {{"intencao":"remover","palavra":"","tema":"saudacoes"}}
- "como estou indo?" -> {{"intencao":"progresso","palavra":"","tema":""}}

REGRAS:
- Se pedir um "texto", "leitura", "historia" ou "paragrafo" para LER -> "leitura".
- Se pedir "praticar/estudar sobre <tema>", "termos", "vocabulario", "palavras
  sobre/de" + tema -> "gerar_termos".
- Se for UMA palavra especifica -> "aprender".
- Se disser "revisar" (praticar flashcards), classifique como "revisar". Se mencionar
  um tema junto ("revisar sobre X"), coloque o tema no campo "tema".

Entrada: "{state['entrada']}"

JSON:"""

    resp = await llm.ainvoke(prompt)
    texto = resp.content.strip()
    try:
        if "```" in texto:
            texto = texto.split("```")[1]
            if texto.startswith("json"):
                texto = texto[4:]
        dados = json.loads(texto)
        intencao = dados.get("intencao", "outro")
        palavra = dados.get("palavra", "")
        tema = dados.get("tema", "")
    except (json.JSONDecodeError, IndexError):
        intencao = "outro"
        palavra = ""
        tema = ""

    validos = ("aprender", "gerar_termos", "leitura", "revisar", "remover",
               "progresso", "outro")
    if intencao not in validos:
        intencao = "outro"

    return {**state, "intencao": intencao, "palavra": palavra.strip().lower(),
            "tema": tema.strip()}


def consultar_dicionario(state: EstadoAgente) -> EstadoAgente:
    """Consulta a Free Dictionary API."""
    palavra = state.get("palavra", "")
    if not palavra:
        return {**state, "dados_dicionario": {"erro": "Nenhuma palavra identificada."}}
    resultado = dic.resumo_completo(palavra)
    try:
        dados = json.loads(resultado)
    except (json.JSONDecodeError, TypeError):
        dados = {"erro": resultado}
    return {**state, "dados_dicionario": dados}


def montar_flashcard(state: EstadoAgente) -> EstadoAgente:
    """Monta o flashcard a partir dos dados do dicionario."""
    dados = state.get("dados_dicionario", {})
    if "erro" in dados:
        return {**state, "flashcard": {"erro": dados["erro"]}}
    card = {
        "word": dados.get("word", state.get("palavra", "")),
        "definition_en": dados.get("definition_en", ""),
        "example_en": dados.get("example_en", ""),
        "synonyms": dados.get("synonyms", []),
        "ipa": dados.get("ipa", ""),
        "audio": dados.get("audio", ""),
    }
    return {**state, "flashcard": card}


async def traduzir_definicao(state: EstadoAgente) -> EstadoAgente:
    """Traduz a definicao em ingles para portugues (pro verso do flashcard)."""
    card = state.get("flashcard", {})
    if "erro" in card:
        return state
    definicao = card.get("definition_en", "")
    palavra = card.get("word", "")
    if not definicao:
        return {**state, "flashcard": {**card, "meaning_pt": palavra}}

    prompt = f"""Traduza para portugues do Brasil de forma breve (max 10 palavras):
Palavra: {palavra}
Definicao em ingles: {definicao}

Traducao (apenas a traducao, sem explicacao):"""
    resp = await llm.ainvoke(prompt)
    traducao = resp.content.strip().strip('"').strip("*")
    return {**state, "flashcard": {**card, "meaning_pt": traducao}}


def salvar_memoria_sqlite(state: EstadoAgente) -> EstadoAgente:
    """Salva o flashcard no banco SQLite."""
    card = state.get("flashcard", {})
    if "erro" in card:
        return {**state, "resultado_sqlite": f"Nao salvou: {card['erro']}"}
    # Usa a definição em inglês como meaning por enquanto
    # (o campo 'meaning' no banco serve como referência pra revisão)
    resultado = cards_db.adicionar_card(
        word=card.get("word", ""),
        meaning=card.get("meaning_pt", card.get("definition_en", "sem definicao")),
        example=card.get("example_en", ""),
        ipa=card.get("ipa", ""),
        audio=card.get("audio", ""),
    )
    return {**state, "resultado_sqlite": resultado}


async def gerar_lista_termos(state: EstadoAgente) -> EstadoAgente:
    """Usa o LLM para gerar uma lista de termos em ingles sobre um tema."""
    tema = state.get("tema", "")
    prompt = f"""Gere uma lista de 5 a 8 termos/palavras em INGLES relacionados ao tema:
"{tema}"

Para cada termo, forneca:
- word: a palavra/termo em ingles
- meaning: traducao/significado em portugues (breve)
- example: uma frase curta em ingles usando o termo

Responda APENAS com um JSON array, sem markdown:
[{{"word":"...","meaning":"...","example":"..."}}, ...]"""

    resp = await llm.ainvoke(prompt)
    texto = resp.content.strip()
    try:
        if "```" in texto:
            texto = texto.split("```")[1]
            if texto.startswith("json"):
                texto = texto[4:]
        termos = json.loads(texto)
        if not isinstance(termos, list):
            termos = []
    except (json.JSONDecodeError, IndexError):
        termos = []
    return {**state, "termos_gerados": termos}


def salvar_multiplos_cards(state: EstadoAgente) -> EstadoAgente:
    """Salva varios cards de uma vez no SQLite, marcados com o tema."""
    termos = state.get("termos_gerados", [])
    tema = state.get("tema", "")
    salvos = []
    for t in termos:
        if not t.get("word"):
            continue
        cards_db.adicionar_card(
            word=t["word"],
            meaning=t.get("meaning", ""),
            example=t.get("example", ""),
            tema=tema,
        )
        salvos.append(t["word"])
    return {**state, "resultado_sqlite": f"Salvos {len(salvos)} cards: {', '.join(salvos)}"}


def _normalizar(texto: str) -> str:
    """Minuscula e sem acentos, para comparar temas de forma tolerante."""
    t = unicodedata.normalize("NFKD", (texto or "").lower())
    return "".join(c for c in t if not unicodedata.combining(c))


# Palavras que nao ajudam a identificar o tema (ignoradas no filtro de revisao)
_STOPWORDS_TEMA = {"sobre", "de", "da", "do", "a", "o", "em", "ingles", "termos",
                   "palavras", "relacionados", "relacionadas", "revisar", "vocabulario"}


async def gerar_leitura(state: EstadoAgente) -> EstadoAgente:
    """Gera um texto curto em ingles para pratica de leitura.

    Usa o tema informado e, quando possivel, o vocabulario ja salvo pelo aluno
    (cards existentes) para o texto ficar conectado ao que ele estuda.
    """
    tema = state.get("tema", "") or "cotidiano"
    try:
        cards = json.loads(cards_db.listar_cards())
        vocab = [c["frente"] for c in cards][:15] if isinstance(cards, list) else []
    except (json.JSONDecodeError, TypeError, KeyError):
        vocab = []

    dica_vocab = (f"Se fizer sentido, use algumas destas palavras que o aluno ja "
                  f"estuda: {', '.join(vocab)}." if vocab else "")

    prompt = f"""Escreva um texto curto (4 a 6 frases) em INGLES, nivel A2/B1 (simples),
sobre o tema: "{tema}". {dica_vocab}

Depois do texto, adicione a traducao em portugues e 2 perguntas curtas de
compreensao (em portugues). Use exatamente este formato markdown:

**📖 Texto (EN):**
<texto aqui>

**🇧🇷 Tradução:**
<traducao aqui>

**❓ Perguntas:**
1. ...
2. ..."""

    # Retry simples: o modelo as vezes devolve conteudo vazio (flakiness).
    texto = ""
    for _ in range(3):
        resp = await llm.ainvoke(prompt)
        texto = (resp.content or "").strip()
        if texto:
            break
    return {**state, "texto_leitura": texto}


async def avaliar_leitura(state: EstadoAgente) -> EstadoAgente:
    """Avalia as respostas do aluno as perguntas do texto e extrai vocabulario.

    Da feedback de compreensao, corrige gentilmente o ingles do aluno, e seleciona
    palavras-chave do texto para virarem flashcards.
    """
    texto = state.get("contexto_leitura", "")
    respostas = state.get("entrada", "")

    prompt = f"""Voce e um tutor de ingles. O aluno leu o texto abaixo (com perguntas de
compreensao) e enviou respostas. Avalie em PORTUGUES, de forma breve e encorajadora.

TEXTO E PERGUNTAS:
{texto}

RESPOSTAS DO ALUNO:
{respostas}

Tarefas:
1. Diga se as respostas de compreensao estao corretas (comente cada uma rapidamente).
2. Corrija gentilmente os principais erros de ingles do aluno, mostrando a frase
   corrigida (formato: "voce escreveu X -> correto: Y").
3. Selecione de 3 a 5 palavras-chave EM INGLES do texto que valem virar flashcard.

Responda APENAS com JSON valido (sem markdown):
{{"feedback": "<seu feedback em texto/markdown>", "vocabulario": [{{"word":"...","meaning":"<traducao pt>","example":"<frase em ingles>"}}]}}"""

    feedback, vocab = "", []
    for _ in range(3):
        resp = await llm.ainvoke(prompt)
        txt = (resp.content or "").strip()
        if not txt:
            continue
        try:
            if "```" in txt:
                txt = txt.split("```")[1]
                if txt.startswith("json"):
                    txt = txt[4:]
            dados = json.loads(txt)
            feedback = dados.get("feedback", "")
            vocab = dados.get("vocabulario", [])
            if feedback:
                break
        except (json.JSONDecodeError, IndexError):
            # Se nao veio JSON, usa o texto cru como feedback
            feedback = txt
            break

    if not isinstance(vocab, list):
        vocab = []
    return {**state, "feedback_leitura": feedback, "vocab_leitura": vocab}


def salvar_vocab_leitura(state: EstadoAgente) -> EstadoAgente:
    """Salva as palavras-chave extraidas do texto como flashcards (marcadas com o tema)."""
    vocab = state.get("vocab_leitura", [])
    tema = state.get("tema", "")
    salvos = []
    for v in vocab:
        if not v.get("word"):
            continue
        cards_db.adicionar_card(
            word=v["word"],
            meaning=v.get("meaning", ""),
            example=v.get("example", ""),
            tema=tema,
        )
        salvos.append(v["word"])
    return {**state, "resultado_sqlite": f"Salvos {len(salvos)} cards: {', '.join(salvos)}"}


def buscar_cards_revisao(state: EstadoAgente) -> EstadoAgente:
    """Busca os cards devidos para revisao hoje, opcionalmente filtrando por tema."""
    resultado = cards_db.cards_para_revisar(limite=100)
    try:
        cards = json.loads(resultado)
    except (json.JSONDecodeError, TypeError):
        cards = []
    if not isinstance(cards, list):
        cards = []

    # Se o aluno pediu um tema especifico, filtra os cards por tema (tolerante a
    # acentos). Se nenhum card do tema estiver devido, cai de volta pra todos.
    tema = state.get("tema", "").strip()
    filtrado_por_tema = False
    if tema and cards:
        alvo = _normalizar(tema)
        palavras = [w for w in alvo.split() if w not in _STOPWORDS_TEMA and len(w) > 2]
        if palavras:
            filtrados = [
                c for c in cards
                if any(p in _normalizar(c.get("tema", "")) for p in palavras)
            ]
            if filtrados:
                cards = filtrados
                filtrado_por_tema = True

    cards = cards[:8]
    return {**state, "cards_revisao": cards, "filtrado_por_tema": filtrado_por_tema}


def avaliar_resposta(state: EstadoAgente) -> EstadoAgente:
    """Avalia a resposta do aluno ao card em revisao (comparacao simples)."""
    card = state.get("card_em_revisao", {})
    entrada = state.get("entrada", "").lower().strip()
    verso = card.get("verso", "").lower().strip()

    # Aceita se a resposta contem o significado ou vice-versa
    acertou = (entrada in verso or verso in entrada or
               any(p.strip() in entrada for p in verso.split(",") if p.strip()))

    # Registra no SRS
    card_id = card.get("id")
    if card_id:
        cards_db.registrar_revisao(card_id, acertou)

    return {**state, "intencao": "responder_card",
            "avaliacao": {"acertou": acertou, "card": card}}


def buscar_estatisticas(state: EstadoAgente) -> EstadoAgente:
    """Busca estatisticas de estudo."""
    resultado = cards_db.estatisticas()
    try:
        dados = json.loads(resultado)
    except (json.JSONDecodeError, TypeError):
        dados = {}
    return {**state, "estatisticas": dados}


def _cards_do_tema(tema: str) -> list[dict]:
    """Retorna os cards que casam com um tema (vazio = todos)."""
    try:
        cards = json.loads(cards_db.listar_cards())
        if not isinstance(cards, list):
            return []
    except (json.JSONDecodeError, TypeError):
        return []

    tema = (tema or "").strip()
    if not tema:
        return cards
    palavras = [w for w in _normalizar(tema).split()
                if w not in _STOPWORDS_TEMA and len(w) > 2]
    if not palavras:
        return cards
    return [c for c in cards
            if any(p in _normalizar(c.get("tema", "")) for p in palavras)]


def contar_para_remover(state: EstadoAgente) -> EstadoAgente:
    """Conta quantos cards seriam removidos e prepara a confirmacao."""
    tema = state.get("tema", "").strip()
    cards = _cards_do_tema(tema)
    return {**state, "qtd_para_remover": len(cards), "tema": tema}


def executar_remocao(state: EstadoAgente) -> EstadoAgente:
    """Executa a remocao (apos confirmacao). Remove por tema ou todos."""
    tema = state.get("tema", "").strip()
    if not tema:
        resultado = cards_db.remover_todos_cards()
    else:
        cards = _cards_do_tema(tema)
        removidos = 0
        for c in cards:
            if c.get("id") is not None:
                cards_db.remover_card(c["id"])
                removidos += 1
        resultado = f"Removidos {removidos} card(s) do tema '{tema}'."
    return {**state, "resultado_remocao": resultado}


async def gerar_resposta_final(state: EstadoAgente) -> EstadoAgente:
    """Gera a resposta final em portugues."""
    intencao = state.get("intencao", "outro")
    entrada = state.get("entrada", "")

    # Leitura: o texto ja foi gerado no no gerar_leitura; passa direto (sem outra
    # chamada ao LLM, economizando tokens).
    if intencao == "leitura":
        texto = state.get("texto_leitura", "")
        if texto:
            return {**state, "resposta_final": texto}
        return {**state, "resposta_final": "Nao consegui gerar o texto agora. Tente de novo."}

    # Feedback de leitura: junta o feedback do LLM com a confirmacao dos cards salvos.
    if intencao == "responder_leitura":
        fb = state.get("feedback_leitura", "") or "Boa tentativa!"
        vocab = state.get("vocab_leitura", [])
        palavras = ", ".join(v.get("word", "") for v in vocab if v.get("word"))
        extra = f"\n\n📌 Adicionei aos seus cards: **{palavras}**" if palavras else ""
        return {**state, "resposta_final": fb + extra}

    # Remocao: pede confirmacao (operacao destrutiva). Resposta deterministica.
    if intencao == "remover":
        qtd = state.get("qtd_para_remover", 0)
        tema = state.get("tema", "").strip()
        if qtd == 0:
            alvo = f"do tema '{tema}'" if tema else ""
            return {**state, "resposta_final": f"Voce nao tem cards {alvo} para remover. 🙂"}
        alvo = f"do tema '{tema}'" if tema else "TODOS os seus cards"
        return {**state, "resposta_final":
                f"⚠️ Isso vai remover {qtd} card(s) ({alvo}). Esta acao NAO pode ser "
                f"desfeita.\nDigite 'confirmar' para apagar, ou qualquer outra coisa "
                f"para cancelar."}

    # Resultado da remocao ja confirmada.
    if intencao == "confirmar_remocao":
        return {**state, "resposta_final": "🗑️ " + state.get("resultado_remocao", "Feito.")}

    contexto_partes = [f"Entrada do usuario: {entrada}", f"Intencao: {intencao}"]

    if intencao == "aprender":
        card = state.get("flashcard", {})
        sqlite = state.get("resultado_sqlite", "")
        contexto_partes.append(f"Dados do dicionario: {json.dumps(card, ensure_ascii=False)}")
        contexto_partes.append(f"Resultado: {sqlite}")
    elif intencao == "gerar_termos":
        termos = state.get("termos_gerados", [])
        sqlite = state.get("resultado_sqlite", "")
        contexto_partes.append(f"Termos gerados: {json.dumps(termos, ensure_ascii=False)}")
        contexto_partes.append(f"Resultado: {sqlite}")
    elif intencao == "revisar":
        cards = state.get("cards_revisao", [])
        qtd = len(cards)
        contexto_partes.append(f"Quantidade de cards para revisar: {qtd}")
        if state.get("filtrado_por_tema"):
            contexto_partes.append(f"Filtrado pelo tema: {state.get('tema', '')}")
    elif intencao == "responder_card":
        aval = state.get("avaliacao", {})
        contexto_partes.append(f"Avaliacao: {json.dumps(aval, ensure_ascii=False)}")
    elif intencao == "progresso":
        stats = state.get("estatisticas", {})
        contexto_partes.append(f"Estatisticas: {json.dumps(stats, ensure_ascii=False)}")

    contexto = "\n".join(contexto_partes)

    prompt = f"""Voce e um tutor de ingles amigavel que fala em portugues do Brasil.
Gere uma resposta breve e util com base no contexto.

Se "aprender": confirme o card salvo (palavra, definicao, exemplo, sinonimos, IPA, audio).
Traduza a definicao pra portugues.

Se "gerar_termos": mostre a lista de termos salvos em formato de tabela
(palavra | significado | exemplo). Parabenize o aluno.

Se "revisar": diga de forma breve e animada que a revisao vai comecar e quantos
cards ha. Se o contexto indicar que foi filtrado por um tema, mencione o tema.
NAO pergunte sobre nenhum card especifico (isso e feito depois). Se nao houver
cards, diga que nao tem cards para revisar agora.

Se "responder_card": diga se acertou ou errou. Mostre o significado correto e o
exemplo. Se acertou, parabenize. Se errou, encoraje.

Se "progresso": resuma as estatisticas de forma motivadora.

Se "outro": explique como usar (aprender <palavra>, termos sobre <tema>, revisar, progresso).

Contexto:
{contexto}

Resposta:"""

    resp = await llm.ainvoke(prompt)
    return {**state, "resposta_final": resp.content.strip()}


# ═══════════════════════════════════════════════════════════════════════════════
# ROTEAMENTO
# ═══════════════════════════════════════════════════════════════════════════════

def rota_validacao(state: EstadoAgente) -> str:
    return "identificar_intencao" if state.get("entrada_valida") else "gerar_resposta_erro"


def rota_intencao(state: EstadoAgente) -> str:
    intencao = state.get("intencao", "outro")
    rotas = {
        "aprender": "consultar_dicionario",
        "gerar_termos": "gerar_lista_termos",
        "leitura": "gerar_leitura",
        "responder_leitura": "avaliar_leitura",
        "revisar": "buscar_cards_revisao",
        "responder_card": "avaliar_resposta",
        "remover": "contar_para_remover",
        "confirmar_remocao": "executar_remocao",
        "progresso": "buscar_estatisticas",
    }
    return rotas.get(intencao, "gerar_resposta_final")


def gerar_resposta_erro(state: EstadoAgente) -> EstadoAgente:
    erro = state.get("erro_validacao", "Entrada invalida.")
    return {**state, "resposta_final": f"⚠️ {erro} Tente novamente."}


# ═══════════════════════════════════════════════════════════════════════════════
# MONTAGEM DO GRAFO
# ═══════════════════════════════════════════════════════════════════════════════

def criar_grafo():
    """Cria e compila o StateGraph do agente."""
    grafo = StateGraph(EstadoAgente)

    # Nos
    grafo.add_node("validar_entrada", validar_entrada)
    grafo.add_node("identificar_intencao", identificar_intencao)
    grafo.add_node("consultar_dicionario", consultar_dicionario)
    grafo.add_node("montar_flashcard", montar_flashcard)
    grafo.add_node("traduzir_definicao", traduzir_definicao)
    grafo.add_node("salvar_memoria_sqlite", salvar_memoria_sqlite)
    grafo.add_node("gerar_lista_termos", gerar_lista_termos)
    grafo.add_node("salvar_multiplos_cards", salvar_multiplos_cards)
    grafo.add_node("gerar_leitura", gerar_leitura)
    grafo.add_node("avaliar_leitura", avaliar_leitura)
    grafo.add_node("salvar_vocab_leitura", salvar_vocab_leitura)
    grafo.add_node("buscar_cards_revisao", buscar_cards_revisao)
    grafo.add_node("avaliar_resposta", avaliar_resposta)
    grafo.add_node("buscar_estatisticas", buscar_estatisticas)
    grafo.add_node("contar_para_remover", contar_para_remover)
    grafo.add_node("executar_remocao", executar_remocao)
    grafo.add_node("gerar_resposta_final", gerar_resposta_final)
    grafo.add_node("gerar_resposta_erro", gerar_resposta_erro)

    # Entrada
    grafo.set_entry_point("validar_entrada")

    # Edges condicionais
    grafo.add_conditional_edges("validar_entrada", rota_validacao)
    grafo.add_conditional_edges("identificar_intencao", rota_intencao)

    # Fluxo "aprender": dicionario -> flashcard -> traduzir -> sqlite -> resposta
    grafo.add_edge("consultar_dicionario", "montar_flashcard")
    grafo.add_edge("montar_flashcard", "traduzir_definicao")
    grafo.add_edge("traduzir_definicao", "salvar_memoria_sqlite")
    grafo.add_edge("salvar_memoria_sqlite", "gerar_resposta_final")

    # Fluxo "gerar_termos": gera lista -> salva multiplos -> resposta
    grafo.add_edge("gerar_lista_termos", "salvar_multiplos_cards")
    grafo.add_edge("salvar_multiplos_cards", "gerar_resposta_final")

    # Fluxo "leitura": gera texto -> resposta
    grafo.add_edge("gerar_leitura", "gerar_resposta_final")

    # Fluxo "responder_leitura": avalia respostas -> salva vocab -> resposta
    grafo.add_edge("avaliar_leitura", "salvar_vocab_leitura")
    grafo.add_edge("salvar_vocab_leitura", "gerar_resposta_final")

    # Fluxo "revisar", "responder_card", "progresso" -> resposta
    grafo.add_edge("buscar_cards_revisao", "gerar_resposta_final")
    grafo.add_edge("avaliar_resposta", "gerar_resposta_final")
    grafo.add_edge("buscar_estatisticas", "gerar_resposta_final")

    # Fluxo "remover": conta -> pede confirmacao | confirma -> executa
    grafo.add_edge("contar_para_remover", "gerar_resposta_final")
    grafo.add_edge("executar_remocao", "gerar_resposta_final")

    # Fim
    grafo.add_edge("gerar_resposta_final", END)
    grafo.add_edge("gerar_resposta_erro", END)

    return grafo.compile()


# ═══════════════════════════════════════════════════════════════════════════════
# LOOP PRINCIPAL
# ═══════════════════════════════════════════════════════════════════════════════

import random  # noqa: E402

# Frases variadas para a pergunta do card ficar mais natural (menos robotica)
_FRASES_CARD = [
    "Como se diz **{p}** em portugues?",
    "O que significa **{p}**?",
    "Qual a traducao de **{p}**?",
    "Voce lembra o que quer dizer **{p}**?",
    "Traduza para mim: **{p}**.",
]


def _pergunta_card(card: dict, primeiro: bool = False) -> str:
    """Formata a pergunta de um card na sessao de revisao, de forma mais natural."""
    frase = random.choice(_FRASES_CARD).format(p=card.get("frente", "?"))
    if primeiro:
        frase += ("\n_(responda o significado — a revisao segue automaticamente. "
                  "Digite 'parar' quando quiser encerrar.)_")
    return frase


# Palavras que encerram a sessao de revisao
_PARAR_REVISAO = {"parar", "pausar", "chega", "encerrar", "parar revisao",
                  "sair da revisao", "stop"}


async def main():
    app = criar_grafo()

    print("=" * 60)
    print("  Tutor de Ingles — Agente LangGraph")
    print("=" * 60)
    print("Comandos:")
    print("  'aprender <palavra>'         - salva um card com definicao real")
    print("  'termos sobre <tema>'        - gera varios cards de um tema")
    print("  'texto sobre <tema>'         - gera um texto em ingles pra leitura")
    print("  'revisar'                    - pratica flashcards (SRS)")
    print("  'progresso'                  - mostra estatisticas")
    print("  'sair'                       - encerra")
    print()

    # ── Estado das sessoes (memoria de curto prazo, entre turnos) ──
    fila_revisao: list[dict] = []   # cards ainda nao revisados nesta sessao
    card_atual: dict | None = None  # card aguardando resposta do aluno
    contexto_leitura: str | None = None  # texto aguardando respostas do aluno
    remocao_pendente = False        # ha uma remocao aguardando confirmacao?
    remocao_tema = ""               # tema da remocao pendente

    _CONFIRMAR = {"confirmar", "confirmo", "sim", "pode", "apagar", "isso", "s"}

    while True:
        try:
            entrada = input("Voce: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAte mais!")
            break
        if entrada.lower() in {"sair", "exit", "quit"}:
            print("Ate mais!")
            break
        if not entrada:
            continue

        # ── MODO REVISAO: se ha um card aguardando, a entrada e a resposta ──
        if card_atual is not None:
            # Comando para encerrar a revisao no meio
            if entrada.lower() in _PARAR_REVISAO:
                card_atual = None
                fila_revisao = []
                print("Tutor: Revisao encerrada. Bom trabalho! 👏\n")
                continue

            # Avalia a resposta ao card atual (via grafo)
            resultado = await app.ainvoke(
                {"entrada": entrada, "card_em_revisao": card_atual}
            )
            feedback = resultado.get("resposta_final", "")

            # Puxa o proximo card da fila (revisao continua)
            if fila_revisao:
                card_atual = fila_revisao.pop(0)
                proxima = _pergunta_card(card_atual)
                print(f"Tutor: {feedback}\n\n{proxima}\n")
            else:
                card_atual = None
                print(f"Tutor: {feedback}\n\n🎉 Voce revisou todos os cards de "
                      f"hoje! Excelente trabalho!\n")
            continue

        # ── CONFIRMACAO DE REMOCAO: entrada e "sim/confirmar" ou cancela ──
        if remocao_pendente:
            if entrada.lower() in _CONFIRMAR:
                resultado = await app.ainvoke(
                    {"entrada": entrada, "remocao_pendente": True, "tema": remocao_tema}
                )
                print(f"Tutor: {resultado.get('resposta_final', 'Feito.')}\n")
            else:
                print("Tutor: Remocao cancelada. Seus cards estao intactos. 👍\n")
            remocao_pendente = False
            remocao_tema = ""
            continue

        # ── MODO LEITURA: se ha um texto aguardando, a entrada sao as respostas ──
        if contexto_leitura is not None:
            if entrada.lower() in _PARAR_REVISAO:
                contexto_leitura = None
                print("Tutor: Sem problema, seguimos! O que quer fazer agora?\n")
                continue
            resultado = await app.ainvoke(
                {"entrada": entrada, "contexto_leitura": contexto_leitura}
            )
            contexto_leitura = None  # consome (1 rodada de feedback)
            print(f"Tutor: {resultado.get('resposta_final', '...')}\n")
            continue

        # ── FLUXO NORMAL (aprender, termos, leitura, revisar, progresso, outro) ──
        resultado = await app.ainvoke({"entrada": entrada})
        intencao = resultado.get("intencao")

        # Se iniciou revisao e ha cards, monta a fila e ja apresenta o 1o card
        if intencao == "revisar" and resultado.get("cards_revisao"):
            fila_revisao = list(resultado["cards_revisao"])
            card_atual = fila_revisao.pop(0)
            intro = resultado.get("resposta_final", "Vamos revisar!")
            print(f"Tutor: {intro}\n\n{_pergunta_card(card_atual, primeiro=True)}\n")

        # Se gerou um texto de leitura, guarda o contexto para avaliar as respostas
        elif intencao == "leitura" and resultado.get("texto_leitura"):
            contexto_leitura = resultado["texto_leitura"]
            texto = resultado.get("resposta_final", "")
            print(f"Tutor: {texto}\n\n_(Responda as perguntas que eu te dou feedback "
                  "e salvo as palavras-chave nos seus cards. Ou digite 'pular'.)_\n")

        # Se pediu remocao e ha cards a remover, aguarda confirmacao
        elif intencao == "remover" and resultado.get("qtd_para_remover", 0) > 0:
            remocao_pendente = True
            remocao_tema = resultado.get("tema", "")
            print(f"Tutor: {resultado.get('resposta_final', '')}\n")

        else:
            print(f"Tutor: {resultado.get('resposta_final', '...')}\n")


if __name__ == "__main__":
    asyncio.run(main())
