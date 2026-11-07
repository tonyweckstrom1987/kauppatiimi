"""Passiivisten tapojen vertailu historiadatalla (vain luku, ei oikeaa rahaa, ei sijoitusneuvo).
Tavat: (a) BTC kuukausiosto, (b) S&P 500 -hintaindeksin kuukausiosto, (c) yhdistelmat 80/20 ja 50/50 (BTC/indeksi),
vuosittainen tasapainotus. Kulut: rajat.KULU + rajat.SLIPPAGE per kauppa (sama kuin kauppatiimin salkuissa).
Aineisto A: 12 eraa (alkupaaoma/12, ostamaton osa kateisena), 12 kuukauden jakso, 12 aloituskuukautta 3 kuukauden valein.
Aineisto B: jatkuva kuukausisaasto (talletus) aloituksesta holdoutia edeltavaan paivaan, tasapainotus 12 kk valein.
Holdout (rajat.HOLDOUT_ALKU) pysyy lukittuna: jakso paattyy viimeistaan paivaa ennen sita.
Kaytto: python3 passiivinen_vertailu.py <historia.db> <indeksi.csv>"""
import sqlite3
import statistics
import sys
from datetime import date, datetime, timedelta, timezone

import indeksidata
import passiivinen_kk as pk
import rajat

ALKU0 = date(2022, 2, 1)         # sama alku kuin kokeet.jakso()/BACKTEST_ALKU
ALOITUKSIA = 12
VALI_KK = 3
TAVAT = [
    ("BTC kuukausiosto", {"BTCUSDT": 1.0}),
    ("S&P 500 kuukausiosto", {"SPX": 1.0}),
    ("Yhdistelma 80/20 (BTC/indeksi)", {"BTCUSDT": 0.8, "SPX": 0.2}),
    ("Yhdistelma 50/50 (BTC/indeksi)", {"BTCUSDT": 0.5, "SPX": 0.5}),
]


def holdout_loppu():
    return date(*rajat.HOLDOUT_ALKU) - timedelta(days=1)


def lue_btc(polku, loppu):
    if loppu > holdout_loppu():
        raise ValueError("jakso ylittaa lukitun loppujakson")
    con = sqlite3.connect("file:%s?immutable=1" % polku, uri=True, timeout=10)
    try:
        raja = int(datetime(loppu.year, loppu.month, loppu.day, tzinfo=timezone.utc).timestamp() * 1000)
        avaus, sulku = {}, {}
        for ts, o, c in con.execute("SELECT ts, open, close FROM hinnat WHERE symbol='BTCUSDT' AND ts<=? ORDER BY ts", (raja,)):
            p = datetime.fromtimestamp(ts / 1000, timezone.utc).date()
            avaus[p], sulku[p] = o, c
    finally:
        con.close()
    return {"open": avaus, "close": sulku}


def aloitukset(n=ALOITUKSIA, vali=VALI_KK, alku=ALKU0):
    return [pk.lisaa_kuukausia(alku, k * vali) for k in range(n)]


def aja_aineisto_a(hinnat, painot, alku):
    """12 eraa, 12 kuukauden jakso."""
    loppu = pk.lisaa_kuukausia(alku, 12) - timedelta(days=1)
    return pk.simuloi(hinnat, painot, alku, loppu, rajat.ALKUPAOMA / pk.ERIA, erat=pk.ERIA, alkukassa=rajat.ALKUPAOMA)


def aja_aineisto_b(hinnat, painot, alku, loppu):
    """Jatkuva kuukausisaasto (sama kuukausiera kuin A:ssa), vuosittainen tasapainotus."""
    return pk.simuloi(hinnat, painot, alku, loppu, rajat.ALKUPAOMA / pk.ERIA, erat=None, alkukassa=0.0, talletus=True, tasapaino_kk=12)


def kerro(r):
    m = pk.tulosmittarit(r)
    vuosia = m["paivia"] / 365.25
    vt = pk.verotapahtumat(r["kaupat"])
    m["krypto_myynnit_v"] = sum(a[0] for a in vt.values()) / vuosia
    m["krypto_ostot_v"] = sum(a[1] for a in vt.values()) / vuosia
    m["muut_myynnit_v"] = sum(a[2] for a in vt.values()) / vuosia
    m["kauppoja_v"] = m["kauppoja"] / vuosia
    return m


def yhteenveto(rivit):
    """rivit: [mittarit-dict]. Mediaani (min .. max) tuotolle ja pudotukselle seka keskiarvot muille."""
    f = lambda k: [x[k] for x in rivit]
    med = statistics.median
    return {
        "tuotto": (med(f("tuotto")), min(f("tuotto")), max(f("tuotto"))),
        "max_dd": (med(f("max_dd")), min(f("max_dd")), max(f("max_dd"))),
        "tappio_osuus": sum(x["tappiokuukausia"] for x in rivit) / sum(x["kuukausia"] for x in rivit),
        "kauppoja_v": med(f("kauppoja_v")), "kulut_pros": med(f("kulut_pros")),
        "krypto_myynnit_v": med(f("krypto_myynnit_v")), "krypto_ostot_v": med(f("krypto_ostot_v")),
        "muut_myynnit_v": med(f("muut_myynnit_v")), "tappiolla_aloituksia": sum(1 for x in rivit if x["tuotto"] < 0),
        "n": len(rivit),
    }


