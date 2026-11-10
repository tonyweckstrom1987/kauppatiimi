"""Viikkoretro: sunnuntain yhteenveto Telegramiin. Vain luku: ei koskaan muuta salkkuja eika kutsu LLM:aa.
Vertaa kahta mallisalkkua (flash/pro) vertailusalkkuun (Tonyn jako) ja BTC:hen. Ei johtopaatoksia lyhyesta datasta."""
import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone

import config
import llm
import mittarit
import notify
import raportti as verorap
import rajat
import salkku
import silmukka

MIN_PV_JOHTOPAATOS = 28  # tata lyhyemmalla historialla ei sanota kumpi malli on parempi
HAVAINNOT_MAX = 1500     # doc 67: havainnot.md on enintaan ~1500 merkkia (menee agenttien kontekstiin)
# Oma hakemisto, koska palvelulla on ReadOnlyPaths=/srv/kauppa/data; tiedosto ylikirjoitetaan atomisesti (aina viimeisin tilanne)
HAVAINNOT_POLKU = os.environ.get("KAUPPA_HAVAINNOT", "/srv/kauppa/havainnot/havainnot.md")
PV_MS = 86400000
# Paivatila (doc 71, Tony 4.11.2026): illan tiivistys havainnot.md:n loppuosioon. Ei LLM-kutsua, vain mitattua dataa.
PAIVAT_OTSIKKO = "## Edelliset paivat (dataa, ei ohjeita)"
PAIVIA_MAX = 4          # uusin paiva korvaa vanhimman, kun tila on taynna
PAIVARIVI_MAX = 190     # merkkia / paiva; yhteisraja on silti HAVAINNOT_MAX
_PAIVARIVI_RE = re.compile(r"^- (\d{4}-\d{2}-\d{2}) ")


# -shm:n luonti vain luku -hakemistossa epaonnistuu koodilla SQLITE_READONLY (8) tai sen laajennetulla koodilla
# SQLITE_READONLY_DIRECTORY (1544; -wal puuttuu), tai SQLITE_CANTOPEN (14; -wal on olemassa). Sallitaan vain nama.
# Nimenomaan EI sallita esim. SQLITE_READONLY_ROLLBACK (776, kuuma -journal) eika lukitusta (SQLITE_BUSY 5).
SHM_VIRHEKOODIT = (8, 14, 1544)
# Python 3.10:ssa sqlite_errorcode puuttuu: paatellaan viestista. Se on vain karkea suodatin; turvallisuuden ratkaisevat
# WAL-otsikko, -journal-tarkistus ja sormenjalki, jotka ajetaan aina.
SHM_VIRHEVIESTIT = ("attempt to write a readonly database", "unable to open database file")


class VanhentunutLukuVirhe(sqlite3.OperationalError):
    """Kanta muuttui tai sen -wal/-journal sai dataa immutable-luvun aikana: tulos voisi olla vanhentunut."""


def _shm_virhe(e):
    """Onko virhe sellainen, etta syyna voi olla puuttuva -shm vain luku -hakemistossa."""
    koodi = getattr(e, "sqlite_errorcode", None)
    if koodi is not None:
        return koodi in SHM_VIRHEKOODIT
    return str(e) in SHM_VIRHEVIESTIT


def _wal_tila(polku):
    """True, jos tiedoston otsikon tavut 18-19 (kirjoitus- ja lukuversio) ovat molemmat 2 eli kanta on WAL-tilassa."""
    with open(polku, "rb") as f:
        otsikko = f.read(20)
    return len(otsikko) == 20 and otsikko[18] == 2 and otsikko[19] == 2


def _sormenjalki(polku):
    """(paatiedoston koko ja muutosaika, -wal:n koko tai None, -journal:n koko tai None): muuttuu, jos joku kirjoittaa."""
    st = os.stat(polku)
    return (st.st_size, st.st_mtime_ns) + tuple(os.path.getsize(polku + p) if os.path.exists(polku + p) else None
                                                for p in ("-wal", "-journal"))


