"""K6: rehellinen backtest. Saannot:
 1. Sama simulaattori kuin oikeassa paperikaupassa (salkku.suorita_paiva: kulu, slippage, riskirajat, point-in-time-universumi).
 2. Paatos vain datalla ENNEN paivaa D (Data.sulut palauttaa vain ts < D).
 3. Jokainen ajo kirjataan kokeilurekisteriin (kokeilujen maara tiedossa -> monivertailukorjaus).
 4. Walk-forward: parametrit valitaan vain opetusjaksolla, raportoidaan vain testijaksot (OOS).
 5. Lukittu loppujakso (rajat.HOLDOUT_ALKU): aja() kieltaytyy, yksi katsaus per strategia (holdout_arvioi).
 6. Satunnaisvertailu: samat k, osuus ja rebalanssitiheys, satunnaiset kolikot -> p-arvo.
Ei LLM:aa. Puhdasta koodia."""
import hashlib
import json
import random
import sqlite3
import statistics
from datetime import date, datetime, timedelta, timezone

import kynnys as kyn
import mittarit as mit
import rajat
import salkku
import universumi as uni

PV = 86400000
KOODIVERSIO = "k8-1"   # k8-1: auditointikorjaukset (deterministinen jarjestys, holdout-vartija _aja:ssa, foldikohtainen satunnaisvertailu)


class Holdoutvirhe(Exception):
    pass


def _ms(p):
    return int(datetime(p.year, p.month, p.day, tzinfo=timezone.utc).timestamp() * 1000)


def holdout_alku():
    return date(*rajat.HOLDOUT_ALKU)


class Data:
    """Hinnat muistissa. sulut() palauttaa vain ts < d_ms (ei tulevaisuutta)."""

    def __init__(self, con, n_universumi=20):
        self.con = con
        self.n = n_universumi
        self.close, self.open = {}, {}
        for s, ts, o, c in con.execute("SELECT symbol, ts, open, close FROM hinnat ORDER BY symbol, ts"):
            self.close.setdefault(s, {})[ts] = c
            self.open.setdefault(s, {})[ts] = o
        self._uni = {}

    def sulut(self, symbol, d_ms, n):
        """n viimeista sulkuhintaa ennen d_ms aikajarjestyksessa; None jos aukkoja/puuttuu."""
        d = self.close.get(symbol)
        if d is None:
            return None
        ulos = []
        for k in range(n, 0, -1):
            v = d.get(d_ms - k * PV)
            if v is None:
                return None
            ulos.append(v)
        return ulos

    def universum(self, d_ms):
        if d_ms not in self._uni:
            self._uni[d_ms] = [s for s, _ in uni.universumi(self.con, d_ms, n=self.n)]
        return self._uni[d_ms]


# ---------- strategiat (tehtaat: jokainen ajo saa tuoreen tilan) ----------

def momentum(ikkuna=90, k=5, osuus=0.8, reb=7):
    """Poikkileikkausmomentum: top-k universumista viime ikkuna paivan tuoton mukaan, tasapainot, rebalanssi reb paivan valein."""
    def tehdas():
        tila = {"n": 0, "paino": {}}

        def strat(data, paiva, sallitut):
            d_ms = _ms(paiva)
            if tila["n"] % reb == 0:
                pisteet = []
                for s in sallitut:
                    c = data.sulut(s, d_ms, ikkuna + 1)
                    if c:
                        pisteet.append((c[-1] / c[0] - 1, s))
                pisteet.sort(key=lambda x: (-x[0], x[1]))
                valitut = [s for _, s in pisteet[:k]]
                tila["paino"] = {s: osuus / k for s in valitut}
            tila["n"] += 1
            return dict(tila["paino"])
        return strat
    return tehdas


def tasapaino(osuus=0.8):
    """Kaikki universumin kolikot tasapainoin (jatkuva rebalanssi pienten kauppojen rajalla)."""
    def tehdas():
        def strat(data, paiva, sallitut):
            n = len(sallitut)
            return {s: osuus / n for s in sallitut} if n else {}
        return strat
    return tehdas


