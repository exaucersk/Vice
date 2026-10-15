# Vice-Share-Dienst für gclips.kimonima.de

Dieses Paket enthält den kleinen Share-Dienst und die Compose-Dateien für deinen
Homeserver. Nextcloud speichert die Videos; der Dienst speichert nur Clip-IDs und
Metadaten in einem dauerhaften Docker-Volume. Die Umwandlung in H.264/AAC und die
Uploads übernimmt Vice auf deinem Gaming-PC.

Es verwendet deinen **bestehenden Cloudflare Tunnel**. Ein neuer Tunnel oder ein
Cloudflare-Token für dieses Paket ist nicht erforderlich. Hier werden keine
Nextcloud-Zugangsdaten gespeichert; der Dienst liest einzelne öffentliche
Dateifreigaben. Die Registrierung neuer Clips ist durch einen eigenen API-Token
geschützt. Wer einen Clip-Link besitzt, kann das Video ansehen und herunterladen.

## 1. Paket auf den Server kopieren

Das Archiv aus `dist/vice-share-host.tar.gz` mit deinem üblichen Dateitransfer auf
den Homeserver kopieren und dort entpacken:

```bash
tar -xzf vice-share-host.tar.gz
cd vice-share-host/deploy/share-host
```

Den vollständigen entpackten Verzeichnisbaum behalten: Der Docker-Build verwendet
auch die beiden Dateien unter `../../vice/`. Es werden keine Desktop- oder
Recording-Abhängigkeiten im Servercontainer installiert.

## 2. Einstellungen erzeugen

`https://DEINE-NEXTCLOUD` durch die Basisadresse deiner Nextcloud ersetzen,
gegebenenfalls inklusive Unterordner. Keine WebDAV- oder Freigabe-URL verwenden.

**Wenn cloudflared direkt auf dem Server läuft:**

```bash
python3 init_env.py --nextcloud-url https://DEINE-NEXTCLOUD
docker compose up -d --build
```

**Wenn cloudflared als Docker-Container läuft:**

Der Share-Dienst muss sich ein Docker-Netzwerk mit deinem vorhandenen
cloudflared-Container teilen. Den Netzwerknamen bei Bedarf so ermitteln
(`CLOUDFLARED_CONTAINER` durch den tatsächlichen Containernamen ersetzen):

```bash
docker ps --format 'table {{.Names}}\t{{.Image}}'
docker inspect CLOUDFLARED_CONTAINER --format '{{json .NetworkSettings.Networks}}'
```

Dann den Namen des bereits vorhandenen Netzwerks einsetzen:

```bash
python3 init_env.py --nextcloud-url https://DEINE-NEXTCLOUD --tunnel-network DEIN_NETZWERK
docker compose -f compose.yaml -f compose.tunnel.yaml up -d --build
```

`init_env.py` erzeugt eine `.env` mit Dateirechten `0600` und einem zufälligen
`VICE_SHARE_API_TOKEN`. Eine bestehende Datei wird nicht überschrieben. Den Token
aufbewahren und später in Vice eintragen. Niemals dein Nextcloud-App-Passwort als
diesen Token verwenden.

Standard ist `NEXTCLOUD_MEDIA_MODE=dav` für Nextcloud ab Version 29. Bei einer
älteren Nextcloud in `.env` `NEXTCLOUD_MEDIA_MODE=download` einstellen. Dieser
Modus verwendet `/s/TOKEN/download`; die direkte URL muss ohne Redirect oder
Anmeldeseite die Datei liefern. Nach einer Änderung den passenden Compose-Befehl
erneut ausführen.

`NEXTCLOUD_BASE_URL` muss aus dem Share-Container erreichbar sein. Auch eine
interne HTTP-Adresse deiner Nextcloud ist möglich, wenn Docker-Netzwerk und
Nextcloud-`trusted_domains` dazu passen. Token und öffentliche Freigaben müssen
von genau dieser Nextcloud stammen.

## 3. Bestehenden Cloudflare Tunnel erweitern

Im Cloudflare-Dashboard deinen bestehenden Tunnel öffnen und eine zusätzliche
öffentliche Anwendungsroute / einen Public Hostname anlegen:

| Einstellung | Wert |
|---|---|
| Subdomain | `gclips` |
| Domain | `kimonima.de` |
| Pfad | leer lassen |
| Service-Typ | `HTTP` |
| Service bei cloudflared auf dem Host | `localhost:8787` |
| Service bei cloudflared im gemeinsamen Docker-Netzwerk | `vice-share:8080` |

`localhost` innerhalb eines normalen cloudflared-Containers bezeichnet den
cloudflared-Container selbst. Dafür ist die zweite Variante vorgesehen.

Falls dein Tunnel über eine lokale Konfigurationsdatei verwaltet wird, die
entsprechende Regel **vor** der abschließenden Catch-all-Regel ergänzen; vorhandene
Regeln behalten:

```yaml
ingress:
  # Bestehende Regeln hier beibehalten.
  - hostname: gclips.kimonima.de
    service: http://vice-share:8080
  # Die bisherige Catch-all-Regel bleibt die letzte Regel.
  - service: http_status:404
```