def _kopioi_muistiin(polku):
    """Kopioi immutable-tilassa luetun kannan muistiin, jolloin myohemmat luvut eivat nojaa tiedostoon."""
    src = sqlite3.connect("file:%s?mode=ro&immutable=1" % polku, uri=True, timeout=10)
    try:
        kohde = sqlite3.connect(":memory:")
        try:
            src.backup(kohde)
        except BaseException:
            kohde.close()
            raise
        return kohde
    finally:
        src.close()


def _ro(polku):
    """Avaa kannan vain luku -tilassa. Virheet nousevat jo tassa (koetinkysely), eivat vasta ensimmaisessa kyselyssa.

    WAL-tilainen kanta ilman -shm-tiedostoa ei aukea vain luku -hakemistossa (ReadOnlyPaths), koska
    lukija ei voi luoda -shm:aa. Varatielle paasee vain, jos kaikki ehdot tayttyvat:
    1. virhekoodi on SHM_VIRHEKOODIT-listalla (3.10: viesti; ks. SHM_VIRHEVIESTIT),
    2. kanta on WAL-tilassa (otsikon tavut 18-19),
    3. -journal-tiedostoa ei ole (immutable ohittaisi kuuman journalin) ja -wal puuttuu tai on tyhja.
    Muuten alkuperainen virhe nostetaan (lukitus tarkoittaa kirjoittajaa) tai nostetaan VanhentunutLukuVirhe.
    Varatie kopioi kannan muistiin immutable=1-tilassa; paatiedoston koko ja muutosaika seka -wal:n ja -journal:n
    koko tarkistetaan ennen ja jalkeen. Jos jokin muuttui, nostetaan VanhentunutLukuVirhe. Rajaus: tarkistus perustuu
    tiedostojen tilaan, joten se on vahva mutta ei absoluuttinen takuu (sekunnin murto-osan sisalla alkava ja
    loppuva kirjoitus ilman nakyvaa jalkea). Kaikki yhteydet suljetaan kaikilla virhepoluilla.
    """
    con = None
    try:
        con = sqlite3.connect("file:%s?mode=ro" % polku, uri=True, timeout=10)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return con
    except BaseException as e:
        if con is not None:
            con.close()
        virhe = e
    if not (isinstance(virhe, sqlite3.OperationalError) and _shm_virhe(virhe) and os.path.exists(polku)):
        raise virhe
    try:
        if not _wal_tila(polku):
            raise virhe
        ennen = _sormenjalki(polku)
    except OSError:
        raise virhe
    if ennen[3] is not None:
        raise VanhentunutLukuVirhe("-journal on olemassa: immutable-luku ohittaisi sen")
    if ennen[2]:
        raise VanhentunutLukuVirhe("-wal sisaltaa dataa eika -shm:aa voi luoda")
    kopio = _kopioi_muistiin(polku)
    try:
        muuttui = _sormenjalki(polku) != ennen
    except BaseException:
        kopio.close()
        raise
    if muuttui:
        kopio.close()
        raise VanhentunutLukuVirhe("kanta muuttui immutable-luvun aikana")
    return kopio


def lue_salkku(polku):
    """Palauttaa dictin tai None jos tiedostoa ei ole / ei viela ajettu."""
    if not os.path.exists(polku):
        return None
    con = _ro(polku)
    try:
        tila = dict(con.execute("SELECT avain, arvo FROM tila").fetchall())
        arvot = con.execute("SELECT paiva, arvo FROM arvot ORDER BY paiva").fetchall()
        kaupat = con.execute("SELECT paiva, symbol, puoli, maara, hinta, kulu, slippage_kulu FROM kaupat ORDER BY id").fetchall()
        paatokset = con.execute("SELECT paiva, hylatty FROM paatokset ORDER BY paiva").fetchall()
        kateiset = con.execute("SELECT arvo, kateinen FROM arvot ORDER BY paiva").fetchall()
        try:    # taulu syntyy vasta, kun paiva.py on kirjannut ensimmaisen kriitikon paatoksen
            kriitikko = con.execute("SELECT paiva, ehdotus, paatos, kerroin FROM kriitikko_loki ORDER BY paiva").fetchall()
        except sqlite3.OperationalError:
            kriitikko = []
    finally:
        con.close()
    return {"tila": tila, "arvot": arvot, "kaupat": kaupat, "paatokset": paatokset, "kateiset": kateiset, "kriitikko": kriitikko}