def satunnainen(seed, k=5, osuus=0.8, reb=7, ikkuna=None):
    """Satunnaiset k kolikkoa universumista, samat osuus ja rebalanssitiheys kuin vertailtavalla.
    ikkuna: jos annettu, arvotaan vain kolikoista joilla on taysi ikkunan mittainen historia (kuten kandidaatilla)."""
    def tehdas():
        rnd = random.Random(seed)
        tila = {"n": 0, "paino": {}}

        def strat(data, paiva, sallitut):
            if tila["n"] % reb == 0:
                lista = sorted(sallitut)
                if ikkuna:
                    d_ms = _ms(paiva)
                    lista = [s for s in lista if data.sulut(s, d_ms, ikkuna + 1)]
                valitut = rnd.sample(lista, min(k, len(lista)))
                tila["paino"] = {s: osuus / max(1, len(valitut)) for s in valitut}
            tila["n"] += 1
            return dict(tila["paino"])
        return strat
    return tehdas


def vain_btc(osuus=0.9):
    def tehdas():
        def strat(data, paiva, sallitut):
            return {"BTCUSDT": osuus} if "BTCUSDT" in sallitut else {}
        return strat
    return tehdas


# ---------- rekisteri ----------

REK_SKEEMA = """
CREATE TABLE IF NOT EXISTS kokeilut (
  id INTEGER PRIMARY KEY AUTOINCREMENT, aika TEXT NOT NULL, nimi TEXT NOT NULL, parametrit TEXT NOT NULL,
  jakso TEXT NOT NULL, alku TEXT NOT NULL, loppu TEXT NOT NULL,
  tuotto REAL, sharpe REAL, max_dd REAL, kauppoja INTEGER, versio TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS holdout_kaytto (nimi TEXT PRIMARY KEY, aika TEXT NOT NULL, tulos TEXT NOT NULL);
"""


def avaa_rekisteri(polku=None):
    con = sqlite3.connect(polku or rajat.KOKEILU_DB, timeout=30)
    con.executescript(REK_SKEEMA)
    return con


def kirjaa(rek, nimi, parametrit, jakso, alku, loppu, m, kauppoja):
    rek.execute(
        "INSERT INTO kokeilut (aika,nimi,parametrit,jakso,alku,loppu,tuotto,sharpe,max_dd,kauppoja,versio) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (datetime.now(timezone.utc).isoformat(timespec="seconds"), nimi, json.dumps(parametrit, sort_keys=True), jakso,
         alku.isoformat(), loppu.isoformat(), m and m["tuotto"], m and m["sharpe_pa"], m and m["max_dd"], kauppoja, KOODIVERSIO))
    rek.commit()


def kokeiluja(rek, nimi=None):
    if nimi is None:
        return rek.execute("SELECT COUNT(*) FROM kokeilut").fetchone()[0]
    return rek.execute("SELECT COUNT(*) FROM kokeilut WHERE nimi=?", (nimi,)).fetchone()[0]


# ---------- ajo ----------

def _paivat(alku, loppu):
    p = alku
    while p <= loppu:
        yield p
        p += timedelta(days=1)


_HOLDOUT_LUPA = object()   # vain holdout_arvioi antaa taman _aja:lle


class _Nakyma:
    """Strategia nakee vain sulut() (ts < d). Ei paasya data.close/open/con -> ei tulevaisuutta vahingossakaan."""
    __slots__ = ("_d",)

    def __init__(self, data):
        self._d = data

    def sulut(self, symbol, d_ms, n):
        return self._d.sulut(symbol, d_ms, n)


def _aja(tehdas, data, alku, loppu, _lupa=None, kynnys_pp=None):
    """Sisainen ajo. Kieltaytyy lukitusta jaksosta ellei kutsuja ole holdout_arvioi. Palauttaa (arvot_alkupaoma_mukana, kauppoja)."""
    if loppu >= holdout_alku() and _lupa is not _HOLDOUT_LUPA:
        raise Holdoutvirhe("jakso ylittaa lukitun loppujakson (%s alkaen)" % holdout_alku())
    sal = salkku.avaa(":memory:")
    strat = tehdas()
    nak = _Nakyma(data)
    for p in _paivat(alku, loppu):
        sallitut = data.universum(_ms(p))
        ehd = strat(nak, p, sallitut)
        ehd = kyn.suodata(sal, data.con, p, ehd, set(sallitut), kynnys_pp)
        salkku.suorita_paiva(sal, data.con, p, ehd, set(sallitut))
    arvot = [rajat.ALKUPAOMA] + [r[0] for r in sal.execute("SELECT arvo FROM arvot ORDER BY paiva")]
    kauppoja = sal.execute("SELECT COUNT(*) FROM kaupat").fetchone()[0]
    sal.close()
    return arvot, kauppoja


