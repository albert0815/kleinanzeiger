---
name: search-watches
description: Suchaufträge auf Kleinanzeigen.de einrichten, anzeigen und löschen; neue Treffer werden per Telegram gemeldet.
allowed-tools: create_search_watch list_search_watches delete_search_watch
---

## Suchaufträge

Der Nutzer kann sich über neue Angebote auf Kleinanzeigen benachrichtigen lassen, z. B.
„Sag mir Bescheid, wenn jemand ein Lego Technic 42056 in der Nähe von 80331 anbietet“.
Der Bot prüft die Suchaufträge jeden Abend zwischen 20 und 21 Uhr und schickt neue Treffer an den Chat, in
dem der Auftrag angelegt wurde.

1. **Anlegen**: `create_search_watch(query=..., location=..., radius_km=..., min_price=..., max_price=...)`.
   - `query` sind die Suchwörter, so wie man sie bei Kleinanzeigen eintippt. Kurz halten,
     keine ganzen Sätze.
   - Für eine Umkreissuche `location` (am besten Postleitzahl, sonst Ort) und `radius_km` setzen.
     Möglich sind 5, 10, 20, 30, 50, 100, 150 und 200 km. Ohne Angabe: 30 km.
   - Ohne `location` wird deutschlandweit gesucht. Ist unklar, ob der Nutzer im Umkreis oder
     deutschlandweit suchen will, frage nach.
   - Preise nur setzen, wenn der Nutzer sie nennt.
   - Nenne danach kurz, was angelegt wurde. Sage, dass nur neue Treffer gemeldet werden.
2. **Anzeigen**: `list_search_watches()`. Der Nutzer kann auch /suchauftraege verwenden.
3. **Löschen**: `delete_search_watch(watch_id=...)`. Passen mehrere Aufträge, frage nach,
   welcher gemeint ist.

Ändern geht durch Löschen und neu Anlegen. Sage das, wenn danach gefragt wird.
