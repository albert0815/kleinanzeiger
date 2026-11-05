---
name: search-watches
description: Suchaufträge auf Kleinanzeigen.de einrichten, anzeigen und löschen; neue Treffer werden per Telegram gemeldet.
allowed-tools: create_search_watch update_search_watch list_search_watches delete_search_watch
---

## Suchaufträge

Der Nutzer kann sich über neue Angebote auf Kleinanzeigen benachrichtigen lassen, z. B.
„Sag mir Bescheid, wenn jemand ein Lego Technic 42056 in der Nähe von 80331 anbietet“.
Der Bot prüft die Suchaufträge jeden Abend zwischen 20 und 21 Uhr und schickt neue Treffer an den Chat, in
dem der Auftrag angelegt wurde.

Der Nutzer kann Aufträge auch per Button löschen, ohne dass du es mitbekommst. Der Bot
schreibt dir das dann als Hinweis vor die nächste Nachricht. Bevor du sagst, welche oder wie
viele Aufträge es gibt, rufe `list_search_watches()` auf; verlass dich nicht auf den
Gesprächsverlauf. „Wieder anlegen“ nach dem Löschen heißt: denselben Auftrag neu anlegen.

1. **Anlegen**: `create_search_watch(query=..., location=..., radius_km=..., min_price=..., max_price=...)`.
   - `query` sind die Suchwörter, so wie man sie bei Kleinanzeigen eintippt. Kurz halten,
     keine ganzen Sätze.
   - Für eine Umkreissuche `location` (am besten Postleitzahl, sonst Ort) und `radius_km` setzen.
     Möglich sind 5, 10, 20, 30, 50, 100, 150 und 200 km. Ohne Angabe: 30 km.
   - Ohne `location` wird deutschlandweit gesucht. Ist unklar, ob der Nutzer im Umkreis oder
     deutschlandweit suchen will, frage nach.
   - Preise nur setzen, wenn der Nutzer sie nennt.
   - Der Bot zeigt danach selbst die aktuellen Treffer mit Link. Bestätige nur kurz, was
     angelegt wurde, und zähle keine Treffer auf.
2. **Ändern**: `update_search_watch(watch_id=..., ...)` mit nur den Angaben, die sich ändern.
   - Korrigiert der Nutzer einen gerade angelegten Auftrag („Nein, nicht deutschlandweit, um
     Gerolsbach“, „lieber 50 km“, „bis 100 €“), ändere diesen Auftrag. Lege keinen zweiten an.
   - Für einen anderen Auftrag hole die ID mit `list_search_watches()`.
   - Deutschlandweit statt Umkreis: `nationwide=true`. Ein neuer Ort schaltet auf Umkreissuche.
   - Preisgrenzen entfernen: `remove_price_limits=true`.
   - Auch hier zeigt der Bot danach die aktuellen Treffer.
3. **Anzeigen**: `list_search_watches()`. Der Nutzer kann auch /suchauftraege verwenden.
4. **Löschen**: `delete_search_watch(watch_id=...)`. Passen mehrere Aufträge, frage nach,
   welcher gemeint ist.