def aja(tehdas, data, alku, loppu, rek=None, nimi="?", parametrit=None, jakso="kehitys", kynnys_pp=None):
    if loppu >= holdout_alku():
        raise Holdoutvirhe("jakso ylittaa lukitun loppujakson (%s alkaen); kayta holdout_arvioi()" % holdout_alku())
    arvot, kauppoja = _aja(tehdas, data, alku, loppu, kynnys_pp=kynnys_pp)
    m = mit.mittarit(arvot)
    if rek is not None:
        kirjaa(rek, nimi, parametrit or {}, jakso, alku, loppu, m, kauppoja)
    return {"arvot": arvot, "mittarit": m, "kauppoja": kauppoja}


def holdout_arvioi(tehdas, data, rek, nimi, parametrit, loppu=None):
    """Yksi katsaus per nimi. Toinen kutsu samalla nimella kieltaytyy (ja se on kirjattu)."""
    # Avain = normalisoitu nimi (+ parametrien tiiviste jos annettu): uudelleennimeaminen ei anna toista katsausta.
    # Rivit lisataan ENNEN ajoa (PRIMARY KEY -> atominen); kesken jaanyt ajo kuluttaa katsauksen (konservatiivinen).
    avaimet = [" ".join(str(nimi).lower().split())]
    if parametrit:
        avaimet.append("par:" + hashlib.sha256(json.dumps(parametrit, sort_keys=True).encode()).hexdigest()[:16])
    lisatyt = []
    for av in avaimet:
        try:
            rek.execute("INSERT INTO holdout_kaytto VALUES (?,?,?)", (av, datetime.now(timezone.utc).isoformat(timespec="seconds"), "kaynnissa"))
            lisatyt.append(av)
        except sqlite3.IntegrityError:
            for a2 in lisatyt:
                rek.execute("DELETE FROM holdout_kaytto WHERE nimi=? AND tulos='kaynnissa'", (a2,))
            rek.commit()
            raise Holdoutvirhe("holdout on jo katsottu (%s)" % av)
    rek.commit()
    alku = holdout_alku()
    if loppu is None:
        viim = max(data.close.get("BTCUSDT", {_ms(alku): 0}))
        loppu = datetime.fromtimestamp(viim / 1000, timezone.utc).date()
    arvot, kauppoja = _aja(tehdas, data, alku, loppu, _HOLDOUT_LUPA)
    m = mit.mittarit(arvot)
    for av in lisatyt:
        rek.execute("UPDATE holdout_kaytto SET tulos=? WHERE nimi=?", (json.dumps(m), av))
    rek.commit()
    kirjaa(rek, nimi, parametrit, "holdout", alku, loppu, m, kauppoja)
    return {"arvot": arvot, "mittarit": m, "kauppoja": kauppoja}


def _ketju(osat):
    """Yhdistaa jaksojen tuotot yhdeksi arvosarjaksi (alkupaoma 10000)."""
    arvot = [rajat.ALKUPAOMA]
    for a in osat:
        for i in range(1, len(a)):
            arvot.append(arvot[-1] * (a[i] / a[i - 1]))
    return arvot


def ikkunat(alku, loppu, train_pv=365, testi_pv=90):
    """[(train_alku, train_loppu, testi_alku, testi_loppu)], testijaksot eivat mene paallekkain, train aina ennen testia."""
    ulos, t = [], alku
    while True:
        tr_a, tr_l = t, t + timedelta(days=train_pv - 1)
        te_a, te_l = tr_l + timedelta(days=1), tr_l + timedelta(days=testi_pv)
        if te_l > loppu:
            break
        ulos.append((tr_a, tr_l, te_a, te_l))
        t += timedelta(days=testi_pv)
    return ulos


def walk_forward(nimi, tehdas_fn, ruudukko, data, rek, alku=None, loppu=None, train_pv=365, testi_pv=90, edistyminen=None):
    """tehdas_fn(**parametrit) -> tehdas. ruudukko: lista parametri-dictejä. Valinta vain train-jaksolla (Sharpe)."""
    alku = alku or date(*rajat.BACKTEST_ALKU)
    loppu = loppu or (holdout_alku() - timedelta(days=1))
    if loppu >= holdout_alku():
        raise Holdoutvirhe("walk-forward ei saa ulottua lukittuun jaksoon")
    foldit, osat = [], []
    for i, (a, b, c, d) in enumerate(ikkunat(alku, loppu, train_pv, testi_pv)):
        paras, paras_s = None, None
        for par in ruudukko:
            r = aja(tehdas_fn(**par), data, a, b, rek, nimi, par, "wf-train")
            s = (r["mittarit"] or {}).get("sharpe_pa")
            s = -1e9 if s is None else s
            if paras_s is None or s > paras_s:
                paras, paras_s = par, s
        r = aja(tehdas_fn(**paras), data, c, d, rek, nimi, paras, "wf-oos")
        osat.append(r["arvot"])
        foldit.append({"train": (a, b), "testi": (c, d), "valittu": paras, "train_sharpe": paras_s,
                       "oos_tuotto": (r["mittarit"] or {}).get("tuotto")})
        if edistyminen:
            edistyminen(i + 1, foldit[-1])
    arvot = _ketju(osat)
    return {"arvot": arvot, "mittarit": mit.mittarit(arvot), "foldit": foldit,
            "ikkunat": [(f["testi"][0], f["testi"][1]) for f in foldit]}


