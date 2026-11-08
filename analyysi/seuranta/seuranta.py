"""Elavan paperisalkun viikkoseuranta: salkut vs. passiiviset tavat samalla jaksolla.

Vain luku (sqlite immutable=1), vain stdlib, ei verkkoa, ei kirjoitusta kantaan.
Tulostaa vain prosentteja. Ei sijoitusneuvo eika johtopaatos alle 90 paivan datasta.

Kaytto (repon juuresta): python3 -m analyysi.seuranta.seuranta [kauppa-hakemisto] [--loppu YYYY-MM-DD] [--min-paivat N]
  oletushakemisto /var/lib/claude-jono/kauppa
  --loppu        rajaa jakson loppupaivan (vertailuun vanhaan tulokseen)
  --min-paivat   bootstrap-valin vahimmaispaivat (oletus 30)

Passiiviset (kulumalli kuten kauppatiimilla: 0,1 % kulu + 0,1 % slippage per osto):
  BTC osta ja pida: kaikki paaoma ostetaan jakson ensimmaisen paivan avauskurssilla.
  Kuukausiosto, paaoman jako erin: paaoma jaetaan tasan jakson kalenterikuukausille,
    era ostetaan kunkin kuukauden ensimmaisena jakson paivana, ostamaton osa on kateista.
  Kuukausiosto, jatkuva saasto: sama kuukausisumma ja samat ostot, mutta vain jo
    talletettu raha on mukana: tuotto = arvo / talletetut - 1, pudotus lasketaan
    pelkan sijoitetun arvon sarjasta (kateista ei ole vakuutena).
"""
import random
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

OLETUS_HAKEMISTO = "/var/lib/claude-jono/kauppa"
KULU = 0.002
SIEMEN, TOISTOJA, LOHKO = 1, 5000, 4
MIN_PAIVAT_VALI = 30
TAVOITE_PAIVAT = 90
VALI_B = 30  # tapa B: osto 30 paivan valein jakson alusta
SALKUT = ("salkku_flash", "salkku_pro")


def avaa(hakemisto, nimi):
    return sqlite3.connect("file:%s/%s?immutable=1" % (hakemisto, nimi), uri=True)


def ms(p):
    return int(datetime(p.year, p.month, p.day, tzinfo=timezone.utc).timestamp()) * 1000


def paivat(a, b):
    p = a
    while p <= b:
        yield p
        p += timedelta(days=1)


def pudotus(v):
    huippu, pahin = v[0], 0.0
    for x in v:
        huippu = max(huippu, x)
        pahin = max(pahin, 1 - x / huippu)
    return pahin


def lue_btc(hakemisto):
    c = avaa(hakemisto, "markkina.db")
    avaus, sulku = {}, {}
    for ts, o, cl in c.execute("select ts,open,close from hinnat where symbol='BTCUSDT'"):
        avaus[ts], sulku[ts] = o, cl
    return avaus, sulku


def lue_salkku_laaja(hakemisto, nimi):
    """Palauttaa (alkupaoma, [(paiva, arvo)], {paiva: (kateinen, positiot_arvo)}).
    Jos arvot-taulussa ei ole sarakkeita kateinen ja positiot_arvo (vanha kanta),
    kolmas on tyhja sanakirja: kateisosuutta ei voi laskea ("?")."""
    c = avaa(hakemisto, nimi + ".db")
    tila = dict(c.execute("select avain,arvo from tila").fetchall())
    alku = float(tila["alkupaoma"])
    sarakkeet = {r[1] for r in c.execute("pragma table_info(arvot)")}
    on_osuus = {"kateinen", "positiot_arvo"} <= sarakkeet
    kysely = "select paiva,arvo%s from arvot order by paiva" % (
        ",kateinen,positiot_arvo" if on_osuus else "")
    rivit, osuudet = [], {}
    for r in c.execute(kysely):
        p = date.fromisoformat(r[0])
        rivit.append((p, float(r[1])))
        if on_osuus and r[2] is not None and r[3] is not None:
            osuudet[p] = (float(r[2]), float(r[3]))
    return alku, rivit, osuudet


