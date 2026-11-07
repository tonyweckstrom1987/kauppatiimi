"""Osakeindeksin paivahinnat CSV-tiedostosta, jonka Tony lataa itse (ei automaattista hakua, ei verkkoa).

Tiedostomuoto: otsikkorivi `paiva,open,close`, rivit `2026-01-05,100.0,101.5` (paiva ISO-muodossa, hinnat positiivisia lukuja).
Indeksin on tarkoitus olla hintaindeksi (esim. S&P 500): osingot eivat ole mukana.
Lahteen valinta ja kayttoehtojen tarkistus on Tonin tehtava: moduuli ei hae mitaan miltaan palvelulta.
Kaytto: passiivinen_vertailu.py antaa CSV-polun funktiolle lue()."""
import csv
from datetime import date


def lue(polku):
    """CSV -> {"open": {date: x}, "close": {date: x}}. Nostaa ValueError virheellisesta tai tyhjasta tiedostosta."""
    avaus, sulku = {}, {}
    with open(polku, newline="") as f:
        lukija = csv.DictReader(f)
        if not lukija.fieldnames or not {"paiva", "open", "close"} <= set(lukija.fieldnames):
            raise ValueError("otsikkorivin pitaa sisaltaa sarakkeet paiva,open,close")
        for r in lukija:
            p = date.fromisoformat(r["paiva"])
            o, c = float(r["open"]), float(r["close"])
            if not (o > 0 and c > 0):
                raise ValueError("hinnan pitaa olla positiivinen: %s" % r["paiva"])
            avaus[p], sulku[p] = o, c
    if not sulku:
        raise ValueError("tiedosto on tyhja")
    return {"open": avaus, "close": sulku}
