# Vinted Sniper

Asynchroniczny Zwiadowca Vinted (asyncio + httpx + Playwright): co kilka sekund sprawdza najnowsze oferty
w wybranej kategorii przez rotacyjne proxy IPRoyal, odrzuca sprzedane i spoza zakresu ceny, zbiera komplet
danych (opis, zdjęcia jako URL-e, sprzedawca, wysyłka) i wysyła alert e-mail. Dane ofert są gotowe do
przekazania modelowi AI (następny etap).

```bash
pip install -r sniper/requirements.txt
playwright install chromium
cp sniper/.env.example sniper/.env   # uzupełnij proxy, SMTP i filtry
python -m sniper
```

* Dokumentacja modułu: [`sniper/README.md`](sniper/README.md)
* Kontekst projektu, ustalenia o API Vinted i plan dalszych prac: [`CLAUDE.md`](CLAUDE.md)
  (ten sam tekst co `sniper/CONTEXT.md`; Claude Code wczytuje go automatycznie)
* Testy: `pip install pytest && python -m pytest sniper/tests`
