"""Hakee/paivittaa markkinadatan kantaan. Ajo: python3 lataa.py [--symbolit BTCUSDT,ETHUSDT]
Idempotentti: voi ajaa milloin tahansa uudelleen. Koodi on laki: lista kovakoodattu config.py:ssa."""
import argparse
import calendar
import fcntl
import sys
import time
from datetime import date, datetime, timedelta, timezone

import config
import db
import lahde
import silmukka


def loki(*a):
    print(datetime.now().strftime("%H:%M:%S"), *a, flush=True)


def kuukaudet(tanaan):
    y, m = config.ALKU
    while (y, m) <= (tanaan.year, tanaan.month):
        yield y, m
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)


def edellinen_kk(tanaan):
    return (tanaan.year - 1, 12) if tanaan.month == 1 else (tanaan.year, tanaan.month - 1)


def _hae(hae, url, paussi):
    if paussi:
        time.sleep(paussi)
    return hae(url)


def _spot_paivat(con, s, y, m, tanaan, hae, paussi, tulos):
    viimeinen = min(calendar.monthrange(y, m)[1], 31)
    for d in range(1, viimeinen + 1):
        paiva = date(y, m, d)
        if paiva >= tanaan:
            break
        jakso = paiva.isoformat()
        if db.tila(con, "spot_pv", s, jakso) == "ok":
            continue
        try:
            data = _hae(hae, lahde.url_spot_pv(s, y, m, d), paussi)
            if data is None:
                continue  # ei viela julkaistu, ei kirjata
            rivit = lahde.parse_klines(lahde.pura_zip(data))
            tulos["hinnat"] += db.lisaa_hinnat(con, s, rivit)
            db.kirjaa(con, "spot_pv", s, jakso, "ok", len(rivit))
        except (lahde.Parsevirhe, lahde.Verkkovirhe) as e:
            db.kirjaa(con, "spot_pv", s, jakso, "virhe")
            tulos["virheet"].append("%s %s: %s" % (s, jakso, e))


def paivita_spot(con, s, tanaan, hae, paussi, tulos):
    nyt = (tanaan.year, tanaan.month)
    ed = edellinen_kk(tanaan)
    for y, m in kuukaudet(tanaan):
        if (y, m) == nyt:
            _spot_paivat(con, s, y, m, tanaan, hae, paussi, tulos)
            continue
        jakso = "%d-%02d" % (y, m)
        if db.tila(con, "spot_kk", s, jakso) in ("ok", "puuttuu"):
            continue
        try:
            data = _hae(hae, lahde.url_spot_kk(s, y, m), paussi)
            if data is None:
                if (y, m) == ed:
                    _spot_paivat(con, s, y, m, tanaan, hae, paussi, tulos)  # kk-tiedostoa ei viela
                else:
                    db.kirjaa(con, "spot_kk", s, jakso, "puuttuu")
                continue
            rivit = lahde.parse_klines(lahde.pura_zip(data))
            tulos["hinnat"] += db.lisaa_hinnat(con, s, rivit)
            db.kirjaa(con, "spot_kk", s, jakso, "ok", len(rivit))
        except (lahde.Parsevirhe, lahde.Verkkovirhe) as e:
            db.kirjaa(con, "spot_kk", s, jakso, "virhe")
            tulos["virheet"].append("%s %s: %s" % (s, jakso, e))