def lue_salkku(hakemisto, nimi):
    alku, rivit, _ = lue_salkku_laaja(hakemisto, nimi)
    return alku, rivit


def sijoitusaste(jakso, arvot, osuudet):
    """Palauttaa (viimeisen paivan kateisosuus, jakson keskimaarainen sijoitusaste w).
    Kumpikin on None, jos jonkin jakson paivan osuus puuttuu tai arvo on 0 (tulostuu "?").
    Sijoitusaste = positiot_arvo / arvo; kateisosuus = kateinen / arvo."""
    ast = []
    for p in jakso:
        if p not in osuudet or not arvot.get(p):
            return None, None
        ast.append(osuudet[p][1] / arvot[p])
    kat, _ = osuudet[jakso[-1]]
    return kat / arvot[jakso[-1]], sum(ast) / len(ast)


def pros(x):
    return "?" if x is None else "%.1f %%" % (x * 100)


def salkun_sarja(nimi, alku, arvot, jakso):
    """Salkun arvosarja [lahto, ...] jaksolta; indeksi 0 = lahtotilanne.
    Jos salkku on alkanut ennen jaksoa, lahto on jaksoa edeltavan paivan arvo
    (muuten ensimmaisen paivan tuotto sisaltaisi jaksoa edeltaneen kehityksen).
    Jos salkku alkaa jakson ensimmaisena paivana, lahto on alkupaoma."""
    ensimmainen = min(arvot)
    if ensimmainen < jakso[0]:
        edellinen = jakso[0] - timedelta(days=1)
        if edellinen not in arvot:
            raise ValueError("%s: jaksoa edeltavan paivan arvorivi puuttuu" % nimi)
        lahto = arvot[edellinen]
    else:
        lahto = alku
    return [lahto] + [arvot[p] for p in jakso]


def passiivi_bh(jakso, avaus, sulku):
    """Arvosarja [1, ...] jakson alusta; indeksi 0 = lahtotilanne."""
    q = 1.0 / (avaus[ms(jakso[0])] * (1 + KULU))
    return [1.0] + [q * sulku[ms(p)] for p in jakso]


def ostopaivat(jakso):
    """Kunkin kalenterikuukauden ensimmainen jakson paiva."""
    nahty = {}
    for p in jakso:
        nahty.setdefault((p.year, p.month), p)
    return set(nahty.values())


def ostopaivat_vali(jakso, vali=VALI_B):
    """Tapa B: jakson ensimmainen paiva ja siita joka vali:s paiva (indeksit 0, vali, 2*vali, ...)."""
    return {p for i, p in enumerate(jakso) if i % vali == 0}


def passiivi_w(jakso, avaus, sulku, w):
    """BTC samalla keskimaaraisella sijoitusasteella w: arvo = (1 - w) + w * BTC_t / BTC_0,
    yksi ostokulu (KULU) jakson ensimmaisen paivan avauskurssilla. w=1 on sama kuin osta ja pida.
    w tiedetaan vasta jalkikateen, joten tama ei ole toteutettavissa oleva tapa."""
    q = w / (avaus[ms(jakso[0])] * (1 + KULU))
    return [1.0] + [(1 - w) + q * sulku[ms(p)] for p in jakso]


def passiivi_vali(jakso, avaus, sulku, vali=VALI_B):
    """Kuukausiosto B, 30 paivan valein: paaoma jaetaan tasan jakson ostopaiville,
    sama kulu kuin tavalla A (KULU per osto), ostamaton osa on kateista."""
    ost = ostopaivat_vali(jakso, vali)
    era = 1.0 / len(ost)
    kassa, q, v = 1.0, 0.0, [1.0]
    for p in jakso:
        if p in ost:
            q += era / (avaus[ms(p)] * (1 + KULU))
            kassa -= era
        v.append(kassa + q * sulku[ms(p)])
    return v


