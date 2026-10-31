"""K7: strategiatutkija. LLM ehdottaa HYPOTEESEJA (ei koodia); deterministinen koodi testaa ne K6:n lapi.
Saannot (koodi on laki):
 1. LLM nakee ei hintadataa, vain DSL-kuvauksen ja aiempien hypoteesien tulokset. Vastaus = tiukka JSON.
 2. Strategia on kiintea DSL (sallitut arvot listattu): ei suoritettavaa koodia mallilta.
 3. Hypoteesi rekisteroidaan ENNEN testia. Viikkokiintio (rajat.K7_VIIKKO_KIINTIO). Samaa speksia ei testata kahdesti.
 4. Monivertailukorjaus: p kerrotaan (ruudukon koko x K7_PERHE_BUDJETTI). Perhebudjetti on kiintea etukateen (FWER <= 5 % koko
    ohjelmalle, ei jarjestysriippuvaa), K6:n momentum-ajo vie yhden paikan; budjetin tayttyessa K7 pysahtyy ja Tony paattaa jatkosta.
 5. Satunnaisvertailu kayttaa SAMAA markkinasuodinta ja samaa rebalanssia kuin kandidaatti.
 6. Pitaa voittaa BTC (80 %) sekä tuotossa etta Sharpessa. Muuten EI KELPAA.
 7. K7 ei koskaan koske holdoutiin eika paperikauppaan. 'EHDOLLA KELPAA' = ihminen paattaa seuraavan askeleen."""
import argparse
import hashlib
import itertools
import json
import os
import random
import re
import sqlite3
import statistics
import sys
import time
from datetime import date, datetime, timedelta, timezone

import agentit
import backtest as bt
import config
import db
import laaja
import llm
import mittarit as mit
import notify
import rajat
import silmukka
import tapahtumat

K7_VIIKKO_KIINTIO = 3
MAX_RUUDUKKO = 8
K7_PERHE_BUDJETTI = 30      # kaikki hypoteesit koskaan (sis. K6 momentum = 1). LAKI: vain Tony muuttaa.
MAX_SATUNNAISIA = 6000
MIN_SATUNNAISIA = 200

SALLITUT = {
    "signaali": ["momentum", "volatiliteetti", "huippuetaisyys"],
    "suunta": ["korkea", "matala"],
    "ikkuna": [7, 14, 30, 60, 90, 180],
    "k": [5, 6, 8, 10],
    "reb": [3, 7, 14, 30],
    "osuus": [0.5, 0.7, 0.8],
    "suodin": ["ei", "sma50", "sma100", "sma200"],
    "suodin_toiminto": ["kateinen", "btc"],
}
RUUDUKKOSALLITUT = ("ikkuna", "k", "reb", "osuus", "suodin")
PAKOLLISET = tuple(SALLITUT)


class Speksivirhe(Exception):
    pass


def _sallittu(k, v):
    """Sama tyyppi ja arvo kuin sallitussa (7.0 ei kelpaa arvolle 7, True ei kelpaa)."""
    return any(type(v) is type(a) and v == a for a in SALLITUT[k])


# ---------- DSL: speksin tarkistus ----------