def _paivatuotto(markkina, paiva, painot):
    """Painotettu paivatuotto (osuus salkusta) open->close annetuilla painoilla, tai None jos hinta tai paino puuttuu."""
    d = date.fromisoformat(paiva)
    ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    tuotto = 0.0
    for symbol, paino in painot.items():
        try:
            w = float(paino)
        except (TypeError, ValueError):
            return None
        h = markkina.execute("SELECT open, close FROM hinnat WHERE symbol=? AND ts=?", (symbol, ts)).fetchone()
        if h is None or not h[0] or h[0] <= 0:
            return None
        tuotto += w * (h[1] / h[0] - 1)
    return tuotto


def kriitikon_vaikutus(rivit, markkina):
    """rivit: (paiva, ehdotus-JSON, paatos, kerroin). Kriitikon arvo = lopullisen ja analyytikon ehdotuksen paivatuoton ero (ilman kuluja):
    (kerroin - 1) * R(ehdotus), koska kriitikko vain skaalaa painoja. Positiivinen = kriitikko auttoi. Vain paivat, joilla kriitikko muutti jotain."""
    pien = sum(1 for r in rivit if r[2] == "pienenna")
    hyl = sum(1 for r in rivit if r[2] == "hylkaa")
    arvo, paivia = 0.0, 0
    for paiva, ehdotus, paatos, kerroin in rivit:
        if paatos not in ("pienenna", "hylkaa"):
            continue
        try:
            painot = json.loads(ehdotus)
            k = 0.0 if paatos == "hylkaa" else float(kerroin)
        except (TypeError, ValueError):
            continue
        if not isinstance(painot, dict):
            continue
        r = _paivatuotto(markkina, paiva, painot)
        if r is not None:
            arvo += (k - 1.0) * r
            paivia += 1
    return {"n": len(rivit), "pienensi": pien, "hylkasi": hyl, "arvo": 100.0 * arvo, "arvo_paivia": paivia}


def _osuus(a, b):
    return 100.0 * a / b if b else 0.0


def havaintorivit(s, markkina):
    """Yhden mallisalkun mitatut luvut (vain prosessi, ei tuottoa): paatokset, kriitikko, kulut, vaihtuvuus, kateisosuus."""
    n = len(s["paatokset"])
    hylatyt = sum(1 for _, h in s["paatokset"] if h)
    rivit = ["- paatospaivia %d, hylattyja %.0f %%" % (n, _osuus(hylatyt, n))]
    k = kriitikon_vaikutus(s["kriitikko"], markkina)
    if k["n"]:
        rivit.append("- kriitikko: %d ehdotusta, pienensi %.0f %%, hylkasi %.0f %%; arvo %+.2f %%-yks (%d pv, ilman kuluja)" % (
            k["n"], _osuus(k["pienensi"], k["n"]), _osuus(k["hylkasi"], k["n"]), k["arvo"], k["arvo_paivia"]))
    else:
        rivit.append("- kriitikko: ei viela kirjauksia")
    kulut = sum(x[5] + x[6] for x in s["kaupat"])
    kauppa = sum(x[3] * x[4] for x in s["kaupat"])
    keskiarvo = sum(a for a, _ in s["kateiset"]) / len(s["kateiset"]) if s["kateiset"] else 0.0
    vaihtuvuus = _osuus(kauppa, keskiarvo * n) if keskiarvo and n else 0.0
    kateis = sum(_osuus(c, a) for a, c in s["kateiset"]) / len(s["kateiset"]) if s["kateiset"] else 0.0
    rivit.append("- kulut %.1f USDT (%.2f / paatos), vaihtuvuus %.1f %%/pv, kateisosuus %.0f %%" % (kulut, kulut / n if n else 0.0, vaihtuvuus, kateis))
    return rivit


