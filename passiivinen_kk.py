"""Passiivinen paperisalkku 'passiivinen_kk': tasainen BTC-kuukausiosto, ei myynteja (vain paperi, ei oikeaa rahaa).

Kaksi osaa:
- simuloi(): puhdas, monta kohdetta tukeva simulaattori (kaytetaan historiavertailussa, passiivinen_vertailu.py).
- aja_paiva() / main(): elava paivaajo samalla kantaskeemalla kuin muut paperisalkut (salkku.avaa), omaan kantaan.

Kulumalli on sama kuin salkku.py:ssa: toteutus paivan avaushintaan, slippage + kulu (rajat.SLIPPAGE, rajat.KULU).
Tama moduuli vain TUO rajat.py:n, salkku.py:n ja notify.py:n; se ei muuta niita eika ohita riskivahtia:
passiivinen salkku ei myy, joten riskivahdin positio- ja kateisrajat (jotka estaisivat 100 % BTC:n) eivat ole kaytossa.
Sen sijaan salkulle on oma halytysraja (HALYTYSRAJA), joka vain ilmoittaa Telegramiin eika muuta salkkua."""
import argparse
import json
import os
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

import llm
import notify
import rajat
import salkku
from salkku import Datavirhe

ERIA = 12                       # kuukausieria: alkupaaoma / ERIA ostetaan kerran kuukaudessa
HALYTYSRAJA = 0.30              # halytys, kun salkun arvo on 30 % alle huipun (vain ilmoitus)
HALYTYS_PALAUTUS = 0.20         # halytys nollautuu, kun pudotus on alle taman
SYMBOLI = "BTCUSDT"
OLETUS_DB = "/srv/kauppa/data/salkku_passiivinen_kk.db"
PV = 86400000


def lisaa_kuukausia(p, n):
    """Paiva + n kuukautta; kuukauden lopun yli meneva paiva pyoristyy kuukauden viimeiseen."""
    k = p.year * 12 + (p.month - 1) + n
    v, kk = divmod(k, 12)
    kk += 1
    for paiva in (p.day, 30, 29, 28):
        try:
            return date(v, kk, paiva)
        except ValueError:
            continue
    raise ValueError("paivamaara")


def osto(summa, avaus):
    """Osto summalla (USDT, sis. kulun) avaushintaan. Palauttaa (maara, hinta, kulu, slippage_kulu); sama kaava kuin salkku.py."""
    hinta = avaus * (1 + rajat.SLIPPAGE)
    qty = summa / (hinta * (1 + rajat.KULU))
    return qty, hinta, qty * hinta * rajat.KULU, qty * avaus * rajat.SLIPPAGE


def myynti(qty, avaus):
    """Myynti avaushintaan. Palauttaa (hinta, kulu, slippage_kulu, nettotulo)."""
    hinta = avaus * (1 - rajat.SLIPPAGE)
    kulu = qty * hinta * rajat.KULU
    return hinta, kulu, qty * avaus * rajat.SLIPPAGE, qty * hinta - kulu


# ---------------------------------------------------------------- historiasimulaattori