def tarkista_spec(o):
    """Palauttaa siistityn speksin tai nostaa Speksivirhe. Mitaan ei 'korjata' arvaamalla."""
    if not isinstance(o, dict):
        raise Speksivirhe("spec ei ole olio")
    nimi = re.sub(r"[^a-z0-9_]+", "_", str(o.get("nimi", "")).lower()).strip("_")[:30]
    if not nimi:
        raise Speksivirhe("nimi puuttuu")
    hyp = str(o.get("hypoteesi", "")).strip()
    if len(hyp) < 20:
        raise Speksivirhe("hypoteesi puuttuu tai liian lyhyt (perustelu pakollinen)")
    kiinteat, ruudukko = o.get("kiinteat", {}), o.get("ruudukko", {})
    if not isinstance(kiinteat, dict) or not isinstance(ruudukko, dict):
        raise Speksivirhe("kiinteat/ruudukko eivat ole olioita")
    for k in list(kiinteat) + list(ruudukko):
        if k not in SALLITUT:
            raise Speksivirhe("tuntematon parametri %r" % k)
    for k in ruudukko:
        if k not in RUUDUKKOSALLITUT:
            raise Speksivirhe("parametria %r ei saa ruudukoida" % k)
    if set(kiinteat) & set(ruudukko):
        raise Speksivirhe("parametri sekä kiinteissa etta ruudukossa")
    puuttuu = [k for k in PAKOLLISET if k not in kiinteat and k not in ruudukko]
    if puuttuu:
        raise Speksivirhe("puuttuvat parametrit: %s" % ",".join(puuttuu))
    kiinteat = dict(kiinteat)
    for k, v in kiinteat.items():
        if not _sallittu(k, v):
            raise Speksivirhe("arvo %r ei sallittu parametrille %s" % (v, k))
    siisti_r = {}
    for k, lista in ruudukko.items():
        if not isinstance(lista, list) or not lista:
            raise Speksivirhe("ruudukko %s tyhja" % k)
        for v in lista:
            if not _sallittu(k, v):
                raise Speksivirhe("arvo %r ei sallittu parametrille %s" % (v, k))
        uniikit = sorted(set(lista), key=lambda x: SALLITUT[k].index(x))
        if len(uniikit) == 1:       # yhden arvon ruudukko = kiintea (kanoninen muoto, ei duplikaatteja hashiin)
            kiinteat[k] = uniikit[0]
        else:
            siisti_r[k] = uniikit
    if kiinteat.get("suodin") == "ei":
        kiinteat["suodin_toiminto"] = "kateinen"   # merkityksetön kun suodinta ei ole -> kanoninen arvo
    koko = 1
    for lista in siisti_r.values():
        koko *= len(lista)
    if koko > MAX_RUUDUKKO:
        raise Speksivirhe("ruudukko liian suuri (%d > %d)" % (koko, MAX_RUUDUKKO))
    return {"nimi": nimi, "hypoteesi": hyp[:400], "odotus": str(o.get("odotus", ""))[:200],
            "kiinteat": dict(sorted(kiinteat.items())), "ruudukko": dict(sorted(siisti_r.items())), "ruudukon_koko": koko}


def spec_hash(spec):
    kanoninen = json.dumps({"kiinteat": spec["kiinteat"], "ruudukko": spec["ruudukko"]}, sort_keys=True)
    return hashlib.sha256(kanoninen.encode()).hexdigest()[:12]


def ruudukko_lista(spec):
    avaimet = sorted(spec["ruudukko"])
    return [dict(zip(avaimet, vals)) for vals in itertools.product(*(spec["ruudukko"][k] for k in avaimet))] or [{}]


# ---------- strategia ----------

def _pisteet(signaali, c):
    if signaali == "momentum":
        return c[-1] / c[0] - 1
    if signaali == "huippuetaisyys":
        return c[-1] / max(c) - 1
    r = [c[i] / c[i - 1] - 1 for i in range(1, len(c))]
    return statistics.pstdev(r)


def rakenna(par, valitsin=None):
    """par: taysi parametridict (kaikki PAKOLLISET). valitsin(tila, data, paiva, sallitut, par) -> {symbol: paino}
    korvaa signaalivalinnan (kaytetaan satunnaisvertailussa, jolloin suodin ja rebalanssi ovat identtiset)."""
    p = dict(par)
    for k in PAKOLLISET:
        if k not in p:
            raise Speksivirhe("rakenna: parametri %s puuttuu" % k)

    def tehdas():
        tila = {"n": 0, "paino": {}, "paalla_ed": False}
        sma = None if p["suodin"] == "ei" else int(p["suodin"][3:])

        def strat(data, paiva, sallitut):
            d_ms = bt._ms(paiva)
            paalla = True
            if sma:
                c = data.sulut("BTCUSDT", d_ms, sma)
                paalla = bool(c) and c[-1] >= sum(c) / len(c)   # data puuttuu -> pois paalta (fail closed)
            ulos = None
            if not paalla:
                ulos = {"BTCUSDT": p["osuus"]} if (p["suodin_toiminto"] == "btc" and "BTCUSDT" in sallitut) else {}
            else:
                if tila["n"] % p["reb"] == 0 or not tila["paalla_ed"]:
                    if valitsin:
                        tila["paino"] = valitsin(tila, data, paiva, sallitut, p)
                    else:
                        pist = []
                        for s in sallitut:
                            c = data.sulut(s, d_ms, p["ikkuna"] + 1)
                            if c:
                                pist.append((_pisteet(p["signaali"], c), s))
                        pist.sort(key=lambda x: (-x[0], x[1]) if p["suunta"] == "korkea" else (x[0], x[1]))
                        valitut = [s for _, s in pist[:p["k"]]]
                        tila["paino"] = {s: p["osuus"] / p["k"] for s in valitut}
                ulos = dict(tila["paino"])
            tila["paalla_ed"] = paalla
            tila["n"] += 1
            return ulos
        return strat
    return tehdas


