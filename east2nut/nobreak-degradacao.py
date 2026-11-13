#!/usr/bin/env python3
"""Acompanha a degradacao do banco de baterias do nobreak ao longo do tempo.

Roda offline sobre o recorder. Nao chama nada, nao altera nada.

    python3 /homeassistant/scripts/nobreak-degradacao.py
    python3 /homeassistant/scripts/nobreak-degradacao.py --dias 90
    python3 /homeassistant/scripts/nobreak-degradacao.py --modulos 2

FONTE: estatisticas horarias (`statistics`), nao a tabela `states`. O recorder
purga estados em ~10 dias, mas estatisticas de sensores com `state_class` ficam
para sempre -- em 09/2026 havia 252 dias disponiveis. E dai que sai o historico.

COMO MEDE: toda queda de energia e um teste de carga gratuito. No instante em
que a carga transfere para bateria, a tensao cai por DOIS motivos somados:

    queda_total  =  (flutuacao -> repouso)  +  (I x R_interna)
                     ^^^^ fixo, nao e R ^^^    ^^^^ ohmico ^^^^

Regredindo queda contra corrente em varios eventos, o intercepto da a parcela
fixa e a inclinacao da a resistencia real. Dividir a queda inteira pela corrente
(erro que cometi na primeira analise) infla a resistencia em 2-4x.

DUAS ARMADILHAS, ambas corrigidas aqui:

1. So a PRIMEIRA hora de cada descarga vale. Numa queda longa, o minimo horario
   das horas seguintes e esgotamento do banco, nao queda instantanea -- em
   05/03/2026 sete horas seguidas marcaram 178,5 V e contaminavam o ajuste.

2. A tensao de flutuacao nao e a de repouso. O carregador segura o banco acima
   do seu proprio potencial; ao soltar, cai ~13 V mesmo com corrente zero num
   banco SAUDAVEL. Queda fixa muito maior que isso indica perda de capacidade.

REFERENCIAS (datasheet Intelbras DNB 6 kVA 220V RT SB + MB 1607 192V RT):
    modulo MB 1607      : 16 baterias VRLA 12 V 7 Ah, barramento 192 V (96 celulas)
    flutuacao (padrao)  : 13,5 V por bateria = 2,25 V/celula = 216 V no banco
    corte DOD (padrao)  : 10,5 V por bateria = 1,75 V/celula = 168 V no banco
    expansao            : ate 5 modulos em paralelo

SAUDAVEL (VRLA 12 V 7 Ah a 25 C):
    resistencia   : 25-35 mOhm por bloco
    repouso       : 2,13-2,15 V/celula (204-206 V no banco)
    queda fixa    : ~13 V (flutuacao -> repouso)

LINHA DE BASE do banco original, medida em 28/09/2026 antes da troca -- guardar
para comparacao:
    resistencia : 56 mOhm/bloco   (1,6-2x acima do saudavel)
    repouso     : 2,02 V/celula   (banco semi-carregado com carregador em 100%)
    autonomia   : 2 min 45 s a 1300 W, contra ~30 min esperados de um banco novo
"""
import argparse
import csv
import datetime
import math
import subprocess
import sqlite3
import sys

DB = "/homeassistant/home-assistant_v2.db"

# O nobreak mudou de caminho em 11/11/2026: a serial saiu do Home Assistant e foi
# para o Raspberry do quorum (srv-qdevice01), que virou o unico master Modbus e
# publica por NUT. A integracao east_ups foi removida, entao o historico esta sob
# os nomes ANTIGOS e o dado novo entra sob os nomes do NUT. Os dois precisam ser
# costurados, senao o script ve so o passado ou so o presente.
#
# As estatisticas de longo prazo sobrevivem a remocao da integracao -- a tabela
# `statistics` nao e purgada --, por isso os 252 dias continuam disponiveis.
SERIES = {
    "V": ["sensor.east_ups_ea900_g4_battery_voltage", "sensor.east_battery_voltage"],
    "I": ["sensor.east_ups_ea900_g4_battery_current", "sensor.east_battery_current"],
    "P": ["sensor.east_ups_ea900_g4_output_active_power", "sensor.east_real_power"],
}
BLOCOS = 16          # baterias de 12 V em serie
CELULAS = 96         # 16 x 6
AH_MODULO = 7        # MB 1607
FLUT_PADRAO = 216.0  # 2,25 V/celula x 96
CORTE_DOD = 168.0    # 1,75 V/celula x 96
R_SAUDAVEL = (25, 35)      # mOhm por bloco
REPOUSO_SAUDAVEL = (2.13, 2.15)  # V por celula
I_MIN_DESCARGA = 2.0       # A -- abaixo disso e ruido de flutuacao