def passiivi_era(jakso, avaus, sulku):
    """Kuukausiosto, paaoman jako erin. Arvo sisaltaa kateisen."""
    ost = ostopaivat(jakso)
    era = 1.0 / len(ost)
    kassa, q, v = 1.0, 0.0, [1.0]
    for p in jakso:
        if p in ost:
            q += era / (avaus[ms(p)] * (1 + KULU))
            kassa -= era
        v.append(kassa + q * sulku[ms(p)])
    return v


def passiivi_saasto(jakso, avaus, sulku):
    """Kuukausiosto, jatkuva saasto. Palauttaa (sijoitetun arvon sarja ilman
    kateista, talletetut yhteensa, paivittaiset aikapainotetut tuotot)."""
    ost = ostopaivat(jakso)
    era = 1.0 / len(ost)
    q, talletettu = 0.0, 0.0
    v, tuotot = [], []
    edellinen = None
    for p in jakso:
        talletus = era if p in ost else 0.0
        if talletus:
            q += talletus / (avaus[ms(p)] * (1 + KULU))
            talletettu += talletus
        arvo = q * sulku[ms(p)]
        lahto = talletus + (edellinen or 0.0)  # paivan alun sijoitettu arvo
        tuotot.append(arvo / lahto - 1 if lahto else 0.0)
        v.append(arvo)
        edellinen = arvo
    return v, talletettu, tuotot


def paivatuotot(v):
    return [v[i + 1] / v[i] - 1 for i in range(len(v) - 1)]


def bootstrap(x, rng, lohko=LOHKO, toistoja=TOISTOJA):
    """Pyoreiden lohkojen bootstrap-vali (5 % ja 95 %) keskiarvolle."""
    n = len(x)
    tulos = []
    for _ in range(toistoja):
        s = []
        while len(s) < n:
            i = rng.randrange(n)
            s += [x[(i + j) % n] for j in range(lohko)]
        tulos.append(sum(s[:n]) / n)
    tulos.sort()
    return tulos[int(0.05 * toistoja)], tulos[int(0.95 * toistoja) - 1]


def laske(hakemisto, loppu=None, min_paivat=MIN_PAIVAT_VALI):
    """Palauttaa sanakirjan: jakso, salkut ja passiiviset. Ei tulosta mitaan."""
    avaus, sulku = lue_btc(hakemisto)
    data = {n: lue_salkku_laaja(hakemisto, n) for n in SALKUT}
    alut = [rivit[0][0] for _, rivit, _o in data.values() if rivit]
    loput = [rivit[-1][0] for _, rivit, _o in data.values() if rivit]
    if not alut:
        raise ValueError("salkuissa ei ole arvorivej\u00e4")
    a, b = max(alut), min(loput)
    if loppu is not None:
        b = min(b, loppu)
    jakso = list(paivat(a, b))
    puuttuu = [p for p in jakso if ms(p) not in sulku or ms(p) not in avaus]
    if puuttuu:
        raise ValueError("BTC-hinta puuttuu %d paivalta, ensimmainen %s" % (len(puuttuu), puuttuu[0]))
    bh = passiivi_bh(jakso, avaus, sulku)
    era = passiivi_era(jakso, avaus, sulku)
    sv, talletettu, str_ = passiivi_saasto(jakso, avaus, sulku)
    vv = passiivi_vali(jakso, avaus, sulku)
    passiiviset = [
        ("BTC osta ja pida", bh[-1] - 1, pudotus(bh), paivatuotot(bh)),
        ("Kuukausiosto, p\u00e4\u00e4oman jako erin", era[-1] - 1, pudotus(era), paivatuotot(era)),
        ("Kuukausiosto, jatkuva s\u00e4\u00e4st\u00f6", sv[-1] / talletettu - 1, pudotus(sv), str_),
        ("Kuukausiosto B, %d pv v\u00e4lein" % VALI_B, vv[-1] - 1, pudotus(vv), paivatuotot(vv)),
    ]
    rng = random.Random(SIEMEN)
    salkut = []
    for nimi in SALKUT:
        alku, rivit, osuudet = data[nimi]
        arvot = {p: x for p, x in rivit}
        if any(p not in arvot for p in jakso):
            raise ValueError("%s: arvorivi puuttuu jaksolta" % nimi)
        v = salkun_sarja(nimi, alku, arvot, jakso)
        r = paivatuotot(v)
        kateinen, w = sijoitusaste(jakso, arvot, osuudet)
        rivi = {"nimi": nimi, "tuotto": v[-1] / v[0] - 1, "pudotus": pudotus(v), "vertailut": [],
                "kateinen": kateinen, "w": w}
        vertailtavat = list(passiiviset)
        if w is not None:
            ww = passiivi_w(jakso, avaus, sulku, w)
            vertailtavat.append(("BTC, sijoitusaste %.1f %%" % (w * 100), ww[-1] - 1, pudotus(ww),
                                 paivatuotot(ww)))
        for pn, pt, pd, pr in vertailtavat:
            vali = None
            if len(jakso) >= min_paivat:
                vali = bootstrap([x - y for x, y in zip(r, pr)], rng)
            rivi["vertailut"].append({"nimi": pn, "tuotto": pt, "pudotus": pd,
                                      "ero": rivi["tuotto"] - pt, "vali": vali})
        salkut.append(rivi)
    return {"alku": a, "loppu": b, "paivia": len(jakso), "salkut": salkut,
            "min_paivat": min_paivat}


