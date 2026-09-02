# Mongo2SQL Converter

MongoDB collection'larini tarayip olculere dayali **DRDL** ve **DDL** ureten arac.

[SelimMongoDBtoSQL](https://github.com/ugurceren/SelimMongoDBtoSQL) sync projesinden **ayri** bir urundur.

| SelimMongoDBtoSQL | Mongo2SQLConverter |
|-------------------|-------------------|
| Sabit stream'ler (hybrid_conversations, conversations) | Herhangi bir collection |
| Elle flatten + MERGE sync | Otomatik sema cikarimi |
| Zamanlanmis batch | DRDL / DDL uretimi |

## Kurulum

```powershell
cd "D:\Code and Business\Mongo2SQLConverter"
pip install -r requirements.txt
```

Baglanti bilgileri `config.yaml` icinde yoktur. Streamlit'te **Baglantilar** sayfasindan doldurun; kayit `config.local.yaml` dosyasina yazilir (git'e eklenmez). Elle yazmak icin:

```powershell
copy config.local.example.yaml config.local.yaml
```

Ornek dosyada `mydb`, `user`, `srv\INSTANCE` gibi yer tutucular vardir; gercek sifre ve sunucu adini kendiniz yazin.

## Kullanim

**Streamlit:**

```powershell
python run.py
```

**CLI (DRDL):**

```powershell
python tools/infer_schema.py --collection conversations --out-drdl conversations.drdl
python tools/infer_schema.py --collection conversations --sample 5000 --out-ddl draft.sql
python tools/infer_schema.py --from-file export.json --collection mycol --out-drdl out.drdl
```

## Aktarimi daraltma

**SQL aktarimi** sayfasinda koleksiyonun tamamini yazmak zorunlu degil.

**Tarih araligi:** Profilleme sirasinda bulunan tarih tipli alanlar (`createdAt`, `updatedAt` gibi) listelenir; birini secip baslangic ve bitis gunu verirsiniz. Bitis gunu dahildir. Gunler "Yerel saat" ya da "UTC" olarak yorumlanir ve Mongo'ya UTC olarak gider. Secilen alanda index yoksa uyari cikar; hizlandirmak icin:

```javascript
db.conversations.createIndex({ createdAt: 1 })
```

Aralik yalnizca yazmayi degil profillemeyi de daraltir, boylece kolon genisliklerini o donemin verisi belirler. Dizi elemani icindeki tarihlere gore filtreleme desteklenmez.

**Kolon secimi:** Kolonlar kartinda planin her kolonu ve alt tablosu tek tek kapatilabilir. `mongo_id`, alt tablo anahtarlari ve dizi sira kolonlari her zaman aktarilir. Bir alt tablonun butun kolonlari kapatilirsa o tablo hic olusturulmaz. Liste koleksiyonun profilinden geldigi icin once **Kolonlari getir** (ya da **Plani hazirla**) gerekir.

Iki secim de `config.local.yaml` icine koleksiyon basina yazilir, uygulama yeniden acildiginda geri yuklenir.

**Dikkat:** Artimli senkron ile tarih araligini birlikte kullanirken belgeler `_id` sirasiyla okunur ve isaret yalnizca filtreden gecen son belgeye ilerler; aralik disinda kalan daha buyuk `_id`'ler sonraki kosularda bir daha okunmaz. Donem bazli yukleme icin tam senkron daha guvenlidir.

## Yapi

- `core/inspect.py` — sema profilleme, DRDL/DDL
- `core/transfer.py` — plana gore flatten + MSSQL'e yazma
- `core/mongo.py` — Mongo baglantisi
- `core/mssql.py` — MSSQL baglantisi
- `core/settings.py` — config.yaml + config.local.yaml
- `app/main.py` — Streamlit kabugu (gezinme, durum)
- `app/ui/theme.py` — stil ve ortak arayuz parcalari
- `app/ui/services.py` — arayuzun kullandigi baglanti/profil yardimcilari
- `app/ui/discovery.py` — Sema kesfi sayfasi (SQL gerekmez)
- `app/ui/transfer.py` — SQL aktarimi sayfasi
- `app/ui/connections.py` — Baglantilar sayfasi
- `tools/infer_schema.py` — CLI

## Ayirma

Bu repoya **alınmayan** parcalar: `sync.py`, `flatten*.py`, `streams.py`, `scheduler/`, stream-spesifik SQL scriptleri. Sync icin SelimMongoDBtoSQL kullanilmaya devam eder.