def _stat(cur, eids):
    """Hourly statistics for one measurement, merged across its entity names.

    Takes a list of statistic_ids, oldest naming first. Later names win on any
    overlapping hour, which matters only on the changeover day itself.
    """
    if isinstance(eids, str):
        eids = [eids]
    out = {}
    for eid in eids:
        cur.execute("SELECT id FROM statistics_meta WHERE statistic_id=?", (eid,))
        r = cur.fetchone()
        if not r:
            continue
        cur.execute(
            "SELECT start_ts, mean, min, max FROM statistics "
            "WHERE metadata_id=? ORDER BY start_ts", (r[0],))
        out.update({int(a): (b, c, d) for a, b, c, d in cur.fetchall()})
    return out


def _ajuste(pontos):
    """Minimos quadrados: queda = offset + I * R.

    Devolve (offset, R, diag) com diag trazendo os erros padrao, o R2 e a faixa
    de corrente. Reportar o ajuste sem isso ja induziu a erro: com os 20 eventos
    horarios do banco velho o R2 e 0,12 e o erro de R e +-44 mOhm/bloco, ou seja
    os "70 mOhm" atravessam a faixa saudavel de 25-35. O numero sozinho parece
    medida e nao e.
    """
    n = len(pontos)
    if n < 3:
        return None, None, None
    xs = [p[0] for p in pontos]
    mx = sum(xs) / n
    my = sum(p[1] for p in pontos) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None, None, None
    R = sum((p[0] - mx) * (p[1] - my) for p in pontos) / sxx
    off = my - R * mx
    res = [p[1] - (off + R * p[0]) for p in pontos]
    sse = sum(r * r for r in res)
    sst = sum((p[1] - my) ** 2 for p in pontos)
    s = math.sqrt(sse / (n - 2)) if n > 2 else float("nan")
    diag = {
        "n": n,
        "se_R": s / math.sqrt(sxx),
        "se_off": s * math.sqrt(1.0 / n + mx * mx / sxx),
        "r2": (1 - sse / sst) if sst else float("nan"),
        "resid": s,
        "i_min": min(xs),
        "i_max": max(xs),
    }
    return off, R, diag


# Discharge events recorded by east2nut on the Raspberry Pi, at 1s resolution.
# This is the better source and eventually the only one that matters: a real
# outage takes Home Assistant down with it, and even when it survives it polls
# NUT about once a minute -- on 2026-11-12 it missed a 16s self-test entirely,
# logging 209.6 V when the bank actually sagged to 187.5 V.
CSV_LOCAL = "/homeassistant/tmp/east2nut-discharges.csv"
PI_HOST = "192.168.0.5"           # by IP on purpose: DNS is down in an outage
PI_CSV = "/var/lib/east2nut/discharges.csv"

# Bank replaced on this date. Events before and after belong to different
# hardware and must never share a fit or a trend line. It also keeps the badly
# measured self-tests of 12/11 out of series A: Home Assistant polls NUT about
# once a minute and caught 209.6 V on a dip that actually reached 187.5 V, so
# those rows understate the sag and would drag the old bank's fit.
TROCA_BANCO = datetime.datetime(2026, 11, 11, 16, 38).timestamp()


def _buscar_csv(destino):
    """Copies the event log off the Pi. Explicit --fetch only: the default run
    stays offline, as the module docstring promises."""
    subprocess.run(
        ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
         "root@%s:%s" % (PI_HOST, PI_CSV), destino],
        check=True, capture_output=True, timeout=120)