def simuloi(hinnat, painot, alku, loppu, era, erat=None, alkukassa=0.0, talletus=False, tasapaino_kk=None):
    """Kuukausiosto paivittaisella arvostuksella.
    hinnat: {symbol: {"open": {date: x}, "close": {date: x}}}; painot: {symbol: osuus}, summa 1.
    era: kuukausiera (USDT). erat: erien enimmaismaara (None = jokaisena kuukautena loppuun asti).
    alkukassa: alussa kateisena oleva summa. talletus: True = era tulee ulkopuolelta (saasto), False = era otetaan kassasta.
    tasapaino_kk: jos annettu, ostopaivana tasapainotetaan tavoitepainoihin kun edellisesta on kulunut nain monta kuukautta.
    Osto tehdaan kuukauden ensimmaisena paivana, jona kaikilla kohteilla on avaushinta (porssi auki).
    Palauttaa dictin: arvot [(paiva, arvo)], indeksi (aikapainotettu), kaupat, talletettu [(paiva, kertyma)], kulut."""
    if abs(sum(painot.values()) - 1.0) > 1e-9:
        raise ValueError("painojen summa")
    kassa = float(alkukassa)
    maarat = {s: 0.0 for s in painot}
    kaupat, arvot, indeksi, kertyma = [], [], [], []
    viim_kk, tehty, seur_tasap = None, 0, None
    sulku, talletettu_yht, kulut = {}, 0.0, 0.0
    edell, idx = None, 1.0
    d = alku
    while d <= loppu:
        for s in painot:
            c = hinnat[s]["close"].get(d)
            if c is not None:
                sulku[s] = c
        dep = 0.0
        if (d.year, d.month) != viim_kk and (erat is None or tehty < erat) \
                and all(d in hinnat[s]["open"] for s in painot):
            av = {s: hinnat[s]["open"][d] for s in painot}
            if talletus:
                kassa += era
                dep = era
                talletettu_yht += era
            summa = min(era, kassa)
            nykyinen = {s: maarat[s] * av[s] for s in painot}
            if tasapaino_kk and seur_tasap is None:
                seur_tasap = lisaa_kuukausia(d, tasapaino_kk)
            if tasapaino_kk and d >= seur_tasap:
                kokonais = sum(nykyinen.values()) + summa
                tarve = {s: painot[s] * kokonais - nykyinen[s] for s in painot}
                seur_tasap = lisaa_kuukausia(seur_tasap, tasapaino_kk)
            else:
                tarve = {s: painot[s] * summa for s in painot}
            kaytettavissa = summa
            for s in sorted(painot):
                if tarve[s] < 0:
                    q = min(maarat[s], -tarve[s] / av[s])
                    hinta, kulu, slip, netto = myynti(q, av[s])
                    maarat[s] -= q
                    kassa += netto
                    kaytettavissa += netto
                    kulut += kulu + slip
                    kaupat.append((d.isoformat(), s, "SELL", q, hinta, kulu, slip))
            haluttu = sum(t for t in tarve.values() if t > 0)
            k = min(1.0, kaytettavissa / haluttu) if haluttu > 0 else 0.0
            for s in sorted(painot):
                if tarve[s] > 0 and k > 0:
                    q, hinta, kulu, slip = osto(tarve[s] * k, av[s])
                    maarat[s] += q
                    kassa -= tarve[s] * k
                    kulut += kulu + slip
                    kaupat.append((d.isoformat(), s, "BUY", q, hinta, kulu, slip))
            tehty += 1
            viim_kk = (d.year, d.month)
        if any(maarat[s] > 0 and s not in sulku for s in painot):
            raise Datavirhe("ei sulkuhintaa %s" % d)
        arvo = kassa + sum(maarat[s] * sulku[s] for s in painot if maarat[s] > 0)
        pohja = (edell if edell is not None else alkukassa) + dep
        if pohja > 0:
            idx *= arvo / pohja
        arvot.append((d, arvo))
        indeksi.append(idx)
        kertyma.append((d, alkukassa + talletettu_yht))
        edell = arvo
        d += timedelta(days=1)
    return {"arvot": arvot, "indeksi": indeksi, "kaupat": kaupat, "kertyma": kertyma, "kulut": kulut,
            "sijoitettu": alkukassa + talletettu_yht}


def tulosmittarit(r):
    """Tuotto kulujen jalkeen (loppuarvo / sijoitettu paaoma), suurin pudotus (aikapainotetusta indeksista),
    tappiolla olevat kuukauden viimeiset paivat (arvo < sijoitettu paaoma siihen mennessa), kauppojen maara, kulut."""
    arvot, kertyma = r["arvot"], r["kertyma"]
    huippu, mdd = 0.0, 0.0
    for x in r["indeksi"]:
        huippu = max(huippu, x)
        mdd = max(mdd, 1 - x / huippu)
    kk_loput = [i for i in range(len(arvot)) if i == len(arvot) - 1 or arvot[i + 1][0].month != arvot[i][0].month]
    tappiolla = sum(1 for i in kk_loput if kertyma[i][1] > 0 and arvot[i][1] < kertyma[i][1] - 1e-9)
    return {"tuotto": arvot[-1][1] / r["sijoitettu"] - 1, "max_dd": mdd, "tappiokuukausia": tappiolla,
            "kuukausia": len(kk_loput), "kauppoja": len(r["kaupat"]), "kulut_pros": r["kulut"] / r["sijoitettu"],
            "paivia": len(arvot)}


def verotapahtumat(kaupat, krypto=(SYMBOLI,)):
    """Tapahtumamaarat (ei veroneuvoa): kryptomyynnit, kryptoostot (vaihto, jos maksetaan toisella kryptolla kuten USDT),
    ei-kryptomyynnit (esim. osakeindeksirahasto). Palauttaa {vuosi: (krypto_myynnit, krypto_ostot, muut_myynnit)}."""
    ulos = {}
    for paiva, s, puoli, *_ in kaupat:
        a = ulos.setdefault(paiva[:4], [0, 0, 0])
        if s in krypto:
            a[0 if puoli == "SELL" else 1] += 1
        elif puoli == "SELL":
            a[2] += 1
    return {v: tuple(a) for v, a in ulos.items()}


# ---------------------------------------------------------------- elava paivaajo

def _ms(p):
    return int(datetime(p.year, p.month, p.day, tzinfo=timezone.utc).timestamp() * 1000)


