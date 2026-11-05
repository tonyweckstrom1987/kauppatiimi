# Oikean rahan suojat (ei käytössä)

Tämä ohje kuvaa suojat, jotka on rakennettu valmiiksi ennen oikeaa kaupankäyntiä. **Mikään ajastin tai salkkuajo ei vielä käytä niitä.** Oikean rahan aloitus on Tonyn erillinen päätös kauppatiimin porttien 1–5 jälkeen. Tämä ei ole sijoitusneuvo.

| Moduuli | Tehtävä |
|---|---|
| `avaintarkistus.py` | Käynnistystarkistus: avaintiedoston oikeudet ja avaimen oikeudet pörssissä. |
| `hatapysaytys.py` | Pysäytystiedosto ja Telegram-komento "seis kauppa". Peruu avoimet toimeksiannot. |
| `verokirjanpito.py` | Jokainen kauppa CSV-riviksi yksityiseen paikkaan, euromääräinen arvo mukana. |
| `porssi.py` | Pieni Binance spot -asiakas. Jokainen toimeksianto tarkistaa hätäpysäytyksen ensin. |

## 1. API-avaimen luonti (Tony tekee itse)

Avain luodaan vasta, kun oikeaan rahaan siirtymisestä on päätetty.

1. Binance → API Management → Create API → System generated (HMAC).
2. Rajaa avain luotettuun IP-osoitteeseen ("Restrict access to trusted IPs only"). Osoitteeksi palvelimen julkinen lähtöosoite.
3. Oikeuksiksi vain **Enable Reading** ja **Enable Spot & Margin Trading**.
4. Jätä pois: **Enable Withdrawals**, **Enable Internal Transfer**, **Permits Universal Transfer**, **Enable Margin**, **Enable Futures** ja **Enable Options**.
5. Tallenna avain palvelimelle tiedostoon, jonka oikeudet ovat 600. Avain ei koskaan repoon eikä chattiin.

```
sudo install -d -m 700 -o kauppa -g kauppa /srv/kauppa/salaiset
sudo install -m 600 -o kauppa -g kauppa /dev/null /srv/kauppa/salaiset/binance.key
sudo -u kauppa nano /srv/kauppa/salaiset/binance.key
```

Tiedoston sisältö:

```
BINANCE_API_KEY=...
BINANCE_API_SECRET=...
```

Käyttäjä `kauppa` on sama, jolla `kauppa-ajo.service` ajetaan. Alla olevat `python3 -m` -komennot ajetaan koodihakemistossa (`cd /srv/kauppa/k1`).

Tarkistus, joka ei tee kauppoja:

```
sudo -u kauppa python3 -m avaintarkistus --avain /srv/kauppa/salaiset/binance.key
```

Ohjelma kieltäytyy käynnistymästä, jos

- avaimella on nosto-oikeus tai siirto-oikeus
- avainta ei ole rajattu IP-osoitteisiin
- avaimella on margin-, futuuri- tai optio-oikeus
- tiedoston oikeudet eivät ole 600, tiedosto on symbolinen linkki tai sen omistaja on väärä
- tiedosto on git-työpuun sisällä
- hätäpysäytys on päällä
- oikeuksia ei saada luettua (esimerkiksi verkkovirhe).

Epäselvä tilanne johtaa aina kieltoon. Avaimen arvoa ei koskaan tulosteta.

Testiverkossa (testnet.binance.vision) ei ole avaimen oikeuksien rajapintaa, joten tarkistus kieltäytyy siellä aina. Se on tarkoituksellista.

## 2. Hätäpysäytys

Pysäytys tekee kaksi asiaa tässä järjestyksessä:

1. Se kirjoittaa pysäytystiedoston (`/srv/kauppa/data/PYSAYTYS`, sama kuin paperisalkkujen kill switch). Uusia toimeksiantoja ei lähetetä.
2. Se peruu kaikki avoimet toimeksiannot ja tarkistaa, ettei niitä jäänyt.

Pysäytys ei myy positioita. Jatkaminen tapahtuu vain käsin: Tony poistaa tiedoston.

```
python3 -m hatapysaytys --syy kasin                                         # pelkkä tiedosto
python3 -m hatapysaytys --syy kasin --avain /srv/kauppa/salaiset/binance.key  # tiedosto ja avointen peruutus
rm /srv/kauppa/data/PYSAYTYS                                                # jatko (Tonyn päätös)
```

Paluukoodi kertoo, onnistuiko pysäytys kokonaan. Nollasta poikkeava koodi tarkoittaa, että jokin jäi tekemättä:

- `0`: pysäytystiedosto on kirjoitettu, ja jos avain annettiin, kaikki avoimet toimeksiannot on peruttu.
- `1`: tiedosto on kirjoitettu, mutta peruutus jäi kesken (virhe tai avoimia jäi). Tarkista pörssistä käsin.
- `2`: tiedosto on kirjoitettu, mutta avainta ei voitu käyttää. Avoimia toimeksiantoja ei peruttu.
- `3`: pysäytystiedostoa ei voitu kirjoittaa. Pysäytys ei ole päällä.

Hätäpysäytyksen pörssiasiakas ei voi lähettää uusia toimeksiantoja: sen tarkistin kieltää ne aina.

Telegram: komento `seis kauppa` toimii vain, kun viestin chat-id ja lähettäjän id ovat Tonyn chat-id. Muut viestit ohitetaan. Kytkentäpiste on `hatapysaytys.kasittele_telegram(viesti, sallittu_chat_id, asiakas)`. Se kytketään Telegram-lukijaan (tehtävä 2026-11-04-12), kun lukija on valmis. Siihen asti pysäytys tehdään tiedostolla.

## 3. Verokirjanpito

`verokirjanpito.Verokirja` kirjaa jokaisen kaupan yhdeksi riviksi tiedostoon `/srv/kauppa/yksityinen/verokirjanpito.csv` (muutettavissa muuttujalla `KAUPPA_VEROKIRJA`).

Sarakkeet: `aika_utc, porssi, pari, puoli, maara, hinta, hinta_valuutta, arvo, kulu, kulu_valuutta, eur_kurssi, arvo_eur, kulu_eur, kauppa_id, toimeksianto_id`.

- Tiedosto ei saa olla git-työpuun sisällä. Hakemiston oikeudet ovat 700 ja tiedoston 600.
- Sama kauppa kirjataan vain kerran (`kauppa_id`).
- Rahamäärät lasketaan Decimal-tyypillä.
- `fifo_syote()` antaa rivit nykyiselle `kirjanpito.fifo`-funktiolle euroina kohdevaluutan mukaan ryhmiteltynä.

## 4. Muistutus ennen ensimmäistä oikeaa kauppaa (portti 5)

- [ ] Botin julkinen forkki (agenttifirma-bot/kauppatiimi) poistetaan.
- [ ] kauppatiimi-repo muutetaan yksityiseksi.
- [ ] Avain luotu yllä olevan ohjeen mukaan, ja `python3 -m avaintarkistus` antaa OK.
- [ ] Hätäpysäytys kokeiltu kerran: tiedosto ja Telegram-komento.
- [ ] Verokirjanpidon polku on yksityinen ja varmuuskopioitu.

## Avoimet asiat

- BNB:llä maksettu kaupan kulu on verotuksessa myös BNB:n luovutus. Sitä ei vielä kirjata erikseen.
- EUR-kurssin lähde valitaan, kun oikea kauppa-ajo rakennetaan (esimerkiksi Binancen EURUSDT tai EKP:n kurssi). Kurssi kirjataan jokaiselle riville.