def tuoreus(hakemisto, nyt=None):
    try:
        with open(hakemisto + "/PAIVITETTY", encoding="utf-8") as f:
            teksti = f.read().strip()
    except OSError as e:
        return "tuntematon (PAIVITETTY: %s)" % type(e).__name__, None
    try:
        t = datetime.fromisoformat(teksti)
    except ValueError:
        return "tuntematon (PAIVITETTY ei j\u00e4senny)", None
    nyt = nyt or datetime.now(timezone.utc)
    ika = (nyt - t).total_seconds() / 3600
    huom = " VANHA, yli 36 h" if ika > 36 else ""
    return "%s (%.1f h sitten)%s" % (teksti, ika, huom), ika


def pp(x):
    return "%+.2f %%" % (x * 100)


def laskentatapa():
    """Vertailujen laskentatapa raporttiin. Kulu luetaan vakiosta KULU."""
    kulu = "%.2f %%" % (KULU * 100)
    return [
        "Vertailujen laskentatapa: ostohinta = ostop\u00e4iv\u00e4n avaushinta + kulu %s per osto (KULU); "
        "arvostus = p\u00e4iv\u00e4n p\u00e4\u00e4t\u00f6shinta, loppuarvo jakson viimeisen p\u00e4iv\u00e4n p\u00e4\u00e4t\u00f6shinta; "
        "myyntikulua ei v\u00e4hennet\u00e4." % kulu,
        "  Ostohetki vertailuittain: osta ja pida = jakson 1. p\u00e4iv\u00e4, koko p\u00e4\u00e4oma; "
        "p\u00e4\u00e4oman jako erin ja jatkuva s\u00e4\u00e4st\u00f6 = kunkin kalenterikuukauden 1. jakson p\u00e4iv\u00e4; "
        "B = jakson 1. p\u00e4iv\u00e4 ja sen j\u00e4lkeen %d pv v\u00e4lein." % VALI_B,
        "  Tuoton jakaja: s\u00e4\u00e4st\u00f6ss\u00e4 talletetut eik\u00e4 koko p\u00e4\u00e4oma, muissa jakson alun p\u00e4\u00e4oma (kassa mukana). "
        "Salkkujen tuotto on salkun oma arvosarja kannasta.",
    ]


