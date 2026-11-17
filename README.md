# billingo – Billingo számlázás ERPNext v16-hoz

Frappe/ERPNext app, ami az ERPNext értékesítési számláit a **Billingo** (billingo.hu) API-n keresztül
állítja ki: piszkozat a Billingóban mentéskor, végleges számla beküldéskor (NAV-jelentéssel a
Billingón át), sztornó, díjbekérő és előlegszámla a Vevői rendelésből, NAV-állapot visszaolvasása.

Eredeti fejlesztő: FOSS ERP (pavithra@fosserp.com). Licenc: MIT (`license.txt`).

## Telepítés

**Frappe Cloud:** Bench → Apps → Add App → GitHub → `Balinatorok/billingo_public`, ág: `main`
→ Deploy → a site-on Install App → *billingo*.

**Saját szerver:**

```bash
bench get-app https://github.com/Balinatorok/billingo_public --branch main
bench --site <site> install-app billingo
bench --site <site> migrate
```

Telepítéskor (és minden migrate-kor) az app létrehozza a szükséges egyéni mezőket
(Értékesítési számla, Vevői rendelés, Vevő, Költséghely) és a **Billingo Szamlatomb** doctype-ot.

## Beállítás (site config)

Frappe Cloudon: Site → Site Config. Saját szerveren: `bench --site <site> set-config <kulcs> <érték>`.

| Kulcs | Kötelező | Mi ez |
|---|---|---|
| `billingo_api_key` | igen | Billingo → Beállítások → API kulcs (a saját Billingo-fiókodé) |
| `billingo_document_block_id` | igen | Az alapértelmezett Billingo bizonylattömb ID-je (nem a számlaszám-előtag) |
| `billingo_bank_account_id_huf` / `_eur` / `_usd` | ha devizánként több bankszámlád van a Billingóban | Melyik Billingo-bankszámla kerüljön a számlára. Ha devizánként csak egy van, azt magától választja |
| `billingo_document_language` | nem | A számla nyelve, alap: `hu` |
| `billingo_foreign_document_language` | nem | Külföldi vevő számlájának nyelve, alap: `en` |

Az API-kulcs **csak a site configba** kerül, soha ne a kódba.

**Több számlatömb (pl. külön webshop-tömb):** hozz létre egy *Billingo Szamlatomb* rekordot a tömb
Billingo-azonosítójával, és állítsd be a Költséghelyen. Az onnan kiállított számlák abból a tömbből
mennek; üresen az alapértelmezett tömb.

**Vevő:** a számlához a Vevőnek legyen e-mail-címes kapcsolattartója — a Billingo erre küldi a számlát.

## Tesztek

```bash
python3 tests/billingo_offline/test_proforma_flow.py
```

Frappe és valódi Billingo-kapcsolat nélkül fut (memóriabeli Frappe + szkriptelt Billingo API).

## Contributing

This app uses `pre-commit` for code formatting and linting (ruff, eslint, prettier, pyupgrade):

```bash
cd apps/billingo
pre-commit install
```
