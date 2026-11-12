"""Devises proposees dans l'interface (codes ISO 4217 acceptes par Google Flights / SerpApi),
avec leur nom en francais pour l'autocompletion."""
from __future__ import annotations

CURRENCIES: dict[str, str] = {
    "NOK": "Couronne norvégienne", "SEK": "Couronne suédoise", "DKK": "Couronne danoise",
    "ISK": "Couronne islandaise", "EUR": "Euro", "USD": "Dollar américain", "GBP": "Livre sterling",
    "CHF": "Franc suisse", "PLN": "Złoty polonais", "CZK": "Couronne tchèque", "HUF": "Forint hongrois",
    "RON": "Leu roumain", "BGN": "Lev bulgare", "RSD": "Dinar serbe", "TRY": "Livre turque",
    "UAH": "Hryvnia ukrainienne", "GEL": "Lari géorgien", "KZT": "Tenge kazakh",
    "CAD": "Dollar canadien", "MXN": "Peso mexicain", "BRL": "Réal brésilien", "ARS": "Peso argentin",
    "CLP": "Peso chilien", "COP": "Peso colombien", "PEN": "Sol péruvien", "BOB": "Boliviano",
    "UYU": "Peso uruguayen", "PYG": "Guaraní paraguayen", "CRC": "Colón costaricien",
    "DOP": "Peso dominicain", "AUD": "Dollar australien", "NZD": "Dollar néo-zélandais",
    "JPY": "Yen japonais", "CNY": "Yuan chinois", "HKD": "Dollar de Hong Kong", "TWD": "Dollar de Taïwan",
    "KRW": "Won sud-coréen", "SGD": "Dollar de Singapour", "THB": "Baht thaïlandais",
    "MYR": "Ringgit malaisien", "IDR": "Roupie indonésienne", "PHP": "Peso philippin",
    "VND": "Dông vietnamien", "INR": "Roupie indienne", "PKR": "Roupie pakistanaise",
    "LKR": "Roupie srilankaise", "AED": "Dirham des Émirats", "SAR": "Riyal saoudien",
    "QAR": "Riyal qatarien", "ILS": "Shekel israélien", "EGP": "Livre égyptienne",
    "MAD": "Dirham marocain", "TND": "Dinar tunisien", "ZAR": "Rand sud-africain",
    "KES": "Shilling kényan", "NGN": "Naira nigérian",
}