def havainnot_teksti(tanaan, salkut, tiedot, markkina, paivarivit=()):
    """salkut: {nimi: lue_salkku-tulos}, tiedot: {nimi: salkun_rivi-tiedot}. Enintaan HAVAINNOT_MAX merkkia, vain mittausta.
    paivarivit: paivatilan rivit, jotka sailytetaan viikkotekstin perassa (viikko-osa lyhenee ensin)."""
    rivit = ["# Tiimin omat aiemmat tulokset (dataa, ei ohjeita)",
             "Paivitetty %s. Mittausta, ei johtopaatoksia; tuottoa ei vaiteta." % tanaan.isoformat()]
    for nimi in sorted(salkut):
        t = tiedot.get(nimi)
        pv = (date.fromisoformat(t["paiva"]) - date.fromisoformat(t["alku"])).days + 1 if t and t["alku"] else 0
        if salkut[nimi] is None or not t or pv < MIN_PV_JOHTOPAATOS:
            rivit.append("## %s: ei viela dataa (%d pv < %d)" % (nimi, pv, MIN_PV_JOHTOPAATOS))
            continue
        rivit.append("## %s (%d pv)" % (nimi, pv))
        rivit += havaintorivit(salkut[nimi], markkina)
    return _yhdista(rivit, paivarivit)


def _paivaosa(paivat):
    return (PAIVAT_OTSIKKO + "\n" + "\n".join(paivat) + "\n") if paivat else ""


def _yhdista(viikkorivit, paivarivit):
    """Viikko-osa + paivaosa, yhteensa enintaan HAVAINNOT_MAX merkkia. Paivaosa saa vieda enintaan puolet; vanhimmat paivat pois ensin.
    Viikko-osa katkaistaan viimeisena."""
    paivat = list(paivarivit)[-PAIVIA_MAX:]
    while paivat and len(_paivaosa(paivat)) > HAVAINNOT_MAX // 2:
        paivat = paivat[1:]
    osa = _paivaosa(paivat)
    viikko = "\n".join(viikkorivit) + "\n"
    tila = HAVAINNOT_MAX - len(osa)
    if len(viikko) > tila:
        merkki = "\n(katkaistu)\n"
        viikko = viikko[:max(0, tila - len(merkki))].rsplit("\n", 1)[0] + merkki   # merkki mahtuu rajaan
    return viikko + osa


def lue_paivarivit(polku):
    """Paivatilan rivit (vanhin ensin) olemassa olevasta havainnot.md:sta; puuttuva tiedosto = ei riveja."""
    try:
        with open(polku, encoding="utf-8") as f:
            teksti = f.read()
    except OSError:
        return []
    if PAIVAT_OTSIKKO not in teksti:
        return []
    return [r for r in teksti.split(PAIVAT_OTSIKKO, 1)[1].splitlines() if _PAIVARIVI_RE.match(r)]


def lue_viikko_osa(polku):
    """Viikkoretron osa (ennen paivaosaa) rivilistana; tyhja jos tiedostoa ei ole."""
    try:
        with open(polku, encoding="utf-8") as f:
            teksti = f.read()
    except OSError:
        return []
    return teksti.split(PAIVAT_OTSIKKO, 1)[0].rstrip("\n").splitlines()


def paivarivi(tanaan, salkut):
    """Yhden illan rivi: kullekin salkulle viimeisin kirjattu paiva (tanaan tai eilen), muutos, kulut, kaupat, tappion mitattu syy.
    Tappion syy luokitellaan datasta: kulut (>= puolet tappiosta), hylatty paatos, muuten markkina (muutos + kulut). Ei arvailua.
    Palauttaa None jos mikaan salkku ei ole ajanut tanaan eika eilen."""
    osat, paiva = [], None
    for nimi in sorted(salkut):
        s = salkut[nimi]
        if not s or not s["arvot"]:
            continue
        p, a = s["arvot"][-1]
        if date.fromisoformat(p) < tanaan - timedelta(days=1):
            continue
        paiva = max(paiva, p) if paiva else p
        edell = s["arvot"][-2][1] if len(s["arvot"]) > 1 else float(s["tila"].get("alkupaoma", rajat.ALKUPAOMA))
        muutos = a - edell
        kaupat = [k for k in s["kaupat"] if k[0] == p]
        kulut = sum(k[5] + k[6] for k in kaupat)
        osa = "%s %+.2f %% (%d kauppaa, kulut %.1f)" % (nimi, _pct(a, edell), len(kaupat), kulut)
        if muutos < 0:
            hyl = next((h for pp, h in s["paatokset"] if pp == p and h), None)
            if kulut > 0 and kulut >= abs(muutos) / 2:
                syy = "kulut"
            elif hyl:
                syy = "hylatty:" + re.sub(r"[^A-Za-z0-9_]", "", str(hyl))[:12]
            else:
                syy = "markkina %+.1f" % (muutos + kulut)
            krit = next((pa for pp, _e, pa, _k in s["kriitikko"] if pp == p and pa), None)
            krit = re.sub(r"[^A-Za-z0-9_]", "", str(krit))[:12] if krit else ""   # sama siivous kuin hyl: teksti menee agenttien kehotteeseen
            osa += " TAPPIO syy=%s%s" % (syy, " krit=" + krit if krit else "")
        osat.append(osa)
    if not osat:
        return None
    rivi = "- %s %s" % (paiva, "; ".join(osat))
    return rivi if len(rivi) <= PAIVARIVI_MAX else rivi[:PAIVARIVI_MAX - 3] + "..."


