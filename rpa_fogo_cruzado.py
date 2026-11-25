"""
RPA Fogo Cruzado — baixa o "CSV" de TODAS as cidades de TODOS os estados.

Reproduz o fluxo manual em https://api.fogocruzado.org.br/search:
  estado -> cidade -> tipo de ocorrência "Completo" -> BUSCAR -> CSV

Uso:
  python rpa_fogo_cruzado.py                 # tudo, navegador invisível
  python rpa_fogo_cruzado.py --show          # mostra o navegador
  python rpa_fogo_cruzado.py --estado Bahia  # só um estado
  python rpa_fogo_cruzado.py --force         # rebaixa arquivos já existentes

Saída: downloads_csv/<Estado>/<Cidade>.csv  +  downloads_csv/relatorio.csv
"""

import argparse
import csv
import os
import re
import time
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

URL = "https://api.fogocruzado.org.br/search"
TIPO_OCORRENCIA = "Completo"
OUT_DIR = Path(os.environ.get("FC_OUT_DIR", Path(__file__).parent / "downloads_csv"))
DOWNLOAD_TIMEOUT_MS = 15 * 60 * 1000  # cidades grandes paginam muito antes de gerar o CSV
TENTATIVAS = 4
# A API do site aceita 60 requisições/min; cada página traz 200 ocorrências.
MAX_OCORRENCIAS_POR_DOWNLOAD = 8_000  # ~40 páginas, folga abaixo do limite
PAUSA_RATE_LIMIT_S = 65
ANO_INICIAL = 2016  # início da base do Fogo Cruzado


def slug(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]+", "_", t).strip("_")


def abrir_combo(page, data_cy: str):
    """Os campos ficam desabilitados enquanto o site carrega estados/cidades,
    e um clique nesse momento não abre a lista — então espera e insiste."""
    campo = page.locator(f'input[data-cy="{data_cy}"]')
    opcao = page.locator('[role="listbox"] [role="option"]').first
    for _ in range(12):
        page.wait_for_function("el => !el.disabled", arg=campo.element_handle(), timeout=60_000)
        campo.click()
        try:
            opcao.wait_for(timeout=5_000)
            return campo
        except PWTimeout:
            page.keyboard.press("Escape")
    raise PWTimeout(f"lista do campo {data_cy} não abriu")


def opcoes_visiveis(page) -> list[str]:
    return [t.strip() for t in page.locator('[role="listbox"] [role="option"]').all_inner_texts()]


def escolher_opcao(page, data_cy: str, texto: str):
    abrir_combo(page, data_cy)
    page.locator('[role="listbox"] [role="option"]').filter(
        has_text=re.compile(rf"^\s*{re.escape(texto)}\s*$")
    ).first.click()
    page.keyboard.press("Escape")


def carregar_pagina(page):
    page.goto(URL, wait_until="networkidle")
    page.locator('input[data-cy="state"]').wait_for()


def listar_estados(page) -> list[str]:
    carregar_pagina(page)
    abrir_combo(page, "state")
    estados = opcoes_visiveis(page)
    page.keyboard.press("Escape")
    return estados


def listar_cidades(page, estado: str) -> list[str]:
    carregar_pagina(page)
    escolher_opcao(page, "state", estado)
    abrir_combo(page, "cities")
    cidades = opcoes_visiveis(page)
    page.keyboard.press("Escape")
    return cidades


def nova_pagina(browser):
    context = browser.new_context(accept_downloads=True, locale="pt-BR")
    page = context.new_page()
    page.set_default_timeout(60_000)
    page.on("response", _registrar_erro_api)
    return context, page


_ultimo_429 = 0.0  # quando a API do site recusou por limite de requisições


def _registrar_erro_api(resposta):
    """Loga erros da API do site (o 401 de login é normal) e anota os 429."""
    global _ultimo_429
    if "api-service" in resposta.url and resposta.status >= 400 and resposta.status != 401:
        if resposta.status == 429:
            _ultimo_429 = time.time()
        print(f"    API {resposta.status}: {resposta.url.split('?')[0]}", flush=True)


def pausa_apos_falha(erro: str, tentativa: int, base_s: int):
    """Se a API limitou as requisições (429) no último minuto, espera a janela
    zerar; senão, uma pausa curta crescente."""
    if "429" in erro or time.time() - _ultimo_429 < PAUSA_RATE_LIMIT_S:
        print(f"    limite da API atingido — aguardando {PAUSA_RATE_LIMIT_S}s", flush=True)
        time.sleep(PAUSA_RATE_LIMIT_S)
    else:
        time.sleep(base_s * tentativa)