def satunnaisvalitsin(siemen):
    rnd = random.Random(siemen)

    def v(tila, data, paiva, sallitut, p):
        d_ms = bt._ms(paiva)
        lista = [s for s in sorted(sallitut) if data.sulut(s, d_ms, p["ikkuna"] + 1)]   # vain kolikot joilla taysi historia (kuten kandidaatilla)
        valitut = rnd.sample(lista, min(p["k"], len(lista)))
        return {s: p["osuus"] / max(1, len(valitut)) for s in valitut}
    return v


def mediaaniparametrit(spec):
    """Satunnaisvertailun parametrit: kiinteat sellaisenaan, ruudukon keskimmainen arvo."""
    par = dict(spec["kiinteat"])
    for k, lista in spec["ruudukko"].items():
        par[k] = lista[len(lista) // 2]
    return par


def satunnaisjakauma(data, testi_ikkunat, n, par_per_ikkuna, siemen=5000):
    """par_per_ikkuna: lista taysia parametridicteja, yksi per testi-ikkuna (kandidaatin foldikohtainen valinta:
    sama k, reb, osuus, suodin kuin kandidaatilla kussakin foldissa). Satunnainen valinta, siemen vaihtuu ikkunoittain."""
    ulos = []
    for i in range(n):
        osat = []
        for w, (a, b) in enumerate(testi_ikkunat):
            t = rakenna(par_per_ikkuna[w], satunnaisvalitsin(siemen + i + 100003 * w))
            osat.append(bt._aja(t, data, a, b)[0])
        ulos.append(mit.mittarit(bt._ketju(osat)))
    return ulos


# ---------- rekisteri ----------

K7_SKEEMA = """
CREATE TABLE IF NOT EXISTS k7_hypoteesit (
  id INTEGER PRIMARY KEY AUTOINCREMENT, aika TEXT NOT NULL, viikko TEXT NOT NULL, lahde TEXT NOT NULL,
  nimi TEXT NOT NULL, hash TEXT NOT NULL UNIQUE, spec TEXT NOT NULL,
  tila TEXT NOT NULL, tulos TEXT, verdikti TEXT);
"""


def avaa(polku=None):
    rek = bt.avaa_rekisteri(polku)
    rek.executescript(K7_SKEEMA)
    return rek


def viikko(tanaan):
    y, w, _ = tanaan.isocalendar()
    return "%d-W%02d" % (y, w)


def perhe_koko(rek):
    """Kaytetyt paikat perhebudjetista: K7-hypoteesit + 1 (K6:n momentum-ajo)."""
    return rek.execute("SELECT COUNT(*) FROM k7_hypoteesit").fetchone()[0] + 1


def budjettia_jaljella(rek):
    return max(0, K7_PERHE_BUDJETTI - perhe_koko(rek))


def kiintiota_jaljella(rek, tanaan):
    kaytetty = rek.execute("SELECT COUNT(*) FROM k7_hypoteesit WHERE viikko=? AND lahde='llm'", (viikko(tanaan),)).fetchone()[0]
    return max(0, K7_VIIKKO_KIINTIO - kaytetty)


def rekisteroi(rek, spec, lahde, tanaan):
    """Palauttaa id tai None jos sama speksi on jo rekisteroity."""
    h = spec_hash(spec)
    nimi = "k7-%s-%s" % (h[:8], spec["nimi"])
    try:
        cur = rek.execute("INSERT INTO k7_hypoteesit (aika,viikko,lahde,nimi,hash,spec,tila) VALUES (?,?,?,?,?,?,'rekisteroity')",
                          (datetime.now(timezone.utc).isoformat(timespec="seconds"), viikko(tanaan), lahde, nimi, h, json.dumps(spec, sort_keys=True)))
    except sqlite3.IntegrityError:
        return None, nimi
    rek.commit()
    return cur.lastrowid, nimi


def aiemmat_tekstina(rek, n=15):
    rivit = []
    for nimi, spec, tila, verd in rek.execute("SELECT nimi, spec, tila, verdikti FROM k7_hypoteesit ORDER BY id DESC LIMIT ?", (n,)):
        s = json.loads(spec)
        rivit.append("- %s | %s | fixed=%s grid=%s | %s" % (nimi, s["hypoteesi"][:120], json.dumps(s["kiinteat"], sort_keys=True), json.dumps(s["ruudukko"], sort_keys=True), verd or tila))
    return "\n".join(rivit) or "(none yet)"


# ---------- LLM-ehdottaja ----------

EHDOTTAJA = """You are the STRATEGY RESEARCHER of a paper-trading team (simulated money, daily crypto spot, long-only, universe = 20 most liquid USDT coins by 30-day volume).
You see NO price data. Propose hypotheses from market-structure reasoning only (why would this edge exist and persist after ~0.2 % round-trip costs?). Your proposals are tested by code with walk-forward, a random-coin baseline and a Bonferroni correction over ALL hypotheses ever tried, so every weak idea makes the bar higher for the next. Propose only ideas with a real mechanism. Quality over quantity: if you have fewer than N good ideas, return fewer.
The benchmark to beat is BTC buy-and-hold (80 %% allocation) in BOTH return and Sharpe. A strategy that only reduces risk does not qualify.
STRATEGY DSL (the only thing you may output; values outside these lists are rejected):
- signaali: momentum (return over window) | volatiliteetti (stdev of daily returns over window) | huippuetaisyys (close / window-high - 1, 0 = at the high)
- suunta: korkea (pick highest score) | matala (pick lowest score; momentum+matala = reversal, volatiliteetti+matala = low-vol)
- ikkuna (days): %(ikkuna)s ; k (number of coins): %(k)s ; reb (rebalance days): %(reb)s ; osuus (invested fraction): %(osuus)s
- suodin: ei | sma50 | sma100 | sma200 (market filter: if BTC close is below its N-day average -> no altcoin positions)
- suodin_toiminto (when filter is off): kateinen (cash) | btc (hold BTC instead)
Every one of the 8 parameters (signaali, suunta, ikkuna, k, reb, osuus, suodin, suodin_toiminto) must appear exactly once, either in "kiinteat" (single value) or in "ruudukko" (list of values to choose from in-sample; only ikkuna,k,reb,osuus,suodin may be gridded; total grid size at most %(max)d). Only %(budjetti)d hypotheses will EVER be tested in total (a fixed budget; each extra one is spent forever), so propose only ideas you would bet on.
HARD LIMITS of the simulator: each position is capped at 15 %% of the portfolio, so the 'btc' fallback holds at most 15 %% BTC (rest cash); total invested at most 90 %%, cash buffer at least 10 %%.
FORBIDDEN known-losing families: EMA scalping, MACD, RSI variants, DCA+RSI, adaptive market-state switching, long-term daily indicator combinations.
Output exactly ONE JSON object and nothing else:
{"hypoteesit": [{"nimi": "short_slug", "hypoteesi": "mechanism, 20-400 chars", "odotus": "what result would falsify it", "kiinteat": {...}, "ruudukko": {...}}]}"""


def ehdottaja_prompt():
    return EHDOTTAJA % {"ikkuna": SALLITUT["ikkuna"], "k": SALLITUT["k"], "reb": SALLITUT["reb"], "osuus": SALLITUT["osuus"], "max": MAX_RUUDUKKO, "budjetti": K7_PERHE_BUDJETTI}


def ehdota(rek, n, malli, env, hae=None):
    """Palauttaa (validit_specit, hylatyt_syyt). Yksi LLM-kutsu, ei uusintaa."""
    viestit = [{"role": "system", "content": ehdottaja_prompt()},
               {"role": "user", "content": "N = %d\nAlready tried (do not repeat; learn from verdicts):\n<aiemmat>\n%s\n</aiemmat>" % (n, aiemmat_tekstina(rek))}]
    teksti, _ = llm.kysy(viestit, malli, env, hae)
    o = agentit.pura_json(teksti)
    lista = o.get("hypoteesit")
    if not isinstance(lista, list):
        raise agentit.Vastausvirhe("hypoteesit-lista puuttuu")
    validit, hylatyt = [], []
    for x in lista[:n]:
        try:
            validit.append(tarkista_spec(x))
        except Speksivirhe as e:
            hylatyt.append(str(e))
    return validit, hylatyt


# ---------- testaus ----------

def testaa(spec, data, rek, nimi, perhe, satunnaisia=None, edistyminen=None, wf_kw=None):
    """Aja walk-forward + satunnaisvertailu + BTC. Palauttaa (arvio, teksti)."""
    ruudukko = ruudukko_lista(spec)
    kiinteat = spec["kiinteat"]
    wf = bt.walk_forward(nimi, lambda **g: rakenna({**kiinteat, **g}), ruudukko, data, rek, edistyminen=edistyminen, **(wf_kw or {}))
    kerroin = len(ruudukko) * K7_PERHE_BUDJETTI     # kiintea, jarjestyksesta riippumaton (perhe-parametri vain raportointiin)
    n = satunnaisia or min(MAX_SATUNNAISIA, max(MIN_SATUNNAISIA, 25 * kerroin))
    par_ik = [{**kiinteat, **f["valittu"]} for f in wf["foldit"]]
    sat = satunnaisjakauma(data, wf["ikkunat"], n, par_ik)
    btc = bt.btc_vertailu(data, wf["ikkunat"])
    a = bt.arvio(wf, sat, btc, kerroin)
    a["kerroin"] = kerroin
    t = bt.teksti("%s (ruudukko %d x perhebudjetti %d = korjaus %d; perhe nyt %d/%d)" % (
        nimi, len(ruudukko), K7_PERHE_BUDJETTI, kerroin, perhe, K7_PERHE_BUDJETTI), a, bt.kokeiluja(rek),
        rehellinen=hasattr(data, "kuollut"))
    return a, t


def kelpaa(a):
    return bool(a["riittavasti_satunnaisia"] and a["voittaa_satunnaiset"] and a["voittaa_btc_tuotto"] and a["voittaa_btc_sharpe"])


def aja_hypoteesi(rek, data, hid, nimi, spec, perhe, **kw):
    try:
        a, t = testaa(spec, data, rek, nimi, perhe, **kw)
    except Exception as e:   # mikaan virhe ei saa jaada 'rekisteroity'-tilaan ilman merkintaa
        rek.execute("UPDATE k7_hypoteesit SET tila='virhe', verdikti=? WHERE id=?", ("VIRHE %s" % type(e).__name__, hid))
        rek.commit()
        return None, "%s: virhe testissa (%s)" % (nimi, type(e).__name__)
    verd = "EHDOLLA KELPAA" if kelpaa(a) else "EI KELPAA"
    if silmukka.kaytossa():   # oppiva silmukka: mitattu tulos havaintotiedostoon (vain walk-forward OOS, holdout koskematta)
        try:
            silmukka.kirjaa_havainnot(silmukka.HAVAINTO_POLKU, [silmukka.k7_havainto(spec, a, verd, datetime.now(timezone.utc).date())],
                                      datetime.now(timezone.utc).date())
        except (silmukka.Havaintovirhe, OSError):
            pass
    rek.execute("UPDATE k7_hypoteesit SET tila='valmis', tulos=?, verdikti=? WHERE id=?", (json.dumps(a, default=str), verd, hid))
    rek.commit()
    return a, t


def raportti(tulokset, hylatyt, perhe):
    rivit = ["K7 strategiatutkija: %d hypoteesia testattu (perhe yhteensa %d)" % (len(tulokset), perhe)]
    for spec, t in tulokset:
        rivit.append("\n# %s\n%s\n%s" % (spec["nimi"], spec["hypoteesi"][:200], t))
    if hylatyt:
        rivit.append("\nHylatty ennen testia: " + "; ".join(hylatyt[:5]))
    if any("EHDOLLA KELPAA" in t for _, t in tulokset):
        rivit.append("\nHUOM: yksikaan strategia ei siirry holdoutiin eika paperikauppaan ilman Tonyn paatosta.")
    return "\n".join(rivit)


def kuiva_ehdotus(rek, a, env_tg):
    """Mallivertailu: yksi ehdotuskutsu, ei rekisterointia eika testia. Kirjaa vain laatumittarit (ei speksien sisaltoa).
    Kelvollinen = speksi lapaisee tarkista_spec; duplikaatti = sama speksi on jo rekisterissa (hash). Ei koske kiintioon, budjettiin, holdoutiin eika salkkuihin."""
    alkoi = time.monotonic()
    malli = rajat.MALLIT[a.malli]
    try:
        specit, hylatyt = ehdota(rek, K7_VIIKKO_KIINTIO, malli, llm.lue_env(a.llm_env))
    except (llm.LLMVirhe, agentit.Vastausvirhe) as e:
        syy = "%s: %s" % (type(e).__name__, str(e)[:80])   # LLMVirhe/Vastausvirhe-viestit: HTTP-koodi, finish_reason jne., ei salaisuuksia
        tapahtumat.kirjaa("k7_kuiva", "virhe", ryhma=a.malli, malli=malli, virhekoodi=syy, kesto_s=round(time.monotonic() - alkoi, 1))
        print("K7 kuiva (%s): ehdotus epaonnistui (%s)" % (a.malli, syy))
        return 1
    tuplia = sum(1 for sp in specit if rek.execute("SELECT 1 FROM k7_hypoteesit WHERE hash=?", (spec_hash(sp),)).fetchone())
    tapahtumat.kirjaa("k7_kuiva", "ok" if specit else "ei_tietoa", ryhma=a.malli, malli=malli, ehdotettu=len(specit) + len(hylatyt),
                      kelvollinen=len(specit), hylatty=len(hylatyt), tuplia=tuplia, kesto_s=round(time.monotonic() - alkoi, 1))
    print("K7 kuiva (%s): ehdotettu %d, kelvollisia %d, hylattyja %d, duplikaatteja rekisterissä %d" % (
        a.malli, len(specit) + len(hylatyt), len(specit), len(hylatyt), tuplia))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="K7 strategiatutkija (ei koske holdoutiin eika salkkuihin)")
    ap.add_argument("--malli", default="pro", choices=sorted(rajat.MALLIT))
    ap.add_argument("--llm-env", default="/srv/kauppa/llm.env")
    ap.add_argument("--tg-env", default="/srv/kauppa/.env")
    ap.add_argument("--ei-tg", action="store_true")
    ap.add_argument("--spec-tiedosto", help="testaa kasin kirjoitettu speksi (JSON), ei LLM:aa; laskee perheeseen mutta ei viikkokiintioon")
    ap.add_argument("--satunnaisia", type=int, default=0)
    ap.add_argument("--kuiva", action="store_true",
                    help="vain ehdotuksen laatu: yksi LLM-kutsu, ei rekisterointia, ei testia, ei kiintio- tai budjettikulutusta (mallivertailuun)")
    ap.add_argument("--db", default=config.DB_POLKU)
    ap.add_argument("--rekisteri", default=rajat.KOKEILU_DB)
    ap.add_argument("--data", choices=("rehellinen", "markkina"), default="rehellinen",
                    help="rehellinen = historia.db point-in-time kaikilla pareilla (K12, oletus); markkina = vanha jalkikateen valittu lista")
    a = ap.parse_args(argv)
    if os.path.exists(rajat.PYSAYTYS):
        print("PYSAYTETTY (kill switch), K7 ei aja")
        return 0
    tanaan = datetime.now(timezone.utc).date()
    rek = avaa(a.rekisteri)
    try:
        env_tg = llm.lue_env(a.tg_env)
    except llm.LLMVirhe:
        env_tg = {}
    specit, hylatyt, lahde = [], [], "kasin"
    if a.kuiva:
        return kuiva_ehdotus(rek, a, env_tg)
    if a.spec_tiedosto:
        with open(a.spec_tiedosto) as f:
            try:
                specit = [tarkista_spec(json.load(f))]
            except (Speksivirhe, ValueError) as e:
                print("speksi hylatty: %s" % e)
                return 2
    else:
        lahde = "llm"
        n = min(kiintiota_jaljella(rek, tanaan), budjettia_jaljella(rek))
        if budjettia_jaljella(rek) == 0:
            print("K7 perhebudjetti (%d) taynna: Tony paattaa jatkosta" % K7_PERHE_BUDJETTI)
            return 0
        if n == 0:
            print("viikkokiintio (%d) taynna: %s" % (K7_VIIKKO_KIINTIO, viikko(tanaan)))
            return 0
        if a.data == "rehellinen" and not os.path.exists(laaja.HISTORIA_DB):   # ennen LLM-kutsua: ei turhaa kulua
            print("K7 pysahtyy: historia.db puuttuu (%s)" % laaja.HISTORIA_DB)
            if not a.ei_tg:
                notify.laheta("K7 pysahtyi: historia.db puuttuu", env_tg)
            return 2
        alkoi = time.monotonic()
        try:
            specit, hylatyt = ehdota(rek, n, rajat.MALLIT[a.malli], llm.lue_env(a.llm_env))
            tapahtumat.kirjaa("k7", "ok" if specit else "ei_tietoa", ryhma=a.malli, malli=rajat.MALLIT[a.malli], ehdotettu=len(specit) + len(hylatyt),
                              kelvollinen=len(specit), hylatty=len(hylatyt), kesto_s=round(time.monotonic() - alkoi, 1))
        except (llm.LLMVirhe, agentit.Vastausvirhe) as e:
            tapahtumat.kirjaa("k7", "virhe", ryhma=a.malli, malli=rajat.MALLIT[a.malli], virhekoodi=type(e).__name__, kesto_s=round(time.monotonic() - alkoi, 1))
            msg = "K7: ehdotus epaonnistui (%s), ei testeja" % e
            print(msg)
            if not a.ei_tg:
                notify.laheta(msg, env_tg)
            return 1
    if not specit:
        print("ei testattavia hypoteeseja; hylatyt: %s" % hylatyt)
        return 0
    if a.data == "rehellinen":
        try:
            data = laaja.rehellinen_data()
        except laaja.HistoriaPuuttuu as e:
            print("K7 pysahtyy: %s" % e)
            if not a.ei_tg:
                notify.laheta("K7 pysahtyi: historia.db puuttuu", env_tg)
            return 2
    else:
        data = bt.Data(sqlite3.connect("file:%s?mode=ro" % a.db, uri=True, timeout=30))
    tulokset = []
    for spec in specit:
        if budjettia_jaljella(rek) == 0:
            hylatyt.append("%s: perhebudjetti taynna" % spec["nimi"])
            continue
        hid, nimi = rekisteroi(rek, spec, lahde, tanaan)
        if hid is None:
            hylatyt.append("%s: sama speksi testattu jo" % spec["nimi"])
            continue
        a_, t = aja_hypoteesi(rek, data, hid, nimi, spec, perhe_koko(rek), satunnaisia=a.satunnaisia or None,
                              edistyminen=lambda i, f: print("  fold %d" % i, flush=True))
        tulokset.append((spec, t))
        print(t, flush=True)
    msg = raportti(tulokset, hylatyt, perhe_koko(rek))
    print(msg)
    if not a.ei_tg:
        notify.laheta(msg, env_tg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