def _per(x, w):
    return x[w] if isinstance(x, (list, tuple)) else x


def satunnaisjakauma(data, testi_ikkunat, n, k=5, osuus=0.8, reb=7, siemen=1000, ikkuna=None):
    """n satunnaista strategiaa ajettuna SAMOILLA testijaksoilla kuin kandidaatti. Palauttaa lista mittareita.
    k, osuus, reb, ikkuna: yksi arvo TAI lista (yksi arvo per testi-ikkuna = kandidaatin foldikohtainen valinta, auditointi A3).
    Siemen vaihtuu ikkunoittain (ei samaa arvontajonoa joka foldissa)."""
    ulos = []
    for i in range(n):
        osat = [_aja(satunnainen(siemen + i + 100003 * w, _per(k, w), _per(osuus, w), _per(reb, w), _per(ikkuna, w)), data, a, b)[0]
                for w, (a, b) in enumerate(testi_ikkunat)]
        m = mit.mittarit(_ketju(osat))
        ulos.append(m)
    return ulos


def p_arvo(kandidaatti_sharpe, satunnaiset):
    """Osuus satunnaisista, joiden Sharpe >= kandidaatin (+1 -korjaus). Pieni = harvinaista sattumalta."""
    s = [m["sharpe_pa"] for m in satunnaiset if m and m["sharpe_pa"] is not None]
    if kandidaatti_sharpe is None or not s:
        return 1.0
    return (sum(1 for x in s if x >= kandidaatti_sharpe) + 1) / (len(s) + 1)


def btc_vertailu(data, ikkunat_):
    if any(b >= holdout_alku() for _, b in ikkunat_):
        raise Holdoutvirhe("BTC-vertailu ei saa ulottua lukittuun jaksoon")
    osat = []
    for a, b in ikkunat_:
        osat.append([v for _, v in mit_vertailu(data, a, b)])
    arvot = _ketju([[rajat.ALKUPAOMA] + o for o in osat])
    return {"arvot": arvot, "mittarit": mit.mittarit(arvot)}


def mit_vertailu(data, a, b):
    return mit.vertailusalkku(data.con, {"BTCUSDT": 0.8}, a, b)


def arvio(wf, satunnaiset, btc, ruudukon_koko, alpha=0.05):
    """Palauttaa sanakirjan, jossa kaikki kolme ehtoa erikseen. Ei 'kyllä'-vastausta jos yksikin pettaa."""
    m = wf["mittarit"] or {}
    p = p_arvo(m.get("sharpe_pa"), satunnaiset)
    n_kelvollisia = sum(1 for x in satunnaiset if x and x.get("sharpe_pa") is not None)   # sama joukko kuin p_arvo kayttaa
    riittaa = (1.0 / (n_kelvollisia + 1)) * ruudukon_koko < alpha  # pienin mahdollinen korjattu p alle rajan
    p_korj = min(1.0, p * ruudukon_koko)  # Bonferroni: kandidaatti valittiin ruudukosta
    bm = btc["mittarit"] or {}
    return {
        "oos_tuotto": m.get("tuotto"), "oos_sharpe": m.get("sharpe_pa"), "oos_max_dd": m.get("max_dd"),
        "btc_tuotto": bm.get("tuotto"), "btc_sharpe": bm.get("sharpe_pa"),
        "p_satunnainen": p, "p_korjattu": p_korj, "satunnaisia": len(satunnaiset), "riittavasti_satunnaisia": riittaa,
        "voittaa_satunnaiset": p_korj < alpha,
        "voittaa_btc_tuotto": (m.get("tuotto") is not None and bm.get("tuotto") is not None and m["tuotto"] > bm["tuotto"]),
        "voittaa_btc_sharpe": (m.get("sharpe_pa") is not None and bm.get("sharpe_pa") is not None and m["sharpe_pa"] > bm["sharpe_pa"]),
    }