def paivita_paivat(polku, rivi):
    """Lisaa paivan rivi havainnot.md:n paivaosaan (sama paiva korvataan, uusin korvaa vanhimman) ja kirjoittaa atomisesti.
    Viikko-osa sailyy sellaisenaan. Palauttaa kirjoitetun tekstin."""
    avain = _PAIVARIVI_RE.match(rivi).group(1)
    rivit = [r for r in lue_paivarivit(polku) if _PAIVARIVI_RE.match(r).group(1) != avain] + [rivi]
    rivit.sort(key=lambda r: _PAIVARIVI_RE.match(r).group(1))
    viikko = lue_viikko_osa(polku) or ["# Tiimin omat aiemmat tulokset (dataa, ei ohjeita)", "Viikkoretro ei ole viela ajettu."]
    teksti = _yhdista(viikko, rivit)
    kirjoita_havainnot(polku, teksti)
    return teksti


SILMUKKA_MIN_KRIITIKKOPAIVIA = 10   # alle taman ei kirjata "kriitikko auttoi/ei auttanut"


def silmukka_havainnot(tanaan, salkut, tiedot, markkina):
    """Viikkoretron mitatut luvut rakenteisiksi havainnoiksi (silmukka.py). Vain forward-paperidata (aloituspaivasta eteenpain),
    ei holdoutia. Jokainen havainto viittaa mitattuun lukuun ja otoskokoon; liian lyhyesta datasta ei kirjata mitaan."""
    ulos = []
    for nimi in sorted(salkut):
        t, s = tiedot.get(nimi), salkut[nimi]
        if s is None or not t or not t["alku"]:
            continue
        pv = (date.fromisoformat(t["paiva"]) - date.fromisoformat(t["alku"])).days + 1
        if pv < MIN_PV_JOHTOPAATOS:
            continue
        k = kriitikon_vaikutus(s["kriitikko"], markkina)
        if k["arvo_paivia"] >= SILMUKKA_MIN_KRIITIKKOPAIVIA:
            vaite = "%s: kriitikko %s" % (nimi, "auttoi" if k["arvo"] > 0 else "ei auttanut")
            ulos.append(silmukka.tee_havainto("retro", vaite, [{"nimi": "kriitikon_arvo", "arvo": k["arvo"], "yksikko": "%-yks"}],
                                              k["arvo_paivia"], "forward-paperi, ilman kuluja", tanaan, "retro|kriitikko|%s" % nimi))
        n = len(s["paatokset"])
        if n:
            kulut = sum(x[5] + x[6] for x in s["kaupat"])
            ulos.append(silmukka.tee_havainto("retro", "%s: kulut paatosta kohti" % nimi,
                                              [{"nimi": "kulut_per_paatos", "arvo": kulut / n, "yksikko": "USDT"}],
                                              n, "forward-paperi", tanaan, "retro|kulut|%s" % nimi))
    return ulos


def kirjoita_havainnot(polku, teksti):
    """Ylikirjoittaa tiedoston atomisesti (kirjoitus tmp-tiedostoon samassa hakemistossa, sitten os.replace)."""
    tmp = polku + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(teksti)
    os.replace(tmp, polku)


