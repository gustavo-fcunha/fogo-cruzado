"""
Atualização semanal da base Fogo Cruzado no Postgres (job agendado no Railway).

Modo incremental (padrão, o que o Railway roda):
  1. Se a tabela `ocorrencias` estiver vazia, carrega a base inicial
     (base_inicial/*.csv.gz, a carga completa feita pelo RPA).
  2. Para cada estado, pega a maior data registrada e baixa pelo site, num único
     download por estado (todas as cidades), o período de (maior data - 60 dias) até hoje.
  3. Confere cada arquivo com o total que o próprio site informou ao clicar em BUSCAR.
  4. Numa única transação: insere as ocorrências novas, atualiza as existentes (pelo id)
     e remove as que sumiram do site dentro do período baixado.
  5. Registra a execução na tabela `execucoes`.
  Se qualquer estado falhar ou não conferir, nada é alterado e o job termina com erro.

Modo completo (--completo): baixa cidade por cidade, todo o histórico, e substitui a
tabela inteira. Útil para realinhar a base de tempos em tempos.

Variáveis: DATABASE_URL (obrigatória, conexão do Postgres) e SLACK_WEBHOOK_URL
(opcional: webhook de entrada do Slack que recebe o resumo de cada execução).
"""

import argparse
import csv
import gzip
import json
import os
import sys
import tempfile
import traceback
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import psycopg

import rpa_fogo_cruzado

JANELA_DIAS = 60  # sobreposição: pega registros atrasados e correções recentes
BASE_INICIAL = sorted((Path(__file__).parent / "base_inicial").glob("*.csv.gz"))

COLUNAS = [
    ("id", "text PRIMARY KEY"),
    ("address", "text"), ("region", "text"), ("state", "text"), ("city", "text"),
    ("neighborhood", "text"), ("sub_neighborhood", "text"), ("locality", "text"),
    ("latitude", "double precision"), ("longitude", "double precision"),
    ("data", "timestamp"),  # horário de Brasília, como o site exibe
    ("police_action", "boolean"), ("agent_presence", "boolean"),
    ("police_unit", "text"), ("main_reason", "text"), ("complementary_reason", "text"),
    ("clippings", "text"), ("massacre", "boolean"),
    *[(f"{grupo}_{tipo}", "integer")
      for grupo in ("civilians", "agents", "men", "women", "kids", "teenagers", "elderly")
      for tipo in ("dead", "wounded")],
]
NOMES = [nome for nome, _ in COLUNAS]

DDL = f"""
CREATE TABLE IF NOT EXISTS ocorrencias (
    {", ".join(f"{nome} {tipo}" for nome, tipo in COLUNAS)},
    atualizado_em timestamptz NOT NULL
);
CREATE INDEX IF NOT EXISTS ocorrencias_state_city ON ocorrencias (state, city);
CREATE INDEX IF NOT EXISTS ocorrencias_data ON ocorrencias (data);
CREATE TABLE IF NOT EXISTS execucoes (
    id serial PRIMARY KEY,
    inicio timestamptz NOT NULL,
    fim timestamptz NOT NULL,
    status text NOT NULL,              -- sucesso | falha
    total_ocorrencias integer,         -- tamanho da base ao final
    cidades integer,
    detalhe text,
    relatorio jsonb                    -- conferência por estado (incremental) ou cidade (completo)
);
"""


def converter(campo: str, valor: str):
    tipo = dict(COLUNAS)[campo]
    if valor == "" and not tipo.startswith("text"):
        return None
    if tipo == "double precision":
        return float(valor)
    if tipo == "integer":
        return int(valor)
    if tipo == "boolean":
        return valor == "true"
    if tipo == "timestamp":
        return datetime.strptime(valor, "%d/%m/%Y, %H:%M:%S")
    return valor