def com_tentativas(descricao: str, funcao, *args):
    """Repete uma etapa de navegação que falha de forma passageira (campo do site
    desabilitado, lentidão, limite de requisições da API)."""
    for tentativa in range(1, TENTATIVAS + 1):
        try:
            return funcao(*args)
        except Exception as e:  # noqa: BLE001
            erro = f"{type(e).__name__}: {e}".splitlines()[0]
            print(f"{descricao}: tentativa {tentativa} falhou — {erro}", flush=True)
            if tentativa == TENTATIVAS:
                raise
            pausa_apos_falha(erro, tentativa, 15)


def baixar_cidade(browser, estado: str, cidade: str, destino: Path) -> tuple[str, int]:
    """Retorna (status, total de ocorrências que o site informou), com status
    'ok', 'ok (N partes por ano)' ou 'sem_resultados'.

    Usa um contexto novo por download: o site só salva o CSV depois de paginar a
    API inteira, e o Chrome bloqueia em silêncio esse download "automático" se o
    mesmo contexto já tiver baixado outros arquivos.
    """
    status, total = _em_contexto_novo(browser, estado, cidade, destino)
    if status != "grande":
        return status, total

    # A API limita a 60 requisições/min e o site pagina de 200 em 200: acima de
    # ~10 mil ocorrências ele toma HTTP 429 e quebra. Nesses casos usamos os
    # campos de data do próprio site para baixar ano a ano e juntamos os CSVs.
    print(f"    {total} ocorrências — baixando ano a ano (pausa de {PAUSA_RATE_LIMIT_S}s entre anos)")
    partes = []
    for ano in range(ANO_INICIAL, datetime.now().year + 1):
        parte = destino.with_name(f"{destino.stem}__{ano}.csv")
        time.sleep(PAUSA_RATE_LIMIT_S)
        status, _ = _em_contexto_novo(
            browser, estado, cidade, parte, periodo=(f"{ano}-01-01", f"{ano + 1}-01-01"),
            dividir_se_grande=False,
        )
        print(f"    {ano}: {status}")
        if status == "ok":
            partes.append(parte)
    juntar_csvs(partes, destino, total)
    for parte in partes:
        parte.unlink()
    return f"ok ({len(partes)} partes por ano)", total