def _pct(a, b):
    return 100.0 * (a / b - 1) if b else 0.0


def salkun_rivi(nimi, s, tanaan):
    """Palauttaa (rivit, tiedot) yhdelle salkulle."""
    if s is None or not s["arvot"]:
        return ["%s: ei viela dataa" % nimi], None
    arvot = s["arvot"]
    viim_p, viim_a = arvot[-1]
    alku_a = float(s["tila"].get("alkupaoma", rajat.ALKUPAOMA))
    viikko_alku = (date.fromisoformat(viim_p) - timedelta(days=7)).isoformat()
    ed = [a for p, a in arvot if p <= viikko_alku]
    viikko_a = ed[-1] if ed else alku_a
    mdd = mittarit.mittarit([a for _, a in arvot])
    vk_kaupat = [k for k in s["kaupat"] if k[0] > viikko_alku]
    kulut = sum(k[5] + k[6] for k in vk_kaupat)
    vk_p = [h for p, h in s["paatokset"] if p > viikko_alku]
    hylatyt = sum(1 for h in vk_p if h)
    rivit = ["%s: %.0f USDT | alusta %+.2f %% | viikko %+.2f %%" % (nimi, viim_a, _pct(viim_a, alku_a), _pct(viim_a, viikko_a)),
             "  kaupat %d (kulut %.1f USDT), paatospaivia %d, hylattyja %d%s" % (
                 len(vk_kaupat), kulut, len(vk_p), hylatyt, ", max-drawdown %.1f %%" % (100 * mdd["max_dd"]) if mdd else "")]
    rivit.append("  " + verorap.rivi(s["kaupat"], viim_p[:4]))
    if s["tila"].get("jarru") == "1":
        rivit.append("  HATAJARRU LUKOSSA")
    return rivit, {"paiva": viim_p, "alku": s["tila"].get("aloituspaiva"), "tuotto": _pct(viim_a, alku_a), "viikko": _pct(viim_a, viikko_a), "arvot": arvot}


def vertailut(markkina, aloitus, viimeisin):
    """Palauttaa {nimi: (tuotto_alusta, tuotto_viikko)} tai {} jos dataa ei ole."""
    ulos = {}
    for nimi, painot in (("vertailu (Tonyn jako)", rajat.VERTAILU_PAINOT), ("BTC", {"BTCUSDT": 1.0})):
        try:
            v = mittarit.vertailusalkku(markkina, painot, aloitus, viimeisin)
        except salkku.Datavirhe:
            continue
        if len(v) < 2:
            continue
        viikko = v[max(0, len(v) - 8)][1]
        ulos[nimi] = (_pct(v[-1][1], rajat.ALKUPAOMA), _pct(v[-1][1], viikko))
    return ulos


