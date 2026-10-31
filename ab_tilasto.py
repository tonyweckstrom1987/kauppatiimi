"""A/B-vertailun tilastollinen arvio: flash vs pro (vain luku, ei kaupankaynti- tai rajalogiikkaa).
Laskee paivatuottojen eron, Sharpen, max drawdownin, bootstrap-luottamusvalin erolle (parittainen, lohko-bootstrap)
ja sen, montako paivaa dataa tarvitaan merkitsevyyteen nykyisella hajonnalla.
Kaytto: python3 ab_tilasto.py [--flash polku.db] [--pro polku.db] [--lohko 5] [--toistoja 5000] [--siemen 1]
Oletuspolut: rajat.SALKUT. Salkku-DB avataan vain luku -tilassa."""
import argparse
import math
import random
import sqlite3
import statistics
import sys
from statistics import NormalDist

VUOSI = 365  # kryptot: kauppa joka paiva
MIN_PAIVAT = 10


def lue_arvot(polku):
    """Palauttaa {paiva: arvo} salkun arvot-taulusta. Avaa tiedoston vain luku."""
    con = sqlite3.connect("file:%s?mode=ro" % polku, uri=True, timeout=30)
    try:
        return dict(con.execute("SELECT paiva, arvo FROM arvot ORDER BY paiva").fetchall())
    finally:
        con.close()


def yhteiset(a, b):
    """Kahden {paiva: arvo} -sanakirjan yhteiset paivat jarjestyksessa -> (paivat, arvot_a, arvot_b)."""
    p = sorted(set(a) & set(b))
    return p, [a[x] for x in p], [b[x] for x in p]


def paivatuotot(arvot):
    return [arvot[i] / arvot[i - 1] - 1 for i in range(1, len(arvot))]


def max_drawdown(arvot):
    huippu, mdd = arvot[0], 0.0
    for a in arvot:
        huippu = max(huippu, a)
        mdd = max(mdd, 1 - a / huippu)
    return mdd


def sharpe(tuotot):
    """Vuosistettu Sharpe (rf=0). None jos hajonta nolla tai alle 3 havaintoa."""
    if len(tuotot) < 3:
        return None
    sd = statistics.stdev(tuotot)
    if sd == 0:
        return None
    return statistics.fmean(tuotot) / sd * math.sqrt(VUOSI)


def _bootstrap_indeksit(n, lohko, rng):
    """Ympyralohko-bootstrap: n indeksia, lohkopituus 'lohko' (1 = tavallinen bootstrap)."""
    idx = []
    while len(idx) < n:
        alku = rng.randrange(n)
        idx.extend((alku + k) % n for k in range(lohko))
    return idx[:n]


def bootstrap_ci(fl, pr, lohko=5, toistoja=5000, siemen=1, taso=0.95):
    """Parittainen bootstrap: samat paivat arvotaan molemmille salkuille.
    Palauttaa {"ero_ka": (matala, korkea), "sharpe_ero": (matala, korkea)}."""
    n = len(fl)
    rng = random.Random(siemen)
    lohko = max(1, min(lohko, n))
    erot, shero = [], []
    for _ in range(toistoja):
        idx = _bootstrap_indeksit(n, lohko, rng)
        f = [fl[i] for i in idx]
        p = [pr[i] for i in idx]
        erot.append(statistics.fmean(p) - statistics.fmean(f))
        sf, sp = sharpe(f), sharpe(p)
        if sf is not None and sp is not None:
            shero.append(sp - sf)
    a = (1 - taso) / 2

    def kvant(x):
        x = sorted(x)
        if not x:
            return None
        return x[int(a * (len(x) - 1))], x[int(round((1 - a) * (len(x) - 1)))]
    return {"ero_ka": kvant(erot), "sharpe_ero": kvant(shero)}


def tarvittavat_paivat(sd, ero, alfa=0.05, teho=0.8):
    """Parillisen t-testin (normaaliapproksimaatio) vaatima n: ((z_a/2 + z_b) * sd / ero)^2.
    sd = paivaerojen keskihajonta, ero = oletettu keskimaarainen paivaero. None jos ero = 0."""
    if not ero or sd <= 0:
        return None
    z = NormalDist().inv_cdf
    return math.ceil(((z(1 - alfa / 2) + z(teho)) * sd / abs(ero)) ** 2)


def pienin_havaittava_ero(sd, n, alfa=0.05, teho=0.8):
    """Pienin keskimaarainen paivaero, jonka n paivaa ehtii erottaa kohinasta (alfa, teho)."""
    z = NormalDist().inv_cdf
    return (z(1 - alfa / 2) + z(teho)) * sd / math.sqrt(n)


