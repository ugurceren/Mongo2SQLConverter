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
copy config.local.example.yaml config.local.yaml
```

`config.local.yaml` icinde MongoDB ve MSSQL baglanti bilgilerini duzenleyin.

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

## Yapi

- `core/inspect.py` — sema profilleme, DRDL/DDL
- `core/mongo.py` — Mongo baglantisi
- `core/mssql.py` — MSSQL baglantisi
- `core/settings.py` — config.yaml + config.local.yaml
- `app/main.py` — Streamlit arayuz
- `tools/infer_schema.py` — CLI

## Ayirma

Bu repoya **alınmayan** parcalar: `sync.py`, `flatten*.py`, `streams.py`, `scheduler/`, stream-spesifik SQL scriptleri. Sync icin SelimMongoDBtoSQL kullanilmaya devam eder.