def ler_csv(arquivo: Path) -> list[dict]:
    abrir = gzip.open if arquivo.suffix == ".gz" else open
    with abrir(arquivo, "rt", newline="", encoding="utf-8") as f:
        leitor = csv.DictReader(f)
        if leitor.fieldnames != NOMES:
            raise RuntimeError(f"colunas do CSV mudaram no site ({arquivo.name}): {leitor.fieldnames}")
        return list(leitor)


def conferir(registros: list[dict], total_site: int, nome: str):
    """O arquivo precisa ter exatamente as ocorrências que o site disse ter."""
    ids = {r["id"] for r in registros}
    if len(ids) != total_site or len(registros) != total_site:
        raise RuntimeError(f"{nome}: arquivo com {len(registros)} linhas / {len(ids)} ids, "
                           f"site informou {total_site}")


def copiar_para_carga(cur, registros: list[dict], agora: datetime):
    """Cria a tabela temporária `carga` com os registros (vale até o fim da transação)."""
    cur.execute("CREATE TEMP TABLE IF NOT EXISTS carga (LIKE ocorrencias) ON COMMIT DROP")
    with cur.copy(f"COPY carga ({', '.join(NOMES)}, atualizado_em) FROM STDIN") as copy:
        for registro in registros:
            copy.write_row([converter(c, registro[c]) for c in NOMES] + [agora])


def substituir_tudo(conn, registros: list[dict], agora: datetime) -> int:
    with conn.cursor() as cur:
        copiar_para_carga(cur, registros, agora)
        cur.execute("DELETE FROM ocorrencias")
        cur.execute("INSERT INTO ocorrencias SELECT * FROM carga")
        return cur.rowcount


def mesclar_periodo(conn, estado: str, inicio: date, fim: date, registros: list[dict],
                    agora: datetime) -> dict:
    """Insere/atualiza os registros baixados de um estado e remove os que sumiram
    do site dentro do período. Na remoção a janela encolhe 2 dias de cada lado,
    porque o filtro de data do site tem efeito de fuso nas pontas."""
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in NOMES[1:] + ["atualizado_em"])
    with conn.cursor() as cur:
        cur.execute("DROP TABLE IF EXISTS carga")
        copiar_para_carga(cur, registros, agora)
        cur.execute("SELECT count(*) FROM carga c JOIN ocorrencias o USING (id)")
        existentes = cur.fetchone()[0]
        cur.execute(f"INSERT INTO ocorrencias SELECT * FROM carga ON CONFLICT (id) DO UPDATE SET {sets}")
        cur.execute(
            "DELETE FROM ocorrencias o WHERE o.state = %s AND o.data >= %s AND o.data < %s"
            " AND NOT EXISTS (SELECT 1 FROM carga c WHERE c.id = o.id)",
            (estado, inicio + timedelta(days=2), fim - timedelta(days=2)),
        )
        removidas = cur.rowcount
    return {"novas": len(registros) - existentes, "atualizadas": existentes, "removidas": removidas}


def registrar(conn, inicio, status, total=None, cidades=None, detalhe=None, relatorio=None):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO execucoes (inicio, fim, status, total_ocorrencias, cidades, detalhe, relatorio)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (inicio, datetime.now(timezone.utc), status, total, cidades, detalhe,
             json.dumps(relatorio, ensure_ascii=False, default=str) if relatorio is not None else None),
        )


def total_base(conn) -> tuple[int, int]:
    return conn.execute("SELECT count(*), count(DISTINCT (state, city)) FROM ocorrencias").fetchone()


def carregar_base_inicial(conn, agora: datetime):
    if not BASE_INICIAL:
        raise RuntimeError("tabela vazia e nenhuma base inicial em base_inicial/")
    arquivo = BASE_INICIAL[-1]
    registros = ler_csv(arquivo)
    with conn.transaction():
        total = substituir_tudo(conn, registros, agora)
    print(f"Base inicial carregada: {total} ocorrências de {arquivo.name}", flush=True)


