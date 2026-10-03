"""Anty-Duplikator: pamięć ostatnich ID w RAM (zastępuje PostgreSQL z db_management.py)."""
from collections import deque


class RecentIds:
    """Bufor kołowy ostatnich ID - deque(maxlen) + set dla sprawdzania w O(1).

    Wszystkie operacje są synchroniczne i trwają mikrosekundy, więc nie blokują pętli zdarzeń.
    Pamięć musi być kilka razy większa niż strona katalogu (96 ofert): oferta, która wypadnie
    z bufora, a nadal jest na stronie, zostałaby uznana za nową. Nie używamy progu "ID <= X = stare",
    bo Vinted nadaje ID przy tworzeniu ogłoszenia, nie przy publikacji - szkic opublikowany później
    ma niższe ID niż oferty już widziane i byłby po cichu pominięty.
    """

    def __init__(self, maxlen=500):
        self._order = deque(maxlen=maxlen)
        self._ids = set()

    def __contains__(self, item_id):
        return item_id in self._ids

    def __len__(self):
        return len(self._order)

    @property
    def maxlen(self):
        return self._order.maxlen

    def add(self, item_id):
        """Zapamiętuje ID. Zwraca True, jeśli było nowe."""
        if item_id in self._ids:
            return False
        if len(self._order) == self._order.maxlen:
            self._ids.discard(self._order.popleft())
        self._order.append(item_id)
        self._ids.add(item_id)
        return True

    def snapshot(self):
        return list(self._order)