def pudotus(arvot):
    """Pudotus huipusta (0..1); arvot kronologisesti, alkupaaoma ensimmaisena."""
    if not arvot:
        return 0.0
    huippu = max(arvot)
    return 1.0 - arvot[-1] / huippu if huippu > 0 else 0.0


def halytys_tarvitaan(dd, jo_halytetty):
    """(laheta, uusi_tila). Halytys kerran per jakso: nollautuu vasta kun pudotus on alle HALYTYS_PALAUTUS."""
    if dd >= HALYTYSRAJA and not jo_halytetty:
        return True, True
    if dd < HALYTYS_PALAUTUS:
        return False, False
    return False, jo_halytetty


def aja_paiva(con, markkina, paiva):
    """Yksi paiva. Ostaa alkupaaoma/ERIA BTC:ta, jos kuukausi vaihtui ja eria on jaljella. Ei koskaan myy.
    Palauttaa dictin (paiva, arvo, kaupat, dd, halytys). Nostaa Datavirhe, jos hinta puuttuu tai paiva on jo ajettu."""
    pstr = paiva.isoformat()
    edellinen = salkku._hae(con, "viimeisin_paiva")
    if edellinen is not None and pstr <= edellinen:
        raise Datavirhe("paiva %s on jo ajettu (viimeisin %s)" % (pstr, edellinen))
    h = markkina.execute("SELECT open, close FROM hinnat WHERE symbol=? AND ts=?", (SYMBOLI, _ms(paiva))).fetchone()
    if h is None:
        raise Datavirhe("ei BTC-hintaa paivalle %s" % pstr)
    avaus, sulku = h
    qty0 = dict(con.execute("SELECT symbol, maara FROM positiot").fetchall()).get(SYMBOLI, 0.0)
    kassa = salkku.kateinen(con)
    eria = int(salkku._hae(con, "eria_ostettu") or 0)
    viim_kk = salkku._hae(con, "viim_ostokuukausi")
    kaupat, huom = [], []
    if viim_kk != "%d-%02d" % (paiva.year, paiva.month) and eria < ERIA:
        summa = min(float(salkku._hae(con, "alkupaoma")) / ERIA, kassa)
        qty, hinta, kulu, slip = osto(summa, avaus)
        qty0 += qty
        kassa -= summa
        kaupat.append((pstr, SYMBOLI, "BUY", qty, hinta, kulu, slip))
        eria += 1
        huom.append("kuukausiosto %d/%d" % (eria, ERIA))
        viim_kk = "%d-%02d" % (paiva.year, paiva.month)
    arvo = kassa + qty0 * sulku
    with con:
        if aloituspaiva_puuttuu(con):
            con.execute("INSERT OR REPLACE INTO tila VALUES ('aloituspaiva', ?)", (pstr,))
        con.execute("DELETE FROM positiot")
        if qty0 > 1e-12:
            con.execute("INSERT INTO positiot VALUES (?,?)", (SYMBOLI, qty0))
        con.executemany("INSERT INTO kaupat (paiva,symbol,puoli,maara,hinta,kulu,slippage_kulu) VALUES (?,?,?,?,?,?,?)", kaupat)
        con.execute("INSERT OR REPLACE INTO tila VALUES ('kateinen', ?)", (str(kassa),))
        con.execute("INSERT OR REPLACE INTO tila VALUES ('eria_ostettu', ?)", (str(eria),))
        con.execute("INSERT OR REPLACE INTO tila VALUES ('viim_ostokuukausi', ?)", (viim_kk or "",))
        con.execute("INSERT INTO arvot VALUES (?,?,?,?)", (pstr, arvo, kassa, arvo - kassa))
        con.execute("INSERT OR REPLACE INTO paatokset VALUES (?,?,?,?)", (pstr, json.dumps({"strategia": "passiivinen_kk"}), json.dumps(huom), None))
        con.execute("INSERT OR REPLACE INTO tila VALUES ('viimeisin_paiva', ?)", (pstr,))
    historia = [float(salkku._hae(con, "alkupaoma"))] + [float(r[0]) for r in con.execute("SELECT arvo FROM arvot ORDER BY paiva")]
    dd = pudotus(historia)
    laheta, uusi = halytys_tarvitaan(dd, salkku._hae(con, "halytetty") == "1")
    if not laheta:
        # Nollaus on turvallinen heti. Tilaa "halytetty=1" EI kirjata tassa: kuittaa_halytys() kirjaa sen vasta,
        # kun lahetys onnistui, jotta epaonnistunut halytys yritetaan uudelleen.
        salkku._aseta(con, "halytetty", "1" if uusi else "0")
    return {"paiva": pstr, "arvo": arvo, "kaupat": kaupat, "huomautukset": huom, "dd": dd, "halytys": laheta, "eria": eria}


def kuittaa_halytys(con):
    """Kirjaa halytyksen lahetetyksi. Kutsutaan vasta, kun notify.laheta palautti True."""
    salkku._aseta(con, "halytetty", "1")