def atualizar_incremental(conn, pasta: Path, agora: datetime) -> list[dict]:
    if conn.execute("SELECT NOT EXISTS (SELECT 1 FROM ocorrencias)").fetchone()[0]:
        carregar_base_inicial(conn, agora)

    maiores = dict(conn.execute("SELECT state, max(data)::date FROM ocorrencias GROUP BY state").fetchall())
    inicio_por_estado = {estado: d - timedelta(days=JANELA_DIAS) for estado, d in maiores.items()}
    fim = date.today() + timedelta(days=1)  # o filtro do site não inclui o último dia
    for estado, d in maiores.items():
        print(f"{estado}: maior data na base {d:%d/%m/%Y} → baixa de "
              f"{inicio_por_estado[estado]:%d/%m/%Y} até hoje", flush=True)

    relatorio_rpa = rpa_fogo_cruzado.executar_incremental(pasta, inicio_por_estado, fim)

    baixados, relatorio = [], []
    for estado, inicio, fim_estado, status, arquivo, total_site in relatorio_rpa:
        if status == "erro":
            raise RuntimeError(f"{estado}: download falhou — {arquivo}")
        registros = [] if status == "sem_resultados" else ler_csv(Path(arquivo))
        conferir(registros, total_site or 0, estado)
        baixados.append((estado, inicio, fim_estado, registros))

    with conn.transaction():
        for estado, inicio, fim_estado, registros in baixados:
            resultado = mesclar_periodo(conn, estado, inicio, fim_estado, registros, agora)
            relatorio.append({"estado": estado, "inicio": inicio, "fim": fim_estado,
                              "baixadas": len(registros), **resultado})
            print(f"{estado}: {len(registros)} baixadas — {resultado}", flush=True)
        total, cidades = total_base(conn)

    # Conferência geral: total de cada estado no site x na base. Diferença indica
    # correções/remoções antigas, fora da janela — sinal para rodar --completo.
    # Se a conferência falhar, a atualização (já gravada) continua valendo.
    na_base = dict(conn.execute("SELECT state, count(*) FROM ocorrencias GROUP BY state").fetchall())
    try:
        no_site = rpa_fogo_cruzado.contar_no_site([estado for estado, *_ in baixados])
    except Exception as e:  # noqa: BLE001
        no_site = None
        print(f"Conferência geral não executada: {type(e).__name__}: {e}", flush=True)
    divergencias = {e: {"site": n, "base": na_base.get(e, 0)}
                    for e, n in (no_site or {}).items() if n != na_base.get(e, 0)}
    for item in relatorio:
        item["total_base"] = na_base.get(item["estado"], 0)
        item["total_site"] = no_site.get(item["estado"]) if no_site else None
    detalhe = "incremental"
    if no_site is None:
        detalhe += " — conferência geral não executada"
    elif divergencias:
        detalhe += f" — AVISO: base diferente do site em {divergencias}; considere rodar --completo"
        print(f"AVISO: totais diferentes do site: {divergencias}", flush=True)
    else:
        print("Conferência geral: os totais de todos os estados batem com o site.", flush=True)
    registrar(conn, agora, "sucesso", total, cidades, detalhe=detalhe, relatorio=relatorio)
    return relatorio


def atualizar_completo(conn, pasta: Path, agora: datetime) -> list[dict]:
    relatorio_rpa = rpa_fogo_cruzado.executar(pasta, force=True)
    registros, relatorio = [], []
    for estado, cidade, status, arquivo, total_site in relatorio_rpa:
        if status == "erro":
            raise RuntimeError(f"{estado}/{cidade}: download falhou — {arquivo}")
        da_cidade = [] if status == "sem_resultados" else ler_csv(Path(arquivo))
        conferir(da_cidade, total_site or 0, f"{estado}/{cidade}")
        registros += da_cidade
        relatorio.append({"estado": estado, "cidade": cidade, "registros": len(da_cidade)})

    with conn.transaction():
        substituir_tudo(conn, registros, agora)
        total, cidades = total_base(conn)
        registrar(conn, agora, "sucesso", total, cidades, detalhe="completo", relatorio=relatorio)
    return relatorio