def analysoi(a_fl, a_pr, lohko=5, toistoja=5000, siemen=1):
    """a_fl, a_pr: {paiva: arvo}. Palauttaa tulos-dictin tai {"virhe": ...} jos dataa liian vahan."""
    p, af, ap = yhteiset(a_fl, a_pr)
    if len(p) < MIN_PAIVAT + 1:
        return {"virhe": "data ei riita: yhteisia paivia %d, tarvitaan vahintaan %d" % (len(p), MIN_PAIVAT + 1)}
    if min(af) <= 0 or min(ap) <= 0:
        return {"virhe": "salkun arvo <= 0, tuottoja ei voi laskea"}
    tf, tp = paivatuotot(af), paivatuotot(ap)
    ero = [y - x for x, y in zip(tf, tp)]
    n = len(ero)
    ka = statistics.fmean(ero)
    sd = statistics.stdev(ero)
    t = ka / (sd / math.sqrt(n)) if sd > 0 else None
    p_arvo = 2 * (1 - NormalDist().cdf(abs(t))) if t is not None else None
    return {
        "paivia": n, "alku": p[0], "loppu": p[-1],
        "flash": {"tuotto": af[-1] / af[0] - 1, "sharpe": sharpe(tf), "max_dd": max_drawdown(af), "ka_pv": statistics.fmean(tf)},
        "pro": {"tuotto": ap[-1] / ap[0] - 1, "sharpe": sharpe(tp), "max_dd": max_drawdown(ap), "ka_pv": statistics.fmean(tp)},
        "ero_ka_pv": ka, "ero_sd_pv": sd, "t": t, "p": p_arvo,
        "ci": bootstrap_ci(tf, tp, lohko, toistoja, siemen),
        "n_tarvitaan_havaitulle": tarvittavat_paivat(sd, ka),
        "n_tarvitaan": {e: tarvittavat_paivat(sd, e) for e in (0.0005, 0.001, 0.002)},
        "pienin_havaittava_nyt": pienin_havaittava_ero(sd, n),
    }


def _f(x, d=3, pct=False):
    if x is None:
        return "n/a"
    return ("%.2f %%" % (x * 100)) if pct else ("%.*f" % (d, x))


def muotoile(r):
    if "virhe" in r:
        return r["virhe"]
    o = ["Jakso %s .. %s, %d paivatuottoa" % (r["alku"], r["loppu"], r["paivia"]),
         "malli | tuotto | Sharpe (vuosi) | max dd | ka/pv"]
    for k in ("flash", "pro"):
        m = r[k]
        o.append("%s | %s | %s | %s | %s" % (k, _f(m["tuotto"], pct=True), _f(m["sharpe"], 2), _f(m["max_dd"], pct=True), _f(m["ka_pv"], pct=True)))
    ci = r["ci"]["ero_ka"]
    o.append("Ero (pro - flash), keskimaarin per paiva: %s, sd %s" % (_f(r["ero_ka_pv"], pct=True), _f(r["ero_sd_pv"], pct=True)))
    o.append("  95 %% bootstrap-CI erolle: %s" % ("n/a" if ci is None else "[%s, %s]" % (_f(ci[0], pct=True), _f(ci[1], pct=True))))
    sc = r["ci"]["sharpe_ero"]
    o.append("  95 %% bootstrap-CI Sharpe-erolle: %s" % ("n/a" if sc is None else "[%s, %s]" % (_f(sc[0], 2), _f(sc[1], 2))))
    o.append("  t = %s, p = %s (normaaliapproksimaatio)" % (_f(r["t"], 2), _f(r["p"], 3)))
    merk = ci is not None and (ci[0] > 0 or ci[1] < 0)
    o.append("Tulos: ero %s tilastollisesti merkitseva (CI %s nollan)." % ("ON" if merk else "EI ole", "ei sisalla" if merk else "sisaltaa"))
    nh = r["n_tarvitaan_havaitulle"]
    o.append("Paivia tarvitaan, jos nykyinen ka-ero olisi todellinen (alfa 5 %%, teho 80 %%): %s" % ("n/a" if nh is None else "%d (nyt %d)" % (nh, r["paivia"])))
    for e, n in r["n_tarvitaan"].items():
        o.append("  jos todellinen ero olisi %s/pv: %s paivaa" % (_f(e, pct=True), n))
    o.append("Pienin ero, jonka %d paivaa ehtii havaita: %s/pv" % (r["paivia"], _f(r["pienin_havaittava_nyt"], pct=True)))
    o.append("Huom: lyhyt data -> estimaatit epavarmoja; ei johtopaatoksia ilman riittavaa n:aa.")
    return "\n".join(o)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--flash")
    ap.add_argument("--pro")
    ap.add_argument("--lohko", type=int, default=5)
    ap.add_argument("--toistoja", type=int, default=5000)
    ap.add_argument("--siemen", type=int, default=1)
    a = ap.parse_args(argv)
    if not (a.flash and a.pro):
        import rajat
        a.flash = a.flash or rajat.SALKUT["flash"]
        a.pro = a.pro or rajat.SALKUT["pro"]
    try:
        r = analysoi(lue_arvot(a.flash), lue_arvot(a.pro), a.lohko, a.toistoja, a.siemen)
    except sqlite3.Error as e:
        print("Virhe: %s" % type(e).__name__)
        return 1
    print(muotoile(r))
    return 1 if "virhe" in r else 0


if __name__ == "__main__":
    sys.exit(main())
