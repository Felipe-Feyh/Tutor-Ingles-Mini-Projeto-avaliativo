# Registro de Prompts — Tutor de Inglês (Agente LangGraph)

Autor: Felipe Feyh

Este documento registra os principais prompts utilizados no agente, tanto os que
rodam **em produção** (dentro dos nós do grafo) quanto os usados para **planejar e
evoluir** o projeto com apoio de IA.

---

## 1. Classificação de intenção

**Nó:** `identificar_intencao` (`agente_langgraph.py`)
**Objetivo:** decidir o caminho do grafo e extrair `palavra`/`tema`.

```text
Voce e um classificador de intencao para um tutor de ingles.
Retorne APENAS um JSON puro (sem markdown, sem explicacao).

Categorias:
- "aprender": aprender UMA palavra especifica em ingles (extraia "palavra").
- "gerar_termos": aprender VARIOS termos sobre um tema (extraia "tema").
- "leitura": LER um texto/historia em ingles (extraia "tema").
- "revisar": praticar os flashcards (SRS). Se citar um tema, extraia "tema".
- "remover": apagar/excluir cards (tema opcional; vazio = todos).
- "progresso": ver estatisticas.
- "outro": qualquer outra coisa.

[+ exemplos few-shot para cada categoria]

Entrada: "{entrada do usuario}"
JSON:
```

**Por quê:** JSON puro facilita o parsing; os exemplos (few-shot) melhoram a precisão
em modelos menores; o campo `tema` habilita gerar/revisar/remover por assunto.

---

## 2. Tradução da definição (verso do card)

**Nó:** `traduzir_definicao`

```text
Traduza para portugues do Brasil de forma breve (max 10 palavras):
Palavra: {palavra}
Definicao em ingles: {definicao}
Traducao (apenas a traducao, sem explicacao):
```

**Por quê:** o dicionário retorna definição em inglês; o verso do flashcard fica mais
útil com uma tradução curta em português.

---

## 3. Geração de termos por tema

**Nó:** `gerar_lista_termos`

```text
Gere uma lista de 5 a 8 termos/palavras em INGLES relacionados ao tema: "{tema}".
Para cada termo: word (ingles), meaning (pt breve), example (frase em ingles).
Responda APENAS com um JSON array, sem markdown:
[{"word":"...","meaning":"...","example":"..."}, ...]
```

---

## 4. Geração de texto de leitura

**Nó:** `gerar_leitura`

```text
Escreva um texto curto (4 a 6 frases) em INGLES, nivel A2/B1, sobre o tema "{tema}".
[Se possível, use palavras que o aluno já estuda.]
Depois, adicione a traducao em portugues e 2 perguntas de compreensao.
Formato: **📖 Texto (EN):** ... **🇧🇷 Tradução:** ... **❓ Perguntas:** 1... 2...
```

**Por quê:** conecta a leitura ao vocabulário já salvo pelo aluno (contexto).

---

## 5. Avaliação da leitura + extração de vocabulário

**Nó:** `avaliar_leitura`

```text
O aluno leu o texto e respondeu às perguntas. Avalie em PORTUGUES, breve:
1. Diga se as respostas de compreensao estao corretas.
2. Corrija gentilmente os erros de ingles ("voce escreveu X -> correto: Y").
3. Selecione 3 a 5 palavras-chave EM INGLES do texto para virar flashcard.
Responda APENAS com JSON:
{"feedback": "...", "vocabulario": [{"word":"...","meaning":"...","example":"..."}]}
```

---

## 6. Geração da resposta final

**Nó:** `gerar_resposta_final`

```text
Voce e um tutor de ingles amigavel que fala portugues do Brasil.
Gere uma resposta breve e util com base no contexto (varia por intenção:
aprender / gerar_termos / revisar / responder_card / progresso / outro).
Contexto: {dados acumulados no estado}
Resposta:
```

**Por quê:** o LLM só formata a resposta **depois** que as ferramentas rodaram, usando
dados reais do dicionário/SQLite — evitando alucinação. Casos determinísticos
(leitura, remoção) têm resposta montada em código, sem chamada extra ao LLM.

---

## 7. Prompts usados no planejamento (conversa com IA)

- "preciso fazer o exercício com todos os TODOs" — ponto de partida (aula MCP)
- "sinto falta de algo mais do mundo real que resolva um problema do cotidiano"
- "seria interessante ter algo como cards para memorização, leitura, etc."
- "como operacionalizar isso para o contexto de um agente?"
- "encontre um MCP online que possamos plugar na solução de idiomas"
- "monte um MCP local que consome a Free Dictionary API"
- Feedback do professor: definiu o grafo
  `START → validar_entrada → identificar_intencao → consultar_dicionario →
  montar_flashcard → salvar_memoria_sqlite → gerar_resposta_final → END`
- "refatore para usar LangGraph (StateGraph)"
- "a revisão deveria seguir para o próximo card até eu pedir para parar"
- "no fluxo de leitura, dê feedback e adicione as palavras trabalhadas aos cards"
- "permita remover todos os cards ou só os de um tema"

---

## 8. Observações

- Vários nós usam **retry** (até 3x) para lidar com respostas vazias ocasionais do
  modelo, e o parsing de JSON é tolerante (remove blocos markdown).
- A separação entre classificar (prompt 1) e responder (prompt 6) evita que o modelo
  "pule etapas" antes de as ferramentas rodarem.
- Nenhum prompt contém chaves de API ou dados sensíveis.