def odottava_halytys(con):
    """Halytys, jota ei ole viela kuitattu (aiempi lahetys epaonnistui), tai None. Lukee vain tallennetun historian."""
    historia = [float(salkku._hae(con, "alkupaoma"))] + [float(r[0]) for r in con.execute("SELECT arvo FROM arvot ORDER BY paiva")]
    dd = pudotus(historia)
    if len(historia) < 2 or not halytys_tarvitaan(dd, salkku._hae(con, "halytetty") == "1")[0]:
        return None
    return {"paiva": salkku._hae(con, "viimeisin_paiva"), "arvo": historia[-1], "dd": dd}


def laheta_halytys(con, t, env_tg):
    """Lahettaa halytyksen ja kuittaa sen vain onnistuneen lahetyksen jalkeen. Palauttaa True/False."""
    if notify.laheta(halytysteksti(t), env_tg):
        kuittaa_halytys(con)
        return True
    print("passiivinen_kk %s: halytyksen lahetys epaonnistui, yritetaan uudelleen seuraavalla ajolla" % t["paiva"])
    return False


def aloituspaiva_puuttuu(con):
    return salkku._hae(con, "aloituspaiva") is None


def raportti(t, con, markkina):
    alku = float(salkku._hae(con, "alkupaoma"))
    rivit = ["Kauppatiimi (passiivinen_kk) %s" % t["paiva"]]
    rivit.append("Kaupat: " + ", ".join("BUY BTC %.1f" % (k[3] * k[4]) for k in t["kaupat"]) if t["kaupat"] else "Ei kauppoja.")
    rivi = "Salkku %.0f USDT (%+.2f %% alusta) | erat %d/%d | pudotus huipusta %.1f %%" % (
        t["arvo"], 100 * (t["arvo"] / alku - 1), t["eria"], ERIA, 100 * t["dd"])
    try:
        import mittarit
        a0 = date.fromisoformat(salkku._hae(con, "aloituspaiva"))
        b = mittarit.vertailusalkku(markkina, {SYMBOLI: 1.0}, a0, date.fromisoformat(t["paiva"]))
        rivi += " | BTC kertaosto %+.2f %%" % (100 * (b[-1][1] / rajat.ALKUPAOMA - 1))
    except (Datavirhe, ValueError, TypeError, sqlite3.Error) as e:
        rivi += " | vertailu ei saatavilla (%s)" % type(e).__name__
    rivit.append(rivi)
    return "\n".join(rivit)


def halytysteksti(t):
    return "HALYTYS passiivinen_kk (paperi): salkun arvo %.0f USDT on %.1f %% alle huipun (raja %.0f %%). Vain ilmoitus, salkkuun ei tehda muutoksia." % (
        t["arvo"], 100 * t["dd"], 100 * HALYTYSRAJA)


def viimeisin_paiva(markkina):
    ts = markkina.execute("SELECT MAX(ts) FROM hinnat WHERE symbol=?", (SYMBOLI,)).fetchone()[0]
    return datetime.fromtimestamp(ts / 1000, timezone.utc).date() if ts else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--salkku-db", default=OLETUS_DB)
    ap.add_argument("--markkina-db", default=None, help="oletus config.DB_POLKU")
    ap.add_argument("--tg-env", default="/srv/kauppa/.env")
    ap.add_argument("--ei-tg", action="store_true")
    a = ap.parse_args()
    if os.path.exists(rajat.PYSAYTYS):
        print("passiivinen_kk: PYSAYTETTY (kill switch), ei ajoa")
        return 0
    import config
    import db
    markkina = db.avaa(a.markkina_db or config.DB_POLKU)
    con = salkku.avaa(a.salkku_db)
    try:
        env_tg = llm.lue_env(a.tg_env)
    except llm.LLMVirhe:
        env_tg = {}
    viim = viimeisin_paiva(markkina)
    if viim is None:
        print("ei markkinadataa")
        return 2
    edellinen = salkku._hae(con, "viimeisin_paiva")
    paivat = [viim] if edellinen is None else []
    if edellinen is not None:
        p = date.fromisoformat(edellinen) + timedelta(days=1)
        while p <= viim:
            paivat.append(p)
            p += timedelta(days=1)
    if not paivat:
        print("ei uutta paivaa")
        t = odottava_halytys(con)
        if t is not None and not a.ei_tg:
            laheta_halytys(con, t, env_tg)
        return 0
    for p in paivat:
        try:
            t = aja_paiva(con, markkina, p)
        except Datavirhe as e:
            print("passiivinen_kk %s: DATAVIRHE, keskeytetty: %s" % (p, e))
            return 2
        print(raportti(t, con, markkina))
        if t["halytys"] and not a.ei_tg:
            laheta_halytys(con, t, env_tg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