def baixar_estado(browser, estado: str, inicio: date, fim: date, destino: Path) -> tuple[str, int]:
    """Baixa o CSV de todas as cidades de um estado num período (campo de cidades
    vazio + datas). Retorna (status, total informado pelo site). Períodos com
    ocorrências demais para um download são divididos mês a mês."""
    periodo = (inicio.isoformat(), fim.isoformat())
    status, total = _em_contexto_novo(browser, estado, None, destino, periodo=periodo)
    if status != "grande":
        return status, total

    print(f"    {total} ocorrências — baixando mês a mês", flush=True)
    partes, atual = [], inicio
    while atual < fim:
        proximo = min(date(atual.year + atual.month // 12, atual.month % 12 + 1, 1), fim)
        parte = destino.with_name(f"{destino.stem}__{atual:%Y_%m}.csv")
        time.sleep(PAUSA_RATE_LIMIT_S)
        # o fim de cada parte avança 1 dia (sobreposição); juntar_csvs remove duplicatas
        status, _ = _em_contexto_novo(
            browser, estado, None, parte,
            periodo=(atual.isoformat(), min(proximo + timedelta(days=1), fim).isoformat()),
            dividir_se_grande=False,
        )
        if status == "ok":
            partes.append(parte)
        atual = proximo
    juntar_csvs(partes, destino, total)
    for parte in partes:
        parte.unlink()
    return f"ok ({len(partes)} partes por mês)", total


def juntar_csvs(partes: list[Path], destino: Path, total_ocorrencias: int):
    """Concatena as partes no mesmo formato do site (vírgula, CRLF), sem linhas
    duplicadas (os períodos se sobrepõem em 1 dia para não perder nada)."""
    cabecalho, linhas, vistas = None, [], set()
    for parte in partes:
        with parte.open(newline="", encoding="utf-8") as f:
            leitor = csv.reader(f)
            cab = next(leitor)
            if cabecalho is None:
                cabecalho = cab
            elif cab != cabecalho:
                raise RuntimeError(f"cabeçalho diferente em {parte.name}")
            for linha in leitor:
                if tuple(linha) not in vistas:
                    vistas.add(tuple(linha))
                    linhas.append(linha)
    ids = {linha[0] for linha in linhas}
    if len(ids) != total_ocorrencias:
        raise RuntimeError(f"partes somam {len(ids)} ocorrências, esperado {total_ocorrencias}")
    with destino.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\r\n")
        w.writerow(cabecalho)
        w.writerows(linhas)


def _em_contexto_novo(browser, *args, **kwargs):
    context, page = nova_pagina(browser)
    try:
        return _baixar(page, *args, **kwargs)
    finally:
        context.close()


def contar_no_site(estados: list[str], headless=True) -> dict[str, int]:
    """Total de ocorrências de cada estado no site (BUSCAR sem cidade e sem datas),
    sem baixar nada. Serve para conferir se a base inteira está alinhada."""
    totais = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        for estado in estados:
            def buscar():
                context, page = nova_pagina(browser)
                try:
                    carregar_pagina(page)
                    escolher_opcao(page, "state", estado)
                    escolher_opcao(page, "type-occurrences", TIPO_OCORRENCIA)
                    with page.expect_response(lambda r: "/occurrences" in r.url and r.request.method == "GET",
                                              timeout=120_000) as resp:
                        page.locator('button[data-cy="filter-button"]').click()
                    if resp.value.status == 429:
                        raise RuntimeError("API: limite de requisições (429)")
                    return resp.value.json()["pageMeta"]["itemCount"]
                finally:
                    context.close()
            totais[estado] = com_tentativas(f"Contar {estado}", buscar)
        browser.close()
    return totais


def _baixar(page, estado: str, cidade: str | None, destino: Path, periodo=None,
            dividir_se_grande=True) -> tuple[str, int]:
    """Um download pelo site. cidade=None deixa o campo de cidades vazio (o estado
    inteiro). O total vem da resposta que o próprio site recebe ao clicar em BUSCAR."""
    carregar_pagina(page)
    escolher_opcao(page, "state", estado)
    if cidade:
        escolher_opcao(page, "cities", cidade)
    if periodo:
        page.locator('input[data-cy="initial-date"]').fill(periodo[0])
        page.locator('input[data-cy="final-date"]').fill(periodo[1])
    escolher_opcao(page, "type-occurrences", TIPO_OCORRENCIA)

    with page.expect_response(lambda r: "/occurrences" in r.url and r.request.method == "GET", timeout=120_000) as resp:
        page.locator('button[data-cy="filter-button"]').click()
    if resp.value.status == 429:
        raise RuntimeError("API: limite de requisições (429)")
    total = resp.value.json().get("pageMeta", {}).get("itemCount", 1)
    if total == 0:
        return "sem_resultados", 0
    if dividir_se_grande and total > MAX_OCORRENCIAS_POR_DOWNLOAD:
        return "grande", total

    botao = page.locator('button[data-cy="download-csv-button"]')
    page.wait_for_function("el => !el.disabled", arg=botao.element_handle(), timeout=120_000)

    downloads, bloqueios = [], []
    page.on("download", lambda d: downloads.append(d))
    page.on("response", lambda r: r.status == 429 and bloqueios.append(r.url))
    botao.click()
    # O botão mostra "Baixando..." enquanto o site pagina a API; se ele voltar ao
    # normal e nenhum download aparecer, o site falhou — não adianta esperar.
    # Em cidades enormes (ex.: Rio de Janeiro) a aba fica travada montando o CSV,
    # por isso as leituras do botão usam o timeout longo.
    def baixando() -> bool:
        return "BAIXANDO" in botao.inner_text(timeout=DOWNLOAD_TIMEOUT_MS).upper()

    inicio = time.time()
    while not downloads:
        if time.time() - inicio > DOWNLOAD_TIMEOUT_MS / 1000:
            raise TimeoutError("download não iniciou")
        page.wait_for_timeout(1_000)
        if bloqueios:  # o site quebra ao tomar 429 — não adianta esperar
            raise RuntimeError("API: limite de requisições (429) durante o download")
        if not downloads and not baixando():
            page.wait_for_timeout(5_000)
            if not downloads and not baixando():
                alerta = " ".join(page.locator(".MuiAlert-message").all_inner_texts())
                raise RuntimeError(f"site terminou sem gerar arquivo {alerta}".strip())
    downloads[0].save_as(destino)
    return "ok", total


def executar(out_dir: Path = OUT_DIR, estados_filtro=None, force=False, headless=True) -> list[list[str]]:
    """Carga completa: baixa o CSV de cada cidade em out_dir/<Estado>/<Cidade>.csv e
    devolve as linhas [estado, cidade, status, arquivo_ou_erro, total_no_site]."""
    out_dir.mkdir(parents=True, exist_ok=True)
    relatorio = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        _, page = nova_pagina(browser)  # só para listar estados/cidades

        estados = com_tentativas("Listar estados", listar_estados, page)
        if estados_filtro:
            estados = [e for e in estados if e in estados_filtro]
        print(f"Estados: {estados}", flush=True)

        for estado in estados:
            cidades = com_tentativas(f"Listar cidades de {estado}", listar_cidades, page, estado)
            print(f"\n=== {estado}: {len(cidades)} cidades ===", flush=True)
            pasta = out_dir / slug(estado)
            pasta.mkdir(exist_ok=True)

            for i, cidade in enumerate(cidades, 1):
                destino = pasta / f"{slug(cidade)}.csv"
                prefixo = f"[{estado} {i}/{len(cidades)}] {cidade}"
                if destino.exists() and not force:
                    print(f"{prefixo}: já existe, pulando", flush=True)
                    relatorio.append([estado, cidade, "ja_existia", str(destino), None])
                    continue

                status, erro, total = "erro", "", None
                for tentativa in range(1, TENTATIVAS + 1):
                    try:
                        t0 = time.time()
                        status, total = baixar_cidade(browser, estado, cidade, destino)
                        print(f"{prefixo}: {status} ({time.time() - t0:.0f}s)", flush=True)
                        erro = ""
                        break
                    except Exception as e:  # noqa: BLE001
                        erro = f"{type(e).__name__}: {e}".splitlines()[0]
                        print(f"{prefixo}: tentativa {tentativa} falhou — {erro}", flush=True)
                        pausa_apos_falha(erro, tentativa, 5)
                relatorio.append([estado, cidade, status if not erro else "erro",
                                  str(destino) if status.startswith("ok") else erro, total])

        browser.close()
    return relatorio


def executar_incremental(out_dir: Path, inicio_por_estado: dict, fim: date,
                         headless=True) -> list[list]:
    """Atualização incremental: um download por estado (todas as cidades) do período
    [inicio_por_estado[estado], fim). Estados do site ausentes do dicionário usam
    ANO_INICIAL. Devolve [estado, inicio, fim, status, arquivo_ou_erro, total_no_site]."""
    out_dir.mkdir(parents=True, exist_ok=True)
    relatorio = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        _, page = nova_pagina(browser)
        estados = com_tentativas("Listar estados", listar_estados, page)
        print(f"Estados: {estados}", flush=True)

        for estado in estados:
            inicio = inicio_por_estado.get(estado, date(ANO_INICIAL, 1, 1))
            destino = out_dir / f"{slug(estado)}.csv"
            prefixo = f"[{estado}] {inicio:%d/%m/%Y} a {fim:%d/%m/%Y}"
            status, erro, total = "erro", "", None
            for tentativa in range(1, TENTATIVAS + 1):
                try:
                    t0 = time.time()
                    status, total = baixar_estado(browser, estado, inicio, fim, destino)
                    print(f"{prefixo}: {status}, {total} ocorrências ({time.time() - t0:.0f}s)", flush=True)
                    erro = ""
                    break
                except Exception as e:  # noqa: BLE001
                    erro = f"{type(e).__name__}: {e}".splitlines()[0]
                    print(f"{prefixo}: tentativa {tentativa} falhou — {erro}", flush=True)
                    pausa_apos_falha(erro, tentativa, 5)
            relatorio.append([estado, inicio, fim, status if not erro else "erro",
                              str(destino) if status.startswith("ok") else erro, total])
        browser.close()
    return relatorio


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--show", action="store_true", help="mostra o navegador")
    ap.add_argument("--estado", action="append", help="limita a um/mais estados")
    ap.add_argument("--force", action="store_true", help="rebaixa arquivos já existentes")
    args = ap.parse_args()

    relatorio = executar(OUT_DIR, args.estado, args.force, headless=not args.show)

    rel = OUT_DIR / "relatorio.csv"
    with rel.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["estado", "cidade", "status", "arquivo_ou_erro", "total_no_site"])
        w.writerows(relatorio)
    resumo = {s: sum(1 for r in relatorio if r[2] == s) for s in {r[2] for r in relatorio}}
    print(f"\nConcluído em {datetime.now():%H:%M:%S} — {resumo}. Relatório: {rel}")


if __name__ == "__main__":
    main()
