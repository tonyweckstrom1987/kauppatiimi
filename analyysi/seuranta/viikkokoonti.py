"""Viikkosivun runko suoraan datasta: jakso, tuoreus, tuotot ja tyhjat paivat.

Vain luku (sqlite immutable=1), vain stdlib, ei verkkoa. Luvut tulevat seuranta.py:n
funktioista laske ja tuoreus; tuottoja ei lasketa tassa omalla kaavalla.
Ei arvioi paatoksia eika anna suosituksia.

Kaytto (repon juuresta): python3 -m analyysi.seuranta.viikkokoonti [kauppa-hakemisto] [--loppu YYYY-MM-DD]
"""
import sqlite3
import sys
from datetime import date

from analyysi.seuranta import seuranta as s

# Tyhjan paatospaivan luokat. Luokitus on siirretty laske_tyhjat.py:sta sellaisenaan.
LUOKAT = (
    ("d", "ei LLM-kutsuja"),
    ("a", "kriitikko hylkäsi"),
    ("c", "kutsusarja päättyi analyytikkoon"),
    ("b", "analyytikko ja kriitikko, ei kriitikkolokia"),
    ("e", "muu"),
)
MAX_RIVEJA = 40


def luokka(kutsut, loki):
    """kutsut: roolit kutsujarjestyksessa, loki: kriitikko_loki.paatos tai None."""
    if not kutsut:
        return "d"
    if loki == "hylkaa":
        return "a"
    if kutsut[-1] == "analyytikko":
        return "c"
    if kutsut[-2:] == ["analyytikko", "kriitikko"] and loki is None:
        return "b"
    return "e"


def tyhjat(hakemisto, nimi, alku, loppu):
    """Palauttaa {luokka: lkm} jakson tyhjille paatospaiville tai None, jos kantaa
    tai taulua ei voi lukea (tulostuu "?")."""
    try:
        c = s.avaa(hakemisto, nimi + ".db")
        loki = dict(c.execute("select paiva, paatos from kriitikko_loki"))
        paivat = [r[0] for r in c.execute(
            "select paiva from paatokset where hylatty is not null order by paiva")]
        tulos = {}
        for p in paivat:
            if not alku.isoformat() <= p <= loppu.isoformat():
                continue
            k = [r[0] for r in c.execute(
                "select rooli from llm_kutsut where paiva=? order by rowid", (p,))]
            l = luokka(k, loki.get(p))
            tulos[l] = tulos.get(l, 0) + 1
        return tulos
    except sqlite3.Error:
        return None


def pros(x):
    return "?" if x is None else ("%.2f %%" % (x * 100)).replace(".", ",")


def pp(x):
    return ("%+.2f %%" % (x * 100)).replace(".", ",").replace("-", "−")


def aste(x):
    return "?" if x is None else ("%.1f %%" % (x * 100)).replace(".", ",")


def tuoreus_rivi(hakemisto):
    teksti, ika = s.tuoreus(hakemisto)
    if ika is None:
        return "Data: " + teksti
    aika = teksti.split(" (")[0]
    huom = "; VANHA, yli 36 h" if ika > 36 else ""
    return "Data: PAIVITETTY %s, viive %s h%s" % (aika, ("%.1f" % ika).replace(".", ","), huom)


def koonti(hakemisto, loppu=None, min_paivat=s.MIN_PAIVAT_VALI):
    """Palauttaa Markdown-tekstin."""
    t = s.laske(hakemisto, loppu, min_paivat)
    r = ["# Kauppatiimin viikkoluvut (ei sijoitusneuvo)", ""]
    r.append("Jakso: %s .. %s, %d pv / %d (%d %%)" % (
        t["alku"], t["loppu"], t["paivia"], s.TAVOITE_PAIVAT, round(100 * t["paivia"] / s.TAVOITE_PAIVAT)))
    r.append(tuoreus_rivi(hakemisto))
    r += ["", "| Tapa | Tuotto | Pudotus | Sijoitusaste |", "|---|---|---|---|"]
    for sa in t["salkut"]:
        r.append("| %s | %s | %s | %s |" % (sa["nimi"], pp(sa["tuotto"]), pros(sa["pudotus"]), aste(sa["w"])))
    ensin = t["salkut"][0]["vertailut"]
    for v in ensin:
        if v["nimi"].startswith("BTC, sijoitusaste"):
            continue  # salkkukohtainen, lisataan alle
        w = 1.0 if v["nimi"] == "BTC osta ja pida" else None
        r.append("| %s | %s | %s | %s |" % (v["nimi"], pp(v["tuotto"]), pros(v["pudotus"]), aste(w)))
    for sa in t["salkut"]:
        for v in sa["vertailut"]:
            if v["nimi"].startswith("BTC, sijoitusaste"):
                r.append("| %s, vertailu %s | %s | %s | %s |" % (
                    v["nimi"], sa["nimi"], pp(v["tuotto"]), pros(v["pudotus"]), aste(sa["w"])))
    r += ["", "Tyhjät päätöspäivät jaksolla (luokka: lukumäärä):"]
    for sa in t["salkut"]:
        lk = tyhjat(hakemisto, sa["nimi"], t["alku"], t["loppu"])
        if lk is None:
            r.append("- %s: ?" % sa["nimi"])
        else:
            osat = ["%s %s %d" % (k, nimi, lk[k]) for k, nimi in LUOKAT if lk.get(k)]
            r.append("- %s: %s" % (sa["nimi"], "; ".join(osat) if osat else "ei tyhjiä päiviä"))
    r += ["", "Ehdot 2, 4 ja 5: ei mitattu tällä komennolla."]
    return "\n".join(r)


def main(argv):
    try:
        hakemisto, loppu, min_paivat = s.jasenna(argv)
        teksti = koonti(hakemisto, loppu, min_paivat)
    except (sqlite3.Error, ValueError, KeyError, OSError, IndexError) as e:
        print("virhe: " + type(e).__name__)
        return 1
    print(teksti)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
