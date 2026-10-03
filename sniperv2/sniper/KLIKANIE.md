# Jak robić automatyczne klikanie w Vinted (na podstawie outerHTML z F12)

Krótka instrukcja dla kolejnego czatu. Wzorzec sprawdzony na przycisku „Kup teraz” → checkout
(`sniper/account_session.py`, `sniper/buyer.py`). Sterujemy prawdziwą przeglądarką przez Playwright,
z zalogowanej sesji konta (bez proxy). **Bot nigdy nie klika „Zapłać” ani nie obchodzi captchy** —
płatność i suwak zostają po stronie człowieka.

## Krok 1: zdobądź outerHTML przycisku
W przeglądarce (zalogowany): F12 → Elementy → prawy klik na przycisk → Kopiuj → „Kopiuj element (outerHTML)”.
Przykład:
```html
<button type="button" class="web_ui__Button__button ... web_ui__Button__primary" data-testid="item-buy-button">
  <span class="web_ui__Button__content"><span class="web_ui__Button__label">Kup teraz</span></span>
</button>
```

## Krok 2: zrób z tego STABILNY selektor (kolejność preferencji)
1. **`data-testid`** — najlepszy. `page.locator('[data-testid="item-buy-button"]')`. Vinted dodaje go celowo,
   przetrwa zmiany wyglądu.
2. **Stabilne klasy semantyczne** — np. kontener akcji + rola przycisku:
   `.details-list--actions button.web_ui__Button__primary`.
3. **Tekst na przycisku** — `page.get_by_role("button", name=re.compile("kup teraz", re.I))`.

**NIE używaj jako głównych** JS path ani XPath z F12
(`#sidebar > div:nth-child(1) > … > button[1]`, `//*[@id="sidebar"]/div[2]/…`). Są kruche:
- klasy z losowym hashem (`_item-details-module-scss-module__ymptpG__…`) zmieniają się przy każdym buildzie Vinted,
- `div:nth-child(…)` / `div[10]` psują się, gdy dojdzie jeden element.
Używaj ich najwyżej jako inspiracji do wyłuskania trwałego fragmentu (jak w punkcie 2).

Dawaj 2-3 selektory z zapasem i loguj, który trafił (patrz `BUY_NOW_SELECTORS` w `account_session.py`).

## Krok 3: POCZEKAJ, aż strona się załaduje (najważniejsze!)
Przycisk jest w HTML, zanim React podepnie obsługę kliknięcia (hydracja SPA). Klik „za wcześnie” trafia
w martwy przycisk i NIC się nie dzieje (żadnego błędu). Dlatego:
- `await page.goto(url, wait_until="domcontentloaded")`, potem zamknij baner ciastek (`_dismiss_consent`).
- `await page.wait_for_load_state("networkidle", timeout=10000)` przed klikiem.
- **Ponawiaj klik do 2-3 razy**: po kliknięciu sprawdzaj przez ~8 s, czy coś zareagowało
  (zmiana URL, zapytanie API, nowa karta). Jak nie — kliknij jeszcze raz. Pierwszy klik bywa ślepy.

## Krok 4: potwierdź reakcję i złap dane (bez zgadywania endpointów)
- Nasłuchuj odpowiedzi na CAŁYM kontekście (`context.on("response", …)`) — łapie też nową kartę.
- Rozpoznaj właściwą odpowiedź po URL zweryfikowanym realnym ruchem (F12), np.
  `/api/v2/purchases/{id}/checkout`. **Nowych endpointów nie zgaduj** — poproś użytkownika o cURL z F12.
- Czekaj na `wait_for_url(lambda u: "/checkout" in u)` jako sygnał wejścia na ekran.
- Przy niepowodzeniu rób diagnostykę: zrzut ekranu (`page.screenshot`), treść modalu/toastu, liczbę kart
  (patrz `_dump_failure`). To od razu pokazuje przyczynę (baner ciastek, panel dostawy, komunikat Vinted).

## Gdzie to jest w kodzie
- `sniper/account_session.py` — steruje przeglądarką: `_click_buy_now` (selektory), `open_item` +
  `_dismiss_consent` + `wait_for_load_state` + pętla ponawiania w `buy_now_and_get_checkout`.
- `sniper/buyer.py` — dyryguje (decyzja, limity, rejestr `bought.jsonl`); woła metody z account_session.
- Test: `python -m sniper.buyer "<link do oferty>" --ignore-limits` (dochodzi do checkoutu, NIE płaci).

## Zasady, których trzymamy się twardo
- **Bot nie płaci.** Klik „Zapłać” i suwak potwierdzenia robi człowiek. Nie automatyzujemy finalizacji
  płatności ani nie obchodzimy captchy / zabezpieczeń anty-bot (datadome).
- **Sesja konta tylko z domowego IP, nigdy przez proxy IPRoyal.**
- **Nie commitować sekretów** (`my_headers.txt`, ciastka, tokeny, profil przeglądarki — są w `.gitignore`).
- Selektory i endpointy zmieniamy tylko na podstawie realnego HTML/ruchu z F12 u użytkownika, nie z głowy.