Bei cloudflared auf dem Host ist der Service stattdessen
`http://localhost:8787`. Bei lokal verwalteten Tunneln muss außerdem der DNS-Eintrag
für `gclips.kimonima.de` auf den vorhandenen Tunnel zeigen. Eine im Dashboard
verwaltete veröffentlichte Route richtet den passenden DNS-Eintrag normalerweise
mit ein.

Die öffentlichen Clip- und Medienpfade müssen ohne Cloudflare-Access-Login oder
JavaScript-Challenge erreichbar sein, damit Discord sie abrufen kann. Die
schreibenden API-Endpunkte sind durch den API-Token des Share-Dienstes geschützt.
Bestehende Schutzregeln anderer Subdomains müssen dafür nicht geändert werden.

## 4. Erreichbarkeit prüfen

Auf dem Server:

```bash
docker compose ps
curl --fail http://127.0.0.1:8787/healthz
curl --fail https://gclips.kimonima.de/healthz
```

Erwartet wird jeweils `{"service": "vice-share", "api": 1}`. Die Startseite `/`
hat absichtlich keine Clip-Liste und antwortet mit 404. Bei Problemen:

```bash
docker compose logs --tail=80 vice-share
```

Für spätere Änderungen an Netzwerk/Container immer dieselbe Compose-Dateikombination
wie beim Start verwenden. Beispielsweise mit Docker-cloudflared:

```bash
docker compose -f compose.yaml -f compose.tunnel.yaml up -d --build
```

## 5. Vice anbinden

Erst wenn die öffentliche Health-URL funktioniert, auf dem Gaming-PC die neue
Version aus diesem Checkout installieren:

```bash
cd /home/exo/Coding/Vice
bash tools/install-local.sh
```

Unter **Einstellungen → Teilen**:

- Dienst: **Nextcloud mit Discord-Vorschau**.
- Bestehende Nextcloud-URL, Benutzername, App-Passwort und Clip-Ordner behalten.
- Share-Dienst-URL: **https://gclips.kimonima.de**.
- API-Token des Share-Dienstes: der Wert von `VICE_SHARE_API_TOKEN` in der `.env`.
- **Teilen kopiert eine Discord-Datei** ausgeschaltet lassen.
- Speichern und Vice bei angezeigter Aufforderung neu starten.

Beim Teilen prüft Vice zuerst den Share-Dienst, bereitet eine H.264/AAC-MP4 mit
Faststart und ein JPEG vor, lädt Original und Vorschau-Dateien zu Nextcloud hoch
und registriert den Clip. Anschließend wird `https://gclips.kimonima.de/c/…`
kopiert. Unveränderte Clips verwenden ihre vorhandenen Uploads und IDs erneut.
Es gibt für diese Vorschau kein festes 20-MB-Anhangslimit; Nextclouds eigene
Uploadlimits und die Anforderungen externer Player gelten weiterhin.

## 6. Mit einem Testclip abnehmen

1. Zuerst einen kurzen Clip teilen und den Link im Browser öffnen.
2. Den Link selbst in einen Discord-Testkanal posten und die Videowiedergabe sowie
   das Springen innerhalb des Clips prüfen.
3. Den Gaming-PC ausschalten und denselben Link auf einem anderen Gerät öffnen.
4. Den Share-Container neu starten und denselben Link nochmals öffnen.

Die SQLite-Dateien im Volume `share-data` sichern. `docker compose down` behält
das Volume; `docker compose down -v` würde die registrierten Links löschen. Videos
und Nextcloud-Freigaben werden weder durch lokale Clip-Löschung noch durch das
Entfernen eines Share-Eintrags gelöscht. Freigaben kannst du in Nextcloud
widerrufen; ein einzelner Host-Link kann zusätzlich über den authentifizierten
Endpunkt `DELETE /api/clips/ID` entfernt werden.

Die Metadaten und H.264/AAC-Kopie sind auf eine direkte Videovorschau ausgelegt.
Ob Discord einen Player anzeigt, muss mit der echten Domain getestet werden;
Anzeigeeinstellungen und Discord-Verarbeitung lassen sich nicht erzwingen.

## Cloudflare und Videotraffic

Die Videoantworten tragen `Cache-Control: no-store`; im Tunnel ist dafür keine
zusätzliche Cache-Everything-Regel vorgesehen. Das ist keine Freigabe für einen
bestimmten Cloudflare-Tarif: Cloudflare nennt für Video und große Dateien über
sein CDN gesonderte Dienste bzw. Bedingungen. Vor dauerhaftem Betrieb die
Anwendbarkeit auf dein Setup prüfen:

- [Tunnel-Routing](https://developers.cloudflare.com/tunnel/concepts/routing/)
- [Cloudflare CDN Service-Specific Terms](https://www.cloudflare.com/service-specific-terms-application-services/)
- [Öffentliche Nextcloud-WebDAV-Freigaben](https://docs.nextcloud.com/server/stable/developer_manual/client_apis/WebDAV/basic.html)

## Paket neu erstellen

Auf dem Entwicklungsrechner:

```bash
bash tools/package-share-host.sh
```

Das Archiv enthält nur Servercode, Docker-Dateien und diese Anleitung, keine
Konfiguration, API-Token, Nextcloud-Zugangsdaten oder eigenen Videos.