def muotoile(t, tuoreus_teksti):
    rivit = []
    rivit.append("Paperisalkkujen seuranta (vain prosentteja, ei sijoitusneuvo)")
    rivit.append("Datan tuoreus (PAIVITETTY): " + tuoreus_teksti)
    rivit.append("Jakso: %s .. %s, p\u00e4ivi\u00e4 %d (tavoite v\u00e4hint\u00e4\u00e4n %d, arvio t\u00e4yttymisest\u00e4 %s)" % (
        t["alku"], t["loppu"], t["paivia"], TAVOITE_PAIVAT,
        t["alku"] + timedelta(days=TAVOITE_PAIVAT - 1)))
    rivit.extend(laskentatapa())
    for s in t["salkut"]:
        rivit.append("")
        rivit.append("%s: tuotto kulujen j\u00e4lkeen %s, suurin pudotus %.2f %%" % (
            s["nimi"], pp(s["tuotto"]), s["pudotus"] * 100))
        rivit.append("  k\u00e4teisosuus viimeisen\u00e4 p\u00e4iv\u00e4n\u00e4: %s, jakson keskim\u00e4\u00e4r\u00e4inen sijoitusaste w: %s" % (
            pros(s.get("kateinen")), pros(s.get("w"))))
        rivit.append("  %-34s %10s %10s %10s  %s" % ("vertailu", "tuotto", "pudotus", "ero", "ero, 90 %:n v\u00e4li (pp/pv)"))
        for v in s["vertailut"]:
            if v["vali"] is None:
                vali = "data ei riit\u00e4 (alle %d p\u00e4iv\u00e4\u00e4)" % t["min_paivat"]
            else:
                vali = "[%+.3f; %+.3f]" % (v["vali"][0] * 100, v["vali"][1] * 100)
            rivit.append("  %-34s %10s %9.2f%% %10s  %s" % (
                v["nimi"], pp(v["tuotto"]), v["pudotus"] * 100,
                "%+.2f pp" % (v["ero"] * 100), vali))
    if any(s.get("w") is not None for s in t["salkut"]):
        rivit.append("")
        rivit.append("Huom: sijoitusaste w tiedet\u00e4\u00e4n vasta j\u00e4lkik\u00e4teen, joten \"BTC, sijoitusaste\"-rivi kuvaa "
                     "riskitasoa eik\u00e4 ole toteutettavissa oleva tapa.")
    rivit.append("")
    if t["paivia"] < MIN_PAIVAT_VALI:
        rivit.append("Vertailusta ei voi p\u00e4\u00e4tell\u00e4 mit\u00e4\u00e4n: p\u00e4ivi\u00e4 %d < %d." % (t["paivia"], MIN_PAIVAT_VALI))
    if t["paivia"] < TAVOITE_PAIVAT:
        rivit.append("Johtop\u00e4\u00e4t\u00f6ksi\u00e4 ei tehd\u00e4: p\u00e4ivi\u00e4 %d/%d." % (t["paivia"], TAVOITE_PAIVAT))
    return "\n".join(rivit)


def jasenna(argv):
    hakemisto, loppu, min_paivat = OLETUS_HAKEMISTO, None, MIN_PAIVAT_VALI
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--loppu":
            loppu = date.fromisoformat(argv[i + 1])
            i += 2
        elif a == "--min-paivat":
            min_paivat = int(argv[i + 1])
            i += 2
        else:
            hakemisto = a.rstrip("/")
            i += 1
    return hakemisto, loppu, min_paivat


def main(argv):
    try:
        hakemisto, loppu, min_paivat = jasenna(argv)
        t = laske(hakemisto, loppu, min_paivat)
    except (sqlite3.Error, ValueError, KeyError, OSError, IndexError) as e:
        print("virhe: " + type(e).__name__)
        return 1
    teksti, _ = tuoreus(hakemisto)
    print(muotoile(t, teksti))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