def _eventos_csv(path):
    """Turns the Pi's CSV into the same (corrente, queda, ts, flut, watts)
    tuples the hourly statistics produce, so the fit code is shared.

    Two things this gets right that the hourly data cannot:

    - The float reference is MEASURED, from the `pre` rows captured before the
      transfer, instead of falling back to the FLUT_PADRAO constant.
    - The sag is INSTANTANEOUS and paired with the current from the very same
      sample. The hourly minimum mixes the ohmic step with however much the bank
      drained during that hour, which inflates R on long outages, and it pairs a
      minimum voltage with a minimum current that may be from another moment.
    """
    try:
        fh = open(path, newline="")
    except OSError:
        return []
    eventos = {}
    with fh:
        for row in csv.DictReader(fh):
            eventos.setdefault(row["event"], []).append(row)

    saida = []
    for eid, linhas in sorted(eventos.items()):
        pre = [float(r["battery_voltage"]) for r in linhas if r["phase"] == "pre"]
        desc = [r for r in linhas if r["phase"] == "discharge"]
        if not pre or not desc:
            continue
        flut = sum(pre) / len(pre)
        fundo = min(desc, key=lambda r: float(r["battery_voltage"]))
        corrente = abs(float(fundo["battery_current"]))
        if corrente < I_MIN_DESCARGA:
            continue
        ts = datetime.datetime.fromisoformat(fundo["timestamp"]).timestamp()
        saida.append((corrente, flut - float(fundo["battery_voltage"]), ts, flut,
                      float(fundo["output_active_power"]), len(desc)))
    return saida


def _relatar_ajuste(titulo, pontos, flut_med, nota=None):
    """Prints one fit with its uncertainty. Never prints a bare point value."""
    print("\n  " + titulo)
    if nota:
        print("    " + nota)
    if len(pontos) < 3:
        print("    n=%d -- insuficiente para ajustar (minimo 3)." % len(pontos))
        for c, q, t, *_ in pontos:
            print("      %s  %.1f A  queda %.1f V"
                  % (datetime.datetime.fromtimestamp(t).strftime("%d/%m %H:%M:%S"), c, q))
        return None
    off, R, d = _ajuste([(c, q) for c, q, *_ in pontos])
    if off is None:
        print("    ajuste impossivel (sem dispersao de corrente)")
        return None
    mohm, se_mohm = R / BLOCOS * 1000, d["se_R"] / BLOCOS * 1000
    repouso = (flut_med - off) / CELULAS
    print("    queda = %.1f +-%.1f V  +  I x %.3f +-%.3f ohm"
          % (off, d["se_off"], R, d["se_R"]))
    print("    n=%d  R2=%.2f  residuo %.1f V  corrente %.1f-%.1f A"
          % (d["n"], d["r2"], d["resid"], d["i_min"], d["i_max"]))
    lo = max(0.0, mohm - 2 * se_mohm)
    print("    resistencia : %.0f +-%.0f mOhm/bloco  -> faixa %.0f a %.0f  (saudavel %d-%d)"
          % (mohm, se_mohm, lo, mohm + 2 * se_mohm, R_SAUDAVEL[0], R_SAUDAVEL[1]))
    if mohm - 2 * se_mohm < 0:
        print("    (faixa truncada em zero: o ajuste admite resistencia nula,")
        print("     o que e impossivel -- sinal de que ele nao esta restringindo nada)")
    print("    repouso     : %.3f +-%.3f V/celula  (saudavel %.2f-%.2f)"
          % (repouso, d["se_off"] / CELULAS, REPOUSO_SAUDAVEL[0], REPOUSO_SAUDAVEL[1]))
    if d["r2"] < 0.5:
        print("    AVISO: R2 baixo -- a corrente explica pouco da variacao da queda.")
        print("           Trate estes valores como ordem de grandeza, nao medida.")
    if d["i_max"] / max(d["i_min"], 0.1) < 2:
        print("    AVISO: faixa de corrente estreita -- o intercepto e extrapolado longe.")
    return off, R, d