def teksti(nimi, a, kokeiluja_yht, rehellinen=False):
    ok = a["riittavasti_satunnaisia"] and a["voittaa_satunnaiset"] and a["voittaa_btc_tuotto"] and a["voittaa_btc_sharpe"]
    f = lambda x, p=1: "n/a" if x is None else ("%.*f" % (p, x * 100)) + " %"
    return "\n".join([
        "Strategia: %s" % nimi,
        "OOS tuotto %s, max dd %s, Sharpe %s | BTC (80 %%) tuotto %s, Sharpe %s" % (
            f(a["oos_tuotto"]), f(a["oos_max_dd"]), "n/a" if a["oos_sharpe"] is None else "%.2f" % a["oos_sharpe"],
            f(a["btc_tuotto"]), "n/a" if a["btc_sharpe"] is None else "%.2f" % a["btc_sharpe"]),
        "Satunnaisvertailu (%d satunnaista): p=%.3f, ruudukkokorjattu p=%.3f -> %s%s" % (
            a["satunnaisia"], a["p_satunnainen"], a["p_korjattu"], "voittaa" if a["voittaa_satunnaiset"] else "EI voita",
            "" if a["riittavasti_satunnaisia"] else " (LIIAN VAHAN SATUNNAISIA: tulos ei voi ylittaa rajaa)"),
        "BTC: tuotto %s, Sharpe %s" % ("voittaa" if a["voittaa_btc_tuotto"] else "EI voita",
                                       "voittaa" if a["voittaa_btc_sharpe"] else "EI voita"),
        ("Universumi: point-in-time top 20 volyymin mukaan kaikista USDT-pareista, myos myohemmin kuolleet (delistaus-haircut 10 %); ei jalkikateen valittua listaa."
         if rehellinen else
         "HUOM universumi: kolikkolista on jalkikateen valittu (2026), ei sisalla kuolleita kolikoita -> absoluuttiset tuotot ja BTC-vertailu ovat optimistisia."),
        "Kokeiluja rekisterissä yhteensä: %d" % kokeiluja_yht,
        "TULOS: %s" % ("EHDOLLA KELPAA -> seuraavaksi holdout (yksi katsaus)" if ok else "EI KELPAA"),
    ])


def main():
    import argparse
    import config
    import db
    ap = argparse.ArgumentParser(description="K6 walk-forward (ei koske holdoutiin)")
    ap.add_argument("--satunnaisia", type=int, default=0, help="0 = automaattinen (25 x ruudukon koko, min 200)")
    ap.add_argument("--db", default=config.DB_POLKU)
    ap.add_argument("--rekisteri", default=rajat.KOKEILU_DB)
    ap.add_argument("--data", choices=("rehellinen", "markkina"), default="rehellinen",
                    help="rehellinen = historia.db point-in-time (K12, oletus); markkina = vanha jalkikateen valittu lista")
    args = ap.parse_args()
    if args.data == "rehellinen":
        import laaja
        try:
            data = laaja.rehellinen_data()
        except laaja.HistoriaPuuttuu as e:
            print(e)
            return 2
    else:
        data = Data(db.avaa(args.db))
    rek = avaa_rekisteri(args.rekisteri)
    ruudukko = [{"ikkuna": i, "k": k, "reb": r} for i in (30, 60, 90) for k in (3, 5, 8) for r in (7, 14, 30)]
    wf = walk_forward("momentum", lambda **p: momentum(**p), ruudukko, data, rek,
                      edistyminen=lambda i, f: print("fold %d: valittu %s, OOS %s" % (i, f["valittu"], f["oos_tuotto"]), flush=True))
    f = wf["foldit"]
    sat = satunnaisjakauma(data, wf["ikkunat"], args.satunnaisia or max(200, 25 * len(ruudukko)),
                           k=[x["valittu"]["k"] for x in f], reb=[x["valittu"]["reb"] for x in f], ikkuna=[x["valittu"]["ikkuna"] for x in f])
    btc = btc_vertailu(data, wf["ikkunat"])
    a = arvio(wf, sat, btc, len(ruudukko))
    print(teksti("momentum (walk-forward, ruudukko %d)" % len(ruudukko), a, kokeiluja(rek), rehellinen=args.data == "rehellinen"))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