def raportti(data_polku, salkut, tanaan, pysaytys_polku=None, havainnot_polku=None, silmukka_polku=None):
    rivit = ["Kauppatiimi viikkoretro (%s)" % tanaan.isoformat()]
    tiedot, raaka = {}, {}
    for nimi, polku in sorted(salkut.items()):
        try:
            s = lue_salkku(polku)
        except sqlite3.Error as e:
            rivit.append("%s: kantaa ei voi lukea (%s)" % (nimi, type(e).__name__))
            raaka[nimi] = None
            continue
        raaka[nimi] = s
        r, t = salkun_rivi(nimi, s, tanaan)
        rivit += r
        if t:
            tiedot[nimi] = t
    if tiedot:
        alut = [t["alku"] for t in tiedot.values() if t["alku"]]
        viim = max(date.fromisoformat(t["paiva"]) for t in tiedot.values())
        if alut:
            aloitus = date.fromisoformat(min(alut))
            try:
                markkina = _ro(data_polku)
                try:
                    v = vertailut(markkina, aloitus, viim)
                finally:
                    markkina.close()
            except sqlite3.Error:
                v = {}
            for n, (a, w) in v.items():
                rivit.append("%s: alusta %+.2f %% | viikko %+.2f %%" % (n, a, w))
            if not v:
                rivit.append("vertailu ei saatavilla")
            pv = (viim - aloitus).days + 1
        else:
            pv = 0
        if pv < MIN_PV_JOHTOPAATOS:
            rivit.append("Historiaa %d pv < %d: ei johtopaatoksia kummastakaan mallista (satunnaisvaihtelu dominoi)." % (pv, MIN_PV_JOHTOPAATOS))
        elif len(tiedot) == 2:
            (n1, t1), (n2, t2) = sorted(tiedot.items())
            ero = t1["tuotto"] - t2["tuotto"]
            rivit.append("%d pv: %s vs %s ero %+.2f %%-yks. Yksi jakso ei todista mallin paremmuutta; K6-testit ratkaisevat." % (pv, n1, n2, ero))
    if havainnot_polku:
        try:
            markkina = _ro(data_polku)
            try:
                kirjoita_havainnot(havainnot_polku, havainnot_teksti(tanaan, raaka, tiedot, markkina, lue_paivarivit(havainnot_polku)))
            finally:
                markkina.close()
        except (sqlite3.Error, OSError) as e:
            rivit.append("havaintoja ei voitu kirjoittaa (%s)" % type(e).__name__)
    if silmukka_polku and silmukka.kaytossa():
        try:
            markkina = _ro(data_polku)
            try:
                silmukka.kirjaa_havainnot(silmukka_polku, silmukka_havainnot(tanaan, raaka, tiedot, markkina), tanaan)
            finally:
                markkina.close()
        except (sqlite3.Error, OSError, silmukka.Havaintovirhe) as e:
            rivit.append("silmukan havaintoja ei voitu kirjoittaa (%s)" % type(e).__name__)
    if pysaytys_polku and os.path.exists(pysaytys_polku):
        rivit.append("KILL SWITCH PAALLA: ajot pysaytetty")
    return "\n".join(rivit)


def main_paiva(tanaan, salkut, havainnot_polku):
    """Paivatila: lukee salkut vain luku, kirjoittaa havainnot.md:n paivaosan. Ei LLM-kutsuja eika kuluja."""
    raaka = {}
    for nimi, polku in sorted(salkut.items()):
        try:
            raaka[nimi] = lue_salkku(polku)
        except sqlite3.Error as e:
            print("%s: kantaa ei voi lukea (%s)" % (nimi, type(e).__name__))
    if salkut and not any(raaka.values()):
        # Yksikaan salkku ei ollut luettavissa (kanta puuttuu tai virhe): ei ole "ei uutta paivaa", vaan vika. Nollasta poikkeava
        # koodi nakyy systemd-yksikon tilassa, eika havainnot.md:aa kirjoiteta.
        print("Paivatila %s: yhtaan salkkua ei voitu lukea (%d salkkua), havainnot.md ennallaan" % (tanaan.isoformat(), len(salkut)))
        return 1
    rivi = paivarivi(tanaan, raaka)
    if rivi is None:
        print("Paivatila %s: ei uutta salkkupaivaa, havainnot.md ennallaan" % tanaan.isoformat())
        return 0
    try:
        paivita_paivat(havainnot_polku, rivi)
    except OSError as e:
        print("havaintoja ei voitu kirjoittaa (%s)" % type(e).__name__)
        return 1
    print(rivi)
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tg-env", default="/srv/kauppa/.env")
    ap.add_argument("--ei-tg", action="store_true")
    ap.add_argument("--paiva", action="store_true", help="illan paivatila: paivittaa vain havainnot.md:n paivaosan, ei Telegramia, ei LLM:aa")
    a = ap.parse_args()
    tanaan = datetime.now(timezone.utc).date()
    if a.paiva:
        return main_paiva(tanaan, rajat.SALKUT, HAVAINNOT_POLKU)
    msg = raportti(config.DB_POLKU, rajat.SALKUT, tanaan, rajat.PYSAYTYS, HAVAINNOT_POLKU, silmukka.HAVAINTO_POLKU)
    print(msg)
    if not a.ei_tg:
        try:
            env = llm.lue_env(a.tg_env)
        except llm.LLMVirhe:
            env = {}
        notify.laheta(msg, env)
    return 0


if __name__ == "__main__":
    sys.exit(main())