def pros(x):
    return "%+.1f %%" % (100 * x)


def taulukko(otsikko, tulokset):
    ulos = ["### " + otsikko, "",
            "| Tapa | Tuotto kulujen jalkeen, mediaani (min .. max) | Suurin pudotus, mediaani (pahin) | Tappiollisia aloituksia | Tappiolla olevia kuukausien loppuja | Kauppoja / vuosi | Kulut % sijoitetusta |",
            "|---|---|---|---|---|---|---|"]
    for nimi, y in tulokset:
        ulos.append("| %s | %s (%s .. %s) | %.1f %% (%.1f %%) | %d / %d | %.0f %% | %.0f | %.2f %% |" % (
            nimi, pros(y["tuotto"][0]), pros(y["tuotto"][1]), pros(y["tuotto"][2]), 100 * y["max_dd"][0], 100 * y["max_dd"][2],
            y["tappiolla_aloituksia"], y["n"], 100 * y["tappio_osuus"], y["kauppoja_v"], 100 * y["kulut_pros"]))
    return "\n".join(ulos)


def verotaulukko(tulokset):
    ulos = ["| Tapa | Kryptomyynnit / vuosi | Kryptoostot / vuosi (vaihto, jos maksetaan USDT:lla) | Muut myynnit (indeksi) / vuosi |", "|---|---|---|---|"]
    for nimi, y in tulokset:
        ulos.append("| %s | %.1f | %.1f | %.1f |" % (nimi, y["krypto_myynnit_v"], y["krypto_ostot_v"], y["muut_myynnit_v"]))
    return "\n".join(ulos)


def aloitustaulukko(nimet, per_tapa, alut):
    ulos = ["| Aloitus | " + " | ".join(nimet) + " |", "|---|" + "---|" * len(nimet)]
    for i, a in enumerate(alut):
        ulos.append("| %s | " % a.strftime("%Y-%m") + " | ".join("%s / pud. %.0f %%" % (pros(rs[i]["tuotto"]), 100 * rs[i]["max_dd"]) for rs in per_tapa) + " |")
    return "\n".join(ulos)


def main(historia, indeksi_csv):
    loppu = holdout_loppu()
    hinnat_btc = lue_btc(historia, loppu)
    hinnat = {"BTCUSDT": hinnat_btc, "SPX": indeksidata.lue(indeksi_csv)}
    alut = aloitukset()
    a_tul, b_tul, a_rivit, b_rivit = [], [], [], []
    for nimi, painot in TAVAT:
        ra = [kerro(aja_aineisto_a(hinnat, painot, a)) for a in alut]
        rb = [kerro(aja_aineisto_b(hinnat, painot, a, loppu)) for a in alut]
        a_rivit.append(ra), b_rivit.append(rb)
        a_tul.append((nimi, yhteenveto(ra)))
        b_tul.append((nimi, yhteenveto(rb)))
    print("Lahteet: BTC %s (Binance-arkisto, historia.db), indeksi %s (Tonin itse lataama CSV, hintaindeksi ilman osinkoja)." % (
        historia.split("/")[-1], indeksi_csv.split("/")[-1]))
    print("Jakso: aloitukset %s .. %s (joka %d. kk), data loppuu %s (holdout lukittu)." % (alut[0], alut[-1], VALI_KK, loppu))
    print("Kulut: %.1f %% + %.1f %% slippage per kauppa.\n" % (100 * rajat.KULU, 100 * rajat.SLIPPAGE))
    print(taulukko("Aineisto A: 12 kuukausieraa, 12 kuukauden jakso, 12 aloitusta", a_tul)); print()
    print(taulukko("Aineisto B: jatkuva kuukausisaasto aloituksesta dataan loppuun, vuosittainen tasapainotus, 12 aloitusta", b_tul)); print()
    print("### Verokirjanpidon tapahtumamaarat, aineisto A\n"); print(verotaulukko(a_tul)); print()
    print("### Verokirjanpidon tapahtumamaarat, aineisto B\n"); print(verotaulukko(b_tul)); print()
    nimet = [n for n, _ in TAVAT]
    print("### Aloituskohtaiset tulokset, aineisto A (tuotto / suurin pudotus)\n"); print(aloitustaulukko(nimet, a_rivit, alut)); print()
    print("### Aloituskohtaiset tulokset, aineisto B (tuotto / suurin pudotus)\n"); print(aloitustaulukko(nimet, b_rivit, alut))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("kaytto: passiivinen_vertailu.py <historia.db> <indeksi.csv>")
        sys.exit(2)
    try:
        sys.exit(main(sys.argv[1], sys.argv[2]))
    except (OSError, ValueError, KeyError, sqlite3.Error) as e:
        print("virhe:", type(e).__name__)
        sys.exit(1)
