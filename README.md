# Fogo Cruzado — base de ocorrências

Job semanal no Railway que mantém a tabela `ocorrencias` do Postgres alinhada com o
**CSV** de ocorrências do site https://api.fogocruzado.org.br/search
(estado → [cidade] → tipo "Completo" → BUSCAR → CSV). Tudo é feito pela interface do site
(Playwright); as conferências usam o total que o próprio site informa ao clicar em BUSCAR.

## Como a atualização funciona (modo incremental, o padrão)

1. Na primeira execução, com a tabela vazia, carrega a **base inicial**
   (`base_inicial/fogo_cruzado_2026-10-06.csv.gz`: 68.921 ocorrências, carga completa
   cidade por cidade feita em 06/10/2026).
2. Para cada estado, pega a **maior data** na base e baixa pelo site, num único download
   por estado (todas as cidades), o período de **maior data − 60 dias até hoje**.
3. Confere cada arquivo com o total do site.
4. Numa transação: insere as novas, atualiza as existentes (pelo `id`) e remove as que
   sumiram do site dentro do período. Se algo falhar, nada é alterado e o job sai com erro.
5. **Conferência geral**: total de cada estado no site × na base. Diferença (correções
   ou remoções antigas, fora da janela de 60 dias) fica registrada como aviso em
   `execucoes.detalhe` — sinal para rodar uma carga completa.

**Carga completa** (`python atualizar_base.py --completo`): baixa todo o histórico cidade por
cidade (~40 min) e substitui a tabela inteira.

## Arquivos

| Arquivo | O que faz |
|---|---|
| `atualizar_base.py` | Fluxo de atualização (incremental ou `--completo`) e gravação no Postgres. |
| `rpa_fogo_cruzado.py` | RPA: download por cidade, por estado+período, e contagem por estado. Espera 65s quando a API do site limita requisições (429; limite de 60/min). |
| `base_inicial/` | Base inicial comprimida, usada só quando a tabela está vazia. |
| `Dockerfile` | Python 3.12 + Playwright/Chromium. |

## Tabelas

- `ocorrencias`: uma linha por ocorrência (`id` é a chave), as 32 colunas do CSV com tipos
  (`data` em horário de Brasília, booleanos, inteiros) + `atualizado_em`.
- `execucoes`: histórico de cada rodada (`status` sucesso/falha, total da base, `detalhe`
  com avisos, `relatorio` jsonb com novas/atualizadas/removidas por estado).

## Railway (projeto `fogo-cruzado`, workspace Enter Apps)

- Serviço `atualizador-semanal`, deploy a partir deste repositório (branch `main`).
- Variáveis: `DATABASE_URL = ${{Postgres.DATABASE_URL}}` e `SLACK_WEBHOOK_URL` (webhook de entrada
  do Slack; cada execução posta o título "Fogo Cruzado", a tabela Estado | Última data | Novos
  eventos e a data/hora da atualização — ou o motivo, se falhar)
- Cron: `0 9 * * 1` (UTC) = toda segunda às 06:00 em Brasília. Restart policy: `NEVER`.

## Rodar localmente

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt && .venv/bin/playwright install chromium
DATABASE_URL=postgresql://... .venv/bin/python atualizar_base.py              # incremental
DATABASE_URL=postgresql://... .venv/bin/python atualizar_base.py --completo   # carga completa
.venv/bin/python rpa_fogo_cruzado.py --estado Bahia                            # só o RPA, por cidade
```