def pontos_det_para_relato(detalhe):
    """detalhe = (corrente, queda, ts, flut) -- ja no formato do relator."""
    return list(detalhe)


def _autonomia(ah_total, watts):
    """Estimativa grosseira com derating por taxa de descarga (Peukert)."""
    wh = ah_total * 192.0
    corrente = (watts / 0.93) / 192.0
    taxa = corrente / ah_total if ah_total else 99
    # fracao entregue ate 1,75 V/celula, por taxa C
    if taxa <= 0.3:   frac = 0.85
    elif taxa <= 0.6: frac = 0.70
    elif taxa <= 1.0: frac = 0.55
    elif taxa <= 1.6: frac = 0.43
    else:             frac = 0.33
    return (wh * frac) / (watts / 0.93) * 60, taxa


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dias", type=int, default=0, help="0 = todo o historico")
    ap.add_argument("--modulos", type=int, default=1, help="quantos MB 1607 instalados")
    ap.add_argument("--csv", default=CSV_LOCAL,
                    help="eventos de 1s do east2nut (padrao: %s)" % CSV_LOCAL)
    ap.add_argument("--fetch", action="store_true",
                    help="copia o CSV do Pi antes de analisar (sai do modo offline)")
    args = ap.parse_args()

    if args.fetch:
        try:
            _buscar_csv(args.csv)
            print("CSV atualizado de %s:%s\n" % (PI_HOST, PI_CSV))
        except Exception as err:  # noqa: BLE001
            print("aviso: nao consegui buscar o CSV do Pi (%s)\n" % err)

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True, timeout=30)
    cur = con.cursor()
    V = _stat(cur, SERIES["V"])
    I = _stat(cur, SERIES["I"])
    P = _stat(cur, SERIES["P"])
    if not V or not I:
        sys.exit("sem estatisticas de longo prazo do nobreak")

    corte = 0
    if args.dias:
        corte = (datetime.datetime.now(datetime.timezone.utc)
                 - datetime.timedelta(days=args.dias)).timestamp()

    horas = sorted(t for t in V if t >= corte and t < TROCA_BANCO)
    desc = [t for t in horas
            if I.get(t) and I[t][1] is not None and I[t][1] < -I_MIN_DESCARGA
            and V[t][1] is not None]
    inicios = [t for t in desc if (t - 3600) not in desc]

    D = datetime.datetime.fromtimestamp
    inicio_h = D(horas[0]) if horas else None
    print(f"janela: {inicio_h:%d/%m/%Y} a {D(horas[-1]):%d/%m/%Y} "
          f"({(horas[-1]-horas[0])/86400:.0f} dias, {len(horas)} horas)")
    print(f"descargas: {len(desc)} horas em {len(inicios)} eventos\n")
    if len(inicios) < 3:
        sys.exit("poucos eventos para ajustar (minimo 3). Sem quedas de energia,\n"
                 "use o botao 'Battery test' do nobreak para gerar um ponto.")

    print(f"{'evento':>17} {'I(A)':>6} {'queda(V)':>9} {'Wout':>6}")
    print("-" * 44)
    pontos, detalhe = [], []
    for t in inicios:
        ant = [V[x][0] for x in (t - 3600, t - 7200)
               if x in V and V[x][0] and I.get(x) and I[x][1] is not None and I[x][1] > -1]
        flut = sum(ant) / len(ant) if ant else FLUT_PADRAO
        queda = flut - V[t][1]
        corrente = abs(I[t][1])
        pot = (P.get(t) or (None,))[0]
        pontos.append((corrente, queda, t))
        detalhe.append((corrente, queda, t, flut))
        print(f"{D(t):%d/%m/%Y %H:%M} {corrente:6.1f} {queda:9.1f} "
              f"{(round(pot) if pot else '-'):>6}")

    flut_med = sum(f for _, _, _, f in detalhe) / len(detalhe)

    # As duas series sao ajustadas SEPARADAMENTE, de proposito. Misturar o
    # minimo horario (que inclui esgotamento) com a queda instantanea de 1s num
    # ajuste so contamina o bom com o ruim, e as duas medem bancos diferentes:
    # as estatisticas sao quase todas do banco velho, o CSV so do novo.
    eventos_csv = _eventos_csv(args.csv)
    print("\n" + "=" * 60)
    print("ESTADO DO BANCO")
    print("=" * 60)

    _relatar_ajuste(
        "SERIE A -- banco VELHO, estatisticas horarias do HA (baixa resolucao)",
        pontos_det_para_relato(detalhe), flut_med,
        "minimo horario: mistura a queda ohmica com o esgotamento da hora")

    if eventos_csv:
        flut_csv = sum(e[3] for e in eventos_csv) / len(eventos_csv)
        print("\n    eventos de 1s registrados pelo Pi:")
        for c, q, t, f, w, n in eventos_csv:
            print("      %s  %.1f A  queda %.1f V  (%.0f W, flut %.1f V, %d amostras)"
                  % (datetime.datetime.fromtimestamp(t).strftime("%d/%m %H:%M:%S"),
                     c, q, w, f, n))
        _relatar_ajuste(
            "SERIE B -- banco NOVO, eventos de 1s do east2nut (alta resolucao)",
            [(c, q, t, f) for c, q, t, f, _w, _n in eventos_csv], flut_csv,
            "flutuacao medida antes do evento; queda e corrente da mesma amostra")
    else:
        print("\n  SERIE B -- banco NOVO, eventos de 1s do east2nut")
        print("    nenhum evento em %s" % args.csv)
        print("    use --fetch para copiar do Pi, ou aguarde a proxima queda.")

    off, R, _d = _ajuste([(c, q) for c, q, _ in pontos])
    mohm = R / BLOCOS * 1000

    if len(pontos) >= 6:
        # NAO refazer a regressao por subconjunto: em fev/mar 2026 todos os
        # eventos tinham corrente 6,6-7,4 A, e sem dispersao na variavel
        # independente o ajuste explode (deu 499 -> -26 mOhm, sem sentido).
        # Em vez disso: fixa o R global e acompanha a QUEDA FIXA por evento,
        # que e o indicador de capacidade -- ela cresce quando o banco perde
        # carga util.
        print("\n  TENDENCIA da queda fixa (R global de "
              f"{mohm:.0f} mOhm/bloco descontado de cada evento)")
        por_periodo = {}
        for c, q, t, _ in detalhe:
            chave = D(t).strftime("%Y-%m")
            por_periodo.setdefault(chave, []).append(q - c * R)
        for k in sorted(por_periodo):
            v = por_periodo[k]
            med = sum(v) / len(v)
            rep = (flut_med - med) / CELULAS
            print(f"    {k}  queda fixa {med:5.1f} V  -> repouso {rep:.3f} V/celula"
                  f"  (n={len(v)})")
        ks = sorted(por_periodo)
        if len(ks) >= 2:
            p1 = sum(por_periodo[ks[0]]) / len(por_periodo[ks[0]])
            p2 = sum(por_periodo[ks[-1]]) / len(por_periodo[ks[-1]])
            print(f"    {ks[0]} -> {ks[-1]}: {p1:.1f} V -> {p2:.1f} V ({p2-p1:+.1f} V)")
            print("    (queda fixa CRESCENDO = banco perdendo capacidade)")

    ah = AH_MODULO * args.modulos
    print("\n" + "=" * 60)
    print(f"AUTONOMIA ESPERADA DE UM BANCO NOVO ({args.modulos} modulo(s), {ah} Ah)")
    print("=" * 60)
    for w in (1300, 1900, 2700):
        mins, taxa = _autonomia(ah, w)
        print(f"  {w:5} W  ->  ~{mins:4.0f} min   (descarga a {taxa:.2f}C)")
    print("\n  Estimativa com derating tipico de VRLA, margem ~25%.")
    print("  O numero real sai do botao 'Battery test' com o banco novo.")


if __name__ == "__main__":
    main()