def paivita_funding(con, s, tanaan, hae, paussi, tulos):
    nyt = (tanaan.year, tanaan.month)
    for y, m in kuukaudet(tanaan):
        if (y, m) == nyt:
            continue
        jakso = "%d-%02d" % (y, m)
        if db.tila(con, "funding_kk", s, jakso) in ("ok", "puuttuu"):
            continue
        try:
            data = _hae(hae, lahde.url_funding_kk(s, y, m), paussi)
            if data is None:
                if (y, m) != edellinen_kk(tanaan):
                    db.kirjaa(con, "funding_kk", s, jakso, "puuttuu")
                continue
            rivit = lahde.parse_funding(lahde.pura_zip(data))
            tulos["funding"] += db.lisaa_funding(con, s, rivit)
            db.kirjaa(con, "funding_kk", s, jakso, "ok", len(rivit))
        except (lahde.Parsevirhe, lahde.Verkkovirhe) as e:
            db.kirjaa(con, "funding_kk", s, jakso, "virhe")
            tulos["virheet"].append("funding %s %s: %s" % (s, jakso, e))
    # REST-taydennys kuluvalle kuukaudelle (kk-tiedosto julkaistaan vasta kuun jalkeen)
    alku = db.viimeisin_funding(con, s)
    if alku is None:
        ed = edellinen_kk(tanaan)
        alku = int(datetime(ed[0], ed[1], 1, tzinfo=timezone.utc).timestamp() * 1000)
    else:
        alku += 1
    for _ in range(20):
        try:
            data = _hae(hae, lahde.url_funding_rest(s, alku), paussi)
            if data is None:
                break
            rivit = lahde.parse_funding_rest(data)
        except (lahde.Parsevirhe, lahde.Verkkovirhe) as e:
            tulos["virheet"].append("funding-rest %s: %s" % (s, e))
            break
        if not rivit:
            break
        tulos["funding"] += db.lisaa_funding(con, s, rivit)
        if len(rivit) < 1000:
            break
        alku = max(r[0] for r in rivit) + 1


def yhteystesti(hae):
    """Varmistaa etta lahde vastaa: tunnettu vanha tiedosto pitaa loytya."""
    data = hae(lahde.url_spot_kk("BTCUSDT", 2023, 1))
    if data is None:
        return False
    return len(lahde.parse_klines(lahde.pura_zip(data))) == 31


def aja(con, symbolit, tanaan, hae, paussi=0.0):
    yht = {"hinnat": 0, "funding": 0, "virheet": []}
    for s in symbolit:
        t = {"hinnat": 0, "funding": 0, "virheet": []}
        paivita_spot(con, s, tanaan, hae, paussi, t)
        paivita_funding(con, s, tanaan, hae, paussi, t)
        loki("%-10s uusia hintarivej: %-5d funding: %-5d virheita: %d" % (s, t["hinnat"], t["funding"], len(t["virheet"])))
        yht["hinnat"] += t["hinnat"]
        yht["funding"] += t["funding"]
        yht["virheet"] += t["virheet"]
    return yht


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbolit", help="pilkulla eroteltu, oltava listassa config.SYMBOLIT")
    ap.add_argument("--ohita-testi", action="store_true")
    a = ap.parse_args()
    symbolit = config.SYMBOLIT
    if a.symbolit:
        pyydetyt = [x.strip().upper() for x in a.symbolit.split(",") if x.strip()]
        vieraat = [x for x in pyydetyt if x not in config.SYMBOLIT]
        if vieraat:
            loki("EI SALLITTU (ei listassa):", ",".join(vieraat))
            return 3
        symbolit = pyydetyt
    lukko = open(config.LUKKO_POLKU, "w")
    try:
        fcntl.flock(lukko, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        loki("toinen ajo kaynnissa, lopetetaan")
        return 3
    con = db.avaa(config.DB_POLKU)
    tanaan = datetime.now(timezone.utc).date()
    try:
        if not a.ohita_testi and not yhteystesti(lahde.http_hae):
            loki("YHTEYSTESTI EPAONNISTUI: data.binance.vision ei anna tunnettua tiedostoa")
            return 2
        yht = aja(con, symbolit, tanaan, lahde.http_hae, config.PAUSSI)
    except lahde.Estetty as e:
        loki("ALUE ESTETTY (451), ei yriteta kiertaa:", e)
        return 2
    except lahde.Verkkovirhe as e:
        loki("VERKKOVIRHE:", e)
        return 2
    if silmukka.kaytossa():
        try:
            loki("datapyynnot:", silmukka.hae_pyynnot(silmukka.PYYNTO_POLKU, silmukka.LISADATA_DB, lahde.http_hae,
                                                      datetime.now(timezone.utc).date()))
        except (OSError, lahde.Estetty):
            loki("datapyynnot: ohitettu")
    loki("VALMIS uusia hintarivej %d, funding %d, virheita %d" % (yht["hinnat"], yht["funding"], len(yht["virheet"])))
    for v in yht["virheet"][:30]:
        loki("  VIRHE", v)
    return 2 if yht["virheet"] else 0


if __name__ == "__main__":
    sys.exit(main())