def notificar_slack(conn, inicio: datetime, relatorio=None, erro: str | None = None):
    """Resumo no Slack: título, tabela por estado (última data na base e novos
    eventos) e data/hora da atualização. Falha no envio não derruba o job."""
    webhook = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook:
        return
    try:
        ultimas = dict(conn.execute(
            "SELECT state, max(data) FROM ocorrencias GROUP BY state ORDER BY state").fetchall())
        total, _ = total_base(conn)
    except Exception:  # noqa: BLE001
        ultimas, total = {}, None
    por_estado = {item["estado"]: item for item in (relatorio or []) if "estado" in item and "novas" in item}

    linhas = [f"{'Estado':<16}{'Última data':>13}{'Novos eventos':>16}"]
    for estado in sorted(set(ultimas) | set(por_estado)):
        data = ultimas.get(estado)
        novos = por_estado.get(estado, {}).get("novas")
        data_txt = f"{data:%d/%m/%Y}" if data else "—"
        novos_txt = str(novos) if novos is not None else "—"
        linhas.append(f"{estado:<16}{data_txt:>13}{novos_txt:>16}")
    if por_estado:
        linhas.append(f"{'Total':<16}{'':>13}{sum(i['novas'] for i in por_estado.values()):>16}")

    quando = inicio.astimezone(ZoneInfo("America/Sao_Paulo"))
    rodape = f"Atualização: {quando:%d/%m/%Y às %H:%M} (Brasília)"
    if total is not None:
        rodape += f" · base com {total:,} ocorrências".replace(",", ".")
    blocos = [{"type": "header", "text": {"type": "plain_text", "text": "Fogo Cruzado"}}]
    if erro:
        blocos.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f":x: *A atualização falhou* — a base anterior foi mantida.\n`{erro[:500]}`"}})
    divergentes = [i for i in (relatorio or []) if i.get("total_site") not in (None, i.get("total_base"))]
    if divergentes:
        aviso = ", ".join(f"{i['estado']} (site {i['total_site']}, base {i['total_base']})" for i in divergentes)
        blocos.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f":warning: Totais diferentes do site: {aviso}. Vale rodar uma carga completa."}})
    blocos += [
        {"type": "section", "text": {"type": "mrkdwn", "text": "```" + "\n".join(linhas) + "```"}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": rodape}]},
    ]
    corpo = json.dumps({"text": "Fogo Cruzado — " + ("falha na atualização" if erro else "base atualizada"),
                        "blocks": blocos}).encode()
    try:
        requisicao = urllib.request.Request(webhook, data=corpo, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(requisicao, timeout=20).read()
        print("Resumo enviado ao Slack.", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"Não foi possível enviar ao Slack: {type(e).__name__}: {e}", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--completo", action="store_true",
                    help="baixa todo o histórico cidade por cidade e substitui a tabela")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        sys.exit("Defina DATABASE_URL com a conexão do Postgres.")

    inicio = datetime.now(timezone.utc)
    modo = "completo" if args.completo else "incremental"
    print(f"Atualização ({modo}) iniciada em {inicio:%Y-%m-%d %H:%M} UTC", flush=True)
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(DDL)
        pasta = Path(tempfile.mkdtemp(prefix="fogo_cruzado_"))
        try:
            if args.completo:
                relatorio = atualizar_completo(conn, pasta, inicio)
            else:
                relatorio = atualizar_incremental(conn, pasta, inicio)
            total, cidades = total_base(conn)
            print(f"\nBase atualizada: {total} ocorrências de {cidades} cidades.", flush=True)
            notificar_slack(conn, inicio, relatorio)
        except Exception as e:
            detalhe = f"{type(e).__name__}: {e}"
            registrar(conn, inicio, "falha", detalhe=f"{modo}: {detalhe}\n{traceback.format_exc()}")
            print(f"\nFALHA — base anterior mantida. {detalhe}", flush=True)
            notificar_slack(conn, inicio, erro=detalhe)
            sys.exit(1)


if __name__ == "__main__":
    main()
