"""Verokirjanpidon raporttirivit: kutsuu vain kirjanpito.fifo / kirjanpito.vuosiyhteenveto (ei omaa laskentaa).
Kaytto: python3 -m raportti vero 2026 [salkku-nimi]   (lukee salkun kannan vain luku -tilassa)"""
import sqlite3
import sys

import kirjanpito
import rajat


def tulos_vuodelle(kaupat, vuosi):
    """kaupat: rivit (paiva, symbol, puoli, maara, hinta, kulu, ...) aikajarjestyksessa.
    Palauttaa (realisoitunut_tulos, kauppoja_vuonna, myyntirealisointeja_vuonna). Nostaa ValueError jos kirjanpito ei tasmaa."""
    vuosi = str(vuosi)
    rivit = [tuple(k[:6]) for k in kaupat]
    real = kirjanpito.fifo(rivit)
    tulos = kirjanpito.vuosiyhteenveto(real).get(vuosi, 0.0)
    return (tulos,
            sum(1 for k in rivit if k[0][:4] == vuosi),
            sum(1 for r in real if r[0][:4] == vuosi))


def rivi(kaupat, vuosi, nimi=None):
    """Yksi raporttirivi: realisoitunut tulos (paperi) ja kauppojen maara. Virhe ei kaada raporttia."""
    otsikko = "Realisoitunut tulos %s (paperi, FIFO)" % vuosi
    if nimi:
        otsikko = "%s: %s" % (nimi, otsikko)
    try:
        tulos, n, myynnit = tulos_vuodelle(kaupat, vuosi)
    except (ValueError, TypeError, IndexError) as e:
        return "%s: ei saatavilla (%s)" % (otsikko, type(e).__name__)
    return "%s: %+.2f USDT | kauppoja %d (myyntia %d)" % (otsikko, tulos, n, myynnit)


def lue_kaupat(polku):
    con = sqlite3.connect("file:%s?mode=ro" % polku, uri=True, timeout=10)
    try:
        return con.execute("SELECT paiva, symbol, puoli, maara, hinta, kulu FROM kaupat ORDER BY id").fetchall()
    finally:
        con.close()


def vero(vuosi, salkut=None):
    salkut = salkut if salkut is not None else rajat.SALKUT
    ulos = []
    for nimi, polku in sorted(salkut.items()):
        try:
            ulos.append(rivi(lue_kaupat(polku), vuosi, nimi))
        except sqlite3.Error as e:
            ulos.append("%s: kantaa ei voi lukea (%s)" % (nimi, type(e).__name__))
    return "\n".join(ulos)


def main(argv):
    if len(argv) < 3 or argv[1] != "vero" or not argv[2].isdigit() or len(argv[2]) != 4:
        print("kaytto: python3 -m raportti vero VVVV [salkku]")
        return 2
    salkut = rajat.SALKUT
    if len(argv) > 3:
        if argv[3] not in salkut:
            print("tuntematon salkku (vaihtoehdot: %s)" % ", ".join(sorted(salkut)))
            return 2
        salkut = {argv[3]: salkut[argv[3]]}
    print(vero(argv[2], salkut))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
